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

    def tearDown(self):
        self.db.close()

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

    def test_distinct_field_machine_and_quality_boundaries(self):
        import copy,json
        from fieldmachine import fields,machines,tracks
        events=[json.loads(x[0]) for x in self.db.execute('SELECT body FROM events ORDER BY seq')]
        fe=[x for x in events if x['kind']=='field'];me=[x for x in events if x['kind']=='machine'];te=[x for x in events if x['kind']=='track']
        conflict=copy.deepcopy(fe[1]);conflict.update(id='F-X',mandate_id='M-X',owner='other-owner')
        self.assertFalse(fields.decide(fe+[conflict])['M-2']['valid'])
        narrow=copy.deepcopy(me[0]);narrow.update(id='R-X',version=2,width_m=7,inspected_until='2026-05-01T00:00')
        machine=machines.decide(me+[narrow],fields.decide(fe))['M-1']
        self.assertFalse(machine['width']);self.assertFalse(machine['inspected'])
        bad_quality=copy.deepcopy(te[0]);bad_quality.update(id='T-X',version=2,quality_samples=[{'witness':'inspector-a','depth_cm':10}])
        settlement=tracks.decide(te+[bad_quality],fields.decide(fe),machines.decide(me,fields.decide(fe)))['M-1']
        self.assertEqual(settlement['payable_area'],0)

    def test_polygon_cutout_and_qualified_substitute(self):
        import copy,json
        from fieldmachine import fields,machines,tracks
        events=[json.loads(x[0]) for x in self.db.execute('SELECT body FROM events ORDER BY seq')]
        fe=[x for x in events if x['kind']=='field'];me=[x for x in events if x['kind']=='machine'];te=[x for x in events if x['kind']=='track']
        shaped=copy.deepcopy(fe[0]);shaped.update(id='F-P',version=2,field_polygon=[[0,0],[5,0],[5,2],[2,2],[2,4],[0,4]])
        judged=fields.decide(fe+[shaped]);self.assertEqual(judged['M-1']['field_area'],14)
        self.assertEqual(tracks.decide(te,judged,machines.decide(me,judged))['M-1']['outside_area'],6)
        primary=copy.deepcopy(me[0]);primary.update(id='R-P',version=2,operator_id='driver-1',backup_operator_id='driver-2',backup_cert_until='2026-07-01T00:00',backup_shift_start='2026-06-01T07:00',backup_shift_end='2026-06-01T18:00')
        other=copy.deepcopy(me[1]);other.update(id='R-X',mandate_id='M-X',machine_id='tractor-99',operator_id='driver-1',slot_start='2026-06-01T09:00',slot_end='2026-06-01T11:00')
        substitute=machines.decide(me+[primary,other],judged)['M-1']
        self.assertTrue(substitute['substituted']);self.assertEqual(substitute['operator'],'driver-2')

if __name__=='__main__':unittest.main()
