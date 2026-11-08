"""Burn severity (whole pipeline): one algorithm from fire to report. Image pair ->
pre/post NBR -> dNBR, RdNBR and offset -> MTBS classes -> area summary ->
report, all written to one folder."""
import csv
import datetime
import html
import importlib.util
import json
import os

from qgis.core import (QgsDataSourceUri, QgsDistanceArea, QgsProcessing, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingFeatureSourceDefinition,
                       QgsProcessingMultiStepFeedback, QgsProcessingOutputFile, QgsProcessingOutputFolder,
                       QgsProcessingOutputRasterLayer, QgsProcessingOutputVectorLayer, QgsProcessingParameterBoolean,
                       QgsProcessingParameterDateTime, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProcessingUtils, QgsVectorLayer)

from qgis.PyQt.QtCore import QDate

from .. import VERSION
from ..incident import fire_details, read_incident, read_mtbs
from .. import mtbs, planetary, stac, styles
from ..compat import advanced, date_type, double_type, integer_type, polygon_source
from .build_dnbr import BuildDnbr, source_union
from .draft_perimeter import DraftPerimeter
from .classify_severity import (CLASS_COLORS, ESTIMATES, EXTENDED, FIRE_MTBS, NEARBY, NONE_VALUE, OWN_DATA,
                                ClassifySeverity, write_png, write_style)
from .find_hls_scenes import aoi_geometry, search_granules
from .fetch_hls_nbr import FetchHlsNbr
from .planetary_algorithms import FetchPlanetaryNbr
from .suggest_image_pairs import ASSESSMENTS, INITIAL, SOURCES, SuggestImagePairs

CLASSES = ['Unburned to low', 'Low', 'Moderate', 'High']
# PERIMETER_MODE options; order is API (append only)
PERIMETER_MODES = ['As given', 'Drafted from dNBR inside the given polygons (a generous outline)']
AS_GIVEN, DRAFTED = 0, 1
# LOAD options; order is API (append only)
LOAD_MODES = ['Results, styled, in a group named after the fire and date', 'Severity classes only', 'Nothing']
LOAD_GROUP, LOAD_CLASSES, LOAD_NOTHING = 0, 1, 2
# PAIR options; order is API (append only)
PAIRS = ["MTBS's own image dates when MTBS has mapped the fire, else suggested", 'Always suggested']
PAIR_MTBS, PAIR_SUGGESTED = 0, 1
CLASS_LABELS = ['Unburned to low', 'Low', 'Moderate', 'High', 'Increased greenness']
DRAFT_HOLE_HA = 2.0  # largest unburned hole filled in a drafted burned area


def _summary_algorithm():
    """BurnSeveritySummary from burn_severity_summary.py, which sits next to the
    fireanalysis folder both in the repository and in the plugin."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'burn_severity_summary.py')
    spec = importlib.util.spec_from_file_location('fireanalysis_burn_severity_summary', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.BurnSeveritySummary()


class BurnSeverityPipeline(QgsProcessingAlgorithm):
    SOURCE = 'SOURCE'
    PERIMETER = 'PERIMETER'
    PERIMETER_MODE = 'PERIMETER_MODE'
    MTBS_FIRE = 'MTBS_FIRE'
    PAIR = 'PAIR'
    MTBS_DOWNLOAD = 'MTBS_DOWNLOAD'
    MTBS_COMPARE = 'MTBS_COMPARE'
    DRAFT_THRESHOLD = 'DRAFT_THRESHOLD'
    DRAFT_MIN_AREA = 'DRAFT_MIN_AREA'
    DRAFT_KEEP_SHARE = 'DRAFT_KEEP_SHARE'
    FIRE_NAME = 'FIRE_NAME'
    FIRE_START = 'FIRE_START'
    FIRE_END = 'FIRE_END'
    ASSESSMENT = 'ASSESSMENT'
    PRE_GRANULES = 'PRE_GRANULES'
    POST_GRANULES = 'POST_GRANULES'
    SUBAREAS = 'SUBAREAS'
    SUBAREA_FIELD = 'SUBAREA_FIELD'
    SUBAREA_NAME = 'SUBAREA_NAME'
    ESTIMATE = 'ESTIMATE'
    LOW_T = 'LOW_T'
    MODERATE_T = 'MODERATE_T'
    HIGH_T = 'HIGH_T'
    MIN_CLEAR = 'MIN_CLEAR'
    LOAD = 'LOAD'
    OUTPUT_FOLDER = 'OUTPUT_FOLDER'
    FOLDER = 'FOLDER'
    DNBR = 'DNBR'
    CLASSES = 'CLASSES'
    SUMMARY = 'SUMMARY'
    REPORT = 'REPORT'

    def name(self):
        return 'burnseveritypipeline'

    def displayName(self):
        return 'Burn severity (whole pipeline)'

    def group(self):
        return 'Pipeline'

    def groupId(self):
        return 'pipeline'

    def shortHelpString(self):
        return (
            '<p>Runs the whole assessment for a fire from its burned-area polygons and dates:</p><ol>'
            '<li><i>Suggest image pairs</i> picks the best-ranked pre/post pair (skipped when you '
            'give the ids);</li>'
            '<li><i>Fetch HLS NBR</i> or <i>Fetch Landsat / Sentinel-2 NBR</i> for both dates (3 km '
            'buffer, UTM grid), with the post-fire SWIR2/NIR/red reflectance for display;</li>'
            '<li><i>Build dNBR</i>: dNBR, RdNBR and the offset from a ring outside the burned area;</li>'
            '<li>with <i>Burned area: drafted from dNBR</i>, <i>Draft burned-area perimeter</i> inside '
            'the given polygons (perimeter_draft.gpkg), which then serves as the burned area;</li>'
            '<li><i>Classify severity (MTBS classes)</i> with your or estimated thresholds;</li>'
            '<li><i>Burn severity summary</i>: percent, hectares and acres per class for the fire and '
            'its sub-areas;</li>'
            '<li>a report (report.html, report.json) of every input, scene, calibration, offset, '
            'threshold and result.</li></ol>'
            '<p><b>Imagery:</b> NASA HLS (default; from 2013; needs the Earthdata login in ~/.netrc), or '
            'Landsat 4–9 Collection 2 (from 1982) or Sentinel-2 L2A through Planetary Computer (no '
            'account). Everything is written to the output folder. Choosing the pair reads the quality '
            'band of every candidate date, about a minute or two.</p>'
            '<p><b>From an MTBS fire:</b> type its MTBS fire id or name and year (e.g. <i>Dragon Bravo '
            '2025</i>) in <i>MTBS fire</i> and leave the polygons empty, or give one fire from <i>Find MTBS '
            'fires</i> or the MTBS perimeter file. The plugin fetches the MTBS record itself (USGS, cached in '
            'your profile) and uses its name, ignition date, assessment type, MTBS\'s own pre- and post-fire '
            'image dates and, by default, MTBS\'s thresholds moved to this pair\'s offset; the perimeter '
            'comes from the MTBS map service, or for fires it lacks from the MTBS perimeter file (a one-time '
            '420 MB download you allow under Advanced). For fires through 2024 the report compares the classes '
            'with MTBS\'s published ones. A WFIGS perimeter is matched to its MTBS record by IRWIN id, and any '
            'perimeter by an MTBS fire centred inside it that ignited between the fire\'s dates.</p>'
            '<p><b>From a WFIGS perimeter:</b> give one fire from WFIGS Interagency Fire Perimeters '
            '(NIFC; pick it and tick Selected features only) and leave the fire name and dates empty: '
            'they are read from its record (name; discovery date; containment, else control, else fire-out '
            'date), and the report lists the record (size, cause, ignition point, mapping method).</p>'
            '<p><b>Burned area.</b> <i>As given</i> uses your polygons for everything: draw them along '
            'the burned edge. <i>Drafted from dNBR</i> takes a generous outline drawn around the whole '
            'fire instead: it sets the imagery window and the unburned ring for the offset, and the '
            'burned area is drafted inside it (dNBR at or above the threshold, small patches dropped, '
            'small holes filled). The draft is a starting point (mean IoU 0.69–0.73 against MTBS '
            'perimeters): edit perimeter_draft.gpkg against the imagery and rerun with it as given for a '
            'final assessment.</p>'
            '<p>Validated against 17 MTBS fires: with MTBS\'s thresholds the classes agree with MTBS '
            'at kappa 0.78; estimated thresholds are a starting point (kappa about 0.56). See the '
            'README.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PERIMETER, 'Burned-area polygons (or a generous outline, when drafted; empty = the MTBS fire\'s)',
            [polygon_source()], optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.MTBS_FIRE, 'MTBS fire: id, or name and year (optional; e.g. Dragon Bravo 2025)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.PERIMETER_MODE, 'Burned area', options=PERIMETER_MODES, defaultValue=AS_GIVEN))
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE, 'Imagery', options=[s[1] for s in SOURCES], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            self.FIRE_NAME, 'Fire name (empty = from a WFIGS perimeter, else "Fire")', optional=True))
        self.addParameter(QgsProcessingParameterDateTime(
            self.FIRE_START, 'Fire start (ignition) date (empty = from a WFIGS perimeter)', type=date_type(),
            optional=True))
        self.addParameter(QgsProcessingParameterDateTime(
            self.FIRE_END, 'Fire end (containment) date (empty = from a WFIGS perimeter)', type=date_type(),
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.ASSESSMENT, 'Assessment', options=ASSESSMENTS, defaultValue=INITIAL))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SUBAREAS, 'Sub-area polygons, e.g. watersheds (optional)', [polygon_source()], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.SUBAREA_FIELD, 'Sub-area name field (one zone per value)', parentLayerParameterName=self.SUBAREAS,
            optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.SUBAREA_NAME, 'Sub-area name (when no name field)', 'Sub-area', optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.ESTIMATE, 'Estimate empty thresholds from', options=ESTIMATES, defaultValue=FIRE_MTBS))
        for name, label in ((self.LOW_T, 'Low'), (self.MODERATE_T, 'Moderate'), (self.HIGH_T, 'High')):
            self.addParameter(QgsProcessingParameterNumber(
                name, f'{label} threshold (raw dNBR; empty = estimate)', type=double_type(), optional=True))
        self.addParameter(advanced(QgsProcessingParameterEnum(
            self.PAIR, 'Image pair', options=PAIRS, defaultValue=PAIR_MTBS)))
        self.addParameter(advanced(QgsProcessingParameterBoolean(
            self.MTBS_DOWNLOAD, 'Allow the one-time 420 MB MTBS perimeter download (fires the map service lacks)',
            defaultValue=False)))
        self.addParameter(advanced(QgsProcessingParameterBoolean(
            self.MTBS_COMPARE, 'Compare with MTBS\'s published classes (fires through 2024)', defaultValue=True)))
        self.addParameter(advanced(QgsProcessingParameterString(
            self.PRE_GRANULES, 'Pre-fire granule or item ids (instead of suggesting a pair)', optional=True)))
        self.addParameter(advanced(QgsProcessingParameterString(
            self.POST_GRANULES, 'Post-fire granule or item ids (instead of suggesting a pair)', optional=True)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.DRAFT_THRESHOLD, 'Drafted burned area: dNBR of at least this above the offset', type=double_type(),
            defaultValue=0.05)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.DRAFT_MIN_AREA, 'Drafted burned area: smallest patch kept (ha)', type=double_type(), minValue=0,
            defaultValue=20)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.DRAFT_KEEP_SHARE, 'Drafted burned area: keep patches of at least this share of the largest (%)',
            type=double_type(), minValue=0, maxValue=100, defaultValue=25)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.MIN_CLEAR, 'Minimum clear share inside the area for a suggested pair (%)', type=integer_type(),
            minValue=0, maxValue=100, defaultValue=95)))
        self.addParameter(advanced(QgsProcessingParameterEnum(
            self.LOAD, 'Load into the project', options=LOAD_MODES, defaultValue=LOAD_GROUP)))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT_FOLDER, 'Output folder'))
        self.addOutput(QgsProcessingOutputFolder(self.FOLDER, 'Output folder'))
        self.addOutput(QgsProcessingOutputRasterLayer(self.DNBR, 'dNBR'))
        self.addOutput(QgsProcessingOutputRasterLayer(self.CLASSES, 'Severity classes (MTBS)'))
        self.addOutput(QgsProcessingOutputVectorLayer(self.SUMMARY, 'Severity area summary'))
        self.addOutput(QgsProcessingOutputFile(self.REPORT, 'Report (HTML)'))

    # ---- helpers ------------------------------------------------------------------

    @staticmethod
    def _child(alg, params, context, feedback, log):
        """Run one of our algorithms as a step; collect its log for the report."""
        alg.initAlgorithm()
        full = {d.name(): d.defaultValue() for d in alg.parameterDefinitions()}
        full.update(params)
        sink = []
        push, warn_ = feedback.pushInfo, getattr(feedback, 'pushWarning', feedback.pushInfo)
        # Forward messages to the parent and keep a copy for the report
        child_feedback = QgsProcessingMultiStepFeedback(1, feedback)
        child_feedback.pushInfo = lambda m: (sink.append(m), push(m))[1]
        child_feedback.pushWarning = lambda m: (sink.append('WARNING ' + m), warn_(m))[1]
        feedback.pushInfo(f'--- {alg.displayName()} ---')
        try:
            res, ok = alg.create().run(full, context, child_feedback)
        except QgsProcessingException:
            raise
        except Exception as e:  # QGIS 3 raises from run()
            raise QgsProcessingException(f'{alg.displayName()} failed: {e}') from None
        log[alg.name()] = sink
        if not ok or feedback.isCanceled():
            raise QgsProcessingException(f'{alg.displayName()} failed; see the log above.')
        return res

    def _layer_info(self, parameters, name, context, geom=None, crs=None):
        """Where a polygon input came from, for the report: layer name, source (no password),
        feature count, CRS, selected features only, and the area of geom (its union)."""
        value = parameters.get(name)
        if value in (None, ''):
            return None
        source = self.parameterAsSource(parameters, name, context)
        layer = self.parameterAsVectorLayer(parameters, name, context)
        if layer is None and isinstance(value, QgsProcessingFeatureSourceDefinition):
            # "Selected features only" wraps the layer: look it up by its id or source
            layer = QgsProcessingUtils.mapLayerFromString(str(value.source.staticValue()), context)
        info = {'layer': layer.name() if layer else str(value),
                'source': QgsDataSourceUri.removePassword(layer.source()) if layer else str(value),
                'provider': layer.providerType() if layer else '',
                'features': source.featureCount() if source else None,
                'crs': source.sourceCrs().authid() if source else '',
                'selected_only': isinstance(value, QgsProcessingFeatureSourceDefinition) and value.selectedFeaturesOnly}
        info['temporary'] = info['provider'] == 'memory'
        if geom is not None:
            info.update(_area(geom, crs, context))
        return info

    def _load(self, parameters, context, fire, folder, drafted, low, moderate, high):
        """Load the results into the project: by default into one group per run, named
        after the fire and date, styled, with only the classes and outlines switched on."""
        mode = self.parameterAsEnum(parameters, self.LOAD, context)
        f = lambda name: os.path.join(folder, name)  # noqa: E731
        if mode == LOAD_NOTHING:
            return
        if mode == LOAD_CLASSES:
            styles.load_on_completion(context, f('dnbr6.tif'), f'{fire} severity classes (MTBS)', styles.mtbs_classes)
            return
        group = styles.unique_group(context, f'{fire} {datetime.date.today().isoformat()}', f('dnbr6.tif'))
        # (file, name, style, shown, vector); listed top to bottom
        layers = [('zones.gpkg', 'Burned area and sub-areas', styles.zones(fire), True, True),
                  ('perimeter_draft.gpkg', 'Drafted burned area (to edit)', styles.outline('200,0,0,255', 0.4, True),
                   False, True),
                  ('dnbr6.tif', 'Severity classes (MTBS)', styles.mtbs_classes, True, False),
                  ('mtbs_dnbr6.tif', 'MTBS published classes', styles.mtbs_classes, False, False),
                  ('dnbr.tif', f'dNBR (thresholds {low:.3f} / {moderate:.3f} / {high:.3f})',
                   styles.index_ramp(-0.5, 1.0), False, False),
                  ('rdnbr.tif', 'RdNBR', styles.index_ramp(-1.0, 2.0), False, False),
                  ('rbr.tif', 'RBR', styles.index_ramp(-0.3, 0.6), False, False),
                  ('reflectance_post.tif', 'Post-fire reflectance (SWIR2 / NIR / red)', styles.false_color, False, False),
                  ('nbr_post.tif', 'Post-fire NBR', styles.gray(-1, 1), False, False),
                  ('nbr_pre.tif', 'Pre-fire NBR', styles.gray(-1, 1), False, False)]
        for i, (name, label, style, shown, vector) in enumerate(layers):
            if (name != 'perimeter_draft.gpkg' or drafted) and os.path.exists(f(name)):
                styles.load_on_completion(context, f(name), label, style, shown, group, len(layers) - i, vector)

    @staticmethod
    def _compare(record, classes_path, folder, feedback, log):
        """Our classes against MTBS's published ones (image service, the fire's year) inside
        the burned area; writes mtbs_dnbr6.tif. A dict for the report."""
        from osgeo import gdal
        from .. import raster
        year = record.get('year')
        ds = gdal.Open(classes_path)
        try:
            theirs = mtbs.published_classes(ds, year, feedback)
        except mtbs.MtbsError as e:
            log.setdefault('pipeline', []).append(f'Comparison with MTBS skipped: {e}')
            return {'note': f'not done: {e}'}
        if theirs is None:
            return {'note': 'not done: the area is too large for one MTBS image service request'}
        ours = ds.GetRasterBand(1).ReadAsArray()
        inside = (ours > 0) & (ours < 255)
        result = mtbs.compare(ours, theirs, inside)
        if result is None:
            return {'note': f'MTBS has not published classes for this fire yet (its image service holds the '
                            f'classes by year; none for {year} inside the burned area)'}
        out = os.path.join(folder, 'mtbs_dnbr6.tif')
        theirs = theirs.copy()
        theirs[~inside] = 0
        raster.write(out, [('mtbs_dnbr6', theirs)], ds, gdal.GDT_Byte, 255,
                     {'SOURCE': 'MTBS CONUS image service (USFS EDW)', 'MTBS_EVENT_ID': str(record['event_id']),
                      'YEAR': str(year)})
        write_style(out)
        result['file'] = 'mtbs_dnbr6.tif'
        feedback.pushInfo(f'Compared with MTBS\'s published classes: {result["agreement"]:.1f} % of '
                          f'{result["pixels"]:,} pixels agree, kappa {result["kappa"]:.3f}, '
                          f'{result["within_one"]:.1f} % within one class.')
        return result

    @staticmethod
    def _map_png(classes_path, geom, crs, png, context):
        """dnbr6.png: the classes with the burned-area outline (geom in crs)."""
        from osgeo import gdal
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry
        from .. import raster
        ds = gdal.Open(classes_path)
        classes = ds.GetRasterBand(1).ReadAsArray()
        ring = QgsGeometry(geom.constGet().boundary())  # polygon boundary as lines
        width = 1.5 * abs(ds.GetGeoTransform()[1])  # outline about 1.5 pixels wide
        rcrs = QgsCoordinateReferenceSystem.fromWkt(ds.GetProjection())
        ring.transform(QgsCoordinateTransform(crs, rcrs, context.transformContext()))
        outline = raster.rasterize(ring.buffer(width / 2, 4), rcrs, ds, context.transformContext())
        write_png(classes, ds, outline, png)

    def _record(self, parameters, context, feedback):
        """This fire's MTBS record and how it was found: named in MTBS_FIRE, else the
        burned-area layer's own MTBS attributes, else (later) by IRWIN id or location."""
        text = (self.parameterAsString(parameters, self.MTBS_FIRE, context) or '').strip()
        layer_record = None
        if parameters.get(self.PERIMETER) not in (None, ''):
            layer_record = read_mtbs(self.parameterAsSource(parameters, self.PERIMETER, context))
        if text:
            try:
                r = mtbs.resolve(mtbs.records(feedback), text)
            except mtbs.MtbsError as e:
                raise QgsProcessingException(f'MTBS fire: {e}') from None
            if layer_record and layer_record.get('event_id') != r.get('event_id'):
                steps_warn = f'The burned-area layer is MTBS fire {layer_record.get("event_id")}, but MTBS fire ' \
                             f'names {r.get("event_id")}; using {r.get("event_id")}.'
                getattr(feedback, 'pushWarning', feedback.pushInfo)(steps_warn)
            return r, 'named in MTBS fire'
        if layer_record:
            return layer_record, "the burned-area layer's MTBS attributes"
        return None, None

    @staticmethod
    def _match(incident, geom, crs, start, end, area_ac, context, feedback):
        """The MTBS record of a fire given without one: by the WFIGS IRWIN id, else an
        MTBS fire centred inside the burned area that ignited between its dates."""
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry
        try:
            recs = mtbs.records(feedback)
        except mtbs.MtbsError as e:
            feedback.pushInfo(f'MTBS records not available ({e}); no MTBS record looked up.')
            return None, None
        if incident and incident.get('irwin'):
            r = mtbs.by_irwin(recs, incident['irwin'])
            if r:
                return r, f'matched by the WFIGS IRWIN id {incident["irwin"]}'
        g = QgsGeometry(geom)
        g.transform(QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem('EPSG:4326'), context.transformContext()))
        found = mtbs.by_location(recs, g, start, end, area_ac)
        if not found:
            return None, None
        how = 'matched by location and date (an MTBS fire centred inside the burned area, ignited between its dates)'
        if len(found) > 1:
            others = ', '.join(f'{mtbs.display_name(r)} {r["event_id"]}' for r in found[1:4])
            getattr(feedback, 'pushWarning', feedback.pushInfo)(
                f'{len(found)} MTBS fires are centred inside the burned area and ignited between its dates; using '
                f'the closest in size, {mtbs.display_name(found[0])} ({found[0]["event_id"]}); others: {others}. '
                'Name the fire in MTBS fire to choose another.')
        return found[0], how

    def _mtbs_pair(self, r, source, perimeter, context, feedback):
        """MTBS's own pre/post scenes as {label: (ids, source key, date, platform)}. With
        HLS chosen, both come from HLS when it has them (Landsat 8/9, Sentinel-2, and for
        Sentinel-2 the very tile MTBS used); otherwise both come from Planetary Computer,
        each from its sensor's collection, so the pair is processed alike. With Landsat or
        Sentinel-2 (Planetary Computer) chosen, MTBS's scenes are used only when they are
        that sensor's; None otherwise (the user's choice of imagery wins). MtbsError when a
        scene can't be found."""
        from qgis.core import QgsJsonUtils
        aoi = aoi_geometry(self.parameterAsSource({self.PERIMETER: perimeter}, self.PERIMETER, context), context)
        box = aoi.boundingBox()
        bbox = [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()]
        scenes = {}
        for label, key in (('pre', 'pre_id'), ('post', 'post_id')):
            try:
                scenes[label] = (key, mtbs.parse_scene(r.get(key)))
            except ValueError as e:
                raise mtbs.MtbsError(str(e)) from None

        def from_hls(scene):
            try:
                rows = search_granules(aoi, scene['date'], scene['date'], [scene['hls']], 100, context, feedback)
            except QgsProcessingException as e:  # the HLS search service is down or failing
                feedback.pushInfo(f'NASA HLS search failed ({str(e)[:200]}).')
                return None
            tiles = {row[3] for row in rows}
            if not rows or (scene['kind'] == 'sentinel-2' and scene['where'] not in tiles):
                return None
            return [row[1] for row in rows]

        def from_planetary(scene):
            try:
                items = planetary.search(scene['planetary'], bbox, scene['date'], scene['date'], feedback)
            except stac.StacError as e:
                raise mtbs.MtbsError(str(e)) from None
            return [i['id'] for i in items
                    if str(i['properties'].get('platform', '')).lower() == scene['platform']
                    and QgsJsonUtils.geometryFromGeoJson(json.dumps(i['geometry'])).intersects(aoi)]

        if source != 'hls' and any(sc['planetary'] != source for _, sc in scenes.values()):
            used = ' and '.join(sorted({sc['platform'] for _, sc in scenes.values()}))
            feedback.pushInfo(f'MTBS used {used}; with {dict(SOURCES)[source].split(",")[0]} chosen as the imagery, '
                              'a pair is suggested from it instead of MTBS\'s scenes.')
            return None
        ids = {}
        if source == 'hls' and all(sc['hls'] for _, sc in scenes.values()):
            for label, (key, sc) in scenes.items():
                ids[label] = from_hls(sc)
            if all(ids.values()):
                return {label: (' '.join(ids[label]), 'hls', sc['date'].isoformat(), sc['platform'])
                        for label, (key, sc) in scenes.items()}
            lacking = [f'{scenes[k][1]["platform"]} {scenes[k][1]["date"]} tile {scenes[k][1]["where"]}'
                       for k, v in ids.items() if not v]
            feedback.pushInfo('NASA HLS lacks MTBS\'s ' + ', '.join(lacking) + '; taking both of MTBS\'s scenes '
                              'from Planetary Computer instead, so the pair is processed alike.')
        elif source == 'hls':
            feedback.pushInfo('NASA HLS has no Landsat 4-7; taking MTBS\'s scenes from Planetary Computer.')
        out = {}
        for label, (key, sc) in scenes.items():
            found = from_planetary(sc)
            if not found:
                raise mtbs.MtbsError(f'MTBS\'s {label}-fire scene {r.get(key)} ({sc["platform"]}, {sc["date"]}) is not '
                                     'in Planetary Computer over the burned area')
            out[label] = (' '.join(found), sc['planetary'], sc['date'].isoformat(), sc['platform'])
        return out

    # ---- main ---------------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        if folder == QgsProcessing.TEMPORARY_OUTPUT or not folder:
            folder = QgsProcessingUtils.generateTempFilename('burn_severity')
        os.makedirs(folder, exist_ok=True)
        parameters = dict(parameters)
        record, record_how = self._record(parameters, context, feedback)

        # Burned area: as given, else the MTBS fire's perimeter
        perimeter_source = None
        if parameters.get(self.PERIMETER) in (None, ''):
            if record is None:
                raise QgsProcessingException('Give the burned-area polygons, or an MTBS fire to fetch them for.')
            gj, how = mtbs.perimeter(record, feedback, self.parameterAsBool(parameters, self.MTBS_DOWNLOAD, context))
            if gj is None:
                raise QgsProcessingException(how)
            path = os.path.join(folder, 'mtbs_perimeter.geojson')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(gj, f)
            parameters[self.PERIMETER] = path
            perimeter_source = how
            feedback.pushInfo(f'Burned area: the MTBS perimeter of {mtbs.describe(record)}, from the {how}.')
        perimeter = parameters[self.PERIMETER]
        pre_g = (self.parameterAsString(parameters, self.PRE_GRANULES, context) or '').strip()
        post_g = (self.parameterAsString(parameters, self.POST_GRANULES, context) or '').strip()
        if bool(pre_g) != bool(post_g):
            raise QgsProcessingException('Give both the pre-fire and the post-fire granule ids, or neither.')
        want_mtbs_pair = self.parameterAsEnum(parameters, self.PAIR, context) == PAIR_MTBS and not pre_g

        # The WFIGS record's IRWIN id may name the MTBS fire before the dates are needed
        source_layer = self.parameterAsSource(parameters, self.PERIMETER, context)
        geom, crs = source_union(source_layer, 'burned-area')
        if record is None:
            wfigs = read_incident(source_layer)
            if wfigs and wfigs.get('irwin'):
                try:
                    r = mtbs.by_irwin(mtbs.records(feedback), wfigs['irwin'])
                except mtbs.MtbsError as e:
                    feedback.pushInfo(f'MTBS records not available ({e}).')
                    r = None
                if r:
                    record, record_how = r, f'matched by the WFIGS IRWIN id {wfigs["irwin"]}'
        # Name and dates: as given, else from the perimeter's WFIGS record, else the MTBS record
        # (stops on a layer of many fires); MTBS's own pair needs no end date
        fire, start_d, end_d, incident = fire_details(
            self, parameters, context, feedback, self.PERIMETER, self.FIRE_NAME, self.FIRE_START, self.FIRE_END,
            mtbs_record=record, need_end=not (want_mtbs_pair and record is not None))
        area = _area(geom, crs, context)
        if record is None:
            record, record_how = self._match(incident, geom, crs, start_d, end_d, area['area_ac'], context, feedback)
            if record is not None and not fire:
                fire = mtbs.display_name(record)
        if record is not None:
            feedback.pushInfo(f'MTBS record: {mtbs.describe(record)}; {record_how}.')
        fire = fire or 'Fire'
        start = QDate(start_d)
        end = QDate(end_d) if end_d else QDate()
        assessment = self.parameterAsEnum(parameters, self.ASSESSMENT, context)
        drafted = self.parameterAsEnum(parameters, self.PERIMETER_MODE, context) == DRAFTED
        source_index = self.parameterAsEnum(parameters, self.SOURCE, context)
        source = SOURCES[source_index][0]
        steps = QgsProcessingMultiStepFeedback(7 if drafted else 6, feedback)
        log, report = {}, {'fireanalysis_version': VERSION, 'created': datetime.datetime.now().isoformat(timespec='seconds'),
                           'fire': fire, 'fire_start': start.toString('yyyy-MM-dd'),
                           'fire_end': end.toString('yyyy-MM-dd') if end.isValid() else None,
                           'assessment': ASSESSMENTS[assessment],
                           'imagery': SOURCES[source_index][1], 'incident': incident}
        if record is not None:
            report['mtbs'] = {k: record.get(k) for k in mtbs.PUBLISHED}
            report['mtbs'].update({'how': record_how, 'perimeter_source': perimeter_source,
                                   'thresholds_raw': mtbs.thresholds(record), 'pair_used': False})

        # Inputs, so a report says which perimeter and sub-areas it used
        given = {k.lower(): self.parameterAsDouble(parameters, k, context)
                 for k in (self.LOW_T, self.MODERATE_T, self.HIGH_T) if parameters.get(k) not in (None, '')}
        report['inputs'] = {
            'perimeter': self._layer_info(parameters, self.PERIMETER, context, geom, crs),
            'subareas': self._layer_info(parameters, self.SUBAREAS, context),
            'subarea_field': self.parameterAsString(parameters, self.SUBAREA_FIELD, context) or None,
            'subarea_name': self.parameterAsString(parameters, self.SUBAREA_NAME, context) or None,
            'estimate': ESTIMATES[self.parameterAsEnum(parameters, self.ESTIMATE, context)],
            'thresholds_given': given,
            'granules_given': bool(pre_g),
            'min_clear': self.parameterAsInt(parameters, self.MIN_CLEAR, context),
            'output_folder': folder,
            'perimeter_mode': PERIMETER_MODES[self.parameterAsEnum(parameters, self.PERIMETER_MODE, context)]}
        if perimeter_source:
            report['inputs']['perimeter']['layer'] = f'MTBS perimeter, {perimeter_source}'

        # 1. Image pair: given ids; else MTBS's own dates; else the best suggested pair
        fetch = {}  # label -> (ids, source key)
        if pre_g:
            report['pair'] = {'source': 'ids given'}
            fetch = {'pre': (pre_g, source), 'post': (post_g, source)}
        elif want_mtbs_pair and record is not None:
            try:
                pair = self._mtbs_pair(record, source, perimeter, context, feedback)
            except mtbs.MtbsError as e:
                pair = None
                text = f'{e}; suggesting a pair instead.'
                steps.pushWarning(text)
                log.setdefault('pipeline', []).append('WARNING ' + text)
            if pair:
                fetch = {k: (v[0], v[1]) for k, v in pair.items()}
                report['pair'] = {'source': f'MTBS\'s own scenes ({record.get("pre_id")}, {record.get("post_id")})',
                                  'pre_date': pair['pre'][2], 'pre_sensor': pair['pre'][3],
                                  'post_date': pair['post'][2], 'post_sensor': pair['post'][3]}
                report['mtbs']['pair_used'] = True
                kind = EXTENDED if 'extended' in str(record.get('asmnt_type', '')).lower() else INITIAL
                if kind != assessment:
                    feedback.pushInfo(f'Assessment: {ASSESSMENTS[kind]}, as MTBS\'s ({record.get("asmnt_type")}).')
                    assessment = kind
                    report['assessment'] = ASSESSMENTS[assessment]
                labels = {s[0]: s[1].split(',')[0] for s in SOURCES}
                for k, v in pair.items():
                    feedback.pushInfo(f'MTBS {k}-fire scene: {v[3]} {v[2]} from {labels[v[1]]}: {v[0]}')
        if not fetch:
            if not end.isValid():
                end = start
                text = 'No fire end date (MTBS records have none); suggesting post-fire dates from the ignition date.'
                steps.pushWarning(text)
                log.setdefault('pipeline', []).append('WARNING ' + text)
            res = self._child(SuggestImagePairs(), {
                'SOURCE': source_index, 'AOI': perimeter, 'FIRE_START': start, 'FIRE_END': end, 'ASSESSMENT': assessment,
                'MIN_CLEAR': self.parameterAsInt(parameters, self.MIN_CLEAR, context), 'COUNT': 5,
                'OUTPUT': os.path.join(folder, 'image_pairs.csv')}, context, steps, log)
            layer = QgsProcessingUtils.mapLayerFromString(res['OUTPUT'], context)
            # image_pairs.csv comes back with text fields, so compare the rank as a number
            best = next((f for f in layer.getFeatures() if int(float(str(f['rank']))) == 1), None) if layer else None
            if best is None:
                raise QgsProcessingException('No image pair found; see the log, or give the granule ids.')
            fetch = {'pre': (str(best['pre_granules']), source), 'post': (str(best['post_granules']), source)}
            report['pair'] = {k: best[k] for k in ('pre_date', 'pre_sensor', 'pre_clear', 'post_date', 'post_sensor',
                                                   'post_clear', 'doy_gap', 'years')}
            report['pair']['source'] = 'Suggest image pairs, rank 1 (others in image_pairs.csv)'
        report['pair'].update({'pre_granules': fetch['pre'][0].split(), 'post_granules': fetch['post'][0].split(),
                               'fetched_from': {k: v[1] for k, v in fetch.items()}})
        steps.setCurrentStep(1)

        # 2. NBR for both dates
        nbr = {}
        keys = [s[0] for s in SOURCES]
        for label in ('pre', 'post'):
            granules, used = fetch[label]
            params = {'AOI': perimeter, 'NBR': os.path.join(folder, f'nbr_{label}.tif')}
            if label == 'post':
                params['REFLECTANCE'] = os.path.join(folder, 'reflectance_post.tif')
            if used == 'hls':
                res = self._child(FetchHlsNbr(), dict(params, GRANULES=granules), context, steps, log)
            else:
                res = self._child(FetchPlanetaryNbr(), dict(params, ITEMS=granules, SOURCE=keys.index(used) - 1),
                                  context, steps, log)
            nbr[label] = res['NBR']
        steps.setCurrentStep(2)

        # 3. dNBR, RdNBR, offset
        res = self._child(BuildDnbr(), {
            'PRE_NBR': nbr['pre'], 'POST_NBR': nbr['post'], 'PERIMETER': perimeter,
            'DNBR': os.path.join(folder, 'dnbr.tif'), 'RDNBR': os.path.join(folder, 'rdnbr.tif'),
            'RBR': os.path.join(folder, 'rbr.tif')}, context, steps, log)
        dnbr = res['DNBR']
        report['offset'] = {'value': res['OFFSET'], 'std': res['OFFSET_STD'], 'pixels': res['OFFSET_N'],
                            'method': 'median dNBR in a ring 90-2090 m outside the '
                                      + ('given outline' if drafted else 'burned area')}

        # 3b. Burned area drafted from dNBR inside the given outline; it replaces the perimeter from here on
        if drafted:
            threshold = self.parameterAsDouble(parameters, self.DRAFT_THRESHOLD, context)
            min_area = self.parameterAsDouble(parameters, self.DRAFT_MIN_AREA, context)
            keep_share = self.parameterAsDouble(parameters, self.DRAFT_KEEP_SHARE, context)
            res = self._child(DraftPerimeter(), {
                'DNBR': dnbr, 'THRESHOLD': threshold, 'MIN_AREA': min_area, 'HOLE_AREA': DRAFT_HOLE_HA,
                'SEED': perimeter, 'CLIP': True, 'KEEP_SHARE': keep_share, 'OFFSET': report['offset']['value'],
                'OUTPUT': os.path.join(folder, 'perimeter_draft.gpkg')}, context, steps, log)
            layer = QgsProcessingUtils.mapLayerFromString(res['OUTPUT'], context) \
                or QgsVectorLayer(res['OUTPUT'], 'perimeter_draft', 'ogr')
            if not layer or not layer.isValid() or layer.featureCount() == 0:
                raise QgsProcessingException(
                    f'No burned area found at dNBR >= {threshold} inside the given polygons. Lower the threshold, '
                    'or check the outline and the image pair.')
            perimeter = res['OUTPUT']
            geom, crs = source_union(layer, 'drafted burned-area')
            info = _area(geom, crs, context)
            outline_ha = report['inputs']['perimeter']['area_ha']
            report['perimeter_draft'] = {
                'file': 'perimeter_draft.gpkg', 'threshold': threshold, 'offset': report['offset']['value'],
                'keep_share': keep_share, 'min_area_ha': min_area,
                'hole_area_ha': DRAFT_HOLE_HA, 'area_ha': info['area_ha'], 'area_ac': info['area_ac'],
                'share_of_outline': 100.0 * info['area_ha'] / outline_ha if outline_ha else None}
            feedback.pushInfo(f'Drafted burned area: {info["area_ha"]:,.0f} ha ({info["area_ac"]:,.0f} ac), '
                              f'{report["perimeter_draft"]["share_of_outline"]:.0f} % of the given outline.')
        steps.setCurrentStep(3)

        # 4. MTBS classes
        params = {'DNBR': dnbr, 'PRE_NBR': nbr['pre'], 'PERIMETER': perimeter, 'ASSESSMENT': assessment,
                  'ESTIMATE': self.parameterAsEnum(parameters, self.ESTIMATE, context),
                  'OUTPUT': os.path.join(folder, 'dnbr6.tif')}
        if record is not None:
            params['MTBS_FIRE'] = record['event_id']
            # the record is in hand: let Classify severity read it from the perimeter when that carries it
            if not drafted and read_mtbs(self.parameterAsSource({self.PERIMETER: perimeter}, self.PERIMETER, context)):
                params['MTBS_FIRE'] = None
        for k in (self.LOW_T, self.MODERATE_T, self.HIGH_T):
            if parameters.get(k) not in (None, ''):
                params[k] = self.parameterAsDouble(parameters, k, context)
        res = self._child(ClassifySeverity(), params, context, steps, log)
        classes = res['OUTPUT']
        low, moderate, high = res['LOW_T'], res['MODERATE_T'], res['HIGH_T']
        report['thresholds'] = {'low': low, 'moderate': moderate, 'high': high,
                                'notes': [m for m in log.get('classifyseverity', []) if 'threshold:' in m]}
        method = params['ESTIMATE']
        report['thresholds']['from_mtbs'] = any('; MTBS ' in m or 'MTBS mapped no' in m
                                                for m in report['thresholds']['notes'])
        # Cross-check: another estimate method, for the report only (MTBS's own against nearby fires)
        if any(parameters.get(k) in (None, '') for k in (self.LOW_T, self.MODERATE_T, self.HIGH_T)):
            other = NEARBY if report['thresholds']['from_mtbs'] else (OWN_DATA if method in (NEARBY, FIRE_MTBS)
                                                                       else NEARBY)
            try:
                alt = self._child(ClassifySeverity(), dict(params, ESTIMATE=other, OUTPUT=QgsProcessing.TEMPORARY_OUTPUT),
                                  context, steps, {})
                report['thresholds']['alternative'] = {
                    'method': ESTIMATES[other], 'low': alt['LOW_T'], 'moderate': alt['MODERATE_T'], 'high': alt['HIGH_T']}
                if abs(min(alt['HIGH_T'], 1.5) - min(high, 1.5)) > 0.1:
                    text = (f'The two threshold estimates disagree on the high threshold ({high:.3f}, and '
                            f'{alt["HIGH_T"]:.3f} from {_lower_first(ESTIMATES[other])}). Check the high class '
                            'against the post-fire imagery before using it.')
                    steps.pushWarning(text)
                    log.setdefault('pipeline', []).append('WARNING ' + text)
            except QgsProcessingException as e:
                report['thresholds']['alternative'] = {'method': ESTIMATES[other], 'error': str(e)}
        if record is not None and self.parameterAsBool(parameters, self.MTBS_COMPARE, context):
            report['mtbs']['comparison'] = self._compare(record, classes, folder, feedback, log)
        steps.setCurrentStep(4)

        # 5. Area summary per class and zone (MTBS classes 1-4; increased greenness counts as unburned to low)
        table, names = [], []
        bounds = [-1e10, low, moderate, high, 1e10]
        for i, name in enumerate(CLASSES):
            if bounds[i] < NONE_VALUE and bounds[i] < bounds[i + 1]:
                table += [bounds[i], min(bounds[i + 1], 1e10), i + 1]
                names.append(name)
        sub = parameters.get(self.SUBAREAS) or None
        res = self._child(_summary_algorithm(), {
            'DNBR': dnbr, 'BURN_AREA': perimeter, 'ZONE_A_NAME': fire, 'WATERSHED': sub,
            'SUBAREA_FIELD': parameters.get(self.SUBAREA_FIELD) if sub else None,
            'ZONE_B_NAME': self.parameterAsString(parameters, self.SUBAREA_NAME, context) or 'Sub-area',
            'TABLE': table, 'CLASS_NAMES': ','.join(names),
            'ZONES': os.path.join(folder, 'zones.gpkg'), 'SEVERITY': os.path.join(folder, 'severity_summary_classes.tif'),
            'HISTOGRAM': os.path.join(folder, 'histogram.gpkg'), 'SUMMARY': os.path.join(folder, 'summary.gpkg')},
            context, steps, log)
        summary = res['SUMMARY']
        rows = []
        layer = QgsProcessingUtils.mapLayerFromString(summary, context) or QgsVectorLayer(summary, 'summary', 'ogr')
        fields = [n for n in layer.fields().names() if n != 'fid']
        for f in layer.getFeatures():
            rows.append({n: f[n] for n in fields})
        report['summary'] = rows
        zones_layer = QgsProcessingUtils.mapLayerFromString(res['ZONES'], context) \
            or QgsVectorLayer(res['ZONES'], 'zones', 'ogr')
        names = [str(f['zone']) for f in zones_layer.getFeatures()] if zones_layer and zones_layer.isValid() else []
        codes = [n[4:] for n in fields if n.startswith('pct_')]
        # zone A first, then the sub-areas: the summary columns follow the same order
        report['zones'] = dict(zip(codes, names)) if len(codes) == len(names) else {}
        with open(os.path.join(folder, 'summary.csv'), 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(fields)
            for row in rows:
                w.writerow([(int(round(row[n])) if n.startswith('ac_') else round(row[n], 2))
                            if isinstance(row[n], float) else row[n] for n in fields])
        steps.setCurrentStep(5)

        # 6. Report, with a map picture of the classes
        try:
            self._map_png(classes, geom, crs, os.path.join(folder, 'dnbr6.png'), context)
        except Exception as e:  # a picture must never fail the assessment
            log.setdefault('pipeline', []).append(f'WARNING Could not draw dnbr6.png: {e}')
        report['warnings'] = [m[len('WARNING '):] for msgs in log.values() for m in msgs if m.startswith('WARNING ')]
        report['files'] = sorted(os.listdir(folder))
        report['log'] = log
        with open(os.path.join(folder, 'report.json'), 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=1, default=str)
        html_path = os.path.join(folder, 'report.html')
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(_html(report, fields, rows))
        feedback.pushInfo(f'Report: {html_path}')

        self._load(parameters, context, fire, folder, drafted, low, moderate, high)
        return {self.FOLDER: folder, self.DNBR: dnbr, self.CLASSES: classes, self.SUMMARY: summary,
                self.REPORT: html_path}


def _area(geom, crs, context):
    """{'area_ha', 'area_ac'} of geom (in crs), measured on the ellipsoid."""
    da = QgsDistanceArea()
    da.setSourceCrs(crs, context.transformContext())
    da.setEllipsoid(context.ellipsoid() if context.ellipsoid() not in ('', 'NONE') else 'EPSG:7030')
    area = da.measureArea(geom)  # square meters
    return {'area_ha': area / 1e4, 'area_ac': area / 4046.8564224}


def _chart(fields, rows, names=None):
    """Horizontal bars of percent per class for each zone, in plain HTML; names maps
    a field code (pct_<code>) to the zone's name."""
    e = html.escape
    zones = [n for n in fields if n.startswith('pct_')]
    colours = {'Unburned to low': CLASS_COLORS[1], 'Low': CLASS_COLORS[2], 'Moderate': CLASS_COLORS[3],
               'High': CLASS_COLORS[4]}
    out = ['<div style="margin:.5rem 0 1rem">']
    for z in zones:
        out.append(f'<div style="margin:.4rem 0 .2rem"><b>{e((names or {}).get(z[4:], z[4:]))}</b></div>')
        for row in rows:
            v = float(row[z] or 0)
            colour = colours.get(str(row['sev_class']), '#888')
            out.append(
                f'<div style="display:flex;align-items:center;gap:.5rem;font-size:.9em">'
                f'<span style="width:9rem">{e(str(row["sev_class"]))}</span>'
                f'<span style="display:inline-block;height:.9rem;width:{max(v, 0.2) * 3:.1f}px;background:{colour};'
                f'border:1px solid #999"></span><span>{v:.1f} %</span></div>')
    out.append('</div>')
    return ''.join(out)


def _lower_first(text):
    """'Initial (soon...)' -> 'initial (soon...)', but keep 'dNBR' and other inner capitals."""
    return text[:1].lower() + text[1:]


def _incident_html(r):
    """The Fire record section: the perimeter's WFIGS incident record, when it has one."""
    e = html.escape
    f = r.get('incident')
    if not f:
        return ''
    rows = [('Incident', ' '.join(str(x) for x in (f.get('name'), f'({f["id"]})' if f.get('id') else '') if x)),
            ('Discovered', f'{f["start"]} ({f["start_field"]})' if f.get('start') else ''),
            ('End', f'{f["end"]} ({f["end_note"]}, {f["end_field"]})' if f.get('end') else 'not recorded'),
            ('Reported size', f'{f["acres"]:,.0f} ac ({f["acres"] / 2.4710538:,.0f} ha)' if f.get('acres') else ''),
            ('Ignition point', f'{f["ignition"][0]:.5f}, {f["ignition"][1]:.5f}' if f.get('ignition') else ''),
            ('Perimeter', ', '.join(x for x in (f.get('map_method'), f'dated {f["perimeter_date"]}'
                                                if f.get('perimeter_date') else '', f.get('feature_category')) if x)),
            ('Cause', f.get('cause') or ''),
            ('Location', ', '.join(x for x in (f.get('description'), f.get('county'), f.get('state'), f.get('unit'))
                                   if x))]
    body = ''.join(f'<tr><td>{e(k)}</td><td style="text-align:left">{e(v)}</td></tr>' for k, v in rows if v)
    return (f'<h2>Fire record</h2><p class="note">From the perimeter\'s {e(f["source"])} attributes; '
            'dates the run used unless you gave them.</p><table>' + body + '</table>')


def _mtbs_html(r):
    """The MTBS section: the fire's MTBS record, what the run took from it, and the
    comparison with MTBS's published classes."""
    e = html.escape
    m = r.get('mtbs')
    if not m:
        return ''

    def x1000(v):
        return '' if v is None else ('none' if abs(v) >= 9999 else f'{v:.0f}')
    used = []
    if m.get('perimeter_source'):
        used.append(f'perimeter ({m["perimeter_source"]})')
    if m.get('pair_used'):
        used.append('image dates')
    if (r.get('thresholds') or {}).get('from_mtbs'):
        used.append('thresholds (moved to this pair\'s offset)')
    rows = [('Fire', f'{m.get("incid_name") or ""} ({m.get("event_id")})'),
            ('How found', m.get('how') or ''),
            ('Ignition', m.get('ig_date') or ''),
            ('Assessment', f'{m.get("asmnt_type") or ""}, {m.get("map_prog") or ""}'.strip(', ')),
            ('MTBS burned area', f'{m["burnbndac"]:,.0f} ac' if m.get('burnbndac') else ''),
            ('MTBS scenes', f'pre {m.get("pre_id") or "?"}, post {m.get("post_id") or "?"}'),
            ('MTBS offset and thresholds (x1000)',
             f'offset {x1000(m.get("dnbr_offst"))}; low {x1000(m.get("low_t"))}, moderate {x1000(m.get("mod_t"))}, '
             f'high {x1000(m.get("high_t"))}'),
            ('Used in this run', ', '.join(used) if used else 'nothing (given or other choices)')]
    body = ''.join(f'<tr><td>{e(k)}</td><td style="text-align:left">{e(str(v))}</td></tr>' for k, v in rows if v)
    out = ['<h2>MTBS record</h2><p class="note">From the MTBS fire records (USGS), which the plugin fetches and '
           'caches.</p><table>' + body + '</table>']
    c = m.get('comparison')
    if c and 'agreement' in c:
        cls = ''.join(f'<tr><td>{e(name)}</td><td>{a:.1f}</td><td>{b:.1f}</td></tr>'
                      for name, a, b in zip(CLASS_LABELS, c['ours'], c['theirs']))
        out.append(f'<h3>Compared with MTBS\'s published classes</h3><p>{c["agreement"]:.1f} % of {c["pixels"]:,} '
                   f'pixels agree (kappa {c["kappa"]:.3f}); {c["within_one"]:.1f} % within one class. '
                   '<span class="note">Pixels both mapped, inside the burned area; MTBS\'s classes are in '
                   'mtbs_dnbr6.tif.</span></p><table><tr><th>Class</th><th>% ours</th><th>% MTBS</th></tr>'
                   f'{cls}</table>')
    elif c and c.get('note'):
        out.append(f'<p class="note">Comparison with MTBS\'s published classes: {e(c["note"])}.</p>')
    return ''.join(out)


def _inputs_html(r):
    """The Inputs section: which perimeter and sub-area layers the run used."""
    e = html.escape
    i = r.get('inputs') or {}

    def layer(info):
        if not info:
            return ''
        notes = [f'{info["features"]:,} feature(s)' if info.get('features') is not None else '',
                 info.get('crs') or '', 'selected features only' if info.get('selected_only') else '',
                 'temporary layer, not saved' if info.get('temporary') else '']
        if 'area_ha' in info:
            notes.insert(0, f'{info["area_ha"]:,.0f} ha ({info["area_ac"]:,.0f} ac)')
        return (f'{e(info["layer"])} <span class="note">({e(", ".join(n for n in notes if n))})</span>'
                f'<br><code>{e(info["source"])}</code>')
    d = r.get('perimeter_draft')
    if d:
        out = [f'<h2>Inputs</h2><p>Burned area: <b>drafted from dNBR</b>, {d["area_ha"]:,.0f} ha ({d["area_ac"]:,.0f} ac), '
               f'{d["share_of_outline"]:.0f} % of the outline; dNBR at least {d["threshold"]:g} above the offset '
               f'({d.get("offset", 0) + d["threshold"]:.3f}), patches of {d["min_area_ha"]:g} ha or more and at least '
               f'{d.get("keep_share", 0):g} % of the largest, holes up to {d["hole_area_ha"]:g} ha filled '
               f'(<code>{e(d["file"])}</code>). '
               '<span class="note">A draft: edit it against the imagery and rerun with it as given for a final '
               'assessment.</span></p>'
               f'<p>Outline (imagery window and offset ring): {layer(i.get("perimeter"))}</p>']
    else:
        out = [f'<h2>Inputs</h2><p>Burned area: {layer(i.get("perimeter"))}</p>']
    out.insert(0, _mtbs_html(r))
    out.insert(0, _incident_html(r))
    if i.get('subareas'):
        how = (f'one zone per value of <code>{e(i["subarea_field"])}</code>' if i.get('subarea_field')
               else f'one zone, {e(i.get("subarea_name") or "Sub-area")}')
        out.append(f'<p>Sub-areas ({how}): {layer(i["subareas"])}</p>')
    return ''.join(out)


def _html(r, fields, rows):
    """A plain, self-contained report page."""
    e = html.escape
    t = r['thresholds']

    def thr(v):
        return 'none' if v >= NONE_VALUE else f'{v:.3f} ({v * 1000:.0f})'
    pair = r['pair']
    def cell(n, v):
        if isinstance(v, float):
            return f'{v:,.0f}' if n.startswith('ac_') else f'{v:,.2f}'
        return str(v)
    rows_html = ''.join('<tr>' + ''.join(f'<td>{e(cell(n, row[n]))}</td>' for n in fields) + '</tr>' for row in rows)
    zones = r.get('zones') or {}

    def head(n):
        """Column header: the zone's name instead of the field code (pct_db -> % 416 Fire)."""
        if n == 'ord':
            return '#'
        if n == 'sev_class':
            return 'Class'
        for prefix, label in (('pct_', '%'), ('ha_', 'ha'), ('ac_', 'acres')):
            if n.startswith(prefix):
                return f'{label} {zones.get(n[len(prefix):], n[len(prefix):])}'
        return n
    # 4. the pair's dates, sensors and clear shares, when the pair was suggested
    dates = ''
    if pair.get('pre_date'):
        def n_of(v, unit):
            return f'{v} {unit}' + ('' if str(v) == '1' else 's')
        gap = (f'{n_of(pair["years"], "year")} apart, {n_of(pair["doy_gap"], "day")} apart in the season'
               if pair.get('years') else '')
        def one(k):
            clear = f', {e(str(pair[k + "_clear"]))} % clear inside the area' if pair.get(k + '_clear') else ''
            return f'{e(str(pair[k + "_date"]))} ({e(str(pair[k + "_sensor"]))}{clear})'
        dates = f'<p>Pre-fire {one("pre")}; post-fire {one("post")}{"; " + gap if gap else ""}.</p>'
    # 3. the validation note only when a threshold was estimated
    given = (r.get('inputs') or {}).get('thresholds_given') or {}
    estimated_note = '' if len(given) == 3 else (
        '<p class="note">MTBS\'s own thresholds for this fire, each taken relative to MTBS\'s offset and moved to '
        'this pair\'s: on 17 MTBS fires, MTBS\'s thresholds on our dNBR reproduced MTBS\'s classes at a mean kappa '
        'of 0.78.</p>' if t.get('from_mtbs') else
        (
        '<p class="note">Estimated thresholds are a starting point: on 21 MTBS fires the nearby-fire estimate '
        '(moved to each pair\'s offset) reproduces MTBS\'s classes at a mean kappa of about 0.61, against 0.75 with '
        'MTBS\'s own thresholds. Check them against the imagery (reflectance_post.tif, SWIR2/NIR/red) and field '
        'data.</p>'))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(r["fire"])} burn severity</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;color:#222;background:#fff}}
table{{border-collapse:collapse;margin:.5rem 0 1.5rem}}td,th{{border:1px solid #ccc;padding:.25rem .6rem;text-align:right}}
th{{background:#f3f3f3}}td:first-child,th:first-child{{text-align:left}}h1{{font-size:1.6rem}}h2{{font-size:1.15rem;margin-top:2rem}}
code{{font-size:.85em}}.note{{color:#555}}</style></head><body>
<h1>{e(r["fire"])}: burn severity</h1>
<p>Fire {e(r["fire_start"])} to {e(r["fire_end"] or "(end not recorded)")}, {e(_lower_first(r["assessment"]))}. Wildfire Analysis Toolkit {e(r["fireanalysis_version"])}, {e(r["created"])}.</p>
{_inputs_html(r)}
<h2>Severity by class</h2>
{_chart(fields, rows, zones)}
<table><tr>{"".join(f"<th>{e(head(n))}</th>" for n in fields)}</tr>{rows_html}</table>
{'<h2>Map</h2><p><img src="dnbr6.png" alt="Burn severity classes" style="max-width:100%;border:1px solid #ccc"></p><p class="note">Dark green unburned to low, aquamarine low, yellow moderate, red high, light green increased greenness, grey no data; black line: burned-area boundary' + (' (drafted)' if r.get('perimeter_draft') else '') + '.</p>' if 'dnbr6.png' in r['files'] else ''}
{"<h2>Check</h2><ul>" + "".join(f"<li>{e(w)}</li>" for w in r["warnings"]) + "</ul>" if r["warnings"] else ""}
<h2>Images</h2>
{dates}<p>Pre-fire: {e(", ".join(pair["pre_granules"]))}<br>Post-fire: {e(", ".join(pair["post_granules"]))}<br>
<span class="note">{e(pair["source"])}. {e(r["imagery"])}; calibrated to surface reflectance from the product metadata; cloud, adjacent cloud, shadow, snow and water masked.</span></p>
<h2>dNBR offset and thresholds</h2>
<p>Offset {r["offset"]["value"]:.3f} (standard deviation {r["offset"]["std"]:.3f}, {r["offset"]["pixels"]:,} pixels; {e(r["offset"]["method"])}).</p>
<p>Low {thr(t["low"])}, moderate {thr(t["moderate"])}, high {thr(t["high"])} (raw dNBR, x1000 in brackets).</p>
<ul>{"".join(f"<li>{e(n)}</li>" for n in t["notes"])}</ul>
{(f"<p>Cross-check, {e(_lower_first(t['alternative']['method']))}: low {thr(t['alternative']['low'])}, "
  f"moderate {thr(t['alternative']['moderate'])}, high {thr(t['alternative']['high'])}.</p>")
 if 'alternative' in t and 'low' in t['alternative'] else ''}
{estimated_note}
<h2>Files</h2><p>{"".join(f"<code>{e(n)}</code> " for n in r["files"])}</p>
<p class="note">Full log and inputs: report.json.</p></body></html>'''
