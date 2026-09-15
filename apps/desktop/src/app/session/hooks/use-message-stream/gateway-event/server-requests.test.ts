import { afterEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $connectionRequests } from '@/store/connection-request'
import { resetServerRequestsForTests } from '@/store/server-requests'
import { $toursEnabled } from '@/store/tours'

import { handleServerRequest } from './server-requests'
import type { ServerRequestContext } from './server-requests'

const deps = {
  activeSessionIdRef: { current: null },
  sessionInterrupted: () => false,
  updateSessionState: (_sessionId, update) => update(createClientSessionState('stored-session')),
  upsertToolCall: () => undefined
} as ServerRequestContext['deps']

function deliver(method: string, params: Record<string, unknown>, activeSessionId: null | string) {
  const respond = vi.fn()
  const fail = vi.fn()
  const handled = handleServerRequest({ fail, id: 'srq-1', method, params, profile: 'default', respond }, deps, activeSessionId)

  return { fail, handled, respond }
}

describe('connection request routing', () => {
  afterEach(() => {
    $connectionRequests.set({})
    resetServerRequestsForTests()
  })

  it('parks a connection request and replaces a replayed request with the same id', () => {
    const params = {
      deadline_at: 1_800_000_000,
      op_id: 'op-1',
      reason: 'Install Linear',
      session_id: 'session-a',
      targets: [{ action: 'install', kind: 'mcp', name: 'linear' }],
      timeout_seconds: 60
    }

    expect(deliver('connection', params, 'session-a').handled).toBe(true)
    expect($connectionRequests.get()['session-a']).toMatchObject({ opId: 'op-1', requestId: 'srq-1' })

    expect(deliver('connection', { ...params, op_id: 'op-2' }, 'session-a').handled).toBe(true)
    expect($connectionRequests.get()['session-a']?.opId).toBe('op-2')
  })
})

describe('preview action request routing', () => {
  it('leaves a scoped action request unanswered in a window showing another session', () => {
    const { handled, respond, fail } = deliver('preview.act', { action: 'elements', session_id: 'session-a' }, 'session-b')

    expect(handled).toBe(true)
    expect(respond).not.toHaveBeenCalled()
    expect(fail).not.toHaveBeenCalled()
  })

  it('fails fast for an unscoped request with no session in view', () => {
    const { respond } = deliver('preview.act', { action: 'elements' }, null)

    expect(respond).toHaveBeenCalledWith({
      value: JSON.stringify({
        error: 'The in-app browser only takes actions in the session the user is looking at.',
        success: false
      })
    })
  })
})

describe('tour request routing', () => {
  afterEach(() => {
    $toursEnabled.set(true)
  })

  it('leaves a scoped request unanswered in another session even when tours are disabled', () => {
    $toursEnabled.set(false)
    const { handled, respond } = deliver('tour', { action: 'discover', session_id: 'session-a' }, 'session-b')

    expect(handled).toBe(true)
    expect(respond).not.toHaveBeenCalled()
  })

  it('fails fast for an unscoped request with no session in view', () => {
    const { respond } = deliver('tour', { action: 'discover' }, null)

    expect(respond).toHaveBeenCalledWith({
      value: JSON.stringify({ error: 'Tours only run in the session the user is looking at.', success: false })
    })
  })
})
