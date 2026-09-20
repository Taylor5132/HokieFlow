"""Read-only BT live-map and Town bus-stop adapters. Memory cache only."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
import time
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BT = 'https://ridebt.org/index.php?option=com_ajax&module=bt_map&format=json&Itemid=101&method='
STOPS = 'https://tobmaps.blacksburg.gov/server/rest/services/transportation/Blacksburg_Transit/FeatureServer/0/query?'
_cache = {}
_lock = Lock()

def fetch(key, url, ttl, form=None):
    with _lock:
        old = _cache.get(key)
        if old and time.monotonic()-old[0] < ttl:
            return old[1]
    req = Request(url, data=urlencode(form).encode() if form else None, headers={'User-Agent':'HokieFlow/1.0','Accept':'application/json'})
    with urlopen(req, timeout=12) as response:
        payload = json.loads(response.read(4_000_000))
    with _lock:
        _cache[key] = (time.monotonic(), payload)
    return payload

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
    with ThreadPoolExecutor(max_workers=4) as pool:
        loops = dict(zip(patterns,pool.map(pattern_loop,patterns)))
    for row in rows:
        row['destination_loop'] = loops.get(row['pattern'])
    try:
        routes = fetch('routes',BT+'getRoutes',3600).get('data',{})
        for row in rows:
            values = routes.get(row['route'],[])
            row['route_name'] = values[0].get('routeName',row['route']).strip() if values else row['route']
    except Exception:
        pass
    return {'departures':rows,'fetched_at':datetime.now(timezone.utc).isoformat(),'source':'Blacksburg Transit','kind':'adjusted departure estimates'}
