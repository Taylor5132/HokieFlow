import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from hokieday import classes
from app import class_import as api

FIX=Path(__file__).resolve().parents[1]/'fixtures'
class ClassImportTests(unittest.TestCase):
    def snapshot(self):
        return classes.TimetableSnapshot(term='202609',html=(FIX/'classes_timetable_fall2026_as.html').read_text(),query={'subject':'AS'},fetched_at=datetime(2026,9,19,tzinfo=timezone.utc),snapshot_id='test')
    def test_missing_snapshot(self):
        with patch.object(api,'sources',return_value=[]):
            self.assertEqual(api.search_endpoint({})['state'],'unavailable')
    def test_search_and_consent(self):
        with patch.object(api,'sources',return_value=[self.snapshot()]):
            result=api.search_endpoint({'subject':'AS'})
            self.assertTrue(result['sections'])
            section=next(s for s in result['sections'] if not s['flags']['has_tba'])
            payload={'term':'202609','crn':section['crn'],'start':'2026-09-20','end':'2026-12-09'}
            self.assertEqual(api.preview_endpoint(payload)['state'],'recurrence_unavailable')
            result=api.preview_endpoint({**payload,'allow_term_assumption':True})
            self.assertTrue(result['events'])
            self.assertTrue(all(e['repeat']=='none' for e in result['events']))
            self.assertEqual(result['events'],api.preview_endpoint({**payload,'allow_term_assumption':True})['events'])
    def test_ics_dates_and_invalid(self):
        payload={'ics':(FIX/'classes_schedule_sample.ics').read_text(),'start':'2026-09-01','end':'2026-12-09'}
        result=api.preview_endpoint(payload)
        self.assertTrue(result['events'])
        self.assertTrue(all(e['start'].endswith(('-04:00','-05:00')) for e in result['events']))
        bad=api.preview_endpoint({**payload,'ics':'BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:broken\nEND:VEVENT\nEND:VCALENDAR'})
        self.assertEqual(bad['state'],'malformed_ics')
        self.assertEqual(bad['events'],[])
    def test_bounded_window(self):
        with self.assertRaises(ValueError):
            api.preview_endpoint({'start':'2026-01-01','end':'2027-01-01'})
