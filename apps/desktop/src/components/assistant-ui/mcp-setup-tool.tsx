'use client'

import { type ToolCallMessagePartProps, useAuiState } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { capabilityScoped } from '@/api/client'
import { useSessionView } from '@/app/chat/session-view'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { WIDGET_SHELL_CLASS } from '@/components/chat/widget-shell'
import { ConnectorCard, type ConnectorCardCopy, ConnectorSummary } from '@/components/ui/connector-card'
import { getActionStatus, getMcpCatalog, installMcpCatalogEntry, type McpCatalogEntry, setMcpServerEnabled } from '@/hermes'
import { useI18n } from '@/i18n'
import { connectorText, mcpTargets } from '@/lib/connector-tools'
import { triggerHaptic } from '@/lib/haptics'
import { Loader2 } from '@/lib/icons'
import { isSubmitEnter } from '@/lib/ime'
import { completeMcpDesktopOAuth, McpOAuthCancelled } from '@/lib/mcp-dashboard-oauth'
import { prettyName } from '@/lib/text'
import { cn } from '@/lib/utils'
import {
  type ConnectionTargetOutcome,
  respondToConnectionRequest,
  sessionConnectionRequest
} from '@/store/connection-request'
import { $gateway } from '@/store/gateway'
import { notifyError } from '@/store/notifications'
import { invalidateMcpSuggestionIndex } from '@/store/suggestion-providers/mcp'

import { selectMessageRunning } from './tool/fallback-model'
import { parseMaybeObject } from './tool/fallback-model/format'

type SetupAction = 'authorize' | 'enable' | 'install'

interface SetupArgs {
  server: string
  action: SetupAction
}

const CATALOG_INSTALL_POLL_MS = 1500

// The declined response is already sent before this sentinel reaches the catch path.
const CANCELLED = Symbol('mcp-setup-cancelled')

function readSetupArgs(args: unknown): SetupArgs {
  const row = parseMaybeObject(args)
  const [target] = mcpTargets('manage_connections', row)

  return {
    action: target?.action ?? 'install',
    server: target?.name ?? ''
  }
}

interface SettledResult {
  status?: 'connected' | 'not_connected' | 'skipped' | 'unavailable'
  detail?: string
  server?: string
  tools?: string[]
}

function readSetupResult(result: unknown): SettledResult {
  const row = parseMaybeObject(result)
  const [target] = Array.isArray(row.targets) ? row.targets.map(parseMaybeObject) : []

  if (!target) {
    return {}
  }

  const STATES: readonly NonNullable<SettledResult['status']>[] = ['connected', 'not_connected', 'skipped', 'unavailable']

  return {
    detail: connectorText(target.detail),
    server: connectorText(target.name),
    status: STATES.find(state => state === target.state),
    tools: Array.isArray(target.tools) ? target.tools.map(connectorText).filter((t): t is string => t !== undefined) : undefined
  }
}

const SHELL_CLASS = `${WIDGET_SHELL_CLASS} text-[length:var(--conversation-text-font-size)] text-(--ui-text-primary)`

function cardCopy(
  copy: ReturnType<typeof useI18n>['t']['assistant']['mcpSetup'],
  action: SetupAction
): ConnectorCardCopy {
  return {
    connectAction:
      action === 'enable' ? copy.enableAction : action === 'authorize' ? copy.authorizeAction : copy.installAction,
    connectTitle:
      action === 'enable' ? copy.enableTitle : action === 'authorize' ? copy.authorizeTitle : copy.installTitle,
    decline: copy.decline,
    envRequired: copy.envRequired,
    grantAction: copy.authorizeAction,
    retryAction: copy.installAction,
    stateConnected: '',
    stateDeclined: copy.declined,
    stateDisabled: '',
    stateFailed: '',
    stateNeedsAuth: '',
    toolCount: copy.toolCount,
    trustCommunity: '',
    trustCommunityTip: () => '',
    trustVerified: () => '',
    trustVerifiedTip: () => ''
  }
}

export const McpSetupTool = (props: ToolCallMessagePartProps) => {
  if (props.result !== undefined) {
    return <McpSetupSettled {...props} />
  }

  return <McpSetupLive {...props} />
}

const McpSetupLive = (props: ToolCallMessagePartProps) => {
  const messageRunning = useAuiState(selectMessageRunning)

  if (!messageRunning) {
    return <ToolFallback {...props} />
  }

  return <McpSetupPending {...props} />
}

function McpSetupSettled({ args, result }: ToolCallMessagePartProps) {
  const { t } = useI18n()
  const copy = t.assistant.mcpSetup
  const fromArgs = useMemo(() => readSetupArgs(args), [args])
  const fromResult = useMemo(() => readSetupResult(result), [result])

  const server = fromResult.server || fromArgs.server
  const status = fromResult.status ?? 'not_connected'
  const displayName = prettyName(server)

  const connectedLine =
    fromArgs.action === 'enable'
      ? copy.enabled(displayName)
      : fromArgs.action === 'authorize'
        ? copy.authorized(displayName)
        : copy.installed(displayName)

  const line =
    status === 'connected'
      ? connectedLine
      : status === 'skipped'
        ? copy.declined
        : status === 'not_connected' && fromResult.detail === 'deadline'
          ? copy.unanswered
          : copy.failed(displayName)

  const ok = status === 'connected'
  const neutral = status === 'skipped' || (status === 'not_connected' && fromResult.detail === 'deadline')
  const toolCount = Array.isArray(fromResult.tools) ? fromResult.tools.length : 0

  return (
    <ConnectorSummary
      connector={{ name: server, title: displayName }}
      meta={
        ok && toolCount > 0
          ? `${line} · ${copy.toolCount(toolCount)}`
          : !ok && !neutral && fromResult.detail
            ? `${line} — ${fromResult.detail}`
            : line
      }
      tone={ok ? 'ok' : neutral ? undefined : 'error'}
    />
  )
}

function McpSetupPending({ args }: ToolCallMessagePartProps) {
  const { t } = useI18n()
  const copy = t.assistant.mcpSetup
  // Use the rendering transcript's session, not the globally active one.
  const sessionId = useStore(useSessionView().$runtimeId)
  const $request = useMemo(() => sessionConnectionRequest(sessionId), [sessionId])
  const request = useStore($request)
  const gateway = useStore($gateway)
  const fromArgs = useMemo(() => readSetupArgs(args), [args])

  const [requestTarget] = request?.targets ?? []
  const server = fromArgs.server || requestTarget?.name || ''
  const action: SetupAction = fromArgs.action ?? requestTarget?.action ?? 'install'

  const [working, setWorking] = useState(false)
  const [envDraft, setEnvDraft] = useState<Record<string, string>>({})
  const [entry, setEntry] = useState<McpCatalogEntry | null | undefined>(undefined)
  const [envOpen, setEnvOpen] = useState(false)
  const cancelRef = useRef(false)

  // `tool.start` arrives before `connection.request`.
  const ready = Boolean(request)

  const respond = useCallback(
    async (outcome: ConnectionTargetOutcome) => {
      if (!request) {
        return
      }

      if (!gateway) {
        notifyError(new Error(copy.gatewayDisconnected), copy.sendFailed)

        return
      }

      const success = outcome.status === 'connected'

      if (success) {
        invalidateMcpSuggestionIndex()
      }

      try {
        await respondToConnectionRequest(request, { targets: [outcome] })
      } catch (error) {
        notifyError(error, copy.sendFailed)
      }
    },
    [copy.gatewayDisconnected, copy.sendFailed, gateway, request]
  )

  const decline = useCallback(() => {
    // Respond first; cancelRef stops abandoned work at its next poll.
    cancelRef.current = true
    triggerHaptic('cancel')
    void respond({ name: server, status: 'skipped' })
  }, [respond, server])

  const approve = useCallback(async () => {
    cancelRef.current = false
    const oauthScope = capabilityScoped()
    setWorking(true)

    // OAuth owns its cancellation; polling needs an explicit boundary check.
    const throwIfCancelled = <T,>(value: T): T => {
      if (cancelRef.current) {
        throw CANCELLED
      }

      return value
    }

    try {
      if (action === 'enable') {
        await setMcpServerEnabled(server, true)
        triggerHaptic('submit')
        await respond({ name: server, status: 'connected' })

        return
      }

      if (action === 'authorize') {
        const flow = await completeMcpDesktopOAuth({
          serverName: server,
          profile: oauthScope,
          cancelled: () => cancelRef.current
        })

        triggerHaptic('submit')
        await respond({ name: server, status: 'connected', tools: (flow.tools ?? []).map(tool => tool.name) })

        return
      }

      let resolved = entry

      if (resolved === undefined) {
        const catalog = await getMcpCatalog()
        resolved = catalog.entries.find(candidate => candidate.name === server) ?? null
        setEntry(resolved)
      }

      if (!resolved) {
        await respond({ detail: copy.notInCatalog(server), name: server, status: 'failed' })

        return
      }

      const required = resolved.required_env.filter(env => env.required)

      if (required.some(env => !envDraft[env.name]?.trim())) {
        setEnvOpen(true)

        return
      }

      const res = await installMcpCatalogEntry(server, envDraft)

      // Poll background installs so non-zero exits cannot report false success.
      if (res.background && res.action) {
        for (;;) {
          const status = throwIfCancelled(await getActionStatus(res.action, 1))

          if (!status.running) {
            if (status.exit_code !== 0) {
              throw new Error(copy.failed(server))
            }

            break
          }

          await new Promise(resolve => setTimeout(resolve, CATALOG_INSTALL_POLL_MS))
        }
      }

      triggerHaptic('submit')
      await respond({ name: server, status: 'connected' })
    } catch (error) {
      // The declined response is already sent; do not report cancellation as failure.
      if (error === CANCELLED || error instanceof McpOAuthCancelled) {
        return
      }

      notifyError(error, copy.failed(server))
      await respond({
        detail: error instanceof Error ? error.message : String(error),
        name: server,
        status: 'failed'
      })
    } finally {
      setWorking(false)
    }
  }, [action, copy, entry, envDraft, respond, server])

  const displayName = prettyName(server)
  const card = cardCopy(copy, action)

  const sourceLine = action === 'install' ? (entry?.url ?? copy.catalogSource) : null

  // Do not capture shortcuts while a focusable control owns typed input.
  useEffect(() => {
    if (!ready) {
      return
    }

    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.defaultPrevented) {
        return
      }

      const active = document.activeElement as HTMLElement | null

      if (
        active &&
        (active.isContentEditable || active.matches('a[href], button, input, select, textarea, [role="button"]'))
      ) {
        return
      }

      if (isSubmitEnter(event) && (event.metaKey || event.ctrlKey)) {
        if (!working) {
          event.preventDefault()
          void approve()
        }
      } else if (event.key === 'Escape') {
        event.preventDefault()
        decline()
      }
    }

    window.addEventListener('keydown', onKeyDown, true)

    return () => window.removeEventListener('keydown', onKeyDown, true)
  }, [approve, decline, ready, working])

  if (!ready) {
    return (
      <div className={cn(SHELL_CLASS, 'my-1.5 flex items-center gap-2')} data-slot="connector-card">
        <Loader2 aria-hidden className="size-4 animate-spin text-(--ui-text-tertiary)" />
        <span className="text-(--ui-text-tertiary)">{card.connectTitle?.(displayName)}</span>
      </div>
    )
  }

  return (
    <ConnectorCard
      accelerators
      busy={working}
      connector={{
        name: server,
        requiredEnv: entry?.required_env,
        title: displayName
      }}
      copy={{ ...card, decline: working ? t.common.cancel : card.decline }}
      envDraft={envDraft}
      envOpen={envOpen && !!entry && entry.required_env.length > 0}
      onConnect={() => void approve()}
      onDismiss={decline}
      onEnvChange={(key, value) => setEnvDraft(prev => ({ ...prev, [key]: value }))}
      phase={working ? '' : undefined}
      source={sourceLine ? { text: sourceLine } : undefined}
      state="not_configured"
      variant="avatar"
    />
  )
}
