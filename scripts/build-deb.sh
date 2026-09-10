#!/bin/sh
set -eu

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT HUP INT TERM

ROOT="$BUILD/root"
mkdir -p \
    "$ROOT/DEBIAN" \
    "$ROOT/usr/bin" \
    "$ROOT/usr/lib/installed-software" \
    "$ROOT/usr/share/applications" \
    "$ROOT/usr/share/icons/hicolor/scalable/apps" \
    "$ROOT/usr/share/doc/installed-software"

install -m 0644 "$PROJECT/packaging/control" "$ROOT/DEBIAN/control"

cp -R "$PROJECT/installed_software" \
    "$ROOT/usr/lib/installed-software/installed_software"
find "$ROOT/usr/lib/installed-software/installed_software" \
    -type d -name __pycache__ -exec rm -rf {} +
find "$ROOT/usr/lib/installed-software/installed_software" \
    -type f -name '*.pyc' -delete

install -m 0755 "$PROJECT/privileged/apt_helper.py" \
    "$ROOT/usr/lib/installed-software/apt_helper.py"

cat > "$ROOT/usr/bin/installed-software" <<'EOF'
#!/usr/bin/python3 -I
import sys
sys.path.insert(0, "/usr/lib/installed-software")
from installed_software.__main__ import main
raise SystemExit(main())
EOF
chmod 0755 "$ROOT/usr/bin/installed-software"

install -m 0644 \
    "$PROJECT/data/io.github.installedsoftware.Manager.desktop" \
    "$ROOT/usr/share/applications/io.github.installedsoftware.Manager.desktop"
install -m 0644 \
    "$PROJECT/data/icons/hicolor/scalable/apps/io.github.installedsoftware.Manager.svg" \
    "$ROOT/usr/share/icons/hicolor/scalable/apps/io.github.installedsoftware.Manager.svg"
install -m 0644 "$PROJECT/README.md" \
    "$ROOT/usr/share/doc/installed-software/README.md"

find "$ROOT" -type d -exec chmod 0755 {} +
find "$ROOT/usr/lib/installed-software/installed_software" \
    -type f -exec chmod 0644 {} +

mkdir -p "$PROJECT/dist"
dpkg-deb --root-owner-group --build "$ROOT" \
    "$PROJECT/dist/installed-software_1.0.0_all.deb"

printf '\nBuilt %s\n' "$PROJECT/dist/installed-software_1.0.0_all.deb"
