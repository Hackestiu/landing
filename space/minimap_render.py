"""
Browser rendering of the UNO Q's minimap.

On the board, `core/minimap_module.py` is a thin relay: it resolves a landmark
code and hands it to the sketch over Bridge RPC, and the drawing happens in
C++ against a 160x128 TFT (`sketch/src/display/minimap.cpp`). There is no
display here, so that relay has nothing to relay to — this module replaces it
the same way the browser already replaces the camera and the microphone, by
doing in the page what the hardware does on the device.

What it renders is the board's own picture, not a reinterpretation of it: the
40x28 tile grid at 4 px a tile, the palettes from the vendored landmark JSON,
the dither stamps the sketch draws per terrain character, and Adafruit GFX's
midpoint rasteriser for the landmark pins (whose `fillCircle` is a visibly
asymmetric blob that an SVG <circle> does not reproduce). Output is an SVG of
1x1 rects, scaled up with crisp edges, so it stays a pixel picture.

Two places where the JSON and the firmware disagree, and what this does:

  * `zones`, `zoneColors` and `paletteLocked` have no consumer anywhere in the
    board repo. The real reveal is a Voronoi partition over the landmark
    points -- a tile is lit iff the landmark nearest its centre is visited --
    and the locked colour is a greyscale computed at runtime, which differs
    from `paletteLocked` by up to 29 per channel. Firmware wins: the three
    unused keys are ignored.

  * `laterals` maps to both NL and NR in the JSON's `visionLabelMapping` (and
    in `unlockRules.linked`), but to NL alone in the board's Python, which
    means the board can never complete the Sagrada map. The JSON wins.

One deliberate addition. The board is a souvenir collected over a visit, so
two states are enough for it: lit or not. A visitor here classifies one
element per run, and a region lit just now would otherwise be indistinguishable
from one lit three runs ago -- so freshly detected regions get a third state,
the palette colour lightened toward white.

Colours are not quantised to RGB565. The device's `color565()` drops the low
bits of every channel; matching that would mean degrading colours on a canvas
that has no such limit.
"""

import json
from functools import lru_cache

from config import MINIMAP_DIR, logger

# Site id -> vendored landmark file, mirroring LANDMARKS_FILES in the board's
# core/minimap_module.py.
# Site id -> vendored landmark file, mirroring LANDMARKS_FILES in the board's
# core/minimap_module.py. Exactly 4 sites, same keys as the board.
LANDMARKS_FILES = {
    "park_guell":      MINIMAP_DIR / "landmarks_guell.json",
    "sagrada_familia": MINIMAP_DIR / "landmarks_sagrada.json",
    "casa_batllo":     MINIMAP_DIR / "landmarks_batllo.json",
    "pedrera":       MINIMAP_DIR / "landmarks_mila.json",
}

# Vision label -> landmark code. Sagrada Família carries this in its JSON as
# `visionLabelMapping`; the other three have their mappings copied from
# VISION_LABEL_TO_LANDMARK in the board's core/minimap_module.py.
# Values are tuples because one label can light more than one landmark.
LABEL_TO_CODE: dict[str, dict[str, tuple[str, ...]]] = {
    "park_guell": {
        "escalinata_drac":      ("DR",),
        "sala_hipostila":       ("HH",),
        "placa_natura":         ("NS",),
        "casa_museu":           ("CG",),
        "3_viaductes":          ("TV",),
        "turo_3_creus":         ("CH",),
        "pavellons_consergeria": ("PL",),
    },
    "sagrada_familia": {},  # filled from visionLabelMapping in the JSON on first load
    "casa_batllo": {
        # labels from models/vision/casa_batllo/labels.json id2label
        "pla_frontal":             ("PS",),  # full-facade shot -> upper zone
        "pla_inferior":            ("PI",),  # lower half shot -> lower zone
    },
    "pedrera": {
        # labels from models/vision/pedrera/labels.json id2label
        "facana_1":            ("CN",),  # straight-on corner shot
        "facana_2":            ("AE",),  # left wing
        "facana_3":            ("AD",),  # right wing
    },
}

# What the status bar shows before anything has been found, matching the board's
# default label text for each site (unaccented capitals, 5x7 bitmap font).
FALLBACK_LABEL = {
    "park_guell":      "PARK GUELL",
    "sagrada_familia": "SAGRADA FAMILIA",
    "casa_batllo":     "CASA BATLLO",
    "pedrera":         "LA PEDRERA",
}

# Site names, spelled properly, for the SVG aria-label / screen reader.
SITE_NAMES = {
    "park_guell":      "Park Güell",
    "sagrada_familia": "Sagrada Família",
    "casa_batllo":     "Casa Batlló",
    "pedrera":       "La Pedrera",
}

# Status-bar label overrides, from the board's landmarks_*.h `screen` fields.
# Only needed where the JSON's `screen` value differs from what the firmware
# actually prints; Park Güell is the only case (JSON is Catalan, sketch English).
SCREEN_LABELS = {
    "park_guell": {
        "PL": "PORTERS LODGE",
        "DR": "DRAGON STAIRS",
        "HH": "HYPOSTYLE HALL",
        "NS": "NATURE SQUARE",
        "CG": "CASA GAUDI",
        "TV": "3 VIADUCTS",
        "AG": "AUSTRIA GARDEN",
        "CH": "CALVARY HILL",
    },
    # casa_batllo and casa_mila use the JSON's `screen` field directly
    # (CANTONADA, ALA ESQUERRA, ALA DRETA, PLA SUPERIOR, PLA INFERIOR)
}

# Screen geometry, from the `screen` block of either JSON.
TILE = 4
COLS, ROWS = 40, 28
MAP_W, MAP_H = COLS * TILE, ROWS * TILE  # 160 x 112
BAR_H = 16
SCREEN_H = MAP_H + BAR_H  # 128

# How far a freshly detected region is pushed toward white.
FRESH_MIX = 0.35

# Sagrada Família paints flat 4x4 fills, one palette key per terrain char.
# Note that '#' really does draw as wallFill: the sketch sends both '#' and 'w'
# to C_BLOCK, and loads wallEdge into a slot no Sagrada tile ever reads. Its
# only appearance on screen is the hairline above the status bar.
SAGRADA_TILES = {
    "#": "wallFill",
    "w": "wallFill",
    "a": "apse",
    "y": "sacristy",
    "x": "crucero",
    "t": "torres",
    "n": "naveCentral",
    "l": "naveLateral",
    "c": "porch",
}

# Casa Batlló elevation paints flat 4x4 fills per character.
BATLLO_TILES = {
    " ": "void",
    "x": "cross",
    "n": "onion",
    "y": "cone",
    "v": "roofPink",
    "V": "roofPinkDark",
    "u": "roofTeal",
    "U": "roofTealDark",
    "t": "roofGreen",
    "T": "roofGreenDark",
    "z": "tower",
    "Z": "towerDark",
    "e": "cornice",
    "m": "mosaic",
    "M": "mosaicTeal",
    "c": "mosaicCream",
    "f": "frame",
    "h": "glass",
    "k": "bone",
    "b": "boneShadow",
    "o": "oculus",
    "a": "stone",
    "A": "stoneDark",
    "q": "shadow",
    "g": "tree",
    "i": "amaGable",
    "I": "amaGableDark",
    "p": "amaWall",
    "P": "amaWallDark",
    "l": "amaGlass",
    "r": "amaPlinth",
    "B": "eixWall",
    "C": "eixWallDark",
    "D": "eixRoof",
}

# Casa Milà (La Pedrera) facade elevation paints flat 4x4 fills per character.
MILA_TILES = {
    " ": "void",
    "c": "chimney",
    "w": "parapet",
    "o": "oculus",
    "s": "stone",
    "i": "window",
    "b": "balcony",
    "r": "recede",
    "d": "divider",
    "a": "ground",
    "p": "portal",
    "j": "neighbor",
    "t": "tree",
    "l": "lamp",
    "g": "sidewalk",
}

# Park Güell dithers instead: a base fill plus a fixed stamp of accent pixels.
# The stamps are position-independent, so every tile of a given character is
# pixel-identical. Transcribed from paintTile() in minimap.cpp.
GUELL_TILES = {
    " ": ("void", "blockD", ((0, 0), (2, 2))),
    "#": ("block", "blockD", ((0, 0), (1, 0), (2, 0), (3, 0), (0, 3), (3, 3))),
    "F": ("forestDD", "forest", ((1, 0), (2, 1), (0, 2), (3, 2), (1, 3))),
    "f": ("forest", "forestL", ((0, 1), (2, 0), (3, 2), (1, 3))),
    "g": ("scrub", "scrubL", ((0, 0), (2, 1), (1, 2), (3, 3))),
    "p": ("path", "pathD", ((1, 1), (3, 2))),
    "S": ("sand", "sandD", ((0, 2), (2, 0))),
    "w": ("stone", "stoneD", ((0, 0), (1, 0), (2, 0), (3, 0),
                              (0, 2), (1, 2), (2, 2), (3, 2))),
    "b": ("roof", "roofL", ((0, 0), (1, 0), (2, 0), (3, 0))),
    "c": ("tile", "tileL", ((0, 0), (2, 1), (1, 2), (3, 3))),
    "r": ("rock", "rockD", ((0, 3), (1, 3), (3, 1))),
}
GUELL_DEFAULT = ("void", None, ())

# Sagrada's ink is hardcoded in the sketch rather than kept in the palette.
SAGRADA_INK = "#32281f"
GOLD = "#fffc00"  # RGB565 0xFFE0, the completed-map accent


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------


def _rgb(colour: str) -> tuple[int, int, int]:
    c = colour.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def greyscale(colour: str) -> str:
    """The sketch's locked colour: an integer luma, drawn as a neutral grey.

    Public because the step indicator locks its tiles the same way the map locks
    its terrain — one rule for "not yours yet", in both places.

    Reproduces grayOf() from minimap.cpp byte for byte -- the >> 8 is a
    deliberate part of it, not a rounded division, and the result is a little
    darker than a float luma would be.
    """
    r, g, b = _rgb(colour)
    y = (r * 77 + g * 151 + b * 28) >> 8
    return _hex((y, y, y))


def _lighten(colour: str, amount: float = FRESH_MIX) -> str:
    r, g, b = _rgb(colour)
    return _hex(tuple(round(c + (255 - c) * amount) for c in (r, g, b)))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def load(site: str) -> dict:
    """Reads a site's vendored landmark file. Raises KeyError for unknown sites."""
    path = LANDMARKS_FILES[site]
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    mapping = data.get("visionLabelMapping")
    if mapping:
        LABEL_TO_CODE[site] = {
            label: tuple(codes) if isinstance(codes, list) else (codes,)
            for label, codes in mapping.items()
        }
    return data


def codes_for(site: str, vision_label: str) -> tuple[str, ...]:
    """Landmark codes lit by a vision label, or () if the label maps to none.

    The board's equivalent is mark_detected(); unrecognised labels, and the
    `unknown` class, simply light nothing.
    """
    load(site)
    return LABEL_TO_CODE.get(site, {}).get(vision_label, ())


def _nearest(landmarks: list[dict], x: int, y: int) -> int:
    """Index of the landmark nearest (x, y), ties going to the lowest index.

    Squared-Euclidean, matching nearestLandmarkId() in minimap.cpp. This is
    what partitions the map: every tile belongs to exactly one landmark, and
    lights up when that landmark does.
    """
    best, best_d = 0, None
    for i, lm in enumerate(landmarks):
        dx, dy = lm["x"] - x, lm["y"] - y
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


# ---------------------------------------------------------------------------
# Rasterising
# ---------------------------------------------------------------------------


class _Canvas:
    """A 160x128 pixel buffer, flushed to SVG as horizontal runs of one colour."""

    def __init__(self, width: int, height: int, fill=None):
        self.w, self.h = width, height
        self.px = [[fill] * width for _ in range(height)]

    def pixel(self, x: int, y: int, colour: str) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = colour

    def rect(self, x: int, y: int, w: int, h: int, colour: str) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.pixel(xx, yy, colour)

    def vline(self, x: int, y: int, length: int, colour: str) -> None:
        for yy in range(y, y + length):
            self.pixel(x, yy, colour)

    def fill_circle(self, cx: int, cy: int, r: int, colour: str) -> None:
        """Adafruit_GFX::fillCircle -- a vertical span plus the midpoint helper."""
        self.vline(cx, cy - r, 2 * r + 1, colour)
        f, ddf_x, ddf_y, x, y = 1 - r, 1, -2 * r, 0, r
        while x < y:
            if f >= 0:
                y -= 1
                ddf_y += 2
                f += ddf_y
            x += 1
            ddf_x += 2
            f += ddf_x
            self.vline(cx + x, cy - y, 2 * y, colour)
            self.vline(cx + y, cy - x, 2 * x, colour)
            self.vline(cx - x, cy - y, 2 * y, colour)
            self.vline(cx - y, cy - x, 2 * x, colour)

    def draw_circle(self, cx: int, cy: int, r: int, colour: str) -> None:
        """Adafruit_GFX::drawCircle -- the 1 px midpoint outline."""
        f, ddf_x, ddf_y, x, y = 1 - r, 1, -2 * r, 0, r
        for p in ((cx, cy + r), (cx, cy - r), (cx + r, cy), (cx - r, cy)):
            self.pixel(*p, colour)
        while x < y:
            if f >= 0:
                y -= 1
                ddf_y += 2
                f += ddf_y
            x += 1
            ddf_x += 2
            f += ddf_x
            for p in (
                (cx + x, cy + y), (cx - x, cy + y), (cx + x, cy - y), (cx - x, cy - y),
                (cx + y, cy + x), (cx - y, cy + x), (cx + y, cy - x), (cx - y, cy - x),
            ):
                self.pixel(*p, colour)

    def to_svg_rects(self, ox: int = 0, oy: int = 0) -> str:
        """Run-length encodes each row; unpainted pixels emit nothing."""
        parts = []
        for y, row in enumerate(self.px):
            x = 0
            while x < self.w:
                colour = row[x]
                run = 1
                while x + run < self.w and row[x + run] == colour:
                    run += 1
                if colour is not None:
                    parts.append(
                        f'<rect x="{x + ox}" y="{y + oy}" width="{run}" '
                        f'height="1" fill="{colour}"/>'
                    )
                x += run
        return "".join(parts)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _tile_colour(palette: dict, key: str, state: str) -> str:
    base = palette.get(key, palette["void"])
    if state == "locked":
        return greyscale(base)
    if state == "fresh":
        return _lighten(base)
    return base


def _tile_svg(palette: dict, site: str, ch: str, state: str) -> str:
    """The 4x4 stamp for one terrain character, as SVG at the origin.

    Sagrada Família fills flat; Park Güell lays accent pixels over the fill in a
    fixed pattern. Either way the result depends only on (char, state), which is
    what lets the map be built from a few dozen reusable tiles.
    """
    if site == "sagrada_familia":
        colour = _tile_colour(palette, SAGRADA_TILES.get(ch, "void"), state)
        return f'<rect width="4" height="4" fill="{colour}"/>'

    if site == "casa_batllo":
        colour = _tile_colour(palette, BATLLO_TILES.get(ch, "void"), state)
        return f'<rect width="4" height="4" fill="{colour}"/>'

    if site in ("casa_mila", "la_pedrera", "pedrera"):
        colour = _tile_colour(palette, MILA_TILES.get(ch, "void"), state)
        return f'<rect width="4" height="4" fill="{colour}"/>'

    base_key, accent_key, stamp = GUELL_TILES.get(ch, GUELL_DEFAULT)
    parts = [f'<rect width="4" height="4" fill="{_tile_colour(palette, base_key, state)}"/>']
    if accent_key is not None:
        accent = _tile_colour(palette, accent_key, state)
        # One path per tile rather than a rect per pixel: the stamps are up to
        # eight pixels each, across 1120 tiles.
        d = "".join(f"M{dx} {dy}h1v1h-1z" for dx, dy in stamp)
        parts.append(f'<path d="{d}" fill="{accent}"/>')
    return "".join(parts)


def _terrain(data: dict, site: str, states: list[str]) -> tuple[str, str]:
    """Returns (<defs> of distinct tiles, the grid of <use> references).

    Emitting a rect per pixel would be honest and enormous -- Park Güell's
    dither breaks every horizontal run, so the naive render is ~450 KB of SVG
    for a picture with 36 distinct tiles in it. This draws each tile once and
    references it 1120 times instead.
    """
    palette = data["palette"]
    landmarks = data["landmarks"]
    rows = data["map"]

    defs: dict[tuple[str, str], str] = {}
    uses = []
    for row in range(min(ROWS, len(rows))):
        line = rows[row]
        for col in range(COLS):
            ch = line[col] if col < len(line) else " "
            px, py = col * TILE, row * TILE
            # The sketch tests the tile's centre, not its corner.
            state = states[_nearest(landmarks, px + 2, py + 2)]
            key = (ch, state)
            if key not in defs:
                defs[key] = f"t{len(defs)}"
            uses.append(f'<use href="#{defs[key]}" x="{px}" y="{py}"/>')

    symbols = "".join(
        f'<g id="{name}">{_tile_svg(palette, site, ch, state)}</g>'
        for (ch, state), name in defs.items()
    )
    return f"<defs>{symbols}</defs>", "".join(uses)


def _paint_pins(canvas: _Canvas, data: dict, ink: str, visited: set[str]) -> None:
    """Landmark pins, drawn after the terrain and never greyed.

    That is what keeps an undiscovered map readable: the terrain may be a grey
    field, but the pins still say how many places there are and where.
    """
    markers = data["markerColors"]
    for lm in data["landmarks"]:
        dot = markers["visitedDot"] if lm["code"] in visited else markers["unvisitedDot"]
        canvas.fill_circle(lm["x"], lm["y"], 4, markers["ring"])
        canvas.draw_circle(lm["x"], lm["y"], 4, ink)
        canvas.fill_circle(lm["x"], lm["y"], 2, dot)


def _status_bar(data: dict, site: str, ink: str, visited: set[str], last: str | None):
    """The bottom strip: a count badge and the last landmark's screen name.

    Returns (pixel-drawing callback, list of SVG text elements). Text is the one
    thing not rasterised -- the device uses Adafruit's 5x7 bitmap font, and this
    approximates it with the page's monospace at a forced 6 px advance.
    """
    palette = data["palette"]
    markers = data["markerColors"]
    landmarks = data["landmarks"]
    count, total = len(visited), len(landmarks)
    completed = total > 0 and count >= total

    hairline = palette.get("blockD") or palette.get("wallEdge") or ink
    badge = GOLD if completed else markers["ring"]
    count_ink = "#000000" if completed else ink
    text_colour = GOLD if completed else markers["ring"]

    if completed:
        # The sketch prints this in Catalan; the rest of this page is English,
        # and one Catalan string in an otherwise English status bar reads as an
        # oversight rather than as fidelity.
        label = "MAP COMPLETE!"
    elif last:
        by_code = {lm["code"]: lm for lm in landmarks}
        label = SCREEN_LABELS.get(site, {}).get(last) or by_code.get(
            last, {}
        ).get("screen", FALLBACK_LABEL[site])
    else:
        label = FALLBACK_LABEL[site]

    def draw(canvas: _Canvas) -> None:
        canvas.rect(0, MAP_H, MAP_W, BAR_H, ink)
        canvas.rect(0, MAP_H, MAP_W, 1, hairline)
        canvas.rect(3, 115, 11, 11, badge)

    # The sketch nudges the count left by 2 px once it needs two digits.
    count_x = 6 if count < 10 else 4
    texts = [
        _text(str(count), count_x, 118, count_ink),
        _text(label, 19, 118, text_colour),
    ]
    return draw, texts


def _text(s: str, x: int, y: int, colour: str) -> str:
    """One run of status-bar text, y being the glyph top as the sketch treats it.

    textLength pins the run to the device's 6 px per character, so the label
    breaks at the same place it does on the board.
    """
    body = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<text x="{x}" y="{y + 7}" fill="{colour}" font-size="8" '
        f'font-family="\'JetBrains Mono\', ui-monospace, monospace" '
        f'textLength="{6 * len(s)}" lengthAdjust="spacingAndGlyphs">{body}</text>'
    )


def render(
    site: str,
    visited=(),
    current=None,
    scale: int = 3,
) -> str:
    """Returns the minimap for `site` as an inline SVG.

    visited -- landmark codes discovered so far, drawn in full colour.
    current -- code, or codes, detected on this run; drawn lightened so the new
               region is distinguishable from ones lit earlier.
    """
    try:
        data = load(site)
    except (KeyError, OSError) as exc:
        logger.warning("No minimap for {!r}: {}", site, exc)
        return ""

    visited = set(visited)
    if current is None:
        fresh = set()
    elif isinstance(current, str):
        fresh = {current}
    else:
        fresh = set(current)
    visited |= fresh

    landmarks = data["landmarks"]
    states = [
        "fresh" if lm["code"] in fresh
        else "revealed" if lm["code"] in visited
        else "locked"
        for lm in landmarks
    ]
    ink = data["palette"].get("ink", SAGRADA_INK)
    # lastVisitedId on the device; here, whatever this run turned up.
    last = next((lm["code"] for lm in landmarks if lm["code"] in fresh), None)

    defs, grid = _terrain(data, site, states)

    # Pins and the status bar go on a transparent overlay so they paint over the
    # terrain, in the sketch's own draw order.
    overlay = _Canvas(MAP_W, SCREEN_H)
    _paint_pins(overlay, data, ink, visited)
    draw_bar, texts = _status_bar(data, site, ink, visited, last)
    draw_bar(overlay)

    return (
        f'<svg class="cv-minimap" viewBox="0 0 {MAP_W} {SCREEN_H}" '
        f'width="{MAP_W * scale}" height="{SCREEN_H * scale}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-label="Minimap of {SITE_NAMES.get(site, site)}: '
        f'{len(visited)} of {len(landmarks)} landmarks found" '
        f'shape-rendering="crispEdges">'
        f"{defs}{grid}{overlay.to_svg_rects()}{''.join(texts)}"
        f"</svg>"
    )
