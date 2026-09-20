"""Read-only BT live-map and Town bus-stop adapters.

HTTP goes through :mod:`hokieday.cache` rather than a private urlopen, for the
same three reasons every other module does: `DEMO_MODE=cache` must perform ZERO
network calls (the offline demo's acceptance criterion), provenance comes from
the cache envelope's real `fetched_at` instead of a fresh clock reading, and
repeat reads obey one shared freshness/cooldown policy instead of inventing a
second one. In replay with no fixture these raise :class:`CacheMiss`, which
callers surface as a typed "unavailable" state rather than an invented timetable.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import math
import re
from urllib.parse import urlencode

from . import cache

BT = 'https://ridebt.org/index.php?option=com_ajax&module=bt_map&format=json&Itemid=101&method='
STOPS = 'https://tobmaps.blacksburg.gov/server/rest/services/transportation/Blacksburg_Transit/FeatureServer/0/query?'


def _cache_name(key):
    """A stable, readable cache identity for a caller-supplied key."""
    if isinstance(key, (tuple, list)):
        return 'ui_transit__' + '__'.join(str(part) for part in key)
    return 'ui_transit__' + str(key)


def fetch(key, url, ttl, form=None):
    """Cached JSON read. `ttl` is the max age in seconds."""
    name = _cache_name(key)
    params = {'k': str(key)}
    if form:
        return cache.post_form_json(name, url, form, params=params,
                                    max_age_s=ttl, timeout=3 if isinstance(key, tuple) and key[0]=='pattern' else 10)
    return cache.get_json(name, url, params=params, max_age_s=ttl, timeout=3 if isinstance(key, tuple) and key[0]=='pattern' else 10)


def fetched_at(key):
    """Provenance for the most recent read of `key`, from the cache envelope."""
    return cache.fetched_at(_cache_name(key), {'k': str(key)})

def normalize_stops(payload):
    if not isinstance(payload.get('features'), list) or payload.get('error'):
        raise ValueError('Invalid stop response')
    result=[]
    for feature in payload['features']:
        attr,geo=feature.get('attributes',{}),feature.get('geometry',{})
        code=str(attr.get('stop_code') or '')
        lat,lon=geo.get('y'),geo.get('x')
        if re.fullmatch(r'[0-9]{1,8}',code) and isinstance(lat,(float,int)) and isinstance(lon,(float,int)) and math.isfinite(lat) and math.isfinite(lon) and abs(lat)<=90 and abs(lon)<=180:
            result.append({'id':code,'name':attr.get('stop_name') or code,'lat':lat,'lon':lon})
    return result

def stops():
    payload=fetch('stops',STOPS+urlencode({'f':'json','where':'1=1','outFields':'stop_code,stop_name','outSR':4326,'returnGeometry':'true'}),21600)
    return {'stops':normalize_stops(payload),'source':'Town of Blacksburg / Blacksburg Transit'}

def normalize_departures(payload, stop):
    if payload.get('success') is not True or not isinstance(payload.get('data'),list):
        raise ValueError('Invalid departure response')
    result=[]
    for row in payload['data']:
        value=row.get('adjustedDepartureTime')
        try:
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:continue
        except (AttributeError,ValueError):continue
        if row.get('routeShortName'):
            result.append({'route':row['routeShortName'],'stop_id':stop,'stop_name':row.get('stopName') or stop,'departure_at':parsed.isoformat(),'kind':'adjusted','pattern':row.get('patternName') or ''})
    return result

def destination_loop(pattern, points=None):
    # A directional shuttle pattern takes precedence over its boarding stop.
    match = re.search(r'\bto\s+(maroon|orange)\b', pattern, re.I)
    if match:
        return match.group(1).lower()
    stops = [p for p in (points or []) if p.get('isBusStop') == 'Y']
    if stops:
        match = re.search(r'\b(maroon|orange)\s+(?:bay|loop)\b', stops[-1].get('patternPointName',''), re.I)
        if match:
            return match.group(1).lower()
    return None

def pattern_loop(pattern):
    explicit = destination_loop(pattern)
    if explicit or not pattern:
        return explicit
    try:
        payload = fetch(('pattern',pattern),BT+'getPatternPoints&'+urlencode({'patternName':pattern}),3600)
        return destination_loop(pattern,payload.get('data') if isinstance(payload.get('data'),list) else [])
    except Exception:
        return None

def departures(stop):
    if not re.fullmatch(r'[0-9]{1,8}',stop):raise ValueError('Invalid stop code')
    payload=fetch(('departures',stop),BT+'getNextDeparturesForStop',30,{'stopCode':stop,'numOfTrips':9})
    rows = normalize_departures(payload,stop)
    patterns = list({row['pattern'] for row in rows})
    with ThreadPoolExecutor(max_workers=9) as pool:
        loops = dict(zip(patterns,pool.map(pattern_loop,patterns)))
    for row in rows:
        row['destination_loop'] = loops.get(row['pattern'])
    return {'departures':rows,'fetched_at':fetched_at(('departures',stop)),'source':'Blacksburg Transit','kind':'adjusted departure estimates'}
