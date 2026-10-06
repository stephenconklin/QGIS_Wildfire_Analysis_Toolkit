"""
Online test for the HLS algorithms (needs a network and an Earthdata login in
~/.netrc). The offline smoke test is dev/smoke_test.py.

Inside QGIS (Python Console):
    exec(open('/path/to/QGIS_Burn_Severity/dev/online_test.py').read())

Headless (macOS):
    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app online_test.py

Uses the test-data burned area (Dragon Bravo, clipped) as the AOI:
  1. Find HLS scenes for the Lab 6 dates: S30 on 2024-09-13 (pre-fire) and
     L30 on 2025-09-15 (post-fire), and check the expected tiles come back.
  2. Fetch HLS NBR for each, and check the two grids are identical, the CRS is
     UTM 12N, and the NBR is in range and mostly valid.
  3. A missing netrc file stops the fetch with a clear message.
Prints PASS or FAIL per check and the dNBR summary inside the burned area.
"""
import os
import sys
import tempfile

try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:  # exec() from the console has no __file__
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Burn_Severity"
sys.path.insert(0, REPO)

from qgis.core import (Qgis, QgsApplication, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, QgsProcessingUtils, QgsProject)
from qgis.PyQt.QtCore import QDate  # noqa: E402

_app = None
if QgsApplication.instance() is None:  # headless: start QGIS and Processing
    _app = QgsApplication([], False)
    _app.initQgis()
    from processing.core.Processing import Processing
    Processing.initialize()

from fireanalysis import VERSION  # noqa: E402
from fireanalysis.algorithms.fetch_hls_nbr import FetchHlsNbr  # noqa: E402
from fireanalysis.algorithms.find_hls_scenes import FindHlsScenes  # noqa: E402

AOI = os.path.join(REPO, "testdata", "testdata.gpkg") + "|layername=burn_area"
OUT = tempfile.mkdtemp(prefix="fireanalysis_online_")
DATES = [("pre", "S30", QDate(2024, 9, 13)), ("post", "L30", QDate(2025, 9, 15))]


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.log = []

    def reportError(self, error, fatalError=False):
        self.log.append("ERROR " + error)

    def pushInfo(self, info):
        self.log.append(info)

    def pushWarning(self, warning):
        self.log.append("WARNING " + warning)


def _run(alg, params):
    alg.initAlgorithm()
    p = {d.name(): d.defaultValue() for d in alg.parameterDefinitions()}
    p.update(params)
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = _Feedback()
    try:
        res, ok = alg.create().run(p, context, feedback)
    except Exception as e:  # QGIS 3 raises, QGIS 4 reports through feedback
        return None, context, feedback.log + ["ERROR " + str(e)]
    return (res if ok else None), context, feedback.log


failed = 0


def check(name, ok, detail=""):
    global failed
    failed += not ok
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else "\n      " + detail))


print(f"Fire Analysis {VERSION} online test on QGIS {Qgis.QGIS_VERSION}; outputs in {OUT}")
nbr = {}
found = {}
for label, sensor, day in DATES:
    res, context, log = _run(FindHlsScenes(), {
        "AOI": AOI, "START": day, "END": day, "SENSORS": [["L30", "S30"].index(sensor)],
        "OUTPUT": "TEMPORARY_OUTPUT"})
    if res is None:
        check(f"find {sensor} {day.toString('yyyy-MM-dd')}", False, " | ".join(log))
        continue
    scenes = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], context)
    found[label] = scenes
    tiles = sorted(f["tile"] for f in scenes.getFeatures())
    check(f"find {sensor} {day.toString('yyyy-MM-dd')}: tiles {tiles}",
          scenes.featureCount() > 0 and "12SUF" in tiles)

    path = os.path.join(OUT, f"nbr_{label}.tif")
    res2, _, log = _run(FetchHlsNbr(), {
        "AOI": AOI, "SCENES": scenes, "BUFFER": 0, "NBR": path,
        "REFLECTANCE": os.path.join(OUT, f"refl_{label}.tif")})
    check(f"fetch {sensor} NBR", res2 is not None, " | ".join(log))
    for line in log:
        print("      " + line)
    if res2 is not None:
        nbr[label] = res2["NBR"]

# Missing netrc file -> clear error
if "post" in found:
    res, _, log = _run(FetchHlsNbr(), {
        "AOI": AOI, "SCENES": found["post"], "NETRC": os.path.join(OUT, "no_such_netrc"),
        "NBR": os.path.join(OUT, "never.tif")})
    check("missing netrc is refused", res is None and any("No netrc file" in m for m in log),
          " | ".join(log))
res, _, log = _run(FetchHlsNbr(), {
    "AOI": AOI, "SCENES": AOI, "NBR": os.path.join(OUT, "never.tif")})
check("scenes layer without granule field is refused",
      res is None and any('no "granule" field' in m for m in log), " | ".join(log))

if len(nbr) == 2:
    import numpy as np
    from osgeo import gdal
    pre, post = gdal.Open(nbr["pre"]), gdal.Open(nbr["post"])
    check("pre and post grids are identical",
          pre.GetGeoTransform() == post.GetGeoTransform()
          and (pre.RasterXSize, pre.RasterYSize) == (post.RasterXSize, post.RasterYSize),
          f"{pre.GetGeoTransform()} vs {post.GetGeoTransform()}")
    check("CRS is WGS 84 / UTM 12N", pre.GetSpatialRef().GetAuthorityCode(None) == "32612")
    a, b = pre.ReadAsArray(), post.ReadAsArray()
    ok = (a != -9999) & (b != -9999)
    check("NBR within -1..1", bool((np.abs(a[a != -9999]) <= 1).all()
                                   and (np.abs(b[b != -9999]) <= 1).all()))
    check(f"over half the grid valid in both ({100 * ok.mean():.1f} %)", ok.mean() > 0.5)

    # dNBR inside the burned area, classified with the USGS thresholds
    burn = gdal.Rasterize("", AOI.split("|")[0], format="MEM", layers=["burn_area"], burnValues=[1],
                          outputBounds=(pre.GetGeoTransform()[0],
                                        pre.GetGeoTransform()[3] + pre.GetGeoTransform()[5] * pre.RasterYSize,
                                        pre.GetGeoTransform()[0] + pre.GetGeoTransform()[1] * pre.RasterXSize,
                                        pre.GetGeoTransform()[3]),
                          xRes=30, yRes=30, outputSRS="EPSG:32612", outputType=gdal.GDT_Byte)
    inside = ok & (burn.ReadAsArray() == 1)
    d = (a - b)[inside]
    edges = [-np.inf, 0.10, 0.27, 0.44, 0.66, np.inf]
    counts = np.histogram(d, bins=edges)[0]
    print(f"\nHLS dNBR inside the burned area: {inside.sum():,} px, mean {d.mean():.3f}, "
          f"range {d.min():.3f} to {d.max():.3f}")
    print("  USGS classes (Unburned..High): " + " / ".join(f"{c:,} ({100 * c / d.size:.2f} %)" for c in counts))
    print("  Lab 6 test data, same classes: 7,248 / 140,263 / 107,045 / 31,598 / 33")

print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
if _app is not None:
    _app.exitQgis()
    sys.exit(1 if failed else 0)
elif failed:
    raise RuntimeError(f"{failed} online test check(s) failed")
