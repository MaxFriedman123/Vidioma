#!/usr/bin/env bash
# Regenerate every app icon from the SVG masters in public/.
#
# Run this after editing public/logo.svg or public/logo-maskable.svg, then commit
# the regenerated binaries. Without it the PNGs and .ico are opaque blobs nobody
# can update.
#
# Requires rsvg-convert:  brew install librsvg
set -euo pipefail

cd "$(dirname "$0")/.."
PUBLIC="public"

command -v rsvg-convert >/dev/null || {
  echo "rsvg-convert not found. Install it with: brew install librsvg" >&2
  exit 1
}

echo "Rendering PNGs from $PUBLIC/logo.svg"
rsvg-convert -w 192 -h 192 "$PUBLIC/logo.svg" -o "$PUBLIC/logo192.png"
rsvg-convert -w 512 -h 512 "$PUBLIC/logo.svg" -o "$PUBLIC/logo512.png"

echo "Rendering the maskable (Android-cropped) variant"
rsvg-convert -w 512 -h 512 "$PUBLIC/logo-maskable.svg" -o "$PUBLIC/logo-maskable512.png"

echo "Building multi-resolution $PUBLIC/favicon.ico"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for size in 16 24 32 64; do
  rsvg-convert -w "$size" -h "$size" "$PUBLIC/logo.svg" -o "$TMP/icon_$size.png"
done

# ImageMagick isn't assumed to be installed, and the ICO container is simple
# enough to write directly. PNG-compressed entries are valid (Vista+) and are
# what the previous favicon used as well.
python3 - "$TMP" "$PUBLIC/favicon.ico" <<'PY'
import struct
import sys

tmp, out = sys.argv[1], sys.argv[2]
sizes = [16, 24, 32, 64]
images = []
for s in sizes:
    with open(f"{tmp}/icon_{s}.png", "rb") as fh:
        images.append((s, fh.read()))

offset = 6 + 16 * len(images)
entries, blobs = b"", b""
for s, data in images:
    # A width/height byte of 0 means 256; all our sizes are smaller than that.
    entries += struct.pack("<BBBBHHII", s, s, 0, 0, 1, 32, len(data), offset)
    blobs += data
    offset += len(data)

with open(out, "wb") as fh:
    fh.write(struct.pack("<HHH", 0, 1, len(images)) + entries + blobs)
PY

echo "Done. Regenerated:"
ls -la "$PUBLIC/favicon.ico" "$PUBLIC/logo192.png" "$PUBLIC/logo512.png" "$PUBLIC/logo-maskable512.png"
