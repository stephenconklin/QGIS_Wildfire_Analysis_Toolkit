"""Find MTBS fires: published MTBS fires by name, id, years or area, with their whole
MTBS record (dates, scenes, offset, thresholds), as perimeters or points. The
records come from the USGS file the plugin downloads and caches itself."""
import json

from qgis.core import (Qgis, QgsCoordinateReferenceSystem, QgsFeature, QgsFeatureSink,
                       QgsFields, QgsGeometry, QgsJsonUtils, QgsPointXY, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterNumber, QgsProcessingParameterString)

from .. import VERSION, mtbs
from ..compat import advanced, field, integer_type, polygon_source, warn

GEOMETRIES = ['Burned area perimeters', 'Points (burned-area centres; fast, no perimeter download)']
PERIMETERS, POINTS = 0, 1
# Output fields: MTBS's published names, so the pipeline recognizes the layer, plus year and source
TEXT = ('event_id', 'irwinid', 'incid_name', 'incid_type', 'map_prog', 'asmnt_type', 'ig_date', 'pre_id',
        'post_id', 'perim_id', 'comment')
NUMBERS = ('burnbndac', 'burnbndlat', 'burnbndlon', 'dnbr_offst', 'dnbr_stddv', 'nodata_t', 'incgreen_t',
           'low_t', 'mod_t', 'high_t')
WARN_PERIMETERS = 200


def output_fields():
    fields = QgsFields()
    for name in ('event_id', 'irwinid', 'incid_name', 'incid_type', 'map_prog', 'asmnt_type', 'ig_date'):
        fields.append(field(name, 'string', 80))
    fields.append(field('year', 'int'))
    for name in NUMBERS:
        fields.append(field(name, 'double'))
    for name in ('pre_id', 'post_id', 'perim_id', 'comment'):
        fields.append(field(name, 'string', 254 if name == 'comment' else 80))
    fields.append(field('perimeter', 'string', 80))
    return fields


class FindMtbsFires(QgsProcessingAlgorithm):
    SEARCH = 'SEARCH'
    START_YEAR = 'START_YEAR'
    END_YEAR = 'END_YEAR'
    AOI = 'AOI'
    WILDFIRES_ONLY = 'WILDFIRES_ONLY'
    GEOMETRY = 'GEOMETRY'
    DOWNLOAD = 'DOWNLOAD'
    MAX_FIRES = 'MAX_FIRES'
    OUTPUT = 'OUTPUT'

    def name(self):
        return 'findmtbsfires'

    def displayName(self):
        return 'Find MTBS fires'

    def group(self):
        return 'Fire records'

    def groupId(self):
        return 'firerecords'

    def shortHelpString(self):
        return (
            '<p>Published MTBS (Monitoring Trends in Burn Severity) fires, with their whole MTBS record: '
            'name, ignition date, assessment type, MTBS\'s pre- and post-fire scenes, dNBR offset and '
            'thresholds (x1000), and the IRWIN id that links them to WFIGS perimeters.</p>'
            '<p>Filter by <b>name or id</b> (whole words, any case; add the year, as in <i>Dragon Bravo '
            '2025</i>, or give the MTBS fire id), by <b>years</b> of ignition and by <b>area</b> (fires whose '
            'burned area is centred inside the polygons). Give at least one filter for perimeters.</p>'
            '<p><b>Where the data comes from.</b> The fire records are the USGS MTBS file (about 3.5 MB), '
            'downloaded the first time and cached in your QGIS profile; the plugin checks for a newer copy '
            'at most once a day and works offline from the cache. Perimeters come from the MTBS map service; '
            'for fires it does not have yet (most fires of the last year or so), from the full MTBS perimeter '
            'file, a one-time 420 MB download that happens only when you allow it.</p>'
            '<p>Give one fire from the output (pick it, then <i>Selected features only</i>) to <i>Burn '
            'severity (whole pipeline)</i>: it reads the name, dates, MTBS\'s image pair and thresholds from '
            'the record. Or type the fire\'s id in the pipeline\'s <i>MTBS fire</i> field.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterString(
            self.SEARCH, 'Fire name or MTBS fire id (optionally with the year)', optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.START_YEAR, 'Ignition year from', type=integer_type(), minValue=1984, maxValue=2100,
            optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.END_YEAR, 'Ignition year to', type=integer_type(), minValue=1984, maxValue=2100, optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.AOI, 'Area (fires centred inside these polygons)', [polygon_source()], optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.WILDFIRES_ONLY, 'Wildfires only (no prescribed fires)', defaultValue=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.GEOMETRY, 'Output', options=GEOMETRIES, defaultValue=PERIMETERS))
        self.addParameter(QgsProcessingParameterBoolean(
            self.DOWNLOAD, 'Allow the one-time 420 MB MTBS perimeter download for fires the map service lacks',
            defaultValue=False))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.MAX_FIRES, 'Most fires to return', type=integer_type(), minValue=1, defaultValue=1000)))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, 'MTBS fires'))

    def processAlgorithm(self, parameters, context, feedback):
        text = (self.parameterAsString(parameters, self.SEARCH, context) or '').strip() or None
        first = self.parameterAsInt(parameters, self.START_YEAR, context) \
            if parameters.get(self.START_YEAR) not in (None, '') else None
        last = self.parameterAsInt(parameters, self.END_YEAR, context) \
            if parameters.get(self.END_YEAR) not in (None, '') else None
        geometry = self.parameterAsEnum(parameters, self.GEOMETRY, context)
        aoi = None
        if parameters.get(self.AOI) not in (None, ''):
            source = self.parameterAsSource(parameters, self.AOI, context)
            from .find_hls_scenes import aoi_geometry
            aoi = aoi_geometry(source, context)
        if geometry == PERIMETERS and not (text or first or last or aoi):
            raise QgsProcessingException('Give a name or id, years or an area to find perimeters for (or ask for '
                                         'points, which can list every fire).')
        try:
            recs = mtbs.records(feedback)
        except mtbs.MtbsError as e:
            raise QgsProcessingException(str(e)) from None
        info = mtbs.cache_info('records')
        feedback.pushInfo(f'MTBS fire records: {len(recs):,} fires (USGS file of {info.get("last_modified", "?")}).')
        box = None
        if aoi is not None:
            b = aoi.boundingBox()
            box = (b.xMinimum(), b.yMinimum(), b.xMaximum(), b.yMaximum())
        found = mtbs.search(recs, text, first, last, box,
                            self.parameterAsBool(parameters, self.WILDFIRES_ONLY, context))
        if aoi is not None:
            found = [r for r in found if aoi.contains(QgsGeometry.fromPointXY(QgsPointXY(r['lon'], r['lat'])))]
        limit = self.parameterAsInt(parameters, self.MAX_FIRES, context)
        if len(found) > limit:
            warn(feedback, f'{len(found):,} fires match; returning the first {limit:,} (exact names first, then '
                           'the largest). Narrow the search or raise "Most fires to return".')
            found = found[:limit]
        feedback.pushInfo(f'{len(found):,} MTBS fire(s) match.')
        for r in found[:20]:
            feedback.pushInfo(f'  {mtbs.describe(r)}')

        crs = QgsCoordinateReferenceSystem('EPSG:4326')
        wkb = Qgis.WkbType.MultiPolygon if geometry == PERIMETERS else Qgis.WkbType.Point
        sink, dest = self.parameterAsSink(parameters, self.OUTPUT, context, output_fields(), wkb, crs)
        if sink is None:
            raise QgsProcessingException('Could not create the output layer.')
        shapes = {}
        if geometry == PERIMETERS and found:
            if len(found) > WARN_PERIMETERS:
                warn(feedback, f'Fetching {len(found):,} perimeters may take a while.')
            shapes, missing, note = mtbs.perimeters(found, feedback,
                                                    self.parameterAsBool(parameters, self.DOWNLOAD, context))
            if missing:
                warn(feedback, f'{len(missing)} fire(s) without a perimeter, {note}: '
                               + ', '.join(missing[:10]) + (', ...' if len(missing) > 10 else ''))
        written = 0
        for r in found:
            f = QgsFeature(output_fields())
            for name in TEXT:
                f[name] = None if r.get(name) in (None, '') else str(r[name])
            for name in NUMBERS:
                f[name] = r.get(name)
            f['year'] = r.get('year')
            if geometry == PERIMETERS:
                if r['event_id'] not in shapes:
                    continue
                geoms, src = shapes[r['event_id']]
                parts = [QgsJsonUtils.geometryFromGeoJson(json.dumps(g)) for g in geoms]
                g = QgsGeometry.unaryUnion(parts) if len(parts) > 1 else parts[0]
                g.convertToMultiType()
                f.setGeometry(g)
                f['perimeter'] = src
            else:
                if r.get('lon') is None:
                    continue
                f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(r['lon'], r['lat'])))
                f['perimeter'] = 'point: burned-area centre'
            sink.addFeature(f, QgsFeatureSink.Flag.FastInsert)
            written += 1
        feedback.pushInfo(f'{written:,} fire(s) written.')
        return {self.OUTPUT: dest}
