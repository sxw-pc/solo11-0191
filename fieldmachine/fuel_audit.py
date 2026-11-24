"""Compare pump-delivered fuel against tank receipt."""
from .fuel import delivered


def reconcile(pulses, tank_receipts):
    totals = delivered(pulses)
    return {machine: {'delivered': liters, 'status': 'balanced' if liters == tank_receipts.get(machine, 0) else 'short'}
            for machine, liters in totals.items()}
