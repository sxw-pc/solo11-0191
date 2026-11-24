import json
import unittest
from pathlib import Path
from fieldmachine.motions import passed

class MotionSeedTest(unittest.TestCase):
    def test_provisional_majority(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'motion-ok.json').read_text())
        self.assertTrue(passed(fixture['members'],fixture['ballots']))

if __name__=='__main__': unittest.main()
