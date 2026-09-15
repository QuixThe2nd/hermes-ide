import { SCAFFOLD_META_CLASS, ScaffoldRow } from '@/components/chat/scaffold-row'
import { WIDGET_SHELL_CLASS } from '@/components/chat/widget-shell'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ConnectorLogo, type ConnectorLogoSubject } from '@/components/ui/connector-logo'
import { Input } from '@/components/ui/input'
import { Tip } from '@/components/ui/tooltip'
import { MarkdownLinkText } from '@/lib/external-link'
import { CheckCircle2 } from '@/lib/icons'
import { cn } from '@/lib/utils'

/** Presentation leaf: callers own connector semantics and localized copy. */
export type ConnectorCardState = 'connected' | 'disabled' | 'needs_auth' | 'not_configured'

export type ConnectorCardTrust = 'catalog' | 'community' | 'verified'

export interface ConnectorCardField {
  name: string
  prompt?: string
  required?: boolean
}

/** Structural to accept callers' richer subject types without importing them. */
export interface ConnectorCardSubject extends ConnectorLogoSubject {
  description?: string
  publisher?: string
  requiredEnv?: ConnectorCardField[]
  setup?: string[]
  title: string
  trust?: ConnectorCardTrust
}

export interface ConnectorCardOutcome {
  detail?: string
  /** An access refusal; offer authorization instead of retry. */
  needsAuth?: boolean
  status: 'connected' | 'declined' | 'error'
  tools?: unknown[]
}

/** Passed in so this presentation leaf has no i18n dependency. */
export interface ConnectorCardCopy {
  connectAction: string
  connectTitle?: (title: string) => string
  decline: string
  envRequired: string
  grantAction: string
  retryAction: string
  stateConnected: string
  stateDeclined: string
  stateDisabled: string
  stateFailed: string
  stateNeedsAuth: string
  toolCount: (count: number) => string
  trustCommunity: string
  trustCommunityTip: (host: string) => string
  trustVerified: (publisher: string) => string
  trustVerifiedTip: (publisher: string) => string
}

export interface ConnectorCardSource {
  text: string
}

const SHELL_CLASS = `${WIDGET_SHELL_CLASS} text-[length:var(--conversation-text-font-size)] text-(--ui-text-primary)`

const isMac = typeof navigator !== 'undefined' && /Mac|iP(hone|ad|od)/.test(navigator.platform)

const hostOf = (url: null | string | undefined): string => {
  if (!url) {
    return ''
  }

  try {
    return new URL(url).hostname
  } catch {
    return url
  }
}

export function outcomeMeta(outcome: ConnectorCardOutcome, copy: ConnectorCardCopy): string {
  if (outcome.status === 'connected') {
    const toolCount = Array.isArray(outcome.tools) ? outcome.tools.length : 0

    return toolCount > 0 ? `${copy.stateConnected} · ${copy.toolCount(toolCount)}` : copy.stateConnected
  }

  if (outcome.status === 'error') {
    return outcome.detail || copy.stateFailed
  }

  return copy.stateDeclined
}

export function ConnectorSummary({
  connector,
  meta,
  tone
}: {
  connector: ConnectorLogoSubject
  meta?: string
  tone?: 'error' | 'ok'
}) {
  // Apply opacity to rows, not a shared container: it would create a stacking context for every sibling.
  return (
    <div data-conversation-scaffold="" data-slot="connector-card">
      <ScaffoldRow>
        <ConnectorLogo className="size-4 rounded-[0.25rem]" connector={connector} />
        <span className="truncate text-[length:var(--conversation-tool-font-size)] text-(--ui-text-primary)">
          {connector.title || connector.name}
        </span>
        {meta ? (
          <span
            className={cn(
              SCAFFOLD_META_CLASS,
              tone === 'error' && 'text-destructive',
              tone === 'ok' && 'text-emerald-600/85 dark:text-emerald-400/85'
            )}
          >
            {meta}
          </span>
        ) : null}
      </ScaffoldRow>
    </div>
  )
}

// Catalog entries are ordinary; reserve badges for verified and community exceptions.
function TrustBadge({ connector, copy }: { connector: ConnectorCardSubject; copy: ConnectorCardCopy }) {
  if (!connector.trust || connector.trust === 'catalog') {
    return null
  }

  if (connector.trust === 'verified') {
    if (!connector.publisher) {
      return null
    }

    return (
      <Tip label={copy.trustVerifiedTip(connector.publisher)}>
        <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{copy.trustVerified(connector.publisher)}</span>
      </Tip>
    )
  }

  return (
    <Tip label={copy.trustCommunityTip(hostOf(connector.url))}>
      <span className="inline-flex items-center gap-1 text-[0.6875rem] text-amber-500">
        <Codicon name="warning" size="0.6875rem" />
        {copy.trustCommunity}
      </span>
    </Tip>
  )
}

export interface ConnectorCardProps {
  /** Only callers that handle these keys may show the accelerator hint. */
  accelerators?: boolean
  collapseWhenSettled?: boolean
  connector: ConnectorCardSubject
  copy: ConnectorCardCopy
  dismissed?: boolean
  envDraft?: Record<string, string>
  /** The caller reveals fields after a refused credential. */
  envOpen?: boolean
  onConnect: () => void
  onDismiss: () => void
  onEnvChange?: (key: string, value: string) => void
  /** Prevent concurrent sign-in tabs; decline remains enabled. */
  otherBusy?: boolean
  actionDisabled?: boolean
  outcome?: ConnectorCardOutcome
  /** The action itself is running (spinner, button held). Independent of `phase`: a row can show
   *  "Finish connecting in your browser" while Connect stays clickable to reopen the link. */
  busy?: boolean
  phase?: string
  source?: ConnectorCardSource
  state: ConnectorCardState
  variant?: 'avatar' | 'compact'
}

export function ConnectorCard({
  accelerators = false,
  collapseWhenSettled = true,
  connector,
  copy,
  dismissed = false,
  envDraft = {},
  envOpen = false,
  onConnect,
  onDismiss,
  onEnvChange,
  otherBusy = false,
  actionDisabled = false,
  busy = false,
  outcome,
  phase,
  source,
  state,
  variant = 'compact'
}: ConnectorCardProps) {
  const working = phase !== undefined
  const connected = outcome?.status === 'connected'
  const failed = outcome?.status === 'error'

  if ((connected || dismissed) && collapseWhenSettled) {
    return (
      <ConnectorSummary
        connector={connector}
        meta={outcomeMeta(outcome ?? { status: 'declined' }, copy)}
        tone={connected ? 'ok' : undefined}
      />
    )
  }

  const settled = connected || dismissed

  const stateLabel = state === 'disabled' ? copy.stateDisabled : state === 'needs_auth' ? copy.stateNeedsAuth : null

  // Keep setup and credentials visible after failure so they can be corrected.
  const fixable = state === 'not_configured' || failed
  const envFields = fixable ? (connector.requiredEnv ?? []) : []
  const steps = fixable ? (connector.setup ?? []) : []

  const avatar = variant === 'avatar'

  return (
    <div className={cn(SHELL_CLASS, 'my-1.5 grid gap-1.5')} data-slot="connector-card">
      <div className={cn('flex items-start', avatar ? 'gap-3' : 'gap-2')}>
        {avatar ? <ConnectorLogo className="size-10 rounded-xl text-base" connector={connector} /> : null}
        <div className="grid min-w-0 flex-1 gap-0.5">
          <div className="flex flex-wrap items-baseline gap-x-1.5">
            <span className="font-medium leading-(--conversation-line-height)">
              {copy.connectTitle ? copy.connectTitle(connector.title) : connector.title}
            </span>
            {working ? (
              <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{phase}</span>
            ) : (
              stateLabel && <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{stateLabel}</span>
            )}
            <TrustBadge connector={connector} copy={copy} />
          </div>

          {connector.description ? <p className="text-(--ui-text-secondary)">{connector.description}</p> : null}

          {source ? <p className="truncate text-[0.6875rem] text-(--ui-text-tertiary)">{source.text}</p> : null}

          {failed && outcome.detail ? <p className="text-[0.6875rem] text-destructive">{outcome.detail}</p> : null}

          {steps.length > 0 && (
            <ol className="mt-1.5 grid gap-1" data-slot="connector-card-steps">
              {steps.map((step, index) => (
                <li className="flex gap-1.5 text-[0.6875rem] text-(--ui-text-secondary)" key={step}>
                  <span className="tabular-nums text-(--ui-text-tertiary)">{index + 1}.</span>
                  <MarkdownLinkText text={step} />
                </li>
              ))}
            </ol>
          )}

          {envOpen && envFields.length > 0 && (
            <div className="mt-1 grid gap-2" data-slot="connector-card-env">
              <p className="text-[0.6875rem] text-(--ui-text-tertiary)">{copy.envRequired}</p>
              {envFields.map(env => (
                <label className="grid gap-1" key={env.name}>
                  <span className="text-[0.6875rem] text-(--ui-text-secondary)">
                    {env.prompt || env.name}
                    {env.required ? ' *' : ''}
                  </span>
                  <Input
                    className="h-7 text-xs"
                    onChange={event => onEnvChange?.(env.name, event.currentTarget.value)}
                    type="password"
                    value={envDraft[env.name] ?? ''}
                  />
                </label>
              ))}
            </div>
          )}
        </div>
        {avatar ? null : (
          <ConnectorLogo className="mt-px size-5 rounded-[0.3rem] text-[0.6875rem]" connector={connector} />
        )}
      </div>

      <div className={cn('flex items-center gap-2.5', avatar && 'pl-13')}>
        {settled ? (
          <div
            className={cn(
              'inline-flex h-6 items-stretch overflow-hidden rounded-md border',
              connected
                ? 'border-emerald-600/25 bg-emerald-600/10 text-emerald-700 dark:border-emerald-400/25 dark:bg-emerald-400/10 dark:text-emerald-300'
                : 'border-(--ui-stroke-tertiary) bg-(--ui-bg-quaternary) text-(--ui-text-tertiary)'
            )}
            data-slot="connector-card-verdict"
          >
            <span className="inline-flex h-full items-center gap-1 px-2 text-xs font-medium">
              {connected ? <CheckCircle2 aria-hidden className="size-3" /> : null}
              {outcomeMeta(outcome ?? { status: 'declined' }, copy)}
            </span>
          </div>
        ) : (
          <>
            <div className="inline-flex h-6 items-stretch overflow-hidden rounded-md border border-primary/25 bg-primary/10 text-primary">
              <Button
                className="h-full gap-1 rounded-none px-2 text-xs font-medium text-primary hover:bg-primary/15 hover:text-primary"
                disabled={otherBusy || actionDisabled}
                loading={busy}
                onClick={onConnect}
                size="xs"
                variant="ghost"
              >
                {outcome?.needsAuth ? copy.grantAction : failed ? copy.retryAction : copy.connectAction}
                {accelerators ? (
                  <span className="text-[0.625rem] text-primary/60">{isMac ? '⌘⏎' : 'Ctrl⏎'}</span>
                ) : null}
              </Button>
            </div>
            {/* Never disable: this exits a stuck sign-in tab or hung install. */}
            <Button
              className="h-6 gap-1.5 rounded-md px-1.5 text-xs font-normal text-(--ui-text-tertiary) hover:text-foreground"
              onClick={onDismiss}
              size="xs"
              variant="ghost"
            >
              {copy.decline}
              {accelerators ? <span className="text-[0.625rem] opacity-55">Esc</span> : null}
            </Button>
          </>
        )}
      </div>
    </div>
  )
}
