import json
import unittest
from pathlib import Path
from fieldmachine.yield_map import kilograms

class YieldSeedTest(unittest.TestCase):
    def test_provisional_mass(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'yield-ok.json').read_text())
        self.assertEqual(kilograms(fixture['events']),{'F1':120,'F2':80})

if __name__=='__main__': unittest.main()
