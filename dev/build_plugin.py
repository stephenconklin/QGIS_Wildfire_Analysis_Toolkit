"""
Build an installable QGIS plugin zip from plugin/ and burn_severity_summary.py.

    python3 dev/build_plugin.py        ->  dist/qgis_burn_severity-<version>.zip

Install it in QGIS with Plugins > Manage and Install Plugins > Install from ZIP.
Plain Python; QGIS is not needed to build.
"""
import pathlib
import re
import zipfile

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = "qgis_burn_severity"  # plugin folder name inside the zip

script = (REPO / "burn_severity_summary.py").read_text(encoding="utf-8")
version = re.search(r'^VERSION = "([^"]+)"', script, re.M).group(1)

metadata = (REPO / "plugin" / "metadata.txt").read_text(encoding="utf-8")
metadata = re.sub(r"^version=.*$", f"version={version}", metadata, flags=re.M)
changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
latest = [ln for ln in changelog.split("\n## ")[1].strip().splitlines() if ln.strip()]
metadata += "changelog=" + "\n    ".join(latest) + "\n"

dist = REPO / "dist"
dist.mkdir(exist_ok=True)
out = dist / f"{PKG}-{version}.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in ("__init__.py", "plugin.py", "provider.py", "icon.svg"):
        z.write(REPO / "plugin" / f, f"{PKG}/{f}")
    z.writestr(f"{PKG}/metadata.txt", metadata)
    z.write(REPO / "burn_severity_summary.py", f"{PKG}/burn_severity_summary.py")
    z.write(REPO / "LICENSE", f"{PKG}/LICENSE")
    z.write(REPO / "README.md", f"{PKG}/README.md")
print(out)
