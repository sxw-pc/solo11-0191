"""Provisional recall acknowledgement status."""
from .incident import prepare


def status(incident, acknowledgements):
    planned=prepare(incident)
    return 'acknowledged' if any(a['incident_id']==planned['incident_id'] for a in acknowledgements) else 'pending'
