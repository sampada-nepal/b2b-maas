#!/usr/bin/env python3
"""
gcode_3d.py  —  Minecraft 3D Structure → LEGO G-code Converter
===============================================================

Reads a Minecraft .nbt structure file, treats every non-air block as a RED
LEGO 2×1 brick, and generates G-code using the same pick-and-place setup as
message.py — single RED dispenser, same printer, same origin offsets.

Coordinate mapping
------------------
  Minecraft X  →  printer X  (left-right)
  Minecraft Y  →  printer Z  (height, builds upward)
  Minecraft Z  →  printer Y  (depth, front-to-back)

Build order: bottom layer first, front-to-back within each layer,
left-to-right within each depth slice.

Usage
-----
  python gcode_3d.py <structure.nbt> [output.gcode]
"""

import sys
from pathlib import Path

try:
    import nbtlib
except ImportError:
    sys.exit("Missing dependency — install it with:  pip install nbtlib")


# ═══════════════════════════════════════════════════════════════════════════════
#  PHYSICAL CONFIGURATION  ← edit these to match your printer / setup
# ═══════════════════════════════════════════════════════════════════════════════

# ── Dispenser (RED only) ───────────────────────────────────────────────────────
DISPENSER = {
    "x": 24.0,   # mm  ← same as message.py RED dispenser
    "y":  0.0,   # mm
    "z":  3.4,   # mm  ← nozzle height to grab a brick
}
DISPENSER_DWELL = 500   # ms

# ── 3D structure origin — front-left-bottom corner of the LEGO build area ─────
#   Minecraft X=0  →  printer X = WALL_ORIGIN_X
#   Minecraft Z=0  →  printer Y = WALL_ORIGIN_Y  (front face of structure)
#   Minecraft Y=0  →  printer Z = WALL_ORIGIN_Z  (bottom layer)
WALL_ORIGIN_X    = 64.0   # mm   stud-aligned, left edge of structure
WALL_ORIGIN_Y    = 32.0   # mm   stud-aligned, front edge of structure
WALL_ORIGIN_Z    =  0.0   # mm   Z of first LEGO layer (manual Z=0)

# ── LEGO 2×1 brick dimensions ──────────────────────────────────────────────────
BRICK_WIDTH      =  8.0   # mm   1-stud pitch  (short axis, X spacing)
BRICK_DEPTH      = 16.0   # mm   2-stud length (long axis, Y spacing)
BRICK_HEIGHT     =  9.6   # mm   stacking height per layer

# ── Motion ─────────────────────────────────────────────────────────────────────
SAFE_Z             = 70.0   # mm   travel height between pick and place
FEED_TRAVEL        = 20000  # mm/min  fast travel (no brick)
FEED_CARRY         = 12000  # mm/min  travel while holding a brick
FEED_APPROACH      =  4000  # mm/min  slow descent into placement zone
FEED_PUSH          =   500  # mm/min  final push onto studs
APPROACH_CLEARANCE =   6.0  # mm   start slowing this far above placement Z


# ═══════════════════════════════════════════════════════════════════════════════
#  AIR BLOCKS  (treated as empty — no brick placed)
# ═══════════════════════════════════════════════════════════════════════════════
AIR_BLOCKS = {
    "minecraft:air",
    "minecraft:cave_air",
    "minecraft:void_air",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_structure(nbt_path: str):
    """
    Parse a Minecraft .nbt structure file.

    Returns
    -------
    blocks   : list of (col_x, col_y, row) tuples for every non-air block
               col_x = Minecraft X, col_y = Minecraft Z, row = Minecraft Y
    size_x   : structure width  (Minecraft X)
    size_y   : structure height (Minecraft Y)
    size_z   : structure depth  (Minecraft Z)
    """
    print(f"  Loading NBT: {nbt_path}")
    nbt  = nbtlib.load(nbt_path)
    root = nbt.get("", nbt)

    size       = root["size"]
    palette    = root["palette"]
    raw_blocks = root["blocks"]

    size_x = int(size[0])   # Minecraft X → printer X
    size_y = int(size[1])   # Minecraft Y → printer Z (height)
    size_z = int(size[2])   # Minecraft Z → printer Y (depth)

    blocks = []
    seen   = set()

    for blk in raw_blocks:
        pos       = blk["pos"]
        state_idx = int(blk["state"])
        name      = str(palette[state_idx]["Name"])

        if name in AIR_BLOCKS:
            continue

        col_x = int(pos[0])   # Minecraft X
        row   = int(pos[1])   # Minecraft Y → height
        col_y = int(pos[2])   # Minecraft Z → depth

        key = (col_x, col_y, row)
        if key not in seen:
            seen.add(key)
            blocks.append((col_x, col_y, row))

    return blocks, size_x, size_y, size_z


# ═══════════════════════════════════════════════════════════════════════════════
#  ASCII PREVIEW
# ═══════════════════════════════════════════════════════════════════════════════

def print_preview(blocks, size_x, size_y, size_z):
    """Print a top-down slice per layer (Minecraft Y). R = brick, . = air."""
    filled = {(cx, cy, r) for cx, cy, r in blocks}
    print(f"\n  Preview (one slice per height layer — X = left-right, Z = top-to-bottom):")
    for row in range(size_y):
        print(f"\n  Layer Y={row}  (printer Z = {WALL_ORIGIN_Z + row * BRICK_HEIGHT:.1f} mm):")
        for cy in range(size_z):
            line = "".join("R" if (cx, cy, row) in filled else "." for cx in range(size_x))
            print(f"    {line}")


# ═══════════════════════════════════════════════════════════════════════════════
#  COORDINATE MATH
# ═══════════════════════════════════════════════════════════════════════════════

def brick_x(col_x: int) -> float:
    """Printer X for a given Minecraft X column.
    Spaced by BRICK_DEPTH (16mm = 2 studs) — long axis of brick runs in X."""
    return WALL_ORIGIN_X + col_x * BRICK_DEPTH

def brick_y(col_y: int) -> float:
    """Printer Y for a given Minecraft Z column (depth).
    Spaced by BRICK_WIDTH (8mm = 1 stud) — short axis of brick runs in Y."""
    return WALL_ORIGIN_Y + col_y * BRICK_WIDTH

def placement_nozzle_z(row: int) -> float:
    """Printer Z to push brick onto studs at this height row."""
    return WALL_ORIGIN_Z + row * BRICK_HEIGHT

def approach_nozzle_z(row: int) -> float:
    """Printer Z to start slowing down before final push."""
    return placement_nozzle_z(row) + APPROACH_CLEARANCE


# ═══════════════════════════════════════════════════════════════════════════════
#  G-CODE GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def generate_gcode(blocks, size_x: int, size_y: int, size_z: int) -> str:
    lines = []

    def emit(*args):
        lines.extend(args)

    def move(x=None, y=None, z=None, e=None, feed=None, comment=""):
        is_extrude = e is not None
        parts = ["G1" if is_extrude or (feed is not None and feed < FEED_APPROACH) else "G0"]
        if x    is not None: parts.append(f"X{x:.3f}")
        if y    is not None: parts.append(f"Y{y:.3f}")
        if z    is not None: parts.append(f"Z{z:.3f}")
        if e    is not None: parts.append(f"E{e:.4f}")
        if feed is not None: parts.append(f"F{int(feed)}")
        if comment:          parts.append(f"; {comment}")
        lines.append(" ".join(parts))

    total = len(blocks)

    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d at %H:%M:%S UTC")

    # ── File header ───────────────────────────────────────────────────────────
    emit(
        f"; generated by gcode_3d.py on {timestamp}",
        "; prusaslicer:gcode_flavor = marlin2",
        "; prusaslicer:printer_model = MK3S",
        f"; layer_count = {size_y}",
        "; ============================================================",
        f"; 3D LEGO G-code  —  generated by gcode_3d.py",
        f"; Structure  : {size_x} wide × {size_z} deep × {size_y} tall  (Minecraft X/Z/Y)",
        f"; Physical   : {size_x*BRICK_WIDTH:.0f} mm × {size_z*BRICK_WIDTH:.0f} mm × {size_y*BRICK_HEIGHT:.0f} mm",
        f"; Bricks     : {total} total (all RED)",
        f"; Origin     : X={WALL_ORIGIN_X:.1f}  Y={WALL_ORIGIN_Y:.1f}  Z={WALL_ORIGIN_Z:.1f}",
        f"; Dispenser  : X={DISPENSER['x']}  Y={DISPENSER['y']}  Z={DISPENSER['z']}",
        "; ============================================================",
        "",
    )

    # ── Start G-code (identical to message.py) ────────────────────────────────
    emit(
        "M73 P0 R0 Q0 S0        ; progress: 0%",
        "M201 X1000 Y1000 Z200  ; max accelerations [mm/s^2]",
        "M203 X200 Y200 Z12     ; max feedrates [mm/s]",
        "M204 P1250 T1250       ; print / travel acceleration [mm/s^2]",
        "M205 X8.00 Y8.00 Z0.40 ; jerk limits [mm/s]",
        "G21                    ; units: millimetres",
        "G90                    ; absolute positioning",
        "G28 X Y                ; home X and Y",
        "G92 X-11 Y-7           ; offset origin to working zero",
        "G92 Z3.8               ; declare current Z (manually parked before run)",
        "M211 S0                ; disable software endstops — allow negative Z",
        "M83                    ; relative extruder mode",
        "G92 E0                 ; reset extruder position",
        "",
    )
    move(z=SAFE_Z, feed=FEED_TRAVEL, comment="raise to safe travel height")
    emit(";TYPE:Travel", "")

    # Sort: bottom layer first (row ascending), then front-to-back (col_y), then left-to-right (col_x)
    sorted_blocks = sorted(blocks, key=lambda b: (b[2], b[1], b[0]))

    current_row = -1

    for idx, (col_x, col_y, row) in enumerate(sorted_blocks):
        target_x = brick_x(col_x)
        target_y = brick_y(col_y)
        place_z  = placement_nozzle_z(row)
        appr_z   = approach_nozzle_z(row)
        travel_z = SAFE_Z

        # Layer change marker
        if row != current_row:
            current_row = row
            emit(
                ";LAYER_CHANGE",
                f";Z:{place_z:.3f}",
                f";HEIGHT:{BRICK_HEIGHT:.3f}",
                f"; --- Layer {row + 1}/{size_y} ---",
            )

        pct = int(round(idx / total * 100))
        emit(f"M73 P{pct} R0 Q{pct} S0  ; progress {pct}%")
        emit(f"; ── Brick {idx+1:4d}/{total}  col_x={col_x}  col_y={col_y}  row={row}"
             f"  →  X={target_x:.1f}  Y={target_y:.1f}  Z={place_z:.1f} ──")

        # 1. Pick up from RED dispenser
        emit(";    [pick-up RED]", ";TYPE:Travel")
        move(x=DISPENSER["x"], y=DISPENSER["y"], feed=FEED_TRAVEL,
             comment="move over RED dispenser")
        move(z=DISPENSER["z"], feed=FEED_APPROACH,
             comment="descend to grab height")
        emit(f"G4 P{DISPENSER_DWELL}  ; dwell — let block seat in socket")
        move(z=travel_z, feed=FEED_CARRY, comment="rise with brick")
        emit("")

        # 2. Travel to target XY
        emit(";    [travel to brick]", ";TYPE:Custom")
        move(x=target_x, y=target_y, e=0.05, feed=FEED_CARRY,
             comment=f"position over col_x={col_x} col_y={col_y} row={row}")
        emit("G92 E0   ; reset E after travel mark")
        emit("")

        # 3. Approach
        emit(";    [place]", ";TYPE:Travel")
        move(z=appr_z, feed=FEED_APPROACH,
             comment=f"slow approach ({APPROACH_CLEARANCE:.0f} mm above target)")

        # 4. Push onto studs
        move(z=place_z, feed=FEED_PUSH, comment="push brick onto studs")
        emit("G4 P200  ; dwell 200 ms — ensure engagement")

        # 5. Retract
        emit(";TYPE:Travel")
        move(z=travel_z, feed=FEED_TRAVEL, comment="retract to travel height")
        emit("")

    # ── End G-code ────────────────────────────────────────────────────────────
    final_z = min(SAFE_Z + 10.0, 210.0)
    emit(
        "M73 P100 R0 Q100 S0  ; progress: 100%",
        "",
        "; ── All bricks placed ──────────────────────────────────────",
        ";TYPE:Travel",
    )
    move(z=final_z, feed=720, comment="raise nozzle clear of structure")
    emit(
        "M211 S1      ; re-enable software endstops",
        "M84          ; disable steppers",
        "",
    )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit("Usage: python gcode_3d.py <structure.nbt> [output.gcode]")

    nbt_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else Path(nbt_path).stem + ".gcode"

    print("=" * 60)
    print("  gcode_3d  —  Minecraft 3D Structure → LEGO G-code")
    print("=" * 60)

    blocks, size_x, size_y, size_z = parse_structure(nbt_path)

    print(f"  Structure size : {size_x} wide × {size_z} deep × {size_y} tall")
    print(f"  Non-air blocks : {len(blocks)}")
    print(f"  Physical size  : "
          f"{size_x * BRICK_WIDTH:.0f} mm wide × "
          f"{size_z * BRICK_WIDTH:.0f} mm deep × "
          f"{size_y * BRICK_HEIGHT:.0f} mm tall")

    if not blocks:
        sys.exit("No non-air blocks found.")

    print_preview(blocks, size_x, size_y, size_z)

    print(f"\n  Generating G-code …")
    gcode = generate_gcode(blocks, size_x, size_y, size_z)

    with open(out_path, "w") as f:
        f.write(gcode)

    print(f"  Written → {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
