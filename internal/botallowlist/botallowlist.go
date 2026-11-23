// Package botallowlist is the single parser and membership oracle for the
// per-profile bot allowlist setting: the set of bot user IDs whose messages
// a platform adapter may admit (“discord.allowed_bots“ in config.yaml,
// mirrored to the “DISCORD_ALLOWED_BOTS“ env var).
//
// The setting accepts three shapes, mirroring the coercion the gateway's
// sibling CSV gates already apply:
//
//   - a YAML/JSON list, entries being strings or numbers (user IDs exceed
//     float64 precision, so JSON numbers must decode as json.Number);
//   - a comma-separated string;
//   - a JSON-array literal string — a string that starts with "[" but is
//     not valid JSON falls back to CSV, exactly like the Python gate's
//     csv-set coercion, so no shape changes meaning between the two.
//
// Any other shape (a JSON object, a boolean, a nested container, a list
// with non-scalar entries) is malformed: callers fail closed and deny bot
// authors rather than guess at the intent.
//
// The allowlist only narrows admission. An empty set narrows nothing, the
// platform's allow_bots mode rules still apply to listed bots, and
// human-authored messages never consult it.
package botallowlist

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// ErrMalformed marks a raw value whose shape can never name a bot ID.
// Callers fail closed on it: bot authors are denied with one warning.
var ErrMalformed = errors.New(
	"allowed_bots: malformed value (want a list of bot user IDs or a comma-separated string)",
)

// Parse normalizes a raw setting value into the sorted set of allowed bot
// user IDs. A nil value — and any shape that names no ID — yields an empty
// set, which narrows nothing.
func Parse(raw any) ([]string, error) {
	switch value := raw.(type) {
	case nil:
		return nil, nil
	case string:
		return parseString(value)
	case []any:
		return parseEntries(value)
	case json.Number:
		return parseEntries([]any{value})
	case float64:
		return parseEntries([]any{value})
	case int:
		return parseEntries([]any{value})
	case int64:
		return parseEntries([]any{value})
	default:
		return nil, ErrMalformed
	}
}

// Member reports whether authorID is exactly one of the allowed IDs. An
// empty author ID never matches: a bot message with no usable author ID is
// not in any operator-written list.
func Member(ids []string, authorID string) bool {
	id := strings.TrimSpace(authorID)
	if id == "" {
		return false
	}
	for _, allowed := range ids {
		if allowed == id {
			return true
		}
	}
	return false
}

// parseString handles the scalar forms: a comma-separated string, or a
// JSON-array literal string (falling back to CSV when the "["-prefixed
// text is not exactly one JSON array, mirroring the Python coercion).
func parseString(raw string) ([]string, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return nil, nil
	}
	if strings.HasPrefix(trimmed, "[") {
		var entries []any
		decoder := json.NewDecoder(strings.NewReader(trimmed))
		decoder.UseNumber()
		// !decoder.More(): trailing garbage after the array makes the whole
		// value CSV-shaped, matching the Python json.loads fallback.
		if err := decoder.Decode(&entries); err == nil && !decoder.More() {
			return parseEntries(entries)
		}
	}
	parts := strings.Split(trimmed, ",")
	set := make(map[string]struct{}, len(parts))
	for _, part := range parts {
		if entry := strings.TrimSpace(part); entry != "" {
			set[entry] = struct{}{}
		}
	}
	return sorted(set), nil
}

// parseEntries normalizes list entries. Strings and numbers are IDs;
// anything else (booleans, null, nested containers) is malformed — the
// operator wrote a shape that cannot name a bot, so fail closed rather
// than silently keep the valid-looking entries.
func parseEntries(entries []any) ([]string, error) {
	set := make(map[string]struct{}, len(entries))
	for _, entry := range entries {
		text, err := entryText(entry)
		if err != nil {
			return nil, err
		}
		if trimmed := strings.TrimSpace(text); trimmed != "" {
			set[trimmed] = struct{}{}
		}
	}
	return sorted(set), nil
}

// entryText renders one list entry as an ID string. json.Number preserves
// user IDs digit-exact; a float64 renders without exponent so a
// JSON-decoded integral number still names the same ID.
func entryText(entry any) (string, error) {
	switch value := entry.(type) {
	case string:
		return value, nil
	case json.Number:
		return value.String(), nil
	case float64:
		return strconv.FormatFloat(value, 'f', -1, 64), nil
	case int:
		return strconv.Itoa(value), nil
	case int64:
		return strconv.FormatInt(value, 10), nil
	default:
		return "", fmt.Errorf("%w (entry %#v)", ErrMalformed, entry)
	}
}

// sorted returns the set's members in sorted order so callers (and tests)
// compare a deterministic list.
func sorted(set map[string]struct{}) []string {
	ids := make([]string, 0, len(set))
	for id := range set {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
