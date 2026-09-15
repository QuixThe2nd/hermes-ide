import type { ToolCallMessagePartProps } from '@assistant-ui/react'
import type { ConnectionTargetState } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useState } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import { resolveSessionOwner } from '@/app/session/hooks/use-session-actions/utils'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { Button } from '@/components/ui/button'
import {
  ConnectorCard,
  type ConnectorCardCopy,
  type ConnectorCardOutcome,
  type ConnectorCardState,
  ConnectorSummary,
  outcomeMeta
} from '@/components/ui/connector-card'
import { useI18n } from '@/i18n'
import { connectorCalls, connectorText, connectorTitle, connectorToolName, recordOf } from '@/lib/connector-tools'
import {
  type ConnectionRequest,
  type ConnectionTarget,
  continueConnectionRequest,
  sessionConnectionRequest,
  skipConnectionTarget
} from '@/store/connection-request'
import { requestGatewayForAgent } from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { assertSessionOwnerResolved } from '@/store/session-owner-resolution'
import { isSessionOwnerRoute } from '@/store/session-request-router'

interface ConnectorOwner {
  connectionId: null | string
  profile: string
}

/** Names requested by a manage_connections part, including an event-projected row. */
function requestedConnectorNames(args: ToolCallMessagePartProps['args']): string[] {
  const connectors = recordOf(args).connectors
  const entries = Array.isArray(connectors) ? connectors : [connectors]

  return entries.flatMap(entry => {
    const row = recordOf(entry)
    const name = connectorText(entry) ?? connectorText(row.name) ?? connectorText(row.connector)
    const trimmed = name?.trim()

    return trimmed ? [trimmed] : []
  })
}

function matchingTargetNames(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) {
    return false
  }

  const leftSorted = [...left].sort()
  const rightSorted = [...right].sort()

  return leftSorted.every((name, index) => name === rightSorted[index])
}

/** The card lives on the tool row whose id opened the operation and on no other. */
export function connectionRequestOwnsPart(props: ToolCallMessagePartProps, request: ConnectionRequest | null): boolean {
  return Boolean(request && props.toolCallId === request.toolCallId)
}

export function ConnectorTool(props: ToolCallMessagePartProps) {
  const view = useSessionView()
  const runtimeId = useStore(view.$runtimeId)
  const storedId = useStore(view.$storedId)
  const $request = useMemo(() => sessionConnectionRequest(runtimeId), [runtimeId])
  const request = useStore($request)
  const targetNames = requestedConnectorNames(props.args)

  const untargetedStatus =
    props.toolName === 'manage_connections' &&
    (recordOf(props.args).action ?? 'status') === 'status' &&
    targetNames.length === 0

  const live = !untargetedStatus && connectionRequestOwnsPart(props, request)
  // Owner routes and hints are keyed by the stored id, not the runtime id the events carry.
  const ownerSessionId = storedId
  const [owner, setOwner] = useState<ConnectorOwner | null>(null)

  useEffect(() => {
    if (!ownerSessionId || !live) {
      setOwner(null)

      return
    }

    let cancelled = false
    const ambientProfile = $activeGatewayProfile.get()

    void resolveSessionOwner(ownerSessionId)
      .then(scope => {
        assertSessionOwnerResolved(scope, { method: 'connectors.connect', sessionId: ownerSessionId })

        if (!cancelled) {
          setOwner({
            connectionId: isSessionOwnerRoute(scope) ? scope.connectionId : null,
            profile: isSessionOwnerRoute(scope) ? scope.profile : scope || ambientProfile
          })
        }
      })
      .catch(() => {
        if (!cancelled) {
          setOwner(null)
        }
      })

    return () => {
      cancelled = true
    }
  }, [live, ownerSessionId])

  if (!live || !request) {
    return <ToolFallback {...props} />
  }

  return owner ? <ConnectorOffer owner={owner} request={request} /> : null
}

type ConnectorCopy = ReturnType<typeof useI18n>['t']['connectors']
type ConnectorAction = 'none' | 'open' | 'reissue'

interface ConnectorCardPhase {
  action: ConnectorAction
  cardState: ConnectorCardState
  dismissed: boolean
  outcome: (target: ConnectionTarget, copy: ConnectorCopy) => ConnectorCardOutcome | undefined
  phase: (copy: ConnectorCopy) => string | undefined
  requiresUrl: boolean
  unresolved: boolean
}

const noOutcome = (): undefined => undefined
const noPhase = (): undefined => undefined

const errorOutcome = (target: ConnectionTarget, copy: ConnectorCopy): ConnectorCardOutcome => ({
  detail: target.detail || copy.failed,
  status: 'error'
})

const connectedOutcome = (target: ConnectionTarget): ConnectorCardOutcome => ({
  status: 'connected',
  tools: target.tools
})

const CONNECTOR_CARD_PHASES = {
  connected: {
    action: 'none',
    cardState: 'connected',
    dismissed: false,
    outcome: connectedOutcome,
    phase: noPhase,
    requiresUrl: false,
    unresolved: false
  },
  expired: {
    action: 'reissue',
    cardState: 'needs_auth',
    dismissed: false,
    outcome: errorOutcome,
    phase: noPhase,
    requiresUrl: false,
    unresolved: true
  },
  failed: {
    action: 'reissue',
    cardState: 'not_configured',
    dismissed: false,
    outcome: errorOutcome,
    phase: noPhase,
    requiresUrl: false,
    unresolved: true
  },
  initiated: {
    action: 'open',
    cardState: 'not_configured',
    dismissed: false,
    outcome: noOutcome,
    phase: copy => copy.waiting,
    requiresUrl: true,
    unresolved: true
  },
  not_connected: {
    action: 'none',
    cardState: 'not_configured',
    dismissed: false,
    outcome: (target, copy) => ({ detail: target.detail || copy.notConnected, status: 'error' }),
    phase: noPhase,
    requiresUrl: false,
    unresolved: true
  },
  pending: {
    action: 'open',
    cardState: 'not_configured',
    dismissed: false,
    outcome: noOutcome,
    phase: noPhase,
    requiresUrl: true,
    unresolved: true
  },
  skipped: {
    action: 'none',
    cardState: 'not_configured',
    dismissed: true,
    outcome: noOutcome,
    phase: noPhase,
    requiresUrl: false,
    unresolved: false
  },
  unavailable: {
    action: 'none',
    cardState: 'disabled',
    dismissed: false,
    outcome: errorOutcome,
    phase: noPhase,
    requiresUrl: false,
    unresolved: true
  }
} satisfies Record<ConnectionTargetState, ConnectorCardPhase>

interface ConnectorOfferProps {
  owner: ConnectorOwner
  request: ConnectionRequest
}

function connectorCardCopy(copy: ConnectorCopy): ConnectorCardCopy {
  return {
    connectAction: copy.connect,
    connectTitle: copy.connectTitle,
    decline: copy.skip,
    envRequired: '',
    grantAction: copy.grant,
    retryAction: copy.retry,
    stateConnected: copy.connected,
    stateDeclined: copy.skipped,
    stateDisabled: copy.disabled,
    stateFailed: copy.failed,
    stateNeedsAuth: copy.needsAuth,
    toolCount: count => String(count),
    trustCommunity: '',
    trustCommunityTip: () => '',
    trustVerified: () => '',
    trustVerifiedTip: () => ''
  }
}

export function ConnectorOffer({ owner, request }: ConnectorOfferProps) {
  const { t } = useI18n()
  const copy = t.connectors
  const cardCopy = connectorCardCopy(copy)
  const [reissuing, setReissuing] = useState<ReadonlySet<string>>(new Set())
  const unresolved = request.targets.some(target => CONNECTOR_CARD_PHASES[target.state].unresolved)

  const reissue = async (name: string): Promise<void> => {
    setReissuing(current => new Set(current).add(name))

    try {
      await requestGatewayForAgent(
        owner.connectionId,
        owner.profile,
        'connectors.connect',
        {
          connectors: [name],
          reconnect: true,
          session_id: request.sessionId
        },
        45000
      )
    } finally {
      setReissuing(current => {
        const next = new Set(current)
        next.delete(name)

        return next
      })
    }
  }

  // A settled operation is a static per-target summary: no controls, no polling, nothing live.
  if (request.settled) {
    return (
      <div className="my-2 grid min-w-0 max-w-lg gap-1" data-connector-offer>
        {request.targets.map(target => {
          const phase = CONNECTOR_CARD_PHASES[target.state]
          const outcome = phase.outcome(target, copy) ?? { status: 'declined' as const }

          return (
            <ConnectorSummary
              connector={{ name: target.name, title: connectorTitle(target.name) }}
              key={target.name}
              meta={outcomeMeta(outcome, cardCopy)}
              tone={outcome.status === 'connected' ? 'ok' : outcome.status === 'error' ? 'error' : undefined}
            />
          )
        })}
      </div>
    )
  }

  return (
    <div className="my-2 grid min-w-0 max-w-lg gap-1" data-connector-offer>
      {request.targets.map(target => {
        const phase = CONNECTOR_CARD_PHASES[target.state]
        const title = connectorTitle(target.name)
        const waitingForReissue = reissuing.has(target.name)

        return (
          <ConnectorCard
            actionDisabled={phase.action === 'none' || waitingForReissue || (phase.requiresUrl && target.connectUrl === null)}
            busy={waitingForReissue}
            collapseWhenSettled={false}
            connector={{
              description: copy.describe(title),
              name: target.name,
              title
            }}
            copy={cardCopy}
            dismissed={phase.dismissed}
            key={target.name}
            onConnect={() => {
              if (phase.action === 'open' && target.connectUrl && window.hermesDesktop?.openExternal) {
                void window.hermesDesktop.openExternal(target.connectUrl)
              }

              if (phase.action === 'reissue') {
                void reissue(target.name)
              }
            }}
            onDismiss={() => void skipConnectionTarget(request, target.name)}
            otherBusy={reissuing.size > 0 && !waitingForReissue}
            outcome={phase.outcome(target, copy)}
            phase={phase.phase(copy)}
            state={phase.cardState}
            variant="avatar"
          />
        )
      })}
      {unresolved ? (
        <div className="px-3.5">
          <Button onClick={() => void continueConnectionRequest(request)} size="xs" variant="textStrong">
            {t.common.continue}
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/** Keep execution output in the standard disclosure, with one row per app call. */
export function ConnectorExecution(props: ToolCallMessagePartProps) {
  const view = useSessionView()
  const sessionId = useStore(view.$runtimeId)
  const $request = useMemo(() => sessionConnectionRequest(sessionId), [sessionId])
  const request = useStore($request)
  const calls = connectorCalls(props.toolName, props.args)
  const input = recordOf(props.args)
  const batch = Array.isArray(input.calls) ? input.calls : [input]

  // Mixed remote batches keep their complete disclosure and original result order.
  if (props.toolName === 'tool_call' && calls.length !== batch.length) {
    return <ToolFallback {...props} />
  }

  const output = recordOf(props.result)
  const results = Array.isArray(output.results) ? output.results : []

  const repair = calls
    .filter((_call, index) => {
      const item = recordOf(props.toolName === 'tool_call' ? results[index] : props.result)

      return recordOf(item.error).connect_card_available === true
    })
    .map(call => {
      // SAFETY: connectorCalls includes only names accepted by connectorToolName.
      return connectorToolName(call.name)!.connector
    })

  const openRepair =
    request &&
    !request.settled &&
    matchingTargetNames(
      repair,
      request.targets.map(target => target.name)
    )

  return (
    <>
      {calls.map((call, index) => {
        const item =
          props.toolName === 'tool_call' ? (results[index] ?? (output.error ? output : undefined)) : props.result

        const result = recordOf(item)
        // SAFETY: connectorCalls includes only names accepted by connectorToolName.
        const identity = connectorToolName(call.name)!

        return (
          <ToolFallback
            {...props}
            args={recordOf(call.arguments)}
            isError={Boolean(result.error) || props.isError === true}
            key={`${props.toolCallId}:${index}`}
            result={props.result === undefined ? undefined : (item ?? { error: 'Missing connector result' })}
            toolCallId={`${props.toolCallId}:${index}`}
            toolName={`${connectorTitle(identity.connector)}: ${identity.action}`}
          />
        )
      })}
      {openRepair ? (
        <ConnectorTool {...props} args={{ action: 'status', connectors: repair }} result={undefined} />
      ) : null}
    </>
  )
}
