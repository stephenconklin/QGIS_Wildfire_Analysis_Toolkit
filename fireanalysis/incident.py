"""Fire metadata from the attributes of a perimeter layer.

WFIGS Interagency Fire Perimeters (NIFC; the poly_ and attr_ fields of the
ArcGIS service and its downloads) carry the incident record from IRWIN: name,
discovery, containment, control and fire-out dates, size, cause and ignition
point. Field names are matched with or without the poly_/attr_ prefix and
case-insensitively, so the Current, Year-to-date and archive layers all work.
Dates may be date-times, text (GeoJSON exports) or epoch milliseconds (Esri JSON).

MTBS perimeters and records (the USGS files, or Find MTBS fires output) carry the
MTBS record instead: read_mtbs returns it, and fire_details uses its name and
ignition date when nothing better is given.
"""
import datetime
import re

from qgis.core import QgsProcessingException

# Where each item comes from, in order of preference (names without the prefix)
NAME = ('IncidentName',)
ID = ('UniqueFireIdentifier', 'IrwinID', 'IRWINID')
START = ('FireDiscoveryDateTime',)
END = (('ContainmentDateTime', 'containment'), ('ControlDateTime', 'control'), ('FireOutDateTime', 'fire out'))
ACRES = ('GISAcres', 'IncidentSize', 'FinalAcres')
TEXT = {'cause': ('FireCause',), 'state': ('POOState',), 'county': ('POOCounty',), 'unit': ('POOProtectingUnit',),
        'map_method': ('MapMethod',), 'feature_category': ('FeatureCategory',),
        'description': ('IncidentShortDescription',)}


class Fields:
    """Field lookup by name, with or without the WFIGS prefixes, any case."""

    def __init__(self, names):
        self.by_lower = {n.lower(): n for n in names}

    def find(self, key):
        for name in (f'poly_{key}', f'attr_{key}', key):
            if name.lower() in self.by_lower:
                return self.by_lower[name.lower()]
        return None


def _empty(v):
    if v is None:
        return True
    if hasattr(v, 'isNull') and v.isNull():
        return True
    return isinstance(v, str) and not v.strip()


def to_date(v, lon=None):
    """A datetime.date from a QDateTime, QDate, datetime, ISO-like text or epoch
    milliseconds; None when empty or unreadable. WFIGS stores UTC, so an evening
    event in the western US falls on the next UTC day: with lon (degrees east), the
    time is shifted to local solar time (lon / 15 hours) before taking the date.
    Times without a zone count as UTC; plain dates are kept as they are."""
    if _empty(v):
        return None
    if hasattr(v, 'toUTC'):  # QDateTime
        if not v.isValid():
            return None
        v = v.toUTC()
        t = datetime.datetime(v.date().year(), v.date().month(), v.date().day(), v.time().hour(), v.time().minute())
    elif hasattr(v, 'toPyDate'):  # QDate
        return v.toPyDate() if v.isValid() else None
    elif isinstance(v, datetime.datetime):
        t = v.astimezone(datetime.timezone.utc).replace(tzinfo=None) if v.tzinfo else v
    elif isinstance(v, datetime.date):
        return v
    elif isinstance(v, (int, float)) and v > 1e11:  # epoch milliseconds
        t = datetime.datetime.fromtimestamp(v / 1000.0, datetime.timezone.utc).replace(tzinfo=None)
    else:
        text = str(v).strip()
        try:
            d = datetime.datetime.fromisoformat(text.replace('Z', '+00:00').replace('/', '-'))
            t = d.astimezone(datetime.timezone.utc).replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            m = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', text)
            return datetime.date(*map(int, m.groups())) if m else None
    if lon is not None:
        t += datetime.timedelta(hours=lon / 15.0)
    return t.date()


def _number(v):
    if _empty(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first(f, fields, keys):
    """The first non-empty value of f among the fields named by keys."""
    for key in keys:
        name = fields.find(key)
        if name is not None and not _empty(f[name]):
            return f[name]
    return None


def read_incident(source):
    """The fire record of a perimeter layer (a QgsFeatureSource), or None when its
    fields are not WFIGS's. Keys: source, name, id, start, start_field, end,
    end_field, end_note, acres, ignition (lat, lon), perimeter_date, the TEXT keys,
    features (count) and fires (the distinct incident ids or names; reading stops
    after six, so a long list means "many")."""
    if source is None:
        return None
    fields = Fields(source.fields().names())
    start_field = fields.find(START[0])
    if start_field is None:
        return None
    # Read until a few different fires have been seen: a whole WFIGS layer has tens of thousands
    feats, seen = [], set()
    for f in source.getFeatures():
        feats.append(f)
        seen.add(str(_first(f, fields, ID) or _first(f, fields, NAME) or ''))
        if len(seen - {''}) > 5:
            break
    if not feats:
        return None

    def get(f, keys):
        return _first(f, fields, keys)

    fires = sorted(seen - {''})
    f = feats[0]
    lat, lon = _number(get(f, ('InitialLatitude',))), _number(get(f, ('InitialLongitude',)))
    ignition = (lat, lon) if lat is not None and lon is not None else None
    if lon is None and f.hasGeometry():  # local time from the perimeter's centroid instead
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
        c = f.geometry().centroid()
        c.transform(QgsCoordinateTransform(source.sourceCrs(), QgsCoordinateReferenceSystem('EPSG:4326'),
                                           QgsProject.instance()))
        lon = c.asPoint().x()

    def day(v):
        return to_date(v, lon)
    info = {'source': 'WFIGS Interagency Fire Perimeters', 'features': source.featureCount(), 'fires': fires,
            'name': str(get(f, NAME) or '').strip() or None, 'id': get(f, ID),
            'irwin': str(get(f, ('IrwinID',)) or '').strip('{} ').upper() or None,
            'start': day(f[start_field]), 'start_field': start_field,
            'acres': _number(get(f, ACRES)), 'perimeter_date': day(get(f, ('PolygonDateTime',))),
            'ignition': ignition, 'dates': 'local solar time at longitude {:.2f}'.format(lon) if lon is not None
            else 'UTC'}
    for key, names in TEXT.items():
        v = get(f, names)
        info[key] = None if v is None else str(v)

    # End of the fire: containment, else control, else fire out; else the last ICS-209
    # report once 100 % contained; else the perimeter's own date (the fire may have grown since)
    info['end'] = info['end_field'] = info['end_note'] = None
    info['end_estimated'] = False
    for key, label in END:
        d = day(get(f, (key,)))
        if d is not None:
            info.update(end=d, end_field=fields.find(key), end_note=f'{label} date')
            break
    else:
        contained = _number(get(f, ('PercentContained',)))
        d = day(get(f, ('ICS209RptForTimePeriodTo', 'ICS209ReportDateTime')))
        if contained is not None and contained >= 100 and d is not None:
            info.update(end=d, end_field=fields.find('ICS209RptForTimePeriodTo') or fields.find('ICS209ReportDateTime'),
                        end_note='estimated: no containment date; period end of the last ICS-209 report, '
                                 'at 100 % contained', end_estimated=True)
        elif info['perimeter_date'] is not None:
            info.update(end=info['perimeter_date'], end_field=fields.find('PolygonDateTime'),
                        end_note='estimated: no containment date; the perimeter\'s date (the fire may have '
                                 'grown since)', end_estimated=True)
    return info


def read_mtbs(source):
    """The MTBS record of a perimeter layer whose fields are MTBS's (see mtbs.is_record),
    or None. Stops when the layer holds several MTBS fires."""
    from . import mtbs
    if source is None or not mtbs.is_record(source.fields().names()):
        return None
    names = source.fields().names()
    first, ids = None, set()
    for f in source.getFeatures():
        attrs = {n: (None if _empty(f[n]) else f[n]) for n in names}
        ids.add(str(attrs.get(next(n for n in names if n.lower() == 'event_id')) or ''))
        first = first or attrs
        if len(ids - {''}) > 5:
            break
    if first is None:
        return None
    if len(ids - {''}) > 1:
        shown = ', '.join(sorted(ids - {''})[:5]) + (', ...' if len(ids - {''}) > 5 else '')
        raise QgsProcessingException(
            f'The perimeter layer holds several MTBS fires ({shown}). Select one fire and tick "Selected '
            'features only", or name the fire in the MTBS fire field.')
    for k, v in list(first.items()):  # QDate / QDateTime from a GeoPackage -> ISO text
        if hasattr(v, 'toString') and not isinstance(v, str):
            first[k] = to_date(v).isoformat() if to_date(v) else None
    return mtbs.record(first)


def fire_details(alg, parameters, context, feedback, perimeter_param, name_param, start_param, end_param,
                 mtbs_record=None, need_end=True):
    """(name, start, end, incident) for an algorithm, as datetime.date values: what
    the user gave, else the perimeter's WFIGS record, else mtbs_record (name and
    ignition date; MTBS has no end date). name is None when none has one; name_param
    may be None. end may be None when not need_end. Stops when the layer holds
    several fires or a needed date is missing."""
    incident = read_incident(alg.parameterAsSource(parameters, perimeter_param, context)) \
        if parameters.get(perimeter_param) not in (None, '') else None
    if incident and len(incident['fires']) > 1:
        shown = ', '.join(incident['fires'][:5]) + (', ...' if len(incident['fires']) > 5 else '')
        raise QgsProcessingException(
            f'The perimeter layer holds several fires ({shown}; {incident["features"]:,} features). Select one '
            'fire and tick "Selected features only", or export it to its own layer.')

    def given(param):
        if param is None or parameters.get(param) in (None, ''):
            return None
        d = alg.parameterAsDateTime(parameters, param, context)
        return d.date().toPyDate() if d.isValid() else None

    name = (alg.parameterAsString(parameters, name_param, context) or '').strip() if name_param else ''
    start, end = given(start_param), given(end_param)
    if incident:
        used = []
        if name_param and not name and incident['name']:
            name = incident['name']
            used.append(f'name {name}')
        if start is None and incident['start']:
            start = incident['start']
            used.append(f'start {start} ({incident["start_field"]})')
        if end is None and incident['end']:
            end = incident['end']
            used.append(f'end {end} ({incident["end_note"]}, {incident["end_field"]})')
        if used:
            feedback.pushInfo(f'From the perimeter\'s {incident["source"]} record: ' + '; '.join(used) + '.')
        if incident['end_estimated'] and given(end_param) is None:
            from .compat import warn
            warn(feedback, f'The fire end date {end} is an estimate (no containment, control or fire-out date in '
                           'the record). Set it if you know it.')
    if mtbs_record:
        from . import mtbs
        used = []
        if name_param and not name and mtbs.display_name(mtbs_record):
            name = mtbs.display_name(mtbs_record)
            used.append(f'name {name}')
        if start is None and to_date(mtbs_record.get('ig_date')):
            start = to_date(mtbs_record.get('ig_date'))
            used.append(f'ignition {start}')
        if used:
            feedback.pushInfo(f'From the MTBS record {mtbs_record.get("event_id")}: ' + '; '.join(used) + '.')
    if start is None or (end is None and need_end):
        missing = ' and '.join(w for w, d in (('start', start), ('end', end)) if d is None and (w == 'start' or need_end))
        raise QgsProcessingException(
            f'Set the fire {missing} date: ' + ('the fire\'s record has none.' if incident or mtbs_record
                                               else 'the perimeter has no WFIGS or MTBS fire record to read it from.'))
    if end is not None and end < start:
        raise QgsProcessingException(f'The fire end date ({end}) is before the start date ({start}).')
    return name or None, start, end, incident
