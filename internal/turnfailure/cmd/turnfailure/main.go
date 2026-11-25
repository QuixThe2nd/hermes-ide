// Command turnfailure is the executable form of the internal/turnfailure
// Go twin: read one shared-table case (a JSON object) on stdin, print the
// twin's answer as a JSON object on stdout. Exit codes: 0 answer, 2 bad
// case/op, 3 contract-shaped refusal (unknown site code). The Python
// contract test (tests/gateway/test_turn_failure_go_contract.py) builds and
// drives it; production Python never shells out to it.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/turnfailure"
)

func main() {
	var c map[string]any
	dec := json.NewDecoder(os.Stdin)
	dec.UseNumber()
	if err := dec.Decode(&c); err != nil {
		fmt.Fprintf(os.Stderr, "bad case json: %v\n", err)
		os.Exit(2)
	}
	answer, err := answer(c)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(3)
	}
	out, err := json.Marshal(answer)
	if err != nil {
		fmt.Fprintf(os.Stderr, "marshal answer: %v\n", err)
		os.Exit(2)
	}
	fmt.Println(string(out))
}

func answer(c map[string]any) (map[string]any, error) {
	str := func(k string) string {
		v, _ := c[k].(string)
		return v
	}
	boolean := func(k string) bool {
		v, _ := c[k].(bool)
		return v
	}
	num := func(k string) json.Number {
		v, _ := c[k].(json.Number)
		return v
	}
	floatPtr := func(k string) *float64 {
		v, ok := c[k].(json.Number)
		if !ok {
			return nil
		}
		f, err := v.Float64()
		if err != nil {
			return nil
		}
		return &f
	}

	switch op := str("op"); op {
	case "site_code":
		return map[string]any{"member": turnfailure.SiteFailureCodes[str("code")]}, nil

	case "exit_reason_failure":
		verdict := turnfailure.ExitReasonFailure(str("reason"))
		if verdict == nil {
			return nil, nil
		}
		return map[string]any{
			"reason": verdict.Reason, "retryable": verdict.Retryable, "fails_turn": verdict.FailsTurn,
		}, nil

	case "invalid_response_reason":
		return map[string]any{"reason": turnfailure.InvalidResponseFailureReason(c["code"])}, nil

	case "failed_turn_notice":
		rows, _ := c["messages"].([]any)
		msgs := make([]turnfailure.TurnMessage, 0, len(rows))
		for _, row := range rows {
			m, _ := row.(map[string]any)
			role, _ := m["role"].(string)
			calls, _ := m["tool_calls"].(bool)
			msgs = append(msgs, turnfailure.TurnMessage{Role: role, ToolCalls: calls})
		}
		return map[string]any{"notice": turnfailure.FailedTurnNoticeFor(msgs)}, nil
	case "add_failed_turn_notice":
		return map[string]any{"response": turnfailure.AddFailedTurnNotice(str("response"), str("notice"))}, nil

	case "close_failed_turn":
		return map[string]any{"append": turnfailure.CloseFailedTurnBoundary(str("tail_role"))}, nil

	case "interrupt_tool_reason":
		return map[string]any{"tool_reason": turnfailure.InterruptToolReason(str("reason"))}, nil

	case "site_copy":
		fields, _ := c["fields"].(map[string]any)
		copy, err := turnfailure.SiteCopy(str("code"), fields)
		if err != nil {
			return nil, err
		}
		return map[string]any{"copy": copy}, nil

	case "exhausted_copy":
		return map[string]any{"copy": turnfailure.ExhaustedCopy(
			str("reason"), str("label"), num("attempts"), str("summary"), floatPtr("reset_seconds"),
		)}, nil

	case "failure_cause_gloss":
		subject, possessive := "it", "its"
		if v, ok := c["subject"].(string); ok {
			subject = v
		}
		if v, ok := c["possessive"].(string); ok {
			possessive = v
		}
		gloss, ok := turnfailure.FailureCauseGloss(str("reason"), subject, possessive)
		if !ok {
			return map[string]any{"gloss": nil}, nil
		}
		return map[string]any{"gloss": gloss}, nil

	case "stamp_failure":
		stamped := turnfailure.StampFailure(str("reason"), boolean("retryable"))
		return stamped, nil

	default:
		return nil, fmt.Errorf("unknown op %q", op)
	}
}
