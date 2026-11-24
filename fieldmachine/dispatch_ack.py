"""Round- and content-fenced emergency recall acknowledgement status."""
from __future__ import annotations

from .incident import evaluate, prepare


def detail_status(incident, acknowledgements, **options):
    """Return the full requirement evaluation without changing ``status``."""
    return evaluate(incident, acknowledgements, **options)


def status(incident, acknowledgements, **options):
    """Return ``acknowledged`` only when every current role gap is closed.

    Controller acknowledgement is not enough: the controller requirement needs
    explicit stopped evidence for the current round/content/token fence.
    """
    return evaluate(incident, acknowledgements, **options)["status"]


__all__ = ["status", "detail_status", "prepare"]
