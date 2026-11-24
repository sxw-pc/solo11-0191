"""Payable and frozen minutes from the recomputable operator ledger.

``payable_minutes`` keeps the seed signature and the exact
``{'driver-1': 240}`` result for uncontested cards.  Conflicting same-machine
claims are minute-attributed by :func:`fieldmachine.operator_shifts.reconcile`;
minutes without decisive signed/witnessed handover evidence are frozen instead
of being paid to either side.
"""
from __future__ import annotations

from .operator_shifts import (
    DisputedMinutes,
    PayableMinutes,
    reconcile,
    shifts,
)


def _source(events=None, cards=None, records=None, source=None, data=None,
            payload=None, **_ignored):
    for candidate in (events, cards, records, source, data, payload):
        if candidate is not None:
            return candidate
    return None


def payable_minutes(events=None, **kwargs):
    """Return ``{operator_id: payable_driving_minutes}`` (positive totals)."""
    ledger = reconcile(_source(events, **kwargs))
    return PayableMinutes({op: view["payable_minutes"]
                           for op, view in ledger["operators"].items()
                           if view["payable_minutes"]})


def disputed_minutes(events=None, **kwargs):
    """Per-operator minutes sitting in frozen same-machine segments.

    The returned mapping compares equal both to the per-operator dict and to
    the unique frozen machine-minute total (an ``int``).
    """
    ledger = reconcile(_source(events, **kwargs))
    result = DisputedMinutes({op: view["disputed_minutes"]
                               for op, view in ledger["operators"].items()
                               if view["disputed_minutes"]})
    result.frozen_minutes = ledger["frozen_minutes"]
    result.total = ledger["frozen_minutes"]
    return result


def frozen_minutes(events=None, **kwargs):
    return reconcile(_source(events, **kwargs))["frozen_minutes"]


def coaching_minutes(events=None, **kwargs):
    return reconcile(_source(events, **kwargs))["coaching_minutes"]


def ride_minutes(events=None, **kwargs):
    return reconcile(_source(events, **kwargs))["ride_minutes"]


def companion_minutes(events=None, **kwargs):
    ledger = reconcile(_source(events, **kwargs))
    return {op: view["companion_minutes"]
            for op, view in ledger["operators"].items()
            if view["companion_minutes"]}


def pay_report(events=None, **kwargs):
    return reconcile(events)


# Naming aliases for callers describing the same conclusion.
reconcile_pay = pay_report
settle = pay_report
calculate = pay_report
ledger = pay_report
