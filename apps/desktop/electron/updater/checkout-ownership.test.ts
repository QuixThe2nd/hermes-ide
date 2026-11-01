import { describe, expect, it, vi } from 'vitest'

import { type CheckoutStrategyDeps, createCheckoutStrategy } from './checkout'

function dependencies(): CheckoutStrategyDeps {
  return {
    hermesHome: 'home',
    isWindows: process.platform === 'win32',
    isMac: process.platform === 'darwin',
    defaultUpdateBranch: 'main',
    updateHandoffDwellMs: 0,
    directoryExists: () => true,
    readCanonicalInstallStamp: () => ({ updateMechanism: 'external' }),
    readDesktopUpdateConfig: () => ({ branch: 'main' }),
    resolveUpdateRoot: () => 'repo',
    resolveUpdaterBinary: () => null,
    resolveHealedBranch: async (_, branch) => branch,
    getOriginUrl: async () => '',
    runGit: vi.fn(async () => { throw new Error('unexpected git invocation') }),
    firstLine: text => text.split('\n')[0],
    readCommitLog: async () => [],
    fetchCompareBehindCount: async () => null,
    pathWithVenvBin: () => '',
    venvHermesShimPath: () => '',
    emitUpdateProgress: vi.fn(),
    rememberLog: vi.fn(),
    startHermes: vi.fn(async () => {}),
    startGatewaysAfterUpdateAbort: vi.fn(),
    releaseBackendLockForUpdate: vi.fn(async () => ({ unlocked: true })),
    repairMacUpdaterHelper: vi.fn(),
    preflightStateDb: vi.fn(),
    runningAppBundle: () => null,
    markQuittingForHandoff: vi.fn(),
    quit: vi.fn()
  }
}

describe('checkout update admission', () => {
  it('refuses steward-owned code without fetching or stopping the backend', async () => {
    const deps = dependencies()
    const strategy = createCheckoutStrategy(deps)
    const result = await strategy.check()

    expect(result.supported).toBe(false)
    expect(result.mechanism).toBe(strategy.mechanism)
    expect(deps.runGit).not.toHaveBeenCalled()
    expect(deps.releaseBackendLockForUpdate).not.toHaveBeenCalled()
    expect(deps.quit).not.toHaveBeenCalled()
  })

  it('rejects a missing source checkout without attempting git', async () => {
    const deps = dependencies()
    deps.directoryExists = () => false
    const result = await createCheckoutStrategy(deps).check()

    expect(result.reason).toBe('not-a-git-checkout')
    expect(deps.runGit).not.toHaveBeenCalled()
  })
})
