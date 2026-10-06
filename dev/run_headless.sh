#!/bin/sh
# Run dev/smoke_test.py headless with the Python bundled in a QGIS macOS app.
#   dev/run_headless.sh                       # /Applications/QGIS.app
#   dev/run_headless.sh /Applications/QGIS-final-4_2_3.app
set -e
APP="${1:-/Applications/QGIS.app}/Contents"
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONHOME="$APP/Frameworks"
export PYTHONPATH="$APP/Resources/qgis/python:$APP/Resources/qgis/python/plugins"
export QGIS_PREFIX_PATH="$APP/MacOS"
export PROJ_DATA="$APP/Resources/qgis/proj"
export QT_QPA_PLATFORM=offscreen
cd "$HERE"   # keep the bundled Python away from the repo root
exec "$APP/MacOS/python3.12" "$HERE/smoke_test.py"
