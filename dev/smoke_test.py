"""
Smoke test for Burn Severity Summary.

Inside QGIS (Python Console):
    exec(open('/path/to/QGIS_Wildfire_Analysis_Toolkit/dev/smoke_test.py').read())

Headless, against any installed QGIS (macOS):
    dev/run_headless.sh /Applications/QGIS.app

Loads burn_severity_summary.py straight from this repo (it does not need to be
installed in the Toolbox) and runs it on testdata/ in every mode, checking the
pixel counts per class against known values. With FULL_LAB = True it also runs
the full Lab 6 data, if present. Nothing is added to the project. Prints PASS or
FAIL for each case and raises at the end if any case failed.
"""
import importlib.util
import os
import sys

# ---- edit these -------------------------------------------------------------
FULL_LAB = True   # also check the full Lab 6 rasters when they exist
LAB = ("/Users/stephenconklin/Library/CloudStorage/OneDrive-FortLewisCollege/"
       "GEOG-400-Raster-GIS/Lab-6/QGISProject/data/")
# -----------------------------------------------------------------------------

try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:  # exec() from the console has no __file__
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Wildfire_Analysis_Toolkit"
T = os.path.join(REPO, "testdata") + os.sep

from qgis.core import (Qgis, QgsApplication, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, QgsProcessingUtils, QgsProject,
                       QgsRasterLayer)

_app = None
if QgsApplication.instance() is None:  # headless: start QGIS and Processing
    _app = QgsApplication([], False)
    _app.initQgis()
    from processing.core.Processing import Processing
    Processing.initialize()

spec = importlib.util.spec_from_file_location(
    "burn_severity_summary", os.path.join(REPO, "burn_severity_summary.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

TEST = {"DNBR": T + "dnbr.tif",
        "BURN_AREA": T + "testdata.gpkg|layername=burn_area",
        "WATERSHED": T + "testdata.gpkg|layername=watershed"}
PRE = T + "prefire_nbr.tif"
CLIP_A = [7248, 140263, 107045, 31598, 33]  # zone A inside the test-data window

# (name, parameters, {zone: expected counts per class}, expected summary fields or None)
CASES = [
    ("dNBR, two zones", {}, {
        "Dragon Bravo": CLIP_A,
        "Bright Angel": [3997, 78398, 57213, 16001, 9]},
     ["ord", "sev_class", "pct_db", "pct_ba", "ha_db", "ha_ba", "ac_db", "ac_ba"]),
    ("dNBR, zone A only", {"WATERSHED": None}, {"Dragon Bravo": CLIP_A},
     ["ord", "sev_class", "pct_db", "ha_db", "ac_db"]),
    ("dNBR, one zone per sub-area name",
     {"WATERSHED": T + "testdata.gpkg|layername=subareas", "SUBAREA_FIELD": "name"}, {
         "Dragon Bravo": CLIP_A,
         "Bright Angel West": [2170, 38535, 21820, 6353, 0],
         "Bright Angel East": [1827, 39863, 35393, 9648, 9]},
     ["ord", "sev_class", "pct_db", "pct_bright", "pct_brigh2",
      "ha_db", "ha_bright", "ha_brigh2", "ac_db", "ac_bright", "ac_brigh2"]),
    ("RdNBR", {"INDEX": 1, "PRE_NBR": PRE}, {
        "Dragon Bravo": [180, 24485, 135672, 125850],
        "Bright Angel": [78, 15722, 78536, 61282]}, None),
    ("RBR", {"INDEX": 2, "PRE_NBR": PRE}, {
        "Dragon Bravo": [422, 43816, 172604, 69345],
        "Bright Angel": [190, 26410, 93262, 35756]}, None),
]
# Runs that must stop with an error containing this text
ERRORS = [
    ("RdNBR without pre-fire NBR", {"INDEX": 1}, "needs the pre-fire NBR"),
    ("class names don't match table", {"CLASS_NAMES": "a,b"}, "5 classes"),
    ("bad field code", {"ZONE_A_CODE": "d b"}, "may only contain"),
]
if FULL_LAB and os.path.exists(LAB + "Output/dNBR.tif"):
    FULL = {"DNBR": LAB + "Output/dNBR.tif",
            "BURN_AREA": LAB + "Output/BurnArea_Clip.shp",
            "WATERSHED": LAB + "BrightAngelWatershed/BrightAngelWatershed.shp"}
    CASES.append(("Full Lab 6 data, dNBR", FULL, {
        "Dragon Bravo": [11556, 290803, 188947, 55906, 44],
        "Bright Angel": [3997, 78398, 57213, 16001, 9]}, None))
    if os.path.exists(LAB + "Output/dNBR_Burn_Area_Recscale.tif"):
        ERRORS.append(("0-1 rescaled dNBR is refused",
                       dict(FULL, DNBR=LAB + "Output/dNBR_Burn_Area_Recscale.tif"),
                       "rescaled to 0-1"))


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.errors = []

    def reportError(self, error, fatalError=False):
        self.errors.append(error)


def _run(params):
    alg = mod.BurnSeveritySummary()
    alg.initAlgorithm()
    p = {d.name(): d.defaultValue() for d in alg.parameterDefinitions()}
    p.update(TEST)
    p.update(params)
    p.update({k: "TEMPORARY_OUTPUT" for k in ("ZONES", "SEVERITY", "HISTOGRAM", "SUMMARY")})
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = _Feedback()
    try:
        res, ok = alg.create().run(p, context, feedback)
    except Exception as e:  # QGIS 3 raises, QGIS 4 reports through feedback
        return None, context, [str(e)]
    return (res if ok else None), context, feedback.errors


print(f"Burn severity summary v{mod.VERSION} on QGIS {Qgis.QGIS_VERSION}")
failed = 0
for name, params, expected, fields in CASES:
    res, context, errors = _run(params)
    problems = []
    if res is None:
        problems.append("run failed: " + " | ".join(errors))
    else:
        hist = QgsProcessingUtils.mapLayerFromString(res["HISTOGRAM"], context)
        got = {}
        for f in hist.getFeatures():
            if f["zone"] in expected:
                n = len(expected[f["zone"]])
                got[f["zone"]] = [int(f[f"C_{i}"] or 0) if f"C_{i}" in f.fields().names() else 0
                                  for i in range(1, n + 1)]
        for zone, want in expected.items():
            if got.get(zone) != want:
                problems.append(f"{zone}: got {got.get(zone)}, expected {want}")
        severity = QgsRasterLayer(res["SEVERITY"])
        if not severity.crs().isValid():
            problems.append("severity raster has no CRS")
        summary = QgsProcessingUtils.mapLayerFromString(res["SUMMARY"], context)
        if fields and summary.fields().names() != fields:
            problems.append(f"summary fields {summary.fields().names()}")
        if summary.featureCount() != len(next(iter(expected.values()))):
            problems.append(f"summary has {summary.featureCount()} rows")
    failed += bool(problems)
    print(("PASS  " if not problems else "FAIL  ") + name)
    for p in problems:
        print("      " + p)

for name, params, text in ERRORS:
    res, context, errors = _run(params)
    ok = res is None and any(text in e for e in errors)
    failed += not ok
    print(("PASS  " if ok else "FAIL  ") + name)
    if not ok:
        print("      errors: " + (" | ".join(errors) or "none; the run succeeded"))

print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
if _app is not None:
    _app.exitQgis()
    sys.exit(1 if failed else 0)
elif failed:
    raise RuntimeError(f"{failed} smoke test case(s) failed")
