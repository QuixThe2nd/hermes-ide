import type { GatewayEventName } from '@hermes/shared'
import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'late-tool-complete-session'

let stream: MessageStreamHarness

const event = (type: GatewayEventName, timestamp: number, payload: Record<string, unknown> = {}) =>
  act(() => stream.handleEvent({ payload: { ...payload, timestamp }, session_id: SID, type }))

const toolParts = () =>
  (stream.state(SID).messages ?? []).flatMap(message => message.parts.filter(part => part.type === 'tool-call'))

describe('late tool completions across interim boundaries', () => {
  beforeEach(async () => {
    stream = renderMessageStream(SID)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('attaches a late completion to the sealed bubble that owns the call', () => {
    event('message.start', 100)
    event('message.delta', 101, { text: 'Scraping the page.' })
    event('tool.start', 102, { args: { url: 'https://example.test' }, name: 'browser', tool_id: 'call-late-1' })
    // Interim commentary seals the streaming bubble while the browser scrape
    // keeps running (minutes in practice). streamId drops; the tool part is
    // sealed with completedAt and no result.
    event('message.interim', 103, { text: 'Scraping the page.' })
    event('tool.complete', 260.5, { name: 'browser', result: 'ok', tool_id: 'call-late-1' })

    const assistants = (stream.state(SID).messages ?? []).filter(message => message.role === 'assistant')

    expect(assistants).toHaveLength(1)
    // The sealed part got the result instead of staying "Result unavailable"...
    expect(assistants[0].parts).toEqual([
      expect.objectContaining({ completedAt: 102, type: 'text' }),
      expect.objectContaining({ result: 'ok', toolCallId: 'call-late-1', type: 'tool-call' })
    ])

    // ...and no duplicate row was seeded into a fresh bubble.
    expect(toolParts()).toHaveLength(1)
  })

  it('seeds a fresh bubble for a completion with no owner', () => {
    event('message.start', 400)
    event('message.interim', 401, { text: 'Sealed.' })
    // A completion whose tool.start never arrived (reconnect gap): no part
    // owns the id, so it seeds a bubble as before instead of vanishing.
    event('tool.complete', 402, { name: 'grep', result: 'match', tool_id: 'call-fresh-1' })

    const assistants = (stream.state(SID).messages ?? []).filter(message => message.role === 'assistant')

    expect(assistants).toHaveLength(2)
    expect(assistants[1].parts).toEqual([
      expect.objectContaining({ result: 'match', toolCallId: 'call-fresh-1', type: 'tool-call' })
    ])
  })
})
