import { useState, useEffect, useRef, type FormEvent } from 'react'
import { createRoot } from 'react-dom/client'
import { create } from 'zustand'
import { motion, MotionConfig } from 'motion/react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog'
import PromptBar from '@/components/primitives/PromptBar'
import { Shimmer } from '@/components/atoms/Shimmer'
import { Workbench } from '@/workbench'
import { ChatOptionsPanel, SessionState, freshOptions, workflow } from '@/chat-options'
import { OperationOwner, type OwnedOperation } from '@/operation-owner'
import { blocksRetainedSession, canParkTurn, canSendTurn, isPreHandlerRejection, requestTransition, type FailureFacts, type SubmittedTurn, type RequestEvent, type TurnOwner, type TurnRequests } from '@/turn-request'
import '@/app/beautifui/foundation.css'
import { Group, Panel, Separator } from 'react-resizable-panels'
import { z } from 'zod'
import { BookOpen, PanelLeftClose, PanelLeftOpen, Plus, Search, MessageSquare, Library, ArrowUpRight, Sparkles, FileText, ChevronDown, KeyRound, CircleHelp, X, LoaderCircle, Menu } from 'lucide-react'
import './style.css'

const Scope = z.object({ scope_id: z.string(), name: z.string() })
const Source = z.object({ source_id: z.string(), title: z.string().optional() })
type Source = z.infer<typeof Source>
const Session = z.object({ session_id: z.string(), scope_id: z.string(), revision: z.number(), updated_at: z.string().optional(), state: SessionState.optional() })
const Evidence = z.object({ evidence_id: z.string(), citation_id: z.string().optional(), title: z.string().optional(), filename: z.string().optional(), doc_id: z.string().optional(), source_id: z.string().optional() })
const Response = z.object({ answer: z.string().nullable().optional(), status: z.string().optional(), stop_reason: z.string().nullable().optional(), evidence: z.array(Evidence).optional(), claims: z.array(z.object({ evidence_ids: z.array(z.string()).optional() })).optional(), unresolved: z.array(z.string()).optional(), user_statements: z.array(z.object({event_seq: z.number(), quote: z.string()})).optional(), trace: z.unknown().optional() })
const CompletedResponse = Response.extend({ session_id: z.string(), revision: z.number().int().nonnegative(), status: z.enum(['answered', 'partial', 'abstained', 'clarification', 'error']) })
const RecoveryIdentity = z.object({ session_id: z.string(), request_id: z.string(), revision: z.number().int().nonnegative(), send_status: z.literal('unknown') })
const Recovery = z.discriminatedUnion('status', [
  RecoveryIdentity.extend({ status: z.literal('running'), recovery_action: z.literal('wait') }),
  RecoveryIdentity.extend({ status: z.literal('interrupted'), recovery_action: z.literal('inspect_request') }),
  RecoveryIdentity.extend({ status: z.literal('not_found'), recovery_action: z.literal('verify_request_id') }),
  RecoveryIdentity.extend({ status: z.literal('reconciliation_required'), recovery_action: z.literal('inspect_operation') }),
  RecoveryIdentity.extend({ status: z.literal('completed'), recovery_action: z.enum(['retrieve_result', 'inspect_request']), response: z.union([CompletedResponse, Response.extend({ status: z.literal('redacted') })]) }),
])
const History = z.object({ events: z.array(z.object({ kind: z.string(), group_id: z.string().optional(), payload: z.object({ text: z.string().optional() }).and(Response) })) })
type Scope = z.infer<typeof Scope>
type Session = z.infer<typeof Session>
type Answer = z.infer<typeof Response>
type Message = ({ role: 'user'; text: string } | { role: 'assistant'; text: string; response: Answer }) & { requestId?: string }
const answerText = (r: Answer) => r.status === 'redacted' ? '此历史答案的来源已不可访问。' : r.answer || `本轮未完成${r.stop_reason ? `（${r.stop_reason}）` : ''}，请查看服务状态后再继续。`
const historyMessages = (history: z.infer<typeof History>): Message[] => history.events.map(e => e.kind === 'user' ? { role: 'user', text: e.payload.text || '', requestId: e.group_id } : { role: 'assistant', text: answerText(e.payload), response: e.payload, requestId: e.group_id })
const errorText = (error: unknown) => error instanceof Error ? error.message : '连接中断'
const recoveryText = { running: '原请求仍在执行，请稍后再次核对。', interrupted: '原请求已中断；请核查服务记录，当前不能确认其外部执行结果。', not_found: '尚未查到原请求记录；请核对请求身份，不能据此认定模型未执行。', reconciliation_required: '原请求的提交或结算需要维护者核对，请保留请求身份与服务记录。' }
const ErrorFacts = z.object({ code: z.string().optional(), mode: z.string().optional(), reason: z.string().optional(), model_sent: z.boolean().optional(), automatic_retry: z.boolean().optional(), recovery_action: z.string().optional(), request_id: z.string().optional(), physical_request_id: z.string().optional(), provider_request_id: z.string().optional(), operation_id: z.string().optional(), budget_operation_id: z.string().optional(), admission_receipt_id: z.string().optional(), outcome: z.string().optional(), capture_state: z.string().optional(), ledger_settlement: z.string().optional() })
class RequestFailure extends Error {
 constructor(message: string, readonly facts: FailureFacts) { super(message) }
}
function FailureDetails({ facts }: { facts?: FailureFacts }) { return facts && Object.keys(facts).length > 0 ? <details><summary>服务恢复信息</summary><pre>{JSON.stringify(facts, null, 2)}</pre></details> : null }
async function api(path: string, token: string, body?: unknown): Promise<unknown> {
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) }, body: body === undefined ? undefined : JSON.stringify(body) })
  const data: unknown = await response.json()
  if (!response.ok) {
   const parsed = ErrorFacts.extend({ detail: z.string().optional(), message: z.string().optional(), error: z.union([z.string(), ErrorFacts.extend({ message: z.string().optional() })]).optional() }).safeParse(data)
   const error = parsed.success ? parsed.data : {}
   const nested = typeof error.error === 'object' ? error.error : {}
   const facts = ErrorFacts.parse({ ...nested, ...error, code: typeof error.error === 'string' ? error.error : error.code || nested.code })
   throw new RequestFailure(error.detail || error.message || nested.message || facts.code || `请求失败 (${response.status})`, facts)
  }
  return data
}
type Store = { identityEpoch: number; scopeEpoch: number; workbenchNotice: string; token: string; scopes: Scope[]; scope: string; sources: Source[]; sessions: Session[]; session: Session | null; messages: Message[]; busy: boolean; error: string; set: (v: Partial<Store>) => void }
const useApp = create<Store>(set => ({ identityEpoch: 0, scopeEpoch: 0, workbenchNotice: '', token: '', scopes: [], scope: '', sources: [], sessions: [], session: null, messages: [], busy: false, error: '', set: patch => set(state => ({ ...patch, scopeEpoch: patch.scope !== undefined && patch.scope !== state.scope ? state.scopeEpoch + 1 : state.scopeEpoch, workbenchNotice: patch.workbenchNotice ?? (patch.scope !== undefined && patch.scope !== state.scope ? '' : state.workbenchNotice) })) }))

function App() {
 const { identityEpoch, scopeEpoch, workbenchNotice, token, scopes, scope, sources, sessions, session, messages, busy, error, set } = useApp()
 const [collapsed, setCollapsed] = useState(() => window.matchMedia('(max-width: 700px)').matches); const [dialog, setDialog] = useState(false); const [tokenInput, setTokenInput] = useState(''); const [draft, writeDraft] = useState(''); const [query, setQuery] = useState('')
 const draftOwner = useRef(0)
 function setDraft(value: string) { draftOwner.current++; writeDraft(value) }
 const [requests, setRequests] = useState<TurnRequests<Answer>>({ active: { phase: 'idle' }, retained: [] })
 const requestsRef = useRef(requests)
 const turn = requests.active
 const turnOperations = useRef(new OperationOwner())
 function transition(event: RequestEvent<Answer>, request?: TurnOwner) {
  if (request && useApp.getState().identityEpoch !== request.identityEpoch) return
  requestsRef.current = requestTransition(requestsRef.current, event)
  setRequests(requestsRef.current)
 }
 useEffect(() => { const owner = turnOperations.current; return () => owner.invalidate() }, [])
 const [previewUrl, setPreviewUrl] = useState('')
 const [runtime, setRuntime] = useState('')
 const [options, setOptions] = useState(freshOptions)
 const [view, setView] = useState<'chat' | 'documents'>('chat')
 const viewRef = useRef<'chat' | 'documents'>('chat')
 const viewEpochRef = useRef(0)
 const viewEpoch = viewEpochRef.current
 const [agentAvailable, setAgentAvailable] = useState(false)
 const [wide, setWide] = useState(() => window.matchMedia('(min-width: 1101px)').matches)
 useEffect(() => { const media = window.matchMedia('(min-width: 1101px)'); const change = () => setWide(media.matches); media.addEventListener('change', change); return () => media.removeEventListener('change', change) }, [])
 const selected = scopes.find(s => s.scope_id === scope)
 async function run(fn: () => Promise<void>) { if (useApp.getState().busy) return; set({ busy: true, error: '' }); try { await fn() } catch (e) { set({ error: e instanceof Error ? e.message : '请求失败，请检查连接。' }) } finally { set({ busy: false }) } }
 async function loadSources(t: string, id: string) { return id ? z.array(Source).parse(await api(`/scopes/${encodeURIComponent(id)}/agent-sources`, t)) : [] }
 async function refreshSessions(t: string) { return z.object({ sessions: z.array(Session) }).parse(await api('/agent/sessions?limit=50', t)).sessions }
 async function connect(e: FormEvent) { e.preventDefault(); const t = tokenInput.trim(); if (!t) return; await run(async () => { turnOperations.current.invalidate(); transition({type: 'reset'}); if (token && token !== t) setDraft(''); set({ identityEpoch: useApp.getState().identityEpoch + 1, workbenchNotice: '', token: '', scopes: [], scope: '', sources: [], sessions: [], session: null, messages: [] }); setAgentAvailable(false); setPreviewUrl(''); setRuntime(''); setOptions(freshOptions()); const list = z.array(Scope).parse(await api('/scopes', t)); const capability = z.object({ agent_conversations: z.unknown().nullable() }).parse(await api('/capabilities', t)); const available = capability.agent_conversations !== null; const saved = available ? await refreshSessions(t) : []; const first = list[0]?.scope_id || ''; const availableSources = available ? await loadSources(t, first) : []; setAgentAvailable(available); setOptions(freshOptions()); set({ token: t, scopes: list, scope: first, sources: availableSources, sessions: saved, session: null, messages: [] }); setDialog(false); setTokenInput('') }) }
 function restoreOptions(current: Session) { const st = current.state; setOptions(st ? { policy: st.source_policy, constraints: (st.constraints || []).map(c => c.text).join('\n'), step: st.workflow_state?.current_step || '', facts: Object.entries(st.workflow_state?.facts || {}).map(([key, value]) => `${key}=${String(value)}`).join('\n') } : freshOptions()) }
 async function restore(item: Session) { await run(async () => { turnOperations.current.invalidate(); const [s, h] = await Promise.all([api(`/agent/sessions/${encodeURIComponent(item.session_id)}`, token), api(`/agent/sessions/${encodeURIComponent(item.session_id)}/history`, token)]); const current = Session.parse(s); const history = History.parse(h); const availableSources = await loadSources(token, current.scope_id); restoreOptions(current); set({ session: current, scope: current.scope_id, sources: availableSources, messages: historyMessages(history) }) }) }
 function confirm(request: SubmittedTurn, result: Answer, operation: OwnedOperation) {
  transition({ type: 'received', requestId: request.requestId, response: result }, request)
  operation.commit(() => {
   const current = useApp.getState()
   if (current.scope !== request.scopeId || current.session?.session_id !== request.sessionId) return
   const answer: Message = { role: 'assistant', text: answerText(result), response: result, requestId: request.requestId }
   const existing = current.messages.some(message => message.role === 'assistant' && message.requestId === request.requestId)
   set({ messages: existing ? current.messages.map(message => message.role === 'assistant' && message.requestId === request.requestId ? answer : message) : [...current.messages, answer] })
   if (draftOwner.current === request.draftOwner) setDraft('')
  })
 }
 async function refreshConfirmed(request: SubmittedTurn, operation: OwnedOperation, restoreHistory: boolean) {
  transition({ type: 'refresh', requestId: request.requestId }, request)
  try {
   const current = Session.parse(await api(`/agent/sessions/${encodeURIComponent(request.sessionId)}`, token))
   if (current.session_id !== request.sessionId || current.scope_id !== request.scopeId) throw Error('会话身份与原请求不一致')
   const history = restoreHistory ? History.parse(await api(`/agent/sessions/${encodeURIComponent(request.sessionId)}/history`, token)) : null
   const availableSources = restoreHistory ? await loadSources(token, request.scopeId) : null
   let saved: Session[] | undefined
   let warning = ''
   try { saved = await refreshSessions(token) } catch (e) { warning = `本轮结果已确认；最近对话列表暂未刷新：${errorText(e)}。` }
   const committed = operation.commit(() => {
    if (restoreHistory) {
     if (viewRef.current !== 'chat') viewEpochRef.current++
     viewRef.current = 'chat'; setView('chat')
     restoreOptions(current)
    }
    set({ session: current, ...(saved ? { sessions: saved } : {}), ...(history ? { scope: request.scopeId, messages: historyMessages(history), sources: availableSources || [] } : {}), error: warning })
   })
   transition(committed ? { type: 'synced', requestId: request.requestId, detail: warning } : { type: 'refresh_failed', requestId: request.requestId, detail: '界面范围已切换，原请求结果已确认。请刷新原会话后继续。' }, request)
  } catch (e) {
   transition({ type: 'refresh_failed', requestId: request.requestId, detail: `本轮结果已确认；会话状态暂未刷新：${errorText(e)}。刷新后可继续提问。` }, request)
  }
 }
 async function send(text: string) {
  const question = text.trim()
  if (!question || useApp.getState().busy) return
  if (!canSendTurn(requestsRef.current, useApp.getState().session?.session_id || null)) { set({ error: '请先核对原请求或刷新原会话，当前不会重复发送。' }); return }
  if (!token) { setDialog(true); return }
  if (!scope) { set({ error: '请先创建或选择知识库。' }); return }
  if (!agentAvailable) { set({ error: '对话服务尚未配置，请先配置模型并重启服务。文档管理和检索仍可使用。' }); return }
  let workflowState
  try { workflowState = workflow(options); if (options.policy.mode === 'LOCKED' && !options.policy.source_ids.length) throw Error('请先选择要锁定的来源。') }
  catch (e) { set({ error: errorText(e) }); return }
  const captured = useApp.getState()
  let expectedSession = captured.session?.session_id || null
  const request: TurnOwner = { identityEpoch: captured.identityEpoch, scopeId: captured.scope, sessionId: expectedSession, requestId: crypto.randomUUID(), question, draftOwner: draftOwner.current }
  const operation = turnOperations.current.begin(() => { const current = useApp.getState(); return current.identityEpoch === captured.identityEpoch && current.scopeEpoch === captured.scopeEpoch && current.scope === captured.scope && (current.session?.session_id || null) === expectedSession })
  transition({ type: 'start', request })
  await run(async () => {
   let active = captured.session
   if (!active) {
    try {
     active = Session.parse(await api(`/scopes/${encodeURIComponent(request.scopeId)}/sessions`, token, { config_name: 'recommended', source_policy: options.policy }))
     if (!operation.isCurrent()) { transition({ type: 'creation_failed', requestId: request.requestId }, request); return }
     if (active.scope_id !== request.scopeId) throw Error('新会话的知识库身份不一致')
     if (blocksRetainedSession(requestsRef.current, active.session_id)) throw Error('新会话与待核对记录的原会话相同')
     expectedSession = active.session_id
     set({ session: active })
     transition({ type: 'created', requestId: request.requestId, sessionId: active.session_id }, request)
    } catch (e) {
     transition({ type: 'creation_failed', requestId: request.requestId }, request)
     operation.commit(() => set({ error: `会话未建立，问题已保留：${errorText(e)}。` }))
     return
    }
   }
   const submitted: SubmittedTurn = { ...request, sessionId: active.session_id }
   if (!operation.commit(() => set({ messages: [...useApp.getState().messages, { role: 'user', text: question, requestId: request.requestId }] }))) {
    transition({ type: 'not_submitted', requestId: request.requestId }, request)
    return
   }
   try {
    const result = CompletedResponse.parse(await api(`/agent/sessions/${encodeURIComponent(active.session_id)}/turns`, token, { message: question, request_id: request.requestId, expected_revision: active.revision, source_policy: { ...options.policy, versions: active.state?.source_policy.version_policy === 'pinned' && options.policy.version_policy === 'pinned' && JSON.stringify(active.state.source_policy.source_ids) === JSON.stringify(options.policy.source_ids) ? active.state.source_policy.versions : options.policy.versions }, constraints: options.constraints.split('\n').map(x => x.trim()).filter(Boolean), workflow_state: workflowState }))
    if (result.session_id !== submitted.sessionId) throw Error('响应会话身份与原请求不一致')
    confirm(submitted, result, operation)
    operation.commit(() => { if (options.policy.lifetime === 'turn') setOptions({ ...options, policy: freshOptions().policy }) })
   } catch (e) {
    if (e instanceof RequestFailure && isPreHandlerRejection(e.facts)) {
     const detail = `服务在接收本轮前已拒绝提交，问题已保留：${errorText(e)}。请先处理服务提示。`
     transition({ type: 'rejected', requestId: request.requestId, detail }, request)
     operation.commit(() => set({ error: detail, messages: useApp.getState().messages.filter(message => message.requestId !== request.requestId) }))
    } else transition({ type: 'unconfirmed', requestId: request.requestId, detail: `本轮请求结果待核对：${errorText(e)}。请核对原请求，当前不会重复发送。`, facts: e instanceof RequestFailure ? e.facts : undefined }, request)
    return
   }
   await refreshConfirmed(submitted, operation, false)
  })
 }
 async function recover(requestId: string) {
  const retained = requestsRef.current.retained.find(attempt => attempt.request.requestId === requestId)
  const attempt = retained || requestsRef.current.active
  if (useApp.getState().busy || (attempt.phase !== 'unconfirmed' && attempt.phase !== 'refresh_required' && !(retained && attempt.phase === 'confirmed')) || attempt.request.requestId !== requestId) return
  const request = attempt.request
  const captured = useApp.getState()
  if (captured.identityEpoch !== request.identityEpoch) return
  const operation = turnOperations.current.begin(() => { const current = useApp.getState(); return current.identityEpoch === captured.identityEpoch && current.scopeEpoch === captured.scopeEpoch && (current.session?.session_id || null) === (captured.session?.session_id || null) })
  await run(async () => {
   if (attempt.phase === 'unconfirmed') {
    transition({ type: 'check', requestId: request.requestId }, request)
    try {
     const result = Recovery.parse(await api(`/agent/sessions/${encodeURIComponent(request.sessionId)}/requests/${encodeURIComponent(request.requestId)}`, token))
     if (result.session_id !== request.sessionId || result.request_id !== request.requestId) throw Error('查询结果与原请求身份不一致')
     if (result.status !== 'completed') { transition({ type: 'observed', requestId: request.requestId, status: result.status, detail: recoveryText[result.status] }, request); return }
     if ((result.response.status === 'redacted') !== (result.recovery_action === 'inspect_request')) throw Error('查询结果与恢复动作不一致')
     if (result.response.status !== 'redacted' && result.response.session_id !== request.sessionId) throw Error('已保存响应与原会话身份不一致')
     if (retained) { transition({ type: 'received', requestId: request.requestId, response: result.response }, request); return }
     confirm(request, result.response, operation)
    } catch (e) {
     transition({ type: 'check_failed', requestId: request.requestId, detail: `原请求仍待核对：${errorText(e)}。未发送新的模型请求。` }, request)
     return
    }
   }
   await refreshConfirmed(request, operation, true)
  })
 }
 function park() {
  const attempt = requestsRef.current.active
  if (useApp.getState().busy || attempt.phase !== 'unconfirmed' || !canParkTurn(attempt)) return
  turnOperations.current.invalidate()
  transition({ type: 'park', requestId: attempt.request.requestId }, attempt.request)
  set({ session: null, messages: [], error: '' }); setDraft(''); setOptions(freshOptions()); switchView('chat')
 }
 function newChat() { if (!busy) { turnOperations.current.invalidate(); set({ session: null, messages: [], error: '' }); setDraft(''); setOptions(freshOptions()); switchView('chat') } }
 async function exportSession() { if (!session) return; await run(async () => { const result = await api(`/agent/sessions/${encodeURIComponent(session.session_id)}/export`, token); const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' })); const link = document.createElement('a'); link.href = url; link.download = `session-${session.session_id}.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }) }
 async function showRuntime() { await run(async () => { const result = z.object({ configured: z.boolean(), status: z.string().optional(), pending_or_unknown: z.unknown().optional(), remaining: z.record(z.string(), z.unknown()).optional() }).parse(await api('/agent/runtime-status', token)); setRuntime(result.configured ? `服务状态：${result.status || '未知'}。剩余预算：${JSON.stringify(result.remaining || {})}。${result.pending_or_unknown ? '存在未决或未知请求，请核对记录，勿重复发送。' : ''}` : '当前服务未配置交付预算状态。') }) }
 async function preview(id: string) { await run(async () => { const r = await fetch(`/knowledge-bases/${encodeURIComponent(scope)}/citations/${encodeURIComponent(id)}/preview`, { headers: { Authorization: `Bearer ${token}` } }); if (!r.ok) throw Error('原文当前不可访问'); const url = URL.createObjectURL(await r.blob()); setPreviewUrl(url) }) }
 useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl) }, [previewUrl])
 useEffect(() => { const onKey = (e: KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); document.getElementById('chat-search')?.focus() } }; window.addEventListener('keydown', onKey); return () => window.removeEventListener('keydown', onKey) }, [])
 function switchView(next: 'chat' | 'documents') { if (viewRef.current !== next) viewEpochRef.current += 1; viewRef.current = next; setView(next); if (window.matchMedia('(max-width: 700px)').matches) setCollapsed(true); if (next === 'chat' && token && scope && agentAvailable) void run(async () => { const available = await loadSources(token, scope); set({sources: available}) }) }
 const filtered = sessions.filter(s => (scopes.find(x => x.scope_id === s.scope_id)?.name || '').includes(query) || s.session_id.includes(query))
 const canSend = canSendTurn(requests, session?.session_id || null)
 return <MotionConfig reducedMotion="user"><div className={`shell ${collapsed ? 'is-collapsed' : ''}`}>
  <aside className="sidebar"><div className="brand"><div className="brand-mark"><BookOpen size={18}/></div>{!collapsed && <span>Evidence <b>KB</b></span>}<Button className="icon-button collapse" aria-label={collapsed ? '展开侧栏' : '收起侧栏'} onClick={() => setCollapsed(!collapsed)}>{collapsed ? <PanelLeftOpen/> : <PanelLeftClose/>}</Button></div>
   <Button className="new-chat" aria-label="新建对话" onClick={newChat} disabled={busy}><Plus size={17}/>{!collapsed && <><span>新建对话</span><span className="shortcut">＋</span></>}</Button>
   <Button className={`nav-item ${view === 'chat' ? 'active' : ''}`} onClick={() => switchView('chat')}><MessageSquare size={17}/>{!collapsed && <span>知识库对话</span>}</Button><Button className={`nav-item ${view === 'documents' ? 'active' : ''}`} onClick={() => switchView('documents')}><Library size={17}/>{!collapsed && <span>知识库管理</span>}</Button>
   {!collapsed && <><div className="section-label"><span>最近对话</span><span>{sessions.length || ''}</span></div><div className="search"><Search size={15}/><input id="chat-search" aria-label="搜索最近对话" placeholder="搜索对话" value={query} onChange={e => setQuery(e.target.value)}/><kbd>⌘ K</kbd></div><div className="history">{filtered.length ? filtered.map(s => <Button key={s.session_id} className={`history-item ${session?.session_id === s.session_id ? 'selected' : ''}`} disabled={busy} onClick={() => void restore(s)}><MessageSquare size={14}/><span>{scopes.find(x => x.scope_id === s.scope_id)?.name || '知识库'} · 第 {s.revision} 轮</span></Button>) : <p>{token ? '暂无匹配的对话' : '连接后查看最近对话'}</p>}</div></>}
   <div className="sidebar-bottom"><Button className="account" onClick={() => setDialog(true)} disabled={busy}><span className="avatar"><KeyRound size={15}/></span>{!collapsed && <span><strong>{token ? '连接已建立' : '连接知识库'}</strong><small>{token ? '管理访问令牌' : '使用本地访问令牌'}</small></span>}</Button></div>
  </aside>
  <main className="workspace"><header className="topbar"><div className="breadcrumb"><span>工作空间</span><span className="slash">/</span><strong>{view === 'chat' ? '知识库对话' : '知识库管理'}</strong></div><div className="top-actions">{agentAvailable && token && <Button className="top-connect" disabled={busy} onClick={() => void showRuntime()}>服务状态</Button>}{session && <Button className="top-connect" disabled={busy} onClick={() => void exportSession()}>导出会话</Button>}<span className="status-dot"/>{token ? '已连接' : '未连接'}<Button className="top-connect" onClick={() => setDialog(true)} disabled={busy}>{token ? '切换连接' : '连接知识库'} <ArrowUpRight size={14}/></Button><Button className="mobile-menu icon-button" onClick={() => setCollapsed(!collapsed)} aria-label="切换侧栏"><Menu/></Button></div></header>
  {(turn.phase === 'unconfirmed' || turn.phase === 'checking' || turn.phase === 'refresh_required') && <section className="request-recovery" role="status" aria-live="polite"><strong>{turn.phase === 'refresh_required' ? '结果已确认，等待刷新会话' : turn.phase === 'checking' ? '正在核对原请求…' : '原请求结果待核对'}</strong><p>{turn.detail}</p><p className="request-identity">会话 {turn.request.sessionId} · 请求 {turn.request.requestId}</p><FailureDetails facts={turn.facts}/>{turn.phase === 'refresh_required' && <details><summary>查看已确认结果</summary><p>{answerText(turn.response)}</p></details>}<div className="recovery-actions"><Button className="secondary-action" disabled={busy || turn.phase === 'checking'} onClick={() => void recover(turn.request.requestId)}>{turn.phase === 'refresh_required' ? '刷新原会话' : '核对原请求'}</Button>{canParkTurn(turn) && <Button className="secondary-action" disabled={busy} onClick={park}>保留记录并新建对话</Button>}</div>{canParkTurn(turn) && <small>保留记录后可另开空白对话；原问题不会复制或重发，原会话继续等待核对。</small>}<small>核对只读取原请求，不会重新发送。切换连接或刷新页面前请保留会话与请求 ID。</small></section>}
  {requests.retained.length > 0 && <details className="retained-requests" open><summary>保留的请求（{requests.retained.length}）</summary>{requests.retained.map(attempt => 'detail' in attempt && <section key={attempt.request.requestId} className="request-recovery retained-request" data-request-id={attempt.request.requestId} role="status"><strong>{'response' in attempt ? attempt.phase === 'complete' ? '原请求已恢复' : '已找回原请求结果' : attempt.phase === 'checking' ? '正在核对保留请求…' : '保留的原请求待核对'}</strong><p>{attempt.detail}</p><p className="request-identity">会话 {attempt.request.sessionId} · 请求 {attempt.request.requestId}</p><details><summary>原问题</summary><p>{attempt.request.question}</p></details><FailureDetails facts={'facts' in attempt ? attempt.facts : undefined}/>{'response' in attempt && <details><summary>查看已确认结果</summary><p>{answerText(attempt.response)}</p></details>}{attempt.phase !== 'complete' && <Button className="secondary-action" disabled={busy || attempt.phase === 'checking' || attempt.phase === 'refreshing'} onClick={() => void recover(attempt.request.requestId)}>{'response' in attempt ? '打开已确认会话' : '核对保留请求'}</Button>}<small>保留记录仅在此页面内有效；核对不会覆盖当前对话或草稿。切换连接或刷新页面前请保留会话与请求 ID。</small></section>)}</details>}
  {view === 'documents' ? <Workbench key={`${identityEpoch}:${scopeEpoch}:${viewEpoch}`} token={token} scopes={scopes} scope={scope} busy={busy} notice={workbenchNotice} isCurrent={() => useApp.getState().identityEpoch === identityEpoch && useApp.getState().scopeEpoch === scopeEpoch && useApp.getState().scope === scope && viewEpochRef.current === viewEpoch && viewRef.current === 'documents'} onConnect={() => setDialog(true)} onChat={() => switchView('chat')} onScopes={(next, id, notice = '') => { if (useApp.getState().identityEpoch !== identityEpoch || useApp.getState().scopeEpoch !== scopeEpoch || useApp.getState().scope !== scope || viewEpochRef.current !== viewEpoch || viewRef.current !== 'documents') return; turnOperations.current.invalidate(); setOptions(freshOptions()); set({ scopes: next, scope: id, workbenchNotice: notice, sources: [], session: null, messages: [], error: '' }) }} /> : <Group orientation="horizontal" className="panels"><Panel minSize="55%"><div className="chat-layout"><div className="chat-scroll"><div className="content"><div className="page-heading"><div><p className="eyebrow">KNOWLEDGE WORKSPACE <span> / 01</span></p><h1>知识库对话<span className="heading-dot">.</span></h1><p className="subtitle">让知识成为答案，让每个回答都有据可依。</p></div><div className="heading-icon"><Sparkles size={20}/></div></div>
   <div className="context-line"><span className="context-icon"><Library size={15}/></span><span>当前知识库</span><div className="select-wrap"><select aria-label="选择知识库" value={scope} disabled={busy || !scopes.length} onChange={e => { const id = e.target.value; void run(async () => { turnOperations.current.invalidate(); const availableSources = agentAvailable ? await loadSources(token, id) : []; setOptions(freshOptions()); set({ scope: id, sources: availableSources, session: null, messages: [], error: '' }) }) }}>{scopes.length ? scopes.map(s => <option key={s.scope_id} value={s.scope_id}>{s.name}</option>) : <option value="">{token ? '暂无知识库' : '连接后选择'}</option>}</select><ChevronDown size={14}/></div></div>
   {messages.length === 0 ? <motion.section className="welcome" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}><div className="welcome-mark"><Sparkles size={21}/></div><p className="eyebrow">START A CONVERSATION</p><h2>从一个问题开始</h2><p>关于文档、流程或某个想法，<br/>在这里找到有来源的答案。</p><div className="suggestions">{['帮我总结知识库的核心内容','查找与这个主题相关的资料','梳理文档中的关键流程'].map((s,i) => <Button key={s} onClick={() => { setDraft(s); document.getElementById('prompt')?.focus() }}><span className="suggestion-number">0{i+1}</span><span>{s}</span><ArrowUpRight size={15}/></Button>)}</div></motion.section> : <section className="messages" aria-live="polite">{messages.map((m,i) => <motion.article key={i} className={`message ${m.role}`} initial={{opacity:0,y:6}} animate={{opacity:1,y:0}}><div className="message-role">{m.role === 'user' ? <><span className="mini-avatar">你</span> 你</> : <><span className="mini-brand"><Sparkles size={14}/></span> Evidence KB</>}</div><div className="message-text">{m.text}</div>{m.role === 'assistant' && <>{m.response.unresolved && m.response.unresolved.length > 0 && <p className="unresolved">待确认：{m.response.unresolved.join('；')}</p>}{m.response.user_statements && m.response.user_statements.length > 0 && <div className="statement-list"><strong>你在会话中的陈述</strong>{m.response.user_statements.map((item, n) => <p key={n}>事件 #{item.event_seq}：{item.quote}</p>)}</div>}{m.response.evidence?.filter(e => m.response.claims?.some(c => c.evidence_ids?.includes(e.evidence_id))).map((e, j) => <Button className="citation" key={e.evidence_id} onClick={() => e.citation_id && void preview(e.citation_id)} disabled={!e.citation_id || busy}><FileText size={14}/><span>{j+1}. {e.title || e.filename || sources.find(source => source.source_id === e.source_id)?.title || e.doc_id || e.source_id || '来源文档'}</span><ArrowUpRight size={13}/></Button>)}{m.response.trace !== undefined && <details className="turn-details"><summary>查看本轮步骤与用量</summary><pre>{JSON.stringify({trace: m.response.trace, stop_reason: m.response.stop_reason}, null, 2)}</pre></details>}</>}</motion.article>)}{busy && <div className="thinking"><LoaderCircle size={15} className="spin"/><Shimmer>正在检索与整理证据</Shimmer></div>}</section>}
  </div></div><div className="composer-area"><div className="composer-inner"><ChatOptionsPanel options={options} sources={sources} disabled={busy || !agentAvailable || !canSend} onChange={setOptions}/>{error && <div className="error" role="alert">{error}<Button onClick={() => set({error:''})} aria-label="关闭提示"><X size={14}/></Button></div>}<div className="composer beautiful-composer"><PromptBar demo={false} tall value={draft} onValueChange={setDraft} disabled={busy} sendDisabled={!canSend} onSend={text => void send(text)} placeholder="向知识库提问，或输入你想了解的内容…"/><div className="composer-context"><BookOpen size={15}/> {selected?.name || '知识库问答'}</div></div><div className="composer-note"><span>回答基于知识库内容生成，请核对重要信息</span><span>Enter 发送 · Shift + Enter 换行</span></div></div></div></div></Panel>{wide && <><Separator className="resize-handle"/><Panel defaultSize="25%" minSize="18%" maxSize="32%" className="info-panel"><div className="side-info"><p className="eyebrow">YOUR WORKSPACE</p><h3>知识，从这里连接</h3><p>选择知识库，提出问题。回答会尽可能附上相关来源，方便你继续查看和核实。</p><div className="info-divider"/><div className="info-row"><span>01</span><div><strong>选择知识库</strong><small>限定本次对话的知识范围</small></div></div><div className="info-row"><span>02</span><div><strong>提出你的问题</strong><small>用自然语言描述你的需求</small></div></div><div className="info-row"><span>03</span><div><strong>查看回答与来源</strong><small>回到原文，了解更多上下文</small></div></div><div className="info-bottom"><CircleHelp size={15}/> <span>内容以实际检索结果为准</span></div></div></Panel></>}</Group>}</main>
  <Dialog open={!!runtime} onOpenChange={open => { if (!open) setRuntime('') }}><DialogContent className="modal" showCloseButton={false}><DialogTitle>服务状态</DialogTitle><DialogDescription>{runtime}</DialogDescription><Button className="secondary-action" onClick={() => setRuntime('')}>关闭</Button></DialogContent></Dialog>
  <Dialog open={!!previewUrl} onOpenChange={open => { if (!open) setPreviewUrl('') }}><DialogContent className="preview-dialog" showCloseButton={false}><div className="preview-header"><DialogTitle>原文预览</DialogTitle><Button className="icon-button" aria-label="关闭原文预览" onClick={() => setPreviewUrl('')}><X/></Button></div><DialogDescription>当前有权访问的原文定位</DialogDescription>{previewUrl && <iframe title="原文内容" sandbox="" src={previewUrl}/>}</DialogContent></Dialog>
  <Dialog open={dialog} onOpenChange={open => { if (!busy) setDialog(open) }}><DialogContent className="modal" showCloseButton={false}><div className="modal-head"><span className="modal-icon"><KeyRound size={18}/></span><Button className="icon-button" aria-label="关闭" onClick={() => setDialog(false)} disabled={busy}><X/></Button></div><DialogTitle>连接知识库</DialogTitle><DialogDescription>输入本地访问令牌，连接当前身份可访问的知识库和对话。</DialogDescription><form onSubmit={e => void connect(e)}><label htmlFor="token">访问令牌</label><input id="token" type="password" autoComplete="off" placeholder="粘贴访问令牌" value={tokenInput} onChange={e => setTokenInput(e.target.value)} autoFocus/><small>令牌仅保留在当前页面内，刷新后需要重新连接。</small>{error && <div className="modal-error" role="alert">{error}</div>}<Button className="connect-button" type="submit" disabled={!tokenInput.trim() || busy}>{busy ? '正在连接…' : '连接知识库'} <ArrowUpRight size={15}/></Button></form></DialogContent></Dialog>
 </div></MotionConfig>
}

createRoot(document.getElementById('root')!).render(<App />)
