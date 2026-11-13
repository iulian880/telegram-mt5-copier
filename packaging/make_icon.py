"""Generate packaging/tgmt5.ico for the Windows build.

Usage:  python packaging/make_icon.py      (needs Pillow: pip install pillow)

The design is drawn at 1024 px and downscaled. It shows a blue rounded tile with
three rising candlesticks and an upward signal arrow.
"""
from pathlib import Path

from PIL import Image, ImageDraw

S = 1024
HERE = Path(__file__).parent


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def draw() -> Image.Image:
    # Diagonal gradient background.
    top, bottom = (30, 58, 138), (14, 165, 233)  # navy -> sky blue
    grad = Image.new("RGBA", (S, S))
    px = grad.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = (*lerp(top, bottom, (x + y) / (2 * S - 2)), 255)

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((32, 32, S - 32, S - 32), radius=200, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    white, green = (255, 255, 255, 255), (74, 222, 128, 255)

    # Three rising candlesticks: (center x, wick top, wick bottom, body top, body bottom)
    candles = [
        (300, 560, 830, 620, 780),
        (512, 420, 760, 480, 690),
        (724, 260, 640, 320, 560),
    ]
    for cx, wt, wb, bt, bb in candles:
        d.rounded_rectangle((cx - 14, wt, cx + 14, wb), radius=14, fill=white)
        d.rounded_rectangle((cx - 70, bt, cx + 70, bb), radius=28, fill=white)

    # Upward signal arrow across the top-left.
    d.line([(190, 520), (420, 330), (560, 400), (790, 190)], fill=green, width=56, joint="curve")
    d.polygon([(860, 130), (700, 170), (820, 290)], fill=green)
    return img


def main() -> None:
    img = draw()
    img.save(HERE / "tgmt5.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])    print("Wrote", HERE / "tgmt5.ico")


if __name__ == "__main__":
    main()
