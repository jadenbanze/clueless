"""Generates the Clueless app icon as .icns.

Drawn procedurally so the source is reviewable and the icon can be regenerated
without a design tool. The mark is an open ring with one dot: a C, and a
meeting that stays on the machine. Flat tile, no gradient. The same geometry
is drawn in site/logo.svg.
"""

from pathlib import Path
import subprocess
import shutil

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "overlay" / "build"
ICONSET = OUT / "Clueless.iconset"
SIZE = 1024


# Matches overlay/tokens.css: --surface-solid and --fg.
TILE = (28, 28, 33, 255)
MARK = (236, 236, 239, 255)


def rounded_tile(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.223), fill=TILE
    )
    return img


def mark(img: Image.Image) -> Image.Image:
    draw = ImageDraw.Draw(img)
    size = img.width
    cx = cy = size / 2
    radius = size * 0.27
    stroke = max(2, int(round(size * 0.085)))
    box = [cx - radius, cy - radius, cx + radius, cy + radius]
    # Pillow measures clockwise from 3 o'clock. The gap around 0° holds the dot.
    draw.arc(box, start=42, end=318, fill=MARK, width=stroke)
    dot_r = stroke * 0.9
    dot = cx + radius
    draw.ellipse([dot - dot_r, cy - dot_r, dot + dot_r, cy + dot_r], fill=MARK)
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True)

    master = mark(rounded_tile(SIZE))

    # Every size Apple expects in an iconset.
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = base * scale
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            master.resize((px, px), Image.LANCZOS).save(ICONSET / name)

    icns = OUT / "Clueless.icns"
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(icns)], check=True)
    shutil.rmtree(ICONSET)
    print(f"wrote {icns} ({icns.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
