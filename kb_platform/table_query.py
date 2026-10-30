from __future__ import annotations

import math

from .core import PlatformError


def verified_table(table):

    if not isinstance(table, dict):
        raise PlatformError('INVALID_TABLE_QUERY', 'An original typed table is required', 422)
    headers, values = table.get('headers'), table.get('rows')
    if (not isinstance(headers, list) or not headers or
            any(not isinstance(header, str) for header in headers) or len(set(headers)) != len(headers) or
            not isinstance(values, list) or any(not isinstance(row, list) or len(row) > len(headers) for row in values)):
        raise PlatformError('AMBIGUOUS_TABLE_STRUCTURE', 'Each column requires a unique original header', 422)
    width = len(headers)
    cells = table.get('cells')
    if cells is not None and (not isinstance(cells, list) or len(cells) != (len(values) + 1) * width or
            any(not isinstance(cell, dict) for cell in cells)):
        raise PlatformError('INVALID_TABLE_STRUCTURE', 'Cell metadata does not match the original table grid', 422)
    effective, row_numbers, cell_lineage, formula_lineage = [], [], [], []
    previous_row = 0
    for index, values_row in enumerate([headers, *values]):
        row = [*values_row, *([None] * (width - len(values_row)))]
        metadata = cells[index * width:(index + 1) * width] if cells is not None else []
        source_rows = [cell.get('row', index + 1) for cell in metadata]
        if any(type(number) is not int or number < 1 for number in source_rows):
            raise PlatformError('INVALID_TABLE_STRUCTURE', 'Original row coordinates are inconsistent', 422)
        numbers = set(source_rows)
        if len(numbers) > 1:
            raise PlatformError('INVALID_TABLE_STRUCTURE', 'Original row coordinates are inconsistent', 422)
        source_row = next(iter(numbers)) if numbers else index + 1
        if source_row <= previous_row:
            raise PlatformError('INVALID_TABLE_STRUCTURE', 'Original source rows must retain their order', 422)
        previous_row = source_row
        formulas = []
        for column_index, cell in enumerate(metadata):
            formula = cell.get('formula')
            if formula or cell.get('value_status') == 'cached_missing':
                if index == 0:
                    raise PlatformError('AMBIGUOUS_TABLE_STRUCTURE', 'Formula headers require a verified schema', 422)
                if (not formula or cell.get('value_status') != 'cached' or cell.get('cached_value') is None or
                        type(cell.get('parsed_value')) is not type(cell.get('cached_value')) or
                        cell.get('parsed_value') != cell.get('cached_value')):
                    raise PlatformError('TABLE_FORMULA_CACHE_MISSING',
                                        'A formula has no verified cached value; no formula execution is permitted', 422)
                row[column_index] = cell['cached_value']
                formulas.append({'header': headers[column_index], 'source_row_number': cell.get('row', index + 1),
                    'source_column_number': cell.get('column', column_index + 1), 'cell_anchor': cell.get('cell_anchor'),
                    'formula': formula, 'cached_value': cell['cached_value'], 'value_origin': 'original_stored_formula_cache'})
        if index:
            effective.append(row)
            row_numbers.append(source_row)
            cell_lineage.append([cell.get('cell_anchor') for cell in metadata])
            formula_lineage.append(formulas)
    return {'headers': headers, 'rows': effective, 'source_row_numbers': row_numbers,
            'cell_anchors': cell_lineage,
            'cached_formula_cells': formula_lineage,
            'header_cell_anchors': [cell.get('cell_anchor') for cell in cells[:width]] if cells else [],
            'merged_ranges': table.get('merged_ranges', [])}


def _key(value):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if type(value) not in (str, bool, int, float) or isinstance(value, float) and not math.isfinite(value):
        raise PlatformError('INVALID_TABLE_JOIN_KEY', 'Join keys must be finite exact typed scalar values', 422)

    return type(value), value


def join_tables(left, right, *, left_key, right_key, how='inner', max_rows=100):
    if (how not in ('inner', 'left') or type(max_rows) is not int or not 1 <= max_rows <= 1000 or
            not isinstance(left_key, str) or not left_key or not isinstance(right_key, str) or not right_key):
        raise PlatformError('INVALID_TABLE_JOIN', 'Use explicit key columns and a bounded inner/left join', 422)
    left, right = verified_table(left), verified_table(right)
    if left_key not in left['headers'] or right_key not in right['headers']:
        raise PlatformError('TABLE_JOIN_COLUMN_MISSING', 'The exact join key must exist in each source table', 422)
    left_index, right_index = left['headers'].index(left_key), right['headers'].index(right_key)
    right_rows = {}
    for index, row in enumerate(right['rows']):
        key = _key(row[right_index])
        if key is not None:
            right_rows.setdefault(key, []).append(index)
    rows, lineage, total_rows, matched_pairs = [], [], 0, 0
    for left_row_index, row in enumerate(left['rows']):
        key = _key(row[left_index])
        matches = right_rows.get(key, []) if key is not None else []
        matched_pairs += len(matches)
        total_rows += len(matches) if matches else int(how == 'left')
        available = max_rows - len(rows)
        if available <= 0:
            continue
        for right_row_index in matches[:available] if matches else ([None] if how == 'left' else []):
            left_values = {'left.' + header: value for header, value in zip(left['headers'], row, strict=True)}
            right_values = {'right.' + header: None if right_row_index is None else right['rows'][right_row_index][index]
                            for index, header in enumerate(right['headers'])}
            rows.append(left_values | right_values)
            lineage.append({'left_source_row_number': left['source_row_numbers'][left_row_index],
                'right_source_row_number': None if right_row_index is None else right['source_row_numbers'][right_row_index],
                'left_cell_anchors': left['cell_anchors'][left_row_index],
                'right_cell_anchors': [] if right_row_index is None else right['cell_anchors'][right_row_index],
                'left_cached_formula_cells': left['cached_formula_cells'][left_row_index],
                'right_cached_formula_cells': [] if right_row_index is None else right['cached_formula_cells'][right_row_index]})
    truncated = total_rows > len(rows)
    return {'operation': 'exact_typed_equi_join', 'how': how, 'rows': rows,
        'columns': ['left.' + header for header in left['headers']] + ['right.' + header for header in right['headers']],
        'left_key': left_key, 'right_key': right_key, 'lineage': lineage,
        'left_source_row_numbers': [item['left_source_row_number'] for item in lineage],
        'right_source_row_numbers': [item['right_source_row_number'] for item in lineage],
        'total_rows': total_rows, 'matched_pair_count': matched_pairs, 'returned_rows': len(rows),
        'max_rows': max_rows, 'complete_result': not truncated, 'truncated': truncated,
        'source_tables': {'left': {key: left[key] for key in ('headers', 'header_cell_anchors', 'merged_ranges')},
                          'right': {key: right[key] for key in ('headers', 'header_cell_anchors', 'merged_ranges')}},
        'trace': {'method': 'canonical-typed-table-join/1', 'key_semantics': 'exact_typed_equality',
                  'null_keys_match': False, 'duplicate_key_semantics': 'all_pairs',
                  'unit_conversion': 'none', 'formula_execution': False, 'model_calls': 0}}
