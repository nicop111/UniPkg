#!/bin/sh
# Build UniPkg as a Flatpak, install it for the current user and export a
# single-file bundle (unipkg.flatpak). With --no-install only the bundle is
# built.
#
# Requirements:  flatpak install flathub org.flatpak.Builder org.gnome.Sdk//50 org.gnome.Platform//50
set -eu
cd "$(dirname "$0")/.."

INSTALL="--install"
if [ "${1:-}" = "--no-install" ]; then
    INSTALL=""
fi

if command -v flatpak-builder >/dev/null 2>&1; then
    BUILDER="flatpak-builder"
else
    BUILDER="flatpak run org.flatpak.Builder"
fi

$BUILDER --user $INSTALL --force-clean --disable-rofiles-fuse --repo=repo build-dir io.github.unipkg.UniPkg.json
flatpak build-bundle repo unipkg.flatpak io.github.unipkg.UniPkg \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
if [ -n "$INSTALL" ]; then
    echo "Built and installed. Run with: flatpak run io.github.unipkg.UniPkg"
fi
echo "Bundle written to unipkg.flatpak"
