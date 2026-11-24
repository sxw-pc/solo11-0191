"""离线恢复证明测试，覆盖任务要求的五类验证：

1. 混版停机（ECU1 单独 v2 不成立，ECU2 经启动验证 v2 后才可出车）；
2. 板型/字节签名/校准模式三匹配；
3. 校准 K1->K2 中断与断电幂等（不可逆写入不得重复）；
4. 同一断点上的两条合法恢复（令牌降级 v1 基础耕作 / 前向 v2 变幅控制）；
5. 旧升级公告与新旧结论的保留及差异解释。

运行::

    python -B -m unittest discover -s tests -v
"""
import copy
import json
import unittest
from pathlib import Path

from fieldmachine import firmware_capture as fc
from fieldmachine.firmware import versions
from fieldmachine.fleet_update import ready, readiness

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'firmware-mixed.json'
SEED_FIXTURE = ROOT / 'fixtures' / 'firmware-ok.json'


def load_mixed():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def fresh_breakpoint(**kwargs):
    return fc.breakpoint_state(**kwargs)


# ---------------------------------------------------------------------------
# 1) 混版停机：single-board inference is forbidden
# ---------------------------------------------------------------------------

class MixedVersionHoldTest(unittest.TestCase):
    """一、混版组合必须停机，不能按单板版本推断整机。"""

    def test_fixed_breakpoint_versions_are_ecu1_v2_ecu2_v1(self):
        """固定断点：硬件回读与事件投影均为 ECU1=v2、ECU2=v1。"""
        state = fresh_breakpoint()
        self.assertEqual(
            {'ECU1': 'v2', 'ECU2': 'v1'}, fc.HardwareBank(state).versions()
        )
        events = load_mixed()['events']
        self.assertEqual({'ECU1': 'v2', 'ECU2': 'v1'}, versions(events))

    def test_seed_fixture_no_longer_reports_ready(self):
        """firmware-ok.json 的混版数据不再被错误判为 ready。"""
        doc = json.loads(SEED_FIXTURE.read_text(encoding='utf-8'))
        self.assertFalse(ready(doc['events'], doc['target']))

    def test_ecu1_alone_at_v2_is_not_enough(self):
        """只有 ECU1 或任一单板达到 v2 都不能出车。"""
        self.assertFalse(ready([{'controller_id': 'ECU1', 'version': 'v2'}], 'v2'))
        self.assertFalse(ready([{'controller_id': 'ECU2', 'version': 'v2'}], 'v2'))
        mixed = [
            {'controller_id': 'ECU1', 'version': 'v2'},
            {'controller_id': 'ECU2', 'version': 'v1'},
        ]
        self.assertFalse(ready(mixed, 'v2'))
        self.assertTrue(ready([
            {'controller_id': 'ECU1', 'version': 'v2'},
            {'controller_id': 'ECU2', 'version': 'v2'},
        ], 'v2'))

    def test_ecu2_must_be_boot_verified_v2_to_flip_ready(self):
        """ECU2 补成经启动验证的 v2 后，ready 才由停机翻为可出车。"""
        fx = load_mixed()
        events = fx['events']
        self.assertFalse(ready(events, 'v2'))
        failed = events + [{
            'controller_id': 'ECU2', 'version': 'v2',
            'boot_verified': False,
        }]
        self.assertFalse(ready(failed, 'v2'))
        report = readiness(failed, 'v2')
        self.assertTrue(any('boot_self_test_failed' in r for r in report['reasons']))
        verified = events + [{
            'controller_id': 'ECU2', 'version': 'v2',
            'boot_verified': True, 'active_partition': 'B',
        }]
        self.assertEqual({'ECU1': 'v2', 'ECU2': 'v2'}, versions(verified))
        self.assertTrue(ready(verified, 'v2'))

    def test_breakpoint_assessment_grounds_machine_with_no_functions(self):
        """断点整机 grounded：安全功能集为空，并给出含两条路线的恢复断点。"""
        verdict = fc.assess(fresh_breakpoint())
        self.assertTrue(verdict['grounded'])
        self.assertFalse(verdict['ready'])
        self.assertFalse(verdict['can_dispatch'])
        self.assertEqual([], verdict['functions'])
        self.assertIn('mixed_combination_unconfirmed', verdict['reasons'])
        self.assertFalse(verdict['checks']['combination_listed'])
        bp = verdict['breakpoint']
        self.assertIsNotNone(bp)
        self.assertIn('forward_v2', bp['resume_options'])
        self.assertIn('downgrade_v1', bp['resume_options'])
        self.assertEqual({'ECU1': 'v2', 'ECU2': 'v1'}, bp['controller_versions'])

    def test_unsigned_or_tampered_matrix_also_grounds(self):
        """兼容矩阵签名失效时同样必须停机。"""
        bad_matrix = copy.deepcopy(fc.DEFAULT_MATRIX)
        bad_matrix['entries'][0]['functions'] = ['amplitude_control']
        verdict = fc.assess(fresh_breakpoint(), matrix=bad_matrix)
        self.assertFalse(verdict['checks']['matrix_signature'])
        self.assertIn('matrix_signature_invalid', verdict['reasons'])
        self.assertTrue(verdict['grounded'])

    def test_signed_matrix_can_certify_an_explicit_combination(self):
        """只有厂商签名矩阵明确列入的组合（即便混版）才按其功能集放行。"""
        matrix = fc.seal_document({
            'matrix_id': 'CM-TEST-CERTIFIED-MIX',
            'entries': [{
                'id': 'CMB-test-mixed-limited',
                'controllers': {'ECU1': 'v2', 'ECU2': 'v1'},
                'calibration': 'K1',
                'status': 'certified',
                'functions': ['basic_tillage'],
            }],
        })
        state = fresh_breakpoint()
        state['upgrade_job']['closed'] = True
        verdict = fc.assess(state, matrix=matrix)
        self.assertTrue(verdict['checks']['matrix_signature'])
        self.assertTrue(verdict['checks']['combination_listed'])
        self.assertTrue(verdict['roadworthy'], verdict['reasons'])
        self.assertEqual(['basic_tillage'], verdict['functions'])


# ---------------------------------------------------------------------------
# 2) 固件字节签名 + 板型适配 + 校准数据模式 triple match
# ---------------------------------------------------------------------------

class PackageTripleMatchTest(unittest.TestCase):
    """二、字节签名、板型适配、校准模式必须同时匹配。"""

    def setUp(self):
        self.packages = {
            (p['controller_id'], p['version']): p
            for p in fc.build_default_packages()
        }

    def test_all_known_packages_pass_triple_check(self):
        """fixture 中 v1/v2 包对各自板型全部验签通过。"""
        for (cid, ver), pkg in self.packages.items():
            report = fc.verify_package(pkg, board_type=fc.BOARD_TYPES[cid])
            self.assertTrue(report['ok'], (cid, ver, report['errors']))
            self.assertTrue(report['checks']['byte_signature'])
            self.assertTrue(report['checks']['byte_digest'])
            self.assertTrue(report['checks']['board_type'])

    def test_byte_tampering_is_rejected(self):
        """固件字节被改动后摘要/验签失败。"""
        pkg = copy.deepcopy(self.packages[('ECU2', 'v2')])
        pkg['image'] = pkg['image'] + 'TAMPERED-BYTE'
        report = fc.verify_package(pkg, board_type=fc.BOARD_TYPES['ECU2'])
        self.assertFalse(report['ok'])
        self.assertIn('firmware_byte_digest_mismatch', report['errors'])

    def test_rehashed_but_forged_signature_is_rejected(self):
        """即使重算摘要，没有厂商签名仍被拒绝。"""
        pkg = copy.deepcopy(self.packages[('ECU2', 'v2')])
        pkg['image'] = pkg['image'] + 'x'
        pkg['sha256'] = fc.sha256_hex(pkg['image'])
        report = fc.verify_package(pkg, board_type=fc.BOARD_TYPES['ECU2'])
        self.assertFalse(report['checks']['byte_signature'])
        self.assertIn('firmware_signature_invalid', report['errors'])

    def test_board_type_mismatch_is_rejected(self):
        """ECU2 包不能适配 ECU1 板型。"""
        pkg = self.packages[('ECU2', 'v2')]
        report = fc.verify_package(pkg, board_type=fc.BOARD_TYPES['ECU1'])
        self.assertFalse(report['ok'])
        self.assertTrue(any(e.startswith('board_type_mismatch')
                            for e in report['errors']))

    def test_calibration_schema_must_match_not_just_version(self):
        """包要求的校准模式必须与提交模式一致，不能只改版本号。"""
        k1_pkg = self.packages[('ECU1', 'v1')]
        self.assertTrue(fc.verify_package(
            k1_pkg, board_type=fc.BOARD_TYPES['ECU1'],
            calibration_schema='K1')['ok'])
        mismatch = fc.verify_package(
            k1_pkg, board_type=fc.BOARD_TYPES['ECU1'],
            calibration_schema='K2')
        self.assertFalse(mismatch['ok'])
        self.assertFalse(mismatch['checks']['calibration_schema_match'])
        self.assertTrue(any(e.startswith('calibration_schema_mismatch')
                            for e in mismatch['errors']))

    def test_route_refuses_wrong_board_package_before_flash(self):
        """错误板型包在任何 flash 动作之前就令路线停机。"""
        bad = fc.build_package(controller_id='ECU2', version='v2',
                               board_type='WORK-CTRL-OTHER-REV9')
        state = fresh_breakpoint()
        before = state['counters']['flash']
        result = fc.route_forward(state, package=bad)
        self.assertTrue(result['halted'])
        self.assertEqual('package_verification_failed',
                         result['breakpoint']['failure_code'])
        self.assertFalse(result['can_dispatch'])
        self.assertEqual(before, state['counters']['flash'])


# ---------------------------------------------------------------------------
# 3) 校准中断与提交幂等
# ---------------------------------------------------------------------------

class CalibrationInterruptionTest(unittest.TestCase):
    """三、校准 K1->K2 中断可识别，且不可逆写入断电重试不重复。"""

    def test_relabelled_k1_as_k2_is_rejected(self):
        """只把 schema 从 K1 改成 K2 的“转换”过不了严格校验。"""
        k1 = fc.default_k1('FM-07')
        fake_k2 = dict(k1)
        fake_k2['schema'] = 'K2'
        report = fc.validate_calibration(fake_k2, schema='K2')
        self.assertFalse(report['ok'])
        self.assertTrue(any(e.startswith('k2_field_missing') for e in report['errors']))

    def test_real_conversion_is_complete_and_reverifiable(self):
        """真正的 K2 转换生成定点量、分段幅宽与转换摘要并可复验。"""
        k1 = fc.default_k1('FM-07')
        k2 = fc.convert_calibration(k1)
        self.assertEqual('K2', k2['schema'])
        self.assertTrue(fc.validate_calibration(k2)['ok'])
        proof = fc.verify_conversion(k1, k2)
        self.assertTrue(proof['ok'], proof['errors'])
        self.assertAlmostEqual(
            sum(k2['section_widths_m']), k1['work_width_m'], places=9
        )

    def test_breakpoint_exposes_uncommitted_valid_k2_in_readback(self):
        """回读显示 K1 已提交、K2 有效但未提交，且双分区字段齐全。"""
        cap = fc.capture(fresh_breakpoint())
        self.assertTrue(cap['readback'])
        self.assertEqual('K1', cap['calibration']['committed_schema'])
        self.assertEqual('K2', cap['calibration']['staged_schema'])
        self.assertTrue(cap['calibration']['staged_valid'])
        self.assertTrue(cap['calibration']['transition_open'])
        for cid in ('ECU1', 'ECU2'):
            p = cap['partitions'][cid]
            self.assertIn('active_partition', p)
            self.assertIn('pending_partition', p)
            self.assertEqual(p['active_partition'], p['active_slot'])

    def test_corrupt_half_written_k2_halts_instead_of_rewriting(self):
        """半写/损坏 K2 页必须停机给断点，而不是擅自再次不可逆写入。"""
        state = fresh_breakpoint(k2='corrupt')
        result = fc.route_forward(state, packages=fc.build_default_packages())
        self.assertTrue(result['halted'])
        self.assertEqual('calibration_page_corrupt',
                         result['breakpoint']['failure_code'])
        self.assertTrue(result['assessment']['grounded'])
        self.assertEqual(1, state['counters']['calibration_program'])

    def test_repeated_forward_upgrades_never_reprogram_calibration(self):
        """重复执行前向升级（含断电重启）校准页编程次数始终为 1。"""
        packages = fc.build_default_packages()
        hw = fc.HardwareBank(fresh_breakpoint())
        self.assertEqual(1, hw.state['counters']['calibration_program'])
        first = fc.route_forward(hw, packages=packages)
        self.assertTrue(first['ok'])
        self.assertEqual(1, hw.state['counters']['calibration_program'])
        self.assertEqual(1, hw.state['counters']['calibration_commit'])
        hw.power_cycle()
        second = fc.route_forward(hw, packages=packages)
        self.assertTrue(second['ok'])
        self.assertEqual(1, hw.state['counters']['calibration_program'])
        self.assertEqual(1, hw.state['counters']['calibration_commit'])
        self.assertEqual(2, hw.state['counters']['flash'])

    def test_k2_program_happens_exactly_once_across_crash_before_commit(self):
        """更早断点：本次完成唯一一次写入，断电重试只跳过/提交。"""
        hw = fc.HardwareBank(fresh_breakpoint(k2='missing'))
        self.assertEqual(0, hw.state['counters']['calibration_program'])
        evidence = fc.ensure_k2_committed(hw)
        self.assertEqual(1, evidence['calibration_writes'])
        self.assertEqual(1, hw.state['counters']['calibration_program'])
        hw = hw.power_cycle()
        again = fc.ensure_k2_committed(hw)
        self.assertEqual('skip_already_committed', again['action'])
        self.assertEqual(1, hw.state['counters']['calibration_program'])

    def test_resume_between_program_and_commit_does_not_reprogram(self):
        """写页后、提交标记前断电：续跑只提交，不再写校准页。"""
        hw = fc.HardwareBank(fresh_breakpoint(k2='missing'))
        k2 = fc.convert_calibration(fc.default_k1())
        hw.program_calibration_page('K2', k2)
        self.assertEqual(1, hw.state['counters']['calibration_program'])
        hw = hw.power_cycle()
        result = fc.route_forward(hw, packages=fc.build_default_packages())
        self.assertTrue(result['ok'], result.get('breakpoint'))
        self.assertEqual(1, hw.state['counters']['calibration_program'])
        self.assertEqual(1, hw.state['counters']['calibration_commit'])


# ---------------------------------------------------------------------------
# 4a) 合法恢复路线 A：本机令牌降级 v1
# ---------------------------------------------------------------------------

class DowngradeRouteTest(unittest.TestCase):
    """四-A、凭本机厂商恢复令牌从 ECU1 保留分区降级 v1。"""

    def setUp(self):
        self.machine_id = 'FM-07'
        self.token = fc.issue_recovery_token(machine_id=self.machine_id)

    def test_downgrade_without_valid_token_is_refused(self):
        """无令牌/伪令牌/他机令牌/过期令牌一律拒绝且不改启动分区。"""
        for bad_token, label in [
            (None, 'missing'),
            ({'machine_id': self.machine_id, 'action': 'downgrade_to_v1'}, 'unsigned'),
            (fc.issue_recovery_token(machine_id='FM-OTHER'), 'other_machine'),
            (fc.issue_recovery_token(
                machine_id=self.machine_id,
                expires_at='2020-01-01T00:00:00',
                token_id='RT-EXPIRED'), 'expired'),
        ]:
            with self.subTest(label=label):
                state = fresh_breakpoint(machine_id=self.machine_id)
                result = fc.route_downgrade(
                    state, token=bad_token,
                    now='2026-11-24T12:00:00',
                )
                self.assertTrue(result['halted'])
                self.assertFalse(result['can_dispatch'])
                self.assertEqual('recovery_token_invalid',
                                 result['breakpoint']['failure_code'])
                self.assertEqual('B', state['controllers']['ECU1']['active_slot'])

    def test_forged_token_signature_is_rejected(self):
        """篡改令牌目标版本会破坏厂商签名。"""
        token = fc.issue_recovery_token(machine_id=self.machine_id)
        forged = copy.deepcopy(token)
        forged['target_versions'] = {'ECU1': 'v9', 'ECU2': 'v9'}
        report = fc.verify_recovery_token(forged, self.machine_id)
        self.assertFalse(report['ok'])
        self.assertIn('recovery_token_signature_invalid', report['errors'])

    def test_legal_downgrade_reaches_v1_basic_tillage_only(self):
        """合法降级后两板 v1：可出车基础耕作，不可用变幅控制及对应调度。"""
        state = fresh_breakpoint(machine_id=self.machine_id)
        result = fc.route_downgrade(state, token=self.token)
        self.assertTrue(result['ok'], result.get('breakpoint'))
        verdict = result['assessment']
        self.assertTrue(verdict['roadworthy'])
        self.assertTrue(verdict['can_dispatch'])
        self.assertEqual({'ECU1': 'v1', 'ECU2': 'v1'},
                         verdict['controller_versions'])
        self.assertEqual(['basic_tillage'], verdict['functions'])
        self.assertNotIn('amplitude_control', verdict['functions'])
        sched = verdict['scheduling']
        self.assertTrue(sched['can_dispatch'])
        self.assertFalse(sched['amplitude_control_available'])
        self.assertNotIn('amplitude_tillage', sched['eligible_task_types'])
        self.assertIn('basic_tillage', sched['eligible_task_types'])

    def test_downgrade_uses_ecu1_retained_partition_and_rolls_k2(self):
        """ECU1 从保留 A 分区启动 v1；未提交 K2 被回退，K1 不重写。"""
        state = fresh_breakpoint(machine_id=self.machine_id)
        result = fc.route_downgrade(state, token=self.token)
        self.assertTrue(result['ok'])
        p1 = fc.HardwareBank(state).read_partitions('ECU1')
        self.assertEqual('A', p1['active_partition'])
        self.assertEqual('v1', p1['active_version'])
        self.assertTrue(p1['active_boot_verified'])
        self.assertEqual('discarded_rollback',
                         state['calibration']['pages']['K2']['state'])
        self.assertEqual('K1', state['calibration']['commit_marker']['schema'])
        self.assertEqual(1, state['counters']['calibration_program'])
        self.assertEqual(0, state['counters']['calibration_commit'])
        step_names = [s['step'] for s in result['trail']]
        self.assertIn('restore_ECU1_retained_A_v1', step_names)
        self.assertIn('rollback_uncommitted_K2', step_names)

    def test_downgrade_is_idempotent_over_power_cycles(self):
        """降级路线在断电重启后用同一令牌重放，结果稳定且不重复回退写入。"""
        hw = fc.HardwareBank(fresh_breakpoint(machine_id=self.machine_id))
        first = fc.route_downgrade(hw, token=self.token)
        self.assertTrue(first['ok'])
        hw = hw.power_cycle()
        second = fc.route_downgrade(hw, token=self.token)
        self.assertTrue(second['ok'])
        self.assertEqual(['basic_tillage'], second['assessment']['functions'])
        self.assertEqual(1, hw.state['counters']['calibration_discard'])
        self.assertEqual(1, hw.state['counters']['calibration_program'])

    def test_downgrade_boot_failure_keeps_machine_grounded(self):
        """恢复槽自检失败时保持停机并记录失败步骤断点。"""
        state = fresh_breakpoint(machine_id=self.machine_id)
        state['fault_injection']['self_test_fail'] = ['ECU1']
        result = fc.route_downgrade(state, token=self.token)
        self.assertTrue(result['halted'])
        self.assertIn('ECU1_boot_self_test_failed',
                      result['breakpoint']['failure_code'])
        self.assertFalse(result['assessment']['can_dispatch'])
        self.assertIsNotNone(result['breakpoint']['failed_step'])


# ---------------------------------------------------------------------------
# 4b) 合法恢复路线 B：K2 提交 + ECU2 v2 + 双板自检
# ---------------------------------------------------------------------------

class ForwardRouteTest(unittest.TestCase):
    """四-B、先提交 K2 再升 ECU2 v2 并逐板自检，得到变幅控制能力。"""

    def setUp(self):
        self.packages = fc.build_default_packages()

    def test_legal_forward_reaches_v2_with_amplitude_control(self):
        """前向恢复后两板 v2/K2：可出车且功能集含变幅控制与调度任务。"""
        state = fresh_breakpoint()
        result = fc.route_forward(state, packages=self.packages)
        self.assertTrue(result['ok'], result.get('breakpoint'))
        verdict = result['assessment']
        self.assertEqual({'ECU1': 'v2', 'ECU2': 'v2'},
                         verdict['controller_versions'])
        self.assertEqual('CMB-v2-v2', verdict['matrix_entry_id'])
        self.assertEqual(
            ['basic_tillage', 'amplitude_control'], verdict['functions']
        )
        sched = verdict['scheduling']
        self.assertTrue(sched['amplitude_control_available'])
        self.assertTrue(sched['variable_width_available'])
        self.assertIn('amplitude_tillage', sched['eligible_task_types'])
        self.assertIn('variable_width_work', sched['eligible_task_types'])
        self.assertTrue(state['upgrade_job']['closed'])
        self.assertNotEqual(False, state['upgrade_job']['completion_marker'])

    def test_forward_boots_ecu2_from_verified_v2_partition(self):
        """ECU2 从 B 分区启动 v2 且自检 passed。"""
        hw = fc.HardwareBank(fresh_breakpoint())
        result = fc.route_forward(hw, packages=self.packages)
        self.assertTrue(result['ok'])
        p2 = hw.read_partitions('ECU2')
        self.assertEqual('B', p2['active_partition'])
        self.assertEqual('v2', p2['active_version'])
        self.assertTrue(p2['active_boot_verified'])
        self.assertEqual('passed', hw.controller('ECU2')['slots']['B']['self_test'])

    def test_forward_is_idempotent_over_power_cycles(self):
        """前向路线断电重放不重复刷写/校准，最终仍 v2 可出车。"""
        hw = fc.HardwareBank(fresh_breakpoint())
        self.assertTrue(fc.route_forward(hw, packages=self.packages)['ok'])
        hw = hw.power_cycle()
        again = fc.route_forward(hw, packages=self.packages)
        self.assertTrue(again['ok'])
        self.assertEqual(1, hw.state['counters']['calibration_program'])
        self.assertEqual(1, hw.state['counters']['calibration_commit'])
        self.assertEqual(2, hw.state['counters']['flash'])

    def test_ecu2_self_test_failure_holds_at_boot_breakpoint(self):
        """ECU2 v2 自检失败：停在启动断点，活动分区仍是已知良好 v1。"""
        state = fresh_breakpoint()
        state['fault_injection']['self_test_fail'] = ['ECU2']
        result = fc.route_forward(state, packages=self.packages)
        self.assertTrue(result['halted'])
        self.assertEqual('ECU2_boot_self_test_failed',
                         result['breakpoint']['failure_code'])
        self.assertFalse(result['can_dispatch'])
        self.assertTrue(result['assessment']['grounded'])
        partitions = fc.HardwareBank(state).read_partitions('ECU2')
        self.assertEqual('A', partitions['active_partition'])
        self.assertEqual('v1', partitions['active_version'])
        self.assertEqual('boot_failed',
                         state['controllers']['ECU2']['slots']['B']['state'])

    def test_tampered_package_halts_and_keeps_grounded_breakpoint(self):
        """签名伪造的 ECU2 包令前向路线在验包步停机。"""
        packages = copy.deepcopy(self.packages)
        for p in packages:
            if (p['controller_id'], p['version']) == ('ECU2', 'v2'):
                p['signature'] = '0' * 64
        state = fresh_breakpoint()
        result = fc.route_forward(state, packages=packages)
        self.assertTrue(result['halted'])
        self.assertEqual('package_verification_failed',
                         result['breakpoint']['failure_code'])
        self.assertFalse(result['assessment']['can_dispatch'])

    def test_any_failure_result_carries_resumable_breakpoint(self):
        """任何步骤失败都保持停机并给出失败步骤与两条续跑选项。"""
        state = fresh_breakpoint(k2='corrupt')
        result = fc.route_forward(state, packages=self.packages)
        bp = result['breakpoint']
        self.assertTrue(result['halted'])
        self.assertIsNotNone(bp)
        self.assertTrue(bp.get('failed_step'))
        self.assertIn('forward_v2', bp['resume_options'])
        self.assertIn('downgrade_v1', bp['resume_options'])


# ---------------------------------------------------------------------------
# 同一断点两条路线 -> 不同功能集合与调度能力
# ---------------------------------------------------------------------------

class TwoRoutesDifferentCapabilityTest(unittest.TestCase):
    """同一断点、同一已验证包与硬件状态，两条合法路线能力不同。"""

    def test_same_breakpoint_yields_two_legal_but_different_outcomes(self):
        """两路均可出车，但仅前向 v2 路线多出 amplitude_control。"""
        packages = fc.build_default_packages()
        token = fc.issue_recovery_token(machine_id='FM-07')

        down = fc.route_downgrade(fresh_breakpoint(), token=token)
        forward = fc.route_forward(fresh_breakpoint(), packages=packages)
        self.assertTrue(down['ok'])
        self.assertTrue(forward['ok'])

        cmp_ = fc.compare_recoveries(down, forward)
        self.assertTrue(cmp_['both_roadworthy'])
        self.assertEqual(['amplitude_control'], cmp_['difference'])
        self.assertEqual(['basic_tillage'],
                         cmp_['functions_by_route']['downgrade_v1'])
        self.assertEqual(['basic_tillage', 'amplitude_control'],
                         cmp_['functions_by_route']['forward_v2'])
        self.assertFalse(cmp_['scheduling_by_route']['downgrade_v1']
                         ['amplitude_control_available'])
        self.assertTrue(cmp_['scheduling_by_route']['forward_v2']
                        ['amplitude_control_available'])

    def test_both_routes_obey_mixed_version_hold_and_signatures(self):
        """两条终点组合都落在签名矩阵内，校准已提交且事务关闭。"""
        token = fc.issue_recovery_token(machine_id='FM-07')
        down = fc.route_downgrade(fresh_breakpoint(), token=token)
        forward = fc.route_forward(
            fresh_breakpoint(), packages=fc.build_default_packages()
        )
        for result, expected in [
            (down, {'ECU1': 'v1', 'ECU2': 'v1'}),
            (forward, {'ECU1': 'v2', 'ECU2': 'v2'}),
        ]:
            v = result['assessment']
            self.assertTrue(v['checks']['matrix_signature'])
            self.assertTrue(v['checks']['combination_listed'])
            self.assertEqual(expected, v['controller_versions'])
            self.assertTrue(v['checks']['calibration_committed'])
            self.assertTrue(v['checks']['transaction_closed'])


# ---------------------------------------------------------------------------
# 5) 历史升级公告与新结论：保留且可解释差异
# ---------------------------------------------------------------------------

class AnnouncementHistoryTest(unittest.TestCase):
    """五、旧升级公告/旧结论保留，新可出车结论可解释差异。"""

    def setUp(self):
        self.fx = fc.build_fixture()
        self.ledger = fc.HistoryLedger.from_fixture(self.fx)
        self.old_announcements = copy.deepcopy(self.ledger.announcements)
        self.old_conclusions = copy.deepcopy(self.ledger.conclusions)

    def test_old_announcement_and_wrong_conclusion_are_present(self):
        """历史中同时保留 v2 升级公告与旧的“任一板即 ready”结论。"""
        ids_a = [a['id'] for a in self.ledger.announcements]
        ids_c = [c['id'] for c in self.ledger.conclusions]
        self.assertIn('ANN-20261120-V2', ids_a)
        self.assertIn('C-20261123-OLD', ids_c)
        old = self.ledger.get('C-20261123-OLD')
        self.assertTrue(old['ready'])
        self.assertEqual('any_controller_at_target', old['rule'])

    def test_history_is_append_only_and_explains_hold_difference(self):
        """追加新停机结论与两条恢复结论后，旧记录逐字未改且差异可解释。"""
        hold = self.ledger.add_conclusion(
            fc.assess(self.fx['hardware']),
            conclusion_id='C-NEW-HOLD',
        )
        self.assertFalse(hold['can_dispatch'])

        down = fc.route_downgrade(
            self.fx, token=self.fx['recovery_token'], ledger=self.ledger,
            conclusion_id='C-NEW-DOWN',
        )
        self.assertTrue(down['ok'])
        forward = fc.route_forward(
            fresh_breakpoint(), packages=self.fx['packages'],
            ledger=self.ledger, conclusion_id='C-NEW-FWD',
        )
        self.assertTrue(forward['ok'])

        self.assertEqual(self.old_announcements, self.ledger.announcements[:1])
        self.assertEqual(
            self.old_conclusions[0],
            self.ledger.get('C-20261123-OLD'),
        )
        self.assertEqual(1, len(self.ledger.announcements))
        self.assertEqual(
            ['C-20261123-OLD', 'C-NEW-HOLD', 'C-NEW-DOWN', 'C-NEW-FWD'],
            [c['id'] for c in self.ledger.conclusions],
        )

        diff = self.ledger.difference('C-20261123-OLD', 'C-NEW-HOLD')
        self.assertTrue(diff['dispatch_changed'])
        self.assertTrue(diff['old_dispatchable'])
        self.assertFalse(diff['new_dispatchable'])
        self.assertTrue(diff['changes'])
        joined = json.dumps(diff, ensure_ascii=False)
        self.assertIn('mixed_combination_unconfirmed', joined)
        self.assertIn('compatibility matrix', joined)

    def test_route_conclusions_explain_different_function_sets(self):
        """降级结论与前向结论的差异明确落在 amplitude_control。"""
        token = fc.issue_recovery_token(machine_id='FM-07')
        self.ledger.add_conclusion(
            fc.route_downgrade(fresh_breakpoint(), token=token)['assessment'],
            conclusion_id='C-DOWN', route='downgrade_v1',
        )
        self.ledger.add_conclusion(
            fc.route_forward(
                fresh_breakpoint(), packages=fc.build_default_packages()
            )['assessment'],
            conclusion_id='C-FWD', route='forward_v2',
        )
        diff = fc.difference_report(
            self.ledger.get('C-DOWN'), self.ledger.get('C-FWD')
        )
        self.assertEqual(['basic_tillage'], diff['functions_old'])
        self.assertEqual(['basic_tillage', 'amplitude_control'],
                         diff['functions_new'])
        self.assertFalse(diff['dispatch_changed'])
        self.assertTrue(any('amplitude_control' in c for c in diff['changes']))

    def test_old_announcement_survives_both_recoveries(self):
        """两条恢复执行后旧公告仍在，且正文声明了兼容矩阵要求。"""
        fc.route_downgrade(
            self.fx, token=self.fx['recovery_token'], ledger=self.ledger
        )
        fc.route_forward(
            fresh_breakpoint(), packages=self.fx['packages'], ledger=self.ledger
        )
        ann = self.ledger.get('ANN-20261120-V2')
        self.assertIsNotNone(ann)
        self.assertIn('兼容矩阵', ann['body_zh'])


# ---------------------------------------------------------------------------
# 交付 fixture 文件与职责边界
# ---------------------------------------------------------------------------

class DeliveredFixtureAndScopeTest(unittest.TestCase):
    """交付物存在、firmware-mixed.json 自洽，且不越界重判其他业务。"""

    def test_required_files_exist(self):
        """五个指定交付路径全部存在。"""
        for rel in (
            'fieldmachine/firmware_capture.py',
            'fieldmachine/firmware.py',
            'fieldmachine/fleet_update.py',
            'fixtures/firmware-mixed.json',
            'tests/test_firmware_recovery.py',
        ):
            self.assertTrue((ROOT / rel).exists(), rel)

    def test_firmware_mixed_fixture_is_self_consistent_evidence(self):
        """磁盘上的 firmware-mixed.json 可直接验签并得出停机结论。"""
        fx = load_mixed()
        self.assertEqual('v2', fx['target'])
        self.assertFalse(ready(fx))
        verdict = fc.assess(fx)
        self.assertTrue(verdict['grounded'])
        self.assertIn('mixed_combination_unconfirmed', verdict['reasons'])
        self.assertTrue(fc.verify_matrix(fx['matrix'])['ok'])
        token_report = fc.verify_recovery_token(
            fx['recovery_token'], fx['machine_id']
        )
        self.assertTrue(token_report['ok'], token_report['errors'])
        for pkg in fx['packages']:
            self.assertTrue(fc.verify_package(
                pkg, board_type=fc.BOARD_TYPES[pkg['controller_id']]
            )['ok'], pkg['package_id'])
        self.assertIn('announcements', fx['history'])

    def test_scope_excludes_settlement_evacuation_and_routing(self):
        """结论只覆盖功能集与出车，明确排除结算/险情撤离/调拨路线。"""
        verdict = fc.assess(fresh_breakpoint())
        for excluded in (
            'plot_settlement', 'danger_evacuation_order', 'allocation_route'
        ):
            self.assertIn(excluded, verdict['scope']['excluded'])


if __name__ == '__main__':
    unittest.main()
