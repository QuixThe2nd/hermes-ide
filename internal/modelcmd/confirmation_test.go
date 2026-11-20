package modelcmd

import "testing"

// TestRenderConfirmationSharedTable runs every shared-table record through
// RenderConfirmation and compares the exact reply text.
func TestRenderConfirmationSharedTable(t *testing.T) {
	for _, tc := range loadTable(t).Confirmation {
		if got := RenderConfirmation(tc.Record); got != tc.Want {
			t.Errorf("%s:\n got:\n%s\nwant:\n%s", tc.Name, got, tc.Want)
		}
	}
}

// TestGroupThousands pins the digit grouping to Python's f"{n:,}" — the
// twin formats tokens identically, so the contract test compares the
// rendered text byte for byte.
func TestGroupThousands(t *testing.T) {
	cases := map[int64]string{
		0: "0", 7: "7", 999: "999", 1000: "1,000",
		272000: "272,000", 1000000: "1,000,000", 1234567: "1,234,567",
		-272000: "-272,000",
	}
	for n, want := range cases {
		if got := groupThousands(n); got != want {
			t.Errorf("groupThousands(%d) = %q, want %q", n, got, want)
		}
	}
}

// TestRenderConfirmationZeroTokensHidden: an unknown context length must
// omit the context line, not render "Context: 0 tokens".
func TestRenderConfirmationZeroTokensHidden(t *testing.T) {
	out := RenderConfirmation(ConfirmationRecord{Model: "m", Provider: "p"})
	if contains(out, "Context:") {
		t.Errorf("zero-token record rendered a context line:\n%s", out)
	}
}
