# App Steward

App Steward is a native GTK4 desktop utility for inspecting and managing
installed applications on Ubuntu, Pop!_OS, and compatible Debian-based systems.
It brings APT, Flatpak, Snap, desktop launchers, and selected AppImages into one
clear inventory so users can understand what is installed and where it came from.

App Steward focuses on deliberate, reviewable operations rather than blind
cleanup. It shows the exact package-manager command before updating,
reinstalling, or removing supported software, protects important system
packages, and requires administrator authorization for privileged changes.

The project is designed for local desktop use, transparent package operations,
and maintainable open-source development. It does not claim to discover every
script, container, language environment, or manually copied binary on a Linux
system.

## What it does

- Inventories installed Debian packages using dpkg-query.
- Enriches Debian metadata with python3-apt repository information.
- Lists user, default-system, and configured named-system Flatpak applications.
- Lists active Snap installations when Snap is installed.
- Scans XDG desktop application directories.
- Discovers recognizable AppImages in bounded configurable locations.
- Associates desktop launchers with package records where ownership is known.
- Provides search, source filters, sorting, details and launch actions.
- Updates APT, Flatpak, and Snap applications from their native package
    managers after showing the exact update command.
- Reinstalls supported applications from their recorded APT repository,
    Flatpak remote, or Snap Store source after the user chooses a source.
- Removes supported applications after an explicit confirmation and plan review.
- Reports partial discovery failures without discarding other backends.

App Steward does not claim that Linux has a universal database of every application.
This application aggregates supported sources; it does not claim to identify
every program, script, container, language environment, or manually copied binary.

## Dependencies

Required on Ubuntu/Pop!_OS 24.04:

- Python 3.12
- python3-gi
- python3-apt
- gir1.2-gtk-4.0
- apt and dpkg
- pkexec
- A graphical session with a functioning PolicyKit authentication agent

Optional:

- flatpak
- snap/snapd

The program does not install Flatpak or Snap merely to inspect the system.

Install development dependencies:

```bash
sudo apt-get update
sudo apt-get install \
    python3 python3-gi python3-apt gir1.2-gtk-4.0 \
    pkexec dpkg-dev desktop-file-utils
```

Use the distribution's /usr/bin/python3, not a Python installation that cannot
import the distribution's GI and APT bindings.

## Run from source

```bash
cd installed-software
/usr/bin/python3 -m installed_software
```

Do not run the GUI with sudo.

Discovery, details, launching, Flatpak/Snap planning and user-owned AppImage
handling work from source.

The Update action uses APT's `--only-upgrade --no-remove`, Flatpak's exact
application reference, or Snap's exact instance name. AppImages and desktop
entries without a package manager are not automatically updateable.

The Reinstall action offers only a source verified for the selected installed
record. It does not guess a package ID across stores, and AppImages or
unowned desktop entries do not expose automatic reinstall.

APT removal requires installing the .deb so that its privileged helper has a
fixed root-owned installation path.

## Build and install

Review the source first, especially privileged/apt_helper.py.

```bash
sh scripts/build-deb.sh
sudo apt-get install ./dist/installed-software_1.0.0_all.deb
```

Alternatively:

```bash
sh scripts/install.sh
```

Launch from the desktop menu or:

```bash
installed-software
```

Remove this utility using the system package manager:

```bash
sudo apt-get remove installed-software
```

The utility refuses to remove itself during its own transaction.

## Application versus package views

The default view prioritizes applications with visible desktop launchers,
Flatpak applications, and recognized AppImages.

The command-line/other view includes non-desktop Snap packages and manually
installed Debian packages when python3-apt exposes that information.

"Manually installed" is an APT dependency-management flag, not proof that a
package is an end-user command-line application.

Enable Show system packages to inspect the full supported inventory,
including libraries and packages without desktop launchers.

## Supported sources

### Debian / APT / DPKG

`dpkg-query` provides package name, version, architecture, installed size,
maintainer, status, essential flag, priority, section and description.

`python3-apt` adds automatic-installation state and matching repository origins
from current local APT indexes.

### Flatpak

Queries machine-oriented explicit tab-separated columns.

### Snap

Uses `snap list`.

### AppImage

Default locations:

- ~/Applications
- ~/AppImages
- ~/Downloads

## Search and sorting

Search matches name, package ID, description, publisher, source and type.
It supports substring matching plus a lightweight name subsequence fallback.

Sorts:

- Name ascending / descending
- Approximate size descending
- Installation type
- Version text

## Security model

The GUI refuses to run as root.

Discovery is unprivileged and does not modify package state.

All commands use argument arrays with shell=False.
Package identifiers, Flatpak refs/scopes and Snap instance names are validated.

APT removal runs through a root-owned helper and refuses protected packages.

## Tests

```bash
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q installed_software privileged tests
```

## Code quality and CI

Development tools are pinned in `requirements-dev.txt`:

- Black formats Python code.
- isort keeps imports ordered using Black-compatible rules.
- Flake8 checks Python style and common errors.
- pytest is available for future test migration, while the current suite uses
    the standard-library unittest runner.

Run the local quality checks with:

```bash
python3 -m pip install -r requirements-dev.txt
isort --check-only .
black --check .
flake8 .
/usr/bin/python3 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q installed_software privileged tests
sh scripts/build-deb.sh
```

GitHub Actions runs the same checks for pushes to `main` and pull requests.
The workflow uses GitHub's free hosted Ubuntu runner and does not require a
paid CI service or project secrets.

Suggested next improvements:

- Add integration tests around discovery using recorded command fixtures.
- Add a release workflow that publishes the `.deb` as a GitHub release asset.
- Add dependency and workflow update automation with Dependabot.
- Add UI smoke tests for confirmation dialogs and source selection.
- Add package metadata checks for desktop files and icon installation.
