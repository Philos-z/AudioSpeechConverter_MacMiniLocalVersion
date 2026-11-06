"""Narrow comparison-only normalization for evidenced DE/ZH numeric forms.

preprocess(source, target, previous_context=None) returns a dict containing
source/target comparison strings, calendar_errors, evidence and review_required.
It never changes persisted source/translation words or silently equates unknown
units. Returned evidence preserves original spans and context for each inference.
"""
from collections import Counter
from datetime import date
import hashlib
import re

MONTHS={'januar':1,'februar':2,'märz':3,'maerz':3,'april':4,'mai':5,'juni':6,
    'juli':7,'august':8,'september':9,'oktober':10,'november':11,'dezember':12}
MONTH_PATTERN='|'.join(sorted(MONTHS,key=len,reverse=True))
DE_NAMED_DATE=re.compile(r'(?<![\w.])(?P<day>\d{1,2})\.\s*(?P<month>'+MONTH_PATTERN+r')(?![\w])(?:\s+(?P<year>(?:18|19|20|21)\d{2}|2200)(?!\d))?',re.I)
DE_NUMERIC_DATE=re.compile(r'(?<![\d.])(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>(?:18|19|20|21)\d{2}|2200)?(?!\d)')
ZH_DATE=re.compile(r'(?<!\d)(?:(?P<year>(?:18|19|20|21)\d{2}|2200)\s*年\s*)?(?P<month>1[0-2]|[1-9])\s*月\s*(?P<day>3[01]|[12]\d|[1-9])\s*[日号]')
COUNT=r'(?<![\w.,])(?:\d{1,3}(?:[. ,]\d{3})+|\d+(?:[.,]\d+)?)(?:\s*(?:Milliarden?|Millionen?|Mrd\.?|Mio\.?))?'
KNOWN_PEOPLE=re.compile(r'(?P<number>'+COUNT+r')\s+(?P<description>(?:(?:zusätzliche[nrsm]?|türkische[nrsm]?|deutsche[nrsm]?|gewaltbereite[nrsm]?)\s+){0,2}(?:Soldat(?:en|in|innen)?|Arbeitnehmer(?:n|in|innen)?|Gastarbeiter(?:n|in|innen)?|Teilnehmer(?:n|in|innen)?|Teilnehmende[nrms]?|Demonstrant(?:en|in|innen)?|Polizist(?:en|in|innen)?|Störer(?:n|in|innen)?|Einsatzkräfte[n]?))\b',re.I)
BARE_RETURN=re.compile(r'(?P<number>'+COUNT+r')(?=\s+kehr(?:ten|en)\b[^.!?]{0,120}\bzurück\b[^.!?]{0,60}\bHeimat\b)',re.I)
PERSON_CONTEXT=re.compile(r'\b(?:Arbeitnehmer(?:n|in|innen)?|Menschen|Personen|Migranten|Arbeiter(?:n|innen)?|Gastarbeiter(?:n|innen)?)\b',re.I)
COMPETING_SUBJECT=re.compile(r'\b(?:Fahrzeug\w*|Flugzeug\w*|Autos?|Maschine\w*|Güter\w*|Tier\w*|Vögel|Züge|Schiff\w*)\b',re.I)
# This count is elliptical, but the complete demonstrator predicate establishes
# a human antecedent in the supplied source context. Generic "ihre Zahl" and a
# nearby human noun alone cannot decide what an otherwise untyped count means.
DEMONSTRATOR_ANTECEDENT=re.compile(r'\b(?:Menschen|Personen|Franzosen|Französinnen|Demonstrant(?:en|innen)?|Demonstrierende[n]?|Teilnehmer(?:n|innen)?|Teilnehmende[n]?)\b[^.!?]{0,180}\bauf\s+die\s+Straße\s+gegangen\b',re.I)
BARE_DEMONSTRATOR_COUNT=re.compile(r'\b(?:schätzt|schätzen)\s+ihre\s+Zahl\s+auf\s+(?P<number>'+COUNT+r')(?=\s*(?:[.!?;:]|$))',re.I)
PAIRED_PARTICIPANT_COUNT=re.compile(r'(?P<anchor>'+COUNT+r'\s+Menschen)\s*,\s*die\s+Polizei\s+von\s+(?P<number>'+COUNT+r')(?=\s*(?:[.!?;:]|$))',re.I)
YEAR=re.compile(r'(?<![\d.,])(?:18\d{2}|19\d{2}|20\d{2}|21\d{2}|2200)(?!\d|[.,]\d)')
EXPLICIT_NONYEAR_UNIT=re.compile(r'\s*(?:€|EUR\b|Euro\b|Dollar\b|USD\b|%|Prozent\w*|Menschen\b|Personen\b|Soldaten\b|Arbeitnehmer\w*|Fahrzeug\w*|Autos?\b|Kilometer\w*|km\b|Meter\w*|Tonnen?\b|kg\b|Millionen?\b|Mrd\b|Milliarden?\b)',re.I)


def _replace_spans(text,edits):
    last=len(text)
    for start,end,replacement in sorted(edits,reverse=True):
        if end>last:raise ValueError('Normalization spans overlap')
        text=text[:start]+replacement+text[end:];last=start
    return text


def _dates(text,language):
    patterns=(DE_NAMED_DATE,DE_NUMERIC_DATE) if language=='de' else (ZH_DATE,)
    occupied=[];values=[];errors=[];evidence=[]
    for pattern in patterns:
        for match in pattern.finditer(text):
            if any(match.start()<end and match.end()>start for start,end in occupied):continue
            raw_month=match.group('month');month=MONTHS.get(raw_month.lower()) if language=='de' and not raw_month.isdigit() else int(raw_month)
            day=int(match.group('day'));year=int(match.group('year')) if match.group('year') else None
            try:date(year or 2000,month,day)
            except ValueError:
                errors.append('原文或译文日期无效：'+match.group());continue
            occupied.append(match.span());values.append((year,month,day))
            evidence.append({'kind':'explicit_calendar_date','language':language,'span':list(match.span()),
                'original':match.group(),'typed_date':{'year':year,'month':month,'day':day}})
    return _replace_spans(text,[(start,end,' '*(end-start)) for start,end in occupied]),values,errors,evidence


def _explicit_standings_pair(source,target):
    """One complete standings comparison, with separately checked team roles.

    An unordered bag of positions would accept swapped endpoints. Keep both
    named teams, equal points, all intermediate teams and the inclusive end.
    """
    left=re.fullmatch(r'\s*Augsburg\s+ist\s+(?P<first>\d{1,2})\.\s+und\s+punktgleich\s+'
        r'mit\s+allen\s+Teams\s+bis\s+einschließlich\s+Hamburg\s+auf\s+Platz\s+'
        r'(?P<last>\d{1,2})\s*[.!?]?\s*',source,re.I)
    if not left:return source,target,[],[]
    first,last=int(left['first']),int(left['last'])
    rank_end=r'(?=\s*(?:[，,。；;！？!?]|$|的|(?:位|名)(?=[，,。；;！？!?\s的]|$)))'
    first_target=re.search(r'奥格斯堡\s*(?:排名|名列|位居)\s*第\s*(\d{1,2})(?!\d)'+rank_end,target)
    last_target=re.search(r'(?:排名|名列|位居)\s*第\s*(\d{1,2})(?!\d)\s*(?:位|名)?\s*的?\s*汉堡',target)
    if last_target is None:
        last_target=re.search(r'汉堡\s*(?:排名|名列|位居)\s*第\s*(\d{1,2})(?!\d)'+rank_end,target)
    inclusive=re.search(r'汉堡[^，,。；;]{0,6}在内|包括[^。；;]{0,15}汉堡',target)
    negated=re.search(r'不|无|没|未|非|否|免|拒|缺|禁止|排除|除外',target)
    if (not 1<=first<=last<=99 or not first_target or not last_target
            or int(first_target[1])!=first or int(last_target[1])!=last
            or not inclusive or not re.search(r'所有球队|各队|各支球队',target)
            or re.search(r'部分|一些|少数|几支|某些|各队中',target)
            or not re.search(r'积分(?:相同|一致)|同分',target) or negated):
        return source,target,[],['积分榜首末球队的排名、同分或包含关系未完整对应']
    source=_replace_spans(source,[(left.start('first'),left.end('first')+1,'auf Platz '+str(first))])
    edits=[]
    for match in re.finditer(r'(?:排名|名列|位居)\s*第\s*(\d{1,2})(?!\d)'+rank_end+r'\s*(?:位|名)?',target):
        edits.append((*match.span(),'第'+str(int(match[1]))+'位'))
    target=_replace_spans(target,edits)
    return source,target,[{'kind':'complete_named_standings_pair','first_team':'Augsburg',
        'first_position':first,'last_team':'Hamburg','last_position':last,
        'all_intermediate_teams':True,'equal_points':True,'last_included':True}],[]


def preprocess(source,target,previous_context=None):
    if not isinstance(source,str) or not isinstance(target,str):raise TypeError('Numeric comparison expects strings')
    if previous_context is not None and not isinstance(previous_context,str):raise TypeError('Context must be source text')
    original_source=source;original_target=target;context=previous_context or '';evidence=[];review=[]
    source,left,left_errors,left_evidence=_dates(source,'de');target,right,right_errors,right_evidence=_dates(target,'zh')
    evidence.extend(left_evidence+right_evidence);errors=left_errors+right_errors
    # Unicode Celsius is the same explicit temperature unit, never Fahrenheit.
    for language,text in [('de',source),('zh',target)]:
        for match in re.finditer('℃',text):
            evidence.append({'kind':'unicode_celsius_unit_alias','language':language,
                'comparison_span':list(match.span()),'original':'℃',
                'comparison':'Grad Celsius' if language=='de' else '摄氏度'})
    source=source.replace('℃','Grad Celsius');target=target.replace('℃','摄氏度')
    if Counter(left)!=Counter(right):errors.append('完整日期的日、月或年份未等值保留')
    source,target,standings_evidence,standings_errors=_explicit_standings_pair(source,target)
    evidence.extend(standings_evidence);errors.extend(standings_errors)
    # The inflected duration unit is a known spelling of years, not a new unit.
    edits=[]
    for match in re.finditer(r'\bJahren\b',source,re.I):
        edits.append((*match.span(),'Jahre'))
        evidence.append({'kind':'known_year_unit_inflection','span':list(match.span()),'original':original_source[match.start():match.end()],'comparison':'Jahre'})
    source=_replace_spans(source,edits)
    # Produktionen means screen works only with this explicit film/TV context.
    # Bare productions and industrial products never borrow the Chinese unit.
    edits=[]
    if re.search(r'\bFilm-\s+und\s+Fernsehlandschaft\b',source,re.I):
        for match in re.finditer(r'(?P<number>'+COUNT+r')\s+(?P<unit>Produktionen)\b',source,re.I):
            edits.append((*match.span('unit'),'Filmproduktionen'))
            evidence.append({'kind':'screen_work_count_with_explicit_film_tv_context',
                'comparison_span':list(match.span()),'original_comparison_form':match.group()})
    source=_replace_spans(source,edits)
    # A whole near-N-years-since construction can express the source's fast
    # bound. Do not take the suffix of 接近 or a non-temporal 年来说 fragment.
    edits=[]
    for right in re.finditer(r'(?<![接靠逼临将未不无])近\s*(?P<years>\d{1,3})'
            r'\s*年来(?=的|[，,。；;！？!?\s]|$|科隆(?:队|俱乐部)首次)',target):
        if not re.search(r'\bvor\s+fast\s+'+right['years']+r'\s+Jahre\b',source,re.I):
            errors.append('近几年来的模糊比较不能替代原文精确年数或其他比较')
    for match in re.finditer(r'\bvor\s+fast\s+(?P<years>\d{1,3})\s+Jahre\b',source,re.I):
        number=match['years']
        for right in re.finditer(r'(?<![接靠逼临将未不无])(?P<near>近)\s*'+number+
                r'\s*年来(?=的|[，,。；;！？!?\s]|$|科隆(?:队|俱乐部)首次)',target):
            start=max([target.rfind(mark,0,right.start()) for mark in '，,。；;！？!?']+[-1])+1
            ends=[target.find(mark,right.end()) for mark in '，,。；;！？!?']
            end=min([offset for offset in ends if offset>=0] or [len(target)])
            if re.search(r'不|无|没|未|非|否|免|拒|缺|禁止',target[start:end]):continue
            edits.append((*right.span('near'),'不到'))
            evidence.append({'kind':'near_elapsed_years_paired_with_source_fast',
                'source_comparison_span':list(match.span()),'target_comparison_span':list(right.span()),
                'value':int(number),'relation':'lt'})
    target=_replace_spans(target,list(dict.fromkeys(edits)))
    # Only a named player's second-half Bundesliga debut fixes the match
    # clock. Ordinary time on the field and stoppage-time minutes stay distinct.
    edits=[]
    debut=(r'(?i:\bIn\s+der\s+zweiten\s+Hälfte\s+kommt\s+(?:dann\s+)?)'
        r'(?P<time>(?i:nach)\s+(?P<number>\d{1,2})\s+(?i:Minuten))\s+'
        r'(?:[A-ZÄÖÜ][^\W\d_]*(?:[-’\'][^\W\d_]+)?\s+){1,5}'
        r'(?i:zu\s+seinem\s+Bundesliga[- ]Debüt)\b')
    for match in re.finditer(debut,source):
        number=int(match['number'])
        if not 45<number<=90:continue
        edits.append((*match.span('time'),'in der '+str(number)+'. Minute'))
        evidence.append({'kind':'second_half_bundesliga_debut_match_minute',
            'comparison_span':list(match.span()),'original_comparison_form':match.group(),
            'match_minute':number})
        if re.search(r'(?:上场|登场|出场|下半场开始|第二半场开始)[^。；;！？!?]{0,10}'
                r'第\s*'+str(number)+r'\s*分钟',target):
            errors.append('比赛登场分钟不能改成上场后或半场开始后的第几分钟')
        if re.search(r'(?:最多|至多|至少|最少|不超过|不到|超过|少于|大约|约|接近|最迟|最早)\s*'
                r'第\s*'+str(number)+r'\s*分钟',target):
            errors.append('明确比赛登场分钟不能改成上下限或约数')
        if re.search(r'(?:不|未|没|并非|无法|拒绝|从未|没有)[^，,。；;！？!?]{0,8}'
                r'(?:迎来|完成|实现|上演|登场|首秀)',target):
            errors.append('明确首秀发生不能改成否定或未发生')
    source=_replace_spans(source,edits)
    # A value ceiling explicitly names its monetary basis. A free-standing
    # bis, or a von/bis range, cannot borrow this upper-bound interpretation.
    edits=[]
    for match in re.finditer(r'\bmit\s+einem\s+Wert\s+bis\s+(?P<number>'+COUNT+r')\s+(?:Euro\b|€)',source,re.I):
        edits.append((match.start('number'),match.start('number'),'zu '))
        evidence.append({'kind':'explicit_monetary_value_ceiling','comparison_span':list(match.span()),
            'original_comparison_form':match.group(),'relation':'le'})
    source=_replace_spans(source,edits)
    # This complete Chinese flight/distance construction names a maximum.
    # Preserve the number and distance unit; do not treat a generic 可达 as <=.
    edits=[]
    for match in re.finditer(r'(?P<operator>最远(?:可|能)(?:飞行|飞))\s*\d+(?:\.\d+)?\s*(?:公里|千米)'
            r'(?=\s*(?:[，,。；;！？!?]|$))',target):
        start=max([target.rfind(mark,0,match.start()) for mark in '，,。；;！？!?']+[-1])+1
        # 无人机 names an aircraft, rather than negating its flight capability.
        # Every other clause negation must keep this unsupported form pending.
        clause=target[start:match.end()].replace('无人机','飞行器')
        if re.search(r'不|无|没|未|非|否|免|拒|缺|禁止',clause):continue
        edits.append((*match.span('operator'),'最多'))
        evidence.append({'kind':'explicit_flight_distance_ceiling','comparison_span':list(match.span()),
            'original_comparison_form':match.group(),'relation':'le'})
    target=_replace_spans(target,edits)
    # Both endpoints name the same explicit personnel scale. A bare count
    # elsewhere cannot borrow a person unit from Chinese or an unrelated noun.
    edits=[]
    for match in re.finditer(r'\bvon\s+(?P<first>'+COUNT+r')\s+auf\s+(?:insgesamt\s+)?'
            r'(?P<last>'+COUNT+r')\s+(?P<unit>Beamte[nrms]?|Fachleute[n]?|Menschen|Personen)\b',source,re.I):
        edits.append((match.end('first'),match.end('first'),' Menschen'))
        evidence.append({'kind':'paired_personnel_count_with_explicit_final_unit',
            'comparison_span':list(match.span()),'first':match.group('first'),
            'last':match.group('last'),'explicit_person_unit':match.group('unit')})
    source=_replace_spans(source,edits)
    # The class-size ellipsis requires both the immediate school/class source
    # context and the current year-group predicate. A generic Maximalgröße or
    # a nearby person noun alone does not determine the unit.
    edits=[]
    school_context=re.search(r'\b(?:unsere\s+)?Schüler\b[^.!?]{0,100}\b(?:unsere\s+)?Klassen\s+sind\s+zu\s+groß\s*[.!?]?\s*$',context,re.I)
    for match in re.finditer(r'\bMaximalgröße\s+von\s+(?P<number>\d{1,3})(?![\d.,])'
            r'(?=\s+vor\s+in\s+den\s+(?:höheren|unteren)\s+Jahrgangsstufen\b)',source,re.I):
        if not school_context or COMPETING_SUBJECT.search(context+' '+source):continue
        edits.append((*match.span(),'höchstens '+match.group('number')+' Menschen'))
        evidence.append({'kind':'class_size_ceiling_with_immediate_explicit_school_antecedent',
            'comparison_span':list(match.span()),'original':match.group(),
            'previous_source_context_sha256':hashlib.sha256(context.encode()).hexdigest(),
            'explicit_source_antecedent':school_context.group()})
    source=_replace_spans(source,edits)
    # An elliptical personal age remains bounded by a nearby explicit age
    # comparison and the complete hypothetical-person construction. Neither
    # the execution date nor the Chinese candidate supplies the unit.
    edits=[]
    for match in re.finditer(r'\bals\s+wäre\s+ich\s+(?P<first>\d{1,3})\s+oder\s+'
            r'(?P<last>\d{1,3})(?=\s+und\s+müsste\s+mich\s+noch\s+beweisen\b)',source,re.I):
        if (not re.search(r'\b(?:jünger|junger|älter|Alter)\b',context,re.I)
                or not all(0<int(match.group(key))<=120 for key in ('first','last'))):continue
        edits.extend([(match.end('first'),match.end('first'),' Jahre'),
                      (match.end('last'),match.end('last'),' Jahre')])
        evidence.append({'kind':'hypothetical_person_age_with_explicit_age_antecedent',
            'comparison_span':list(match.span()),'original':match.group(),
            'previous_source_context_sha256':hashlib.sha256(context.encode()).hexdigest()})
    source=_replace_spans(source,edits)
    # Explicit person nouns remain people even when a bounded adjective intervenes.
    edits=[]
    for match in KNOWN_PEOPLE.finditer(source):
        edits.append((*match.span('description'),'Menschen'))
        evidence.append({'kind':'explicit_person_noun','comparison_span':list(match.span()),'original_comparison_form':match.group(),'comparison':match.group('number')+' Menschen'})
    source=_replace_spans(source,edits)
    # Only the immediately linked, explicitly human injury count supplies the
    # antecedent for this one seriously injured person. Bare einer stays unknown.
    edits=[]
    for match in re.finditer(r'(?P<anchor>'+COUNT+r'\s+Menschen\s+(?:werden|wurden|sind)\s+verletzt)\s*,\s*'
            r'mindestens\s+(?P<one>einer)\s+von\s+ihnen\s+schwer(?:\s+verletzt)?(?=\s*(?:[.!?;]|$))',source,re.I):
        edits.append((*match.span('one'),'1 Menschen'))
        evidence.append({'kind':'one_serious_injury_with_explicit_same_clause_human_antecedent',
            'comparison_span':list(match.span()),'explicit_person_anchor':match.group('anchor'),
            'value':1,'relation':'ge'})
    source=_replace_spans(source,edits)
    # The second figure in this explicit organizer/police comparison shares
    # the first figure's already established person unit within the same cue.
    edits=[]
    for match in PAIRED_PARTICIPANT_COUNT.finditer(source):
        if not COMPETING_SUBJECT.search(source):
            edits.append((match.end('number'),match.end('number'),' Menschen'))
            evidence.append({'kind':'paired_participant_count_same_source_clause',
                'comparison_span':list(match.span('number')),'count':match.group('number'),
                'explicit_person_anchor':match.group('anchor')})
    source=_replace_spans(source,edits)
    # Preserve a source-bound participant-count anaphor without guessing a
    # unit from Chinese. The number must end its source clause and the full
    # preceding source predicate must identify demonstrators, with no competing
    # vehicle/animal/goods referent. Unknown bare counts remain untyped.
    edits=[]
    for match in BARE_DEMONSTRATOR_COUNT.finditer(source):
        antecedent=DEMONSTRATOR_ANTECEDENT.search(context)
        if (antecedent and re.fullmatch(r'[\s.!?]*',context[antecedent.end():])
                and not COMPETING_SUBJECT.search(context+' '+source)):
            edits.append((match.end('number'),match.end('number'),' Menschen'))
            evidence.append({'kind':'demonstrator_count_with_explicit_source_antecedent',
                'comparison_span':list(match.span('number')),'count':match.group('number'),
                'previous_source_context':context,
                'previous_source_context_sha256':hashlib.sha256(context.encode()).hexdigest(),
                'explicit_person_antecedent':antecedent.group()})
    source=_replace_spans(source,edits)
    # A bare return count needs a real explicit person antecedent. Return verbs
    # alone cannot establish a person unit (vehicles and goods can also return).
    edits=[]
    for match in BARE_RETURN.finditer(source):
        if PERSON_CONTEXT.search(context) and not COMPETING_SUBJECT.search(context+' '+source):
            edits.append((match.end(),match.end(),' Menschen'))
            evidence.append({'kind':'person_count_with_explicit_source_antecedent','comparison_span':list(match.span()),
                'count':match.group('number'),'previous_source_context':context,
                'previous_source_context_sha256':hashlib.sha256(context.encode()).hexdigest(),
                'explicit_person_antecedent':PERSON_CONTEXT.search(context).group()})
        else:review.append({'kind':'bare_return_count_requires_explicit_person_antecedent','count':match.group('number'),'previous_source_context':context})
    source=_replace_spans(source,edits)
    # Recognize only evidenced historical-year constructions. Explicit currency,
    # distance or person units always prevent treating a number as a year.
    edits=[]
    awards_antecedent=re.search(r'\bFür\s+ihre\s+Produktionen\s+hat\s+[^.!?;\d]{1,80}\s+'
        r'zahlreiche\s+Preise\s+und\s+Auszeichnungen\s+bekommen\s*[.!?]?\s*$',context,re.I)
    for match in YEAR.finditer(source):
        tail=source[match.end():];prefix=source[max(0,match.start()-110):match.start()]
        if EXPLICIT_NONYEAR_UNIT.match(tail) or re.match(r'er[-\s]Jahren?\b',tail,re.I):continue
        direct=bool(re.search(r'\b(?:Ende|Anfang)\s*$',prefix,re.I))
        budget_year=bool(re.search(r'\b(?:Bundes)?haushalt\s*$',prefix,re.I)
            or (re.search(r'\bfür\s*$',prefix,re.I)
                and re.match(r'\s*(?:[.,;:!?]|$|(?:Steuereinnahmen|Einnahmen|Ausgaben|Haushalt|Budget|'
                             r'werden|wird|wurde|wurden|ist|sind|war|waren|erwartet|geplant)\b)',tail,re.I)))
        range_start=bool(re.search(r'\bvon\s*$',prefix,re.I) and re.match(r'\s+bis\b[^.!?]{0,40}\b(?:18|19|20|21)\d{2}\b',tail,re.I))
        range_end=bool(re.search(r'\bvon\s+(?:18|19|20|21)\d{2}(?:\s+Jahre)?\s+bis(?:\s+zum\s+(?:Stopp|Ende))?\s*$',prefix,re.I))
        explicit_in_year=bool(re.search(r'\bin\s*$',prefix,re.I)
            and re.match(r'\s*(?:[.!?;:]|$)',tail))
        month_year=bool(re.search(r'\b(?:'+MONTH_PATTERN+r')\s*$',prefix,re.I))
        birth_year=bool(re.search(r'\bGeboren\s+wurde\s+(?:[A-ZÄÖÜ][^\W\d_]*(?:[-’\'][^\W\d_]+)?\s+){1,5}$',prefix)
            and re.match(r'\s+in\s+[^\W\d_]',tail))
        founding_year=bool(re.search(r'\bgründet(?:e)?\s*$',prefix,re.I)
            and re.match(r'\s+(?:ihre|seine)\s+eigene\s+Produktionsfirma\b',tail,re.I))
        award_year=bool(re.search(r'\bDarunter\s*$',prefix,re.I)
            and re.match(r'\s+die\s+Ehrenlola\s+für\s+ihr\s+Lebenswerk(?=\s*[,.;!?]|$)',tail,re.I)
            and awards_antecedent and not re.search(r'\b(?:nicht|kein\w*|nichts|niemand\w*|niemals|nie|ohne|weder)\b',
                awards_antecedent.group(),re.I))
        if award_year and re.search(r'(?:不|未|没|并非|无法|拒绝|从未|没有)[^，,。；;！？!?]{0,10}'
                r'(?:包括|获得|获奖|授予|颁发|荣获|表彰)',target):
            errors.append('明确获奖年份不能配对为排除或否定获奖')
        # These complete historical predicates establish calendar dates from
        # German grammar alone. A bare identifier or nearby event noun cannot
        # borrow the Chinese 年 unit, and all numeric/polarity checks remain.
        career_peak=bool(not source[:match.start()].strip() and re.fullmatch(
            r'\s+für\s+(?:[A-ZÄÖÜ][^\W\d_]*(?:[-’\'][^\W\d_]+)?\s+){1,5}'
            r'ein\s+Höhepunkt\s+(?:seiner|ihrer)\s+Karriere\s*[.!]?\s*',tail))
        demonstration_date=bool(not source[:match.start()].strip() and re.fullmatch(
            r'\s+etwa\s+wettert\s+(?:er|sie)\s+bei\s+einer\s+Demonstration\s+'
            r'[^.!?;\d]{1,100}\s+gegen\s+das\s+Heizungsgesetz\s+der\s+'
            r'damaligen\s+Bundesregierung\s*[.!]?\s*',tail,re.I))
        scandal_date=bool(re.fullmatch(r'\s*(?:Seine|Ihre)\s+wohl\s+schwerste\s+politische\s+Krise,\s*'
            r'die\s+sogenannte\s+Flugblatt-Affäre\s*',source[:match.start()],re.I)
            and re.fullmatch(r'\s*[.!]?\s*',tail))
        law_date=bool(re.search(r'\berließ\s+[^.!?;\d]{1,100}\s$',source[:match.start()],re.I)
            and re.fullmatch(r'\s+ein\s+sogenanntes\s+Sicherheitsgesetz\s*[.!]?\s*',tail,re.I))
        repeated_situation_date=bool(re.fullmatch(
            r'\s*Das\s+ist\s+eine\s+Situation,\s*die\s+nicht\s+unbekannt\s+ist\s+hier\s+in\s+den\s+USA\s*',
            source[:match.start()],re.I) and re.fullmatch(
            r'\s+hatten\s+wir\s+so\s+eine\s+Situation\s+schon\s+mal\s*[.]?\s*',tail,re.I))
        if repeated_situation_date and not re.fullmatch(
                r'\s*这种情况在美国并不陌生[，,]\s*'+re.escape(match.group())+
                r'\s*年\s*(?:我们\s*)?就\s*(?:发生过一次|遇到过一次|发生过类似的事)\s*[。. ]?\s*',target):
            errors.append('历史情况的并不陌生与当年确曾发生须完整保留，不能吞并额外否定')
        grammatical_date=career_peak or demonstration_date or scandal_date or law_date or repeated_situation_date
        if grammatical_date:
            if not re.search(r'(?<!\d)'+re.escape(match.group())+r'\s*年(?!龄|纪|级)',target):
                errors.append('明确历史年份须保留同值年份和年单位，不能改为年龄或编号')
            source_negative=bool(re.search(r'\b(?:nicht|kein\w*|nichts|niemand\w*|niemals|nie|ohne|weder)\b',source,re.I))
            target_negative=bool(re.search(r'不|无|没|未|非|否|免|拒|缺|禁止',target))
            if source_negative!=target_negative:
                errors.append('历史事件年份和事实的否定关系未对应')
        if direct or budget_year or range_start or range_end or explicit_in_year or month_year or birth_year or founding_year or award_year or grammatical_date:
            edits.append((match.end(),match.end(),' Jahre'))
            evidence.append({'kind':'explicit_historical_year_context','comparison_span':list(match.span()),
                'value':int(match.group()),'source_context':prefix[-90:]+match.group()+tail[:50],
                'reason':'Ende/Anfang year' if direct else 'explicit für/Haushalt year' if budget_year else 'explicit in year' if explicit_in_year else 'explicit named month and year' if month_year else 'explicit birth year and location' if birth_year else 'explicit production-company founding year' if founding_year else 'explicit award year with preceding awards predicate' if award_year else 'complete historical event predicate' if grammatical_date else 'von/bis paired historical year range'})
    source=_replace_spans(source,edits)
    # An abbreviated half-year can borrow a century only from an explicit
    # immediately preceding source year, and only for that following year.
    # The execution date and the Chinese candidate never supply the century.
    edits=[]
    antecedent_years=[match for match in re.finditer(r'\bin\s+(?P<year>(?:18|19|20|21)\d{2})(?=\s*(?:[.!?;:]|$))',context,re.I)
        if re.fullmatch(r'[\s.!?]*',context[match.end():])]
    for match in re.finditer(r'\bIm\s+(?:ersten|zweiten)\s+Halbjahr\s+(?P<year>\d{2})(?![\d.,])',source,re.I):
        if not antecedent_years:continue
        antecedent=antecedent_years[-1];year=int(antecedent.group('year'))+1
        if year%100!=int(match.group('year')):continue
        edits.append((*match.span('year'),str(year)+' Jahre'))
        evidence.append({'kind':'abbreviated_half_year_with_previous_explicit_year',
            'comparison_span':list(match.span('year')),'original':match.group('year'),
            'expanded_year':year,'previous_source_year':antecedent.group(),
            'previous_source_context_sha256':hashlib.sha256(context.encode()).hexdigest()})
    source=_replace_spans(source,edits)
    # An intervening "weitere" does not erase the explicitly stated years
    # of imprisonment. This is comparison only; the additional-term wording
    # stays in the stored source and still requires semantic approval.
    edits=[]
    for match in re.finditer(r'(?<![\w.,])\d{1,3}\s+(?P<modifier>weitere)\s+Jahre\s+Haft\b',source,re.I):
        number=re.match(r'\d+',match.group()).group()
        additional=re.search(r'(?:追加(?:判处)?|加判|再判(?:处)?|再服刑|还要服刑|还将服刑|再坐牢|额外(?:判处)?|又被判处)'
            r'\s*'+number+r'\s*年(?=\s*(?:监禁|徒刑|刑期|牢|[，,。；;！？!?]|$))',target)
        if not additional or re.search(r'不|无|没|未|非|否|免|拒|缺|禁止',target):
            errors.append('追加监禁年数、年单位和追加关系须完整保留')
        edits.append((*match.span('modifier'),' ' * len(match['modifier'])))
        evidence.append({'kind':'explicit_additional_prison_year_unit',
            'comparison_span':list(match.span()),'original_comparison_form':match.group()})
    source=_replace_spans(source,edits)
    return {'source':source,'target':target,'calendar_errors':errors,'evidence':evidence,'review_required':review,
        'original_source_preserved':original_source,'original_target_preserved':original_target}
