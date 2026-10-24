"""Real Pillow layout geometry and ASS compatibility; no models or task edits."""
import copy
import importlib.util
import math
from pathlib import Path
import re
import tempfile
import unittest

from PIL import ImageFont

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('layout_fit_compact',ROOT/'scripts/compact_edit.py')
compact=importlib.util.module_from_spec(spec);spec.loader.exec_module(compact)


class SubtitleLayoutFitTests(unittest.TestCase):
    def assertSafe(self,layout,zh,de):
        self.assertGreaterEqual(layout['zh_top_y'],240)
        self.assertEqual(layout['de_bottom_y'],520)
        self.assertEqual(layout['de_top_y']-layout['zh_bottom_y'],10)
        self.assertLessEqual(layout['line_counts']['zh'],3)
        self.assertLessEqual(layout['line_counts']['de'],5)
        for lang,original,low,high in [('zh',zh,30,34),('de',de,20,24)]:
            size=layout['font_sizes'][lang]
            self.assertIn(size,range(low,high+1))
            font=ImageFont.truetype(compact.FONT,size)
            lines=layout[lang+'_lines']
            self.assertEqual(''.join(''.join(lines).split()),''.join(original.split()))
            self.assertEqual(layout['line_counts'][lang],len(lines))
            actual=[float(font.getlength(line)) for line in lines]
            self.assertEqual(layout['line_widths_px'][lang],actual)
            self.assertTrue(all(width<=900 for width in actual))

    def test_short_cue_keeps_default_sizes_and_existing_ass_event_bytes(self):
        row={'segment_id':'line0001','text_zh':'你好。','text_de':'Guten Abend.',
             'output_start_s':1.23,'output_end_s':3.45}
        original=copy.deepcopy(row)
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'short.ass'
            result=compact.build_compact_ass({'utterances':[row]},target)
            source=target.read_text()
        events=[line for line in source.splitlines() if line.startswith('Dialogue:')]
        self.assertEqual(events,[
            r'Dialogue: 0,0:00:01.23,0:00:03.45,Chinese,line0001,0,0,0,,{\an2\pos(480,477)\q2}你好。',
            r'Dialogue: 0,0:00:01.23,0:00:03.45,German,line0001,0,0,0,,{\an2\pos(480,520)\q2}Guten Abend.'])
        self.assertIn('Style: Chinese,Arial Unicode MS,34,',source)
        self.assertIn('Style: German,Arial Unicode MS,24,',source)
        self.assertNotIn(r'\fs',source)
        self.assertFalse(result['placements'][0]['layout_adjusted'])
        self.assertEqual(row,original)

    def test_long_cue_allows_three_chinese_lines_with_explicit_local_sizes(self):
        zh='中'*62;de='Das Gericht hat die Entscheidung in dieser Sache begründet. '*5
        layout=compact.fit_subtitle_layout(zh,de,segment_id='long')
        self.assertSafe(layout,zh,de)
        self.assertTrue(layout['adjusted'])
        self.assertEqual(layout['line_counts']['zh'],3)
        row={'segment_id':'long','text_zh':zh,'text_de':de,'output_start_s':0.,'output_end_s':6.}
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'long.ass';report=compact.build_compact_ass({'utterances':[row]},path)
            events=[line for line in path.read_text().splitlines() if line.startswith('Dialogue:')]
        self.assertEqual(len(events),2)
        self.assertIn(r'\fs'+str(layout['font_sizes']['zh']),events[0])
        self.assertIn(r'\fs'+str(layout['font_sizes']['de']),events[1])
        placement=report['placements'][0]
        self.assertEqual(placement['font_sizes'],layout['font_sizes'])
        self.assertEqual(placement['line_widths_px'],layout['line_widths_px'])
        self.assertEqual(placement['de_top_y']-placement['zh_bottom_y'],10)

    def test_minimum_font_combination_fits_exact_three_and_five_line_limits(self):
        # A 31px Chinese font holds at most 29 of these 31px glyphs per line;
        # 88 glyphs require 30px to stay at three lines. Force source font 20px
        # analogously, independently from the layout selector.
        source_font=ImageFont.truetype(compact.FONT,21)
        source_count=5*math.floor(900/source_font.getlength('W'))+1
        zh='中'*88;de='W'*source_count
        layout=compact.fit_subtitle_layout(zh,de,segment_id='limits')
        self.assertSafe(layout,zh,de)
        self.assertEqual(layout['font_sizes'],{'zh':30,'de':20})
        self.assertEqual(layout['line_counts'],{'zh':3,'de':5})
        self.assertEqual(layout['zh_top_y'],244)

    def test_chosen_combination_maximizes_readable_font_sizes(self):
        zh='中'*62;de='Ein ausführlicher deutscher Satz mit mehreren langen Wörtern. '*5
        layout=compact.fit_subtitle_layout(zh,de)
        feasible=[]
        for z in range(30,35):
            for d in range(20,25):
                zf=ImageFont.truetype(compact.FONT,z);df=ImageFont.truetype(compact.FONT,d)
                zl=compact._wrap(zh,zf,'zh',900);dl=compact._wrap(de,df,'de',900)
                top=520-len(dl)*sum(df.getmetrics())-10-len(zl)*sum(zf.getmetrics())
                if len(zl)<=3 and len(dl)<=5 and top>=240:
                    feasible.append((z+d,z,d))
        self.assertEqual((sum(layout['font_sizes'].values()),layout['font_sizes']['zh'],
                          layout['font_sizes']['de']),max(feasible))

    def test_single_long_source_word_is_broken_without_losing_characters(self):
        zh='完整原文。';de='Donaudampfschifffahrtsgesellschaftskapitän'*6
        layout=compact.fit_subtitle_layout(zh,de)
        self.assertSafe(layout,zh,de)
        self.assertEqual(''.join(layout['de_lines']),de)

    def test_punctuation_numbers_case_and_order_are_preserved(self):
        zh='法院说：“若有9980亿欧元，至少保留3人，不是所有人。”'*3
        de='Das Gericht sagte: „Wenn 998 Milliarden Euro verfügbar sind, bleiben mindestens 3 Personen – nicht alle.“ '*3
        layout=compact.fit_subtitle_layout(zh,de)
        self.assertSafe(layout,zh,de)
        for lang,text in [('zh',zh),('de',de)]:
            self.assertEqual(re.sub(r'\s','',text),re.sub(r'\s','',''.join(layout[lang+'_lines'])))

    def test_more_than_three_or_five_lines_at_minimum_size_fails_without_truncation(self):
        source_font=ImageFont.truetype(compact.FONT,20)
        source_count=5*math.floor(900/source_font.getlength('W'))+1
        for zh,de in [('中'*91,'Kurzer Satz.'),('完整句子。','W'*source_count)]:
            with self.subTest(zh=len(zh),de=len(de)),self.assertRaisesRegex(ValueError,'cannot fit.*line9999'):
                compact.fit_subtitle_layout(zh,de,segment_id='line9999')

    def test_invalid_empty_subtitles_fail_explicitly(self):
        for zh,de in [('', 'Satz.'),('完整句子。','   '),(None,'Satz.')]:
            with self.subTest(zh=zh,de=de),self.assertRaisesRegex(ValueError,'invalid subtitle text'):
                compact.fit_subtitle_layout(zh,de)


if __name__=='__main__':
    unittest.main()
