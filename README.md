# UniPkg — universal package manager

UniPkg is a GTK 4 / libadwaita application, shipped as a Flatpak. It manages every kind of package on a Debian or Ubuntu system from one window:

| Format | Detected as | Backend |
| --- | --- | --- |
| APT | Packages that a configured repository provides | `dpkg-query`, `apt`, `apt-get`, `apt-cache` |
| Local `.deb` | Installed packages whose version no repository provides (`[installed,local]`) | same as APT |
| Flatpak | System and per-user installations | `flatpak` CLI, the app's metadata and overrides, the portal permission store, AppStream |
| Snap | All installed snaps | the snapd REST API (`/run/snapd.socket`) |

## Features

- **Installed list**: one sortable, filterable list of everything on the system. You can sort by name, developer, package type, source, permissions and size, either from the "Sort by" menu or by clicking a column header. You can filter by text, package type, scope (applications, manually installed, or all packages), source/repository and permission (network, files, camera, sound, notifications, devices, background, location, unrestricted, sandboxed).
- **Permissions**
  - Flatpak: network, display sockets, sound, devices, Bluetooth, file system locations, D-Bus names and more. Changes are written as per-user overrides (`flatpak override --user`), and **Reset** removes them. Notifications, background, camera and location use the portal permission store (`flatpak permission-set`).
  - Snap: connect or disconnect each interface plug (`snap connect` / `snap disconnect`).
  - APT and `.deb` packages: shown honestly as *unrestricted*, because they cannot be sandboxed.
- **Explore**: searches APT, Flatpak and Snap at the same time and groups results that are the same app. A comparison dialog lists each format side by side:
  - version
  - download and installed size (for APT this includes any dependencies not installed yet)
  - developer and source
  - sandboxing and permissions
  - license and how it gets updates
  - signature verification

  The best size is highlighted, and the most sandboxed, best-verified format is recommended.
- **Install from file**: `.deb`, `.snap`, `.flatpakref` and `.flatpak` files. Before a `.deb` is installed, its SHA-256 is checked against the signed repository index.
- **Uninstall**, optionally deleting data:
  - Flatpak: `--delete-data`, which removes `~/.var/app/<id>`
  - Snap: `snap remove --purge` plus `~/snap/<name>`
  - APT: `apt-get purge` plus the matching config, data and cache folders in your home folder

  For APT, the confirmation dialog lists any other packages apt would also remove.
- **Updates**: one list for APT, Flatpak and Snap, with **Update** buttons per app and per group, **Update All**, and **Refresh package lists** (`apt-get update`).
- **Running apps**: before an update, UniPkg checks whether the apps involved are running. Flatpak and Snap apps are matched by their systemd scope; APT apps by their executables. Background services and helpers are ignored. If any are running you can:
  - **Close and Update**: asks the apps to quit normally (SIGTERM), waits up to 10 seconds and never force-kills. Afterwards a toast offers **Reopen**.
  - **Update Anyway**: Flatpak and APT apps keep running the old version, and a toast offers **Restart**. snapd refuses to update running snaps, so those are skipped.
- **Repositories**:
  - APT sources, in both one-line `.list` and deb822 `.sources` format: enable, disable, remove, inspect signing keys, and add PPAs or URL repositories. When adding a repository, the signing key's fingerprint is shown before it is installed into `/etc/apt/keyrings`.
  - Flatpak remotes (system and user): enable, disable, add and remove.
  - The Snap Store is shown for information.
- **Verification**:
  - **APT**: re-checks the signature of the repository's `InRelease` file with `gpgv` against that repository's `Signed-By` keyring. It flags `trusted=yes` repositories and shows key fingerprints and expiry dates. **Deep check** runs `dpkg --verify` to find modified files.
  - **Local `.deb`**: flagged as *unverified*, because no signed repository provides it.
  - **Flatpak**: checks that the remote enforces GPG verification, shows the trusted key, and shows whether Flathub has verified the developer. **Deep check** compares the installed commit with the remote's signed commit and, for user installations, runs `flatpak repair --dry-run`.
  - **Snap**: checks the store-signed `snap-revision` and `snap-declaration` assertions and whether the publisher is verified. **Deep check** hashes the `.snap` file with SHA3-384 and compares it with the signed assertion. The file is root-only, so this needs authentication.

## How it works inside the sandbox

UniPkg has to manage the host, so it runs package-manager commands there with `flatpak-spawn --host`. That requires the `--talk-name=org.freedesktop.Flatpak` permission, which **gives the app the same power as a program running outside the sandbox**. A package manager cannot work without it.

Privileged APT operations and edits to source files run through `pkexec`, so polkit asks for your password. Flatpak system operations and `snap` commands use their own polkit integration. Read-only `host-os` and `host-etc` access is used only to display icons.

The host needs the usual tools: `apt`, `dpkg`, `flatpak`, `snap`/`snapd` (each backend is optional), `pkexec`, `gpg`/`gpgv` and `curl` (for the snapd API).

## Building

```sh
# one-time: build tooling and runtimes
flatpak install flathub org.flatpak.Builder org.gnome.Sdk//50 org.gnome.Platform//50

./build-aux/build-flatpak.sh          # builds, installs for the user, writes unipkg.flatpak
flatpak run io.github.unipkg.UniPkg
```

To run straight from the source tree during development (needs PyGObject, GTK 4 and libadwaita ≥ 1.6):

```sh
python3 -m unipkg
```

## Tests

```sh
python3 -m unittest discover -s tests
```

The UI can be exercised automatically. `UNIPKG_SCREENSHOT=/tmp/shot UNIPKG_SCRIPT="wait:10;shot:installed;page:explore;search:vlc;wait:8;compare:0;wait:8;shot:compare" python3 -m unipkg` renders PNG screenshots (see `unipkg/devtools.py`).

## Layout

```
unipkg/
  host.py          run commands on the host (flatpak-spawn / pkexec), streaming output
  models.py        Package, Permission, Repository, SearchResult, Verification
  manager.py       loads packages and updates from all backends
  backends/        apt.py (APT + local .deb), flatpak.py, snap.py, gpg.py
  ui/              window, installed list, details, explore/compare, updates, repositories
data/              desktop file, AppStream metainfo, icon
io.github.unipkg.UniPkg.json   Flatpak manifest
```

License: GPL-3.0-or-later
