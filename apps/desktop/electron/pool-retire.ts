// Cooperative retirement of a provably idle pooled backend for a foreground dial.
//
// The local pool caps spawned `hermes serve` children (3 by default) and a
// child's hard slot lease lives exactly as long as the child. The renderer
// keeps every open socket's entry keepalive-fresh (60s touch), so LRU eviction
// and the idle reaper — both keyed off lastActiveAt — never free a slot held by
// a bot tile that is merely pinned. Occupied is not busy: two pinned residents
// plus one foreground resident fill the pool and the fourth foreground open
// waits 30s for a slot that nothing will release, then times out.
//
// This module lets a foreground dial retire ONE resident, under these rules:
//
//   * Proof comes from the backend. Every candidate is probed
//     (`/api/health/idle`: running sessions + running cron jobs + prompts
//     waiting on a human) and only `true` is idle. `false` and `null` (an older
//     runtime, a probe error, an unreadable ledger) are both busy. The
//     renderer-published `activeTurn` lease is an early skip, never the proof —
//     it cannot see cron fires or messaging turns on a pooled backend.
//   * Admission fence. Concurrent foreground dials share one retirement; the
//     coordinator hands the freed slot to whichever ticket queued first.
//   * Identity is rechecked after every await (`pool.get(key) === entry`), and
//     the candidate is re-probed immediately before SIGTERM: a turn may have
//     started while the waiter queued.
//   * The slot is released only by the caller's `stopBackend`, i.e. after the
//     child has actually exited (stopPoolBackend → releaseLocalBackendSlot).
//     Nothing here touches the lease.
//   * `onRetiring(key)` fires before the stop so the renderer can park the
//     scope instead of redialing into the slot it just vacated.
//
// Pure/DI-testable like pool-spawn-coordinator.ts and pool-stop.ts. Trigger
// point (foreground dial at activeCount >= cap, before coordinator.request)
// and the LRU-among-eligible selector shape are from #104871 by @bounce12340.

export interface PoolRetireEntry {
  /** Renderer-published prompt-turn lease (`touchBackend(scope, { activeTurn })`). Undefined = unknown. */
  activeTurn?: boolean
  lastActiveAt?: null | number
  process?: unknown
}

/** true = the backend proved it is idle; false = busy; null = cannot prove (treated as busy). */
export type IdleVerdict = boolean | null

export interface PoolRetirerDeps<E extends PoolRetireEntry> {
  pool: Map<string, E>
  /** Ask the backend itself. Must never throw; map every failure to null. */
  probeIdle: (key: string, entry: E) => Promise<IdleVerdict>
  /** Bounded teardown; resolves only after the child has actually exited and its slot was released. */
  stopBackend: (key: string) => Promise<void>
  /** Fires before the stop so the renderer parks the scope instead of redialing. */
  onRetiring?: (key: string) => void
  log?: (message: string) => void
}

export interface PoolRetirement {
  key: string
  /**
   * Final identity + idle recheck, then stop. Resolves true when the child was
   * retired (after its real exit), false when the recheck aborted. Idempotent:
   * every foreground dial sharing this retirement gets the same promise.
   */
  commit: () => Promise<boolean>
  /** Release the fence without stopping anything (the caller failed before it could queue). */
  abandon: () => void
}

export interface PoolRetirer {
  /**
   * Find a resident this foreground dial may retire. Resolves null when no
   * candidate can prove idle (the dial falls through to the ordinary slot
   * queue). While a retirement is being prepared or committed, every caller
   * shares it.
   */
  retireForForeground: (waiterKey: string) => Promise<PoolRetirement | null>
  /** Test/diagnostic: whether a retirement is in flight. */
  inFlight: () => boolean
}

/**
 * Spawned residents a foreground dial may consider, least-recently-used first.
 * Entries without a child (descriptors, spawns still queued) hold no slot;
 * entries the renderer reports as mid-turn are skipped early; the waiter's own
 * key is never a candidate. Unknown activity (`activeTurn` undefined) stays
 * eligible — the backend probe decides, not the renderer.
 */
export function selectRetirementCandidates<K, E extends PoolRetireEntry>(
  entries: Iterable<[K, E]>,
  exclude: ReadonlySet<K>
): [K, E][] {
  return [...entries]
    .filter(([key, entry]) => Boolean(entry.process) && entry.activeTurn !== true && !exclude.has(key))
    .sort((a, b) => (a[1].lastActiveAt || 0) - (b[1].lastActiveAt || 0))
}

export function createPoolRetirer<E extends PoolRetireEntry>(deps: PoolRetirerDeps<E>): PoolRetirer {
  let inFlight: Promise<PoolRetirement | null> | null = null

  const log = (message: string) => deps.log?.(message)

  // Still the same live entry, and the renderer has not leased a turn since.
  const stillRetirable = (key: string, entry: E): boolean =>
    deps.pool.get(key) === entry && entry.activeTurn !== true

  function makeRetirement(key: string, entry: E, release: () => void): PoolRetirement {
    let committed: Promise<boolean> | null = null

    return {
      key,
      commit: () => {
        if (committed) {
          return committed
        }

        committed = (async () => {
          try {
            // Re-probe right before the stop: the waiter's request() is queued
            // by now, so whatever this proves holds for the slot handoff.
            const verdict = await deps.probeIdle(key, entry)

            if (verdict !== true || !stillRetirable(key, entry)) {
              log(`Pool retirement of "${key}" aborted: backend no longer provably idle`)

              return false
            }

            deps.onRetiring?.(key)
            log(`Retiring provably idle profile backend "${key}" for a foreground dial`)
            // stopBackend must SIGTERM synchronously on entry (pool-stop.ts does):
            // no await sits between the recheck above and the signal.
            await deps.stopBackend(key)

            return true
          } finally {
            release()
          }
        })()

        return committed
      },
      abandon: () => {
        if (!committed) {
          release()
        }
      }
    }
  }

  async function prepare(waiterKey: string, release: () => void): Promise<PoolRetirement | null> {
    const candidates = selectRetirementCandidates(deps.pool.entries(), new Set([waiterKey]))

    for (const [key, entry] of candidates) {
      const verdict = await deps.probeIdle(key, entry)

      if (verdict !== true) {
        log(`Pool resident "${key}" not retirable for a foreground dial (idle=${String(verdict)})`)

        continue
      }

      // Identity recheck after the await: a reaper, a delete, or an exit may
      // have replaced or removed the entry while the probe was in flight.
      if (!stillRetirable(key, entry)) {
        continue
      }

      return makeRetirement(key, entry, release)
    }

    return null
  }

  return {
    retireForForeground: waiterKey => {
      if (inFlight) {
        return inFlight
      }

      let released = false
      let run: Promise<PoolRetirement | null> | null = null

      const release = () => {
        if (released) {
          return
        }

        released = true

        if (inFlight === run) {
          inFlight = null
        }
      }

      run = prepare(waiterKey, release).then(
        retirement => {
          if (!retirement) {
            release()
          }

          return retirement
        },
        error => {
          release()
          throw error
        }
      )

      inFlight = run

      return run
    },
    inFlight: () => inFlight !== null
  }
}
