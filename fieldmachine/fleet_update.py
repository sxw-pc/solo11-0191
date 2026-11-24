"""Fleet update safety verdict.

The provisional seed returned ``True`` when *any* controller reported the
target version.  After a power loss that turns a mixed ECU1=v2 / ECU2=v1
machine into a false "ready".  Readiness now requires the complete controller
set to reach the target and honours explicit boot-self-test evidence.

The signed compatibility matrix / calibration / partition proof lives in
:mod:`fieldmachine.firmware_capture`; :func:`ready` stays a small boolean
projection for simple report streams.  Pass a capture/fixture/hardware dict
(or ``matrix=``) to obtain the full offline proof through :func:`assess`.
"""
from .firmware import versions, latest_events, boot_verified as _event_boot

CANONICAL_CONTROLLERS = ('ECU1', 'ECU2')


def _events_from_doc(doc):
    """Best-effort extraction of an event list from a mapping argument."""
    if isinstance(doc.get('events'), list):
        return doc['events']
    # Plain {controller_id: version} map.
    if doc and all(isinstance(v, str) for v in doc.values()):
        return [{'controller_id': k, 'version': v} for k, v in doc.items()]
    return []


def readiness(events=None, target=None, *, controllers=None, matrix=None,
              require_boot_verification=False, **_):
    """Rich readiness report.  :func:`ready` is its boolean projection."""
    # HardwareBank / capture-service style object -> full proof path.
    if hasattr(events, 'state') and isinstance(getattr(events, 'state'), dict) \
            and hasattr(events, 'read_partitions'):
        from .firmware_capture import assess as _assess
        verdict = _assess(events, matrix=matrix)
        return {
            'ready': verdict['can_dispatch'],
            'reasons': verdict['reasons'],
            'versions': verdict['controller_versions'],
            'assessment': verdict,
        }

    # A bare event sequence.
    if isinstance(events, (list, tuple)):
        event_list = list(events)
        events = None
    else:
        event_list = None

    # Polymorphic first argument: fixture/capture/verdict/hardware/map.
    embedded_verdict = None
    if isinstance(events, dict):
        doc = events
        if target is None:
            target = doc.get('target')
        if isinstance(doc.get('events'), list):
            # Explicit report stream always drives the lightweight ready().
            event_list = doc['events']
        elif any(k in doc for k in ('roadworthy', 'can_dispatch')):
            embedded_verdict = doc
        elif 'controllers' in doc or 'hardware' in doc:
            # Full hardware/fixture proof path (also tolerates report shapes).
            from .firmware_capture import assess as _assess
            verdict = _assess(doc, matrix=matrix)
            return {
                'ready': verdict['can_dispatch'],
                'reasons': verdict['reasons'],
                'versions': verdict['controller_versions'],
                'assessment': verdict,
            }
        else:
            event_list = _events_from_doc(doc)

    target = target or 'v2'

    if embedded_verdict is not None:
        ok = bool(embedded_verdict.get(
            'can_dispatch', embedded_verdict.get('roadworthy')))
        return {
            'ready': ok,
            'reasons': [] if ok else ['embedded_verdict_grounded'],
            'versions': embedded_verdict.get('controller_versions')
            or embedded_verdict.get('versions') or {},
            'assessment': embedded_verdict,
        }

    events = event_list if event_list is not None else (events or [])
    state = versions(events)
    latest = latest_events(events or [])
    reasons = []

    if not state:
        reasons.append('no_controller_reports')

    expected = set(controllers) if controllers else None
    if expected is None and ('ECU1' in state or 'ECU2' in state):
        # This machine always has both canonical controllers.
        expected = set(CANONICAL_CONTROLLERS)
    if expected is not None:
        missing = sorted(expected - set(state))
        if missing:
            reasons.append('missing_controller_report:' + ','.join(missing))

    mismatched = sorted(
        f'{cid}={version}' for cid, version in state.items()
        if version != target
    )
    if mismatched:
        reasons.append('controller_version_not_target:' + ','.join(mismatched))

    boot_bad = []
    boot_unknown = []
    for cid, event in latest.items():
        verdict = _event_boot(event)
        if verdict is False:
            boot_bad.append(cid)
        elif verdict is None:
            boot_unknown.append(cid)
    if boot_bad:
        reasons.append('boot_self_test_failed:' + ','.join(sorted(boot_bad)))
    if require_boot_verification and boot_unknown:
        reasons.append('boot_verification_missing:'
                       + ','.join(sorted(boot_unknown)))

    entry = None
    if matrix is not None and state:
        from .firmware_capture import verify_matrix, lookup_matrix
        matrix_report = verify_matrix(matrix)
        if not matrix_report['ok']:
            reasons.extend(matrix_report['errors'])
        else:
            entry = lookup_matrix(state, matrix)
            if entry is None:
                reasons.append('mixed_combination_unconfirmed')

    return {
        'ready': not reasons,
        'reasons': reasons,
        'versions': state,
        'matrix_entry': entry,
        'boot_verified': {
            cid: _event_boot(event) for cid, event in latest.items()
        },
    }


def ready(events=None, target=None, **kwargs):
    """Whole-machine readiness.

    ``False`` for the fixed mixed ECU1=v2 / ECU2=v1 breakpoint, and ``True``
    only once every controller reports the target (with no failed boot
    evidence; and a certified matrix entry when ``matrix`` is supplied).
    """
    return readiness(events, target, **kwargs)['ready']


def assess(source=None, **kwargs):
    """Delegate the full offline proof to :mod:`firmware_capture`."""
    from .firmware_capture import assess as _assess
    return _assess(source, **kwargs)


def fleet_ready(machines, target='v2', **kwargs):
    """Aggregate ``{machine_id: events-or-state}`` without hiding groundings."""
    out = {}
    all_ready = True
    for machine_id, payload in machines.items():
        result = readiness(payload, target, **kwargs)
        out[machine_id] = result
        all_ready = all_ready and result['ready']
    return {'ready': all_ready, 'machines': out}


# Aliases for alternative call styles.
evaluate = readiness
decide = readiness
is_ready = ready
fleet_readiness = readiness

