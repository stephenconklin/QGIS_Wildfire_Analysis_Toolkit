"""Suggest HLS image pairs: rank pre/post-fire HLS dates for a fire by the clear
share inside the burned area and how well the seasons match, following the
MTBS scene-selection rules."""
import datetime

from qgis.core import (QgsFeature, QgsFeatureSink, QgsFields, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingParameterDateTime, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterNumber)

from .. import VERSION, earthdata, hls
from ..compat import date_type, field, integer_type, polygon_source, warn
from .find_hls_scenes import aoi_geometry, search_granules

INITIAL, EXTENDED = 0, 1
WORKERS = 6  # Fmask windows read at once
ASSESSMENTS = ['Initial (soon after the fire)', 'Extended (next growing season)']


def _no_geometry():
    from qgis.core import Qgis, QgsWkbTypes
    try:
        return Qgis.WkbType.NoGeometry
    except AttributeError:
        return QgsWkbTypes.NoGeometry


def years_back(d, n):
    """The same day n years earlier (28 February for 29 February)."""
    try:
        return d.replace(year=d.year - n)
    except ValueError:
        return d.replace(year=d.year - n, day=28)


def doy_gap(a, b):
    """Days between two dates' days of year, across the year end."""
    d = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(d, 365 - d)


class SuggestHlsPairs(QgsProcessingAlgorithm):
    AOI = 'AOI'
    FIRE_START = 'FIRE_START'
    FIRE_END = 'FIRE_END'
    ASSESSMENT = 'ASSESSMENT'
    SENSORS = 'SENSORS'
    MIN_CLEAR = 'MIN_CLEAR'
    SEASON_DAYS = 'SEASON_DAYS'
    PRE_YEARS = 'PRE_YEARS'
    POST_DAYS = 'POST_DAYS'
    COUNT = 'COUNT'
    OUTPUT = 'OUTPUT'

    def name(self):
        return 'suggesthlspairs'

    def displayName(self):
        return 'Suggest HLS image pairs'

    def group(self):
        return 'Imagery'

    def groupId(self):
        return 'imagery'

    def shortHelpString(self):
        return (
            '<p>Finds HLS dates before and after a fire and ranks pre/post pairs, the way MTBS '
            'analysts choose scenes:</p><ul>'
            '<li><b>Post-fire:</b> initial assessment, from the fire end date to the given number of '
            'days after it; extended, the next growing season (June to September of the following '
            'year).</li>'
            '<li><b>Pre-fire:</b> the same season (within the given days of the post-fire day of '
            'year) in the years before the fire, and before the fire start.</li>'
            '<li>Both dates must be clear inside the area (Fmask: no cloud, adjacent cloud, shadow or '
            'snow) for at least the minimum share. Pairs are ranked by the day-of-year gap (in 5-day '
            'steps), then fewer years apart, then the lower of the two clear shares, then the earliest '
            'post-fire date.</li></ul>'
            '<p>Reads Fmask for every candidate date, so it needs the Earthdata login and can take '
            'a few minutes. The <i>pre_granules</i> and <i>post_granules</i> fields go straight into '
            'the granule ids box of <i>Fetch HLS NBR</i>.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.AOI, 'Burned area or area of interest', [polygon_source()]))
        self.addParameter(QgsProcessingParameterDateTime(self.FIRE_START, 'Fire start (ignition) date',
                                                         type=date_type()))
        self.addParameter(QgsProcessingParameterDateTime(self.FIRE_END, 'Fire end (containment) date',
                                                         type=date_type()))
        self.addParameter(QgsProcessingParameterEnum(
            self.ASSESSMENT, 'Assessment', options=ASSESSMENTS, defaultValue=INITIAL))
        self.addParameter(QgsProcessingParameterEnum(
            self.SENSORS, 'Sensors', options=[hls.PRODUCTS[s]['label'] for s in hls.SENSORS],
            allowMultiple=True, defaultValue=list(range(len(hls.SENSORS)))))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_CLEAR, 'Minimum clear share inside the area (%)', type=integer_type(),
            minValue=0, maxValue=100, defaultValue=95))
        self.addParameter(QgsProcessingParameterNumber(
            self.SEASON_DAYS, 'Pre-fire: days of year from the post-fire date', type=integer_type(),
            minValue=1, maxValue=120, defaultValue=30))
        self.addParameter(QgsProcessingParameterNumber(
            self.PRE_YEARS, 'Pre-fire: years before the fire to search', type=integer_type(),
            minValue=1, maxValue=5, defaultValue=2))
        self.addParameter(QgsProcessingParameterNumber(
            self.POST_DAYS, 'Initial assessment: days after the fire end to search', type=integer_type(),
            minValue=5, maxValue=365, defaultValue=60))
        self.addParameter(QgsProcessingParameterNumber(
            self.COUNT, 'Pairs to list', type=integer_type(), minValue=1, defaultValue=10))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, 'Image pairs'))

    def _dates(self, rows, aoi, context, feedback, label):
        """{(date, sensor): (clear %, [granules])} for every date in rows. Fmask is read
        for several dates at once; messages are written here, on the algorithm's thread."""
        from concurrent.futures import ThreadPoolExecutor
        from .fetch_hls_nbr import aoi_grid, clear_share_on
        groups = sorted({(r[0], r[2]) for r in rows})
        feedback.pushInfo(f'{label}: {len(groups)} dates to check.')
        if not groups:
            return {}
        try:
            earthdata.configure_gdal()
        except earthdata.EarthdataError as e:
            raise QgsProcessingException(str(e)) from None
        grid = aoi_grid(aoi, context.transformContext())
        jobs = {g: [r[1] for r in rows if (r[0], r[2]) == g] for g in groups}

        def one(group):
            error = None
            for _attempt in range(2):  # a read now and then fails under load; try once more
                if feedback.isCanceled():
                    return group, None, 'cancelled'
                try:
                    return group, clear_share_on(jobs[group], grid, feedback), None
                except Exception as e:  # reported below, on this thread
                    error = str(e)
            return group, None, error

        out = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for (date, sensor), result, error in pool.map(one, groups):
                if error == 'cancelled' or feedback.isCanceled():
                    continue
                if error or result is None or result[0] is None:
                    warn(feedback, f'  {date} {sensor}: could not read Fmask ({error}); skipped.')
                    continue
                feedback.pushInfo(f'  {date} {sensor}: {result[0]:.0f} % clear')
                out[(datetime.date.fromisoformat(date), sensor)] = (result[0], jobs[(date, sensor)])
        return out

    def processAlgorithm(self, parameters, context, feedback):
        aoi = aoi_geometry(self.parameterAsSource(parameters, self.AOI, context), context)
        start = self.parameterAsDateTime(parameters, self.FIRE_START, context).date().toPyDate()
        end = self.parameterAsDateTime(parameters, self.FIRE_END, context).date().toPyDate()
        if end < start:
            raise QgsProcessingException('The fire end date is before the start date.')
        assessment = self.parameterAsEnum(parameters, self.ASSESSMENT, context)
        sensors = [hls.SENSORS[i] for i in self.parameterAsEnums(parameters, self.SENSORS, context)]
        min_clear = self.parameterAsInt(parameters, self.MIN_CLEAR, context)
        season = self.parameterAsInt(parameters, self.SEASON_DAYS, context)
        years = self.parameterAsInt(parameters, self.PRE_YEARS, context)
        count = self.parameterAsInt(parameters, self.COUNT, context)
        if assessment == INITIAL:
            post_from = end
            post_to = end + datetime.timedelta(days=self.parameterAsInt(parameters, self.POST_DAYS, context))
        else:
            post_from, post_to = datetime.date(start.year + 1, 6, 1), datetime.date(start.year + 1, 9, 30)
        post_to = min(post_to, datetime.date.today())
        if post_to < post_from:
            raise QgsProcessingException('The post-fire window has not started yet.')
        if start < datetime.date(2014, 1, 1):
            warn(feedback, 'HLS starts in 2013; for older fires use Landsat Collection 2 with '
                           'Prepare local scene NBR.')

        feedback.pushInfo(f'Post-fire window {post_from} to {post_to}.')
        post_rows = search_granules(aoi, post_from, post_to, sensors, 100, context, feedback)
        post = {k: v for k, v in self._dates(post_rows, aoi, context, feedback, 'Post-fire').items()
                if v[0] >= min_clear}
        if feedback.isCanceled():
            return {}
        if not post:
            raise QgsProcessingException(
                f'No post-fire date from {post_from} to {post_to} is at least {min_clear} % clear inside '
                'the area. Lower the minimum clear share or widen the window.')
        feedback.setProgress(40)

        # Pre-fire windows: the post-fire dates' seasons, in each of the years before
        pre_rows = []
        lo_doy = min(d for d, _ in post)
        hi_doy = max(d for d, _ in post)
        for back in range(1, years + 1):
            frm = years_back(lo_doy, back) - datetime.timedelta(days=season)
            to = years_back(hi_doy, back) + datetime.timedelta(days=season)
            to = min(to, start - datetime.timedelta(days=1))
            if to >= frm:
                feedback.pushInfo(f'Pre-fire window {frm} to {to}.')
                pre_rows += search_granules(aoi, frm, to, sensors, 100, context, feedback)
        pre = {k: v for k, v in self._dates(pre_rows, aoi, context, feedback, 'Pre-fire').items()
               if v[0] >= min_clear}
        if feedback.isCanceled():
            return {}
        feedback.setProgress(90)

        pairs = []
        for (pd, ps), (pc, pg) in post.items():
            for (qd, qs), (qc, qg) in pre.items():
                gap = doy_gap(pd, qd)
                if gap <= season and qd < start:
                    pairs.append((gap, -round(min(pc, qc)), pd.year - qd.year, qd, qs, qc, qg, pd, ps, pc, pg))
        # 5-day season bins, then fewer years apart, then clearer (whole percent), then the earliest post-fire date
        pairs.sort(key=lambda p: (p[0] // 5, p[2], p[1], p[7], p[3]))

        fields = QgsFields()
        for name, kind, length in (('rank', 'int', 0), ('pre_date', 'string', 10), ('pre_sensor', 'string', 3),
                                   ('pre_clear', 'double', 0), ('post_date', 'string', 10),
                                   ('post_sensor', 'string', 3), ('post_clear', 'double', 0),
                                   ('doy_gap', 'int', 0), ('years', 'int', 0),
                                   ('pre_granules', 'string', 254), ('post_granules', 'string', 254)):
            fields.append(field(name, kind, length))
        sink, dest_id = self.parameterAsSink(parameters, self.OUTPUT, context, fields, _no_geometry())
        for rank, (gap, _, yrs, qd, qs, qc, qg, pd, ps, pc, pg) in enumerate(pairs[:count], start=1):
            feat = QgsFeature(fields)
            feat.setAttributes([rank, qd.isoformat(), qs, round(qc, 1), pd.isoformat(), ps, round(pc, 1),
                                gap, yrs, ' '.join(qg), ' '.join(pg)])
            sink.addFeature(feat, QgsFeatureSink.FastInsert)
            feedback.pushInfo(f'{rank}. pre {qd} {qs} ({qc:.0f} % clear) / post {pd} {ps} ({pc:.0f} % clear), '
                              f'{gap} days apart in the season, {yrs} year(s)')
        if not pairs:
            warn(feedback, 'No pre-fire date matches the post-fire season closely enough. Raise the days of '
                           'year, search more years, or lower the minimum clear share.')
        return {self.OUTPUT: dest_id}
