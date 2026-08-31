from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SCALE = 4


def scaled(values):
    return tuple(round(value * SCALE) for value in values)


canvas = Image.new("RGBA", (256 * SCALE, 256 * SCALE), (0, 0, 0, 0))
draw = ImageDraw.Draw(canvas)
draw.rounded_rectangle(
    scaled((18, 18, 238, 238)),
    radius=56 * SCALE,
    fill="#183c34",
)
draw.ellipse(
    scaled((57, 70, 176, 189)),
    outline="#fff9e9",
    width=23 * SCALE,
)
draw.ellipse(scaled((108, 121, 125, 138)), fill="#fff9e9")
draw.line(
    scaled((153, 99, 201, 51)),
    fill="#ef6845",
    width=23 * SCALE,
)
draw.line(
    scaled((164, 51, 201, 51, 201, 88)),
    fill="#ef6845",
    width=23 * SCALE,
    joint="curve",
)

preview = canvas.resize((512, 512), Image.Resampling.LANCZOS)
preview.save(ASSETS / "agent-icon.png")
canvas.save(
    ASSETS / "agent-icon.ico",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
