"""Fetch HLS NBR: read the AOI window of HLS granules from NASA Earthdata,
mosaic them on a 30 m grid, mask with Fmask and compute NBR."""
import math
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry,
                       QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterCrs,
                       QgsProcessingParameterEnum, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterDestination, QgsRasterFileWriter)

from .. import VERSION, earthdata, hls
from ..compat import advanced, double_type, polygon_source, warn
from .find_hls_scenes import aoi_geometry


def utm_crs(lon, lat):
    zone = min(int((lon + 180) // 6) + 1, 60)
    return QgsCoordinateReferenceSystem(f'EPSG:{(32600 if lat >= 0 else 32700) + zone}')


def tile_epsg(tile):
    """MGRS tile '12SUF' -> 32612. Latitude bands N and later are northern."""
    return (32600 if tile[2] >= 'N' else 32700) + int(tile[:2])


class FetchHlsNbr(QgsProcessingAlgorithm):
    AOI = 'AOI'
    SCENES = 'SCENES'
    BUFFER = 'BUFFER'
    CRS = 'CRS'
    MASK = 'MASK'
    NETRC = 'NETRC'
    NBR = 'NBR'
    REFLECTANCE = 'REFLECTANCE'
    FMASK = 'FMASK'

    def name(self):
        return 'fetchhlsnbr'

    def displayName(self):
        return 'Fetch HLS NBR'

    def group(self):
        return 'Imagery'

    def groupId(self):
        return 'imagery'

    def shortHelpString(self):
        return (
            '<p>Reads the area of interest (plus a buffer) from HLS v2.0 granules on '
            'NASA Earthdata, mosaics them on a 30 m grid, masks clouds and other '
            'Fmask classes, and writes NBR in raw units (-1 to 1). Only the window '
            'is read, not whole tiles.</p>'
            '<p><b>Scenes:</b> the output of <i>Find HLS scenes</i>, or a selection '
            'from it. All scenes must share one date and one sensor; select every tile '
            'of that date that the AOI touches.</p>'
            '<p><b>Grid:</b> 30 m, extent snapped to multiples of 30 m in the target '
            'CRS (default: the UTM zone of the AOI). Runs with the same AOI, buffer and '
            'CRS give identical grids, so a pre-fire and a post-fire NBR line up for '
            'dNBR.</p>'
            '<p><b>Login:</b> needs a NASA Earthdata account in ~/.netrc:<br>'
            '<code>machine urs.earthdata.nasa.gov login USER password PASS</code></p>'
            '<p>Fmask can mistake fresh burn scars for cloud shadow or water; the log '
            'reports how much of the area each class removed.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.AOI, 'Area of interest', [polygon_source()]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.SCENES, 'HLS scenes (from Find HLS scenes; one date)', [polygon_source()]))
        self.addParameter(QgsProcessingParameterNumber(
            self.BUFFER, 'Buffer around the AOI (m)', type=double_type(),
            minValue=0, defaultValue=3000))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, 'Target CRS (empty = UTM zone of the AOI)', optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            self.MASK, 'Fmask classes to mask', options=[c[1] for c in hls.FMASK_CLASSES],
            allowMultiple=True,
            defaultValue=[i for i, c in enumerate(hls.FMASK_CLASSES) if c[3]]))
        self.addParameter(advanced(QgsProcessingParameterFile(
            self.NETRC, 'netrc file with the Earthdata login (empty = ~/.netrc)', optional=True)))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.NBR, 'NBR'))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.REFLECTANCE, 'Reflectance SWIR2 / NIR / red (for display)',
            optional=True, createByDefault=False))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.FMASK, 'Fmask', optional=True, createByDefault=False))

    # ---- helpers --------------------------------------------------------------

    def _granules(self, parameters, context):
        source = self.parameterAsSource(parameters, self.SCENES, context)
        if source is None:
            raise QgsProcessingException('The scenes layer could not be loaded.')
        if source.fields().indexOf('granule') < 0:
            raise QgsProcessingException(
                'The scenes layer has no "granule" field; use the output of Find HLS scenes.')
        granules = sorted({str(f['granule']) for f in source.getFeatures() if f['granule']})
        if not granules:
            raise QgsProcessingException('No scenes given; select at least one.')
        parsed = []
        for g in granules:
            try:
                parsed.append((g,) + hls.parse_granule(g))
            except ValueError as e:
                raise QgsProcessingException(str(e)) from None
        dates = sorted({p[3] for p in parsed})
        sensors = sorted({p[1] for p in parsed})
        if len(dates) > 1 or len(sensors) > 1:
            raise QgsProcessingException(
                'All scenes must share one date and one sensor; got '
                + ', '.join(f'{s} {d}' for s, d in sorted({(p[1], p[3].isoformat()) for p in parsed}))
                + '. Select the scenes of one date.')
        return parsed, sensors[0], dates[0]

    @staticmethod
    def _warp(urls, bounds, crs_wkt, resample, nodata, out_type, feedback):
        from osgeo import gdal

        def progress(_pct, _msg, _data):
            return 0 if feedback.isCanceled() else 1

        try:
            ds = gdal.Warp('', urls, format='MEM', outputBounds=bounds, xRes=hls.PIXEL,
                           yRes=hls.PIXEL, dstSRS=crs_wkt, resampleAlg=resample,
                           srcNodata=nodata, dstNodata=nodata, outputType=out_type,
                           multithread=True, callback=progress)
        except RuntimeError as e:
            if feedback.isCanceled():
                return None
            raise QgsProcessingException(f'{e}\n{earthdata.open_error_hint(urls[0])}') from None
        if ds is None and not feedback.isCanceled():
            raise QgsProcessingException(earthdata.open_error_hint(urls[0]))
        return ds

    @staticmethod
    def _write(path, arrays, like, gdal_type, nodata, metadata):
        from osgeo import gdal
        ext = os.path.splitext(path)[1].lstrip('.').lower()
        driver_name = QgsRasterFileWriter.driverForExtension(ext) if ext else 'GTiff'
        driver = gdal.GetDriverByName(driver_name or 'GTiff')
        options = ['COMPRESS=DEFLATE', 'TILED=YES'] if driver.ShortName == 'GTiff' else []
        out = driver.Create(path, like.RasterXSize, like.RasterYSize, len(arrays), gdal_type,
                            options=options)
        if out is None:
            raise QgsProcessingException(f'Could not create {path}.')
        out.SetGeoTransform(like.GetGeoTransform())
        out.SetProjection(like.GetProjection())
        out.SetMetadata(metadata)
        for i, (name, array) in enumerate(arrays, start=1):
            band = out.GetRasterBand(i)
            band.SetDescription(name)
            band.SetNoDataValue(nodata)
            band.WriteArray(array)
        out = None  # close and flush

    # ---- main -----------------------------------------------------------------

    def processAlgorithm(self, parameters, context, feedback):
        import numpy as np
        from osgeo import gdal, osr
        gdal.UseExceptions()

        scenes, sensor, date = self._granules(parameters, context)
        bands = hls.PRODUCTS[sensor]['bands']
        aoi = aoi_geometry(self.parameterAsSource(parameters, self.AOI, context), context)
        crs = self.parameterAsCrs(parameters, self.CRS, context)
        if not crs.isValid():
            c = aoi.centroid().asPoint()
            crs = utm_crs(c.x(), c.y())
        if crs.isGeographic():
            raise QgsProcessingException('The target CRS must be projected, in meters.')
        buffer_m = self.parameterAsDouble(parameters, self.BUFFER, context)
        mask_keys = {hls.FMASK_CLASSES[i][0] for i in self.parameterAsEnums(parameters, self.MASK, context)}
        netrc_path = self.parameterAsFile(parameters, self.NETRC, context) or None

        try:
            for w in earthdata.configure_gdal(netrc_path):
                warn(feedback, w)
        except earthdata.EarthdataError as e:
            raise QgsProcessingException(str(e)) from None

        # Grid: AOI + buffer in the target CRS, snapped outward to 30 m
        area = QgsGeometry(aoi)
        area.transform(QgsCoordinateTransform(QgsCoordinateReferenceSystem('EPSG:4326'), crs,
                                              context.transformContext()))
        box = area.buffer(buffer_m, 16).boundingBox() if buffer_m > 0 else area.boundingBox()
        px = hls.PIXEL
        bounds = (math.floor(box.xMinimum() / px) * px, math.floor(box.yMinimum() / px) * px,
                  math.ceil(box.xMaximum() / px) * px, math.ceil(box.yMaximum() / px) * px)
        ncols, nrows = int((bounds[2] - bounds[0]) / px), int((bounds[3] - bounds[1]) / px)
        srs = osr.SpatialReference()
        srs.SetFromUserInput(crs.toWkt())
        crs_wkt = srs.ExportToWkt()
        same_zone = all(crs.authid() == f'EPSG:{tile_epsg(s[2])}' for s in scenes)
        feedback.pushInfo(
            f'{sensor} {date.isoformat()}, tiles {", ".join(s[2] for s in scenes)}; '
            f'grid {ncols} x {nrows} px at {px:.0f} m in {crs.authid() or crs.description()}'
            + ('' if same_zone else '; reprojecting (bilinear for reflectance)'))
        if ncols * nrows > 40_000_000:
            raise QgsProcessingException(
                f'The area is {ncols} x {nrows} pixels; that is too large. Use a smaller AOI or buffer.')

        wanted = ['nir', 'swir2', 'fmask']
        want_refl = parameters.get(self.REFLECTANCE) not in (None, '')
        if want_refl:
            wanted.insert(2, 'red')
        data = {}
        like = None
        for i, key in enumerate(wanted):
            name = 'Fmask' if key == 'fmask' else bands[key]
            feedback.pushInfo(f'Reading {name} ({key})...')
            urls = ['/vsicurl/' + hls.band_url(s[0], name) for s in scenes]
            is_mask = key == 'fmask'
            ds = self._warp(urls, bounds, crs_wkt,
                            'near' if is_mask or same_zone else 'bilinear',
                            hls.FMASK_FILL if is_mask else hls.FILL,
                            gdal.GDT_Byte if is_mask else gdal.GDT_Int16, feedback)
            if ds is None:
                return {}
            data[key] = ds.GetRasterBand(1).ReadAsArray()
            like = like or ds
            feedback.setProgress(80 * (i + 1) / len(wanted))

        # Calibrate, mask and compute NBR (raw units)
        nir = data['nir'].astype(np.float32) * hls.SCALE
        swir2 = data['swir2'].astype(np.float32) * hls.SCALE
        fmask = data['fmask']
        has_data = (data['nir'] != hls.FILL) & (data['swir2'] != hls.FILL) & (fmask != hls.FMASK_FILL)
        masked = hls.fmask_mask(fmask, mask_keys)
        good = has_data & ~masked & (nir > 0) & (swir2 > 0)
        nbr = np.full(nir.shape, hls.FILL, dtype=np.float32)
        nbr[good] = (nir[good] - swir2[good]) / (nir[good] + swir2[good])

        n_all = nbr.size
        feedback.pushInfo(f'Pixels with data: {100.0 * has_data.sum() / n_all:.1f} % of the grid; '
                          f'NBR computed for {100.0 * good.sum() / n_all:.1f} %.')
        if has_data.sum() < n_all:
            warn(feedback, f'{100.0 * (1 - has_data.sum() / n_all):.1f} % of the grid has no HLS data; '
                           'a tile may be missing from the scenes, or the area runs off the swath.')
        for label, pct in hls.fmask_shares(fmask, has_data).items():
            if pct >= 0.05:
                feedback.pushInfo(f'  Fmask {label}: {pct:.1f} %')
        if good.any():
            feedback.pushInfo(f'NBR range {nbr[good].min():.3f} to {nbr[good].max():.3f}, '
                              f'mean {nbr[good].mean():.3f}')
        else:
            warn(feedback, 'Every pixel is masked; NBR is empty.')

        metadata = {
            'FIREANALYSIS_VERSION': VERSION,
            'HLS_SENSOR': sensor,
            'HLS_DATE': date.isoformat(),
            'HLS_GRANULES': ' '.join(s[0] for s in scenes),
            'FMASK_MASKED': ' '.join(sorted(mask_keys)),
            'INDEX': f'NBR = ({bands["nir"]} - {bands["swir2"]}) / ({bands["nir"]} + {bands["swir2"]}), '
                     f'reflectance = DN x {hls.SCALE}',
        }
        results = {}
        out = self.parameterAsOutputLayer(parameters, self.NBR, context)
        self._write(out, [('NBR', nbr)], like, gdal.GDT_Float32, hls.FILL, metadata)
        results[self.NBR] = out
        if want_refl:
            out = self.parameterAsOutputLayer(parameters, self.REFLECTANCE, context)
            stack = []
            for key in ('swir2', 'nir', 'red'):
                refl = data[key].astype(np.float32) * hls.SCALE
                refl[data[key] == hls.FILL] = hls.FILL
                stack.append((f'{bands[key]} {key}', refl))
            self._write(out, stack, like, gdal.GDT_Float32, hls.FILL, metadata)
            results[self.REFLECTANCE] = out
        if parameters.get(self.FMASK) not in (None, ''):
            out = self.parameterAsOutputLayer(parameters, self.FMASK, context)
            self._write(out, [('Fmask', fmask)], like, gdal.GDT_Byte, hls.FMASK_FILL, metadata)
            results[self.FMASK] = out
        feedback.setProgress(100)
        return results
