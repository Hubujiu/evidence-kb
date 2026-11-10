export type TurnOwner = Readonly<{
  identityEpoch: number
  scopeId: string
  sessionId: string | null
  requestId: string
  question: string
  draftOwner: number
}>

export type SubmittedTurn = TurnOwner & { sessionId: string }
export type UnresolvedStatus = 'unknown' | 'running' | 'interrupted' | 'not_found' | 'reconciliation_required'
export type FailureFacts = Readonly<{
  code?: string
  mode?: string
  reason?: string
  model_sent?: boolean
  automatic_retry?: boolean
  recovery_action?: string
  request_id?: string
  physical_request_id?: string
  provider_request_id?: string
  operation_id?: string
  budget_operation_id?: string
  admission_receipt_id?: string
  outcome?: string
  capture_state?: string
  ledger_settlement?: string
}>

export type TurnState<T> =
  | { phase: 'idle' }
  | { phase: 'creating'; request: TurnOwner }
  | { phase: 'submitting'; request: SubmittedTurn }
  | { phase: 'rejected'; request: SubmittedTurn; detail: string }
  | { phase: 'unconfirmed' | 'checking'; request: SubmittedTurn; status: UnresolvedStatus; detail: string; facts?: FailureFacts }
  | { phase: 'confirmed' | 'refreshing' | 'refresh_required' | 'complete'; request: SubmittedTurn; response: T; detail: string; facts?: FailureFacts }

export type TurnEvent<T> =
  | { type: 'reset' }
  | { type: 'start'; request: TurnOwner }
  | { type: 'created'; requestId: string; sessionId: string }
  | { type: 'creation_failed'; requestId: string }
  | { type: 'not_submitted'; requestId: string }
  | { type: 'unconfirmed'; requestId: string; detail: string; facts?: FailureFacts }
  | { type: 'rejected'; requestId: string; detail: string }
  | { type: 'received'; requestId: string; response: T }
  | { type: 'check'; requestId: string }
  | { type: 'observed'; requestId: string; status: UnresolvedStatus; detail: string }
  | { type: 'check_failed'; requestId: string; detail: string }
  | { type: 'refresh'; requestId: string }
  | { type: 'refresh_failed'; requestId: string; detail: string }
  | { type: 'synced'; requestId: string; detail: string }

export function canStartTurn<T>(state: TurnState<T>) { return state.phase === 'idle' || state.phase === 'complete' || state.phase === 'rejected' }

export function isPreHandlerRejection(facts: FailureFacts) {
  return facts.code === 'DELIVERY_RECOVERY_REQUIRED' && facts.mode === 'restricted_recovery'
    && facts.reason === 'UNRESOLVED_PHYSICAL_REQUEST' && facts.model_sent === false && facts.automatic_retry === false
    && !facts.recovery_action && !facts.request_id && !facts.physical_request_id && !facts.provider_request_id
    && !facts.operation_id && !facts.budget_operation_id && !facts.admission_receipt_id
    && !facts.outcome && !facts.capture_state && !facts.ledger_settlement
}

export function canParkTurn<T>(state: TurnState<T>) {
  return state.phase === 'unconfirmed' && ['interrupted', 'not_found', 'reconciliation_required'].includes(state.status)
}

export function turnTransition<T>(state: TurnState<T>, event: TurnEvent<T>): TurnState<T> {
  if (event.type === 'reset') return { phase: 'idle' }
  if (event.type === 'start') {
    if (!canStartTurn(state)) return state
    return event.request.sessionId === null ? { phase: 'creating', request: event.request }
      : { phase: 'submitting', request: { ...event.request, sessionId: event.request.sessionId } }
  }
  if (state.phase === 'idle' || state.request.requestId !== event.requestId) return state
  switch (event.type) {
    case 'created':
      return state.phase === 'creating' ? { phase: 'submitting', request: { ...state.request, sessionId: event.sessionId } } : state
    case 'creation_failed':
      return state.phase === 'creating' ? { phase: 'idle' } : state
    case 'not_submitted':
      return state.phase === 'submitting' ? { phase: 'idle' } : state
    case 'unconfirmed':
      return state.phase === 'submitting' ? { phase: 'unconfirmed', request: state.request, status: 'unknown', detail: event.detail, facts: event.facts } : state
    case 'rejected':
      return state.phase === 'submitting' ? { phase: 'rejected', request: state.request, detail: event.detail } : state
    case 'received':
      return state.phase === 'submitting' || state.phase === 'checking'
        ? { phase: 'confirmed', request: state.request, response: event.response, detail: '', facts: 'facts' in state ? state.facts : undefined } : state
    case 'check':
      return state.phase === 'unconfirmed' ? { ...state, phase: 'checking' } : state
    case 'observed':
      return state.phase === 'checking' ? { ...state, phase: 'unconfirmed', status: event.status, detail: event.detail } : state
    case 'check_failed':
      return state.phase === 'checking' ? { ...state, phase: 'unconfirmed', detail: event.detail } : state
    case 'refresh':
      return state.phase === 'confirmed' || state.phase === 'refresh_required' ? { ...state, phase: 'refreshing' } : state
    case 'refresh_failed':
      return state.phase === 'refreshing' ? { ...state, phase: 'refresh_required', detail: event.detail } : state
    case 'synced':
      return state.phase === 'refreshing' ? { ...state, phase: 'complete', detail: event.detail } : state
  }
}

export type TurnRequests<T> = Readonly<{ active: TurnState<T>; retained: Exclude<TurnState<T>, { phase: 'idle' }>[] }>
export type RequestEvent<T> = TurnEvent<T> | { type: 'park'; requestId: string }

export function blocksRetainedSession<T>(state: TurnRequests<T>, sessionId: string | null) {
  return sessionId !== null && state.retained.some(attempt => attempt.request.sessionId === sessionId && !canStartTurn(attempt))
}

export function canSendTurn<T>(state: TurnRequests<T>, sessionId: string | null) {
  return canStartTurn(state.active) && !blocksRetainedSession(state, sessionId)
}

export function requestTransition<T>(state: TurnRequests<T>, event: RequestEvent<T>): TurnRequests<T> {
  if (event.type === 'reset') return { active: { phase: 'idle' }, retained: [] }
  if (event.type === 'park') {
    if (state.active.phase !== 'unconfirmed' || state.active.request.requestId !== event.requestId || !canParkTurn(state.active)) return state
    return { active: { phase: 'idle' }, retained: [...state.retained, state.active] }
  }
  if (event.type === 'start') {
    if (!canSendTurn(state, event.request.sessionId) || state.retained.some(attempt => attempt.request.requestId === event.request.requestId)) return state
    return { ...state, active: turnTransition(state.active, event) }
  }
  const index = state.retained.findIndex(attempt => attempt.request.requestId === event.requestId)
  if (index < 0) return { ...state, active: turnTransition(state.active, event) }
  const updated = turnTransition(state.retained[index], event)
  if (updated.phase === 'idle') return state
  return { ...state, retained: state.retained.map((attempt, current) => current === index ? updated : attempt) }
}
