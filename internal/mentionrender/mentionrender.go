// Package mentionrender renders Discord mention tokens into text a model can
// read: the bot's own token becomes its display name (dropped when it is the
// addressing prefix at the very start of a message), other user mentions
// become "@<display name> (<@id>)" so the model can both tell who was
// mentioned and ping them back, and a mention with no resolvable name keeps
// its raw token. A second mode derives auto-thread placeholder titles: the
// addressing run at the start is dropped, later user mentions become
// "@<display name>" with no id, and unresolvable ones are dropped.
//
// The package is the ONLY implementation of these rules. The Discord adapter
// reaches it through the cmd/mentionrender binary (stdin JSON → stdout JSON)
// and a thin Python bridge; the table in testdata/cases.json is shared with
// the Python bridge tests so both sides pin the same rendering.
//
// Display names are user-controlled text: SanitizeName strips control and
// format characters, collapses whitespace, drops the characters that could
// forge a mention token or a mass ping, refuses names that ARE a mass ping
// ("everyone"/"here"), and caps the length. A name that is empty after
// cleaning counts as unresolvable.
package mentionrender

import (
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Render modes.
const (
	ModeBody  = "body"
	ModeTitle = "title"
)

const (
	// maxNameRunes bounds one rendered display name. Discord caps display
	// names at 32; 64 is generous headroom while still bounding injected
	// length.
	maxNameRunes = 64
	// Title shaping mirrors the historical Python renderer: cap at 80
	// runes, an over-long title keeps the first 77 plus an ellipsis, and an
	// empty title falls back to a fixed placeholder.
	maxTitleRunes   = 80
	titleEllipsisAt = 77
	titleEllipsis   = "..."
	emptyTitle      = "Hermes"
)

// Request is one render job. Names maps a mentioned user's ID to the display
// name Discord delivered with the message (guild member name preferred); the
// caller gathers it from message.mentions and the guild member cache, never
// from extra REST calls.
type Request struct {
	Content string            `json:"content"`
	BotID   string            `json:"bot_id"`
	BotName string            `json:"bot_name"`
	Names   map[string]string `json:"names"`
	Mode    string            `json:"mode"`
}

// mentionToken matches every Discord mention form: <@123> and the legacy
// <@!123> (users), <@&123> (roles), <#123> (channels).
var mentionToken = regexp.MustCompile(`<@(!|&)?([0-9]+)>|<#([0-9]+)>`)

type tokenKind int

const (
	kindUser tokenKind = iota
	kindRole
	kindChannel
)

// parseToken classifies one matched mention token and returns its snowflake.
// Input always comes from mentionToken, so the shape is guaranteed.
func parseToken(tok string) (tokenKind, string) {
	inner := tok[1 : len(tok)-1] // strip < >
	switch {
	case strings.HasPrefix(inner, "#"):
		return kindChannel, inner[1:]
	case strings.HasPrefix(inner, "@&"):
		return kindRole, inner[2:]
	case strings.HasPrefix(inner, "@!"):
		return kindUser, inner[2:]
	default: // "@123"
		return kindUser, inner[1:]
	}
}

// SanitizeName makes a user-controlled display name safe to inline into text
// the model reads: control and zero-width format characters are removed,
// whitespace collapses to single spaces, and '<', '>' and '@' are dropped so
// the name cannot forge a mention token or an @everyone/@here ping. A name
// that IS a mass ping, or that is empty after cleaning, is unresolvable.
func SanitizeName(name string) string {
	var b strings.Builder
	b.Grow(len(name))
	for _, r := range name {
		switch {
		case unicode.IsSpace(r):
			b.WriteByte(' ')
		case unicode.IsControl(r) || unicode.Is(unicode.Cf, r) ||
			r == '<' || r == '>' || r == '@':
			// dropped
		default:
			b.WriteRune(r)
		}
	}
	cleaned := strings.Join(strings.Fields(b.String()), " ")
	if cleaned == "" {
		return ""
	}
	if strings.EqualFold(cleaned, "everyone") || strings.EqualFold(cleaned, "here") {
		return ""
	}
	runes := []rune(cleaned)
	if len(runes) > maxNameRunes {
		cleaned = string(runes[:maxNameRunes])
	}
	return cleaned
}

// Render dispatches on Request.Mode. Callers validate the mode, so an
// unknown mode falls through to the body renderer.
func Render(req Request) string {
	if req.Mode == ModeTitle {
		return renderTitle(req)
	}
	return renderBody(req)
}

// renderBody renders the text the model receives. The bot's own token inside
// the addressing run (whitespace and mention tokens at the very start of the
// message) is an address prefix and is dropped; anywhere else it becomes the
// bot's display name. Other user mentions become "@name (<@id>)", or stay
// raw when no name resolves. Role and channel tokens stay raw — body text
// never rewrote them.
func renderBody(req Request) string {
	content := req.Content
	matches := mentionToken.FindAllStringIndex(content, -1)
	if len(matches) == 0 {
		return strings.TrimSpace(content)
	}
	var b strings.Builder
	b.Grow(len(content) + 32)
	botName := SanitizeName(req.BotName)
	addressing := true
	last := 0
	for _, m := range matches {
		gap := content[last:m[0]]
		if addressing && strings.TrimSpace(gap) != "" {
			addressing = false
		}
		b.WriteString(gap)
		tok := content[m[0]:m[1]]
		kind, id := parseToken(tok)
		switch {
		case kind != kindUser:
			b.WriteString(tok)
		case id == req.BotID && req.BotID != "":
			if addressing {
				// Address prefix: drop the token together with the
				// whitespace separating it from what follows, so the
				// remainder keeps its original single spacing.
				last = m[1]
				for last < len(content) {
					r, size := utf8.DecodeRuneInString(content[last:])
					if !unicode.IsSpace(r) {
						break
					}
					last += size
				}
				continue
			}
			if botName != "" {
				b.WriteByte('@')
				b.WriteString(botName)
			}
		default:
			if name := SanitizeName(req.Names[id]); name != "" {
				b.WriteByte('@')
				b.WriteString(name)
				b.WriteString(" (<@")
				b.WriteString(id)
				b.WriteString(">)")
			} else {
				b.WriteString(tok)
			}
		}
		last = m[1]
	}
	b.WriteString(content[last:])
	return strings.TrimSpace(b.String())
}

// renderTitle derives an auto-thread placeholder title: the addressing run
// at the start is dropped, later user mentions become "@name" with no id,
// unresolvable mentions and role/channel tokens are dropped, whitespace is
// collapsed, and the historical 80-rune cap (77 + ellipsis) and empty-title
// fallback are preserved.
func renderTitle(req Request) string {
	s := req.Content
	for {
		s = strings.TrimLeftFunc(s, unicode.IsSpace)
		loc := mentionToken.FindStringIndex(s)
		if loc == nil || loc[0] != 0 {
			break
		}
		s = s[loc[1]:]
	}
	matches := mentionToken.FindAllStringIndex(s, -1)
	botName := SanitizeName(req.BotName)
	var b strings.Builder
	b.Grow(len(s))
	last := 0
	for _, m := range matches {
		b.WriteString(s[last:m[0]])
		kind, id := parseToken(s[m[0]:m[1]])
		if kind == kindUser {
			name := botName
			if id != req.BotID || req.BotID == "" {
				name = SanitizeName(req.Names[id])
			}
			if name != "" {
				b.WriteByte('@')
				b.WriteString(name)
			}
		}
		last = m[1]
	}
	b.WriteString(s[last:])
	collapsed := strings.Join(strings.Fields(b.String()), " ")
	if collapsed == "" {
		return emptyTitle
	}
	runes := []rune(collapsed)
	if len(runes) > maxTitleRunes {
		return string(runes[:titleEllipsisAt]) + titleEllipsis
	}
	return collapsed
}
