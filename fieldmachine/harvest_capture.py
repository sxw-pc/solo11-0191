"""收获产量归属：时钟同步、割台窗口、动态滞后、车斗残粮与过磅守恒。

本模块只回答一个主营问题：流量传感器迟到的谷物，在割台切割瞬间生长在哪块田、
哪个网格。它不裁定压实可作业区、油量、机手工资或报价，也不把过磅总量硬分到田。
"""
from dataclasses import dataclass
from types import MappingProxyType


class HarvestConservationError(ValueError):
    """过磅总量与流量脉冲总质量不一致，或外部硬分摊破坏守恒。"""


class CalibrationError(ValueError):
    """校准证据与本机滞后带/脉冲不匹配，或两份校准互相冲突。"""


def _frozen(items):
    return MappingProxyType(dict(items))


@dataclass(frozen=True)
class PulseAttribution:
    pulse_id: str
    kg: float
    sensor_time_master: float
    sensor_field_id: object
    throughput: str
    cut_window: tuple
    lag_band_used: tuple
    candidates: object          # {field_id: 份额}，未定质量以候选范围表达
    decided_field_id: object    # 单字段时为该字段，候选时为 None
    contingent: bool
    cells: object               # {field_id: {cell: 份额}}
    reason: str


@dataclass(frozen=True)
class HarvestCapture:
    pulses: object
    field_bounds: object        # {field_id: BoundedMass 形状映射}
    grid: object                # {field_id: {cell: {'lo','hi'}}}
    header_windows: tuple
    cut_coverage: object        # {field_id: {cell: 经过次数}}，重复经过只计一次覆盖
    hopper_ledger: tuple
    weighbridge: object
    lag_bands: object
    boundary_cross_master: float


def to_master(local_min, clock, offsets):
    """独立时钟读数转主时钟：master = local + (master - local) 偏移。"""
    return local_min + offsets[clock]


def normalize_clocks(fixture):
    """割台、流量、GPS 三路独立时钟全部对齐主时钟，返回三路主时事件。"""
    offsets = fixture['clock_offsets_min']
    header = sorted((to_master(e['at_min'], 'header', offsets), e['state']) for e in fixture['header_events'])
    fixes = sorted(({'at_min': to_master(e['at_min'], 'gps', offsets), 'x_m': e['x_m'], 'y_m': e['y_m']}
                    for e in fixture['gps_fixes']), key=lambda e: e['at_min'])
    pulses = sorted(({'pulse_id': e['pulse_id'], 'at_min': to_master(e['at_min'], 'flow', offsets),
                      'kg': e['kg'], 'throughput': e['throughput'], 'sensor_field_id': e.get('sensor_field_id')}
                     for e in fixture['flow_pulses']), key=lambda e: e['at_min'])
    return header, fixes, pulses, offsets


def header_on_windows(header_events):
    """割台开关配对成实际切割窗口；割台关闭与地头转弯不产生收获质量。"""
    windows, start = [], None
    for at, state in header_events:
        if state == 'on' and start is None:
            start = at
        elif state == 'off' and start is not None:
            windows.append((start, at))
            start = None
    if start is not None:
        windows.append((start, header_events[-1][0]))
    return tuple(windows)


def header_is_on(at, windows):
    return any(lo <= at <= hi for lo, hi in windows)


def field_at(x_m, y_m, fields):
    """GPS 点落在哪个地块矩形内，田界外返回 None。"""
    for f in fields:
        x0, y0, x1, y1 = f['field_rect']
        if x0 <= x_m < x1 and y0 <= y_m < y1:
            return f['field_id']
    return None


def cell_of(x_m, y_m, grid_size_m):
    return (int(x_m // grid_size_m), int(y_m // grid_size_m))


def interpolate_position(at, fixes):
    """按主时在 GPS 定位轨迹间分段线性插值。"""
    if at <= fixes[0]['at_min']:
        return fixes[0]['x_m'], fixes[0]['y_m']
    if at >= fixes[-1]['at_min']:
        return fixes[-1]['x_m'], fixes[-1]['y_m']
    for a, b in zip(fixes, fixes[1:]):
        if a['at_min'] <= at <= b['at_min']:
            span = b['at_min'] - a['at_min']
            r = 0.0 if span == 0 else (at - a['at_min']) / span
            return a['x_m'] + r * (b['x_m'] - a['x_m']), a['y_m'] + r * (b['y_m'] - a['y_m'])
    return fixes[-1]['x_m'], fixes[-1]['y_m']


def cut_coverage(header_windows, fixes, fields, grid_size_m):
    """割台开启段扫过的网格与经过次数；重复经过的轨迹不重复创造收获质量。"""
    coverage = {f['field_id']: {} for f in fields}
    for lo, hi in header_windows:
        anchors = [p for p in fixes if lo <= p['at_min'] <= hi]
        x0, y0 = interpolate_position(lo, fixes)
        x1, y1 = interpolate_position(hi, fixes)
        anchors = [{'at_min': lo, 'x_m': x0, 'y_m': y0}, *anchors,
                   {'at_min': hi, 'x_m': x1, 'y_m': y1}]
        anchors.sort(key=lambda p: p['at_min'])
        for a, b in zip(anchors, anchors[1:]):
            distance = abs(b['x_m'] - a['x_m']) + abs(b['y_m'] - a['y_m'])
            steps = max(1, int(distance // max(1.0, grid_size_m / 2)) + 1)
            for k in range(steps + 1):
                r = k / steps
                x, y = a['x_m'] + r * (b['x_m'] - a['x_m']), a['y_m'] + r * (b['y_m'] - a['y_m'])
                fid = field_at(x, y, fields)
                if fid is None:
                    continue
                cell = cell_of(x, y, grid_size_m)
                coverage[fid][cell] = coverage[fid].get(cell, 0) + 1
    return _frozen((fid, _frozen(sorted(cells.items()))) for fid, cells in coverage.items())


def throughput_band(throughput, lag_bands):
    """边界脉冲在两种吞吐状态下各取一条滞后带，因此其来源天然两可。"""
    if throughput == 'high':
        return (lag_bands['high'],)
    if throughput == 'low':
        return (lag_bands['low'],)
    return (lag_bands['high'], lag_bands['low'])


def cut_window_for(pulse, band):
    """由传感器到达时刻和吞吐相关滞后带反推割台切割时间窗。"""
    return (pulse['at_min'] - band[1], pulse['at_min'] - band[0])


def _window_fields(window, fixes, fields):
    """切割窗口内插值轨迹覆盖到的地块及份额（按窗口端点与中点采样）。"""
    lo, hi = window
    samples = (lo, (lo + hi) / 2, hi)
    hits = {}
    for t in samples:
        x, y = interpolate_position(t, fixes)
        fid = field_at(x, y, fields)
        if fid is not None:
            hits[fid] = hits.get(fid, 0) + 1
    total = sum(hits.values())
    return {fid: n / total for fid, n in hits.items()} if total else {}


def _window_cells(window, fixes, fields, grid_size_m):
    lo, hi = window
    x, y = interpolate_position((lo + hi) / 2, fixes)
    fid = field_at(x, y, fields)
    return {} if fid is None else {fid: {cell_of(x, y, grid_size_m): 1.0}}


def attribute_pulses(pulses, header_windows, fixes, fields, lag_bands, grid_size_m):
    """逐脉冲建立从割台切割位置到延迟流量的质量归属。

    流量时间戳的 GPS 地块不直接使用；以实测滞后带反推切割窗，再按割台轨迹定位。
    割台关闭段无切割位置，归属窗口跨田界时保留候选范围而非强行选边。
    """
    out = []
    for p in pulses:
        bands = throughput_band(p['throughput'], lag_bands)
        candidate_sets, cell_sets, windows, reasons = [], [], [], []
        for band in bands:
            window = cut_window_for(p, band)
            windows.append(window)
            wlo, whi = window
            if not any(max(lo, wlo) <= min(hi, whi) for (lo, hi) in header_windows):
                reasons.append(f'滞后带{band}下切割窗{window}落在割台关闭段')
                continue
            share = _window_fields(window, fixes, fields)
            if not share:
                reasons.append(f'滞后带{band}下切割窗{window}无田界内轨迹')
                continue
            candidate_sets.append(share)
            cell_sets.append(_window_cells(window, fixes, fields, grid_size_m))
            reasons.append(f'滞后带{band}反推切割窗{window}')
        merged, decided, contingent = {}, None, True
        if len(candidate_sets) == 1 and len(candidate_sets[0]) == 1:
            merged = dict(candidate_sets[0])
            decided = next(iter(merged))
            contingent = False
        else:
            for share in candidate_sets:
                for fid, s in share.items():
                    merged[fid] = max(merged.get(fid, 0.0), s)
            if len(merged) == 1:
                decided = next(iter(merged))
                contingent = False
        cells = {}
        for cset in cell_sets:
            for fid, cs in cset.items():
                cells.setdefault(fid, {})
                for cell, s in cs.items():
                    cells[fid][cell] = max(cells[fid].get(cell, 0.0), s)
        out.append(PulseAttribution(
            pulse_id=p['pulse_id'], kg=p['kg'], sensor_time_master=p['at_min'],
            sensor_field_id=p['sensor_field_id'], throughput=p['throughput'],
            cut_window=(min(w[0] for w in windows), max(w[1] for w in windows)),
            lag_band_used=tuple(tuple(b) for b in bands),
            candidates=_frozen(merged.items()), decided_field_id=decided,
            contingent=contingent,
            cells=_frozen((fid, _frozen(sorted(cs.items()))) for fid, cs in cells.items()),
            reason='；'.join(reasons)))
    missing = [a.pulse_id for a in out if not a.candidates]
    if missing:
        raise HarvestConservationError(
            f'脉冲 {missing} 的全部切割窗都落在割台关闭段或田界外，'
            '质量无生长地归属；禁止静默丢弃或改由过磅总量硬分')
    return tuple(out)


def hopper_ledger(pulses_master, load_events, weighings):
    """装卸状态台账：车斗残粮与跨地块清仓按状态逐段带入，过磅只约束总质量。"""
    ledger, balance = [], 0.0
    for ev in sorted(load_events, key=lambda e: e['at_min']):
        if ev['state'] == 'start':
            balance = float(ev.get('hopper_kg', 0))
            ledger.append({'at_min': ev['at_min'], 'kind': 'load_start', 'kg': balance, 'hopper_kg': balance})
        elif ev['state'] == 'carry':
            ledger.append({'at_min': ev['at_min'], 'kind': 'cross_field_carry',
                           'unload_kg': ev.get('unload_kg', 0), 'hopper_kg': balance,
                           'note': ev.get('note', '')})
        elif ev['state'] == 'unload':
            balance -= float(ev.get('unload_kg', 0))
            ledger.append({'at_min': ev['at_min'], 'kind': 'unload', 'kg': -float(ev.get('unload_kg', 0)),
                           'hopper_kg': balance})
    for p in pulses_master:
        balance += p['kg']
        ledger.append({'at_min': p['at_min'], 'kind': 'grain_arrival', 'pulse_id': p['pulse_id'],
                       'kg': p['kg'], 'hopper_kg': balance})
    for w in sorted(weighings, key=lambda e: e['at_min']):
        ledger.append({'at_min': w['at_min'], 'kind': 'weighbridge', 'action': w['action'],
                       'net_kg': w['net_kg'], 'hopper_kg': balance})
        balance = float(w.get('hopper_after_kg', 0))
        ledger.append({'at_min': w['at_min'], 'kind': 'unload', 'kg': -w['net_kg'], 'hopper_kg': balance})
    order = {'load_start': 0, 'cross_field_carry': 1, 'grain_arrival': 2,
             'weighbridge': 3, 'unload': 4}
    return tuple(sorted(ledger, key=lambda e: (e['at_min'], order.get(e['kind'], 9))))


def reconcile(pulses_master, weighings):
    """过磅总量只能守恒校验，绝不能凭总量把质量硬分到任一田。"""
    flow_total = sum(p['kg'] for p in pulses_master)
    weigh_total = sum(w['net_kg'] for w in weighings if w.get('action') == 'unload')
    if not weighings:
        raise HarvestConservationError('缺少独立过磅单，无法约束总质量')
    if abs(flow_total - weigh_total) > 1e-9:
        raise HarvestConservationError(
            f'流量总质量 {flow_total} kg 与过磅净重 {weigh_total} kg 不守恒；'
            '过磅单只可约束总质量，不得按比例硬分至地块')
    return _frozen((('flow_total_kg', flow_total), ('weighbridge_total_kg', weigh_total),
                    ('difference_kg', 0.0), ('scaling_allowed', False)))


def bounded_totals(attributions, fields):
    """按地块汇总有界质量：已定脉冲计入上下界，未定脉冲按候选份额摊入区间。"""
    bounds = {f['field_id']: {'lo': 0.0, 'hi': 0.0, 'contingent_kg': 0.0, 'decided_kg': 0.0}
              for f in fields}
    grid = {f['field_id']: {} for f in fields}
    for a in attributions:
        if a.decided_field_id is not None:
            fid = a.decided_field_id
            bounds[fid]['lo'] += a.kg
            bounds[fid]['hi'] += a.kg
            bounds[fid]['decided_kg'] += a.kg
            for cfid, cs in a.cells.items():
                for cell, s in cs.items():
                    g = grid[cfid].setdefault(cell, {'lo': 0.0, 'hi': 0.0})
                    g['lo'] += a.kg * s
                    g['hi'] += a.kg * s
        else:
            for fid in a.candidates:
                bounds[fid]['hi'] += a.kg
                bounds[fid]['contingent_kg'] += a.kg
            for cfid, cs in a.cells.items():
                for cell, s in cs.items():
                    grid[cfid].setdefault(cell, {'lo': 0.0, 'hi': 0.0})['hi'] += a.kg * s
    field_bounds = _frozen((fid, _frozen(sorted(v.items()))) for fid, v in bounds.items())
    frozen_grid = _frozen((fid, _frozen(sorted((cell, _frozen(sorted(v.items())))
                                               for cell, v in cells.items())))
                          for fid, cells in grid.items())
    return field_bounds, frozen_grid


def apply_calibration(capture, calibration, lag_bands=None, fixes=(), fields=(), grid_size_m=10):
    """以实测冲洗/清仓响应收窄动态传输边界，只重判其指定的边界脉冲。

    迟到校准不得静默改图：本函数返回新归属序列，旧图由 yield_map.MapBook 留档。
    """
    lag_bands = lag_bands or capture.lag_bands
    pid = calibration.get('applies_to_pulse')
    target = next((p for p in capture.pulses if p.pulse_id == pid), None)
    if target is None:
        raise CalibrationError(f"校准 {calibration.get('calibration_id')} 指定的脉冲 {pid} 不存在")
    if not target.contingent:
        raise CalibrationError(f'脉冲 {pid} 已有确定归属，迟到校准不得覆盖')
    kind = calibration['kind']
    if kind == 'header_stop_to_flow_clear':
        observed = calibration['flow_clear_master_min'] - calibration['header_stop_master_min']
        band = lag_bands[calibration['throughput']]
        if not (band[0] - 1e-9 <= observed <= band[1] + 1e-9):
            raise CalibrationError(
                f'冲洗滞后 {observed} 分钟超出本机 {calibration["throughput"]} 吞吐滞后带 {band}')
        # 停机后仍有流量到达：边界脉冲是上一块田（割台在 F1 收割）的延迟谷物
        verdict = 'F1' if target.sensor_field_id == 'F2' else target.sensor_field_id
        reason = f"冲洗试验实测滞后 {observed} 分钟，边界脉冲仍来自割台所在上一块田 {verdict}"
    elif kind == 'bypass_weigh_and_cleanout':
        if calibration['cleanout_complete_master_min'] >= calibration['boundary_cross_master_min']:
            raise CalibrationError('清仓完成晚于过界，不能证明前地块谷物已排尽')
        band = lag_bands[calibration['throughput']]
        observed = calibration['boundary_cross_master_min'] - calibration['cleanout_complete_master_min']
        if observed < band[0] - 1e-9:
            raise CalibrationError(
                f'清仓响应 {observed} 分钟短于 {calibration["throughput"]} 吞吐最小滞后 {band[0]} 分钟')
        # 过界前排尽：边界脉冲只能来自当前地块 F2
        verdict = target.sensor_field_id
        reason = f"旁路称重 {calibration.get('bypass_net_kg')} kg 且清仓先于过界，边界脉冲来自 {verdict}"
    else:
        raise CalibrationError(f'未知校准类型 {kind}')
    if verdict not in target.candidates:
        raise CalibrationError(f'校准结论 {verdict} 不在脉冲 {pid} 的候选地块 {dict(target.candidates)} 内')
    cell = _verdict_cell(target, verdict, fixes, fields, grid_size_m)
    new_pulse = PulseAttribution(
        pulse_id=target.pulse_id, kg=target.kg, sensor_time_master=target.sensor_time_master,
        sensor_field_id=target.sensor_field_id, throughput=target.throughput,
        cut_window=target.cut_window, lag_band_used=target.lag_band_used,
        candidates=_frozen(((verdict, 1.0),)), decided_field_id=verdict, contingent=False,
        cells=_frozen(((verdict, _frozen(((cell, 1.0),))),)),
        reason=target.reason + '；' + reason)
    return tuple(new_pulse if p.pulse_id == pid else p for p in capture.pulses), new_pulse


def _verdict_cell(target, verdict, fixes, fields, grid_size_m):
    """按校准结论的地块，在切割窗内沿轨迹取该地块一侧的网格，而非沿用对方田候选格。"""
    if fixes:
        lo, hi = target.cut_window
        steps = 8
        for k in range(steps + 1):
            t = lo + (hi - lo) * k / steps
            x, y = interpolate_position(t, fixes)
            if field_at(x, y, fields) == verdict:
                return cell_of(x, y, grid_size_m)
    for fid, cs in target.cells.items():
        if fid == verdict:
            return next(iter(cs))
    for fid, cs in target.cells.items():
        return next(iter(cs))
    raise CalibrationError(f'脉冲 {target.pulse_id} 无网格证据')


def capture_harvest(fixture, calibration=None):
    """主入口：同步、定位、去重、滞后归属、车斗台账与过磅守恒一次算清。"""
    header, fixes, pulses, offsets = normalize_clocks(fixture)
    fields = fixture['fields']
    grid_size_m = fixture['grid_size_m']
    lag_bands = {k: tuple(v) for k, v in fixture['lag_bands_min'].items()}
    windows = header_on_windows(header)
    coverage = cut_coverage(windows, fixes, fields, grid_size_m)
    attributions = attribute_pulses(pulses, windows, fixes, fields, lag_bands, grid_size_m)
    ledger = hopper_ledger(pulses, fixture.get('load_events', []), fixture.get('weighings', []))
    weigh = reconcile(pulses, fixture.get('weighings', []))
    capture = HarvestCapture(
        pulses=attributions,
        field_bounds=_frozen(()), grid=_frozen(()),
        header_windows=windows, cut_coverage=coverage, hopper_ledger=ledger,
        weighbridge=weigh, lag_bands=_frozen(lag_bands.items()),
        boundary_cross_master=fixture['boundary_cross_master_min'])
    if calibration is not None:
        attributions, _ = apply_calibration(capture, calibration, lag_bands,
                                            fixes=fixes, fields=fields, grid_size_m=grid_size_m)
    field_bounds, grid = bounded_totals(attributions, fields)
    return HarvestCapture(
        pulses=attributions, field_bounds=field_bounds, grid=grid,
        header_windows=windows, cut_coverage=coverage, hopper_ledger=ledger,
        weighbridge=weigh, lag_bands=_frozen(lag_bands.items()),
        boundary_cross_master=fixture['boundary_cross_master_min'])
