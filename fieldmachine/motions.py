"""章程议案表决：截止日股权、密封票计票、代理链与议案版本隔离。

主结果只有一个：章程修订是否生效（pass / reject / pending）。本模块不触碰
农机产权过户、报价、工时或调度，仅对议案票权作可复核裁定。

四条硬约束在此闭环：

1. 股权截止日——投票权取自 members.shares(events, cutoff)，迟到转让与草稿
   无法回溯改写截止日名册；名册改判会联动翻转或冻结计票结果。
2. 不可双投——同一委托人（principal）的同一权重，本人票与受托票竞争时
   默认全部隔离待核，绝不按收件先后选边；代理链必须逐边核验范围、时窗
   与撤销回执。
3. 隐私公开——公开报告只含议案总权重、有效/无效/待核票数与结果；个人
   票意、会员标识与盲标签仅进入审计包，审计人可逐封验证只计一次。
4. 版本隔离——旧版票不迁移到修订版，新版结果另存；已公告决议不可覆写。
"""
import hashlib
import json

from .members import shares as cutoff_shares
from .ballot_capture import capture_box, public_box_report, _envelope_id


class ImmutableResolutionError(RuntimeError):
    """同一议案版本重复公告或试图覆写已封存决议。"""


def _default_verify(document):
    """默认签名/封存核验：无异议标记即采信，争议由证据包显式触发。"""
    flag = document.get('authenticity')
    if flag is None:
        return True
    return flag is True or flag in ('verified', 'authentic', 'valid')


def _canonical_hash(payload):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------- #
# 代理图：针对议案版本的授权范围、时窗与撤销回执
# --------------------------------------------------------------------------- #
def grant_status(grant, revocation, motion_id, motion_version, at_time=None):
    """单边授权核验，返回 None（有效）或中性失效原因。"""
    scope_id = grant.get('scope_motion_id', grant.get('motion_id'))
    if scope_id is not None and scope_id != motion_id:
        return 'scope_mismatch'
    scope_version = grant.get('scope_version', grant.get('motion_version'))
    if scope_version is not None and scope_version != motion_version:
        return 'scope_mismatch'
    if at_time is not None:
        if grant.get('valid_from') and at_time < grant['valid_from']:
            return 'outside_window'
        if grant.get('valid_until') and at_time > grant['valid_until']:
            return 'outside_window'
    if grant.get('revoked') or (revocation and at_time is not None
                                and revocation.get('revoked_at')
                                and revocation['revoked_at'] <= at_time):
        return 'proxy_revoked'
    if revocation and at_time is None and grant.get('revoked'):
        return 'proxy_revoked'
    return None


def resolve_proxy(principal, voter, motion_id, motion_version,
                  grants, revocations, at_time=None):
    """遍历代理图，寻找 principal -> ... -> voter 的全边有效路径。"""
    if principal == voter:
        return {'ok': True, 'reason': 'self', 'path': []}
    rev_index = {r['grant_id']: r for r in (revocations or [])}
    adjacency = {}
    for grant in grants or []:
        adjacency.setdefault(grant['principal'], []).append(grant)

    stack = [(principal, [])]
    visited = set()
    while stack:
        node, path = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        for grant in adjacency.get(node, ()):
            revocation = rev_index.get(grant['grant_id'])
            if grant_status(grant, revocation, motion_id,
                            motion_version, at_time) is not None:
                continue
            nxt = grant['proxy']
            chain = path + [grant['grant_id']]
            if nxt == voter:
                return {'ok': True, 'reason': 'proxy', 'path': chain}
            stack.append((nxt, chain))
    # 给出最具体的直连失效原因，便于审计追踪撤销。
    for grant in adjacency.get(principal, ()):
        if grant['proxy'] == voter:
            revocation = rev_index.get(grant['grant_id'])
            reason = grant_status(grant, revocation, motion_id,
                                  motion_version, at_time)
            return {'ok': False, 'reason': reason or 'no_valid_grant', 'path': []}
    return {'ok': False, 'reason': 'no_valid_grant', 'path': []}


# --------------------------------------------------------------------------- #
# 主裁定
# --------------------------------------------------------------------------- #
def _principal_of(envelope):
    return envelope.get('principal_id') or envelope.get('member_id')


def _evidence_verdict(group, evidence, version, verify):
    """在不泄露票意的前提下，用封存证据决定竞争权重归谁。

    返回 'self' / 'proxy' / None（证据不足或相互冲突 → 待核）。
    """
    principal = group['principal']
    self_kinds = ('self_witness', 'proxy_revocation_receipt')
    proxy_kinds = ('void_self_declaration',)
    self_hit = proxy_hit = False
    for doc in evidence or []:
        if doc.get('kind') not in self_kinds + proxy_kinds:
            continue
        if doc.get('principal') != principal:
            continue
        if doc.get('motion_version') is not None \
                and doc.get('motion_version') != version:
            continue
        if not verify(doc):
            continue
        if doc['kind'] in self_kinds:
            self_hit = True
        else:
            proxy_hit = True
    if self_hit and proxy_hit:
        return None  # 两路封存证据相互冲突，不得猜票
    if self_hit:
        return 'self'
    if proxy_hit:
        return 'proxy'
    return None


def adjudicate(motion, member_events, ballots, grants=None,
               revocations=None, evidence=None, cutoff=None,
               box_id=None, verify_signature=None):
    """对单个议案版本执行完整裁定，返回公开报告 + 审计包。"""
    verify = verify_signature or _default_verify
    version = motion['version']
    motion_id = motion['motion_id']
    cutoff = cutoff or motion.get('notice_cutoff')
    box_id = box_id or motion.get('box_id') or f'box-{motion_id}-v{version}'

    # 约束 1：截止日股权快照（迟到转让/草稿被名册层剔除）。
    roster = cutoff_shares(member_events, cutoff)
    eligible_weight = sum(roster.values())

    # 密封票箱去重（身份盲）。无封签编号的旧格式票补稳定编号。
    envelopes = []
    for i, envelope in enumerate(ballots):
        row = dict(envelope)
        row.setdefault('ballot_id', f'{box_id}-legacy-{i}')
        row.setdefault('seal_id', f'seal-{box_id}-{i}')
        envelopes.append(row)
    box = capture_box(box_id, envelopes)
    accepted = box.accepted()
    envelope_by_id = {_envelope_id(e): e for e in envelopes}

    tally_rows = []          # 审计包明细（含票意，不进公开报告）
    invalid_reasons = {}     # 公开侧仅保留中性原因计数
    consumed_principals = set()
    yes_weight = no_weight = pending_weight = invalid_weight = 0

    def mark_invalid(principal, weight, reason, token=None, extra=None):
        nonlocal invalid_weight
        invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
        if principal not in consumed_principals:
            invalid_weight += weight
            consumed_principals.add(principal)
        tally_rows.append({'token': token, 'principal': principal,
                           'status': 'invalid', 'reason': reason,
                           'weight': 0 if principal in consumed_principals else weight,
                           **(extra or {})})

    # 票箱层拒收：去重/坏封签只作中性登记，不预占权重；权重在分组阶段
    # 按委托人只裁定一次，避免同一权重被重复计入无效桶。
    for row in box.rejected():
        envelope = envelope_by_id.get(row['ballot_id'], {})
        principal = _principal_of(envelope)
        mark_invalid(principal, 0, row['reason'], row['token'],
                     {'voter': envelope.get('member_id')})

    # 按委托人归集当前版本信封，绝不使用收件先后作为裁决依据。
    groups = {}
    for row in accepted:
        envelope = envelope_by_id[row['ballot_id']]
        principal = _principal_of(envelope)
        groups.setdefault(principal, []).append((row, envelope))

    for principal, items in sorted(groups.items()):
        weight = roster.get(principal, 0)
        current = [(r, e) for r, e in items if e.get('motion_version', version) == version]
        stale = [(r, e) for r, e in items if e.get('motion_version', version) != version]

        # 约束 4：旧版票不迁移，单独计无效，绝不占用当前版本权重。
        for r, e in stale:
            mark_invalid(principal, 0, 'old_version', r['token'],
                         {'voter': e.get('member_id'),
                          'ballot_version': e.get('motion_version')})
        if not current:
            continue
        if weight <= 0:
            for r, e in current:
                mark_invalid(principal, weight, 'ineligible', r['token'],
                             {'voter': e.get('member_id')})
            continue

        authorized = []
        for r, e in current:
            voter = e.get('member_id')
            at_time = e.get('cast_at') or e.get('received_at')
            decision = resolve_proxy(principal, voter, motion_id, version,
                                     grants or [], revocations or [], at_time)
            if not decision['ok']:
                mark_invalid(principal, 0, decision['reason'], r['token'],
                             {'voter': voter})
                continue
            if not verify(e):
                mark_invalid(principal, 0, 'bad_signature', r['token'],
                             {'voter': voter})
                continue
            if e.get('void_declared'):
                mark_invalid(principal, 0, 'void', r['token'],
                             {'voter': voter})
                continue
            if e.get('vote') not in ('yes', 'no'):
                mark_invalid(principal, 0, 'bad_vote', r['token'],
                             {'voter': voter})
                continue
            authorized.append((r, e, decision))

        # authorized 全部来自已接受信封；按声称人/票意判断是否存在竞争。
        sides = {e['vote'] for _, e, _ in authorized}
        claimants = {(e.get('member_id'), d['reason'])
                     for _, e, d in authorized}

        if len(claimants) <= 1 and len(sides) <= 1 and authorized:
            r, e, d = sorted(authorized, key=lambda x: x[0]['ballot_id'])[0]
            # 单一声称人：权重只计一次，其余重复封签已在票箱层拦截。
            if e['vote'] == 'yes':
                yes_weight += weight
            else:
                no_weight += weight
            consumed_principals.add(principal)
            tally_rows.append({'token': r['token'], 'principal': principal,
                               'voter': e.get('member_id'), 'vote': e['vote'],
                               'status': 'valid', 'weight': weight,
                               'authorization': d['reason'],
                               'proxy_path': d['path']})
            continue

        # 约束 2：本人票与受托票竞争（或票意冲突）→ 隔离待核。
        verdict = _evidence_verdict(
            {'principal': principal}, evidence, version, verify)
        chosen = None
        if verdict == 'self':
            chosen = next((x for x in authorized if x[2]['reason'] == 'self'), None)
        elif verdict == 'proxy':
            chosen = next((x for x in authorized if x[2]['reason'] == 'proxy'), None)
        if chosen is not None:
            r, e, d = chosen
            if e['vote'] == 'yes':
                yes_weight += weight
            else:
                no_weight += weight
            consumed_principals.add(principal)
            for r2, e2, d2 in authorized:
                # 仅胜出信封承载权重，被替代信封留痕但 weight 为 0，
                # 保证审计加总时同一委托人权重绝不被重复累计。
                tally_rows.append(
                    {'token': r2['token'], 'principal': principal,
                     'voter': e2.get('member_id'),
                     'vote': e2['vote'],
                     'weight': weight if r2 is r else 0,
                     'status': 'valid' if r2 is r else 'invalid',
                     'reason': None if r2 is r else f'superseded_by_{verdict}',
                     'authorization': d2['reason'], 'proxy_path': d2['path']})
            continue

        # 证据不足或相互冲突：权重从赞成/反对双方隔离，公告待核。
        pending_weight += weight
        consumed_principals.add(principal)
        for i, (r, e, d) in enumerate(authorized):
            # 逐封留痕但只在第一行承载权重，待核桶隔离该委托人权重一次。
            tally_rows.append({'token': r['token'], 'principal': principal,
                               'voter': e.get('member_id'), 'vote': e['vote'],
                               'weight': weight if i == 0 else 0,
                               'status': 'pending',
                               'reason': 'competing_claims',
                               'authorization': d['reason'],
                               'proxy_path': d['path']})

    unused_weight = sum(w for m, w in roster.items()
                        if m not in consumed_principals)
    valid_weight = yes_weight + no_weight
    participated = valid_weight + invalid_weight + pending_weight
    # 默认无最低出席门槛以保持简单多数兼容；正式议案在 fixture 中显式设定。
    quorum_ratio = motion.get('quorum_ratio', 0.0)
    quorum_ok = eligible_weight == 0 or participated >= quorum_ratio * eligible_weight

    if pending_weight > 0 or not quorum_ok:
        result = 'pending'
        result_reason = 'quorum_failed' if not quorum_ok and pending_weight == 0 \
            else 'undetermined_weight_quarantined'
        decided = False
    else:
        result = 'pass' if yes_weight > no_weight else 'reject'
        result_reason = 'majority' if result == 'pass' else 'majority_lost'
        decided = True

    snapshot = {
        'motion_id': motion_id,
        'version': version,
        'notice_cutoff': cutoff,
        'eligible_weight': eligible_weight,
        'yes_weight': yes_weight,
        'no_weight': no_weight,
        'pending_weight': pending_weight,
        'invalid_weight': invalid_weight,
        'unused_weight': unused_weight,
        'result': result,
        'decided': decided,
    }
    snapshot['snapshot_hash'] = _canonical_hash(snapshot)

    # 公开报告：无会员标识、无票意明细、无盲标签。
    public_report = {
        'motion_id': motion_id,
        'version': version,
        'notice_cutoff': cutoff,
        'eligible_weight': eligible_weight,
        'ballots': {
            'valid': {'count': sum(1 for x in tally_rows if x['status'] == 'valid'),
                      'weight': valid_weight},
            'invalid': {'count': sum(1 for x in tally_rows if x['status'] == 'invalid'),
                        'weight': invalid_weight},
            'pending': {'count': sum(1 for x in tally_rows if x['status'] == 'pending'),
                        'weight': pending_weight},
            'unused': {'count': 0, 'weight': unused_weight},
        },
        'invalid_reasons': dict(sorted(invalid_reasons.items())),
        'box': public_box_report(box),
        'result': result,
        'decided': decided,
        'result_reason': result_reason,
        'snapshot_hash': snapshot['snapshot_hash'],
    }

    # 审计包：受信审计人可见，用于逐封复核一次性计数与权重链路。
    audit_bundle = {
        'snapshot': snapshot,
        'seal_audit': box.verify_once(),
        'ledger': box.ledger,
        'roster_cards': roster,
        'tally': tally_rows,
    }
    return {'report': public_report, 'audit': audit_bundle,
            'snapshot': snapshot}


def adjudicate_payload(payload, evidence=None, **kwargs):
    """从单个 fixture 字典（含 grants/revocations/evidence）执行裁定。"""
    motion = payload['motion']
    if evidence is None:
        evidence = payload.get('evidence', [])
    return adjudicate(
        motion, payload['members'], payload['ballots'],
        grants=payload.get('grants', []),
        revocations=payload.get('revocations', []),
        evidence=evidence, **kwargs)


# --------------------------------------------------------------------------- #
# 版本化、不可覆写的决议公告册
# --------------------------------------------------------------------------- #
class ResolutionBook:
    """每个议案版本只存一份快照；重复公告直接拒绝，历史永不覆写。"""

    def __init__(self):
        self._entries = {}

    def announce(self, outcome):
        version = outcome['snapshot']['version']
        motion_id = outcome['snapshot']['motion_id']
        if version in self._entries:
            raise ImmutableResolutionError(
                f'motion {motion_id} version {version} 已公告，禁止覆写')
        sealed = dict(outcome['snapshot'])
        sealed['announced_hash'] = _canonical_hash(sealed)
        self._entries[version] = sealed
        return sealed

    def snapshot_for(self, version):
        return self._entries.get(version)

    def versions(self):
        return sorted(self._entries)

    def is_immutable(self, version):
        return version in self._entries


# --------------------------------------------------------------------------- #
# 兼容旧接口：无代理/无版本信息时即普通多数表决
# --------------------------------------------------------------------------- #
def passed(member_events, ballots, cutoff=None):
    """旧接口保留：返回 True 仅当章程在截止日名册下明确通过。

    待核（pending）与否决（reject）均返回 False；完整三态与证据链请使用
    adjudicate。名册改判导致赞成多数消失时，此处同步翻转。
    """
    motion = {'motion_id': 'legacy-motion', 'version': 1,
              'notice_cutoff': cutoff}
    # 旧格式票以 member_id 同时充当委托人，保证截止日名册改判能联动票权。
    legacy_ballots = [dict(b, motion_version=1,
                           principal_id=b.get('principal_id', b['member_id']))
                      for b in ballots]
    outcome = adjudicate(motion, member_events, legacy_ballots, cutoff=cutoff)
    return outcome['snapshot']['result'] == 'pass'
