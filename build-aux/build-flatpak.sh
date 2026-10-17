#!/bin/sh
# Build UniPkg as a Flatpak, install it for the current user and export a
# single-file bundle (unipkg.flatpak).
#
# Requirements:  flatpak install flathub org.flatpak.Builder org.gnome.Sdk//50 org.gnome.Platform//50
set -eu
cd "$(dirname "$0")/.."

if command -v flatpak-builder >/dev/null 2>&1; then
    BUILDER="flatpak-builder"
else
    BUILDER="flatpak run org.flatpak.Builder"
fi

$BUILDER --user --install --force-clean --disable-rofiles-fuse --repo=repo build-dir io.github.unipkg.UniPkg.json
flatpak build-bundle repo unipkg.flatpak io.github.unipkg.UniPkg \
    --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo
echo "Built and installed. Run with: flatpak run io.github.unipkg.UniPkg"
echo "Bundle written to unipkg.flatpak"
