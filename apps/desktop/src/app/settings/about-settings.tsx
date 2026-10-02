import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { Button } from '@/components/ui/button'
import { UpdateStatusCard, VersionHero } from '@/components/update-status'
import { VersionDetails } from '@/components/version-details'
import { useI18n } from '@/i18n'
import { AlertTriangle, RefreshCw } from '@/lib/icons'
import { $connection } from '@/store/session'
import { $desktopVersion, checkBackendUpdates, refreshDesktopVersion } from '@/store/updates'

import { SectionHeading, SettingsContent } from './primitives'
import { UninstallSection } from './uninstall-section'

export function AboutSettings() {
  const { t } = useI18n()
  const u = t.updates
  const a = t.settings.about
  const version = useStore($desktopVersion)
  const connection = useStore($connection)
  const remote = connection?.mode === 'remote'

  // The version atom is loaded once at app boot, which makes About show a
  // stale number after a self-update (the running binary is current, the
  // displayed string is not). Re-read on mount so opening About always
  // reflects the running build. In remote mode also seed the backend update
  // state so the backend card opens answered instead of on "never checked".
  useEffect(() => {
    void refreshDesktopVersion()

    if (remote) {
      void checkBackendUpdates()
    }
  }, [remote])

  return (
    <SettingsContent>
      <VersionHero version={version} />
      {version?.bundleSwapPending && (
        <div className="mx-auto w-full max-w-2xl rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-left text-sm">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
            <div className="min-w-0">
              {/* The updated app is already on disk — the updater swapped it
                  under this running process — so a restart loads it. Saying
                  "App build out of date" here would repeat the contradiction
                  this banner is meant to resolve: the Updates card below
                  already reports the runtime as current. Upstream's VersionHero
                  owns the bundleOutOfSync case, so only the fork's
                  bundleSwapPending branch is rendered here. */}
              <p className="font-medium">{a.bundleSwapPending}</p>
              <p className="mt-1 text-xs text-muted-foreground">{a.bundleSwapPendingDesc}</p>
              <Button
                className="mt-2"
                onClick={() => void window.hermesDesktop?.relaunchApp?.()}
                size="sm"
                variant="textStrong"
              >
                <RefreshCw className="size-3" />
                {a.bundleSwapPendingAction}
              </Button>
            </div>
          </div>
        </div>
      )}

      <div className="mx-auto mt-4 w-full max-w-2xl">
        <SectionHeading icon={RefreshCw} title={u.updatesSection} />

        <div className="grid gap-3">
          <UpdateStatusCard target="client" />
          {/* The desktop client and a remote backend update independently — in
              remote mode the statusbar shows both pills, so About shows both
              states too. The backend has no GitHub release notes link of its
              own; the client card already carries it. */}
          {remote && <UpdateStatusCard showReleaseNotes={false} target="backend" />}
        </div>

        {version && <VersionDetails version={version} />}

        <UninstallSection />
      </div>
    </SettingsContent>
  )
}
