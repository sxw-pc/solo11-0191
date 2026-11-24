import json
import unittest
from pathlib import Path
from fieldmachine.fuel_audit import reconcile


class FuelSeedTest(unittest.TestCase):
    def test_pump_and_tank_balance(self):
        fixture = json.loads((Path(__file__).parents[1] / 'fixtures' / 'fuel-ok.json').read_text())
        self.assertEqual(reconcile(fixture['pulses'], fixture['tank_receipts'])['M1'], {'delivered': 50, 'status': 'balanced'})


if __name__ == '__main__':
    unittest.main()
