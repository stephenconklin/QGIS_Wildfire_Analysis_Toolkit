"""Burn severity (whole pipeline): one algorithm from fire to report. Image pair ->
pre/post NBR -> dNBR, RdNBR and offset -> MTBS classes -> area summary ->
report, all written to one folder."""
import csv
import datetime
import html
import importlib.util
import json
import os

from qgis.core import (QgsProcessing, QgsProcessingAlgorithm, QgsProcessingContext, QgsProcessingException,
                       QgsProcessingMultiStepFeedback, QgsProcessingOutputFile, QgsProcessingOutputFolder,
                       QgsProcessingOutputRasterLayer, QgsProcessingOutputVectorLayer,
                       QgsProcessingParameterDateTime, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterString, QgsProcessingUtils, QgsProject, QgsVectorLayer)

from .. import VERSION
from ..compat import advanced, date_type, double_type, integer_type, polygon_source
from .build_dnbr import BuildDnbr
from .classify_severity import CLASS_COLORS, ESTIMATES, NEARBY, NONE_VALUE, OWN_DATA, ClassifySeverity, write_png
from .fetch_hls_nbr import FetchHlsNbr
from .planetary_algorithms import FetchPlanetaryNbr
from .suggest_image_pairs import ASSESSMENTS, INITIAL, SOURCES, SuggestImagePairs

CLASSES = ['Unburned to low', 'Low', 'Moderate', 'High']


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
            '<li><i>Classify severity (MTBS classes)</i> with your or estimated thresholds;</li>'
            '<li><i>Burn severity summary</i>: percent, hectares and acres per class for the fire and '
            'its sub-areas;</li>'
            '<li>a report (report.html, report.json) of every input, scene, calibration, offset, '
            'threshold and result.</li></ol>'
            '<p><b>Imagery:</b> NASA HLS (default; from 2013; needs the Earthdata login in ~/.netrc), or '
            'Landsat 4–9 Collection 2 (from 1982) or Sentinel-2 L2A through Planetary Computer (no '
            'account). Everything is written to the output folder. Choosing the pair reads the quality '
            'band of every candidate date, about a minute or two.</p>'
            '<p>Validated against 17 MTBS fires: with MTBS\'s thresholds the classes agree with MTBS '
            'at kappa 0.78; estimated thresholds are a starting point (kappa about 0.56). See the '
            'README.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PERIMETER, 'Burned-area polygons', [polygon_source()]))
        self.addParameter(QgsProcessingParameterEnum(
            self.SOURCE, 'Imagery', options=[s[1] for s in SOURCES], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(self.FIRE_NAME, 'Fire name', 'Fire'))
        self.addParameter(QgsProcessingParameterDateTime(self.FIRE_START, 'Fire start (ignition) date',
                                                         type=date_type()))
        self.addParameter(QgsProcessingParameterDateTime(self.FIRE_END, 'Fire end (containment) date',
                                                         type=date_type()))
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
            self.MIN_CLEAR, 'Minimum clear share inside the area for a suggested pair (%)', type=integer_type(),
            minValue=0, maxValue=100, defaultValue=95)))
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
        fire = self.parameterAsString(parameters, self.FIRE_NAME, context).strip() or 'Fire'
        start = self.parameterAsDateTime(parameters, self.FIRE_START, context).date()
        end = self.parameterAsDateTime(parameters, self.FIRE_END, context).date()
        assessment = self.parameterAsEnum(parameters, self.ASSESSMENT, context)
        perimeter = parameters[self.PERIMETER]
        source_index = self.parameterAsEnum(parameters, self.SOURCE, context)
        source = SOURCES[source_index][0]
        steps = QgsProcessingMultiStepFeedback(6, feedback)
        log, report = {}, {'fireanalysis_version': VERSION, 'created': datetime.datetime.now().isoformat(timespec='seconds'),
                           'fire': fire, 'fire_start': start.toString('yyyy-MM-dd'),
                           'fire_end': end.toString('yyyy-MM-dd'), 'assessment': ASSESSMENTS[assessment],
                           'imagery': SOURCES[source_index][1]}

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
                            'method': 'median dNBR in a ring 90-2090 m outside the burned area'}
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
            from .build_dnbr import source_union
            geom, crs = source_union(self.parameterAsSource(parameters, self.PERIMETER, context), 'burned-area')
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

        for path, name in ((classes, f'{fire} severity classes (MTBS)'), (dnbr, f'{fire} dNBR')):
            context.addLayerToLoadOnCompletion(
                path, QgsProcessingContext.LayerDetails(name, context.project() or QgsProject.instance(), name))
        return {self.FOLDER: folder, self.DNBR: dnbr, self.CLASSES: classes, self.SUMMARY: summary,
                self.REPORT: html_path}


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
<p>Fire {e(r["fire_start"])} to {e(r["fire_end"])}, {e(_lower_first(r["assessment"]))}. Fire Analysis {e(r["fireanalysis_version"])}, {e(r["created"])}.</p>
<h2>Severity by class</h2>
{_chart(fields, rows)}
<table><tr>{"".join(f"<th>{e(n)}</th>" for n in fields)}</tr>{rows_html}</table>
{'<h2>Map</h2><p><img src="dnbr6.png" alt="Burn severity classes" style="max-width:100%;border:1px solid #ccc"></p><p class="note">Dark green unburned to low, aquamarine low, yellow moderate, red high, light green increased greenness, grey no data; black line: burned-area boundary.</p>' if 'dnbr6.png' in r['files'] else ''}
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
<p class="note">Full log and parameters: report.json.</p></body></html>'''
