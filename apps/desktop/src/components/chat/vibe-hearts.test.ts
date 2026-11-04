import { beforeEach, describe, expect, it, vi } from 'vitest'

const { burst } = vi.hoisted(() => ({ burst: vi.fn() }))

vi.mock('@/components/particles/particle-field', () => ({
  createParticleEmitter: () => ({ burst, subscribe: () => () => undefined }),
  ParticleField: () => null
}))

import { burstVibeHearts } from '@/components/chat/vibe-hearts'
import { setVibeHeartsEnabled } from '@/store/vibe-hearts-enabled'

describe('burstVibeHearts', () => {
  beforeEach(() => {
    burst.mockClear()
    setVibeHeartsEnabled(true)
  })

  it('plays hearts when the preference is on', () => {
    burstVibeHearts()
    expect(burst).toHaveBeenCalledOnce()
  })

  it('no-ops when the preference is off', () => {
    setVibeHeartsEnabled(false)
    burstVibeHearts()
    expect(burst).not.toHaveBeenCalled()
  })

  it('reads the live atom (toggle mid-session takes effect)', () => {
    setVibeHeartsEnabled(false)
    burstVibeHearts()
    expect(burst).not.toHaveBeenCalled()

    setVibeHeartsEnabled(true)
    burstVibeHearts()
    expect(burst).toHaveBeenCalledOnce()
  })
})
