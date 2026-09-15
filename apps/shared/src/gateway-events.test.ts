import { describe, expect, it } from 'vitest'

import { BACKEND_EVENT_NAMES, SERVER_REQUEST_METHODS } from './gateway-events'
import contract from './gateway-events.json'

/**
 * Two-sided contract with `tests/tui_gateway/test_gateway_event_contract.py`: the
 * Python side pins the emitter / server-request call sites to `gateway-events.json`;
 * this side pins `BACKEND_EVENT_NAMES` (checked against `BackendGatewayEventMap` via
 * `satisfies`) and `SERVER_REQUEST_METHODS` (against `ServerRequestMap`) to the same
 * JSON. A name added on either side alone goes red here.
 */
describe('gateway-events.json ⇄ BackendGatewayEventMap / ServerRequestMap', () => {
  it('lists exactly the backend-emitted notification names', () => {
    expect([...BACKEND_EVENT_NAMES]).toEqual(contract.events)
  })

  it('lists exactly the server→client request methods', () => {
    expect([...SERVER_REQUEST_METHODS]).toEqual(contract.server_requests)
  })

  it.each([
    ['events', contract.events],
    ['server_requests', contract.server_requests]
  ])('keeps %s sorted and duplicate-free (stable diffs)', (_label, list) => {
    expect(list).toEqual([...list].sort())
    expect(new Set(list).size).toBe(list.length)
  })
})
