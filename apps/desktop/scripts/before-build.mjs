/**
 * Desktop bundles ship precompiled renderer assets. Returning false here tells
 * electron-builder to skip the node_modules collector/install step, which
 * avoids workspace dependency graph explosions and keeps packaging
 * deterministic across environments. The Hermes Agent Python payload is no
 * longer bundled; the Electron app fetches it at first launch via
 * `install.ps1`'s stage protocol (Windows). See `electron/main.ts`.
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'

import { CLI_LAUNCHER_SPECS } from '../../../scripts/desktop-cli/cli-entrypoints.mjs'

const require = createRequire(import.meta.url)
const {
  light,
  displayName,
  appNamePascal
} = require('../product-identity.cjs')

export default async function beforeBuild() {
  return false
}

function stageMsixAssets() {
  const desktop = path.join(import.meta.dirname, '..')
  const sourceDir = path.join(desktop, 'assets', 'appx')
  const stageDir = path.join(desktop, 'build', 'appx')
  const names = [
    'Square44x44Logo.png',
    'Square150x150Logo.png',
    'StoreLogo.png',
    'Wide310x150Logo.png'
  ]

  fs.mkdirSync(stageDir, { recursive: true })
  for (const name of names) {
    const source = path.join(sourceDir, name)
    if (!fs.existsSync(source)) {
      throw new Error(`missing MSIX asset ${source}`)
    }
    fs.copyFileSync(source, path.join(stageDir, name))
  }
}

function writeMsixExtensions() {
  const desktop = path.join(import.meta.dirname, '..')
  const output = path.join('build', 'msix-extensions.xml')
  const file = path.join(desktop, output)
  // One uap5:Extension per payload CLI launcher exe (see
  // scripts/desktop-cli/cli-entrypoints.mjs): the minted exes are three
  // REAL executables now — the old rust shim's one-exe-argv[0]-dispatch is
  // gone — and an AppExecutionAlias's Executable must name the exact exe
  // that serves the alias, so each alias gets its own Extension block.
  const aliases = light ? '' : appExecutionAliasExtensions()
  // The uap3:AppExtension fragment that registers the app as a Windows
  // Copilot hardware key provider. The press activates hermes://copilot-key/start.
  //
  // Content rules (violations are an opaque makeappx 0x80080204):
  //   * xmlns:uap3 rides on the fragment root — the stock manifest template
  //     declares no uap3 prefix. A/B-verified fine.
  //   * children of uap3:Properties are UNPREFIXED (xs:any content, per
  //     Microsoft's copilot-key-state sample).
  const copilot = light
    ? ''
    : `<uap3:Extension
    xmlns:uap3="http://schemas.microsoft.com/appx/manifest/uap/windows10/3"
    Category="windows.appExtension">
  <uap3:AppExtension
      Name="com.microsoft.windows.copilotkeyprovider"
      Id="${appNamePascal}CopilotKeyProvider"
      DisplayName="${displayName}"
      Description="Launch ${displayName} with the Copilot key"
      PublicFolder="Public">
    <uap3:Properties>
      <SingleTap>hermes://copilot-key/start?state=Tap</SingleTap>
      <PressAndHoldStart>hermes://copilot-key/start?state=Down</PressAndHoldStart>
      <PressAndHoldStop>hermes://copilot-key/stop?state=Up</PressAndHoldStop>
    </uap3:Properties>
  </uap3:AppExtension>
</uap3:Extension>
${aliases}`

  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, copilot)
}

/**
 * One uap5:Extension block per payload CLI launcher, each naming its own
 * Executable (the distlib-minted launcher exes under bin/) and the alias
 * that exe serves. Exported pure for tests.
 * @param {{ name: string }[]} [launchers] exe stems under bin/
 */
export function appExecutionAliasExtensions(launchers = CLI_LAUNCHER_SPECS.map((s) => s.name)) {
  const bs = String.fromCharCode(92)
  const executable = (name) => ['app', 'resources', 'agent-payload', 'bin', `${name}.exe`].join(bs)
  return launchers
    .map(
      (name) => `<uap5:Extension
    xmlns:uap5="http://schemas.microsoft.com/appx/manifest/uap/windows10/5"
    Category="windows.appExecutionAlias"
    Executable="${executable(name)}"
    EntryPoint="Windows.FullTrustApplication">
  <uap5:AppExecutionAlias>
    <uap5:ExecutionAlias Alias="${name}.exe" />
  </uap5:AppExecutionAlias>
</uap5:Extension>`
    )
    .join('\n')
}
