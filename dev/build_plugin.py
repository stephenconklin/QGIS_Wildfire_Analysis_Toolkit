"""
Build an installable QGIS plugin zip from plugin/, fireanalysis/ and
burn_severity_summary.py.

    python3 dev/build_plugin.py        ->  dist/wildfire_analysis_toolkit-<version>.zip

Install it in QGIS with Plugins > Manage and Install Plugins > Install from ZIP.
Plain Python; QGIS is not needed to build. The plugin version is VERSION in
fireanalysis/__init__.py. The metadata is checked as QGIS and plugins.qgis.org
read it (ConfigParser, required fields); its changelog is kept by hand.
"""
import configparser
import pathlib
import re
import sys
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = "wildfire_analysis_toolkit"  # plugin folder name inside the zip

init = (REPO / "fireanalysis" / "__init__.py").read_text(encoding="utf-8")
version = re.search(r'^VERSION = "([^"]+)"', init, re.M).group(1)

metadata = (REPO / "plugin" / "metadata.txt").read_text(encoding="utf-8")
metadata = re.sub(r"^version=.*$", f"version={version}", metadata, flags=re.M)

# Check the metadata as QGIS and plugins.qgis.org read it: ConfigParser with
# interpolation (a stray % breaks it) and the fields the repository requires.
parser = configparser.ConfigParser()
parser.read_string(metadata)
general = parser["general"]
missing = [k for k in ("name", "qgisMinimumVersion", "description", "about", "version", "author",
                       "email", "repository", "changelog") if not general.get(k, "").strip()]
if missing:
    sys.exit(f"metadata.txt: missing {', '.join(missing)}")
if not general["changelog"].lstrip().startswith(version.split("-")[0]):
    print(f"warning: metadata.txt changelog does not start with {version.split('-')[0]}", file=sys.stderr)

dist = REPO / "dist"
dist.mkdir(exist_ok=True)
out = dist / f"{PKG}-{version}.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in ("__init__.py", "plugin.py", "provider.py", "icon.svg"):
        z.write(REPO / "plugin" / f, f"{PKG}/{f}")
    z.writestr(f"{PKG}/metadata.txt", metadata)
    z.write(REPO / "burn_severity_summary.py", f"{PKG}/burn_severity_summary.py")
    for f in sorted((REPO / "fireanalysis").rglob("*.py")):
        z.write(f, f"{PKG}/{f.relative_to(REPO)}")
    z.write(REPO / "LICENSE", f"{PKG}/LICENSE")
    z.write(REPO / "README.md", f"{PKG}/README.md")
print(out)
