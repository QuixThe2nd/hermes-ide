import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { expect, it } from 'vitest'

const styles = readFileSync(join(process.cwd(), 'src/styles.css'), 'utf8')
const groupChatView = readFileSync(join(process.cwd(), 'src/plugins/hermes-bots/group-chat-view.tsx'), 'utf8')

it('uses theme-backed inline-code colors in Bot Mode group-chat messages', () => {
  expect(groupChatView).toContain('data-slot="group-chat-message-content"')
  expect(styles).toMatch(
    /\[data-slot='group-chat-message-content'\][\s\S]*?:not\(pre\) > code[\s\S]*?background: var\(--ui-inline-code-background\);[\s\S]*?color: var\(--ui-inline-code-foreground\);/
  )
})
