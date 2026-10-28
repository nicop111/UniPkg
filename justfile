# UniPkg development and packaging tasks. Run `just` to list them.

app_id := "io.github.unipkg.UniPkg"
version := `sed -n 's/^__version__ = "\(.*\)"/\1/p' unipkg/__init__.py`
deb := "dist/unipkg_" + version + "_all.deb"

# List all recipes
default:
    @just --list --unsorted

# Install the libraries needed to run UniPkg from source
deps:
    sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1

# Install the tools, Flathub remote and GNOME SDK needed to build the Flatpak
deps-flatpak:
    sudo apt install git flatpak
    flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
    flatpak install --user -y flathub org.flatpak.Builder org.gnome.Sdk//50 org.gnome.Platform//50

# Run UniPkg from the source tree
run *args:
    python3 -m unipkg {{args}}

# Run the unit tests
test:
    python3 -m unittest discover -s tests

# Build the Debian package and the Flatpak bundle
build: build-deb build-flatpak

# Build the Debian package (dist/unipkg_<version>_all.deb)
build-deb:
    ./build-aux/build-deb.sh

# Build the Flatpak bundle (unipkg.flatpak) without installing it
build-flatpak:
    ./build-aux/build-flatpak.sh --no-install

# Build and install (or update) the Debian package system-wide
install-deb: build-deb
    #!/usr/bin/env bash
    set -euo pipefail
    # apt's sandbox user (_apt) can't read inside a private home directory,
    # so hand it a copy from a world-readable temp dir instead.
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    chmod 755 "$tmp"
    install -m 644 {{deb}} "$tmp/"
    sudo apt install --reinstall "$tmp/$(basename {{deb}})"

# Pull the latest source, then rebuild and reinstall the Debian package
update-deb: pull install-deb

# Remove the Debian package
uninstall-deb:
    sudo apt remove unipkg

# Build and install (or update) the Flatpak for the current user
install-flatpak:
    ./build-aux/build-flatpak.sh

# Pull the latest source, then rebuild and reinstall the Flatpak
update-flatpak: pull install-flatpak

# Remove the Flatpak together with its data
uninstall-flatpak:
    flatpak uninstall --user --delete-data {{app_id}}

# Run the installed Flatpak
run-flatpak:
    flatpak run {{app_id}}

# Fetch the latest source from git
pull:
    git pull --ff-only

# Delete build output
clean:
    rm -rf build-deb dist build-dir repo .flatpak-builder unipkg.flatpak
    find . -name __pycache__ -type d -prune -exec rm -rf {} +
