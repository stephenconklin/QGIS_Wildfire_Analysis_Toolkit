"""
Score Draft burned-area perimeter against MTBS perimeters, on the outputs of
dev/mtbs_validation.py (no network needed).

    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app perimeter_study.py <validation output folder>

For each fire: our dNBR on the validation grid (the MTBS perimeter plus 3 km),
the draft at several thresholds and minimum patch sizes, seeded with a 1 km
circle around the MTBS fire location (as a user would give an ignition point),
and the intersection over union (IoU) with the MTBS perimeter.
"""
import glob
import json
import os
import sys

try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Burn_Severity"
sys.path.insert(0, REPO)

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,  # noqa: E402
                       QgsCoordinateTransformContext, QgsGeometry, QgsPointXY, QgsVectorLayer)

_app = None
if QgsApplication.instance() is None:
    _app = QgsApplication([], False)
    _app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal  # noqa: E402

from fireanalysis.algorithms.draft_perimeter import burned_mask, polygonize  # noqa: E402

gdal.UseExceptions()
CTX = QgsCoordinateTransformContext()
THRESHOLDS = (0.05, 0.075, 0.10, 0.15)
MIN_HA = (1, 4, 20)


def main(root):
    results = {}
    for folder in sorted(glob.glob(os.path.join(root, "*/"))):
        if not os.path.exists(os.path.join(folder, "dnbr.tif")):
            continue
        a = json.load(open(os.path.join(folder, "perimeter.geojson")))["features"][0]["properties"]
        ds = gdal.Open(os.path.join(folder, "dnbr.tif"))
        d = ds.ReadAsArray().astype(float)
        d[d == -9999] = np.nan
        px_ha = abs(ds.GetGeoTransform()[1] * ds.GetGeoTransform()[5]) / 10000
        rcrs = QgsCoordinateReferenceSystem.fromWkt(ds.GetProjection())
        lyr = QgsVectorLayer(os.path.join(folder, "perimeter.geojson"), "p", "ogr")
        truth = QgsGeometry.unaryUnion([f.geometry() for f in lyr.getFeatures()])
        truth.transform(QgsCoordinateTransform(lyr.crs(), rcrs, CTX))
        seed = QgsGeometry.fromPointXY(QgsPointXY(a["longitude"], a["latitude"]))
        seed.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), rcrs, CTX))
        seed = seed.buffer(1000, 16)
        row = {}
        for t in THRESHOLDS:
            for ha in MIN_HA:
                mask = burned_mask(d, t, int(ha / px_ha), int(2 / px_ha))
                polys = polygonize(mask, ds)
                for seeded in (False, True):
                    keep = [g for g in polys if g.intersects(seed)] if seeded else polys
                    if not keep:
                        row[(t, ha, seeded)] = 0.0
                        continue
                    g = QgsGeometry.unaryUnion(keep)
                    inter = g.intersection(truth).area()
                    union = g.combine(truth).area()
                    row[(t, ha, seeded)] = inter / union if union else 0.0
        results[a["fire_name"]] = row
        best = max(row, key=row.get)
        print(f"{a['fire_name'][:14]:14s} best IoU {row[best]:.3f} at dNBR >= {best[0]}, {best[1]} ha, "
              f"{'seeded' if best[2] else 'all patches'}; default (0.1, 4 ha, seeded) {row[(0.10, 4, True)]:.3f}")
    print(f"\nMean IoU over {len(results)} fires:")
    keys = next(iter(results.values())).keys()
    for k in sorted(keys, key=lambda k: -np.mean([r[k] for r in results.values()]))[:12]:
        vals = [r[k] for r in results.values()]
        print(f"  dNBR >= {k[0]:<5} min {k[1]:>2} ha {'seeded' if k[2] else 'all   '}  mean {np.mean(vals):.3f}  min {np.min(vals):.3f}")


main(sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("/var/folders/*/*/T/fireanalysis_mtbs_*"))[-1])
if _app is not None:
    _app.exitQgis()
