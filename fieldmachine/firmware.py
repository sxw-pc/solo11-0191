"""Collect offline controller firmware inventory."""


def versions(events):
    result={}
    for event in events:
        result[event['controller_id']]=event['version']
    return result
