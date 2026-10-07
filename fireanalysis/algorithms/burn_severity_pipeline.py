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
                       QgsProcessingOutputRasterLayer, QgsProcessingOutputVectorLayer,
                       QgsProcessingParameterDateTime, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProcessingUtils, QgsVectorLayer)

from qgis.PyQt.QtCore import QDate

from .. import VERSION
from ..incident import fire_details
from .. import styles
from ..compat import advanced, date_type, double_type, integer_type, polygon_source
from .build_dnbr import BuildDnbr, source_union
from .draft_perimeter import DraftPerimeter
from .classify_severity import CLASS_COLORS, ESTIMATES, NEARBY, NONE_VALUE, OWN_DATA, ClassifySeverity, write_png
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
    DRAFT_THRESHOLD = 'DRAFT_THRESHOLD'
    DRAFT_MIN_AREA = 'DRAFT_MIN_AREA'
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
            '<p><b>From a WFIGS perimeter:</b> give one fire from WFIGS Interagency Fire Perimeters '
            '(NIFC; select it and tick Selected features only) and leave the fire name and dates empty: '
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
            self.PERIMETER, 'Burned-area polygons (or a generous outline, when drafted)', [polygon_source()]))
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
            self.ESTIMATE, 'Estimate empty thresholds from', options=ESTIMATES, defaultValue=NEARBY))
        for name, label in ((self.LOW_T, 'Low'), (self.MODERATE_T, 'Moderate'), (self.HIGH_T, 'High')):
            self.addParameter(QgsProcessingParameterNumber(
                name, f'{label} threshold (raw dNBR; empty = estimate)', type=double_type(), optional=True))
        self.addParameter(advanced(QgsProcessingParameterString(
            self.PRE_GRANULES, 'Pre-fire granule or item ids (instead of suggesting a pair)', optional=True)))
        self.addParameter(advanced(QgsProcessingParameterString(
            self.POST_GRANULES, 'Post-fire granule or item ids (instead of suggesting a pair)', optional=True)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.DRAFT_THRESHOLD, 'Drafted burned area: dNBR of at least', type=double_type(), defaultValue=0.05)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.DRAFT_MIN_AREA, 'Drafted burned area: smallest patch kept (ha)', type=double_type(), minValue=0,
            defaultValue=20)))
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

    # ---- main ---------------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsString(parameters, self.OUTPUT_FOLDER, context)
        if folder == QgsProcessing.TEMPORARY_OUTPUT or not folder:
            folder = QgsProcessingUtils.generateTempFilename('burn_severity')
        os.makedirs(folder, exist_ok=True)
        # Name and dates: as given, else from the perimeter's WFIGS record (stops on a layer of many fires)
        fire, start_d, end_d, incident = fire_details(self, parameters, context, feedback, self.PERIMETER,
                                                      self.FIRE_NAME, self.FIRE_START, self.FIRE_END)
        fire = fire or 'Fire'
        start, end = QDate(start_d), QDate(end_d)
        assessment = self.parameterAsEnum(parameters, self.ASSESSMENT, context)
        perimeter = parameters[self.PERIMETER]
        drafted = self.parameterAsEnum(parameters, self.PERIMETER_MODE, context) == DRAFTED
        source_index = self.parameterAsEnum(parameters, self.SOURCE, context)
        source = SOURCES[source_index][0]
        steps = QgsProcessingMultiStepFeedback(7 if drafted else 6, feedback)
        log, report = {}, {'fireanalysis_version': VERSION, 'created': datetime.datetime.now().isoformat(timespec='seconds'),
                           'fire': fire, 'fire_start': start.toString('yyyy-MM-dd'),
                           'fire_end': end.toString('yyyy-MM-dd'), 'assessment': ASSESSMENTS[assessment],
                           'imagery': SOURCES[source_index][1], 'incident': incident}

        # Inputs, so a report says which perimeter and sub-areas it used
        geom, crs = source_union(self.parameterAsSource(parameters, self.PERIMETER, context), 'burned-area')
        given = {k.lower(): self.parameterAsDouble(parameters, k, context)
                 for k in (self.LOW_T, self.MODERATE_T, self.HIGH_T) if parameters.get(k) not in (None, '')}
        report['inputs'] = {
            'perimeter': self._layer_info(parameters, self.PERIMETER, context, geom, crs),
            'subareas': self._layer_info(parameters, self.SUBAREAS, context),
            'subarea_field': self.parameterAsString(parameters, self.SUBAREA_FIELD, context) or None,
            'subarea_name': self.parameterAsString(parameters, self.SUBAREA_NAME, context) or None,
            'estimate': ESTIMATES[self.parameterAsEnum(parameters, self.ESTIMATE, context)],
            'thresholds_given': given,
            'granules_given': bool((self.parameterAsString(parameters, self.PRE_GRANULES, context) or '').strip()),
            'min_clear': self.parameterAsInt(parameters, self.MIN_CLEAR, context),
            'output_folder': folder,
            'perimeter_mode': PERIMETER_MODES[self.parameterAsEnum(parameters, self.PERIMETER_MODE, context)]}

        # 1. Image pair
        pre_g = (self.parameterAsString(parameters, self.PRE_GRANULES, context) or '').strip()
        post_g = (self.parameterAsString(parameters, self.POST_GRANULES, context) or '').strip()
        if bool(pre_g) != bool(post_g):
            raise QgsProcessingException('Give both the pre-fire and the post-fire granule ids, or neither.')
        if not pre_g:
            res = self._child(SuggestImagePairs(), {
                'SOURCE': source_index, 'AOI': perimeter, 'FIRE_START': start, 'FIRE_END': end, 'ASSESSMENT': assessment,
                'MIN_CLEAR': self.parameterAsInt(parameters, self.MIN_CLEAR, context), 'COUNT': 5,
                'OUTPUT': os.path.join(folder, 'image_pairs.csv')}, context, steps, log)
            layer = QgsProcessingUtils.mapLayerFromString(res['OUTPUT'], context)
            # image_pairs.csv comes back with text fields, so compare the rank as a number
            best = next((f for f in layer.getFeatures() if int(float(str(f['rank']))) == 1), None) if layer else None
            if best is None:
                raise QgsProcessingException('No image pair found; see the log, or give the granule ids.')
            pre_g, post_g = str(best['pre_granules']), str(best['post_granules'])
            report['pair'] = {k: best[k] for k in ('pre_date', 'pre_sensor', 'pre_clear', 'post_date', 'post_sensor',
                                                   'post_clear', 'doy_gap', 'years')}
            report['pair']['source'] = 'Suggest image pairs, rank 1 (others in image_pairs.csv)'
        else:
            report['pair'] = {'source': 'ids given'}
        report['pair'].update({'pre_granules': pre_g.split(), 'post_granules': post_g.split()})
        steps.setCurrentStep(1)

        # 2. NBR for both dates
        nbr = {}
        for label, granules in (('pre', pre_g), ('post', post_g)):
            params = {'AOI': perimeter, 'NBR': os.path.join(folder, f'nbr_{label}.tif')}
            if label == 'post':
                params['REFLECTANCE'] = os.path.join(folder, 'reflectance_post.tif')
            if source == 'hls':
                res = self._child(FetchHlsNbr(), dict(params, GRANULES=granules), context, steps, log)
            else:
                res = self._child(FetchPlanetaryNbr(), dict(params, ITEMS=granules, SOURCE=source_index - 1),
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
            res = self._child(DraftPerimeter(), {
                'DNBR': dnbr, 'THRESHOLD': threshold, 'MIN_AREA': min_area, 'HOLE_AREA': DRAFT_HOLE_HA, 'SEED': perimeter, 'CLIP': True,
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
                'file': 'perimeter_draft.gpkg', 'threshold': threshold, 'min_area_ha': min_area,
                'hole_area_ha': DRAFT_HOLE_HA, 'area_ha': info['area_ha'], 'area_ac': info['area_ac'],
                'share_of_outline': 100.0 * info['area_ha'] / outline_ha if outline_ha else None}
            feedback.pushInfo(f'Drafted burned area: {info["area_ha"]:,.0f} ha ({info["area_ac"]:,.0f} ac), '
                              f'{report["perimeter_draft"]["share_of_outline"]:.0f} % of the given outline.')
        steps.setCurrentStep(3)

        # 4. MTBS classes
        params = {'DNBR': dnbr, 'PRE_NBR': nbr['pre'], 'PERIMETER': perimeter, 'ASSESSMENT': assessment,
                  'ESTIMATE': self.parameterAsEnum(parameters, self.ESTIMATE, context),
                  'OUTPUT': os.path.join(folder, 'dnbr6.tif')}
        for k in (self.LOW_T, self.MODERATE_T, self.HIGH_T):
            if parameters.get(k) not in (None, ''):
                params[k] = self.parameterAsDouble(parameters, k, context)
        res = self._child(ClassifySeverity(), params, context, steps, log)
        classes = res['OUTPUT']
        low, moderate, high = res['LOW_T'], res['MODERATE_T'], res['HIGH_T']
        report['thresholds'] = {'low': low, 'moderate': moderate, 'high': high,
                                'notes': [m for m in log.get('classifyseverity', []) if 'threshold:' in m]}
        # Cross-check: the other estimate method, for the report only
        method = params['ESTIMATE']
        if any(parameters.get(k) in (None, '') for k in (self.LOW_T, self.MODERATE_T, self.HIGH_T)):
            other = OWN_DATA if method == NEARBY else NEARBY
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


def _chart(fields, rows):
    """Horizontal bars of percent per class for each zone, in plain HTML."""
    e = html.escape
    zones = [n for n in fields if n.startswith('pct_')]
    colours = {'Unburned to low': CLASS_COLORS[1], 'Low': CLASS_COLORS[2], 'Moderate': CLASS_COLORS[3],
               'High': CLASS_COLORS[4]}
    out = ['<div style="margin:.5rem 0 1rem">']
    for z in zones:
        out.append(f'<div style="margin:.4rem 0 .2rem"><b>{e(z[4:])}</b></div>')
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
               f'{d["share_of_outline"]:.0f} % of the outline; dNBR at least {d["threshold"]:g}, patches of '
               f'{d["min_area_ha"]:g} ha or more, holes up to {d["hole_area_ha"]:g} ha filled '
               f'(<code>{e(d["file"])}</code>). '
               '<span class="note">A draft: edit it against the imagery and rerun with it as given for a final '
               'assessment.</span></p>'
               f'<p>Outline (imagery window and offset ring): {layer(i.get("perimeter"))}</p>']
    else:
        out = [f'<h2>Inputs</h2><p>Burned area: {layer(i.get("perimeter"))}</p>']
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
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(r["fire"])} burn severity</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:52rem;margin:2rem auto;padding:0 1rem;color:#222;background:#fff}}
table{{border-collapse:collapse;margin:.5rem 0 1.5rem}}td,th{{border:1px solid #ccc;padding:.25rem .6rem;text-align:right}}
th{{background:#f3f3f3}}td:first-child,th:first-child{{text-align:left}}h1{{font-size:1.6rem}}h2{{font-size:1.15rem;margin-top:2rem}}
code{{font-size:.85em}}.note{{color:#555}}</style></head><body>
<h1>{e(r["fire"])}: burn severity</h1>
<p>Fire {e(r["fire_start"])} to {e(r["fire_end"])}, {e(_lower_first(r["assessment"]))}. Wildfire Analysis Toolkit {e(r["fireanalysis_version"])}, {e(r["created"])}.</p>
{_inputs_html(r)}
<h2>Severity by class</h2>
{_chart(fields, rows)}
<table><tr>{"".join(f"<th>{e(n)}</th>" for n in fields)}</tr>{rows_html}</table>
{'<h2>Map</h2><p><img src="dnbr6.png" alt="Burn severity classes" style="max-width:100%;border:1px solid #ccc"></p><p class="note">Dark green unburned to low, aquamarine low, yellow moderate, red high, light green increased greenness, grey no data; black line: burned-area boundary' + (' (drafted)' if r.get('perimeter_draft') else '') + '.</p>' if 'dnbr6.png' in r['files'] else ''}
{"<h2>Check</h2><ul>" + "".join(f"<li>{e(w)}</li>" for w in r["warnings"]) + "</ul>" if r["warnings"] else ""}
<h2>Images</h2>
<p>Pre-fire: {e(", ".join(pair["pre_granules"]))}<br>Post-fire: {e(", ".join(pair["post_granules"]))}<br>
<span class="note">{e(pair["source"])}. {e(r["imagery"])}; calibrated to surface reflectance from the product metadata; cloud, adjacent cloud, shadow, snow and water masked.</span></p>
<h2>dNBR offset and thresholds</h2>
<p>Offset {r["offset"]["value"]:.3f} (standard deviation {r["offset"]["std"]:.3f}, {r["offset"]["pixels"]:,} pixels; {e(r["offset"]["method"])}).</p>
<p>Low {thr(t["low"])}, moderate {thr(t["moderate"])}, high {thr(t["high"])} (raw dNBR, x1000 in brackets).</p>
<ul>{"".join(f"<li>{e(n)}</li>" for n in t["notes"])}</ul>
{(f"<p>Cross-check, {e(_lower_first(t['alternative']['method']))}: low {thr(t['alternative']['low'])}, "
  f"moderate {thr(t['alternative']['moderate'])}, high {thr(t['alternative']['high'])}.</p>")
 if 'alternative' in t and 'low' in t['alternative'] else ''}
<p class="note">Estimated thresholds are a starting point: on 17 MTBS fires they reproduce MTBS's classes at kappa about 0.56,
against 0.78 with MTBS's own thresholds. Check them against the imagery (reflectance_post.tif, SWIR2/NIR/red) and field data.</p>
<h2>Files</h2><p>{"".join(f"<code>{e(n)}</code> " for n in r["files"])}</p>
<p class="note">Full log and inputs: report.json.</p></body></html>'''
