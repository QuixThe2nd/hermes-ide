// Package citationstrip removes leftover unledgered URLs from a research
// report after its correction pass. It is the Go twin of
// plugins/deep_research/citations.py:strip_safe_unledgered_urls — same rules,
// independently tested. Production Python never calls it; it exists so the
// strip behavior has a second, line-balance-accounted implementation.
//
// Rules (shared with the Python twin):
//   - allowed = normalized evidence URLs.
//   - An ATX heading whose title starts with sources/references/bibliography
//     (case-insensitive) opens a summary section; the next ATX heading ends it.
//   - A summary list item (possibly spanning following non-blank, non-item,
//     non-heading lines) is dropped whole when it cites at least one URL and
//     every URL is unledgered. A mixed item keeps its ledgered URLs and loses
//     only the unledgered occurrences.
//   - Outside a summary section each line is processed alone: unledgered URL
//     occurrences are removed only when that line also cites a ledgered URL.
//     A line whose only URLs are unledgered is unchanged (fail closed).
//   - Removing an occurrence keeps a Markdown link's label, drops <autolink>
//     brackets, and deletes a bare URL's text. Ledgered URLs stay byte-for-byte.
package citationstrip

import (
	"net/url"
	"regexp"
	"strings"
)

// urlRE matches the optional Markdown "](" opener plus an http(s) URL.
// Parentheses are allowed inside the URL; the prose tail (including the
// Markdown closer) is split off by paren balance, not by the character class.
var urlRE = regexp.MustCompile(`(?:\]\(\s*)?(https?://[^\s<>"'\]]+)`)

var (
	atxHeadingRE  = regexp.MustCompile(`^(#{1,6})\s+(.*)$`)
	summaryTitle  = regexp.MustCompile(`^(?i:sources|references|bibliography)\b`)
	listItemRE    = regexp.MustCompile(`^\s*(?:[-*+]|\d+[.)])\s`)
	schemeHTTP    = regexp.MustCompile(`(?i)^https?://`)
	trailingPunct = ".,;:!?]}'\">’”》"
)

// NormalizeURL canonicalizes a URL for provenance matching: strip the prose
// tail, drop the fragment, lowercase scheme/host, drop a default port, and
// remove a bare trailing slash on the root path. Paths and queries compare
// verbatim.
func NormalizeURL(raw string) string {
	candidate := stripProseTail(raw)
	if !schemeHTTP.MatchString(candidate) {
		return strings.ToLower(candidate)
	}
	parts, err := url.Parse(candidate)
	if err != nil || parts.Host == "" {
		return strings.ToLower(candidate)
	}
	host := strings.ToLower(parts.Hostname())
	port := parts.Port()
	scheme := strings.ToLower(parts.Scheme)
	netloc := host
	if port != "" && !((scheme == "http" && port == "80") || (scheme == "https" && port == "443")) {
		netloc = host + ":" + port
	}
	path := parts.Path
	if path == "" {
		path = "/"
	}
	// Reassemble by hand: url.URL.String percent-escapes parentheses that
	// provenance matching must compare verbatim.
	out := scheme + "://" + netloc + path
	if parts.RawQuery != "" {
		out += "?" + parts.RawQuery
	}
	return out
}

func stripUnbalancedParens(u string) string {
	for strings.HasSuffix(u, ")") && strings.Count(u, ")") > strings.Count(u, "(") {
		u = u[:len(u)-1]
	}
	return u
}

func stripProseTail(u string) string {
	candidate := strings.TrimSpace(u)
	for {
		trimmed := stripUnbalancedParens(strings.TrimRight(candidate, trailingPunct))
		if trimmed == candidate {
			return trimmed
		}
		candidate = trimmed
	}
}

// ExtractReportURLs returns every http(s) URL a reader could take as a
// source, deduplicated, in order of appearance (normalized).
func ExtractReportURLs(text string) []string {
	var urls []string
	seen := map[string]bool{}
	for _, m := range urlRE.FindAllStringSubmatch(text, -1) {
		raw := stripProseTail(m[1])
		if raw == "" {
			continue
		}
		normalized := NormalizeURL(raw)
		if normalized == "" || seen[normalized] {
			continue
		}
		seen[normalized] = true
		urls = append(urls, normalized)
	}
	return urls
}

func allowedSet(evidence []string) map[string]bool {
	allowed := map[string]bool{}
	for _, u := range evidence {
		if u != "" {
			allowed[NormalizeURL(u)] = true
		}
	}
	return allowed
}

// StripSafeUnledgeredURLs applies the package rules to reportText.
func StripSafeUnledgeredURLs(reportText string, evidence []string) string {
	allowed := allowedSet(evidence)
	lines := splitKeepEnds(reportText)
	var out []string
	inSummary := false
	for i := 0; i < len(lines); i++ {
		if title, ok := atxTitle(lines[i]); ok {
			inSummary = summaryTitle.MatchString(title)
			out = append(out, lines[i])
			continue
		}
		if inSummary && listItemRE.MatchString(lines[i]) {
			block := []string{lines[i]}
			j := i + 1
			for ; j < len(lines); j++ {
				nxt := lines[j]
				if strings.TrimSpace(nxt) == "" || listItemRE.MatchString(nxt) {
					break
				}
				if _, isHeading := atxTitle(nxt); isHeading {
					break
				}
				block = append(block, nxt)
			}
			urls := ExtractReportURLs(strings.Join(block, ""))
			allUnledgered := len(urls) > 0
			for _, u := range urls {
				if allowed[u] {
					allUnledgered = false
					break
				}
			}
			if !allUnledgered {
				for _, line := range block {
					out = append(out, stripUnledgered(line, allowed))
				}
			}
			i = j - 1
			continue
		}
		out = append(out, stripUnledgered(lines[i], allowed))
	}
	return strings.Join(out, "")
}

func atxTitle(line string) (string, bool) {
	trimmed := strings.TrimSpace(line)
	m := atxHeadingRE.FindStringSubmatch(trimmed)
	if m == nil {
		return "", false
	}
	return m[2], true
}

// stripUnledgered drops unledgered URL occurrences from one line, but only
// when the line also cites at least one ledgered URL.
func stripUnledgered(line string, allowed map[string]bool) string {
	urls := ExtractReportURLs(line)
	hasLedgered := false
	for _, u := range urls {
		if allowed[u] {
			hasLedgered = true
			break
		}
	}
	if len(urls) == 0 || !hasLedgered {
		return line
	}
	matches := urlRE.FindAllStringSubmatchIndex(line, -1)
	for k := len(matches) - 1; k >= 0; k-- {
		m := matches[k]
		fullStart, urlStart, urlEndIdx := m[0], m[2], m[3]
		raw := stripProseTail(line[urlStart:urlEndIdx])
		if raw == "" || allowed[NormalizeURL(raw)] {
			continue
		}
		urlEnd := urlStart + len(raw)
		switch {
		case fullStart < urlStart: // Markdown link destination: keep the label
			opener := strings.LastIndex(line[:fullStart], "[")
			label := ""
			start := fullStart
			if opener >= 0 {
				label = line[opener+1 : fullStart]
				start = opener
			}
			closer := 0
			if urlEnd < len(line) && line[urlEnd] == ')' {
				closer = 1
			}
			line = line[:start] + label + line[urlEnd+closer:]
		case urlStart > 0 && line[urlStart-1] == '<' && urlEnd < len(line) && line[urlEnd] == '>':
			line = line[:urlStart-1] + line[urlEnd+1:] // <autolink>
		default:
			line = line[:urlStart] + line[urlEnd:] // bare URL text
		}
	}
	return line
}

func splitKeepEnds(s string) []string {
	if s == "" {
		return nil
	}
	var lines []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			lines = append(lines, s[start:i+1])
			start = i + 1
		}
	}
	if start < len(s) {
		lines = append(lines, s[start:])
	}
	return lines
}
