#!/usr/bin/env python3
"""Generate the website icons from a single master SVG.

Usage (from repo root):
    .venv/Scripts/python.exe scripts/generate_icons.py           # write
    .venv/Scripts/python.exe scripts/generate_icons.py --check   # verify only

Master:  assets/icon-master.svg
    A 1024x1024 SVG. The background must be a WHITE ROUNDED SQUARE
    (rx ~ 245, spanning the full canvas) with the artwork on top —
    the transparent corners outside the squircle are what give the
    icons their app-icon shape everywhere downstream. If the master
    is missing, a red-circle placeholder is written automatically;
    drop in the real artwork and re-run.

--check mode regenerates every target in memory and byte-compares it
against the committed file, exiting nonzero on any drift. This is what
CI runs (see .github/workflows/icons-freshness-check.yml) so the repo
can never hold a hand-edited or stale generated icon.

Rendering: resvg (resvg-py) for SVG -> PNG fidelity at every size.
Containers: Pillow for multi-size .ico and .icns.

Deps:
    Pillow (core dependency), resvg-py (dev extra):
    uv sync --extra dev

Outputs (7 files, all measured against the originals):
  website/static/img/logo.png                     1772x1799 black-frame wordmark
  website/static/img/nous-logo.png                150x150 on white (opaque)
  website/static/img/favicon-16x16.png            16x16
  website/static/img/favicon-32x32.png            32x32
  website/static/img/apple-touch-icon.png         180x180
  website/static/img/favicon.ico                  16,32,48
  website/static/img/favicon.svg                  copy of the master
"""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

from PIL import Image

try:
    import resvg_py
except ImportError:
    sys.exit(
        "resvg-py is missing. Install it with:\n"
        "  uv sync --extra dev"
    )

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "assets" / "icon-master.svg"

# The squircle corner radius measured on the real 1024 icon (~245px).
SQUIRCLE_RADIUS = 245

PLACEHOLDER = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <rect x="0" y="0" width="1024" height="1024" rx="{SQUIRCLE_RADIUS}" fill="#ffffff"/>
  <circle cx="512" cy="512" r="420" fill="#e34c4c"/>
</svg>
'''

# (relpath, kind, arg)
TARGETS: list[tuple[str, str, object]] = [
    ("website/static/img/logo.png", "logo", None),
    ("website/static/img/nous-logo.png", "png_white", 150),
    ("website/static/img/favicon-16x16.png", "png", 16),
    ("website/static/img/favicon-32x32.png", "png", 32),
    ("website/static/img/apple-touch-icon.png", "png", 180),
    ("website/static/img/favicon.ico", "ico", [16, 32, 48]),
    ("website/static/img/favicon.svg", "svg_copy", None),
]


def ensure_master(check: bool = False) -> Path:
    if not MASTER.exists():
        if check:
            sys.exit(
                f"[check] {MASTER.relative_to(ROOT)} is missing — "
                "cannot verify generated icons without a master."
            )
        MASTER.write_text(PLACEHOLDER, encoding="utf-8")
        print(f"[master] wrote placeholder: {MASTER.relative_to(ROOT)}")
    return MASTER


def render(size: int, *, background: str | None = None) -> Image.Image:
    """Render the master to an RGBA PNG of `size`x`size`."""
    data = resvg_py.svg_to_bytes(
        svg_path=str(MASTER), width=size, height=size, background=background
    )
    return Image.open(io.BytesIO(data)).convert("RGBA")


def build_logo_image() -> Image.Image:
    """1772x1799: black frame (~14-17px) + white interior + art at ~1.7x.

    Measured from the real logo.png: border L/R 14, T 17, B 16; white
    interior spans (15,18)..(1758,1784) = 1743x1766.
    """
    from PIL import ImageDraw

    W, H = 1772, 1799
    art_w, art_h = 1743, 1766
    border = 14
    top, bottom = 17, 16
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((0, 0, W - 1, H - 1), radius=2, fill=(0, 0, 0, 255))
    draw.rectangle(
        (border, top, W - 1 - border, H - 1 - bottom),
        fill=(255, 255, 255, 255),
    )

    # Art: render the master square at ~1.7x, tiny vertical stretch to fill.
    art = render(art_w).resize((art_w, art_h), Image.LANCZOS)
    canvas.alpha_composite(art, (border, top))
    return canvas


def target_bytes(kind: str, arg: object) -> bytes:
    """Produce the exact bytes for one target. Shared by write + check."""
    if kind == "svg_copy":
        return MASTER.read_bytes()

    buf = io.BytesIO()
    if kind == "png":
        render(arg).save(buf, "PNG")
    elif kind == "png_white":
        render(arg, background="#ffffff").save(buf, "PNG")
    elif kind == "ico":
        img = render(max(arg))
        img.save(buf, format="ICO", sizes=[(s, s) for s in arg])
    elif kind == "logo":
        build_logo_image().save(buf, "PNG")
    else:
        raise ValueError(f"unknown kind {kind!r}")
    return buf.getvalue()


def cmd_write() -> None:
    ensure_master()
    written = 0
    for rel, kind, arg in TARGETS:
        path = ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_bytes(target_bytes(kind, arg))
            written += 1
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"  !! {rel}: FAILED ({exc})")
    print(f"[ok] wrote {written}/{len(TARGETS)} files from {MASTER.name}")

    print("\n[verify]")
    for rel, kind, arg in TARGETS:
        path = ROOT / rel
        try:
            if kind == "svg_copy":
                print(f"  {rel}: {path.stat().st_size} bytes SVG")
                continue
            im = Image.open(path)
            if kind == "ico":
                sizes = []
                try:
                    for i in range(im.n_frames):
                        im.seek(i)
                        sizes.append(im.size)
                except Exception:
                    sizes = [im.size]
                print(f"  {rel}: ICO {sorted(set(sizes))}")
            else:
                print(f"  {rel}: {im.format} {im.size}")
        except Exception as exc:
            print(f"  {rel}: VERIFY FAILED ({exc})")


def cmd_check() -> int:
    ensure_master(check=True)
    drifted: list[str] = []
    for rel, kind, arg in TARGETS:
        path = ROOT / rel
        if not path.exists():
            drifted.append(f"{rel}: MISSING (expected generated file)")
            continue
        try:
            expected = target_bytes(kind, arg)
        except Exception as exc:  # noqa: BLE001
            drifted.append(f"{rel}: REGENERATE FAILED ({exc})")
            continue
        actual = path.read_bytes()
        if actual != expected:
            drifted.append(f"{rel}: drift ({len(actual)} bytes on disk vs {len(expected)} generated)")

    if not drifted:
        print(f"[ok] all {len(TARGETS)} generated icons match {MASTER.name}")
        return 0

    print(f"[check] {len(drifted)} file(s) out of sync with {MASTER.name}:")
    for line in drifted:
        print(f"  - {line}")
    print(
        "\nFix: run `.venv/Scripts/python.exe scripts/generate_icons.py` "
        "and commit the regenerated files."
    )
    return 1


def main() -> None:
    if "--check" in sys.argv:
        sys.exit(cmd_check())
    cmd_write()


if __name__ == "__main__":
    main()
