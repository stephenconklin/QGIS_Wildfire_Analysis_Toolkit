"""
Burn Severity Summary (QGIS Processing script)

Summarizes dNBR burn severity for two zones in one Processing tool:

  1. Build two zones: the dissolved burned area (zone A) and the part of it
     inside a watershed (zone B), each tagged with a text field "zone".
  2. Reclassify raw dNBR into USGS (Key & Benson 2006) severity classes 1-5.
  3. Count pixels per class in each zone (Zonal Histogram, prefix C_).
  4. Build a one-row-per-class summary table (ord, sev_class, pct/ha/ac per
     zone) that drives the layout bar chart.

Install: Processing Toolbox > Scripts button (python icon) >
Add Script to Toolbox..., then pick this file. It appears under
Scripts > Fire Analysis > Burn severity summary.

See README.md for the method and CHANGELOG.md for version history.
"""

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterMatrix,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorDestination,
    QgsProcessingUtils,
    QgsUnitTypes,
    QgsWkbTypes,
)
import processing

VERSION = "1.0.0"

ACRES_PER_HA = 2.4710538

# Key & Benson (2006) USGS dNBR thresholds: min, max, class value
# (min <= value < max). Shown as the default reclassification table.
DEFAULT_TABLE = [
    '-1e10', '0.10', '1',
    '0.10', '0.27', '2',
    '0.27', '0.44', '3',
    '0.44', '0.66', '4',
    '0.66', '1e10', '5',
]
DEFAULT_CLASS_NAMES = 'Unburned,Low,Moderate-low,Moderate-high,High'


# ---- QGIS 3.x / 4.x compatibility -------------------------------------------
# Prefer the newer enum names (QGIS >= 3.30/3.38) and fall back to the old ones.

def _try(new, old):
    try:
        return new()
    except AttributeError:
        return old()


def _field(name, kind):
    """kind: 'int', 'double' or 'string'. QGIS >= 3.38 wants QMetaType, older QVariant."""
    try:
        from qgis.PyQt.QtCore import QMetaType
        t = {'int': QMetaType.Type.Int, 'double': QMetaType.Type.Double,
             'string': QMetaType.Type.QString}[kind]
        return QgsField(name, t, len=50 if kind == 'string' else 0)
    except (AttributeError, TypeError):
        from qgis.PyQt.QtCore import QVariant
        t = {'int': QVariant.Int, 'double': QVariant.Double, 'string': QVariant.String}[kind]
        return QgsField(name, t, len=50 if kind == 'string' else 0)


def _polygon_type():
    return _try(lambda: Qgis.ProcessingSourceType.VectorPolygon,
                lambda: _polygon_type())


def _any_vector_type():
    return _try(lambda: Qgis.ProcessingSourceType.Vector,
                lambda: QgsProcessing.TypeVector)


def _meters():
    return _try(lambda: Qgis.DistanceUnit.Meters, lambda: QgsUnitTypes.DistanceMeters)


def _no_geometry():
    return _try(lambda: Qgis.WkbType.NoGeometry, lambda: _no_geometry())


class BurnSeveritySummary(QgsProcessingAlgorithm):
    DNBR = 'DNBR'
    BURN_AREA = 'BURN_AREA'
    WATERSHED = 'WATERSHED'
    ZONE_A_NAME = 'ZONE_A_NAME'
    ZONE_B_NAME = 'ZONE_B_NAME'
    TABLE = 'TABLE'
    CLASS_NAMES = 'CLASS_NAMES'
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
            'Classifies raw (unscaled) dNBR into USGS burn severity classes and '
            'summarizes percent, hectares and acres per class for two zones: '
            'the full burned area (zone A) and the burned area inside a '
            'watershed (zone B).\n\n'
            'Use the raw dNBR raster (about -0.6 to 0.8), not a 0-1 rescaled '
            'copy; the thresholds only apply to raw values. All inputs should '
            'share a projected CRS in meters.\n\n'
            'Summary fields: ord, sev_class, pct_db/pct_ba, ha_db/ha_ba, '
            'ac_db/ac_ba (db = zone A, ba = zone B).\n\n'
            f'Version {VERSION}'
        )

    def createInstance(self):
        return BurnSeveritySummary()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DNBR, 'Raw dNBR raster'))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.BURN_AREA, 'Burned-area polygons (zone A)',
            [_polygon_type()]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.WATERSHED, 'Watershed boundary (zone B = burned area inside it)',
            [_polygon_type()]))
        self.addParameter(QgsProcessingParameterString(
            self.ZONE_A_NAME, 'Zone A name', 'Dragon Bravo'))
        self.addParameter(QgsProcessingParameterString(
            self.ZONE_B_NAME, 'Zone B name', 'Bright Angel'))
        self.addParameter(QgsProcessingParameterMatrix(
            self.TABLE, 'Severity thresholds (min <= value < max)',
            numberRows=5, hasFixedNumberRows=False,
            headers=['Minimum', 'Maximum', 'Value'],
            defaultValue=DEFAULT_TABLE))
        self.addParameter(QgsProcessingParameterString(
            self.CLASS_NAMES, 'Class names, in class-value order (comma separated)',
            DEFAULT_CLASS_NAMES))

        self.addParameter(QgsProcessingParameterVectorDestination(
            self.ZONES, 'Severity zones', _polygon_type()))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.SEVERITY, 'Severity classes (USGS)'))
        self.addParameter(QgsProcessingParameterVectorDestination(
            self.HISTOGRAM, 'Zonal histogram', _polygon_type()))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.SUMMARY, 'Severity area summary', _any_vector_type()))

    def _run(self, alg, params, context, feedback):
        return processing.run(alg, params, context=context, feedback=feedback,
                              is_child_algorithm=True)['OUTPUT']

    def processAlgorithm(self, parameters, context, feedback):
        dnbr = self.parameterAsRasterLayer(parameters, self.DNBR, context)
        name_a = self.parameterAsString(parameters, self.ZONE_A_NAME, context)
        name_b = self.parameterAsString(parameters, self.ZONE_B_NAME, context)
        table = self.parameterAsMatrix(parameters, self.TABLE, context)
        class_names = [s.strip() for s in
                       self.parameterAsString(parameters, self.CLASS_NAMES, context).split(',')]

        if len(table) % 3:
            raise QgsProcessingException('Threshold table must have 3 columns.')
        class_values = [int(float(table[i + 2])) for i in range(0, len(table), 3)]
        if len(class_names) != len(class_values):
            raise QgsProcessingException(
                f'{len(class_values)} classes in the table but '
                f'{len(class_names)} class names.')

        # Pixel area from the raster itself (requires a meter-based CRS)
        if dnbr.crs().mapUnits() != _meters():
            raise QgsProcessingException('dNBR raster must be in a CRS with meter units.')
        pixel_ha = dnbr.rasterUnitsPerPixelX() * dnbr.rasterUnitsPerPixelY() / 10000.0
        feedback.pushInfo(f'Pixel area: {pixel_ha:.4f} ha')

        # --- 3.1 Build the two zones ---
        feedback.pushInfo('Building zones...')
        burn_fixed = self._run('native:fixgeometries',
                               {'INPUT': parameters[self.BURN_AREA], 'OUTPUT': 'memory:'},
                               context, feedback)
        ws_fixed = self._run('native:fixgeometries',
                             {'INPUT': parameters[self.WATERSHED], 'OUTPUT': 'memory:'},
                             context, feedback)
        zone_a = self._run('native:dissolve',
                           {'INPUT': burn_fixed, 'FIELD': [], 'OUTPUT': 'memory:'},
                           context, feedback)
        inter = self._run('native:intersection',
                          {'INPUT': zone_a, 'OVERLAY': ws_fixed, 'OUTPUT': 'memory:'},
                          context, feedback)
        zone_b = self._run('native:dissolve',
                           {'INPUT': inter, 'FIELD': [], 'OUTPUT': 'memory:'},
                           context, feedback)

        tagged = []
        for layer, zname in ((zone_a, name_a), (zone_b, name_b)):
            expr = "'" + zname.replace("'", "''") + "'"
            tagged.append(self._run('native:fieldcalculator', {
                'INPUT': layer, 'FIELD_NAME': 'zone', 'FIELD_TYPE': 2,
                'FIELD_LENGTH': 50, 'FIELD_PRECISION': 0, 'FORMULA': expr,
                'OUTPUT': 'memory:'}, context, feedback))

        merged = self._run('native:mergevectorlayers',
                           {'LAYERS': tagged, 'CRS': dnbr.crs(), 'OUTPUT': 'memory:'},
                           context, feedback)
        zones = self._run('native:retainfields',
                          {'INPUT': merged, 'FIELDS': ['zone'],
                           'OUTPUT': parameters[self.ZONES]},
                          context, feedback)
        if feedback.isCanceled():
            return {}

        # --- 3.2 Reclassify dNBR ---
        feedback.pushInfo('Classifying dNBR...')
        severity = self._run('native:reclassifybytable', {
            'INPUT_RASTER': dnbr, 'RASTER_BAND': 1, 'TABLE': table,
            'NO_DATA': 0, 'NODATA_FOR_MISSING': True,
            'RANGE_BOUNDARIES': 1,  # min <= value < max
            'DATA_TYPE': 5,  # Int32
            'OUTPUT': parameters[self.SEVERITY]}, context, feedback)

        # --- 3.3 Zonal histogram ---
        feedback.pushInfo('Counting pixels per class...')
        histogram = self._run('native:zonalhistogram', {
            'INPUT_RASTER': severity, 'RASTER_BAND': 1, 'INPUT_VECTOR': zones,
            'COLUMN_PREFIX': 'C_', 'OUTPUT': parameters[self.HISTOGRAM]},
            context, feedback)
        if feedback.isCanceled():
            return {}

        # --- 3.4 Summary table, one row per class ---
        hist_layer = QgsProcessingUtils.mapLayerFromString(histogram, context)
        counts = {}
        for f in hist_layer.getFeatures():
            row = {}
            for v in class_values:
                idx = f.fields().indexOf(f'C_{v}')
                row[v] = int(f[idx] or 0) if idx >= 0 else 0  # class absent -> 0
            counts[f['zone']] = row
        for zname in (name_a, name_b):
            if zname not in counts:
                raise QgsProcessingException(f'Zone "{zname}" missing from histogram.')

        fields = QgsFields()
        fields.append(_field('ord', 'int'))
        fields.append(_field('sev_class', 'string'))
        for prefix in ('pct', 'ha', 'ac'):
            for suffix in ('db', 'ba'):
                fields.append(_field(f'{prefix}_{suffix}', 'double'))

        sink, dest_id = self.parameterAsSink(
            parameters, self.SUMMARY, context, fields, _no_geometry())

        totals = {z: sum(counts[z].values()) for z in (name_a, name_b)}
        for ord_, (v, cname) in enumerate(zip(class_values, class_names), start=1):
            vals = [ord_, cname]
            ca, cb = counts[name_a][v], counts[name_b][v]
            vals += [100.0 * ca / totals[name_a] if totals[name_a] else 0.0,
                     100.0 * cb / totals[name_b] if totals[name_b] else 0.0]
            vals += [ca * pixel_ha, cb * pixel_ha]
            vals += [float(round(ca * pixel_ha * ACRES_PER_HA)),
                     float(round(cb * pixel_ha * ACRES_PER_HA))]
            feat = QgsFeature(fields)
            feat.setAttributes(vals)
            sink.addFeature(feat, QgsFeatureSink.FastInsert)

        for zname in (name_a, name_b):
            feedback.pushInfo(
                f'{zname}: {totals[zname]:,} pixels = '
                f'{round(totals[zname] * pixel_ha * ACRES_PER_HA):,} ac')

        return {self.ZONES: zones, self.SEVERITY: severity,
                self.HISTOGRAM: histogram, self.SUMMARY: dest_id}
