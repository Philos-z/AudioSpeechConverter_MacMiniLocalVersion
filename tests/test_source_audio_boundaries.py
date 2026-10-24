import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from source_audio_recheck import slot_tail_end,slot_head_start,exact_window_matches

class SourceBoundaryTests(unittest.TestCase):
 def setUp(self):
  self.row={'start_s':778.06,'end_s':782.86,'slot_end_s':783.42,'overlap':False}
  self.rows=[self.row,{'start_s':783.48,'end_s':785.78}]
  self.proof={'window_start_s':778.06,'window_end_s':783.42,'original_end_s':782.86,
    'method':'complete_existing_source_slot_without_next_sentence','no_next_sentence_included':True}
 def test_tail_word_can_finish_inside_existing_slot(self):
  self.assertTrue(exact_window_matches(self.proof,self.row,self.rows,924))
 def test_neighbor_or_unallocated_context_is_never_exact(self):
  for end in (783.421,783.48,783.86):
   with self.subTest(end=end):
    value={**self.proof,'window_end_s':end}
    self.assertFalse(exact_window_matches(value,self.row,self.rows,924))
 def test_overlap_large_gap_and_changed_next_boundary_reject_extension(self):
  for changes,next_start in (({'overlap':True},783.48),({'slot_end_s':784.0},784.5),({},783.0)):
   row={**self.row,**changes};rows=[row,{'start_s':next_start,'end_s':785.78}]
   self.assertEqual(slot_tail_end(row,rows,924),row['end_s'])
   self.assertFalse(exact_window_matches(self.proof,row,rows,924))
 def test_context_claim_without_boundary_provenance_is_rejected(self):
  for key in ('method','no_next_sentence_included','original_end_s'):
   value=copy.deepcopy(self.proof);value.pop(key)
   self.assertFalse(exact_window_matches(value,self.row,self.rows,924))
 def test_original_exact_window_is_still_valid(self):
  self.assertTrue(exact_window_matches({'window_start_s':778.06,'window_end_s':782.86},self.row,self.rows,924))
 def test_missing_first_word_uses_only_allocated_gap(self):
  row={'start_s':617.1,'end_s':620.82,'slot_end_s':621.16,'overlap':False}
  rows=[{'start_s':611.82,'end_s':615.28,'slot_end_s':616.08,'overlap':False},row,{'start_s':621.22,'end_s':625}]
  proof={'window_start_s':616.1,'window_end_s':621.16,'original_start_s':617.1,'original_end_s':620.82,
   'method':'complete_existing_source_slot_without_neighbor_speech','no_neighbor_sentence_included':True}
  self.assertTrue(exact_window_matches(proof,row,rows,924))
  for start in (616.08,615.28,611.82):
   self.assertFalse(exact_window_matches({**proof,'window_start_s':start},row,rows,924))
  self.assertFalse(exact_window_matches({**proof,'window_end_s':621.22},row,rows,924))
 def test_previous_overlap_or_invalid_slot_forbids_head_extension(self):
  for previous in ({'start_s':770.,'end_s':777.,'slot_end_s':777.5,'overlap':True},
                   {'start_s':770.,'end_s':777.,'slot_end_s':776.,'overlap':False}):
   self.assertEqual(slot_head_start(self.row,[previous,self.row]),self.row['start_s'])
if __name__=='__main__':unittest.main()
