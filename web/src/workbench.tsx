import { useEffect, useRef, useState, type FormEvent } from 'react'
import { OperationOwner, type OwnedOperation } from '@/operation-owner'
import { z } from 'zod'
import { ArrowRight, BookOpen, Check, FilePlus2, FileSearch, FolderPlus, LoaderCircle, Search, Upload } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'

const Scopes = z.array(z.object({ scope_id: z.string(), name: z.string() }))
const Created = z.object({ scope_id: z.string() })
const UploadResult = z.object({ document: z.object({ status: z.string(), elements: z.array(z.unknown()) }) })
const Build = z.object({ build_id: z.string() })
const Manifest = z.object({ manifest_id: z.string() })
const SearchResult = z.object({ hits: z.array(z.object({ citation_id: z.string(), text: z.string() })) })
const Citation = z.object({ filename: z.string(), source_version_id: z.string(), text: z.string(), anchors: z.unknown() })
type Scope = z.infer<typeof Scopes>[number]
type Citation = z.infer<typeof Citation>

async function request(operation: OwnedOperation, token: string, path: string, body?: unknown): Promise<Response> {
 if (!operation.isCurrent()) throw new DOMException('Operation superseded', 'AbortError')
 const isForm = body instanceof FormData
 const response = await fetch(path, { signal: operation.signal, method: body === undefined ? 'GET' : 'POST', headers: { Authorization: `Bearer ${token}`, ...(body !== undefined && !isForm ? { 'Content-Type': 'application/json' } : {}) }, body: body === undefined ? undefined : isForm ? body : JSON.stringify(body) })
 if (!response.ok) { const data: unknown = await response.json().catch(() => null); const error = z.object({ detail: z.string().optional(), error: z.string().optional() }).safeParse(data); throw new Error(error.success ? error.data.detail || error.data.error || `请求失败 (${response.status})` : `请求失败 (${response.status})`) }
 return response
}

export function Workbench({ token, scopes, scope, busy, notice, isCurrent, onConnect, onChat, onScopes }: { token: string; scopes: Scope[]; scope: string; busy: boolean; notice: string; isCurrent: () => boolean; onConnect: () => void; onChat: () => void; onScopes: (scopes: Scope[], id: string, notice?: string) => void }) {
 const [working, setWorking] = useState(false)
 const [name, setName] = useState('')
 const [file, setFile] = useState<File | null>(null)
 const [search, setSearch] = useState('')
 const [channel, setChannel] = useState<'bm25' | 'structure'>('bm25')
 const [hits, setHits] = useState<z.infer<typeof SearchResult>['hits']>([])
 const [status, setStatus] = useState(notice)
 const [error, setError] = useState('')
 const [citation, setCitation] = useState<Citation | null>(null)
 const [citationId, setCitationId] = useState('')
 const [preview, setPreview] = useState('')
 const owner = useRef(new OperationOwner())
 const running = useRef(false)
 const previewRef = useRef('')
 const downloads = useRef(new Set<string>())
 const prefix = `/knowledge-bases/${encodeURIComponent(scope)}`
 const locked = working || busy || !token
 function releasePreview() { if (previewRef.current) URL.revokeObjectURL(previewRef.current); previewRef.current = '' }
 useEffect(() => {
  const operations = owner.current
  const urls = downloads.current
  return () => { operations.invalidate(); releasePreview(); for (const url of urls) URL.revokeObjectURL(url); urls.clear() }
 }, [])
 async function run(action: (operation: OwnedOperation) => Promise<void>) {
  if (locked || running.current || !isCurrent()) return
  const operation = owner.current.begin(isCurrent)
  running.current = true
  setWorking(true); setError(''); setStatus('')
  try { await action(operation) }
  catch (e) { operation.commit(() => setError(e instanceof Error ? e.message : '请求失败')) }
  finally { running.current = false; operation.commit(() => setWorking(false)) }
 }
 function closeCitation() { releasePreview(); setPreview(''); setCitation(null); setCitationId('') }
 function choose(id: string) { if (locked || !isCurrent()) return; owner.current.invalidate(); closeCitation(); setHits([]); setFile(null); setStatus(''); setError(''); onScopes(scopes, id) }
 async function create(e: FormEvent) {
  e.preventDefault(); if (!name.trim()) return
  await run(async operation => {
   const created = Created.parse(await (await request(operation, token, '/scopes', { name: name.trim() })).json())
   const all = Scopes.parse(await (await request(operation, token, '/scopes')).json())
   operation.commit(() => { setName(''); setHits([]); setStatus('知识库已创建。添加文件后构建索引。'); setWorking(false); closeCitation(); onScopes(all, created.scope_id, '知识库已创建。添加文件后构建索引。') })
  })
 }
 async function upload() {
  if (!file) return
  await run(async operation => {
   const form = new FormData(); form.append('file', file)
   const result = UploadResult.parse(await (await request(operation, token, `${prefix}/sources`, form)).json())
   operation.commit(() => { setStatus(`${file.name} · ${result.document.status} · ${result.document.elements.length} 个结构元素。请构建并发布索引。`); setFile(null) })
  })
 }
 async function publish() {
  await run(async operation => {
   const build = Build.parse(await (await request(operation, token, `${prefix}/builds`, { channels: ['bm25'] })).json())
   const result = Manifest.parse(await (await request(operation, token, `${prefix}/builds/${encodeURIComponent(build.build_id)}/publish`, {})).json())
   operation.commit(() => setStatus(`索引已发布。版本 ${result.manifest_id}`))
  })
 }
 async function find(e: FormEvent) {
  e.preventDefault(); if (!search.trim()) return
  await run(async operation => {
   closeCitation()
   const result = SearchResult.parse(await (await request(operation, token, `${prefix}/search`, { query: search.trim(), channel })).json())
   operation.commit(() => { setHits(result.hits); setStatus(`找到 ${result.hits.length} 条证据。`) })
  })
 }
 async function show(id: string) {
  await run(async operation => {
   const path = `${prefix}/citations/${encodeURIComponent(id)}`
   const item = Citation.parse(await (await request(operation, token, path)).json())
   const blob = await (await request(operation, token, `${path}/preview`)).blob()
   operation.commit(() => { closeCitation(); const url = URL.createObjectURL(blob); previewRef.current = url; setCitationId(id); setPreview(url); setCitation(item) })
  })
 }
 async function download() {
  if (!citation) return
  await run(async operation => {
   const blob = await (await request(operation, token, `${prefix}/citations/${encodeURIComponent(citationId)}/original`)).blob()
   operation.commit(() => {
    const url = URL.createObjectURL(blob); downloads.current.add(url)
    const link = document.createElement('a'); link.href = url; link.download = citation.filename
    try { link.click() } finally { setTimeout(() => { URL.revokeObjectURL(url); downloads.current.delete(url) }, 1000) }
   })
  })
 }

 return <div className="workbench"><div className="workbench-content"><div className="workbench-heading"><div><p className="eyebrow">KNOWLEDGE WORKSPACE / 02</p><h1>知识库管理<span className="heading-dot">.</span></h1><p className="subtitle">从原始文档到可追溯的答案，一步一步建立你的知识库。</p></div><Button className="workbench-chat" onClick={onChat}>进入对话 <ArrowRight size={15}/></Button></div>
 <div className="flow-steps"><div><span>01</span><strong>创建知识库</strong><small>确定文档的组织范围</small></div><div><span>02</span><strong>添加文档</strong><small>上传并解析原始文件</small></div><div><span>03</span><strong>发布索引</strong><small>让文档可以被检索</small></div><div><span>04</span><strong>检索或对话</strong><small>查看答案与原文来源</small></div></div>
 {!token && <div className="connect-banner"><BookOpen size={18}/><span>连接工作空间后，即可使用下方的建库、文档和检索功能。</span><Button className="primary-action" onClick={onConnect}>连接知识库 <ArrowRight size={15}/></Button></div>}
 <div className="workbench-grid"><section className="work-card"><div className="card-title"><span className="card-icon"><FolderPlus size={18}/></span><div><h2>知识库</h2><p>创建或选择文档所属的知识库。</p></div></div><label htmlFor="work-scope">当前知识库</label><select id="work-scope" value={scope} disabled={locked || !scopes.length} onChange={e => choose(e.target.value)}>{scopes.length ? scopes.map(x => <option key={x.scope_id} value={x.scope_id}>{x.name}</option>) : <option value="">暂无知识库，请先创建</option>}</select><form onSubmit={e => void create(e)}><label htmlFor="new-scope">新知识库名称</label><div className="inline-form"><input id="new-scope" disabled={locked} placeholder="例如：团队知识库" value={name} onChange={e => setName(e.target.value)} maxLength={120}/><Button className="primary-action" type="submit" disabled={locked || !name.trim()}>创建</Button></div></form></section>
 <section className="work-card"><div className="card-title"><span className="card-icon"><FilePlus2 size={18}/></span><div><h2>文档与索引</h2><p>添加文件，解析完成后发布索引。</p></div></div><label htmlFor="work-file">添加文档</label><input id="work-file" type="file" accept=".pdf,.docx,.pptx,.xlsx,.csv,.md,.txt,.html,.png,.jpg,.jpeg" disabled={locked || !scope} onChange={e => setFile(e.target.files?.[0] || null)}/><div className="card-actions"><Button className="secondary-action" disabled={locked || !scope || !file} onClick={() => void upload()}><Upload size={15}/> 上传并解析</Button><Button className="primary-action" disabled={locked || !scope} onClick={() => void publish()}><Check size={15}/> 构建并发布索引</Button></div><p className="card-note">当前构建使用 BM25 关键词索引；文档解析方式由服务配置决定。</p></section></div>
 <section className="work-card search-card"><div className="card-title"><span className="card-icon"><FileSearch size={18}/></span><div><h2>检索与来源</h2><p>在已发布的知识库中查找证据，查看原文定位。</p></div></div><form className="search-form" onSubmit={e => void find(e)}><div><label htmlFor="work-query">检索问题或关键词</label><input id="work-query" disabled={locked} placeholder="输入关键词或问题" value={search} onChange={e => setSearch(e.target.value)}/></div><div><label htmlFor="work-channel">检索方式</label><select id="work-channel" disabled={locked} value={channel} onChange={e => setChannel(e.target.value === 'structure' ? 'structure' : 'bm25')}><option value="bm25">关键词 BM25</option><option value="structure">章节与流程扩展</option></select></div><Button className="primary-action" type="submit" disabled={locked || !scope || !search.trim()}><Search size={15}/> 检索</Button></form><div className="search-results">{hits.map(hit => <article key={hit.citation_id}><p>{hit.text}</p><Button className="secondary-action" disabled={locked} onClick={() => void show(hit.citation_id)}>查看原文与定位 <ArrowRight size={14}/></Button></article>)}</div></section>
 {(status || error || working) && <div className={`work-status ${error ? 'is-error' : ''}`} role="status" aria-live="polite">{working ? <><LoaderCircle className="spin" size={16}/> 正在处理…</> : error || status}</div>}
 </div><Dialog open={!!citation} onOpenChange={open => { if (!open) closeCitation() }}><DialogContent className="preview-dialog" showCloseButton={false}><div className="preview-header"><DialogTitle>{citation?.filename || '原文定位'}</DialogTitle><Button className="secondary-action" onClick={closeCitation}>关闭</Button></div><DialogDescription>原件版本 {citation?.source_version_id}</DialogDescription><div className="citation-details"><strong>证据摘录</strong><p>{citation?.text}</p><strong>原文定位</strong><pre>{JSON.stringify(citation?.anchors, null, 2)}</pre></div>{preview && <iframe title="原文内容" sandbox="" src={preview}/>}<Button className="secondary-action" disabled={locked} onClick={() => void download()}>下载原件</Button></DialogContent></Dialog></div>
}
