"""Classify severity (MTBS): dNBR to the MTBS thematic classes (dnbr6), with
threshold estimates when the analyst gives none."""
import os

from qgis.core import (QgsGeometry, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputNumber,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterString)

from .. import VERSION, mtbs, raster
from ..compat import advanced, double_type, integer_type, polygon_source, warn
from .build_dnbr import source_union

# MTBS dnbr6 codes
OUTSIDE, UNBURNED_LOW, LOW, MODERATE, HIGH, GREENER, NON_MAPPING = range(7)
CLASS_NAMES = {UNBURNED_LOW: 'Unburned to low', LOW: 'Low', MODERATE: 'Moderate', HIGH: 'High',
               GREENER: 'Increased greenness', NON_MAPPING: 'Non-mapping area'}
CLASS_COLORS = {UNBURNED_LOW: '#006400', LOW: '#7fffd4', MODERATE: '#ffff00', HIGH: '#ff0000',
                GREENER: '#7fff00', NON_MAPPING: '#ffffff'}

INITIAL, EXTENDED = 0, 1
ASSESSMENTS = ['Initial (post-fire image the same season)', 'Extended (next growing season)']
# RdNBR high-severity thresholds MTBS uses to place the dNBR high threshold
# (Miller & Thode 2007; FMT3 user guide), raw units
RDNBR_HIGH = {INITIAL: 0.750, EXTENDED: 0.640}
NONE_VALUE = 9.999      # "no such class": MTBS writes 9999 in x1000 units
MIN_HIGH_SHARE = 0.005  # less than this share of RdNBR-high pixels -> no high class
# Offline high estimate: match RdNBR only where the pre-fire vegetation is dense, as MTBS
# analysts confine high severity to the densest cover; try sparser levels if too few pixels
DENSE_PRE_NBR = (0.5, 0.4, 0.3)
MIN_DENSE_PIXELS = 100

# ESTIMATE options; order is API (append only)
NEARBY, OWN_DATA, FIRE_MTBS = 0, 1, 2
ESTIMATES = ['Median thresholds of nearby MTBS fires (online)', "This fire's dNBR and RdNBR (offline)",
             "This fire's own MTBS thresholds when MTBS has mapped it, else nearby MTBS fires"]
MIN_NEARBY_FIRES = 5


def match_threshold(dnbr, target, lo=0.0, hi=1.5, step=0.005):
    """dNBR value t whose pattern {dNBR >= t} best matches the boolean target
    (largest F1 score), the automated form of MTBS's visual RdNBR comparison."""
    import numpy as np
    best_t, best_f1 = None, -1.0
    n_target = int(target.sum())
    if n_target == 0:
        return None, 0.0
    for t in np.arange(lo, hi + step / 2, step):
        pred = dnbr >= t
        tp = int((pred & target).sum())
        n_pred = int(pred.sum())
        f1 = 2.0 * tp / (n_pred + n_target) if n_pred + n_target else 0.0
        if f1 > best_f1:
            best_t, best_f1 = float(t), f1
    return best_t, best_f1


def round5(x):
    """Round a raw threshold to 0.005 (5 in x1000 units), as analysts do."""
    return round(x * 200) / 200


def write_png(classes, like, outline, path, max_size=1600):
    """A PNG of the class raster in the MTBS colours (outside transparent), with the
    burned-area outline in black, for reports. classes: uint8 array on like's grid;
    outline: boolean array (pixels on the perimeter line). Thread-safe (GDAL only)."""
    import numpy as np
    from osgeo import gdal
    rgba = np.zeros(classes.shape + (4,), np.uint8)
    for code, colour in CLASS_COLORS.items():
        c = colour.lstrip('#')
        rgba[classes == code] = [int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), 255]
    rgba[classes == NON_MAPPING] = [200, 200, 200, 255]
    rgba[outline] = [0, 0, 0, 255]
    h, w = classes.shape
    mem = gdal.GetDriverByName('MEM').Create('', w, h, 4, gdal.GDT_Byte)
    for i in range(4):
        mem.GetRasterBand(i + 1).WriteArray(rgba[:, :, i])
    scale = min(1.0, max_size / max(w, h))
    gdal.Translate(path, mem, format='PNG', width=max(1, int(w * scale)), height=max(1, int(h * scale)),
                   resampleAlg='nearest')
    aux = path + '.aux.xml'
    if os.path.exists(aux):
        os.remove(aux)


def style_on_load(context, path):
    """Give path the MTBS colours when Processing loads it (it skips the .qml)."""
    if context.willLoadLayerOnCompletion(path):
        from ..styles import StyleOnLoad, mtbs_classes
        post = StyleOnLoad(mtbs_classes)
        StyleOnLoad.keep = StyleOnLoad.keep[-50:] + [post]
        context.layerToLoadOnCompletionDetails(path).setPostProcessor(post)


def write_style(path):
    """A .qml next to the raster, so QGIS styles it with the MTBS colours on load."""
    items = '\n'.join(
        f'        <paletteEntry value="{v}" color="{CLASS_COLORS[v]}" alpha="255" label="{CLASS_NAMES[v]}"/>'
        for v in sorted(CLASS_NAMES))
    qml = f'''<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.40" styleCategories="Symbology">
  <pipe>
    <rasterrenderer type="paletted" band="1" opacity="1" alphaBand="-1" nodataColor="">
      <colorPalette>
{items}
      </colorPalette>
    </rasterrenderer>
  </pipe>
</qgis>
'''
    with open(os.path.splitext(path)[0] + '.qml', 'w', encoding='utf-8') as f:
        f.write(qml)


class ClassifySeverity(QgsProcessingAlgorithm):
    DNBR = 'DNBR'
    PRE_NBR = 'PRE_NBR'
    PERIMETER = 'PERIMETER'
    ASSESSMENT = 'ASSESSMENT'
    LOW_T = 'LOW_T'
    MODERATE_T = 'MODERATE_T'
    HIGH_T = 'HIGH_T'
    OFFSET = 'OFFSET'
    OFFSET_STD = 'OFFSET_STD'
    GREENNESS_T = 'GREENNESS_T'
    ESTIMATE = 'ESTIMATE'
    RADIUS = 'RADIUS'
    YEARS = 'YEARS'
    MTBS_FIRE = 'MTBS_FIRE'
    OUTPUT = 'OUTPUT'

    def name(self):
        return 'classifyseverity'

    def displayName(self):
        return 'Classify severity (MTBS classes)'

    def group(self):
        return 'Severity'

    def groupId(self):
        return 'severity'

    def shortHelpString(self):
        return (
            '<p>Classifies dNBR (raw units, from <i>Build dNBR</i>) inside the burned area '
            'into the MTBS thematic classes: 1 unburned to low, 2 low, 3 moderate, '
            '4 high, 5 increased greenness, 6 non-mapping (no data); 0 outside the burned '
            'area. Class n starts at its threshold (dNBR ≥ threshold); increased greenness is '
            'dNBR ≤ the greenness threshold.</p>'
            '<p><b>Thresholds</b> are fire-specific choices of an analyst. Leave one empty to '
            'estimate it:</p><ul>'
            '<li><b>This fire\'s own MTBS thresholds</b> (default): when MTBS has mapped the fire (the '
            'burned-area layer is an MTBS perimeter, or the fire is named in <i>MTBS fire</i>), MTBS\'s '
            'published thresholds, taken relative to MTBS\'s offset and moved to this pair\'s. Otherwise '
            'as nearby MTBS fires. With MTBS\'s thresholds our classes agreed with MTBS at a mean kappa of '
            '0.78 on 17 fires.</li>'
            '<li><b>Nearby MTBS fires</b> (default, online): the median low, moderate and high '
            'thresholds of MTBS-mapped wildfires whose burned area is centred within the radius over the '
            'past years (same assessment type when there are at least 5), from the MTBS fire records '
            '(downloaded from USGS and cached in your QGIS profile; refreshed when USGS updates them). Each is taken '
            'relative to that fire\'s MTBS offset and moved to this pair\'s offset (from the dNBR '
            'metadata or the Offset option), so a pair whose unburned land reads 0.1 gets thresholds '
            '0.1 higher. Our dNBR is on the MTBS scale, so their analysts\' choices carry over.</li>'
            '<li><b>This fire\'s data</b> (offline): low = offset + 2 robust standard deviations '
            'of the offset sample; high = the dNBR value whose pattern best matches RdNBR ≥ 750 '
            '(initial) or 640 (extended) where the pre-fire NBR is ≥ 0.5 (then 0.4, 0.3), since '
            'MTBS confines high severity to the densest vegetation; moderate = midway.</li></ul>'
            '<p>Tested on 17 MTBS fires (2022–2024, Southwest), classes from estimated thresholds '
            'agree with MTBS at a mean kappa of about 0.56 (nearby fires) or 0.49 (this fire\'s '
            'data), against 0.78 with MTBS\'s own thresholds. Treat estimates as a starting point '
            'and check them against the imagery and field data. Enter 9.999 to switch a class off.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.DNBR, 'dNBR (raw units)'))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.PRE_NBR, 'Pre-fire NBR (for the high-threshold estimate)', optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PERIMETER, 'Burned-area polygons', [polygon_source()]))
        self.addParameter(QgsProcessingParameterEnum(
            self.ASSESSMENT, 'Assessment', options=ASSESSMENTS, defaultValue=INITIAL))
        for name, label in ((self.LOW_T, 'Low threshold'), (self.MODERATE_T, 'Moderate threshold'),
                            (self.HIGH_T, 'High threshold')):
            self.addParameter(QgsProcessingParameterNumber(
                name, f'{label} (raw dNBR; empty = estimate)', type=double_type(), optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.ESTIMATE, 'Estimate empty thresholds from', options=ESTIMATES, defaultValue=FIRE_MTBS))
        self.addParameter(advanced(QgsProcessingParameterString(
            self.MTBS_FIRE, 'MTBS fire (id, or name and year; empty = from the burned-area layer)', optional=True)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.RADIUS, 'Nearby MTBS fires: radius (km)', type=double_type(), minValue=10, defaultValue=150)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.YEARS, 'Nearby MTBS fires: years back', type=integer_type(), minValue=1, defaultValue=10)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.OFFSET, 'dNBR offset (empty = from the dNBR metadata, else 0)', type=double_type(),
            optional=True)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.OFFSET_STD, 'Offset robust standard deviation (empty = from the dNBR metadata)',
            type=double_type(), optional=True)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.GREENNESS_T, 'Increased-greenness threshold', type=double_type(), defaultValue=-0.150)))
        self.addParameter(QgsProcessingParameterRasterDestination(self.OUTPUT, 'Severity classes (dnbr6)'))
        for name, label in ((self.LOW_T, 'Low threshold used'), (self.MODERATE_T, 'Moderate threshold used'),
                            (self.HIGH_T, 'High threshold used')):
            self.addOutput(QgsProcessingOutputNumber(name, label))

    def _mtbs_record(self, parameters, context, feedback):
        """This fire's MTBS record: named in MTBS_FIRE, else the burned-area layer's own
        MTBS attributes; None when neither (or the records can't be had)."""
        from ..incident import read_mtbs
        text = (self.parameterAsString(parameters, self.MTBS_FIRE, context) or '').strip()
        if text:
            try:
                return mtbs.resolve(mtbs.records(feedback), text)
            except mtbs.MtbsError as e:
                warn(feedback, f'MTBS fire: {e}')
                return None
        return read_mtbs(self.parameterAsSource(parameters, self.PERIMETER, context))

    @staticmethod
    def _from_record(r, offset, given):
        """This fire's MTBS thresholds at this pair's offset (None: not known)."""
        values = mtbs.thresholds(r, offset)
        notes = []
        for key, v, label in zip(mtbs.RECORD_KEYS, values, ('low', 'moderate', 'high')):
            m = r.get(key)
            if v is None:
                notes.append('')
            elif v >= NONE_VALUE:
                notes.append(f'none: MTBS mapped no {label} class for {r["event_id"]}')
            elif offset is None or r.get('dnbr_offst') is None:
                notes.append(f'MTBS {r["event_id"]}: {m:.0f}')
            else:
                notes.append(f'MTBS {r["event_id"]}: {m:.0f} with MTBS offset {r["dnbr_offst"]:.0f} (x1000), '
                             f'at this pair\'s offset {offset:+.3f}')
        values = [None if v is None else (v if v >= NONE_VALUE else round(v, 3)) for v in values]
        return values, notes

    def _number(self, parameters, name, context):
        v = parameters.get(name)
        if v is None or v == '':
            return None
        return self.parameterAsDouble(parameters, name, context)

    def _nearby(self, geom, crs, md, assessment, parameters, context, feedback, offset):
        """Median thresholds of MTBS wildfires near the burned area, each relative to its
        own offset and moved to this pair's offset (offset: on this dNBR's scale)."""
        import datetime
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform
        c = QgsGeometry(geom.centroid())
        c.transform(QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem('EPSG:4326'), context.transformContext()))
        lon, lat = c.asPoint().x(), c.asPoint().y()
        date = md.get('POST_HLS_DATE') or md.get('POST_SOURCE_DATE') or datetime.date.today().isoformat()
        year = int(date[:4])
        radius = self.parameterAsDouble(parameters, self.RADIUS, context)
        years = self.parameterAsInt(parameters, self.YEARS, context)
        fires, where = mtbs.nearby(lon, lat, radius, year - years, year, feedback=feedback)
        kind = 'Extended' if assessment == EXTENDED else 'Initial'
        exclude = {self._record['event_id']} if getattr(self, '_record', None) else set()
        thresholds, counts, n, scope = mtbs.median_thresholds(fires, kind, exclude=exclude, offset=offset)
        feedback.pushInfo(f'{n} MTBS wildfires within {radius:.0f} km, {year - years}-{year} ({scope}; {where}).')
        if n < MIN_NEARBY_FIRES:
            return [None] * 3, [''] * 3
        notes = [f'median of {k} nearby MTBS fires' + ('' if offset is None else
                                                        f', relative to their offsets, at this pair\'s offset {offset:+.3f}')
                 for k in counts]
        return [round5(t) if t is not None else None for t in thresholds], notes

    @staticmethod
    def _own(dnbr, pre, valid, offset, offset_std, assessment, given):
        """Estimates from this fire's dNBR, offset and RdNBR."""
        import numpy as np
        notes = ['', '', '']
        if offset_std is None:
            low = round5(offset + 0.1)
            notes[0] = f'offset + 0.1 (no offset spread known) = {low:.3f}'
        else:
            low = round5(offset + 2 * offset_std)
            notes[0] = f'offset {offset:.3f} + 2 x robust SD {offset_std:.3f} = {low:.3f}'
        if given[0] is not None:
            low = given[0]
        high = given[2]
        if high is None:
            if pre is None:
                raise QgsProcessingException(
                    "Estimating the high threshold from this fire's data needs the pre-fire NBR (for "
                    'RdNBR). Add it, enter the thresholds, or estimate from nearby MTBS fires.')
            rdnbr = (dnbr - offset) / np.sqrt(np.maximum(np.abs(pre), 0.001))
            r_high = RDNBR_HIGH[assessment]
            ok = valid & np.isfinite(rdnbr)
            high = NONE_VALUE
            notes[2] = (f'none: under {100 * MIN_HIGH_SHARE:.1f} % of the densely vegetated burned area '
                        f'has RdNBR >= {r_high * 1000:.0f}')
            for level in DENSE_PRE_NBR:
                sel = ok & (pre >= level)
                if sel.sum() < MIN_DENSE_PIXELS:
                    continue
                target = rdnbr[sel] >= r_high
                if target.mean() < MIN_HIGH_SHARE:
                    continue
                t, f1 = match_threshold(dnbr[sel], target, lo=max(low, 0.0))
                high = round5(t)
                notes[2] = (f'best match (F1 {f1:.2f}) to RdNBR >= {r_high * 1000:.0f} where pre-fire '
                            f'NBR >= {level} = {high:.3f}')
                break
        if given[1] is not None:
            moderate = given[1]
        elif high < NONE_VALUE:
            moderate = round5((low + high) / 2)
            notes[1] = f'midway between low and high = {moderate:.3f}'
        else:
            moderate = NONE_VALUE
            notes[1] = 'none (no high class to place it against); enter one if the fire has moderate severity'
        return [low, moderate, high], notes

    def processAlgorithm(self, parameters, context, feedback):
        import numpy as np
        from osgeo import gdal
        gdal.UseExceptions()

        dnbr_ds = raster.open_raster(self.parameterAsRasterLayer(parameters, self.DNBR, context))
        dnbr, _ = raster.read_on_grid(dnbr_ds, dnbr_ds)
        valid_any = dnbr[np.isfinite(dnbr)]
        if valid_any.size and (valid_any.min() < -3 or valid_any.max() > 3):
            raise QgsProcessingException('dNBR values are outside -3 to 3; it looks scaled (x1000). '
                                         'Use dNBR in raw units.')
        md = dnbr_ds.GetMetadata() or {}
        if md.get('DNBR_OFFSET_SUBTRACTED') == 'YES':
            warn(feedback, 'This dNBR already has the offset subtracted; the MTBS thresholds are '
                           'usually set on uncorrected dNBR.')
        offset = self._number(parameters, self.OFFSET, context)
        offset_known = offset is not None or 'DNBR_OFFSET' in md
        if offset is None:
            offset = float(md.get('DNBR_OFFSET', 0.0))
        offset_std = self._number(parameters, self.OFFSET_STD, context)
        if offset_std is None and float(md.get('DNBR_OFFSET_N', 0)) > 0:
            offset_std = float(md.get('DNBR_OFFSET_ROBUST_STD', md.get('DNBR_OFFSET_STD', 0)))
        assessment = self.parameterAsEnum(parameters, self.ASSESSMENT, context)

        geom, crs = source_union(self.parameterAsSource(parameters, self.PERIMETER, context), 'burned-area')
        inside = raster.rasterize(geom, crs, dnbr_ds, context.transformContext())
        if not inside.any():
            raise QgsProcessingException('The burned-area polygons do not overlap the dNBR.')
        valid = inside & np.isfinite(dnbr)
        if not valid.any():
            raise QgsProcessingException('No valid dNBR inside the burned area.')

        pre = None
        if parameters.get(self.PRE_NBR):
            pre, _ = raster.read_on_grid(
                raster.open_raster(self.parameterAsRasterLayer(parameters, self.PRE_NBR, context)), dnbr_ds)

        # --- thresholds ---
        given = [self._number(parameters, k, context) for k in (self.LOW_T, self.MODERATE_T, self.HIGH_T)]
        estimated, notes = [None] * 3, ['given'] * 3
        self._record = None
        if None in given:
            method = self.parameterAsEnum(parameters, self.ESTIMATE, context)
            pair_offset = (0.0 if md.get('DNBR_OFFSET_SUBTRACTED') == 'YES' else offset) if offset_known else None
            if method == FIRE_MTBS:
                self._record = self._mtbs_record(parameters, context, feedback)
                if self._record is not None:
                    estimated, notes = self._from_record(self._record, pair_offset, given)
                    feedback.pushInfo(f'MTBS record: {mtbs.describe(self._record)}.')
                    if None in [e for e, g in zip(estimated, given) if g is None]:
                        warn(feedback, 'The MTBS record lacks a threshold; using nearby MTBS fires instead.')
                        method = NEARBY
                else:
                    feedback.pushInfo('No MTBS record for this fire (not mapped by MTBS yet, or not recognized); '
                                      'using the median of nearby MTBS fires.')
                    method = NEARBY
            if method == NEARBY:
                try:
                    # thresholds sit on this dNBR: its offset is 0 once subtracted
                    estimated, notes = self._nearby(geom, crs, md, assessment, parameters, context, feedback,
                                                    pair_offset)
                except mtbs.MtbsError as e:
                    warn(feedback, f'{e}. Using this fire\'s own data instead.')
                    method = OWN_DATA
                if method == NEARBY and None in [e for e, g in zip(estimated, given) if g is None]:
                    warn(feedback, 'Too few nearby MTBS fires; using this fire\'s own data instead.')
                    method = OWN_DATA
            if method == OWN_DATA:
                estimated, notes = self._own(dnbr, pre, valid, offset, offset_std, assessment, given)
        low, moderate, high = [g if g is not None else e for g, e in zip(given, estimated)]
        notes = [('given' if g is not None else n) for g, n in zip(given, notes)]
        if moderate > high:  # an estimated moderate above a given or estimated high
            moderate = high
        greenness = self.parameterAsDouble(parameters, self.GREENNESS_T, context)
        if not greenness < low <= moderate <= high:
            raise QgsProcessingException(
                f'Thresholds must increase: greenness {greenness} < low {low} <= moderate {moderate} '
                f'<= high {high}.')
        for key, value, how in zip(('Low', 'Moderate', 'High'), (low, moderate, high), notes):
            shown = 'none' if value >= NONE_VALUE else f'{value:.3f} (x1000: {value * 1000:.0f})'
            feedback.pushInfo(f'{key} threshold: {shown}; {how}')

        # --- classes ---
        classes = np.full(dnbr.shape, OUTSIDE, np.uint8)
        classes[inside] = NON_MAPPING
        classes[valid] = UNBURNED_LOW
        classes[valid & (dnbr >= low)] = LOW
        classes[valid & (dnbr >= moderate)] = MODERATE
        classes[valid & (dnbr >= high)] = HIGH
        classes[valid & (dnbr <= greenness)] = GREENER
        n = int(inside.sum())
        for code in sorted(CLASS_NAMES):
            c = int((classes == code).sum())
            if c:
                feedback.pushInfo(f'  {code} {CLASS_NAMES[code]}: {100.0 * c / n:.2f} % ({c:,} px)')

        out = self.parameterAsOutputLayer(parameters, self.OUTPUT, context)
        metadata = {'FIREANALYSIS_VERSION': VERSION, 'CLASSES': 'MTBS dnbr6',
                    'LOW_T': f'{low:.4f}', 'MODERATE_T': f'{moderate:.4f}', 'HIGH_T': f'{high:.4f}',
                    'GREENNESS_T': f'{greenness:.4f}', 'DNBR_OFFSET': f'{offset:.6f}',
                    'ASSESSMENT': ASSESSMENTS[assessment].split()[0]}
        if self._record is not None:
            metadata['MTBS_EVENT_ID'] = str(self._record['event_id'])
        metadata.update({f'THRESHOLD_NOTE_{k}': v for k, v in zip(('LOW', 'MODERATE', 'HIGH'), notes)})
        metadata.update({k: v for k, v in md.items() if k.startswith(('PRE_', 'POST_'))})
        raster.write(out, [('dnbr6', classes)], dnbr_ds, gdal.GDT_Byte, 255, metadata)
        if os.path.splitext(out)[1].lower() in ('.tif', '.tiff'):
            write_style(out)  # for adding the raster by hand later
        style_on_load(context, out)
        return {self.OUTPUT: out, self.LOW_T: low, self.MODERATE_T: moderate, self.HIGH_T: high}
