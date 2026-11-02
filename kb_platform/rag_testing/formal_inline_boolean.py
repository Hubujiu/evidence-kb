from __future__ import annotations

import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


RULE = 'INLINE_PRIMED_BOOLEAN_REVIEW'
FIXTURE_DIRECTORY = 'formal/controls/inline_boolean'
MAX_XML_BYTES = 16 * 1024 * 1024
CONTROL_EXPECTATIONS = {
    'postflight-bad': ('PostflightAuthority', [(83, 'commitAllowed', 'evidenceAllowed'),
                                             (92, 'evidenceChecked', 'valid')]),
    'postflight-good': ('PostflightAuthority', []),
    'projection-bad': ('ErrorProjection', [(31, 'privateVisible', '=')]),
    'projection-good': ('ErrorProjection', []),
    'safe-shapes': ('InlineSafeShapes', []),
    'intentional-guard': ('InlineIntentionalGuard', [(3, 'x', 'B')]),
    'scope-limits': ('InlineScopeLimits', []),
}


def one(node: ET.Element, path: str) -> ET.Element:
    matches = node.findall(path)
    if len(matches) != 1:
        raise ValueError(f'Invalid SANY XML: expected one {path} in {node.tag}')
    return matches[0]


def required_text(node: ET.Element, path: str) -> str:
    text = one(node, path).text
    if not text:
        raise ValueError(f'Invalid SANY XML: missing text at {path}')
    return text


def inline_boolean_findings(xml: bytes, module: str) -> list[dict]:
    if not isinstance(xml, bytes) or len(xml) > MAX_XML_BYTES:
        raise ValueError('Invalid SANY XML: expected at most 16 MiB of exported bytes')
    try:
        document = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError(f'Invalid SANY XML: {exc}') from exc
    if document.tag != 'modules' or required_text(document, 'RootModule') != module:
        raise ValueError('Invalid SANY XML: root module mismatch')
    entries = {}
    for entry in one(document, 'context'):
        uid = required_text(entry, 'UID')
        values = [item for item in entry if item.tag != 'UID']
        if entry.tag != 'entry' or not uid.isdecimal() or uid in entries or len(values) != 1:
            raise ValueError('Invalid SANY XML: malformed or duplicate context entry')
        entries[uid] = values[0]
    if not entries:
        raise ValueError('Invalid SANY XML: empty context')
    if sum(entry.tag == 'ModuleNode' and entry.findtext('uniquename') == module
           for entry in entries.values()) != 1:
        raise ValueError('Invalid SANY XML: missing or duplicate root module declaration')

    def operator(node):
        if node.tag != 'OpApplNode':
            return None, None
        reference = one(node, 'operator')
        if len(reference) != 1:
            raise ValueError('Invalid SANY XML: operator requires one reference')
        uid = required_text(reference[0], 'UID')
        definition = entries.get(uid)
        if definition is None or reference[0].tag != definition.tag + 'Ref':
            raise ValueError(f'Invalid SANY XML: unresolved operator reference {uid}')
        return definition.tag, required_text(definition, 'uniquename')

    def builtin(node, name):
        return operator(node) == ('BuiltInKind', name)

    def operands(node):
        return list(one(node, 'operands'))

    def primed_assignment(node):
        if not builtin(node, '='):
            return False
        arguments = operands(node)
        if len(arguments) != 2:
            raise ValueError('Invalid SANY XML: equality arity')
        if not builtin(arguments[0], "'"):
            return False
        if len(operands(arguments[0])) != 1:
            raise ValueError('Invalid SANY XML: prime arity')
        return True

    def location(node):
        source = required_text(node, 'location/filename')
        positions = [int(required_text(node, f'location/{kind}/{edge}'))
                     for kind, edge in [('line', 'begin'), ('column', 'begin'),
                                        ('line', 'end'), ('column', 'end')]]
        if source == module and (min(positions) < 1 or tuple(positions[:2]) > tuple(positions[2:])):
            raise ValueError('Invalid SANY XML: source location')
        return source, positions

    findings = []
    applications = list(document.iter('OpApplNode'))
    local_nodes = 0
    for node in applications:
        operation = operator(node)
        arguments = operands(node)
        filename, _ = location(node)
        local_nodes += filename == module
        if filename != module or operation != ('BuiltInKind', '\\land'):
            continue
        if len(arguments) != 2:
            raise ValueError('Invalid SANY XML: conjunction arity')
        left, right = arguments
        if not primed_assignment(left) or primed_assignment(right) or builtin(right, 'UNCHANGED'):
            continue
        left_source, left_location = location(left)
        right_source, right_location = location(right)
        if left_source != module or right_source != module or left_location[2] != right_location[0]:
            continue
        assigned = operator(operands(operands(left)[0])[0])[1]
        findings.append({'rule': RULE, 'module': module, 'line': left_location[0],
                         'column': left_location[1], 'assignment': assigned,
                         'assignment_end_line': left_location[2],
                         'assignment_end_column': left_location[3],
                         'guard_column': right_location[1], 'guard_operator': operator(right)[1],
                         'message': 'SANY parsed a primed equality followed by an independent inline guard. '
                                    'Parenthesize the whole Boolean RHS if it is the assigned value; '
                                    'otherwise put the intended guard on a separate conjunct line. '
                                    'Review required; no automatic semantic repair.'})
    if not applications or not local_nodes:
        raise ValueError('Invalid SANY XML: no root-module expressions')
    return sorted(findings, key=lambda row: (row['line'], row['column'], row['guard_column']))


def finding_signature(findings: list[dict]) -> list[tuple]:
    return [(row['line'], row['assignment'], row['guard_operator']) for row in findings]


def inline_control_fixtures(root: Path) -> list[dict]:
    directory = root / FIXTURE_DIRECTORY
    manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or
            not isinstance(manifest.get('fixtures'), dict) or
            set(manifest['fixtures']) != set(CONTROL_EXPECTATIONS)):
        raise ValueError('Inline review controls require the complete pinned fixture set')
    lock = json.loads((root / 'formal/toolchain-v1.json').read_text('utf-8'))
    if manifest.get('exporter_jar_sha256') != lock['sha256']:
        raise ValueError('Inline review fixtures require the pinned exporter toolchain')
    result = []
    for identity, (module, expected) in CONTROL_EXPECTATIONS.items():
        metadata = manifest['fixtures'][identity]
        if not isinstance(metadata, dict) or metadata.get('module') != module:
            raise ValueError(f'Invalid inline review fixture metadata: {identity}')
        source_path = directory / identity / f'{module}.tla.txt'
        xml_path = directory / identity / f'{module}.xml'
        source = source_path.read_bytes()
        xml = xml_path.read_bytes()
        if (hashlib.sha256(source).hexdigest() != metadata.get('source_sha256') or
                hashlib.sha256(xml).hexdigest() != metadata.get('xml_sha256')):
            raise ValueError(f'Inline review fixture integrity mismatch: {identity}')
        if finding_signature(inline_boolean_findings(xml, module)) != expected:
            raise ValueError(f'Inline review fixture expectation mismatch: {identity}')
        result.append({'id': identity, 'module': module, 'source': source,
                       'source_path': source_path.relative_to(root).as_posix(),
                       'source_sha256': metadata['source_sha256'],
                       'xml_sha256': metadata['xml_sha256'], 'expected': expected})
    return result
