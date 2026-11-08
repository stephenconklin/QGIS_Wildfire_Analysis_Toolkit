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


def corner_alpha(layer):
    """Alpha of the rendered top-left corner, outside the test burned area (class 0)."""
    from qgis.core import QgsMapRendererParallelJob, QgsMapSettings
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor
    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setDestinationCrs(layer.crs())
    ms.setExtent(layer.extent())
    ms.setOutputSize(QSize(100, 100))
    ms.setBackgroundColor(QColor(0, 0, 0, 0))
    job = QgsMapRendererParallelJob(ms)
    job.start()
    job.waitForFinished()
    return QColor.fromRgba(job.renderedImage().pixel(1, 1)).alpha()  # QColor(int) would drop the alpha


if styled:
    from fireanalysis import styles as _styles  # noqa: E402
    by_style = QgsRasterLayer(out_path, "classes")
    _styles.mtbs_classes(by_style)
    by_qml = QgsRasterLayer(out_path, "classes")
    by_qml.loadNamedStyle(os.path.splitext(out_path)[0] + ".qml")
    a_style, a_qml = corner_alpha(by_style), corner_alpha(by_qml)
    check(f"classes: outside the burned area is transparent (alpha {a_style} styled, {a_qml} from the .qml)",
          a_style == 0 and a_qml == 0)

# 16. Pipeline results load into one styled group per run; a rerun into the same folder reuses it
import shutil  # noqa: E402
try:
    from processing.gui.Postprocessing import handleAlgorithmResults  # noqa: E402
except ImportError:
    handleAlgorithmResults = None
if handleAlgorithmResults is not None and os.path.exists(out_path):
    run = os.path.join(OUT, "fake_run")
    os.makedirs(run, exist_ok=True)
    for src, dst in ((out_path, "dnbr6.tif"), (T + "dnbr.tif", "dnbr.tif"), (T + "prefire_nbr.tif", "nbr_pre.tif")):
        shutil.copy(src, os.path.join(run, dst))
    project = QgsProject.instance()
    groups = []
    for i in range(2):
        if i == 1:  # scramble the order and visibility, as an earlier version or the user might leave them
            g = project.layerTreeRoot().findGroup("Test fire " + datetime.date.today().isoformat())
            first = g.children()[0]
            g.addChildNode(first.clone())
            g.removeChildNode(first)
            g.findLayers()[0].setItemVisibilityChecked(True)
        alg = BurnSeverityPipeline()
        alg.initAlgorithm()
        ctx = QgsProcessingContext()
        ctx.setProject(project)
        alg._load({"LOAD": 0}, ctx, "Test fire", run, False, 0.1, 0.27, 0.66)
        handleAlgorithmResults(alg, ctx, QgsProcessingFeedback(), {})
        groups = [g for g in project.layerTreeRoot().findGroups(True) if g.name().startswith("Test fire")]
    nodes = groups[0].findLayers() if groups else []
    got = [(n.layer().name(), n.itemVisibilityChecked(), n.layer().renderer().type()) for n in nodes]
    want = [("Severity classes (MTBS)", True, "paletted"),
            ("dNBR (thresholds 0.100 / 0.270 / 0.660)", False, "singlebandpseudocolor"),
            ("Pre-fire NBR", False, "singlebandgray")]
    check("pipeline results: one group, named, ordered, styled, only the classes shown; reused on rerun",
          len(groups) == 1 and got == want, f"{[g.name() for g in groups]} {got}")
    for g in groups:
        project.removeMapLayers([n.layerId() for n in g.findLayers()])
        g.parent().removeChildNode(g)

# 17. The dNBR offset: the draft threshold is above it, and nearby MTBS thresholds move with it
from fireanalysis import mtbs as mtbs_service  # noqa: E402
from qgis.core import QgsCoordinateReferenceSystem  # noqa: E402
fires = [{"fire_id": str(i), "asmnt_type": "Initial", "dnbr_offst": o, "low_threshold": 100 + o,
          "moderate_threshold": 300 + o, "high_threshold": 600 + o} for i, o in enumerate((-30, -10, 0, 20, 40))]
raw_t = mtbs_service.median_thresholds(fires, "Initial")[0]
adj_t = mtbs_service.median_thresholds(fires, "Initial", offset=0.105)[0]
check(f"nearby thresholds relative to each fire's offset, at this pair's ({[round(x, 3) for x in adj_t]})",
      [round(x, 3) for x in raw_t] == [0.1, 0.3, 0.6] and [round(x, 3) for x in adj_t] == [0.205, 0.405, 0.705])

# A synthetic dNBR whose unburned land reads 0.1 (offset in the metadata, as Build dNBR writes it),
# with a large burn and a small separate one
grid = np.full((200, 200), 0.1, dtype=np.float32)
grid[20:120, 20:120] = 0.5   # 100 x 100 px = 900 ha
grid[150:175, 150:175] = 0.5  # 25 x 25 px = 56 ha, 6 % of the large one
path = os.path.join(OUT, "offset_dnbr.tif")
ds = gdal.GetDriverByName("GTiff").Create(path, 200, 200, 1, gdal.GDT_Float32)
ds.SetGeoTransform((400000, 30, 0, 4000000, 0, -30))
ds.SetProjection(QgsCoordinateReferenceSystem("EPSG:26912").toWkt())
ds.GetRasterBand(1).WriteArray(grid)
ds.GetRasterBand(1).SetNoDataValue(-9999)
ds.SetMetadata({"DNBR_OFFSET": "0.100000"})
ds = None
for label, extra, want_ha in (("threshold above the metadata offset", {}, 956),
                              ("keep patches of at least 25 % of the largest", {"KEEP_SHARE": 25}, 900),
                              ("offset given as 0: everything burned", {"OFFSET": 0.0}, 3600)):
    res, log = _run(DraftPerimeter(), dict({"DNBR": path, "OUTPUT": os.path.join(OUT, "draft_off.gpkg")}, **extra))
    area = next(QgsVectorLayer(os.path.join(OUT, "draft_off.gpkg"), "d", "ogr").getFeatures())["area_ha"] if res else 0
    check(f"draft: {label} ({area:,.0f} ha, want {want_ha:,})", res is not None and abs(area - want_ha) < 15,
          " | ".join(log[-3:]))

# 18. Report: a "selected features only" input names its layer; zone names, image dates, threshold note
from qgis.core import QgsProcessingFeatureSourceDefinition  # noqa: E402
from fireanalysis.algorithms.burn_severity_pipeline import _html  # noqa: E402
sel = QgsVectorLayer(BURN, "burn_area", "ogr")
QgsProject.instance().addMapLayer(sel)
sel.selectByIds([next(sel.getFeatures()).id()])
pipe = BurnSeverityPipeline()
pipe.initAlgorithm()
ctx = QgsProcessingContext()
ctx.setProject(QgsProject.instance())
info = pipe._layer_info({"PERIMETER": QgsProcessingFeatureSourceDefinition(sel.id(), True)}, "PERIMETER", ctx)
check("report: a selected-features input shows its layer name and file",
      info["layer"] == "burn_area" and "testdata.gpkg" in info["source"] and info["selected_only"] and info["features"] == 1,
      str(info))
QgsProject.instance().removeMapLayer(sel.id())
fields = ["ord", "sev_class", "pct_db", "pct_ba", "ha_db", "ha_ba", "ac_db", "ac_ba"]
rows = [{"ord": 1, "sev_class": "Low", "pct_db": 60.0, "pct_ba": 55.0, "ha_db": 10.0, "ha_ba": 5.0, "ac_db": 25.0,
         "ac_ba": 12.0}]
rep = {"fire": "Test", "fire_start": "2025-07-04", "fire_end": "2025-09-01", "assessment": "Initial (soon after)",
       "fireanalysis_version": "x", "created": "now", "imagery": "HLS", "files": [], "warnings": [],
       "zones": {"db": "Test fire", "ba": "Bright Angel"},
       "pair": {"pre_date": "2024-09-12", "pre_sensor": "L30", "pre_clear": "100", "post_date": "2025-09-15",
                "post_sensor": "L30", "post_clear": "99.5", "doy_gap": "3", "years": "1", "source": "suggested",
                "pre_granules": ["a"], "post_granules": ["b"]},
       "offset": {"value": 0.01, "std": 0.02, "pixels": 100, "method": "ring"},
       "thresholds": {"low": 0.1, "moderate": 0.27, "high": 0.66, "notes": []},
       "inputs": {"thresholds_given": {}}}
page = _html(rep, fields, rows)
check("report: zone names, not field codes, in the chart and table; the pair's dates",
      "<b>Bright Angel</b>" in page and "<th>% Test fire</th>" in page and "<th>acres Bright Angel</th>" in page
      and "pct_db" not in page and "Pre-fire 2024-09-12 (L30" in page and "1 year apart, 3 days apart" in page)
rep["inputs"]["thresholds_given"] = {"low_t": 0.1, "moderate_t": 0.27, "high_t": 0.66}
check("report: the estimated-threshold note only when a threshold was estimated",
      "Estimated thresholds are a starting point" in _html(dict(rep, inputs={"thresholds_given": {}}), fields, rows)
      and "Estimated thresholds are a starting point" not in _html(rep, fields, rows))

# ---- MTBS records (offline: synthetic records, no download) ----------------------
import datetime  # noqa: E402
from fireanalysis import mtbs as mtbs_records  # noqa: E402
from fireanalysis.incident import read_mtbs  # noqa: E402
from fireanalysis.algorithms.burn_severity_pipeline import _mtbs_html  # noqa: E402
from qgis.core import QgsFeature, QgsField, QgsPointXY  # noqa: E402
from qgis.PyQt.QtCore import QMetaType  # noqa: E402

sc = mtbs_records.parse_scene("803503420170919")
check("MTBS scene id: Landsat 8 path/row and date, HLS L30",
      (sc["platform"], sc["where"], sc["date"], sc["hls"], sc["planetary"]) ==
      ("landsat-8", "35/34", datetime.date(2017, 9, 19), "L30", "landsat"), str(sc))
sc = mtbs_records.parse_scene("C12SUF20260620_30m")
check("MTBS scene id: Sentinel-2C tile and date, HLS S30",
      (sc["platform"], sc["where"], sc["date"], sc["hls"]) == ("sentinel-2c", "12SUF", datetime.date(2026, 6, 20), "S30"),
      str(sc))
sc = mtbs_records.parse_scene("503403520110624")
check("MTBS scene id: Landsat 5 is not in HLS (Planetary Computer only)", sc["hls"] is None and sc["planetary"] == "landsat")
try:
    mtbs_records.parse_scene("X")
    bad = False
except ValueError:
    bad = True
check("MTBS scene id: anything else is refused", bad)

DRAGON = {"event_id": "AZ3623811210320250704", "irwinid": "EC1EC492-0C76-4B92-8960-D18A13405F1C",
          "incid_name": "DRAGON BRAVO", "incid_type": "Wildfire", "asmnt_type": "Extended", "burnbndac": 148455,
          "burnbndlat": "36.35300970204768", "burnbndlon": "-112.0524556472839", "ig_date": "2025-07-04",
          "pre_id": "B12SUF20250620_30m", "post_id": "C12SUF20260620_30m", "dnbr_offst": 22.0, "dnbr_stddv": 26.0,
          "low_t": 66, "mod_t": 216, "high_t": 471, "map_prog": "MTBS"}
recs = [mtbs_records.record(DRAGON),
        mtbs_records.record(dict(DRAGON, event_id="AZ3623811210320250999", incid_name="DRAGON", ig_date="2018-06-01",
                                 irwinid="X", burnbndac=500, high_t=9999)),
        mtbs_records.record(dict(DRAGON, event_id="CO3746110780820180601", incid_name="416", ig_date="2018-06-01",
                                 burnbndlat="37.494", burnbndlon="-107.905", irwinid="Y", incid_type="Prescribed Fire"))]
r = recs[0]
check("MTBS record: year, centre and map-service keys", r["year"] == 2025 and abs(r["lon"] + 112.0525) < 1e-3
      and r["fire_id"] == r["event_id"] and r["low_threshold"] == 66 and mtbs_records.display_name(r) == "Dragon Bravo")
t0 = mtbs_records.thresholds(r)
t1 = mtbs_records.thresholds(r, 0.036)
check(f"MTBS thresholds: raw {t0}, at this pair's offset {[round(x, 3) for x in t1]}",
      t0 == [0.066, 0.216, 0.471] and [round(x, 4) for x in t1] == [0.08, 0.23, 0.485])
check("MTBS thresholds: 9999 is no class (9.999)", mtbs_records.thresholds(recs[1])[2] == 9.999)
check("MTBS search: name and year", [x["event_id"] for x in mtbs_records.search(recs, "Dragon Bravo 2025")]
      == ["AZ3623811210320250704"])
check("MTBS search: whole words ('Dragon' finds both, exact name first)",
      [x["incid_name"] for x in mtbs_records.search(recs, "dragon")] == ["DRAGON", "DRAGON BRAVO"])
check("MTBS search: event id", len(mtbs_records.search(recs, "co3746110780820180601")) == 1)
check("MTBS search: wildfires only, years", [x["incid_name"] for x in mtbs_records.search(
    recs, first_year=2018, last_year=2018, wildfires_only=True)] == ["DRAGON"])
check("MTBS resolve: exact name wins over a longer one", mtbs_records.resolve(recs, "Dragon")["incid_name"] == "DRAGON")
try:
    mtbs_records.resolve(recs + [mtbs_records.record(dict(DRAGON, event_id="AZ1", ig_date="2020-01-01"))],
                         "Dragon Bravo")
    msg = ""
except mtbs_records.MtbsError as e:
    msg = str(e)
check("MTBS resolve: several matches are listed, asking for the year", "2 MTBS fires match" in msg and "add the year" in msg,
      msg)
check("MTBS by IRWIN id: braces and case don't matter",
      mtbs_records.by_irwin(recs, "{ec1ec492-0c76-4b92-8960-d18a13405f1c}")["event_id"] == "AZ3623811210320250704")
box = QgsGeometry.fromWkt("POLYGON((-112.3 36.1, -111.8 36.1, -111.8 36.6, -112.3 36.6, -112.3 36.1))")
found = mtbs_records.by_location(recs, box, datetime.date(2025, 7, 1), datetime.date(2025, 9, 1), 140000)
check("MTBS by location and date: the fire centred inside that ignited between the dates",
      [x["event_id"] for x in found] == ["AZ3623811210320250704"])
check("MTBS by location: no match outside the dates",
      mtbs_records.by_location(recs, box, datetime.date(2010, 1, 1), datetime.date(2010, 2, 1)) == [])

# read_mtbs: a perimeter layer with MTBS fields (the USGS file, or Find MTBS fires output)
def mtbs_layer(rows):
    lyr = QgsVectorLayer("Polygon?crs=EPSG:26912", "mtbs", "memory")
    names = ["event_id", "incid_name", "ig_date", "asmnt_type", "pre_id", "post_id", "irwinid"]
    nums = ["low_t", "mod_t", "high_t", "dnbr_offst", "burnbndac"]
    lyr.dataProvider().addAttributes([QgsField(n, QMetaType.Type.QString) for n in names]
                                     + [QgsField(n, QMetaType.Type.Double) for n in nums])
    lyr.updateFields()
    geom = next(QgsVectorLayer(BURN, "b", "ogr").getFeatures()).geometry()
    for row in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(geom)
        for n in names + nums:
            f[n] = row.get(n)
        lyr.dataProvider().addFeature(f)
    return lyr


one = mtbs_layer([DRAGON])
rec = read_mtbs(one)
check("MTBS layer: the record is read from its fields", rec and rec["event_id"] == "AZ3623811210320250704"
      and rec["low_t"] == 66 and rec["year"] == 2025)
try:
    read_mtbs(mtbs_layer([DRAGON, dict(DRAGON, event_id="CO3746110780820180601")]))
    msg = ""
except Exception as e:
    msg = str(e)
check("MTBS layer with several fires is refused", "several MTBS fires" in msg, msg)
check("non-MTBS layer: no MTBS record", read_mtbs(QgsVectorLayer(BURN, "b", "ogr")) is None)

# Classify severity with the fire's own MTBS thresholds, read from the perimeter layer
QgsProject.instance().addMapLayer(one)
res, log = _run(ClassifySeverity(), {"DNBR": T + "dnbr.tif", "PERIMETER": one.id(), "OFFSET": 0.036,
                                     "OUTPUT": os.path.join(OUT, "mtbs_classes.tif")})
check("classify, MTBS thresholds (default): the record's, moved to the offset (0.080 / 0.230 / 0.485)",
      res is not None and [round(res[k], 3) for k in ("LOW_T", "MODERATE_T", "HIGH_T")] == [0.08, 0.23, 0.485]
      and any("; MTBS AZ3623811210320250704: 66 with MTBS offset 22" in m for m in log), " | ".join(log))
ds = gdal.Open(os.path.join(OUT, "mtbs_classes.tif")) if res else None
check("classify, MTBS thresholds: the event id in the raster metadata",
      ds is not None and ds.GetMetadata().get("MTBS_EVENT_ID") == "AZ3623811210320250704")
QgsProject.instance().removeMapLayer(one.id())

# compare(): agreement and kappa
ours = np.array([[1, 2, 3, 4], [1, 2, 3, 4], [0, 0, 6, 5]], np.uint8)
theirs = np.array([[1, 2, 3, 3], [1, 2, 4, 4], [1, 1, 1, 5]], np.uint8)
c = mtbs_records.compare(ours, theirs, ours > 0)
check(f"compare with MTBS: agreement {c and round(c['agreement'], 1)} % over 9 px, within one class 100 %",
      c and c["pixels"] == 9 and abs(c["agreement"] - 700 / 9) < 1e-9 and c["within_one"] == 100.0 and -1 < c["kappa"] < 1)
check("compare with MTBS: no overlap gives None", mtbs_records.compare(ours, np.zeros_like(ours), ours > 0) is None)

# Report: the MTBS section
rep2 = dict(rep, mtbs={**{k: DRAGON.get(k) for k in mtbs_records.PUBLISHED}, "how": "named in MTBS fire",
                       "perimeter_source": "MTBS map service (USFS EDW)", "pair_used": True,
                       "comparison": {"pixels": 9, "agreement": 77.8, "kappa": 0.7, "within_one": 100.0,
                                      "ours": [20, 20, 20, 20, 20], "theirs": [20, 20, 20, 20, 20]}},
            thresholds=dict(rep["thresholds"], from_mtbs=True), inputs={"thresholds_given": {}})
page = _html(rep2, fields, rows)
check("report: MTBS record, what was used, and the comparison",
      "<h2>MTBS record</h2>" in page and "AZ3623811210320250704" in page and "image dates" in page
      and "thresholds (moved to this pair" in page and "kappa 0.700" in page
      and "own thresholds for this fire, each taken relative" in page, page[page.find("MTBS record"):][:600])
check("report: no MTBS section without a record", "<h2>MTBS record</h2>" not in _html(rep, fields, rows))

print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
if _app is not None:
    _app.exitQgis()
    sys.exit(1 if failed else 0)
elif failed:
    raise RuntimeError(f"{failed} pipeline test check(s) failed")
