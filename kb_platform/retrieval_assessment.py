import json

from .core import PlatformError


def unpack_assessment(value, candidates):
    if isinstance(value, list):
        return value, None
    if not isinstance(value, dict) or set(value) not in ({'ranking', 'coverage'}, {'ranking', 'coverage', 'coverage_protocol'}):
        raise PlatformError('INVALID_RERANK_OUTPUT', 'Invalid retrieval assessment', 502)
    protocol = value.get('coverage_protocol', 'coverage-array/1')
    if protocol not in {'coverage-array/1', 'separate-coverage/1', 'coverage-references/2', 'coverage-contributions/3'}:
        raise PlatformError('INVALID_RERANK_OUTPUT', 'Unknown assessment protocol', 502)
    coverage = value['coverage']
    if not isinstance(coverage, list) or not 1 <= len(coverage) <= 20:
        raise PlatformError('INVALID_RERANK_OUTPUT', 'Missing factual requirements', 502)
    originals = {row['citation_id']: row['text'] for row in candidates}
    for requirement in coverage:
        if (not isinstance(requirement, dict) or set(requirement) != {'need', 'supported', 'evidence'} or
                not isinstance(requirement['need'], str) or not requirement['need'].strip() or
                type(requirement['supported']) is not bool or not isinstance(requirement['evidence'], list) or
                (requirement['supported'] and not requirement['evidence'])):
            raise PlatformError('INVALID_RERANK_OUTPUT', 'Invalid factual requirement', 502)
        for quote in requirement['evidence']:
            if protocol in {'coverage-references/2', 'coverage-contributions/3'}:
                if (not isinstance(quote, dict) or set(quote) != {'citation_id'} or
                        not isinstance(quote['citation_id'], str) or quote['citation_id'] not in originals):
                    raise PlatformError('INVALID_RERANK_OUTPUT', 'Unknown candidate reference', 502)
                continue
            if (not isinstance(quote, dict) or set(quote) != {'citation_id', 'quote'} or
                    not isinstance(quote['citation_id'], str) or quote['citation_id'] not in originals or
                    not isinstance(quote['quote'], str) or not quote['quote'].strip() or
                    quote['quote'] not in originals[quote['citation_id']]):
                raise PlatformError('INVALID_RERANK_OUTPUT', 'Coverage is not bound to candidate original text', 502)
    return value['ranking'], coverage


def supporting_ids(coverage, *, include_partial=False):
    return {quote['citation_id'] for requirement in coverage if include_partial or requirement['supported']
            for quote in requirement['evidence']}


def assessment_input(query, assessment_question=None):
    if assessment_question is None:
        return query
    if not isinstance(assessment_question, str) or not assessment_question.strip() or len(assessment_question) > 20000:
        raise PlatformError('INVALID_ASSESSMENT_QUESTION', 'Invalid assessment question', 422)
    return ('Assess evidence needed for the original user question, preserving every requested relationship and exact entity identity. '
            'The retrieval query is an unverified search hypothesis, not evidence and not a replacement question.\n'
            + json.dumps({'original_question': assessment_question, 'retrieval_hypothesis': query}, ensure_ascii=False))
