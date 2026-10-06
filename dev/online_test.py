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
  4. Build dNBR with the ring offset, then Burn severity summary on the result:
     the whole pipeline from search to summary table.
  5. Burn severity from HLS (the one-step pipeline) with the same granules:
     every output file, a 4-class summary and a report with the warnings.
  6. Planetary Computer: the same Landsat 9 acquisition (2025-09-15) through
     Find and Fetch Landsat / Sentinel-2 must match the HLS post-fire NBR.
  7. If the Lab 6 raw data zip is present: Prepare local scene NBR on the same
     two acquisitions must match the HLS NBR on the same grid (r > 0.99).
Prints PASS or FAIL per check, the offset and the summary table.
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
from fireanalysis.algorithms.build_dnbr import BuildDnbr  # noqa: E402
from fireanalysis.algorithms.fetch_hls_nbr import FetchHlsNbr  # noqa: E402
from fireanalysis.algorithms.find_hls_scenes import FindHlsScenes  # noqa: E402
from fireanalysis.algorithms.hls_burn_severity import HlsBurnSeverity  # noqa: E402
from fireanalysis.algorithms.planetary_algorithms import FetchPlanetaryNbr, FindPlanetaryScenes  # noqa: E402
from fireanalysis.algorithms.prepare_local_nbr import PrepareLocalNbr  # noqa: E402

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
granule_ids = {}
for label, sensor, day in DATES:
    res, context, log = _run(FindHlsScenes(), {
        "AOI": AOI, "START": day, "END": day, "SENSORS": [["L30", "S30"].index(sensor)],
        "OUTPUT": "TEMPORARY_OUTPUT"})
    if res is None:
        check(f"find {sensor} {day.toString('yyyy-MM-dd')}", False, " | ".join(log))
        continue
    scenes = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], context)
    found[label] = scenes
    granule_ids[label] = " ".join(f["granule"] for f in scenes.getFeatures())  # the layer dies with its context
    tiles = sorted(f["tile"] for f in scenes.getFeatures())
    check(f"find {sensor} {day.toString('yyyy-MM-dd')}: tiles {tiles}",
          scenes.featureCount() > 0 and "12SUF" in tiles)

    path = os.path.join(OUT, f"nbr_{label}.tif")
    res2, _, log = _run(FetchHlsNbr(), {
        "AOI": AOI, "SCENES": scenes, "NBR": path,
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

    # Build dNBR (ring offset) and Burn severity summary: the whole chain
    res, _, log = _run(BuildDnbr(), {"PRE_NBR": nbr["pre"], "POST_NBR": nbr["post"], "PERIMETER": AOI,
                                  "DNBR": os.path.join(OUT, "dnbr.tif"), "RDNBR": os.path.join(OUT, "rdnbr.tif"),
                                  "RBR": None})
    check("build dNBR with ring offset", res is not None, " | ".join(log))
    if res is not None:
        print("      " + next(m for m in log if m.startswith("dNBR offset")))
        check(f"offset is small ({res['OFFSET']:.4f}) for a well-matched pair", abs(res["OFFSET"]) < 0.1)
        # The test-data burned area is clipped to the window, so its ring runs into burned land
        check("clipped perimeter: burned-sample warning", any("looks burned" in m for m in log), " | ".join(log))
        LAB_PERIMETER = ("/Users/stephenconklin/Library/CloudStorage/OneDrive-FortLewisCollege/"
                         "GEOG-400-Raster-GIS/Lab-6/QGISProject/data/Output/BurnArea_Clip.shp")
        if os.path.exists(LAB_PERIMETER):
            full, _, flog = _run(BuildDnbr(), {"PRE_NBR": nbr["pre"], "POST_NBR": nbr["post"],
                                               "PERIMETER": LAB_PERIMETER, "DNBR": os.path.join(OUT, "dnbr_full.tif"),
                                               "RDNBR": None, "RBR": None})
            ok = full is not None and abs(full["OFFSET"] - 0.026) < 0.005 and not any("looks burned" in m for m in flog)
            check("full Lab 6 perimeter: offset ≈ 0.026, no burned-sample warning", ok, " | ".join(flog))
            if full is not None:
                print("      " + next(m for m in flog if m.startswith("dNBR offset")))
        import importlib.util
        spec = importlib.util.spec_from_file_location("burn_severity_summary",
                                                      os.path.join(REPO, "burn_severity_summary.py"))
        bss = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bss)
        # Classify with thresholds from nearby MTBS fires (online) and from this fire's data
        from fireanalysis.algorithms.classify_severity import ClassifySeverity
        for method, label in ((0, "nearby MTBS fires"), (1, "this fire's data")):
            cres, _, clog = _run(ClassifySeverity(), {"DNBR": res["DNBR"], "PRE_NBR": nbr["pre"], "PERIMETER": AOI,
                                                      "ESTIMATE": method,
                                                      "OUTPUT": os.path.join(OUT, f"dnbr6_{method}.tif")})
            ok = cres is not None and 0 < cres["LOW_T"] < cres["MODERATE_T"] <= cres["HIGH_T"]
            check(f"classify with thresholds from {label}", ok and (method == 1 or any("nearby MTBS" in m for m in clog)),
                  " | ".join(clog))
            for m in clog:
                if "threshold:" in m or "MTBS wildfires" in m:
                    print("      " + m)
        res2, context2, log2 = _run(bss.BurnSeveritySummary(), {
            "DNBR": res["DNBR"], "BURN_AREA": AOI,
            "WATERSHED": os.path.join(REPO, "testdata", "testdata.gpkg") + "|layername=watershed",
            **{k: "TEMPORARY_OUTPUT" for k in ("ZONES", "SEVERITY", "HISTOGRAM", "SUMMARY")}})
        check("burn severity summary on the HLS dNBR", res2 is not None, " | ".join(log2))
        if res2 is not None:
            table = QgsProcessingUtils.mapLayerFromString(res2["SUMMARY"], context2)
            print("      class            % Dragon Bravo   % Bright Angel   ac Dragon Bravo   ac Bright Angel")
            for f in table.getFeatures():
                print(f"      {f['sev_class']:15s}  {f['pct_db']:14.2f}   {f['pct_ba']:14.2f}   "
                      f"{f['ac_db']:15,.0f}   {f['ac_ba']:15,.0f}")

# The one-step pipeline with the granules found above (no pair search, to stay fast)
if len(granule_ids) == 2:
    import json
    folder = os.path.join(OUT, "pipeline")
    res, _, log = _run(HlsBurnSeverity(), {
        "PERIMETER": AOI, "FIRE_NAME": "Dragon Bravo", "FIRE_START": QDate(2025, 7, 4), "FIRE_END": QDate(2025, 9, 1),
        "SUBAREAS": os.path.join(REPO, "testdata", "testdata.gpkg") + "|layername=watershed", "SUBAREA_NAME": "Bright Angel",
        "PRE_GRANULES": granule_ids["pre"], "POST_GRANULES": granule_ids["post"],
        "OUTPUT_FOLDER": folder})
    check("pipeline: runs", res is not None, " | ".join(log[-5:]))
    if res is not None:
        want = {"nbr_pre.tif", "nbr_post.tif", "reflectance_post.tif", "dnbr.tif", "rdnbr.tif", "dnbr6.tif",
                "dnbr6.qml", "dnbr6.png", "summary.csv", "summary.gpkg", "report.html", "report.json"}
        missing = want - set(os.listdir(folder))
        check("pipeline: every output file", not missing, f"missing {sorted(missing)}")
        with open(os.path.join(folder, "report.json"), encoding="utf-8") as f:
            rep = json.load(f)
        check("pipeline: 4-class summary for both zones", len(rep["summary"]) == 4 and "pct_ba" in rep["summary"][0],
              str(rep["summary"])[:300])
        check("pipeline: report lists the clipped-perimeter warning",
              any("looks burned" in w for w in rep["warnings"]), str(rep["warnings"]))
        print("      thresholds: " + ", ".join(f"{k} {rep['thresholds'][k]:.3f}" for k in ("low", "moderate", "high"))
              + f"; cross-check {rep['thresholds'].get('alternative')}")

# Planetary Computer: Landsat 9 on the post-fire date, against HLS L30 of the same acquisition
if "post" in nbr:
    res, ctx, log = _run(FindPlanetaryScenes(), {"AOI": AOI, "START": QDate(2025, 9, 15), "END": QDate(2025, 9, 15),
                                                 "SOURCE": 0, "AOI_CLEAR": True, "OUTPUT": "TEMPORARY_OUTPUT"})
    ids = []
    if res is not None:
        layer = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx)
        ids = [f["item"] for f in layer.getFeatures() if f["platform"] == "landsat-9"]
        clear = [f["aoi_clear"] for f in layer.getFeatures() if f["platform"] == "landsat-9"]
    check(f"Planetary Computer: Landsat 9 scenes found ({', '.join(ids)}), clear share {clear if ids else ''}",
          bool(ids), " | ".join(log))
    if ids:
        res, _, log = _run(FetchPlanetaryNbr(), {"AOI": AOI, "ITEMS": " ".join(ids), "SOURCE": 0,
                                                 "NBR": os.path.join(OUT, "pc_post.tif")})
        if res is None:
            check("Planetary Computer: fetch", False, " | ".join(log))
        else:
            x, y = gdal.Open(res["NBR"]), gdal.Open(nbr["post"])
            same = x.GetGeoTransform() == y.GetGeoTransform() and x.RasterXSize == y.RasterXSize
            a, b = x.ReadAsArray().astype(float), y.ReadAsArray().astype(float)
            ok = (a != -9999) & (b != -9999)
            r = np.corrcoef(a[ok], b[ok])[0, 1]
            check(f"Planetary Computer NBR matches HLS on the same grid (r {r:.3f}, mean diff "
                  f"{np.mean(a[ok] - b[ok]):+.3f})", same and r > 0.98, " | ".join(log))

# Local scenes from the Lab 6 zip against HLS (same acquisitions, same grid)
LAB_ZIP = ("/Users/stephenconklin/Library/CloudStorage/OneDrive-FortLewisCollege/GEOG-400-Raster-GIS/"
           "Lab-6/Lab6-data-2026.zip")
if len(nbr) == 2 and os.path.exists(LAB_ZIP):
    import zipfile
    cache = os.path.join(tempfile.gettempdir(), "fireanalysis_lab6raw")
    with zipfile.ZipFile(LAB_ZIP) as z:
        for name in z.namelist():
            if (name.startswith("DragonBravo/Sentinel_20240913/") and ("_B8A_" in name or "_B12_" in name)) or \
                    (name.startswith("DragonBravo/Landsat9_20250915/") and
                     (name.endswith(("_SR_B5.TIF", "_SR_B7.TIF")) or name.endswith(".json"))):
                if not os.path.exists(os.path.join(cache, name)):
                    z.extract(name, cache)
    for label, folder in (("pre", "Sentinel_20240913"), ("post", "Landsat9_20250915")):
        res, _, log = _run(PrepareLocalNbr(), {"SCENE": os.path.join(cache, "DragonBravo", folder), "AOI": AOI,
                                               "NBR": os.path.join(OUT, f"local_{label}.tif")})
        if res is None:
            check(f"local {label} NBR from the Lab 6 files", False, " | ".join(log))
            continue
        x, y = gdal.Open(res["NBR"]), gdal.Open(nbr[label])
        same = x.GetGeoTransform() == y.GetGeoTransform() and x.RasterXSize == y.RasterXSize
        a, b = x.ReadAsArray().astype(float), y.ReadAsArray().astype(float)
        ok = (a != -9999) & (b != -9999)
        r = np.corrcoef(a[ok], b[ok])[0, 1]
        check(f"local {label} NBR matches HLS on the same grid (r {r:.3f}, mean diff {np.mean(a[ok] - b[ok]):+.3f})",
              same and r > 0.99, " | ".join(log))

print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
if _app is not None:
    _app.exitQgis()
    sys.exit(1 if failed else 0)
elif failed:
    raise RuntimeError(f"{failed} online test check(s) failed")
