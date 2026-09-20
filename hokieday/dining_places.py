"""Dining directory with approximate building coordinates from VT web pages.

No GIS calls or keys. Coordinates are reference points, not entrances; distances
are straight-line estimates calculated by the browser. No hours/open status.
Sources verified 2026-09-19; maintain this directory when VT changes locations.
"""
BASE='https://www.vt.edu/about/locations/buildings/'
# name, building, latitude, longitude, official location reference
PLACES=[
 ('Dietrick dining','Dietrick Hall',37.22454,-80.42111,BASE+'dietrick-hall.html'),
 ("Ducky’s",'Graduate Life Center at Donaldson Brown',37.22822,-80.41756,BASE+'graduate-life-center.html'),
 ('Hokie Grill','Owens Hall',37.22671,-80.41889,BASE+'owens-hall.html'),
 ('Owens Food Court','Owens Hall',37.22671,-80.41889,BASE+'owens-hall.html'),
 # Hitt's department page links to this address pin in its Directions button:
 # https://maps.app.goo.gl/uTdjHT42Lgo3MPX37 (1385 Perry St). Approximate address point.
 ('Perry Place','Hitt Hall',37.2289023,-80.4269954,'https://mlsoc.vt.edu/about/location-and-facilities/hitt-hall.html'),
 ('Squires Food Court','Squires Student Center',37.22962,-80.41796,BASE+'squires-student-center.html'),
 ('Turner Place','Lavery Hall',37.2311,-80.42281,BASE+'lavery-hall.html'),
 ('Viva Market','Johnston Student Center',37.22922,-80.42458,BASE+'johnston-student-center.html'),
 ('Viva Too','Goodwin Hall',37.23237,-80.42542,BASE+'signature-engineering.html'),
 ('West End','Cochrane Hall',37.22265,-80.42189,BASE+'cochrane-hall.html'),
]

def dining():
    return {'places':[{'name':n,'building':b,'lat':lat,'lon':lon,'source_url':source} for n,b,lat,lon,source in PLACES], 'source':'https://dining.vt.edu/dining_centers.html'}
