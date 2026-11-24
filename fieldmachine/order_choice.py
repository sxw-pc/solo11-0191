"""Choose a procurement offer from collected quote cards."""
from .catalog import quotes


def choose(cards, part):
    offers = [card for card in quotes(cards) if card['part'] == part]
    return min(offers, key=lambda card: (card['price'], card['supplier'])) if offers else None
