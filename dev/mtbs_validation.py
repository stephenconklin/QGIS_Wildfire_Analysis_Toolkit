"""
Validate the HLS pipeline against published MTBS fires (needs a network and the
Earthdata login in ~/.netrc). Slow: a few minutes per fire.

Headless (macOS):
    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app mtbs_validation.py

For each fire in FIRES:
  1. Read the MTBS burned-area boundary and its attributes (pre/post scene ids,
     dNBR offset, thresholds) from the USFS EDW MTBS map service.
  2. Run Find HLS scenes, Fetch HLS NBR (pre and post, same dates and sensor
     family as MTBS) and Build dNBR (ring offset around the MTBS perimeter).
  3. Download the MTBS dnbr6 mosaic for the fire year on the same grid.
  4. Compare: our offset with MTBS's, and our dNBR classified with MTBS's own
     thresholds against the published dnbr6, inside the perimeter.
The comparison isolates imagery and calibration (HLS vs MTBS's Landsat or
Sentinel-2 processing): thresholds and perimeter are MTBS's.

With --pc the imagery comes from Planetary Computer (Landsat Collection 2
Level-2 and Sentinel-2 L2A, the same satellite as MTBS's scene) instead of HLS;
use it for fires before 2013 (HISTORICAL below).

    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app mtbs_validation.py --pc NM3581210654120110626

Writes a JSON report next to the outputs and prints a summary.
"""
from http.client import HTTPException
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Burn_Severity"
sys.path.insert(0, REPO)

from qgis.core import (Qgis, QgsApplication, QgsGeometry, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, QgsProcessingUtils, QgsProject, QgsVectorLayer)
from qgis.PyQt.QtCore import QDate  # noqa: E402

_app = None
if QgsApplication.instance() is None:
    _app = QgsApplication([], False)
    _app.initQgis()
    from processing.core.Processing import Processing
    Processing.initialize()

import numpy as np  # noqa: E402
from osgeo import gdal  # noqa: E402

from fireanalysis import VERSION, raster  # noqa: E402
from fireanalysis.algorithms.build_dnbr import BuildDnbr  # noqa: E402
from fireanalysis.algorithms.classify_severity import ClassifySeverity  # noqa: E402
from fireanalysis.algorithms.fetch_hls_nbr import FetchHlsNbr  # noqa: E402
from fireanalysis.algorithms.find_hls_scenes import FindHlsScenes  # noqa: E402
from fireanalysis.algorithms.planetary_algorithms import FetchPlanetaryNbr, FindPlanetaryScenes  # noqa: E402

gdal.UseExceptions()
MAPSERVER = "https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MTBS_01/MapServer"
IMAGESERVER = "https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/USFS_EDW_MTBS_CONUS/ImageServer"
BOUNDARIES = MAPSERVER + "/63"  # Burned Area Boundaries (All Years)

# (MTBS fire id, year)
# Every MTBS fire over 15,000 ac in AZ, NM, CO and UT, 2022-2024, as published in 2026.
# The first three are the core cases; the rest test the threshold estimates.
FIRES = [
    ("NM3577510658420220422", 2022),  # Cerro Pelado: Landsat 8 -> Landsat 8, initial
    ("AZ3527011166620220612", 2022),  # Pipeline: Sentinel-2A -> Sentinel-2A, initial
    ("NM3320410805920220514", 2022),  # Black: Landsat 8 -> Landsat 9, initial, 328,000 ac
    ("AZ3137511119220220612", 2022),  # Tonto Canyon: S2B -> S2A
    ("AZ3184911157420220611", 2022),  # Contreras: L8 -> L8
    ("NM3385010753320220501", 2022),  # Bear Trap: L8 -> L9
    ("NM3571810539920220406", 2022),  # Hermits Peak: L8 -> L8, extended, 352,000 ac
    ("NM3624410503720220417", 2022),  # Cooks Peak: L8 -> L8
    ("AZ3530411158920220417", 2022),  # Tunnel: S2B -> S2B
    ("NM3580210384220220422", 2022),  # Mitchell: L9 -> L9, no high class
    ("NM3340610828920230519", 2023),  # Pass: S2A -> S2B, low class only
    ("NM3354010838820230713", 2023),  # Divide: L8 -> L9
    ("AZ3487111328820230701", 2023),  # Pilot: S2B -> S2A
    ("AZ3418111207820240803", 2024),  # Skeleton: S2B -> S2A, low class only
    ("AZ3279011092620240712", 2024),  # Freeman: S2A -> S2B
    ("AZ3375911159520240726", 2024),  # Sand Stone: S2B -> S2B, low class only
    ("AZ3343911144120240910", 2024),  # Siphon: S2A -> S2A, low class only
    ("UT4046911107620240928", 2024),  # Yellow Lake: S2B -> S2A, extended
]
# Before HLS: run with --pc
HISTORICAL = [
    ("NM3590710632420000505", 2000),  # Cerro Grande: L7 -> L5, extended
    ("AZ3662511217320060608", 2006),  # Warm: L5 -> L5, extended
    ("AZ3532911160120100620", 2010),  # Schultz: L5 -> L5, extended
    ("NM3581210654120110626", 2011),  # Las Conchas: L5 -> L7 (SLC-off), extended
    ("NM3326710859220120509", 2012),  # Whitewater-Baldy: L5 -> L8, extended
    ("AZ3423311046320020618", 2002),  # Rodeo: L7 -> L5, extended, 461,000 ac
]
PC = "--pc" in sys.argv
OUT = tempfile.mkdtemp(prefix="fireanalysis_mtbs_" + ("pc_" if PC else ""))
CLASSES = ["Unburned-low", "Low", "Moderate", "High", "Incr. greenness"]


def http(url, **params):
    """GET with retries: the MTBS services sometimes stall."""
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=120) as r:
                return r.read()
        except (OSError, HTTPException) as e:  # URLError, timeouts, IncompleteRead
            last = e
            print(f"   (retrying {url.rsplit('/', 2)[-2]}: {e})")
    raise RuntimeError(f"{url} failed after retries: {last}")


def pc_scene(mtbs_id):
    """MTBS scene id -> (Planetary Computer source, platform, QDate). '503403520110624' is
    Landsat 5 path 34 row 35; 'A12SVE20220601_30m' is Sentinel-2A."""
    s = mtbs_id.split("_")[0]
    d = s[-8:]
    day = QDate(int(d[:4]), int(d[4:6]), int(d[6:]))
    if s[0] in "AB":
        return 1, f"sentinel-2{s[0].lower()}", day
    return 0, f"landsat-{s[0]}", day


def scene(mtbs_id):
    """MTBS scene id -> (HLS sensor, QDate). '803303520210612' is Landsat 8 path 33
    row 35; 'A12SVE20220601_30m' is Sentinel-2A tile 12SVE."""
    s = mtbs_id.split("_")[0]
    if s[0] in "89" and s[:-8].isdigit():
        d = s[-8:]
        return "L30", QDate(int(d[:4]), int(d[4:6]), int(d[6:]))
    if s[0] in "AB":
        d = s[-8:]
        return "S30", QDate(int(d[:4]), int(d[4:6]), int(d[6:]))
    raise ValueError(f"Unknown MTBS scene id {mtbs_id}")


def run(alg, params):
    alg.initAlgorithm()
    p = {d.name(): d.defaultValue() for d in alg.parameterDefinitions()}
    p.update(params)
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = QgsProcessingFeedback()
    log = []
    feedback.pushInfo = log.append
    feedback.pushWarning = lambda m: log.append("WARNING " + m)
    feedback.reportError = lambda m, fatal=False: log.append("ERROR " + m)
    try:
        res, ok = alg.create().run(p, context, feedback)
    except Exception as e:
        raise RuntimeError(f"{alg.name()} failed: {e} | " + " | ".join(log)) from None
    if not ok:
        raise RuntimeError(f"{alg.name()} failed: " + " | ".join(log))
    return res, context, log


def kappa(confusion):
    n = confusion.sum()
    po = np.trace(confusion) / n
    pe = (confusion.sum(0) * confusion.sum(1)).sum() / n ** 2
    return (po - pe) / (1 - pe)


def validate(fire_id, year):
    folder = os.path.join(OUT, fire_id)
    os.makedirs(folder)
    gj = json.loads(http(f"{BOUNDARIES}/query", where=f"fire_id='{fire_id}'",
                         outFields="*", outSR=4326, f="geojson"))
    attrs = gj["features"][0]["properties"]
    perimeter = os.path.join(folder, "perimeter.geojson")
    with open(perimeter, "w") as f:
        json.dump(gj, f)
    print(f"\n== {attrs['fire_name']} ({fire_id}), {int(attrs['acres']):,} ac, {attrs['asmnt_type']}; "
          f"MTBS pre {attrs['pre_id']}, post {attrs['post_id']}")

    nbr = {}
    for label, mtbs_scene in (("pre", attrs["pre_id"]), ("post", attrs["post_id"])):
        if PC:
            source, platform, day = pc_scene(mtbs_scene)
            res, ctx, _ = run(FindPlanetaryScenes(), {"AOI": perimeter, "START": day, "END": day, "SOURCE": source,
                                                      "OUTPUT": "TEMPORARY_OUTPUT"})
            layer = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx)
            ids = [f["item"] for f in layer.getFeatures() if str(f["platform"]).lower() == platform]
            if not ids:
                raise RuntimeError(f"No Planetary Computer {platform} scene on {day.toString('yyyy-MM-dd')}")
            res, _, log = run(FetchPlanetaryNbr(), {"AOI": perimeter, "ITEMS": " ".join(ids), "SOURCE": source,
                                                    "NBR": os.path.join(folder, f"nbr_{label}.tif")})
            nbr[label] = res["NBR"]
            print(f"   {label}: Planetary Computer {platform} {day.toString('yyyy-MM-dd')} {', '.join(ids)}; "
                  + "; ".join(m.strip() for m in log if m.strip().startswith(("NBR computed", "Cloud", "Water"))))
            continue
        sensor, day = scene(mtbs_scene)
        res, ctx, _ = run(FindHlsScenes(), {"AOI": perimeter, "START": day, "END": day,
                                            "SENSORS": [["L30", "S30"].index(sensor)],
                                            "OUTPUT": "TEMPORARY_OUTPUT"})
        layer = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx)
        if layer.featureCount() == 0:
            raise RuntimeError(f"No HLS {sensor} granule on {day.toString('yyyy-MM-dd')}")
        tiles = sorted(f["tile"] for f in layer.getFeatures())
        res, _, log = run(FetchHlsNbr(), {"AOI": perimeter, "SCENES": layer,
                                          "NBR": os.path.join(folder, f"nbr_{label}.tif")})
        nbr[label] = res["NBR"]
        print(f"   {label}: HLS {sensor} {day.toString('yyyy-MM-dd')} tiles {', '.join(tiles)}; "
              + "; ".join(m.strip() for m in log if "Fmask" in m or "computed" in m))

    res, _, log = run(BuildDnbr(), {"PRE_NBR": nbr["pre"], "POST_NBR": nbr["post"], "PERIMETER": perimeter,
                                    "DNBR": os.path.join(folder, "dnbr.tif"), "RDNBR": None, "RBR": None})
    ours_offset, ours_std = res["OFFSET"], res["OFFSET_STD"]
    warnings = [m for m in log if m.startswith("WARNING")]
    dnbr_ds = gdal.Open(res["DNBR"])
    dnbr = dnbr_ds.ReadAsArray().astype(np.float64)
    dnbr[dnbr == -9999] = np.nan

    # MTBS dnbr6 on the same grid
    gt = dnbr_ds.GetGeoTransform()
    w, h = dnbr_ds.RasterXSize, dnbr_ds.RasterYSize
    bbox = [gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3]]
    epsg = dnbr_ds.GetSpatialRef().GetAuthorityCode(None)
    data = http(f"{IMAGESERVER}/exportImage", bbox=",".join(map(str, bbox)), bboxSR=epsg, imageSR=epsg,
                size=f"{w},{h}", format="tiff", pixelType="U8", interpolation="RSP_NearestNeighbor",
                mosaicRule=json.dumps({"mosaicMethod": "esriMosaicAttribute", "where": f"year={year}"}),
                f="image", noData=0)
    mtbs_path = os.path.join(folder, "mtbs_dnbr6.tif")
    with open(mtbs_path, "wb") as f:
        f.write(data)
    mtbs_ds = gdal.Open(mtbs_path)
    if mtbs_ds.GetGeoTransform() != gt or (mtbs_ds.RasterXSize, mtbs_ds.RasterYSize) != (w, h):
        raise RuntimeError(f"MTBS export grid differs: {mtbs_ds.GetGeoTransform()} vs {gt}")
    mtbs = mtbs_ds.ReadAsArray()

    # Classify our dNBR with MTBS's thresholds (x1000 -> raw)
    t = {k: attrs[k] / 1000.0 for k in ("low_threshold", "moderate_threshold", "high_threshold",
                                        "greenness_threshold")}
    ours = np.zeros(dnbr.shape, np.uint8)
    v = np.isfinite(dnbr)
    ours[v] = 1
    ours[v & (dnbr >= t["low_threshold"])] = 2
    ours[v & (dnbr >= t["moderate_threshold"])] = 3
    ours[v & (dnbr >= t["high_threshold"])] = 4
    ours[v & (dnbr <= t["greenness_threshold"])] = 5

    # Only this fire's perimeter: the annual mosaic also holds neighbouring fires of the year
    # (Pipeline 2022 borders Tunnel 2022, which had burned before Pipeline's pre-fire date)
    per = QgsVectorLayer(perimeter, "perimeter", "ogr")
    inside = raster.rasterize(QgsGeometry.unaryUnion([f.geometry() for f in per.getFeatures()]),
                              per.crs(), dnbr_ds, QgsProject.instance().transformContext())
    if not (inside & (mtbs >= 1) & (mtbs <= 6)).any():
        raise RuntimeError(f"the MTBS {year} mosaic has no classes inside this perimeter (not published "
                           "in the image service yet?)")
    both = inside & (mtbs >= 1) & (mtbs <= 5) & (ours >= 1)
    confusion = np.zeros((5, 5), np.int64)
    np.add.at(confusion, (mtbs[both] - 1, ours[both] - 1), 1)
    agree = float(np.trace(confusion) / confusion.sum())
    within1 = float(sum(confusion[i, j] for i in range(5) for j in range(5) if abs(i - j) <= 1 and i < 4 and j < 4)
                    / confusion[:4, :4].sum())
    share_mtbs = 100.0 * confusion.sum(1) / confusion.sum()
    share_ours = 100.0 * confusion.sum(0) / confusion.sum()
    masked_mtbs = int((inside & (mtbs == 6)).sum())
    no_hls = int((inside & (mtbs >= 1) & (mtbs <= 5) & (ours == 0)).sum())

    print(f"   offset: ours {ours_offset * 1000:.0f} ± {ours_std * 1000:.0f}, MTBS {attrs['dnbr_offst']} ± {attrs['dnbr_stddv']} (x1000)"
          + ("" if not warnings else "\n   " + "\n   ".join(warnings)))
    print(f"   thresholds (MTBS, x1000): low {attrs['low_threshold']}, moderate {attrs['moderate_threshold']}, "
          f"high {attrs['high_threshold']}")
    print(f"   {confusion.sum():,} px compared; MTBS non-mapping (6) {masked_mtbs:,}; no HLS NBR {no_hls:,}")
    print(f"   per-pixel agreement {100 * agree:.1f} %, within one class {100 * within1:.1f} %, kappa {kappa(confusion):.3f}")
    print("   class            MTBS %   ours %")
    for i, c in enumerate(CLASSES):
        print(f"   {c:15s}  {share_mtbs[i]:6.2f}   {share_ours[i]:6.2f}")
    # dNBR of each MTBS class, to see whether a difference is a shift or noise
    print("   our dNBR (x1000) by MTBS class: " + ", ".join(
        f"{CLASSES[i]} median {1000 * np.nanmedian(dnbr[both & (mtbs == i + 1)]):.0f}"
        for i in range(4) if (both & (mtbs == i + 1)).any()))
    # Our offline estimates (this fire's data) against MTBS's analyst choices. The nearby-fires
    # estimate is scored leave-one-out by dev/threshold_study.py (here it would see this fire).
    assessment = 1 if str(attrs["asmnt_type"]).startswith("Extended") else 0
    est, _, elog = run(ClassifySeverity(), {"DNBR": res["DNBR"], "PRE_NBR": nbr["pre"], "PERIMETER": perimeter,
                                            "ASSESSMENT": assessment, "ESTIMATE": 1,
                                            "OUTPUT": os.path.join(folder, "dnbr6_estimated.tif")})
    est_t = [est["LOW_T"], est["MODERATE_T"], est["HIGH_T"]]
    ec = gdal.Open(est["OUTPUT"]).ReadAsArray()
    both_e = inside & (mtbs >= 1) & (mtbs <= 5) & (ec >= 1) & (ec <= 5)
    conf_e = np.zeros((5, 5), np.int64)
    np.add.at(conf_e, (mtbs[both_e] - 1, ec[both_e] - 1), 1)
    share_est = 100.0 * conf_e.sum(0) / conf_e.sum()
    fmt = lambda v: "none" if v >= 9.999 else f"{v * 1000:.0f}"
    print(f"   estimated thresholds (x1000): low {fmt(est_t[0])}, moderate {fmt(est_t[1])}, high {fmt(est_t[2])}"
          f"  vs MTBS {attrs['low_threshold']}, {attrs['moderate_threshold']}, {attrs['high_threshold']}")
    for m in elog:
        if "threshold:" in m:
            print("      " + m)
    print(f"   with estimated thresholds: agreement {100 * np.trace(conf_e) / conf_e.sum():.1f} %, "
          f"kappa {kappa(conf_e):.3f}; class % " + " / ".join(f"{x:.1f}" for x in share_est[:4]))
    return {"fire_id": fire_id, "thresholds_estimated": [v * 1000 for v in est_t],
            "agreement_estimated": float(np.trace(conf_e) / conf_e.sum()), "kappa_estimated": float(kappa(conf_e)),
            "share_estimated": share_est.tolist(), "name": attrs["fire_name"], "acres": attrs["acres"],
            "pre_id": attrs["pre_id"], "post_id": attrs["post_id"],
            "offset_ours": ours_offset * 1000, "offset_std_ours": ours_std * 1000,
            "offset_mtbs": attrs["dnbr_offst"], "offset_std_mtbs": attrs["dnbr_stddv"],
            "thresholds_mtbs": [attrs["low_threshold"], attrs["moderate_threshold"], attrs["high_threshold"]],
            "pixels": int(confusion.sum()), "agreement": agree, "within_one": within1,
            "kappa": float(kappa(confusion)), "share_mtbs": share_mtbs.tolist(),
            "share_ours": share_ours.tolist(), "confusion": confusion.tolist(), "warnings": warnings}


print(f"Wildfire Analysis Toolkit {VERSION} MTBS validation on QGIS {Qgis.QGIS_VERSION}; outputs in {OUT}")
report = []
ALL = FIRES + HISTORICAL
ONLY = [a for a in sys.argv[1:] if a in {f for f, _ in ALL}]  # optional: fire ids to run
for fire_id, year in (HISTORICAL if PC and not ONLY else ALL):
    if ONLY and fire_id not in ONLY:
        continue
    try:
        report.append(validate(fire_id, year))
    except Exception as e:  # keep going with the other fires
        print(f"\n== {fire_id}: FAILED {e}")
        report.append({"fire_id": fire_id, "error": str(e)})
with open(os.path.join(OUT, "report.json"), "w") as f:
    json.dump(report, f, indent=1)
print(f"\nReport: {os.path.join(OUT, 'report.json')}")
if _app is not None:
    _app.exitQgis()
