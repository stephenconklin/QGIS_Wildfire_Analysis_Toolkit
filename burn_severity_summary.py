"""
Burn Severity Summary (QGIS Processing script)

Summarizes burn severity for a burned area and any number of sub-areas in one
Processing tool:

  1. Build the zones: the dissolved burned area (zone A) and, optionally, the
     part of it inside each sub-area (zone B, or one zone per value of a name
     field), each tagged with a text field "zone".
  2. Classify a severity index into classes: raw dNBR with the USGS thresholds
     (Key & Benson 2006), or RdNBR / RBR computed from dNBR and pre-fire NBR.
  3. Count pixels per class in each zone (Zonal Histogram, prefix C_).
  4. Build a one-row-per-class summary table (ord, sev_class, pct/ha/ac per
     zone) that drives a layout bar chart.

Install: Processing Toolbox > Scripts button (python icon) >
Add Script to Toolbox..., then pick this file. It appears under
Scripts > Fire Analysis > Burn severity summary.

See README.md for the method and CHANGELOG.md for version history.
"""

import re

from qgis.core import (
    Qgis,
    QgsExpression,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterMatrix,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorDestination,
    QgsProcessingUtils,
)
from qgis.PyQt.QtCore import QMetaType
import processing

VERSION = "1.2.0"

ACRES_PER_HA = 2.4710538

# Preset thresholds per index: (label, table rows (min, max, value), class names).
# Tables use min <= value < max on raw (unscaled) index values. Published
# RdNBR and RBR thresholds are in x1000 units and are divided by 1000 here.
PRESETS = [
    ('dNBR — USGS, Key & Benson (2006)',
     [(-1e10, 0.10, 1), (0.10, 0.27, 2), (0.27, 0.44, 3), (0.44, 0.66, 4), (0.66, 1e10, 5)],
     ['Unburned', 'Low', 'Moderate-low', 'Moderate-high', 'High']),
    ('RdNBR — Miller & Thode (2007)',
     [(-1e10, 0.069, 1), (0.069, 0.315, 2), (0.315, 0.640, 3), (0.640, 1e10, 4)],
     ['Unchanged', 'Low', 'Moderate', 'High']),
    ('RBR — Parks et al. (2014)',
     [(-1e10, 0.035, 1), (0.035, 0.130, 2), (0.130, 0.298, 3), (0.298, 1e10, 4)],
     ['Unchanged', 'Low', 'Moderate', 'High']),
]
DNBR_INDEX, RDNBR_INDEX, RBR_INDEX = 0, 1, 2

# Raster calculator expressions, A = dNBR, B = pre-fire NBR (raw units).
# RdNBR floors |preNBR| at 0.001 so sparse or bare pixels can't divide by ~0.
INDEX_EXPRESSIONS = {
    RDNBR_INDEX: '"A@1" / sqrt((abs("B@1") < 0.001) * 0.001 + (abs("B@1") >= 0.001) * abs("B@1"))',
    RBR_INDEX: '"A@1" / ("B@1" + 1.001)',
}

CODE_RE = re.compile(r'^[A-Za-z0-9_]+$')


# ---- QGIS 3.44+ / 4.x helpers ------------------------------------------------
# Enums are written only in their scoped form: QGIS 4 (Qt6) accepts nothing
# else, and the plugins.qgis.org Qt6 check flags every flat name.

def _field(name, kind):
    """kind: 'int', 'double' or 'string'."""
    length = 50 if kind == 'string' else 0
    t = {'int': QMetaType.Type.Int, 'double': QMetaType.Type.Double,
         'string': QMetaType.Type.QString}[kind]
    return QgsField(name, t, len=length)


def _polygon_type():
    return Qgis.ProcessingSourceType.VectorPolygon


def _any_vector_type():
    return Qgis.ProcessingSourceType.Vector


def _meters():
    return Qgis.DistanceUnit.Meters


def _no_geometry():
    return Qgis.WkbType.NoGeometry


def _advanced(param):
    param.setFlags(param.flags() | Qgis.ProcessingParameterFlag.Advanced)
    return param


def _warn(feedback, text):
    """pushWarning (QGIS >= 3.16) shows in orange; fall back to plain info."""
    getattr(feedback, 'pushWarning', feedback.pushInfo)(text)


def _slug(name, used):
    """Short field-name code for a zone name: lowercase alphanumerics, max 6
    characters so 'pct_' + code fits a 10-character shapefile field name."""
    base = re.sub(r'[^a-z0-9]', '', name.lower())[:6] or 'zone'
    code, n = base, 2
    while code in used:
        suffix = str(n)
        code, n = base[:6 - len(suffix)] + suffix, n + 1
    return code


class BurnSeveritySummary(QgsProcessingAlgorithm):
    DNBR = 'DNBR'
    PRE_NBR = 'PRE_NBR'
    INDEX = 'INDEX'
    BURN_AREA = 'BURN_AREA'
    WATERSHED = 'WATERSHED'
    SUBAREA_FIELD = 'SUBAREA_FIELD'
    ZONE_A_NAME = 'ZONE_A_NAME'
    ZONE_B_NAME = 'ZONE_B_NAME'
    TABLE = 'TABLE'
    CLASS_NAMES = 'CLASS_NAMES'
    ZONE_A_CODE = 'ZONE_A_CODE'
    ZONE_B_CODE = 'ZONE_B_CODE'
    ZONES = 'ZONES'
    SEVERITY = 'SEVERITY'
    HISTOGRAM = 'HISTOGRAM'
    SUMMARY = 'SUMMARY'

    def name(self):
        return 'burnseveritysummary'

    def displayName(self):
        return 'Burn severity summary'

    def group(self):
        return 'Fire Analysis'

    def groupId(self):
        return 'fireanalysis'

    def shortHelpString(self):
        return (
            '<p>Classifies burn severity and summarizes percent, hectares and acres '
            'per class for a burned area (zone A) and, optionally, the burned area '
            'inside each sub-area such as a watershed (zone B, or one zone per value '
            'of a name field).</p>'
            '<p><b>Index.</b> dNBR uses the USGS thresholds (Key &amp; Benson 2006). '
            'RdNBR (Miller &amp; Thode 2007) and RBR (Parks et al. 2014) are computed '
            'from dNBR and the pre-fire NBR raster. Rasters must hold raw values '
            '(dNBR about -0.6 to 1.3, NBR -1 to 1), not 0-1 rescaled or x1000 copies; '
            'the tool stops if the dNBR looks rescaled and the preset thresholds are '
            'in use. The dNBR raster must be in a projected CRS with meter units.</p>'
            '<p><b>Custom thresholds</b> (Advanced): fill in the table (min &lt;= value '
            '&lt; max) and class names to override the preset for the chosen index.</p>'
            '<p><b>Summary fields:</b> ord, sev_class, then pct_, ha_ and ac_ for each '
            'zone, using its code (zone A: db, zone B: ba by default; per-name zones '
            'get a code from their name).</p>'
            f'<p><i>Version {VERSION}</i></p>'
        )

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DNBR, 'Raw dNBR raster'))
        self.addParameter(QgsProcessingParameterEnum(
            self.INDEX, 'Severity index and thresholds',
            options=[p[0] for p in PRESETS], defaultValue=DNBR_INDEX))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.PRE_NBR, 'Pre-fire NBR raster (RdNBR and RBR only)', optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.BURN_AREA, 'Burned-area polygons (zone A)', [_polygon_type()]))
        self.addParameter(QgsProcessingParameterString(
            self.ZONE_A_NAME, 'Zone A name', 'Dragon Bravo'))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.WATERSHED, 'Sub-area polygons, e.g. a watershed (zone B = burned area inside)',
            [_polygon_type()], optional=True))
        self.addParameter(QgsProcessingParameterField(
            self.SUBAREA_FIELD, 'Sub-area name field (one zone per value; leave empty for one zone B)',
            parentLayerParameterName=self.WATERSHED, optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.ZONE_B_NAME, 'Zone B name (when no name field is set)', 'Bright Angel',
            optional=True))

        self.addParameter(_advanced(QgsProcessingParameterMatrix(
            self.TABLE, 'Custom thresholds (min <= value < max; empty = preset for the index)',
            numberRows=0, hasFixedNumberRows=False,
            headers=['Minimum', 'Maximum', 'Value'], optional=True)))
        self.addParameter(_advanced(QgsProcessingParameterString(
            self.CLASS_NAMES, 'Custom class names, comma separated (empty = preset)',
            optional=True)))
        self.addParameter(_advanced(QgsProcessingParameterString(
            self.ZONE_A_CODE, 'Summary field code for zone A', 'db')))
        self.addParameter(_advanced(QgsProcessingParameterString(
            self.ZONE_B_CODE, 'Summary field code for zone B', 'ba')))

        self.addParameter(QgsProcessingParameterVectorDestination(
            self.ZONES, 'Severity zones', _polygon_type()))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.SEVERITY, 'Severity classes'))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.HISTOGRAM, 'Zonal histogram', _polygon_type()))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SUMMARY, 'Severity area summary', _any_vector_type()))

    def _run(self, alg, params, context, feedback):
        return processing.run(alg, params, context=context, feedback=feedback,
                              is_child_algorithm=True)['OUTPUT']

    @staticmethod
    def _int32_option():
        """Index of 'Int32' in reclassifybytable's DATA_TYPE list (3 in QGIS 3.44
        and 4.2), looked up by name so a reordered list can't silently change it."""
        from qgis.core import QgsApplication
        alg = QgsApplication.processingRegistry().algorithmById('native:reclassifybytable')
        options = alg.parameterDefinition('DATA_TYPE').options()
        return options.index('Int32') if 'Int32' in options else 3

    @staticmethod
    def _ensure_crs(path, crs, feedback):
        """Write the dNBR's CRS into the severity raster if it came out without one.
        Seen in a QGIS 4.2.3 GUI session: reclassifying some ArcGIS-exported
        GeoTIFFs wrote no CRS, although the same call headless kept it."""
        from osgeo import gdal, osr
        ds = gdal.Open(path, gdal.GA_Update)
        if ds is None:
            _warn(feedback, 'Could not check the severity raster for a CRS; '
                            'set it in Layer Properties if it is missing.')
            return
        if not ds.GetProjection():
            srs = osr.SpatialReference()
            srs.SetFromUserInput(crs.authid() or crs.toWkt())
            ds.SetProjection(srs.ExportToWkt())
            feedback.pushInfo(f'Severity raster had no CRS; set it to {crs.authid() or "the dNBR CRS"}.')
        ds = None  # close and flush

    # ---- parameter checks ---------------------------------------------------

    def _thresholds(self, parameters, context, index):
        """Return (flat table, class values, class names, is_custom)."""
        table = self.parameterAsMatrix(parameters, self.TABLE, context) or []
        if all(x in (None, '') for x in table):  # an empty matrix comes back as [None]
            table = []
        names_text = self.parameterAsString(parameters, self.CLASS_NAMES, context) or ''
        custom = bool(table)
        if not custom:
            _, rows, names = PRESETS[index]
            table = [str(x) for row in rows for x in row]
        elif len(table) % 3:
            raise QgsProcessingException('Custom threshold table must have 3 columns.')
        values = [int(float(table[i + 2])) for i in range(0, len(table), 3)]
        if names_text.strip():
            names = [s.strip() for s in names_text.split(',')]
        elif custom:
            names = [f'Class {v}' for v in values]
        if len(names) != len(values):
            raise QgsProcessingException(
                f'{len(values)} classes in the threshold table but {len(names)} class names.')
        if len(set(values)) != len(values):
            raise QgsProcessingException('Class values in the threshold table must be unique.')
        return table, values, names, custom

    def _check_range(self, layer, label, lo, hi, strict, feedback, extra=''):
        """Stop (strict) or warn if a raster's values fall outside raw-index range."""
        # Exact stats over the whole band. Passing a stats flag argument triggers a
        # deprecation warning on QGIS 3.40+ whatever its type, so use the defaults.
        stats = layer.dataProvider().bandStatistics(1)
        mn, mx = stats.minimumValue, stats.maximumValue
        feedback.pushInfo(f'{label} range: {mn:.3f} to {mx:.3f}')
        problem = None
        if mn < lo or mx > hi:
            problem = (f'{label} values ({mn:.3f} to {mx:.3f}) are outside the raw range '
                       f'{lo} to {hi}; it looks scaled (for example x1000).')
        elif label == 'dNBR' and mn >= -1e-6 and mx <= 1.0001:  # float32 rounding
            problem = (f'dNBR values ({mn:.3f} to {mx:.3f}) are all between 0 and 1; it looks '
                       'rescaled to 0-1. Raw dNBR normally includes negative values.')
        if problem and strict:
            raise QgsProcessingException(
                problem + ' Use the raw raster.' + extra)
        if problem:
            _warn(feedback, problem)

    # ---- main ---------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        dnbr = self.parameterAsRasterLayer(parameters, self.DNBR, context)
        pre_nbr = self.parameterAsRasterLayer(parameters, self.PRE_NBR, context)
        index = self.parameterAsEnum(parameters, self.INDEX, context)
        name_a = self.parameterAsString(parameters, self.ZONE_A_NAME, context).strip()
        has_sub = parameters.get(self.WATERSHED) not in (None, '')
        sub_field = self.parameterAsString(parameters, self.SUBAREA_FIELD, context) if has_sub else ''
        name_b = (self.parameterAsString(parameters, self.ZONE_B_NAME, context) or '').strip()
        code_a = self.parameterAsString(parameters, self.ZONE_A_CODE, context).strip()
        code_b = self.parameterAsString(parameters, self.ZONE_B_CODE, context).strip()

        table, class_values, class_names, custom = self._thresholds(parameters, context, index)
        if not name_a:
            raise QgsProcessingException('Zone A needs a name.')
        if has_sub and not sub_field and not name_b:
            raise QgsProcessingException('Zone B needs a name (or set a sub-area name field).')
        for code in (code_a, code_b):
            if not CODE_RE.match(code):
                raise QgsProcessingException(
                    f'Field code "{code}" may only contain letters, digits and _.')
        if index != DNBR_INDEX and pre_nbr is None:
            raise QgsProcessingException(f'{PRESETS[index][0]} needs the pre-fire NBR raster.')

        # Pixel area from the raster itself (requires a meter-based CRS)
        if dnbr.crs().mapUnits() != _meters():
            raise QgsProcessingException('dNBR raster must be in a CRS with meter units.')
        pixel_ha = dnbr.rasterUnitsPerPixelX() * dnbr.rasterUnitsPerPixelY() / 10000.0
        feedback.pushInfo(f'Pixel area: {pixel_ha:.4f} ha')

        # Range checks: refuse rescaled rasters when the preset thresholds apply
        override = (' If it really is raw, enter thresholds in the Advanced table '
                    'to skip this check.')
        self._check_range(dnbr, 'dNBR', -2.0, 2.0, not custom, feedback, override)
        if pre_nbr is not None and index != DNBR_INDEX:
            self._check_range(pre_nbr, 'Pre-fire NBR', -1.0001, 1.0001, not custom, feedback, override)

        # --- 1. Build the zones ---
        feedback.pushInfo('Building zones...')
        burn_fixed = self._run('native:fixgeometries',
                               {'INPUT': parameters[self.BURN_AREA], 'OUTPUT': 'memory:'},
                               context, feedback)
        zone_a = self._run('native:dissolve',
                           {'INPUT': burn_fixed, 'FIELD': [], 'OUTPUT': 'memory:'},
                           context, feedback)

        def tag(layer, expr):
            tagged = self._run('native:fieldcalculator', {
                'INPUT': layer, 'FIELD_NAME': 'zone', 'FIELD_TYPE': 2,
                'FIELD_LENGTH': 50, 'FIELD_PRECISION': 0, 'FORMULA': expr,
                'OUTPUT': 'memory:'}, context, feedback)
            return self._run('native:retainfields',
                             {'INPUT': tagged, 'FIELDS': ['zone'], 'OUTPUT': 'memory:'},
                             context, feedback)

        parts = [tag(zone_a, QgsExpression.quotedValue(name_a))]
        if has_sub:
            sub_fixed = self._run('native:fixgeometries',
                                  {'INPUT': parameters[self.WATERSHED], 'OUTPUT': 'memory:'},
                                  context, feedback)
            by = [sub_field] if sub_field else []
            sub_diss = self._run('native:dissolve',
                                 {'INPUT': sub_fixed, 'FIELD': by, 'OUTPUT': 'memory:'},
                                 context, feedback)
            inter = self._run('native:intersection', {
                'INPUT': sub_diss, 'OVERLAY': zone_a, 'INPUT_FIELDS': by,
                'OVERLAY_FIELDS': [], 'OVERLAY_FIELDS_PREFIX': 'ovl_', 'OUTPUT': 'memory:'},
                context, feedback)
            zone_b = self._run('native:dissolve',
                               {'INPUT': inter, 'FIELD': by, 'OUTPUT': 'memory:'},
                               context, feedback)
            if sub_field:
                in_burn = {f[sub_field] for f in
                           QgsProcessingUtils.mapLayerFromString(zone_b, context).getFeatures()}
                missed = sorted(str(f[sub_field]) for f in
                                QgsProcessingUtils.mapLayerFromString(sub_diss, context).getFeatures()
                                if f[sub_field] not in in_burn)
                if missed:
                    _warn(feedback, 'No burned area inside: ' + ', '.join(missed) + '. Skipped.')
            expr = (f'to_string({QgsExpression.quotedColumnRef(sub_field)})' if sub_field
                    else QgsExpression.quotedValue(name_b))
            parts.append(tag(zone_b, expr))

        merged = self._run('native:mergevectorlayers',
                           {'LAYERS': parts, 'CRS': dnbr.crs(), 'OUTPUT': 'memory:'},
                           context, feedback)
        zones = self._run('native:retainfields',
                          {'INPUT': merged, 'FIELDS': ['zone'], 'OUTPUT': parameters[self.ZONES]},
                          context, feedback)
        if feedback.isCanceled():
            return {}

        # Zone order and summary codes: zone A first, then sub-zones in layer order
        zone_layer = QgsProcessingUtils.mapLayerFromString(zones, context)
        zone_names = []
        for f in zone_layer.getFeatures():
            z = f['zone']
            if z in (None, '') or str(z) == 'NULL':
                _warn(feedback, 'Skipping a sub-area with an empty name.')
                continue
            if z in zone_names:
                raise QgsProcessingException(f'Two zones are named "{z}"; zone names must be unique.')
            zone_names.append(z)
        if has_sub and len(zone_names) == 1:
            _warn(feedback, 'No sub-area overlaps the burned area; only zone A is summarized.')
        codes = {name_a: code_a}
        used = {code_a}
        for z in zone_names[1:]:
            code = code_b if not sub_field else _slug(z, used)
            if code in used:
                raise QgsProcessingException(f'Zone codes must differ; "{code}" is used twice.')
            codes[z] = code
            used.add(code)
        feedback.pushInfo('Zones: ' + ', '.join(f'{z} ({codes[z]})' for z in zone_names))

        # --- 2. Severity index and classes ---
        if index == DNBR_INDEX:
            index_raster = dnbr
        else:
            feedback.pushInfo(f'Computing {PRESETS[index][0].split()[0]}...')
            index_raster = self._run('native:modelerrastercalc', {
                'LAYERS': [dnbr, pre_nbr], 'EXPRESSION': INDEX_EXPRESSIONS[index],
                'EXTENT': dnbr.extent(), 'CELL_SIZE': dnbr.rasterUnitsPerPixelX(),
                'CRS': dnbr.crs(), 'OUTPUT': QgsProcessing.TEMPORARY_OUTPUT},
                context, feedback)

        feedback.pushInfo('Classifying...')
        severity = self._run('native:reclassifybytable', {
            'INPUT_RASTER': index_raster, 'RASTER_BAND': 1, 'TABLE': table,
            'NO_DATA': 0, 'NODATA_FOR_MISSING': True,
            'RANGE_BOUNDARIES': 1,  # min <= value < max
            'DATA_TYPE': self._int32_option(),
            'OUTPUT': parameters[self.SEVERITY]}, context, feedback)
        self._ensure_crs(severity, dnbr.crs(), feedback)

        # --- 3. Zonal histogram ---
        feedback.pushInfo('Counting pixels per class...')
        histogram = self._run('native:zonalhistogram', {
            'INPUT_RASTER': severity, 'RASTER_BAND': 1, 'INPUT_VECTOR': zones,
            'COLUMN_PREFIX': 'C_', 'OUTPUT': parameters[self.HISTOGRAM]},
            context, feedback)
        if feedback.isCanceled():
            return {}

        # --- 4. Summary table, one row per class ---
        hist_layer = QgsProcessingUtils.mapLayerFromString(histogram, context)
        counts = {}
        for f in hist_layer.getFeatures():
            if f['zone'] not in codes:
                continue
            row = {}
            for v in class_values:
                idx = f.fields().indexOf(f'C_{v}')
                row[v] = int(f[idx] or 0) if idx >= 0 else 0  # class absent -> 0
            counts[f['zone']] = row

        fields = QgsFields()
        fields.append(_field('ord', 'int'))
        fields.append(_field('sev_class', 'string'))
        for prefix in ('pct', 'ha', 'ac'):
            for z in zone_names:
                fields.append(_field(f'{prefix}_{codes[z]}', 'double'))

        sink, dest_id = self.parameterAsSink(
            parameters, self.SUMMARY, context, fields, _no_geometry())

        totals = {z: sum(counts[z].values()) for z in zone_names}
        for ord_, (v, cname) in enumerate(zip(class_values, class_names), start=1):
            vals = [ord_, cname]
            vals += [100.0 * counts[z][v] / totals[z] if totals[z] else 0.0 for z in zone_names]
            vals += [counts[z][v] * pixel_ha for z in zone_names]
            vals += [float(round(counts[z][v] * pixel_ha * ACRES_PER_HA)) for z in zone_names]
            feat = QgsFeature(fields)
            feat.setAttributes(vals)
            sink.addFeature(feat, QgsFeatureSink.Flag.FastInsert)

        for z in zone_names:
            feedback.pushInfo(
                f'{z}: {totals[z]:,} pixels = {round(totals[z] * pixel_ha * ACRES_PER_HA):,} ac')

        return {self.ZONES: zones, self.SEVERITY: severity,
                self.HISTOGRAM: histogram, self.SUMMARY: dest_id}
