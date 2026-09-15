import type {
  ConnectionAction,
  ConnectionRequestParams,
  ConnectionResult,
  ConnectionRequestTarget as ConnectionTarget,
  ConnectionTargetKind,
  ConnectionTargetOutcomeState,
  ConnectionTargetOutcome as GatewayConnectionTargetOutcome
} from '@hermes/shared'
import { atom, computed } from 'nanostores'

import { respondToServerRequest } from './server-requests'

/** Pending `connection` requests, keyed by runtime session id. The backend owns `opId`,
 *  targets and `deadlineAt`; the renderer never recomputes them. */
export type { ConnectionAction, ConnectionTarget, ConnectionTargetKind }

export interface ConnectionRequest {
  requestId: string
  opId: string
  /** Unix seconds, server-owned. */
  deadlineAt: number
  /** One sentence from the agent, shown on the card. */
  reason: string
  targets: ConnectionTarget[]
  /** Local receipt time (Unix seconds), used to reject stale resume cleanup. */
  receivedAt?: number
  sessionId: string | null
}

/** Generated target state for a connection operation result. */
export type ConnectionTargetState = ConnectionTargetOutcomeState

/** Generated target result for a connection operation. */
export type ConnectionTargetOutcome = GatewayConnectionTargetOutcome

/** Generated result sent through the server-request response rail. */
export type ConnectionOutcome = ConnectionResult

const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''

export const $connectionRequests = atom<Record<string, ConnectionRequest>>({})

/** One session's pending request (same shape as `sessionClarifyRequest`). */
export const sessionConnectionRequest = (sessionId: string | null) =>
  computed($connectionRequests, requests => requests[keyFor(sessionId)] ?? null)

/** Park the `connection` request's params (already validated against the generated contract by the
 *  backend). Null when it names no target: there is nothing for a card to show. */
export function normalizeConnectionRequest(
  params: ConnectionRequestParams | null | undefined,
  requestId: string,
  sessionId: string | null
): ConnectionRequest | null {
  if (!params || !requestId || !params.op_id || params.deadline_at <= 0 || params.targets.length === 0) {
    return null
  }

  return {
    deadlineAt: params.deadline_at,
    opId: params.op_id,
    reason: params.reason ?? '',
    receivedAt: Date.now() / 1000,
    requestId,
    sessionId,
    targets: params.targets.map(({ action, kind, name }) => ({ action, kind, name }))
  }
}

export function setConnectionRequest(request: ConnectionRequest): void {
  $connectionRequests.set({ ...$connectionRequests.get(), [keyFor(request.sessionId)]: request })
}

export function clearConnectionRequest(requestId?: string, sessionId?: string | null): void {
  const requests = $connectionRequests.get()

  if (sessionId !== undefined) {
    const key = keyFor(sessionId)
    const current = requests[key]

    if (!current || (requestId && current.requestId !== requestId)) {
      return
    }

    const next = { ...requests }
    delete next[key]
    $connectionRequests.set(next)

    return
  }

  const next: Record<string, ConnectionRequest> = {}
  let changed = false

  for (const [key, value] of Object.entries(requests)) {
    if (requestId && value.requestId !== requestId) {
      next[key] = value
    } else {
      changed = true
    }
  }

  if (changed) {
    $connectionRequests.set(next)
  }
}

/** Non-reactive read for the composer's Enter handler. */
export const hasConnectionRequest = (sessionId: string | null | undefined): boolean =>
  Boolean($connectionRequests.get()[keyFor(sessionId)])

/** Send the card's answer. Clears the entry first so the card cannot be answered twice;
 *  false when the request is already gone. */
export async function respondToConnectionRequest(request: ConnectionRequest, outcome: ConnectionOutcome): Promise<boolean> {
  const current = $connectionRequests.get()[keyFor(request.sessionId)]

  if (!current || current.requestId !== request.requestId) {
    return false
  }

  clearConnectionRequest(request.requestId, request.sessionId)

  return respondToServerRequest(request.requestId, {
    settled_by: outcome.settled_by,
    targets: outcome.targets
  })
}

/** Typing a message while the card is open declines every target, otherwise the typed
 *  message would wait behind the blocked tool until the deadline. */
export async function skipConnectionRequest(sessionId: string | null | undefined): Promise<boolean> {
  const request = $connectionRequests.get()[keyFor(sessionId)]

  if (!request) {
    return false
  }

  try {
    await respondToConnectionRequest(request, {
      settled_by: 'all_resolved',
      targets: request.targets.map(target => ({ name: target.name, state: 'declined' }))
    })
  } catch {
    // A failed skip must not block the message being sent; the tool settles on its deadline.
  }

  return true
}
