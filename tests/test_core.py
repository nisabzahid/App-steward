import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from installed_software.backends import (
    AppImageBackend,
    AptBackend,
    FlatpakBackend,
    SnapBackend,
    parse_dpkg,
    parse_flatpak,
    parse_size,
    parse_snap,
)
from installed_software.desktop import (
    DesktopEntry,
    merge_applications,
    parse_desktop,
    scan_desktops,
)
from installed_software.discovery import DiscoveryService
from installed_software.model import Application, Plan, fuzzy_match
from installed_software.operations import (
    OperationService,
    check_appimage,
    flatpak_flags,
    validate_flatpak_ref,
    validate_package,
)
from installed_software.process import CommandError, Runner


class FakeRunner:
    def __init__(self, output="", available=True, failure=False):
        self.output = output
        self.present = available
        self.failure = failure
        self.commands = []

    def available(self, name):
        return self.present

    def executable(self, name):
        if not self.present:
            raise CommandError(f"{name} is not installed")
        return f"/usr/bin/{name}"

    def run(self, argv, **kwargs):
        self.commands.append(argv)
        if self.failure:
            raise CommandError("simulated command failure")
        return self.output

    def stream(self, argv, output):
        self.commands.append(argv)
        output("simulated transaction")
        return 0


def desktop(path="/usr/share/applications/editor.desktop"):
    return DesktopEntry(
        "editor.desktop",
        path,
        "Editor",
        "Edit documents",
        "accessories-text-editor",
        "/usr/bin/editor %U",
        "Utility;",
        False,
        False,
        False,
        "",
        "",
    )


class ParsingTests(unittest.TestCase):
    def test_dpkg(self):
        data = (
            "editor:amd64\t1.2\tamd64\t100\tExample Maintainer\t"
            "ii \tno\toptional\teditors\tEditor\n Long description\x1e"
            "removed\t1\tall\t0\tNobody\trc \tno\toptional\tmisc\tOld\x1e"
        )
        applications = parse_dpkg(data)
        self.assertEqual(len(applications), 1)
        application = applications[0]
        self.assertEqual(application.package_name, "editor:amd64")
        self.assertEqual(application.size, 102400)
        self.assertIn("Long description", application.description)

    def test_dpkg_malformed(self):
        self.assertEqual(parse_dpkg("invalid\x1e"), [])

    def test_dpkg_protection(self):
        data = "libc6\t1\tamd64\t1\tDebian\tii \tyes\trequired\tlibs\tC\x1e"
        self.assertFalse(parse_dpkg(data)[0].can_uninstall)

    def test_flatpak(self):
        text = (
            "org.example.Editor\tEditor\t2\t"
            "x86_64\tstable\torg.gnome.Platform/x86_64/46\t"
            "flathub\t12.5 MB\tapp/org.example.Editor/x86_64/stable\n"
        )
        application = parse_flatpak(text, "user", "/home/u/.local/share/flatpak")[0]
        self.assertEqual(application.size, 12500000)
        self.assertEqual(application.metadata["scope"], "user")
        self.assertTrue(application.desktop_application)

    def test_flatpak_scopes_distinct(self):
        text = (
            "org.example.App\tApp\t1\tx86_64\tstable\tR\tO\t1 MB\t"
            "app/org.example.App/x86_64/stable"
        )
        user = parse_flatpak(text, "user", "/u")[0]
        system = parse_flatpak(text, "system", "/s")[0]
        self.assertNotEqual(user.id, system.id)

    def test_snap(self):
        text = (
            "Name Version Rev Tracking Publisher Notes\n"
            "editor 1.0 42 latest/stable publisher✓ -\n"
        )
        application = parse_snap(text)[0]
        self.assertEqual(application.metadata["revision"], "42")
        self.assertEqual(application.metadata["channel"], "latest/stable")

    def test_size(self):
        self.assertEqual(parse_size("1.5 KiB"), 1536)
        self.assertEqual(parse_size("2 GB"), 2000000000)
        self.assertIsNone(parse_size("-"))

    def test_unavailable_backends(self):
        runner = FakeRunner(available=False)
        for backend in (
            AptBackend(runner),
            FlatpakBackend(runner),
            SnapBackend(runner),
        ):
            result = backend.discover()
            self.assertEqual(result.applications, [])
            self.assertTrue(result.notices)
        self.assertEqual(runner.commands, [])

    def test_command_failure_isolated(self):
        service = DiscoveryService(FakeRunner(failure=True))
        service.backends = [SnapBackend(service.runner)]
        with patch("installed_software.discovery.scan_desktops", return_value=([], [])):
            result = service.discover()
        self.assertEqual(result.applications, [])
        self.assertTrue(result.notices)

    def test_runner_timeout(self):
        import subprocess

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(["query"], 1),
        ):
            with self.assertRaises(CommandError):
                Runner().run(["/nonexistent/query"], timeout=1)

    def test_runner_failure(self):
        import subprocess

        completed = subprocess.CompletedProcess(["x"], 7, "", "locked")
        with patch("subprocess.run", return_value=completed):
            with self.assertRaisesRegex(CommandError, "locked"):
                Runner().run(["x"])


class DesktopTests(unittest.TestCase):
    def test_desktop_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editor.desktop"
            path.write_text(
                "[Desktop Entry]\n"
                "Type=Application\nName=Editor\nComment=Write things\n"
                "Exec=/usr/bin/editor %U\nIcon=editor\n"
                "Categories=Utility;TextEditor;\nTerminal=false\n"
                "NoDisplay=true\n",
                encoding="utf-8",
            )
            entry = parse_desktop(path, "editor.desktop")
            self.assertEqual(entry.name, "Editor")
            self.assertTrue(entry.no_display)
            self.assertFalse(entry.visible)
            self.assertEqual(entry.executable, "/usr/bin/editor %U")

    def test_hidden_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user, system = root / "user", root / "system"
            user.mkdir()
            system.mkdir()
            (user / "x.desktop").write_text("[Desktop Entry]\nName=X\nHidden=true\n")
            (system / "x.desktop").write_text("[Desktop Entry]\nName=System X\n")
            entries, notices = scan_desktops([user, system])
            self.assertFalse(notices)
            self.assertEqual(len(entries), 1)
            self.assertTrue(entries[0].hidden)

    def test_malformed_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.desktop"
            path.write_text("not an ini file")
            entries, notices = scan_desktops([Path(directory)])
            self.assertFalse(entries)
            self.assertTrue(notices)

    def test_deduplicate_apt_desktop(self):
        application = Application(
            "apt:editor",
            "editor",
            "APT",
            package_name="editor",
            system_package=True,
        )
        entry = desktop()
        merged = merge_applications([application], [entry], {entry.path: ["editor"]})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].name, "Editor")
        self.assertTrue(merged[0].desktop_application)

    def test_same_name_not_deduplicated(self):
        applications = [
            Application("apt:editor", "Editor", "APT"),
            Application("snap:editor", "Editor", "Snap"),
        ]
        self.assertEqual(len(merge_applications(applications, [], {})), 2)

    def test_multiple_launchers_one_package(self):
        application = Application("apt:suite", "suite", "APT", package_name="suite")
        first = desktop("/usr/share/applications/first.desktop")
        second = desktop("/usr/share/applications/second.desktop")
        result = merge_applications(
            [application],
            [first, second],
            {first.path: ["suite"], second.path: ["suite"]},
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].desktop_files), 2)


class AppImageTests(unittest.TestCase):
    def create_image(self, directory):
        path = Path(directory) / "Example.AppImage"
        path.write_bytes(b"\x7fELF" + b"\x00" * 4 + b"AI\x02" + b"\x00" * 30)
        return path

    def test_discovery_and_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.create_image(directory)
            application = (
                AppImageBackend(FakeRunner(), [Path(directory)])
                .discover()
                .applications[0]
            )
            self.assertEqual(application.install_location, str(path))
            plan = OperationService(FakeRunner()).plan(application)
            self.assertIsNotNone(plan.local_delete)
            self.assertTrue(path.exists())

    def test_fake_extension_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "Fake.AppImage").write_text("not an AppImage")
            result = AppImageBackend(FakeRunner(), [Path(directory)]).discover()
            self.assertEqual(result.applications, [])

    def test_symlink_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.create_image(directory)
            (Path(directory) / "Link.AppImage").symlink_to(path)
            result = AppImageBackend(FakeRunner(), [Path(directory)]).discover()
            self.assertEqual(len(result.applications), 1)

    def test_changed_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.create_image(directory)
            application = (
                AppImageBackend(FakeRunner(), [Path(directory)])
                .discover()
                .applications[0]
            )
            plan = OperationService(FakeRunner()).plan(application)
            path.write_bytes(path.read_bytes() + b"changed")
            with self.assertRaises(PermissionError):
                check_appimage(plan.local_delete)

    def test_temporary_file_removal_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.create_image(directory)
            application = (
                AppImageBackend(FakeRunner(), [Path(directory)])
                .discover()
                .applications[0]
            )
            service = OperationService(FakeRunner())
            plan = service.plan(application)
            self.assertEqual(service.execute(plan, lambda _: None), 0)
            self.assertFalse(path.exists())


class OperationTests(unittest.TestCase):
    def test_invalid_debian_names(self):
        for name in ("-y", "x;rm -rf /", "../pkg", "pkg\nother", "pkg*"):
            with self.assertRaises(ValueError):
                validate_package(name)
        self.assertEqual(validate_package("libfoo1:amd64"), "libfoo1:amd64")

    def test_flatpak_validation(self):
        with self.assertRaises(ValueError):
            validate_flatpak_ref(
                "app/org.example.App/x86_64/../../etc", "org.example.App"
            )
        with self.assertRaises(ValueError):
            flatpak_flags("system:--bad=option")

    def test_flatpak_command(self):
        application = Application(
            "flatpak:test",
            "Test",
            "Flatpak",
            package_name="org.example.App",
            can_uninstall=True,
            metadata={
                "scope": "user",
                "ref": "app/org.example.App/x86_64/stable",
            },
        )
        plan = OperationService(FakeRunner()).plan(application)
        self.assertEqual(
            plan.argv,
            [
                "/usr/bin/flatpak",
                "--user",
                "uninstall",
                "--noninteractive",
                "--assumeyes",
                "--no-related",
                "app/org.example.App/x86_64/stable",
            ],
        )
        self.assertNotIn("--delete-data", plan.argv)

    def test_snap_command(self):
        application = Application(
            "snap:editor",
            "Editor",
            "Snap",
            package_name="editor",
            can_uninstall=True,
        )
        plan = OperationService(FakeRunner()).plan(application)
        self.assertEqual(
            plan.argv, ["/usr/bin/pkexec", "/usr/bin/snap", "remove", "editor"]
        )
        self.assertNotIn("--purge", plan.argv)

    def test_apt_update_command(self):
        application = Application(
            "apt:editor",
            "Editor",
            "APT",
            package_name="editor",
            version="1",
            can_update=True,
            update_version="2",
        )
        plan = OperationService(FakeRunner()).update_plan(application)
        self.assertEqual(
            plan.argv,
            [
                "/usr/bin/pkexec",
                "/usr/bin/apt-get",
                "install",
                "--only-upgrade",
                "--no-remove",
                "--assume-yes",
                "editor",
            ],
        )

    def test_flatpak_update_command(self):
        application = Application(
            "flatpak:test",
            "Test",
            "Flatpak",
            package_name="org.example.App",
            can_update=True,
            metadata={"scope": "user", "ref": "app/org.example.App/x86_64/stable"},
        )
        plan = OperationService(FakeRunner()).update_plan(application)
        self.assertEqual(
            plan.argv,
            [
                "/usr/bin/flatpak",
                "--user",
                "update",
                "--noninteractive",
                "--assumeyes",
                "app/org.example.App/x86_64/stable",
            ],
        )

    def test_snap_update_command(self):
        application = Application(
            "snap:editor",
            "Editor",
            "Snap",
            package_name="editor",
            can_update=True,
        )
        plan = OperationService(FakeRunner()).update_plan(application)
        self.assertEqual(
            plan.argv,
            [
                "/usr/bin/pkexec",
                "/usr/bin/snap",
                "refresh",
                "editor",
            ],
        )

    def test_apt_reinstall_command_and_source(self):
        application = Application(
            "apt:editor",
            "Editor",
            "APT",
            package_name="editor",
        )
        service = OperationService(FakeRunner())
        self.assertEqual(service.reinstall_sources(application), ["APT repositories"])
        plan = service.reinstall_plan(application, "APT repositories")
        self.assertEqual(
            plan.argv,
            [
                "/usr/bin/pkexec",
                "/usr/bin/apt-get",
                "install",
                "--reinstall",
                "--no-remove",
                "--assume-yes",
                "editor",
            ],
        )

    def test_flatpak_reinstall_command_and_source(self):
        application = Application(
            "flatpak:test",
            "Test",
            "Flatpak",
            package_name="org.example.App",
            metadata={
                "scope": "user",
                "origin": "flathub",
                "ref": "app/org.example.App/x86_64/stable",
            },
        )
        service = OperationService(FakeRunner())
        source = "Flatpak remote: flathub"
        self.assertEqual(service.reinstall_sources(application), [source])
        plan = service.reinstall_plan(application, source)
        self.assertEqual(
            plan.argv,
            [
                "/usr/bin/flatpak",
                "--user",
                "install",
                "--reinstall",
                "--noninteractive",
                "--assumeyes",
                "flathub",
                "app/org.example.App/x86_64/stable",
            ],
        )

    def test_snap_reinstall_command_and_source(self):
        application = Application(
            "snap:editor",
            "Editor",
            "Snap",
            package_name="editor",
        )
        service = OperationService(FakeRunner())
        self.assertEqual(service.reinstall_sources(application), ["Snap Store"])
        plan = service.reinstall_plan(application, "Snap Store")
        self.assertEqual(
            plan.argv,
            [
                "/usr/bin/pkexec",
                "/usr/bin/snap",
                "refresh",
                "--amend",
                "editor",
            ],
        )

    def test_invalid_snap(self):
        application = Application(
            "snap:bad",
            "Bad",
            "Snap",
            package_name="--purge",
            can_uninstall=True,
        )
        with self.assertRaises(ValueError):
            OperationService(FakeRunner()).plan(application)

    def test_apt_command(self):
        application = Application(
            "apt:editor",
            "Editor",
            "APT",
            package_name="editor",
            can_uninstall=True,
        )
        runner = FakeRunner(json.dumps({"version": "1", "digest": "a" * 64}))
        with patch("installed_software.operations.check_helper_installation"):
            plan = OperationService(runner).plan(application)
        self.assertEqual(
            plan.argv,
            [
                "/usr/bin/pkexec",
                "/usr/lib/installed-software/apt_helper.py",
                "--apply",
                "editor",
                "a" * 64,
            ],
        )
        self.assertEqual(len(runner.commands), 1)

    def test_unknown_removal_refused(self):
        application = Application("desktop:x", "X", "DesktopEntry")
        with self.assertRaises(PermissionError):
            OperationService(FakeRunner()).plan(application)

    def test_execution_uses_fake_runner(self):
        runner = FakeRunner()
        service = OperationService(runner)
        self.assertEqual(
            service.execute(Plan("x", "x", "x", ["/FAKE/manager"]), lambda _: None),
            0,
        )
        self.assertEqual(runner.commands, [["/FAKE/manager"]])

    def test_search(self):
        application = Application(
            "x", "Visual Studio Code", "APT", package_name="code", publisher="Microsoft"
        )
        self.assertTrue(fuzzy_match("vsc", application))
        self.assertTrue(fuzzy_match("microsoft apt", application))
        self.assertFalse(fuzzy_match("unrelated", application))


class HelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "privileged/apt_helper.py"
        spec = importlib.util.spec_from_file_location("apt_helper_test", path)
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def test_protection(self):
        self.assertTrue(self.helper.protected("libc6", {"Essential": "yes"}))
        self.assertTrue(self.helper.protected("pop-desktop", {}))
        self.assertTrue(self.helper.protected("linux-image-6.8.0-test", {}))
        self.assertFalse(self.helper.protected("editor", {"Priority": "optional"}))

    def test_helper_identifier_validation(self):
        with self.assertRaises(ValueError):
            self.helper.validate_package("editor; touch /tmp/bad")

    def test_helper_rejects_untrusted_location(self):
        with self.assertRaises(PermissionError):
            self.helper.trusted_installation()


if __name__ == "__main__":
    unittest.main()
