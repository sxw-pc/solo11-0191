"""Combine offline fuel pump pulses for each machine."""

def delivered(pulses):
    totals = {}
    for pulse in pulses:
        key = pulse['machine_id']
        totals[key] = totals.get(key, 0) + pulse['liters']
    return totals
