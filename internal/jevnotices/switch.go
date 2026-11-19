package jevnotices

import "strings"

// NoticesKey is the record field carrying the owning profile's
// display.jev_notices value. The gateway bridge copies the raw config value
// in (or leaves it out when the profile does not set it); this package alone
// decides what it means.
const NoticesKey = "notices"

// truthyStrings mirrors utils.TRUTHY_STRINGS, the project's shared bool-ish
// string set, so a YAML string reads the same here as in every Python key.
var truthyStrings = map[string]bool{"1": true, "true": true, "yes": true, "on": true}

// NoticesEnabled reports whether a record may render. Notices default on: an
// absent or null switch keeps them. Any other value is coerced like
// utils.is_truthy_value — strings through the shared truthy set (an unknown
// word never opts in), numbers by non-zero, collections by non-emptiness.
// A silenced record changes nothing but the chat line: the decision it
// reports and its logging happen before the record is built.
func NoticesEnabled(record map[string]any) bool {
	switch v := record[NoticesKey].(type) {
	case nil:
		return true
	case bool:
		return v
	case string:
		return truthyStrings[strings.ToLower(strings.TrimSpace(v))]
	case float64:
		return v != 0
	case int:
		return v != 0
	case map[string]any:
		return len(v) > 0
	case []any:
		return len(v) > 0
	default:
		return true
	}
}
