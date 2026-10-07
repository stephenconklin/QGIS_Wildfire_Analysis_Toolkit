"""Find and fetch Landsat Collection 2 Level-2 and Sentinel-2 L2A scenes from the
Microsoft Planetary Computer (no account needed)."""
import json

from qgis.core import (QgsCoordinateReferenceSystem, QgsDistanceArea, QgsFeature, QgsFeatureSink, QgsFields,
                       QgsJsonUtils, QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingParameterBoolean, QgsProcessingParameterCrs, QgsProcessingParameterDateTime,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsProcessingParameterString)

from .. import VERSION, planetary, stac
from ..compat import date_type, double_type, field, integer_type, polygon_source, polygon_wkb, warn
from .find_hls_scenes import WGS84, aoi_geometry
from .prepare_local_nbr import MASK_CLASSES, write_scene_nbr

SOURCE_KEYS = ['landsat', 's2']
SOURCE_LABELS = [planetary.SOURCES[k][1] for k in SOURCE_KEYS]
CLEAR_FLAGS = {'cloud', 'adjacent', 'shadow', 'snow'}


def scene_clear_share(scene, aoi, context, feedback):
    """(clear %, data %) of the AOI for one date, from QA_PIXEL or SCL on a 30 m grid."""
    from .fetch_hls_nbr import aoi_grid
    return scene_clear_share_on(scene, aoi_grid(aoi, context.transformContext()), feedback)


def scene_clear_share_on(scene, grid, feedback):
    """scene_clear_share on a grid from fetch_hls_nbr.aoi_grid; safe in worker threads
    (GDAL reads only; the scene's URLs are already signed)."""
    from osgeo import gdal
    if not scene.qa:
        return None, None
    inside = grid['inside']
    ds = gdal.Warp('', scene.qa, format='MEM', outputBounds=grid['bounds'], width=inside.shape[1],
                   height=inside.shape[0], dstSRS=grid['wkt'], resampleAlg='near', srcNodata=None, dstNodata=None,
                   outputType=gdal.GDT_UInt16, callback=lambda *_: 0 if feedback.isCanceled() else 1)
    if ds is None:
        return None, None
    qa = ds.GetRasterBand(1).ReadAsArray()
    flagged, fill = scene.qa_mask(qa, CLEAR_FLAGS)
    if scene.kind == 'landsat':
        fill |= qa == 0  # outside the scene
    n = max(int(inside.sum()), 1)
    data = inside & ~fill
    return 100.0 * int((data & ~flagged).sum()) / n, 100.0 * int(data.sum()) / n


class FindPlanetaryScenes(QgsProcessingAlgorithm):
    AOI = 'AOI'
    START = 'START'
    END = 'END'
    SOURCE = 'SOURCE'
    MAX_CLOUD = 'MAX_CLOUD'
    AOI_CLEAR = 'AOI_CLEAR'
    OUTPUT = 'OUTPUT'

    def name(self):
        return 'findplanetaryscenes'

    def displayName(self):
        return 'Find Landsat / Sentinel-2 scenes (Planetary Computer)'

    def group(self):
        return 'Imagery'

    def groupId(self):
        return 'imagery'

    def shortHelpString(self):
        return (
            '<p>Searches Microsoft Planetary Computer for Landsat Collection 2 Level-2 (Landsat 4–9, '
            'from 1982) or Sentinel-2 L2A scenes over the area of interest. No account is needed. '
            'Use it for fires before HLS (2013), or for 20 m Sentinel-2.</p>'
            '<p><b>Fields:</b> item id, collection, platform, path/row or tile, date, day of year, '
            'cloud (scene %), aoi_pct, and with <i>Measure the clear share</i> aoi_clear / aoi_data '
            '(from QA_PIXEL or SCL, over the AOI only).</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(self.AOI, 'Area of interest', [polygon_source()]))
        self.addParameter(QgsProcessingParameterDateTime(self.START, 'Start date', type=date_type()))
        self.addParameter(QgsProcessingParameterDateTime(self.END, 'End date', type=date_type()))
        self.addParameter(QgsProcessingParameterEnum(self.SOURCE, 'Imagery', options=SOURCE_LABELS, defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_CLOUD, 'Maximum scene cloud cover (%)', type=integer_type(), minValue=0, maxValue=100,
            defaultValue=100))
        self.addParameter(QgsProcessingParameterBoolean(
            self.AOI_CLEAR, 'Measure the clear share inside the AOI (reads the quality band)', defaultValue=False))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, 'Scenes', polygon_source()))

    def processAlgorithm(self, parameters, context, feedback):
        aoi = aoi_geometry(self.parameterAsSource(parameters, self.AOI, context), context)
        start = self.parameterAsDateTime(parameters, self.START, context).date().toPyDate()
        end = self.parameterAsDateTime(parameters, self.END, context).date().toPyDate()
        if end < start:
            raise QgsProcessingException('The end date is before the start date.')
        source = SOURCE_KEYS[self.parameterAsEnum(parameters, self.SOURCE, context)]
        max_cloud = self.parameterAsInt(parameters, self.MAX_CLOUD, context)
        box = aoi.boundingBox()
        feedback.pushInfo(f'Searching {planetary.SOURCES[source][1]} from {start} to {end}...')
        try:
            items = planetary.search(source, [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()],
                                     start, end, feedback)
        except stac.StacError as e:
            raise QgsProcessingException(str(e)) from None
        area = QgsDistanceArea()
        area.setSourceCrs(QgsCoordinateReferenceSystem(WGS84), context.transformContext())
        area.setEllipsoid('EPSG:7030')
        aoi_area = area.measureArea(aoi)
        rows = []
        for item in items:
            p = item['properties']
            cloud = p.get('eo:cloud_cover')
            if cloud is not None and cloud > max_cloud:
                continue
            geom = QgsJsonUtils.geometryFromGeoJson(json.dumps(item['geometry']))
            if geom.isEmpty() or not geom.intersects(aoi):
                continue
            d = planetary.item_date(item)
            rows.append([d.isoformat(), item['id'], p.get('platform', ''), planetary.item_label(item),
                         d.timetuple().tm_yday, cloud, 100.0 * area.measureArea(geom.intersection(aoi)) / aoi_area,
                         item, geom])
        rows.sort(key=lambda r: (r[0], r[1]))
        clear = {}
        if self.parameterAsBoolean(parameters, self.AOI_CLEAR, context) and rows:
            from .suggest_image_pairs import SHORT, candidate_dates
            try:
                found = candidate_dates(source, [(r[0], r[1], SHORT.get(str(r[2]).lower(), r[2]), r[7]) for r in rows],
                                        aoi, context, feedback, 'Clear share inside the AOI')
            except QgsProcessingException as e:  # optional: keep the scenes without it
                warn(feedback, f'Clear share not measured: {e}')
                found = {}
            for (d, sensor), (c, data, ids) in found.items():
                for iid in ids:
                    clear[iid] = (c, data)
            if feedback.isCanceled():
                return {}

        fields = QgsFields()
        for name, kind, length in (('item', 'string', 80), ('collection', 'string', 20), ('platform', 'string', 20),
                                   ('scene', 'string', 10), ('date', 'string', 10), ('doy', 'int', 0),
                                   ('cloud', 'double', 0), ('aoi_pct', 'double', 0),
                                   ('aoi_clear', 'double', 0), ('aoi_data', 'double', 0)):
            fields.append(field(name, kind, length))
        sink, dest_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, polygon_wkb(),
                                             QgsCoordinateReferenceSystem(WGS84))
        for date, iid, platform, label, doy, cloud, pct, item, geom in rows:
            c, d = clear.get(iid, (None, None))
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            feat.setAttributes([iid, planetary.SOURCES[source][0], platform, label, date, doy,
                                None if cloud is None else round(cloud, 1), round(pct, 2),
                                None if c is None else round(c, 1), None if d is None else round(d, 1)])
            sink.addFeature(feat, QgsFeatureSink.FastInsert)
        feedback.pushInfo(f'{len(rows)} scenes on {len({r[0] for r in rows})} dates.')
        return {self.OUTPUT: dest_id}


class FetchPlanetaryNbr(QgsProcessingAlgorithm):
    AOI = 'AOI'
    SCENES = 'SCENES'
    ITEMS = 'ITEMS'
    SOURCE = 'SOURCE'
    BUFFER = 'BUFFER'
    CRS = 'CRS'
    PIXEL = 'PIXEL'
    MASK = 'MASK'
    NBR = 'NBR'
    REFLECTANCE = 'REFLECTANCE'

    def name(self):
        return 'fetchplanetarynbr'

    def displayName(self):
        return 'Fetch Landsat / Sentinel-2 NBR (Planetary Computer)'

    def group(self):
        return 'Imagery'

    def groupId(self):
        return 'imagery'

    def shortHelpString(self):
        return (
            '<p>Reads the AOI window (plus a buffer) of Landsat Collection 2 Level-2 or Sentinel-2 L2A '
            'scenes from Microsoft Planetary Computer, calibrates them to surface reflectance, masks '
            'clouds from QA_PIXEL or SCL and writes NBR on the same grid as <i>Fetch HLS NBR</i> and '
            '<i>Prepare local scene NBR</i>. No account needed.</p>'
            '<p><b>Scenes:</b> the output of <i>Find Landsat / Sentinel-2 scenes</i> (or a selection), '
            'or item ids; all of one date and one satellite family.</p>'
            '<p>Landsat: DN × 0.0000275 − 0.2 (from the item). Sentinel-2: (DN − 1000) / 10000 for '
            'processing baseline 04.00 and later, DN / 10000 before.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(self.AOI, 'Area of interest', [polygon_source()]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SCENES, 'Scenes (from Find Landsat / Sentinel-2 scenes; one date)', [polygon_source()],
            optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.ITEMS, 'Or: item ids, separated by spaces', optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE, 'Imagery (for item ids)', options=SOURCE_LABELS, defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.BUFFER, 'Buffer around the AOI (m)', type=double_type(), minValue=0, defaultValue=3000))
        self.addParameter(QgsProcessingParameterCrs(self.CRS, 'Target CRS (empty = UTM zone of the AOI)',
                                                    optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.PIXEL, 'Pixel size (m)', type=double_type(), minValue=10, defaultValue=30))
        self.addParameter(QgsProcessingParameterEnum(
            self.MASK, 'Classes to mask', options=[c[1] for c in MASK_CLASSES], allowMultiple=True,
            defaultValue=[i for i, c in enumerate(MASK_CLASSES) if c[2]]))
        self.addParameter(QgsProcessingParameterRasterDestination(self.NBR, 'NBR'))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.REFLECTANCE, 'Reflectance SWIR2 / NIR / red (for display)', optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):
        text = (self.parameterAsString(parameters, self.ITEMS, context) or '').replace(',', ' ').split()
        source = SOURCE_KEYS[self.parameterAsEnum(parameters, self.SOURCE, context)]
        if not text:
            if not parameters.get(self.SCENES):
                raise QgsProcessingException('Give the scenes layer or the item ids.')
            layer = self.parameterAsSource(parameters, self.SCENES, context)
            if layer is None or layer.fields().indexOf('item') < 0:
                raise QgsProcessingException('The scenes layer has no "item" field; use the output of '
                                             'Find Landsat / Sentinel-2 scenes.')
            text = sorted({str(f['item']) for f in layer.getFeatures() if f['item']})
            collections = {str(f['collection']) for f in layer.getFeatures()}
            for key in SOURCE_KEYS:
                if collections == {planetary.SOURCES[key][0]}:
                    source = key
        if not text:
            raise QgsProcessingException('No scenes given.')
        try:
            items = planetary.search(source, None, None, None, feedback, ids=text)
            if feedback.isCanceled():
                return {}
            missing = set(text) - {i['id'] for i in items}
            if missing:
                raise QgsProcessingException(f'Not found in {planetary.SOURCES[source][0]}: {", ".join(sorted(missing))}')
            scene = planetary.items_to_scene(items, source, feedback)
        except (planetary.PlanetaryError, stac.StacError) as e:
            raise QgsProcessingException(str(e)) from None
        feedback.pushInfo(f'{scene.sensor} {scene.date.isoformat()}, {len(items)} scene(s): {scene.label}')
        aoi = aoi_geometry(self.parameterAsSource(parameters, self.AOI, context), context)
        mask_keys = {MASK_CLASSES[i][0] for i in self.parameterAsEnums(parameters, self.MASK, context)}
        refl_out = (self.parameterAsOutputLayer(parameters, self.REFLECTANCE, context)
                    if parameters.get(self.REFLECTANCE) not in (None, '') else None)
        out = write_scene_nbr(scene, aoi, self.parameterAsCrs(parameters, self.CRS, context),
                              self.parameterAsDouble(parameters, self.BUFFER, context),
                              self.parameterAsDouble(parameters, self.PIXEL, context), mask_keys,
                              self.parameterAsOutputLayer(parameters, self.NBR, context), refl_out, context, feedback,
                              {'SOURCE_ITEMS': ' '.join(i['id'] for i in items),
                               'SOURCE_PROVIDER': 'Microsoft Planetary Computer'})
        if out is None:
            return {}
        results = {self.NBR: out['nbr']}
        if 'reflectance' in out:
            results[self.REFLECTANCE] = out['reflectance']
        return results
