from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class JsonValue:
    obj: object


class KnowledgeQuery(StrEnum):
    CORE_BUMP_1 = 'UPDATE tenant_state SET revision=revision+1 WHERE tenant_id=%s'
    CORE_BUMP_2 = 'UPDATE knowledge_bases SET revision=revision+1 WHERE tenant_id=%s AND kb_id=%s'
    CORE_CREATE_KB_1 = 'INSERT INTO tenant_state (tenant_id) VALUES (%s) ON CONFLICT DO NOTHING'
    CORE_CREATE_KB_2 = 'INSERT INTO knowledge_bases (tenant_id,kb_id,name,owner_id) VALUES (%s,%s,%s,%s)'
    CORE_CREATE_KB_3 = 'INSERT INTO members VALUES (%s,%s,%s,%s)'
    CORE_SET_MEMBER_1 = 'INSERT INTO members VALUES (%s,%s,%s,%s) ON CONFLICT (tenant_id,kb_id,principal_id) DO UPDATE SET capabilities=EXCLUDED.capabilities'
    CORE_INGEST_1 = 'INSERT INTO source_versions VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING'
    CORE_INGEST_2 = 'INSERT INTO parse_snapshots VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING'
    CORE_SET_SOURCE_READERS_1 = 'UPDATE sources SET readers=%s,revision=revision+1 WHERE tenant_id=%s AND source_id=%s'
    CORE_SET_SOURCE_POLICY_1 = 'UPDATE sources SET policy=%s,revision=revision+1 WHERE tenant_id=%s AND source_id=%s'
    CORE_STAGE_1 = "INSERT INTO builds VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'STAGING',%s)"
    CORE_STAGE_2 = "UPDATE builds SET status='VALIDATED' WHERE tenant_id=%s AND kb_id=%s AND build_id=%s"
    CORE_RESUME_BUILD_1 = "UPDATE builds SET status='VALIDATED' WHERE tenant_id=%s AND kb_id=%s AND build_id=%s AND status='STAGING'"
    CORE_PUBLISH_1 = 'UPDATE knowledge_bases SET manifest_id=%s WHERE tenant_id=%s AND kb_id=%s'
    CORE_PUBLISH_2 = "UPDATE builds SET status='PUBLISHED' WHERE tenant_id=%s AND kb_id=%s AND build_id=%s"
    CORE_AUTH_1 = 'SELECT k.*, m.capabilities FROM knowledge_bases k JOIN members m USING (tenant_id,kb_id) WHERE k.tenant_id=%s AND k.kb_id=%s AND m.principal_id=%s'
    CORE_LOCK_AUTHORITY_1 = 'SELECT revision FROM tenant_state WHERE tenant_id=%s FOR UPDATE'
    CORE_REVISION_1 = 'SELECT revision FROM tenant_state WHERE tenant_id=%s'
    CORE_SOURCE_1 = 'SELECT * FROM sources WHERE tenant_id=%s AND source_id=%s'
    CORE_INGEST_3 = 'UPDATE sources SET readers=%s,policy=%s,revision=revision+1 WHERE tenant_id=%s AND source_id=%s'
    CORE_INGEST_4 = 'INSERT INTO sources (tenant_id,source_id,owner_id,readers,policy) VALUES (%s,%s,%s,%s,%s)'
    CORE_BIND_1 = "INSERT INTO bindings (tenant_id,kb_id,binding_id,source_id,source_version_id,parse_snapshot_id,status) VALUES (%s,%s,%s,%s,%s,%s,'ACTIVE') ON CONFLICT (tenant_id,kb_id,source_id) DO UPDATE SET source_version_id=EXCLUDED.source_version_id,parse_snapshot_id=EXCLUDED.parse_snapshot_id,status='ACTIVE',revision=bindings.revision+1 RETURNING binding_id,source_id,source_version_id"
    CORE_BINDINGS_1 = "SELECT b.*,s.owner_id,s.readers,s.policy,s.revision AS source_revision,p.document FROM bindings b JOIN sources s USING (tenant_id,source_id) JOIN parse_snapshots p USING (tenant_id,source_id,source_version_id,parse_snapshot_id) WHERE b.tenant_id=%s AND b.kb_id=%s AND b.status='ACTIVE' ORDER BY b.binding_id"
    CORE_STAGE_3 = 'INSERT INTO citations VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)'
    CORE_LIST_KBS_1 = "SELECT k.kb_id,k.name,k.manifest_id,m.capabilities FROM knowledge_bases k JOIN members m USING (tenant_id,kb_id) WHERE k.tenant_id=%s AND m.principal_id=%s AND m.capabilities<>'[]'::jsonb ORDER BY k.name,k.kb_id"
    CORE_SET_MEMBER_2 = 'SELECT capabilities FROM members WHERE tenant_id=%s AND kb_id=%s AND principal_id=%s'
    CORE_BIND_SOURCE_1 = 'SELECT parse_snapshot_id FROM parse_snapshots WHERE tenant_id=%s AND source_id=%s AND source_version_id=%s ORDER BY parse_snapshot_id LIMIT 1'
    CORE_REMOVE_BINDING_1 = "UPDATE bindings SET status='REMOVED',revision=revision+1 WHERE tenant_id=%s AND kb_id=%s AND binding_id=%s RETURNING binding_id,status"
    CORE_STAGE_4 = "UPDATE builds SET status='FAILED' WHERE tenant_id=%s AND kb_id=%s AND build_id=%s"
    CORE_BUILD_STATUS_1 = 'SELECT build_id,status,unit_count FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s'
    CORE_RESUME_BUILD_2 = 'SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s'
    CORE_RESUME_BUILD_3 = 'SELECT * FROM citations WHERE tenant_id=%s AND kb_id=%s AND build_id=%s'
    CORE_PUBLISH_3 = 'SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s FOR UPDATE'
    CORE_SEARCH_1 = "SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s AND status='PUBLISHED'"
    CORE_RESOLVE_CITATION_1 = "SELECT c.*,p.document,v.filename,v.original_path,v.original_sha256,v.media_type,s.policy,s.owner_id,s.readers FROM citations c JOIN bindings b USING (tenant_id,kb_id,binding_id) JOIN builds d ON c.tenant_id=d.tenant_id AND c.kb_id=d.kb_id AND c.build_id=d.build_id JOIN source_versions v ON c.tenant_id=v.tenant_id AND c.source_id=v.source_id AND c.source_version_id=v.source_version_id JOIN sources s ON c.tenant_id=s.tenant_id AND c.source_id=s.source_id JOIN parse_snapshots p ON c.tenant_id=p.tenant_id AND c.source_id=p.source_id AND c.source_version_id=p.source_version_id AND c.parse_snapshot_id=p.parse_snapshot_id WHERE c.tenant_id=%s AND c.kb_id=%s AND c.citation_id=%s AND b.source_id=c.source_id AND b.status='ACTIVE' AND d.status='PUBLISHED'"
    CORE_SEARCH_2 = 'SELECT * FROM citations WHERE tenant_id=%s AND kb_id=%s AND citation_id=%s AND build_id=%s'
    AGENT_TOOLS_CANONICAL_METADATA_1 = 'SELECT c.element_id,p.document FROM citations c JOIN parse_snapshots p USING (tenant_id,source_id,source_version_id,parse_snapshot_id) WHERE c.tenant_id=%s AND c.kb_id=%s AND c.citation_id=%s AND c.build_id=%s AND c.source_id=%s AND c.source_version_id=%s AND c.parse_snapshot_id=%s'
    AGENT_TOOLS_STATE_1 = "SELECT b.*,s.owner_id,s.readers,s.policy,v.filename,jsonb_build_object('processing_policy',p.document->'processing_policy') AS document FROM bindings b JOIN sources s USING (tenant_id,source_id) JOIN parse_snapshots p USING (tenant_id,source_id,source_version_id,parse_snapshot_id) JOIN source_versions v USING (tenant_id,source_id,source_version_id) WHERE b.tenant_id=%s AND b.kb_id=%s AND b.status='ACTIVE' ORDER BY b.binding_id"
    AGENT_TOOLS_STATE_2 = "SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s AND status='PUBLISHED'"
    SEMANTIC_PAGES_V2_CATALOG_1 = 'SELECT * FROM citations WHERE tenant_id=%s AND kb_id=%s AND build_id=%s'
    SEMANTIC_PAGES_V2_LOCKED_1 = "SELECT * FROM builds WHERE tenant_id=%s AND kb_id=%s AND build_id=%s AND status='PUBLISHED'"
    SEMANTIC_PAGES_V2_CATALOG_2 = 'SELECT original_path,original_sha256 FROM source_versions WHERE tenant_id=%s AND source_id=%s AND source_version_id=%s'
