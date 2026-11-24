"""Collect offline machine ownership register entries."""


def owners(events):
    result={}
    for event in events:
        result[event['machine_id']]=event['owner']
    return result
