"""Provisional machine title transfer decisions."""
from .asset_titles import owners


def accepted(title_events, sales):
    current=owners(title_events)
    return {sale['sale_id']:current[sale['machine_id']]==sale['seller'] for sale in sales}
