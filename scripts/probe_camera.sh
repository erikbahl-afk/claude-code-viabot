#!/usr/bin/env bash
# Report what the USB camera can actually do, so config/config.yaml can be set
# to a mode the hardware supports instead of one ffmpeg will reject.
#
#   ./scripts/probe_camera.sh [/dev/video0]

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DEVICE="${1:-/dev/video0}"

step "USB devices"
if command -v lsusb >/dev/null; then lsusb; else warn "lsusb not installed"; fi

step "Video devices"
if command -v v4l2-ctl >/dev/null; then
  v4l2-ctl --list-devices || true
else
  warn "v4l2-ctl not installed — run: sudo apt install v4l-utils"
  ls -l /dev/video* 2>/dev/null || warn "no /dev/video* nodes at all"
fi

[[ -e "$DEVICE" ]] || die "$DEVICE does not exist. Pick one of the nodes listed above."

step "Formats and resolutions supported by $DEVICE"
if command -v v4l2-ctl >/dev/null; then
  v4l2-ctl -d "$DEVICE" --list-formats-ext || true
fi

step "Suggested config/config.yaml camera block"
if command -v v4l2-ctl >/dev/null; then
  v4l2-ctl -d "$DEVICE" --list-formats-ext 2>/dev/null | python3 - "$DEVICE" <<'PY'
import re, sys

device = sys.argv[1]
text = sys.stdin.read()

# Collect MJPEG sizes with their advertised frame intervals. MJPEG is what we
# want: the camera compresses on-board, so the Pi does not have to.
blocks = re.split(r"\n\s*\[\d+\]:", text)
best = None
for block in blocks:
    if "MJPG" not in block.upper() and "Motion-JPEG" not in block:
        continue
    for match in re.finditer(r"Size: Discrete (\d+)x(\d+)(.*?)(?=Size: Discrete|\Z)", block, re.S):
        w, h = int(match.group(1)), int(match.group(2))
        fps = [float(f) for f in re.findall(r"\(([\d.]+) fps\)", match.group(3))]
        top = max(fps) if fps else 0.0
        # Prefer a widescreen frame: the rig is looking down a driving lane, so
        # horizontal field of view is what tells a ramp from a corner, and a
        # 5:4 mode of the same pixel count throws that away for dead height.
        # Cap at 1280 wide — above that the Pi 4 struggles to burn in the
        # timestamp overlay in real time.
        widescreen = abs((w / h) - 16 / 9) < 0.06
        score = (1 if widescreen else 0, w * h)
        if top >= 10 and w <= 1280 and (best is None or score > best[3]):
            best = (w, h, top, score)

if best is None:
    print("  Could not find an MJPEG mode at 10 fps or better.")
    print("  Set camera.mode: copy in config/config.yaml and pick a size from the list above.")
else:
    w, h, fps = best[0], best[1], best[2]
    print("camera:")
    print(f"  device: {device}")
    print(f"  width: {w}")
    print(f"  height: {h}")
    print("  fps: 10          # written to disk")
    print("  capture_fps: null  # let the driver use its own rate")
    print("  mode: overlay")
    print(f"#  (camera offers this size at {fps:.0f} fps)")
PY
fi

step "10-second recording test"
OUT="$(mktemp -d)/probe.mkv"
if command -v ffmpeg >/dev/null; then
  info "writing $OUT"
  if ffmpeg -hide_banner -loglevel error -f v4l2 -input_format mjpeg -i "$DEVICE" \
       -t 10 -c:v copy "$OUT" 2>&1; then
    ok "captured $(du -h "$OUT" | cut -f1) — copy it off and confirm it plays"
  else
    warn "capture failed; the format list above shows what this camera accepts"
  fi
else
  warn "ffmpeg not installed — run scripts/setup.sh first"
fi
