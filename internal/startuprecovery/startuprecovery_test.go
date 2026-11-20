package startuprecovery

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// The asking process's PID in table rows; "dead" and "live_other" owners
// use other PIDs, with ownerLive taken from the row.
const me = 4242

type sharedCase struct {
	Name        string    `json:"name"`
	CleanMarker bool      `json:"clean_marker"`
	Sentinel    *Sentinel `json:"-"`
	RawSentinel *struct {
		Phase            string `json:"phase"`
		Owner            string `json:"owner"`
		PriorUncleanExit bool   `json:"prior_unclean_exit"`
	} `json:"sentinel"`
	ExpectUnclean bool `json:"expect_ledger_unclean"`
	ExpectMode    Mode `json:"expect_mode"`
}

func loadCases(t *testing.T) []sharedCase {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "cases.json"))
	if err != nil {
		t.Fatalf("read shared table: %v", err)
	}
	var table struct {
		Cases []sharedCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &table); err != nil {
		t.Fatalf("parse shared table: %v", err)
	}
	if len(table.Cases) == 0 {
		t.Fatal("shared table has no cases")
	}
	return table.Cases
}

// resolve maps a row's symbolic owner onto (sentinel, ownerLive).
func resolve(t *testing.T, tc sharedCase) (*Sentinel, bool) {
	t.Helper()
	if tc.RawSentinel == nil {
		return nil, false
	}
	s := &Sentinel{Phase: tc.RawSentinel.Phase, PriorUncleanExit: tc.RawSentinel.PriorUncleanExit}
	switch tc.RawSentinel.Owner {
	case "me":
		s.PID = me
		return s, true
	case "live_other":
		s.PID = me + 1
		return s, true
	case "dead":
		s.PID = me + 2
		return s, false
	default:
		t.Fatalf("case %q: unknown owner %q", tc.Name, tc.RawSentinel.Owner)
		return nil, false
	}
}

func TestSharedTable(t *testing.T) {
	for _, tc := range loadCases(t) {
		t.Run(tc.Name, func(t *testing.T) {
			sentinel, ownerLive := resolve(t, tc)
			unclean := LedgerUnclean(sentinel, me, ownerLive)
			if unclean != tc.ExpectUnclean {
				t.Fatalf("LedgerUnclean = %t, want %t", unclean, tc.ExpectUnclean)
			}
			if got := Decide(tc.CleanMarker, unclean); got != tc.ExpectMode {
				t.Fatalf("Decide(clean_marker=%t, unclean=%t) = %q, want %q",
					tc.CleanMarker, unclean, got, tc.ExpectMode)
			}
		})
	}
}

// TestUncleanLedgerAlwaysRecovers is the auto-resume bug: a clean marker
// left by an overlap predecessor must not discard the turns of a successor
// the ledger saw die.
func TestUncleanLedgerAlwaysRecovers(t *testing.T) {
	for _, cleanMarker := range []bool{false, true} {
		if got := Decide(cleanMarker, true); got != ModeRecover {
			t.Fatalf("Decide(clean_marker=%t, unclean=true) = %q, want %q", cleanMarker, got, ModeRecover)
		}
	}
}

// TestCleanLedgerKeepsMarkerVerdict pins that the ledger only ever adds
// recovery: with a clean or absent ledger the marker alone decides.
func TestCleanLedgerKeepsMarkerVerdict(t *testing.T) {
	want := map[bool]Mode{true: ModeDiscard, false: ModeRecover}
	for cleanMarker, mode := range want {
		if got := Decide(cleanMarker, false); got != mode {
			t.Fatalf("Decide(clean_marker=%t, unclean=false) = %q, want %q", cleanMarker, got, mode)
		}
	}
}

// TestOwnClaimIsAuthoritative pins that once this boot claimed the sentinel,
// only the stamped prior verdict counts. The claim itself is phase
// "running" and must never read as this process's own unclean death.
func TestOwnClaimIsAuthoritative(t *testing.T) {
	for _, prior := range []bool{false, true} {
		for _, ownerLive := range []bool{false, true} {
			s := &Sentinel{Phase: "running", PID: me, PriorUncleanExit: prior}
			if got := LedgerUnclean(s, me, ownerLive); got != prior {
				t.Fatalf("own claim prior=%t ownerLive=%t: got %t", prior, ownerLive, got)
			}
		}
	}
}

// TestLiveOwnerIsHandoverNotDeath pins the pre-claim rule: a running
// sentinel whose owner still lives (a draining predecessor) is a handover
// in flight, and an exited sentinel is clean whoever owns it.
func TestLiveOwnerIsHandoverNotDeath(t *testing.T) {
	if LedgerUnclean(&Sentinel{Phase: "running", PID: me + 1}, me, true) {
		t.Fatal("live predecessor's running sentinel read as unclean")
	}
	for _, ownerLive := range []bool{false, true} {
		if LedgerUnclean(&Sentinel{Phase: "exited", PID: me + 1}, me, ownerLive) {
			t.Fatalf("exited sentinel read as unclean (ownerLive=%t)", ownerLive)
		}
	}
	if LedgerUnclean(nil, me, false) {
		t.Fatal("absent sentinel read as unclean")
	}
}
