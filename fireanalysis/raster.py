"""GDAL helpers shared by the algorithms: read on a reference grid, rasterize
QGIS geometries, write outputs."""
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
                       QgsProcessingException, QgsRasterFileWriter)

NODATA = -9999.0


def open_raster(layer_or_path):
    from osgeo import gdal
    path = layer_or_path if isinstance(layer_or_path, str) else layer_or_path.source()
    ds = gdal.Open(path)
    if ds is None:
        raise QgsProcessingException(f'Could not open {path} with GDAL.')
    return ds


def read_on_grid(ds, like, resample='bilinear'):
    """Band 1 of ds as float32 on like's grid, with nodata and non-finite as NaN.
    No resampling when the grids already match."""
    import numpy as np
    from osgeo import gdal
    same = (ds.GetGeoTransform() == like.GetGeoTransform()
            and (ds.RasterXSize, ds.RasterYSize) == (like.RasterXSize, like.RasterYSize)
            and ds.GetSpatialRef().IsSame(like.GetSpatialRef()))
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if same:
        a = band.ReadAsArray().astype(np.float32)
        if nodata is not None:
            a[a == np.float32(nodata)] = np.nan
    else:
        gt = like.GetGeoTransform()
        bounds = (gt[0], gt[3] + gt[5] * like.RasterYSize, gt[0] + gt[1] * like.RasterXSize, gt[3])
        out = gdal.Warp('', ds, format='MEM', outputBounds=bounds, width=like.RasterXSize,
                        height=like.RasterYSize, dstSRS=like.GetProjection(), resampleAlg=resample,
                        srcNodata=nodata, dstNodata=float('nan'), outputType=gdal.GDT_Float32)
        a = out.GetRasterBand(1).ReadAsArray()
    a[~np.isfinite(a)] = np.nan
    return a, same


def rasterize(geometry, geometry_crs, like, transform_context):
    """Boolean array on like's grid: True where the pixel centre is inside geometry."""
    import numpy as np
    from osgeo import gdal, ogr, osr
    if geometry is None or geometry.isEmpty():
        return np.zeros((like.RasterYSize, like.RasterXSize), bool)
    crs = QgsCoordinateReferenceSystem.fromWkt(like.GetProjection())
    g = QgsGeometry(geometry)
    if geometry_crs != crs:
        g.transform(QgsCoordinateTransform(geometry_crs, crs, transform_context))
    srs = osr.SpatialReference()
    srs.ImportFromWkt(like.GetProjection())
    # vector MEM since GDAL 3.11; 'Memory' before (deprecated after)
    src = (ogr.GetDriverByName('MEM') if int(gdal.VersionInfo()) >= 3110000
           else ogr.GetDriverByName('Memory')).CreateDataSource('')
    layer = src.CreateLayer('g', srs, ogr.wkbUnknown)
    feat = ogr.Feature(layer.GetLayerDefn())
    feat.SetGeometry(ogr.CreateGeometryFromWkb(bytes(g.asWkb())))
    layer.CreateFeature(feat)
    mem = gdal.GetDriverByName('MEM').Create('', like.RasterXSize, like.RasterYSize, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(like.GetGeoTransform())
    mem.SetProjection(like.GetProjection())
    gdal.RasterizeLayer(mem, [1], layer, burn_values=[1])
    return mem.GetRasterBand(1).ReadAsArray().astype(bool)


def write(path, arrays, like, gdal_type, nodata, metadata):
    """Write [(band name, array)] on like's grid. NaN in float arrays becomes nodata."""
    import numpy as np
    from osgeo import gdal
    ext = os.path.splitext(path)[1].lstrip('.').lower()
    driver_name = QgsRasterFileWriter.driverForExtension(ext) if ext else 'GTiff'
    driver = gdal.GetDriverByName(driver_name or 'GTiff')
    options = ['COMPRESS=DEFLATE', 'TILED=YES'] if driver.ShortName == 'GTiff' else []
    out = driver.Create(path, like.RasterXSize, like.RasterYSize, len(arrays), gdal_type,
                        options=options)
    if out is None:
        raise QgsProcessingException(f'Could not create {path}.')
    out.SetGeoTransform(like.GetGeoTransform())
    out.SetProjection(like.GetProjection())
    out.SetMetadata({k: str(v) for k, v in metadata.items()})
    for i, (name, array) in enumerate(arrays, start=1):
        if array.dtype.kind == 'f':
            array = np.where(np.isfinite(array), array, nodata)
        band = out.GetRasterBand(i)
        band.SetDescription(name)
        band.SetNoDataValue(nodata)
        band.WriteArray(array)
    out = None  # close and flush
