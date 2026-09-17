/**
 * Supervisor decision for a primary backend child's post-ready exit (#112344).
 *
 * The child's `exit` handler classifies the exit as "current" (this child
 * still owned the connection slot) or "stale" (the slot was already cleared
 * or moved on). A stale exit is normally harmless: a replacement owns the
 * slot or a start is already in flight. But the same classification also
 * fires when the slot was emptied and NOTHING followed — the connection was
 * invalidated without a replacement start, or the child's own `error`
 * handler cleared the slot first — and then the UI keeps running with no
 * engine until the user relaunches the app (9 h observed).
 *
 * `claim` answers "does the supervisor own the respawn for this exit?" from
 * the primary slot's state alone. Pool children never enter the decision:
 * they do not own the window backend and must not suppress its recovery.
 */
export type BackendExitRecoveryState = {
  /** A live primary (local child or remote descriptor) or a published attempt still holds the slot. */
  hasCurrentOwner: boolean
  /** startHermes() is running but has not published its attempt yet. */
  hasPendingStart: boolean
  /** The slot was emptied on purpose (re-home, quit, hand-off, latched boot failure). */
  intentionalTeardown: boolean
}

export function createBackendExitRecoveryLatch() {
  let claimed = false

  return {
    /** True exactly once per empty slot; `reset()` when a backend becomes ready again. */
    claim(state: BackendExitRecoveryState): boolean {
      if (claimed || state.hasCurrentOwner || state.hasPendingStart || state.intentionalTeardown) {
        return false
      }

      claimed = true

      return true
    },
    reset(): void {
      claimed = false
    }
  }
}
