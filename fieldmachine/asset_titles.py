"""Per-machine traceable title state.

替代“同机具最后一条记录覆盖所有人”的旧写法。所有权只在登记终局
（transfer_completed 或登记机关带令牌的终局证明）时转移；受理回执
（transfer_receipt）只推进申请阶段；抵押/冻结在解除前阻断处分权；
迟到的撤销登记指名回滚并留下纠正痕迹，绝不静默改写历史。
"""
from .title_capture import TitleCapture, event_order

TERMINAL_PHASES = ('completed', 'rejected')


class TitleRegistry:
    """从离线事件流重建逐台机具的可追产权状态。"""

    def __init__(self, events=None):
        self.capture = TitleCapture()
        self.owner = {}                 # 规范机具 id -> 当前登记所有人
        self.chain = {}                 # 规范机具 id -> [过户完成事件] 可追链条
        self.encumbrances = {}          # 规范机具 id -> {ref: 事件}
        self.applications = {}          # token -> 申请阶段记录
        self.revocations = []           # 迟到撤销/纠正留痕
        self.events = []
        if events:
            self.ingest(events)

    # ---- 摄入 ---------------------------------------------------------------
    def ingest(self, batch):
        snapshot = self._snapshot()
        try:
            self.capture.ingest(batch)
            for event in sorted(batch, key=event_order):
                self._apply(event)
                self.events.append(event)
        except Exception:
            self._restore(snapshot)
            raise
        return self

    def _snapshot(self):
        import copy
        return copy.deepcopy((self.owner, self.chain, self.encumbrances,
                              self.applications, self.revocations, self.events))

    def _restore(self, snapshot):
        (self.owner, self.chain, self.encumbrances, self.applications,
         self.revocations, self.events) = snapshot

    def _machine(self, event):
        ref = event.get('machine_id')
        canonical = self.capture.canonical(ref) if ref is not None else None
        if canonical is None and event.get('token'):
            record = self.capture.tokens.get(event['token'], {})
            stored = record.get('machine_ref')
            if stored:
                canonical = self.capture.canonical(stored)
        return canonical

    def _apply(self, event):
        kind = event.get('kind', 'title')
        machine = self._machine(event)
        if kind == 'title':
            if machine is None:
                return  # 身份冲突已在采集层隔离
            # 登记原件只在机具无主时确立初始登记；已有机具不得被新登记顶名
            self.owner.setdefault(machine, event['owner'])
            self.chain.setdefault(machine, [])
        elif kind in ('mortgage', 'judicial_freeze', 'encumbrance_release'):
            if machine is None:
                return
            bag = self.encumbrances.setdefault(machine, {})
            ref = event.get('encumbrance_id', event.get('id'))
            if kind == 'encumbrance_release':
                bag.pop(event.get('encumbrance_id'), None)
            else:
                bag[ref] = {'type': kind, 'holder': event.get('holder'),
                            'effective_from': event.get('effective_from'),
                            'effective_until': event.get('effective_until'),
                            'source_event': event.get('id')}
        elif kind in ('transfer_app', 'transfer_receipt', 'transfer_completed',
                      'transfer_rejected', 'registry_proof'):
            self._apply_transfer(event, machine)

    def _apply_transfer(self, event, machine):
        token = event.get('token')
        if not token:
            return
        phase = {'transfer_app': 'requested',
                 'transfer_receipt': 'accepted',
                 'transfer_completed': 'completed',
                 'transfer_rejected': 'rejected',
                 'registry_proof': event.get('phase', 'completed')}[event['kind']]
        record = self.applications.get(token, {'phases': []})
        record.update({'token': token, 'machine': machine,
                       'sale_id': event.get('sale_id', record.get('sale_id')),
                       'phase': phase, 'source_event': event.get('id')})
        if phase == 'rejected' and event.get('reason'):
            record['rejection'] = event['reason']
        record['phases'].append({'phase': phase, 'at': event.get('at'),
                                 'event_id': event.get('id')})
        self.applications[token] = record
        if machine is None:
            return
        if phase == 'completed':
            self._complete(event, machine, token, record)
        elif event['kind'] == 'registry_proof' and phase == 'revoked':
            self._revoke(event, machine, token)

    def _complete(self, event, machine, token, record):
        # 同机唯一：一台机具只能有一笔终局过户，迟到的第二笔一律拒绝留痕
        winner = self._winner_token(machine)
        if winner and winner != token:
            record['phase'] = 'rejected'
            record['rejection'] = ('同机唯一：终局登记已由申请 %s 取得，拒绝二次转让' % winner)
            return
        buyer = event.get('buyer') or record.get('buyer')
        if not buyer:
            record['phase'] = 'rejected'
            record['rejection'] = '终局证明缺少买方，无法登记所有人'
            return
        self.owner[machine] = buyer
        self.chain.setdefault(machine, []).append({
            'event_id': event.get('id'), 'token': token,
            'sale_id': record.get('sale_id'), 'owner': buyer, 'at': event.get('at')})
        record['owner_after'] = buyer

    def _revoke(self, event, machine, token):
        # 迟到的登记撤销：指名纠正，产权回滚到上一手，受影响合同交回重裁
        target = self.applications.get(token)
        rolled = None
        chain = self.chain.setdefault(machine, [])
        if target and target.get('sale_id'):
            for idx, link in enumerate(chain):
                if link.get('token') == token:
                    rolled = chain.pop(idx)
                    break
        self.owner[machine] = chain[-1]['owner'] if chain else event.get('restored_owner')
        self.revocations.append({'event_id': event.get('id'), 'token': token,
                                 'machine': machine, 'sale_id': target.get('sale_id') if target else None,
                                 'rolled_back': rolled, 'owner_after': self.owner[machine],
                                 'reason': event.get('reason', '登记机关迟到撤销'),
                                 'affected_contracts': list(event.get('affected_contracts') or
                                                            ([target.get('sale_id')] if target and target.get('sale_id') else []))})
        if target:
            target['phase'] = 'revoked'

    def _winner_token(self, machine):
        for link in self.chain.get(machine, ()):
            return link['token']
        return None

    # ---- 查询 ---------------------------------------------------------------
    def current_owner(self, machine_ref):
        canonical = self.capture.canonical(machine_ref)
        return self.owner.get(canonical) if canonical else None

    def active_encumbrances(self, machine_ref):
        canonical = self.capture.canonical(machine_ref)
        return dict(self.encumbrances.get(canonical, {})) if canonical else {}

    def has_disposition_right(self, machine_ref, party):
        """当前处分权 = 登记所有人且无在效融资抵押/司法冻结。"""
        canonical = self.capture.canonical(machine_ref)
        if not canonical or self.owner.get(canonical) != party:
            return False
        return not self.encumbrances.get(canonical)

    def open_breakpoints(self):
        return self.capture.open_breakpoints()

    def machine_states(self):
        """逐台机具的可追产权转移状态。"""
        out = {}
        for machine in sorted(set(self.capture.identities) | set(self.owner)):
            winner = self._winner_token(machine)
            out[machine] = {
                'owner': self.owner.get(machine),
                'encumbrances': sorted(self.encumbrances.get(machine, {}).keys()),
                'transfer_chain': list(self.chain.get(machine, ())),
                'winner_token': winner,
                'breakpoint_tokens': sorted(t for t, info in self.capture.tokens.items()
                                            if info.get('machine_ref') and
                                            self.capture.canonical(info['machine_ref']) == machine and
                                            info.get('phase') in ('requested', 'accepted')),
                'aliases': sorted(self.capture.identities.get(machine, {}).get('aliases', ())),
            }
        return out


def owners(events):
    """规范机具 id -> 当前登记所有人。

    仅终局过户可改变所有人；受理回执、换牌别名、迟到登记都不会顶名覆盖。
    无 kind 的旧版条目（{"machine_id","owner"}）视为初始登记原件。
    """
    registry = TitleRegistry(events)
    return {machine: state['owner'] for machine, state in registry.machine_states().items()
            if state['owner'] is not None}
