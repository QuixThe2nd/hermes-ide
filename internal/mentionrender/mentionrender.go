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
// forge a mention token or a mass ping (including their fullwidth and
// small-form lookalikes), refuses names that hold a mass ping, and caps the
// length. A name that is empty or carries no visible character after
// cleaning counts as unresolvable.
//
// Cleaning alone is not the safety story: a cleaned name can still combine
// with the text around it (stray angle brackets, a dropped mention joining
// its neighbours) into a forged token or mass ping. Render therefore
// VERIFIES the output against the input after rendering — every output
// mention token must be a token the input carried, and pings may not
// multiply — and falls back to a name-free rendering, then to the raw
// input, when the property does not hold (see mentionContractOK).
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
// whitespace collapses to single spaces, and every character that could
// build a mention token or a ping — '<', '>', '@' plus their fullwidth and
// small-form lookalikes — is dropped. A name whose '@'-prefixed form holds
// a mass ping, or that has no visible character after cleaning, is
// unresolvable.
func SanitizeName(name string) string {
	var b strings.Builder
	b.Grow(len(name))
	visible := false
	for _, r := range name {
		switch {
		case unicode.IsSpace(r):
			b.WriteByte(' ')
		case unicode.IsControl(r) || unicode.Is(unicode.Cf, r) || isForgeryRune(r):
			// dropped
		default:
			b.WriteRune(r)
			if !isInvisibleRune(r) {
				visible = true
			}
		}
	}
	cleaned := strings.Join(strings.Fields(b.String()), " ")
	if cleaned == "" || !visible {
		return ""
	}
	// The rendered form is always "@"+name: refuse any name whose rendered
	// form already holds a mass ping on its own. Combinations with the text
	// around the name are the output contract's job (mentionContractOK).
	if massPing.MatchString("@" + cleaned) {
		return ""
	}
	runes := []rune(cleaned)
	if len(runes) > maxNameRunes {
		cleaned = string(runes[:maxNameRunes])
	}
	return cleaned
}

// isForgeryRune reports whether r can build a mention token or a ping when
// inlined into text: the ASCII token characters plus their fullwidth and
// small-form lookalikes. An explicit list, not a Unicode-range guess — only
// these shapes read as mention syntax to a human or a downstream renderer.
func isForgeryRune(r rune) bool {
	switch r {
	case '<', '>', '@',
		'\uFF1C', '\uFF1E', '\uFF20', // fullwidth < > @
		'﹤', '﹥', '﹫': // small-form < > @
		return true
	}
	return false
}

// isInvisibleRune reports whether r shows no glyph on its own: combining
// marks need a base character, and filler/blank codepoints are empty by
// design. A name with no visible rune would render as a bare '@'.
func isInvisibleRune(r rune) bool {
	if unicode.Is(unicode.Mn, r) || unicode.Is(unicode.Me, r) {
		return true
	}
	switch r {
	case '\u3164', '\u2800': // hangul filler, braille blank
		return true
	}
	return false
}

// massPing matches the mass pings the output contract (I2) bounds.
var massPing = regexp.MustCompile(`(?i)@(everyone|here)`)

// mentionContractOK enforces the render safety invariants as a property of
// the output, not only of the cleaned names:
//
//   - I1: every mention token in the output must be a token the input
//     already carried (same kind and snowflake) — a name, the text around
//     a name, or a deletion joining two neighbours can never mint a token
//     for an id that was not mentioned.
//   - I2: the output holds no more @everyone/@here pings (case-insensitive)
//     than the input did.
//
// A legitimate render always passes: it only copies input text verbatim,
// drops tokens, or emits the " (<@id>)" suffix for an id it just read from
// the input, and name sanitization refuses self-pinging names, so pings in
// the output are exactly pings the input carried.
func mentionContractOK(input, output string) bool {
	budget := map[tokenKey]int{}
	for _, tok := range mentionToken.FindAllString(input, -1) {
		kind, id := parseToken(tok)
		budget[tokenKey{kind, id}]++
	}
	for _, tok := range mentionToken.FindAllString(output, -1) {
		kind, id := parseToken(tok)
		key := tokenKey{kind, id}
		if budget[key] == 0 {
			return false
		}
		budget[key]--
	}
	pingsIn := len(massPing.FindAllString(input, -1))
	pingsOut := len(massPing.FindAllString(output, -1))
	return pingsOut <= pingsIn
}

type tokenKey struct {
	kind tokenKind
	id   string
}

// Render dispatches on Request.Mode and guarantees the output contract
// (mentionContractOK): the full render is returned when it verifies, else a
// name-free rendering (every mention keeps or drops its raw token exactly
// as if no name resolved), else the safest possible shape — the raw input
// for body text, the fixed placeholder for titles. Callers validate the
// mode, so an unknown mode falls through to the body renderer.
func Render(req Request) string {
	if req.Mode == ModeTitle {
		return renderTitleVerified(req)
	}
	return renderBodyVerified(req)
}

// withoutNames returns the request with every name fact removed: the render
// then treats all mentions as unresolvable, the always-safe fallback shape.
func withoutNames(req Request) Request {
	req.Names = nil
	req.BotName = ""
	return req
}

func renderBodyVerified(req Request) string {
	if out := renderBody(req); mentionContractOK(req.Content, out) {
		return out
	}
	// Name-free body: only the addressing run is dropped (a prefix deletion
	// cannot join neighbours), every other token stays raw.
	if out := renderBody(withoutNames(req)); mentionContractOK(req.Content, out) {
		return out
	}
	return strings.TrimSpace(req.Content)
}

func renderTitleVerified(req Request) string {
	if out := renderTitle(req); mentionContractOK(req.Content, out) {
		return out
	}
	// Name-free title drops every mention; a drop can join the text on both
	// sides into a forged token or ping, so it is verified too.
	if out := renderTitle(withoutNames(req)); mentionContractOK(req.Content, out) {
		return out
	}
	return emptyTitle
}

// renderBody renders the text the model receives. The bot's own token inside
// the addressing run (whitespace and mention tokens at the very start of the
// message) is an address prefix and is dropped; anywhere else it becomes the
// bot's display name, or stays raw when the bot's name is unresolvable.
// Other user mentions become "@name (<@id>)", or stay raw when no name
// resolves. Role and channel tokens stay raw — body text never rewrote them.
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
			} else {
				// An unresolvable bot name gets the same rule as any other
				// unresolvable user: keep the raw token, never leave a hole.
				b.WriteString(tok)
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
