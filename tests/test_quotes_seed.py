import json
import unittest
from pathlib import Path
from fieldmachine.order_choice import choose


class QuoteSeedTest(unittest.TestCase):
    def test_cheapest_visible_card_wins(self):
        fixture = json.loads((Path(__file__).parents[1] / 'fixtures' / 'quotes-ok.json').read_text())
        self.assertEqual(choose(fixture['cards'], fixture['part'])['supplier'], 'A')


if __name__ == '__main__':
    unittest.main()
