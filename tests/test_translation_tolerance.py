"""Regression coverage for evidenced DE/ZH forms; no network or media model."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
from proxy_client import invariants


class TranslationToleranceTests(unittest.TestCase):
    def accepted(self, source, target, context=None):
        self.assertEqual(invariants(source, target, previous_context=context), [])

    def rejected(self, source, target, context=None):
        self.assertTrue(invariants(source, target, previous_context=context))

    def test_explicit_person_roles_and_bounded_adjectives(self):
        for source,target in [
            ('Bei der Schulung sitzen rund 240 Delegierte.', '培训现场坐着约240名代表。'),
            ('Drei deutsche Angreifer hätten vier Soldaten erschossen und fünf verletzt.',
             '据称，3名德国袭击者打死4名士兵、打伤5人。'),
            ('Sechs Länderchefs prüfen den Vorschlag.', '6位州长研究这个建议。'),
            ('Drei CSUler in den Ämtern geht nicht.', '3名基社盟人士担任这些职务，这是行不通的。'),
        ]:
            with self.subTest(source=source):self.accepted(source,target)
        for source,target in [
            ('240 Delegierte kamen.', '来了240欧元。'),
            ('240 Delegierte kamen.', '来了241名代表。'),
            ('Drei Autos kamen.', '来了3人。'),
            ('Drei CSU-Lahe betreten den Saal.', '来了3人。'),
        ]:
            with self.subTest(source=source):self.rejected(source,target)

    def test_bare_casualties_keep_both_counts_and_competing_referents(self):
        source='Einer wurde getötet, sechs schwer verletzt.'
        self.accepted(source,'1人被打死，6人重伤。')
        self.rejected(source,'1人被打死，7人重伤。')
        self.rejected(source,'6人重伤。')
        self.rejected(source,'1人被打死，6人重伤。','Die Tiere waren im Stall.')

    def test_temporal_year_grammar_preserves_value_and_dimension(self):
        self.accepted('Die 42.000 Euro, die 2016 von einem deutschen Verein flossen.',
                      '这42,000欧元于2016年由一家德国协会转来。')
        self.accepted('2021 verkauft Firma Alpha das letzte Gerät.','2021年，阿尔法公司卖出最后一台设备。')
        for source,target in [
            ('2021 verkauft Firma Alpha das letzte Gerät.','2022年，阿尔法公司卖出最后一台设备。'),
            ('2021 Euro verkauft Firma Alpha.','2021年，阿尔法公司销售。'),
            ('Die Nummer 2021 wurde gewählt.','选择了2021年。'),
            ('Die Nummer 2021 verkauft Firma Alpha.','阿尔法公司在2021年销售。'),
            ('Es sind 2016 Schrauben.','这是2016年。'),
        ]:
            with self.subTest(source=source):self.rejected(source,target)

    def test_dative_decade_and_year_are_distinct(self):
        source='In den späten 1990ern spielte man Karten.'
        self.accepted(source,'在1990年代末，人们打牌。')
        self.accepted(source,'在20世纪90年代末，人们打牌。')
        self.rejected(source,'在2000年代末，人们打牌。')
        self.rejected(source,'1990年，人们打牌。')

    def test_named_war_is_not_arbitrary_digit_removal(self):
        source='Eine Übung behandelt den Sechs-Tage-Krieg.'
        self.accepted(source,'课程练习涉及六日战争。')
        self.rejected(source,'课程练习涉及这场战争。')
        self.rejected(source,'课程练习涉及五日战争。')
        self.rejected('Eine Übung zum Sechs-Tage-Krieg zählt 9 Menschen.', '六日战争练习统计了10人。')
        self.rejected('Eine Übung behandelt den Sieben-Tage-Krieg.', '课程练习涉及六日战争。')

    def test_one_thing_is_a_count_and_not_an_extra_person(self):
        source='Im Versuch zählt vor allem eins.'
        self.accepted(source,'实验中最看重的只有一件事。')
        self.accepted(source,'实验中最看重的只有1件事。')
        self.rejected(source,'实验中最看重的是两件事。')
        self.rejected(source,'实验中最看重的只有1人。')

    def test_post_quantity_more_preserves_comparison_and_unit(self):
        source='Ein Mustergerät kostet mehr als 320 Euro.'
        self.accepted(source,'样机要320多欧元。')
        self.accepted(source,'样机要320余欧元。')
        for target in ['样机要320欧元。','样机要320多美元。','样机要321多欧元。','样机要不到320欧元。']:
            with self.subTest(target=target):self.rejected(source,target)
        self.rejected('Ein Mustergerät kostet 320 Euro.','样机要320多欧元。')

    def test_concessive_pair_cannot_hide_a_separate_condition(self):
        self.accepted('Auch wenn der Testgast heute zu Besuch ist.','尽管测试访客今天在场。')
        self.accepted('Auch wenn es regnet.','即使下雨。')
        self.rejected('Wenn es regnet, bleiben wir.','下雨了，我们留下。')
        self.rejected('Auch wenn es regnet.','下雨了。')
        self.rejected('Auch wenn er kommt, wenn es regnet, bleiben wir.','尽管他来了，我们留下。')

    def test_colloquial_conditional_keeps_the_consequence_conditional(self):
        self.accepted('Wenn keine Daten vorliegen, müssen wir warten.','没有数据的话，我们必须等待。')
        self.rejected('Wenn keine Daten vorliegen, müssen wir warten.','没有数据，我们必须等待。')
        self.rejected('Wenn keine Daten vorliegen, müssen wir warten.','有数据的话，我们必须等待。')

    def test_stagnation_pair_does_not_hide_remaining_negation(self):
        self.accepted('Die Testarbeiten kommen derzeit nicht voran.','测试工作目前进展停滞。')
        self.rejected('Die Testarbeiten kommen derzeit nicht voran.','测试工作目前进展顺利。')
        self.rejected('Er lernt nicht. Die Testarbeiten kommen nicht voran.','他在学习。测试工作进展停滞。')

    def test_unprecedented_since_pair_keeps_scope_and_other_negations(self):
        source='Das Beispiel beschreibt einen Anstieg wie nie seit dem Sechs-Tage-Krieg.'
        self.accepted(source,'示例描述的增幅是六日战争以来规模空前的。')
        self.rejected(source,'示例描述的增幅出现在六日战争以来。')
        self.rejected(source,'示例描述的增幅是规模空前的。')
        self.rejected('Er kommt nicht. '+source,'他来了。示例描述的增幅是六日战争以来规模空前的。')

    def test_asr_garbage_still_needs_source_recovery(self):
        self.rejected('Die Tür wurde wie nie später geöffnet.',
                      '门稍后被打开。')


if __name__=='__main__':
    unittest.main()
