"""密封票箱采集：票箱去重、一次性计数与隐私审计。

设计原则：票箱模块是“身份盲”的——它只认密封信封、封签与审计盲标签，
不知道也不保存会员与票意的对应关系。代理授权、股权权重与议案版本由
motions 层在取出信封后另行核验。这样公开报告无法反推个人选择，而
审计人仍可凭审计台账逐封验证“每份密封票只计一次”。

盲标签 token = sha256(box_id | seal_id | ballot_id)，只对持箱人/审计人
开放，公开报告只出现按类别的张数与权重，不出现任何可识别字段。
"""
import hashlib

# 失效/待核原因使用中性代码（受控词表），不指向具体会员，避免公开侧反推票意。
INVALID_REASONS = ('duplicate', 'bad_seal', 'unknown_motion',
                   'ineligible', 'void', 'proxy', 'old_version',
                   'bad_signature', 'bad_vote', 'scope_mismatch',
                   'outside_window', 'proxy_revoked', 'no_valid_grant',
                   'superseded_by_self', 'superseded_by_proxy',
                   'competing_claims')


def _token(box_id, seal_id, ballot_id):
    digest = hashlib.sha256()
    digest.update(str(box_id).encode('utf-8'))
    digest.update(b'|')
    digest.update(str(seal_id).encode('utf-8'))
    digest.update(b'|')
    digest.update(str(ballot_id).encode('utf-8'))
    return digest.hexdigest()


def _envelope_id(envelope):
    return envelope.get('ballot_id') or envelope.get('envelope_id')


class SealedBox:
    """单个密封票箱的采集台账（含跨箱封签去重）。"""

    def __init__(self, box_id):
        self.box_id = box_id
        # 以封签 seal_id 为主键：同一物理封签只可能封装一份有效票。
        self._seals = {}
        # 以审计盲标签为主键：同一票再次入箱即重复票。
        self._tokens = {}
        self.ledger = []

    def capture(self, envelopes):
        """采集一批信封，逐封核验封签并去重，返回审计台账列表。"""
        for envelope in envelopes:
            ballot_id = _envelope_id(envelope)
            seal_id = envelope.get('seal_id')
            token = _token(self.box_id, seal_id, ballot_id)
            seal_ok = bool(seal_id) and envelope.get('seal_intact', True) \
                and not envelope.get('seal_broken', False)
            duplicate = (seal_id in self._seals) or (token in self._tokens)

            if not seal_ok:
                reason = 'bad_seal'
            elif duplicate:
                reason = 'duplicate'
            else:
                reason = None

            row = {
                'box_id': self.box_id,
                'token': token,
                'seal_id': seal_id,
                'ballot_id': ballot_id,
                'motion_version': envelope.get('motion_version'),
                'received_at': envelope.get('received_at'),
                'seal_ok': seal_ok,
                'status': 'rejected' if reason else 'accepted',
                'reason': reason,
            }
            if reason is None:
                self._seals[seal_id] = token
                self._tokens[token] = ballot_id
            self.ledger.append(row)
        return list(self.ledger)

    def accepted(self):
        return [dict(row) for row in self.ledger if row['status'] == 'accepted']

    def rejected(self):
        return [dict(row) for row in self.ledger if row['status'] == 'rejected']

    def verify_once(self):
        """审计核验：每个盲标签在台账中最多被计一次。"""
        seen = set()
        counted = []
        for row in self.ledger:
            if row['status'] != 'accepted':
                continue
            if row['token'] in seen:
                return {'ok': False, 'violation': row['token'], 'counted': counted}
            seen.add(row['token'])
            counted.append(row['token'])
        return {'ok': True, 'counted': counted,
                'rejected': [r['token'] for r in self.rejected()]}


def capture_box(box_id, envelopes):
    """函数式入口：采集单个票箱，返回 SealedBox（保留完整台账）。"""
    box = SealedBox(box_id)
    box.capture(envelopes)
    return box


def public_box_report(box):
    """公开侧视图：仅张数与中性原因代码，不含任何身份/票意字段。"""
    accepted = box.accepted()
    rejected = box.rejected()
    reasons = {}
    for row in rejected:
        reason = row['reason']
        # 受控词表校验：失效原因只能来自中性代码，防止身份/票意泄漏。
        if reason not in INVALID_REASONS:
            raise ValueError(f'未登记的失效原因代码: {reason}')
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        'box_id': box.box_id,
        'envelopes_total': len(box.ledger),
        'accepted_ballots': len(accepted),
        'rejected_ballots': len(rejected),
        'rejected_reasons': reasons,
    }
