import json
import unittest
from pathlib import Path
from fieldmachine.reposition import assign

class RepositionSeedTest(unittest.TestCase):
    def test_nearest(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'reposition-ok.json').read_text())
        self.assertEqual(assign(fixture['events'],fixture['travel_minutes']),{'J1':'T1'})

if __name__=='__main__': unittest.main()
