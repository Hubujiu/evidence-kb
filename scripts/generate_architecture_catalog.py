from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / 'docs/functional-architecture.html'
START = '<template id="function-catalog-start"></template>'
END = '<template id="function-catalog-end"></template>'
GROUPS = {
    '接入与配置': {'api', 'agent_api', 'delivery', 'settings'},
    '通用 Agent 与插件宿主': {'agent_decisions', 'agent_engine', 'agent_native', 'agent_plugins',
                          'agent_tool_loop', 'harness_loop', 'harness_services', 'harness_sessions',
                          'plugin_host', 'plugin_native'},
    '知识库业务与会话': {'knowledge_application', 'knowledge_profile', 'knowledge_service',
                      'knowledge_tool', 'knowledge_turn', 'knowledge_workflow', 'knowledge_model',
                      'knowledge_planning', 'knowledge_answer', 'knowledge_policy',
                      'knowledge_plugin_tools', 'session_store', 'knowledge_sessions', 'knowledge_state', 'agent_work_state',
                      'agent_context', 'agent_history', 'agent_protocol', 'agent_schema', 'agent_model'},
    '工具与检索': {'agent_tools', 'agent_tool_registry', 'agent_builtin_tools', 'agent_tool_view',
                 'retrieval_pipeline', 'retrieval_expansion',
                 'search', 'search_tokens', 'postgres_search', 'embedded_search',
                 'reranking', 'cross_document', 'table_query', 'workflow_query'},
    '证据与内容处理': {'evidence_selection', 'evidence_packing', 'delivery_units', 'documents',
                    'markdown_source', 'page_parsing', 'parsing_backends', 'pdf_layout', 'ocr',
                    'semantic_units', 'semantic_pages_v2', 'semantic_page_ingestion',
                    'semantic_page_runtime', 'prose_windows', 'title_packets'},
    '平台、存储与提供者': {'core', 'storage', 'research', 'remote', 'luna_sdk', 'agent_luna',
                    'agent_provider', 'agent_runtime', 'agent_call_budget', 'model_errors', 'provider_admission',
                    'provider_runtime', 'embeddings', 'embedding_jobs', 'google_semantic_provider'},
}


def category(path: Path) -> str:
    parts = path.relative_to(ROOT).parts
    if parts[0] == 'scripts':
        return '命令行与运维脚本'
    if len(parts) > 1 and parts[1] == 'rag_testing':
        return '统一评测与合同'
    if len(parts) > 1 and parts[1] == 'vendor':
        return '第三方代码'
    return next((name for name, modules in GROUPS.items() if path.stem in modules), '其他产品模块')


class FunctionFacts(ast.NodeVisitor):


    def __init__(self):
        self.calls = []
        self.writes = []
        self.returns = []
        self.raises = []

    def visit_FunctionDef(self, node):
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_Call(self, node):
        name = ast.unparse(node.func)
        if name not in self.calls:
            self.calls.append(name)
        self.generic_visit(node)

    def visit_Return(self, node):
        value = ast.unparse(node.value) if node.value else 'None'
        if value not in self.returns:
            self.returns.append(value)
        self.generic_visit(node)

    def visit_Raise(self, node):
        value = ast.unparse(node.exc) if node.exc else '重新抛出异常'
        if value not in self.raises:
            self.raises.append(value)
        self.generic_visit(node)

    def visit_Assign(self, node):
        for target in node.targets:
            self.record_write(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self.record_write(node.target)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self.record_write(node.target)
        self.generic_visit(node)

    def record_write(self, target):
        if isinstance(target, (ast.Attribute, ast.Subscript)):
            value = ast.unparse(target)
            if value not in self.writes:
                self.writes.append(value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.record_write(item)


def function_facts(node):
    facts = FunctionFacts()
    for statement in node.body:
        facts.visit(statement)
    args = [arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
    if node.args.vararg:
        args.append('*' + node.args.vararg.arg)
    if node.args.kwarg:
        args.append('**' + node.args.kwarg.arg)
    return {'inputs': args, 'calls': facts.calls[:14], 'writes': facts.writes[:12],
            'returns': facts.returns[:12], 'raises': facts.raises[:5],
            'call_count': len(facts.calls), 'write_count': len(facts.writes),
            'return_count': len(facts.returns)}

def entries(path: Path) -> list[dict[str, object]]:
    tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    rows: list[dict[str, object]] = []

    def visit(node: ast.AST, parents: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = '.'.join((*parents, child.name))
                signature = ('async ' if isinstance(child, ast.AsyncFunctionDef) else '') + child.name
                signature += '(' + ast.unparse(child.args) + ')'
                if child.returns:
                    signature += ' → ' + ast.unparse(child.returns)
                rows.append({'name': qualified, 'signature': signature, 'line': child.lineno,
                             **function_facts(child), 'kind': '嵌套函数' if any(isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                                                    for x in ancestors) else ('方法' if parents else '函数')})
                ancestors.append(child)
                visit(child, (*parents, child.name))
                ancestors.pop()
            elif isinstance(child, ast.ClassDef):
                ancestors.append(child)
                visit(child, (*parents, child.name))
                ancestors.pop()
            else:
                visit(child, parents)

    ancestors: list[ast.AST] = []
    visit(tree, ())
    return rows


def generate() -> str:
    paths = sorted((ROOT / 'kb_platform').rglob('*.py')) + sorted((ROOT / 'scripts').rglob('*.py'))
    modules = []
    explanation_path = ROOT / 'docs/function-explanations.json'
    explanations = json.loads(explanation_path.read_text(encoding='utf-8')) if explanation_path.exists() else {}
    for path in paths:
        rows = entries(path)
        if not rows:
            continue
        relative = path.relative_to(ROOT).as_posix()
        for row in rows:
            row['explanation'] = explanations.get(f"{relative}:{row['name']}")
        modules.append({'path': relative, 'group': category(path),
                        'functions': rows})
    payload = json.dumps(modules, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    return f'''{START}
<section class="panel" id="catalog"><div class="panel-head"><h2>06 / 源码函数归属目录</h2><span>从 Python 源码静态提取</span></div>
<p class="catalog-note">按功能分组、文件和类查看定义。包含类方法、私有函数和嵌套函数；名称反映词法归属，不代表调用关系。输入、返回表达式、调用与写入位置由静态语法提取；这些是代码线索，并非完整的业务语义或运行时行为。覆盖 kb_platform 与 scripts 下的 Python 文件，包括评测合同和第三方代码。</p>
<div class="catalog-controls"><label for="catalog-search">搜索函数、文件或说明</label><input id="catalog-search" type="search" placeholder="例如 SessionStore、reserve、retrieval"><label for="catalog-group">功能分组</label><select id="catalog-group"><option value="">全部分组</option></select></div>
<p id="catalog-count" class="catalog-note" aria-live="polite"></p><div id="catalog-list"></div><p class="catalog-note">更新目录：在项目根目录运行 <code>python scripts/generate_architecture_catalog.py</code>。</p></section>
<style>.catalog-note{{font-size:13px;color:#617782}}.catalog-controls{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}.catalog-controls label{{font-size:12px;font-weight:bold}}.catalog-controls input,.catalog-controls select{{border:1px solid #cbdcda;border-radius:8px;padding:9px;background:#fff;font:inherit}}.catalog-controls input{{min-width:280px;flex:1}}.catalog-module{{border-top:1px solid #e2ecea;padding:10px 0}}.catalog-module summary{{cursor:pointer;font-weight:700}}.catalog-module summary span{{font-weight:400;color:#71828a;font-size:12px;margin-left:8px}}.catalog-row{{border-top:1px solid #eef2f2;padding:8px 8px 8px 20px;display:grid;grid-template-columns:minmax(150px,1fr) minmax(200px,2fr);gap:8px}}.reviewed{{background:#edf8f4;border-left:3px solid #168c79;padding:8px;margin:8px 0}}.catalog-row code{{font-size:12px;overflow-wrap:anywhere}}.catalog-row small{{display:block;color:#70828b}}.catalog-row a{{color:#087d79;text-decoration:none}}@media(max-width:700px){{.catalog-row{{grid-template-columns:1fr}}}}</style>
<script type="application/json" id="catalog-data">{payload}</script>
<script>
(()=>{{const data=JSON.parse(document.getElementById('catalog-data').textContent), search=document.getElementById('catalog-search'), group=document.getElementById('catalog-group'), list=document.getElementById('catalog-list'), count=document.getElementById('catalog-count');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
[...new Set(data.map(m=>m.group))].forEach(g=>{{const o=document.createElement('option');o.value=g;o.textContent=g;group.append(o)}});
function render(){{const q=search.value.trim().toLowerCase(), selected=group.value;let total=0,files=0;list.replaceChildren();for(const m of data){{if(selected&&m.group!==selected)continue;const moduleMatch=(m.path+' '+m.group).toLowerCase().includes(q);const rows=m.functions.filter(f=>moduleMatch||(f.name+' '+f.signature).toLowerCase().includes(q));if(!rows.length)continue;files++;total+=rows.length;const d=document.createElement('details');d.className='catalog-module';if(q)d.open=true;d.innerHTML='<summary>'+esc(m.group)+' · '+esc(m.path)+' <span>'+rows.length+' 项'+'</span></summary>'+rows.map(f=>'<div class="catalog-row"><div><a href="../'+esc(m.path)+'">'+esc(f.name)+'</a><small>'+esc(f.kind)+' · 第 '+f.line+' 行</small></div><div><code>'+esc(f.signature)+'</code>'+(f.explanation?'<div class="reviewed"><b>已核对的功能说明</b><small><b>用途：</b>'+esc(f.explanation.purpose)+'</small><small><b>输入：</b>'+esc(f.explanation.inputs)+'</small><small><b>操作对象：</b>'+esc(f.explanation.objects)+'</small><small><b>输出：</b>'+esc(f.explanation.output)+'</small></div>':'<small>尚未完成人工功能说明；以下是静态代码线索。</small>')+'<small><b>输入：</b>'+esc(f.inputs.length?f.inputs.join('、'):'无显式参数')+'</small><small><b>操作对象：</b>'+esc(f.writes.length?f.writes.join('；')+(f.write_count>f.writes.length?' 等':''):'未发现属性或下标直接赋值；可能通过调用产生作用')+'</small><small><b>处理动作：</b>'+esc(f.calls.length?'调用 '+f.calls.join('、')+(f.call_count>f.calls.length?' 等':''):'未发现函数调用')+'</small><small><b>输出：</b>'+esc(f.returns.length?f.returns.join('；')+(f.return_count>f.returns.length?' 等':''):'没有显式 return，通常返回 None；生成器及异常路径需查看源码')+'</small>'+(f.raises.length?'<small><b>显式异常：</b>'+esc(f.raises.join('；'))+'</small>':'')+'</div></div>').join('');list.append(d)}}count.textContent='显示 '+files+' 个文件，'+total+' 个函数定义。点击文件展开。'}}search.addEventListener('input',render);group.addEventListener('change',render);render()}})();
</script>
{END}'''


def main() -> None:
    page = PAGE.read_text(encoding='utf-8-sig')
    block = generate()
    before, rest = page.split(START, 1)
    _, after = rest.split(END, 1)
    page = before + block + after
    PAGE.write_text(page, encoding='utf-8')
    print(f'Updated {PAGE.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
