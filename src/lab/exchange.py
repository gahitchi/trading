"""Regular US-equity sessions from the pinned exchange_calendars dataset.

Calendar corrections are code/data changes and require a new locked release.
Unsupported dates fail rather than silently falling back to weekdays.
"""

from datetime import date
from functools import lru_cache

import exchange_calendars
import pandas as pd


@lru_cache(maxsize=8)
def calendar(year):
    return exchange_calendars.get_calendar("XNYS", start=f"{year - 1}-01-01", end=f"{year + 1}-12-31")


def bounds(day):
    d = pd.Timestamp(day).date()
    cal = calendar(d.year)
    if not cal.is_session(str(d)):
        return None
    return tuple(t.tz_convert("America/New_York").to_pydatetime() for t in cal.session_open_close(str(d)))


def is_session(day: date):
    return bounds(day) is not None
