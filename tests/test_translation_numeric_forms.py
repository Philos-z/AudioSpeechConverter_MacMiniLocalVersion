"""Offline checks for bounded German/Chinese comparison forms; no model/API."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
from proxy_client import clock_mentions, invariants, numeric_mentions
from numeric_calendar_context import preprocess


class TranslationNumericFormsTests(unittest.TestCase):
    def test_temperature_degree_symbol_preserves_scale_and_quantity(self):
        self.assertEqual(invariants('Morgen werden 8° und bis zu 16° erreicht.','明天8°，最高可达16°。'),[])
        self.assertEqual(invariants('Es sind 16 Grad Fahrenheit.','气温为16°F。'),[])
        self.assertTrue(invariants('Es sind 16 Grad Fahrenheit.','气温为16°。'))
        self.assertTrue(invariants('Es sind 16 Grad Celsius.','气温为16°F。'))
        self.assertTrue(invariants('Morgen werden 8° erreicht.','明天18°。'))
    def assertAccepted(self, source, target, context=None):
        self.assertEqual(invariants(source, target, previous_context=context), [])

    def assertRejected(self, source, target, context=None):
        self.assertTrue(invariants(source, target, previous_context=context))

    def test_explicit_budget_years(self):
        self.assertAccepted('Für 2027 werden Steuereinnahmen erwartet.', '预计2027年有税收收入。')
        self.assertAccepted('Für den Bundeshaushalt 2027 wird das erwartet.', '预计用于2027年联邦预算。')
        self.assertAccepted('Für den Bundeshaushalt 2027, das wird erwartet.', '预计是2027年的联邦预算。')
        self.assertRejected('Der Bundeshaushalt 2027 wird beraten.', '正在讨论2028年联邦预算。')
        self.assertRejected('Für 2027 Euro reicht es.', '2027年就够了。')
        self.assertRejected('Für 2027,50 Euro reicht es.', '2027年就够了。')
        self.assertRejected('Für 2027 Personen reicht es.', '2027年就够了。')
        self.assertRejected('Für 2027 Schrauben reicht es.', '2027年就够了。')
        self.assertRejected('Die Nummer 2027 wurde gewählt.', '选择了2027年。')
        normalized=preprocess('Für 2027 werden Einnahmen erwartet.', '预计2027年有收入。')
        self.assertTrue(any(x.get('reason')=='explicit für/Haushalt year' for x in normalized['evidence']))

    def test_condition_yaoshi(self):
        self.assertAccepted('Wenn mehr Geld reinkommt, dann ist das gut.', '要是能多收一些，那是好事。')
        self.assertRejected('Wenn mehr Geld reinkommt, dann ist das gut.', '多收了一些，这是好事。')

    def test_explicit_inflected_people_counts(self):
        self.assertAccepted('Der Fall zweier Justizvollzugsbeamter wurde entschieden.', '两名狱警一案已有裁决。')
        self.assertAccepted('Die beiden Männer schieden vor drei Jahren aus.', '两人在3年前离职。')
        self.assertRejected('Der Fall zweier Justizvollzugsbeamter wurde entschieden.', '3名狱警一案已有裁决。')
        self.assertRejected('Der Fall zweier unbekannter Geräte wurde entschieden.', '涉及两人，已有裁决。')

    def test_one_of_two_is_not_both(self):
        source='Nur einer der beiden Beamten kam zum Test.'
        self.assertAccepted(source, '两名公务员中，只有一人参加测试。')
        self.assertAccepted(source, '2名公务员中，只有1人参加测试。')
        self.assertRejected(source, '两名公务员都参加测试。')
        self.assertRejected(source, '3名公务员中，只有1人参加测试。')
        self.assertRejected(source, '只有一名公务员参加测试。')

    def test_person_ordinals_have_a_separate_dimension(self):
        source='Der dritte Beklagte war im Testraum nicht anwesend.'
        self.assertAccepted(source, '第三名被告当时不在测试室。')
        self.assertAccepted('Der 3. Beklagte war nicht anwesend.', '第3名被告不在场。')
        self.assertRejected(source, '3名被告当时不在测试室。')
        self.assertRejected(source, '第四名被告当时不在测试室。')
        self.assertRejected('Der dritte Wagen war nicht da.', '第三名被告不在场。')
        mentions=numeric_mentions('第三名被告不在场。','zh')
        self.assertEqual(mentions[0]['dimension'], 'ordinal:people')

    def test_bare_both_requires_explicit_person_antecedent(self):
        source='Bei beiden war die Probe nicht nachweisbar.'
        context='Die beiden Männer nahmen teil. Ein Test prüfte die Ergebnisse der Beamten.'
        self.assertAccepted(source, '两人都未检测到样本。', context)
        self.assertRejected(source, '两人都检测到样本。', context)
        self.assertRejected(source, '只有一人未检测到样本。', context)
        self.assertRejected(source, '两人都未检测到样本。')
        self.assertRejected(source, '两人都未检测到样本。', 'Ein Beamter stellte eine Frage.')
        self.assertRejected(source, '两人都未检测到样本。', 'Die beiden Autos wurden geprüft.')

    def test_decades_are_complete_typed_dates(self):
        source='Die Bewegung nahm in den 1970er-Jahren ihren Anfang.'
        self.assertAccepted(source, '运动始于20世纪70年代。')
        self.assertAccepted('In den 1980er Jahren änderte sich das.', '这在20世纪80年代改变了。')
        for target in ['运动始于20世纪80年代。','运动始于21世纪70年代。','运动始于1970年。','运动始于70年。']:
            with self.subTest(target=target):self.assertRejected(source,target)
        self.assertRejected('Im Jahr 1970 begann die Bewegung.', '运动始于20世纪70年代。')
        self.assertRejected('In den 1973er-Jahren begann sie.', '运动始于20世纪70年代。')

    def test_billion_is_a_trillion_and_preserves_comparison(self):
        self.assertAccepted('Es wird eine Billion Euro erwartet.', '预计1万亿欧元。')
        self.assertAccepted('Es werden zwei Billionen Euro erwartet.', '预计2万亿欧元。')
        self.assertRejected('Es wird eine Billion Euro erwartet.', '预计10亿欧元。')
        self.assertAccepted('Es werden mehr als einer Billion Euro erwartet.', '预计超过1万亿欧元。')
        self.assertRejected('Es werden mehr als einer Billion Euro erwartet.', '预计1万亿欧元。')
        self.assertRejected('Es wird eine Billion Euro erwartet.', '预计超过1万亿欧元。')
        self.assertRejected('Es werden weniger als einer Billion Euro erwartet.', '预计超过1万亿欧元。')
        self.assertAccepted('Es werden gut 4 Mrd. Euro erwartet.', '预计超过40亿欧元。')
        self.assertRejected('Es werden gut 4 Mrd. Euro erwartet.', '预计少于40亿欧元。')
        self.assertAccepted('Es werden knapp 4 Mrd. Euro erwartet.', '预计不到40亿欧元。')

    def test_knapp_preserves_the_below_direction(self):
        source='Das Rechenbeispiel erwartet knapp 733 Milliarden Euro.'
        for target in ['预计今年将略低于7330亿欧元。','预计今年将不到7330亿欧元。',
                       '预计今年将近7330亿欧元。','预计今年尚未达到7330亿欧元。']:
            with self.subTest(target=target):self.assertAccepted(source,target)
        for target in ['预计今年将接近7330亿欧元。','预计今年将达到7330亿欧元。',
                       '预计今年将超过7330亿欧元。','预计今年将略低于7340亿欧元。']:
            with self.subTest(target=target):self.assertRejected(source,target)
        self.assertRejected('Es werden 4 Mrd. Euro erwartet.', '预计接近40亿欧元。')
        self.assertRejected('Es werden gut 4 Mrd. Euro erwartet.', '预计将近40亿欧元。')
        self.assertEqual(numeric_mentions('knapp 4 Mrd. Euro','de')[0]['relation'],'lt')
        self.assertEqual(numeric_mentions('接近40亿欧元','zh')[0]['relation'],'approx')

    def test_record_equivalence_is_paired_and_narrow(self):
        source='Für 2027 wird eine Billion Euro erwartet, so viel wie noch nie.'
        self.assertAccepted(source, '预计2027年有1万亿欧元，创历史新高。')
        self.assertAccepted(source, '预计2027年有1万亿欧元，比以往任何时候都高。')
        self.assertRejected(source, '预计2027年有1万亿欧元。')
        self.assertRejected(source, '预计2027年有1万亿欧元，但没有创历史新高。')
        self.assertRejected('Noch nie wurden 2 Menschen gesehen.', '见到了2人。')
        self.assertRejected('Noch nie wurden 2 Menschen gesehen.', '见到了2人，创历史新高。')
        self.assertRejected(source, '预计2027年有10亿欧元，创历史新高。')

    def test_standard_chinese_clock_and_invalid_ueber(self):
        source='Die Sendung beginnt um 22 Uhr 15.'
        self.assertAccepted(source, '节目在22点15分开始。')
        self.assertAccepted('Die Sendung beginnt um 22:15.', '节目在22时15分开始。')
        self.assertAccepted('Die Sendung beginnt um 22:15.', '节目在22:15时开始。')
        self.assertRejected(source, '节目在22点50分开始。')
        self.assertRejected(source, '节目在22点75分开始。')
        self.assertRejected('Die Sendung beginnt um 22 über 15.', '节目在22点15分开始。')
        self.assertEqual(clock_mentions('22点15分','zh')[0][2], '22:15')
        self.assertEqual(clock_mentions('22 über 15','de'), [])

    def test_only_explicit_co2_formula_is_masked(self):
        self.assertAccepted('CO2 kommt aus dem Auspuff.', '二氧化碳从尾气排出。')
        self.assertAccepted('Es entstehen 2 kg CO₂.', '产生2公斤CO2。')
        self.assertAccepted('Der CO2-Ausstoß beträgt 2 kg.', 'CO2排放量是2公斤。')
        self.assertRejected('Es entstehen 2 kg CO2.', '产生3公斤二氧化碳。')
        self.assertRejected('CO3 kommt heraus.', '排出CO2。')
        self.assertRejected('CODECO2 ist ein Produkt.', '这是一个产品。')
        self.assertRejected('CO20 ist ein Produkt.', '这是一个产品。')

    def test_paired_confirmation_tags_are_not_factual_negation(self):
        self.assertAccepted('Verrückt zu sein, nicht? Hier müssen wir alle verschwinden.',
                            '疯疯癫癫的，是吧？我们都得从这儿消失。')
        for source,target in [
            ('Das stimmt, nicht?', '这是真的，对吧？'),
            ('Das stimmt, nicht wahr?', '这是真的，是吧？'),
            ('Das stimmt, nicht wahr?', '这是真的，不是吗？'),
            ('Du kommst, nicht?', '你会来吧？'),
            ('2 Menschen sind anwesend, nicht?', '2人在场，对吧？'),
            ('Das stimmt, nicht? Er kommt, nicht wahr?', '这是真的，对吧？他会来，不是吗？'),
        ]:
            with self.subTest(source=source,target=target):self.assertAccepted(source,target)

    def test_confirmation_tag_does_not_hide_remaining_negation(self):
        self.assertAccepted('Du bist nicht verrückt, nicht wahr?', '你没疯，对吧？')
        self.assertAccepted('2 Menschen sind nicht anwesend, nicht?', '2人不在场，是吧？')
        for source,target in [
            ('Du bist nicht verrückt, nicht wahr?', '你疯了，不是吗？'),
            ('2 Menschen sind nicht anwesend, nicht?', '2人在场，不是吗？'),
            ('Er kommt, nicht? Er ist nicht krank.', '他会来吧？他病了。'),
            ('Er kommt nicht?', '他会来吧？'),
            ('Er kommt, nicht heute?', '他今天会来吧？'),
            ('Das ist nicht wahr.', '这是真的，对吧？'),
            ('Er kommt, nicht?', '他会来吧。'),
            ('Er kommt, nicht?', '他会来。'),
            ('2 Menschen sind anwesend, nicht?', '3人在场，对吧？'),
            ('Kommt er? Das stimmt, nicht?', '这是真的，对吧？'),
        ]:
            with self.subTest(source=source,target=target):self.assertRejected(source,target)


if __name__=='__main__':
    unittest.main()
