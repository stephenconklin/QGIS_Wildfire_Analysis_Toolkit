"""MTBS (Monitoring Trends in Burn Severity) published fires, from the USFS
Enterprise Data Warehouse map service: perimeters with their dNBR offset and
thresholds (x1000 units; 9999 means the class was not mapped)."""
import json
import statistics
import urllib.parse

from . import stac

BOUNDARIES = 'https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MTBS_01/MapServer/63'  # all years
FIELDS = ('fire_id,fire_name,year,fire_type,asmnt_type,acres,dnbr_offst,dnbr_stddv,'
          'low_threshold,moderate_threshold,high_threshold')
NONE = 9999
KEYS = ('low_threshold', 'moderate_threshold', 'high_threshold')
RETRIES = 4


class MtbsError(Exception):
    pass


def nearby_fires(lon, lat, radius_km, first_year, last_year, fire_type='Wildfire', feedback=None):
    """MTBS fires whose boundary lies within radius_km of (lon, lat), as attribute dicts."""
    where = f'year >= {int(first_year)} AND year <= {int(last_year)}'
    if fire_type:
        where += f" AND fire_type = '{fire_type}'"
    params = dict(geometry=f'{lon},{lat}', geometryType='esriGeometryPoint', inSR=4326,
                  distance=radius_km, units='esriSRUnit_Kilometer', spatialRel='esriSpatialRelIntersects',
                  where=where, outFields=FIELDS, returnGeometry='false', f='json')
    url = BOUNDARIES + '/query?' + urllib.parse.urlencode(params)
    for attempt in range(RETRIES):  # the service drops connections now and then
        try:
            data = stac._request(url, None, feedback)
            break
        except stac.StacError as e:
            if attempt == RETRIES - 1:
                raise MtbsError(f'MTBS map service request failed after {RETRIES} tries: {e}') from None
    if data is None:  # cancelled
        return []
    if 'error' in data:
        raise MtbsError(f'MTBS map service error: {json.dumps(data["error"])[:300]}')
    return [f['attributes'] for f in data.get('features', [])]


def median_thresholds(fires, assessment=None, exclude=(), min_fires=5):
    """Median low, moderate and high thresholds (raw dNBR units) over fires, each over
    the fires that mapped that class. Uses only fires of the same assessment type
    when there are at least min_fires of them. Returns (thresholds, counts, n fires, scope)."""
    pool = [f for f in fires if f.get('fire_id') not in exclude]
    scope = 'all assessment types'
    if assessment:
        same = [f for f in pool if str(f.get('asmnt_type', '')).lower().startswith(assessment.lower())]
        if len(same) >= min_fires:
            pool, scope = same, f'{assessment.lower()} assessments'
    thresholds, counts = [], []
    for k in KEYS:
        vals = [f[k] for f in pool if f.get(k) is not None and -NONE < f[k] < NONE]
        counts.append(len(vals))
        thresholds.append(statistics.median(vals) / 1000.0 if vals else None)
    return thresholds, counts, len(pool), scope
