// Watch-URL twin of plugins/omp_viewer/url.py: the status-line prefix the
// lane launcher emits, the watch-URL builder, and the adapter-side safety
// gate. The gate mirrors the Python implementation decision-for-decision:
// http/https only, no userinfo, path "/" only, no query, fragment empty or
// an absolute traversal-free selector, and a private-network host
// (loopback, RFC1918, Tailscale CGNAT, ::1) or a configured extra host.
package ompviewer

import (
	"fmt"
	"net"
	"net/url"
	"strings"
	"unicode"
)

// StatusPrefix mirrors tools/tool_status.py OMP_LANE_VIEWER_STATUS_PREFIX:
// the producer/consumer contract prefix of the mid-tool status line.
const StatusPrefix = "OMP Lane: "

// allowedV4 mirrors _ALLOWED_V4_NETS in the Python twin.
var allowedV4 = []*net.IPNet{
	mustCIDR("10.0.0.0/8"),
	mustCIDR("100.64.0.0/10"),
	mustCIDR("127.0.0.0/8"),
	mustCIDR("172.16.0.0/12"),
	mustCIDR("192.168.0.0/16"),
}

var v6Loopback = net.ParseIP("::1")

func mustCIDR(cidr string) *net.IPNet {
	_, network, err := net.ParseCIDR(cidr)
	if err != nil {
		panic(err)
	}
	return network
}

// quotePath mirrors Python's urllib.parse.quote(selector, safe="/"):
// ASCII letters, digits, and "_.-~/" pass through; every other byte of the
// UTF-8 encoding becomes an uppercase %XX escape.
func quotePath(selector string) string {
	var b strings.Builder
	for i := range len(selector) {
		c := selector[i]
		switch {
		case c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9':
			b.WriteByte(c)
		case strings.IndexByte("_.-~/", c) >= 0:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// unquote mirrors Python's urllib.parse.unquote at the byte level: valid
// %XX sequences decode; anything malformed stays literal.
func unquote(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); {
		if s[i] == '%' && i+2 < len(s) && isHexDigit(s[i+1]) && isHexDigit(s[i+2]) {
			b.WriteByte(hexValue(s[i+1])<<4 | hexValue(s[i+2]))
			i += 3
			continue
		}
		b.WriteByte(s[i])
		i++
	}
	return b.String()
}

func isHexDigit(c byte) bool {
	return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F'
}

func hexValue(c byte) byte {
	switch {
	case c >= '0' && c <= '9':
		return c - '0'
	case c >= 'a' && c <= 'f':
		return c - 'a' + 10
	default:
		return c - 'A' + 10
	}
}

// WatchURL mirrors watch_url(): the viewer base URL plus the quoted
// session-directory selector as fragment.
func WatchURL(base, selector string) string {
	return base + "/#" + quotePath(selector)
}

// selectorOK mirrors _selector_ok(): the fragment must decode to an
// absolute, traversal-free directory free of NUL and control characters.
func selectorOK(fragment string) bool {
	decoded := unquote(fragment)
	if decoded == "" || strings.ContainsRune(decoded, 0) {
		return false
	}
	for _, r := range decoded {
		if r < 0x20 || r == 0x7F {
			return false
		}
	}
	if !strings.HasPrefix(decoded, "/") {
		return false
	}
	for _, part := range strings.Split(decoded, "/") {
		if part == ".." {
			return false
		}
	}
	return true
}

// hostAllowed mirrors _host_is_allowed(): configured extras win; otherwise
// the host must be a literal IP inside the private allowlist. Bare DNS
// names are rejected, and v4-mapped v6 literals take the v6 path exactly
// like Python's ipaddress (any colon in the hostname means v6).
func hostAllowed(hostname string, extras []string) bool {
	if hostname == "" {
		return false
	}
	lowered := strings.ToLower(hostname)
	for _, extra := range extras {
		if lowered == strings.ToLower(strings.TrimSpace(extra)) {
			return true
		}
	}
	ip := net.ParseIP(hostname)
	if ip == nil {
		return false
	}
	if strings.Contains(hostname, ":") {
		return ip.Equal(v6Loopback)
	}
	v4 := ip.To4()
	if v4 == nil {
		return false
	}
	for _, network := range allowedV4 {
		if network.Contains(v4) {
			return true
		}
	}
	return false
}

// IsAllowed mirrors is_allowed_watch_url(): true when raw is a viewer URL
// safe to render as a platform card.
func IsAllowed(raw string, extras []string) bool {
	if raw == "" {
		return false
	}
	for _, r := range raw {
		if unicode.IsSpace(r) {
			return false
		}
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return false
	}
	if parsed.Scheme != "http" && parsed.Scheme != "https" {
		return false
	}
	if parsed.User != nil {
		return false
	}
	decodedPath := parsed.Path
	if strings.Contains(decodedPath, "..") || decodedPath != "/" {
		return false
	}
	if parsed.RawQuery != "" {
		return false
	}
	// Go's parser drops a bare trailing "#" like urlparse does, so test the
	// raw URL: a present-but-empty fragment must fail the selector guard.
	if hash := strings.IndexByte(raw, '#'); hash >= 0 {
		if !selectorOK(raw[hash+1:]) {
			return false
		}
	}
	return hostAllowed(parsed.Hostname(), extras)
}
