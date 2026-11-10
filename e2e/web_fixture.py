import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse

from kb_platform.agent_api import install_agent_routes
from kb_platform.knowledge_application import KnowledgeApplication
from kb_platform.rag_testing.agent_graph_contracts import ScenarioModel, normal_script
from kb_platform.rag_testing.product_fixture import Authority, Gateway, knowledge_service

space = Path(os.environ['KB_WEB_FIXTURE_DIR'])
authority = Authority([('Original.md', 'Original rule: value 42.')])
gateway = Gateway(authority)
gateway.authorize = lambda actor, scope: {'scope_id': scope, 'manifest_id': 'build', 'authority_revision': authority.revision}
gateway.sources = lambda actor, scope: [{'source_id': row['source_id'], 'doc_id': row['source_id'], 'source_version_id': row['source_version_id'], 'title': row['filename']} for row in authority.rows]
model = ScenarioModel(normal_script() * 3)
service = knowledge_service(None, model, root=space, gateway=gateway, retrieval_config={'channels': ['bm25'], 'rerank': False})

class BrowserApplication(KnowledgeApplication):
    def turn(self, actor, session_id, **kwargs):
        return self.knowledge.turn(actor, session_id, **kwargs)

application = BrowserApplication(service, None, None)
app = FastAPI()

def identity(authorization: str = Header(default='')):
    if authorization != 'Bearer web-e2e-token':
        raise HTTPException(401, '身份验证失败')
    return authority.actor

@app.get('/capabilities')
def capabilities(actor=Depends(identity)):
    return {'agent_conversations': {'ui': '/agent'}}


@app.get('/scopes')
def scopes(actor=Depends(identity)):
    return [{'scope_id': authority.scope, 'name': '测试知识库'}]

@app.get('/knowledge-bases/{scope_id}/citations/{citation_id}/preview')
def citation(scope_id: str, citation_id: str, actor=Depends(identity)):
    return PlainTextResponse(authority.resolve_citation(actor, scope_id, citation_id, 'llm_context')['text'])

install_agent_routes(app, None, None, identity, application=application)
