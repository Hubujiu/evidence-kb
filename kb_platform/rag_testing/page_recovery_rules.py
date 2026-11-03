import ast
from pathlib import Path


def _contains(node, source):
    expected = ast.dump(ast.parse(source).body[0], include_attributes=False)
    return any(ast.dump(child, include_attributes=False) == expected for child in ast.walk(node))


def page_recovery_violations(root):
    errors = []

    def require(value, message):
        if not value:
            errors.append('page_recovery: ' + message)

    try:
        pages = ast.parse((Path(root) / 'kb_platform/page_parsing.py').read_text('utf-8'))
        ocr = ast.parse((Path(root) / 'kb_platform/ocr.py').read_text('utf-8'))
        functions = {node.name: node for module in (pages, ocr) for node in module.body
                     if isinstance(node, ast.FunctionDef)}
        accept = functions['_accept_page_result']
        require(_contains(accept, 'if parsed.get("original_sha256") != row["page_binary_sha256"]:\n'
                          '    raise ValueError("external page source hash mismatch")'),
                'every admitted page must match its exact source bytes')
        require(_contains(accept, 'conversion = parsed.get("details", {}).get("conversion_status", "")')
                and _contains(accept, 'if conversion and conversion.split(".")[-1] != "SUCCESS":\n'
                              '    row["partial_conversion"] = conversion'),
                'admitted live and cached pages must preserve partial conversion')
        assembly = functions['assemble_pdf_pages']
        for statement in ('_accept_page_result(row, cached, identity, True)',
                          '_accept_page_result(row, parsed, actual_identity, False)',
                          '_accept_page_result(row, parsed, identity, cache_hit)'):
            require(_contains(assembly, statement), 'all admission paths must call the page validator: ' + statement)
        fallback = next(node for node in ast.walk(assembly) if isinstance(node, ast.For)
                        and ast.unparse(node.iter) == 'empty_pages' and any(
                            isinstance(child, ast.Try) for child in node.body))
        guarded = next(node for node in fallback.body if isinstance(node, ast.Try))
        require(bool(guarded.handlers) and not any(isinstance(child, (ast.Break, ast.Return)) for child in ast.walk(guarded)),
                'fallback failures must terminate only their own page attempt')
        require(_contains(guarded.handlers[0], 'row["attempts"].append({"backend": fallback, "status": "FAILED", "diagnostic": diagnostic})'),
                'fallback failure diagnostics must retain the attempted backend')
        require(any(isinstance(node, ast.For) and ast.unparse(node.iter) == 'completed'
                    and _contains(node, 'quality["ocr_decisions"].append({"page_index": index, "decision": "LOCAL_PAGE_PARSE_COMPLETED"})')
                    for node in assembly.body), 'successful page completion must not depend on sibling failures')
        frames = functions['_frames']
        require(any(isinstance(node, ast.For) and ast.unparse(node.iter) == 'sorted(needed - completed)'
                    for node in ast.walk(frames)), 'PDF recovery must exclude completed pages')
        require(_contains(frames, 'if index in completed:\n    continue'),
                'image recovery must exclude completed frames')
        require(_contains(frames, 'completed = {item["page_index"] for item in bundle["quality_report"].get("ocr_decisions", []) '
                          'if item.get("decision") in {"OCR_COMPLETED", "LOCAL_PAGE_PARSE_COMPLETED"} and "page_index" in item}'),
                'completion identity must come from successful page decisions')
        augment = functions['augment_ocr']
        require(_contains(augment, 'if not attempts and not failures:\n    return result'),
                'completed OCR recovery must be an exact no-op')
        require(_contains(augment, 'report["warnings"] = [item for item in report["warnings"] if not ('
                          'item.get("page_index") in completed and (item.get("stage") == "ocr" or item.get("code") in completed_codes))]'),
                'only recovered-page OCR diagnostics may be cleared during partial recovery')
    except (OSError, SyntaxError, KeyError, StopIteration, AttributeError) as error:
        errors.append('page_recovery: invalid transition structure: ' + type(error).__name__)
    return errors
