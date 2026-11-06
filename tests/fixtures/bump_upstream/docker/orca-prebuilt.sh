#!/bin/sh
# Invoked ONLY inside the Docker build stage. No host AppImage installation.
# OrcaSlicer is never built from source: the one bundled build installs the
# pinned official prebuilt Linux release for the target architecture, verified
# by SHA256 before extraction. Upstream publishes both x86_64 and aarch64
# AppImages; Docker's TARGETARCH spells them amd64 and arm64, so arm64 selects
# the aarch64 asset. Any other architecture (for example arm/v7, for which
# upstream ships no Linux build) fails closed.
#   orca-prebuilt.sh stable amd64|arm64
set -eu
channel=${1:-}
arch=${2:-}
case "$arch" in
    amd64|arm64) ;;
    *) echo "Orca prebuilt supports amd64 and arm64 only (got '${arch}')" >&2; exit 2 ;;
esac
case "$channel" in
    stable)
        # Print Handy ships ONE bundled OrcaSlicer: v2.5.0-rc (catduck's
        # direction, 2026-11-05: one Orca only, the stable channel pins the
        # RC, the preview channel and the beta pin are removed; there is no
        # second channel). The 2.5 line is the first whose BBL vendor
        # profiles cover the H2C and A2L (#356), and the profiles pipeline
        # reads its packed preset caches (profiles/<Vendor>.opc) directly.
        # Upstream published v2.5.0-rc on 2026-11-01; no final v2.5.0 exists
        # yet. The exact build this one channel pins is recorded in this file
        # and in the build-info file only.
        version=2.5.0-rc
        revision=af2ffb8cddd2f3c0bdadc56d9cc3945fb23577ea
        sha256_amd64=203104a3b0a880975b476ff00cfdd3a8fd2beb95fd8bfb2a9a28fb2bcbb47159
        sha256_arm64=fdd29d685ed54671a1a2887684fff4d888bd98b74809154a9731a788a7c6dced
        asset_amd64=OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.0-rc.AppImage
        asset_arm64=OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.5.0-rc.AppImage
        ;;
    *) echo 'usage: orca-prebuilt.sh stable amd64|arm64' >&2; exit 2 ;;
esac
# The source revision is shared by both architectures; the asset
# name and SHA256 are per architecture.
case "$arch" in
    amd64) sha256=$sha256_amd64; asset=$asset_amd64 ;;
    arm64) sha256=$sha256_arm64; asset=$asset_arm64 ;;
esac
mkdir -p "/work/orca-prebuilt-$channel" /out/opt /out/usr/local/bin
cd "/work/orca-prebuilt-$channel"
curl --fail --location --retry 3 --output "$asset" \
  "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${version}/${asset}"
printf '%s  %s\n' "$sha256" "$asset" | sha256sum --check --strict -
chmod 0755 "$asset"
./"$asset" --appimage-extract
# Keep upstream AppRun, resources, private libraries and environment helper intact.
test -x squashfs-root/AppRun
test -x squashfs-root/bin/orca-slicer
mv "squashfs-root" "/out/opt/orca-$channel"
# The prebuilt AppImage extracts with directories the runtime user cannot traverse
# (stat on bin/orca-slicer failed with EACCES in hosted validation); normalize the
# extracted tree to world-readable/traversable without changing upstream files.
chmod -R a+rX "/out/opt/orca-$channel"
printf '#!/bin/sh\nexec /opt/orca-%s/AppRun "$@"\n' "$channel" > "/out/usr/local/bin/orca-slicer-$channel"
chmod 0755 "/out/usr/local/bin/orca-slicer-$channel"
if [ "$channel" = stable ]; then
  ln -s orca-slicer-stable /out/usr/local/bin/orca-slicer
fi
printf '%s\n' "$revision" > "/out/orca-$channel-source-revision"
printf 'channel=%s\nsource=prebuilt\nversion=%s\nrevision=%s\narch=%s\nsha256=%s\n' \
  "$channel" "$version" "$revision" "$arch" "$sha256" > "/out/orca-$channel-build-info"
rm -f "$asset"
