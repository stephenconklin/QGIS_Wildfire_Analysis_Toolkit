#!/bin/sh
# Run a dev script (default smoke_test.py) headless with the Python bundled in a QGIS macOS app.
#   dev/run_headless.sh                                          # /Applications/QGIS.app
#   dev/run_headless.sh /Applications/QGIS-final-4_2_3.app
#   dev/run_headless.sh /Applications/QGIS-final-4_2_3.app online_test.py
#   dev/run_headless.sh /Applications/QGIS-final-4_2_3.app mtbs_validation.py <fire id> ...   # extra arguments go to the script
set -e
APP="${1:-/Applications/QGIS.app}/Contents"
SCRIPT="${2:-smoke_test.py}"
[ $# -ge 2 ] && shift 2 || shift $#
HERE="$(cd "$(dirname "$0")" && pwd)"
export PYTHONHOME="$APP/Frameworks"
export PYTHONPATH="$APP/Resources/qgis/python:$APP/Resources/qgis/python/plugins"
export QGIS_PREFIX_PATH="$APP/MacOS"
export PROJ_DATA="$APP/Resources/qgis/proj"
export QT_QPA_PLATFORM=offscreen
cd "$HERE"   # keep the bundled Python away from the repo root
exec "$APP/MacOS/python3.12" "$HERE/$SCRIPT" "$@"
