package jevnotices

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
)

func retrospectiveRequest() RetrospectiveRequest {
	return RetrospectiveRequest{
		Message:           "why is the proxy down?",
		FinalResponse:     "It is probably a DNS issue.",
		InitialLabel:      "chat",
		InitialConfidence: 0.41,
		Credential:        "scoped-test-key",
	}
}

func TestRetrospectiveClassificationSendsExchangeAndInitialDecision(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if got := r.Header.Get("Authorization"); got != "Bearer scoped-test-key" {
			t.Errorf("Authorization = %q", got)
		}
		if got := r.Header.Get("Content-Type"); got != "application/json" {
			t.Errorf("Content-Type = %q", got)
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("request JSON: %v", err)
		}
		if payload["model"] != JEVModel {
			t.Errorf("model = %#v", payload["model"])
		}
		state, ok := payload["state"].(map[string]any)
		if !ok {
			t.Fatalf("state = %#v", payload["state"])
		}
		for key, want := range map[string]any{
			"message":            "why is the proxy down?",
			"assistant_response": "It is probably a DNS issue.",
			"initial_label":      "chat",
			"initial_confidence": 0.41,
		} {
			if state[key] != want {
				t.Errorf("state[%q] = %#v, want %#v", key, state[key], want)
			}
		}
		questions, ok := payload["questions"].(map[string]any)
		if !ok {
			t.Fatalf("questions = %#v", payload["questions"])
		}
		intent, ok := questions["intent"].(map[string]any)
		if !ok || intent["type"] != "choice" {
			t.Fatalf("intent question = %#v", questions["intent"])
		}
		instructions, _ := intent["instructions"].(string)
		for _, fragment := range []string{"assistant_response", "initial_label", "evidence only"} {
			if !strings.Contains(instructions, fragment) {
				t.Errorf("instructions omit %q", fragment)
			}
		}
		criteria, ok := intent["criteria"].(map[string]any)
		if !ok {
			t.Fatalf("criteria = %#v", intent["criteria"])
		}
		for _, label := range []string{"chat", "task", "approval", "chase"} {
			criterion, ok := criteria[label].(map[string]any)
			if !ok || criterion["what"] == "" {
				t.Errorf("criterion %q = %#v", label, criteria[label])
			}
			if _, forbidden := criterion["options"]; forbidden {
				t.Errorf("criterion %q carries options", label)
			}
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"answers":{"intent":{"choice":"task","confidence":0.93}}}`)
	}))
	defer server.Close()

	result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, retrospectiveRequest())
	if calls.Load() != 1 {
		t.Fatalf("requests = %d", calls.Load())
	}
	if result.Label != "task" || result.Confidence != 0.93 || result.Error != "" {
		t.Fatalf("result = %+v", result)
	}
	if !result.Recover {
		t.Fatal("high-confidence task after chat did not recover")
	}
	if result.Comparison != "changed" {
		t.Fatalf("comparison = %q", result.Comparison)
	}
	if result.Notice != "```\n🔍 Retrospective Intent: Task (0.93)\n```" {
		t.Fatalf("notice = %q", result.Notice)
	}
}

func TestRetrospectiveLabelsAreVisible(t *testing.T) {
	for _, label := range []string{"chat", "task", "approval", "chase"} {
		t.Run(label, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				fmt.Fprintf(w, `{"answers":{"intent":{"choice":%q,"confidence":0.88}}}`, label)
			}))
			defer server.Close()
			request := retrospectiveRequest()
			request.InitialLabel = label
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request)
			if result.Label != label || result.Confidence != 0.88 || result.Error != "" {
				t.Fatalf("result = %+v", result)
			}
			want := "```\n🔍 Retrospective Intent: " + strings.ToUpper(label[:1]) + label[1:] + " (0.88)\n```"
			if result.Notice != want {
				t.Fatalf("notice = %q, want %q", result.Notice, want)
			}
			if result.Comparison != "consistent" {
				t.Fatalf("comparison = %q", result.Comparison)
			}
			if result.Recover {
				t.Fatal("an already-handled initial label recovered again")
			}
		})
	}
}

func TestRetrospectiveRecoveryPolicy(t *testing.T) {
	tests := []struct {
		name       string
		initial    string
		label      string
		confidence float64
		want       bool
	}{
		{"chat task at floor", "chat", "task", 0.6, true},
		{"unknown task", "unknown", "task", 0.95, true},
		{"chat task below floor", "chat", "task", 0.59, false},
		{"chat chat", "chat", "chat", 0.99, false},
		{"chat approval", "chat", "approval", 0.99, false},
		{"chat chase", "chat", "chase", 0.99, false},
		{"handled task", "task", "task", 0.99, false},
		{"handled approval", "approval", "task", 0.99, false},
		{"handled chase", "chase", "task", 0.99, false},
		{"failed verdict", "chat", "unknown", 0, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := retrospectiveRecovery(test.initial, test.label, test.confidence); got != test.want {
				t.Fatalf("retrospectiveRecovery(%q, %q, %v) = %v, want %v", test.initial, test.label, test.confidence, got, test.want)
			}
		})
	}
}

func TestRetrospectiveFailuresRetryOnceAndFailClosed(t *testing.T) {
	tests := []struct {
		name      string
		status    int
		body      string
		wantError string
	}{
		{"HTTP", http.StatusInternalServerError, `{}`, "http_500"},
		{"invalid choice", http.StatusOK, `{"answers":{"intent":{"choice":"other","confidence":0.9}}}`, "schema_mismatch"},
		{"invalid confidence", http.StatusOK, `{"answers":{"intent":{"choice":"task","confidence":1.5}}}`, "schema_mismatch"},
		{"missing answer", http.StatusOK, `{"answers":{}}`, "schema_mismatch"},
		{"invalid JSON", http.StatusOK, `{`, "invalid_json"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls.Add(1)
				w.WriteHeader(test.status)
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, retrospectiveRequest())
			if calls.Load() != 2 {
				t.Fatalf("requests = %d, want 2", calls.Load())
			}
			if result.Label != "unknown" || result.Confidence != 0 || result.Recover || result.Error != test.wantError {
				t.Fatalf("result = %+v", result)
			}
			if result.Notice != "```\n🔍 Retrospective Intent: failed ("+test.wantError+")\n```" {
				t.Fatalf("notice = %q", result.Notice)
			}
		})
	}
}

func TestRetrospectiveInvalidInputMakesNoRequest(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"answers":{"intent":{"choice":"task","confidence":0.9}}}`)
	}))
	defer server.Close()
	tests := []struct {
		name   string
		mutate func(*RetrospectiveRequest)
	}{
		{"empty message", func(r *RetrospectiveRequest) { r.Message = " " }},
		{"empty response", func(r *RetrospectiveRequest) { r.FinalResponse = " " }},
		{"missing credential", func(r *RetrospectiveRequest) { r.Credential = "" }},
		{"unknown initial label", func(r *RetrospectiveRequest) { r.InitialLabel = "bogus" }},
		{"invalid initial confidence", func(r *RetrospectiveRequest) { r.InitialConfidence = 2 }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := retrospectiveRequest()
			test.mutate(&request)
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request)
			if result.Label != "unknown" || result.Recover || result.Error == "" {
				t.Fatalf("result = %+v", result)
			}
		})
	}
	if calls.Load() != 0 {
		t.Fatalf("invalid inputs made %d requests", calls.Load())
	}
}
