"""Whole-fleet machine repositioning plans.

The legacy ``assign`` projection (one shortest travel time per job) is kept
for the seed fixture.  New plans must go through ``dispatch_plan``, which:

* routes on documented, time-dependent roads via :mod:`fieldmachine.road_capture`
  instead of straight-line travel matrices;
* treats each machine as one rolling timeline, so one machine can never hold
  overlapping village commitments and unloading / disinfection / shift change
  time is added before the next leg departs;
* freezes village windows once confirmed: a plan never silently rewrites a
  window, it emits a change or reschedule notice carrying the old commitment;
* keeps a serviceable / unconfirmable boundary: missing or unusable road
  evidence makes a job unserved with the document-level reason, never an
  assumed shortest path.
"""
from datetime import timedelta

from .depots import fleet, machines
from .road_capture import RoadNetwork, parse_time, iso, REASON_TEXT as ROAD_REASON_TEXT

LATE_ARRIVAL = 'LATE_ARRIVAL'
NO_MACHINE_AVAILABLE = 'NO_MACHINE_AVAILABLE'
REASON_TEXT = {
    LATE_ARRIVAL: '可通行路线存在但到达时刻晚于村委确认窗口',
    NO_MACHINE_AVAILABLE: '没有机具能在窗口内凭有效道路原件到达',
}


def assign(events, travel_minutes):
    """Legacy per-job nearest-machine projection (no mutex/road evidence)."""
    located = machines(events)
    return {job: min(located, key=lambda machine: travel_minutes[located[machine]][job])
            for job in travel_minutes['jobs']}


def _build_network(spec, scenario_docs):
    road_doc = spec['road_network']
    closures = list(road_doc.get('closures', ()))
    permits = list(road_doc.get('permits', ()))
    for doc in scenario_docs:
        kind = doc['doc_type']
        if kind == 'closure_order':
            closures.append(doc)
        elif kind == 'heavy_vehicle_pass':
            permits.append(doc)
        else:
            raise ValueError(f'未知道路文书类型: {kind}')
    return RoadNetwork(road_doc['roads'], closures, permits)


def _ordered_jobs(jobs):
    return sorted(jobs, key=lambda job: (job['arrive_by'], -job.get('priority', 0)))


def _route_for(network, machine, origin_node, depart_at, job):
    route = network.earliest_route(origin_node, job['village_id'], depart_at, machine)
    if not route['feasible']:
        return route
    arrival = parse_time(route['arrival_at'])
    if arrival > parse_time(job['arrive_by']):
        late = dict(route)
        late['feasible'] = False
        late['reason_code'] = LATE_ARRIVAL
        late['reason'] = REASON_TEXT[LATE_ARRIVAL]
        late['would_arrive_at'] = route['arrival_at']
        return late
    return route


def dispatch_plan(spec, scenario_docs=()):
    """Build an executable whole-fleet plan under the supplied road documents."""
    network = _build_network(spec, scenario_docs)
    machines_records = fleet(spec['depot_events'])
    ready_at = parse_time(spec['fleet_ready_at'])
    jobs = _ordered_jobs(spec['jobs'])
    prior = {item['job_id']: item for item in spec.get('prior_commitments', ())}

    init_state = {machine_id: {'node': record['depot_id'], 'free_at': ready_at}
                  for machine_id, record in machines_records.items()}
    cache = {}

    def candidate(machine_id, state, job):
        record = machines_records[machine_id]
        key = (machine_id, state['node'], iso(state['free_at']), job['job_id'])
        if key not in cache:
            cache[key] = _route_for(network, record, state['node'], state['free_at'], job)
        return cache[key]

    leaves = []

    def search(index, state, chosen):
        if index == len(jobs):
            leaves.append(list(chosen))
            return
        job = jobs[index]
        search(index + 1, state, chosen)  # leave the job unservable, with reasons
        for machine_id in sorted(machines_records):
            route = candidate(machine_id, state[machine_id], job)
            if not route['feasible']:
                continue
            service = job['service_minutes']
            next_free = parse_time(route['arrival_at']) + timedelta(
                minutes=service['unload'] + service['sanitize'] + service['shift_change'])
            next_state = {mid: dict(item) for mid, item in state.items()}
            next_state[machine_id] = {'node': job['village_id'], 'free_at': next_free}
            search(index + 1, next_state, chosen + [(job, machine_id, route)])

    search(0, init_state, [])

    def score(leaf):
        served_ids = {job['job_id'] for job, _, _ in leaf}
        travel = sum(route['travel_minutes'] for _, _, route in leaf)
        # Honour the village-confirmed machine pairing before raw travel time:
        # two villages must be split across two machines when both are feasible.
        matched = 0
        for job, machine_id, route in leaf:
            old = prior.get(job['job_id'])
            if old and old['machine_id'] == machine_id:
                matched += 1
            if old and [leg['road_id'] for leg in route['legs']] == old.get('route_road_ids'):
                matched += 1
        return (len(leaf),
                sum(next(j['priority'] for j in jobs if j['job_id'] == sid)
                    for sid in served_ids),
                matched, -travel)

    best = max(leaves, key=score)
    served = {job['job_id']: (machine_id, route) for job, machine_id, route in best}
    return _assemble(spec, scenario_docs, network, machines_records, jobs, prior,
                     ready_at, served, candidate)


def _assemble(spec, scenario_docs, network, machines_records, jobs, prior,
              ready_at, served, candidate):
    assignments = []
    timeline = {machine_id: [{'kind': 'ready', 'at': iso(ready_at),
                              'node': record['depot_id']}]
                for machine_id, record in machines_records.items()}
    for job in jobs:
        if job['job_id'] not in served:
            continue
        machine_id, route = served[job['job_id']]
        record = machines_records[machine_id]
        arrival = parse_time(route['arrival_at'])
        service = job['service_minutes']
        service_minutes = service['unload'] + service['sanitize'] + service['shift_change']
        next_depart = arrival + timedelta(minutes=service_minutes)
        depart_at = route['legs'][0]['entry_at'] if route['legs'] else iso(ready_at)
        for leg in route['legs']:
            timeline[machine_id].append({'kind': 'travel', 'job_id': job['job_id'], **leg})
        timeline[machine_id].append(
            {'kind': 'service', 'job_id': job['job_id'], 'node': job['village_id'],
             'start_at': iso(arrival), 'unload_minutes': service['unload'],
             'sanitize_minutes': service['sanitize'],
             'shift_change_minutes': service['shift_change'],
             'next_depart_at': iso(next_depart)})
        old = prior.get(job['job_id'])
        assignments.append({
            'job_id': job['job_id'], 'village_id': job['village_id'],
            'machine_id': machine_id, 'depot_id': record['depot_id'],
            'depart_at': depart_at, 'arrive_at': route['arrival_at'],
            'arrive_by': job['arrive_by'],
            'within_window': arrival <= parse_time(job['arrive_by']),
            'route': route,
            'next_depart_at': iso(next_depart),
            'service_minutes': service_minutes,
            'evidence': route['evidence'],
            'prior_commitment_id': old['commitment_id'] if old else None,
        })

    unserved = []
    for job in jobs:
        if job['job_id'] in served:
            continue
        attempts = []
        for machine_id in sorted(machines_records):
            state = _state_after(machines_records, assignments, machine_id, ready_at)
            route = candidate(machine_id, state, job)
            attempt = {'machine_id': machine_id, 'reason_code': route['reason_code'],
                       'reason': route.get('reason') or ROAD_REASON_TEXT.get(route['reason_code'],
                                                                             route['reason_code'])}
            if 'would_arrive_at' in route:
                attempt['would_arrive_at'] = route['would_arrive_at']
            if route.get('evidence'):
                attempt['evidence'] = route['evidence']
            attempts.append(attempt)
        unserved.append({'job_id': job['job_id'], 'village_id': job['village_id'],
                         'arrive_by': job['arrive_by'],
                         'reason_code': NO_MACHINE_AVAILABLE,
                         'reason': REASON_TEXT[NO_MACHINE_AVAILABLE],
                         'attempts': attempts})

    notices, ledger = _commitments(spec, scenario_docs, prior, assignments, unserved)
    return {
        'plan_id': spec['plan_id'], 'plan_version': spec.get('plan_version', 2),
        'generated_at': spec.get('generated_at', iso(parse_time(spec['fleet_ready_at']))),
        'fleet_ready_at': iso(ready_at),
        'assignments': assignments, 'unserved': unserved,
        'notices': notices, 'commitment_ledger': ledger,
        'machine_timeline': timeline,
        'scenario_docs': [doc['doc_id'] for doc in scenario_docs],
    }


def _state_after(machines_records, assignments, machine_id, ready_at):
    node = machines_records[machine_id]['depot_id']
    free_at = ready_at
    for item in assignments:
        if item['machine_id'] != machine_id:
            continue
        node = item['village_id']
        free_at = parse_time(item['next_depart_at'])
    return {'node': node, 'free_at': free_at}


def _commitments(spec, scenario_docs, prior, assignments, unserved):
    notices = []
    ledger = []
    by_job = {item['job_id']: item for item in assignments}
    unserved_ids = {item['job_id'] for item in unserved}
    scenario_evidence = [doc['doc_id'] for doc in scenario_docs]
    for job_id, old in prior.items():
        base = {'commitment_id': old['commitment_id'], 'job_id': job_id,
                'village_committee': old['village_committee'],
                'confirmed_machine_id': old['machine_id'],
                'confirmed_arrive_at': old['arrive_at'],
                'arrive_by': old['arrive_by'], 'plan_id': spec['plan_id'],
                'plan_version': spec.get('plan_version', 2)}
        if job_id in unserved_ids:
            detail = next(item for item in unserved if item['job_id'] == job_id)
            notice = {**base, 'notice_type': 'RESCHEDULE_NOTICE', 'status': 'RESCHEDULED',
                      'message': f"{old['village_committee']}：原承诺到机 {old['arrive_at']} 无法兑现，正式改期，原因见下，原窗口不被静默改写",
                      'reason_code': detail['reason_code'], 'reason': detail['reason'],
                      'machine_attempts': detail['attempts'],
                      'evidence': scenario_evidence or sorted({
                          doc for attempt in detail['attempts']
                          for doc in attempt.get('evidence', ())})}
            notices.append(notice)
            ledger.append({**base, 'status': 'RESCHEDULED', 'notice_id': notice['commitment_id'],
                           'readback': dict(old)})
            continue
        item = by_job[job_id]
        changed = (item['machine_id'] != old['machine_id']
                   or item['arrive_at'] != old['arrive_at'])
        if changed:
            notice = {**base, 'notice_type': 'CHANGE_NOTICE', 'status': 'REVISED',
                      'message': f"{old['village_committee']}：到机承诺变更，原承诺 {old['machine_id']} {old['arrive_at']}，现承诺 {item['machine_id']} {item['arrive_at']}",
                      'new_machine_id': item['machine_id'],
                      'new_arrive_at': item['arrive_at'],
                      'reason_code': 'ROAD_DOCUMENT_UPDATE',
                      'reason': '道路主管部门文书更新触发重算，已确认窗口本身未改动',
                      'evidence': sorted(set(item['evidence']) | set(scenario_evidence))}
            notices.append(notice)
            ledger.append({**base, 'status': 'REVISED', 'notice_id': notice['commitment_id'],
                           'readback': dict(old)})
        else:
            ledger.append({**base, 'status': 'CONFIRMED',
                           'arrive_at': item['arrive_at'], 'machine_id': item['machine_id'],
                           'readback': dict(old)})
    return notices, ledger
