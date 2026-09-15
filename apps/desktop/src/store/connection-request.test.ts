import type { ConnectionRequestParams } from '@hermes/shared'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $connectionRequests,
  clearConnectionRequest,
  type ConnectionRequest,
  hasConnectionRequest,
  normalizeConnectionRequest,
  respondToConnectionRequest,
  setConnectionRequest,
  skipConnectionRequest
} from './connection-request'
import { rememberServerRequest, resetServerRequestsForTests } from './server-requests'

const WIRE: ConnectionRequestParams = {
  deadline_at: 1_800_000_000,
  op_id: 'op-1',
  reason: 'tickets',
  session_id: 's1',
  targets: [
    { action: 'install', kind: 'mcp', name: 'linear' },
    { action: 'install', kind: 'mcp', name: 'figma' }
  ],
  timeout_seconds: 120
}

function request(sessionId: string | null, requestId = 'req-1'): ConnectionRequest {
  return normalizeConnectionRequest(WIRE, requestId, sessionId)!
}

function remember(requestId: string) {
  const respond = vi.fn()
  rememberServerRequest({ fail: vi.fn(), id: requestId, method: 'connection', params: {}, respond })

  return respond
}

describe('connection-request store', () => {
  beforeEach(() => {
    $connectionRequests.set({})
  })

  afterEach(() => {
    $connectionRequests.set({})
    resetServerRequestsForTests()
  })

  it('normalizes the wire payload and keeps the server-owned deadline verbatim', () => {
    const parsed = normalizeConnectionRequest(WIRE, 'req-1', 's1')

    expect(parsed?.deadlineAt).toBe(WIRE.deadline_at)
    expect(parsed?.opId).toBe('op-1')
    expect(parsed?.targets.map(t => t.name)).toEqual(['linear', 'figma'])
  })

  it('rejects a payload with no targets, no op id or no deadline', () => {
    expect(normalizeConnectionRequest({ ...WIRE, targets: [] }, 'req-1', 's1')).toBeNull()
    expect(normalizeConnectionRequest({ ...WIRE, op_id: '' }, 'req-1', 's1')).toBeNull()
    expect(normalizeConnectionRequest({ ...WIRE, deadline_at: 0 }, 'req-1', 's1')).toBeNull()
    expect(normalizeConnectionRequest(null, 'req-1', 's1')).toBeNull()
  })

  it('keeps requests from concurrent sessions independent', () => {
    setConnectionRequest(request('a', 'req-a'))
    setConnectionRequest(request('b', 'req-b'))

    expect(hasConnectionRequest('a')).toBe(true)
    clearConnectionRequest('req-a', 'a')
    expect(hasConnectionRequest('a')).toBe(false)
    expect(hasConnectionRequest('b')).toBe(true)
  })

  it('a stale request id never clears a newer card', () => {
    setConnectionRequest(request('a', 'req-new'))
    clearConnectionRequest('req-old', 'a')

    expect($connectionRequests.get().a?.requestId).toBe('req-new')
  })

  it('respond clears the entry before the RPC and refuses a second answer', async () => {
    const req = request('a')
    const respond = remember(req.requestId)
    setConnectionRequest(req)

    const first = await respondToConnectionRequest(req, {
      settled_by: 'all_resolved',
      targets: [{ name: 'linear', state: 'installed' }]
    })

    const second = await respondToConnectionRequest(req, {
      settled_by: 'all_resolved',
      targets: [{ name: 'linear', state: 'declined' }]
    })

    expect(first).toBe(true)
    expect(second).toBe(false)
    expect(respond).toHaveBeenCalledWith({
      settled_by: 'all_resolved',
      targets: [{ name: 'linear', state: 'installed' }]
    })
  })

  it('skip declines every target of the pending operation', async () => {
    const req = request('a')
    const respond = remember(req.requestId)
    setConnectionRequest(req)

    expect(await skipConnectionRequest('a')).toBe(true)
    expect(await skipConnectionRequest('a')).toBe(false)
    expect(respond).toHaveBeenCalledWith({
      settled_by: 'all_resolved',
      targets: [
        { name: 'linear', state: 'declined' },
        { name: 'figma', state: 'declined' }
      ]
    })
  })
})
