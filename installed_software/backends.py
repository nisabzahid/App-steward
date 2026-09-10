import configparser
import logging
import os
import re
import stat
from abc import ABC, abstractmethod
from pathlib import Path

from .model import Application, DiscoveryResult
from .process import Runner

LOG = logging.getLogger(__name__)

DPKG_FORMAT = (
    "${binary:Package}\t${Version}\t${Architecture}\t"
    "${Installed-Size}\t${Maintainer}\t${db:Status-Abbrev}\t"
    "${Essential}\t${Priority}\t${Section}\t${Description}\x1e"
)


class PackageBackend(ABC):
    key = ""

    def __init__(self, runner: Runner):
        self.runner = runner

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def discover(self) -> DiscoveryResult:
        raise NotImplementedError

    def get_details(self, application: Application) -> dict:
        return dict(application.metadata)


def parse_size(text: str) -> int | None:
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B|bytes?)\s*",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    amount, suffix = match.groups()
    suffix = suffix.upper()
    powers = {"K": 1, "M": 2, "G": 3, "T": 4}
    power = powers.get(suffix[0], 0)
    base = 1024 if "I" in suffix else 1000
    return int(float(amount) * base**power)


def parse_dpkg(text: str) -> list[Application]:
    applications = []
    for record in text.split("\x1e"):
        if not record.strip():
            continue
        fields = record.split("\t", 9)
        if len(fields) != 10:
            LOG.warning("Ignoring malformed DPKG record")
            continue
        (
            package,
            version,
            architecture,
            size,
            maintainer,
            status,
            essential,
            priority,
            section,
            description,
        ) = fields

        if len(status) < 2 or status[1] != "i":
            continue

        try:
            installed_size = int(size) * 1024
        except ValueError:
            installed_size = None

        applications.append(
            Application(
                id=f"apt:{package}",
                name=package,
                installation_type="APT",
                package_name=package,
                version=version,
                description=description.replace("\n .\n", "\n\n"),
                architecture=architecture,
                size=installed_size,
                publisher=maintainer,
                source="DPKG database; repository not determined",
                system_package=True,
                can_uninstall=(
                    essential != "yes"
                    and priority not in {"required", "important"}
                    and status.strip() == "ii"
                ),
                metadata={
                    "status": status,
                    "essential": essential == "yes",
                    "priority": priority,
                    "section": section,
                    "automatic": None,
                    "origins": [],
                    "management": "APT / DPKG",
                },
            )
        )
    return applications


class AptBackend(PackageBackend):
    key = "APT"

    def is_available(self) -> bool:
        return self.runner.available("dpkg-query")

    def discover(self) -> DiscoveryResult:
        if not self.is_available():
            return DiscoveryResult(notices=["APT/DPKG: dpkg-query is unavailable."])

        data = self.runner.run(
            [
                self.runner.executable("dpkg-query"),
                "--show",
                f"--showformat={DPKG_FORMAT}",
            ]
        )
        applications = parse_dpkg(data)
        notices = []

        try:
            import apt

            cache = apt.Cache()
            for application in applications:
                try:
                    package = cache[application.package_name]
                    version = package.installed
                    if version is None:
                        continue
                    candidate = package.candidate
                    if candidate is not None and candidate.version != version.version:
                        application.can_update = True
                        application.update_version = candidate.version
                    application.metadata["automatic"] = package.is_auto_installed
                    origins = sorted(
                        {
                            " / ".join(
                                filter(
                                    None,
                                    (
                                        origin.origin,
                                        origin.label,
                                        origin.archive,
                                        origin.component,
                                        origin.site,
                                    ),
                                )
                            )
                            for origin in version.origins
                            if origin.site or origin.origin or origin.label
                        }
                    )
                    application.metadata["origins"] = origins
                    application.source = (
                        "; ".join(origins)
                        if origins
                        else "DPKG; no matching repository in current APT indexes"
                    )
                except (KeyError, AttributeError):
                    continue
        except Exception as error:
            notices.append(
                f"APT enrichment unavailable: {error}. "
                "The DPKG inventory is still available."
            )

        return DiscoveryResult(applications, notices)


def flatpak_installations() -> list[tuple[str, list[str], str]]:
    """Include default user/system and configured named system installations."""
    home = Path.home()
    user_data = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
    installations = [
        ("user", ["--user"], str(user_data / "flatpak")),
        ("system", ["--system"], "/var/lib/flatpak"),
    ]
    seen = {"default"}
    directory = Path("/etc/flatpak/installations.d")
    if directory.is_dir():
        for filename in sorted(directory.glob("*.conf")):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(filename, encoding="utf-8")
                for section in parser.sections():
                    match = re.fullmatch(r'Installation "([^"]+)"', section)
                    if not match:
                        continue
                    name = match.group(1)
                    if name in seen or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                        continue
                    seen.add(name)
                    location = parser.get(section, "Path", fallback="")
                    installations.append(
                        (
                            f"system:{name}",
                            [f"--installation={name}"],
                            location,
                        )
                    )
            except (OSError, configparser.Error):
                LOG.warning("Cannot parse Flatpak installation configuration")
    return installations


def parse_flatpak(text: str, scope: str, location: str) -> list[Application]:
    applications = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 9:
            if line.strip():
                LOG.warning("Ignoring malformed Flatpak record")
            continue
        app_id, name, version, arch, branch, runtime, origin, size, ref = fields
        if not ref.startswith("app/"):
            continue
        applications.append(
            Application(
                id=f"flatpak:{scope}:{ref}",
                name=name or app_id,
                installation_type="Flatpak",
                package_name=app_id,
                version=version,
                architecture=arch,
                size=parse_size(size),
                source=f"{origin} ({scope})",
                install_location=location,
                desktop_application=True,
                can_uninstall=True,
                can_update=True,
                metadata={
                    "scope": scope,
                    "ref": ref,
                    "runtime": runtime,
                    "origin": origin,
                    "branch": branch,
                },
            )
        )
    return applications


class FlatpakBackend(PackageBackend):
    key = "Flatpak"

    def is_available(self) -> bool:
        return self.runner.available("flatpak")

    def discover(self) -> DiscoveryResult:
        if not self.is_available():
            return DiscoveryResult(notices=["Flatpak is not installed."])
        result = DiscoveryResult()
        for scope, flags, location in flatpak_installations():
            try:
                data = self.runner.run(
                    [
                        self.runner.executable("flatpak"),
                        *flags,
                        "list",
                        "--app",
                        "--columns=application,name,version,arch,branch,"
                        "runtime,origin,size,ref",
                    ]
                )
                result.applications.extend(parse_flatpak(data, scope, location))
            except Exception as error:
                result.notices.append(f"Flatpak ({scope}): {error}")
        return result


def parse_snap(text: str) -> list[Application]:
    applications = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] == "Name" or len(fields) < 6:
            continue
        name, version, revision, channel, publisher = fields[:5]
        notes = " ".join(fields[5:])
        path = Path("/var/lib/snapd/snaps") / f"{name}_{revision}.snap"
        try:
            size = path.stat().st_size
        except OSError:
            size = None

        applications.append(
            Application(
                id=f"snap:{name}",
                name=name,
                installation_type="Snap",
                package_name=name,
                version=version,
                publisher=publisher,
                source=f"Snap Store ({channel})",
                size=size,
                install_location=str(path),
                desktop_application=False,
                can_uninstall=True,
                can_update=True,
                metadata={
                    "revision": revision,
                    "channel": channel,
                    "notes": notes,
                },
            )
        )
    return applications


class SnapBackend(PackageBackend):
    key = "Snap"

    def is_available(self) -> bool:
        return self.runner.available("snap")

    def discover(self) -> DiscoveryResult:
        if not self.is_available():
            return DiscoveryResult(notices=["Snap is not installed."])
        text = self.runner.run([self.runner.executable("snap"), "list"], timeout=45)
        return DiscoveryResult(parse_snap(text))


def configured_appimage_roots() -> list[Path]:
    home = Path.home()
    default = [
        home / "Applications",
        home / "AppImages",
        home / "Downloads",
    ]
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    filename = config_home / "installed-software/config.ini"
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(filename, encoding="utf-8")
        value = parser.get("AppImage", "directories", fallback="")
        if value.strip():
            return [
                Path(line.strip()).expanduser().absolute()
                for line in value.splitlines()
                if line.strip()
            ]
    except (OSError, configparser.Error):
        LOG.warning("Invalid AppImage configuration; using defaults")
    return default


def appimage_signature(header: bytes) -> bool:
    return (
        len(header) >= 11
        and header[:4] == b"\x7fELF"
        and header[8:11] in (b"AI\x01", b"AI\x02")
    )


def fingerprint(info: os.stat_result) -> list[int]:
    return [
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ]


class AppImageBackend(PackageBackend):
    key = "AppImage"

    def __init__(self, runner: Runner, roots: list[Path] | None = None):
        super().__init__(runner)
        self.roots = roots if roots is not None else configured_appimage_roots()

    def is_available(self) -> bool:
        return True

    def discover(self) -> DiscoveryResult:
        result = DiscoveryResult()
        seen = set()
        examined = 0

        for configured_root in self.roots:
            root = configured_root.resolve()
            if not root.is_dir():
                continue
            if root == Path("/"):
                result.notices.append("AppImage: refusing to scan filesystem root.")
                continue
            try:
                for current, directories, filenames in os.walk(root, followlinks=False):
                    current_path = Path(current)
                    depth = len(current_path.relative_to(root).parts)
                    directories[:] = [
                        name
                        for name in sorted(directories)
                        if not name.startswith(".")
                        and not (current_path / name).is_symlink()
                    ]
                    if depth >= 2:
                        directories[:] = []

                    for name in sorted(filenames):
                        examined += 1
                        if examined > 20000:
                            result.notices.append(
                                "AppImage scan stopped after 20,000 directory entries."
                            )
                            return result
                        if not name.casefold().endswith(".appimage"):
                            continue
                        path = current_path / name
                        try:
                            info = path.lstat()
                            if not stat.S_ISREG(info.st_mode):
                                continue
                            identity = (info.st_dev, info.st_ino)
                            if identity in seen:
                                continue
                            with path.open("rb") as stream:
                                if not appimage_signature(stream.read(11)):
                                    continue
                            seen.add(identity)
                            parent = path.parent.stat()
                            removable = (
                                info.st_uid == os.getuid()
                                and info.st_nlink == 1
                                and parent.st_uid == os.getuid()
                                and not parent.st_mode & 0o022
                            )
                            result.applications.append(
                                Application(
                                    id=f"appimage:{path}",
                                    name=path.stem,
                                    installation_type="AppImage",
                                    package_name=name,
                                    size=info.st_size,
                                    install_location=str(path),
                                    source="Portable AppImage file",
                                    desktop_application=True,
                                    can_uninstall=removable,
                                    metadata={
                                        "root": str(root),
                                        "fingerprint": fingerprint(info),
                                        "parent_identity": [
                                            parent.st_dev,
                                            parent.st_ino,
                                        ],
                                        "version_note": (
                                            "Version cannot be determined reliably "
                                            "without inspecting application contents."
                                        ),
                                    },
                                )
                            )
                        except OSError:
                            result.notices.append(f"AppImage: unable to inspect {path}")
            except OSError as error:
                result.notices.append(f"AppImage ({root}): {error}")
        return result
