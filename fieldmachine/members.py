"""Collect cooperative membership shares."""


def shares(events):
    result={}
    for event in events:
        result[event['member_id']]=event['shares']
    return result
