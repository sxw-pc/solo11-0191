"""会员名册与股权截止日台账。

议事规则第一硬约束：只有会议通知截止日（cutoff）已生效的会员股份才能
形成该议案的投票权。迟到的股权转让不得改写旧截止日，草稿（draft）文件
不产生股权效力，被改判为会前已转出的权重在名册层即被剔除。

事件（事件溯源，按录入顺序保留同日内次序）：

* enrollment / 入会：member_id + shares
* share_adjust / 股权调整：member_id + shares（覆盖为该时点股数）
* transfer / 股权转让：from_member -> to_member + shares，必须在生效日
  转出方名下仍有足额股份，否则该笔记为 dispute（过度转让，待核）
* transfer_out / 转出退会：member_id + shares（不足额时全额转出并待核）

任一条目带 draft=true 时仅作记录，不改变股权。
"""
from datetime import datetime


def _ts(value):
    """把 ISO8601 字符串解析为可比较的 datetime；None 透传。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _entry(e, seq):
    """规范化一条名册事件，补全稳定序号与生效日。

    无 kind 的简式 {member_id, shares} 记录按入会/股权调整处理，保持
    对早期名册格式的兼容，并仍由同 member_id 的后一条覆盖权重。
    """
    return {
        'seq': e.get('seq', seq),
        'member_id': e['member_id'],
        'kind': e.get('kind', 'share_adjust'),
        'shares': e.get('shares', 0),
        'effective_on': e.get('effective_on') or e.get('effective_at'),
        'from_member': e.get('from_member'),
        'to_member': e.get('to_member'),
        'draft': bool(e.get('draft', False)),
        'note': e.get('note'),
    }


def roster(member_events, cutoff=None):
    """计算截止日时点的名册快照。

    返回 {member_id: {'shares', 'status', 'effective_on', 'notes'}}：

    * shares 只统计 cutoff 当日或之前已生效且非草稿的有效权重；
    * status 为 effective / inactive / disputed；
    * 迟到转让、草稿、过度转让写入 notes，便于审计复核但不计入权重。
    """
    cutoff_dt = _ts(cutoff)
    entries = [_entry(e, i) for i, e in enumerate(member_events)]
    # 同截止日内按显式 seq（缺省为录入次序）稳定排序，保证转让链可重放。
    # 无生效日的记录按“最初在册”处理，排在带日期事件之前，避免一份未注
    # 日期的旧名册在带日期的转让之后被错误地重新覆盖权重。
    dated = sorted((e for e in entries if e['effective_on'] is not None),
                   key=lambda x: (x['effective_on'], x['seq']))
    undated = sorted((e for e in entries if e['effective_on'] is None),
                     key=lambda x: x['seq'])
    entries = undated + dated

    books = {}

    def card(member_id):
        if member_id not in books:
            books[member_id] = {'shares': 0, 'status': 'inactive',
                                'effective_on': None, 'notes': []}
        return books[member_id]

    for e in entries:
        actor = e['member_id']
        if e['kind'] in ('transfer',):
            actor = e['from_member']
        target = card(actor)

        if e['draft']:
            target['notes'].append(f"draft:{e['kind']}:{e['shares']}@(未生效)")
            continue

        effective_on = e['effective_on']
        if cutoff_dt is not None and effective_on is not None \
                and _ts(effective_on) > cutoff_dt:
            # 迟到股权事件不得回溯改写截止日名册。
            target['notes'].append(
                f"late:{e['kind']}:{e['shares']}@{effective_on}>cutoff")
            continue

        if e['kind'] in ('enrollment', 'share_adjust'):
            card(actor)['shares'] = e['shares']
            books[actor]['status'] = 'effective' if e['shares'] > 0 else 'inactive'
            books[actor]['effective_on'] = effective_on
        elif e['kind'] == 'transfer':
            counterparty = card(e['to_member'])
            if target['shares'] < e['shares']:
                target['status'] = 'disputed'
                target['notes'].append(
                    f"over-transfer:{e['shares']}>{target['shares']}@{effective_on}")
                continue
            target['shares'] -= e['shares']
            counterparty['shares'] += e['shares']
            counterparty['status'] = 'effective'
            if counterparty['effective_on'] is None:
                counterparty['effective_on'] = effective_on
        elif e['kind'] == 'transfer_out':
            moved = min(target['shares'], e['shares'])
            if moved < e['shares']:
                target['status'] = 'disputed'
                target['notes'].append(
                    f"over-transfer-out:{e['shares']}>{target['shares']}@{effective_on}")
            target['shares'] -= moved
            if target['shares'] == 0 and target['status'] != 'disputed':
                target['status'] = 'inactive'
        else:
            target['notes'].append(f"unknown-kind:{e['kind']}")

    return books


def shares(member_events, cutoff=None):
    """截止日有效股权权重 {member_id: shares}（待核/失效会员不返回权重）。

    保持对旧调用的兼容：无 cutoff 时返回全部在册会员的最终权重，
    且仍以每个 member_id 的最后一条有效记录覆盖权重。
    """
    books = roster(member_events, cutoff)
    return {member: card['shares']
            for member, card in books.items()
            if card['status'] == 'effective' and card['shares'] > 0}
