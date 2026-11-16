package cursorcloud

import (
	"strings"
	"testing"
)

// refusalMessage renders the wrapped API error the way
// tools.cursor_agent_tool._http_request raises it, with every fragment
// variable so the table can mutate one part at a time.
func refusalMessage(branch, repo, status, code, verb string) string {
	return "Cursor Cloud Agent API error (" + verb + "): " + status +
		`: {"code":"` + code + `","message":"Failed to verify existence of branch ` +
		branch + ` in repository ` + repo + `"}` + "\n"
}

// observedSymptom is the exact wrapped body from the field report that
// motivated the failover.
const observedSymptom = "Cursor Cloud Agent API error (POST /v1/agents): HTTP 400: " +
	`{"code":"validation_error","message":"Failed to verify existence of branch ` +
	"'feature/DEV-029' in repository filnos/monolith.\"}"

func TestMatchPrivateOrgBranchVerifyRefusal(t *testing.T) {
	defaults := func() (branch, repo, status, code, verb string) {
		return "'feature/DEV-029'", "filnos/monolith.", "HTTP 400", "validation_error", "POST /v1/agents"
	}
	// with applies each non-empty override to the default fragments.
	with := func(overrides map[string]string, use func(branch, repo, status, code, verb string)) {
		branch, repo, status, code, verb := defaults()
		for key, value := range overrides {
			switch key {
			case "branch":
				branch = value
			case "repo":
				repo = value
			case "status":
				status = value
			case "code":
				code = value
			case "verb":
				verb = value
			}
		}
		use(branch, repo, status, code, verb)
	}

	cases := []struct {
		name      string
		overrides map[string]string
		message   string
		want      bool
	}{
		{
			name:    "observed wrapped body with trailing period",
			message: observedSymptom,
			want:    true,
		},
		{
			name:      "slash branch name",
			overrides: map[string]string{"branch": "'release/2.0-hotfix'"},
			want:      true,
		},
		{
			name:      "deeply nested slash branch name",
			overrides: map[string]string{"branch": "'feature/DEV-029/sub-task'"},
			want:      true,
		},
		{
			name:      "double quotes around branch",
			overrides: map[string]string{"branch": `"feature/DEV-029"`},
			want:      true,
		},
		{
			name:      "repo without trailing period",
			overrides: map[string]string{"repo": "filnos/monolith"},
			want:      true,
		},
		{
			name:      "owner and repo with dots underscores dashes",
			overrides: map[string]string{"repo": "acme.dev/mono_lith-2"},
			want:      true,
		},
		{
			name:      "single character branch and repo",
			overrides: map[string]string{"branch": "'x'", "repo": "a/b"},
			want:      true,
		},
		{
			name:      "uppercase owner and repo",
			overrides: map[string]string{"repo": "Filnos/Monolith"},
			want:      true,
		},
		{
			name:      "branch containing a space",
			overrides: map[string]string{"branch": "'wip feature'"},
			want:      true,
		},
		{
			name:      "empty single quotes",
			overrides: map[string]string{"branch": "''"},
			want:      false,
		},
		{
			name:      "empty double quotes",
			overrides: map[string]string{"branch": `""`},
			want:      false,
		},
		{
			name:      "mixed quote pair single open double close",
			overrides: map[string]string{"branch": `'feature/DEV-029"`},
			want:      false,
		},
		{
			name:      "mixed quote pair double open single close",
			overrides: map[string]string{"branch": `"feature/DEV-029'`},
			want:      false,
		},
		{
			name:      "empty repo after slash",
			overrides: map[string]string{"repo": "filnos/"},
			want:      false,
		},
		{
			name:      "space inside owner",
			overrides: map[string]string{"repo": "fil nos/monolith"},
			want:      false,
		},
		{
			name: "other 400 validation_error model id invalid",
			message: "Cursor Cloud Agent API error (POST /v1/agents): HTTP 400: " +
				`{"code":"validation_error","message":"model id is invalid"}`,
			want: false,
		},
		{
			name:      "http 500 carrying the branch sentence",
			overrides: map[string]string{"status": "HTTP 500"},
			want:      false,
		},
		{
			name:      "http 409 conflict",
			overrides: map[string]string{"status": "HTTP 409"},
			want:      false,
		},
		{
			name:      "missing validation_error code",
			overrides: map[string]string{"code": "internal_error"},
			want:      false,
		},
		{
			name:      "missing POST /v1/agents marker",
			overrides: map[string]string{"verb": "GET /v1/agents"},
			want:      false,
		},
		{
			name:      "missing HTTP status marker",
			overrides: map[string]string{"status": "undocumented"},
			want:      false,
		},
		{
			name:    "empty message",
			message: "",
			want:    false,
		},
		{
			name:    "whitespace only message",
			message: "   \n\t ",
			want:    false,
		},
		{
			name:    "bare sentence without the API wrapper",
			message: "Failed to verify existence of branch 'main' in repository filnos/monolith",
			want:    false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			message := tc.message
			if message == "" && tc.overrides != nil {
				with(tc.overrides, func(branch, repo, status, code, verb string) {
					message = refusalMessage(branch, repo, status, code, verb)
				})
			}
			if got := MatchPrivateOrgBranchVerifyRefusal(message); got != tc.want {
				t.Fatalf("MatchPrivateOrgBranchVerifyRefusal(%q) = %v, want %v",
					message, got, tc.want)
			}
		})
	}
}

func TestMatchPrivateOrgBranchVerifyRefusalBareSentenceWithMarkers(t *testing.T) {
	// The sentence alone is not enough; all three markers must be present.
	// The matcher searches anywhere in the message, so the sentence may sit
	// outside the JSON body as long as the markers are present too.
	message := "HTTP 400 validation_error on POST /v1/agents: " +
		"Failed to verify existence of branch 'feature/DEV-029' in repository filnos/monolith."
	if !MatchPrivateOrgBranchVerifyRefusal(message) {
		t.Fatalf("sentence with all three markers outside JSON body should match: %q", message)
	}
}

func TestClaudeUnavailable(t *testing.T) {
	cases := []struct {
		name string
		json string
		want bool
	}{
		{
			name: "successful run",
			json: `{"success": true, "error": null, "final_report": "landed the fix"}`,
			want: false,
		},
		{
			name: "failed run with ordinary error",
			json: `{"success": false, "error": "tests failed", "final_report": ""}`,
			want: false,
		},
		{
			name: "failed run with null error",
			json: `{"success": false, "error": null, "final_report": ""}`,
			want: false,
		},
		{
			name: "failed run with non string error",
			json: `{"success": false, "error": 17, "final_report": ""}`,
			want: false,
		},
		{
			name: "wrapper binary not found",
			json: `{"success": false, "error": "wrapper binary not found", "final_report": ""}`,
			want: true,
		},
		{
			name: "full GLM wrapper missing message",
			json: `{"success": false, "error": "Claude Code (GLM) wrapper binary not found. Install the wrapper.", "final_report": ""}`,
			want: true,
		},
		{
			name: "success true even with binary text in error",
			json: `{"success": true, "error": "mentions wrapper binary not found in prose"}`,
			want: false,
		},
		{
			name: "not JSON at all",
			json: `delegate crashed before producing JSON`,
			want: true,
		},
		{
			name: "JSON array is not an object",
			json: `["success"]`,
			want: true,
		},
		{
			name: "JSON null is not an object",
			json: `null`,
			want: true,
		},
		{
			name: "missing success field",
			json: `{"error": null, "final_report": "shape drift"}`,
			want: true,
		},
		{
			name: "success as string instead of bool",
			json: `{"success": "true", "error": null}`,
			want: true,
		},
		{
			name: "empty string",
			json: ``,
			want: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ClaudeUnavailable(tc.json); got != tc.want {
				t.Fatalf("ClaudeUnavailable(%q) = %v, want %v", tc.json, got, tc.want)
			}
		})
	}
}

func TestFormatUnavailableError(t *testing.T) {
	const cursorError = "Cursor Cloud Agent API error (POST /v1/agents): HTTP 400: " +
		`{"code":"validation_error","message":"Failed to verify existence of branch ` +
		"'feature/DEV-029' in repository filnos/monolith.\"}"

	t.Run("appends the phrase once separated by one space", func(t *testing.T) {
		got := FormatUnavailableError(cursorError)
		if !strings.HasPrefix(got, cursorError) {
			t.Fatalf("original must be the prefix, got %q", got)
		}
		if !strings.HasSuffix(got, " "+DoNotRetryStartingRef) {
			t.Fatalf("must end with one space and the phrase, got %q", got)
		}
		if n := strings.Count(got, DoNotRetryStartingRef); n != 1 {
			t.Fatalf("phrase must appear exactly once, got %d in %q", n, got)
		}
		if strings.Contains(got, "  "+DoNotRetryStartingRef) {
			t.Fatalf("phrase must be separated by exactly one space, got %q", got)
		}
	})
	t.Run("already carrying the phrase stays unchanged", func(t *testing.T) {
		prefixed := cursorError + " " + DoNotRetryStartingRef
		got := FormatUnavailableError(prefixed)
		if got != prefixed {
			t.Fatalf("idempotent format changed the text: %q", got)
		}
		if n := strings.Count(got, DoNotRetryStartingRef); n != 1 {
			t.Fatalf("phrase must appear exactly once, got %d", n)
		}
	})
}

func TestClaudeFallbackNoteIsExact(t *testing.T) {
	// The facade asserts this string verbatim; a drift here breaks the
	// consumer-side contract tests, so pin it as a constant relationship.
	if ClaudeFallbackNote != "Cursor refused the private-org branch and Claude ran" {
		t.Fatalf("ClaudeFallbackNote drifted: %q", ClaudeFallbackNote)
	}
	if DoNotRetryStartingRef != "do not retry this startingRef" {
		t.Fatalf("DoNotRetryStartingRef drifted: %q", DoNotRetryStartingRef)
	}
}
