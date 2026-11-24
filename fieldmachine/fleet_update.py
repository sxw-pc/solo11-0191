"""Provisional fleet update success from controller reports."""
from .firmware import versions


def ready(events, target):
    state=versions(events)
    return any(version==target for version in state.values())
