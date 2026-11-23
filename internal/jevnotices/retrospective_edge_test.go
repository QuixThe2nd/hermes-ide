package jevnotices

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"unicode/utf8"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

type errorBody struct{}

func (errorBody) Read([]byte) (int, error) { return 0, errors.New("boom") }
func (errorBody) Close() error             { return nil }

func TestRetrospectiveRedirectIsNotFollowed(t *testing.T) {
	var targetCalls atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		targetCalls.Add(1)
		if got := r.Header.Get("Authorization"); got != "" {
			t.Errorf("redirect target saw bearer %q", got)
		}
		fmt.Fprint(w, `{"answers":{"intent":{"choice":"task","confidence":0.9}}}`)
	}))
	defer target.Close()

	var redirectCalls atomic.Int32
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		redirectCalls.Add(1)
		w.Header().Set("Location", target.URL)
		w.WriteHeader(http.StatusFound)
	}))
	defer redirector.Close()

	result := ClassifyRetrospective(context.Background(), DefaultRetrospectiveClient(), redirector.URL, retrospectiveRequest())
	if targetCalls.Load() != 0 {
		t.Fatalf("redirect was followed %d times", targetCalls.Load())
	}
	if redirectCalls.Load() != int32(retrospectiveAttempts) {
		t.Fatalf("redirector calls = %d, want %d", redirectCalls.Load(), retrospectiveAttempts)
	}
	if result.Label != "unknown" || result.Recover || result.Error != "http_302" {
		t.Fatalf("result = %+v", result)
	}
}

func TestRetrospectiveNonSuccessStatusesFailClosed(t *testing.T) {
	for _, status := range []int{http.StatusMovedPermanently, http.StatusBadRequest, http.StatusUnauthorized, http.StatusNotFound, http.StatusTooManyRequests, http.StatusServiceUnavailable} {
		t.Run(fmt.Sprint(status), func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls.Add(1)
				w.WriteHeader(status)
			}))
			defer server.Close()
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, retrospectiveRequest())
			want := fmt.Sprintf("http_%d", status)
			if result.Label != "unknown" || result.Recover || result.Error != want {
				t.Fatalf("result = %+v, want error %q", result, want)
			}
			if calls.Load() != int32(retrospectiveAttempts) {
				t.Fatalf("requests = %d, want %d", calls.Load(), retrospectiveAttempts)
			}
			if result.Notice != "```\n🔍 Retrospective Intent: failed ("+want+")\n```" {
				t.Fatalf("notice = %q", result.Notice)
			}
		})
	}
}

func TestRetrospectiveOversizedResponseFailsClosed(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, strings.Repeat(" ", retrospectiveBodyLimit+1))
	}))
	defer server.Close()
	result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, retrospectiveRequest())
	if result.Error != "response_too_large" {
		t.Fatalf("result = %+v", result)
	}
	if calls.Load() != int32(retrospectiveAttempts) {
		t.Fatalf("requests = %d, want %d", calls.Load(), retrospectiveAttempts)
	}
}

func TestRetrospectiveUnreadableResponseBodyFailsClosed(t *testing.T) {
	var calls atomic.Int32
	client := &http.Client{Transport: roundTripFunc(func(_ *http.Request) (*http.Response, error) {
		calls.Add(1)
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: errorBody{}}, nil
	})}
	result := ClassifyRetrospective(context.Background(), client, "http://decisions.invalid/jev/decisions", retrospectiveRequest())
	if result.Error != "response_read" {
		t.Fatalf("result = %+v", result)
	}
	if calls.Load() != int32(retrospectiveAttempts) {
		t.Fatalf("round trips = %d, want %d", calls.Load(), retrospectiveAttempts)
	}
}

func TestRetrospectiveTransportErrorFailsClosed(t *testing.T) {
	var calls atomic.Int32
	client := &http.Client{Transport: roundTripFunc(func(_ *http.Request) (*http.Response, error) {
		calls.Add(1)
		return nil, errors.New("dial refused")
	})}
	result := ClassifyRetrospective(context.Background(), client, "http://decisions.invalid/jev/decisions", retrospectiveRequest())
	if result.Label != "unknown" || result.Recover || result.Error != "transport_error" {
		t.Fatalf("result = %+v", result)
	}
	if calls.Load() != int32(retrospectiveAttempts) {
		t.Fatalf("round trips = %d, want %d", calls.Load(), retrospectiveAttempts)
	}
}

func TestRetrospectiveCancelledContextFailsClosed(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"answers":{"intent":{"choice":"task","confidence":0.9}}}`)
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	result := ClassifyRetrospective(ctx, server.Client(), server.URL, retrospectiveRequest())
	if result.Error != "transport_error" {
		t.Fatalf("result = %+v", result)
	}
	if calls.Load() != 0 {
		t.Fatalf("cancelled context still delivered %d requests", calls.Load())
	}
}

func TestRetrospectiveSchemaMismatchVariants(t *testing.T) {
	bodies := []struct {
		name string
		body string
	}{
		{"answers null", `{"answers":null}`},
		{"intent not an object", `{"answers":{"intent":5}}`},
		{"intent null", `{"answers":{"intent":null}}`},
		{"intent a string", `{"answers":{"intent":"task"}}`},
		{"choice missing", `{"answers":{"intent":{"confidence":0.9}}}`},
		{"choice null", `{"answers":{"intent":{"choice":null,"confidence":0.9}}}`},
		{"choice numeric", `{"answers":{"intent":{"choice":3,"confidence":0.9}}}`},
		{"choice boolean", `{"answers":{"intent":{"choice":true,"confidence":0.9}}}`},
		{"confidence missing", `{"answers":{"intent":{"choice":"task"}}}`},
		{"confidence null", `{"answers":{"intent":{"choice":"task","confidence":null}}}`},
		{"confidence a string", `{"answers":{"intent":{"choice":"task","confidence":"high"}}}`},
		{"confidence negative", `{"answers":{"intent":{"choice":"task","confidence":-0.1}}}`},
		{"label empty", `{"answers":{"intent":{"choice":"","confidence":0.9}}}`},
		{"label unknown word", `{"answers":{"intent":{"choice":"other","confidence":0.9}}}`},
	}
	for _, test := range bodies {
		t.Run(test.name, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls.Add(1)
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, retrospectiveRequest())
			if result.Label != "unknown" || result.Recover || result.Error != "schema_mismatch" {
				t.Fatalf("result = %+v", result)
			}
			if calls.Load() != int32(retrospectiveAttempts) {
				t.Fatalf("requests = %d, want %d", calls.Load(), retrospectiveAttempts)
			}
		})
	}
}

func TestRetrospectiveChoiceIsNormalized(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"answers":{"intent":{"choice":"  TASK  ","confidence":0.9}}}`)
	}))
	defer server.Close()
	request := retrospectiveRequest()
	request.InitialLabel = "unknown"
	result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request)
	if result.Label != "task" || result.Error != "" {
		t.Fatalf("result = %+v", result)
	}
	if result.Comparison != "changed" {
		t.Fatalf("comparison = %q", result.Comparison)
	}
	if !result.Recover {
		t.Fatal("normalized task after unknown did not recover")
	}
}

func TestRetrospectiveResultConfidenceBoundaries(t *testing.T) {
	for _, confidence := range []float64{0, 1} {
		t.Run(fmt.Sprint(confidence), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				fmt.Fprintf(w, `{"answers":{"intent":{"choice":"chat","confidence":%v}}}`, confidence)
			}))
			defer server.Close()
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, retrospectiveRequest())
			if result.Error != "" || result.Confidence != confidence {
				t.Fatalf("result = %+v", result)
			}
		})
	}
}

func TestRetrospectiveRetrySucceedsOnSecondAttempt(t *testing.T) {
	tests := []struct {
		name  string
		first string
	}{
		{"http error", "500"},
		{"invalid JSON", "{"},
		{"schema mismatch", `{"answers":{}}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				if calls.Add(1) == 1 {
					if test.first == "500" {
						w.WriteHeader(http.StatusInternalServerError)
						return
					}
					fmt.Fprint(w, test.first)
					return
				}
				fmt.Fprint(w, `{"answers":{"intent":{"choice":"chase","confidence":0.7}}}`)
			}))
			defer server.Close()
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, retrospectiveRequest())
			if calls.Load() != 2 {
				t.Fatalf("requests = %d, want 2", calls.Load())
			}
			if result.Label != "chase" || result.Confidence != 0.7 || result.Error != "" {
				t.Fatalf("result = %+v", result)
			}
			if result.Comparison != "changed" {
				t.Fatalf("comparison = %q", result.Comparison)
			}
			if result.Recover {
				t.Fatal("chase after chat must not recover")
			}
		})
	}
}

func TestRetrospectiveConsistentComparisonThroughFullPath(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprint(w, `{"answers":{"intent":{"choice":"chat","confidence":0.9}}}`)
	}))
	defer server.Close()
	result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, retrospectiveRequest())
	if result.Comparison != "consistent" {
		t.Fatalf("comparison = %q", result.Comparison)
	}
	if result.Recover {
		t.Fatal("consistent chat verdict must not recover")
	}
}

func TestRetrospectiveRecoveryBoundaryThroughFullPath(t *testing.T) {
	tests := []struct {
		name       string
		initial    string
		confidence float64
		want       bool
	}{
		{"chat task at 0.6 floor", "chat", 0.6, true},
		{"unknown task at 0.6 floor", "unknown", 0.6, true},
		{"chat task just below floor", "chat", 0.5999, false},
		{"chat task at max", "chat", 1, true},
		{"task task at max stays handled", "task", 1, false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				fmt.Fprintf(w, `{"answers":{"intent":{"choice":"task","confidence":%v}}}`, test.confidence)
			}))
			defer server.Close()
			request := retrospectiveRequest()
			request.InitialLabel = test.initial
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request)
			if result.Error != "" {
				t.Fatalf("result = %+v", result)
			}
			if result.Recover != test.want {
				t.Fatalf("recover = %v, want %v", result.Recover, test.want)
			}
		})
	}
}

func TestRetrospectiveTruncatesExchangeToLimits(t *testing.T) {
	tests := []struct {
		name         string
		message      string
		response     string
		wantMessage  int
		wantResponse int
	}{
		{
			name:         "overlong ASCII",
			message:      strings.Repeat("m", retrospectiveMessageLimit+500),
			response:     strings.Repeat("r", retrospectiveResponseLimit+500),
			wantMessage:  retrospectiveMessageLimit,
			wantResponse: retrospectiveResponseLimit,
		},
		{
			name:         "multibyte beyond rune limit truncates at rune boundary",
			message:      strings.Repeat("€", retrospectiveMessageLimit+10),
			response:     strings.Repeat("界", retrospectiveResponseLimit+10),
			wantMessage:  retrospectiveMessageLimit,
			wantResponse: retrospectiveResponseLimit,
		},
		{
			name:         "multibyte within rune limit is kept whole",
			message:      strings.Repeat("€", retrospectiveMessageLimit),
			response:     strings.Repeat("界", retrospectiveResponseLimit),
			wantMessage:  retrospectiveMessageLimit,
			wantResponse: retrospectiveResponseLimit,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var payload map[string]any
				if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
					t.Fatalf("request JSON: %v", err)
				}
				state, _ := payload["state"].(map[string]any)
				message, _ := state["message"].(string)
				response, _ := state["assistant_response"].(string)
				if got := utf8.RuneCountInString(message); got != test.wantMessage {
					t.Errorf("message runes = %d, want %d", got, test.wantMessage)
				}
				if got := utf8.RuneCountInString(response); got != test.wantResponse {
					t.Errorf("response runes = %d, want %d", got, test.wantResponse)
				}
				if !utf8.ValidString(message) || !utf8.ValidString(response) {
					t.Error("truncation split a UTF-8 sequence")
				}
				fmt.Fprint(w, `{"answers":{"intent":{"choice":"chat","confidence":0.5}}}`)
			}))
			defer server.Close()
			request := retrospectiveRequest()
			request.Message = test.message
			request.FinalResponse = test.response
			if result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request); result.Error != "" {
				t.Fatalf("result = %+v", result)
			}
		})
	}
}

func TestRetrospectiveInitialConfidenceBoundaries(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		fmt.Fprint(w, `{"answers":{"intent":{"choice":"chat","confidence":0.5}}}`)
	}))
	defer server.Close()

	for _, confidence := range []float64{0, 1} {
		request := retrospectiveRequest()
		request.InitialConfidence = confidence
		result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request)
		if result.Error != "" {
			t.Fatalf("initial confidence %v: result = %+v", confidence, result)
		}
	}
	if calls.Load() != 2 {
		t.Fatalf("boundary confidences made %d requests, want 2", calls.Load())
	}

	calls.Store(0)
	for _, confidence := range []float64{-0.1, 1.1, math.NaN(), math.Inf(1), math.Inf(-1)} {
		request := retrospectiveRequest()
		request.InitialConfidence = confidence
		result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request)
		if result.Label != "unknown" || result.Recover || result.Error != "invalid_initial_confidence" {
			t.Fatalf("initial confidence %v: result = %+v", confidence, result)
		}
	}
	if calls.Load() != 0 {
		t.Fatalf("invalid confidences made %d requests", calls.Load())
	}
}

func TestRetrospectiveInitialLabelIsNormalized(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("request JSON: %v", err)
		}
		state, _ := payload["state"].(map[string]any)
		if state["initial_label"] != "chat" {
			t.Errorf("initial_label = %#v, want normalized chat", state["initial_label"])
		}
		fmt.Fprint(w, `{"answers":{"intent":{"choice":"chat","confidence":0.9}}}`)
	}))
	defer server.Close()
	request := retrospectiveRequest()
	request.InitialLabel = "  CHAT  "
	result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request)
	if result.Error != "" || result.Comparison != "consistent" {
		t.Fatalf("result = %+v", result)
	}
}

func TestRetrospectiveTransportGuardsMakeNoRequest(t *testing.T) {
	tests := []struct {
		name     string
		client   *http.Client
		endpoint string
	}{
		{"nil client", nil, "http://decisions.invalid/jev/decisions"},
		{"empty endpoint", DefaultRetrospectiveClient(), ""},
		{"both missing", nil, ""},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			result := ClassifyRetrospective(context.Background(), test.client, test.endpoint, retrospectiveRequest())
			if result.Label != "unknown" || result.Recover || result.Error != "invalid_transport" {
				t.Fatalf("result = %+v", result)
			}
			if result.Notice != "```\n🔍 Retrospective Intent: failed (invalid_transport)\n```" {
				t.Fatalf("notice = %q", result.Notice)
			}
		})
	}
}

func TestRetrospectiveInvalidInputErrorClasses(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
	}))
	defer server.Close()
	tests := []struct {
		name   string
		mutate func(*RetrospectiveRequest)
		want   string
	}{
		{"empty message", func(r *RetrospectiveRequest) { r.Message = " " }, "empty_message"},
		{"empty response", func(r *RetrospectiveRequest) { r.FinalResponse = "\t\n" }, "empty_response"},
		{"missing credential", func(r *RetrospectiveRequest) { r.Credential = "" }, "missing_credential"},
		{"unknown initial label", func(r *RetrospectiveRequest) { r.InitialLabel = "bogus" }, "invalid_initial_label"},
		{"empty initial label", func(r *RetrospectiveRequest) { r.InitialLabel = "" }, "invalid_initial_label"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := retrospectiveRequest()
			test.mutate(&request)
			result := ClassifyRetrospective(context.Background(), server.Client(), server.URL, request)
			if result.Label != "unknown" || result.Comparison != "unavailable" || result.Recover || result.Error != test.want {
				t.Fatalf("result = %+v, want error %q", result, test.want)
			}
			if result.Notice != "```\n🔍 Retrospective Intent: failed ("+test.want+")\n```" {
				t.Fatalf("notice = %q", result.Notice)
			}
		})
	}
	if calls.Load() != 0 {
		t.Fatalf("invalid inputs made %d requests", calls.Load())
	}
}

func TestDefaultRetrospectiveClientBoundsAttempts(t *testing.T) {
	client := DefaultRetrospectiveClient()
	if client == nil {
		t.Fatal("DefaultRetrospectiveClient returned nil")
	}
	if client.Timeout <= 0 {
		t.Fatalf("timeout = %v, want a positive bound", client.Timeout)
	}
	if client.CheckRedirect == nil {
		t.Fatal("CheckRedirect is nil; bearer could follow redirects")
	}
	if err := client.CheckRedirect(nil, nil); !errors.Is(err, http.ErrUseLastResponse) {
		t.Fatalf("CheckRedirect = %v, want ErrUseLastResponse", err)
	}
}
