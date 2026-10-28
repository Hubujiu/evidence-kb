"""Scorer-only Hotpot-style answer metrics and literal delivered-citation checks.

These metrics do not claim semantic citation entailment or calibrated judging.
"""
from collections import Counter
import re
import string
from decimal import Decimal, InvalidOperation


def ordered_numeric_answer_score(prediction, reference):
    """Scorer-only ordered numeric/date fields; preserve signs and decimal points.

    Unlike Hotpot normalization, 5.5 cannot equal 55, and separators cannot be
    deleted to merge fields. YYYY-MM fields are compared literally.
    """
    expected=reference.split('|')
    actual=prediction.strip().split('|') if isinstance(prediction,str) else []
    if len(actual)!=len(expected):
        return {'exact_match':0,'correct_fields':0,'total_fields':len(expected),'protocol_valid':False}
    matches=[]
    valid=True
    for value,gold in zip(actual,expected):
        value=value.strip()
        if re.fullmatch(r'\d{4}-\d{2}',gold):
            valid &= bool(re.fullmatch(r'\d{4}-\d{2}',value))
            matches.append(value==gold)
        elif re.fullmatch(r'[+-]?\d+(?:\.\d+)?',value):
            try:
                matches.append(Decimal(value)==Decimal(gold))
            except InvalidOperation:
                valid=False
                matches.append(False)
        else:
            valid=False
            matches.append(False)
    return {'exact_match':int(valid and all(matches)),'correct_fields':sum(matches),
            'total_fields':len(expected),'protocol_valid':valid}


def normalize_answer(value):
    value=''.join(c for c in value.lower() if c not in string.punctuation)
    return ' '.join(re.sub(r'\b(a|an|the)\b',' ',value).split())


def answer_score(prediction,references):
    predicted=normalize_answer(prediction)
    best={'exact_match':0,'f1':0.0}
    for reference in references:
        expected=normalize_answer(reference)
        exact=int(predicted==expected)
        if predicted in {'yes','no','noanswer'} or expected in {'yes','no','noanswer'}:
            f1=float(exact)
        else:
            a,b=predicted.split(),expected.split()
            shared=sum((Counter(a)&Counter(b)).values())
            f1=2*shared/(len(a)+len(b)) if shared and a and b else float(exact)
        best['exact_match']=max(best['exact_match'],exact)
        best['f1']=max(best['f1'],f1)
    return best


def score_answer_observations(suite,gold,observations,delivered):
    rows=[]
    for case in suite.cases:
        cid=case.input.case_id
        result=observations.get(cid,{})
        valid=result.get('status')=='COMPLETE'
        value=result.get('value',{}) if valid else {}
        scores=answer_score(value.get('answer',''),gold[cid]['answers']) if valid else {'exact_match':0,'f1':0.0}
        trace=delivered.get(cid)
        fragments=trace.final if trace is not None else ()
        citations=value.get('citations',[])
        literal=bool(citations) and all(isinstance(c,dict) and isinstance(c.get('quote'),str) and c['quote']
            and any(f.source_id==c.get('source_id') and c['quote'] in f.text for f in fragments) for c in citations)
        rows.append({'case_id':cid,'status':result.get('status','NOT_RUN'),**scores,
                     'citation_literal_valid':literal,'answer_and_literal_citations':bool(scores['exact_match'] and literal)})
    return {'total':len(rows),'exact_match':sum(r['exact_match'] for r in rows)/len(rows),
            'f1':sum(r['f1'] for r in rows)/len(rows),
            'citation_literal_valid':sum(r['citation_literal_valid'] for r in rows)/len(rows),
            'counts':{s:sum(r['status']==s for r in rows) for s in ('COMPLETE','ERROR','UNKNOWN','NOT_RUN')},
            'semantic_citation_support':'NOT_MEASURED','rows':rows}
