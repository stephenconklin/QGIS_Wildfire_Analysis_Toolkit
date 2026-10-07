"""
Offline test for the fireanalysis algorithms that need no network: Prepare
local scene NBR (on synthetic Landsat and Sentinel-2 products), Build dNBR,
Classify severity, Draft burned-area perimeter and Severity map (PDF). The online HLS test is dev/online_test.py.

Inside QGIS (Python Console):
    exec(open('/path/to/QGIS_Wildfire_Analysis_Toolkit/dev/pipeline_test.py').read())

Headless (macOS):
    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app pipeline_test.py

Builds a synthetic post-fire NBR on the testdata/dnbr.tif grid (pre-fire NBR
resampled to that grid, minus the dNBR). Build dNBR must then give back
dnbr.tif, an offset that matches an independent OGR/GDAL calculation, and
RdNBR/RBR that match their formulas. Classify severity must put each pixel in
the class its thresholds say, estimate thresholds by its stated rules, and
refuse bad input.
"""
import os
import sys
import tempfile

try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:  # exec() from the console has no __file__
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Wildfire_Analysis_Toolkit"
sys.path.insert(0, REPO)

from qgis.core import (Qgis, QgsApplication, QgsGeometry, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, QgsProject, QgsVectorLayer)

_app = None
if QgsApplication.instance() is None:  # headless: start QGIS and Processing
    _app = QgsApplication([], False)
    _app.initQgis()
    from processing.core.Processing import Processing
    Processing.initialize()

import numpy as np  # noqa: E402
from osgeo import gdal, ogr  # noqa: E402

from fireanalysis import VERSION  # noqa: E402
from fireanalysis.algorithms.build_dnbr import BuildDnbr  # noqa: E402
from fireanalysis.algorithms.classify_severity import ClassifySeverity, match_threshold  # noqa: E402
from fireanalysis.algorithms.prepare_local_nbr import PrepareLocalNbr  # noqa: E402
from fireanalysis.algorithms.draft_perimeter import DraftPerimeter, burned_mask  # noqa: E402
from fireanalysis.algorithms.severity_map import SeverityMap  # noqa: E402

gdal.UseExceptions()
T = os.path.join(REPO, "testdata") + os.sep
GPKG = T + "testdata.gpkg"
BURN = GPKG + "|layername=burn_area"
OUT = tempfile.mkdtemp(prefix="fireanalysis_pipeline_")


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
        return None, feedback.log + ["ERROR " + str(e)]
    return (res if ok else None), feedback.log


failed = 0


def check(name, ok, detail=""):
    global failed
    failed += not ok
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else "\n      " + str(detail)))


def read(path):
    ds = gdal.Open(path)
    a = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    nd = ds.GetRasterBand(1).GetNoDataValue()
    a[(a == nd) | ~np.isfinite(a)] = np.nan
    return ds, a


print(f"Wildfire Analysis Toolkit {VERSION} pipeline test on QGIS {Qgis.QGIS_VERSION}; outputs in {OUT}")

# Synthetic post-fire NBR on the dNBR grid
dnbr_ds, dnbr = read(T + "dnbr.tif")
gt = dnbr_ds.GetGeoTransform()
bounds = (gt[0], gt[3] + gt[5] * dnbr_ds.RasterYSize, gt[0] + gt[1] * dnbr_ds.RasterXSize, gt[3])
pre_on_grid = gdal.Warp("", T + "prefire_nbr.tif", format="MEM", outputBounds=bounds,
                        width=dnbr_ds.RasterXSize, height=dnbr_ds.RasterYSize,
                        dstSRS=dnbr_ds.GetProjection(), resampleAlg="bilinear",
                        dstNodata=float("nan"), outputType=gdal.GDT_Float32).ReadAsArray().astype(np.float64)
_pre_ds = gdal.Open(T + "prefire_nbr.tif")  # keep a reference: the band dies with it
pre_nd = _pre_ds.GetRasterBand(1).GetNoDataValue()
pre_on_grid[(pre_on_grid == pre_nd) | ~np.isfinite(pre_on_grid)] = np.nan
post = (pre_on_grid - dnbr).astype(np.float32)
POST = os.path.join(OUT, "post_nbr.tif")
drv = gdal.GetDriverByName("GTiff").Create(POST, dnbr_ds.RasterXSize, dnbr_ds.RasterYSize, 1, gdal.GDT_Float32)
drv.SetGeoTransform(gt)
drv.SetProjection(dnbr_ds.GetProjection())
drv.GetRasterBand(1).SetNoDataValue(-9999)
drv.GetRasterBand(1).WriteArray(np.where(np.isfinite(post), post, -9999))
drv = None

# Independent ring offset: OGR buffers, GDAL rasterize
src = ogr.Open(GPKG)
union = None
for f in src.GetLayerByName("burn_area"):
    g = f.GetGeometryRef().MakeValid()
    union = g.Clone() if union is None else union.Union(g)


def burn(geom):
    mem = gdal.GetDriverByName("MEM").Create("", dnbr_ds.RasterXSize, dnbr_ds.RasterYSize, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(gt)
    mem.SetProjection(dnbr_ds.GetProjection())
    lyr_ds = ogr.GetDriverByName("MEM" if int(gdal.VersionInfo()) >= 3110000 else "Memory").CreateDataSource("")
    lyr = lyr_ds.CreateLayer("g", dnbr_ds.GetSpatialRef())
    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetGeometry(geom)
    lyr.CreateFeature(feat)
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
    return mem.ReadAsArray().astype(bool)


ring = burn(union.Buffer(2090, 16).Difference(union.Buffer(90, 16))) & ~burn(union)
sample = dnbr[ring & np.isfinite(dnbr) & np.isfinite(pre_on_grid)]
want_offset = float(np.median(sample))
print(f"      independent ring offset: {want_offset:.4f} from {sample.size:,} px")

# 1. No offset: dNBR must reproduce dnbr.tif on its grid
res, log = _run(BuildDnbr(), {"PRE_NBR": T + "prefire_nbr.tif", "POST_NBR": POST, "OFFSET_METHOD": 0,
                              "DNBR": os.path.join(OUT, "dnbr0.tif"), "RDNBR": os.path.join(OUT, "rdnbr0.tif"),
                              "RBR": os.path.join(OUT, "rbr0.tif")})
check("no offset: runs", res is not None, " | ".join(log))
if res:
    ds, got = read(res["DNBR"])
    both = np.isfinite(got) & np.isfinite(dnbr)
    check("no offset: same grid as the post-fire NBR",
          ds.GetGeoTransform() == gt and (ds.RasterXSize, ds.RasterYSize) == (dnbr_ds.RasterXSize, dnbr_ds.RasterYSize))
    err = float(np.nanmax(np.abs(got - dnbr)[both]))
    check(f"no offset: dNBR reproduces dnbr.tif (max error {err:.2e})", err < 1e-5)
    check("no offset: same valid pixels", int(both.sum()) == int(np.isfinite(dnbr).sum()),
          f"{both.sum()} vs {np.isfinite(dnbr).sum()}")
    check("no offset: OFFSET output is 0", res["OFFSET"] == 0.0)
    _, rd = read(res["RDNBR"])
    want = dnbr / np.sqrt(np.maximum(np.abs(pre_on_grid), 0.001))
    check("no offset: RdNBR formula", float(np.nanmax(np.abs(rd - want)[both])) < 1e-4)

# 2. Ring offset matches the independent calculation
res, log = _run(BuildDnbr(), {"PRE_NBR": T + "prefire_nbr.tif", "POST_NBR": POST, "PERIMETER": BURN,
                              "DNBR": os.path.join(OUT, "dnbr1.tif"), "RDNBR": None, "RBR": None})
check("ring offset: runs", res is not None, " | ".join(log))
if res:
    check(f"ring offset {res['OFFSET']:.4f} ≈ independent {want_offset:.4f}",
          abs(res["OFFSET"] - want_offset) < 0.002 and abs(res["OFFSET_N"] - sample.size) < 0.01 * sample.size,
          f"n {res['OFFSET_N']} vs {sample.size}")
    _, got = read(res["DNBR"])
    check("ring offset: dNBR not corrected by default", float(np.nanmax(np.abs(got - dnbr))) < 1e-5)
    for line in log:
        if "offset" in line.lower():
            print("      " + line)

# 3. Fixed offset, subtracted; RBR formula
res, log = _run(BuildDnbr(), {"PRE_NBR": T + "prefire_nbr.tif", "POST_NBR": POST, "OFFSET_METHOD": 3,
                              "OFFSET_VALUE": 0.05, "SUBTRACT_OFFSET": True,
                              "DNBR": os.path.join(OUT, "dnbr3.tif"), "RDNBR": None,
                              "RBR": os.path.join(OUT, "rbr3.tif")})
check("fixed offset: runs", res is not None, " | ".join(log))
if res:
    _, got = read(res["DNBR"])
    check("fixed offset: dNBR - 0.05", float(np.nanmax(np.abs(got - (dnbr - 0.05)))) < 1e-5)
    _, rbr = read(res["RBR"])
    check("fixed offset: RBR formula",
          float(np.nanmax(np.abs(rbr - (dnbr - 0.05) / (pre_on_grid + 1.001)))) < 1e-4)
    check("fixed offset: no RdNBR when not asked", "RDNBR" not in res)

# 4. Unburned-sample polygons: the watershed outside the fire stands in for a sample
res, log = _run(BuildDnbr(), {"PRE_NBR": T + "prefire_nbr.tif", "POST_NBR": POST, "OFFSET_METHOD": 2,
                              "UNBURNED": GPKG + "|layername=subareas", "DNBR": os.path.join(OUT, "dnbr2.tif"),
                              "RDNBR": None, "RBR": None})
check("sample offset: runs and reports a pixel count", res is not None and res["OFFSET_N"] > 1000,
      " | ".join(log))

# 5. Errors
SCALED = os.path.join(OUT, "post_x1000.tif")
gdal.Translate(SCALED, POST, scaleParams=[[-1, 1, -1000, 1000]], outputType=gdal.GDT_Float32)
for name, params, text in [
        ("ring offset without burned area", {"OFFSET_METHOD": 1}, "needs the burned-area polygons"),
        ("sample offset without polygons", {"OFFSET_METHOD": 2}, "needs unburned-sample polygons"),
        ("x1000 NBR is refused", {"OFFSET_METHOD": 0, "POST_NBR": SCALED}, "looks scaled")]:
    p = {"PRE_NBR": T + "prefire_nbr.tif", "POST_NBR": POST, "DNBR": os.path.join(OUT, "never.tif"),
         "RDNBR": None, "RBR": None}
    p.update(params)
    res, log = _run(BuildDnbr(), p)
    check(name, res is None and any(text in m for m in log), " | ".join(log))


# 6. Classify severity with given thresholds: every pixel in the right class
BURN_MASK = burn(union)
res, log = _run(ClassifySeverity(), {"DNBR": T + "dnbr.tif", "PERIMETER": BURN, "LOW_T": 0.1, "MODERATE_T": 0.27,
                                     "HIGH_T": 0.66, "OUTPUT": os.path.join(OUT, "classes.tif")})
check("classify, given thresholds: runs", res is not None, " | ".join(log))
if res:
    got = gdal.Open(res["OUTPUT"]).ReadAsArray()
    want = np.zeros(dnbr.shape, np.uint8)
    v = np.isfinite(dnbr)
    want[BURN_MASK] = 6
    want[BURN_MASK & v] = 1
    for code, t in ((2, 0.1), (3, 0.27), (4, 0.66)):
        want[BURN_MASK & v & (dnbr >= t)] = code
    want[BURN_MASK & v & (dnbr <= -0.15)] = 5
    check(f"classify: classes match ({int((got != want).sum())} pixels differ)", int((got != want).sum()) == 0)
    check("classify: 0 outside the burned area", int(got[~BURN_MASK].max()) == 0)
    check("classify: style file written", os.path.exists(os.path.splitext(res["OUTPUT"])[0] + ".qml"))
    check("classify: thresholds returned", (res["LOW_T"], res["MODERATE_T"], res["HIGH_T"]) == (0.1, 0.27, 0.66))

# 7. Offline estimates: low = offset + 2 robust sd (rounded to 0.005); high matches the RdNBR pattern
#    in dense vegetation; moderate midway
res, log = _run(ClassifySeverity(), {"DNBR": T + "dnbr.tif", "PRE_NBR": T + "prefire_nbr.tif", "PERIMETER": BURN,
                                     "ESTIMATE": 1, "OFFSET": 0.0126, "OFFSET_STD": 0.0428,
                                     "OUTPUT": os.path.join(OUT, "classes_est.tif")})
check("classify, estimated: runs", res is not None, " | ".join(log))
if res:
    check(f"estimated low {res['LOW_T']:.3f} = round(0.0126 + 2 x 0.0428)", abs(res["LOW_T"] - 0.1) < 1e-9)
    hi = res["HIGH_T"]
    check(f"estimated moderate {res['MODERATE_T']:.3f} midway to high {hi:.3f}",
          hi >= 9.999 or abs(res["MODERATE_T"] - round((0.1 + hi) / 2 * 200) / 200) < 1e-9)
    for line in log:
        if "threshold:" in line:
            print("      " + line)
a = np.linspace(-0.2, 1.2, 10001)
t, f1 = match_threshold(a, a >= 0.4)
check(f"match_threshold finds an exact pattern (0.4 -> {t:.3f}, F1 {f1:.2f})", abs(t - 0.4) < 1e-9 and f1 == 1.0)

for name, params, text in [
        ("classify: estimating high from own data needs the pre-fire NBR", {"ESTIMATE": 1}, "needs the pre-fire NBR"),
        ("classify: thresholds must increase", {"LOW_T": 0.3, "MODERATE_T": 0.2, "HIGH_T": 0.6}, "must increase"),
        ("classify: x1000 dNBR is refused", {"DNBR": SCALED, "LOW_T": 0.1, "MODERATE_T": 0.27, "HIGH_T": 0.66},
         "looks scaled")]:
    p = {"DNBR": T + "dnbr.tif", "PERIMETER": BURN, "OUTPUT": os.path.join(OUT, "never_c.tif")}
    p.update(params)
    res, log = _run(ClassifySeverity(), p)
    check(name, res is None and any(text in m for m in log), " | ".join(log))


# 8. Prepare local scene NBR on synthetic products with known DN
from osgeo import osr  # noqa: E402
AOI_SMALL = os.path.join(OUT, "aoi.geojson")
with open(AOI_SMALL, "w") as f:  # about 3 x 3 km near Bright Angel, EPSG:4326
    f.write('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},"geometry":'
            '{"type":"Polygon","coordinates":[[[-112.05,36.20],[-112.02,36.20],[-112.02,36.23],'
            '[-112.05,36.23],[-112.05,36.20]]]}}]}')


def fake_band(path, value, px, epsg=32612, origin=(398000, 4016000), size=(800, 800), dtype=gdal.GDT_UInt16,
              array=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(path, size[0], size[1], 1, dtype)
    ds.SetGeoTransform((origin[0], px, 0, origin[1], 0, -px))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(array if array is not None else np.full((size[1], size[0]), value))
    ds = None


def run_local(folder, **extra):
    p = {"SCENE": folder, "AOI": AOI_SMALL, "BUFFER": 0, "NBR": os.path.join(OUT, os.path.basename(folder) + ".tif")}
    p.update(extra)
    res, log = _run(PrepareLocalNbr(), p)
    if res is None:
        return None, log
    a = gdal.Open(res["NBR"]).ReadAsArray().astype(float)
    return a, log


# Landsat 9 scene: NIR DN 20000, SWIR2 DN 12000 -> reflectance 0.35, 0.13 -> NBR 0.4583
LS = os.path.join(OUT, "LC09_L2SP_037034_20250915_20250916_02_T1")
fake_band(f"{LS}/LC09_L2SP_037034_20250915_20250916_02_T1_SR_B5.TIF", 20000, 30)
fake_band(f"{LS}/LC09_L2SP_037034_20250915_20250916_02_T1_SR_B7.TIF", 12000, 30)
with open(f"{LS}/LC09_L2SP_037034_20250915_20250916_02_T1_MTL.txt", "w") as f:
    f.write("REFLECTANCE_MULT_BAND_5 = 2.75E-05\nREFLECTANCE_ADD_BAND_5 = -0.200000\n"
            "REFLECTANCE_MULT_BAND_7 = 2.75E-05\nREFLECTANCE_ADD_BAND_7 = -0.200000\n")
a, log = run_local(LS)
want = (0.35 - 0.13) / (0.35 + 0.13)
check(f"local Landsat: NBR from scaled reflectance ({want:.4f}), whole AOI covered",
      a is not None and abs(np.nanmax(a) - want) < 1e-5 and abs(a[a != -9999].min() - want) < 1e-5
      and (a != -9999).all(), " | ".join(log))
check("local Landsat: calibration read from the MTL", any("from the metadata" in m for m in log), " | ".join(log))
raw_dn = (20000 - 12000) / (20000 + 12000)
check(f"local Landsat: not the raw-DN NBR ({raw_dn:.4f})", a is not None and abs(np.nanmax(a) - raw_dn) > 0.1)

# QA_PIXEL: cloud bit (3) on the top half -> masked
qa = np.zeros((800, 800), np.uint16)
qa[:, :300] = 1 << 3  # column 300 runs through the AOI
fake_band(f"{LS}/LC09_L2SP_037034_20250915_20250916_02_T1_QA_PIXEL.TIF", 0, 30, array=qa)
a, log = run_local(LS)
gt_ok = a is not None
check("local Landsat: QA_PIXEL cloud bit masks pixels", gt_ok and 0 < (a == -9999).mean() < 1, " | ".join(log))

# Sentinel-2 L2A after 2022 without MTD: offset -1000 by date. NIR DN 4500, SWIR2 DN 2300 -> 0.35, 0.13
S2 = os.path.join(OUT, "S2_20240913")
fake_band(f"{S2}/T12SUF_20240913T180919_B8A_20m.tif", 4500, 20)
fake_band(f"{S2}/T12SUF_20240913T180919_B12_20m.tif", 2300, 20)
a, log = run_local(S2)
check(f"local Sentinel-2 (2024, no MTD): offset -1000 applied, NBR {want:.4f}",
      a is not None and abs(a[a != -9999].mean() - want) < 1e-5, " | ".join(log))
check("local Sentinel-2: 20 m averaged to 30 m", any("average" in m for m in log), " | ".join(log))

# With an MTD that says offset 0 (old baseline): DN/10000 -> 0.45, 0.23
with open(f"{S2}/MTD_MSIL2A.xml", "w") as f:
    f.write("<x><SPACECRAFT_NAME>Sentinel-2A</SPACECRAFT_NAME><BOA_QUANTIFICATION_VALUE unit=\"none\">10000"
            "</BOA_QUANTIFICATION_VALUE></x>")
a, log = run_local(S2)
want0 = (0.45 - 0.23) / (0.45 + 0.23)
check(f"local Sentinel-2 with MTD and no offset: NBR {want0:.4f}",
      a is not None and abs(a[a != -9999].mean() - want0) < 1e-5, " | ".join(log))
with open(f"{S2}/MTD_MSIL2A.xml", "w") as f:
    f.write("<x><SPACECRAFT_NAME>Sentinel-2B</SPACECRAFT_NAME><BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>"
            + "".join(f'<BOA_ADD_OFFSET band_id="{i}">-1000</BOA_ADD_OFFSET>' for i in range(13)) + "</x>")
a, log = run_local(S2)
check("local Sentinel-2 with MTD offsets: read from the file",
      a is not None and abs(a[a != -9999].mean() - want) < 1e-5 and any("from MTD_MSIL2A.xml" in m for m in log),
      " | ".join(log))

# Before 2022 without MTD: refuse unless the offset is set
S2OLD = os.path.join(OUT, "S2_20210601")
fake_band(f"{S2OLD}/T12SUF_20210601T180919_B8A_20m.tif", 4500, 20)
fake_band(f"{S2OLD}/T12SUF_20210601T180919_B12_20m.tif", 2300, 20)
a, log = run_local(S2OLD)
check("local Sentinel-2 before 2022 without MTD is refused", a is None and any("BOA offset" in m for m in log),
      " | ".join(log))
a, log = run_local(S2OLD, S2_OFFSET=2)
check("... and runs with the offset set to 0", a is not None and abs(a[a != -9999].mean() - want0) < 1e-5,
      " | ".join(log))
import shutil  # noqa: E402
MIXED = os.path.join(OUT, "mixed")
shutil.copytree(LS, os.path.join(MIXED, "ls"))
shutil.copytree(S2, os.path.join(MIXED, "s2"))
a, log = run_local(MIXED)
check("local: a folder with Landsat and Sentinel-2 is refused", a is None and any("both Landsat" in m for m in log),
      " | ".join(log))


# 9. Draft burned-area perimeter
m = np.zeros((60, 60))
m[10:40, 10:40] = 0.5   # a 900-pixel burn
m[20:23, 20:23] = 0.0   # a 9-pixel unburned hole inside it
m[50:52, 50:52] = 0.5   # a 4-pixel speck
got = burned_mask(m, 0.1, 10, 20)
check("perimeter mask: speck removed, hole filled, burn kept",
      not got[50:52, 50:52].any() and got[20:23, 20:23].all() and got[10:40, 10:40].all())
res, log = _run(DraftPerimeter(), {"DNBR": T + "dnbr.tif", "OUTPUT": "TEMPORARY_OUTPUT"})
check("draft perimeter: runs", res is not None, " | ".join(log))
if res is not None:
    from qgis.core import QgsProcessingUtils  # noqa: E402
    ctx = QgsProcessingContext()  # kept, so the temporary output layer stays alive
    alg = DraftPerimeter()
    alg.initAlgorithm()
    p = {d.name(): d.defaultValue() for d in alg.parameterDefinitions()}
    p.update({"DNBR": T + "dnbr.tif", "SEED": BURN, "OUTPUT": "TEMPORARY_OUTPUT"})
    fb = _Feedback()
    res, ok = alg.create().run(p, ctx, fb)
    layer = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx)
    draft = next(layer.getFeatures()).geometry()
    truth = QgsGeometry.unaryUnion([f.geometry() for f in QgsVectorLayer(BURN, "b", "ogr").getFeatures()])
    iou = draft.intersection(truth).area() / draft.combine(truth).area()
    check(f"draft perimeter seeded with the test burned area overlaps it (IoU {iou:.2f})", iou > 0.5,
          " | ".join(fb.log))
    # Clipped to the seed: nothing outside it, and still polygons
    p.update({"CLIP": True})
    fb = _Feedback()
    res, ok = alg.create().run(p, ctx, fb)
    clipped = next(QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx).getFeatures()).geometry()
    outside = clipped.difference(truth.buffer(1, 4)).area()
    check(f"draft perimeter clipped to the seed: nothing outside it ({outside:.0f} m²), "
          f"{clipped.area() / 1e4:,.0f} of {draft.area() / 1e4:,.0f} ha kept",
          ok and outside < 1 and 0 < clipped.area() <= draft.area() and clipped.type() == draft.type(),
          " | ".join(fb.log))


# 10. Severity map (PDF) from classes made with given thresholds
res, log = _run(ClassifySeverity(), {"DNBR": T + "dnbr.tif", "PERIMETER": BURN, "LOW_T": 0.1, "MODERATE_T": 0.27,
                                     "HIGH_T": 0.66, "OUTPUT": os.path.join(OUT, "classes_map.tif")})
if res is not None:
    pdf, png = os.path.join(OUT, "map.pdf"), os.path.join(OUT, "map.png")
    res, log = _run(SeverityMap(), {"CLASSES": res["OUTPUT"], "PERIMETER": BURN, "SUBAREAS": GPKG + "|layername=watershed",
                                    "TITLE": "Test map", "PDF": pdf, "PNG": png})
    check("severity map: PDF and PNG written",
          res is not None and os.path.getsize(pdf) > 10000 and os.path.getsize(png) > 10000, " | ".join(log))


# 11. Provenance: local-scene NBR metadata reaches the dNBR (POST_SOURCE_DATE etc.)
a_ls, _ = run_local(LS)
res, log = _run(BuildDnbr(), {"PRE_NBR": os.path.join(OUT, os.path.basename(LS) + ".tif"),
                              "POST_NBR": os.path.join(OUT, os.path.basename(LS) + ".tif"), "OFFSET_METHOD": 0,
                              "DNBR": os.path.join(OUT, "dnbr_prov.tif"), "RDNBR": None, "RBR": None})
md = gdal.Open(res["DNBR"]).GetMetadata() if res else {}
check("provenance: POST_SOURCE_DATE and POST_CALIBRATION copied into the dNBR",
      md.get("POST_SOURCE_DATE") == "2025-09-15" and "POST_CALIBRATION" in md, str(md)[:300])


# 12. Pipeline report inputs: which perimeter and sub-areas a run used, and the perimeter's area
from fireanalysis.algorithms.build_dnbr import source_union  # noqa: E402
from fireanalysis.algorithms.burn_severity_pipeline import BurnSeverityPipeline, _inputs_html  # noqa: E402
pipe = BurnSeverityPipeline()
pipe.initAlgorithm()
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
burn_layer = QgsVectorLayer(BURN, "burn_area", "ogr")
geom, crs = source_union(burn_layer, "burned-area")
info = pipe._layer_info({"PERIMETER": BURN}, "PERIMETER", ctx, geom, crs)
planar = geom.area() / 1e4  # EPSG:26912, so close to the ellipsoidal area
check(f"report inputs: perimeter source, features and area ({info and info.get('area_ha', 0):,.0f} ha, "
      f"planar {planar:,.0f})",
      info is not None and "testdata.gpkg" in info["source"] and info["features"] == burn_layer.featureCount()
      and abs(info["area_ha"] - planar) / planar < 0.01 and not info["temporary"], str(info))
mem = QgsVectorLayer("Polygon?crs=EPSG:26912", "drawn", "memory")
QgsProject.instance().addMapLayer(mem)
info_mem = pipe._layer_info({"SUBAREAS": mem.id()}, "SUBAREAS", ctx)
page = _inputs_html({"inputs": {"perimeter": info, "subareas": info_mem, "subarea_name": "Drawn"}})
check("report inputs: temporary layer flagged, both layers in the HTML",
      info_mem is not None and info_mem["temporary"] and "burn_area" in page and "temporary layer" in page,
      str(info_mem) + " | " + page[:300])
check("report inputs: none for an empty optional layer", pipe._layer_info({"SUBAREAS": None}, "SUBAREAS", ctx) is None)
QgsProject.instance().removeMapLayer(mem.id())

# 13. A scenes layer (Find ... scenes output) given as the area of interest or perimeter is refused
from qgis.core import QgsFeature  # noqa: E402
from qgis.PyQt.QtCore import QDate  # noqa: E402
from fireanalysis.algorithms.find_hls_scenes import FindHlsScenes  # noqa: E402
for label, spec in (("HLS", "granule:string&field=sensor:string&field=date:string"),
                    ("Planetary Computer", "item:string&field=collection:string&field=platform:string&field=date:string")):
    scenes = QgsVectorLayer(f"Polygon?crs=EPSG:4326&field={spec}", "scenes", "memory")
    ft = QgsFeature(scenes.fields())
    ft.setGeometry(QgsGeometry.fromWkt("POLYGON((-112.2 36.1,-112.0 36.1,-112.0 36.3,-112.2 36.3,-112.2 36.1))"))
    scenes.dataProvider().addFeatures([ft])
    res, log = _run(FindHlsScenes(), {"AOI": scenes, "START": QDate(2025, 7, 4), "END": QDate(2025, 9, 30),
                                      "OUTPUT": "TEMPORARY_OUTPUT"})
    check(f"{label} scenes layer as the area of interest is refused",
          res is None and any("scenes layer" in m for m in log), " | ".join(log[-3:]))
    res, log = _run(BuildDnbr(), {"PRE_NBR": T + "prefire_nbr.tif", "POST_NBR": T + "prefire_nbr.tif",
                                  "PERIMETER": scenes, "DNBR": os.path.join(OUT, "dnbr_scenes.tif"),
                                  "RDNBR": None, "RBR": None})
    check(f"{label} scenes layer as the burned area is refused",
          res is None and any("scenes layer" in m for m in log), " | ".join(log[-3:]))

# 14. Fire name and dates from a WFIGS perimeter's attributes
import datetime  # noqa: E402
from qgis.PyQt.QtCore import QDateTime  # noqa: E402
from fireanalysis.incident import fire_details, read_incident  # noqa: E402
from fireanalysis.algorithms.suggest_image_pairs import SuggestImagePairs  # noqa: E402


def wfigs(rows, text_dates=False):
    """A memory layer with WFIGS fields; rows: dicts of attr_/poly_ values."""
    kind = "string" if text_dates else "datetime"
    spec = ("poly_IncidentName:string&field=attr_UniqueFireIdentifier:string"
            f"&field=attr_FireDiscoveryDateTime:{kind}&field=attr_ContainmentDateTime:{kind}"
            f"&field=attr_ControlDateTime:{kind}&field=attr_FireOutDateTime:{kind}"
            "&field=attr_PercentContained:double&field=attr_ICS209RptForTimePeriodTo:datetime"
            "&field=poly_PolygonDateTime:datetime&field=poly_GISAcres:double"
            "&field=attr_InitialLatitude:double&field=attr_InitialLongitude:double")
    layer = QgsVectorLayer(f"Polygon?crs=EPSG:26912&field={spec}", "wfigs", "memory")
    feats = []
    for row in rows:
        ft = QgsFeature(layer.fields())
        ft.setGeometry(QgsGeometry.fromWkt("POLYGON((400000 4000000,401000 4000000,401000 4001000,400000 4001000,"
                                           "400000 4000000))"))
        for k, v in row.items():
            ft[k] = v
        feats.append(ft)
    layer.dataProvider().addFeatures(feats)
    return layer


def utc(*a):
    """A QDateTime for a UTC time, on Qt 5 and 6."""
    return QDateTime.fromSecsSinceEpoch(int(datetime.datetime(*a, tzinfo=datetime.timezone.utc).timestamp()))


base = {"poly_IncidentName": "Ferris", "attr_UniqueFireIdentifier": "2026-COSJF-000536",
        "attr_FireDiscoveryDateTime": utc(2026, 6, 27, 21, 4), "poly_GISAcres": 64881.2,
        "attr_InitialLatitude": 37.657, "attr_InitialLongitude": -108.694}
cases = [
    ("containment date", dict(base, attr_ContainmentDateTime=utc(2026, 7, 20, 19)), False,
     (datetime.date(2026, 6, 27), datetime.date(2026, 7, 20)), "containment"),
    ("no containment: control date", dict(base, attr_ControlDateTime=utc(2026, 7, 25, 19)), False,
     (datetime.date(2026, 6, 27), datetime.date(2026, 7, 25)), "control"),
    ("no end dates, 100 % contained: last ICS-209", dict(base, attr_PercentContained=100.0,
                                                         attr_ICS209RptForTimePeriodTo=utc(2026, 8, 6, 20)), False,
     (datetime.date(2026, 6, 27), datetime.date(2026, 8, 6)), "ICS-209"),
    ("evening containment in UTC is the local day before", dict(base, attr_ContainmentDateTime=utc(2026, 8, 6, 1)),
     False, (datetime.date(2026, 6, 27), datetime.date(2026, 8, 5)), "containment"),
    ("no end dates, not contained: perimeter date", dict(base, poly_PolygonDateTime=utc(2026, 7, 11, 19)), False,
     (datetime.date(2026, 6, 27), datetime.date(2026, 7, 11)), "perimeter"),
    ("dates as text (GeoJSON)", dict(base, attr_FireDiscoveryDateTime="2026-06-27T21:04:00Z",
                                     attr_ContainmentDateTime="2026/07/20 19:00:00"), True,
     (datetime.date(2026, 6, 27), datetime.date(2026, 7, 20)), "containment"),
]
for label, row, text, want, note in cases:
    layer = wfigs([row], text)
    alg = BurnSeverityPipeline()
    alg.initAlgorithm()
    fb = _Feedback()
    try:
        name, start, end, inc = fire_details(alg, {"PERIMETER": layer}, ctx, fb, "PERIMETER", "FIRE_NAME",
                                             "FIRE_START", "FIRE_END")
        ok = (name, start, end) == ("Ferris", *want) and note in (inc["end_note"] or "") \
            and inc["ignition"] == (37.657, -108.694)
        detail = f"{name} {start} {end} {inc['end_note']} | " + " | ".join(fb.log)
    except Exception as e:
        ok, detail = False, str(e)
    check(f"WFIGS record, {label}", ok, detail)

# Given values win over the record
layer = wfigs([dict(base, attr_ContainmentDateTime=utc(2026, 7, 20))])
alg = BurnSeverityPipeline()
alg.initAlgorithm()
name, start, end, _ = fire_details(alg, {"PERIMETER": layer, "FIRE_NAME": "My fire",
                                         "FIRE_END": QDateTime(datetime.datetime(2026, 7, 30))},
                                   ctx, _Feedback(), "PERIMETER", "FIRE_NAME", "FIRE_START", "FIRE_END")
check("WFIGS record: a given name and end date win", (name, start, end) ==
      ("My fire", datetime.date(2026, 6, 27), datetime.date(2026, 7, 30)), f"{name} {start} {end}")

# A layer with several fires, and a layer without a WFIGS record and without dates, are refused
many = wfigs([dict(base, attr_UniqueFireIdentifier=f"2026-XX-{i}") for i in range(8)])
res, log = _run(SuggestImagePairs(), {"AOI": many, "OUTPUT": "TEMPORARY_OUTPUT"})
check("WFIGS layer with several fires is refused", res is None and any("several fires" in m for m in log),
      " | ".join(log[-3:]))
res, log = _run(SuggestImagePairs(), {"AOI": BURN, "OUTPUT": "TEMPORARY_OUTPUT"})
check("no dates and no WFIGS record: asks for the dates", res is None and any("Set the fire start and end date" in m
                                                                             for m in log), " | ".join(log[-3:]))
check("non-WFIGS layer: no record", read_incident(QgsVectorLayer(BURN, "b", "ogr")) is None)

# 15. The classes get the MTBS colours when Processing loads them (it skips the .qml default style)
from qgis.core import QgsProcessingOutputLayerDefinition, QgsRasterLayer  # noqa: E402
alg = ClassifySeverity()
alg.initAlgorithm()
p = {d.name(): d.defaultValue() for d in alg.parameterDefinitions()}
out_path = os.path.join(OUT, "classes_loaded.tif")
p.update({"DNBR": T + "dnbr.tif", "PERIMETER": BURN, "LOW_T": 0.1, "MODERATE_T": 0.27, "HIGH_T": 0.66,
          "OUTPUT": QgsProcessingOutputLayerDefinition(out_path, QgsProject.instance())})
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
fb = _Feedback()
res, ok = alg.create().run(p, ctx, fb)
details = ctx.layersToLoadOnCompletion().get(out_path) if ok else None
styled = None
if details is not None and details.postProcessor() is not None:
    lyr = QgsRasterLayer(out_path, "classes")
    details.postProcessor().postProcessLayer(lyr, ctx, fb)
    styled = lyr.renderer().type()
check(f"classify: loaded classes get the MTBS palette (renderer {styled})", styled == "paletted",
      " | ".join(fb.log[-3:]))

print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
if _app is not None:
    _app.exitQgis()
    sys.exit(1 if failed else 0)
elif failed:
    raise RuntimeError(f"{failed} pipeline test check(s) failed")
