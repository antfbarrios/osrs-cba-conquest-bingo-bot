"""
Generates the /map command's region-control image with Pillow.

Uses the real region shapes extracted from the provided PSD
(osrsmapcolorsandlabels.psd). Each region is a transparent PNG that gets
tinted with the controlling team's color. Karamja is colored the same as
Misthalin (it is scored as part of Misthalin).

Public API is intentionally identical to the old procedural version so
main.py needs no changes:

    assign_team_colors(team_names) -> dict[str, tuple[int,int,int]]
    build_map_image(region_leaders, team_colors) -> io.BytesIO
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Paths & layout
# ---------------------------------------------------------------------------

_ASSETS = Path(__file__).resolve().parent / "assets" / "map"
_META_PATH = _ASSETS / "map_meta.json"

with open(_META_PATH, encoding="utf-8") as f:
    _META = json.load(f)

CANVAS_W, CANVAS_H = _META["canvas_size"]  # 468 x 214
LEGEND_H = 56
FINAL_W, FINAL_H = CANVAS_W, CANVAS_H + LEGEND_H

# How much to upscale the final image for Discord (nearest-neighbor keeps
# the pixel-art edges crisp).
SCALE = 3

# Map from the slug used in the PSD / meta file → the display name used
# in REGION_SOURCES / region_leaders.
# Karamja is special: it is drawn, but colored from Misthalin's leader.
REGION_SLUG_TO_NAME = {
    "kourend": "Kourend",
    "varlamore": "Varlamore",
    "tirannwn": "Tirannwn",
    "kandarin": "Kandarin",
    "asgarnia": "Asgarnia",
    "misthalin": "Misthalin",
    "morytania": "Morytania",
    "desert": "Desert",
    "fremmy": "Fremennik",
    "wilderness": "Wilderness",
    "karamja": "Misthalin",  # scored under Misthalin
}

# Cycled through in team order (sorted alphabetically). Extend if you ever
# run with more than 7 teams.
TEAM_COLOR_PALETTE = [
    (55, 138, 221),   # blue
    (216, 90, 48),    # coral
    (99, 153, 34),    # green
    (127, 119, 221),  # purple
    (239, 159, 39),   # amber
    (29, 158, 117),   # teal
    (212, 83, 126),   # pink
]
NEUTRAL_COLOR = (136, 135, 128)  # gray


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_font(size: int, bold: bool = False):
    path = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )
    try:
        return ImageFont.truetype(path, size)
    except (OSError, IOError):
        return ImageFont.load_default()


def tint(img: Image.Image, rgb: tuple[int, int, int], alpha: int = 220) -> Image.Image:
    """
    Recolor an RGBA region shape while preserving its alpha mask.
    `alpha` controls how opaque the fill is (0-255).
    """
    r, g, b = rgb
    colored = Image.new("RGBA", img.size, (r, g, b, 0))
    original_alpha = img.getchannel("A")
    scaled_alpha = original_alpha.point(lambda a: int(a * alpha / 255))
    colored.putalpha(scaled_alpha)
    return colored


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int] | None:
    """Parse '#RRGGBB' or 'RRGGBB' into (r, g, b). Returns None on bad input."""
    h = hex_color.strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def assign_team_colors(team_names, color_overrides: dict | None = None) -> dict:
    """
    Build {team_name: (r, g, b)}.

    - color_overrides: optional {team_name: "#RRGGBB"} from the database.
      Teams with a valid override use that color.
    - Remaining teams get a deterministic palette color based on
      alphabetical order (same as before), so the map stays stable.
    """
    overrides = color_overrides or {}
    sorted_names = sorted(team_names)
    result = {}
    palette_idx = 0
    for team in sorted_names:
        hex_val = overrides.get(team)
        rgb = _hex_to_rgb(hex_val) if hex_val else None
        if rgb is not None:
            result[team] = rgb
        else:
            result[team] = TEAM_COLOR_PALETTE[palette_idx % len(TEAM_COLOR_PALETTE)]
            palette_idx += 1
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_map_image(region_leaders: dict, team_colors: dict) -> io.BytesIO:
    """
    region_leaders: {region_name: team_name_or_None}
        Same dict produced by compute_region_leaders() in main.py.
        None / missing = neutral (unclaimed or tied).

    team_colors: {team_name: (r, g, b)}
        From assign_team_colors().

    Returns a BytesIO PNG buffer ready to wrap in discord.File.
    """
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))

    # Draw every region shape, tinted by its controlling team
    for slug, info in _META["regions"].items():
        display_name = REGION_SLUG_TO_NAME.get(slug)
        if display_name is None:
            continue

        leader = region_leaders.get(display_name)
        color = team_colors.get(leader, NEUTRAL_COLOR) if leader else NEUTRAL_COLOR

        region_path = _ASSETS / info["file"]
        region_img = Image.open(region_path).convert("RGBA")
        tinted = tint(region_img, color, alpha=210)

        # Cheap 1-px outline for definition
        outline = tint(region_img, tuple(max(0, c - 60) for c in color), alpha=255)
        ox, oy = info["offset"]
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            canvas.paste(outline, (ox + dx, oy + dy), outline)
        canvas.paste(tinted, (ox, oy), tinted)

    # Draw region name + controlling team on top of each main landmass
    draw = ImageDraw.Draw(canvas)
    title_font = _get_font(11, bold=True)
    sub_font = _get_font(10)

    # Approximate center points for labels (derived from PSD offsets + sizes)
    LABEL_CENTERS = {
        "Kourend":    (12 + 64,  3 + 40),
        "Varlamore":  (3 + 63,  101 + 45),
        "Tirannwn":   (160 + 23, 85 + 30),
        "Kandarin":   (184 + 50, 47 + 60),
        "Asgarnia":   (227 + 40, 20 + 70),
        "Misthalin":  (315 + 55, 19 + 50),
        "Morytania":  (377 + 40, 63 + 45),
        "Desert":     (330 + 33, 109 + 40),
        "Fremennik":  (144 + 80, 2 + 30),
        "Wilderness": (311 + 34, 3 + 28),
    }

    for region, (cx, cy) in LABEL_CENTERS.items():
        leader = region_leaders.get(region)
        label = leader if leader else "Neutral"

        bbox = draw.textbbox((0, 0), region, font=title_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw / 2, cy - th / 2 - 8), region,
                  font=title_font, fill=(255, 255, 255, 255))

        bbox2 = draw.textbbox((0, 0), label, font=sub_font)
        tw2, th2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
        draw.text((cx - tw2 / 2, cy - th2 / 2 + 8), label,
                  font=sub_font, fill=(255, 255, 255, 220))

    # ------------------------------------------------------------------
    # Legend bar at the bottom
    # ------------------------------------------------------------------
    full = Image.new("RGBA", (FINAL_W, FINAL_H), (24, 24, 28, 255))
    full.paste(canvas, (0, 0), canvas)

    draw = ImageDraw.Draw(full)
    legend_font = _get_font(12)
    legend_y = CANVAS_H + 16
    swatch = 14
    gap = 20

    items = list(team_colors.items()) + [("Neutral", NEUTRAL_COLOR)]
    widths = []
    total_width = 0
    for name, _ in items:
        bbox = draw.textbbox((0, 0), name, font=legend_font)
        w = swatch + 6 + (bbox[2] - bbox[0])
        widths.append(w)
        total_width += w + gap
    total_width -= gap

    cx = (FINAL_W - total_width) / 2
    for (name, color), w in zip(items, widths):
        draw.rounded_rectangle(
            [cx, legend_y, cx + swatch, legend_y + swatch],
            radius=3,
            fill=color + (255,),
        )
        draw.text(
            (cx + swatch + 6, legend_y + 0),
            name,
            font=legend_font,
            fill=(180, 180, 180, 255),
        )
        cx += w + gap

    # Upscale for Discord
    if SCALE > 1:
        full = full.resize(
            (FINAL_W * SCALE, FINAL_H * SCALE),
            Image.NEAREST,
        )

    buffer = io.BytesIO()
    full.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer
