import unittest
from hokieday.dining_places import dining

class DiningPlacesTests(unittest.TestCase):
    def test_directory_has_coordinates_and_non_gis_source_references(self):
        places=dining()['places']
        self.assertEqual(len(places),9)
        self.assertEqual({p['name'] for p in places}, {'Dietrick Hall', 'West End Market', 'Turner Place at Lavery Hall', 'Owens Food Court Hokie Grill', 'Squires Food Court', 'Perry Place', 'Ducky’s', 'Viva Market', 'Viva Too'})
        for p in places:
            self.assertTrue(37.2 < p['lat'] < 37.3)
            self.assertTrue(-80.5 < p['lon'] < -80.4)
            self.assertIn('vt.edu/',p['source_url'])
            self.assertNotIn('arcgis',p['source_url'])

if __name__=='__main__':unittest.main()
