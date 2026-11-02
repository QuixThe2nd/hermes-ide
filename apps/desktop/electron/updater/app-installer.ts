// updater/app-installer.ts — the win32 out-of-store MSIX strategy.
//
// The OS App Installer owns the apply. The package was installed from an
// .appinstaller, which registered the feed URI as the package's update source;
// the OS checks it and swaps the package wholesale. The app's only jobs:
//   check()  ask the OS whether an update is available (via the bundled
//            payload python's winrt), surfacing UNKNOWN honestly;
//   apply()  stage the descriptor, tear down, open it, and quit with
//            a pending-relaunch marker so Hermes comes back by itself.
//
// Pure-injectable: the impure pieces (python runner, shell, quit, relaunch
// marker) are injected, so vitest covers the arm without a payload.

import {
  type AppInstallerCheck,
  parseCheckOutput,
  type PayloadPythonRunner,
  triggerAppInstallerUpdate,
  win32AppInstallerFeedPath
} from '../app-updater'

import type { UpdaterApplyResultWire, UpdaterStatusWire } from './index'

export interface AppInstallerStrategyDeps {
  /** Absolute path to the bundled payload python (tools/<entry>/python.exe). */
  python: string
  /** The checker script's absolute path (payload repo snapshot). */
  script: string
  run: PayloadPythonRunner['run']
  /** Channel + variant from the baked install stamp. */
  channel: 'stable' | 'canary'
  light: boolean
  /** The App Installer feed base URL; empty when nothing configured it. */
  feedBaseUrl: string
  installer: { prepare: (url: string) => Promise<string>; open: (file: string) => Promise<string> }
  /** Graceful backend teardown before the package swap. */
  teardownBundledBackend: () => void | Promise<void>
  /** Progress emitter for the updates overlay. */
  emitUpdateProgress: (payload: { stage: string; message: string; percent: number | null }) => void
  /** App version label for the status wire. */
  appVersion: string
  /** Quit the app (after handing the swap to the OS). */
  quit: () => void
  /**
   * Register a one-shot post-update relaunch before quitting, so Hermes
   * reopens on the new version with no user action. Resolves true only once
   * the relaunch mechanism has acknowledged (the waiter handshake) — the
   * caller must await this BEFORE quitting. Resolves false when the
   * mechanism could not be started (relaunch stays manual).
   */
  registerPendingRelaunch: (targetVersion: string) => Promise<boolean>
}

export interface CheckOutcome {
  status: UpdaterStatusWire
}

/**
 * Ask the OS whether an App Installer update is available. `available: null`
 * (checker unavailable) is surfaced as an honest unknown on the wire —
 * NEVER as "no update".
 */
export function appInstallerCheckToStatus(check: AppInstallerCheck, appVersion: string): UpdaterStatusWire {
  return {
    supported: true,
    mechanism: 'app-installer',
    currentVersion: appVersion,
    updateAvailable: check.available === true,
    // null = unknown (checker unavailable) — surface honestly, never "no update".
    error: check.available === null ? check.error || 'update check unavailable' : undefined,
    fetchedAt: Date.now()
  }
}

export class AppInstallerStrategy {
  readonly mechanism = 'app-installer' as const

  constructor(private readonly deps: AppInstallerStrategyDeps) {}

  async check(): Promise<UpdaterStatusWire> {
    const { code, stdout } = await this.deps.run(this.deps.python, this.deps.script)
    const check = parseCheckOutput(code, stdout)

    return appInstallerCheckToStatus(check, this.deps.appVersion)
  }

  async apply(_opts: { stopSafeBlockers?: boolean }): Promise<UpdaterApplyResultWire> {
    const feedBaseUrl = this.deps.feedBaseUrl
    let sourceUri: string | undefined

    if (!feedBaseUrl) {
      const { code, stdout } = await this.deps.run(this.deps.python, this.deps.script)
      sourceUri = parseCheckOutput(code, stdout).sourceUri
    }

    if (!feedBaseUrl && !sourceUri) {
      this.deps.emitUpdateProgress({
        stage: 'manual',
        message: 'bundled install: update by installing the new app release',
        percent: null
      })

      return { ok: true, manual: true, bundled: true, mechanism: this.mechanism }
    }

    this.deps.emitUpdateProgress({
      stage: 'restart',
      message: 'Applying the Hermes update — the window will close and the App Installer will finish.',
      percent: 100
    })

    await triggerAppInstallerUpdate(
      feedBaseUrl,
      this.deps.channel,
      this.deps.light,
      this.deps.installer,
      async () => {
        const registered = await this.deps.registerPendingRelaunch(this.deps.appVersion)

        if (!registered) {
          this.deps.emitUpdateProgress({
            stage: 'restart', percent: 100,
            message: 'Automatic relaunch could not be registered. Reopen Hermes after App Installer finishes.'
          })
        }

        await this.deps.teardownBundledBackend()
      },
      sourceUri
    )

    this.deps.quit()

    return { ok: true, manual: false, bundled: true, mechanism: this.mechanism }
  }
}

export { parseCheckOutput }

export { win32AppInstallerFeedPath }
export type { AppInstallerCheck, PayloadPythonRunner }
