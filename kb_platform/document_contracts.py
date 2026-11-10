from .knowledge_contracts import OBJECT, TEXT, object_schema


BOOLEAN = {'type': 'boolean'}
NULL_TEXT = {'type': ['string', 'null']}
REVISION = {'type': 'integer', 'minimum': 0}
EXPECTED_REVISION = {'type': ['integer', 'null'], 'minimum': 0}
STRINGS = {'type': 'array', 'items': TEXT}
DOCUMENT = object_schema({
    'source_id': TEXT, 'binding_id': TEXT, 'filename': TEXT, 'source_version_id': TEXT,
    'parse_snapshot_id': TEXT, 'parse_status': TEXT, 'binding_status': TEXT,
    'source_revision': REVISION, 'binding_revision': REVISION, 'policy': OBJECT,
    'published_version_id': NULL_TEXT, 'published_parse_snapshot_id': NULL_TEXT,
    'needs_publication': BOOLEAN, 'human_search_ready': BOOLEAN, 'qa_ready': BOOLEAN,
    'blockers': STRINGS, 'build_blockers': STRINGS, 'quality_report': OBJECT})
DOCUMENTS_RESPONSE = object_schema({'kb_id': TEXT, 'authority_revision': REVISION,
    'manifest_id': NULL_TEXT, 'provider_id': NULL_TEXT, 'documents': {'type': 'array', 'items': DOCUMENT}})
VERSIONS_RESPONSE = object_schema({'source_id': TEXT, 'versions': {'type': 'array', 'items': object_schema({
    'source_version_id': TEXT, 'filename': TEXT, 'original_sha256': TEXT, 'media_type': TEXT})}})
