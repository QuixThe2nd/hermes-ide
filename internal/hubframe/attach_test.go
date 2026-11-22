package hubframe

import (
	"bufio"
	"errors"
	"reflect"
	"strings"
	"testing"
)

func readAttachParams(t *testing.T, wire string) (AttachParams, error) {
	t.Helper()
	frame := mustReadFrame(t, bufio.NewReader(strings.NewReader(wire)))
	return DecodeAttachParams(frame)
}

func TestDecodeAttachParamsPreservesOwningProfileReloadScope(t *testing.T) {
	params, err := readAttachParams(t, `{"generation_id":"generation-winnie","profile":"winnie",`+
		`"profile_routes":[],"config":{"token":"not-read-by-this-decoder"},`+
		`"gate_credential":"winnie-judge-key","reaction_gate_reload":{`+
		`"config_path":"  /srv/hermes/profiles/winnie/config.yaml  ",`+
		`"managed_dir":"/srv/hermes/profiles/winnie/managed","managed_env":{`+
		`"LITELLM_API_KEY":"winnie-judge-key","WANDB_API_KEY":"winnie-wandb-key"}}}`+"\n")
	if err != nil {
		t.Fatalf("DecodeAttachParams: %v", err)
	}
	if params.GenerationID != "generation-winnie" || params.Profile != "winnie" {
		t.Fatalf("owner identity changed: generation=%q profile=%q", params.GenerationID, params.Profile)
	}
	scope := params.ReactionGateReload
	if scope == nil {
		t.Fatal("reaction_gate_reload scope was dropped")
	}
	if scope.ConfigPath != "/srv/hermes/profiles/winnie/config.yaml" {
		t.Fatalf("config path = %q; want the trimmed owning-profile path", scope.ConfigPath)
	}
	if scope.ManagedDir == nil || *scope.ManagedDir != "/srv/hermes/profiles/winnie/managed" {
		t.Fatalf("managed dir = %v; want owning-profile managed dir", scope.ManagedDir)
	}
	wantEnv := map[string]string{
		"LITELLM_API_KEY": "winnie-judge-key",
		"WANDB_API_KEY":   "winnie-wandb-key",
	}
	if !reflect.DeepEqual(scope.ManagedEnv, wantEnv) {
		t.Fatalf("managed env = %#v; want %#v", scope.ManagedEnv, wantEnv)
	}
}

func TestDecodeAttachParamsAcceptsClientsWithoutReloadScope(t *testing.T) {
	for _, tc := range []struct {
		name string
		wire string
	}{
		{name: "older_client_omits_field", wire: `{"generation_id":"old","profile":"default"}`},
		{name: "explicit_null", wire: `{"generation_id":"null","reaction_gate_reload":null}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			params, err := readAttachParams(t, tc.wire+"\n")
			if err != nil {
				t.Fatalf("DecodeAttachParams: %v", err)
			}
			if params.ReactionGateReload != nil {
				t.Fatalf("scope = %#v; want nil for the legacy local-capture path", params.ReactionGateReload)
			}
		})
	}
}

func TestDecodeAttachParamsPreservesAbsentManagedScope(t *testing.T) {
	params, err := readAttachParams(t, `{"generation_id":"unmanaged","reaction_gate_reload":{`+
		`"config_path":"/profiles/winnie/config.yaml","managed_dir":null,"managed_env":null}}`+"\n")
	if err != nil {
		t.Fatalf("DecodeAttachParams: %v", err)
	}
	scope := params.ReactionGateReload
	if scope == nil {
		t.Fatal("reaction_gate_reload scope was dropped")
	}
	if scope.ManagedDir != nil || scope.ManagedEnv != nil {
		t.Fatalf("managed scope = dir %v, env %#v; want nil, nil", scope.ManagedDir, scope.ManagedEnv)
	}
}

func TestDecodeAttachParamsPreservesEmptyManagedEnvironment(t *testing.T) {
	params, err := readAttachParams(t, `{"reaction_gate_reload":{`+
		`"config_path":"/profiles/winnie/config.yaml","managed_env":{}}}`+"\n")
	if err != nil {
		t.Fatalf("DecodeAttachParams: %v", err)
	}
	scope := params.ReactionGateReload
	if scope == nil {
		t.Fatal("reaction_gate_reload scope was dropped")
	}
	if scope.ManagedEnv == nil || len(scope.ManagedEnv) != 0 {
		t.Fatalf("managed env = %#v; want a present, empty map", scope.ManagedEnv)
	}
}

func TestDecodeAttachParamsRejectsMissingConfigPath(t *testing.T) {
	for _, tc := range []struct {
		name  string
		scope string
	}{
		{name: "missing", scope: `{}`},
		{name: "null", scope: `{"config_path":null}`},
		{name: "empty", scope: `{"config_path":""}`},
		{name: "whitespace", scope: `{"config_path":" \t "}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			wire := `{"reaction_gate_reload":` + tc.scope + `}` + "\n"
			_, err := readAttachParams(t, wire)
			if !errors.Is(err, ErrReactionGateConfigPath) {
				t.Fatalf("error = %v; want ErrReactionGateConfigPath", err)
			}
		})
	}
}

func TestDecodeAttachParamsRejectsMalformedScope(t *testing.T) {
	for _, tc := range []struct {
		name string
		wire string
	}{
		{name: "malformed_json", wire: `{"reaction_gate_reload":`},
		{name: "scope_is_array", wire: `{"reaction_gate_reload":[]}`},
		{name: "config_path_is_number", wire: `{"reaction_gate_reload":{"config_path":42}}`},
		{name: "managed_dir_is_number", wire: `{"reaction_gate_reload":{"config_path":"/winnie/config.yaml","managed_dir":42}}`},
		{name: "managed_env_is_array", wire: `{"reaction_gate_reload":{"config_path":"/winnie/config.yaml","managed_env":[]}}`},
		{name: "managed_env_value_is_number", wire: `{"reaction_gate_reload":{"config_path":"/winnie/config.yaml","managed_env":{"LITELLM_API_KEY":42}}}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := readAttachParams(t, tc.wire+"\n"); err == nil {
				t.Fatal("DecodeAttachParams accepted malformed input")
			}
		})
	}
}

func TestDecodeAttachParamsKeepsFollowingFrame(t *testing.T) {
	wire := `{"generation_id":"first","reaction_gate_reload":{"config_path":"/winnie/config.yaml"}}` + "\n" +
		`{"generation_id":"second","reaction_gate_reload":null}` + "\n"
	r := bufio.NewReader(strings.NewReader(wire))
	first, err := DecodeAttachParams(mustReadFrame(t, r))
	if err != nil {
		t.Fatalf("first attach: %v", err)
	}
	second, err := DecodeAttachParams(mustReadFrame(t, r))
	if err != nil {
		t.Fatalf("second attach: %v", err)
	}
	if first.GenerationID != "first" || first.ReactionGateReload == nil {
		t.Fatalf("first attach = %#v", first)
	}
	if second.GenerationID != "second" || second.ReactionGateReload != nil {
		t.Fatalf("second attach = %#v", second)
	}
}

func TestShouldAdoptReactionGateScope(t *testing.T) {
	explicitScope := &ReactionGateReloadScope{ConfigPath: "/profiles/winnie/config.yaml"}
	for _, tc := range []struct {
		name            string
		currentProfile  string
		incomingProfile string
		explicitScope   bool
		want            bool
	}{
		{
			name:            "legacy_attach_cannot_rebind",
			currentProfile:  "default",
			incomingProfile: "Winnie",
			explicitScope:   false,
			want:            false,
		},
		{
			name:            "default_refreshes_default",
			currentProfile:  "default",
			incomingProfile: "default",
			explicitScope:   true,
			want:            true,
		},
		{
			name:            "empty_legacy_owner_is_default",
			currentProfile:  "",
			incomingProfile: "default",
			explicitScope:   true,
			want:            true,
		},
		{
			name:            "named_adopts_default",
			currentProfile:  "default",
			incomingProfile: "Winnie",
			explicitScope:   true,
			want:            true,
		},
		{
			name:            "named_adopts_legacy_default",
			currentProfile:  "",
			incomingProfile: "Winnie",
			explicitScope:   true,
			want:            true,
		},
		{
			name:            "same_named_profile_refreshes",
			currentProfile:  "Winnie",
			incomingProfile: "Winnie",
			explicitScope:   true,
			want:            true,
		},
		{
			name:            "default_cannot_replace_named",
			currentProfile:  "Winnie",
			incomingProfile: "default",
			explicitScope:   true,
			want:            false,
		},
		{
			name:            "sibling_cannot_replace_named",
			currentProfile:  "Winnie",
			incomingProfile: "Other",
			explicitScope:   true,
			want:            false,
		},
		{
			name:            "blank_incoming_name_is_default",
			currentProfile:  "Winnie",
			incomingProfile: "  ",
			explicitScope:   true,
			want:            false,
		},
		{
			name:            "profile_names_are_trimmed",
			currentProfile:  " Winnie ",
			incomingProfile: "Winnie",
			explicitScope:   true,
			want:            true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var scope *ReactionGateReloadScope
			if tc.explicitScope {
				scope = explicitScope
			}
			got := ShouldAdoptReactionGateScope(tc.currentProfile, tc.incomingProfile, scope)
			if got != tc.want {
				t.Fatalf("ShouldAdoptReactionGateScope(%q, %q, scope=%v) = %v, want %v",
					tc.currentProfile, tc.incomingProfile, tc.explicitScope, got, tc.want)
			}
		})
	}
}

func mustReadFrame(t *testing.T, r *bufio.Reader) []byte {
	t.Helper()
	frame, err := ReadFrame(r)
	if err != nil {
		t.Fatalf("ReadFrame: %v", err)
	}
	return frame
}
