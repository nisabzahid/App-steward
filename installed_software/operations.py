import json
import os
import re
import shlex
import stat
from pathlib import Path

from .backends import appimage_signature, fingerprint
from .model import Application, Plan
from .process import Runner

INSTALLED_HELPER = Path("/usr/lib/installed-software/apt_helper.py")
PACKAGE = re.compile(
    r"[a-z0-9][a-z0-9+.-]+(?::[a-z0-9][a-z0-9-]*)?\Z"
)
SNAP = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:_[a-z0-9]+)?\Z")
FLATPAK_ID = re.compile(
    r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}\Z"
)
REF_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def validate_package(name: str) -> str:
    if not PACKAGE.fullmatch(name):
        raise ValueError("Invalid Debian package identifier.")
    return name


def flatpak_flags(scope: str) -> list[str]:
    if scope == "user":
        return ["--user"]
    if scope == "system":
        return ["--system"]
    if scope.startswith("system:"):
        name = scope.split(":", 1)[1]
        if re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            return [f"--installation={name}"]
    raise ValueError("Invalid Flatpak installation scope.")


def validate_flatpak_ref(ref: str, app_id: str) -> str:
    parts = ref.split("/")
    if (
        len(parts) != 4
        or parts[0] != "app"
        or parts[1] != app_id
        or not FLATPAK_ID.fullmatch(app_id)
        or not REF_PART.fullmatch(parts[2])
        or not REF_PART.fullmatch(parts[3])
    ):
        raise ValueError("Invalid Flatpak application reference.")
    return ref


def check_helper_installation() -> None:
    if not INSTALLED_HELPER.is_file() or INSTALLED_HELPER.is_symlink():
        raise RuntimeError(
            "Install the .deb before performing APT removal. "
            "The source checkout is not a trusted privileged installation."
        )
    for path in (INSTALLED_HELPER, *INSTALLED_HELPER.parents):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError("APT helper installation is not root-owned and safe.")


def check_appimage(
    information: dict, delete: bool = False
) -> None:
    """Validate using a directory fd; never follow a final-component symlink."""
    path = Path(information["path"])
    root = Path(information["root"])
    if (
        not path.is_absolute()
        or not root.is_absolute()
        or root == Path("/")
        or not path.is_relative_to(root)
        or ".." in path.parts
    ):
        raise PermissionError("AppImage is outside its approved discovery root.")

    parent_fd = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        parent = os.fstat(parent_fd)
        if (
            [parent.st_dev, parent.st_ino] != information["parent_identity"]
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o022
        ):
            raise PermissionError("AppImage parent directory changed or is unsafe.")

        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or fingerprint(info) != information["fingerprint"]
            ):
                raise PermissionError("AppImage changed, is linked, or is not user-owned.")
            if not appimage_signature(os.read(descriptor, 11)):
                raise PermissionError("The file is not a recognized AppImage.")
            current = os.stat(
                path.name, dir_fd=parent_fd, follow_symlinks=False
            )
            if fingerprint(current) != fingerprint(info):
                raise PermissionError("AppImage changed during validation.")
            if delete:
                os.unlink(path.name, dir_fd=parent_fd)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


class OperationService:
    def __init__(self, runner: Runner | None = None):
        self.runner = runner or Runner()

    def plan(self, application: Application) -> Plan:
        if not application.can_uninstall:
            raise PermissionError(
                "Automatic removal is unavailable or this package is protected."
            )
        kind = application.installation_type

        if kind == "APT":
            name = validate_package(application.package_name)
            check_helper_installation()
            output = self.runner.run([
                "/usr/bin/python3", "-I", str(INSTALLED_HELPER),
                "--plan", name,
            ])
            data = json.loads(output)
            argv = [
                self.runner.executable("pkexec"),
                str(INSTALLED_HELPER),
                "--apply", name, data["digest"],
            ]
            explanation = (
                f"Remove exactly: {name}\n"
                f"Installed version: {data['version']}\n\n"
                "No other packages will be removed. Configuration files and "
                "unused dependencies will be kept.\n"
                "Administrator authentication is required.\n\n"
                "The helper revalidates this plan under the APT lock."
            )

        elif kind == "Flatpak":
            flags = flatpak_flags(application.metadata["scope"])
            ref = validate_flatpak_ref(
                application.metadata["ref"], application.package_name
            )
            argv = [
                self.runner.executable("flatpak"),
                *flags,
                "uninstall", "--noninteractive", "--assumeyes",
                "--no-related", ref,
            ]
            explanation = (
                f"Remove application reference:\n{ref}\n"
                f"Installation: {application.metadata['scope']}\n\n"
                "Application data is retained. Shared runtimes and related "
                "extensions are not automatically removed.\n"
                "System installations may request PolicyKit authorization."
            )

        elif kind == "Snap":
            name = application.package_name
            if (
                not SNAP.fullmatch(name)
                or len(name) > 80
                or not any(char.isalpha() for char in name.split("_")[0])
            ):
                raise ValueError("Invalid Snap instance name.")
            argv = [
                self.runner.executable("pkexec"),
                self.runner.executable("snap"), "remove", name,
            ]
            explanation = (
                f"Remove Snap instance: {name}\n\n"
                "Snapd controls service shutdown, data handling and automatic "
                "snapshots. This does not use --purge.\n"
                "Administrator authentication is required."
            )

        elif kind == "AppImage":
            information = {
                "path": application.install_location,
                "root": application.metadata["root"],
                "fingerprint": application.metadata["fingerprint"],
                "parent_identity": application.metadata["parent_identity"],
            }
            check_appimage(information)
            return Plan(
                application.id,
                f"Delete {application.name}?",
                "Permanently delete this user-owned AppImage:\n"
                f"{application.install_location}\n\n"
                "This does not delete personal data, configuration, or desktop "
                "launchers. It cannot be undone by this application.",
                local_delete=information,
            )

        else:
            raise PermissionError(
                "No safe automatic removal method is known for this application."
            )

        return Plan(
            application.id,
            f"Uninstall {application.name}?",
            explanation + "\n\nCommand:\n" + shlex.join(argv),
            argv=argv,
        )

    def update_plan(self, application: Application) -> Plan:
        if not application.can_update:
            raise PermissionError("No update is currently available for this application.")
        kind = application.installation_type

        if kind == "APT":
            name = validate_package(application.package_name)
            argv = [
                self.runner.executable("pkexec"),
                self.runner.executable("apt-get"),
                "install", "--only-upgrade", "--no-remove", "--assume-yes",
                name,
            ]
            explanation = (
                f"Update exactly: {name}\n"
                f"Installed version: {application.version}\n"
                f"Available version: {application.update_version}\n\n"
                "APT will not remove packages. Administrator authentication is required."
            )
        elif kind == "Flatpak":
            flags = flatpak_flags(application.metadata["scope"])
            ref = validate_flatpak_ref(
                application.metadata["ref"], application.package_name
            )
            argv = [
                self.runner.executable("flatpak"), *flags,
                "update", "--noninteractive", "--assumeyes", ref,
            ]
            explanation = (
                f"Update application reference:\n{ref}\n"
                f"Installation: {application.metadata['scope']}"
            )
        elif kind == "Snap":
            name = application.package_name
            if (
                not SNAP.fullmatch(name)
                or len(name) > 80
                or not any(char.isalpha() for char in name.split("_")[0])
            ):
                raise ValueError("Invalid Snap instance name.")
            argv = [
                self.runner.executable("pkexec"),
                self.runner.executable("snap"), "refresh", name,
            ]
            explanation = f"Update Snap instance: {name}\n\nAdministrator authentication is required."
        else:
            raise PermissionError("This application cannot be updated automatically.")

        return Plan(
            application.id,
            f"Update {application.name}?",
            explanation + "\n\nCommand:\n" + shlex.join(argv),
            argv=argv,
        )

    def reinstall_sources(self, application: Application) -> list[str]:
        if application.installation_type == "APT":
            return ["APT repositories"]
        if application.installation_type == "Flatpak":
            origin = application.metadata.get("origin", "")
            return [f"Flatpak remote: {origin}"] if REMOTE.fullmatch(origin) else []
        if application.installation_type == "Snap":
            return ["Snap Store"]
        return []

    def reinstall_plan(self, application: Application, source: str) -> Plan:
        if source not in self.reinstall_sources(application):
            raise PermissionError("The selected reinstall source is unavailable.")
        kind = application.installation_type

        if kind == "APT":
            name = validate_package(application.package_name)
            argv = [
                self.runner.executable("pkexec"),
                self.runner.executable("apt-get"),
                "install", "--reinstall", "--no-remove", "--assume-yes", name,
            ]
            explanation = (
                f"Reinstall exactly: {name}\n"
                "Source: configured APT repositories\n\n"
                "APT will not remove packages. Administrator authentication is required."
            )
        elif kind == "Flatpak":
            flags = flatpak_flags(application.metadata["scope"])
            app_id = application.package_name
            ref = validate_flatpak_ref(application.metadata["ref"], app_id)
            origin = application.metadata["origin"]
            if not REMOTE.fullmatch(origin):
                raise ValueError("Invalid Flatpak remote name.")
            argv = [
                self.runner.executable("flatpak"), *flags,
                "install", "--reinstall", "--noninteractive", "--assumeyes",
                origin, ref,
            ]
            explanation = (
                f"Reinstall application reference:\n{ref}\n"
                f"Source: Flatpak remote {origin}\n"
                f"Installation: {application.metadata['scope']}"
            )
        elif kind == "Snap":
            name = application.package_name
            if (
                not SNAP.fullmatch(name)
                or len(name) > 80
                or not any(char.isalpha() for char in name.split("_")[0])
            ):
                raise ValueError("Invalid Snap instance name.")
            argv = [
                self.runner.executable("pkexec"),
                self.runner.executable("snap"), "refresh", "--amend", name,
            ]
            explanation = f"Reinstall Snap instance: {name}\nSource: Snap Store\n\nAdministrator authentication is required."
        else:
            raise PermissionError("This application cannot be reinstalled automatically.")

        return Plan(
            application.id,
            f"Reinstall {application.name}?",
            explanation + "\n\nCommand:\n" + shlex.join(argv),
            argv=argv,
        )

    def execute(self, plan: Plan, output) -> int:
        if plan.local_delete is not None:
            check_appimage(plan.local_delete, delete=True)
            output("AppImage file deleted. Launchers and personal data were retained.")
            return 0
        if not plan.argv:
            raise ValueError("Empty operation.")
        return self.runner.stream(plan.argv, output)
