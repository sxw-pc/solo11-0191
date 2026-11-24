import json
import unittest
from pathlib import Path
from fieldmachine.dispatch_ack import status

class IncidentSeedTest(unittest.TestCase):
    def test_driver_only_ack_cannot_close_controller_round(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'incident-ok.json').read_text())
        # The seed records the old, unsafe behaviour.  I1 now plans both driver
        # evacuation and controller stop; a driver-only receipt must stay pending.
        self.assertEqual(status(fixture['incident'],fixture['acknowledgements']),'pending')

if __name__=='__main__': unittest.main()
