"""Draft burned-area perimeter: polygons of burned land from dNBR, as a starting
point for the analyst (MTBS digitizes the perimeter by hand from the imagery)."""
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink, QgsFields,
                       QgsGeometry, QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterLayer)

from .. import VERSION, raster
from ..compat import advanced, double_type, field, polygon_geometry_type, polygon_source, warn
from .build_dnbr import source_union


def _polygon_wkb():
    from qgis.core import Qgis
    return Qgis.WkbType.MultiPolygon


def burned_mask(dnbr, threshold, min_pixels, hole_pixels):
    """Boolean burned mask: dNBR >= threshold, patches under min_pixels removed and
    holes under hole_pixels filled (GDAL sieve, 8-connected)."""
    import numpy as np
    from osgeo import gdal
    mask = (np.nan_to_num(dnbr, nan=-9.0) >= threshold).astype(np.uint8)
    h, w = mask.shape
    mem = gdal.GetDriverByName('MEM').Create('', w, h, 1, gdal.GDT_Byte)
    band = mem.GetRasterBand(1)
    band.WriteArray(mask)
    if min_pixels > 1:  # sieve the burned patches: small ones take the unburned value
        gdal.SieveFilter(band, None, band, min_pixels, 8)
    if hole_pixels > 1:  # sieve the holes: invert, sieve, invert back
        inv = 1 - band.ReadAsArray()
        band.WriteArray(inv)
        gdal.SieveFilter(band, None, band, hole_pixels, 4)
        band.WriteArray(1 - band.ReadAsArray())
    return band.ReadAsArray().astype(bool)


def polygonize(mask, like):
    """Polygons (QgsGeometry list, in like's CRS) of the True pixels."""
    from osgeo import gdal, ogr, osr
    h, w = mask.shape
    mem = gdal.GetDriverByName('MEM').Create('', w, h, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(like.GetGeoTransform())
    mem.SetProjection(like.GetProjection())
    band = mem.GetRasterBand(1)
    band.WriteArray(mask.astype('uint8'))
    srs = osr.SpatialReference()
    srs.ImportFromWkt(like.GetProjection())
    driver = ogr.GetDriverByName('MEM' if int(gdal.VersionInfo()) >= 3110000 else 'Memory')
    vec = driver.CreateDataSource('')
    layer = vec.CreateLayer('burned', srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn('v', ogr.OFTInteger))
    gdal.Polygonize(band, band, layer, 0, [], callback=None)  # the mask band masks out 0
    out = []
    for f in layer:
        g = QgsGeometry()
        g.fromWkb(bytes(f.GetGeometryRef().ExportToWkb()))
        out.append(g)
    return out


class DraftPerimeter(QgsProcessingAlgorithm):
    DNBR = 'DNBR'
    THRESHOLD = 'THRESHOLD'
    MIN_AREA = 'MIN_AREA'
    HOLE_AREA = 'HOLE_AREA'
    SEED = 'SEED'
    CLIP = 'CLIP'
    OFFSET = 'OFFSET'
    KEEP_SHARE = 'KEEP_SHARE'
    OUTPUT = 'OUTPUT'

    def name(self):
        return 'draftperimeter'

    def displayName(self):
        return 'Draft burned-area perimeter'

    def group(self):
        return 'Severity'

    def groupId(self):
        return 'severity'

    def shortHelpString(self):
        return (
            '<p>Drafts burned-area polygons from dNBR: pixels at or above the threshold, small patches '
            'removed and small unburned holes filled, then turned into polygons. Optional seed polygons '
            '(an ignition point buffer, a rough outline, a reported perimeter) keep only the burned '
            'patches that touch them, which drops other fires and noise. With <i>Clip to the seed</i>, '
            'the result is also cut to the seed polygons, for a generous outline drawn around the '
            'whole fire.</p>'
            '<p>The threshold is above the dNBR offset (the median dNBR of unburned land, from the '
            'dNBR metadata that Build dNBR writes): a pair whose unburned land reads 0.1 would '
            'otherwise come out burned everywhere. With <i>Keep patches of at least this share of the '
            'largest</i>, smaller separate patches are dropped, for an outline that also holds other '
            'change (other fires, drought, harvests).</p>'
            '<p>MTBS analysts digitize the perimeter by hand from the imagery; this is a draft to edit, '
            'not a replacement. Check it against the post-fire reflectance, especially at low-severity '
            'edges and in wet or sparse vegetation.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.DNBR, 'dNBR (raw units)'))
        self.addParameter(QgsProcessingParameterNumber(
            self.THRESHOLD, 'Burned at dNBR of at least this above the offset', type=double_type(),
            defaultValue=0.05))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_AREA, 'Smallest burned patch kept (ha)', type=double_type(), minValue=0, defaultValue=20))
        self.addParameter(QgsProcessingParameterNumber(
            self.HOLE_AREA, 'Largest unburned hole filled (ha)', type=double_type(), minValue=0, defaultValue=2))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SEED, 'Seed polygons (keep burned patches touching them)', [polygon_source()], optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CLIP, 'Clip to the seed polygons', defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(
            self.KEEP_SHARE, 'Keep patches of at least this share of the largest (%; 0 = all)', type=double_type(),
            minValue=0, maxValue=100, defaultValue=0))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.OFFSET, 'dNBR offset (empty = from the dNBR metadata, else 0)', type=double_type(), optional=True)))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, 'Draft perimeter', polygon_source()))

    def processAlgorithm(self, parameters, context, feedback):
        dnbr_layer = self.parameterAsRasterLayer(parameters, self.DNBR, context)
        ds = raster.open_raster(dnbr_layer)
        dnbr, _ = raster.read_on_grid(ds, ds)
        px_ha = abs(ds.GetGeoTransform()[1] * ds.GetGeoTransform()[5]) / 10000.0
        if dnbr_layer.crs().isGeographic():
            raise QgsProcessingException('The dNBR must be in a projected CRS with meter units.')
        above = self.parameterAsDouble(parameters, self.THRESHOLD, context)
        md = ds.GetMetadata() or {}
        if parameters.get(self.OFFSET) not in (None, ''):
            offset = self.parameterAsDouble(parameters, self.OFFSET, context)
        elif md.get('DNBR_OFFSET_SUBTRACTED') == 'YES':
            offset = 0.0  # already taken out of this dNBR
        else:
            offset = float(md.get('DNBR_OFFSET', 0.0))
        t = offset + above
        feedback.pushInfo(f'Burned: dNBR >= {t:.3f} (offset {offset:+.3f} + {above:g}).')
        min_px = int(round(self.parameterAsDouble(parameters, self.MIN_AREA, context) / px_ha))
        hole_px = int(round(self.parameterAsDouble(parameters, self.HOLE_AREA, context) / px_ha))
        mask = burned_mask(dnbr, t, min_px, hole_px)
        feedback.setProgress(40)
        polygons = polygonize(mask, ds)
        rcrs = QgsCoordinateReferenceSystem.fromWkt(ds.GetProjection())
        if parameters.get(self.SEED):
            seed, scrs = source_union(self.parameterAsSource(parameters, self.SEED, context), 'seed')
            seed = QgsGeometry(seed)
            if scrs != rcrs:
                seed.transform(QgsCoordinateTransform(scrs, rcrs, context.transformContext()))
            kept = [g for g in polygons if g.intersects(seed)]
            feedback.pushInfo(f'{len(kept)} of {len(polygons)} burned patches touch the seed.')
            polygons = kept
            if self.parameterAsBoolean(parameters, self.CLIP, context):  # sizes below count inside the seed
                polygons = [p for p in (g.intersection(seed) for g in polygons) if not p.isEmpty() and p.area() > 0]
        share = self.parameterAsDouble(parameters, self.KEEP_SHARE, context)
        if share > 0 and polygons:
            biggest = max(g.area() for g in polygons)
            kept = [g for g in polygons if g.area() >= share / 100.0 * biggest]
            feedback.pushInfo(f'{len(kept)} of {len(polygons)} patches are at least {share:g} % of the largest.')
            polygons = kept
        if not polygons:
            warn(feedback, 'No burned patch found; lower the threshold or check the seed.')
        merged = QgsGeometry.unaryUnion(polygons) if polygons else QgsGeometry()
        if parameters.get(self.SEED) and self.parameterAsBoolean(parameters, self.CLIP, context) and polygons:
            merged = merged.intersection(seed)  # may hold edge slivers as lines or points
            if merged.isMultipart() and not merged.isEmpty():
                merged.convertGeometryCollectionToSubclass(polygon_geometry_type())  # in place
            feedback.pushInfo('Clipped to the seed polygons.')
        fields = QgsFields()
        fields.append(field('area_ha', 'double'))
        fields.append(field('threshold', 'double'))
        sink, dest_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, _polygon_wkb(), rcrs)
        if not merged.isEmpty():
            feat = QgsFeature(fields)
            merged.convertToMultiType()
            feat.setGeometry(merged)
            feat.setAttributes([round(merged.area() / 10000.0, 2), t])
            sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)
            feedback.pushInfo(f'Burned area: {merged.area() / 10000.0:,.0f} ha ({merged.area() / 4046.86:,.0f} ac) '
                              f'at dNBR >= {t}.')
        return {self.OUTPUT: dest_id}
