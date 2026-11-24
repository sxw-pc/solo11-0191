"""Title transfer adjudication and buyer pickup notices.

合同有效只产生债权，不产生对世产权。逐笔合同先验身份、签名、有效期间与卖方
当前处分权；登记申请令牌经历 requested -> accepted（受理不等于完成）->
completed/rejected 的终局；只有终局完成且同机唯一时，买方才取得交付权并收到
指名提机通知。通知只追加不覆盖，撤销一律发指名纠正并要求受影响合同重裁。
"""
from .asset_titles import TitleRegistry
from .title_capture import event_order


def _valid_contract(sale):
    if sale.get('signature_valid') is False:
        return False, '签名验真失败'
    window = sale.get('valid_until')
    if window and sale.get('signed_at') and window < sale['signed_at']:
        return False, '合同已过有效期间'
    return True, ''


class TransferBoard:
    """聚合产权登记与买卖合同，产出归属、交付权与通知留痕。"""

    def __init__(self, title_events=(), sales=()):
        self.registry = TitleRegistry(title_events)
        self.sales = {}
        self.notice_log = []      # 只追加：已发通知不可静默覆盖
        self.key_log = []         # 钥匙交接意图的裁定留痕
        for sale in sales:
            self.sales[sale['sale_id']] = dict(sale)

    def ingest_titles(self, batch):
        self.registry.ingest(batch)
        return self

    def ingest_sales(self, batch):
        snapshot = dict(self.sales)
        try:
            for sale in batch:
                if sale['sale_id'] in self.sales:
                    raise ValueError(f"合同 {sale['sale_id']} 重复建档，单机互斥拒绝")
                self.sales[sale['sale_id']] = dict(sale)
        except Exception:
            self.sales = snapshot
            raise
        return self

    def ingest_notice(self, event):
        """登记一条外部已发通知（如仓库早到的提机通知），仅留痕不改产权。"""
        self.notice_log.append(dict(event, status=event.get('status', 'on_record')))
        return self

    def request_key(self, event):
        """仓库钥匙交接意图：产权状态不可确认前一律 hold，绝不先放钥匙。"""
        decision = self._adjudicate_key(event)
        self.key_log.append(decision)
        return decision

    # ---- 合同逐笔裁定 -------------------------------------------------------
    def _adjudicate_sale(self, sale):
        sale_id = sale['sale_id']
        ref = sale['machine_id']
        machine = self.registry.capture.canonical(ref)
        base = {'sale_id': sale_id, 'machine': machine, 'machine_ref': ref,
                'seller': sale['seller'], 'buyer': sale['buyer'],
                'token': sale.get('token')}
        if machine is None:
            return dict(base, status='rejected',
                        reason=f"身份冲突已隔离：{self.registry.capture.conflict_reason(ref)}")
        ok, why = _valid_contract(sale)
        if not ok:
            return dict(base, status='rejected', reason=why)
        token = sale.get('token')
        application = self.registry.applications.get(token) if token else None
        # 已取得登记终局的合同先按终局裁定，不再用签约时的卖方字符串比对现所有人
        if application and application['phase'] == 'completed':
            if self.registry.owner.get(machine) != sale['buyer']:
                return dict(base, status='lost', token=token,
                            reason=f"同机唯一终局归 {self.registry.owner.get(machine)}，本合同未取得产权")
            return dict(base, status='owned', token=token,
                        reason='登记终局完成，买方取得对世产权与交付权')
        if application and application['phase'] == 'rejected':
            reason = application.get('rejection', '登记机关驳回申请')
            # 同机唯一败下阵来的有效合同：标的仍属他人，状态记 lost 而非普通驳回
            status = 'lost' if ('同机唯一' in reason or
                                (self.registry.owner.get(machine) and
                                 self.registry.owner.get(machine) != sale['seller'])) else 'rejected'
            return dict(base, status=status, token=token, reason=reason)
        if application and application['phase'] == 'revoked':
            return dict(base, status='reopened', token=token,
                        reason='终局登记被迟到撤销，合同须重裁')
        owner = self.registry.owner.get(machine)
        if owner != sale['seller']:
            return dict(base, status='rejected',
                        reason=f"卖方无当前处分权：登记所有人为 {owner}")
        encumbrances = self.registry.encumbrances.get(machine, {})
        if encumbrances:
            return dict(base, status='blocked',
                        reason='在效融资抵押/司法冻结未解除：%s' % ','.join(sorted(encumbrances)),
                        encumbrances=sorted(encumbrances))
        if not application:
            return dict(base, status='contracted', reason='合同有效，登记申请尚未发出')
        phase = application['phase']
        if phase in ('requested', 'accepted'):
            return dict(base, status='pending_registry', token=token, phase=phase,
                        reason='登记仅受理/在途，受理回执不等于过户完成' if phase == 'accepted'
                        else '登记请求已发出，终局未达，对世产权不可断言')
        return dict(base, status='pending_registry', reason='登记状态未知')

    def _adjudicate_key(self, event):
        ref = event['machine_id']
        machine = self.registry.capture.canonical(ref)
        base = {'request_id': event.get('request_id'), 'machine': machine,
                'requester': event.get('requester'), 'sale_id': event.get('sale_id')}
        if machine is None:
            return dict(base, decision='deny', reason='机具身份在隔离区，禁止交接')
        sale = self.sales.get(event.get('sale_id', ''))
        if not sale or sale.get('buyer') != event.get('requester'):
            return dict(base, decision='deny', reason='请求人不是任何有效合同的买方')
        verdict = self._adjudicate_sale(sale)
        if verdict['status'] != 'owned':
            return dict(base, decision='hold', reason='产权状态不可确认：' + verdict['reason'])
        return dict(base, decision='release', token=verdict.get('token'),
                    reason='登记终局归属买方，准予钥匙实物交付')

    # ---- 全量重裁（事件溯源，确定性） ---------------------------------------
    def _notice_timeline(self, sales):
        """从申请的阶段史重建提机通知时间线：只追加，撤销另发指名纠正。"""
        notices = list(self.notice_log)
        seen_issued = {n.get('notice_id') for n in notices if n.get('kind') == 'pickup_notice'}
        seen_corrections = {n.get('notice_id') for n in notices if n.get('kind') == 'correction'}
        for token, app in self.registry.applications.items():
            sale_id = app.get('sale_id')
            verdict = sales.get(sale_id)
            machine = app.get('machine')
            buyer = self.sales.get(sale_id, {}).get('buyer')
            completed_at = next((p['at'] for p in app.get('phases', [])
                                 if p['phase'] == 'completed'), None)
            if completed_at and verdict and verdict['status'] in ('owned', 'reopened', 'lost'):
                notice_id = f'notice:{sale_id}:{token}'
                if notice_id not in seen_issued:
                    notices.append({'notice_id': notice_id, 'kind': 'pickup_notice',
                                    'sale_id': sale_id, 'machine': machine, 'buyer': buyer,
                                    'token': token, 'status': 'issued', 'issued_at': completed_at,
                                    'reason': '登记终局完成，买方可提机'})
                    seen_issued.add(notice_id)
            # 终局被撤销：先前的提机通知失去产权基础，指名纠正并要求合同重裁
            if app.get('phase') == 'revoked':
                notice_id = f'notice:{sale_id}:{token}'
                target = next((n for n in notices
                               if n.get('notice_id') == notice_id and n.get('status') != 'revoked'), None)
                if target:
                    target['status'] = 'revoked'
                    correction_id = f'correction:{notice_id}'
                    if correction_id not in seen_corrections:
                        notices.append({'notice_id': correction_id, 'kind': 'correction',
                                        'revokes': notice_id, 'sale_id': sale_id,
                                        'machine': machine, 'named_buyer': buyer,
                                        'status': 'issued',
                                        'reason': '登记机关迟到撤销，指名纠正并要求合同重裁',
                                        'reopen_contracts': [sale_id]})
                        seen_corrections.add(correction_id)
        return notices

    def adjudicate(self):
        sales = {}
        for sale in self.sales.values():
            sales[sale['sale_id']] = self._adjudicate_sale(sale)
        notices = self._notice_timeline(sales)
        # 旧通知回读：先前发出的提机通知若已失去产权基础，只能指名撤销，不能静默覆盖
        for notice in notices:
            if notice.get('kind') != 'pickup_notice' or notice.get('status') in ('revoked',):
                continue
            verdict = sales.get(notice.get('sale_id'))
            if verdict and verdict['status'] not in ('owned', 'reopened'):
                prior_status = notice.get('status', 'issued')
                notice['status'] = 'revoked'
                correction_id = f'correction:{notice["notice_id"]}'
                if not any(n.get('notice_id') == correction_id for n in notices):
                    notices.append({'notice_id': correction_id, 'kind': 'correction',
                                    'revokes': notice['notice_id'],
                                    'sale_id': notice['sale_id'], 'machine': notice.get('machine'),
                                    'named_buyer': notice.get('buyer'),
                                    'status': 'issued',
                                    'prior_status': prior_status,
                                    'reason': '产权终局变更/撤销，指名纠正并要求合同重裁',
                                    'reopen_contracts': [notice['sale_id']]})
        breakpoints = self.registry.open_breakpoints()
        return {
            'owners': {m: s['owner'] for m, s in self.registry.machine_states().items()
                       if s['owner'] is not None},
            'machine_states': self.registry.machine_states(),
            'sales': sales,
            'notices': notices,
            'key_decisions': list(self.key_log),
            'breakpoints': breakpoints,
            'revocations': list(self.registry.revocations),
            'quarantine': dict(self.registry.capture.quarantine),
        }


def accepted(title_events, sales):
    """向后兼容：返回每份合同在当前产权与处分权下是否成立。

    不再只按卖方字符串比较——抵押/冻结、身份冲突、卖方非现登记所有人都会
    判 False；受理中的合同同样为 False（受理不等于过户，且卖方仍须有处分权）。
    """
    board = TransferBoard(title_events, sales)
    result = {}
    for sale in sales:
        verdict = board._adjudicate_sale(sale)
        result[sale['sale_id']] = verdict['status'] in ('contracted', 'owned', 'pending_registry')
    return result
