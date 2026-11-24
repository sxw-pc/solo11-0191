"""Collect operator shift claims from offline forms."""


def shifts(events):
    result = {}
    for event in events:
        result.setdefault(event['operator_id'], []).append(event)
    return result
