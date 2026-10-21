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
KNOWN_PEOPLE=re.compile(r'(?P<number>'+COUNT+r')\s+(?P<description>(?:(?:zusätzliche[nrsm]?|türkische[nrsm]?|deutsche[nrsm]?)\s+){0,2}(?:Soldat(?:en|in|innen)?|Arbeitnehmer(?:n|in|innen)?|Gastarbeiter(?:n|in|innen)?))\b',re.I)
BARE_RETURN=re.compile(r'(?P<number>'+COUNT+r')(?=\s+kehr(?:ten|en)\b[^.!?]{0,120}\bzurück\b[^.!?]{0,60}\bHeimat\b)',re.I)
PERSON_CONTEXT=re.compile(r'\b(?:Arbeitnehmer(?:n|in|innen)?|Menschen|Personen|Migranten|Arbeiter(?:n|innen)?|Gastarbeiter(?:n|innen)?)\b',re.I)
COMPETING_SUBJECT=re.compile(r'\b(?:Fahrzeug\w*|Flugzeug\w*|Autos?|Maschine\w*|Güter\w*|Tier\w*|Vögel|Züge|Schiff\w*)\b',re.I)
YEAR=re.compile(r'(?<![\d.,])(?:18\d{2}|19\d{2}|20\d{2}|21\d{2}|2200)(?![\d.,])')
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
    # The inflected duration unit is a known spelling of years, not a new unit.
    edits=[]
    for match in re.finditer(r'\bJahren\b',source,re.I):
        edits.append((*match.span(),'Jahre'))
        evidence.append({'kind':'known_year_unit_inflection','span':list(match.span()),'original':original_source[match.start():match.end()],'comparison':'Jahre'})
    source=_replace_spans(source,edits)
    # Explicit person nouns remain people even when a bounded adjective intervenes.
    edits=[]
    for match in KNOWN_PEOPLE.finditer(source):
        edits.append((*match.span('description'),'Menschen'))
        evidence.append({'kind':'explicit_person_noun','comparison_span':list(match.span()),'original_comparison_form':match.group(),'comparison':match.group('number')+' Menschen'})
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
    for match in YEAR.finditer(source):
        tail=source[match.end():];prefix=source[max(0,match.start()-110):match.start()]
        if EXPLICIT_NONYEAR_UNIT.match(tail):continue
        direct=bool(re.search(r'\b(?:Ende|Anfang)\s*$',prefix,re.I))
        range_start=bool(re.search(r'\bvon\s*$',prefix,re.I) and re.match(r'\s+bis\b[^.!?]{0,40}\b(?:18|19|20|21)\d{2}\b',tail,re.I))
        range_end=bool(re.search(r'\bvon\s+(?:18|19|20|21)\d{2}(?:\s+Jahre)?\s+bis(?:\s+zum\s+(?:Stopp|Ende))?\s*$',prefix,re.I))
        if direct or range_start or range_end:
            edits.append((match.end(),match.end(),' Jahre'))
            evidence.append({'kind':'explicit_historical_year_context','comparison_span':list(match.span()),
                'value':int(match.group()),'source_context':prefix[-90:]+match.group()+tail[:50],
                'reason':'Ende/Anfang year' if direct else 'von/bis paired historical year range'})
    source=_replace_spans(source,edits)
    return {'source':source,'target':target,'calendar_errors':errors,'evidence':evidence,'review_required':review,
        'original_source_preserved':original_source,'original_target_preserved':original_target}
