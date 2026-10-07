import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import {
  azureSigningConfigured,
  batchSignAppTree,
  chunk,
  customSign,
  getBinaries,
  resolveDotnetRuntimeDir,
  resolveSigntool,
  resolveTrustedSigningDlib,
  signingArch
} from './batch-sign-binaries.mjs'

const tmpDirs = []

afterEach(() => {
  for (const dir of tmpDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

function tmpTree() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'batch-sign-test-'))
  tmpDirs.push(dir)
  return dir
}

test('getBinaries collects .exe and .dll recursively, case-insensitive, sorted', () => {
  const root = tmpTree()
  fs.mkdirSync(path.join(root, 'resources', 'agent-payload', 'tools', 'python', 'bin'), { recursive: true })
  fs.mkdirSync(path.join(root, 'resources', 'agent-payload', 'tools', 'chromium-1', 'meep'), { recursive: true })
  fs.writeFileSync(path.join(root, 'Hermes.exe'), 'x')
  fs.writeFileSync(path.join(root, 'ffmpeg.dll'), 'x')
  fs.writeFileSync(path.join(root, 'resources', 'agent-payload', 'tools', 'python', 'bin', 'node.exe'), 'x')
  fs.writeFileSync(path.join(root, 'resources', 'agent-payload', 'tools', 'chromium-1', 'meep', 'chrome.dll'), 'x')
  fs.writeFileSync(path.join(root, 'resources', 'README.md'), 'x')

  const files = getBinaries(root).map(f => path.relative(root, f))

  assert.deepEqual(files, [
    path.join('Hermes.exe'),
    path.join('ffmpeg.dll'),
    path.join('resources', 'agent-payload', 'tools', 'chromium-1', 'meep', 'chrome.dll'),
    path.join('resources', 'agent-payload', 'tools', 'python', 'bin', 'node.exe')
  ])
})

test('getBinaries skips symlinks and honors the skip predicate (product exe)', () => {
  const root = tmpTree()
  fs.mkdirSync(path.join(root, 'tools'), { recursive: true })
  const target = path.join(root, 'tools', 'real.exe')
  fs.writeFileSync(target, 'x')
  fs.writeFileSync(path.join(root, 'Hermes.exe'), 'x')
  const link = path.join(root, 'tools', 'link.exe')
  try {
    fs.symlinkSync(target, link)
  } catch {
    // Windows without symlink privilege: the collection contract under test
    // is the skip predicate; symlink skipping is covered on the other OS.
  }

  const exe = path.join(root, 'Hermes.exe')
  const files = getBinaries(root, { skip: file => path.resolve(file) === exe })

  assert.equal(files.includes(exe), false)
  assert.equal(files.includes(target), true)
  if (fs.existsSync(link)) {
    assert.equal(files.includes(link), false)
  }
})

test('getBinaries tolerates a missing directory', () => {
  assert.deepEqual(getBinaries(path.join(tmpTree(), 'nope')), [])
})

test('chunk splits into ~100-file batches with no leftovers', () => {
  assert.deepEqual(chunk([], 100), [])
  const two50 = Array.from({ length: 250 }, (_, i) => `f${i}.exe`)
  const batches = chunk(two50)
  assert.equal(batches.length, 3)
  assert.deepEqual(batches.map(b => b.length), [100, 100, 50])
  assert.deepEqual(batches.flat(), two50)

  const exact = Array.from({ length: 200 }, (_, i) => `f${i}.exe`)
  assert.deepEqual(chunk(exact).map(b => b.length), [100, 100])
})

test('azureSigningConfigured requires endpoint, account, and profile together', () => {
  assert.equal(azureSigningConfigured({}), false)
  assert.equal(azureSigningConfigured({ AZURE_SIGN_ENDPOINT: 'https://cus.codesigning.azure.net' }), false)
  assert.equal(
    azureSigningConfigured({
      AZURE_SIGN_ENDPOINT: 'https://cus.codesigning.azure.net',
      AZURE_SIGN_ACCOUNT: 'codesign2'
    }),
    false
  )
  assert.equal(
    azureSigningConfigured({
      AZURE_SIGN_ENDPOINT: 'https://cus.codesigning.azure.net',
      AZURE_SIGN_ACCOUNT: 'codesign2',
      AZURE_SIGN_PROFILE: 'hermesagent'
    }),
    true
  )
})

test('customSign returns true for batch-covered payload files without touching Azure', async () => {
  let azureTouched = 0
  const result = await customSign(
    { path: 'C:/out/win-unpacked/resources/agent-payload/tools/node.exe' },
    { appInfo: { productFilename: 'Hermes' } },
    { azureSignFile: async () => { azureTouched += 1 } }
  )
  assert.equal(result, true)
  assert.equal(azureTouched, 0)
})

test('customSign skips Store- submission packages (Partner Center signs)', async () => {
  const result = await customSign(
    { path: 'C:/out/Store-HermesBundled-0.28.0-win-x64.msix' },
    { appInfo: { productFilename: 'Hermes' } },
    { signMsix: async () => { throw new Error('must not be called') } }
  )
  assert.equal(result, true)
})

test('customSign delegates the msix package and the product exe to the Azure signer', async () => {
  const delegated = []
  const deps = {
    signMsix: async configuration => delegated.push(['msix', configuration.path]),
    azureSignFile: async file => delegated.push(['exe', file])
  }
  const packager = { appInfo: { productFilename: 'Hermes' } }

  await customSign({ path: 'C:/out/HermesBundled-0.28.0-win-x64.msix' }, packager, deps)
  await customSign({ path: 'C:/out/win-unpacked/Hermes.exe' }, packager, deps)

  // The hook's contract is `true` (handled) even when it delegated —
  // electron-builder must not fall back to per-file default signing.
  assert.deepEqual(delegated, [
    ['msix', 'C:/out/HermesBundled-0.28.0-win-x64.msix'],
    ['exe', 'C:/out/win-unpacked/Hermes.exe']
  ])
  assert.equal(await customSign({ path: 'C:/out/win-unpacked/Hermes.exe' }, packager, deps), true)
})

test('customSign does not mistake a similarly-named payload exe for the product exe', async () => {
  const deps = {
    azureSignFile: async () => { throw new Error('must not be called') }
  }
  assert.equal(
    await customSign(
      { path: 'C:/out/win-unpacked/resources/Hermes-helper.exe' },
      { appInfo: { productFilename: 'Hermes' } },
      deps
    ),
    true
  )
})

test('batchSignAppTree is a no-op (skipped=true) without the Azure env, and signs via chunked argv-array invocations when set', () => {
  const root = tmpTree()
  fs.mkdirSync(path.join(root, 'tools'), { recursive: true })
  const exe = path.join(root, 'Hermes.exe')
  fs.writeFileSync(exe, 'x')
  fs.writeFileSync(path.join(root, 'tools', 'node.exe'), 'x')
  fs.writeFileSync(path.join(root, 'tools', 'ffmpeg.dll'), 'x')

  // Unsigned lane: loud no-op, nothing invoked.
  const skipped = batchSignAppTree(root, exe, { env: {} })
  assert.deepEqual(skipped, { signed: 0, chunks: 0, skipped: true })

  // Signed lane: product exe excluded, chunked execFileSync with argv arrays.
  const invocations = []
  const fakeExec = (tool, args) => {
    invocations.push({ tool, args })
    return Buffer.from('')
  }
  const result = batchSignAppTree(root, exe, {
    env: {
      AZURE_SIGN_ENDPOINT: 'https://cus.codesigning.azure.net',
      AZURE_SIGN_ACCOUNT: 'codesign2',
      AZURE_SIGN_PROFILE: 'hermesagent'
    },
    exec: fakeExec,
    mkdtemp: () => root,
    signtool: 'signtool.exe',
    dlib: 'azure.codesigning.dlib.dll'
  })

  assert.deepEqual(result, { signed: 2, chunks: 1, skipped: false })
  assert.equal(invocations.length, 1)
  assert.equal(invocations[0].tool, 'signtool.exe')
  assert.ok(Array.isArray(invocations[0].args), 'argv array, never a shell string')
  const args = invocations[0].args
  assert.ok(!args.some(arg => typeof arg === 'string' && arg.includes(' ')))
  assert.equal(args.includes(exe), false, 'product exe excluded — signed per-file after rcedit')
  assert.equal(args.includes(path.join(root, 'tools', 'node.exe')), true)
  assert.equal(args.includes(path.join(root, 'tools', 'ffmpeg.dll')), true)
  assert.equal(args[args.indexOf('/dlib') + 1], 'azure.codesigning.dlib.dll')
  assert.ok(args[args.indexOf('/dmdf') + 1].endsWith('batch-sign.json'))
  assert.equal(args[args.indexOf('/fd') + 1], 'SHA256')
  assert.equal(args[args.indexOf('/td') + 1], 'SHA256')
})

test('batchSignAppTree chunks large trees into ~100-file signtool invocations', () => {
  const root = tmpTree()
  fs.mkdirSync(path.join(root, 'tools'), { recursive: true })
  for (let i = 0; i < 250; i += 1) {
    fs.writeFileSync(path.join(root, 'tools', `bin${i}.exe`), 'x')
  }

  const invocations = []
  const result = batchSignAppTree(root, path.join(root, 'Hermes.exe'), {
    env: {
      AZURE_SIGN_ENDPOINT: 'https://cus.codesigning.azure.net',
      AZURE_SIGN_ACCOUNT: 'codesign2',
      AZURE_SIGN_PROFILE: 'hermesagent'
    },
    exec: (tool, args) => {
      invocations.push(args)
      return Buffer.from('')
    },
    mkdtemp: () => root,
    signtool: 'signtool.exe',
    dlib: 'azure.codesigning.dlib.dll'
  })

  assert.deepEqual(result, { signed: 250, chunks: 3, skipped: false })
  assert.equal(invocations.length, 3)
  assert.deepEqual(
    invocations.map(batch => batch.filter(arg => arg.endsWith('.exe')).length),
    [100, 100, 50]
  )
  const flat = invocations.flat().filter(arg => arg.endsWith('.exe'))
  assert.equal(new Set(flat).size, 250, 'every binary signed exactly once')
})

// ── toolset resolution: signtool + dlib must be same-arch ───────────────────
// The ATS bundle ships the dlib for x64/x86 only and a 32-bit signtool cannot
// load a 64-bit dlib — resolveSigntool/resolveTrustedSigningDlib must pair the
// HOST arch, never "whatever sorted last" (which picked x86 signtool + x64
// dlib and broke signing with "No certificates were found"). Regression for
// the win32-x64 release build failure.

function fakeToolsetCache() {
  const cache = path.join(tmpTree(), 'electron-builder', 'Cache')
  const kits = path.join(cache, 'win-codesign@1.3.0', 'windows-kits-bundle-10_0_26100_0-abc')
  for (const arch of ['arm64', 'x64', 'x86']) {
    fs.mkdirSync(path.join(kits, arch), { recursive: true })
    fs.writeFileSync(path.join(kits, arch, 'signtool.exe'), 'x')
  }
  const ats = path.join(cache, 'win-codesign@1.3.0', 'ats-bundle-1_0_95-def')
  for (const arch of ['x64', 'x86']) {
    fs.mkdirSync(path.join(ats, arch), { recursive: true })
    fs.writeFileSync(path.join(ats, arch, 'Azure.CodeSigning.Dlib.dll'), 'x')
  }
  fs.mkdirSync(path.join(cache, 'win-codesign@1.3.0', 'dotnet-runtime-win-x64-8_0_28-ghi'), { recursive: true })
  return { cache, kits, ats }
}

test('signingArch matches the host (x64 unless ia32)', () => {
  assert.equal(signingArch(), process.arch === 'ia32' ? 'x86' : 'x64')
})

test('resolveSigntool prefers the host-arch kit over x86', () => {
  const { cache } = fakeToolsetCache()
  const signtool = resolveSigntool({ ELECTRON_BUILDER_CACHE: cache })
  const expectedArch = signingArch()
  assert.ok(signtool, 'a signtool must resolve from the fake cache')
  assert.ok(
    signtool.toLowerCase().includes(`\\${expectedArch}\\signtool.exe`) ||
      signtool.toLowerCase().includes(`/${expectedArch}/signtool.exe`),
    `expected the ${expectedArch} signtool, got ${signtool}`
  )
})

test('resolveTrustedSigningDlib prefers the host-arch dlib over x86', () => {
  const { cache } = fakeToolsetCache()
  const dlib = resolveTrustedSigningDlib({ ELECTRON_BUILDER_CACHE: cache })
  const expectedArch = signingArch()
  assert.ok(dlib, 'a dlib must resolve from the fake cache')
  assert.ok(
    dlib.toLowerCase().includes(`\\${expectedArch}\\azure.codesigning.dlib.dll`) ||
      dlib.toLowerCase().includes(`/${expectedArch}/azure.codesigning.dlib.dll`),
    `expected the ${expectedArch} dlib, got ${dlib}`
  )
})

test('resolveDotnetRuntimeDir finds the bundled .NET runtime dir', () => {
  const { cache } = fakeToolsetCache()
  const dotnet = resolveDotnetRuntimeDir({ ELECTRON_BUILDER_CACHE: cache })
  assert.ok(dotnet, 'the dotnet-runtime bundle must resolve')
  assert.ok(/dotnet-runtime-/.test(path.basename(dotnet)), `unexpected dotnet dir ${dotnet}`)
})

test('batchSignBinaries sets DOTNET_ROOT from the bundled runtime for the signtool child', () => {
  const { cache } = fakeToolsetCache()
  const root = tmpTree()
  fs.mkdirSync(path.join(root, 'tools'), { recursive: true })
  fs.writeFileSync(path.join(root, 'tools', 'node.exe'), 'x')
  const exe = path.join(root, 'Hermes.exe')

  const invocations = []
  const result = batchSignAppTree(root, exe, {
    env: {
      ELECTRON_BUILDER_CACHE: cache,
      AZURE_SIGN_ENDPOINT: 'https://cus.codesigning.azure.net',
      AZURE_SIGN_ACCOUNT: 'codesign2',
      AZURE_SIGN_PROFILE: 'hermesagent'
    },
    exec: (tool, args, opts) => {
      invocations.push({ tool, args, opts })
      return Buffer.from('')
    },
    mkdtemp: () => root
  })

  assert.equal(result.signed, 1)
  assert.equal(invocations.length, 1)
  const opts = invocations[0].opts
  assert.ok(opts && opts.env, 'exec must receive a child env')
  assert.equal(opts.env.DOTNET_ROOT, path.join(cache, 'win-codesign@1.3.0', 'dotnet-runtime-win-x64-8_0_28-ghi'))
  // The signtool chosen must be the host-arch one, not x86.
  assert.match(invocations[0].tool, new RegExp(`[\\\\/]${signingArch()}[\\\\/]signtool\\.exe$`))
  // dlib must be same arch.
  const dlibArg = invocations[0].args[invocations[0].args.indexOf('/dlib') + 1]
  assert.match(dlibArg, new RegExp(`[\\\\/]${signingArch()}[\\\\/]azure\\.codesigning\\.dlib\\.dll$`, 'i'))
})
