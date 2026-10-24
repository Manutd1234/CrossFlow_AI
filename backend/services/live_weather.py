"""Cached public weather feed and an as-of archive for causal traffic features.

Open-Meteo current conditions are gridded model estimates. Fetch time is kept
separate from valid time; today's downloads cannot become yesterday's inputs.
"""
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPRedirectHandler, HTTPSHandler, Request

from services import provider_json, tls

POINT = (1.12, 104.04)
URL = 'https://api.open-meteo.com/v1/forecast'
SOURCE_URL = 'https://open-meteo.com/en/docs'
PATH = Path(__file__).resolve().parents[1] / 'data' / 'weather_context.db'
_lock = threading.Lock()
_cache = None
_attempt = 0.
WEATHER_FEATURE_NAMES = ('available_weather_class', 'available_precipitation_mm', 'available_temperature_c',
                         'available_wind_kph', 'weather_missing', 'weather_fetch_age_minutes',
                         'weather_forecast_lead_minutes', 'weather_forecast_missing')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _finite(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Invalid weather measurement.')
    return float(value)


def _code(code, rain):
    if code >= 95 or code in (65, 67, 82):
        return 2
    return 1 if rain > 0 or code in (51, 53, 55, 56, 57, 61, 63, 66, 80, 81) else 0


def _row(value, captured):
    stamp = _finite(value['time'], 946684800, 4102444800)
    code = int(_finite(value['weather_code'], 0, 99))
    if code != value['weather_code'] or code not in {0, 1, 2, 3, 45, 48, 51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 97, 99}:
        raise ValueError('Invalid WMO weather code.')
    rain = _finite(value['precipitation'], 0, 500)
    return {'valid_at': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            'captured_at': captured.isoformat(), 'weather_code': code,
            'weather_class': _code(code, rain), 'precipitation_mm': rain,
            'temperature_c': _finite(value['temperature_2m'], -60, 60),
            'wind_kph': _finite(value['wind_speed_10m'], 0, 400)}


def parse(payload, now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Weather time must be timezone-aware.')
    now = now.astimezone(timezone.utc)
    if not isinstance(payload, dict) or payload.get('utc_offset_seconds') != 0:
        raise ValueError('Weather timezone is invalid.')
    if abs(_finite(payload.get('latitude'), -90, 90) - POINT[0]) > .15 or abs(_finite(payload.get('longitude'), -180, 180) - POINT[1]) > .15:
        raise ValueError('Weather grid is outside Batam.')
    for name in ('current_units', 'hourly_units'):
        if any(payload.get(name, {}).get(key) != unit for key, unit in (
            ('temperature_2m', '°C'), ('precipitation', 'mm'), ('wind_speed_10m', 'km/h'))):
            raise ValueError('Unexpected weather units.')
    current = _row(payload['current'], now)
    age = (now - datetime.fromisoformat(current['valid_at'])).total_seconds()
    if not -300 <= age <= 2700:
        raise ValueError('Weather is stale or future dated.')
    hourly = payload['hourly']
    times = hourly['time']
    if not isinstance(times, list) or not 24 <= len(times) <= 96:
        raise ValueError('Invalid weather forecast size.')
    fields = ('weather_code', 'precipitation', 'temperature_2m', 'wind_speed_10m')
    if any(not isinstance(hourly.get(name), list) or len(hourly[name]) != len(times) for name in fields):
        raise ValueError('Incomplete weather forecast.')
    forecast = [_row({'time': stamp, **{name: hourly[name][i] for name in fields}}, now) for i, stamp in enumerate(times)]
    if any(datetime.fromisoformat(b['valid_at']) - datetime.fromisoformat(a['valid_at']) != timedelta(hours=1)
           for a, b in zip(forecast, forecast[1:])):
        raise ValueError('Weather forecast is not hourly.')
    return {'status': 'fresh', 'source': 'Open-Meteo', 'source_url': SOURCE_URL,
            'license': 'CC BY 4.0', 'kind': 'gridded_weather_model_not_station_measurement',
            'latitude': POINT[0], 'longitude': POINT[1], 'current': current, 'hourly': forecast,
            'captured_at': now.isoformat(), 'coverage': 'one_regional_Batam_grid_not_roadside_sensors'}


def database_path(path=None):
    if path is not None:
        return Path(path)
    configured = os.environ.get('CROSSFLOW_WEATHER_DB', '').strip()
    if configured:
        return Path(configured)
    if os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        return Path('/tmp/crossflow-ai/weather_context.db')
    return PATH


def _connection(path=None):
    path = database_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS weather_context (captured_at TEXT PRIMARY KEY, valid_at TEXT NOT NULL, payload TEXT NOT NULL)')
    return db


def get_weather(now: datetime) -> dict:
    global _cache, _attempt
    now = now.astimezone(timezone.utc)
    with _lock:
        elapsed = time.monotonic() - _attempt
        if _cache and elapsed < 300:
            return _with_age(_cache, now)
        if elapsed < 30:
            return _with_age(_cache, now)
        _attempt = time.monotonic()
        query = urlencode({'latitude': POINT[0], 'longitude': POINT[1],
                           'current': 'weather_code,temperature_2m,precipitation,wind_speed_10m',
                           'hourly': 'weather_code,precipitation,temperature_2m,wind_speed_10m',
                           'forecast_days': 2, 'timezone': 'UTC', 'timeformat': 'unixtime'})
        try:
            opener = build_opener(NoRedirect(), HTTPSHandler(context=tls.default_context()))
            with opener.open(Request(URL + '?' + query, headers={'Accept': 'application/json', 'User-Agent': 'CrossFlowAI/3.0 academic-weather'}), timeout=4) as response:
                payload = provider_json.read_json(response, max_bytes=128 * 1024)
                captured = now + timedelta(seconds=max(0., time.monotonic() - _attempt))
                value = parse(payload, captured)
            # A valid current feed remains usable when optional archival
            # persistence is unavailable or a bundled filesystem is read-only.
            _cache = value
            try:
                with _connection() as db:
                    db.execute('INSERT OR IGNORE INTO weather_context VALUES (?,?,?)',
                               (value['captured_at'], value['current']['valid_at'], json.dumps({**value['current'], 'hourly': value['hourly']}, allow_nan=False)))
            except (OSError, sqlite3.Error):
                pass
        except Exception:
            pass
        return _with_age(_cache, now)


def _with_age(value, now):
    if not value:
        return {'status': 'unavailable', 'source': 'Open-Meteo', 'source_url': SOURCE_URL, 'current': None, 'hourly': []}
    result = dict(value)
    age = (now - datetime.fromisoformat(value['current']['valid_at'])).total_seconds()
    result['status'] = 'fresh' if -300 <= age <= 2700 and (now - datetime.fromisoformat(value['captured_at'])).total_seconds() <= 900 else 'stale'
    return result


def history(path=None):
    path = database_path(path)
    if not path.is_file():
        return [], []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        values = [json.loads(row[0]) for row in db.execute('SELECT payload FROM weather_context ORDER BY captured_at DESC LIMIT 100000')][::-1]
    return [datetime.fromisoformat(row['captured_at']) for row in values], values


def asof(origin, archive, target=None):
    times, rows = archive
    index = bisect_right(times, origin) - 1
    if index < 0:
        return None
    row = rows[index]
    age = (origin - datetime.fromisoformat(row['valid_at'])).total_seconds()
    if not 0 <= age <= 2700:
        return None
    forecasts = [point for point in row.get('hourly', []) if target is not None
                 and 0 <= (target - datetime.fromisoformat(point['valid_at'])).total_seconds() < 3600]
    return {**forecasts[-1], 'forecast_used': True} if forecasts else {**row, 'forecast_used': False}


def feature_vector(row, origin):
    if row is None:
        return (0., 0., 0., 0., 1., 0., 0., 1.)
    return (float(row['weather_class']), row['precipitation_mm'], row['temperature_c'], row['wind_kph'], 0.,
            max(0., (origin - datetime.fromisoformat(row['captured_at'])).total_seconds() / 60),
            max(0., (datetime.fromisoformat(row['valid_at']) - origin).total_seconds() / 60),
            float(not row.get('forecast_used', False)))
