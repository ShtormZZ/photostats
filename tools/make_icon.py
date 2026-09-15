# photostats — statistics for a personal photo archive
# Copyright (C) 2026 Sergey Inyutin
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License, version 3, as published by
# the Free Software Foundation. See LICENSE for the full text.
# Commercial licensing is available — see COMMERCIAL.md.
# SPDX-License-Identifier: AGPL-3.0-only
"""Draw the application icon and write it out in every size the program needs.

The mark is the interface in miniature: viewfinder corners around three bars in
the safelight amber. The frame says photograph, the bars say statistics, and at
16 px both are still chunky enough to read — which is the only test an icon has
to pass, since that is the size a taskbar and a browser tab give it.

Colours are lifted from `web/index.html`, so the icon cannot drift away from the
palette it sits next to. Geometry is in a 0..1 square and scaled at draw time;
everything is rendered once at 1024 px and resampled down, because drawing a
16 px bar directly gives you a ragged one.

    python tools/make_icon.py

Writes `web/icon.png`, `web/icon.svg`, `web/favicon.ico` and `web/mark.svg` —
the last one is the same drawing without its tile, for the header of the
interface. Re-run it after changing anything here; the files are committed, so
the program never needs Pillow just to show its own icon.
"""

import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

# --- palette (see :root in web/index.html) ---------------------------------
PANEL = "#1E1D24"      # --panel, the tile
LINE = "#3A3745"       # --line, the inset edge that keeps the tile visible
                       # against a dark taskbar
PAPER = "#EDE8DF"      # --paper, the viewfinder corners
SAFELIGHT = "#E39B33"  # --safelight, the bars

# --- geometry, in fractions of the side ------------------------------------
RADIUS = 0.215         # corner radius of the tile
INSET = 0.155          # how far the viewfinder corners sit from the edge
ARM = 0.155            # length of one arm of a corner
STROKE = 0.052         # thickness of a corner arm
BASELINE = 0.752       # where the bars stand
BAR_W = 0.104
BAR_GAP = 0.056
BAR_H = (0.163, 0.302, 0.224)  # middle bar tallest: a shape, not a staircase
BAR_R = 0.022          # rounded tops, matching the tile's own softness

# Below this the drawing is simplified: the corners are dropped and the bars
# grown to fill the space they leave. At 16 px an arm is one pixel and the gap
# between it and a bar is less than one, so the two smear into a single smudge —
# the frame stops being a frame and only costs the bars their legibility.
SMALL = 24
SMALL_BAR_W = 0.152
SMALL_BAR_GAP = 0.082
SMALL_BASELINE = 0.822
SMALL_BAR_H = (0.28, 0.52, 0.385)

MASTER = 1024
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def bar_boxes(small=False):
    """Left/top/right/bottom of each bar, in fractions of the side."""
    w, gap = (SMALL_BAR_W, SMALL_BAR_GAP) if small else (BAR_W, BAR_GAP)
    base = SMALL_BASELINE if small else BASELINE
    heights = SMALL_BAR_H if small else BAR_H
    span = len(heights) * w + (len(heights) - 1) * gap
    x = (1 - span) / 2
    for h in heights:
        yield x, base - h, x + w, base
        x += w + gap


def corners():
    """The four viewfinder brackets, each as a three-point polyline."""
    a, b = INSET, 1 - INSET
    return [
        [(a, a + ARM), (a, a), (a + ARM, a)],  # top-left
        [(b - ARM, a), (b, a), (b, a + ARM)],  # top-right
        [(b, b - ARM), (b, b), (b - ARM, b)],  # bottom-right
        [(a + ARM, b), (a, b), (a, b - ARM)],  # bottom-left
    ]


def render(size):
    """Draw the icon at `size` px, always via a supersampled master."""
    small = size <= SMALL
    scale = MASTER
    img = Image.new("RGBA", (scale, scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def px(v):
        return v * scale

    # The tile. The outline is drawn inside the shape, so half of its width
    # would fall off the edge and thin out; insetting by that half keeps it
    # even all the way round.
    edge = max(1, round(px(0.012)))
    d.rounded_rectangle(
        [edge / 2, edge / 2, scale - 1 - edge / 2, scale - 1 - edge / 2],
        radius=px(RADIUS), fill=PANEL, outline=LINE, width=edge,
    )

    if not small:
        for poly in corners():
            d.line([(px(x), px(y)) for x, y in poly],
                   fill=PAPER, width=round(px(STROKE)), joint="curve")

    for x0, y0, x1, y1 in bar_boxes(small):
        d.rounded_rectangle([px(x0), px(y0), px(x1), px(y1)],
                            radius=px(BAR_R), fill=SAFELIGHT)

    if size != scale:
        img = img.resize((size, size), Image.LANCZOS)
    return img


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view}" \
width="{w}" height="{w}" role="img" aria-label="photostats">
  <title>photostats</title>
{tile}  <g fill="none" stroke="{paper}" stroke-width="{stroke}" \
stroke-linecap="butt" stroke-linejoin="miter">
{corners}
  </g>
  <g fill="{safelight}">
{bars}
  </g>
</svg>
"""

TILE = """  <rect x="6" y="6" width="1012" height="1012" rx="220"
        fill="{panel}" stroke="{line}" stroke-width="12"/>
"""


def svg(tile=True):
    """The same drawing as vector.

    With the tile it is the app icon. Without it the mark is trimmed to its own
    ink and stands on whatever is behind it — that is the one the header uses,
    where a tile in the panel colour would only add a faint rectangle around
    the mark on a background of exactly that colour.
    """
    def px(v):
        return round(v * 1024, 1)

    paths = "\n".join(
        '    <path d="M {} {} L {} {} L {} {}"/>'.format(
            *[px(v) for xy in poly for v in xy])
        for poly in corners()
    )
    rects = "\n".join(
        '    <rect x="{}" y="{}" width="{}" height="{}" rx="{}"/>'.format(
            px(x0), px(y0), px(x1 - x0), px(y1 - y0), px(BAR_R))
        for x0, y0, x1, y1 in bar_boxes()
    )
    if tile:
        view, side = "0 0 1024 1024", 1024
    else:
        # The corners are the outermost thing drawn, and a stroke straddles its
        # path, so half of it lies outside the corner itself. Trim to that, or
        # the box clips the frame it is supposed to contain.
        edge = px(INSET) - px(STROKE) / 2
        side = round(1024 - 2 * edge, 1)
        view = "{0} {0} {1} {1}".format(round(edge, 1), side)
    return SVG.format(view=view, w=side,
                      tile=TILE.format(panel=PANEL, line=LINE) if tile else "",
                      paper=PAPER, safelight=SAFELIGHT, stroke=px(STROKE),
                      corners=paths, bars=rects)


def main():
    os.makedirs(WEB, exist_ok=True)

    master = render(MASTER)
    master.resize((256, 256), Image.LANCZOS).save(os.path.join(WEB, "icon.png"))

    # Each size is resampled from the master rather than from the one above it:
    # a chain of halvings smears the bars by the time it reaches 16 px.
    sizes = [render(s) for s in ICO_SIZES]
    sizes[-1].save(os.path.join(WEB, "favicon.ico"), format="ICO",
                   sizes=[(s, s) for s in ICO_SIZES],
                   append_images=sizes[:-1])

    for name, tile in (("icon.svg", True), ("mark.svg", False)):
        with open(os.path.join(WEB, name), "w", encoding="utf-8") as f:
            f.write(svg(tile))

    print("written: web/icon.png, web/icon.svg, web/mark.svg, web/favicon.ico")


if __name__ == "__main__":
    main()
