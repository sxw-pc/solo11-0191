"""Prepare emergency machine recall commands."""


def prepare(incident):
    return {'incident_id':incident['incident_id'],'commands':incident['commands']}
