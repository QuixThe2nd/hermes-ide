// msix-shared.mjs — the shared MSIX-distribution building blocks used by
// BOTH the out-of-store feed generator (apps/desktop/scripts/gen-appinstaller.mjs)
// and the release job that stages the feed (scripts/stage-msixbundle.mjs).
//
// The two call sites must agree on every name/URL that Windows keys on — the
// .appinstaller's MainPackage identity and the bundle URI — so the XML
// builder and the version/filename derivations live here, once.

import fs from 'node:fs'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

// The out-of-store MSIX publisher — the ATS signing cert subject, which is
// what Windows compares against the package manifest publisher at install.
// Mirrored from electron-builder.config.cjs so the .appinstaller and the
// manifest can never drift.
export const OUT_OF_STORE_PUBLISHER =
  'CN=Nous Research Inc., O=Nous Research Inc., L=Austin, S=Texas, C=US'

// Content-Type for MSIX / App Installer artifacts. Without the right MIME the
// browser cannot hand a clicked .appinstaller / .msixbundle to the OS App
// Installer (it would download as octet-stream instead). Everything else
// stays octet-stream (R2's default) unchanged. Keys match by filename suffix,
// case-insensitively.
const CONTENT_TYPES = {
  '.appinstaller': 'application/appinstaller',
  '.msixbundle': 'application/msixbundle',
  '.msix': 'application/msix'
}

/** The Content-Type to store for a staged release artifact, if any. */
export function contentTypeFor(filename) {
  const lower = String(filename).toLowerCase()
  for (const [suffix, mime] of Object.entries(CONTENT_TYPES)) {
    if (lower.endsWith(suffix)) return mime
  }
  return undefined
}

// ── makeappx / signtool resolution (SDK BuildTools nuget) ─────────────────
// electron-builder downloads Microsoft.Windows.SDK.BuildTools into its
// winCodeSign cache; the bundle jobs use the SAME pin so makeappx/signtool
// match the builder's. Shared by stage-msixbundle.mjs (out-of-store feed) and
// bundle-store-msixbundle.mjs (Store-submission bundle) — one resolver.
export function resolveWinSdkTools() {
  // electron-builder downloads its signing toolsets into the cache root
  // (ELECTRON_BUILDER_CACHE on CI, %LOCALAPPDATA%/electron-builder/Cache
  // by default) under `win-codesign@<ver>/` — there is NO `winCodeSign`
  // subdir. The Windows Kits bundle extracts to
  // win-codesign@<ver>/windows-kits-bundle-10_0_26100_0-<hash>/ with the
  // HOST tools (signtool.exe + makeappx.exe) in its x64/ subdir. Legacy
  // winCodeSign-2.6.0 used windows-10/<arch>/; the old nuget layout
  // bin/<ver>/x64/ is long gone.
  const roots = [
    process.env.ELECTRON_BUILDER_CACHE || '',
    path.join(process.env.LOCALAPPDATA || '', 'electron-builder', 'Cache'),
    path.join(process.env.LOCALAPPDATA || '', 'electron-builder', 'cache'),
    path.join(process.env.USERPROFILE || '', 'AppData', 'Local', 'electron-builder', 'Cache')
  ]
  for (const root of roots) {
    if (!root || !fs.existsSync(root)) continue
    for (const entry of fs.readdirSync(root)) {
      const dir = path.join(root, entry)
      if (!fs.statSync(dir).isDirectory()) continue
      // Modern electron-builder (win-codesign@1.x): the Windows Kits bundle
      // extracts to <cacheDir>/win-codesign@<ver>/windows-kits-bundle-10_0_26100_0-<hash>/,
      // with the HOST tools (signtool.exe + makeappx.exe) directly in the
      // x64/ subdir of the bundle folder — two levels under the cache root.
      // Legacy winCodeSign-2.6.0 used windows-10/<arch>/ under the toolset
      // dir; the old nuget layout bin/<ver>/x64/ is gone. Check every dir
      // two levels down that carries a makeappx.exe + signtool.exe.
      const toolDirs = []
      for (const sub of fs.readdirSync(dir)) {
        const subDir = path.join(dir, sub)
        if (!fs.statSync(subDir).isDirectory()) continue
        for (const arch of ['x64']) {
          const x64 = path.join(subDir, arch)
          if (fs.existsSync(path.join(x64, 'makeappx.exe')) && fs.existsSync(path.join(x64, 'signtool.exe'))) {
            toolDirs.push(x64)
          }
        }
        // Legacy: windows-10/x64 (winCodeSign-2.6.0)
        const win10 = path.join(subDir, 'windows-10', 'x64')
        if (fs.existsSync(path.join(win10, 'makeappx.exe')) && fs.existsSync(path.join(win10, 'signtool.exe'))) {
          toolDirs.push(win10)
        }
        // Legacy nuget: bin/<ver>/x64
        const binDir = path.join(subDir, 'bin')
        if (fs.existsSync(binDir)) {
          for (const bsub of fs.readdirSync(binDir)) {
            const x64 = path.join(binDir, bsub, 'x64')
            if (fs.existsSync(path.join(x64, 'makeappx.exe')) && fs.existsSync(path.join(x64, 'signtool.exe'))) {
              toolDirs.push(x64)
            }
          }
        }
      }
      // First root with a usable kit wins — the configured
      // ELECTRON_BUILDER_CACHE must beat any stray default cache.
      if (toolDirs.length > 0) {
        toolDirs.sort()
        return toolDirs[toolDirs.length - 1]
      }
    }
  }
  console.error('[resolveWinSdkTools] no makeappx/signtool found under electron-builder winCodeSign cache')
  process.exit(1)
}

function escapeAttr(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

/**
 * Build an .appinstaller document for a channel.
 *
 * @param {{
 *   baseUrl: string             // feed host root (no trailing slash)
 *   variantChannelPath: string  // e.g. "win32/", "win32/light/", "win32/nightly/"
 *   identityName: string        // package Identity Name (e.g. "NousResearch.HermesBundled")
 *   version: string             // 4-part MSIX version, e.g. "1.2.3.0"
 *   bundleFilename: string      // the universal .msixbundle filename in the feed dir
 * }} o
 * @returns {string} the .appinstaller XML
 */
export function buildAppInstaller(o) {
  const bundleUrl = `${o.baseUrl}/${o.variantChannelPath.replace(/\/+$/, '')}/${o.bundleFilename}`
  const appinstallerUri = bundleUrl.replace(/\.msixbundle$/, '.appinstaller')

  return [
    '<?xml version="1.0" encoding="utf-8"?>',
    '<AppInstaller',
    `  Uri="${escapeAttr(appinstallerUri)}"`,
    `  Version="${escapeAttr(o.version)}"`,
    '  xmlns="http://schemas.microsoft.com/appx/appinstaller/2017/2">',
    '  <MainPackage',
    `    Name="${escapeAttr(o.identityName)}"`,
    `    Publisher="${escapeAttr(OUT_OF_STORE_PUBLISHER)}"`,
    `    Version="${escapeAttr(o.version)}"`,
    `    Uri="${escapeAttr(bundleUrl)}" />`,
    '  <UpdateSettings>',
    '    <OnLaunch HoursBetweenUpdateChecks="12" ShowPrompt="false" />',
    '  </UpdateSettings>',
    '</AppInstaller>',
    ''
  ].join('\n')
}

// The nightly tag base + embedded UTC stamp: v0.27.2-nightly.20260829034013
// (8- or 14-digit; the shorter legacy form is midnight of that day). The
// base is the next PATCH over the newest stable, so the first three MSIX
// components come from it (0.27.2) and outversion the stable line
// structurally — cross-line monotonicity is free.
const NIGHTLY_TAG_RE = /^v(\d+\.\d+\.\d+)-nightly\.(20\d{6}(?:\d{6})?)$/
const STABLE_TAG_RE = /^v\d+\.\d+\.\d+$/

// MSIX version components are 16-bit (makeappx rejects >65535). Minutes
// since the last stable cross it at 45.5 days; a nightly cut more than 45
// days after its stable is a process failure worth surfacing loudly, not a
// number to clamp (a clamped number would break monotonicity).
const MAX_BUILD_MINUTES = 45 * 24 * 60

function stampToEpoch(stamp) {
  const parts = /^(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?$/.exec(stamp)
  if (!parts) return 0
  const [, y, mo, d, h, mi, s] = parts
  return Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h ?? 0), Number(mi ?? 0), Number(s ?? 0)) / 1000
}

export function listGitTags(gitRoot, pattern) {
  return execFileSync('git', ['tag', '--list', pattern, '--sort=-v:refname'], { cwd: gitRoot, encoding: 'utf8' })
    .split('\n').filter(Boolean)
}

export function gitTagCommitTime(gitRoot, tag) {
  return Number(execFileSync('git', ['log', '-1', '--format=%ct', tag], { cwd: gitRoot, encoding: 'utf8' }).trim())
}

export function nightlyBuildMinutesFor(tag, stableEpoch) {
  const m = NIGHTLY_TAG_RE.exec(String(tag || ''))
  if (!m) return null
  const minutes = Math.floor((stampToEpoch(m[2]) - stableEpoch) / 60)
  if (minutes < 0) return 0
  if (minutes > MAX_BUILD_MINUTES) {
    throw new Error(
      `nightly ${tag} is ${Math.floor(minutes / 1440)} days past its stable base — ` +
      `MSIX versions cap at 16 bits (45 days); cut a stable first`
    )
  }
  return minutes
}

export function nightlyBuildMinutes(tag, gitRoot) {
  const m = NIGHTLY_TAG_RE.exec(String(tag || ''))
  if (!m) return null
  const majorMinor = m[1].split('.').slice(0, 2).join('.')
  const stable = listGitTags(gitRoot, `v${majorMinor}.*`).find(t => STABLE_TAG_RE.test(t))
  if (!stable) return 0 // degenerate: no stable on this line; build number restarts
  return nightlyBuildMinutesFor(tag, gitTagCommitTime(gitRoot, stable))
}

/**
 * Resolve the app identity for a desktop build from the app dir: the product
 * identity + package version. Pure-ish (reads product-identity.cjs and
 * package.json from the app dir) so callers on any runner can derive the
 * exact feed filename/identity without duplicating the derivation.
 *
 * @param {string} desktopDir absolute apps/desktop path
 * @param {string} [tag] the release tag (defaults to HERMES_PAYLOAD_TAG)
 * @returns {{ identity: object, version: string, name: string, fileVersion: string }}
 */
export function appIdentity(desktopDir, tag = process.env.HERMES_PAYLOAD_TAG || '') {
  const identity = require(path.join(desktopDir, 'product-identity.cjs'))
  const pkg = JSON.parse(fs.readFileSync(path.join(desktopDir, 'package.json'), 'utf8'))
  const repoRoot = path.resolve(desktopDir, '..', '..')
  const nightly = NIGHTLY_TAG_RE.exec(String(tag))
  if (nightly) {
    // Manifest + feed version: tag base (0.27.2) + minutes-since-stable.
    // The artifact FILENAME carries electron-builder's appInfo.version — the
    // full nightly string (HermesBundled-0.27.2-nightly.X-win-x64.msix) — so
    // callers that look files up by name need that string separately.
    return {
      identity,
      version: `${nightly[1]}.${nightlyBuildMinutes(String(tag), repoRoot)}`,
      fileVersion: String(tag).slice(1),
      name: identity.appNamePascal,
    }
  }
  return {
    identity,
    version: `${pkg.version}.0`,
    fileVersion: pkg.version,
    name: identity.appNamePascal,
  }
}
