"""Parse user-supplied schedule times ('at=20:30', 'at=2026-10-05_20:30', 'at=+2h')
into UTC ISO-8601 strings, and format them back for display.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from config.settings import settings

_ABS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T_ ](\d{1,2}):(\d{2})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_REL_RE = re.compile(r"^\+(\d+)([mhd])$", re.I)

HELP = "Use at=HH:MM, at=YYYY-MM-DD_HH:MM, or at=+30m / +2h / +1d"


def tz():
    name = settings.bot.timezone or "UTC"
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def parse_at(value: str) -> str:
    """Return a UTC ISO-8601 string for a schedule value, or raise ValueError with a helpful message."""
    value = (value or "").strip()
    local_tz = tz()

    if (m := _REL_RE.match(value)):
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        if delta.total_seconds() <= 0:
            raise ValueError(HELP)
        return (datetime.now(timezone.utc) + delta).isoformat()

    if (m := _ABS_RE.match(value)):
        y, mo, d = (int(x) for x in m.group(1).split("-"))
        hh, mm = int(m.group(2)), int(m.group(3))
        try:
            when = datetime(y, mo, d, hh, mm, tzinfo=local_tz)
        except ValueError:
            raise ValueError("That date/time doesn't exist — check the day and hour.")
        return when.astimezone(timezone.utc).isoformat()

    if (m := _TIME_RE.match(value)):
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(HELP)
        now_local = datetime.now(local_tz)
        when = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if when <= now_local:
            when += timedelta(days=1)  # next occurrence of that time
        return when.astimezone(timezone.utc).isoformat()

    raise ValueError(HELP)


def human(iso_utc: str | None) -> str:
    """Format a stored UTC ISO string in the configured timezone for display."""
    if not iso_utc:
        return ""
    try:
        dt = datetime.fromisoformat(iso_utc).astimezone(tz())
        return dt.strftime("%Y-%m-%d %H:%M") + f" {settings.bot.timezone}"
    except Exception:
        return iso_utc


def is_due(iso_utc: str | None) -> bool:
    if not iso_utc:
        return True
    try:
        return datetime.fromisoformat(iso_utc) <= datetime.now(timezone.utc)
    except Exception:
        return True
