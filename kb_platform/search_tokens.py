"""Versioned local tokens for the lexical index; never calls an external analyzer."""
import unicodedata

import regex


PROTOCOL = 'unicode-word-cjk-bigram/1'
STOP = frozenset('a an and are as at be but by for if in into is it no not of on or such that the their then there these they this to was will with'.split())
CJK = regex.compile(r'^[\p{Han}\p{Hiragana}\p{Katakana}\p{Hangul}]+$')
WORDS = regex.compile(r'\b', regex.WORD | regex.VERSION1)
LETTERS = regex.compile(r'[\p{L}\p{N}]')


def tokens(text, *, exact=False):
    if not exact:
        # Lucene's CJKWidthFilter normalizes width, not all compatibility forms.
        text = ''.join(unicodedata.normalize('NFKC', c) if '\uff00' <= c <= '\uffef' else c for c in text)
    words = [w.lower() for w in WORDS.split(text) if w]
    if exact:
        return [w for w in words if LETTERS.search(w)]
    result, pending = [], ''
    for word in words + ['']:
        if CJK.fullmatch(word):
            pending += word
            continue
        if pending:
            result.extend([pending] if len(pending) == 1 else [pending[i:i + 2] for i in range(len(pending) - 1)])
            pending = ''
        if LETTERS.search(word) and word not in STOP:
            result.append(word)
    return result
