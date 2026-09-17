import type { GatewayEvent } from '@hermes/shared'
// A tool result that lands AFTER its part was sealed (turn settled without the
// completion, or the user typed mid-turn and the live bubble was closed) must
// re-attach to the part that already carries its tool_call_id. Seeding a new
// bubble instead leaves the original row on "Result unavailable" while a
// duplicate row for the same call shows below it (#113035).
import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { appendMidTurnUserMessage } from '@/app/session/hooks/use-prompt-actions/rewind'
import type { ChatMessage } from '@/lib/chat-messages'

import { renderMessageStream } from './test-harness'

const SID = 'late-tool-result-session'

const emit = (stream: ReturnType<typeof renderMessageStream>, event: GatewayEvent) =>
  act(() => stream.handleEvent(event))

function toolParts(messages: ChatMessage[], toolCallId: string) {
  return messages.flatMap((message, messageIndex) =>
    message.parts.flatMap(part =>
      part.type === 'tool-call' && part.toolCallId === toolCallId ? [{ messageIndex, part }] : []
    )
  )
}

describe('tool events for a part that already exists on a settled message', () => {
  afterEach(() => {
    cleanup()
  })

  it('re-attaches a tool.complete that arrives after message.complete sealed the part', () => {
    const stream = renderMessageStream(SID)

    emit(stream, { session_id: SID, type: 'message.start', payload: {} })
    emit(stream, {
      payload: { args: { command: 'sleep 90' }, name: 'terminal', tool_id: 'call_01' },
      session_id: SID,
      type: 'tool.start'
    })
    // The turn settles before the completion frame reaches this window.
    emit(stream, { payload: { text: 'done' }, session_id: SID, type: 'message.complete' })

    const sealed = toolParts(stream.state().messages, 'call_01')
    expect(sealed).toHaveLength(1)
    expect(sealed[0].part).toMatchObject({ type: 'tool-call' })

    emit(stream, {
      payload: { name: 'terminal', result: 'ok', tool_id: 'call_01' },
      session_id: SID,
      type: 'tool.complete'
    })

    const state = stream.state()
    const found = toolParts(state.messages, 'call_01')

    // One row for one call, on the message that owned it, with the result.
    expect(found).toHaveLength(1)
    expect(found[0].messageIndex).toBe(sealed[0].messageIndex)
    expect(found[0].part).toMatchObject({ result: 'ok', type: 'tool-call' })
    expect(state.messages.filter(m => m.role === 'assistant')).toHaveLength(1)
    // The late completion does not re-open a stream bubble.
    expect(state.streamId).toBeNull()
  })

  it('keeps one live row when the user types mid-turn and the running tool then completes', () => {
    const stream = renderMessageStream(SID)

    emit(stream, { session_id: SID, type: 'message.start', payload: {} })
    emit(stream, {
      payload: { args: { command: 'timeout 120 debug share' }, name: 'terminal', tool_id: 'call_02' },
      session_id: SID,
      type: 'tool.start'
    })

    // Mid-turn user insert (steer): the desktop seals the live bubble and
    // clears streamId so the next assistant output lands below the user row.
    act(() => {
      const current = stream.state()
      stream.states.set(
        SID,
        appendMidTurnUserMessage(current, {
          id: 'user-steer',
          role: 'user',
          parts: [{ type: 'text', text: 'also check the log' }],
          timestamp: Date.now() / 1000
        })
      )
    })

    // A running-phase event for the same call (progress/replay) must update
    // the existing row, not seed a second live row under the user message.
    emit(stream, {
      payload: { args: { command: 'timeout 120 debug share' }, name: 'terminal', tool_id: 'call_02' },
      session_id: SID,
      type: 'tool.start'
    })

    const running = toolParts(stream.state().messages, 'call_02')
    expect(running).toHaveLength(1)
    expect(running[0].messageIndex).toBe(0)
    expect(running[0].part).toMatchObject({ type: 'tool-call' })
    expect((running[0].part as { completedAt?: number }).completedAt).toBeUndefined()

    emit(stream, {
      payload: { name: 'terminal', result: 'shared', tool_id: 'call_02' },
      session_id: SID,
      type: 'tool.complete'
    })

    const state = stream.state()
    const found = toolParts(state.messages, 'call_02')

    expect(found).toHaveLength(1)
    expect(found[0].messageIndex).toBe(0)
    expect(found[0].part).toMatchObject({ result: 'shared', type: 'tool-call' })
    // user row stays the tail: no phantom assistant bubble was appended.
    expect(state.messages.at(-1)?.id).toBe('user-steer')
  })
})
