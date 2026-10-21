"""Generic regression fixtures; no episode transcripts or private media."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from proxy_client import invariants


class QuantityGuardTests(unittest.TestCase):
    def test_spelled_german_channel_name_is_equivalent(self):
        self.assertEqual(invariants('Das erste deutsche Fernsehen berichtet.','德国第一电视台报道。'),[])
        self.assertEqual(invariants('Das zweite deutsche Fernsehen berichtet.','德国电视2台报道。'),[])

    def test_wrong_channel_number_is_rejected(self):
        self.assertTrue(invariants('Das erste deutsche Fernsehen berichtet.','德国第2电视台报道。'))

    def test_real_quantities_remain_protected(self):
        self.assertEqual(invariants('Es sind 3 Kilometer.','距离为3公里。'),[])
        self.assertTrue(invariants('Es sind 3 Kilometer.','距离为4公里。'))
        self.assertTrue(invariants('Es sind 3 Kilometer.','距离为3米。'))

    def test_negation_and_condition_are_not_relaxed(self):
        self.assertTrue(invariants('Die Tür ist nicht offen.','门开着。'))
        self.assertTrue(invariants('Wenn es regnet, bleiben wir hier.','我们留在这里。'))
        self.assertEqual(invariants('Wenn es regnet, bleiben wir hier.','如果下雨，我们就留在这里。'),[])


if __name__=='__main__':unittest.main()
