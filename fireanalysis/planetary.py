"""Microsoft Planetary Computer: Landsat Collection 2 Level-2 (Landsat 4-9, 1982 on)
and Sentinel-2 L2A, as cloud-optimized GeoTIFFs. No account: each collection's
blob URLs are signed with a short-lived token from the SAS API.

Items become local.Scene objects whose band entries are signed /vsicurl/ URLs,
so they are calibrated, masked and written by the same code as downloaded files.

Calibration: Landsat DN x scale + offset from the item's raster:bands (2.75e-05,
-0.2). Sentinel-2 (DN + offset) / 10000 with offset -1000 when the item's
s2:processing_baseline is 04.00 or later (the COGs keep the original DN).
"""
import datetime
import json
import time
import urllib.parse

from . import local, stac

SEARCH = 'https://planetarycomputer.microsoft.com/api/stac/v1/search'
TOKEN = 'https://planetarycomputer.microsoft.com/api/sas/v1/token/'
SOURCES = {  # key -> (collection, label, band assets nir/swir2/red, quality asset)
    'landsat': ('landsat-c2-l2', 'Landsat 4-9 Collection 2 Level-2', ('nir08', 'swir22', 'red'), 'qa_pixel'),
    's2': ('sentinel-2-l2a', 'Sentinel-2 L2A', ('B8A', 'B12', 'B04'), 'SCL'),
}
PLATFORMS = {'landsat-4': 'LT04', 'landsat-5': 'LT05', 'landsat-7': 'LE07', 'landsat-8': 'LC08',
             'landsat-9': 'LC09'}

_tokens = {}  # collection -> (token, expiry epoch seconds)


class PlanetaryError(Exception):
    pass


def token(collection, feedback=None):
    """SAS token for a collection, cached until 5 minutes before it expires."""
    cached = _tokens.get(collection)
    if cached and cached[1] - 300 > time.time():
        return cached[0]
    try:
        data = stac._request(TOKEN + collection, None, feedback)
    except stac.StacError as e:
        raise PlanetaryError(f'Could not get a Planetary Computer token: {e}') from None
    if not data or 'token' not in data:
        raise PlanetaryError(f'Unexpected Planetary Computer token reply: {json.dumps(data)[:200]}')
    expiry = data.get('msft:expiry', '')
    try:
        stamp = datetime.datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp()
    except ValueError:
        stamp = time.time() + 1800
    _tokens[collection] = (data['token'], stamp)
    return data['token']


def sign(href, collection, feedback=None):
    return '/vsicurl/' + href + ('&' if '?' in href else '?') + token(collection, feedback)


def search(source, bbox, start, end, feedback=None, ids=None):
    """Items of one source over bbox between two datetime.date values (or the given ids)."""
    collection = SOURCES[source][0]
    if ids:
        url = SEARCH + '?' + urllib.parse.urlencode({'collections': collection, 'ids': ','.join(ids),
                                                     'limit': 100})
        page = stac._request(url, None, feedback) or {}
        return page.get('features', [])
    when = f'{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z'
    return list(stac.search(SEARCH, [collection], bbox, when, feedback=feedback))


def item_date(item):
    return datetime.date.fromisoformat(item['properties']['datetime'][:10])


def item_label(item):
    """Short id for a field: Landsat path/row or Sentinel-2 tile."""
    p = item['properties']
    if 'landsat:wrs_path' in p:
        return f'{p["landsat:wrs_path"]}/{p["landsat:wrs_row"]}'
    return p.get('s2:mgrs_tile', '')


def items_to_scene(items, source, feedback=None):
    """One acquisition date (several path/rows or tiles allowed) -> local.Scene."""
    if not items:
        raise PlanetaryError('No scenes given.')
    collection, label, assets, qa_asset = SOURCES[source]
    dates = {item_date(i) for i in items}
    if len(dates) > 1:
        raise PlanetaryError('All scenes must be from one date; got ' + ', '.join(sorted(map(str, dates))) + '.')
    date = dates.pop()
    platforms = {i['properties'].get('platform', '').lower() for i in items}
    if source == 'landsat':
        sensors = {PLATFORMS.get(p, p) for p in platforms}
        if len(sensors) > 1:
            raise PlanetaryError(f'Scenes from several Landsat satellites on one date: {sorted(sensors)}.')
        sensor = sensors.pop()
    else:
        sensor = 'S' + '/'.join(sorted(p.replace('sentinel-', '').upper() for p in platforms))
    bands, scale, offsets = {}, {}, {}
    for key, asset in zip(('nir', 'swir2', 'red'), assets):
        urls = []
        for item in items:
            a = item['assets'].get(asset)
            if a is None:
                raise PlanetaryError(f'{item["id"]} has no {asset} band.')
            urls.append(sign(a['href'], collection, feedback))
        bands[key] = urls
        if source == 'landsat':
            rb = (items[0]['assets'][asset].get('raster:bands') or [{}])[0]
            scale[key], offsets[key] = float(rb.get('scale', 2.75e-05)), float(rb.get('offset', -0.2))
    if source == 'landsat':
        text = f'{label}: DN x {scale["nir"]:g} + {offsets["nir"]:g} (from the STAC item)'
    else:
        baselines = {i['properties'].get('s2:processing_baseline') or '' for i in items}
        new = {b >= '04.00' for b in baselines if b}
        if len(new) != 1:
            raise PlanetaryError(f'Unknown or mixed Sentinel-2 processing baselines: {sorted(baselines)}.')
        off = -1000.0 if new.pop() else 0.0
        scale = {k: 1e-4 for k in bands}
        offsets = {k: off for k in bands}
        text = (f'{label}: (DN {off:+g}) / 10000 (processing baseline {", ".join(sorted(baselines))}; '
                'the offset applies from 04.00)')
    qa = [sign(i['assets'][qa_asset]['href'], collection, feedback) for i in items if qa_asset in i['assets']]
    if len(qa) != len(items):
        qa = []
    kind = 'landsat' if source == 'landsat' else 's2'
    return local.Scene(kind, date, sensor, bands, scale, offsets, qa, text)
