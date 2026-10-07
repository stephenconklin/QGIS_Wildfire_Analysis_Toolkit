"""
Rebuild testdata/ from the full Lab 6 rasters and polygons. Run inside QGIS:

    exec(open('/path/to/QGIS_Wildfire_Analysis_Toolkit/dev/make_testdata.py').read())

Clips everything to the Bright Angel watershed's extent plus a 2 km margin
(EPSG:26912), so the burned area there is a partial zone A and the whole
watershed is zone B. Also writes a 'subareas' layer for testing per-name zones:
the watershed split into west and east halves at its center, a box with no
burned area inside, and a polygon with no name.
"""
import os

from osgeo import gdal
import processing
from qgis.core import (QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry,
                       QgsRectangle, QgsVectorFileWriter, QgsVectorLayer,
                       QgsProject)

LAB = ("/Users/stephenconklin/Library/CloudStorage/OneDrive-FortLewisCollege/"
       "GEOG-400-Raster-GIS/Lab-6/QGISProject/data/")
try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:  # exec() from the console has no __file__
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Wildfire_Analysis_Toolkit"
OUT = os.path.join(REPO, "testdata")
os.makedirs(OUT, exist_ok=True)

watershed = QgsVectorLayer(LAB + "BrightAngelWatershed/BrightAngelWatershed.shp", "ws")
box = watershed.extent().buffered(2000)
crs = QgsCoordinateReferenceSystem("EPSG:26912")

# Rasters: same window, float32, lossless DEFLATE with the floating-point predictor
for src, dst in (("Output/dNBR.tif", "dnbr.tif"),
                 ("Output/NBR_Sentinel_Before_20240913.tif", "prefire_nbr.tif")):
    gdal.Translate(os.path.join(OUT, dst), LAB + src,
                   projWin=[box.xMinimum(), box.yMaximum(), box.xMaximum(), box.yMinimum()],
                   creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=3", "ZLEVEL=9"])

# Vectors: one GeoPackage, three layers
gpkg = os.path.join(OUT, "testdata.gpkg")
if os.path.exists(gpkg):
    os.remove(gpkg)


def write(layer, name):
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = name
    opts.actionOnExistingFile = (QgsVectorFileWriter.CreateOrOverwriteLayer if os.path.exists(gpkg)
                                 else QgsVectorFileWriter.CreateOrOverwriteFile)
    err = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, gpkg, QgsProject.instance().transformContext(), opts)
    if err[0] != QgsVectorFileWriter.NoError:
        raise RuntimeError(f"{name}: {err}")


fixed = processing.run("native:fixgeometries", {
    "INPUT": LAB + "Output/BurnArea_Clip.shp", "OUTPUT": "memory:"})["OUTPUT"]
burn = processing.run("native:extractbyextent", {
    "INPUT": fixed, "EXTENT": box, "CLIP": True, "OUTPUT": "memory:"})["OUTPUT"]
write(burn, "burn_area")
write(watershed, "watershed")

geom = QgsGeometry.unaryUnion([f.geometry() for f in watershed.getFeatures()])
e = geom.boundingBox()
mid_x, mid_y = e.center().x(), e.center().y()
sub = QgsVectorLayer("MultiPolygon?crs=EPSG:26912&field=name:string(50)", "subareas", "memory")
feats = []
for g, name in (
        (geom.intersection(QgsGeometry.fromRect(
            QgsRectangle(e.xMinimum(), e.yMinimum(), mid_x, e.yMaximum()))), "Bright Angel West"),
        (geom.intersection(QgsGeometry.fromRect(
            QgsRectangle(mid_x, e.yMinimum(), e.xMaximum(), e.yMaximum()))), "Bright Angel East"),
        (QgsGeometry.fromRect(QgsRectangle(box.xMinimum(), box.yMinimum(),
                                           box.xMinimum() + 1000, box.yMinimum() + 1000)),
         "No Burn Box"),
        (QgsGeometry.fromRect(QgsRectangle(mid_x - 500, mid_y - 500, mid_x + 500, mid_y + 500)),
         None)):
    f = QgsFeature(sub.fields())
    f.setGeometry(g)
    f["name"] = name
    feats.append(f)
sub.dataProvider().addFeatures(feats)
write(sub, "subareas")

for name in sorted(os.listdir(OUT)):
    print(f"{name}: {os.path.getsize(os.path.join(OUT, name)) / 1e6:.2f} MB")
