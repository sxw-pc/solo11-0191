import json
import unittest
from pathlib import Path

from fieldmachine.harvest_capture import (
    capture_harvest, normalize_clocks, header_on_windows, cut_coverage,
    reconcile, hopper_ledger, apply_calibration, HarvestConservationError,
    CalibrationError)
from fieldmachine.yield_map import kilograms, MapBook, review_priority
from fieldmachine import yield_stream

ROOT = Path(__file__).resolve().parents[1]
BOUNDARY = ROOT / 'fixtures' / 'yield-boundary.json'
SEED = ROOT / 'fixtures' / 'yield-ok.json'


class HarvestYieldTest(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(BOUNDARY.read_text(encoding='utf-8'))
        self.cal_a = self.fixture['calibrations']['flush_trial']
        self.cal_b = self.fixture['calibrations']['bypass_weigh']

    def test_01_three_independent_clocks_are_synchronized(self):
        """割台、流量、GPS 各自独立时钟，统一换算到主时钟后才可比对。"""
        header, fixes, pulses, offsets = normalize_clocks(self.fixture)
        self.assertEqual(offsets, {'header': -3, 'flow': 5, 'gps': 2})
        # 割台本地 535（on）+(-3) 偏移 -> 主时 532；F1 在主时 540 结束
        self.assertEqual(header[0], (532, 'on'))
        self.assertEqual(header[1], (540, 'off'))
        # 流量本地 537 的边界脉冲 -> 主时 542；GPS 本地 530 -> 主时 532
        pb = next(p for p in pulses if p['pulse_id'] == 'pB')
        self.assertEqual(pb['at_min'], 542)
        self.assertEqual(fixes[0]['at_min'], 532)
        # 换算只做平移，各流原始读数不被改写
        self.assertEqual(self.fixture['flow_pulses'][2]['at_min'], 537)

    def test_02_header_off_and_headland_turn_create_no_mass(self):
        """割台关闭段（540-541 地头转弯）不产生任何收获质量，切割窗落在关闭段即被拒。"""
        cap = capture_harvest(self.fixture)
        self.assertEqual(cap.header_windows, ((532, 540), (541, 546)))
        for pulse in cap.pulses:
            lo, hi = pulse.cut_window
            self.assertTrue(
                any(max(lo, wlo) <= min(hi, whi) for wlo, whi in cap.header_windows),
                f'{pulse.pulse_id} 的切割窗 {pulse.cut_window} 无割台开启证据')
        # 关闭段一分钟内没有任何脉冲把它当作切割位置
        for pulse in cap.pulses:
            self.assertFalse(pulse.cut_window[0] > 540 and pulse.cut_window[1] < 541)

    def test_03_repeated_track_does_not_double_count(self):
        """重复经过同一网格只保留一次覆盖，流量质量也不被重复创造。"""
        header, fixes, pulses, _ = normalize_clocks(self.fixture)
        windows = header_on_windows(header)
        coverage = cut_coverage(windows, fixes, self.fixture['fields'],
                                self.fixture['grid_size_m'])
        # 轨迹在 x=15 处回退重复经过，该格经过次数 >1 但仍是一个唯一网格
        repeated = [cell for cell, n in coverage['F1'].items() if n > 1]
        self.assertTrue(repeated)
        self.assertEqual(len(coverage['F1']), len(set(coverage['F1'])))
        cap = capture_harvest(self.fixture)
        # 总质量严格等于五个脉冲之和，没有任何重复轨迹额外创造 kg
        self.assertEqual(sum(a.kg for a in cap.pulses), 240)

    def test_04_hopper_residual_carries_across_fields_and_weighbridge_conserves(self):
        """车斗不清仓：F1 残粮带入 F2 段；过磅 240kg 只守恒，不向地块硬分。"""
        _, _, pulses, _ = normalize_clocks(self.fixture)
        ledger = hopper_ledger(pulses, self.fixture['load_events'],
                               self.fixture['weighings'])
        carry = next(e for e in ledger if e['kind'] == 'cross_field_carry')
        self.assertEqual(carry['unload_kg'], 0)
        self.assertIn('残粮', carry['note'])
        # 过界时车斗内 F1 已到达谷物（p1+p2=100）作为残粮继续挂账
        at_cross = [e for e in ledger if e['at_min'] == 540]
        self.assertTrue(any(e['hopper_kg'] >= 100 for e in at_cross))
        weigh = reconcile(pulses, self.fixture['weighings'])
        self.assertEqual(weigh['flow_total_kg'], 240)
        self.assertEqual(weigh['weighbridge_total_kg'], 240)
        self.assertFalse(weigh['scaling_allowed'])
        # 过磅单与流量不符时直接报错，而不是按比例摊派
        bad = [{'at_min': 555, 'action': 'unload', 'net_kg': 230, 'hopper_after_kg': 0}]
        with self.assertRaises(HarvestConservationError):
            reconcile(pulses, bad)

    def test_05_boundary_pulse_is_ambiguous_without_calibration(self):
        """动态滞后随吞吐变化：边界 40kg 无校准时 F1/F2 都只能给 100-140 区间。"""
        cap = capture_harvest(self.fixture)
        pb = next(a for a in cap.pulses if a.pulse_id == 'pB')
        self.assertTrue(pb.contingent)
        self.assertEqual(pb.kg, 40)
        self.assertEqual(set(pb.candidates), {'F1', 'F2'})
        self.assertEqual(cap.field_bounds['F1']['lo'], 100)
        self.assertEqual(cap.field_bounds['F1']['hi'], 140)
        self.assertEqual(cap.field_bounds['F2']['lo'], 100)
        self.assertEqual(cap.field_bounds['F2']['hi'], 140)
        # 田界交叉处的未定质量在网格层同样显示 lo-hi 范围
        boundary_cells = cap.grid['F2']
        ranged = [b for b in boundary_cells.values() if b['hi'] > b['lo']]
        self.assertTrue(ranged)
        priority = review_priority(MapBook().publish(cap))
        self.assertEqual(priority['status'], 'indeterminate')
        self.assertEqual(priority['order'], ())

    def test_06_flush_trial_path_reassigns_40kg_to_F1(self):
        """冲洗/清仓试验证明边界脉冲仍来自 F1：F1=140、F2=100，先复核 F2。"""
        cap_a = capture_harvest(self.fixture, calibration=self.cal_a)
        pb = next(a for a in cap_a.pulses if a.pulse_id == 'pB')
        self.assertEqual(pb.decided_field_id, 'F1')
        self.assertFalse(pb.contingent)
        self.assertEqual(cap_a.field_bounds['F1']['decided_kg'], 140)
        self.assertEqual(cap_a.field_bounds['F2']['decided_kg'], 100)
        self.assertEqual(cap_a.field_bounds['F1']['lo'], 140)
        self.assertEqual(cap_a.field_bounds['F2']['hi'], 100)
        priority = review_priority(MapBook().publish(cap_a))
        self.assertEqual(priority['status'], 'ranked')
        self.assertEqual(priority['order'][0], 'F2')

    def test_07_bypass_weigh_path_reassigns_40kg_to_F2(self):
        """旁路称重与清仓响应证明过界前 F1 已排尽：F1=100、F2=140，先复核 F1。"""
        cap_b = capture_harvest(self.fixture, calibration=self.cal_b)
        pb = next(a for a in cap_b.pulses if a.pulse_id == 'pB')
        self.assertEqual(pb.decided_field_id, 'F2')
        self.assertEqual(cap_b.field_bounds['F1']['decided_kg'], 100)
        self.assertEqual(cap_b.field_bounds['F2']['decided_kg'], 140)
        priority = review_priority(MapBook().publish(cap_b))
        self.assertEqual(priority['order'][0], 'F1')

    def test_08_stream_reclassify_moves_same_40kg_and_conserves_240(self):
        """yield_stream 改判：同一 40kg 从传感器 F2 移到割台 F1，过磅总量不变。

        仅按传感器读数比例缩放找不到真实空间来源；这里是逐脉冲身份转移。
        """
        _, _, pulses, _ = normalize_clocks(self.fixture)
        events = yield_stream.flow_events(pulses)
        before = kilograms(events)
        self.assertEqual(before['F1'], 100)
        self.assertEqual(before['F2'], 140)
        moved = yield_stream.reclassify(events, 'pB', from_field='F2',
                                        to_field='F1', kg=40)
        after = kilograms(moved)
        self.assertEqual(after['F2'], before['F2'] - 40)
        self.assertEqual(after['F1'], before['F1'] + 40)
        self.assertEqual(after, {'F1': 140, 'F2': 100})
        self.assertEqual(yield_stream.total_kg(events), 240)
        self.assertEqual(yield_stream.total_kg(moved), 240)
        # 改判必须点名真实脉冲；质量对不上或脉冲不存在都拒绝
        with self.assertRaises(ValueError):
            yield_stream.reclassify(events, 'pB', 'F2', 'F1', kg=99)
        with self.assertRaises(ValueError):
            yield_stream.reclassify(events, 'nope', 'F2', 'F1', kg=40)

    def test_09_late_calibration_recalculates_only_affected_and_keeps_old_map(self):
        """已发布图不得被迟到校准静默覆盖：只重算受影响地块/网格，旧图原样回读。"""
        book = MapBook()
        cap = capture_harvest(self.fixture)
        v1 = book.publish(cap, 'provisional')
        v2 = book.recalibrate(self.fixture, self.cal_a, 'flush-recalc')
        self.assertEqual(book.revisions, (1, 2))
        # 只影响 F1/F2 和田界网格，F1 内部 100kg 清晰网格 (4,0) 不在受影响集外被重算
        self.assertEqual(set(v2.affected_fields), {'F1', 'F2'})
        old = book.read_back(1)
        self.assertEqual(old['field_totals']['F1'],
                         {'lo': 100.0, 'hi': 140.0, 'decided_kg': 100.0,
                          'contingent_kg': 40.0})
        # 旧图对象本身冻结，回读结果不随新版本变化
        self.assertEqual(book.read(1).field_range('F1'), (100.0, 140.0))
        self.assertEqual(book.read(2).field_range('F1'), (140.0, 140.0))
        self.assertEqual(v2.calibration_id, 'CAL-FLUSH')
        # 再发一份不改变指纹的证据被拒绝，禁止制造空版本
        with self.assertRaises(ValueError):
            book.publish(capture_harvest(self.fixture, calibration=self.cal_a))

    def test_10_calibration_must_fit_measured_lag_band(self):
        """校准证据超出本机吞吐滞后带即拒绝，不得用任意断言收窄传输边界。"""
        bad_flush = dict(self.cal_a, flow_clear_master_min=549.0)
        with self.assertRaises(CalibrationError):
            capture_harvest(self.fixture, calibration=bad_flush)
        bad_bypass = dict(self.cal_b, cleanout_complete_master_min=540.5)
        with self.assertRaises(CalibrationError):
            capture_harvest(self.fixture, calibration=bad_bypass)
        # 对已有确定归属的清晰脉冲套用校准也必须拒绝
        cap = capture_harvest(self.fixture)
        wrong_target = dict(self.cal_a, applies_to_pulse='p1')
        with self.assertRaises(CalibrationError):
            apply_calibration(cap, wrong_target)

    def test_10b_pulse_without_any_cutting_position_is_rejected(self):
        """所有滞后带都把脉冲反推进割台关闭段时，质量不得静默消失。"""
        import copy
        broken = copy.deepcopy(self.fixture)
        # 把一个低吞吐脉冲推迟到割台彻底关闭之后，低带切割窗仍落在关闭段
        broken['flow_pulses'][3]['at_min'] = 552
        with self.assertRaises(HarvestConservationError):
            capture_harvest(broken)

    def test_11_seed_fixture_keeps_provisional_behavior(self):
        """原 yield-ok.json 口径不变：F1=120、F2=80 的传感器合计继续可用。"""
        seed = json.loads(SEED.read_text(encoding='utf-8'))
        self.assertEqual(kilograms(seed['events']), {'F1': 120, 'F2': 80})
        grouped = yield_stream.pulses(seed['events'])
        self.assertEqual({k: len(rows) for k, rows in grouped.items()},
                         {'F1': 1, 'F2': 1})


if __name__ == '__main__':
    unittest.main()
