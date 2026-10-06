"""
Smoke test for Burn Severity Summary. Run inside QGIS:

    Python Console > Show Editor > open this file > Run
    or:  exec(open('/path/to/QGIS_Burn_Severity/dev/smoke_test.py').read())

Loads burn_severity_summary.py straight from this repo (it does not need to be
installed in the Toolbox), runs it with temporary outputs, prints the summary
table and, when EXPECTED is set, checks the pixel counts against it. Nothing is
added to the project.
"""
import importlib.util
import os

from qgis.core import (QgsProcessingContext, QgsProcessingFeedback,
                       QgsProcessingUtils, QgsProject)

# ---- edit these -------------------------------------------------------------
LAB = ("/Users/stephenconklin/Library/CloudStorage/OneDrive-FortLewisCollege/"
       "GEOG-400-Raster-GIS/Lab-6/QGISProject/data/")
DNBR = LAB + "Output/dNBR.tif"
BURN_AREA = LAB + "Output/BurnArea_Clip.shp"
WATERSHED = LAB + "BrightAngelWatershed/BrightAngelWatershed.shp"

# Pixel counts per class (1-5) for zone A and zone B from the Lab 6 run.
# Set to None to skip the check when testing other data.
EXPECTED = {
    "Dragon Bravo": [11556, 290803, 188947, 55906, 44],
    "Bright Angel": [3997, 78398, 57213, 16001, 9],
}
# -----------------------------------------------------------------------------

try:
    here = os.path.dirname(os.path.abspath(__file__))
except NameError:  # exec() from the console has no __file__
    here = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Burn_Severity/dev"
spec = importlib.util.spec_from_file_location(
    "burn_severity_summary", os.path.join(here, "..", "burn_severity_summary.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class _Feedback(QgsProcessingFeedback):
    def pushInfo(self, info):
        print("  " + info)

    def reportError(self, error, fatalError=False):
        print("  ERROR: " + error)


alg = mod.BurnSeveritySummary()
alg.initAlgorithm()
params = {p.name(): p.defaultValue() for p in alg.parameterDefinitions()}
params.update({"DNBR": DNBR, "BURN_AREA": BURN_AREA, "WATERSHED": WATERSHED,
               "ZONES": "TEMPORARY_OUTPUT", "SEVERITY": "TEMPORARY_OUTPUT",
               "HISTOGRAM": "TEMPORARY_OUTPUT", "SUMMARY": "TEMPORARY_OUTPUT"})

context = QgsProcessingContext()
context.setProject(QgsProject.instance())
print(f"Burn severity summary v{mod.VERSION}")
res, ok = alg.create().run(params, context, _Feedback())
if not ok:
    raise RuntimeError("Algorithm failed; see messages above.")

summary = QgsProcessingUtils.mapLayerFromString(res["SUMMARY"], context)
names = summary.fields().names()
print("\n" + " | ".join(names))
for f in summary.getFeatures():
    print(" | ".join(f"{v:.2f}" if isinstance(v, float) else str(v) for v in f.attributes()))

if EXPECTED:
    hist = QgsProcessingUtils.mapLayerFromString(res["HISTOGRAM"], context)
    failures = []
    for f in hist.getFeatures():
        got = [int(f[f"C_{i}"] or 0) for i in range(1, 6)]
        want = EXPECTED.get(f["zone"])
        if want is not None and got != want:
            failures.append(f"{f['zone']}: got {got}, expected {want}")
    print("\nPASS" if not failures else "\nFAIL\n  " + "\n  ".join(failures))
