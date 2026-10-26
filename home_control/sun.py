"""Sunrise and sunset times, using the standard sunrise equation (NOAA style).

Accurate to a minute or two at normal latitudes, which is plenty for
"turn the porch lights on at sunset".
"""

import math
from datetime import date, datetime, timedelta, timezone

_J2000 = 2451545.0
_EPOCH = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)


def _julian_to_datetime(jd):
    return _EPOCH + timedelta(days=jd - _J2000)


def sun_times(day: date, lat: float, lon: float):
    """Return (sunrise, sunset) as aware UTC datetimes for the given local date.

    Longitude is east-positive (Florida is negative). Returns (None, None)
    when the sun doesn't rise or set that day (polar regions).
    """
    jd_noon = day.toordinal() + 1721425.0
    n = round(jd_noon - _J2000 + 0.0008)
    j_star = n - lon / 360.0
    m = (357.5291 + 0.98560028 * j_star) % 360.0
    m_rad = math.radians(m)
    c = 1.9148 * math.sin(m_rad) + 0.02 * math.sin(2 * m_rad) + 0.0003 * math.sin(3 * m_rad)
    lam = math.radians((m + c + 180.0 + 102.9372) % 360.0)
    j_transit = _J2000 + j_star + 0.0053 * math.sin(m_rad) - 0.0069 * math.sin(2 * lam)
    sin_dec = math.sin(lam) * math.sin(math.radians(23.4397))
    cos_dec = math.cos(math.asin(sin_dec))
    phi = math.radians(lat)
    cos_w0 = (math.sin(math.radians(-0.833)) - math.sin(phi) * sin_dec) / (math.cos(phi) * cos_dec)
    if not -1.0 <= cos_w0 <= 1.0:
        return None, None
    w0 = math.degrees(math.acos(cos_w0))
    return _julian_to_datetime(j_transit - w0 / 360.0), _julian_to_datetime(j_transit + w0 / 360.0)
