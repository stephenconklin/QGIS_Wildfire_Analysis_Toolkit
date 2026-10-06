"""
Offline test for the fireanalysis algorithms that need no network (so far:
Build dNBR). The online HLS test is dev/online_test.py.

Inside QGIS (Python Console):
    exec(open('/path/to/QGIS_Burn_Severity/dev/pipeline_test.py').read())

Headless (macOS):
    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app pipeline_test.py

Builds a synthetic post-fire NBR on the testdata/dnbr.tif grid (pre-fire NBR
resampled to that grid, minus the dNBR). Build dNBR must then give back
dnbr.tif, an offset that matches an independent OGR/GDAL calculation, and
RdNBR/RBR that match their formulas.
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
                       QgsProcessingFeedback, QgsProject)

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


print(f"Fire Analysis {VERSION} pipeline test on QGIS {Qgis.QGIS_VERSION}; outputs in {OUT}")

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

print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
if _app is not None:
    _app.exitQgis()
    sys.exit(1 if failed else 0)
elif failed:
    raise RuntimeError(f"{failed} pipeline test check(s) failed")
