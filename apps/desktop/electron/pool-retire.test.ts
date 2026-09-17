import assert from 'node:assert/strict'

import { test } from 'vitest'

import { createPoolRetirer, type IdleVerdict, type PoolRetireEntry, selectRetirementCandidates } from './pool-retire'
import { LocalBackendSpawnCoordinator } from './pool-spawn-coordinator'

// Bug class (supersedes #104871): a foreground open against a full 3-slot pool
// waited 30s and timed out because every resident was keepalive-fresh but
// idle. The salvage rotates ONE resident out — provably idle by the backend's
// own ledgers, behind an admission fence, with the identity rechecked after
// every await, and the slot released only by the real child exit.

const NOW = 1_000_000

interface Entry extends PoolRetireEntry {
  process: { pid: number } | null
}

const resident = (idleMs: number, extra: Partial<Entry> = {}): Entry => ({
  process: { pid: 100 + idleMs },
  lastActiveAt: NOW - idleMs,
  ...extra
})

function harness(verdicts: Record<string, IdleVerdict | IdleVerdict[]>) {
  const pool = new Map<string, Entry>()
  const probes: string[] = []
  const stopped: string[] = []
  const retiring: string[] = []
  const exitResolvers = new Map<string, () => void>()
  const coordinator = new LocalBackendSpawnCoordinator(3)
  const releases = new Map<string, () => void>()

  const retirer = createPoolRetirer<Entry>({
    pool,
    probeIdle: async key => {
      probes.push(key)
      const scripted = verdicts[key]

      if (Array.isArray(scripted)) {
        return scripted.length > 1 ? (scripted.shift() as IdleVerdict) : scripted[0]
      }

      return scripted ?? null
    },
    // Mirrors stopPoolBackend: the entry leaves the pool now, SIGTERM is
    // synchronous, and the slot is released only after the child's real exit.
    stopBackend: key =>
      new Promise<void>(resolve => {
        stopped.push(key)
        pool.delete(key)
        exitResolvers.set(key, () => {
          releases.get(key)?.()
          resolve()
        })
      }),
    onRetiring: key => retiring.push(key)
  })

  // Fill the coordinator like three spawned residents would.
  async function seed(...keys: string[]) {
    for (const key of keys) {
      releases.set(key, await coordinator.acquire(key))
    }
  }

  return { pool, probes, stopped, retiring, exitResolvers, coordinator, retirer, seed }
}

test('selector: LRU among spawned residents, skipping renderer-leased turns and the waiter itself', () => {
  const entries: [string, Entry][] = [
    ['pinned-old', resident(300_000)],
    ['mid-turn', resident(900_000, { activeTurn: true })],
    ['fresh', resident(1_000)],
    ['descriptor', { process: null, lastActiveAt: NOW - 999_999 }],
    ['unknown-activity', resident(600_000, { activeTurn: undefined })],
    ['waiter', resident(999_999)]
  ]

  assert.deepEqual(
    selectRetirementCandidates(entries, new Set(['waiter'])).map(([key]) => key),
    ['unknown-activity', 'pinned-old', 'fresh']
  )
})

test('fence: two concurrent foreground dials share one retirement and exactly one ticket gets the slot', async () => {
  const h = harness({ 'bot-a': true, 'bot-b': true })
  h.pool.set('bot-a', resident(300_000))
  h.pool.set('bot-b', resident(200_000))
  h.pool.set('bot-c', resident(1_000, { activeTurn: true }))
  await h.seed('bot-a', 'bot-b', 'bot-c')
  assert.equal(h.coordinator.activeCount, 3)

  // Two clicks land in the same tick.
  const [first, second] = await Promise.all([
    h.retirer.retireForForeground('new-1'),
    h.retirer.retireForForeground('new-2')
  ])

  assert.ok(first && second)
  assert.equal(first.key, 'bot-a')
  assert.equal(second, first, 'the second dial shares the first retirement, it does not start its own')
  assert.deepEqual(h.probes, ['bot-a'], 'one probe, not one per dial')

  // Trigger point contract: each waiter queues BEFORE the stop so the freed
  // slot cannot be taken by anything that was not already waiting.
  const ticket1 = h.coordinator.request('new-1', { priority: 'foreground', timeoutMs: 10_000 })
  const ticket2 = h.coordinator.request('new-2', { priority: 'foreground', timeoutMs: 10_000 })
  assert.equal(ticket1.queued, true)

  const commits = Promise.all([first.commit(), second.commit()])
  await Promise.resolve()
  assert.deepEqual(h.retiring, ['bot-a'], 'renderer told once, before the stop')
  assert.deepEqual(h.stopped, ['bot-a'], 'exactly one SIGTERM')
  assert.equal(h.pool.has('bot-a'), false)

  // The lease is still held until the child actually exits.
  assert.equal(h.coordinator.activeCount, 3)
  let granted = 0
  void ticket1.acquired.then(() => (granted += 1))
  void ticket2.acquired.then(() => (granted += 1)).catch(() => undefined)
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(granted, 0, 'no slot before the real exit')

  h.exitResolvers.get('bot-a')?.()
  assert.deepEqual(await commits, [true, true])
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(granted, 1, 'exactly one waiter gets the freed slot')
  assert.equal(h.coordinator.queuedCount, 1)
  assert.equal(h.retirer.inFlight(), false, 'the fence lifts once the retirement settled')

  ticket2.cancel()
})

test('identity: the recheck aborts when the entry was swapped or leased a turn while the probe was in flight', async () => {
  const h = harness({ 'bot-a': true, 'bot-b': true })
  const original = resident(300_000)
  h.pool.set('bot-a', original)
  h.pool.set('bot-b', resident(100_000))

  const retirement = await h.retirer.retireForForeground('new')
  assert.equal(retirement?.key, 'bot-a')

  // Between prepare and commit (the waiter is queueing), a respawn replaced
  // bot-a's entry under the same key. The stale retirement must not kill it.
  h.pool.set('bot-a', resident(0))
  assert.equal(await retirement!.commit(), false)
  assert.deepEqual(h.stopped, [])
  assert.deepEqual(h.retiring, [])
  assert.equal(h.retirer.inFlight(), false)

  // Same shape for a renderer turn lease published after the probe answered.
  const h2 = harness({ 'bot-a': true })
  const entry = resident(300_000)
  h2.pool.set('bot-a', entry)
  const r2 = await h2.retirer.retireForForeground('new')
  entry.activeTurn = true
  assert.equal(await r2!.commit(), false)
  assert.deepEqual(h2.stopped, [])
})

test('fail closed: idle null (older runtime / probe error) and cron-running are ineligible; no candidate falls through to the queue', async () => {
  const h = harness({
    'old-runtime': null, // 404 from a backend predating /api/health/idle
    'cron-mid-run': false, // turn_in_flight() saw a running cron job
    'probe-error': null
  })

  h.pool.set('old-runtime', resident(500_000))
  h.pool.set('cron-mid-run', resident(400_000))
  h.pool.set('probe-error', resident(300_000))

  assert.equal(await h.retirer.retireForForeground('new'), null)
  assert.deepEqual(h.probes.sort(), ['cron-mid-run', 'old-runtime', 'probe-error'])
  assert.deepEqual(h.stopped, [])
  assert.equal(h.retirer.inFlight(), false, 'a null result releases the fence for the next dial')
})

test('a renderer activeTurn:false is not proof: the backend re-probe before SIGTERM wins', async () => {
  // Renderer says idle; the backend says a turn started (a messaging-platform
  // turn, a cron fire) between the selection probe and the commit re-probe.
  const h = harness({ 'bot-a': [true, false] })
  h.pool.set('bot-a', resident(300_000, { activeTurn: false }))

  const retirement = await h.retirer.retireForForeground('new')
  assert.equal(retirement?.key, 'bot-a')
  assert.equal(await retirement!.commit(), false)
  assert.deepEqual(h.stopped, [])
  assert.deepEqual(h.probes, ['bot-a', 'bot-a'])
})
