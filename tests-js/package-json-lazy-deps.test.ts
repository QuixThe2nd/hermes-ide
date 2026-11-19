/**
 * Invariants for what is eager vs lazy in the root ``package.json``.
 *
 * The root ``package.json`` is installed by ``hermes update`` on every user,
 * including users who never opted into a given browser backend. Anything
 * listed in ``dependencies`` therefore runs its npm postinstall script for
 * everyone, and — per #43564 — is also part of the npm workspace install
 * graph, where a workspace-scoped ``npm ci`` (``--workspace ui-tui
 * --workspace web``) can silently prune it right back out on the next
 * ``hermes update``.
 *
 * The contract:
 *
 *
 * - ``@askjo/camofox-browser`` is NOT eager. It is installed on demand by
 *   ``tools_config.py`` ``post_setup_key == "camofox"`` during Browser
 *   Automation setup. Its postinstall fetches a ~300MB Firefox-fork binary,
 *   which silently blocked ``hermes update`` for multi-minute stretches on
 *   slow / network-restricted connections (notably users in China running
 *   through a VPN).
 *
 * If a future PR re-adds any of these to root ``dependencies``, this test
 * fails — read the lazy-install guidance in the ``hermes-agent-dev`` skill
 * before changing the expectations.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { test } from 'vitest'

const REPO_ROOT = path.resolve(__dirname, '..')
const ROOT_PKG = path.join(REPO_ROOT, 'package.json')
const ROOT_LOCK = path.join(REPO_ROOT, 'package-lock.json')

function rootPackageJson(): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(ROOT_PKG, 'utf-8'))
}

test('camofox is not in root dependencies (installed by browser setup)', () => {
  const deps = (rootPackageJson().dependencies ?? {}) as Record<string, string>
  assert.ok(
    !('@askjo/camofox-browser' in deps),
    'Camofox is a ~300MB binary-postinstall backend that must stay ' +
      'out of root package.json dependencies. It belongs in the ' +
      'Camofox post_setup handler in hermes_cli/tools_config.py so it ' +
      'installs during Browser Automation setup rather than on every update.'
  )
})


test('root lockfile has no camofox entries', () => {
  if (!fs.existsSync(ROOT_LOCK)) {
    // Some CI matrix shards skip lockfile materialization.
    return
  }

  const text = fs.readFileSync(ROOT_LOCK, 'utf-8')
  assert.ok(
    !text.includes('@askjo/camofox-browser'),
    'package-lock.json still references @askjo/camofox-browser. ' +
      'Regenerate the lockfile after removing the dep: ' +
      '`rm package-lock.json && npm install --package-lock-only ' +
      '--ignore-scripts --no-fund --no-audit`.'
  )
  assert.ok(
    !text.includes('camoufox-js'),
    'package-lock.json still references camoufox-js (transitive of ' +
      '@askjo/camofox-browser). Regenerate the lockfile.'
  )
})

