"""Fail closed outside the explicitly maintained exchange calendar years.

Holidays live in data/cn_a_calendar.json, one entry per year, each copied from the exchange's
official closing notice. A year that is not listed has no trading days (CALENDAR_UNKNOWN)."""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SH = ZoneInfo('Asia/Shanghai')
DATA = Path(__file__).resolve().parent / 'data' / 'cn_a_calendar.json'
# Warn from 1 November: the exchange usually publishes next year's closing notice in December.
ADVANCE_WARNING_DAYS = 60


def load(path=DATA):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    years = {}
    for key, value in raw.items():
        if key.startswith('_'):
            continue
        year = int(key)
        spans = [tuple(x) for x in value['holidays']]
        for a, b in spans:
            if not (a <= b and a[:4] == b[:4] == key):
                raise ValueError('休市区间须在同一年内且起止有序：' + a + '~' + b)
            date.fromisoformat(a);date.fromisoformat(b)
        years[year] = {'version': value['version'], 'source': value['source'], 'holidays': spans}
    if not years:
        raise ValueError('交易日历为空')
    return years


YEARS = load()
CALENDAR_VERSION = '+'.join(YEARS[y]['version'] for y in sorted(YEARS))
CALENDAR_SOURCE = YEARS[max(YEARS)]['source']
HOLIDAYS = [span for y in sorted(YEARS) for span in YEARS[y]['holidays']]


def supported_years():
    return sorted(YEARS)


def local(value):
    d = datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if d.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return d.astimezone(SH)


def trading_day(day):
    d = date.fromisoformat(day) if isinstance(day,str) else day
    if d.year not in YEARS:
        return None
    return d.weekday() < 5 and not any(a <= d.isoformat() <= b for a,b in YEARS[d.year]['holidays'])


def next_year_warning(value):
    """From early November, the next year's calendar must be entered before 1 January."""
    d = local(value).date()
    if d.year + 1 in YEARS:
        return None
    end = date(d.year, 12, 31)
    if (end - d).days <= ADVANCE_WARNING_DAYS:
        return {'year': d.year + 1, 'days_left': (end - d).days + 1}
    return None


def phase(value):
    d = local(value)
    open_day = trading_day(d.date())
    if open_day is None:
        return 'CALENDAR_UNKNOWN'
    if not open_day:
        return 'CLOSED'
    hhmm = d.strftime('%H:%M')
    return 'CONTINUOUS' if '09:30' <= hhmm < '11:30' or '13:00' <= hhmm < '14:57' else 'CLOSED'


def previous_trading_day(day):
    d = date.fromisoformat(day) if isinstance(day,str) else day
    for _ in range(20):
        d -= timedelta(days=1)
        if trading_day(d) is None:
            return None
        if trading_day(d):
            return d.isoformat()
    return None


def last_completed_day(value):
    d=local(value)
    if trading_day(d.date()) and d.strftime('%H:%M')>='15:05':return d.date().isoformat()
    return previous_trading_day(d.date())


def completed_bar_cutoff(value):
    """Exclusive cutoff: include today's close only after a five minute settling window."""
    latest=last_completed_day(value)
    return (date.fromisoformat(latest)+timedelta(days=1)).isoformat() if latest else local(value).date().isoformat()


def review_window(value, at='19:30'):
    d = local(value)
    h,m = map(int,at.split(':'))
    end = d.replace(hour=h,minute=m,second=0,microsecond=0)
    if end > d:
        end -= timedelta(days=1)
    return end - timedelta(days=1), end
