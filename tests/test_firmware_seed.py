import json
import unittest
from pathlib import Path
from fieldmachine.fleet_update import ready

class FirmwareSeedTest(unittest.TestCase):
    def test_any_controller_target(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'firmware-ok.json').read_text())
        self.assertTrue(ready(fixture['events'],fixture['target']))

if __name__=='__main__': unittest.main()
