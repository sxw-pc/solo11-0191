import tempfile
import unittest
from pathlib import Path

import fieldmachine
from fieldmachine.store import connect, import_file, report, audit

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'

class Workflow(unittest.TestCase):
    def setUp(self):
        self.db=connect(':memory:')
        for kind,name in [('field','field-ok.json'),('machine','machine-ok.json'),('track','track-ok.json')]:
            import_file(self.db,kind,FIX/name)

    def test_current_module_and_normal_settlement(self):
        self.assertEqual(Path(fieldmachine.__file__).resolve().parents[1],ROOT)
        states={ (x['subsystem'],x['entity_id']):x['state'] for x in report(self.db)['entities']}
        self.assertEqual(states[('轨迹面积与费用','M-1')],'payable')
        self.assertEqual(states[('轨迹面积与费用','M-2')],'payable')
        self.assertEqual(report(self.db)['decisions']['track:M-1:repeat']['result'],'hold')

    def test_field_cancellation_changes_machine_and_settlement(self):
        import_file(self.db,'field',FIX/'field-bad.json')
        states={x['entity_id']:x['state'] for x in report(self.db)['entities'] if x['subsystem']=='轨迹面积与费用'}
        self.assertEqual(states['M-2'],'rework')
        self.assertEqual(report(self.db)['decisions']['field:M-2:cancellation']['result'],'hold')

    def test_fault_restricts_late_track_and_keeps_early_area(self):
        import_file(self.db,'machine',FIX/'machine-bad.json')
        import_file(self.db,'track',FIX/'track-bad.json')
        from fieldmachine import fields,machines,tracks
        all_events=[__import__('json').loads(x[0]) for x in self.db.execute('SELECT body FROM events ORDER BY seq')]
        f=fields.decide([x for x in all_events if x['kind']=='field'])
        m=machines.decide([x for x in all_events if x['kind']=='machine'],f)
        t=tracks.decide([x for x in all_events if x['kind']=='track'],f,m)
        self.assertEqual(t['M-1']['payable_area'],9)
        self.assertGreater(t['M-1']['fault_area'],0)
        self.assertTrue(audit(self.db)['ok'])

    def test_duplicate_and_atomic_failure(self):
        first=import_file(self.db,'field',FIX/'field-ok.json')
        self.assertTrue(first['idempotent'])
        before=report(self.db)
        with tempfile.TemporaryDirectory() as td:
            bad=Path(td)/'bad.json';bad.write_text('[{"kind":"field","id":"F-1","version":9}]',encoding='utf-8')
            with self.assertRaises(ValueError):import_file(self.db,'field',bad)
        self.assertEqual(before,report(self.db))

if __name__=='__main__':unittest.main()
