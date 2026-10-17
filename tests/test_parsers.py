"""Unit tests for the backend parsers (no host access required).

Run with:  python3 -m unittest discover -s tests
"""

import gzip
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unipkg.backends import apt, flatpak, gpg, snap  # noqa: E402
from unipkg.models import PackageKind, SearchResult, normalize_name  # noqa: E402
from unipkg.util import format_size, parse_deb822, parse_keyfile, parse_size  # noqa: E402


class UtilTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(parse_size("1.4 MB"), 1_400_000)
        self.assertEqual(parse_size("423,6 kB"), 423_600)
        self.assertEqual(parse_size("12.9\xa0MB"), 12_900_000)
        self.assertEqual(parse_size("2 GiB"), 2 * 1024**3)
        self.assertEqual(parse_size(""), 0)
        self.assertEqual(parse_size("garbage"), 0)

    def test_format_size(self):
        self.assertEqual(format_size(0), "—")
        self.assertEqual(format_size(999), "999 B")
        self.assertEqual(format_size(1_400_000), "1.4 MB")

    def test_parse_date(self):
        from unipkg.util import parse_date

        expected = 1767352442.0  # 2026-01-02 11:14:02 UTC
        self.assertEqual(parse_date("Fri, 02 Jan 2026 12:14:02 +0100"), expected)  # changelog
        self.assertEqual(parse_date("2026-01-02 11:14:02 +0000"), expected)  # flatpak
        self.assertAlmostEqual(parse_date("2026-01-02T12:14:02.922672763+01:00"), expected + 0.922672, 3)
        self.assertEqual(parse_date("2026-01-02T11:14:02Z"), expected)  # snap store
        self.assertIsNone(parse_date("not a date"))
        self.assertIsNone(parse_date(""))

    def test_format_date(self):
        from unipkg.util import format_date

        now = 1767352442.0
        self.assertEqual(format_date(now, now), "Today")
        self.assertEqual(format_date(now - 86400, now), "Yesterday")
        self.assertEqual(format_date(now - 3 * 86400, now), "3 days ago")
        self.assertIn("2025", format_date(now - 40 * 86400, now))
        self.assertEqual(format_date(None), "—")

    def test_deb822_continuation(self):
        stanzas = parse_deb822("Package: a\nDescription: x\n more\n\nPackage: b\n")
        self.assertEqual(len(stanzas), 2)
        self.assertEqual(stanzas[0]["Description"], "x\nmore")


METADATA = """[Application]
name=com.example.App
runtime=org.gnome.Platform/x86_64/50

[Context]
shared=network;ipc;
sockets=x11;wayland;pulseaudio;
devices=dri;
filesystems=home;xdg-download:ro;~/.config/example;

[Session Bus Policy]
org.freedesktop.Notifications=talk
org.freedesktop.secrets=talk
"""

OVERRIDE = """[Context]
shared=!network;
filesystems=!home;

[Session Bus Policy]
org.freedesktop.secrets=none
"""


class FlatpakTests(unittest.TestCase):
    def _perms(self, override=None, portal=None):
        state = flatpak.ContextState()
        state.apply(parse_keyfile(METADATA), is_override=False)
        if override:
            state.apply(parse_keyfile(override), is_override=True)
        return {p.key: p for p in flatpak.build_permissions(state, portal or {})}

    def test_metadata_permissions(self):
        perms = self._perms()
        self.assertTrue(perms["shared:network"].granted)
        self.assertTrue(perms["sockets:x11"].granted)
        self.assertFalse(perms["sockets:session-bus"].granted)
        self.assertTrue(perms["filesystems:home"].granted)
        self.assertIn("read-only", perms["filesystems:xdg-download"].label)
        self.assertTrue(perms["filesystems:~/.config/example"].granted)
        self.assertTrue(perms["session:org.freedesktop.secrets"].granted)
        self.assertTrue(perms["portal:notifications"].granted)  # default allowed

    def test_overrides_revoke(self):
        perms = self._perms(OVERRIDE, {"notifications": "no", "camera": "yes"})
        self.assertFalse(perms["shared:network"].granted)
        self.assertTrue(perms["shared:network"].overridden)
        self.assertFalse(perms["filesystems:home"].granted)
        self.assertNotIn("session:org.freedesktop.secrets", perms)
        self.assertFalse(perms["portal:notifications"].granted)
        self.assertTrue(perms["portal:camera"].granted)

    def test_set_permission_commands(self):
        backend = flatpak.FlatpakBackend()
        from unipkg.models import Package

        pkg = Package(kind=PackageKind.FLATPAK, name="com.example.App", display_name="App")
        perms = self._perms()
        cmd = backend.set_permission(pkg, perms["shared:network"], False)[0]
        self.assertEqual(cmd.argv, ["flatpak", "override", "--user", "--unshare=network", "com.example.App"])
        cmd = backend.set_permission(pkg, perms["filesystems:xdg-download"], False)[0]
        self.assertEqual(cmd.argv[-2], "--nofilesystem=xdg-download")
        cmd = backend.set_permission(pkg, perms["portal:notifications"], False)[0]
        self.assertEqual(cmd.argv, ["flatpak", "permission-set", "notifications", "notification",
                                    "com.example.App", "no"])

    def test_permission_store(self):
        text = ("notifications\tnotification\tcom.slack.Slack\tyes\t0x00\n"
                "notifications\tnotification\tsnap.foo\tno\t0x00\n")
        parsed = flatpak.parse_permission_store(text)
        self.assertEqual(parsed["com.slack.Slack"]["notification"], "yes")

    def test_appstream(self):
        xml = b"""<?xml version="1.0"?><components>
        <component type="desktop-application"><id>org.gnome.Evince</id>
          <name>Document Viewer</name><name xml:lang="de">Dokumentenbetrachter</name>
          <developer id="org.gnome"><name xml:lang="be">X</name><name>The GNOME Project</name></developer>
          <icon type="cached" height="64" width="64">org.gnome.Evince.png</icon>
          <icon type="cached" height="128" width="128">org.gnome.Evince.png</icon>
          <custom><value key="flathub::verification::verified">true</value>
          <value key="flathub::verification::method">website</value>
          <value key="flathub::verification::website">gnome.org</value></custom>
        </component></components>"""
        apps = flatpak.parse_appstream(gzip.decompress(gzip.compress(xml)))
        info = apps["org.gnome.Evince"]
        self.assertEqual(info.name, "Document Viewer")
        self.assertEqual(info.developer, "The GNOME Project")
        self.assertTrue(info.verified)
        self.assertEqual(info.verification_detail, "gnome.org")


class AptTests(unittest.TestCase):
    def test_apt_list(self):
        text = ("Listing...\n"
                "anytype/now 0.53.1 amd64 [installed,local]\n"
                "libc6/resolute-updates,resolute-security,now 2.43 amd64 [installed]\n"
                "gnome-shell/now 50.1-0ubuntu1.2 amd64 [installed,upgradable to: 50.1-0ubuntu1.4]\n"
                "clang/resolute,now 1:21 amd64 [installed,automatic]\n")
        parsed = apt.parse_apt_list(text)
        self.assertIn("local", parsed[("anytype", "amd64")]["flags"])
        self.assertEqual(parsed[("libc6", "amd64")]["suites"], ["resolute-updates", "resolute-security"])
        self.assertIn("upgradable to", parsed[("gnome-shell", "amd64")]["raw_flags"])
        self.assertIn("automatic", parsed[("clang", "amd64")]["flags"])

    def test_policy_with_phased(self):
        text = """gnome-shell:
  Installed: 50.1-0ubuntu1.2
  Candidate: 50.1-0ubuntu1.4
  Version table:
     50.1-0ubuntu1.4 500 (phased 70%)
        500 http://ch.archive.ubuntu.com/ubuntu resolute-updates/main amd64 Packages
 *** 50.1-0ubuntu1.2 100
        100 /var/lib/dpkg/status
"""
        pol = apt.parse_policy(text)["gnome-shell"]
        self.assertEqual(pol["candidate"], "50.1-0ubuntu1.4")
        self.assertEqual(pol["versions"]["50.1-0ubuntu1.4"],
                         [("http://ch.archive.ubuntu.com/ubuntu", "resolute-updates/main")])
        self.assertEqual(pol["versions"]["50.1-0ubuntu1.2"], [("/var/lib/dpkg/status", "")])

    def test_policy_origins(self):
        text = """Package files:
 100 /var/lib/dpkg/status
     release a=now
 500 http://security.ubuntu.com/ubuntu resolute-security/main amd64 Packages
     release v=26.04,o=Ubuntu,a=resolute-security,n=resolute,l=Ubuntu,c=main,b=amd64
     origin security.ubuntu.com
 500 https://dl.google.com/linux/chrome/deb stable/main amd64 Packages
     release v=1.0,o=Google LLC,a=stable,n=stable,l=Google,c=main,b=amd64
 500 https://repo.example.org stable/main amd64 Packages
     release o=Example,a=stable,n=stable,c=main,b=amd64
"""
        origins = apt.parse_policy_origins(text)
        self.assertEqual(origins["archive:resolute-security"], "Ubuntu")
        self.assertNotIn("archive:stable", origins)  # ambiguous
        self.assertEqual(origins["https://dl.google.com/linux/chrome/deb stable"], "Google LLC")

    ONE_LINE = ("# comment\n"
                "deb [arch=amd64 signed-by=/usr/share/keyrings/x.gpg] https://example.org/deb stable main\n"
                "# deb http://old.example.org/ubuntu noble main\n")
    DEB822 = ("Types: deb\nURIs: http://archive.ubuntu.com/ubuntu/\nSuites: resolute resolute-updates\n"
              "Components: main universe\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n\n"
              "# second\nTypes: deb\nURIs: https://repo.example.org\nSuites: stable\nComponents: main\n"
              "Trusted: yes\n")

    def test_one_line_sources(self):
        entries = apt.parse_one_line_sources("/etc/apt/sources.list.d/x.list", self.ONE_LINE)
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0].enabled)
        self.assertEqual(entries[0].signed_by, "/usr/share/keyrings/x.gpg")
        self.assertFalse(entries[1].enabled)
        disabled = apt.set_entry_enabled(self.ONE_LINE, entries[0], False)
        self.assertIn("# deb [arch=amd64", disabled)
        enabled = apt.set_entry_enabled(self.ONE_LINE, entries[1], True)
        self.assertIn("\ndeb http://old.example.org/ubuntu noble main", enabled)
        removed = apt.remove_entry(self.ONE_LINE, entries[0])
        self.assertNotIn("example.org/deb", removed)

    def test_deb822_sources(self):
        entries = apt.parse_deb822_sources("/etc/apt/sources.list.d/ubuntu.sources", self.DEB822)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].suites, ["resolute", "resolute-updates"])
        self.assertTrue(entries[1].trusted_yes)
        disabled = apt.set_entry_enabled(self.DEB822, entries[1], False)
        self.assertIn("Trusted: yes\nEnabled: no", disabled)
        self.assertEqual(len(apt.parse_deb822_sources("f", disabled)), 2)
        self.assertFalse(apt.parse_deb822_sources("f", disabled)[1].enabled)
        reenabled = apt.set_entry_enabled(disabled, entries[1], True)
        self.assertNotIn("Enabled:", reenabled)
        removed = apt.remove_entry(self.DEB822, entries[0])
        remaining = apt.parse_deb822_sources("f", removed)
        self.assertEqual([e.uris for e in remaining], [["https://repo.example.org"]])

    def test_parse_dpkg_errors(self):
        output = ("Removing anytype (0.53.1) ...\n"
                  "dpkg: error processing package postfix (--configure):\n"
                  " old postfix package postinst maintainer script subprocess failed\n"
                  "Errors were encountered while processing:\n"
                  " postfix\n"
                  " bsd-mailx\n"
                  "E: Sub-process /usr/bin/dpkg returned an error code (1)\n")
        self.assertEqual(apt.parse_dpkg_errors(output), ["postfix", "bsd-mailx"])
        self.assertEqual(apt.parse_dpkg_errors("all fine\n"), [])

    def test_inline_key_and_lists_prefix(self):
        signed_by = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n.\nmQINBF\n-----END PGP PUBLIC KEY BLOCK-----"
        key = apt.inline_key(signed_by)
        self.assertIn("\n\nmQINBF\n", key)
        self.assertIsNone(apt.inline_key("/usr/share/keyrings/x.gpg"))
        self.assertEqual(apt.lists_prefix("http://ch.archive.ubuntu.com/ubuntu/", "resolute"),
                         "ch.archive.ubuntu.com_ubuntu_dists_resolute")


class GpgTests(unittest.TestCase):
    def test_parse_colons(self):
        text = ("pub:-:4096:1:4184DD4D907A7CAE:1497619136:1812979136::-:::scSC::::::23::0:\n"
                "fpr:::::::::6E5C05D979C76DAF93C081354184DD4D907A7CAE:\n"
                "uid:-::::1497619136::F61E::Flathub Repo Signing Key <flathub@flathub.org>::::::::::0:\n"
                "sub:-:4096:1:562702E9E3ED7EE8:1497619180:1812979180:::::s::::::23:\n"
                "fpr:::::::::54A6CDDD8919FB204200D8AC562702E9E3ED7EE8:\n")
        keys = gpg.parse_colons(text)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0].fingerprint, "6E5C05D979C76DAF93C081354184DD4D907A7CAE")
        self.assertEqual(keys[0].uids, ["Flathub Repo Signing Key <flathub@flathub.org>"])
        self.assertIn("6E5C 05D9", keys[0].describe())


class SnapTests(unittest.TestCase):
    def test_parse_assertions(self):
        text = ("type: snap-revision\nauthority-id: canonical\nsnap-sha3-384: abc\nsnap-revision: 694\n"
                "\nAcLBUgQAAQoABgUCas\n")
        parsed = snap.parse_assertions(text)
        self.assertEqual(parsed[0]["snap-sha3-384"], "abc")
        self.assertEqual(parsed[0]["snap-revision"], "694")

    def test_permissions_from_plugs(self):
        from unipkg.models import Package

        backend = snap.SnapBackend()
        pkg = Package(kind=PackageKind.SNAP, name="foo", display_name="Foo", extra={})
        plugs = [
            {"snap": "foo", "plug": "camera", "interface": "camera",
             "connections": [{"snap": "snapd", "slot": "camera"}]},
            {"snap": "foo", "plug": "home", "interface": "home"},
        ]
        perms = {p.key: p for p in backend._permissions_from_plugs(plugs, pkg)}
        self.assertTrue(perms["camera"].granted)
        self.assertFalse(perms["home"].granted)
        cmd = backend.set_permission(pkg, perms["home"], True)[0]
        self.assertEqual(cmd.argv, ["pkexec", "snap", "connect", "foo:home"])

    def test_state_changing_commands_are_privileged(self):
        from unipkg.models import Package

        backend = snap.SnapBackend()
        pkg = Package(kind=PackageKind.SNAP, name="foo", display_name="Foo", extra={})
        result = SearchResult(PackageKind.SNAP, "foo", "Foo", extra={"classic": True})
        commands = (backend.update(pkg) + backend.update_all() + backend.install(result)
                    + backend.uninstall(pkg, delete_data=False) + backend.install_file("/tmp/x.snap"))
        for cmd in commands:
            self.assertEqual(cmd.argv[:2], ["pkexec", "snap"], cmd.argv)
        # Deleting ~/snap data must run as the user, not as root.
        purge = backend.uninstall(pkg, delete_data=True)
        self.assertEqual(purge[0].argv[:2], ["pkexec", "snap"])
        self.assertNotEqual(purge[1].argv[0], "pkexec")


class GroupingTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_name("org.mozilla.firefox"), "firefox")
        self.assertEqual(normalize_name("Firefox"), "firefox")
        self.assertEqual(normalize_name("vlc-bin"), "vlcbin")

    def test_group_results(self):
        from unipkg.ui.explore import group_results

        results = [
            SearchResult(PackageKind.FLATPAK, "org.videolan.VLC", "VLC"),
            SearchResult(PackageKind.SNAP, "vlc", "VLC", extra={"common_ids": ["org.videolan.vlc"]}),
            SearchResult(PackageKind.APT, "vlc", "vlc"),
            SearchResult(PackageKind.APT, "vlc-bin", "vlc-bin"),
        ]
        groups = group_results(results)
        kinds = [r.kind for r in groups["vlc"]]
        self.assertEqual(kinds, [PackageKind.FLATPAK, PackageKind.SNAP, PackageKind.APT])
        self.assertIn("vlcbin", groups)


if __name__ == "__main__":
    unittest.main()


class RunningAppTests(unittest.TestCase):
    TABLE = (
        "100\tapp-flatpak-com.slack.Slack-123.scope\t/usr/bin/bwrap\tbwrap --args 40 slack\n"
        "101\tsnap.brave.brave-f687.scope\t/snap/brave/694/opt/brave.com/brave/brave\t/snap/brave/x\n"
        "102\tsnap.cups.cups-browsed.service\t/snap/cups/1/sbin/cups-browsed\tcups-browsed\n"
        "103\tapp-gnome-google\\x2dchrome-55.scope\t/opt/google/chrome/chrome (deleted)\tchrome\n"
        "104\tdbus.service\t/usr/bin/gnome-calculator\t/usr/bin/gnome-calculator --gapplication-service\n"
        "105\tapp-gnome-org.gnome.SettingsDaemon.X-1.scope\t/usr/libexec/gsd-disk-utility-notify\tx\n"
        "106\tapp-flatpak-io.github.unipkg.UniPkg-9.scope\t/usr/bin/bwrap\tbwrap unipkg\n"
        "garbage line\n"
    )

    def _pkg(self, kind, name, **extra):
        from unipkg.models import Package

        return Package(kind=kind, name=name, display_name=name, is_app=True, extra=extra)

    def test_match_running(self):
        from unipkg import running

        procs = running.parse_process_table(self.TABLE)
        self.assertEqual(len(procs), 7)
        self.assertEqual(procs[3].exe, "/opt/google/chrome/chrome")
        pkgs = [
            self._pkg(PackageKind.FLATPAK, "com.slack.Slack"),
            self._pkg(PackageKind.FLATPAK, "io.github.unipkg.UniPkg"),
            self._pkg(PackageKind.SNAP, "brave"),
            self._pkg(PackageKind.SNAP, "cups"),
            self._pkg(PackageKind.APT, "google-chrome-stable"),
            self._pkg(PackageKind.APT, "gnome-calculator"),
            self._pkg(PackageKind.APT, "gnome-disk-utility"),
        ]
        files = {
            "google-chrome-stable": {"/opt/google/chrome/chrome"},
            "gnome-calculator": {"/usr/bin/gnome-calculator"},
            "gnome-disk-utility": {"/usr/libexec/gsd-disk-utility-notify"},
        }
        found = {a.pkg.name: a.pids for a in running.match_running(pkgs, procs, files)}
        # Background services, libexec helpers and UniPkg itself are not "running apps".
        self.assertEqual(found, {"com.slack.Slack": [100], "brave": [101],
                                 "google-chrome-stable": [103]})

    def test_snap_running_hint(self):
        out = 'error: cannot refresh "brave": snap "brave" has running apps (brave), pids: 1'
        self.assertIn("brave is still running", snap.running_apps_hint(out))
        self.assertIsNone(snap.running_apps_hint("error: network timeout"))


class PermissionFilterTests(unittest.TestCase):
    def _pkg(self, kind, perms):
        from unipkg.models import Package, Permission, PermissionCategory as C

        cats = {"network": C.NETWORK, "camera": C.CAMERA, "audio": C.AUDIO}
        return Package(kind=kind, name="x", display_name="x", permissions=[
            Permission(k, k, cats[k], True, sensitive=True) for k in perms])

    def test_any_all_and_sandbox(self):
        from unipkg.ui.installed import matches_permission_filter as match

        net = self._pkg(PackageKind.FLATPAK, ["network"])
        net_cam = self._pkg(PackageKind.FLATPAK, ["network", "camera"])
        unconfined = self._pkg(PackageKind.APT, [])
        self.assertTrue(match(net, "any", set(), False))
        self.assertTrue(match(net, "any", {"network", "camera"}, False))     # any
        self.assertFalse(match(net, "any", {"network", "camera"}, True))     # all
        self.assertTrue(match(net_cam, "any", {"network", "camera"}, True))
        self.assertFalse(match(net, "any", {"audio"}, False))
        self.assertTrue(match(unconfined, "any", {"camera"}, True))         # unrestricted can do all
        self.assertFalse(match(unconfined, "sandboxed", set(), False))
        self.assertTrue(match(unconfined, "unrestricted", set(), False))
        self.assertFalse(match(net, "unrestricted", {"network"}, False))


class RiskTests(unittest.TestCase):
    def _pkg(self, kind, **kw):
        from unipkg.models import Package, Trust

        extra = kw.pop("extra", {})
        return Package(kind=kind, name="x", display_name="x", is_app=True,
                       trust=kw.pop("trust", Trust.SIGNED), extra=extra, **kw)

    def test_levels_and_ranking(self):
        from unipkg import risk
        from unipkg.models import Permission, PermissionCategory as C, Trust

        now = 2_000_000_000
        local_deb = self._pkg(PackageKind.DEB, trust=Trust.UNVERIFIED, updated=now - 2 * risk.YEAR)
        apt_sec = self._pkg(PackageKind.APT, update_version="2", source="Ubuntu (resolute)")
        third_party = self._pkg(PackageKind.APT, source="google-chrome (stable)")
        flatpak_ok = self._pkg(PackageKind.FLATPAK, trust=Trust.VERIFIED, updated=now)
        flatpak_escape = self._pkg(PackageKind.FLATPAK, trust=Trust.VERIFIED, updated=now, permissions=[
            Permission("sockets:session-bus", "x", C.BUS, True, sensitive=True),
            Permission("filesystems:home", "x", C.FILESYSTEM, True, sensitive=True),
            Permission("filesystems:host", "x", C.FILESYSTEM, False, sensitive=True)])  # not granted
        classic = self._pkg(PackageKind.SNAP, trust=Trust.VERIFIED, updated=now, extra={"classic": True})

        r = risk.assess(local_deb, now=now)
        self.assertEqual(r.score, 30 + 25 + 15 + 10)
        self.assertEqual(r.level, risk.RiskLevel.HIGH)
        self.assertEqual(risk.assess(apt_sec, security_update=True, now=now).score, 15 + 20)
        self.assertEqual(risk.assess(apt_sec, security_update=False, now=now).score, 15 + 5)
        self.assertEqual(risk.assess(third_party, now=now).score, 30)
        self.assertEqual(risk.assess(flatpak_ok, now=now).level, risk.RiskLevel.NONE)
        self.assertEqual(risk.assess(flatpak_escape, now=now).score, 25 + 12)
        self.assertEqual(risk.assess(classic, now=now).level, risk.RiskLevel.MEDIUM)


class BulkUninstallTests(unittest.TestCase):
    def _pkg(self, kind, name, **extra):
        from unipkg.models import Package

        return Package(kind=kind, name=name, display_name=name, extra=extra)

    def test_batched_commands(self):
        fp = flatpak.FlatpakBackend()
        cmds = fp.uninstall_many([
            self._pkg(PackageKind.FLATPAK, "a.b.C", installation="system", ref="app/a.b.C/x86_64/stable"),
            self._pkg(PackageKind.FLATPAK, "d.e.F", installation="user", ref="app/d.e.F/x86_64/stable"),
            self._pkg(PackageKind.FLATPAK, "g.h.I", installation="system", ref="app/g.h.I/x86_64/stable"),
        ], delete_data=False)
        self.assertEqual(len(cmds), 2)  # one command per installation
        self.assertEqual(cmds[0].argv[-2:], ["app/a.b.C/x86_64/stable", "app/g.h.I/x86_64/stable"])
        self.assertIn("--user", cmds[1].argv)

        sn = snap.SnapBackend()
        cmds = sn.uninstall_many([self._pkg(PackageKind.SNAP, "x"), self._pkg(PackageKind.SNAP, "y")], True)
        self.assertEqual(cmds[0].argv, ["pkexec", "snap", "remove", "--purge", "x", "y"])
        self.assertEqual(len(cmds), 3)  # + one ~/snap cleanup per snap, run as the user
        self.assertNotEqual(cmds[1].argv[0], "pkexec")

        ap = apt.AptBackend()
        cmds = ap.uninstall_many([self._pkg(PackageKind.APT, "foo"), self._pkg(PackageKind.DEB, "bar")], False)
        self.assertEqual(len(cmds), 1)  # one apt-get run, one password prompt
        self.assertTrue(cmds[0].argv[-1].endswith("foo bar"))


class ObsoletePackageTests(unittest.TestCase):
    def test_distribution_maintainer(self):
        self.assertTrue(apt.is_distribution_maintainer(
            "Ubuntu Developers <ubuntu-devel-discuss@lists.ubuntu.com>"))
        self.assertTrue(apt.is_distribution_maintainer("Debian X Team <team@debian.org>"))
        self.assertFalse(apt.is_distribution_maintainer("Docker <support@docker.com>"))
        self.assertFalse(apt.is_distribution_maintainer("Unknown"))

    def test_obsolete_library_risk(self):
        from unipkg import risk
        from unipkg.models import Package

        lib = Package(kind=PackageKind.APT, name="libicu74", display_name="libicu74",
                      source=apt.OBSOLETE_SOURCE, auto_installed=True, extra={"obsolete": True})
        current_lib = Package(kind=PackageKind.APT, name="libc6", display_name="libc6",
                              source="Ubuntu (resolute)")
        r = risk.assess(lib)
        self.assertEqual([f.title for f in r.factors], ["Obsolete, no security updates"])
        self.assertIn("autoremove", r.factors[0].recommendation)
        self.assertEqual(risk.assess(current_lib).factors, [])  # OS libraries are not flagged
        self.assertEqual(len(risk.assess_all([lib, current_lib], set())), 0)  # not apps
        self.assertEqual(len(risk.assess_all([lib, current_lib], set(), include_all=True)), 2)


class ProtectionTests(unittest.TestCase):
    def test_protection_reason(self):
        self.assertIn("Essential", apt.protection_reason("coreutils", essential="yes"))
        self.assertIn("Protected", apt.protection_reason("init", protected="yes"))
        self.assertTrue(apt.protection_reason("gnome-shell"))
        self.assertTrue(apt.protection_reason("ubuntu-desktop-minimal"))
        self.assertIn("required", apt.protection_reason("libfoo", priority="required"))
        self.assertIn("running", apt.protection_reason("linux-image-7.0.0-31-generic",
                                                        running_kernel="7.0.0-31-generic"))
        self.assertEqual(apt.protection_reason("linux-image-6.0.0-1-generic",
                                               running_kernel="7.0.0-31-generic"), "")
        self.assertEqual(apt.protection_reason("vlc", priority="optional"), "")

    def test_backends_refuse_protected(self):
        from unipkg.backends.base import BackendError
        from unipkg.models import Package

        shell = Package(kind=PackageKind.APT, name="gnome-shell", display_name="GNOME Shell",
                        extra={"protected": "The desktop shell"})
        core = Package(kind=PackageKind.SNAP, name="core22", display_name="core22",
                       extra={"protected": "Base snap"})
        with self.assertRaises(BackendError):
            apt.AptBackend().uninstall_many([shell], False)
        with self.assertRaises(BackendError):
            snap.SnapBackend().uninstall(core, False)
