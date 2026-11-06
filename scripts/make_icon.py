"""Generates the Clueless app icon as .icns.

Drawn procedurally so the source is reviewable and the icon can be regenerated
without a design tool. Shape: a rounded glass tile with a speech glyph, which
stays legible at 16px in the Dock and Finder.
"""

from pathlib import Path
import subprocess
import shutil

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "overlay" / "build"
ICONSET = OUT / "Clueless.iconset"
SIZE = 1024


def rounded_tile(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Vertical gradient, dark navy to indigo — reads as "glass" rather than flat.
    top, bottom = (32, 40, 62), (18, 22, 38)
    for y in range(size):
        t = y / size
        colour = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (size, y)], fill=colour + (255,))

    # Rounded mask.
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.225), fill=255
    )
    img.putalpha(mask)

    # Specular highlight along the top edge, echoing the overlay's glass rim.
    gloss = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(gloss).rounded_rectangle(
        [int(size * 0.06), int(size * 0.045), int(size * 0.94), int(size * 0.46)],
        radius=int(size * 0.18),
        fill=(255, 255, 255, 20),
    )
    return Image.alpha_composite(img, gloss)


def speech_glyph(img: Image.Image) -> Image.Image:
    draw = ImageDraw.Draw(img)
    size = img.width
    accent = (122, 168, 255, 255)

    # Speech bubble.
    left, right = int(size * 0.26), int(size * 0.74)
    top, bottom = int(size * 0.31), int(size * 0.63)
    radius = int(size * 0.09)
    draw.rounded_rectangle([left, top, right, bottom], radius=radius, fill=accent)

    # Tail.
    draw.polygon(
        [
            (int(size * 0.40), bottom - int(size * 0.02)),
            (int(size * 0.40), int(size * 0.72)),
            (int(size * 0.52), bottom - int(size * 0.02)),
        ],
        fill=accent,
    )

    # Three dots: "listening", and legible when the icon is tiny.
    cy = (top + bottom) // 2
    r = int(size * 0.033)
    for cx in (int(size * 0.38), int(size * 0.50), int(size * 0.62)):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(20, 24, 38, 255))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if ICONSET.exists():
        shutil.rmtree(ICONSET)
    ICONSET.mkdir(parents=True)

    master = speech_glyph(rounded_tile(SIZE))

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
