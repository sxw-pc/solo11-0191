"""Offline capture of machine title evidence.

机具身份由铭牌（plate）、发动机号（engine_no）和登记原件（registry_doc）共同锚定。
换牌只产生别名；同一发动机号配不同登记原件属于身份冲突，涉事记录进隔离区，
同一机具不得被当作两台分别转让。所有事件按批次原子纳入，登记申请令牌在本地
缺少终局回执时登记为断点，等待登记机关带令牌的终局证明恢复。
"""


def event_order(event):
    """同一批次内的稳定顺序：显式 seq，其次事件 id，保证离线重放确定。"""
    return (event.get('seq', 0), str(event.get('id', '')))


class IdentityConflict(ValueError):
    """身份锚点自相矛盾（发动机号与登记原件不能同时指向两台机具）。"""


class TitleCapture:
    """事件溯源式采集器：身份合并、隔离区、申请令牌与本地事务。"""

    def __init__(self, events=None):
        self.aliases = {}          # 任一已知标识 -> 规范机具 id
        self.identities = {}       # 规范机具 id -> 锚点描述
        self.quarantine = {}       # 被拒标识 -> 原因
        self.events = []           # 已纳入事件（按到达顺序）
        self._engines = {}         # engine_no -> 规范机具 id
        self._docs = {}            # registry_doc -> 规范机具 id
        self.tokens = {}           # 申请令牌 -> {machine_ref, phase, sale_id, source_event}
        self.breakpoints = {}      # 申请令牌 -> 断点原因（已发出申请但本地无终局回执）
        if events:
            self.ingest(events)

    # ---- 身份锚定 ----------------------------------------------------------
    def _anchor(self, event):
        return {'plate': event.get('plate'),
                'engine_no': event.get('engine_no'),
                'registry_doc': event.get('registry_doc')}

    def _bind(self, canonical, ref, anchor):
        self.aliases[ref] = canonical
        for field in ('plate', 'engine_no', 'registry_doc'):
            value = anchor.get(field)
            if value:
                self.aliases[value] = canonical

    def _register_identity(self, event):
        """纳入一条身份类事件，返回规范机具 id；冲突记录进隔离区而非覆盖。"""
        anchor = self._anchor(event)
        ref = event.get('machine_id') or anchor.get('plate')
        engine = anchor.get('engine_no')
        doc = anchor.get('registry_doc')
        if not engine or not doc:
            # 旧版条目只有 machine_id/owner：以机具 id 立最小身份户，
            # 后续带铭牌、发动机号、登记原件的同 id 事件补齐共同锚定。
            if not ref:
                raise IdentityConflict(f"身份锚点不足：事件 {event.get('id')} 无任何机具标识")
            if ref in self.quarantine:
                return None
            if not event.get('machine_id'):
                raise IdentityConflict(
                    f"身份锚点不足：事件 {event.get('id')} 只有铭牌别名，缺机具 id、发动机号或登记原件")
            canonical = self.aliases.get(ref)
            if canonical is None:
                canonical = ref
                self.identities[canonical] = {'plate': anchor.get('plate') or ref,
                                              'engine_no': None, 'registry_doc': None,
                                              'aliases': set()}
                self.aliases[ref] = canonical
            info = self.identities[canonical]
            for field in ('plate', 'engine_no', 'registry_doc'):
                if anchor.get(field):
                    info[field] = anchor[field]
                    self.aliases[anchor[field]] = canonical
            return canonical
        known_engine = self._engines.get(engine)
        known_doc = self._docs.get(doc)
        if known_engine and known_doc and known_engine != known_doc:
            reason = f'发动机号 {engine} 与登记原件 {doc} 分属不同机具'
            self.quarantine[ref] = reason
            return None
        if known_engine and engine and self.identities[known_engine]['registry_doc'] != doc:
            reason = f'发动机号 {engine} 被配上第二份登记原件 {doc}，疑似拼机重号'
            self.quarantine[ref] = reason
            return None
        if known_doc and self.identities[known_doc]['engine_no'] != engine:
            reason = f'登记原件 {doc} 被配上第二台发动机 {engine}，疑似套牌'
            self.quarantine[ref] = reason
            return None
        canonical = known_engine or known_doc
        if canonical is None:
            canonical = ref
            self.identities[canonical] = dict(anchor, aliases=set())
            self._engines[engine] = canonical
            self._docs[doc] = canonical
        self.identities[canonical].setdefault('aliases', set())
        if ref != canonical:
            self.identities[canonical]['aliases'].add(ref)
        for field in ('plate', 'engine_no', 'registry_doc'):
            value = anchor.get(field)
            if value and value not in self.identities[canonical]['aliases']:
                if value != self.identities[canonical].get(field):
                    self.identities[canonical]['aliases'].add(value)
        self._bind(canonical, ref, anchor)
        return canonical

    def canonical(self, ref):
        """把铭牌/别名/机具 id 解析为规范机具 id；隔离标识返回 None。"""
        if ref in self.quarantine:
            return None
        return self.aliases.get(ref, ref)

    def conflict_reason(self, ref):
        return self.quarantine.get(ref)

    # ---- 事件纳入（本地事务） ----------------------------------------------
    def ingest(self, batch):
        """原子纳入一批事件：任一事件非法则整批回滚，本地状态不变。"""
        snapshot = self._snapshot()
        try:
            for event in sorted(batch, key=event_order):
                self._ingest_one(event)
        except Exception:
            self._restore(snapshot)
            raise
        return self

    def _snapshot(self):
        import copy
        return copy.deepcopy((self.aliases, self.identities, self.quarantine,
                              self.events, self._engines, self._docs,
                              self.tokens, self.breakpoints))

    def _restore(self, snapshot):
        (self.aliases, self.identities, self.quarantine, self.events,
         self._engines, self._docs, self.tokens, self.breakpoints) = snapshot

    def _ingest_one(self, event):
        kind = event.get('kind', 'title')
        if kind == 'title':
            self._register_identity(event)
        elif kind == 'replate':
            # 维修换牌：旧牌与新牌必须落在同一具名机具上，只加别名不立户
            old = self.canonical(event['machine_id'])
            if old is None:
                raise IdentityConflict(f"换牌事件 {event.get('id')} 引用了隔离机具 {event['machine_id']}")
            anchor = self._anchor(event)
            anchor['engine_no'] = self.identities[old]['engine_no']
            anchor['registry_doc'] = self.identities[old]['registry_doc']
            anchor['plate'] = event['new_plate']
            self._bind(old, event['new_plate'], anchor)
            self.identities[old].setdefault('aliases', set()).add(event['new_plate'])
        elif kind in ('transfer_app', 'transfer_receipt', 'transfer_completed',
                      'transfer_rejected', 'registry_proof'):
            token = event.get('token')
            phase = {'transfer_app': 'requested',
                     'transfer_receipt': 'accepted',
                     'transfer_completed': 'completed',
                     'transfer_rejected': 'rejected',
                     'registry_proof': event.get('phase', 'completed')}[kind]
            if token:
                record = self.tokens.get(token, {})
                record.update({'token': token, 'machine_ref': event.get('machine_id', record.get('machine_ref')),
                               'sale_id': event.get('sale_id', record.get('sale_id')),
                               'phase': phase, 'source_event': event.get('id')})
                self.tokens[token] = record
                if phase in ('requested', 'accepted'):
                    self.breakpoints[token] = '登记请求已发出，本地尚未取得终局回执'
                else:
                    self.breakpoints.pop(token, None)
            if kind == 'transfer_app':
                ref = event.get('machine_id')
                engine = event.get('engine_no')
                doc = event.get('registry_doc')
                if (ref and ref not in self.quarantine and engine and doc
                        and engine not in self._engines and doc not in self._docs):
                    self._register_identity(event)
        self.events.append(event)

    # ---- 断点恢复 -----------------------------------------------------------
    def open_breakpoints(self):
        """仍无终局的申请：此处不能断言任何一方取得对世产权。"""
        return dict(self.breakpoints)

    def resume_with_proof(self, token, phase, machine_id=None, sale_id=None,
                          source_event=None, batch_id=None):
        """登记机关带申请令牌的终局证明，协同恢复断点。"""
        event = {'kind': 'registry_proof', 'id': source_event or f'proof-{token}',
                 'token': token, 'phase': phase, 'machine_id': machine_id,
                 'sale_id': sale_id}
        if batch_id:
            event['batch_id'] = batch_id
        self.ingest([event])
        return event

    def identity_view(self):
        """供审计与测试回读的身份视图。"""
        return {canonical: {'engine_no': info['engine_no'],
                            'registry_doc': info['registry_doc'],
                            'plate': info['plate'],
                            'aliases': sorted(info.get('aliases', ()))}
                for canonical, info in self.identities.items()}
