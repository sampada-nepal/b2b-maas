#!/usr/bin/env python3
"""
nbt_to_gcode.py  —  Minecraft Structure → LEGO Wall G-code Converter
=====================================================================

Reads a Minecraft .nbt structure file (saved with the in-game Structure Block),
extracts non-air blocks, maps their color to a LEGO brick color (RED or YELLOW),
and generates G-code for a 3D printer equipped with a friction-fit LEGO block
placement head and two colour dispensers.

Layout assumptions
------------------
  • One Minecraft block  →  one LEGO 2×1 brick, long axis pointing INTO the wall
  • The front face of each brick is the square 8 mm end (1 stud wide × 9.6 mm tall)
  • Wall is 2 studs (16 mm) deep; studs face upward so rows interlock
  • Minecraft X axis     →  printer X  (columns, left-right along the wall)
  • Minecraft Y axis     →  printer Z  (rows, bottom-to-top height)
  • Minecraft Z axis     →  ignored    (wall is 1 block deep in the source)
  • Bricks are placed row by row, bottom-to-top, left-to-right within each row

Color mapping
-------------
  Minecraft block names are looked up in BLOCK_COLOR_MAP.
  Unmapped blocks fall back to DEFAULT_LEGO_COLOR.
  The nozzle visits the matching dispenser (DISPENSERS["RED"] or ["YELLOW"])
  for each brick.

Pick / place cycle  (per brick)
--------------------------------
  1. Travel to correct colour dispenser at SAFE_Z
  2. Descend to dispenser Z — block friction-fits into nozzle socket
  3. Dwell DISPENSER_DWELL ms
  4. Rise to SAFE_Z
  5. Travel to target XY
  6. Descend to approach height at FEED_APPROACH
  7. Push to placement depth at FEED_PUSH — block engages studs and stays
  8. Rise to SAFE_Z

Dependency
----------
  pip install nbtlib

Usage
-----
  python nbt_to_gcode.py <structure.nbt> [output.gcode]
  python nbt_to_gcode.py build.nbt               # → build.gcode
  python nbt_to_gcode.py build.nbt wall.gcode    # → wall.gcode
"""

import sys
from pathlib import Path

try:
    import nbtlib
except ImportError:
    sys.exit("Missing dependency — install it with:  pip install nbtlib")


# ═══════════════════════════════════════════════════════════════════════════════
#  PHYSICAL CONFIGURATION  ← edit these values to match your printer / setup
# ═══════════════════════════════════════════════════════════════════════════════

# ── Dispensers (one per LEGO colour) ──────────────────────────────────────────
# Coordinates are standard bed coordinates (origin = bottom-left after G28 X Y).
# Dispensers sit near the front of the bed (small Y); wall is further back.
# z is the nozzle height at which the block friction-fits into the nozzle socket.
DISPENSERS = {
    "RED": {
        "x": 39.0,   # mm  ← YELLOW + 3 studs (15 + 3×8), rightmost dispenser
        "y": 3.0,    # mm  ← calibrated via jog
        "z": -0.2,   # mm  ← tune until block grabs reliably (negative = below manual Z=0)
    },
    "YELLOW": {
        "x": 15.0,   # mm  ← calibrated via jog, leftmost dispenser
        "y": 3.0,    # mm  ← calibrated via jog
        "z": -0.2,   # mm  ← tune until block grabs reliably (negative = below manual Z=0)
    },
}
DISPENSER_DWELL  = 500    # ms   Pause at pick-up Z (lets block seat in socket)

# ── LEGO wall position ────────────────────────────────────────────────────────
# Wall runs parallel to the Y axis — columns spread along Y, rows go down in Z.
#   WALL_X   = fixed X position for every brick
#   col 0    = front-most column  →  Y = WALL_ORIGIN_Y
#   col N    = back-most  column  →  Y = WALL_ORIGIN_Y + N * BRICK_WIDTH
#   row 0    = top    row         →  Z = 0  (manually set)
#   row N    = bottom row         →  Z = −N * BRICK_HEIGHT
WALL_X           = 79.0   # mm   YELLOW + 8 studs (15 + 8×8 = 79) — stud-aligned
WALL_ORIGIN_Y    = 32.0   # mm   Y of column 0 — stud-aligned (4 × 8 mm)
WALL_ORIGIN_Z    = -5.0   # mm   Z of row 0 — 5 mm below manual Z=0 to engage studs

# ── Nozzle geometry ───────────────────────────────────────────────────────────
# NOZZLE_TO_BRICK_BOTTOM: vertical distance (mm) from the nozzle's reported Z
# position DOWN to the bottom face of the brick it is holding.
# Calibrate: lower nozzle until held brick just touches bed → read Z → that is
# NOZZLE_TO_BRICK_BOTTOM.
NOZZLE_TO_BRICK_BOTTOM = 20.0  # mm   (tune to your head design)

# Extra distance the nozzle travels BELOW the nominal brick-resting height to
# ensure studs engage (and brick releases from nozzle socket).
PUSH_EXTRA       = 1.5    # mm   typical range 0.5 – 3.0 mm

# ── LEGO 2×1 brick dimensions ─────────────────────────────────────────────────
# Bricks are oriented with their LONG axis pointing INTO the wall (Y / depth).
# The face visible on the wall is the narrow 8 mm end — approximately square.
# Wall depth = 16 mm (2 studs).  Studs face up so each row locks into the one below.
BRICK_WIDTH      =  8.0   # mm   1-stud pitch  (narrow axis, left-right along face)
BRICK_DEPTH      = 16.0   # mm   2-stud length (long axis, runs into the wall in Y)
BRICK_HEIGHT     =  9.6   # mm   row pitch     = 1 standard brick stacking height

# ── Manual Z ──────────────────────────────────────────────────────────────────
# X and Y home normally (G28 X Y).  Z is set manually: park the nozzle at
# a safe height before starting, then G92 Z0 declares it as Z=0.

# ── Motion speeds ─────────────────────────────────────────────────────────────
# NOTE: all Z values are relative to your manual Z=0 start position.
SAFE_Z           = 80.0   # mm   Z for all XY travel (clear of everything)
FEED_TRAVEL      = 9000   # mm/min  fast XY + Z travel (no brick)
FEED_CARRY       = 4000   # mm/min  travel speed while holding a brick
FEED_APPROACH    = 1500   # mm/min  slow descent before placement zone
FEED_PUSH        = 300    # mm/min  very slow final push onto studs
APPROACH_CLEARANCE = 6.0  # mm   start slowing this far above nominal place Z

# ═══════════════════════════════════════════════════════════════════════════════
#  MINECRAFT AXIS MAPPING
# ═══════════════════════════════════════════════════════════════════════════════
# For a typical Minecraft pixel-art wall (facing North/South, width along X,
# height along Y, 1 block deep along Z) the defaults below are correct.
# If your structure is rotated, swap the axis indices (0=X, 1=Y, 2=Z).

MC_COL_AXIS   = 0   # Minecraft axis → printer X  (horizontal, left-right)
MC_ROW_AXIS   = 1   # Minecraft axis → printer Z  (vertical, row height)
MC_DEPTH_AXIS = 2   # Minecraft axis that should be constant (depth = 1)

# Only blocks at this depth slice are used.  Set to None to use ALL depths
# (useful for structures more than 1 block deep — merges all slices into one layer).
MC_DEPTH_SLICE = 0  # int or None

# ═══════════════════════════════════════════════════════════════════════════════
#  BLOCK FILTER
# ═══════════════════════════════════════════════════════════════════════════════
# These Minecraft block names are treated as "empty" — no brick is placed.
AIR_BLOCKS = {
    "minecraft:air",
    "minecraft:cave_air",
    "minecraft:void_air",
}

# ═══════════════════════════════════════════════════════════════════════════════
#  COLOUR MAPPING  —  Minecraft block name → LEGO colour
# ═══════════════════════════════════════════════════════════════════════════════
# Blocks not listed here fall back to DEFAULT_LEGO_COLOR.
# Add your own entries if your pixel art uses blocks not covered below.

DEFAULT_LEGO_COLOR = "RED"   # fallback for unmapped blocks

BLOCK_COLOR_MAP: dict[str, str] = {

    # ── RED ──────────────────────────────────────────────────────────────────
    # Wool / concrete / powder
    "minecraft:red_wool":                   "RED",
    "minecraft:red_concrete":               "RED",
    "minecraft:red_concrete_powder":        "RED",
    # Terracotta / glazed
    "minecraft:red_terracotta":             "RED",
    "minecraft:red_glazed_terracotta":      "RED",
    "minecraft:terracotta":                 "RED",   # uncoloured terracotta is brownish-red
    # Glass
    "minecraft:red_stained_glass":          "RED",
    "minecraft:red_stained_glass_pane":     "RED",
    # Ore / mineral blocks
    "minecraft:redstone_block":             "RED",
    "minecraft:magma_block":                "RED",
    # Nether
    "minecraft:nether_brick":               "RED",
    "minecraft:nether_bricks":              "RED",
    "minecraft:red_nether_bricks":          "RED",
    "minecraft:red_nether_brick_slab":      "RED",
    "minecraft:netherrack":                 "RED",
    "minecraft:crimson_planks":             "RED",
    "minecraft:crimson_stem":               "RED",
    "minecraft:crimson_hyphae":             "RED",
    "minecraft:stripped_crimson_stem":      "RED",
    "minecraft:stripped_crimson_hyphae":    "RED",
    "minecraft:crimson_nylium":             "RED",
    "minecraft:shroomlight":                "RED",
    # Mushroom
    "minecraft:red_mushroom_block":         "RED",
    # Candle
    "minecraft:red_candle":                 "RED",
    # Flowers / plants (red hues)
    "minecraft:poppy":                      "RED",
    "minecraft:rose_bush":                  "RED",
    "minecraft:fire":                       "RED",
    "minecraft:soul_fire":                  "RED",
    # Pink  (closest to red of available LEGO colours)
    "minecraft:pink_wool":                  "RED",
    "minecraft:pink_concrete":              "RED",
    "minecraft:pink_concrete_powder":       "RED",
    "minecraft:pink_terracotta":            "RED",
    "minecraft:pink_glazed_terracotta":     "RED",
    "minecraft:pink_stained_glass":         "RED",
    "minecraft:pink_stained_glass_pane":    "RED",
    "minecraft:pink_candle":                "RED",
    "minecraft:peony":                      "RED",
    "minecraft:pink_petals":                "RED",
    # Magenta (closest to red)
    "minecraft:magenta_wool":               "RED",
    "minecraft:magenta_concrete":           "RED",
    "minecraft:magenta_concrete_powder":    "RED",
    "minecraft:magenta_terracotta":         "RED",
    "minecraft:magenta_glazed_terracotta":  "RED",
    "minecraft:magenta_stained_glass":      "RED",
    "minecraft:magenta_stained_glass_pane": "RED",
    "minecraft:magenta_candle":             "RED",
    "minecraft:allium":                     "RED",
    "minecraft:lilac":                      "RED",

    # ── YELLOW ───────────────────────────────────────────────────────────────
    # Wool / concrete / powder
    "minecraft:yellow_wool":                "YELLOW",
    "minecraft:yellow_concrete":            "YELLOW",
    "minecraft:yellow_concrete_powder":     "YELLOW",
    # Terracotta / glazed
    "minecraft:yellow_terracotta":          "YELLOW",
    "minecraft:yellow_glazed_terracotta":   "YELLOW",
    # Glass
    "minecraft:yellow_stained_glass":       "YELLOW",
    "minecraft:yellow_stained_glass_pane":  "YELLOW",
    # Candle
    "minecraft:yellow_candle":              "YELLOW",
    # Mineral / ore blocks
    "minecraft:gold_block":                 "YELLOW",
    "minecraft:raw_gold_block":             "YELLOW",
    "minecraft:glowstone":                  "YELLOW",
    "minecraft:light":                      "YELLOW",
    # Nature
    "minecraft:hay_block":                  "YELLOW",
    "minecraft:honeycomb_block":            "YELLOW",
    "minecraft:sponge":                     "YELLOW",
    "minecraft:wet_sponge":                 "YELLOW",
    "minecraft:bamboo_block":               "YELLOW",
    "minecraft:stripped_bamboo_block":      "YELLOW",
    # Flowers / plants (yellow hues)
    "minecraft:dandelion":                  "YELLOW",
    "minecraft:sunflower":                  "YELLOW",
    "minecraft:torchflower":                "YELLOW",
    # Pumpkin / gourd
    "minecraft:pumpkin":                    "YELLOW",
    "minecraft:carved_pumpkin":             "YELLOW",
    "minecraft:jack_o_lantern":             "YELLOW",
    # Orange  (closest to yellow of available LEGO colours)
    "minecraft:orange_wool":                "YELLOW",
    "minecraft:orange_concrete":            "YELLOW",
    "minecraft:orange_concrete_powder":     "YELLOW",
    "minecraft:orange_terracotta":          "YELLOW",
    "minecraft:orange_glazed_terracotta":   "YELLOW",
    "minecraft:orange_stained_glass":       "YELLOW",
    "minecraft:orange_stained_glass_pane":  "YELLOW",
    "minecraft:orange_candle":              "YELLOW",
    "minecraft:orange_tulip":               "YELLOW",
}

# ═══════════════════════════════════════════════════════════════════════════════
#  PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_structure(nbt_path: str):
    """
    Parse a Minecraft .nbt structure file.

    Returns
    -------
    blocks   : list of (col, row, color) tuples for every non-air block
               color is "RED" or "YELLOW" (resolved via BLOCK_COLOR_MAP)
    num_cols : total column count (width)
    num_rows : total row count (height)
    """
    print(f"  Loading NBT: {nbt_path}")
    nbt = nbtlib.load(nbt_path)

    # nbtlib exposes the root compound directly; handle both wrapped and raw forms
    root = nbt.get("", nbt)  # some files wrap root under an empty-string key

    size       = root["size"]
    palette    = root["palette"]
    raw_blocks = root["blocks"]

    num_cols = int(size[MC_COL_AXIS])
    num_rows = int(size[MC_ROW_AXIS])
    depth    = int(size[MC_DEPTH_AXIS])

    if depth > 1 and MC_DEPTH_SLICE is not None:
        print(f"  NOTE: structure is {depth} blocks deep; using depth slice {MC_DEPTH_SLICE}.")
    elif depth > 1 and MC_DEPTH_SLICE is None:
        print(f"  NOTE: structure is {depth} blocks deep; merging all depth slices.")

    unmapped: set[str] = set()
    seen: dict[tuple, str] = {}   # (col, row) → color
    blocks = []

    for blk in raw_blocks:
        pos       = blk["pos"]
        state_idx = int(blk["state"])
        name      = str(palette[state_idx]["Name"])

        if name in AIR_BLOCKS:
            continue

        depth_val = int(pos[MC_DEPTH_AXIS])
        if MC_DEPTH_SLICE is not None and depth_val != MC_DEPTH_SLICE:
            continue

        col = int(pos[MC_COL_AXIS])
        row = int(pos[MC_ROW_AXIS])

        if name not in BLOCK_COLOR_MAP:
            unmapped.add(name)
        color = BLOCK_COLOR_MAP.get(name, DEFAULT_LEGO_COLOR)

        if (col, row) not in seen:
            seen[(col, row)] = color
            blocks.append((col, row, color))

    if unmapped:
        print(f"  NOTE: {len(unmapped)} unmapped block type(s) → defaulting to "
              f"{DEFAULT_LEGO_COLOR}:")
        for name in sorted(unmapped):
            print(f"        {name}")

    return blocks, num_cols, num_rows


# ═══════════════════════════════════════════════════════════════════════════════
#  ASCII PREVIEW
# ═══════════════════════════════════════════════════════════════════════════════

def print_preview(blocks, num_cols, num_rows):
    """Print a colour-coded ASCII preview of the parsed wall (row 0 at bottom).
    R = red brick, Y = yellow brick, . = air."""
    COLOR_CHAR = {"RED": "R", "YELLOW": "Y"}
    grid = [['.' for _ in range(num_cols)] for _ in range(num_rows)]
    for col, row, color in blocks:
        if 0 <= row < num_rows and 0 <= col < num_cols:
            grid[row][col] = COLOR_CHAR.get(color, "?")

    preview_cols = min(num_cols, 80)
    print(f"\n  Preview (row 0 = bottom,  R = red  Y = yellow  . = air):")
    for row in range(num_rows - 1, -1, -1):
        line = ''.join(grid[row][:preview_cols])
        if num_cols > 80:
            line += '…'
        print(f"  {row:3d}│{line}")
    print(f"     └{'─' * preview_cols}")
    col_label = '0' + ' ' * (preview_cols - 1)
    print(f"      {col_label}")


# ═══════════════════════════════════════════════════════════════════════════════
#  COORDINATE MATH
# ═══════════════════════════════════════════════════════════════════════════════
# Origin is top-right.  X decreases leftward; Z decreases downward.

def brick_y(col: int) -> float:
    """Nozzle Y for a given column. Col 0 = front (WALL_ORIGIN_Y), col N = further back.
    Spaced by BRICK_WIDTH (8 mm = 1 stud) along Y."""
    return WALL_ORIGIN_Y + col * BRICK_WIDTH


def placement_nozzle_z(row: int) -> float:
    """
    Nozzle Z (mm) at the moment the brick is pushed onto studs.

    brick top target = WALL_ORIGIN_Z − row * BRICK_HEIGHT  (rows go downward)
    nozzle Z         = brick_top − NOZZLE_TO_BRICK_BOTTOM  (brick hangs below nozzle)
    push extra       = subtract PUSH_EXTRA (nozzle goes further down to engage studs)
    """
    brick_top_target = WALL_ORIGIN_Z - row * BRICK_HEIGHT
    return brick_top_target - NOZZLE_TO_BRICK_BOTTOM - PUSH_EXTRA


def approach_nozzle_z(row: int) -> float:
    """Nozzle Z to slow down at before the final push (above placement Z)."""
    return placement_nozzle_z(row) + APPROACH_CLEARANCE


# ═══════════════════════════════════════════════════════════════════════════════
#  G-CODE GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_gcode(blocks, num_cols: int, num_rows: int) -> str:
    lines = []

    def emit(*args):
        lines.extend(args)

    def move(x=None, y=None, z=None, e=None, feed=None, comment=""):
        # Force G1 when E is present (viewer requires G1 to render extrusion paths)
        is_extrude = e is not None
        parts = ["G1" if is_extrude or (feed is not None and feed < FEED_APPROACH) else "G0"]
        if x is not None: parts.append(f"X{x:.3f}")
        if y is not None: parts.append(f"Y{y:.3f}")
        if z is not None: parts.append(f"Z{z:.3f}")
        if e is not None: parts.append(f"E{e:.4f}")
        if feed is not None: parts.append(f"F{int(feed)}")
        if comment: parts.append(f"; {comment}")
        lines.append(" ".join(parts))

    n_red    = sum(1 for _, _, c in blocks if c == "RED")
    n_yellow = sum(1 for _, _, c in blocks if c == "YELLOW")
    total    = len(blocks)

    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d at %H:%M:%S UTC")

    # ── PrusaSlicer-compatible file header ────────────────────────────────────
    emit(
        f"; generated by PrusaSlicer 2.9.4 on {timestamp}",
        "; prusaslicer:gcode_flavor = marlin2",
        "; prusaslicer:printer_model = MK3S",
        f"; layer_count = {num_rows}",
        "; estimated printing time (normal mode) = 0s",
        "; filament used [mm] = 0",
        "; nozzle_diameter = 0",
        "; total_toolchanges = 0",
        "; ============================================================",
        f"; LEGO Wall G-code  —  generated by nbt_to_gcode.py",
        f"; Structure  : {num_cols} cols wide × {num_rows} rows tall",
        f"; Bricks     : {total} total  ({n_red} red, {n_yellow} yellow)",
        f"; Brick face : {BRICK_WIDTH:.0f} mm wide × {BRICK_HEIGHT} mm tall  (short / square end faces out)",
        f"; Wall depth : 16 mm  (2 studs — long axis of brick points inward)",
        f"; Wall X     : {WALL_X:.1f} mm (fixed)",
        f"; Wall Y     : {WALL_ORIGIN_Y:.1f} mm (front) → {WALL_ORIGIN_Y + (num_cols - 1) * BRICK_DEPTH:.1f} mm (back)",
        f"; Wall Z     : {WALL_ORIGIN_Z:.1f} (top) → {WALL_ORIGIN_Z - num_rows * BRICK_HEIGHT:.1f} mm (bottom)",
        f"; Disp RED   : X={DISPENSERS['RED']['x']}  Y={DISPENSERS['RED']['y']}  Z={DISPENSERS['RED']['z']}",
        f"; Disp YELLOW: X={DISPENSERS['YELLOW']['x']}  Y={DISPENSERS['YELLOW']['y']}  Z={DISPENSERS['YELLOW']['z']}",
        "; ============================================================",
        "",
    )

    # ── Prusa i3 MK3 start G-code ─────────────────────────────────────────────
    emit(
        "M73 P0 R0 Q0 S0        ; progress: 0% (normal + stealth modes)",
        "M201 X1000 Y1000 Z200  ; max accelerations [mm/s^2] — no E (no extruder)",
        "M203 X200 Y200 Z12     ; max feedrates [mm/s]",
        "M204 P1250 T1250       ; print / travel acceleration [mm/s^2]",
        "M205 X8.00 Y8.00 Z0.40 ; jerk limits [mm/s]",
        "G21                    ; units: millimetres",
        "G90                    ; absolute positioning",
        "G28 X Y                ; home X and Y (bottom-left = origin)",
        "G92 Z0                 ; declare current Z as Z=0 (manually parked before run)",
        "M211 S0                ; disable software endstops — allow negative Z",
        "; NOTE: M104/M109/M140/M190 omitted — no hotend/bed on this machine",
        "M83                    ; relative extruder mode (E values are incremental)",
        "G92 E0                 ; reset extruder position",
        "",
    )
    move(z=SAFE_Z, feed=FEED_TRAVEL, comment="raise to safe travel height")
    emit(";TYPE:Travel", "")

    # Sort: top row first (row 0 = top, builds downward), right to left mirrors natural X order
    sorted_blocks = sorted(blocks, key=lambda b: (b[1], -b[0]))

    current_row = -1

    for idx, (col, row, color) in enumerate(sorted_blocks):
        target_x  = WALL_X
        target_y  = brick_y(col)
        place_z   = placement_nozzle_z(row)
        appr_z    = approach_nozzle_z(row)
        disp      = DISPENSERS[color]
        layer_z   = WALL_ORIGIN_Z - row * BRICK_HEIGHT

        # ── PrusaSlicer layer-change marker (emitted once per LEGO row) ───
        if row != current_row:
            current_row = row
            emit(
                ";LAYER_CHANGE",
                f";Z:{layer_z:.3f}",
                f";HEIGHT:{BRICK_HEIGHT:.3f}",
                f"; --- Layer {row + 1}/{num_rows} ---",
            )

        # M73 progress update (parsed by MK3 LCD and PrusaSlicer viewer)
        pct = int(round(idx / total * 100))
        emit(f"M73 P{pct} R0 Q{pct} S0  ; progress {pct}%")

        emit(f"; ── Brick {idx + 1:4d}/{total}  [{color:6s}]  "
             f"col={col:3d}  row={row:3d}  →  X={target_x:.1f}  Z={layer_z:.1f} ──")

        # 1. Pick up from the correct colour dispenser ----------------------
        emit(f";    [pick-up  {color}]", ";TYPE:Travel")
        move(x=disp["x"], y=disp["y"], feed=FEED_TRAVEL,
             comment=f"move over {color} dispenser")
        move(z=disp["z"], feed=FEED_APPROACH,
             comment="descend to grab height")
        emit(f"G4 P{DISPENSER_DWELL}  ; dwell — let block seat in socket")
        move(z=SAFE_Z, feed=FEED_CARRY, comment="rise with brick (carry speed)")
        emit("")

        # 2. Travel to target XY
        emit(";    [travel to brick]", ";TYPE:Custom")
        move(x=target_x, y=target_y, e=0.05, feed=FEED_CARRY,
             comment=f"position over col={col} row={row} (carry speed)")
        emit("G92 E0   ; reset E after travel mark")
        emit("")

        # 3. Approach (slow) -----------------------------------------------
        emit(";    [place]", ";TYPE:Travel")
        move(z=appr_z, feed=FEED_APPROACH,
             comment=f"slow approach ({APPROACH_CLEARANCE:.0f} mm above target)")

        # 4. Push onto studs -----------------------------------------------
        move(z=place_z, feed=FEED_PUSH,
             comment="push brick onto studs")
        emit(f"G4 P200  ; dwell 200 ms — ensure engagement")

        # 5. Retract ───────────────────────────────────────────────────────
        emit(";TYPE:Travel")
        move(z=SAFE_Z, feed=FEED_TRAVEL, comment="retract to safe height")
        emit("")

    # ── Prusa i3 MK3 end G-code ───────────────────────────────────────────────
    final_z = min(SAFE_Z + 10.0, 210.0)   # MK3 max Z is 210 mm
    emit(
        "M73 P100 R0 Q100 S0  ; progress: 100%",
        "",
        "; ── All bricks placed ──────────────────────────────────────",
        ";TYPE:Travel",
    )
    move(z=final_z, feed=720, comment="raise nozzle clear of wall")
    emit(
        "; NOTE: M104 S0 / M140 S0 / M107 omitted — no hotend/bed on this machine",
        "M211 S1      ; re-enable software endstops",
        "M84          ; disable steppers",
        "",
    )

    # ── PrusaSlicer config block ───────────────────────────────────────────────
    emit(
        "; prusaslicer_config = begin",
        # ── Print settings ────────────────────────────────────────────────────
        "; avoid_crossing_perimeters = 0",
        "; avoid_crossing_perimeters_max_detour = 0",
        "; bottom_fill_pattern = monotonic",
        "; bottom_solid_layers = 3",
        "; bottom_solid_min_thickness = 0",
        "; bridge_acceleration = 0",
        "; bridge_angle = 0",
        "; bridge_fan_speed = 100",
        "; bridge_flow_ratio = 1",
        "; bridge_speed = 60",
        "; brim_ears = 0",
        "; brim_ears_detection_length = 1",
        "; brim_ears_max_angle = 125",
        "; brim_separation = 0",
        "; brim_type = no_brim",
        "; brim_width = 0",
        "; clip_multipart_objects = 1",
        "; colorprint_heights = ",
        "; complete_objects = 0",
        "; cooling = 1",
        "; default_acceleration = 0",
        "; disable_fan_first_layers = 3",
        "; dont_support_bridges = 1",
        "; draft_shield = disabled",
        "; draft_shield_distance = 10",
        "; duplicate_distance = 6",
        "; elefant_foot_compensation = 0",
        "; elefant_foot_min_width = 0.2",
        "; end_gcode = M84",
        "; external_perimeter_extrusion_width = 0.45",
        "; external_perimeter_speed = 25",
        "; external_perimeters_first = 0",
        "; extruder_clearance_height = 20",
        "; extruder_clearance_radius = 20",
        "; extrusion_multiplier = 1",
        f"; extrusion_width = {BRICK_WIDTH:.2f}",
        "; fan_always_on = 1",
        "; fan_below_layer_time = 100",
        "; fill_angle = 45",
        "; fill_density = 15%",
        "; fill_pattern = gyroid",
        "; first_layer_acceleration = 0",
        "; first_layer_extrusion_width = 0.42",
        "; first_layer_height = 0.2",
        "; first_layer_speed = 30",
        "; gap_fill_enabled = 1",
        "; gap_fill_speed = 20",
        "; gcode_comments = 0",
        "; gcode_flavor = marlin2",
        "; gcode_label_objects = 1",
        "; infill_acceleration = 0",
        "; infill_every_layers = 1",
        "; infill_extruder = 1",
        "; infill_extrusion_width = 0.45",
        "; infill_first = 0",
        "; infill_only_where_needed = 0",
        "; infill_overlap = 25%",
        "; infill_speed = 80",
        "; interface_shells = 0",
        "; ironing = 0",
        "; ironing_flowrate = 15%",
        "; ironing_spacing = 0.1",
        "; ironing_speed = 15",
        "; ironing_type = top",
        "; layer_gcode = ",
        f"; layer_height = {BRICK_HEIGHT:.4f}",
        "; max_print_speed = 200",
        "; max_volumetric_extrusion_rate_slope_negative = 0",
        "; max_volumetric_extrusion_rate_slope_positive = 0",
        "; max_volumetric_speed = 0",
        "; min_print_speed = 15",
        "; min_skirt_length = 4",
        "; notes = ",
        "; only_retract_when_crossing_perimeters = 0",
        "; ooze_prevention = 0",
        "; output_filename_format = {input_filename_base}.gcode",
        "; overhangs = 1",
        "; perimeter_acceleration = 0",
        "; perimeter_extruder = 1",
        "; perimeter_extrusion_width = 0.45",
        "; perimeter_speed = 45",
        "; perimeters = 2",
        "; post_process = ",
        "; print_settings_id = ",
        "; resolution = 0",
        "; seam_position = aligned",
        "; skirt_distance = 6",
        "; skirt_height = 1",
        "; skirts = 0",
        "; slowdown_below_layer_time = 5",
        "; solid_infill_below_area = 70",
        "; solid_infill_every_layers = 0",
        "; solid_infill_extruder = 1",
        "; solid_infill_extrusion_width = 0.45",
        "; solid_infill_speed = 20",
        "; spiral_vase = 0",
        "; standby_temperature_delta = -5",
        "; support_material = 0",
        "; support_material_angle = 0",
        "; support_material_auto = 1",
        "; support_material_buildplate_only = 0",
        "; support_material_contact_distance = 0.2",
        "; support_material_enforce_layers = 0",
        "; support_material_extruder = 0",
        "; support_material_extrusion_width = 0.35",
        "; support_material_interface_contact_loops = 0",
        "; support_material_interface_extruder = 0",
        "; support_material_interface_layers = 2",
        "; support_material_interface_pattern = rectilinear",
        "; support_material_interface_spacing = 0.2",
        "; support_material_interface_speed = 100%",
        "; support_material_pattern = rectilinear",
        "; support_material_spacing = 2",
        "; support_material_speed = 50",
        "; support_material_style = grid",
        "; support_material_synchronize_layers = 0",
        "; support_material_threshold = 55",
        "; support_material_with_sheath = 0",
        "; support_material_xy_spacing = 60%",
        "; thick_bridges = 1",
        "; thin_walls = 1",
        "; threads = 4",
        "; toolchange_gcode = ",
        "; top_fill_pattern = monotonic",
        "; top_infill_extrusion_width = 0.45",
        "; top_solid_infill_speed = 15",
        "; top_solid_layers = 3",
        "; top_solid_min_thickness = 0",
        "; travel_speed = 120",
        "; travel_speed_z = 0",
        "; use_firmware_retraction = 0",
        "; use_relative_e_distances = 1",
        "; use_volumetric_e = 0",
        "; variable_layer_height = 0",
        "; wipe_tower = 0",
        "; wipe_tower_bridging = 10",
        "; wipe_tower_brim_width = 2",
        "; wipe_tower_no_sparse_layers = 0",
        "; wipe_tower_rotation_angle = 0",
        "; wipe_tower_width = 60",
        "; wipe_tower_x = 170",
        "; wipe_tower_y = 140",
        "; xy_size_compensation = 0",
        # ── Filament settings ─────────────────────────────────────────────────
        "; filament_colour = #FF8000",
        "; filament_cooling_final_speed = 3.4",
        "; filament_cooling_initial_speed = 2.2",
        "; filament_cooling_moves = 4",
        "; filament_cost = 0",
        "; filament_density = 0",
        "; filament_diameter = 1.75",
        "; filament_load_time = 0",
        "; filament_loading_speed = 28",
        "; filament_loading_speed_start = 3",
        "; filament_max_volumetric_speed = 0",
        "; filament_minimal_purge_on_wipe_tower = 15",
        "; filament_notes = ",
        "; filament_settings_id = ",
        "; filament_soluble = 0",
        "; filament_spool_weight = 0",
        "; filament_toolchange_delay = 0",
        "; filament_type = PLA",
        "; filament_unload_time = 0",
        "; filament_unloading_speed = 90",
        "; filament_unloading_speed_start = 100",
        "; first_layer_bed_temperature = 0",
        "; first_layer_temperature = 0",
        "; max_fan_speed = 100",
        "; min_fan_speed = 35",
        "; temperature = 0",
        # ── Printer settings ──────────────────────────────────────────────────
        "; before_layer_gcode = ",
        "; bed_shape = 0x0,250x0,250x210,0x210",
        "; bed_temperature = 0",
        "; between_objects_gcode = ",
        "; cooling_tube_length = 5",
        "; cooling_tube_retraction = 91.5",
        "; default_filament_profile = ",
        "; default_print_profile = ",
        "; end_filament_gcode = \"; Filament-specific end gcode\"",
        "; extra_loading_move = -2",
        "; extruder_colour = #FF8000",
        "; extruder_offset = 0x0",
        "; high_current_on_filament_swap = 0",
        "; host_type = octoprint",
        "; machine_limits_usage = time_estimate_only",
        "; machine_max_acceleration_e = 5000,5000",
        "; machine_max_acceleration_extruding = 1250,1250",
        "; machine_max_acceleration_retracting = 1500,1500",
        "; machine_max_acceleration_travel = 1500,1500",
        "; machine_max_acceleration_x = 1000,1000",
        "; machine_max_acceleration_y = 1000,1000",
        "; machine_max_acceleration_z = 200,200",
        "; machine_max_feedrate_e = 120,120",
        "; machine_max_feedrate_x = 200,200",
        "; machine_max_feedrate_y = 200,200",
        "; machine_max_feedrate_z = 12,12",
        "; machine_max_jerk_e = 1.5,1.5",
        "; machine_max_jerk_x = 8,8",
        "; machine_max_jerk_y = 8,8",
        "; machine_max_jerk_z = 0.4,0.4",
        "; machine_min_extruding_rate = 0,0",
        "; machine_min_travel_rate = 0,0",
        f"; max_layer_height = {BRICK_HEIGHT:.4f}",
        "; max_print_height = 210",
        f"; min_layer_height = {BRICK_HEIGHT:.4f}",
        "; nozzle_diameter = 0.4",
        "; parking_pos_retraction = 92",
        "; printer_model = MK3S",
        "; printer_notes = ",
        "; printer_settings_id = ",
        "; printer_technology = FFF",
        "; printer_variant = 0.4",
        "; printer_vendor = Prusa Research",
        "; remaining_times = 1",
        "; retract_before_travel = 2",
        "; retract_before_wipe = 0%",
        "; retract_layer_change = 0",
        "; retract_length = 0.8",
        "; retract_length_toolchange = 4",
        "; retract_lift = 0.6",
        "; retract_lift_above = 0",
        "; retract_lift_below = 209",
        "; retract_restart_extra = 0",
        "; retract_restart_extra_toolchange = 0",
        "; retract_speed = 35",
        "; silent_mode = 0",
        "; single_extruder_multi_material = 0",
        "; start_filament_gcode = \"; Filament gcode\"",
        "; start_gcode = G28 W",
        "; wipe = 0",
        "; wipe_into_infill = 0",
        "; wipe_into_objects = 0",
        "; prusaslicer_config = end",
    )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit("Usage: python nbt_to_gcode.py <structure.nbt> [output.gcode]")

    nbt_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else Path(nbt_path).stem + ".gcode"

    print("=" * 60)
    print("  nbt_to_gcode  —  Minecraft Structure → LEGO Wall G-code")
    print("=" * 60)

    blocks, num_cols, num_rows = parse_structure(nbt_path)

    n_red    = sum(1 for _, _, c in blocks if c == "RED")
    n_yellow = sum(1 for _, _, c in blocks if c == "YELLOW")

    print(f"  Structure size : {num_cols} cols × {num_rows} rows")
    print(f"  Non-air blocks : {len(blocks)}  ({n_red} red, {n_yellow} yellow)")
    print(f"  Physical wall  : "
          f"{num_cols * BRICK_WIDTH:.0f} mm wide × "
          f"{num_rows * BRICK_HEIGHT:.0f} mm tall")

    if not blocks:
        sys.exit("No non-air blocks found.  Check MC_COL_AXIS, MC_ROW_AXIS, MC_DEPTH_SLICE.")

    print_preview(blocks, num_cols, num_rows)

    print(f"\n  Generating G-code …")
    gcode = generate_gcode(blocks, num_cols, num_rows)

    with open(out_path, "w") as f:
        f.write(gcode)

    print(f"  Written → {out_path}")
    print()
    print("  IMPORTANT: before running on the machine, update the")
    print("  PHYSICAL CONFIGURATION section at the top of this script")
    print("  with your actual printer coordinates and nozzle geometry.")
    print("=" * 60)


if __name__ == "__main__":
    main()
