package moaroster

import (
	"reflect"
	"testing"
)

// indexes flattens a slot slice to its Index values so expectations stay short.
func indexes(slots []Slot) []int {
	out := make([]int, len(slots))
	for i, s := range slots {
		out[i] = s.Index
	}
	return out
}

// Interleaved statuses: responded slots must rise to the front while every
// other status (known, empty, or unrecognized) keeps its input order.
func TestSuccessesFirstInterleaved(t *testing.T) {
	slots := []Slot{
		{Index: 0, Status: "failed"},
		{Index: 1, Status: "responded"},
		{Index: 2, Status: "waiting"},
		{Index: 3, Status: "responded"},
		{Index: 4, Status: "skipped"},
		{Index: 5, Status: "failed"},
		{Index: 6, Status: ""},
		{Index: 7, Status: "unparsed"},
		{Index: 8, Status: "future-status"},
	}
	want := []int{1, 3, 0, 2, 4, 5, 6, 7, 8}
	if got := indexes(SuccessesFirst(slots)); !reflect.DeepEqual(got, want) {
		t.Errorf("interleaved order = %v, want %v", got, want)
	}
}

// 24 failed slots followed by 6 successes: the first 24 of the result are
// the 6 successes (indexes 24-29) followed by failed indexes 0-17; the
// remainder is failed indexes 18-23.
func TestSuccessesFirstFailedThenSuccess(t *testing.T) {
	slots := make([]Slot, 0, 30)
	for i := range 24 {
		slots = append(slots, Slot{Index: i, Status: "failed"})
	}
	for i := 24; i < 30; i++ {
		slots = append(slots, Slot{Index: i, Status: "responded"})
	}

	got := indexes(SuccessesFirst(slots))
	if len(got) != 30 {
		t.Fatalf("result length = %d, want 30", len(got))
	}
	head := got[:24]
	wantHead := []int{24, 25, 26, 27, 28, 29, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}
	if !reflect.DeepEqual(head, wantHead) {
		t.Errorf("first 24 = %v, want %v", head, wantHead)
	}
	if tail := got[24:]; !reflect.DeepEqual(tail, []int{18, 19, 20, 21, 22, 23}) {
		t.Errorf("tail = %v, want [18 19 20 21 22 23]", tail)
	}
}

// Edge cases: nil in → nil out, single slots pass through, an all-success
// roster keeps its input order, and an empty slice stays empty.
func TestSuccessesFirstEdges(t *testing.T) {
	if got := SuccessesFirst(nil); got != nil {
		t.Errorf("nil input returned %#v, want nil", got)
	}

	single := []Slot{{Index: 7, Status: "responded"}}
	if got := SuccessesFirst(single); !reflect.DeepEqual(got, single) {
		t.Errorf("single responded = %v, want %v", got, single)
	}

	singleFailed := []Slot{{Index: 4, Status: "failed"}}
	if got := SuccessesFirst(singleFailed); !reflect.DeepEqual(got, singleFailed) {
		t.Errorf("single failed = %v, want %v", got, singleFailed)
	}

	allSuccess := []Slot{
		{Index: 2, Status: "responded"},
		{Index: 0, Status: "responded"},
		{Index: 1, Status: "responded"},
	}
	if got := SuccessesFirst(allSuccess); !reflect.DeepEqual(got, allSuccess) {
		t.Errorf("all-success = %v, want %v", got, allSuccess)
	}

	if got := SuccessesFirst([]Slot{}); len(got) != 0 {
		t.Errorf("empty input returned %v, want empty", got)
	}
}

// The caller's slice must be untouched after ordering.
func TestSuccessesFirstDoesNotMutateInput(t *testing.T) {
	slots := []Slot{
		{Index: 0, Status: "failed"},
		{Index: 1, Status: "responded"},
		{Index: 2, Status: "responded"},
		{Index: 3, Status: "waiting"},
	}
	before := append([]Slot(nil), slots...)

	SuccessesFirst(slots)

	if !reflect.DeepEqual(slots, before) {
		t.Errorf("input mutated: got %v, want %v", slots, before)
	}
}
