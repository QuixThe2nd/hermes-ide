import { describe, expect, it } from 'vitest'

import { botProfileIdentity, slugifyProfileName } from './labels'

describe('profile-name identity', () => {
  it('derives a stable ASCII profile id from a CJK name and keeps the name as the title', () => {
    expect(slugifyProfileName('小助手')).toBe('u5c0f-u52a9-u624b')
    expect(slugifyProfileName('test机器人')).toBe('test-u673a-u5668-u4eba')
    // The encoded form is its own fixed point, and ASCII input slugs as before.
    expect(slugifyProfileName('u5c0f-u52a9-u624b')).toBe('u5c0f-u52a9-u624b')
    expect(slugifyProfileName('Inbox Triage')).toBe('inbox-triage')

    expect(botProfileIdentity('小助手', '')).toEqual({ slug: 'u5c0f-u52a9-u624b', title: '小助手' })
    expect(botProfileIdentity('小助手', 'Helper')).toEqual({ slug: 'u5c0f-u52a9-u624b', title: 'Helper' })
    expect(botProfileIdentity('Test', '')).toEqual({ slug: 'test', title: '' })
    // Symbols carry no letters or digits: still no id, Create stays disabled.
    expect(botProfileIdentity('🤖', '')).toEqual({ slug: '', title: '🤖' })
  })
})
