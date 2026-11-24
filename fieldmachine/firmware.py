"""Collect offline controller firmware inventory and boot evidence.

``versions`` intentionally keeps only the last reported version per
``controller_id``.  It is a raw report projection, not a roadworthy verdict:
a single controller reaching the target version says nothing about the
second controller or the signed compatibility matrix.
"""

BOOT_FLAG_KEYS = (
    'boot_verified', 'boot_confirmed', 'boot_ok',
    'verified', 'self_test_ok', 'self_test_passed',
)
BOOT_STATE_KEYS = ('self_test', 'self_check', 'boot_self_test')


def versions(events):
    """Last reported ``version`` for every controller, in report order."""
    result = {}
    for event in events:
        result[event['controller_id']] = event['version']
    return result


def latest_events(events):
    """Last report event object per controller (same overwrite rule)."""
    result = {}
    for event in events or []:
        result[event['controller_id']] = event
    return result


def boot_verified(event):
    """Return True/False when a report carries boot evidence, else None.

    Missing evidence is deliberately ``None`` (unknown) rather than False so
    that plain version reports keep working; an explicit failed flag is a
    hard failure.
    """
    if not isinstance(event, dict):
        return None
    seen = False
    for key in BOOT_FLAG_KEYS:
        if key in event:
            seen = True
            if event[key] is False or event[key] in (0, '0', '', 'false', 'False', 'no'):
                return False
    for key in BOOT_STATE_KEYS:
        if key in event:
            seen = True
            value = event[key]
            if value is False:
                return False
            text = str(value).strip().lower()
            if text in ('fail', 'failed', 'error', 'nok', 'not_passed'):
                return False
            if text in ('pass', 'passed', 'ok', 'true'):
                continue
    return True if seen else None
