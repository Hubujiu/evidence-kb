import html
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .core import Actor, Platform, PlatformError
from .database_contracts import DatabaseError
from .storage_sdk import StorageError
from .plugin_contracts import ContractError
from .documents import max_upload_bytes, render_pdf_preview
from .settings import Settings
from .agent_responses import DocumentsResponse, SourceVersionsResponse
from .public_errors import PUBLIC_ERROR_SCHEMA, public_error, public_error_headers
from .configuration_diagnostics import configuration_diagnostics


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewKB(RequestModel):
    name: str = Field(min_length=1, max_length=120)


class MemberUpdate(RequestModel):
    capabilities: list[str]


class SourceReaders(RequestModel):
    readers: list[str]
    expected_revision: int | None = Field(default=None, ge=0)


class SourcePolicy(RequestModel):
    policy: dict
    expected_revision: int | None = Field(default=None, ge=0)


class BindingRequest(RequestModel):
    source_id: str
    source_version_id: str
    expected_revision: int | None = Field(default=None, ge=0)


class ReparseRequest(RequestModel):
    expected_revision: int | None = Field(default=None, ge=0)


class BuildRequest(RequestModel):
    channels: list[Literal["bm25", "structure", "table", "dense", "hybrid"]] | None = None


class SearchRequest(RequestModel):
    query: str = Field(min_length=1, max_length=2000)
    channel: Literal["bm25", "structure", "table", "dense", "hybrid", "rerank"] = "bm25"
    top_k: int = Field(default=5, ge=1, le=30)
    context_budget: int = Field(default=4000, ge=100, le=16000)
    purpose: Literal["human_search", "llm_context"] = "human_search"


class TableRequest(RequestModel):
    operation: Literal["rows", "sum", "count", "min", "max"] = "rows"
    column: str | None = None
    filters: list | None = None


def create_app(settings: Settings | None = None, platform: Platform | None = None, *,
               agent_factory=None, model_clients=None, profile=None, extra_plugins=(),
               storage_configuration=None, provider_catalog=None, workflow_options=None, semantic_observer=None,
               models=None):
    from .application_composition import create_root_application

    settings = settings if settings is not None else Settings.from_environment()
    composition = create_root_application(settings, platform=platform, profile=profile,
        model_clients=model_clients, models=models, service_factory=agent_factory, extra_plugins=extra_plugins,
        remote=False if platform is not None else None, storage_configuration=storage_configuration,
        provider_catalog=provider_catalog, workflow_options=workflow_options,
        semantic_observer=semantic_observer, authentication=True)
    service, research, models = composition.platform, composition.research, composition.models
    provider = service.embedding_provider if service is not None else None
    luna = service.ocr_client if service is not None else None
    app = FastAPI(title="文档证据与单库检索", version="0.2.0", responses={
        code: {'content': {'application/json': {'schema': PUBLIC_ERROR_SCHEMA}}}
        for code in (400, 403, 409, 429, 502, 503)})
    app.state.platform, app.state.research, app.state.models = service, research, models
    app.state.model_clients = model_clients
    app.state.root_plugins = composition.host
    app.router.add_event_handler('shutdown', composition.close)
    if service is None:
        @app.get('/health')
        def native_health():
            return {'status': 'running', 'profile': 'native', 'capabilities': []}

        app.state.agent, app.state.agent_plugins = None, composition.host
        return app
    bearer = HTTPBearer(auto_error=False)

    def identity(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        if credentials is not None and credentials.scheme.lower() == "bearer":
            if composition.authenticate is not None:
                return composition.authenticate(credentials.credentials)
            actor = composition.host.call('authentication', 'authenticate', credentials.credentials)
            if isinstance(actor, Actor):
                return actor
        raise HTTPException(401, detail="身份验证失败", headers={"WWW-Authenticate": "Bearer"})

    principal = Annotated[Actor, Depends(identity)]

    @app.middleware("http")
    async def protected_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(PlatformError)
    @app.exception_handler(ContractError)
    async def domain_error(request, exc):
        payload = public_error(exc)
        return JSONResponse(status_code=exc.status_code, content=payload, headers=public_error_headers(payload))

    @app.exception_handler(DatabaseError)
    @app.exception_handler(StorageError)
    async def authority_unavailable(request, exc):
        payload = public_error(exc, code='COMMIT_UNKNOWN' if getattr(exc, 'code', None) == 'COMMIT_UNKNOWN'
            else 'AUTHORITY_UNAVAILABLE', message='Authority unavailable or commit awaiting reconciliation')
        return JSONResponse(status_code=503, content=payload)

    if models is not None:
        from .remote import RemoteModelError
        from .embeddings import EmbeddingError

        @app.exception_handler(RemoteModelError)
        async def remote_error(request, exc):
            payload = public_error(exc)
            return JSONResponse(status_code=502, content=payload, headers=public_error_headers(payload))

        @app.exception_handler(EmbeddingError)
        async def embedding_error(request, exc):
            payload = public_error(exc, code='EMBEDDING_FAILED')
            return JSONResponse(status_code=502, content=payload, headers=public_error_headers(payload))


    @app.get("/health")
    def health():
        return {"status": "running", "readiness": "Use authenticated requests to verify dependencies"}

    web_root = Path(__file__).resolve().parents[1] / 'web' / 'dist'

    @app.get("/", response_class=HTMLResponse)
    def home():
        index = web_root / 'index.html'
        if not index.is_file():
            return HTMLResponse('Web 界面尚未构建。请在 web 目录运行 npm ci 和 npm run build。', status_code=503)
        return HTMLResponse(index.read_text(encoding='utf-8'))

    @app.get('/assets/{asset_path:path}')
    def web_asset(asset_path: str):
        root = (web_root / 'assets').resolve()
        path = (root / asset_path).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, 'Asset not found')
        return FileResponse(path)


    @app.get('/console.mjs')
    def console_module():
        return Response((Path(__file__).parent / 'console.mjs').read_text(encoding='utf-8'),
                        media_type='text/javascript')

    @app.get('/console', response_class=HTMLResponse)
    def console():
        return HTMLResponse((Path(__file__).parent / 'console.html').read_text(encoding='utf-8'))

    @app.get("/identity")
    def whoami(actor: principal):
        return {"tenant_id": actor.tenant_id, "principal_id": actor.principal_id}

    @app.get('/diagnostics')
    def diagnostics(actor: principal):
        return configuration_diagnostics(composition)

    @app.get("/capabilities")
    def capabilities(actor: principal):
        return {
            "available": ["native_parse", "bm25", "structure", "table", "citations"],
            "experimental_available": ["dense", "hybrid"] if service.embedding_provider and not research else [],
            "remote_configured": {'luna': luna.manifest if luna else None,
                                  'embedding': provider.manifest if provider else None} if models else None,
            "scope_only": False,
            "configured_requires_verification": (["ocr_model"] if luna else []) +
                (["dense", "hybrid"] if provider else []) + (["rerank"] if research else []),
            "not_run": None if research else ["ocr_model", "rerank", "visual"],
            "not_implemented": ["visual"],
            "availability_note": 'Provider configuration does not prove a successful call; each scope needs its own build.',
            "model_channels_default_enabled": research is not None,
            "scope_retrieval_channels": ["bm25", "dense"] if research else [],
            "scope_search_default": None,
            "agent_conversations": {'ui': '/agent', 'protocol': 'agentic-rag/5.0'} if research else None,
            "production_release": "INCONCLUSIVE",
        }

    @app.get("/knowledge-bases")
    def list_kbs(actor: principal):
        return service.list_kbs(actor)

    @app.post("/knowledge-bases", status_code=201)
    def new_kb(body: NewKB, actor: principal):
        return service.create_kb(actor, body.name)


    @app.get('/scopes')
    def scopes(actor: principal):
        return [{**item, 'scope_id': item['kb_id']} for item in service.list_kbs(actor)]

    @app.post('/scopes', status_code=201)
    def create_scope(body: NewKB, actor: principal):
        result = service.create_kb(actor, body.name)
        return {**result, 'scope_id': result['kb_id']}


    @app.put("/knowledge-bases/{kb_id}/members/{principal_id}")
    def member(kb_id: str, principal_id: str, body: MemberUpdate, actor: principal):
        return service.set_member(actor, kb_id, principal_id, body.capabilities)

    @app.post("/knowledge-bases/{kb_id}/sources", status_code=201)
    def upload(kb_id: str, actor: principal, file: UploadFile = File(...),
               readers: str | None = Form(None), policy: str | None = Form(None),
               source_id: str | None = Form(None), expected_revision: int | None = Form(None, ge=0)):
        limit = max_upload_bytes()
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(413, f"文件超过 {limit / (1024 * 1024):g} MiB 配置限制")
        try:
            readers_value = json.loads(readers) if readers is not None else None
            policy_value = json.loads(policy) if policy is not None else None
            if readers is not None and (not isinstance(readers_value, list) or not all(isinstance(x, str) for x in readers_value)):
                raise ValueError("readers")
            if policy is not None and not isinstance(policy_value, dict):
                raise ValueError("policy")
        except (ValueError, TypeError):
            raise HTTPException(422, "readers 必须为字符串数组，policy 必须为对象") from None
        return service.ingest(actor, kb_id, Path(file.filename or "upload.txt").name, data,
                              readers_value, policy_value, source_id,
                              **({} if expected_revision is None else {'expected_revision': expected_revision}))

    @app.get('/knowledge-bases/{kb_id}/documents', response_model=DocumentsResponse)
    def documents(kb_id: str, actor: principal, provider_id: str | None = Query(None, min_length=1, max_length=200),
                  include_removed: bool = False):
        agent = getattr(app.state, 'agent', None)
        selected = provider_id or (agent.model_status().get('provider_id') if agent is not None else None)
        return service.documents(actor, kb_id, provider_id=selected, include_removed=include_removed)

    @app.get('/knowledge-bases/{kb_id}/sources/{source_id}/versions', response_model=SourceVersionsResponse)
    def source_versions(kb_id: str, source_id: str, actor: principal):
        return service.source_versions(actor, kb_id, source_id)

    @app.post('/knowledge-bases/{kb_id}/sources/{source_id}/reparse')
    def reparse_source(kb_id: str, source_id: str, actor: principal, body: ReparseRequest):
        return service.reparse_source(actor, kb_id, source_id,
            **({} if body.expected_revision is None else {'expected_revision': body.expected_revision}))

    @app.post("/knowledge-bases/{kb_id}/bindings")
    def bind(kb_id: str, body: BindingRequest, actor: principal):
        return service.bind_source(actor, kb_id, body.source_id, body.source_version_id,
            **({} if body.expected_revision is None else {'expected_revision': body.expected_revision}))

    @app.delete("/knowledge-bases/{kb_id}/bindings/{binding_id}")
    def remove(kb_id: str, binding_id: str, actor: principal, expected_revision: int | None = Query(None, ge=0)):
        return service.remove_binding(actor, kb_id, binding_id,
            **({} if expected_revision is None else {'expected_revision': expected_revision}))

    @app.put("/sources/{source_id}/readers")
    def readers(source_id: str, body: SourceReaders, actor: principal):
        return service.set_source_readers(actor, source_id, body.readers,
            **({} if body.expected_revision is None else {'expected_revision': body.expected_revision}))

    @app.put("/sources/{source_id}/policy")
    def policy(source_id: str, body: SourcePolicy, actor: principal):
        return service.set_source_policy(actor, source_id, body.policy,
            **({} if body.expected_revision is None else {'expected_revision': body.expected_revision}))

    @app.post("/knowledge-bases/{kb_id}/builds", status_code=201)
    def stage(kb_id: str, actor: principal, body: BuildRequest | None = Body(default=None)):
        return service.stage(actor, kb_id, channels=body.channels if body else None)

    @app.post("/knowledge-bases/{kb_id}/builds/{build_id}/publish")
    def publish(kb_id: str, build_id: str, actor: principal):
        return service.publish(actor, kb_id, build_id)

    @app.get("/knowledge-bases/{kb_id}/builds/{build_id}")
    def build_status(kb_id: str, build_id: str, actor: principal):
        return service.build_status(actor, kb_id, build_id)

    @app.post("/knowledge-bases/{kb_id}/builds/{build_id}/resume")
    def resume_build(kb_id: str, build_id: str, actor: principal):
        return service.resume_build(actor, kb_id, build_id)

    @app.post("/knowledge-bases/{kb_id}/search")
    def search(kb_id: str, body: SearchRequest, actor: principal):
        return service.search(actor, kb_id, **body.model_dump())

    @app.get("/knowledge-bases/{kb_id}/citations/{citation_id}")
    def citation(kb_id: str, citation_id: str, actor: principal):
        result = dict(service.resolve_citation(actor, kb_id, citation_id))
        result.pop("original_path", None)
        return result

    @app.get("/knowledge-bases/{kb_id}/citations/{citation_id}/original")
    def original(kb_id: str, citation_id: str, actor: principal):
        content = service.read_original(actor, kb_id, citation_id, purpose="download")
        return Response(content, media_type="application/octet-stream",
                        headers={"Content-Disposition": "attachment"})

    @app.get("/knowledge-bases/{kb_id}/citations/{citation_id}/preview")
    def preview(kb_id: str, citation_id: str, actor: principal):
        target = service.resolve_citation(actor, kb_id, citation_id)
        if target["media_type"] == "application/pdf":
            data = render_pdf_preview(service.read_original(actor, kb_id, citation_id, purpose="preview"),
                                      target["anchors"][0])
            service.resolve_citation(actor, kb_id, citation_id)
            return Response(data, media_type="image/png")
        if target['media_type'].startswith('image/') and target['anchors'][0].get('selector_type') == 'image_region':
            from .ocr import render_image_preview

            data = render_image_preview(service.read_original(actor, kb_id, citation_id, purpose='preview'),
                                        target['anchors'][0])
            service.resolve_citation(actor, kb_id, citation_id)
            return Response(data, media_type='image/png')
        excerpt = html.escape(target["text"])
        body = f'<!doctype html><meta charset="utf-8"><title>结构定位摘录</title><p>结构定位摘录</p><pre><mark>{excerpt}</mark></pre>'
        service.resolve_citation(actor, kb_id, citation_id)
        return HTMLResponse(body, headers={"Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.post("/knowledge-bases/{kb_id}/citations/{citation_id}/table")
    def table(kb_id: str, citation_id: str, body: TableRequest, actor: principal):
        return service.table_query(actor, kb_id, citation_id, **body.model_dump())

    from .agent_api import install_agent_routes

    install_agent_routes(app, service, research, identity, service_factory=agent_factory,
                         application=composition.application, host=composition.host)
    return app


app = None if os.getenv('KB_DEFER_DEFAULT_APP') == '1' else create_app()
