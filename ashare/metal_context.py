"""Bounded, forward-only macro evidence for gold/silver research.

These are daily observations with publication lags, not live quotes. Each model
input freezes the exact observations and their local availability; this latest
cache is never used to reconstruct a historical vintage.
"""
import json
import math
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .macro_sources import ASSETS
from .storage import digest, normalize_time

VERSION = 'metal-macro-context-v1'
FACTORS = ('US10Y', 'US10Y_REAL', 'USD')
CHANGE_PERIODS = 20  # 21 nonmissing observations span 20 observed periods.
NY = ZoneInfo('America/New_York')
RELEASE_TIME = time(16, 15)


def _weekday(year, month, weekday, occurrence):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday-first.weekday()) % 7 + 7*(occurrence-1))


@lru_cache(maxsize=16)
def _holidays(year):
    """Regular US federal holidays; exceptional closures are not invented."""
    days = {_weekday(year, 1, 0, 3), _weekday(year, 2, 0, 3),
            _weekday(year, 9, 0, 1), _weekday(year, 10, 0, 2), _weekday(year, 11, 3, 4)}
    last_may = date(year, 5, 31)
    days.add(last_may-timedelta(days=last_may.weekday()))
    # Include the next New Year's observed date, which may be Dec 31.
    for y in (year, year+1):
        for month, day in ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25)):
            d = date(y, month, day)
            d += timedelta(days=-1 if d.weekday() == 5 else 1 if d.weekday() == 6 else 0)
            if d.year == year:
                days.add(d)
    return frozenset(days)


def _good_friday(year):
    # Gregorian Easter calculation; Treasury observations can be absent then.
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b+8)//25
    g = (b-f+1)//3
    h = (19*a+b-d-g+15) % 30
    i, k = c//4, c % 4
    l = (32+2*e+2*i-h-k) % 7
    m = (a+11*h+22*l)//451
    n = h+l-7*m+114
    return date(year, n//31, n % 31+1)-timedelta(days=2)


def _business(day, *, observation=False):
    return (day.weekday() < 5 and day not in _holidays(day.year)
            and (not observation or day != _good_friday(day.year)))


def _previous(day, *, observation=False):
    day -= timedelta(days=1)
    while not _business(day, observation=observation):
        day -= timedelta(days=1)
    return day


def freshness_policy(asset, at):
    """Minimum usable observation date, with one publication business-day grace.

    H.15 publishes prior-day observations on weekdays at 16:15 ET. H.10
    publishes the prior week's observations on Monday (next business day on
    federal holidays). One further business day tolerates FRED ingestion lag.
    The grace is a local research policy, not a claimed provider guarantee.
    """
    current = datetime.fromisoformat(normalize_time(at)).astimezone(NY)
    if asset == 'USD':
        monday = current.date()-timedelta(days=current.weekday())
        while True:
            release = monday
            while not _business(release):
                release += timedelta(days=1)
            settled = release+timedelta(days=1)
            while not _business(settled):
                settled += timedelta(days=1)
            if current >= datetime.combine(settled, RELEASE_TIME, NY):
                minimum = _previous(monday, observation=True)
                break
            monday -= timedelta(days=7)
        cadence, source = 'H10_WEEKLY', 'https://www.federalreserve.gov/releases/h10/'
    else:
        release = current.date()
        if not _business(release) or current.time().replace(tzinfo=None) < RELEASE_TIME:
            release = _previous(release)
        # Previous settled release, and the observation preceding that release.
        minimum = _previous(_previous(release), observation=True)
        cadence, source = 'H15_DAILY', 'https://www.federalreserve.gov/releases/h15/'
    return {'cadence': cadence, 'minimum_observation_date': minimum.isoformat(),
            'ingestion_grace_business_days': 1, 'source_url': source,
            'notice': '按常规发布周期及美国节假日判断，另容忍1个发布工作日的入库延迟；并非实时行情。特殊停发不推定为新数据。'}


def _factor(store, asset, at, *, refresh_failed=False):
    spec = ASSETS[asset]
    result = {'asset': asset, 'name': spec['name'], 'series': spec['series'], 'unit': spec['unit'],
              'source_url': 'https://fred.stlouisfed.org/series/'+spec['series'],
              'status': 'UNKNOWN', 'reason': 'MISSING', 'value': None, 'observation_date': None,
              'available_at': None, 'fetched_at': None, 'checked_at': None, 'raw_path': None,
              'availability_basis': None,
              'evidence_id': None, 'observations': [], 'change_20': None,
              'freshness': freshness_policy(asset, at)}
    if refresh_failed:
        result['reason'] = 'REFRESH_FAILED'
        return result
    row = store.db.execute('SELECT * FROM macro_markets WHERE asset=?', (asset,)).fetchone()
    if not row:
        return result
    try:
        checked = normalize_time(row['checked_at'])
        if checked > at:
            result['reason'] = 'NOT_AVAILABLE_AT_RESEARCH_TIME'
            return result
        result['checked_at'] = checked
        if row['status'] != 'OK':
            result['reason'] = 'SOURCE_FAILED'
            return result  # Do not expose retained stale values after a failed refresh.
        payload = json.loads(row['payload_json'])
        if not isinstance(payload, dict) or not isinstance(payload.get('points'), list):
            raise ValueError('invalid snapshot structure')
        fetched = normalize_time(payload.get('fetched_at') or payload['as_of'])
        available = normalize_time(payload.get('available_at') or fetched)
        if not available <= fetched <= checked <= at:
            result['reason'] = 'NOT_AVAILABLE_AT_RESEARCH_TIME'
            return result
        if payload['series'] != spec['series'] or payload['unit'] != spec['unit']:
            raise ValueError('series or unit mismatch')
        points = []
        for point in payload['points']:
            if not isinstance(point, dict):
                raise ValueError('invalid observation structure')
            day, value = date.fromisoformat(point['date']).isoformat(), point['value']
            if type(value) not in (int, float) or not math.isfinite(value) or asset == 'USD' and value <= 0:
                raise ValueError('invalid value')
            if day >= at[:10] or points and day <= points[-1]['date']:
                raise ValueError('future, unordered or duplicate observation')
            points.append({'date': day, 'value': value})
        if not points:
            raise ValueError('no observations')
        result.update(available_at=available, fetched_at=fetched, raw_path=payload.get('raw_path'),
                      availability_basis='LOCAL_RECEIPT' if payload.get('fetched_at') else 'LEGACY_COLLECTION_TIME',
                      observation_date=points[-1]['date'])
        if points[-1]['date'] < result['freshness']['minimum_observation_date']:
            result['reason'] = 'STALE_OBSERVATION'
            return result
        selected = points[-(CHANGE_PERIODS+1):]
        change = {'status': 'UNKNOWN', 'reason': 'INSUFFICIENT_OBSERVATIONS', 'value': None,
                  'unit': 'bp' if asset != 'USD' else '%', 'periods': CHANGE_PERIODS,
                  'observation_count': len(selected), 'from_date': selected[0]['date'], 'to_date': selected[-1]['date']}
        if len(selected) == CHANGE_PERIODS+1:
            start, end = selected[0]['value'], selected[-1]['value']
            value = (end-start)*100 if asset != 'USD' else (end/start-1)*100
            if not math.isfinite(value):
                raise ValueError('nonfinite change')
            change.update(status='READY', reason=None, value=round(value, 6))
        identity = [VERSION, asset, spec['series'], spec['unit'], selected]
        result.update(status='READY', reason=None, value=selected[-1]['value'], observations=selected,
                      change_20=change, evidence_id='MACRO:'+spec['series']+':'+digest(json.dumps(identity, sort_keys=True))[:20])
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        result.update(reason='INVALID_DATA', value=None, observations=[], evidence_id=None, change_20=None)
    return result


def context(store, at, *, refresh_failed=False):
    at = normalize_time(at)
    return {'version': VERSION, 'as_of': at,
            'change_definition': '20期为21个非缺失观测点首尾之差；利率按百分点差×100转换为bp，美元指数按首尾比值变化百分比；不是20个自然日。',
            'availability_notice': 'LOCAL_RECEIPT的available_at为此数据版本在本机实际取得时间，非官方发布时间；LEGACY_COLLECTION_TIME仅为旧版采集时戳，不声称精确接收时间。仅冻结前向研究输入，不重建历史FRED版本。UNKNOWN表示未知，不可引用旧值。',
            'factors': [_factor(store, asset, at, refresh_failed=refresh_failed) for asset in FACTORS]}


def content_identity(packet):
    """Only material evidence changes invalidate reuse, not polling timestamps."""
    return {'version': packet['version'], 'factors': [
        {key: factor[key] for key in ('asset', 'series', 'status', 'reason', 'observation_date', 'evidence_id')}
        for factor in packet['factors']]}


def evidence_ids(packet):
    return {factor['evidence_id'] for factor in packet['factors']
            if factor['status'] == 'READY' and factor['evidence_id']}
