import html
import json
import os
import secrets
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from .core import Actor, Platform, PlatformError
from .documents import max_upload_bytes, render_pdf_preview
from .settings import Settings


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewKB(RequestModel):
    name: str = Field(min_length=1, max_length=120)


class MemberUpdate(RequestModel):
    capabilities: list[str]


class SourceReaders(RequestModel):
    readers: list[str]


class SourcePolicy(RequestModel):
    policy: dict


class BindingRequest(RequestModel):
    source_id: str
    source_version_id: str


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
               agent_factory=None, model_clients=None, profile=None, extra_plugins=()):
    from .application_composition import create_root_application

    settings = settings if settings is not None else Settings.from_environment()
    identities = settings.identities()
    composition = create_root_application(settings, platform=platform, profile=profile,
        model_clients=model_clients, service_factory=agent_factory, extra_plugins=extra_plugins,
        remote=False if platform is not None else None)
    service, research, models = composition.platform, composition.research, composition.models
    provider = service.embedding_provider if service is not None else None
    luna = service.ocr_client if service is not None else None
    app = FastAPI(title="文档证据与单库检索", version="0.2.0")
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
            for token, principal in identities.items():
                if secrets.compare_digest(credentials.credentials, token):
                    return principal
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
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": str(exc)})

    @app.exception_handler(psycopg.Error)
    async def authority_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"error": "AUTHORITY_UNAVAILABLE"})

    if models is not None:
        from .remote import RemoteModelError
        from .embeddings import EmbeddingError

        @app.exception_handler(RemoteModelError)
        async def remote_error(request, exc):
            return JSONResponse(status_code=502, content={'error': exc.code, 'provider_http_status': exc.status})

        @app.exception_handler(EmbeddingError)
        async def embedding_error(request, exc):
            return JSONResponse(status_code=502, content={'error': 'EMBEDDING_FAILED', 'detail': str(exc)})


    @app.get("/health")
    def health():
        return {"status": "running", "readiness": "Use authenticated requests to verify dependencies"}

    @app.get("/", response_class=HTMLResponse)
    def home():
        return (Path(__file__).parent / "console.html").read_text(encoding="utf-8")

    @app.get("/identity")
    def whoami(actor: principal):
        return {"tenant_id": actor.tenant_id, "principal_id": actor.principal_id}

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
               source_id: str | None = Form(None)):
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
                              readers_value, policy_value, source_id)

    @app.post("/knowledge-bases/{kb_id}/bindings")
    def bind(kb_id: str, body: BindingRequest, actor: principal):
        return service.bind_source(actor, kb_id, body.source_id, body.source_version_id)

    @app.delete("/knowledge-bases/{kb_id}/bindings/{binding_id}")
    def remove(kb_id: str, binding_id: str, actor: principal):
        return service.remove_binding(actor, kb_id, binding_id)

    @app.put("/sources/{source_id}/readers")
    def readers(source_id: str, body: SourceReaders, actor: principal):
        return service.set_source_readers(actor, source_id, body.readers)

    @app.put("/sources/{source_id}/policy")
    def policy(source_id: str, body: SourcePolicy, actor: principal):
        return service.set_source_policy(actor, source_id, body.policy)

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
        target = service.resolve_citation(actor, kb_id, citation_id, purpose="download")
        content = Path(target["original_path"]).read_bytes()
        service.resolve_citation(actor, kb_id, citation_id, purpose="download")
        return Response(content, media_type="application/octet-stream",
                        headers={"Content-Disposition": "attachment"})

    @app.get("/knowledge-bases/{kb_id}/citations/{citation_id}/preview")
    def preview(kb_id: str, citation_id: str, actor: principal):
        target = service.resolve_citation(actor, kb_id, citation_id)
        if target["media_type"] == "application/pdf":
            data = render_pdf_preview(Path(target["original_path"]).read_bytes(), target["anchors"][0])
            service.resolve_citation(actor, kb_id, citation_id)
            return Response(data, media_type="image/png")
        if target['media_type'].startswith('image/') and target['anchors'][0].get('selector_type') == 'image_region':
            from .ocr import render_image_preview

            data = render_image_preview(Path(target['original_path']).read_bytes(), target['anchors'][0])
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
