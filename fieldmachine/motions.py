"""Provisional cooperative motion vote count."""
from .members import shares


def passed(member_events, ballots):
    weights=shares(member_events)
    yes=sum(weights[row['member_id']] for row in ballots if row['vote']=='yes')
    no=sum(weights[row['member_id']] for row in ballots if row['vote']=='no')
    return yes>no
