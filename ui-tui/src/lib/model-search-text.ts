/**
 * Extra tokens used only for model-picker search ranking.
 *
 * Wire IDs stay unchanged — some providers report short or brand-less ids
 * (Kimi Coding's flagship is literally `k3`) that users still search for by
 * the familiar `kimi-…` naming of sibling models.
 *
 * Keep in sync with web/src/lib/model-search-text.ts and
 * hermes_cli/model_search.py.
 */
const MODEL_SEARCH_ALIASES: Record<string, readonly string[]> = {
  k3: ['kimi-k3', 'kimi']
  // Brand the retired Ox Alpha preview nowhere: opaque provider ids (e.g.
  // OpenCode Zen's `x-preview-f-free`) stay searchable by their own id and
  // fully usable when the provider returns them or the user types them.
}

/** Haystack for fuzzy/substring model search; never changes the wire id. */
export function modelSearchText(model: string): string {
  const id = model.trim()

  if (!id) {
    return model
  }

  const aliases = MODEL_SEARCH_ALIASES[id.toLowerCase()]

  if (!aliases?.length) {
    return id
  }

  return `${id} ${aliases.join(' ')}`
}
