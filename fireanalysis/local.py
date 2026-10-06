"""Find, identify and calibrate hand-downloaded scenes: Landsat Collection 2
Level-2 (scene or US ARD tile) and Sentinel-2 L2A.

Each product gives a Scene: per-band file lists (several tiles of one date are
mosaicked), the scale and offset that turn DN into surface reflectance, and an
optional quality band with its mask rule.

Calibration (DN -> surface reflectance):
  Landsat C2 L2:  DN x REFLECTANCE_MULT_BAND_n + REFLECTANCE_ADD_BAND_n
                  (2.75e-05 and -0.2 for every C2 L2 product; read from the
                  MTL/ARD metadata when present). Fill DN 0.
  Sentinel-2 L2A: (DN + BOA_ADD_OFFSET) / BOA_QUANTIFICATION_VALUE
                  (offset -1000 from processing baseline 04.00, 25 Jan 2022;
                  0 before; read from MTD_MSIL2A.xml when present). Fill DN 0.
"""
import datetime
import glob
import os
import re

S2_OFFSET_DATE = datetime.date(2022, 1, 25)  # processing baseline 04.00 went live

# Landsat: sensor prefix -> NIR, SWIR2, red band numbers
LANDSAT_BANDS = {
    'LC08': (5, 7, 4), 'LC09': (5, 7, 4), 'LO08': (5, 7, 4), 'LO09': (5, 7, 4),
    'LE07': (4, 7, 3), 'LT05': (4, 7, 3), 'LT04': (4, 7, 3),
}
# QA_PIXEL bits (Collection 2): 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud, 4 shadow, 5 snow, 7 water
QA_PIXEL_BITS = {'adjacent': 1, 'cirrus': 2, 'cloud': 3, 'shadow': 4, 'snow': 5, 'water': 7}
# Sentinel-2 scene classification (SCL) values per mask class
SCL_CLASSES = {'shadow': (3,), 'cloud': (8, 9), 'cirrus': (10,), 'snow': (11,), 'water': (6,)}
# SCL has no "adjacent to cloud" class; Landsat's dilated-cloud bit stands in for it
SCL_FILL = (0, 1)  # no data, saturated or defective


class SceneError(Exception):
    pass


class Scene:
    def __init__(self, kind, date, sensor, bands, scale, offsets, qa, label):
        self.kind = kind          # 'landsat' or 's2'
        self.date = date          # datetime.date
        self.sensor = sensor      # e.g. 'LC09', 'S2A'
        self.bands = bands        # {'nir': [paths], 'swir2': [...], 'red': [...]}
        self.scale = scale        # {'nir': float, ...}
        self.offsets = offsets    # {'nir': float, ...} added after scaling (Landsat) or before (S2)
        self.qa = qa              # [paths] or []
        self.label = label        # how the calibration was found, for the log and metadata

    def reflectance(self, key, dn):
        """DN array (float) -> surface reflectance; fill (DN 0) -> NaN."""
        import numpy as np
        out = dn.astype(np.float32)
        fill = dn == 0
        if self.kind == 'landsat':
            out = out * self.scale[key] + self.offsets[key]
        else:
            out = (out + self.offsets[key]) * self.scale[key]
        out[fill] = np.nan
        return out

    def qa_mask(self, qa, keys):
        """Boolean array, True where any chosen class is flagged; and the fill mask."""
        import numpy as np
        if self.kind == 'landsat':
            qa = qa.astype(np.uint16)
            fill = (qa & 1) > 0
            out = np.zeros(qa.shape, bool)
            for k in keys:
                if k in QA_PIXEL_BITS:
                    out |= (qa & (1 << QA_PIXEL_BITS[k])) > 0
        else:
            fill = np.isin(qa, SCL_FILL)
            out = np.zeros(qa.shape, bool)
            for k in keys:
                if k in SCL_CLASSES:
                    out |= np.isin(qa, SCL_CLASSES[k])
        return out | fill, fill


def _files(folder, pattern):
    return sorted(glob.glob(os.path.join(folder, '**', pattern), recursive=True))


def _read_text(paths):
    text = ''
    for p in paths:
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                text += f.read() + '\n'
        except OSError:
            pass
    return text


def _landsat(folder):
    srs = _files(folder, '*_SR_B*.TIF') + _files(folder, '*_SR_B*.tif')
    if not srs:
        return None
    names = [os.path.basename(p) for p in srs]
    prefixes = {n[:4].upper() for n in names}
    if len(prefixes) != 1 or next(iter(prefixes)) not in LANDSAT_BANDS:
        raise SceneError(f'Landsat files from several sensors or an unknown one: {sorted(prefixes)}')
    sensor = next(iter(prefixes))
    dates = set()
    for n in names:
        m = re.search(r'_(\d{8})_\d{8}_02', n)
        if m:
            dates.add(m.group(1))
    if len(dates) != 1:
        raise SceneError(f'Landsat files from several dates: {sorted(dates)}. Use one date per run.')
    d = next(iter(dates))
    date = datetime.date(int(d[:4]), int(d[4:6]), int(d[6:]))
    nir, swir2, red = LANDSAT_BANDS[sensor]
    bands = {}
    for key, b in (('nir', nir), ('swir2', swir2), ('red', red)):
        bands[key] = [p for p in srs if re.search(rf'_SR_B{b}\.TIF$', p, re.I)]
    if not bands['nir'] or not bands['swir2']:
        raise SceneError(f'Missing SR_B{nir} (NIR) or SR_B{swir2} (SWIR2) for {sensor}.')
    meta = _read_text(_files(folder, '*MTL.txt') + _files(folder, '*MTL.xml') + _files(folder, '*MTL.json')
                      + [p for p in _files(folder, '*.json') if '_CU_' in p or '_AK_' in p or '_HI_' in p]
                      + [p for p in _files(folder, '*.xml') if '_CU_' in p or '_AK_' in p or '_HI_' in p])
    scale, offsets, found = {}, {}, True
    for key, b in (('nir', nir), ('swir2', swir2), ('red', red)):
        m = re.search(rf'REFLECTANCE_MULT_BAND_{b}\W+?([-\d.eE+]+)', meta)
        a = re.search(rf'REFLECTANCE_ADD_BAND_{b}\W+?([-\d.eE+]+)', meta)
        if m and a:
            scale[key], offsets[key] = float(m.group(1)), float(a.group(1))
        else:  # the NBR bands decide whether the metadata was found; red is for display only
            scale[key], offsets[key] = 2.75e-05, -0.2
            found = found and key == 'red'
    label = ('Landsat C2 L2: DN x {:g} + {:g} from the metadata' if found else
             'Landsat C2 L2: DN x {:g} + {:g} (standard; no MTL or ARD metadata found)').format(
        scale['nir'], offsets['nir'])
    qa = _files(folder, '*_QA_PIXEL.TIF') + _files(folder, '*_QA_PIXEL.tif')
    return Scene('landsat', date, sensor, bands, scale, offsets, qa, label), found


def _sentinel2(folder, s2_offset):
    def band(name):
        return (_files(folder, f'*_{name}_20m.jp2') + _files(folder, f'*_{name}_20m.tif')
                + _files(folder, f'*_{name}.jp2'))
    nir, swir2 = band('B8A'), band('B12')
    if not nir and not swir2:
        return None
    if not nir or not swir2:
        raise SceneError('Sentinel-2 needs both B8A (NIR) and B12 (SWIR2), preferably the 20 m files.')
    red = band('B04')
    red = [p for p in red if '_20m' in p] or red
    dates = {m.group(1) for p in nir + swir2 if (m := re.search(r'_(\d{8})T\d{6}', os.path.basename(p)))}
    if len(dates) != 1:
        raise SceneError(f'Sentinel-2 files from several dates: {sorted(dates)}. Use one date per run.')
    d = next(iter(dates))
    date = datetime.date(int(d[:4]), int(d[4:6]), int(d[6:]))
    mtd = _files(folder, 'MTD_MSIL2A.xml')
    meta = _read_text(mtd)
    sensor = 'S2'
    m = re.search(r'<SPACECRAFT_NAME>Sentinel-(2[ABC])</SPACECRAFT_NAME>', meta)
    if m:
        sensor = 'S' + m.group(1)
    quant = re.search(r'<BOA_QUANTIFICATION_VALUE[^>]*>([\d.]+)<', meta)
    scale = 1.0 / float(quant.group(1)) if quant else 1e-4
    # BOA_ADD_OFFSET band_id: 0..12 for B1..B12 with B8A at 8
    offs = dict(re.findall(r'<BOA_ADD_OFFSET band_id="(\d+)">(-?\d+)<', meta))
    band_ids = {'nir': '8', 'swir2': '12', 'red': '3'}
    offsets, label = {}, ''
    if offs:
        for k, i in band_ids.items():
            offsets[k] = float(offs.get(i, 0))
        label = f'Sentinel-2 L2A: (DN + {offsets["nir"]:g}) / {1 / scale:g} from MTD_MSIL2A.xml'
    elif mtd and not offs:
        offsets = {k: 0.0 for k in band_ids}
        label = f'Sentinel-2 L2A: DN / {1 / scale:g} (MTD_MSIL2A.xml has no offset: baseline before 04.00)'
    elif s2_offset is not None:
        offsets = {k: float(s2_offset) for k in band_ids}
        label = f'Sentinel-2 L2A: (DN + {s2_offset:g}) / 10000 (offset given; no MTD_MSIL2A.xml)'
    elif date >= S2_OFFSET_DATE:
        offsets = {k: -1000.0 for k in band_ids}
        label = ('Sentinel-2 L2A: (DN - 1000) / 10000 (no MTD_MSIL2A.xml; acquired after 25 Jan 2022, '
                 'so processing baseline 04.00 or later)')
    else:
        raise SceneError(
            f'No MTD_MSIL2A.xml next to the Sentinel-2 bands, and the scene ({date}) is from before '
            '25 Jan 2022, so the BOA offset can be 0 or -1000 depending on how it was processed. '
            'Add the metadata file, or set the Sentinel-2 offset explicitly.')
    qa = band('SCL')
    bands = {'nir': nir, 'swir2': swir2, 'red': red}
    found = bool(offs) or bool(mtd)
    return Scene('s2', date, sensor, bands, {k: scale for k in band_ids}, offsets, qa, label), found


def find_scene(folder, s2_offset=None):
    """Identify the one product in folder (searched recursively).
    Returns (Scene, metadata_found). Raise SceneError if none or several kinds."""
    if not os.path.isdir(folder):
        folder = os.path.dirname(folder)
    if not os.path.isdir(folder):
        raise SceneError(f'{folder} is not a folder.')
    found = [s for s in (_landsat(folder), _sentinel2(folder, s2_offset)) if s]
    if not found:
        raise SceneError(
            f'No Landsat Collection 2 Level-2 (*_SR_B*.TIF) or Sentinel-2 L2A (*_B8A_20m.jp2, '
            f'*_B12_20m.jp2) bands in {folder}.')
    if len(found) > 1:
        raise SceneError('The folder holds both Landsat and Sentinel-2 bands; use one product per run.')
    return found[0]
