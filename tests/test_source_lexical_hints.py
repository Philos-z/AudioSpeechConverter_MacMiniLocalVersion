import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'web'))
from source_audio_recheck import lexical_hints


class SourceLexicalHintTests(unittest.TestCase):
    def test_both_transcripts_and_financial_source_context_required(self):
        exact = 'Deswegen muss der Geldhang zugemacht werden.'
        self.assertEqual(lexical_hints(exact, exact, 'Es gibt keinen Länderfinanzausgleich.'), ['Geldhahn'])
        self.assertEqual(lexical_hints(exact, 'Es ist nicht zu verstehen.', 'Finanzausgleich'), [])
        self.assertEqual(lexical_hints(exact, exact, 'Die Landschaft wurde beschrieben.'), [])

    def test_no_generic_spelling_or_factual_replacement(self):
        self.assertEqual(lexical_hints('700 Menschen','700 Menschen','Finanzierung'), [])
        self.assertEqual(lexical_hints('Geldhang geöffnet werden','Geldhang geöffnet werden','Finanzierung'), [])
        self.assertEqual(lexical_hints('Geldhahn zugemacht werden','Geldhahn zugemacht werden','Finanzierung'), [])


if __name__ == '__main__':
    unittest.main()
