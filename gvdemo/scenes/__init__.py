"""The demo's scenes, in presentation order."""

from . import anywhere, checkpoint, churn, density, startup, summary

SCENES = {
    "anywhere": anywhere.run,
    "startup": startup.run,
    "churn": churn.run,
    "cr": checkpoint.run,
    "density": density.run,
    "summary": summary.run,
}
