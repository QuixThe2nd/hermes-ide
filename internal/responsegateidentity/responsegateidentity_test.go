package responsegateidentity

import (
	"encoding/json"
	"errors"
	"os"
	"slices"
	"testing"
)

type contractTable struct {
	Automatic []struct {
		Name      string        `json:"name"`
		Exercises string        `json:"exercises"`
		User      ConnectedUser `json:"user"`
		Want      string        `json:"want"`
	} `json:"automatic"`
	Configured []struct {
		Name       string        `json:"name"`
		Configured string        `json:"configured"`
		User       ConnectedUser `json:"user"`
		Want       string        `json:"want"`
	} `json:"configured"`
	InvalidOverrides []string `json:"invalid_overrides"`
	Examples         struct {
		Name  string   `json:"name"`
		True  []string `json:"true"`
		False []string `json:"false"`
	} `json:"examples"`
}

func loadContract(t *testing.T) contractTable {
	t.Helper()
	data, err := os.ReadFile("testdata/identity_cases.json")
	if err != nil {
		t.Fatalf("read shared identity contract: %v", err)
	}
	var table contractTable
	if err := json.Unmarshal(data, &table); err != nil {
		t.Fatalf("decode shared identity contract: %v", err)
	}
	return table
}

func stringPointer(value string) *string {
	return &value
}

func mustGate(t *testing.T, override *string) Gate {
	t.Helper()
	gate, err := NewGate(override)
	if err != nil {
		t.Fatalf("NewGate() returned unexpected error: %v", err)
	}
	return gate
}

func assertIdentity(t *testing.T, got Snapshot, wantName string) {
	t.Helper()
	if got.Name != wantName {
		t.Errorf("identity name = %q, want %q", got.Name, wantName)
	}
	wantDirect := "hey " + wantName + " whats up"
	if got.Examples.True[0] != wantDirect {
		t.Errorf("first direct-address example = %q, want %q", got.Examples.True[0], wantDirect)
	}
	wantIndirect := "i saw " + wantName + " in the other channel"
	if got.Examples.False[0] != wantIndirect {
		t.Errorf("first indirect-mention example = %q, want %q", got.Examples.False[0], wantIndirect)
	}
}

func TestAutomaticIdentityMatchesSharedContract(t *testing.T) {
	table := loadContract(t)
	for _, test := range table.Automatic {
		t.Run(test.Name, func(t *testing.T) {
			gate := mustGate(t, nil)
			assertIdentity(t, gate.Connect(), DefaultName)
			assertIdentity(t, gate.Ready(test.User), test.Want)
		})
	}
}

func TestSharedContractCoversEveryAutomaticFallback(t *testing.T) {
	table := loadContract(t)
	covered := make(map[string]bool, len(table.Automatic))
	for _, test := range table.Automatic {
		covered[test.Exercises] = true
	}
	for _, stage := range []string{"display_name", "username", "name", "default"} {
		if !covered[stage] {
			t.Errorf("shared contract no longer exercises automatic fallback stage %q", stage)
		}
	}
}

func TestConfiguredIdentityMatchesSharedContract(t *testing.T) {
	table := loadContract(t)
	for _, test := range table.Configured {
		t.Run(test.Name, func(t *testing.T) {
			gate := mustGate(t, stringPointer(test.Configured))
			assertIdentity(t, gate.Snapshot(), test.Want)
			assertIdentity(t, gate.Connect(), test.Want)
			assertIdentity(t, gate.Ready(test.User), test.Want)
		})
	}
}

func TestInvalidConfiguredNamesAreRejected(t *testing.T) {
	table := loadContract(t)
	for _, invalid := range table.InvalidOverrides {
		t.Run(invalid, func(t *testing.T) {
			_, err := NewGate(stringPointer(invalid))
			if !errors.Is(err, ErrInvalidBotName) {
				t.Fatalf("NewGate(%q) error = %v, want ErrInvalidBotName", invalid, err)
			}
		})
	}
}

func TestNilOverrideIsNotAnInvalidEmptyName(t *testing.T) {
	gate := mustGate(t, nil)
	assertIdentity(t, gate.Snapshot(), DefaultName)
	assertIdentity(t, gate.Ready(ConnectedUser{
		DisplayName: "Connected Display",
		Username:    "connected-user",
		Name:        "connected-name",
	}), "Connected Display")
}

func TestAddressExamplesMatchSharedContract(t *testing.T) {
	table := loadContract(t)
	got := AddressExamplesFor(table.Examples.Name)
	if !slices.Equal(got.True[:], table.Examples.True) {
		t.Errorf("direct-address examples = %#v, want %#v", got.True, table.Examples.True)
	}
	if !slices.Equal(got.False[:], table.Examples.False) {
		t.Errorf("indirect-mention examples = %#v, want %#v", got.False, table.Examples.False)
	}
}

func TestAddressExamplesFollowTheWholeResolvedName(t *testing.T) {
	user := ConnectedUser{
		DisplayName: "  Gate Name  ",
		Username:    "ignored-user",
		Name:        "ignored-name",
	}
	gate := mustGate(t, nil)
	got := gate.Ready(user)
	want := Snapshot{
		Name: "Gate Name",
		Examples: AddressExamples{
			True: [3]string{
				"hey Gate Name whats up",
				"Gate Name can you check this",
				"what do you think Gate Name?",
			},
			False: [3]string{
				"i saw Gate Name in the other channel",
				"the example greeting is 'hi Gate Name whats up'",
				"Gate Name is down again lol",
			},
		},
	}
	if got != want {
		t.Errorf("READY snapshot = %#v, want %#v", got, want)
	}
}

func TestReadyLifecycle(t *testing.T) {
	tests := []struct {
		name         string
		override     *string
		firstUser    ConnectedUser
		secondUser   ConnectedUser
		initial      string
		beforeFirst  string
		afterFirst   string
		beforeSecond string
		afterSecond  string
	}{
		{
			name: "automatic_identity_refreshes_on_first_ready_and_reconnect",
			firstUser: ConnectedUser{
				DisplayName: "First Display",
				Username:    "first-user",
				Name:        "first-name",
			},
			secondUser: ConnectedUser{
				DisplayName: "Second Display",
				Username:    "second-user",
				Name:        "second-name",
			},
			initial:      DefaultName,
			beforeFirst:  DefaultName,
			afterFirst:   "First Display",
			beforeSecond: "First Display",
			afterSecond:  "Second Display",
		},
		{
			name:     "configured_identity_stays_fixed_across_ready_events",
			override: stringPointer("  Configured Override  "),
			firstUser: ConnectedUser{
				DisplayName: "First Display",
				Username:    "first-user",
				Name:        "first-name",
			},
			secondUser: ConnectedUser{
				DisplayName: "Second Display",
				Username:    "second-user",
				Name:        "second-name",
			},
			initial:      "Configured Override",
			beforeFirst:  "Configured Override",
			afterFirst:   "Configured Override",
			beforeSecond: "Configured Override",
			afterSecond:  "Configured Override",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			gate := mustGate(t, test.override)
			assertIdentity(t, gate.Snapshot(), test.initial)

			// Discord has no client user before the first READY. Connect uses
			// the safe default; on reconnect it uses the previous READY user.
			assertIdentity(t, gate.Connect(), test.beforeFirst)
			assertIdentity(t, gate.Ready(test.firstUser), test.afterFirst)
			assertIdentity(t, gate.Connect(), test.beforeSecond)
			assertIdentity(t, gate.Ready(test.secondUser), test.afterSecond)
		})
	}
}

func TestAutomaticReadyCanReturnToDefaultIdentity(t *testing.T) {
	gate := mustGate(t, nil)
	assertIdentity(t, gate.Ready(ConnectedUser{
		DisplayName: "Previously Named",
		Username:    "previous-user",
		Name:        "previous-name",
	}), "Previously Named")

	// Automatic identity is derived afresh from every READY. A blank new
	// user must not remain stuck on the preceding connection's identity.
	assertIdentity(t, gate.Ready(ConnectedUser{}), DefaultName)
	assertIdentity(t, gate.Connect(), DefaultName)
}

func TestRepeatedReadyWithSameAutomaticUserIsStable(t *testing.T) {
	gate := mustGate(t, nil)
	user := ConnectedUser{
		DisplayName: "Stable Display",
		Username:    "stable-user",
		Name:        "stable-name",
	}
	first := gate.Ready(user)
	second := gate.Ready(user)
	if second != first {
		t.Errorf("repeated READY changed snapshot: first=%#v second=%#v", first, second)
	}
	assertIdentity(t, gate.Connect(), "Stable Display")
}

func TestSnapshotDoesNotChangeWhenLaterReadyArrives(t *testing.T) {
	gate := mustGate(t, nil)
	before := gate.Ready(ConnectedUser{DisplayName: "Before"})
	after := gate.Ready(ConnectedUser{DisplayName: "After"})

	assertIdentity(t, before, "Before")
	assertIdentity(t, after, "After")
	assertIdentity(t, gate.Snapshot(), "After")
	if before == after {
		t.Fatal("different READY users produced identical identity snapshots")
	}
}

func TestConfiguredOverrideIsCapturedByValue(t *testing.T) {
	configured := "Before"
	gate := mustGate(t, &configured)
	configured = "After"

	// The constructor retains the validated value, not a pointer whose later
	// reassignment could silently change a configured identity.
	assertIdentity(t, gate.Ready(ConnectedUser{DisplayName: "Connected"}), "Before")
}

func TestAddressExamplesPreserveUnicodeAndDiscordPunctuation(t *testing.T) {
	tests := []struct {
		name         string
		wantDirect   string
		wantIndirect string
	}{
		{
			name:         "Huhn-Bot_2",
			wantDirect:   "hey Huhn-Bot_2 whats up",
			wantIndirect: "i saw Huhn-Bot_2 in the other channel",
		},
		{
			name:         "助手",
			wantDirect:   "hey 助手 whats up",
			wantIndirect: "i saw 助手 in the other channel",
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			got := AddressExamplesFor(test.name)
			if got.True[0] != test.wantDirect {
				t.Errorf("direct example = %q, want %q", got.True[0], test.wantDirect)
			}
			if got.False[0] != test.wantIndirect {
				t.Errorf("indirect example = %q, want %q", got.False[0], test.wantIndirect)
			}
		})
	}
}
