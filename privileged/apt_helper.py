#!/usr/bin/python3 -I
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

PACKAGE = re.compile(
    r"[a-z0-9][a-z0-9+.-]+(?::[a-z0-9][a-z0-9-]*)?\Z"
)
PROTECTED_NAMES = {
    "apt", "dpkg", "sudo", "pkexec", "polkitd", "systemd",
    "systemd-sysv", "dbus", "dbus-user-session",
    "ubuntu-desktop", "ubuntu-desktop-minimal",
    "pop-desktop", "pop-session", "pop-default-settings",
    "cosmic-session", "cosmic-desktop", "gnome-shell",
    "gdm3", "network-manager", "installed-software",
}
PROTECTED_PREFIXES = (
    "linux-image-", "linux-modules-", "linux-generic",
    "linux-system76", "grub-", "shim-", "system76-",
)


def validate_package(name: str) -> str:
    if not PACKAGE.fullmatch(name):
        raise ValueError("Invalid Debian package identifier.")
    return name


def protected(name: str, record: dict) -> bool:
    base = name.split(":")[0]
    return (
        record.get("Essential", "").casefold() == "yes"
        or record.get("Protected", "").casefold() == "yes"
        or record.get("Priority", "") in {"required", "important"}
        or base in PROTECTED_NAMES
        or base.startswith(PROTECTED_PREFIXES)
    )


def trusted_installation() -> None:
    path = Path(__file__).resolve()
    expected = Path("/usr/lib/installed-software/apt_helper.py")
    if path != expected:
        raise PermissionError("Privileged helper is not installed at its trusted path.")
    for item in (path, *path.parents):
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise PermissionError(
                f"Unsafe helper installation permissions: {item}"
            )
    if not stat.S_ISREG(path.stat().st_mode):
        raise PermissionError("Helper is not a regular file.")


def create_plan(cache, name: str) -> dict:
    validate_package(name)
    try:
        package = cache[name]
    except KeyError as error:
        raise ValueError("Package no longer exists in the APT cache.") from error
    installed = package.installed
    if installed is None:
        raise ValueError("Package is no longer installed.")

    record = installed.record
    if protected(name, record):
        raise PermissionError(
            "This package is protected. Use your distribution's "
            "administrative tools if you intentionally need to change it."
        )

    if cache.broken_count:
        raise RuntimeError(
            "APT already has broken dependencies. Repair the system first."
        )

    package.mark_delete(auto_fix=False, purge=False)

    if cache.broken_count:
        raise RuntimeError(
            "Removing this package would break dependencies. "
            "No changes were made. Review the operation manually with APT."
        )

    changes = cache.get_changes()
    if (
        len(changes) != 1
        or changes[0].name != package.name
        or not changes[0].marked_delete
        or changes[0].marked_install
        or changes[0].marked_upgrade
    ):
        raise RuntimeError("Refusing a transaction with additional package changes.")

    document = {
        "package": name,
        "version": installed.version,
        "architecture": installed.architecture,
        "remove": [name],
        "purge": False,
        "autoremove": False,
        "installed_size": installed.installed_size,
    }
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    document["digest"] = hashlib.sha256(canonical).hexdigest()
    return document


def main() -> int:
    if len(sys.argv) not in (3, 4):
        raise ValueError("Usage: apt_helper.py --plan PACKAGE | --apply PACKAGE DIGEST")
    mode, name = sys.argv[1:3]
    validate_package(name)

    import apt
    import apt_pkg
    import apt.progress.base
    import apt.progress.text

    if mode == "--plan" and len(sys.argv) == 3:
        cache = apt.Cache()
        print(json.dumps(create_plan(cache, name), sort_keys=True))
        return 0

    if mode != "--apply" or len(sys.argv) != 4:
        raise ValueError("Invalid helper arguments.")
    if os.geteuid() != 0:
        raise PermissionError("APT removal requires authorization through PolicyKit.")
    trusted_installation()
    digest = sys.argv[3]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid transaction digest.")

    with apt_pkg.SystemLock():
        cache = apt.Cache()
        plan = create_plan(cache, name)
        if plan["digest"] != digest:
            raise RuntimeError(
                "The package state changed after confirmation. "
                "Refresh and review a new removal plan."
            )
        print(
            f"Removing exactly {name} {plan['version']}; "
            "keeping configuration and unused dependencies.",
            flush=True,
        )
        success = cache.commit(
            apt.progress.text.AcquireProgress(),
            apt.progress.base.InstallProgress(),
        )
        if not success:
            raise RuntimeError("APT did not complete the transaction.")

    print("APT transaction completed.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"Removal refused or failed: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
