"""Time-dependent rural road evidence.

Road segments, closure orders and heavy-vehicle passes are *original road
documents* with explicit effective intervals.  A segment is only traversable
when its document is in force, its direction allows the move, no closure order
covers the entry time, and either the machine weight fits the limit or a pass
that is already effective at departure exempts it.  Straight-line distance is
never used: every leg carries the document id it was projected from.
"""
from datetime import datetime, timedelta
import heapq

ROAD_FIELDS = ('road_id', 'from', 'to', 'minutes', 'weight_limit_t',
               'direction', 'effective_from', 'effective_to')
ORDER_FIELDS = ('doc_id', 'authority', 'road_id', 'effective_from', 'effective_to')
PASS_FIELDS = ORDER_FIELDS + ('machine_id',)

DIRECTION_FORBIDDEN = 'DIRECTION_FORBIDDEN'
ROAD_NOT_EFFECTIVE = 'ROAD_NOT_EFFECTIVE'
ROAD_CLOSED = 'ROAD_CLOSED'
WEIGHT_EXCEEDED = 'WEIGHT_EXCEEDED'
NO_ROAD_EVIDENCE = 'NO_ROAD_EVIDENCE'
UNREACHABLE = 'UNREACHABLE'

REASON_TEXT = {
    DIRECTION_FORBIDDEN: '单行路禁止反向通行',
    ROAD_NOT_EFFECTIVE: '道路原件不在生效区间，不能按畅通处理',
    ROAD_CLOSED: '道路封闭（延长）令生效中',
    WEIGHT_EXCEEDED: '拖挂总重超过道路限重且无出发前生效的通行证',
    NO_ROAD_EVIDENCE: '缺失有效道路原件，不可默认最短路畅通',
    UNREACHABLE: '现有道路证据下不存在可通行路线',
}


def parse_time(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def iso(value):
    return value.isoformat(timespec='minutes') if value.second == 0 else value.isoformat()


def _require(doc, fields, label):
    missing = [key for key in fields if key not in doc]
    if missing:
        raise ValueError(f'{label} {doc.get("doc_id", doc.get("road_id", "?"))} 缺字段: {missing}')
    if not doc.get('authority'):
        raise ValueError(f'{label} {doc.get("doc_id", doc.get("road_id", "?"))} 缺少签发机关 authority')


class RoadNetwork:
    """Road graph projected from documents valid at traversal time."""

    def __init__(self, roads, closures=(), permits=()):
        self.roads = {}
        self.adjacency = {}
        for road in roads:
            _require(road, ROAD_FIELDS, '道路原件')
            if road['direction'] not in ('one_way', 'two_way'):
                raise ValueError(f"direction 仅支持 one_way/two_way: {road['road_id']}")
            if not isinstance(road['minutes'], int) or road['minutes'] <= 0:
                raise ValueError(f"minutes 必须为正整数分钟: {road['road_id']}")
            self.roads[road['road_id']] = road
            self.adjacency.setdefault(road['from'], []).append(road)
            if road['direction'] == 'two_way':
                self.adjacency.setdefault(road['to'], []).append(road)
        self.closures = list(closures)
        self.permits = list(permits)
        for order in self.closures:
            _require(order, ORDER_FIELDS, '封闭令')
        for permit in self.permits:
            _require(permit, PASS_FIELDS, '通行证')

    def _active(self, doc, at):
        return parse_time(doc['effective_from']) <= at < parse_time(doc['effective_to'])

    def active_permit(self, road_id, machine_id, at):
        for permit in self.permits:
            if (permit['road_id'] == road_id and permit['machine_id'] == machine_id
                    and self._active(permit, at)):
                return permit
        return None

    def active_closure(self, road_id, at):
        for order in self.closures:
            if order['road_id'] == road_id and self._active(order, at):
                return order
        return None

    def inspect(self, road_id, at, machine, want_from, want_to):
        """Return violation codes for traversing one documented segment."""
        road = self.roads[road_id]
        at = parse_time(at)
        violations = []
        forward = road['from'] == want_from and road['to'] == want_to
        backward = (road['direction'] == 'two_way'
                    and road['from'] == want_to and road['to'] == want_from)
        if not forward and not backward:
            violations.append(DIRECTION_FORBIDDEN)
        if not self._active(road, at):
            violations.append(ROAD_NOT_EFFECTIVE)
        if self.active_closure(road_id, at):
            violations.append(ROAD_CLOSED)
        weight = machine.get('gross_weight_t')
        if weight is None:
            raise ValueError(f"机具 {machine.get('machine_id', '?')} 缺少拖挂总重证据，不可假设可过桥")
        permit = self.active_permit(road_id, machine['machine_id'], at)
        if weight > road['weight_limit_t'] and permit is None:
            violations.append(WEIGHT_EXCEEDED)
        return violations

    def edges_from(self, node, at, machine):
        at = parse_time(at)
        edges = []
        for road in self.adjacency.get(node, ()):
            target = road['to'] if road['from'] == node else road['from']
            violations = self.inspect(road['road_id'], at, machine, node, target)
            if violations:
                continue
            permit = self.active_permit(road['road_id'], machine['machine_id'], at)
            edges.append((road, target, road['minutes'], permit))
        return edges

    def earliest_route(self, origin, destination, depart_at, machine):
        """Earliest-arrival route; never invents a path lacking documents."""
        depart_at = parse_time(depart_at)
        if origin == destination:
            return {'feasible': True, 'arrival_at': iso(depart_at), 'legs': [],
                    'evidence': [], 'permits_used': []}
        labels = {origin: depart_at}
        previous = {}
        heap = [(depart_at, origin)]
        settled = set()
        while heap:
            current_at, node = heapq.heappop(heap)
            if node in settled:
                continue
            settled.add(node)
            if node == destination:
                break
            for road, target, minutes, permit in self.edges_from(node, current_at, machine):
                arrival = current_at + timedelta(minutes=minutes)
                if target not in labels or arrival < labels[target]:
                    labels[target] = arrival
                    previous[target] = (node, road, current_at, arrival, permit)
                    heapq.heappush(heap, (arrival, target))
        if destination not in labels:
            # NO_ROAD_EVIDENCE: no documented edge leaves the origin at all
            # (the map offers no usable evidence). UNREACHABLE: documents
            # exist from the origin but rules (direction/weight/closure/
            # interval) leave no viable path to this destination.
            origin_edges = self.adjacency.get(origin, ())
            has_any_documented_edge = any(
                (road['from'] == origin)
                or (road['direction'] == 'two_way' and road['to'] == origin)
                for road in origin_edges)
            code = NO_ROAD_EVIDENCE if not has_any_documented_edge else UNREACHABLE
            return {'feasible': False, 'reason_code': code, 'reason': REASON_TEXT[code],
                    'origin': origin, 'destination': destination}
        legs = []
        node = destination
        while node != origin:
            prev_node, road, entry_at, exit_at, permit = previous[node]
            evidence = [road['road_id']]
            if permit:
                evidence.append(permit['doc_id'])
            legs.append({
                'road_id': road['road_id'], 'from': prev_node, 'to': node,
                'entry_at': iso(entry_at), 'exit_at': iso(exit_at),
                'minutes': road['minutes'], 'weight_limit_t': road['weight_limit_t'],
                'permit_id': permit['doc_id'] if permit else None,
                'evidence': evidence,
            })
            node = prev_node
        legs.reverse()
        evidence = []
        for leg in legs:
            for doc_id in leg['evidence']:
                if doc_id not in evidence:
                    evidence.append(doc_id)
        return {'feasible': True, 'arrival_at': iso(labels[destination]),
                'travel_minutes': int((labels[destination] - depart_at).total_seconds() // 60),
                'legs': legs, 'evidence': evidence,
                'permits_used': [leg['permit_id'] for leg in legs if leg['permit_id']]}
