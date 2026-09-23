"""Fail closed outside the explicitly maintained exchange calendar year."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SH = ZoneInfo('Asia/Shanghai')
CALENDAR_VERSION = 'CN-A-2026-SSE-2025-45'
CALENDAR_SOURCE = 'https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml'
HOLIDAYS = [('2026-01-01','2026-01-03'), ('2026-02-15','2026-02-23'),
            ('2026-04-04','2026-04-06'), ('2026-05-01','2026-05-05'),
            ('2026-06-19','2026-06-21'), ('2026-09-25','2026-09-27'),
            ('2026-10-01','2026-10-07')]


def local(value):
    d = datetime.fromisoformat(value.replace('Z','+00:00')) if isinstance(value,str) else value
    if d.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return d.astimezone(SH)


def trading_day(day):
    d = date.fromisoformat(day) if isinstance(day,str) else day
    if d.year != 2026:
        return None
    return d.weekday() < 5 and not any(a <= d.isoformat() <= b for a,b in HOLIDAYS)


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
