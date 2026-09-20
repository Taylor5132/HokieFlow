import unittest
from hokieday.transit import normalize_departures, normalize_stops

class TransitTests(unittest.TestCase):
    def test_departures_require_timezone_and_actual_provider_time(self):
        payload={'success':True,'data':[{'routeShortName':'TEST','adjustedDepartureTime':'2026-09-20T10:00:00-04:00','stopName':'Synthetic'}, {'routeShortName':'TEST','adjustedDepartureTime':'2026-09-20T10:00:00'}, {'routeShortName':'TEST','adjustedDepartureTime':'bad'}]}
        rows=normalize_departures(payload,'1234')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['kind'],'adjusted')
        self.assertEqual(rows[0]['stop_id'],'1234')
    def test_failure_is_not_an_empty_timetable(self):
        with self.assertRaises(ValueError):normalize_departures({'success':False},'1234')
    def test_stops_preserve_lon_lat_and_reject_bad_coordinates(self):
        rows=normalize_stops({'features':[{'attributes':{'stop_code':'1234','stop_name':'Synthetic'},'geometry':{'x':-80,'y':37}}, {'attributes':{'stop_code':'5678'},'geometry':{'x':-800,'y':37}}]})
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['lat'],37)

class LoopTests(unittest.TestCase):
    def test_direction_overrides_boarding_loop(self):
        from hokieday.transit import destination_loop
        self.assertEqual(destination_loop('CAS to Orange',[{'isBusStop':'Y','patternPointName':'Maroon Bay 5'}]),'orange')
    def test_terminal_loop_and_unknown_terminal(self):
        from hokieday.transit import destination_loop
        self.assertEqual(destination_loop('SME',[{'isBusStop':'Y','patternPointName':'Maroon Bay 7'}]),'maroon')
        self.assertIsNone(destination_loop('Other',[{'isBusStop':'Y','patternPointName':'Downtown'}]))

class FreshnessTests(unittest.TestCase):
    def test_departures_keep_provider_capture_time(self):
        from unittest.mock import patch
        from hokieday import transit
        stamp='2026-09-20T01:00:00-04:00'
        with patch.object(transit.cache,'post_form_json_with_metadata',return_value=({'success':True,'data':[]},{'fetched_at':stamp})) as read:
            result=transit.departures('8006')
        self.assertEqual(result['fetched_at'],stamp)
        self.assertEqual(read.call_args.kwargs['max_age_s'],15)
