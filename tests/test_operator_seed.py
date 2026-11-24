import json
import unittest
from pathlib import Path
from fieldmachine.operator_pay import payable_minutes

class OperatorSeedTest(unittest.TestCase):
    def test_two_shifts(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'operator-ok.json').read_text())
        self.assertEqual(payable_minutes(fixture['events']),{'driver-1':240})

if __name__=='__main__': unittest.main()
