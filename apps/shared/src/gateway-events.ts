/**
 * Wire types for `tui_gateway` JSON-RPC notifications and the RPC responses the
 * TypeScript surfaces (Ink TUI, Desktop, web dashboard) share.
 *
 * Every notification arrives as `{jsonrpc: '2.0', method: 'event', params: GatewayEvent}`
 * (`tui_gateway/server.py::_event_frame`); server→client REQUESTS (`{id, method, params}`,
 * `tui_gateway/server_requests.py`) are typed by `ServerRequestMap` below. `GatewayEventMap` is the single map from
 * event `type` to payload shape; `BACKEND_EVENT_NAMES` mirrors the emitter side and is
 * pinned to `gateway-events.json` by `gateway-events.test.ts` (vitest) and
 * `tests/tui_gateway/test_gateway_event_contract.py` (Python), so a name added on one
 * side without the other fails a test instead of drifting silently.
 *
 * Payload interfaces are typed from the Python emitters (file::symbol noted per
 * interface). Events whose payload no TS client reads yet are `Record<string, unknown>`;
 * they still MUST be keys so `on('x', …)` stays exhaustive.
 */
import type { BillingBlock } from './billing-types.js'
import type { HermesSkin } from './skin.js'

// ── Shared value shapes ──────────────────────────────────────────────

/** `tui_gateway/server.py::_get_usage` — a session's token/cost counters. */
export interface Usage {
  active_subagents?: number
  /** Rolling mean API latency over the last 10 calls (seconds). */
  avg_latency_s?: number
  /** Rolling output tokens/sec over the last 10 calls. */
  avg_tps?: number
  /** Session prompt-cache hit ratio (cache_read / prompt tokens, %). Omitted (not 0)
   *  when the provider reports no cache reads. */
  cache_hit_pct?: number
  cache_read?: number
  cache_write?: number
  calls: number
  compressions?: number
  context_max?: number
  context_percent?: number
  context_estimated?: boolean
  context_source?: string
  context_used?: number
  cost_status?: string
  cost_usd?: number
  dev_credits_spent_micros?: number
  input: number
  output: number
  reasoning?: number
  total: number
}

/** Advisory `{layer, code, retryable}` descriptor (`agent/error_surface.py`). */
export interface ErrorSurface {
  code?: string
  layer?: string
  retryable?: boolean
}

/** `tui_gateway/tool_progress.py::_normalize_todo_state` — full task snapshot. */
export interface TodoStatePayload {
  revision?: number
  todos?: unknown[]
}

export type SubagentStatus = 'completed' | 'error' | 'failed' | 'interrupted' | 'queued' | 'running' | 'timeout'

/** `tui_gateway/tool_progress.py::_progress_subagent` — every `subagent.*` frame. */
export interface SubagentEventPayload {
  api_calls?: number
  /** The child's own gateway session id — the key a watch window mirrors. */
  child_session_id?: string
  /** Batch (delegation) id this subagent belongs to — distinguishes
   *  interleaved `[n/N]` progress from concurrent or nested fan-outs. */
  delegation_id?: string
  depth?: number
  duration_seconds?: number
  files_read?: string[]
  files_written?: string[]
  goal: string
  input_tokens?: number
  model?: string
  output_tail?: { is_error?: boolean; preview?: string; tool?: string }[]
  output_tokens?: number
  parent_id?: null | string
  reasoning_tokens?: number
  status?: SubagentStatus
  subagent_id?: string
  summary?: string
  task_count?: number
  task_index: number
  text?: string
  tool_count?: number
  tool_name?: string
  tool_preview?: string
  toolsets?: string[]
}

// ── Event payloads ───────────────────────────────────────────────────

/** `tui_gateway/entry.py` (stdio) / `tui_gateway/ws.py` (WebSocket) first frame. */
export interface GatewayReadyPayload {
  /** Backends with the change watcher broadcast `*.changed` events; consumers
   *  demote their legacy polls to slow backstops. */
  change_events?: boolean
  /** WebSocket transport only: the server answers heartbeat pings. */
  heartbeat?: boolean
  /** Opaque token for this server process's `seq` numbering; a new epoch means
   *  replay watermarks must be discarded. */
  replay_epoch?: string
  skin?: HermesSkin
}

/** `tui_gateway/prompt_turn.py::_complete_turn_payload` and
 *  `tui_gateway/session_auto_continue.py::_emit_terminal_turn_error`. */
export interface MessageCompletePayload {
  /** Structured billing wall when the turn failed with FailoverReason.billing. */
  billing?: BillingBlock
  /** `status: "error"` — the failure message (`text` may be streamed output). */
  error?: string
  error_surface?: ErrorSurface
  failure_reason?: string | null
  /** `status: "error"` — `text` is streamed partial output to keep, not the error string. */
  partial?: boolean
  reasoning?: string
  /** `status: "error"` — the failed turn was retained and replays via `session.resume.inflight`. */
  recoverable?: boolean
  rendered?: string
  /** The final text was already previewed via `message.interim`; settle, don't duplicate. */
  response_previewed?: boolean
  status?: 'complete' | 'error' | 'interrupted' | string
  text?: string
  usage?: Usage
  /** History-commit note (e.g. a mid-turn desync the gateway surfaced instead of dropping). */
  warning?: string
}

/** `tui_gateway/tool_progress.py::_on_tool_start`. */
export interface ToolStartPayload {
  /** Full tool arguments — the 80-char `context` preview is display-only. */
  args?: Record<string, unknown>
  /** Verbose mode only: pretty-printed args. */
  args_text?: string
  context?: string
  name?: string
  /** Mirrored child tool rows carry a short preview instead of args. */
  preview?: string
  tool_id: string
  /** Not on the wire (`_on_tool_start` never sets it): the todo snapshot rides `tool.complete` /
   *  `todo.updated`. Kept because the TUI handler reads it and its fixtures exercise that path. */
  todos?: unknown[]
}

/** `tui_gateway/tool_progress.py::_on_tool_complete`. */
export interface ToolCompletePayload {
  args?: Record<string, unknown>
  duration_s?: number
  inline_diff?: string
  name?: string
  /** Parsed JSON when the tool returned JSON, else the raw string. */
  result?: unknown
  /** Verbose mode only. */
  result_text?: string
  revision?: number
  summary?: string
  tool_id: string
  todos?: unknown[]
}

export interface ToolGeneratingPayload {
  name?: string
}

/** `tui_gateway/tool_progress.py::_progress_output_risk`. */
export interface ToolOutputRiskPayload {
  findings?: string[]
  name?: string
  redacted?: boolean
  risk?: string
  tool_id?: string
}

export interface StatusUpdatePayload {
  kind?: string
  text?: string
}

export interface NotificationShowPayload {
  id?: string
  key?: string
  kind?: 'sticky' | 'ttl' | string
  level?: 'error' | 'info' | 'success' | 'warn' | string
  text?: string
  ttl_ms?: null | number
}

export interface NotificationClearPayload {
  key?: string
}

export interface TextPayload {
  text?: string
}

/** `message.delta` / `reasoning.delta` / `reasoning.available` / `thinking.delta`. */
export interface StreamDeltaPayload {
  rendered?: string
  text?: string
  /** Verbose reasoning mode is on for this session. */
  verbose?: boolean
}

export interface MessageInterimPayload {
  already_streamed?: boolean
  text: string
}

export interface SessionUsagePayload {
  usage?: Usage
}

export interface SessionTitlePayload {
  session_id?: string
  title?: string
}

/** `tui_gateway/methods_session.py` resume hydration progress. */
export interface SessionResumeProgressPayload {
  message?: string
  message_count?: number
  phase?: string
  status?: 'complete' | 'failed' | 'loading' | string
}

/** `tui_gateway/session_lifecycle.py::_announce_session_reclaimed`. */
export interface SessionReclaimedPayload {
  reason?: string
  session_id?: string
  stored_session_id?: string
}

export interface SessionControlUpdatePayload {
  control?: unknown
}

export interface ErrorPayload {
  message?: string
  reason?: string
}

export interface NoticePayload {
  message?: string
}

export interface ReactionPayload {
  kind?: string
}

export interface BillingStepUpVerificationPayload {
  user_code?: string
  verification_url: string
}

export interface VoiceStatusPayload {
  state?: 'idle' | 'listening' | 'transcribing' | string
}

export interface VoiceTranscriptPayload {
  no_speech_limit?: boolean
  stop_phrase?: boolean
  text?: string
  typed?: boolean
  voice_stopped?: boolean
}

export interface WakeDetectedPayload {
  phrase?: string
  profile?: null | string
  start_new_session?: boolean
}

export interface BrowserProgressPayload {
  level?: 'error' | 'info' | 'warn' | string
  message?: string
}

export interface MoaReferencePayload {
  count?: number
  index?: number
  label?: string
  text?: string
}

export interface MoaAggregatingPayload {
  aggregator?: string
}

export interface MoaProgressPayload {
  label?: string
  refs_done?: number
  refs_total?: number
}

export interface MoaPhasePayload {
  aggregator?: string
  phase?: string
  refs_done?: number
  refs_total?: number
}

// ── Server→client requests (`tui_gateway/server_requests.py`) ───────────
//
// The backend asks the renderer a question with a real JSON-RPC request
// (`{id: 'srq-…', method, params}`) and blocks on the response frame. Every
// entry below is one method: its `params` shape and the `result` the client
// answers with. `request.cancel` (an event) withdraws an open request on
// timeout / interrupt / session close; `open_requests` on `session.resume` /
// `session.events.since` re-delivers unanswered ones after a reconnect.

/** `request.cancel` payload — the backend withdrew an open server request. */
export interface RequestCancelPayload {
  id: string
  method: string
  reason: string
}

export interface ClarifyQuestion {
  choices?: null | string[]
  multi_select?: boolean
  qid: string
  question: string
}

/** `clarify` params. Single question: `question`/`choices`(/`multi_select`); batch: `questions`.
 *  `answers` rides along only on a reconnect replay (locks the server already accepted). */
export interface ClarifyRequestParams {
  answers?: Record<string, string>
  choices?: null | string[]
  multi_select?: boolean
  question?: string
  questions?: ClarifyQuestion[]
}

/** `clarify` result. Single: `{answer}` ('' = skip). Batch: the request resolves through
 *  `clarify.lock` RPCs (the last lock completes it); a response with no `answers` is cancel-all. */
export interface ClarifyResult {
  answer?: string
  answers?: Record<string, string>
}

/** `approval` params (`tui_gateway/server.py::_approval_request_payload`, command redacted server-side). */
export interface ApprovalRequestParams {
  allow_permanent?: boolean
  choices?: string[]
  command: string
  description: string
  request_id: string
  smart_denied?: boolean
}

export interface ApprovalResult {
  all?: boolean
  choice: 'always' | 'deny' | 'once' | 'session'
}

/** Every prompt whose answer is one string: `sudo`, `secret`, the vault prompts, the desktop GUI
 *  bridges (`terminal.read`, `preview.read`, `preview.act`, `window.read`, `tour`) and `mcp.setup`.
 *  '' means skipped / declined. */
export interface ValueResult {
  value: string
}

export interface SecretRequestParams {
  env_var: string
  metadata?: Record<string, unknown>
  prompt: string
}

export interface VaultUnlockRequestParams {
  backend: string
  display_name: string
}

export interface VaultSaveLoginRequestParams {
  origin: string
  site: string
}

export interface VaultCodeRequestParams {
  hint?: string
  site?: string
}

export interface McpSetupRequestParams {
  action?: string
  reason?: string
  server?: string
}

export interface ReadRangeRequestParams {
  count?: number
  start?: number
}

/** Server→client request method → `{params, result}`. Every method the backend can ask. */
export interface ServerRequestMap {
  approval: { params: ApprovalRequestParams; result: ApprovalResult }
  clarify: { params: ClarifyRequestParams; result: ClarifyResult }
  'mcp.setup': { params: McpSetupRequestParams; result: ValueResult }
  'preview.act': { params: Record<string, unknown>; result: ValueResult }
  'preview.read': { params: ReadRangeRequestParams; result: ValueResult }
  secret: { params: SecretRequestParams; result: ValueResult }
  sudo: { params: Record<string, never>; result: ValueResult }
  'terminal.read': { params: ReadRangeRequestParams; result: ValueResult }
  tour: { params: Record<string, unknown>; result: ValueResult }
  'vault.code': { params: VaultCodeRequestParams; result: ValueResult }
  'vault.save_login': { params: VaultSaveLoginRequestParams; result: ValueResult }
  'vault.unlock_prompt': { params: VaultUnlockRequestParams; result: ValueResult }
  'window.read': { params: Record<string, never>; result: ValueResult }
}

export type ServerRequestMethod = keyof ServerRequestMap

/** Pinned to `gateway-events.json`'s `server_requests` list by the two contract tests. Keep sorted. */
export const SERVER_REQUEST_METHODS = [
  'approval',
  'clarify',
  'mcp.setup',
  'preview.act',
  'preview.read',
  'secret',
  'sudo',
  'terminal.read',
  'tour',
  'vault.code',
  'vault.save_login',
  'vault.unlock_prompt',
  'window.read'
] as const satisfies readonly ServerRequestMethod[]

/** Side agents (`tui_gateway/methods_prompt.py::_spawn_side_agent`). */
export interface SideAgentCompletePayload {
  question?: string
  task_id: string
  text: string
}

export interface PreviewRestartProgressPayload {
  task_id: string
  text: string
}

export interface TerminalOutputPayload {
  chunk?: string
  process_id?: string
}

export interface TerminalClosePayload {
  process_id?: string
}

// ── The map ──────────────────────────────────────────────────────────

/**
 * Backend-emitted notification names. Derived from the `tui_gateway` emitter call
 * sites and pinned to `gateway-events.json`; keep sorted. Adding a name here without
 * the JSON (or vice versa) fails `gateway-events.test.ts`, and a Python emitter that
 * names an event missing from the JSON fails `test_gateway_event_contract.py`.
 */
export const BACKEND_EVENT_NAMES = [
  'agent.terminal.output',
  'background.complete',
  'billing.step_up.verification',
  'bot_relay.outbox.pending',
  'browser.controller.cancel',
  'browser.controller.command',
  'browser.progress',
  'btw.complete',
  'cron.changed',
  'error',
  'gateway.ready',
  'layout.apply',
  'message.complete',
  'message.delta',
  'message.interim',
  'message.reaction',
  'message.start',
  'moa.aggregating',
  'moa.phase',
  'moa.progress',
  'moa.reference',
  'notice',
  'notification.clear',
  'notification.show',
  'pairing.changed',
  'pane.reveal',
  'pet.changed',
  'pet.generate.progress',
  'pet.hatch.progress',
  'platforms.changed',
  'preview.close',
  'preview.open',
  'preview.restart.complete',
  'preview.restart.progress',
  'reaction',
  'reasoning.available',
  'reasoning.delta',
  'request.cancel',
  'review.summary',
  'session.control.update',
  'session.info',
  'session.reclaimed',
  'session.resume_progress',
  'session.title',
  'session.usage',
  'sessions.changed',
  'setup.ready',
  'skin.changed',
  'status.update',
  'subagent.complete',
  'subagent.progress',
  'subagent.spawn_requested',
  'subagent.start',
  'subagent.thinking',
  'subagent.tool',
  'terminal.close',
  'thinking.delta',
  'tip.show',
  'todo.updated',
  'tool.complete',
  'tool.generating',
  'tool.output_risk',
  'tool.start',
  'voice.interrupted',
  'voice.status',
  'voice.transcript',
  'wake.detected'
] as const satisfies readonly (keyof BackendGatewayEventMap)[]

export type BackendGatewayEventName = (typeof BACKEND_EVENT_NAMES)[number]

/** Payload per backend-emitted notification `type`. Keys are exactly `BACKEND_EVENT_NAMES`. */
export interface BackendGatewayEventMap {
  'agent.terminal.output': TerminalOutputPayload
  'background.complete': SideAgentCompletePayload
  'billing.step_up.verification': BillingStepUpVerificationPayload
  'bot_relay.outbox.pending': Record<string, unknown>
  'browser.controller.cancel': Record<string, unknown>
  'browser.controller.command': Record<string, unknown>
  'browser.progress': BrowserProgressPayload
  'btw.complete': SideAgentCompletePayload
  'cron.changed': Record<string, unknown>
  error: ErrorPayload
  'gateway.ready': GatewayReadyPayload
  'layout.apply': Record<string, unknown>
  'message.complete': MessageCompletePayload
  'message.delta': StreamDeltaPayload
  'message.interim': MessageInterimPayload
  'message.reaction': Record<string, unknown>
  'message.start': undefined
  'moa.aggregating': MoaAggregatingPayload
  'moa.phase': MoaPhasePayload
  'moa.progress': MoaProgressPayload
  'moa.reference': MoaReferencePayload
  notice: NoticePayload
  'notification.clear': NotificationClearPayload
  'notification.show': NotificationShowPayload
  'pairing.changed': Record<string, unknown>
  'pane.reveal': Record<string, unknown>
  'pet.changed': Record<string, unknown>
  'pet.generate.progress': Record<string, unknown>
  'pet.hatch.progress': Record<string, unknown>
  'platforms.changed': Record<string, unknown>
  'preview.close': Record<string, unknown>
  'preview.open': Record<string, unknown>
  'preview.restart.complete': SideAgentCompletePayload
  'preview.restart.progress': PreviewRestartProgressPayload
  reaction: ReactionPayload
  'reasoning.available': StreamDeltaPayload
  'reasoning.delta': StreamDeltaPayload
  'request.cancel': RequestCancelPayload
  'review.summary': TextPayload
  'session.control.update': SessionControlUpdatePayload
  /** Surface-specific shape (`tui_gateway/server.py::_session_info`); each client narrows. */
  'session.info': Record<string, unknown>
  'session.reclaimed': SessionReclaimedPayload
  'session.resume_progress': SessionResumeProgressPayload
  'session.title': SessionTitlePayload
  'session.usage': SessionUsagePayload
  'sessions.changed': Record<string, unknown>
  'setup.ready': Record<string, unknown>
  'skin.changed': HermesSkin
  'status.update': StatusUpdatePayload
  'subagent.complete': SubagentEventPayload
  'subagent.progress': SubagentEventPayload
  'subagent.spawn_requested': SubagentEventPayload
  'subagent.start': SubagentEventPayload
  'subagent.thinking': SubagentEventPayload
  'subagent.tool': SubagentEventPayload
  'terminal.close': TerminalClosePayload
  'thinking.delta': StreamDeltaPayload
  'tip.show': Record<string, unknown>
  'todo.updated': TodoStatePayload
  'tool.complete': ToolCompletePayload
  'tool.generating': ToolGeneratingPayload
  'tool.output_risk': ToolOutputRiskPayload
  'tool.start': ToolStartPayload
  'voice.interrupted': Record<string, unknown>
  'voice.status': VoiceStatusPayload
  'voice.transcript': VoiceTranscriptPayload
  'wake.detected': WakeDetectedPayload
}

/**
 * Client-local synthetic events. Never emitted by `tui_gateway`; the Ink TUI's
 * `gatewayClient` publishes them into the same handler stream to report transport
 * state. Excluded from `gateway-events.json` on purpose.
 */
export interface ClientLocalGatewayEventMap {
  'dashboard.new_session_requested': { reason?: string }
  'gateway.protocol_error': { preview?: string }
  'gateway.reconnecting': { attempt?: number; delay_ms?: number }
  'gateway.start_timeout': { cwd?: string; python?: string; stderr_tail?: string }
  'gateway.stderr': { line: string }
}

export interface GatewayEventMap extends BackendGatewayEventMap, ClientLocalGatewayEventMap {}

export type GatewayEventName = keyof GatewayEventMap

/** One `event` notification's `params`. */
export interface GatewayEvent<K extends GatewayEventName = GatewayEventName> {
  /** Registry connection whose socket delivered the event (renderer-side tag;
   * absent for the local/legacy primary path). */
  connectionId?: string
  payload?: GatewayEventMap[K]
  /** Renderer-side source tag added by the Desktop gateway registry. */
  profile?: string
  /** Per-session monotonic counter stamped by `tui_gateway/event_replay.py::_stamp_event`;
   *  absent on session-less broadcasts. */
  seq?: number
  session_id?: string
  type: K
}

// ── RPC responses shared across surfaces ─────────────────────────────

/** `hermes_cli/inventory.py` one `model.options` provider row (union of every field the
 *  backend sets; `pricing_pending` / `free_tier_pending` mark the cached-only fail-closed path). */
export interface ModelOptionProvider {
  /** User-defined providers only: every accepted identity for this endpoint
   *  (bare config key, `custom:<key>`, normalized display name, …). A session's
   *  `model.options` reports the canonical `custom:<key>` form, so "is this row
   *  the current provider?" must check membership here, not slug equality. */
  aliases?: string[]
  /** OpenAI-compatible endpoint for a user-defined provider. The backend
   *  exposes this as `api_url`; model assignments send it back as `base_url`. */
  api_url?: string
  /** Auth flow for an unconfigured provider: "api_key" can be activated inline
   *  by pasting `key_env`; anything else (oauth_*, external, aws_sdk, …) needs
   *  the `hermes model` CLI / onboarding OAuth flow. */
  auth_type?: string
  /** True when the provider has usable credentials. False for canonical
   *  providers surfaced by `include_unconfigured` that the user hasn't set up
   *  yet — render these with a setup affordance instead of hiding them. */
  authenticated?: boolean
  /** Per-model option support, keyed by model id (present when the picker
   *  requested capabilities). Lets the UI gate fast/reasoning controls. */
  capabilities?: Record<string, ModelCapabilities>
  /** Curated shortlist (one flagship per lab) the picker shows by default for
   *  aggregator providers that serve dozens of models across many labs. */
  featured_models?: string[]
  /** Nous only: whether the current account is on the free plan. */
  free_tier?: boolean
  /** Nous only, cached-only inventory: entitlement unknown, every model rendered locked. */
  free_tier_pending?: boolean
  /** True for the free-tier route's own provider row (no account behind it).
   *  Never match this row by `name` — the label is copy and can change. */
  free_tier_row?: boolean
  is_current?: boolean
  /** True for providers defined via the user's `providers:` config block. */
  is_user_defined?: boolean
  /** Env var to paste an API key into, for unconfigured `api_key` providers. */
  key_env?: string
  models?: string[]
  name: string
  /** Per-model pricing keyed by model id (present when the picker requested
   *  pricing and the provider supports live pricing). */
  pricing?: Record<string, ModelPricing>
  /** Cached-only inventory: pricing not fetched yet. */
  pricing_pending?: boolean
  slug: string
  source?: string
  total_models?: number
  /** Nous only: paid models a free-tier user cannot select (shown disabled). */
  unavailable_models?: string[]
  warning?: string
}

export interface ModelPricing {
  /** Formatted $/Mtok cached-input price, or null when the model has none. */
  cache: null | string
  /** Sale: rounded percent off list when gateway sends pricing.original. */
  discount_percent?: number
  /** True when the model costs nothing (free tier eligible). */
  free: boolean
  /** Formatted $/Mtok input price, e.g. "$3.00", or "free", or "" if unknown. */
  input: string
  /** Formatted $/Mtok output price. */
  output: string
  /** Sale: formatted pre-discount input $/Mtok ("was"). */
  was_input?: string
  /** Sale: formatted pre-discount output $/Mtok ("was"). */
  was_output?: string
}

export interface ModelCapabilities {
  /** False when the route rejects a reasoning disable ("mandatory" in the
   *  provider catalog), so the Thinking toggle must not be offered. */
  can_disable_reasoning?: boolean
  fast: boolean
  reasoning: boolean
}

export interface ModelOptionsResponse {
  model?: string
  provider?: string
  providers?: ModelOptionProvider[]
}

/** `tui_gateway/methods_session.py::_session_row_summary` — one `session.list` row. */
export interface SessionListItem {
  id: string
  message_count: number
  preview: string
  /** The runtime id this stored session is currently attached to, when live. */
  resolved_id?: string
  source?: string
  started_at: number
  title: string
}

export interface SessionListResponse {
  sessions?: SessionListItem[]
}

/** Transcript row as projected by the gateway (`session.resume` / `session.activate`). */
export interface GatewayTranscriptMessage {
  args?: unknown
  context?: string
  display_kind?: string
  display_metadata?: unknown
  name?: string
  role: 'assistant' | 'system' | 'tool' | 'user'
  text?: string
}

export interface SessionInflightTurn {
  assistant?: string
  correction_offsets?: number[]
  corrections?: string[]
  error?: string
  error_surface?: ErrorSurface
  recoverable?: boolean
  status?: string
  streaming?: boolean
  user?: string
}

/** `tui_gateway/methods_session.py::_resume_response`. `info` is surface-specific
 *  (`SessionInfo` in the TUI, `SessionRuntimeInfo` on Desktop); narrow at the call site. */
export interface SessionResumeResponse<Info = Record<string, unknown>, Message = GatewayTranscriptMessage> {
  /** Present when the backend found a fresh crash-interrupted turn and scheduled its
   *  automatic continuation; the turn arrives as a normal message.start stream. */
  auto_continue?: { attempt: number; interrupted_at: number }
  /** Deferred hydration: history arrives via `session.resume_progress`. */
  hydrating?: boolean
  inflight?: null | SessionInflightTurn
  info?: Info
  message_count?: number
  messages: Message[]
  /** `omit_messages` resume: the client still learns the stored size. */
  messages_omitted?: boolean
  /** Server→client requests still unanswered for this session (a clarify, sudo prompt, …
   *  raised while the client was detached); the client re-delivers them to its request
   *  handlers. `pending_approval` (the approval queue's oldest entry) is the approval twin. */
  open_requests?: OpenServerRequest[]
  pending_approval?: ApprovalRequestParams
  resumed?: string
  running?: boolean
  session_id: string
  session_key?: string
  started_at?: number
  status?: string
  todo_state?: TodoStatePayload
}

/** One unanswered server→client request as returned by `open_requests`. */
export interface OpenServerRequest {
  id: string
  method: string
  params: Record<string, unknown> & { session_id?: string }
}
