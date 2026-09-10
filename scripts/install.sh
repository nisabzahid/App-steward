#!/bin/sh
set -eu

PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ "$(id -u)" -eq 0 ]; then
    printf '%s\n' "Run this script as your normal user; it invokes sudo for installation."
    exit 1
fi

sh "$PROJECT/scripts/build-deb.sh"
sudo apt-get install "$PROJECT/dist/installed-software_1.0.0_all.deb"

printf '\nLaunch App Steward from the application menu,\n'
printf 'or run: installed-software\n'
