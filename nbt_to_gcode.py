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
  • One Minecraft block  →  one LEGO 2×1 brick placed flat (studs up)
  • Minecraft X axis     →  printer X  (columns, left-right along the wall)
  • Minecraft Y axis     →  printer Z  (rows, bottom-to-top height)
  • Minecraft Z axis     →  ignored    (wall is 1 block deep)
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
# Set x/y/z for each dispenser to match their physical locations on your bed.
# z is the nozzle height at which the block friction-fits into the nozzle socket.
DISPENSERS = {
    "RED": {
        "x": 0.0,    # mm  ← placeholder
        "y": 0.0,    # mm  ← placeholder
        "z": 5.0,    # mm  ← tune until block grabs reliably
    },
    "YELLOW": {
        "x": 30.0,   # mm  ← placeholder
        "y": 0.0,    # mm  ← placeholder
        "z": 5.0,    # mm  ← tune until block grabs reliably
    },
}
DISPENSER_DWELL  = 500    # ms   Pause at pick-up Z (lets block seat in socket)

# ── LEGO wall origin ──────────────────────────────────────────────────────────
# Position of the bottom-left stud of the wall on your print bed.
WALL_ORIGIN_X    = 50.0   # mm   X of column 0
WALL_ORIGIN_Y    = 150.0  # mm   Y of the wall (constant — wall is 1 brick deep)
WALL_ORIGIN_Z    = 5.0    # mm   Z of the bottom face of the very first row of bricks
                           #      (= top of baseplate / stud tips that row 0 sits on)

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
BRICK_WIDTH      = 16.0   # mm   long axis  (2 studs × 8 mm)
BRICK_HEIGHT     = 9.6    # mm   stacking increment per layer (standard brick)

# ── Motion speeds ─────────────────────────────────────────────────────────────
SAFE_Z           = 80.0   # mm   Z for all XY travel (clear of everything)
FEED_TRAVEL      = 6000   # mm/min  fast XY + Z travel
FEED_APPROACH    = 800    # mm/min  slow descent before placement zone
FEED_PUSH        = 150    # mm/min  very slow final push onto studs
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
#  Z MATH
# ═══════════════════════════════════════════════════════════════════════════════

def placement_nozzle_z(row: int) -> float:
    """
    Nozzle Z (mm) at the moment the brick is pushed onto studs.

    brick_bottom target = WALL_ORIGIN_Z + row * BRICK_HEIGHT
    nozzle Z            = brick_bottom + NOZZLE_TO_BRICK_BOTTOM  (brick hangs below)
    push extra          = subtract PUSH_EXTRA (nozzle goes lower to engage studs)
    """
    brick_bottom_target = WALL_ORIGIN_Z + row * BRICK_HEIGHT
    return brick_bottom_target + NOZZLE_TO_BRICK_BOTTOM - PUSH_EXTRA


def approach_nozzle_z(row: int) -> float:
    """Nozzle Z to slow down at before the final push."""
    return placement_nozzle_z(row) + APPROACH_CLEARANCE


# ═══════════════════════════════════════════════════════════════════════════════
#  G-CODE GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_gcode(blocks, num_cols: int, num_rows: int) -> str:
    lines = []

    def emit(*args):
        lines.extend(args)

    def move(x=None, y=None, z=None, feed=None, comment=""):
        parts = ["G0" if feed is None or feed >= FEED_APPROACH else "G1"]
        if x is not None: parts.append(f"X{x:.3f}")
        if y is not None: parts.append(f"Y{y:.3f}")
        if z is not None: parts.append(f"Z{z:.3f}")
        if feed is not None: parts.append(f"F{int(feed)}")
        if comment: parts.append(f"; {comment}")
        lines.append(" ".join(parts))

    n_red    = sum(1 for _, _, c in blocks if c == "RED")
    n_yellow = sum(1 for _, _, c in blocks if c == "YELLOW")

    # ── Header ────────────────────────────────────────────────────────────────
    emit(
        "; ============================================================",
        f"; LEGO Wall G-code  —  generated by nbt_to_gcode.py",
        f"; Structure  : {num_cols} cols wide × {num_rows} rows tall",
        f"; Bricks     : {len(blocks)} total  ({n_red} red, {n_yellow} yellow)",
        f"; Brick size : {BRICK_WIDTH} mm × {BRICK_HEIGHT} mm (W × H)",
        f"; Wall X     : {WALL_ORIGIN_X:.1f} … {WALL_ORIGIN_X + num_cols * BRICK_WIDTH:.1f} mm",
        f"; Wall Z     : {WALL_ORIGIN_Z:.1f} … {WALL_ORIGIN_Z + num_rows * BRICK_HEIGHT:.1f} mm",
        f"; Disp RED   : X={DISPENSERS['RED']['x']}  Y={DISPENSERS['RED']['y']}  Z={DISPENSERS['RED']['z']}",
        f"; Disp YELLOW: X={DISPENSERS['YELLOW']['x']}  Y={DISPENSERS['YELLOW']['y']}  Z={DISPENSERS['YELLOW']['z']}",
        "; ============================================================",
        "",
        "G21        ; mm mode",
        "G90        ; absolute positioning",
        "G28        ; home all axes",
        "",
    )
    move(z=SAFE_Z, feed=FEED_TRAVEL, comment="raise to safe travel height")
    emit("")

    # Sort: bottom row first, left to right within each row
    sorted_blocks = sorted(blocks, key=lambda b: (b[1], b[0]))

    for idx, (col, row, color) in enumerate(sorted_blocks):
        target_x  = WALL_ORIGIN_X + col * BRICK_WIDTH
        target_y  = WALL_ORIGIN_Y
        place_z   = placement_nozzle_z(row)
        appr_z    = approach_nozzle_z(row)
        disp      = DISPENSERS[color]

        emit(f"; ── Brick {idx + 1:4d}/{len(sorted_blocks)}  [{color:6s}]  "
             f"col={col:3d}  row={row:3d}  →  X={target_x:.1f}  Z={WALL_ORIGIN_Z + row * BRICK_HEIGHT:.1f} ──")

        # 1. Pick up from the correct colour dispenser ----------------------
        emit(f";    [pick-up  {color}]")
        move(x=disp["x"], y=disp["y"], feed=FEED_TRAVEL,
             comment=f"move over {color} dispenser")
        move(z=disp["z"], feed=FEED_APPROACH,
             comment="descend to grab height")
        emit(f"G4 P{DISPENSER_DWELL}  ; dwell — let block seat in socket")
        move(z=SAFE_Z, feed=FEED_TRAVEL, comment="rise with brick")
        emit("")

        # 2. Travel to target XY -------------------------------------------
        emit(";    [travel]")
        move(x=target_x, y=target_y, feed=FEED_TRAVEL,
             comment=f"position over col={col} row={row}")
        emit("")

        # 3. Approach (slow) -----------------------------------------------
        emit(";    [place]")
        move(z=appr_z, feed=FEED_APPROACH,
             comment=f"slow approach ({APPROACH_CLEARANCE:.0f} mm above target)")

        # 4. Push onto studs -----------------------------------------------
        move(z=place_z, feed=FEED_PUSH,
             comment="push brick onto studs")
        emit(f"G4 P200  ; dwell 200 ms — ensure engagement")

        # 5. Retract -------------------------------------------------------
        move(z=SAFE_Z, feed=FEED_TRAVEL, comment="retract to safe height")
        emit("")

    # ── Footer ────────────────────────────────────────────────────────────────
    emit(
        "; ── All bricks placed ──────────────────────────────────────",
        "",
    )
    move(z=SAFE_Z, feed=FEED_TRAVEL)
    move(x=0, y=0, feed=FEED_TRAVEL, comment="return to home XY")
    emit("M84  ; disable steppers")

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
