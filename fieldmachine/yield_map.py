"""Provisional harvested mass by field."""
from .yield_stream import pulses


def kilograms(events):
    return {field:sum(row['kg'] for row in rows) for field,rows in pulses(events).items()}
