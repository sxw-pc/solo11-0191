import json
import unittest
from pathlib import Path

from fieldmachine.asset_titles import TitleRegistry, owners
from fieldmachine.asset_transfer import TransferBoard, accepted
from fieldmachine.title_capture import TitleCapture, IdentityConflict

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def load_conflict():
    return json.loads((FIX / 'title-conflict.json').read_text(encoding='utf-8'))


def sales_from(events):
    return [e for e in events if e.get('kind') == 'sale']


def board_with(prefix, extra_titles=(), notices=()):
    fx = load_conflict()
    board = TransferBoard(prefix, sales_from(fx['shared']))
    for event in notices:
        board.ingest_notice(event)
    if extra_titles:
        board.ingest_titles(list(extra_titles))
    return board


class IdentityAnchoringTest(unittest.TestCase):
    def test_replate_is_alias_not_a_second_machine(self):
        fx = load_conflict()
        capture = TitleCapture(fx['shared'])
        # 新牌、旧牌、机具 id 全部解析到同一规范机具
        self.assertEqual(capture.canonical('P-001'), 'T1')
        self.assertEqual(capture.canonical('P-001X'), 'T1')
        self.assertEqual(capture.canonical('T1'), 'T1')
        view = capture.identity_view()
        self.assertNotIn('P-001X', view)          # 换牌没有另立一户
        self.assertIn('P-001X', view['T1']['aliases'])
        self.assertEqual(view['T1']['engine_no'], 'EN-001')

    def test_same_engine_other_doc_is_quarantined_and_cannot_transfer_twice(self):
        fx = load_conflict()
        capture = TitleCapture(fx['shared'] + fx['fraud_identity'])
        self.assertIsNone(capture.canonical('T1-FAKE'))
        self.assertIn('EN-001', capture.conflict_reason('T1-FAKE'))
        # 真实机具 T1 的身份未被污染
        self.assertEqual(capture.canonical('EN-001'), 'T1')
        board = board_with(fx['shared'], fx['fraud_identity'])
        result = board.adjudicate()
        fraud_sale = {'sale_id': 'SX', 'machine_id': 'T1-FAKE', 'seller': 'CoopA',
                      'buyer': 'CoopZ', 'signed_at': '2026-05-12T09:00',
                      'signature_valid': True, 'token': 'APP-X'}
        board.ingest_sales([fraud_sale])
        verdict = board.adjudicate()['sales']['SX']
        self.assertEqual(verdict['status'], 'rejected')
        self.assertIn('隔离', verdict['reason'])

    def test_anchor_requires_engine_and_doc(self):
        with self.assertRaises(IdentityConflict):
            TitleCapture([{'kind': 'title', 'id': 'x', 'plate': 'P9'}])
        # 仅给机名而无发动机号/登记原件同样不能完成共同锚定
        with self.assertRaises(IdentityConflict):
            TitleCapture([{'kind': 'title', 'id': 'y', 'plate': 'P9', 'engine_no': 'EN-9'}])

    def test_ingest_is_atomic_on_bad_batch(self):
        capture = TitleCapture(load_conflict()['shared'])
        before = capture.identity_view()
        good = {'kind': 'title', 'id': 'g1', 'machine_id': 'T8', 'plate': 'P8',
                'engine_no': 'EN-8', 'registry_doc': 'DOC-8', 'owner': 'CoopA'}
        bad = {'kind': 'title', 'id': 'b1', 'plate': 'P9'}  # 锚点不足且无机具 id
        with self.assertRaises(IdentityConflict):
            capture.ingest([good, bad])
        self.assertEqual(capture.identity_view(), before)  # 整批回滚


class DispositionRightTest(unittest.TestCase):
    def test_mortgage_and_freeze_block_contract(self):
        fx = load_conflict()
        result = board_with(fx['shared']).adjudicate()
        self.assertEqual(result['sales']['S3']['status'], 'blocked')   # 融资抵押
        self.assertEqual(result['sales']['S4']['status'], 'blocked')   # 司法冻结
        self.assertIn('MG-7', result['sales']['S3']['encumbrances'])

    def test_released_encumbrance_allows_contract(self):
        fx = load_conflict()
        release = {'kind': 'encumbrance_release', 'id': 'ER-1', 'seq': 50,
                   'machine_id': 'T2', 'encumbrance_id': 'MG-7'}
        result = board_with(fx['shared'], [release]).adjudicate()
        self.assertEqual(result['sales']['S3']['status'], 'contracted')

    def test_expired_period_and_signature_reject(self):
        fx = load_conflict()
        result = board_with(fx['shared']).adjudicate()
        self.assertEqual(result['sales']['S5']['status'], 'rejected')
        self.assertIn('有效期间', result['sales']['S5']['reason'])
        bad_sale = [dict(s, signature_valid=False) if s.get('sale_id') == 'S2' else s
                    for s in sales_from(fx['shared'])]
        board = TransferBoard(fx['shared'], bad_sale)
        self.assertEqual(board.adjudicate()['sales']['S2']['status'], 'rejected')

    def test_seller_without_title_cannot_dispose(self):
        fx = load_conflict()
        board = board_with(fx['shared'])
        board.ingest_sales([{'sale_id': 'S9', 'machine_id': 'T1', 'seller': 'CoopOutsider',
                             'buyer': 'CoopZ', 'signed_at': '2026-05-12T09:00',
                             'signature_valid': True, 'token': 'APP-S9'}])
        verdict = board.adjudicate()['sales']['S9']
        self.assertEqual(verdict['status'], 'rejected')
        self.assertIn('无当前处分权', verdict['reason'])


class DoubleContractBreakpointTest(unittest.TestCase):
    def setUp(self):
        self.fx = load_conflict()
        self.early_notice = next(e for e in self.fx['shared'] if e.get('kind') == 'pickup_notice')
        self.key_request = next(e for e in self.fx['shared'] if e.get('kind') == 'key_request')

    def test_receipt_is_not_completion_and_breakpoint_blocks_assertion(self):
        board = board_with(self.fx['shared'], notices=[self.early_notice])
        result = board.adjudicate()
        # 产权仍归 CoopA：受理回执没有顶名覆盖
        self.assertEqual(result['owners']['T1'], 'CoopA')
        self.assertEqual(result['sales']['S1']['status'], 'pending_registry')
        self.assertEqual(result['sales']['S1']['phase'], 'accepted')
        self.assertIn('受理回执不等于过户完成', result['sales']['S1']['reason'])
        self.assertEqual(result['sales']['S2']['status'], 'pending_registry')
        self.assertIn('APP-S1', result['breakpoints'])
        self.assertIn('APP-S2', result['breakpoints'])
        # 仓库的 CoopC 提机请求在断点上只能 hold
        decision = board.request_key(self.key_request)
        self.assertEqual(decision['decision'], 'hold')
        self.assertEqual(decision['machine'], 'T1')
        # 提前登记的通知原样留痕，未被静默改写
        self.assertTrue(any(n.get('notice_id') == 'notice:early:S1' for n in result['notices']))

    def test_accepted_tracks_title_and_disposition_not_string_only(self):
        fx = self.fx
        base = accepted(fx['shared'], sales_from(fx['shared']))
        self.assertTrue(base['S1'])   # 卖方仍有处分权、合同有效、登记在途
        # 抵押合同即使字符串卖方相同也不成立
        self.assertFalse(base['S3'])
        # 终局过户给 CoopB 后，CoopA 再签的合同必须随产权变化判 False
        board = board_with(fx['shared'], fx['outcome_a'])
        after = board.adjudicate()
        self.assertEqual(after['owners']['T1'], 'CoopB')
        board.ingest_sales([{'sale_id': 'S6', 'machine_id': 'T1', 'seller': 'CoopA',
                             'buyer': 'CoopG', 'signed_at': '2026-05-12T09:00',
                             'signature_valid': True, 'token': 'APP-S6'}])
        verdict = board.adjudicate()['sales']['S6']
        self.assertEqual(verdict['status'], 'rejected')


class RegistryFinalityTest(unittest.TestCase):
    def setUp(self):
        self.fx = load_conflict()
        self.early_notice = next(e for e in self.fx['shared'] if e.get('kind') == 'pickup_notice')
        self.key_request = next(e for e in self.fx['shared'] if e.get('kind') == 'key_request')

    def test_outcome_a_s1_completed_coopb_owns_coopc_cannot_pickup(self):
        board = board_with(self.fx['shared'], self.fx['outcome_a'], [self.early_notice])
        result = board.adjudicate()
        self.assertEqual(result['owners']['T1'], 'CoopB')
        self.assertEqual(result['sales']['S1']['status'], 'owned')
        self.assertEqual(result['sales']['S2']['status'], 'lost')
        self.assertIn('同机唯一', result['sales']['S2']['reason'])
        self.assertEqual(result['machine_states']['T1']['winner_token'], 'APP-S1')
        # CoopC 的钥匙请求必须拒绝/搁置，不得放机
        decision = board.request_key(self.key_request)
        self.assertIn(decision['decision'], ('hold', 'deny'))
        # CoopB 有正式提机通知；早期通知仍在日志中可回读
        ids = [n.get('notice_id') for n in result['notices']]
        self.assertIn('notice:S1:APP-S1', ids)
        self.assertIn('notice:early:S1', ids)

    def test_outcome_b_s2_wins_coopc_owns_and_coopb_notice_is_revoked_by_name(self):
        board = board_with(self.fx['shared'], self.fx['outcome_b'], [self.early_notice])
        result = board.adjudicate()
        self.assertEqual(result['owners']['T1'], 'CoopC')
        self.assertEqual(result['sales']['S2']['status'], 'owned')
        self.assertEqual(result['sales']['S1']['status'], 'lost')
        self.assertEqual(result['machine_states']['T1']['winner_token'], 'APP-S2')
        # CoopC 提机请求获准
        decision = board.request_key(self.key_request)
        self.assertEqual(decision['decision'], 'release')
        notices = result['notices']
        early = next(n for n in notices if n.get('notice_id') == 'notice:early:S1')
        self.assertEqual(early['status'], 'revoked')          # 旧通知未被静默覆盖
        correction = next(n for n in notices if n.get('kind') == 'correction')
        self.assertEqual(correction['revokes'], 'notice:early:S1')
        self.assertEqual(correction['named_buyer'], 'CoopB')  # 指名纠正
        self.assertIn('S1', correction['reopen_contracts'])   # 受影响合同重裁
        coopc = next(n for n in notices if n.get('notice_id') == 'notice:S2:APP-S2')
        self.assertEqual(coopc['status'], 'issued')

    def test_late_completion_after_rival_finity_is_rejected_not_double_transferred(self):
        board = board_with(self.fx['shared'],
                           self.fx['outcome_b'] + self.fx['late_s1_completion'])
        result = board.adjudicate()
        self.assertEqual(result['owners']['T1'], 'CoopC')      # 主人不变
        record = board.registry.applications['APP-S1']
        self.assertEqual(record['phase'], 'rejected')
        self.assertIn('同机唯一', record['rejection'])

    def test_late_registry_revocation_rolls_back_and_reopens_contract(self):
        board = board_with(self.fx['shared'],
                           self.fx['outcome_b'] + self.fx['late_revocation'])
        result = board.adjudicate()
        self.assertEqual(result['owners']['T1'], 'CoopA')
        self.assertEqual(result['sales']['S2']['status'], 'reopened')
        self.assertEqual(result['revocations'][0]['token'], 'APP-S2')
        self.assertIn('S2', result['revocations'][0]['affected_contracts'])
        # 已发 CoopC 通知被指名撤销
        notice = next(n for n in result['notices'] if n.get('notice_id') == 'notice:S2:APP-S2')
        self.assertEqual(notice['status'], 'revoked')
        correction = next(n for n in result['notices'] if n.get('kind') == 'correction')
        self.assertEqual(correction['named_buyer'], 'CoopC')


class BreakpointResumeTest(unittest.TestCase):
    def test_resume_with_token_proof_reconciles_both_ways(self):
        fx = load_conflict()
        # 路径 A：本地只有申请与受理，靠带令牌终局恢复出 CoopB
        board_a = board_with(fx['shared'])
        board_a.registry.capture.resume_with_proof(
            'APP-S1', 'completed', machine_id='T1', sale_id='S1', source_event='RES-A1')
        board_a.ingest_titles([
            {'kind': 'registry_proof', 'id': 'RES-A1B', 'token': 'APP-S1',
             'phase': 'completed', 'machine_id': 'T1', 'sale_id': 'S1', 'buyer': 'CoopB'},
            {'kind': 'registry_proof', 'id': 'RES-A2', 'token': 'APP-S2',
             'phase': 'rejected', 'machine_id': 'T1', 'sale_id': 'S2'}])
        result_a = board_a.adjudicate()
        self.assertEqual(result_a['owners']['T1'], 'CoopB')
        self.assertEqual(result_a['sales']['S1']['status'], 'owned')
        self.assertEqual(result_a['breakpoints'], {})
        # 路径 B：同一份断点材料，终局反过来则归 CoopC
        board_b = board_with(fx['shared'])
        board_b.ingest_titles(fx['outcome_b'])
        result_b = board_b.adjudicate()
        self.assertEqual(result_b['owners']['T1'], 'CoopC')
        self.assertEqual(result_b['breakpoints'], {})


class NoticeReplayTest(unittest.TestCase):
    def test_replay_is_deterministic_and_notices_remain_readable(self):
        fx = load_conflict()
        early = next(e for e in fx['shared'] if e.get('kind') == 'pickup_notice')
        first = board_with(fx['shared'], fx['outcome_b'], [early]).adjudicate()
        second = board_with(fx['shared'], fx['outcome_b'], [early]).adjudicate()
        self.assertEqual(first, second)  # 事件溯源重放确定
        kinds = [(n['kind'], n.get('status')) for n in first['notices']]
        self.assertIn(('pickup_notice', 'revoked'), kinds)
        self.assertIn(('correction', 'issued'), kinds)
        # 通知只追加：数量与顺序稳定，旧记录仍可按 id 回读
        self.assertEqual(len(first['notices']), len(second['notices']))
        self.assertTrue(any(n.get('notice_id') == 'notice:early:S1' for n in first['notices']))


class LegacyOwnersTest(unittest.TestCase):
    def test_owners_never_overwritten_by_receipt_or_last_row(self):
        fx = load_conflict()
        registry = TitleRegistry(fx['shared'])
        self.assertEqual(registry.current_owner('T1'), 'CoopA')
        self.assertEqual(registry.current_owner('P-001X'), 'CoopA')  # 换牌别名同主
        # 即便再补一条把 owner 写成 CoopB 的“登记原件”，也不得顶名
        registry.ingest([{'kind': 'title', 'id': 'TT-LATE', 'seq': 60,
                          'machine_id': 'T1', 'plate': 'P-001X', 'engine_no': 'EN-001',
                          'registry_doc': 'DOC-001', 'owner': 'CoopB'}])
        self.assertEqual(registry.current_owner('T1'), 'CoopA')


if __name__ == '__main__':
    unittest.main()
