"""Collect offline machine depot assignments.

``machines`` keeps the original parking-point projection (later events for the
same machine supersede earlier ones).  ``fleet`` additionally projects the
gross weight and service evidence required by road weight limits; the source
events remain plain offline documents so a relocated parking point or a new
weight figure forces every affected route to be recalculated.
"""


def machines(events):
    located = {}
    for event in events:
        located[event['machine_id']] = event['depot_id']
    return located


def fleet(events):
    """Project the latest depot event per machine into fleet records."""
    records = {}
    for event in events:
        machine_id = event['machine_id']
        if machine_id not in records or event.get('version', 0) >= records[machine_id].get('version', 0):
            records[machine_id] = dict(event)
    for machine_id, record in records.items():
        if 'gross_weight_t' not in record:
            raise ValueError(f'机具 {machine_id} 缺少拖挂总重证据，限重路段无法判断能否通行')
    return records
