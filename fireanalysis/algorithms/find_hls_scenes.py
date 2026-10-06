"""Find HLS scenes: STAC search of NASA HLS v2.0 granules over an AOI."""
import json

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDistanceArea,
                       QgsFeature, QgsFeatureSink, QgsFields, QgsGeometry, QgsJsonUtils,
                       QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingParameterDateTime, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterNumber)
from qgis.PyQt.QtCore import QDate

from .. import VERSION, hls, stac
from ..compat import date_type, field, integer_type, polygon_source, polygon_wkb, warn

WGS84 = 'EPSG:4326'


def aoi_geometry(source, context):
    """Union of the AOI features, in EPSG:4326."""
    if source is None:
        raise QgsProcessingException('The area of interest layer could not be loaded.')
    xform = QgsCoordinateTransform(source.sourceCrs(), QgsCoordinateReferenceSystem(WGS84),
                                   context.transformContext())
    parts = []
    for f in source.getFeatures():
        if f.hasGeometry():
            g = QgsGeometry(f.geometry())
            g.transform(xform)
            parts.append(g.makeValid())
    if not parts:
        raise QgsProcessingException('The area of interest has no polygons.')
    return QgsGeometry.unaryUnion(parts)


class FindHlsScenes(QgsProcessingAlgorithm):
    AOI = 'AOI'
    START = 'START'
    END = 'END'
    SENSORS = 'SENSORS'
    MAX_CLOUD = 'MAX_CLOUD'
    OUTPUT = 'OUTPUT'

    def name(self):
        return 'findhlsscenes'

    def displayName(self):
        return 'Find HLS scenes'

    def group(self):
        return 'Imagery'

    def groupId(self):
        return 'imagery'

    def shortHelpString(self):
        return (
            '<p>Searches NASA\'s Harmonized Landsat Sentinel-2 (HLS v2.0) archive for '
            'granules that overlap the area of interest, and returns one footprint '
            'per granule. No login is needed to search.</p>'
            '<p><b>Fields:</b> granule id, sensor (L30 = Landsat 8/9, S30 = Sentinel-2), '
            'MGRS tile, date, day of year, cloud (scene cloud cover, %), aoi_pct (share '
            'of the AOI inside the footprint). A large AOI covers several tiles per '
            'date; select all of one date\'s tiles for <i>Fetch HLS NBR</i>.</p>'
            '<p>HLS starts in April 2013 (L30) and late 2015 (S30).</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.AOI, 'Area of interest', [polygon_source()]))
        self.addParameter(QgsProcessingParameterDateTime(
            self.START, 'Start date', type=date_type()))
        self.addParameter(QgsProcessingParameterDateTime(
            self.END, 'End date', type=date_type()))
        self.addParameter(QgsProcessingParameterEnum(
            self.SENSORS, 'Sensors', options=[hls.PRODUCTS[s]['label'] for s in hls.SENSORS],
            allowMultiple=True, defaultValue=list(range(len(hls.SENSORS)))))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_CLOUD, 'Maximum scene cloud cover (%)',
            type=integer_type(),
            minValue=0, maxValue=100, defaultValue=100))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, 'HLS scenes', polygon_source()))

    def processAlgorithm(self, parameters, context, feedback):
        aoi = aoi_geometry(self.parameterAsSource(parameters, self.AOI, context), context)
        start = self.parameterAsDateTime(parameters, self.START, context).date()
        end = self.parameterAsDateTime(parameters, self.END, context).date()
        if not start.isValid() or not end.isValid():
            raise QgsProcessingException('Set a start and an end date.')
        if end < start:
            raise QgsProcessingException('The end date is before the start date.')
        if start < QDate(2013, 4, 1):
            warn(feedback, 'HLS starts in April 2013; earlier dates return nothing.')
        sensors = [hls.SENSORS[i] for i in self.parameterAsEnums(parameters, self.SENSORS, context)]
        if not sensors:
            raise QgsProcessingException('Choose at least one sensor.')
        max_cloud = self.parameterAsInt(parameters, self.MAX_CLOUD, context)

        fields = QgsFields()
        for name, kind, length in (('granule', 'string', 40), ('sensor', 'string', 3),
                                   ('tile', 'string', 5), ('date', 'string', 10),
                                   ('doy', 'int', 0), ('cloud', 'int', 0),
                                   ('aoi_pct', 'double', 0), ('datetime', 'string', 30)):
            fields.append(field(name, kind, length))
        sink, dest_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields,
                                             polygon_wkb(), QgsCoordinateReferenceSystem(WGS84))

        area = QgsDistanceArea()
        area.setSourceCrs(QgsCoordinateReferenceSystem(WGS84), context.transformContext())
        area.setEllipsoid('EPSG:7030')
        aoi_area = area.measureArea(aoi)

        box = aoi.boundingBox()
        when = f'{start.toString("yyyy-MM-dd")}T00:00:00Z/{end.toString("yyyy-MM-dd")}T23:59:59Z'
        feedback.pushInfo(f'Searching {", ".join(sensors)} from {when.replace("/", " to ")}...')
        rows = []
        try:
            for item in stac.search(hls.STAC_SEARCH, [hls.PRODUCTS[s]['collection'] for s in sensors],
                                    [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()],
                                    when, feedback=feedback):
                if feedback.isCanceled():
                    return {}
                try:
                    sensor, tile, date = hls.parse_granule(item['id'])
                except ValueError:
                    continue
                cloud = item['properties'].get('eo:cloud_cover')
                if cloud is not None and cloud > max_cloud:
                    continue
                geom = QgsJsonUtils.geometryFromGeoJson(json.dumps(item['geometry']))
                if geom.isEmpty() or not geom.intersects(aoi):
                    continue  # the bbox search also returns tiles beside a non-rectangular AOI
                inside = area.measureArea(geom.intersection(aoi))
                rows.append((date.isoformat(), item['id'], sensor, tile, date.timetuple().tm_yday,
                             cloud, 100.0 * inside / aoi_area if aoi_area else 0.0,
                             item['properties'].get('datetime', ''), geom))
        except stac.StacError as e:
            raise QgsProcessingException(str(e)) from None

        rows.sort()
        for date, granule, sensor, tile, doy, cloud, pct, dt, geom in rows:
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            feat.setAttributes([granule, sensor, tile, date, doy,
                                None if cloud is None else int(round(cloud)), round(pct, 2), dt])
            sink.addFeature(feat, QgsFeatureSink.FastInsert)
        dates = sorted({r[0] for r in rows})
        feedback.pushInfo(f'{len(rows)} granules on {len(dates)} dates.')
        if not rows:
            warn(feedback, 'No HLS granules found. Widen the dates or raise the cloud limit.')
        return {self.OUTPUT: dest_id}
