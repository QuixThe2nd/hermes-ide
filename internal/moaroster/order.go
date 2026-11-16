// Package moaroster holds ordering helpers for mixture-of-agents roster
// slots. It is deliberately dependency-free so it can be vendored or tested
// in isolation.
package moaroster

import "sort"

// StatusResponded marks a roster slot whose model has already answered.
// Every other status (failed, waiting, skipped, unparsed, ...) is treated
// as not-yet-successful.
const StatusResponded = "responded"

// Slot identifies one member of a roster by position and its last known
// response status.
type Slot struct {
	Index  int
	Status string
}

// SuccessesFirst returns a new slice with all "responded" slots ordered
// before every other status. Relative order within each group is preserved
// (stable). The input slice is never mutated; a nil slice returns nil.
func SuccessesFirst(slots []Slot) []Slot {
	if slots == nil {
		return nil
	}
	ordered := make([]Slot, len(slots))
	copy(ordered, slots)
	sort.SliceStable(ordered, func(i, j int) bool {
		ri, rj := ordered[i].Status == StatusResponded, ordered[j].Status == StatusResponded
		return ri && !rj
	})
	return ordered
}
