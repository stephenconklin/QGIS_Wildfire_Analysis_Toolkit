"""Build dNBR: pre-fire minus post-fire NBR on the post-fire grid, the dNBR
offset of unburned land, and offset-corrected RdNBR and RBR."""
from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
                       QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingOutputNumber, QgsProcessingParameterBoolean,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination,
                       QgsProcessingParameterRasterLayer)

from .. import VERSION, raster
from ..compat import advanced, double_type, meters, polygon_source, warn

OFFSET_NONE, OFFSET_RING, OFFSET_SAMPLE, OFFSET_FIXED = range(4)
OFFSET_METHODS = ['None (offset 0)',
                  'Median dNBR in a ring outside the burned area',
                  'Median dNBR inside unburned-sample polygons',
                  'Fixed value']

# Offset quality limits, from the FMT3 user guide (50 in x1000 units)
OFFSET_LIMIT = 0.10
STD_LIMIT = 0.05
# A sample pixel above the USGS moderate-low bound is burned, not unburned
BURNED_DNBR = 0.27
BURNED_SHARE_LIMIT = 2.0  # percent


def source_union(source, what):
    """(union geometry, CRS) of a polygon source, in its own CRS."""
    if source is None:
        raise QgsProcessingException(f'The {what} layer could not be loaded.')
    parts = [QgsGeometry(f.geometry()).makeValid() for f in source.getFeatures() if f.hasGeometry()]
    if not parts:
        raise QgsProcessingException(f'The {what} layer has no polygons.')
    return QgsGeometry.unaryUnion(parts), source.sourceCrs()


class BuildDnbr(QgsProcessingAlgorithm):
    PRE_NBR = 'PRE_NBR'
    POST_NBR = 'POST_NBR'
    PERIMETER = 'PERIMETER'
    OFFSET_METHOD = 'OFFSET_METHOD'
    UNBURNED = 'UNBURNED'
    OFFSET_VALUE = 'OFFSET_VALUE'
    RING_GAP = 'RING_GAP'
    RING_WIDTH = 'RING_WIDTH'
    SUBTRACT_OFFSET = 'SUBTRACT_OFFSET'
    DNBR = 'DNBR'
    RDNBR = 'RDNBR'
    RBR = 'RBR'
    OFFSET = 'OFFSET'
    OFFSET_STD = 'OFFSET_STD'
    OFFSET_N = 'OFFSET_N'

    def name(self):
        return 'builddnbr'

    def displayName(self):
        return 'Build dNBR'

    def group(self):
        return 'Severity'

    def groupId(self):
        return 'severity'

    def shortHelpString(self):
        return (
            '<p>Computes dNBR = pre-fire NBR − post-fire NBR on the post-fire grid '
            '(the pre-fire NBR is resampled bilinearly if its grid differs), and from '
            'it RdNBR and RBR. NBR rasters must be in raw units (−1 to 1), such as the '
            'output of <i>Fetch HLS NBR</i>.</p>'
            '<p><b>dNBR offset.</b> Unburned land rarely has a dNBR of exactly 0, because '
            'of phenology, moisture and sun angle differences between the two dates. '
            'The offset is the median dNBR of unburned pixels: by default in a ring '
            'outside the burned area (a gap, then a band of the given width), or inside '
            'polygons you drew over unburned vegetation like the burned vegetation. '
            'Offsets beyond ±0.1 or a spread above 0.05 suggest a poorly matched pair '
            'or a sample that includes burned or different land.</p>'
            '<p>RdNBR = (dNBR − offset) / √|pre-fire NBR| (Miller &amp; Thode 2007; '
            '|NBR| floored at 0.001), RBR = (dNBR − offset) / (pre-fire NBR + 1.001) '
            '(Parks et al. 2014). dNBR is written without the offset unless '
            '<i>Subtract the offset from dNBR</i> is on; the USGS dNBR thresholds were '
            'set on uncorrected dNBR.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.PRE_NBR, 'Pre-fire NBR'))
        self.addParameter(QgsProcessingParameterRasterLayer(self.POST_NBR, 'Post-fire NBR'))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.PERIMETER, 'Burned-area polygons (for the offset ring)', [polygon_source()],
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.OFFSET_METHOD, 'dNBR offset', options=OFFSET_METHODS, defaultValue=OFFSET_RING))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.UNBURNED, 'Unburned-sample polygons (for that offset method)', [polygon_source()],
            optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.OFFSET_VALUE, 'Fixed offset (raw dNBR units, for that offset method)',
            type=double_type(), defaultValue=0.0))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.RING_GAP, 'Ring: gap outside the burned area (m)', type=double_type(),
            minValue=0, defaultValue=90)))
        self.addParameter(advanced(QgsProcessingParameterNumber(
            self.RING_WIDTH, 'Ring: width (m)', type=double_type(), minValue=30, defaultValue=2000)))
        self.addParameter(advanced(QgsProcessingParameterBoolean(
            self.SUBTRACT_OFFSET, 'Subtract the offset from dNBR', defaultValue=False)))
        self.addParameter(QgsProcessingParameterRasterDestination(self.DNBR, 'dNBR'))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.RDNBR, 'RdNBR', optional=True, createByDefault=True))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.RBR, 'RBR', optional=True, createByDefault=True))
        self.addOutput(QgsProcessingOutputNumber(self.OFFSET, 'dNBR offset'))
        self.addOutput(QgsProcessingOutputNumber(self.OFFSET_STD, 'Offset sample standard deviation'))
        self.addOutput(QgsProcessingOutputNumber(self.OFFSET_N, 'Offset sample pixels'))

    @staticmethod
    def _check_nbr(label, a):
        import numpy as np
        valid = a[np.isfinite(a)]
        if valid.size == 0:
            raise QgsProcessingException(f'The {label} NBR has no valid pixels.')
        lo, hi = float(valid.min()), float(valid.max())
        if lo < -1.5 or hi > 1.5:
            raise QgsProcessingException(
                f'{label} NBR values ({lo:.3f} to {hi:.3f}) are outside -1 to 1; it looks scaled '
                '(for example x1000). Use NBR in raw units.')
        return lo, hi

    def processAlgorithm(self, parameters, context, feedback):
        import numpy as np
        from osgeo import gdal
        gdal.UseExceptions()

        pre_layer = self.parameterAsRasterLayer(parameters, self.PRE_NBR, context)
        post_layer = self.parameterAsRasterLayer(parameters, self.POST_NBR, context)
        method = self.parameterAsEnum(parameters, self.OFFSET_METHOD, context)
        if post_layer.crs().mapUnits() != meters():
            raise QgsProcessingException('The post-fire NBR must be in a projected CRS with meter units.')
        if pre_layer.source() == post_layer.source():
            warn(feedback, 'Pre-fire and post-fire NBR are the same raster; dNBR will be 0.')

        post_ds = raster.open_raster(post_layer)
        pre_ds = raster.open_raster(pre_layer)
        post, _ = raster.read_on_grid(post_ds, post_ds)
        pre, same = raster.read_on_grid(pre_ds, post_ds, 'bilinear')
        if not same:
            feedback.pushInfo('Pre-fire NBR is on a different grid; resampled (bilinear) to the post-fire grid.')
        for label, a in (('Pre-fire', pre), ('Post-fire', post)):
            lo, hi = self._check_nbr(label, a)
            feedback.pushInfo(f'{label} NBR range {lo:.3f} to {hi:.3f}')
        dnbr = pre - post
        valid = np.isfinite(dnbr)
        if not valid.any():
            raise QgsProcessingException('The two NBR rasters do not overlap.')
        feedback.setProgress(30)

        # --- offset ---
        offset, std, robust, n = 0.0, 0.0, 0.0, 0
        if method == OFFSET_FIXED:
            offset = self.parameterAsDouble(parameters, self.OFFSET_VALUE, context)
            feedback.pushInfo(f'Fixed dNBR offset: {offset:.4f}')
        elif method in (OFFSET_RING, OFFSET_SAMPLE):
            if method == OFFSET_RING:
                if not parameters.get(self.PERIMETER):
                    raise QgsProcessingException(
                        'The ring offset needs the burned-area polygons; add them or choose '
                        'another offset method.')
                geom, crs = source_union(self.parameterAsSource(parameters, self.PERIMETER, context),
                                         'burned-area')
                inner = raster.rasterize(geom, crs, post_ds, context.transformContext())
                gap = self.parameterAsDouble(parameters, self.RING_GAP, context)
                width = self.parameterAsDouble(parameters, self.RING_WIDTH, context)
                # buffer in the raster CRS so the distances are meters
                rcrs = QgsCoordinateReferenceSystem.fromWkt(post_ds.GetProjection())
                g = QgsGeometry(geom)
                if crs != rcrs:
                    g.transform(QgsCoordinateTransform(crs, rcrs, context.transformContext()))
                ring = g.buffer(gap + width, 16).difference(g.buffer(gap, 16) if gap > 0 else g)
                sample = raster.rasterize(ring, rcrs, post_ds, context.transformContext()) & ~inner
                where = f'ring {gap:.0f}–{gap + width:.0f} m outside the burned area'
            else:
                if not parameters.get(self.UNBURNED):
                    raise QgsProcessingException('That offset method needs unburned-sample polygons.')
                geom, crs = source_union(self.parameterAsSource(parameters, self.UNBURNED, context),
                                         'unburned-sample')
                sample = raster.rasterize(geom, crs, post_ds, context.transformContext())
                where = 'unburned-sample polygons'
            values = dnbr[sample & valid]
            n = int(values.size)
            if n < 30:
                raise QgsProcessingException(
                    f'Only {n} valid pixels in the {where}; too few for an offset. '
                    'Check that the polygons overlap the rasters, or use a wider ring.')
            offset, std = float(np.median(values)), float(np.std(values))
            robust = 1.4826 * float(np.median(np.abs(values - offset)))
            burned = 100.0 * float((values > BURNED_DNBR).mean())
            feedback.pushInfo(f'dNBR offset (median, {where}): {offset:.4f}, '
                              f'standard deviation {std:.4f} (robust {robust:.4f}), {n:,} pixels '
                              f'(x1000: {offset * 1000:.0f} ± {std * 1000:.0f}); '
                              f'{burned:.1f} % of the sample has dNBR > {BURNED_DNBR}')
            if burned > BURNED_SHARE_LIMIT:
                warn(feedback, f'{burned:.1f} % of the offset sample looks burned (dNBR > {BURNED_DNBR}). '
                               'The burned-area polygons may be incomplete or clipped, or another fire '
                               'is nearby. Check them, or draw unburned-sample polygons.')
            if abs(offset) >= OFFSET_LIMIT:
                warn(feedback, f'The offset is beyond ±{OFFSET_LIMIT}: the two dates may differ in '
                               'phenology, moisture or sun angle. Consider another image pair.')
            if std > STD_LIMIT:
                warn(feedback, f'The offset sample spreads widely (standard deviation > {STD_LIMIT}). '
                               'It may include burned pixels, clouds or land unlike the burned area.')
        else:
            feedback.pushInfo('No dNBR offset.')
        feedback.setProgress(60)

        # --- outputs ---
        metadata = {'FIREANALYSIS_VERSION': VERSION, 'DNBR_OFFSET': f'{offset:.6f}',
                    'DNBR_OFFSET_METHOD': OFFSET_METHODS[method],
                    'DNBR_OFFSET_STD': f'{std:.6f}', 'DNBR_OFFSET_ROBUST_STD': f'{robust:.6f}',
                    'DNBR_OFFSET_N': n}
        for prefix, ds in (('PRE', pre_ds), ('POST', post_ds)):
            for key, value in (ds.GetMetadata() or {}).items():
                if key.startswith(('HLS_', 'FMASK_')):
                    metadata[f'{prefix}_{key}'] = value
        subtract = self.parameterAsBoolean(parameters, self.SUBTRACT_OFFSET, context)
        corrected = dnbr - offset
        results = {self.OFFSET: offset, self.OFFSET_STD: std, self.OFFSET_N: n}

        out = self.parameterAsOutputLayer(parameters, self.DNBR, context)
        raster.write(out, [('dNBR' + (' - offset' if subtract else ''), corrected if subtract else dnbr)],
                     post_ds, gdal.GDT_Float32, raster.NODATA,
                     dict(metadata, DNBR_OFFSET_SUBTRACTED='YES' if subtract else 'NO'))
        results[self.DNBR] = out
        vals = (corrected if subtract else dnbr)[valid]
        feedback.pushInfo(f'dNBR range {vals.min():.3f} to {vals.max():.3f}, mean {vals.mean():.3f}')

        if parameters.get(self.RDNBR) not in (None, ''):
            out = self.parameterAsOutputLayer(parameters, self.RDNBR, context)
            rdnbr = corrected / np.sqrt(np.maximum(np.abs(pre), 0.001))
            raster.write(out, [('RdNBR', rdnbr)], post_ds, gdal.GDT_Float32, raster.NODATA, metadata)
            results[self.RDNBR] = out
        if parameters.get(self.RBR) not in (None, ''):
            out = self.parameterAsOutputLayer(parameters, self.RBR, context)
            rbr = corrected / (pre + 1.001)
            raster.write(out, [('RBR', rbr)], post_ds, gdal.GDT_Float32, raster.NODATA, metadata)
            results[self.RBR] = out
        feedback.setProgress(100)
        return results
