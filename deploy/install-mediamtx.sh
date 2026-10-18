#!/usr/bin/env bash
# Install the pinned upstream arm64 RTSP server used by AIX5's ONVIF output.
set -euo pipefail
app_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
version=v1.21.2
archive="mediamtx_${version}_linux_arm64.tar.gz"
checksum=180a235374a548d9133cdd5e20f5dbd8b01317be1c86e0d7e66df585dc42ed6f
if [[ $(uname -m) != aarch64 ]]; then
  echo 'This installer targets the RDK X5 (Linux aarch64).' >&2
  exit 1
fi
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
curl --fail --location --retry 2 --connect-timeout 10 --max-time 900 \
  "https://github.com/bluenviron/mediamtx/releases/download/${version}/${archive}" \
  --output "$stage/$archive"
(cd "$stage" && printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check -)
tar -xzf "$stage/$archive" -C "$stage" mediamtx LICENSE
install -m 0755 "$stage/mediamtx" "$app_root/native/mediamtx.next"
mv "$app_root/native/mediamtx.next" "$app_root/native/mediamtx"
install -m 0644 "$stage/LICENSE" "$app_root/native/LICENSE.mediamtx"
"$app_root/native/mediamtx" --version
