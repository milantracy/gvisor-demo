"""The demo's scenes, in presentation order."""

from . import anywhere, checkpoint, density, startup, summary

SCENES = {
    "anywhere": anywhere.run,
    "startup": startup.run,
    "cr": checkpoint.run,
    "density": density.run,
    "summary": summary.run,
}
