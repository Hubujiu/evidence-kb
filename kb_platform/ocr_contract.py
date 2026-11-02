import math

from .model_errors import TerminalContractError


OCR_CONTRACT_VERSION = 'ocr-blocks/1'


def validate_ocr_response(result):
    terminal = isinstance(result, dict) and (
        result.get('complete') is False or
        isinstance(result.get('unreadable'), list) and bool(result['unreadable']) or
        result.get('blocks') == [])
    try:
        if (not isinstance(result, dict) or type(result.get('complete')) is not bool or
                not isinstance(result.get('unreadable'), list) or not isinstance(result.get('blocks'), list)):
            raise ValueError('OCR_INVALID_SCHEMA')
        if len(result['blocks']) > 500:
            raise ValueError('OCR_EXCESSIVE_BLOCKS')
        blocks = []
        for item in result['blocks']:
            if not isinstance(item, dict) or not isinstance(item.get('text'), str) or not item['text'].strip():
                raise ValueError('OCR_INVALID_TEXT')
            box = item.get('bbox')
            if (not isinstance(box, list) or len(box) != 4 or
                    any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                        not math.isfinite(value) or not 0 <= value <= 1 for value in box) or
                    box[0] >= box[2] or box[1] >= box[3]):
                raise ValueError('OCR_INVALID_COORDINATES')
            blocks.append({'text': item['text'], 'bbox': [float(value) for value in box]})
    except ValueError as error:
        if terminal:
            raise TerminalContractError(str(error)) from None
        raise
    return blocks
