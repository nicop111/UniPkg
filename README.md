# UniPkg — universal package manager

UniPkg is a GTK 4 / libadwaita application, shipped as a Flatpak. It manages every kind of package on a Debian or Ubuntu system from one window:

| Format | Detected as | Backend |
| --- | --- | --- |
| APT | Packages that a configured repository provides | `dpkg-query`, `apt`, `apt-get`, `apt-cache` |
| Local `.deb` | Installed packages whose version no repository provides (`[installed,local]`) | same as APT |
| Flatpak | System and per-user installations | `flatpak` CLI, the app's metadata and overrides, the portal permission store, AppStream |
| Snap | All installed snaps | the snapd REST API (`/run/snapd.socket`) |

## Features

- **Installed list**: one list of everything on the system.
  - **Sorting:** click a column header (name, developer, type, source, permissions, size, last updated, version); click again to reverse.
  - **Filtering:** text search; scope (applications, manually installed, or all packages); checkbox lists for package type, source/repository and permissions. Unchecked means "all".
  - **Permission filter:** checkboxes for network, files, camera, sound, notifications, devices, background, location and system services, combined with **Any** or **All**, plus a sandbox choice: any, sandboxed only, or unrestricted only.
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

  It also shows each format's **latest release** date (Flatpak commit date, the Snap channel's release date, or the top entry of the APT changelog) and, for formats already installed, when they were last updated **on this computer**.

  "Last updated" in the installed list means the last install or update on this computer: dpkg's file list timestamp, the Flatpak deployment time, or snapd's install date.

  The best size is highlighted, and the most sandboxed, best-verified format is recommended.
- **Install from file**: `.deb`, `.snap`, `.flatpakref` and `.flatpak` files. Before a `.deb` is installed, its SHA-256 is checked against the signed repository index.
- **Uninstall**, optionally deleting data:
  - Flatpak: `--delete-data`, which removes `~/.var/app/<id>`
  - Snap: `snap remove --purge` plus `~/snap/<name>`
  - APT: `apt-get purge` plus the matching config, data and cache folders in your home folder

  For APT, the confirmation dialog lists any other packages apt would also remove.
- **Bulk uninstall**: every row in the Installed and Security lists has a checkbox. Ticking one reveals a bar with **Select All** (everything the current filters show), **Select None** and **Uninstall**; clicking the app itself still opens its details or risk assessment. The removal is batched per format (one `apt-get`, one `flatpak uninstall` per installation, one `snap remove`), so there is one password prompt per format. The confirmation lists the apps and warns about extra packages APT would also remove.
- **Protection of critical components**: these can't be removed with UniPkg:
  - APT packages dpkg marks Essential or Protected, or with priority required/important;
  - the running kernel;
  - the core of boot, desktop session, networking, administration and package management (systemd, GRUB/shim, GNOME Shell, GDM, NetworkManager, sudo, polkit, apt, snapd, flatpak, desktop meta packages);
  - base/system snaps, and UniPkg itself.

  They show a lock instead of an uninstall button or checkbox. Before any APT removal, a simulation checks whether dependencies would take a protected package along; if so, the removal is blocked and the affected components are named. For example, removing GNOME Settings would also remove GNOME Shell and GDM. The backends also refuse to build removal commands for protected packages.
- **Updates**: one list for APT, Flatpak and Snap, with **Update** buttons per app and per group, **Update All**, and **Refresh package lists** (`apt-get update`).
- **Running apps**: before an update, UniPkg checks whether the apps involved are running. Flatpak and Snap apps are matched by their systemd scope; APT apps by their executables. Background services and helpers are ignored. If any are running you can:
  - **Close and Update**: asks the apps to quit normally (SIGTERM), waits up to 10 seconds and never force-kills. Afterwards a toast offers **Reopen**.
  - **Update Anyway**: Flatpak and APT apps keep running the old version, and a toast offers **Restart**. snapd refuses to update running snaps, so those are skipped.
- **Repositories**:
  - APT sources, in both one-line `.list` and deb822 `.sources` format: enable, disable, remove, inspect signing keys, and add PPAs or URL repositories. When adding a repository, the signing key's fingerprint is shown before it is installed into `/etc/apt/keyrings`.
  - Flatpak remotes (system and user): enable, disable, add and remove.
  - The Snap Store is shown for information.
- **Security report** (fifth tab): ranks installed apps, or with **All packages** every installed package, by risk score, sorted highest first, as High (≥ 50), Medium (≥ 20) or Low. Findings:
  - not sandboxed: 15 for distribution packages, 30 for third-party, local `.deb`s and classic snaps
  - unverified origin; no automatic updates
  - pending update (more for APT security updates)
  - not updated for over a year
  - publisher not verified
  - permissions that weaken the sandbox, e.g. session bus, the Flatpak service, all files or home, all devices, X11, SSH/GPG keys

  With **All packages**, the operating system's own libraries are not flagged for lacking a sandbox. The report still covers pending security updates, third-party and locally installed packages, and **obsolete** packages: leftovers from an earlier distribution release that no longer receive security updates. These are detected as local packages maintained by Ubuntu/Debian, and the Installed list shows them as "Obsolete (no longer in the archive)" instead of "Local .deb".

  Clicking an app shows every finding with its points and what to do about it, plus shortcuts to find a safer alternative in Explore, open the app's permissions, or uninstall. Each row also has a quick uninstall button.
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
