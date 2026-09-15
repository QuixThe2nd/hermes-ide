import { type ChatMessage, type GatewayEventPayload, restorePendingBlockingToolCall } from '@/lib/chat-messages'
import type { ConnectionRequest } from '@/store/connection-request'

/** Tool row for a pending operation whose `tool.start` event was missed. */
export function connectionRequestToolPayload(request: ConnectionRequest): GatewayEventPayload & { name: string } {
  return {
    args: {
      action: request.targets[0]?.action ?? 'install',
      connectors: request.targets.map(target => ({ mcp: target.kind === 'mcp', name: target.name })),
      reason: request.reason
    },
    name: 'manage_connections',
    tool_id: request.requestId
  }
}

/** Add the pending connection row to a projected transcript; null when there is none. */
export function projectPendingConnection(
  messages: ChatMessage[],
  request: ConnectionRequest | null
): { messages: ChatMessage[]; streamId: string } | null {
  return request ? restorePendingBlockingToolCall(messages, connectionRequestToolPayload(request)) : null
}
