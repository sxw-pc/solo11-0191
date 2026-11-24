"""Compute a provisional operator payable interval."""
from .operator_shifts import shifts


def payable_minutes(events):
    result = {}
    for operator, rows in shifts(events).items():
        result[operator] = sum(row['end_minute'] - row['start_minute'] for row in rows if row['kind'] == 'work')
    return result
