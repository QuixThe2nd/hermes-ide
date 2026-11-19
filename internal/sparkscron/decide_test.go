package sparkscron

import "testing"

// decideOf is a helper wrapping Decide for table brevity.
func decideOf(names []string, seeded, enabled, prereq bool) Verdict {
	return Decide(Inputs{
		JobNames: names, Seeded: seeded, CronEnabled: enabled, PrereqOK: prereq,
	})
}

// TestDecide walks the full create/skip table in gate order. The Python
// bridge executes exactly this verdict, so every row here is a seeding
// behavior contract (mirrored end-to-end by
// tests/plugins/hermes_starts/test_sparks_cron.py).
func TestDecide(t *testing.T) {
	tests := []struct {
		name       string
		in         Inputs
		wantAction string
		wantReason string
	}{
		{
			name:       "fresh install with token: creates",
			in:         Inputs{JobNames: []string{"other"}, CronEnabled: true, PrereqOK: true},
			wantAction: ActionCreate,
		},
		{
			name:       "empty store: creates",
			in:         Inputs{CronEnabled: true, PrereqOK: true},
			wantAction: ActionCreate,
		},
		{
			name:       "no token (prereq fails): skips before anything else",
			in:         Inputs{CronEnabled: true, PrereqOK: false},
			wantAction: ActionSkip,
			wantReason: ReasonPrereqMissing,
		},
		{
			name: "config opt-out wins over a fresh empty store",
			in: Inputs{
				JobNames: nil, Seeded: false, CronEnabled: false, PrereqOK: true,
			},
			wantAction: ActionSkip,
			wantReason: ReasonDisabledByConfig,
		},
		{
			name: "config opt-out wins even after a deletion",
			in: Inputs{
				JobNames: nil, Seeded: true, CronEnabled: false, PrereqOK: true,
			},
			wantAction: ActionSkip,
			wantReason: ReasonDisabledByConfig,
		},
		{
			name: "existing job (disabled records included): never touched",
			in: Inputs{
				JobNames: []string{"daily-report", "inbox-sparks"}, CronEnabled: true, PrereqOK: true,
			},
			wantAction: ActionSkip,
			wantReason: ReasonJobExists,
		},
		{
			name: "existing job beats the deletion marker",
			in: Inputs{
				JobNames: []string{"inbox-sparks"}, Seeded: true, CronEnabled: true, PrereqOK: true,
			},
			wantAction: ActionSkip,
			wantReason: ReasonJobExists,
		},
		{
			name: "two jobs with the name: ambiguous, no guess",
			in: Inputs{
				JobNames:    []string{"inbox-sparks", "other", "inbox-sparks"},
				CronEnabled: true, PrereqOK: true,
			},
			wantAction: ActionSkip,
			wantReason: ReasonJobAmbiguous,
		},
		{
			name: "marker with no job: user deleted ours, stay deleted",
			in: Inputs{
				JobNames: []string{"other"}, Seeded: true, CronEnabled: true, PrereqOK: true,
			},
			wantAction: ActionSkip,
			wantReason: ReasonDeletedByUser,
		},
		{
			name: "name matching is exact, not substring",
			in: Inputs{
				JobNames:    []string{"inbox-sparks-extra", "INBOX-SPARKS"},
				CronEnabled: true, PrereqOK: true,
			},
			wantAction: ActionCreate,
		},
		{
			name: "plugins.disabled opt-out: no job even on a fresh store",
			in: Inputs{
				PluginDisabled: true, CronEnabled: true, PrereqOK: true,
			},
			wantAction: ActionSkip,
			wantReason: ReasonPluginDisabled,
		},
		{
			name: "plugins.disabled opt-out wins over an existing job",
			in: Inputs{
				JobNames: []string{"inbox-sparks"}, Seeded: true,
				PluginDisabled: true, CronEnabled: true, PrereqOK: true,
			},
			wantAction: ActionSkip,
			wantReason: ReasonPluginDisabled,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Decide(tt.in)
			if got.Action != tt.wantAction {
				t.Fatalf("Decide() action = %q, want %q (verdict %+v)", got.Action, tt.wantAction, got)
			}
			if got.Reason != tt.wantReason {
				t.Fatalf("Decide() reason = %q, want %q", got.Reason, tt.wantReason)
			}
			if tt.wantAction == ActionCreate {
				if got.Job == nil {
					t.Fatalf("create verdict must carry the job spec")
				}
				if err := got.Job.Validate(); err != nil {
					t.Fatalf("create verdict spec invalid: %v", err)
				}
				if got.Job.Name != JobName {
					t.Fatalf("create verdict name = %q, want %q", got.Job.Name, JobName)
				}
			} else if got.Job != nil {
				t.Fatalf("skip verdict must not carry a job spec, got %+v", got.Job)
			}
		})
	}
}

// TestGateOrder pins the precedence itself: for the same otherwise-creating
// inputs, each earlier gate overrides the later ones.
func TestGateOrder(t *testing.T) {
	if got := decideOf(nil, true, false, false); got.Reason != ReasonPrereqMissing {
		t.Errorf("prereq must dominate: got %q", got.Reason)
	}
	if got := decideOf(nil, true, false, true); got.Reason != ReasonDisabledByConfig {
		t.Errorf("config must dominate marker: got %q", got.Reason)
	}
	if got := decideOf([]string{"inbox-sparks", "inbox-sparks"}, true, true, true); got.Reason != ReasonJobAmbiguous {
		t.Errorf("ambiguity must dominate marker: got %q", got.Reason)
	}
	if got := decideOf([]string{"inbox-sparks"}, true, true, true); got.Reason != ReasonJobExists {
		t.Errorf("existence must dominate marker: got %q", got.Reason)
	}
	if got := decideOf([]string{"x"}, true, true, true); got.Reason != ReasonDeletedByUser {
		t.Errorf("marker must be the last skip gate: got %q", got.Reason)
	}
}

// TestVerdictJSONShape: the wire contract the Python bridge parses — action
// and reason are always present strings; job appears only on create.
func TestVerdictJSONShape(t *testing.T) {
	// Marshaled field coverage is exercised end-to-end in main_test.go's
	// round-trip; here pin the skip shape carries no spec pointer.
	v := Decide(Inputs{CronEnabled: true, PrereqOK: true, JobNames: []string{"inbox-sparks"}})
	if v.Job != nil || v.Action != ActionSkip || v.Reason == "" {
		t.Fatalf("skip verdict shape violated: %+v", v)
	}
}

// TestPluginDisabledGate: “plugins.disabled“ is the home's strongest
// statement about the wake subsystem — when it names hermes_starts or
// inbox_sparks, no other fact (missing token, sparks_cron flag, store
// state, deletion marker) may produce a job record, and the verdict must
// say which gate fired.
func TestPluginDisabledGate(t *testing.T) {
	tests := []struct {
		name string
		in   Inputs
	}{
		{
			name: "dominates a missing prerequisite",
			in:   Inputs{PluginDisabled: true, PrereqOK: false},
		},
		{
			name: "dominates the sparks_cron opt-out",
			in:   Inputs{PluginDisabled: true, CronEnabled: false, PrereqOK: true},
		},
		{
			name: "dominates an ambiguous store",
			in: Inputs{
				JobNames:       []string{"inbox-sparks", "inbox-sparks"},
				PluginDisabled: true, CronEnabled: true, PrereqOK: true,
			},
		},
		{
			name: "dominates an existing job",
			in: Inputs{
				JobNames:       []string{"inbox-sparks"},
				PluginDisabled: true, CronEnabled: true, PrereqOK: true,
			},
		},
		{
			name: "dominates the deletion marker on an empty store",
			in: Inputs{
				Seeded: true, PluginDisabled: true, CronEnabled: true, PrereqOK: true,
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Decide(tt.in)
			if got.Action != ActionSkip || got.Reason != ReasonPluginDisabled || got.Job != nil {
				t.Fatalf("Decide() = %+v, want skip/%s", got, ReasonPluginDisabled)
			}
		})
	}

	// The field is additive on the wire: an old bridge payload (no
	// plugin_disabled key) decodes to false and decides exactly as before.
	if got := Decide(Inputs{CronEnabled: true, PrereqOK: true}); got.Action != ActionCreate {
		t.Fatalf("zero-value plugin_disabled must not change the verdict, got %+v", got)
	}
}
