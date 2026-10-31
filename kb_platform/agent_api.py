from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_policy: dict | None = None
    config_name: str = 'recommended'


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=20000)
    request_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    source_policy: dict | None = None
    constraints: list[str] | None = None
    workflow_state: dict | None = None


def install_agent_routes(app, platform, research, identity, *, service_factory=None):
    from .knowledge_profile import create_knowledge_application
    from .core import Actor

    service = None
    host = None
    if research is not None:
        service, host = create_knowledge_application(platform, research, service_factory=service_factory)
        app.router.add_event_handler('shutdown', host.close)
    app.state.agent = service
    app.state.agent_plugins = host
    principal = Annotated[Actor, Depends(identity)]

    def require():
        current = app.state.agent
        if current is None:
            raise HTTPException(409, 'Configure KB_REMOTE_MODELS=1 before starting an agent conversation')
        return current

    @app.get('/agent', response_class=HTMLResponse)
    def console():
        return Path(__file__).with_name('agent_console.html').read_text(encoding='utf-8')

    @app.get('/agent/capabilities')
    def capabilities(actor: principal):
        return require().capabilities()

    @app.get('/agent/runtime-status')
    def runtime_status(actor: principal):
        status = getattr(app.state, 'delivery_status', None)
        return status(actor) if status else {'configured': False, 'status': 'NOT_CONFIGURED'}

    @app.get('/agent/readiness')
    def readiness(actor: principal):
        check = getattr(app.state, 'delivery_readiness', None)
        if check is None:
            return JSONResponse({'status': 'NOT_CONFIGURED', 'model_calls': 0}, status_code=503)
        result = check(actor)
        return JSONResponse(result, status_code=200 if result['ready'] else 503)

    @app.get('/agent/sessions')
    def sessions(actor: principal, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
        return require().list_sessions(actor, limit=limit, offset=offset)

    @app.get('/agent/sessions/{session_id}/export')
    def export_session(session_id: str, actor: principal):
        current = require()
        session = current.get(actor, session_id)
        history = current.history(actor, session_id)

        transcript = []
        for event in history['events']:
            payload = event['payload']
            public = ({'text': payload.get('text', '')} if event['kind'] == 'user' else
                      {key: payload[key] for key in ('status', 'answer', 'completion', 'unresolved',
                         'claims', 'evidence', 'user_statements', 'reason', 'stop_reason') if key in payload})
            transcript.append({key: event[key] for key in ('seq', 'kind', 'created') if key in event} |
                              {'payload': public})
        value = {'schema': 'authorized-session-export/1', 'exported_at': datetime.now(timezone.utc).isoformat(),
                 'session': {key: session[key] for key in
                             ('session_id', 'scope_id', 'revision', 'created_at', 'updated_at')},
                 'config_name': session['state']['config_name'], 'events': transcript,
                 'authorization': 'owner and current scope/source policy rechecked; unavailable responses redacted',
                 'contains_private_provider_credentials': False}
        return JSONResponse(value, headers={'Content-Disposition':
            f'attachment; filename="session-{session_id}.json"'})

    @app.get('/scopes/{scope_id}/agent-sources')
    def sources(scope_id: str, actor: principal):
        return require().sources(actor, scope_id)

    @app.get('/scopes/{scope_id}/agent-authority')
    def authority(scope_id: str, actor: principal):
        return require().authority(actor, scope_id)

    @app.post('/scopes/{scope_id}/sessions', status_code=201)
    def create(scope_id: str, body: SessionCreate, actor: principal):
        return require().create(actor, scope_id, body.source_policy, body.config_name)

    @app.get('/agent/sessions/{session_id}')
    def get(session_id: str, actor: principal):
        return require().get(actor, session_id)

    @app.get('/agent/sessions/{session_id}/history')
    def history(session_id: str, actor: principal):
        return require().history(actor, session_id)

    @app.post('/agent/sessions/{session_id}/turns')
    def turn(session_id: str, body: TurnRequest, actor: principal):
        return require().turn(actor, session_id, message=body.message, request_id=body.request_id,
                              expected_revision=body.expected_revision, policy=body.source_policy,
                              constraints=body.constraints, workflow_state=body.workflow_state)

    @app.get('/agent/sessions/{session_id}/artifacts/{artifact_id}')
    def artifact(session_id: str, artifact_id: str, actor: principal):
        return require().artifact(actor, session_id, artifact_id)
