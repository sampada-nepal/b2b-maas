#!/usr/bin/env python3
"""
trial_run.py  —  Generate G-code at very slow trial speeds.

Use this instead of nbt_to_gcode.py for your first physical test runs.
Usage is identical:

    python trial_run.py structure.nbt [output.gcode]

All speeds are reduced to ~10 % of normal so you can watch the motion
safely and stop the machine if something looks wrong.
"""

import nbt_to_gcode as ng

# ── Override motion speeds for trial run ──────────────────────────────────────
ng.FEED_TRAVEL   =  500   # mm/min   (normal: 6000)  empty travel
ng.FEED_CARRY    =  300   # mm/min   (normal: 2000)  travel while holding brick
ng.FEED_APPROACH =  100   # mm/min   (normal:  800)  slow descent above studs
ng.FEED_PUSH     =   30   # mm/min   (normal:  150)  final push onto studs

if __name__ == "__main__":
    ng.main()
