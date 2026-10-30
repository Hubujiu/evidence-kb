import io
import re


def pdf_layout_features(data):
    import pdfplumber
    pages=[]
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            words=page.extract_words()
            rows={}
            for word in words:
                rows.setdefault(round(word['top']/3),[]).append(word)
            gaps=[]
            for row in rows.values():
                ordered=sorted(row,key=lambda w:w['x0'])
                gaps.extend((a['x1'],b['x0']) for a,b in zip(ordered,ordered[1:])
                            if b['x0']-a['x1']>page.width*.2)
            columns=any(min(right,other_right)-max(left,other_left)>page.width*.2
                        for i,(left,right) in enumerate(gaps) for other_left,other_right in gaps[i+1:])
            pages.append({'words':len(words),'images':len(page.images),'wide_gap_rows':len(gaps),
                          'column_risk':columns,'ocr_needed':not words and bool(page.images)})
    return {'pages':pages,'prefer_layout_ocr':any(p['column_risk'] or p['ocr_needed'] for p in pages)}


def canonical_pdf_prose(text):

    output,prose=[],[]
    def flush():
        if prose:
            output.append(re.sub(r'\s+',' ',' '.join(prose)).strip())
            prose.clear()
    for line in text.splitlines():
        if '|' in line:
            flush()
            output.append(line.strip())
        elif not line.strip():
            flush()
            output.append('')
        else:
            prose.append(line)
    flush()
    return '\n'.join(output).strip()
