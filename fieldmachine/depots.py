"""Collect offline machine depot assignments."""


def machines(events):
    return {event['machine_id']:event['depot_id'] for event in events}
