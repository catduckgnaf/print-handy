#!/bin/sh
# CI only: install the pinned upstream OrcaSlicer flatpak bundle on the pinned GNOME runtime.
#
#   scripts/install-orca-flatpak.sh
#
# Every pin comes from the environment, set once at the top of .github/workflows/ci.yml:
#   ORCA_BUNDLE_VERSION   upstream release, the same build docker/orca-prebuilt.sh pins for stable
#   ORCA_BUNDLE_SHA256    SHA256 of that release's x86_64 flatpak bundle
#   ORCA_RUNTIME_REF      the runtime the bundle needs
#   ORCA_RUNTIME_COMMIT   Flathub ostree commit of that runtime, from `flatpak remote-info`
#   ORCA_FLATPAK_APP_ID   the app id the bundle installs
# The runtime is pinned for the same reason the bundle is: Flathub rebuilds org.gnome.Platform//50
# several times a week, and a floating runtime changes what the real-slicer legs run (and how fast)
# with no change in this repository. A restored cache may hold any older commit, so the commit is
# deployed explicitly every run and proven afterwards, as the Bambu Studio leg does.
set -eu
: "${ORCA_BUNDLE_VERSION:?}" "${ORCA_BUNDLE_SHA256:?}" "${ORCA_RUNTIME_REF:?}" "${ORCA_RUNTIME_COMMIT:?}" "${ORCA_FLATPAK_APP_ID:?}"

bundle="$HOME/.cache/orcaslicer-v$ORCA_BUNDLE_VERSION.flatpak"
verify() { printf '%s  %s\n' "$ORCA_BUNDLE_SHA256" "$bundle" | sha256sum --check "$@" -; }
mkdir -p "$(dirname "$bundle")"
if [ -f "$bundle" ] && ! verify --status; then
  rm "$bundle"
fi
if [ ! -f "$bundle" ]; then
  curl --fail --location --retry 3 --output "$bundle" \
    "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v$ORCA_BUNDLE_VERSION/OrcaSlicer-Linux-flatpak_V${ORCA_BUNDLE_VERSION}_x86_64.flatpak"
fi
verify

flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
if ! flatpak info --user --show-ref "$ORCA_RUNTIME_REF" >/dev/null 2>&1; then
  flatpak install --user -y --noninteractive flathub "$ORCA_RUNTIME_REF"
fi
# A no-op when the install already is the pin, a downgrade when Flathub has moved on.
flatpak update --user -y --noninteractive --commit="$ORCA_RUNTIME_COMMIT" "$ORCA_RUNTIME_REF"
deployed="$(flatpak info --user --show-commit "$ORCA_RUNTIME_REF")"
echo "GNOME runtime deployed commit: $deployed (pinned: $ORCA_RUNTIME_COMMIT)"
test "$deployed" = "$ORCA_RUNTIME_COMMIT"

# The restored cache may already contain the app; do not reinstall its bundle.
if ! flatpak info --user --show-ref "$ORCA_FLATPAK_APP_ID" >/dev/null 2>&1; then
  flatpak install --user --bundle -y --noninteractive "$bundle"
fi
flatpak info --user --show-ref "$ORCA_FLATPAK_APP_ID"
flatpak info --user --show-commit "$ORCA_FLATPAK_APP_ID"
