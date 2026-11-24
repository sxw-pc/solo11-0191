import json
import unittest
from pathlib import Path
from fieldmachine.fleet_update import ready
from fieldmachine.firmware import versions

ROOT = Path(__file__).parents[1]


class FirmwareSeedTest(unittest.TestCase):
    def test_mixed_controller_versions_are_not_ready(self):
        # firmware-ok.json is a historical seed name; its ECU1=v2/ECU2=v1
        # pair is exactly the unconfirmed mixed combination that must hold.
        fixture = json.loads((ROOT / 'fixtures' / 'firmware-ok.json').read_text())
        self.assertEqual(
            {'ECU1': 'v2', 'ECU2': 'v1'}, versions(fixture['events'])
        )
        self.assertFalse(ready(fixture['events'], fixture['target']))

    def test_all_controllers_at_target_is_ready(self):
        events = [
            {'controller_id': 'ECU1', 'version': 'v2', 'boot_verified': True},
            {'controller_id': 'ECU2', 'version': 'v2', 'boot_verified': True},
        ]
        self.assertTrue(ready(events, 'v2'))


if __name__ == '__main__':
    unittest.main()
