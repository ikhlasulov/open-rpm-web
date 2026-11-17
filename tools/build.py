#!/usr/bin/env python3
"""Build Open RPM for the smallest possible first load.

What it does:
  * subsets fonts/ubuntu.ttf into latin + cyrillic WOFF2 files;
  * inlines style.css and the JS bundle into index.html (between markers);
  * regenerates sw.js with a content hash as the cache version.

Usage:
  python3 tools/build.py            build in place
  python3 tools/build.py --check    fail if generated files are out of date

Font subsetting needs fonttools and brotli:
  python3 -m venv .venv
  .venv/bin/pip install -r tools/requirements.txt
  .venv/bin/python tools/build.py
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

FONT_SOURCE = ROOT / "fonts" / "ubuntu.ttf"
LATIN_FONT = ROOT / "fonts" / "ubuntu-latin.woff2"
CYRILLIC_FONT = ROOT / "fonts" / "ubuntu-cyrillic.woff2"

# Must match the unicode-range declarations in style.css.
LATIN_RANGES = [(0x0000, 0x00FF), (0x2000, 0x206F), (0x20AC, 0x20AC)]
CYRILLIC_RANGES = [(0x0400, 0x045F)]

JS_SOURCES = ("translations.js", "answers.js", "script.js")
TEXT_SOURCES = ("index.html", "style.css", "translations.js", "answers.js", "script.js")

CSS_MARKERS = re.compile(r"(<!-- build:css -->)(.*?)(<!-- /build:css -->)", re.S)
JS_MARKERS = re.compile(r"(<!-- build:js -->)(.*?)(<!-- /build:js -->)", re.S)

CACHE_VERSION_PLACEHOLDER = "__CACHE_VERSION__"


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def expand(ranges: list[tuple[int, int]]) -> list[int]:
    return [code for start, end in ranges for code in range(start, end + 1)]


def required_characters() -> set[int]:
    """Every non-space character that can end up on screen."""
    text = "".join(read(name) for name in TEXT_SOURCES)
    return {ord(char) for char in text if not char.isspace()}


def build_fonts() -> dict[str, bytes]:
    try:
        from fontTools import subset
    except ImportError:
        return {}

    subsets = {"ubuntu-latin.woff2": LATIN_RANGES,
               "ubuntu-cyrillic.woff2": CYRILLIC_RANGES}
    output: dict[str, bytes] = {}

    for filename, ranges in subsets.items():
        options = subset.Options()
        options.flavor = "woff2"
        # Hinting is not needed for modern renderers and costs ~2x the bytes.
        options.hinting = False
        # Kerning keeps the UI typography intact; ligatures and the name table
        # are dead weight for a web-only font.
        options.layout_features = ["kern"]
        options.name_IDs = []
        options.glyph_names = False
        options.notdef_outline = True
        options.recalc_timestamp = False

        font = subset.load_font(str(FONT_SOURCE), options)
        subsetter = subset.Subsetter(options=options)
        subsetter.populate(unicodes=expand(ranges))
        subsetter.subset(font)

        buffer = io.BytesIO()
        subset.save_font(font, buffer, options)
        font.close()
        output[filename] = buffer.getvalue()

    return output


def verify_fonts(fonts: dict[str, bytes]) -> list[str]:
    """Return characters that no subset can render."""
    from fontTools.ttLib import TTFont

    covered: set[int] = set()
    for data in fonts.values():
        font = TTFont(io.BytesIO(data))
        cmap = font.getBestCmap() or {}
        covered.update(cmap.keys())
        font.close()

    missing = sorted(code for code in required_characters() if code not in covered)
    return [f"U+{code:04X} {chr(code)!r}" for code in missing]


def render_html() -> str:
    html = read("index.html")
    css = read("style.css").strip()
    bundle = "\n;\n".join(read(name).strip() for name in JS_SOURCES)

    if "</script" in bundle.lower():
        raise SystemExit("Refusing to inline JS that contains </script>.")

    if not CSS_MARKERS.search(html):
        raise SystemExit("index.html: build:css markers not found.")
    if not JS_MARKERS.search(html):
        raise SystemExit("index.html: build:js markers not found.")

    html = CSS_MARKERS.sub(
        lambda m: f"{m.group(1)}\n<style>\n{css}\n</style>\n{m.group(3)}", html, count=1
    )
    html = JS_MARKERS.sub(
        lambda m: f"{m.group(1)}\n<script>\n{bundle}\n</script>\n{m.group(3)}", html, count=1
    )
    return html


def make_version(html: str) -> str:
    """Hash every input whose change must invalidate the service worker cache.

    Font *outputs* are not hashed: subsetting can differ between fonttools
    versions, while the rendered result is the same. The TTF source and the
    build script are hashed instead, so the version stays reproducible.
    """
    digest = hashlib.sha256()
    digest.update(html.encode("utf-8"))
    digest.update((TOOLS / "sw.template.js").read_bytes())
    digest.update(Path(__file__).read_bytes())
    digest.update(FONT_SOURCE.read_bytes())

    # Images and icons are cached by URL, so any change there must bump the
    # service worker version as well.
    for pattern in ("images/*", "favicon/*"):
        for path in sorted(ROOT.glob(pattern)):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())

    return digest.hexdigest()[:16]


def render_sw(version: str) -> str:
    template = (TOOLS / "sw.template.js").read_text(encoding="utf-8")
    return template.replace(CACHE_VERSION_PLACEHOLDER, version)


def gzip_size(data: bytes) -> int:
    return len(gzip.compress(data, 9))


def format_size(size: int) -> str:
    return f"{size / 1024:.1f} KB"


def report(outputs: dict[str, bytes]) -> None:
    print("\nGenerated files (raw / gzip):")
    for name, data in outputs.items():
        print(f"  {name:<28} {format_size(len(data)):>9} / {format_size(gzip_size(data)):>9}")

    base = ("index.html", "fonts/ubuntu-latin.woff2")
    base_raw = sum(len(outputs[name]) for name in base)
    base_gzip = sum(gzip_size(outputs[name]) for name in base)

    first_image = (ROOT / "images" / "1.svg").read_bytes()

    print(
        f"\nFirst load without a matrix: {format_size(base_raw)} / "
        f"{format_size(base_gzip)} gzip"
    )
    print(
        f"First load with matrix 1:    {format_size(base_raw + len(first_image))} / "
        f"{format_size(base_gzip + gzip_size(first_image))} gzip"
    )


def verify_existing_fonts() -> list[str] | None:
    try:
        import fontTools.ttLib  # noqa: F401
    except ImportError:
        print("warning: fonttools not installed, skipping the glyph check", file=sys.stderr)
        return None

    fonts = {path.name: path.read_bytes() for path in (LATIN_FONT, CYRILLIC_FONT)}
    return verify_fonts(fonts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="exit with status 1 if generated files are stale")
    args = parser.parse_args()

    html = render_html()
    version = make_version(html)
    sw = render_sw(version)

    if args.check:
        if not LATIN_FONT.exists() or not CYRILLIC_FONT.exists():
            print("error: WOFF2 fonts are missing; run python3 tools/build.py", file=sys.stderr)
            return 1

        missing = verify_existing_fonts()
        if missing:
            print("error: fonts lack characters used by the UI:", file=sys.stderr)
            print("  " + ", ".join(missing), file=sys.stderr)
            print("Run python3 tools/build.py to regenerate them.", file=sys.stderr)
            return 1

        expected = {"index.html": html.encode("utf-8"), "sw.js": sw.encode("utf-8")}
        stale = [name for name, data in expected.items()
                 if not (ROOT / name).exists() or (ROOT / name).read_bytes() != data]
        if stale:
            print("Build is out of date: " + ", ".join(stale), file=sys.stderr)
            print("Run: python3 tools/build.py", file=sys.stderr)
            return 1

        print("Build is up to date.")
        return 0

    fonts = build_fonts()
    if not fonts:
        if not (LATIN_FONT.exists() and CYRILLIC_FONT.exists()):
            print("error: fonttools/brotli are required to create the WOFF2 files",
                  file=sys.stderr)
            print("Install them: pip install -r tools/requirements.txt", file=sys.stderr)
            return 1

        print("warning: fonttools/brotli not installed, keeping existing WOFF2 files",
              file=sys.stderr)
        fonts = {path.name: path.read_bytes() for path in (LATIN_FONT, CYRILLIC_FONT)}
    else:
        missing = verify_fonts(fonts)
        if missing:
            print("error: fonts lack characters used by the UI:", file=sys.stderr)
            print("  " + ", ".join(missing), file=sys.stderr)
            return 1

    outputs: dict[str, bytes] = {
        "index.html": html.encode("utf-8"),
        "sw.js": sw.encode("utf-8"),
        **{f"fonts/{name}": data for name, data in fonts.items()},
    }

    for name, data in outputs.items():
        path = ROOT / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    report(outputs)
    print(f"\nCache version: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
