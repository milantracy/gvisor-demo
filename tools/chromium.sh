#!/bin/bash
# Boot systemd in a gVisor sandbox and run Chromium (Carbonyl) in it, on
# this terminal, as the systemd scene does. Ctrl-C closes Chromium; the
# sandbox is then removed.
#
#   sudo tools/chromium.sh [URL]
#
# Needs the image that the systemd scene exports once to
# /var/tmp/gvisor-demo/systemd-chromium/rootfs (run the scene first).
set -euo pipefail

URL=${1:-https://www.youtube.com/watch?v=Aq5WXmQQooo}
CID=chromium-$$
WORK=/tmp/gvdemo-chromium-$$
cd "$(dirname "$0")/.."

r() {
  runsc --root="$WORK/state" --network=host --in-sandbox-cgroup=v2 \
    --allow-suid "$@"
}

cleanup() {
  r delete -force "$CID" >/dev/null 2>&1 || true
  umount -l "$WORK/state/null-netns" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

PYTHONDONTWRITEBYTECODE=1 python3 -c \
  "from gvdemo.scenes.systemd import make_systemd_bundle; make_systemd_bundle('$WORK')"

echo "Booting systemd in gVisor…"
r run --bundle "$WORK/bundles/systemd" -detach "$CID"
until r exec "$CID" systemctl is-system-running 2>/dev/null |
    grep -qE 'running|degraded'; do
  sleep 0.2
done

r exec "$CID" systemd-run --pty --quiet --wait --unit=chromium \
  carbonyl --no-sandbox --autoplay-policy=no-user-gesture-required "$URL" ||
  true
# Leave Carbonyl's screen modes: alternate screen, mouse reporting, cursor.
printf '\e[?1049l\e[?1000l\e[?1002l\e[?1003l\e[?1006l\e[?25h'
