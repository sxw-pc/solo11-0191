"""Collect offline harvester grain-flow pulses."""


def pulses(events):
    result={}
    for event in events:
        result.setdefault(event['field_id'], []).append(event)
    return result
