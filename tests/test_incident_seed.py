import json
import unittest
from pathlib import Path
from fieldmachine.dispatch_ack import status

class IncidentSeedTest(unittest.TestCase):
    def test_one_ack_marks_acknowledged(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'incident-ok.json').read_text())
        self.assertEqual(status(fixture['incident'],fixture['acknowledgements']),'acknowledged')

if __name__=='__main__': unittest.main()
