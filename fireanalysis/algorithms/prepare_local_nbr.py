"""Prepare local NBR: calibrated, masked NBR from hand-downloaded Landsat C2 L2
(scene or US ARD tile) or Sentinel-2 L2A files, on the same grid as Fetch HLS NBR."""
from qgis.core import (QgsCoordinateReferenceSystem, QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingParameterCrs, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber, QgsProcessingParameterRasterDestination)

from .. import VERSION, local, raster
from ..compat import advanced, double_type, polygon_source, warn
from .find_hls_scenes import aoi_geometry

MASK_CLASSES = [  # (key, label, masked by default)
    ('cirrus', 'Cirrus', False),
    ('cloud', 'Cloud', True),
    ('adjacent', 'Dilated cloud (Landsat only)', True),
    ('shadow', 'Cloud shadow', True),
    ('snow', 'Snow or ice', True),
    ('water', 'Water', True),
]
S2_OFFSETS = ['From MTD_MSIL2A.xml, or -1000 if acquired after 25 Jan 2022', '-1000 (baseline 04.00 or later)',
              '0 (baseline before 04.00)']


class PrepareLocalNbr(QgsProcessingAlgorithm):
    SCENE = 'SCENE'
    AOI = 'AOI'
    BUFFER = 'BUFFER'
    CRS = 'CRS'
    PIXEL = 'PIXEL'
    MASK = 'MASK'
    S2_OFFSET = 'S2_OFFSET'
    NBR = 'NBR'
    REFLECTANCE = 'REFLECTANCE'

    def name(self):
        return 'preparelocalnbr'

    def displayName(self):
        return 'Prepare local scene NBR'

    def group(self):
        return 'Imagery'

    def groupId(self):
        return 'imagery'

    def shortHelpString(self):
        return (
            '<p>Calibrates downloaded Landsat Collection 2 Level-2 (a scene, or a US ARD tile) or '
            'Sentinel-2 L2A bands to surface reflectance, masks clouds from QA_PIXEL or SCL, and '
            'writes NBR (raw units) on the same grid <i>Fetch HLS NBR</i> uses, so local and HLS '
            'NBR can be combined.</p>'
            '<p><b>Scene folder:</b> the folder of one acquisition (searched recursively, so '
            'several tiles of the same date in subfolders are mosaicked). Landsat needs '
            '*_SR_B*.TIF; Sentinel-2 needs *_B8A_20m.jp2 and *_B12_20m.jp2.</p>'
            '<p><b>Calibration.</b> Landsat: DN × 0.0000275 − 0.2 (read from the MTL or ARD '
            'metadata). Sentinel-2: (DN + BOA_ADD_OFFSET) / 10000, with the offset from '
            'MTD_MSIL2A.xml; it is −1000 for processing baseline 04.00 and later (from 25 January '
            '2022). Skipping either step biases every index, which is why this tool exists.</p>'
            '<p>Without QA_PIXEL or SCL there is no cloud mask; the log says so.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            self.SCENE, 'Scene folder', behavior=_folder_behavior()))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.AOI, 'Area of interest', [polygon_source()]))
        self.addParameter(QgsProcessingParameterNumber(
            self.BUFFER, 'Buffer around the AOI (m)', type=double_type(), minValue=0, defaultValue=3000))
        self.addParameter(QgsProcessingParameterCrs(
            self.CRS, 'Target CRS (empty = UTM zone of the AOI)', optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.PIXEL, 'Pixel size (m)', type=double_type(), minValue=10, defaultValue=30))
        self.addParameter(QgsProcessingParameterEnum(
            self.MASK, 'Classes to mask', options=[c[1] for c in MASK_CLASSES], allowMultiple=True,
            defaultValue=[i for i, c in enumerate(MASK_CLASSES) if c[2]]))
        self.addParameter(advanced(QgsProcessingParameterEnum(
            self.S2_OFFSET, 'Sentinel-2 BOA offset', options=S2_OFFSETS, defaultValue=0)))
        self.addParameter(QgsProcessingParameterRasterDestination(self.NBR, 'NBR'))
        self.addParameter(QgsProcessingParameterRasterDestination(
            self.REFLECTANCE, 'Reflectance SWIR2 / NIR / red (for display)', optional=True,
            createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):
        import numpy as np
        from osgeo import gdal
        gdal.UseExceptions()

        folder = self.parameterAsFile(parameters, self.SCENE, context)
        choice = self.parameterAsEnum(parameters, self.S2_OFFSET, context)
        try:
            scene, metadata_found = local.find_scene(folder, {0: None, 1: -1000, 2: 0}[choice])
        except local.SceneError as e:
            raise QgsProcessingException(str(e)) from None
        feedback.pushInfo(f'{scene.sensor} {scene.date.isoformat()}: {scene.label}')
        if not metadata_found:
            warn(feedback, 'No product metadata found; the calibration above is the standard one for '
                           'this product. Keep the MTL / ARD / MTD file with the bands to be sure.')
        for key, files in scene.bands.items():
            if files:
                feedback.pushInfo(f'  {key}: ' + ', '.join(f.rsplit("/", 1)[-1] for f in files))

        aoi = aoi_geometry(self.parameterAsSource(parameters, self.AOI, context), context)
        pixel = self.parameterAsDouble(parameters, self.PIXEL, context)
        crs, bounds, ncols, nrows = raster.analysis_grid(
            aoi, self.parameterAsCrs(parameters, self.CRS, context),
            self.parameterAsDouble(parameters, self.BUFFER, context), pixel, context.transformContext())
        wkt = raster.crs_wkt(crs)
        feedback.pushInfo(f'Grid {ncols} x {nrows} px at {pixel:.0f} m in {crs.authid() or crs.description()}')

        def warp(files, resample, out_type):
            src = gdal.Open(files[0])
            src_px = abs(src.GetGeoTransform()[1])
            same_crs = QgsCoordinateReferenceSystem.fromWkt(src.GetProjection()) == crs
            if resample == 'auto':
                aligned = same_crs and abs(src_px - pixel) < 1e-6 and \
                    all(abs((v - src.GetGeoTransform()[i]) / pixel - round((v - src.GetGeoTransform()[i]) / pixel)) < 1e-6
                        for i, v in ((0, bounds[0]), (3, bounds[3])))
                resample = 'near' if aligned else ('average' if src_px < pixel * 0.9 else 'bilinear')
            ds = gdal.Warp('', files, format='MEM', outputBounds=bounds, width=ncols, height=nrows,
                           dstSRS=wkt, resampleAlg=resample, srcNodata=0, dstNodata=0, outputType=out_type,
                           multithread=True,
                           callback=lambda *_: 0 if feedback.isCanceled() else 1)
            if ds is None:
                raise QgsProcessingException(f'Could not read {files[0]}.')
            return ds, resample

        refl, like, used = {}, None, set()
        keys = ['nir', 'swir2'] + (['red'] if parameters.get(self.REFLECTANCE) not in (None, '') else [])
        for i, key in enumerate(keys):
            if not scene.bands.get(key):
                raise QgsProcessingException(f'No {key} band in the scene folder.')
            ds, how = warp(scene.bands[key], 'auto', gdal.GDT_Float32)
            if feedback.isCanceled():
                return {}
            used.add(how)
            refl[key] = scene.reflectance(key, ds.GetRasterBand(1).ReadAsArray())
            like = like or ds
            feedback.setProgress(70 * (i + 1) / (len(keys) + 1))
        feedback.pushInfo(f'Resampling: {", ".join(sorted(used))}')

        mask_keys = {MASK_CLASSES[i][0] for i in self.parameterAsEnums(parameters, self.MASK, context)}
        has_data = np.isfinite(refl['nir']) & np.isfinite(refl['swir2'])
        masked = np.zeros(has_data.shape, bool)
        if scene.qa:
            qa_ds, _ = warp(scene.qa, 'near', gdal.GDT_UInt16)
            qa = qa_ds.GetRasterBand(1).ReadAsArray()
            masked, fill = scene.qa_mask(qa, mask_keys)
            has_data &= ~fill
            n = max(int(has_data.sum()), 1)
            for key, label, _ in MASK_CLASSES:
                share = 100.0 * int((scene.qa_mask(qa, {key})[0] & has_data).sum()) / n
                if share >= 0.05:
                    feedback.pushInfo(f'  {label}: {share:.1f} %' + ('' if key in mask_keys else ' (not masked)'))
        else:
            warn(feedback, 'No QA_PIXEL (Landsat) or SCL (Sentinel-2) file: clouds, shadow, snow and '
                           'water are not masked.')
        nir, swir2 = refl['nir'], refl['swir2']
        good = has_data & ~masked & (nir > 0) & (swir2 > 0)
        nbr = np.full(nir.shape, np.nan, np.float32)
        nbr[good] = (nir[good] - swir2[good]) / (nir[good] + swir2[good])
        feedback.pushInfo(f'NBR computed for {100.0 * good.mean():.1f} % of the grid'
                          + (f'; range {nbr[good].min():.3f} to {nbr[good].max():.3f}, mean {nbr[good].mean():.3f}'
                             if good.any() else ''))
        if not good.any():
            warn(feedback, 'NBR is empty: the scene does not cover the area, or every pixel is masked.')
        elif has_data.mean() < 0.99:
            warn(feedback, f'{100.0 * (1 - has_data.mean()):.1f} % of the grid has no data from this scene.')

        metadata = {'FIREANALYSIS_VERSION': VERSION, 'SOURCE_SENSOR': scene.sensor,
                    'SOURCE_DATE': scene.date.isoformat(), 'CALIBRATION': scene.label,
                    'SOURCE_FILES': ' '.join(f.rsplit('/', 1)[-1] for k in ('nir', 'swir2') for f in scene.bands[k]),
                    'MASKED': ' '.join(sorted(mask_keys)) if scene.qa else 'none (no QA file)'}
        out = self.parameterAsOutputLayer(parameters, self.NBR, context)
        raster.write(out, [('NBR', nbr)], like, gdal.GDT_Float32, raster.NODATA, metadata)
        results = {self.NBR: out}
        if 'red' in refl:
            out = self.parameterAsOutputLayer(parameters, self.REFLECTANCE, context)
            raster.write(out, [('swir2', refl['swir2']), ('nir', refl['nir']), ('red', refl['red'])],
                         like, gdal.GDT_Float32, raster.NODATA, metadata)
            results[self.REFLECTANCE] = out
        feedback.setProgress(100)
        return results


def _folder_behavior():
    from qgis.core import Qgis
    try:
        return Qgis.ProcessingFileParameterBehavior.Folder
    except AttributeError:
        return QgsProcessingParameterFile.Folder
