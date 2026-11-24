"""Provisional nearest-machine positioning plan."""
from .depots import machines


def assign(events, travel_minutes):
    located=machines(events)
    return {job:min(located,key=lambda machine:travel_minutes[located[machine]][job]) for job in travel_minutes['jobs']}
