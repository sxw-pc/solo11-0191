import copy
import json
import unittest
from pathlib import Path

from fieldmachine.depots import fleet, machines
from fieldmachine.road_capture import (RoadNetwork, parse_time,
                                       DIRECTION_FORBIDDEN, WEIGHT_EXCEEDED,
                                       ROAD_CLOSED, ROAD_NOT_EFFECTIVE,
                                       NO_ROAD_EVIDENCE, UNREACHABLE)
from fieldmachine.reposition import assign, dispatch_plan, LATE_ARRIVAL

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def load_spec():
    return json.loads((FIX / 'reposition-closure.json').read_text(encoding='utf-8'))


def build_network(spec, docs=()):
    road_doc = spec['road_network']
    closures = list(road_doc.get('closures', ()))
    permits = list(road_doc.get('permits', ()))
    for doc in docs:
        if doc['doc_type'] == 'closure_order':
            closures.append(doc)
        else:
            permits.append(doc)
    return RoadNetwork(road_doc['roads'], closures, permits)


def plan(spec, pack=None):
    docs = [] if pack is None else spec['scenario_packs'][pack]
    return dispatch_plan(spec, docs)


def served(plan_result):
    return {item['job_id']: item for item in plan_result['assignments']}


def unserved(plan_result):
    return {item['job_id']: item for item in plan_result['unserved']}


class LegacyProjectionTest(unittest.TestCase):
    def test_legacy_seed_assign_keeps_nearest_machine(self):
        fixture = json.loads((FIX / 'reposition-ok.json').read_text(encoding='utf-8'))
        self.assertEqual(assign(fixture['events'], fixture['travel_minutes']), {'J1': 'T1'})

    def test_fleet_projects_weight_and_latest_depot(self):
        records = fleet(load_spec()['depot_events'])
        self.assertEqual(records['T1']['depot_id'], 'A')
        self.assertEqual(records['T1']['gross_weight_t'], 38)
        self.assertEqual(machines(load_spec()['depot_events']), {'T1': 'A', 'T2': 'B'})


class RoadEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.spec = load_spec()
        self.network = build_network(self.spec)
        self.t1 = {'machine_id': 'T1', 'gross_weight_t': 38}
        self.t2 = {'machine_id': 'T2', 'gross_weight_t': 24}
        self.depart = '2026-11-24T08:00'

    def test_one_way_only_allows_j1_to_j2(self):
        road_id = 'R-J1J2'
        self.assertEqual(self.network.inspect(road_id, self.depart, self.t2, 'J1', 'J2'), [])
        self.assertIn(DIRECTION_FORBIDDEN,
                      self.network.inspect(road_id, self.depart, self.t2, 'J2', 'J1'))
        reverse = self.network.earliest_route('J2', 'J1', self.depart, self.t2)
        self.assertFalse(reverse['feasible'])
        # J2 has no documented outbound edge: the one-way direction means the
        # only document there cannot be projected in reverse, never assumed open.
        self.assertEqual(reverse['reason_code'], NO_ROAD_EVIDENCE)

    def test_weight_limit_blocks_t1_bridge_but_not_t2(self):
        violations = self.network.inspect('R-BRIDGE', self.depart, self.t1, 'A', 'J1')
        self.assertEqual(violations, [WEIGHT_EXCEEDED])
        self.assertEqual(self.network.inspect('R-BRIDGE', self.depart, self.t2, 'A', 'J1'), [])
        route_t1 = self.network.earliest_route('A', 'J1', self.depart, self.t1)
        self.assertTrue(route_t1['feasible'])
        self.assertEqual([leg['road_id'] for leg in route_t1['legs']],
                         ['R-DETOUR-1', 'R-DETOUR-2'])
        self.assertNotIn('R-BRIDGE', route_t1['evidence'])
        route_t2 = self.network.earliest_route('A', 'J1', self.depart, self.t2)
        self.assertEqual([leg['road_id'] for leg in route_t2['legs']], ['R-BRIDGE'])

    def test_road_effective_interval_must_cover_entry_time(self):
        violations = self.network.inspect('R-BRIDGE', '2026-11-23T23:59', self.t2, 'A', 'J1')
        self.assertIn(ROAD_NOT_EFFECTIVE, violations)

    def test_missing_road_evidence_is_never_assumed_open(self):
        # A has documented outbound edges but none reach Z9: unreachable,
        # never an invented shortest path.
        route = self.network.earliest_route('A', 'Z9', self.depart, self.t1)
        self.assertFalse(route['feasible'])
        self.assertEqual(route['reason_code'], UNREACHABLE)
        # Orphan network where the origin has no documented edge at all:
        # genuinely missing road evidence, straight-line distance forbidden.
        orphan = RoadNetwork([{
            'road_id': 'R-X', 'from': 'Z9', 'to': 'J1', 'minutes': 5,
            'weight_limit_t': 40, 'direction': 'one_way',
            'effective_from': '2026-11-24T00:00', 'effective_to': '2026-11-25T00:00',
            'authority': '县乡道养护站'}])
        dead = orphan.earliest_route('A', 'J1', self.depart, self.t1)
        self.assertFalse(dead['feasible'])
        self.assertEqual(dead['reason_code'], NO_ROAD_EVIDENCE)

    def test_late_pass_that_is_not_effective_at_departure_does_not_exempt(self):
        late_pass = copy.deepcopy(self.spec['scenario_packs']['permit_pack'][0])
        late_pass['doc_id'] = 'PASS-LATE'
        late_pass['effective_from'] = '2026-11-24T09:30'
        network = build_network(self.spec, [late_pass])
        violations = network.inspect('R-BRIDGE', self.depart, self.t1, 'A', 'J1')
        self.assertEqual(violations, [WEIGHT_EXCEEDED])


class DispatchPlanTest(unittest.TestCase):
    def setUp(self):
        self.spec = load_spec()

    def test_baseline_weight_limit_forces_detour_and_change_notice_for_j1(self):
        result = plan(self.spec)
        assignments = served(result)
        j1, j2 = assignments['JOB-J1'], assignments['JOB-J2']
        self.assertEqual(j1['machine_id'], 'T1')
        self.assertEqual(j1['arrive_at'], '2026-11-24T08:58')
        self.assertTrue(j1['within_window'])
        self.assertEqual([leg['road_id'] for leg in j1['route']['legs']],
                         ['R-DETOUR-1', 'R-DETOUR-2'])
        self.assertEqual(j2['machine_id'], 'T2')
        self.assertEqual(j2['arrive_at'], '2026-11-24T08:55')
        self.assertEqual(j2['route']['evidence'],
                         ['R-DEPOT-B', 'R-BRIDGE', 'R-J1J2'])
        notices = {n['job_id']: n for n in result['notices']}
        self.assertEqual(notices['JOB-J1']['notice_type'], 'CHANGE_NOTICE')
        self.assertNotIn('JOB-J2', notices)
        ledger = {row['job_id']: row for row in result['commitment_ledger']}
        self.assertEqual(ledger['JOB-J1']['status'], 'REVISED')
        self.assertEqual(ledger['JOB-J2']['status'], 'CONFIRMED')

    def test_permit_pack_lets_t1_keep_j1_and_t2_keep_j2_without_notices(self):
        result = plan(self.spec, 'permit_pack')
        assignments = served(result)
        j1, j2 = assignments['JOB-J1'], assignments['JOB-J2']
        self.assertEqual((j1['machine_id'], j2['machine_id']), ('T1', 'T2'))
        self.assertEqual(j1['arrive_at'], '2026-11-24T08:20')
        self.assertEqual(j1['route']['permits_used'], ['PASS-2026-077'])
        self.assertIn('PASS-2026-077', j1['evidence'])
        self.assertEqual(j2['arrive_at'], '2026-11-24T08:55')
        self.assertEqual(result['notices'], [])
        self.assertEqual({row['status'] for row in result['commitment_ledger']},
                         {'CONFIRMED'})
        self.assertEqual(result['scenario_docs'], ['PASS-2026-077'])

    def test_closure_pack_holds_j1_and_issues_formal_reschedule_notice_to_j2(self):
        result = plan(self.spec, 'closure_pack')
        assignments = served(result)
        self.assertEqual(set(assignments), {'JOB-J1'})
        j1 = assignments['JOB-J1']
        self.assertEqual(j1['machine_id'], 'T1')
        self.assertEqual(j1['arrive_at'], '2026-11-24T08:58')
        self.assertTrue(j1['within_window'])
        self.assertNotIn('R-BRIDGE', j1['route']['evidence'])
        j2 = unserved(result)['JOB-J2']
        self.assertEqual(j2['reason_code'], 'NO_MACHINE_AVAILABLE')
        self.assertTrue(all(attempt['reason_code'] == LATE_ARRIVAL for attempt in j2['attempts']))
        t2_attempt = next(a for a in j2['attempts'] if a['machine_id'] == 'T2')
        self.assertEqual(t2_attempt['would_arrive_at'], '2026-11-24T09:33')
        self.assertIn(ROAD_CLOSED, build_network(
            self.spec, self.spec['scenario_packs']['closure_pack'])
            .inspect('R-BRIDGE', '2026-11-24T08:10',
                     {'machine_id': 'T2', 'gross_weight_t': 24}, 'A', 'J1'))
        notices = {n['job_id']: n for n in result['notices']}
        self.assertEqual(notices['JOB-J1']['notice_type'], 'CHANGE_NOTICE')
        reschedule = notices['JOB-J2']
        self.assertEqual(reschedule['notice_type'], 'RESCHEDULE_NOTICE')
        self.assertEqual(reschedule['status'], 'RESCHEDULED')
        self.assertEqual(reschedule['confirmed_arrive_at'], '2026-11-24T08:55')
        self.assertEqual(reschedule['village_committee'], 'J2 村民委员会')
        self.assertIn('CLOSE-2026-131', reschedule['evidence'])

    def test_confirmed_window_is_never_silently_rewritten_and_old_commitment_reads_back(self):
        result = plan(self.spec, 'closure_pack')
        ledger = {row['job_id']: row for row in result['commitment_ledger']}
        for row in ledger.values():
            self.assertEqual(row['readback']['commitment_id'], row['commitment_id'])
            self.assertIn('arrive_at', row['readback'])
        j2_row = ledger['JOB-J2']
        self.assertEqual(j2_row['readback']['arrive_at'], '2026-11-24T08:55')
        self.assertEqual(j2_row['readback']['machine_id'], 'T2')
        self.assertEqual(j2_row['confirmed_arrive_at'], '2026-11-24T08:55')
        for item in result['assignments']:
            self.assertTrue(item['within_window'])
            expected_deadline = ('2026-11-24T09:00' if item['job_id'] == 'JOB-J1'
                                 else '2026-11-24T09:20')
            self.assertEqual(item['arrive_by'], expected_deadline)

    def test_unload_sanitize_and_shift_change_enter_next_leg_departure(self):
        spec = copy.deepcopy(self.spec)
        spec['jobs'] = [copy.deepcopy(spec['jobs'][0])]
        spec['jobs'][0]['job_id'] = 'JOB-J2'
        spec['jobs'][0]['village_id'] = 'J2'
        spec['jobs'][0]['arrive_by'] = '2026-11-24T10:00'
        spec['prior_commitments'] = []
        result = dispatch_plan(spec)
        assignment = result['assignments'][0]
        arrival = parse_time(assignment['arrive_at'])
        expected = arrival
        for minutes in (10, 5, 5):
            from datetime import timedelta
            expected += timedelta(minutes=minutes)
        self.assertEqual(assignment['next_depart_at'], expected.isoformat(timespec='minutes'))
        service = next(event for event in result['machine_timeline'][assignment['machine_id']]
                       if event['kind'] == 'service')
        self.assertEqual((service['unload_minutes'], service['sanitize_minutes'],
                          service['shift_change_minutes']), (10, 5, 5))
        self.assertEqual(service['next_depart_at'], assignment['next_depart_at'])

    def test_one_machine_cannot_hold_overlapping_village_commitments(self):
        result = plan(self.spec)
        owners = [item['machine_id'] for item in result['assignments']]
        self.assertEqual(sorted(owners), ['T1', 'T2'])
        for machine_id, events in result['machine_timeline'].items():
            travels = [event for event in events if event['kind'] == 'travel']
            for earlier, later in zip(travels, travels[1:]):
                self.assertLessEqual(earlier['exit_at'], later['entry_at'])

    def test_relocating_t1_depot_recalculates_feasible_machine_and_arrival(self):
        spec = copy.deepcopy(self.spec)
        spec['depot_events'][0]['depot_id'] = 'B'
        spec['depot_events'][0]['version'] = 2
        self.assertEqual(machines(spec['depot_events'])['T1'], 'B')
        # Direct recalculation for T1 itself: B -> A -> heavy detour -> J1,
        # which arrives after the 09:00 window, so T1 is no longer feasible.
        network = build_network(spec)
        t1 = fleet(spec['depot_events'])['T1']
        t1_route = network.earliest_route('B', 'J1', spec['fleet_ready_at'], t1)
        self.assertTrue(t1_route['feasible'])
        self.assertEqual(t1_route['arrival_at'], '2026-11-24T09:08')
        self.assertEqual(t1_route['evidence'],
                         ['R-DEPOT-B', 'R-DETOUR-1', 'R-DETOUR-2'])
        result = plan(spec)
        j1 = served(result)['JOB-J1']
        self.assertEqual(j1['machine_id'], 'T2')
        self.assertEqual(j1['arrive_at'], '2026-11-24T08:30')
        j1_notice = next(n for n in result['notices'] if n['job_id'] == 'JOB-J1')
        self.assertEqual(j1_notice['notice_type'], 'CHANGE_NOTICE')
        self.assertEqual(j1_notice['confirmed_machine_id'], 'T1')
        self.assertEqual(j1_notice['new_machine_id'], 'T2')

    def test_bridge_reduced_to_block_both_machines_is_recalculated(self):
        spec = copy.deepcopy(self.spec)
        bridge = next(road for road in spec['road_network']['roads']
                      if road['road_id'] == 'R-BRIDGE')
        bridge['weight_limit_t'] = 10
        result = plan(spec)
        j1 = served(result)['JOB-J1']
        self.assertNotIn('R-BRIDGE', j1['route']['evidence'])
        self.assertEqual([leg['road_id'] for leg in j1['route']['legs']],
                         ['R-DETOUR-1', 'R-DETOUR-2'])
        self.assertEqual(j1['arrive_at'], '2026-11-24T08:58')
        network = build_network(spec)
        self.assertEqual(network.inspect(
            'R-BRIDGE', '2026-11-24T08:00',
            {'machine_id': 'T2', 'gross_weight_t': 24}, 'A', 'J1'), [WEIGHT_EXCEEDED])

    def test_every_plan_leg_carries_document_evidence_not_straight_line_time(self):
        from datetime import timedelta
        for pack in (None, 'permit_pack', 'closure_pack'):
            result = plan(self.spec, pack)
            for item in result['assignments']:
                self.assertTrue(item['route']['legs'])
                self.assertTrue(item['evidence'])
                for leg in item['route']['legs']:
                    self.assertTrue(leg['road_id'])
                    self.assertIn(leg['road_id'], leg['evidence'])
                    self.assertEqual(
                        parse_time(leg['exit_at']),
                        parse_time(leg['entry_at']) + timedelta(minutes=leg['minutes']))
                self.assertEqual(
                    item['route']['travel_minutes'],
                    sum(leg['minutes'] for leg in item['route']['legs']))


if __name__ == '__main__':
    unittest.main()
