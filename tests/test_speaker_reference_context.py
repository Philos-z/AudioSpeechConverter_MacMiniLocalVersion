"""Source-bound short-reference selection without widening speaker identity."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from speaker_plan import reference_window


class ReferenceContextTests(unittest.TestCase):
    def rows(self,previous_end=884.76,next_start=888.24):
        return [
            {'segment_id':'before','start_s':881.54,'end_s':previous_end,'speaker_id':'A'},
            {'segment_id':'short','start_s':885.38,'end_s':886.04,'speaker_id':'B'},
            {'segment_id':'after','start_s':next_start,'end_s':893.52,'speaker_id':'C'}]

    def select(self,rows):
        return reference_window({**rows[1],'indices':[1]},rows,1027.5)

    def test_actual_isolated_tail_keeps_own_label_and_complete_native_context(self):
        rows=self.rows();original=[r.copy() for r in rows]
        start,end,receipt=self.select(rows)
        self.assertAlmostEqual(start,885.11)
        self.assertAlmostEqual(end,886.31)
        self.assertEqual(rows,original)
        self.assertEqual(receipt['source_ids'],['short'])
        self.assertFalse(receipt['speaker_labels_changed'])
        self.assertEqual(receipt['synthetic_padding_samples'],0)

    def test_adjacent_different_speakers_are_never_borrowed(self):
        rows=self.rows(previous_end=885.35,next_start=886.07)
        start,end,receipt=self.select(rows)
        self.assertIsNone(receipt)
        self.assertLess(end-start,.8)
        self.assertGreaterEqual(start,rows[0]['end_s'])
        self.assertLessEqual(end,rows[2]['start_s'])

    def test_overlap_and_tiny_fragments_cannot_be_made_valid_with_silence(self):
        for changes in ({'overlap':True},{'end_s':885.7}):
            rows=self.rows();rows[1].update(changes)
            if changes.get('overlap'):
                with self.assertRaisesRegex(ValueError,'overlapping_speech'):self.select(rows)
            else:self.assertIsNone(self.select(rows)[2])
        rows=self.rows(previous_end=885.4)
        with self.assertRaisesRegex(ValueError,'overlapping_speech'):self.select(rows)

    def test_nearby_speech_cannot_trim_the_original_word_to_gain_context(self):
        for rows in (self.rows(previous_end=885.37),self.rows(next_start=886.05)):
            self.assertIsNone(self.select(rows)[2])

    def test_source_ends_and_neighbour_boundaries_limit_real_context(self):
        rows=self.rows(previous_end=885.2)
        start,end,receipt=self.select(rows)
        self.assertAlmostEqual(start,885.23)
        self.assertLessEqual(end,rows[2]['start_s']-.03)
        row={'segment_id':'edge','start_s':0,'end_s':.66}
        start,end,receipt=reference_window({**row,'indices':[0]},[row],.75)
        self.assertIsNone(receipt)
        self.assertLessEqual(end,.75)

    def test_existing_long_reference_window_is_identical(self):
        row={'segment_id':'long','start_s':2,'end_s':13}
        start,end,receipt=reference_window({**row,'indices':[0]},[row],20)
        self.assertEqual((start,end),(1.97,9.97))
        self.assertIsNone(receipt)

    def test_baseline_and_failed_expansion_do_not_borrow_neighbour_words(self):
        for end in (886.04,888.):
            rows=self.rows(previous_end=885.379,next_start=end+.001)
            rows[1]['end_s']=end
            start,stop,context=self.select(rows)
            self.assertGreaterEqual(start,rows[0]['end_s'])
            self.assertLessEqual(stop,rows[2]['start_s'])
            self.assertLessEqual(start,rows[1]['start_s'])
            self.assertGreaterEqual(stop,rows[1]['end_s'])

    def test_non_finite_times_and_empty_assignments_are_rejected(self):
        for key in ('start_s','end_s'):
            rows=self.rows();rows[1][key]=float('nan')
            with self.assertRaisesRegex(ValueError,'non_finite_time'):self.select(rows)
        with self.assertRaisesRegex(ValueError,'empty_assignments'):
            reference_window({'start_s':0,'end_s':1,'indices':[]},[],2)


if __name__=='__main__':unittest.main()
