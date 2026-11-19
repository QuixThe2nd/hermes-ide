package hubframe

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strings"
	"testing"
)

// sharedTable loads testdata/hubframe_cases.json — the same file the Python
// contract test reads — so both sides run the identical contract.
func sharedTable(t *testing.T) (streamLimit int, frames []struct {
	Name        string `json:"name"`
	ContentByte int    `json:"content_bytes"`
	Accepted    bool   `json:"accepted"`
}) {
	t.Helper()
	raw, err := os.ReadFile("testdata/hubframe_cases.json")
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		StreamLimitBytes int `json:"stream_limit_bytes"`
		Frames           []struct {
			Name        string `json:"name"`
			ContentByte int    `json:"content_bytes"`
			Accepted    bool   `json:"accepted"`
		} `json:"frames"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	return table.StreamLimitBytes, table.Frames
}

// The compiled constant must equal the shared table's pinned value — this
// is the link that makes Python == table == Go.
func TestStreamLimitMatchesSharedTable(t *testing.T) {
	limit, _ := sharedTable(t)
	if int(StreamLimit) != limit {
		t.Fatalf("StreamLimit = %d, shared table pins %d", StreamLimit, limit)
	}
}

// Table-driven frame reads: every shared case must be accepted or rejected
// exactly as the table says, and accepted content must round-trip whole.
func TestReadFrameMatchesSharedTable(t *testing.T) {
	_, frames := sharedTable(t)
	for _, tc := range frames {
		t.Run(tc.Name, func(t *testing.T) {
			content := strings.Repeat("a", tc.ContentByte)
			wire := content + "\n"
			got, err := ReadFrame(bufio.NewReader(strings.NewReader(wire)))
			if tc.Accepted {
				if err != nil {
					t.Fatalf("frame of %d bytes rejected: %v", tc.ContentByte, err)
				}
				if len(got) != tc.ContentByte {
					t.Fatalf("content round-trip: got %d bytes, want %d", len(got), tc.ContentByte)
				}
				if tc.ContentByte > 0 && got[len(got)-1] != 'a' {
					t.Fatalf("content corrupted at tail")
				}
			} else {
				if !errors.Is(err, ErrFrameTooLarge) {
					t.Fatalf("frame of %d bytes: err = %v, want ErrFrameTooLarge", tc.ContentByte, err)
				}
			}
		})
	}
}

// The shared table must keep covering the contract's reason to exist: a
// frame over asyncio's 64 KiB default is accepted, the at-limit frame is
// accepted, and an over-limit frame is rejected.
func TestSharedTableCoversTheLimitContract(t *testing.T) {
	limit, frames := sharedTable(t)
	byName := map[string]int{}
	for _, tc := range frames {
		byName[tc.Name] = tc.ContentByte
	}
	if n, ok := byName["just_over_the_64kib_asyncio_default"]; !ok || n <= 65536 {
		t.Fatalf("table lost the >64 KiB accepted case (got %d, present %v)", n, ok)
	}
	if n, ok := byName["content_exactly_at_stream_limit"]; !ok || n != limit {
		t.Fatalf("table lost the at-limit accepted case (got %d, want %d)", n, limit)
	}
	if n, ok := byName["content_one_byte_over_stream_limit"]; !ok || n != limit+1 {
		t.Fatalf("table lost the over-limit rejected case (got %d, want %d)", n, limit+1)
	}
}

// An oversized frame followed by a well-formed one is rejected at the
// oversized frame itself — the reader never treats the excess as the next
// frame's content.
func TestReadFrameRejectsOversizedFrame(t *testing.T) {
	var wire bytes.Buffer
	wire.WriteString(strings.Repeat("b", StreamLimit+1))
	wire.WriteString("\n{\"id\":\"next\"}\n")
	got, err := ReadFrame(bufio.NewReaderSize(&wire, 4096))
	if !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("err = %v (%d bytes), want ErrFrameTooLarge", err, len(got))
	}
}

// Consecutive frames on one reader stay framed: content boundaries land on
// the newlines, not the bufio buffer boundaries.
func TestReadFrameKeepsSequentialFraming(t *testing.T) {
	first := strings.Repeat("x", 4096+17) // spans the default bufio buffer
	second := `{"type":"control"}`
	var wire bytes.Buffer
	wire.WriteString(first)
	wire.WriteString("\n")
	wire.WriteString(second)
	wire.WriteString("\n")
	r := bufio.NewReaderSize(&wire, 4096)
	got1, err := ReadFrame(r)
	if err != nil {
		t.Fatalf("first frame: %v", err)
	}
	if string(got1) != first {
		t.Fatalf("first frame round-trip failed: %d bytes", len(got1))
	}
	got2, err := ReadFrame(r)
	if err != nil {
		t.Fatalf("second frame: %v", err)
	}
	if string(got2) != second {
		t.Fatalf("second frame round-trip failed: %q", got2)
	}
	if _, err := ReadFrame(r); !errors.Is(err, io.EOF) {
		t.Fatalf("exhausted reader: err = %v, want io.EOF", err)
	}
}

// A final frame without a trailing newline is still delivered (asyncio
// readline yields the partial tail); empty input is a clean EOF.
func TestReadFrameTrailingPartialAndCleanEOF(t *testing.T) {
	got, err := ReadFrame(bufio.NewReader(strings.NewReader("tail-without-newline")))
	if err != nil || string(got) != "tail-without-newline" {
		t.Fatalf("partial tail: got %q, err = %v", got, err)
	}
	if _, err := ReadFrame(bufio.NewReader(strings.NewReader(""))); !errors.Is(err, io.EOF) {
		t.Fatalf("empty input: err = %v, want io.EOF", err)
	}
	// An over-limit partial tail is still rejected, not returned.
	if _, err := ReadFrame(bufio.NewReader(strings.NewReader(strings.Repeat("c", StreamLimit+1)))); !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("oversized partial tail: err = %v, want ErrFrameTooLarge", err)
	}
}
