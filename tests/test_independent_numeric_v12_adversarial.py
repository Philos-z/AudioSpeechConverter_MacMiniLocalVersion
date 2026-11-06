"""Independent checks that new typed forms preserve factual operators."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
from proxy_client import invariants


class IndependentNumericV12AdversarialTests(unittest.TestCase):
    def test_fraction_comparison_cannot_be_dropped(self):
        for operator in ('Weniger als', 'Mindestens', 'Mehr als', 'Höchstens', 'Knapp'):
            with self.subTest(operator=operator):
                self.assertTrue(invariants(operator + ' zwei Drittel der Menschen haben gewählt.',
                                           '三分之二的人已投票。'))

    def test_chinese_fraction_comparison_cannot_be_added(self):
        for operator in ('不到', '至少', '超过', '至多'):
            with self.subTest(operator=operator):
                self.assertTrue(invariants('Zwei Drittel der Menschen haben gewählt.',
                                           operator + '三分之二的人已投票。'))

    def test_negating_scarce_hour_is_not_less_than_one_hour(self):
        self.assertTrue(invariants('Es dauert nicht eine knappe Stunde.', '用时不到1小时。'))


if __name__ == '__main__':
    unittest.main()
