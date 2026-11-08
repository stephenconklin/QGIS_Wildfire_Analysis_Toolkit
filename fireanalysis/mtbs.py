"""MTBS (Monitoring Trends in Burn Severity) published fires.

Two sources, fetched by the plugin itself:

- The USGS fire records file (fire occurrence points, about 3.5 MB zipped): one
  point per fire with every attribute MTBS publishes: name, ignition date,
  assessment type, MTBS's pre- and post-fire scenes, dNBR offset and thresholds
  (x1000 units; 9999 means the class was not mapped). It is the most current
  MTBS source (updated about daily), so it backs fire lookup and the nearby-fire
  thresholds. It is cached in the QGIS profile, checked for a newer copy at most
  once a day, and used as it is when the network is down.
- Perimeters: one fire's polygon from the USFS Enterprise Data Warehouse map
  service when it has the fire (it lags behind the records, by most of a year
  for new fires); else from the full USGS perimeter file (a 420 MB download),
  which is fetched only when the caller allows it, then cached like the records.

MTBS's published classes (dnbr6) come from the MTBS CONUS image service, by year.
"""
import datetime
import json
import math
import os
import re
import shutil
import statistics
import time
import urllib.parse
import zipfile

from . import stac

BOUNDARIES = 'https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MTBS_01/MapServer/63'  # all years
IMAGESERVER = ('https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/USFS_EDW_MTBS_CONUS/'
               'ImageServer')
USGS = 'https://edcintl.cr.usgs.gov/downloads/sciweb1/shared/MTBS_Fire/data/composite_data/'
DATASETS = {  # name -> (url, shapefile in the zip, label)
    'records': (USGS + 'fod_pt_shapefile/mtbs_fod_pts_data.zip', 'mtbs_FODpoints_DD.shp',
                'MTBS fire records (USGS, about 3.5 MB)'),
    'perimeters': (USGS + 'burned_area_extent_shapefile/mtbs_perimeter_data.zip', 'mtbs_perims_DD.shp',
                   'MTBS burned area boundaries (USGS, about 420 MB)'),
}
FIELDS = ('fire_id,fire_name,year,fire_type,asmnt_type,acres,dnbr_offst,dnbr_stddv,'
          'low_threshold,moderate_threshold,high_threshold')
NONE = 9999
KEYS = ('low_threshold', 'moderate_threshold', 'high_threshold')
RECORD_KEYS = ('low_t', 'mod_t', 'high_t')
RETRIES = 4
CHECK_EVERY = 24 * 3600   # seconds between checks for a newer USGS file
CHUNK = 16 * 1024 * 1024  # bytes per ranged request when downloading a file
MAX_COMPARE_PX = 4000     # image service export limit per side
EVENT_ID_RE = re.compile(r'^[A-Z]{2}\d{19}$')
CACHE_ENV = 'FIREANALYSIS_MTBS_CACHE'  # overrides the cache folder (tests)


class MtbsError(Exception):
    pass


# ---- map service: nearby fires (the original source; still the fallback) ----------

def nearby_fires(lon, lat, radius_km, first_year, last_year, fire_type='Wildfire', feedback=None):
    """MTBS fires whose boundary lies within radius_km of (lon, lat), as attribute dicts,
    from the map service."""
    where = f'year >= {int(first_year)} AND year <= {int(last_year)}'
    if fire_type:
        where += f" AND fire_type = '{fire_type}'"
    params = dict(geometry=f'{lon},{lat}', geometryType='esriGeometryPoint', inSR=4326,
                  distance=radius_km, units='esriSRUnit_Kilometer', spatialRel='esriSpatialRelIntersects',
                  where=where, outFields=FIELDS, returnGeometry='false', f='json')
    data = _service(BOUNDARIES + '/query?' + urllib.parse.urlencode(params), feedback)
    return [f['attributes'] for f in data.get('features', [])]


def _service(url, feedback):
    try:  # the service drops connections now and then
        data = stac._request(url, None, feedback, attempts=RETRIES)
    except stac.StacError as e:
        raise MtbsError(f'MTBS map service request failed after {RETRIES} tries: {e}') from None
    if data is None:  # cancelled
        return {}
    if 'error' in data:
        raise MtbsError(f'MTBS map service error: {json.dumps(data["error"])[:300]}')
    return data


def median_thresholds(fires, assessment=None, exclude=(), min_fires=5, offset=None):
    """Median low, moderate and high thresholds (raw dNBR units) over fires, each over
    the fires that mapped that class. Uses only fires of the same assessment type
    when there are at least min_fires of them. With offset (this pair's dNBR offset,
    raw units), each fire's threshold is taken relative to its own MTBS offset and the
    median is moved to this pair's offset: thresholds are set on uncorrected dNBR, so
    a pair whose unburned land reads 0.1 needs thresholds 0.1 higher. On 21 validation
    fires this raised the mean kappa against MTBS from 0.58 to 0.61.
    fires: map service attribute dicts, or records (see service_style).
    Returns (thresholds, counts, n fires, scope)."""
    pool = [f for f in fires if f.get('fire_id') not in exclude]
    scope = 'all assessment types'
    if assessment:
        same = [f for f in pool if str(f.get('asmnt_type', '')).lower().startswith(assessment.lower())]
        if len(same) >= min_fires:
            pool, scope = same, f'{assessment.lower()} assessments'
    thresholds, counts = [], []
    for k in KEYS:
        vals = [f[k] for f in pool if f.get(k) is not None and -NONE < f[k] < NONE]
        if offset is not None:
            vals = [f[k] - f['dnbr_offst'] for f in pool if f.get(k) is not None and -NONE < f[k] < NONE
                    and f.get('dnbr_offst') is not None and -NONE < f['dnbr_offst'] < NONE]
        counts.append(len(vals))
        thresholds.append(statistics.median(vals) / 1000.0 + (offset or 0.0) if vals else None)
    return thresholds, counts, len(pool), scope


# ---- the local cache of the USGS files --------------------------------------------

def cache_dir():
    """<QGIS profile>/wildfire_analysis_toolkit/mtbs, or $FIREANALYSIS_MTBS_CACHE."""
    path = os.environ.get(CACHE_ENV)
    if not path:
        from qgis.core import QgsApplication
        path = os.path.join(QgsApplication.qgisSettingsDirPath(), 'wildfire_analysis_toolkit', 'mtbs')
    os.makedirs(path, exist_ok=True)
    return path


def _state_path():
    return os.path.join(cache_dir(), 'state.json')


def _state():
    try:
        with open(_state_path(), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state):
    tmp = _state_path() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, _state_path())


def cached_path(name):
    """The cached shapefile of a dataset, or None."""
    path = os.path.join(cache_dir(), name, DATASETS[name][1])
    return path if os.path.exists(path) else None


def cache_info(name):
    """{'last_modified', 'checked', 'path'} of a cached dataset (empty when none)."""
    info = dict(_state().get(name, {}))
    if cached_path(name):
        info['path'] = cached_path(name)
    return info


def _request(url, method='get', headers=None, feedback=None):
    """(status, headers dict lowercased, body bytes) through QGIS's network stack."""
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest
    req = QNetworkRequest(QUrl(url))
    for k, v in (headers or {}).items():
        req.setRawHeader(k.encode(), v.encode())
    blocking = QgsBlockingNetworkRequest()
    err = getattr(blocking, method)(req, True, feedback)
    if feedback is not None and feedback.isCanceled():
        raise MtbsError('Cancelled.')
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise MtbsError(f'Request to {url} failed: {blocking.errorMessage()}')
    reply = blocking.reply()
    status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
    heads = {bytes(k).decode().lower(): bytes(reply.rawHeader(k)).decode() for k in reply.rawHeaderList()}
    return int(status or 200), heads, bytes(reply.content())


def _download(url, path, size, feedback):
    """Fetch url to path in CHUNK-sized ranged requests (bounded memory, cancellable)."""
    part = path + '.part'
    done = 0
    with open(part, 'wb') as out:
        while size is None or done < size:
            end = done + CHUNK - 1 if size is None else min(done + CHUNK, size) - 1
            for attempt in range(RETRIES):
                try:
                    status, _, body = _request(url, headers={'Range': f'bytes={done}-{end}'}, feedback=feedback)
                    break
                except MtbsError:
                    if attempt == RETRIES - 1 or (feedback is not None and feedback.isCanceled()):
                        raise
                    time.sleep(2 * (attempt + 1))
            if status == 200:  # the server ignored the range: the whole file came back
                out.seek(0)
                out.truncate()
                out.write(body)
                done = len(body)
                break
            out.write(body)
            done += len(body)
            if not body:
                break
            if feedback is not None and size:
                feedback.setProgress(100.0 * done / size)
            if size is None and len(body) < CHUNK:
                break
    if size is not None and done != size:
        raise MtbsError(f'Download of {url} stopped at {done:,} of {size:,} bytes.')
    os.replace(part, path)


def _extract(zip_path, folder):
    """Unzip the shapefile into folder (replacing it); only plain file names are taken."""
    tmp = folder + '.new'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = os.path.basename(info.filename)
            if not name or name != info.filename.split('/')[-1] or name.startswith('.'):
                continue
            with z.open(info) as src, open(os.path.join(tmp, name), 'wb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    old = folder + '.old'
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(folder):
        os.replace(folder, old)
    os.replace(tmp, folder)
    shutil.rmtree(old, ignore_errors=True)


def ensure(name, feedback=None, allow_download=True, force_check=False):
    """Path of the cached shapefile of a dataset ('records' or 'perimeters'), fetching
    or refreshing it as needed. At most one HEAD check per CHECK_EVERY; a newer USGS
    file is downloaded only when allow_download. When the network fails, a cached copy
    is used with a warning. None when there is no copy and downloading is not allowed."""
    url, shp, label = DATASETS[name]
    folder = os.path.join(cache_dir(), name)
    have = cached_path(name)
    state = _state()
    s = state.get(name, {})
    if have and not force_check and time.time() - s.get('checked', 0) < CHECK_EVERY:
        return have
    try:
        _, heads, _ = _request(url, 'head', feedback=feedback)
    except MtbsError as e:
        if have:
            _warn(feedback, f'Could not check for a newer {label} ({e}); using the copy from '
                            f'{s.get("last_modified", "an earlier download")}.')
            return have
        raise MtbsError(f'Could not reach the {label}: {e}') from None
    modified = heads.get('last-modified')
    size = int(heads['content-length']) if heads.get('content-length', '').isdigit() else None
    if have and modified and modified == s.get('last_modified'):
        s['checked'] = time.time()
        state[name] = s
        _save_state(state)
        return have
    if not allow_download:
        return have
    if feedback is not None:
        feedback.pushInfo(f'Downloading the {label}' + (f', {size / 1e6:,.0f} MB' if size else '')
                          + (' (a newer copy)' if have else '') + '...')
    zip_path = os.path.join(cache_dir(), name + '.zip')
    _download(url, zip_path, size, feedback)
    _extract(zip_path, folder)
    os.remove(zip_path)
    state = _state()
    state[name] = {'last_modified': modified, 'checked': time.time(), 'url': url}
    _save_state(state)
    if name == 'records':
        _records_cache.clear()
    return cached_path(name)


def _warn(feedback, text):
    if feedback is not None:
        getattr(feedback, 'pushWarning', feedback.pushInfo)(text)


# ---- fire records -------------------------------------------------------------------

_records_cache = {}  # (path, mtime) -> list of records


def _clean(v):
    return v.strip() if isinstance(v, str) else v


def load_records(path):
    """Every fire in a records (or perimeters) shapefile as a dict of its attributes,
    plus lon, lat (the burned area's centre) and year. Cached per file."""
    key = (path, os.path.getmtime(path))
    if key in _records_cache:
        return _records_cache[key]
    from osgeo import ogr
    ds = ogr.Open(path)
    if ds is None:
        raise MtbsError(f'Could not open {path}.')
    layer = ds.GetLayer(0)
    defn = layer.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    layer.SetIgnoredFields(['OGR_GEOMETRY'])
    out = []
    for f in layer:
        out.append(record(dict((n.lower(), _clean(f.GetField(n))) for n in names)))
    _records_cache.clear()
    _records_cache[key] = out
    return out


def record(attrs):
    """Normalize an MTBS attribute dict (USGS file field names, any case): adds lon, lat,
    year, and the map-service keys median_thresholds reads."""
    r = {str(k).lower(): _clean(v) for k, v in attrs.items()}
    for k in ('burnbndlat', 'burnbndlon'):
        try:
            r[k] = float(r.get(k))
        except (TypeError, ValueError):
            r[k] = None
    r['lat'], r['lon'] = r.get('burnbndlat'), r.get('burnbndlon')
    ig = str(r.get('ig_date') or '')
    r['year'] = int(ig[:4]) if ig[:4].isdigit() else None
    for k in ('low_t', 'mod_t', 'high_t', 'dnbr_offst', 'dnbr_stddv', 'incgreen_t', 'burnbndac'):
        v = r.get(k)
        try:
            r[k] = None if v is None or v == '' else float(v)
        except (TypeError, ValueError):
            r[k] = None
    r.update({'fire_id': r.get('event_id'), 'fire_name': r.get('incid_name'), 'fire_type': r.get('incid_type'),
              'acres': r.get('burnbndac'), 'low_threshold': r['low_t'], 'moderate_threshold': r['mod_t'],
              'high_threshold': r['high_t']})
    return r


def is_record(names):
    """True when a layer's field names are MTBS's (the USGS files or our exports)."""
    lower = {n.lower() for n in names}
    return {'event_id', 'ig_date', 'low_t'} <= lower


def display_name(r):
    """'DRAGON BRAVO' -> 'Dragon Bravo'."""
    name = str(r.get('incid_name') or '').strip()
    return name.title() if name.isupper() else name


def describe(r):
    """'Dragon Bravo (AZ3623811210320250704), ignited 2025-07-04, 148,455 ac, Extended'."""
    acres = f', {r["burnbndac"]:,.0f} ac' if r.get('burnbndac') else ''
    return (f'{display_name(r) or "Unnamed"} ({r.get("event_id")}), ignited {r.get("ig_date")}{acres}, '
            f'{r.get("asmnt_type") or "assessment unknown"}')


def records(feedback=None):
    """All MTBS fire records, from the cache (fetched or refreshed as needed)."""
    return load_records(ensure('records', feedback))


def _norm(text):
    return re.sub(r'[^A-Z0-9]+', ' ', str(text or '').upper()).strip()


def search(recs, text=None, first_year=None, last_year=None, bbox=None, wildfires_only=False):
    """Records matching every given filter. text: an event id, or a name (whole words,
    any case), optionally ending in a year ('Dragon Bravo 2025'). bbox: (west, south,
    east, north) in degrees, against the burned area's centre."""
    year = None
    words = ''
    if text:
        t = text.strip()
        if EVENT_ID_RE.match(t.upper()):
            return [r for r in recs if str(r.get('event_id', '')).upper() == t.upper()]
        m = re.match(r'^(.*?)[\s,]+((?:19|20)\d{2})$', t)
        if m:
            t, year = m.group(1), int(m.group(2))
        words = _norm(t)
    out = []
    for r in recs:
        y = r.get('year')
        if year is not None and y != year:
            continue
        if first_year is not None and (y is None or y < first_year):
            continue
        if last_year is not None and (y is None or y > last_year):
            continue
        if wildfires_only and str(r.get('incid_type', '')).lower() != 'wildfire':
            continue
        if words and f' {words} ' not in f' {_norm(r.get("incid_name"))} ':
            continue
        if bbox is not None:
            if r.get('lon') is None or not (bbox[0] <= r['lon'] <= bbox[2] and bbox[1] <= r['lat'] <= bbox[3]):
                continue
        out.append(r)
    # exact names first, then the largest
    out.sort(key=lambda r: (_norm(r.get('incid_name')) != words, -(r.get('burnbndac') or 0)))
    return out


def resolve(recs, text):
    """The one record an MTBS fire id or 'name year' names; MtbsError listing the
    candidates when there are none or several."""
    found = search(recs, text)
    exact = [r for r in found if _norm(r.get('incid_name')) == _norm(re.sub(r'[\s,]+(19|20)\d{2}$', '', text.strip()))]
    if len(found) > 1 and len(exact) == 1:
        found = exact
    if len(found) == 1:
        return found[0]
    if not found:
        raise MtbsError(f'No MTBS fire matches "{text}". Give the MTBS fire id (event_id), or the name and '
                        'year as MTBS spells it; Find MTBS fires lists them.')
    shown = '; '.join(describe(r) for r in found[:8]) + ('; ...' if len(found) > 8 else '')
    raise MtbsError(f'{len(found)} MTBS fires match "{text}": {shown}. Give the fire id, or add the year.')


def by_irwin(recs, irwin):
    """The record with this IRWIN incident id (WFIGS attr_IrwinID), or None."""
    key = str(irwin or '').strip('{} ').upper()
    if not key:
        return None
    return next((r for r in recs if str(r.get('irwinid') or '').strip('{} ').upper() == key), None)


def by_location(recs, geom4326, start, end, area_ac=None, days_before=7, days_after=30):
    """Records whose burned-area centre is inside geom4326 (QgsGeometry, degrees) and that
    ignited between start - days_before and end + days_after (datetime.date). When
    several, the one closest in size to area_ac comes first. Returns the list."""
    from qgis.core import QgsPointXY, QgsGeometry
    box = geom4326.boundingBox()
    out = []
    for r in recs:
        if r.get('lon') is None or not box.contains(QgsPointXY(r['lon'], r['lat'])):
            continue
        try:
            ig = datetime.date.fromisoformat(str(r.get('ig_date'))[:10])
        except ValueError:
            continue
        if start is not None and ig < start - datetime.timedelta(days=days_before):
            continue
        if end is not None and ig > end + datetime.timedelta(days=days_after):
            continue
        if geom4326.contains(QgsGeometry.fromPointXY(QgsPointXY(r['lon'], r['lat']))):
            out.append(r)
    if area_ac:
        out.sort(key=lambda r: abs(math.log(max(r.get('burnbndac') or 1, 1) / max(area_ac, 1))))
    return out


def distance_km(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def nearby(lon, lat, radius_km, first_year, last_year, feedback=None):
    """MTBS wildfires within radius_km of (lon, lat) (their burned-area centre) between the
    years, from the records cache; the map service when the records can't be had.
    Returns (fires, source)."""
    try:
        recs = records(feedback)
    except MtbsError as e:
        _warn(feedback, f'{e}; asking the MTBS map service instead.')
        return nearby_fires(lon, lat, radius_km, first_year, last_year, feedback=feedback), 'MTBS map service'
    fires = [r for r in search(recs, first_year=first_year, last_year=last_year, wildfires_only=True)
             if r.get('lon') is not None and distance_km(lon, lat, r['lon'], r['lat']) <= radius_km]
    return fires, 'MTBS fire records (USGS)'


def thresholds(r, offset=None):
    """This fire's MTBS low, moderate and high thresholds in raw dNBR units; with offset
    (this pair's), each relative to MTBS's offset and moved to this pair's. A class
    MTBS did not map comes back as NONE / 1000 (9.999); a missing value as None."""
    out = []
    mtbs_offset = r.get('dnbr_offst')
    for k in RECORD_KEYS:
        v = r.get(k)
        if v is None:
            out.append(None)
        elif not -NONE < v < NONE:
            out.append(NONE / 1000.0)
        elif offset is not None and mtbs_offset is not None and -NONE < mtbs_offset < NONE:
            out.append((v - mtbs_offset) / 1000.0 + offset)
        else:
            out.append(v / 1000.0)
    return out


# ---- scenes -----------------------------------------------------------------------

def parse_scene(scene_id):
    """An MTBS scene id -> dict(kind, platform, date (datetime.date), hls (L30, S30 or
    None), planetary ('landsat' or 's2'), where (path/row or tile)).
    '803503420170919' is Landsat 8 path 35 row 34; 'C12SUF20260620_30m' Sentinel-2C tile
    12SUF. ValueError for anything else."""
    s = str(scene_id or '').strip().split('_')[0]
    if len(s) < 9:
        raise ValueError(f'Unknown MTBS scene id {scene_id!r}')
    try:
        date = datetime.date(int(s[-8:-4]), int(s[-4:-2]), int(s[-2:]))
    except ValueError:
        raise ValueError(f'Unknown MTBS scene id {scene_id!r}') from None
    if s[0].isalpha() and s[1:3].isdigit() and len(s) == 14:
        return {'kind': 'sentinel-2', 'platform': f'sentinel-2{s[0].lower()}', 'date': date, 'hls': 'S30',
                'planetary': 's2', 'where': s[1:6]}
    if s[0].isdigit() and len(s) == 15 and s.isdigit():
        n = int(s[0])
        return {'kind': 'landsat', 'platform': f'landsat-{n}', 'date': date, 'hls': 'L30' if n in (8, 9) else None,
                'planetary': 'landsat', 'where': f'{int(s[1:4])}/{int(s[4:7])}'}
    raise ValueError(f'Unknown MTBS scene id {scene_id!r}')


# ---- perimeters -------------------------------------------------------------------

SERVICE_BATCH = 40  # fire ids per map service query


def perimeters(recs, feedback=None, allow_download=False):
    """Burned area boundaries of several fires. Returns (found, missing, note): found maps
    event_id -> (list of GeoJSON geometries in EPSG:4326, source); missing lists the
    event ids found nowhere; note says why (None when nothing is missing)."""
    found = {}
    ids = [r['event_id'] for r in recs if r.get('event_id')]
    for i in range(0, len(ids), SERVICE_BATCH):
        batch = ids[i:i + SERVICE_BATCH]
        where = 'fire_id IN (' + ','.join(f"'{x}'" for x in batch) + ')'
        try:
            data = _service(BOUNDARIES + '/query?' + urllib.parse.urlencode(
                {'where': where, 'outFields': 'fire_id', 'outSR': 4326, 'f': 'geojson'}), feedback)
        except MtbsError as e:
            _warn(feedback, f'{e}; trying the MTBS perimeter file.')
            break
        for f in data.get('features', []):
            fid = (f.get('properties') or {}).get('fire_id')
            if fid and f.get('geometry'):
                found.setdefault(fid, ([], 'MTBS map service (USFS EDW)'))[0].append(f['geometry'])
        if feedback is not None and feedback.isCanceled():
            return found, [x for x in ids if x not in found], 'Cancelled.'
    missing = [x for x in ids if x not in found]
    if not missing:
        return found, [], None
    path = None
    try:
        path = ensure('perimeters', feedback, allow_download=allow_download)
    except MtbsError as e:
        _warn(feedback, str(e))
    if path:
        for event_id, geoms in _from_file_many(path, missing).items():
            found[event_id] = (geoms, 'MTBS burned area boundaries file (USGS, cached)')
        missing = [x for x in ids if x not in found]
        if missing and allow_download:  # an older cached file may predate these fires
            path = ensure('perimeters', feedback, allow_download=True, force_check=True)
            for event_id, geoms in _from_file_many(path, missing).items():
                found[event_id] = (geoms, 'MTBS burned area boundaries file (USGS, cached)')
            missing = [x for x in ids if x not in found]
    if not missing:
        return found, [], None
    if path:
        return found, missing, 'not in the MTBS map service or the MTBS perimeter file'
    return found, missing, ('not in the MTBS map service yet; their perimeters are only in the full MTBS '
                            'perimeter file (a one-time 420 MB download, then cached). Allow that download, or '
                            'give the burned-area polygons yourself')


def perimeter(r, feedback=None, allow_download=False):
    """One fire's burned area boundary as a GeoJSON FeatureCollection (EPSG:4326) whose
    properties are the fire's record. Returns (geojson, source) or (None, reason)."""
    found, missing, note = perimeters([r], feedback, allow_download)
    if r['event_id'] in found:
        geoms, source = found[r['event_id']]
        return _collection(r, geoms), source
    return None, f'{display_name(r)} ({r["event_id"]}) is {note}.'


def _from_file_many(path, event_ids):
    from osgeo import ogr
    out = {}
    ds = ogr.Open(path)
    layer = ds.GetLayer(0)
    for i in range(0, len(event_ids), 200):
        layer.SetAttributeFilter('event_id IN (' + ','.join(f"'{x}'" for x in event_ids[i:i + 200]) + ')')
        for f in layer:
            g = f.GetGeometryRef()
            if g is not None:
                out.setdefault(f.GetField('event_id'), []).append(json.loads(g.ExportToJson()))
    return out


PUBLISHED = ('event_id', 'irwinid', 'incid_name', 'incid_type', 'map_id', 'map_prog', 'asmnt_type', 'burnbndac',
             'burnbndlat', 'burnbndlon', 'ig_date', 'pre_id', 'post_id', 'perim_id', 'dnbr_offst', 'dnbr_stddv',
             'nodata_t', 'incgreen_t', 'low_t', 'mod_t', 'high_t', 'comment')


def _collection(r, geoms):
    props = {k: r.get(k) for k in PUBLISHED}
    return {'type': 'FeatureCollection',
            'features': [{'type': 'Feature', 'properties': props, 'geometry': g} for g in geoms]}


# ---- MTBS's published classes -------------------------------------------------------

def published_classes(like, year, feedback=None):
    """MTBS's dnbr6 classes for a year on the grid of the GDAL dataset like (nearest
    neighbour), as a uint8 array (0 = none), from the image service. None when the
    grid is too large; MtbsError when the request fails."""
    import numpy as np
    from osgeo import gdal
    gt = like.GetGeoTransform()
    w, h = like.RasterXSize, like.RasterYSize
    if max(w, h) > MAX_COMPARE_PX:
        return None
    srs = like.GetSpatialRef()
    epsg = srs.GetAuthorityCode(None) if srs is not None else None
    if not epsg:
        raise MtbsError('The grid has no EPSG code to ask the MTBS image service for.')
    params = {'bbox': ','.join(map(str, (gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3]))),
              'bboxSR': epsg, 'imageSR': epsg, 'size': f'{w},{h}', 'format': 'tiff', 'pixelType': 'U8',
              'interpolation': 'RSP_NearestNeighbor', 'noData': 0, 'f': 'image',
              'mosaicRule': json.dumps({'mosaicMethod': 'esriMosaicAttribute', 'where': f'year={int(year)}'})}
    url = IMAGESERVER + '/exportImage?' + urllib.parse.urlencode(params)
    for attempt in range(RETRIES):
        try:
            status, heads, body = _request(url, feedback=feedback)
            break
        except MtbsError:
            if attempt == RETRIES - 1 or (feedback is not None and feedback.isCanceled()):
                raise
            time.sleep(2 * (attempt + 1))
    if not body.startswith((b'II', b'MM')):
        raise MtbsError(f'The MTBS image service did not return an image: {body[:200]!r}')
    mem = f'/vsimem/mtbs_dnbr6_{os.getpid()}_{time.time_ns()}.tif'
    gdal.FileFromMemBuffer(mem, body)
    try:
        ds = gdal.Open(mem)
        if ds.RasterXSize != w or ds.RasterYSize != h:
            raise MtbsError('The MTBS image service returned a different grid.')
        return ds.GetRasterBand(1).ReadAsArray().astype(np.uint8)
    finally:
        gdal.Unlink(mem)


def compare(ours, theirs, inside):
    """Agreement of our classes with MTBS's inside the burned area, over pixels both
    mapped (classes 1-5). Returns dict(pixels, agreement %, kappa, within_one %,
    ours %, theirs % per class 1-5) or None when there is no overlap."""
    import numpy as np
    both = inside & (ours >= 1) & (ours <= 5) & (theirs >= 1) & (theirs <= 5)
    n = int(both.sum())
    if n == 0:
        return None
    a, b = ours[both].astype(int) - 1, theirs[both].astype(int) - 1
    confusion = np.zeros((5, 5), np.int64)
    np.add.at(confusion, (a, b), 1)
    po = np.trace(confusion) / n
    pe = (confusion.sum(1) * confusion.sum(0)).sum() / n ** 2
    # within one class among the ordered severity classes 1-4 (greenness counts as unburned to low)
    oa, ob = np.where(a == 4, 0, a), np.where(b == 4, 0, b)
    return {'pixels': n, 'agreement': 100.0 * po, 'kappa': float((po - pe) / (1 - pe)) if pe < 1 else 1.0,
            'within_one': 100.0 * float((np.abs(oa - ob) <= 1).mean()),
            'ours': [100.0 * float(v) / n for v in confusion.sum(1)],
            'theirs': [100.0 * float(v) / n for v in confusion.sum(0)]}
