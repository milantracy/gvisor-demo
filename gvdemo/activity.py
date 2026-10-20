"""The "behind the scenes" log: every runsc command and background step.

Runsc reports each command here as it starts and ends; scenes and the
sampler add notes and status lines. The panel is drawn next to (or below)
each scene's live view.
"""

import collections
import math
import threading
import time

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

# Shown once, the first time each subcommand runs.
EXPLAIN = {
    "run": "create a sandbox (sentry + gofer host processes) and start the app",
    "checkpoint": "freeze the sandbox and save its memory and kernel state "
                  "to an image",
    "restore": "start a new sandbox from a saved image, already warm",
    "kill": "send a signal to the sandbox's app",
    "delete": "remove the sandbox and free what it used",
    "list": "list the sandboxes under --root",
}


def _shorten(args):
    """A one-line label: subcommand, container ID, then the other arguments.

    The ID comes early so it survives when a narrow panel cuts the line.
    Paths are cut down to their last component.
    """
    rest = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--bundle" and i + 1 < len(args):
            rest.append(f"--bundle {args[i + 1].rstrip('/').rsplit('/', 1)[-1]}")
            skip = True
        elif a.startswith("--image-path="):
            rest.append("--image-path=" + a.split("=", 1)[1].rsplit("/", 1)[-1])
        else:
            rest.append(a)
    sub = rest.pop(0)
    # runsc takes the container ID as the first positional after the flags.
    pos = [a for a in rest if not a.startswith("-") and " " not in a]
    if pos and sub not in ("list",):
        rest.remove(pos[0])
        return " ".join([sub, pos[0], *rest])
    return " ".join([sub, *rest])


class Activity:
    def __init__(self, keep=300):
        self.lock = threading.Lock()
        self.lines = collections.deque(maxlen=keep)  # (time, Text)
        self.inflight = {}                           # token -> (label, sub, t0)
        self.status = {}                             # key -> text
        self.explained = set()
        self.header = ""
        self._next = 0

    def set_header(self, text):
        self.header = text

    def begin(self, args):
        """Record a runsc command starting. args exclude the global flags."""
        sub = next((a for a in args if not a.startswith("-")), args[0])
        label = _shorten(args)
        with self.lock:
            if sub in EXPLAIN and sub not in self.explained:
                self.explained.add(sub)
                self._add(Text.assemble(("ⓘ ", "cyan"), (sub, "bold cyan"),
                                        (f": {EXPLAIN[sub]}", "cyan")),
                          wrap=True)
            self._next += 1
            self.inflight[self._next] = (label, sub, time.monotonic())
            return self._next

    def end(self, token, rc, fail_note=None):
        """Record a command ending. fail_note marks a failure as expected."""
        with self.lock:
            label, _, t0 = self.inflight.pop(token)
            ms = f"  {(time.monotonic() - t0) * 1000:.0f} ms"
            if rc == 0:
                self._add(Text.assemble(("✓ ", "green"), (label, "white"),
                                        (ms, "grey62")))
            elif fail_note:
                self._add(Text.assemble(("– ", "grey50"), (label, "grey50"),
                                        (f"  ({fail_note})", "grey50")))
            else:
                self._add(Text.assemble(("✗ ", "red"), (label, "white"),
                                        (f"  exit {rc}{ms}", "red")))

    def note(self, text):
        with self.lock:
            self._add(Text.assemble(("• ", "yellow"), (text, "grey70")))

    def set_status(self, key, text):
        with self.lock:
            if text is None:
                self.status.pop(key, None)
            else:
                self.status[key] = text

    def clear(self):
        with self.lock:
            self.lines.clear()
            self.status.clear()

    def _add(self, text, wrap=False):
        self.lines.append((time.time(), text, wrap))

    def panel(self, height, width):
        """The panel, cut to fit height rows."""
        with self.lock:
            now = time.monotonic()
            head = []
            if self.header:
                head.append(Text(self.header, style="grey50"))
            for v in self.status.values():
                head.append(Text(v, style="magenta"))
            if self.inflight:
                counts = collections.Counter(s for _, s, _ in self.inflight.values())
                head.append(Text("running now: " + ", ".join(
                    f"{n} × {s}" for s, n in counts.most_common()), style="bold"))
                oldest = sorted(self.inflight.values(), key=lambda x: x[2])[:3]
                for label, _, t0 in oldest:
                    head.append(Text(f"  ⏳ {label}  {(now - t0) * 1000:.0f} ms",
                                     style="grey70"))
            inner = width - 4
            for t in head[1:] if self.header else head:
                t.no_wrap = True
                t.overflow = "ellipsis"
            head_rows = len(head)
            if self.header:  # the header wraps, so the full command shows
                head_rows += math.ceil(head[0].cell_len / inner) - 1
            # Newest lines last; wrapped lines may take several rows.
            room = max(1, height - 2 - head_rows - 1)
            body = []
            for t, text, wrap in reversed(self.lines):
                line = Text(time.strftime("%H:%M:%S", time.localtime(t)) +
                            f".{int(t * 10) % 10} ", style="grey42")
                line.append_text(text)
                rows = math.ceil(line.cell_len / inner) if wrap else 1
                if rows > room:
                    break
                room -= rows
                if not wrap:
                    line.no_wrap = True
                    line.overflow = "ellipsis"
                body.append(line)
            body.reverse()
        rule = Text("─" * (width - 4), style="grey30")
        return Panel(Group(*head, rule, *body), title="Behind the scenes",
                     border_style="magenta", width=width, height=height)


activity = Activity()
