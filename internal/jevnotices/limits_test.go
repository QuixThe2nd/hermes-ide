package jevnotices

import (
	"strings"
	"testing"
)

func TestBoundariesAreTheDocumentedConstants(t *testing.T) {
	if maxTokenLen != 48 {
		t.Errorf("maxTokenLen = %d, want 48", maxTokenLen)
	}
	if maxAuthorLen != 80 {
		t.Errorf("maxAuthorLen = %d, want 80", maxAuthorLen)
	}
	if Prefix != "🔎 jev " {
		t.Errorf("Prefix = %q", Prefix)
	}
}

func TestSanitizeTokenAtExactCap(t *testing.T) {
	atCap := strings.Repeat("a", maxTokenLen)
	if got := SanitizeToken(atCap); got != atCap {
		t.Errorf("token at cap changed: %d runes", len(got))
	}
	over := strings.Repeat("a", maxTokenLen+1)
	if got := SanitizeToken(over); len(got) != maxTokenLen {
		t.Errorf("token over cap = %d runes, want %d", len(got), maxTokenLen)
	}
}

func TestSanitizeAuthorAtExactCap(t *testing.T) {
	atCap := strings.Repeat("b", maxAuthorLen)
	if got := SanitizeAuthor(atCap); got != atCap {
		t.Errorf("author at cap changed: %d runes", len(got))
	}
	over := strings.Repeat("b", maxAuthorLen+1)
	if got := SanitizeAuthor(over); len(got) != maxAuthorLen {
		t.Errorf("author over cap = %d runes, want %d", len(got), maxAuthorLen)
	}
}
