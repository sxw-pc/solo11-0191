"""按地块和网格的有界产量图，以及低产复核优先级。

已经发布的产量图是冻结证据：迟到校准只能产生新版本，且只重算受影响的地块与
网格，旧图原样保留可回读。未定质量以 lo–hi 区间表达，区间重叠时不得强行排序。
"""
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType

from .yield_stream import pulses
from .harvest_capture import capture_harvest


def kilograms(events):
    """传感器标注口径的分地块合计（保持既有调用方行为）。"""
    return {field: sum(row['kg'] for row in rows) for field, rows in pulses(events).items()}


@dataclass(frozen=True)
class BoundedMass:
    lo: float
    hi: float
    decided_kg: float
    contingent_kg: float

    @property
    def is_point(self):
        return abs(self.hi - self.lo) <= 1e-9

    def as_range(self):
        return (self.lo, self.hi) if not self.is_point else (self.lo, self.lo)


def _bounded(raw):
    return BoundedMass(lo=raw.get('lo', 0.0), hi=raw.get('hi', 0.0),
                       decided_kg=raw.get('decided_kg', 0.0),
                       contingent_kg=raw.get('contingent_kg', 0.0))


def _fingerprint(attributions):
    def cell_key(c):
        return f'{c[0]},{c[1]}'

    payload = [{'pulse_id': a.pulse_id, 'kg': a.kg,
                'decided_field_id': a.decided_field_id, 'contingent': a.contingent,
                'candidates': {str(k): v for k, v in a.candidates.items()},
                'cells': {str(fid): {cell_key(c): s for c, s in cs.items()}
                          for fid, cs in a.cells.items()}}
               for a in attributions]
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class YieldMap:
    revision: int
    label: str
    field_totals: object
    grid: object
    fingerprint: str
    calibration_id: object
    affected_fields: tuple
    affected_cells: object
    weighbridge_total_kg: float

    def field_range(self, field_id):
        return self.field_totals[field_id].as_range()

    def to_jsonable(self):
        return {
            'revision': self.revision, 'label': self.label,
            'calibration_id': self.calibration_id, 'fingerprint': self.fingerprint,
            'weighbridge_total_kg': self.weighbridge_total_kg,
            'affected_fields': list(self.affected_fields),
            'affected_cells': {fid: [list(c) for c in cells]
                               for fid, cells in self.affected_cells.items()},
            'field_totals': {fid: {'lo': b.lo, 'hi': b.hi,
                                   'decided_kg': b.decided_kg,
                                   'contingent_kg': b.contingent_kg}
                             for fid, b in self.field_totals.items()},
            'grid': {fid: {f'{c[0]},{c[1]}': {'lo': b.lo, 'hi': b.hi}
                           for c, b in cells.items()}
                     for fid, cells in self.grid.items()}}


def build_map(capture, revision, label, calibration_id=None, prior=None):
    """由归属结果构造冻结产量图，并相对上一版标出受影响地块与网格。"""
    field_totals = MappingProxyType({fid: _bounded(raw) for fid, raw in capture.field_bounds.items()})
    grid = MappingProxyType({
        fid: MappingProxyType({cell: _bounded(raw) for cell, raw in cells.items()})
        for fid, cells in capture.grid.items()})
    if prior is None:
        affected_fields, affected_cells = (), MappingProxyType({})
    else:
        affected_fields = tuple(sorted(
            fid for fid in field_totals
            if fid not in prior.field_totals or field_totals[fid] != prior.field_totals[fid]))
        cell_diff = {}
        for fid in set(grid) | set(prior.grid):
            changed = set(grid.get(fid, {})) ^ set(prior.grid.get(fid, {}))
            changed |= {c for c in grid.get(fid, {})
                        if c in prior.grid.get(fid, {}) and grid[fid][c] != prior.grid[fid][c]}
            if changed:
                cell_diff[fid] = tuple(sorted(changed))
        affected_cells = MappingProxyType(cell_diff)
    return YieldMap(
        revision=revision, label=label, field_totals=field_totals, grid=grid,
        fingerprint=_fingerprint(capture.pulses), calibration_id=calibration_id,
        affected_fields=affected_fields, affected_cells=affected_cells,
        weighbridge_total_kg=capture.weighbridge['weighbridge_total_kg'])


class MapBook:
    """产量图发布簿：只追加、不覆盖，旧版永久留痕。"""

    def __init__(self):
        self._maps = []

    @property
    def revisions(self):
        return tuple(m.revision for m in self._maps)

    def publish(self, capture, label='provisional', calibration_id=None):
        prior = self._maps[-1] if self._maps else None
        revision = len(self._maps) + 1
        ym = build_map(capture, revision, label, calibration_id, prior)
        if prior is not None and ym.fingerprint == prior.fingerprint:
            raise ValueError('证据指纹未变化，迟到证据未改变任何归属，禁止制造新版本')
        self._maps.append(ym)
        return ym

    def recalibrate(self, fixture, calibration, label):
        """用实测校准重算：只生成新版本并标注受影响范围，旧图保留。"""
        capture = capture_harvest(fixture, calibration=calibration)
        return self.publish(capture, label=label,
                            calibration_id=calibration.get('calibration_id'))

    def read(self, revision):
        for m in self._maps:
            if m.revision == revision:
                return m
        raise KeyError(f'产量图版本 {revision} 不存在；已发布版本 {self.revisions}')

    def read_back(self, revision):
        """旧图原样回读（JSON 可序列化），用于审计迟到校准未静默覆盖。"""
        return json.loads(json.dumps(self.read(revision).to_jsonable(), ensure_ascii=False))

    @property
    def latest(self):
        if not self._maps:
            raise KeyError('尚未发布任何产量图')
        return self._maps[-1]


def review_priority(yield_map):
    """低产复核优先级：区间不相交才可排序，重叠则保留区间并拒绝强行排序。"""
    ranges = {fid: b.as_range() for fid, b in yield_map.field_totals.items()}
    fields = sorted(ranges)
    overlapping = any(
        i != j and not (ranges[i][1] < ranges[j][0] or ranges[j][1] < ranges[i][0])
        for i in fields for j in fields)
    if overlapping:
        return {'status': 'indeterminate', 'order': (), 'ranges': MappingProxyType(ranges),
                'reason': '地块产量区间重叠，证据不足，不能强行排序，两地块均按区间挂起复核'}
    order = tuple(sorted(fields, key=lambda fid: (ranges[fid][0], ranges[fid][1], fid)))
    return {'status': 'ranked', 'order': order, 'ranges': MappingProxyType(ranges),
            'reason': f'按有界产量下界排序，先复核低产田 {order[0]}'}
