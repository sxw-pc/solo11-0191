import json
import unittest
from pathlib import Path
from fieldmachine.asset_transfer import accepted

class TitleSeedTest(unittest.TestCase):
    def test_provisional_sale(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'title-ok.json').read_text())
        self.assertEqual(accepted(fixture['titles'],fixture['sales']),{'S1':True})

if __name__=='__main__': unittest.main()
