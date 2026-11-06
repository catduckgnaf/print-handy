#!/bin/sh
# Invoked ONLY inside the Docker build stage. No host installation.
# go2rtc is never built from source: the pinned official upstream release
# binary for the target architecture is downloaded and verified by SHA256
# before installation, following the docker/orca-prebuilt.sh precedent.
# Upstream publishes linux/amd64 and linux/arm64 builds (among others), so it
# fits the #192 multi-arch matrix; any other architecture fails closed.
#   go2rtc-prebuilt.sh amd64|arm64
set -eu
arch=${1:-}
case "$arch" in
    amd64|arm64) ;;
    *) echo "go2rtc prebuilt supports amd64 and arm64 only (got '${arch}')" >&2; exit 2 ;;
esac
version=1.9.15
revision=7c05066
sha256_amd64=20b46c6910734cb05ecefb234066f36868ff19b42ba1418a7a32e13a3e035712
sha256_arm64=af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763
asset_amd64=go2rtc_linux_amd64
asset_arm64=go2rtc_linux_arm64
case "$arch" in
    amd64) sha256=$sha256_amd64; asset=$asset_amd64 ;;
    arm64) sha256=$sha256_arm64; asset=$asset_arm64 ;;
esac
mkdir -p /work/go2rtc-prebuilt /out/usr/local/bin
cd /work/go2rtc-prebuilt
curl --fail --location --retry 3 --output "$asset" \
  "https://github.com/AlexxIT/go2rtc/releases/download/v${version}/${asset}"
printf '%s  %s\n' "$sha256" "$asset" | sha256sum --check --strict -
install -m 0755 "$asset" /out/usr/local/bin/go2rtc
chmod -R a+rX /out/usr/local/bin/go2rtc
printf '%s\n' "$revision" > /out/go2rtc-source-revision
printf 'source=prebuilt\nversion=%s\nrevision=%s\narch=%s\nsha256=%s\n' \
  "$version" "$revision" "$arch" "$sha256" > /out/go2rtc-build-info
rm -f "$asset"
