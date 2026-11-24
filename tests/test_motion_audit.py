"""章程表决审计：截止日股权、双投隔离、代理撤销、版本隔离、两路封存证据
与隐私报告。运行：python -B -m unittest discover -s tests -v
"""
import copy
import json
import unittest
from pathlib import Path

from fieldmachine.members import shares, roster
from fieldmachine.motions import (
    adjudicate, adjudicate_payload, ResolutionBook,
    ImmutableResolutionError, resolve_proxy, passed,
)
from fieldmachine.ballot_capture import capture_box

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'
MOTION_ID = 'MOTION-SHARED-MACHINERY'
CUTOFF = '2026-11-10T18:00'


def load_proxy():
    return json.loads((FIX / 'motion-proxy.json').read_text(encoding='utf-8'))


def judge(payload, evidence=None):
    data = copy.deepcopy(payload)
    if evidence is not None:
        data['evidence'] = evidence
    return adjudicate_payload(data)


def self_witness_evidence():
    # A 对最终议案（v2）亲笔票的封存见证 + 代理撤销回执。
    return [
        {'kind': 'self_witness', 'principal': 'A', 'motion_version': 2,
         'authenticity': 'verified', 'sealed_by': 'supervisor',
         'note': 'A 对 v2 亲笔赞成票的封存见证'},
        {'kind': 'proxy_revocation_receipt', 'principal': 'A',
         'motion_version': 2, 'authenticity': 'verified',
         'grant_id': 'G-A-P-V2', 'revoked_at': '2026-11-20T09:10',
         'note': '代理撤销回执，撤销可追'},
    ]


def proxy_evidence():
    # A 在封箱前有效的代理授权 + 亲笔票作废声明。
    return [
        {'kind': 'valid_proxy_grant', 'principal': 'A', 'motion_version': 2,
         'authenticity': 'verified', 'grant_id': 'G-A-P-V2',
         'note': '封箱前有效的 v2 代理授权'},
        {'kind': 'void_self_declaration', 'principal': 'A',
         'motion_version': 2, 'authenticity': 'verified',
         'sealed_by': 'notary',
         'note': 'A 亲笔票作废声明'},
    ]


class CutoffShareTest(unittest.TestCase):
    def test_late_transfer_and_draft_cannot_rewrite_cutoff(self):
        events = [
            {'member_id': 'A', 'kind': 'enrollment', 'shares': 60,
             'effective_on': '2026-01-01T00:00'},
            # 草稿转让不生效。
            {'member_id': 'A', 'kind': 'transfer', 'from_member': 'A',
             'to_member': 'X', 'shares': 60, 'draft': True,
             'effective_on': '2026-11-08T10:00'},
            # 截止日后的迟到转让不得回溯旧截止日。
            {'member_id': 'A', 'kind': 'transfer', 'from_member': 'A',
             'to_member': 'Y', 'shares': 20,
             'effective_on': '2026-11-12T10:00'},
        ]
        self.assertEqual(shares(events, CUTOFF), {'A': 60})
        cards = roster(events, CUTOFF)
        self.assertEqual(cards['A']['status'], 'effective')
        self.assertTrue(any(n.startswith('late:') for n in cards['A']['notes']))
        self.assertTrue(any(n.startswith('draft:') for n in cards['A']['notes']))
        # 无截止日视角下，迟到转让在最终状态才生效。
        self.assertEqual(shares(events)['A'], 40)

    def test_transfer_effective_before_cutoff_changes_weight(self):
        events = [
            {'member_id': 'A', 'kind': 'enrollment', 'shares': 60,
             'effective_on': '2026-01-01T00:00'},
            {'member_id': 'B', 'kind': 'enrollment', 'shares': 40,
             'effective_on': '2026-01-01T00:00'},
            {'member_id': 'A', 'kind': 'transfer_out', 'shares': 60,
             'effective_on': '2026-11-09T09:00'},
        ]
        self.assertNotIn('A', shares(events, CUTOFF))
        self.assertEqual(shares(events, CUTOFF)['B'], 40)


class DoubleVoteAndProxyTest(unittest.TestCase):
    def test_competing_self_and_proxy_quarantined_not_first_in_first_served(self):
        outcome = judge(load_proxy())
        snap = outcome['snapshot']
        # 不得按收件先后选票：A 的 60 股从赞成/反对双方隔离。
        self.assertEqual(snap['result'], 'pending')
        self.assertEqual(snap['yes_weight'], 0)
        self.assertEqual(snap['no_weight'], 40)
        self.assertEqual(snap['pending_weight'], 60)
        pending_rows = [r for r in outcome['audit']['tally']
                        if r['status'] == 'pending']
        self.assertEqual({r['principal'] for r in pending_rows}, {'A'})
        self.assertEqual({r['vote'] for r in pending_rows}, {'yes', 'no'})

    def test_revocation_is_traceable_and_removes_proxy_power(self):
        grants = [
            {'grant_id': 'G1', 'principal': 'A', 'proxy': 'P',
             'scope_motion_id': 'M', 'scope_version': 1,
             'valid_from': '2026-11-01T00:00',
             'valid_until': '2026-12-01T00:00'},
        ]
        revocations = [{'grant_id': 'G1',
                        'revoked_at': '2026-11-15T09:00'}]
        after = resolve_proxy('A', 'P', 'M', 1, grants, revocations,
                              at_time='2026-11-20T09:00')
        self.assertFalse(after['ok'])
        self.assertEqual(after['reason'], 'proxy_revoked')
        before = resolve_proxy('A', 'P', 'M', 1, grants, revocations,
                               at_time='2026-11-10T09:00')
        self.assertTrue(before['ok'])
        # 代理链逐边核验：范围不匹配的授权不能用于本议案版本。
        scoped = resolve_proxy('A', 'P', 'M', 2, grants, [],
                               at_time='2026-11-20T09:00')
        self.assertFalse(scoped['ok'])
        self.assertEqual(scoped['reason'], 'scope_mismatch')

    def test_proxy_chain_traversal_and_cycle_guard(self):
        grants = [
            {'grant_id': 'G1', 'principal': 'A', 'proxy': 'P',
             'scope_motion_id': 'M', 'scope_version': 1},
            {'grant_id': 'G2', 'principal': 'P', 'proxy': 'Q',
             'scope_motion_id': 'M', 'scope_version': 1},
            {'grant_id': 'G3', 'principal': 'Q', 'proxy': 'A',
             'scope_motion_id': 'M', 'scope_version': 1},  # 成环
        ]
        chain = resolve_proxy('A', 'Q', 'M', 1, grants, [],
                              at_time='2026-11-20T09:00')
        self.assertTrue(chain['ok'])
        self.assertEqual(chain['path'], ['G1', 'G2'])
        cyclic = resolve_proxy('Q', 'A', 'M', 1, grants, [],
                               at_time='2026-11-20T09:00')
        self.assertTrue(cyclic['ok'])  # 直连边 G3 有效；visited 防止死循环

    def test_same_weight_never_counted_twice(self):
        outcome = judge(load_proxy())
        snap = outcome['snapshot']
        # 即便存在重复封签信封，A 的权重在任何一方最多出现一次。
        for side in ('yes_weight', 'no_weight', 'pending_weight'):
            self.assertLessEqual(snap[side], 60)
        # 桶级守恒：有效+待核+无效+未参与权重恰为截止日总权重，无重复占用。
        accounted = (snap['yes_weight'] + snap['no_weight']
                     + snap['pending_weight'] + snap['invalid_weight']
                     + snap['unused_weight'])
        self.assertEqual(accounted, snap['eligible_weight'])
        # 待核信封可以逐封留痕，但待核桶只隔离 A 的权重一次。
        self.assertEqual(snap['pending_weight'], 60)
        pending = [r for r in outcome['audit']['tally']
                   if r['status'] == 'pending']
        self.assertEqual({r['principal'] for r in pending}, {'A'})
        # 审计明细逐行权重加总必须等于桶级权重，杜绝逐封重复累计。
        tally = outcome['audit']['tally']
        valid_sum = sum(r['weight'] for r in tally if r['status'] == 'valid')
        pending_sum = sum(r['weight'] for r in tally if r['status'] == 'pending')
        self.assertEqual(valid_sum, snap['yes_weight'] + snap['no_weight'])
        self.assertEqual(pending_sum, snap['pending_weight'])


class VersionIsolationTest(unittest.TestCase):
    def test_old_version_ballot_not_migrated(self):
        outcome = judge(load_proxy())
        reasons = outcome['report']['invalid_reasons']
        self.assertIn('old_version', reasons)
        tally = outcome['audit']['tally']
        legacy = [r for r in tally if r.get('reason') == 'old_version']
        self.assertTrue(legacy)
        # 旧版票权重为 0，绝不并入 v2 的赞成/反对。
        self.assertTrue(all(r['weight'] == 0 for r in legacy))

    def test_independent_version_snapshots_are_sealed_separately(self):
        v1 = judge(load_proxy(), evidence=[])
        v2_payload = copy.deepcopy(load_proxy())
        v2_payload['motion']['version'] = 1
        v1_outcome = adjudicate_payload(v2_payload)
        self.assertNotEqual(v1_outcome['snapshot']['snapshot_hash'],
                            v1['snapshot']['snapshot_hash'])


class SealedEvidencePathsTest(unittest.TestCase):
    def test_path_one_self_witness_makes_charter_pass(self):
        outcome = judge(load_proxy(), self_witness_evidence())
        snap = outcome['snapshot']
        self.assertEqual(snap['result'], 'pass')
        self.assertEqual(snap['yes_weight'], 60)
        self.assertEqual(snap['no_weight'], 40)
        self.assertEqual(snap['pending_weight'], 0)
        valid = [r for r in outcome['audit']['tally'] if r['status'] == 'valid']
        a_row = next(r for r in valid if r['principal'] == 'A')
        self.assertEqual(a_row['vote'], 'yes')
        self.assertEqual(a_row['authorization'], 'self')

    def test_path_two_valid_proxy_makes_charter_reject(self):
        outcome = judge(load_proxy(), proxy_evidence())
        snap = outcome['snapshot']
        self.assertEqual(snap['result'], 'reject')
        self.assertEqual(snap['yes_weight'], 0)
        self.assertEqual(snap['no_weight'], 100)
        self.assertEqual(snap['pending_weight'], 0)
        valid = [r for r in outcome['audit']['tally'] if r['status'] == 'valid']
        a_row = next(r for r in valid if r['principal'] == 'A')
        self.assertEqual(a_row['vote'], 'no')
        self.assertEqual(a_row['authorization'], 'proxy')

    def test_conflicting_evidence_stays_pending_no_guessing(self):
        outcome = judge(load_proxy(),
                        self_witness_evidence() + proxy_evidence())
        self.assertEqual(outcome['snapshot']['result'], 'pending')
        self.assertEqual(outcome['snapshot']['pending_weight'], 60)

    def test_forged_or_wrong_version_evidence_is_ignored(self):
        forged = [
            {'kind': 'self_witness', 'principal': 'A', 'motion_version': 2,
             'authenticity': 'forged'},
            {'kind': 'self_witness', 'principal': 'A', 'motion_version': 1,
             'authenticity': 'verified'},  # 仅见证旧版，不覆盖 v2
        ]
        outcome = judge(load_proxy(), forged)
        self.assertEqual(outcome['snapshot']['result'], 'pending')


class PrivacyReportTest(unittest.TestCase):
    FORBIDDEN_KEYS = {'principal', 'principal_id', 'member_id', 'voter',
                      'vote', 'token', 'seal_id', 'ballot_id', 'proxy_path'}

    def _leaves(self, node, enforce_keys=True):
        if isinstance(node, dict):
            for key, value in node.items():
                if enforce_keys:
                    self.assertNotIn(key, self.FORBIDDEN_KEYS)
                yield from self._leaves(value, enforce_keys)
        elif isinstance(node, (list, tuple)):
            for item in node:
                yield from self._leaves(item, enforce_keys)
        else:
            yield node

    def test_public_report_cannot_reveal_individual_choices(self):
        outcome = judge(load_proxy(), self_witness_evidence())
        public = outcome['report']
        leaf_values = set(map(str, self._leaves(public)))
        for identity in ('A', 'B', 'P', 'X', 'yes', 'no'):
            self.assertNotIn(identity, leaf_values)
        # 公开侧只允许总权重与三类票的张数/权重及结果。
        self.assertEqual(set(public['ballots']),
                         {'valid', 'invalid', 'pending', 'unused'})
        self.assertIn(public['result'], ('pass', 'reject', 'pending'))
        # 票意与身份只在审计包中可见，且审计包不套用公开侧字段禁令。
        audit_leaves = set(map(str, self._leaves(outcome['audit'], False)))
        self.assertIn('yes', audit_leaves)
        self.assertIn('A', audit_leaves)

    def test_auditor_can_verify_each_sealed_ballot_once(self):
        outcome = judge(load_proxy())
        seal_audit = outcome['audit']['seal_audit']
        self.assertTrue(seal_audit['ok'])
        tokens = seal_audit['counted']
        self.assertEqual(len(tokens), len(set(tokens)))
        # 重复封签确实被票箱拒绝并记录原因。
        rejected = [r for r in outcome['audit']['ledger']
                    if r['status'] == 'rejected']
        self.assertTrue(any(r['reason'] == 'duplicate' for r in rejected))

    def test_cross_box_seal_dedup_and_identity_blindness(self):
        from fieldmachine.ballot_capture import public_box_report
        envelopes = [
            {'ballot_id': 'b1', 'seal_id': 's1', 'vote': 'yes'},
            {'ballot_id': 'b1', 'seal_id': 's1', 'vote': 'yes'},
            {'ballot_id': 'b2', 'seal_id': 's2', 'vote': 'no'},
        ]
        box = capture_box('BOX-X', envelopes)
        public = json.dumps(public_box_report(box), ensure_ascii=False)
        self.assertNotIn('yes', public)
        self.assertNotIn('no', public)
        self.assertTrue(box.verify_once()['ok'])
        self.assertEqual(len(box.rejected()), 1)


class RosterLinkageTest(unittest.TestCase):
    def test_reclassifying_a_shares_flips_or_holds_majority(self):
        # A 的 60 股被改判为会前已转出：赞成多数必须联动翻转。
        reclassified = copy.deepcopy(load_proxy())
        reclassified['members'].append(
            {'member_id': 'A', 'kind': 'transfer_out', 'shares': 60,
             'effective_on': '2026-11-09T09:00',
             'note': '名册改判：A 会前已转出 60 股'})
        outcome = judge(reclassified, self_witness_evidence())
        self.assertEqual(outcome['snapshot']['yes_weight'], 0)
        self.assertEqual(outcome['snapshot']['no_weight'], 40)
        self.assertEqual(outcome['snapshot']['result'], 'reject')
        reasons = outcome['report']['invalid_reasons']
        self.assertIn('ineligible', reasons)

    def test_late_reclassification_cannot_alter_deadline(self):
        late = copy.deepcopy(load_proxy())
        late['members'].append(
            {'member_id': 'A', 'kind': 'transfer_out', 'shares': 60,
             'effective_on': '2026-11-21T09:00',
             'note': '会后才提交的转出，迟到不得改写截止日'})
        baseline = judge(load_proxy())
        outcome = judge(late)
        self.assertEqual(outcome['snapshot']['eligible_weight'],
                         baseline['snapshot']['eligible_weight'])
        self.assertEqual(outcome['snapshot']['pending_weight'], 60)


class ResolutionBookTest(unittest.TestCase):
    def test_announced_resolution_is_immutable_and_versioned(self):
        book = ResolutionBook()
        v1 = judge(load_proxy())
        sealed_v1 = book.announce(v1)
        with self.assertRaises(ImmutableResolutionError):
            book.announce(v1)
        payload = copy.deepcopy(load_proxy())
        payload['motion']['version'] = 3
        v3 = book.announce(adjudicate_payload(payload))
        self.assertEqual(book.versions(), [2, 3])
        self.assertTrue(book.is_immutable(2))
        self.assertEqual(book.snapshot_for(2)['announced_hash'],
                         sealed_v1['announced_hash'])
        self.assertNotEqual(book.snapshot_for(3)['snapshot_hash'],
                            sealed_v1['snapshot_hash'])


class LegacyCompatibilityTest(unittest.TestCase):
    def test_motion_ok_seed_still_passes_under_enhanced_rules(self):
        fixture = json.loads((FIX / 'motion-ok.json').read_text(encoding='utf-8'))
        self.assertTrue(passed(fixture['members'], fixture['ballots']))

    def test_passed_flips_when_cutoff_removes_yes_weight(self):
        fixture = json.loads((FIX / 'motion-ok.json').read_text(encoding='utf-8'))
        members = fixture['members'] + [
            {'member_id': 'A', 'kind': 'transfer_out', 'shares': 60,
             'effective_on': '2026-11-09T09:00'}]
        cutoff = '2026-11-10T18:00'
        # A 权重被截止日名册剔除后，赞成多数消失（待核/否决均非通过）。
        self.assertFalse(passed(members, fixture['ballots'], cutoff=cutoff))


if __name__ == '__main__':
    unittest.main()
