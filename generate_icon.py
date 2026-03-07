#!/usr/bin/env python3
"""Generate the app icon for Claude Management."""

from PIL import Image, ImageDraw
import os

SIZE = 256
PADDING = 20
BG_COLOR = (26, 26, 46)  # #1a1a2e
CORNER_RADIUS = 40

# Brand colors with alpha for translucency
CLAUDE_ORANGE = (232, 123, 53)   # #E87B35
JIRA_BLUE = (38, 132, 255)      # #2684FF
GITHUB_PURPLE = (137, 87, 229)  # #8957E5

CIRCLE_RADIUS = 52
CIRCLE_ALPHA = 160  # ~63% opacity


def draw_rounded_rect(draw, xy, radius, fill):
    x0, y0, x1, y1 = xy
    draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
    draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
    draw.pieslice([x0, y0, x0 + 2 * radius, y0 + 2 * radius], 180, 270, fill=fill)
    draw.pieslice([x1 - 2 * radius, y0, x1, y0 + 2 * radius], 270, 360, fill=fill)
    draw.pieslice([x0, y1 - 2 * radius, x0 + 2 * radius, y1], 90, 180, fill=fill)
    draw.pieslice([x1 - 2 * radius, y1 - 2 * radius, x1, y1], 0, 90, fill=fill)


def draw_circle(img, center, radius, color, alpha):
    """Draw a translucent circle by compositing an overlay."""
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    x, y = center
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=(*color, alpha))
    return Image.alpha_composite(img, overlay)


def main():
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded rectangle
    draw_rounded_rect(draw, (0, 0, SIZE - 1, SIZE - 1), CORNER_RADIUS, (*BG_COLOR, 255))

    # Three overlapping circles arranged in a triangle
    cx, cy = SIZE // 2, SIZE // 2
    offset = 38  # distance from center to each circle center

    # Top circle (Claude orange)
    img = draw_circle(img, (cx, cy - offset), CIRCLE_RADIUS, CLAUDE_ORANGE, CIRCLE_ALPHA)
    # Bottom-left (Jira blue)
    img = draw_circle(img, (cx - offset + 5, cy + offset - 10), CIRCLE_RADIUS, JIRA_BLUE, CIRCLE_ALPHA)
    # Bottom-right (GitHub purple)
    img = draw_circle(img, (cx + offset - 5, cy + offset - 10), CIRCLE_RADIUS, GITHUB_PURPLE, CIRCLE_ALPHA)

    out_path = os.path.join(os.path.dirname(__file__), "assets", "icon.png")
    img.save(out_path, "PNG")
    print(f"Icon saved to {out_path}")


if __name__ == "__main__":
    main()
