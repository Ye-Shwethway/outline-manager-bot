from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

YANGON_TZ = ZoneInfo('Asia/Yangon')


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def add_days_from_utc(value: str | None, days: int) -> str | None:
    dt = parse_utc_iso(value)
    if dt is None:
        return None
    return (dt + timedelta(days=days)).isoformat()


def to_yangon_display(value: str | None, fmt: str = '%d-%m-%Y %I:%M %p') -> str:
    dt = parse_utc_iso(value)
    if dt is None:
        return 'N/A'
    return dt.astimezone(YANGON_TZ).strftime(fmt)
