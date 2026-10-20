"""The demo's scenes, in presentation order."""

from . import anywhere, checkpoint, density, startup, summary, throughput

SCENES = {
    "anywhere": anywhere.run,
    "startup": startup.run,
    "throughput": throughput.run,
    "cr": checkpoint.run,
    "density": density.run,
    "summary": summary.run,
}
