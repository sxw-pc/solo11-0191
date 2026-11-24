"""Collect supplier quote cards from offline page snapshots."""

def quotes(cards):
    return [card for card in cards if card.get('part') and card.get('price') is not None]
