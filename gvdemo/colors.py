"""Colours shared across the demo."""

# Secondary text (labels, units, notes, captions). Grey was hard to read on
# projectors; this pale blue stays readable while still quieter than white.
MUTED = "light_steel_blue1"

# A sandbox's state, as in startup latency and density. Amber, not orange:
# orange is Kata's colour. Green is gVisor's (ui.GV).
BOX = {"waiting": "grey30", "starting": "yellow3", "ready": "green3",
       "failed": "red"}

# Charts of measurements (startup histograms, density's charts): a light blue
# line over a dark blue fill, not the boxes' green.
HIST = "deep_sky_blue1"
HIST_FILL = "deep_sky_blue4"
