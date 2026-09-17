/**
 * The presentation leaves every Bot Mode surface reads: the name a row shows,
 * the flattened one-line preview beside it, and the slug that identifies a
 * workspace tile.
 *
 * They sit below the surfaces rather than inside any one of them — the roster,
 * the bot row and the group-chat room all render the same identity, and none
 * of them can own it without the others importing a sibling surface.
 */

import { aliasIdentityFor } from './routing'
import type { BotMeta, RosterRow } from './types'

export function displayName(bot: Partial<RosterRow>, meta?: BotMeta | null): string {
  // A configured alias route claiming this row overrides source-derived
  // identity: the friendly alias name must survive hosted-session
  // activation and Cloud-only rosters (#89131).
  const alias = aliasIdentityFor(bot)

  // Only THIN rows from another source trade the friendly name for their
  // connection label — the active gateway's own default must keep reading
  // "Hermes". Annotated active rows carry sourceScoped too, and keying this
  // off sourceScoped renamed the user's main agent to an IP-derived label
  // (community report, Aug 17 2026).
  if (
    bot?.remoteSource &&
    (bot.name || '').trim().toLowerCase() === 'default' &&
    bot.connectionLabel &&
    !alias &&
    !meta?.title?.trim()
  ) {
    return bot.connectionLabel
  }

  if (meta?.title?.trim()) {
    return meta.title.trim()
  }

  // Core-profile display name (profile.yaml, set via `hermes profile rename
  // default <name>` or the dashboard) — the CLI-level equivalent of a Bot
  // Mode title. Rides the profiles.list row; presentation-only.
  if (typeof bot?.display_name === 'string' && bot.display_name.trim()) {
    return bot.display_name.trim()
  }

  // An untitled backend row claimed by an alias reads as the alias name —
  // never generic "Hermes" or a hostname-derived label.
  if (alias) {
    const raw = alias.name.replace(/[-_]+/g, ' ').trim()

    return raw.replace(/\b\w/g, ch => ch.toUpperCase())
  }

  // The primary profile is literally named "default" — as a bot identity
  // that reads like nobody bothered. Present it as Hermes (the agent it is)
  // unless the user gives it a real title.
  if ((bot.name || '').trim().toLowerCase() === 'default' && !bot.title) {
    return 'Hermes'
  }

  const raw = (bot.title || bot.name || '').replace(/[-_]+/g, ' ').trim()

  return raw.replace(/\b\w/g, ch => ch.toUpperCase())
}

export function slugify(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)
}

/** The profile id the backend accepts (`hermes_cli.profiles._PROFILE_ID_RE`
 *  is ASCII-only), derived from whatever the user typed as the bot's name.
 *  Letters and digits outside ASCII become a deterministic `u<hex>` token
 *  per code point — `小助手` → `u5c0f-u52a9-u624b`, `test机器人` →
 *  `test-u673a-u5668-u4eba` — so a CJK name no longer slugs to '' and leaves
 *  Create disabled (#96153). Plain ASCII names slug exactly as before. */
export function slugifyProfileName(value: string) {
  return slugify(
    value
      .normalize('NFKD')
      .replace(/[\p{L}\p{N}]/gu, ch => (/[a-zA-Z0-9]/.test(ch) ? ch : `-u${ch.codePointAt(0)!.toString(16)}-`))
      .replace(/-+/g, '-')
  )
}

/** Split the dialog's Name field into the backend id and the display title.
 *  A non-ASCII name cannot be the profile id, so the entered string survives
 *  as the title when the user left Title empty. */
export function botProfileIdentity(name: string, title: string) {
  const enteredName = name.trim()

  return {
    slug: slugifyProfileName(enteredName),
    title: title.trim() || (/[^\u0020-\u007e]/.test(enteredName) ? enteredName : '')
  }
}

/** Flatten markdown syntax out of a one-line roster preview so rows read
 *  like Discord's — no raw **bold**, `code`, > quotes, or [link](url)
 *  characters in the preview line. */
export function stripPreviewMarkdown(text: unknown) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`\n]*)`/g, '$1')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/(\*\*|__)(.*?)\1/g, '$2')
    .replace(/(^|\s)[*_](\S(?:.*?\S)?)[*_](?=\s|$|[.,;:!?])/g, '$1$2')
    .replace(/~~(.*?)~~/g, '$1')
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}>\s?/gm, '')
    .replace(/\s+/g, ' ')
    .trim()
}
