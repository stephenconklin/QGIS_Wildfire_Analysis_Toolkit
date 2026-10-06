"""NASA HLS v2.0 (Harmonized Landsat Sentinel-2) product facts.

Checked against live granules on 2026-10-06: band files are COGs, Int16,
scale 0.0001, offset 0, nodata -9999, 30 m on the MGRS tile grid in WGS 84 UTM.
"""
import datetime
import re

STAC_SEARCH = 'https://cmr.earthdata.nasa.gov/stac/LPCLOUD/search'
DATA_URL = 'https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected'

# sensor code -> STAC collection, product folder, band names
PRODUCTS = {
    'L30': {'collection': 'HLSL30_2.0', 'folder': 'HLSL30.020', 'label': 'L30 (Landsat 8/9)',
            'bands': {'nir': 'B05', 'swir2': 'B07', 'red': 'B04'}},
    'S30': {'collection': 'HLSS30_2.0', 'folder': 'HLSS30.020', 'label': 'S30 (Sentinel-2)',
            'bands': {'nir': 'B8A', 'swir2': 'B12', 'red': 'B04'}},
}
SENSORS = list(PRODUCTS)

SCALE = 0.0001
FILL = -9999
PIXEL = 30.0

# Fmask bits. 6-7 hold the aerosol level (0 climatology, 1 low, 2 moderate, 3 high).
FMASK_FILL = 255
FMASK_CLASSES = [  # (key, label, bit, masked by default)
    ('cirrus', 'Cirrus', 0, False),
    ('cloud', 'Cloud', 1, True),
    ('adjacent', 'Adjacent to cloud or shadow', 2, True),
    ('shadow', 'Cloud shadow', 3, True),
    ('snow', 'Snow or ice', 4, True),
    ('water', 'Water', 5, True),
    ('aerosol', 'High aerosol (often smoke)', None, False),
]

GRANULE_RE = re.compile(r'^HLS\.(L30|S30)\.T(\d{2}[A-Z]{3})\.(\d{4})(\d{3})T(\d{6})\.v2\.0$')


def parse_granule(granule):
    """'HLS.L30.T12SUF.2025258T180302.v2.0' -> (sensor, tile, date). Raise ValueError."""
    m = GRANULE_RE.match(granule)
    if not m:
        raise ValueError(f'Not an HLS v2.0 granule id: {granule}')
    sensor, tile, year, doy, _ = m.groups()
    date = datetime.date(int(year), 1, 1) + datetime.timedelta(days=int(doy) - 1)
    return sensor, tile, date


def band_url(granule, band):
    sensor, _, _ = parse_granule(granule)
    return f'{DATA_URL}/{PRODUCTS[sensor]["folder"]}/{granule}/{granule}.{band}.tif'


def fmask_mask(fmask, keys):
    """Boolean array: True where any of the chosen Fmask classes is set, or fill."""
    import numpy as np
    fmask = fmask.astype(np.uint16)
    out = fmask == FMASK_FILL
    for key, _label, bit, _default in FMASK_CLASSES:
        if key not in keys:
            continue
        if bit is None:  # high aerosol
            out |= ((fmask >> 6) & 3) == 3
        else:
            out |= (fmask & (1 << bit)) > 0
    return out


def fmask_shares(fmask, valid):
    """Percent of valid pixels flagged by each Fmask class (classes overlap)."""
    import numpy as np
    fmask = fmask.astype(np.uint16)
    n = int(valid.sum())
    shares = {}
    for key, label, bit, _default in FMASK_CLASSES:
        flag = (((fmask >> 6) & 3) == 3) if bit is None else ((fmask & (1 << bit)) > 0)
        shares[label] = 100.0 * int((flag & valid).sum()) / n if n else 0.0
    return shares
