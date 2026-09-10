import configparser
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from .backends import flatpak_installations
from .model import Application
from .process import Runner

LOG = logging.getLogger(__name__)


@dataclass
class DesktopEntry:
    id: str
    path: str
    name: str
    comment: str
    icon: str
    executable: str
    categories: str
    hidden: bool
    no_display: bool
    terminal: bool
    flatpak: str
    snap: str

    @property
    def visible(self) -> bool:
        return not self.hidden and not self.no_display


def unescape(value: str) -> str:
    replacements = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
    result = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 1
            result.append(replacements.get(value[index], value[index]))
        else:
            result.append(value[index])
        index += 1
    return "".join(result)


def localized(section, key: str) -> str:
    language = (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG", "")
    )
    language = language.split(".")[0]
    candidates = [language]
    if "@" in language:
        candidates.append(language.split("@")[0])
    if "_" in language:
        candidates.append(language.split("_")[0])
    for candidate in candidates:
        if candidate and f"{key}[{candidate}]" in section:
            return unescape(section[f"{key}[{candidate}]"])
    return unescape(section.get(key, ""))


def parse_desktop(path: Path, desktop_id: str) -> DesktopEntry | None:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Desktop file exceeds the 1 MiB metadata limit")
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with path.open(encoding="utf-8", errors="replace") as stream:
        parser.read_file(stream)
    if "Desktop Entry" not in parser:
        return None
    section = parser["Desktop Entry"]
    if section.get("Type", "Application") != "Application":
        return None

    def boolean(key):
        return section.get(key, "").casefold() == "true"

    return DesktopEntry(
        id=desktop_id,
        path=str(path),
        name=localized(section, "Name") or path.stem,
        comment=localized(section, "Comment"),
        icon=unescape(section.get("Icon", "")),
        executable=section.get("Exec", ""),
        categories=section.get("Categories", ""),
        hidden=boolean("Hidden"),
        no_display=boolean("NoDisplay"),
        terminal=boolean("Terminal"),
        flatpak=section.get("X-Flatpak", ""),
        snap=section.get("X-SnapInstanceName", ""),
    )


def desktop_directories() -> list[Path]:
    home = Path.home()
    user_data = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
    directories = [user_data / "applications"]
    directories.extend(
        Path(item) / "applications"
        for item in os.environ.get(
            "XDG_DATA_DIRS", "/usr/local/share:/usr/share"
        ).split(":")
        if item and Path(item).is_absolute()
    )
    directories.extend(
        [
            Path("/usr/local/share/applications"),
            Path("/usr/share/applications"),
            Path("/var/lib/snapd/desktop/applications"),
        ]
    )
    for _, _, location in flatpak_installations():
        if location:
            directories.append(Path(location) / "exports/share/applications")

    unique = []
    seen = set()
    for directory in directories:
        value = str(directory)
        if value not in seen:
            seen.add(value)
            unique.append(directory)
    return unique


def scan_desktops(
    directories: list[Path] | None = None,
) -> tuple[list[DesktopEntry], list[str]]:
    entries = []
    notices = []
    seen_ids = set()
    for directory in directories if directories is not None else desktop_directories():
        if not directory.is_dir():
            continue
        try:
            for current, subdirectories, filenames in os.walk(
                directory, followlinks=False
            ):
                subdirectories.sort()
                for name in sorted(filenames):
                    if not name.endswith(".desktop"):
                        continue
                    path = Path(current) / name
                    desktop_id = str(path.relative_to(directory)).replace("/", "-")
                    if desktop_id in seen_ids:
                        continue
                    try:
                        entry = parse_desktop(path, desktop_id)
                        if entry is not None:
                            seen_ids.add(desktop_id)
                            entries.append(entry)
                    except (OSError, ValueError, configparser.Error):
                        notices.append(f"Malformed/unreadable desktop entry: {path}")
        except OSError as error:
            notices.append(f"Desktop directory {directory}: {error}")
    return entries, notices


def dpkg_owners(entries: list[DesktopEntry], runner: Runner) -> dict[str, list[str]]:
    if not runner.available("dpkg-query"):
        return {}
    paths = [entry.path for entry in entries]
    requested = set(paths)
    owners: dict[str, list[str]] = {}
    for start in range(0, len(paths), 100):
        batch = paths[start : start + 100]
        patterns = [
            "".join(
                {"*": "[*]", "?": "[?]", "[": "[[]"}.get(char, char) for char in path
            )
            for path in batch
        ]
        try:
            output = runner.run(
                [runner.executable("dpkg-query"), "--search", *patterns],
                accepted=(0, 1),
            )
            for line in output.splitlines():
                if ": " not in line:
                    continue
                package, filename = line.rsplit(": ", 1)
                if filename in requested and not package.startswith("diversion "):
                    owners[filename] = package.split(", ")
        except Exception as error:
            LOG.warning("Desktop ownership query failed: %s", error)
    return owners


def entry_executable(entry: DesktopEntry) -> str:
    try:
        parts = shlex.split(entry.executable)
    except ValueError:
        return ""
    return parts[0] if parts else ""


def merge_applications(
    applications: list[Application],
    entries: list[DesktopEntry],
    owners: dict[str, list[str]],
) -> list[Application]:
    result = list(applications)
    apt = {
        application.package_name: application
        for application in applications
        if application.installation_type == "APT"
    }
    flatpaks = [
        application
        for application in applications
        if application.installation_type == "Flatpak"
    ]
    snaps = {
        application.package_name: application
        for application in applications
        if application.installation_type == "Snap"
    }
    images = {
        application.install_location: application
        for application in applications
        if application.installation_type == "AppImage"
    }

    for entry in entries:
        if entry.hidden:
            continue
        match = None

        for package in owners.get(entry.path, []):
            match = apt.get(package)
            if match is None:
                possibilities = [
                    value for key, value in apt.items() if key.split(":")[0] == package
                ]
                if len(possibilities) == 1:
                    match = possibilities[0]
            if match:
                break

        if match is None and entry.flatpak:
            possibilities = [
                application
                for application in flatpaks
                if application.package_name == entry.flatpak
            ]
            scoped = [
                application
                for application in possibilities
                if application.install_location
                and Path(entry.path).is_relative_to(Path(application.install_location))
            ]
            if len(scoped) == 1:
                match = scoped[0]
            elif len(possibilities) == 1:
                match = possibilities[0]

        if match is None and entry.snap:
            match = snaps.get(entry.snap)

        if match is None:
            executable = entry_executable(entry)
            if executable.startswith("/"):
                match = images.get(str(Path(executable).absolute()))

        if match:
            first_visible = not match.metadata.get("visible_desktop_attached")
            match.desktop_files.append(entry.path)
            match.metadata.setdefault("desktop_entries", []).append(vars(entry).copy())
            if entry.visible:
                match.desktop_application = True
                match.system_package = False
                if first_visible:
                    match.name = entry.name
                    match.icon = entry.icon or match.icon
                    match.description = entry.comment or match.description
                    match.category = entry.categories
                    match.desktop_files.remove(entry.path)
                    match.desktop_files.insert(0, entry.path)
                    match.metadata["visible_desktop_attached"] = True
            continue

        result.append(
            Application(
                id=f"desktop:{entry.id}",
                name=entry.name,
                installation_type="DesktopEntry",
                description=entry.comment,
                icon=entry.icon or "application-x-executable",
                install_location=entry.path,
                desktop_files=[entry.path],
                desktop_application=entry.visible,
                system_package=not entry.visible,
                category=entry.categories,
                source="Desktop entry; package ownership not established",
                can_uninstall=False,
                metadata={
                    "desktop_entries": [vars(entry).copy()],
                    "removal_note": (
                        "Removing a launcher does not uninstall its application. "
                        "Use the original installer or administrator instructions."
                    ),
                },
            )
        )

    return list({application.id: application for application in result}.values())
