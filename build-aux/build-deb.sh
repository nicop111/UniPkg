#!/bin/sh
# Build UniPkg as an architecture-independent Debian package
# (dist/unipkg_<version>_all.deb). Needs only dpkg-deb, which every Debian and
# Ubuntu system has.
set -eu
cd "$(dirname "$0")/.."
umask 022

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' unipkg/__init__.py)
PKGDATADIR=/usr/share/unipkg
STAGE=build-deb/unipkg
DEB=dist/unipkg_${VERSION}_all.deb
MAINTAINER="${DEBFULLNAME:-$(git config user.name || echo UniPkg)} <${DEBEMAIL:-$(git config user.email || echo unipkg@localhost)}>"

rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" "$STAGE$PKGDATADIR/unipkg/backends" "$STAGE$PKGDATADIR/unipkg/ui"
install -m644 unipkg/*.py unipkg/*.css "$STAGE$PKGDATADIR/unipkg/"
install -m644 unipkg/backends/*.py "$STAGE$PKGDATADIR/unipkg/backends/"
install -m644 unipkg/ui/*.py "$STAGE$PKGDATADIR/unipkg/ui/"
sed -e "s|@pkgdatadir@|$PKGDATADIR|" -e '1s|.*|#!/usr/bin/python3|' bin/unipkg.in > build-deb/unipkg-launcher
install -Dm755 build-deb/unipkg-launcher "$STAGE/usr/bin/unipkg"
install -Dm644 data/io.github.unipkg.UniPkg.desktop "$STAGE/usr/share/applications/io.github.unipkg.UniPkg.desktop"
install -Dm644 data/io.github.unipkg.UniPkg.metainfo.xml "$STAGE/usr/share/metainfo/io.github.unipkg.UniPkg.metainfo.xml"
install -Dm644 data/io.github.unipkg.UniPkg.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/io.github.unipkg.UniPkg.svg"
install -Dm644 LICENSE "$STAGE/usr/share/doc/unipkg/copyright"

INSTALLED_SIZE=$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: unipkg
Version: $VERSION
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_SIZE
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.6), pkexec | policykit-1, gpgv, curl
Recommends: flatpak, snapd, gpg
Section: admin
Priority: optional
Homepage: https://github.com/nicop111/UniPkg
Description: Universal package manager for APT, .deb, Flatpak and Snap
 UniPkg is a GTK 4 / libadwaita application that manages APT packages,
 local .deb files, Flatpaks and Snaps from one window, including
 permissions, updates, repositories, verification and a security report.
CONTROL

# Byte-compile at install time and clean up the caches on removal.
cat > "$STAGE/DEBIAN/postinst" <<'SCRIPT'
#!/bin/sh
set -e
python3 -m compileall -q /usr/share/unipkg >/dev/null 2>&1 || true
SCRIPT
cat > "$STAGE/DEBIAN/prerm" <<'SCRIPT'
#!/bin/sh
set -e
find /usr/share/unipkg -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
SCRIPT
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

mkdir -p dist
dpkg-deb --root-owner-group --build "$STAGE" "$DEB"
echo "Debian package written to $DEB"
