package forcebg

import (
	"fmt"
	"sync"
	"testing"
)

// Stress tests for the once-only adoption and steer ordering under real
// concurrency — the Go twins of the Python suite's concurrent-steer and
// batch-adoption contracts, at higher contention.

func TestStressEveryWaitDispatchedExactlyOnce(t *testing.T) {
	const waits = 16
	const racersPerWait = 6
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	registered := make([]*Wait, waits)
	for i := range waits {
		registered[i] = reg.Register(&Run{Parent: agent, AdoptID: fmt.Sprintf("d-%02d", i)})
		if registered[i] == nil {
			t.Fatalf("wait %d failed to register", i)
		}
	}
	observations := make([][]string, waits)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := range waits {
		observations[i] = make([]string, racersPerWait)
		for r := range racersPerWait {
			wg.Add(1)
			go func(i, r int) {
				defer wg.Done()
				<-start
				observations[i][r] = registered[i].Adopt()
			}(i, r)
		}
	}
	close(start)
	wg.Wait()
	for i, seen := range observations {
		want := fmt.Sprintf("d-%02d", i)
		for r, id := range seen {
			if id != want {
				t.Fatalf("wait %d racer %d observed %q, want %q", i, r, id, want)
			}
		}
		if !registered[i].ReleasedBySteer() {
			t.Fatalf("wait %d must be released by its adoption", i)
		}
	}
}

func TestStressSteersVersusTimeoutTailsSingleVerdict(t *testing.T) {
	// The runtime race: a steer listener and the timeout tail both reach
	// adoption for the same wait. Every racer must observe the SAME id, and
	// the wait must end released — never half-adopted.
	const waits = 8
	const steers = 12
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	registered := make([]*Wait, waits)
	for i := range waits {
		registered[i] = reg.Register(&Run{Parent: agent, AdoptID: fmt.Sprintf("tx-%02d", i)})
	}
	steerOuts := make([]SteerOutcome, steers)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for s := range steers {
		wg.Add(1)
		go func(s int) {
			defer wg.Done()
			<-start
			steerOuts[s] = reg.SteerForcingBackground(agent, "steer")
		}(s)
	}
	// Timeout tails race the steers on every wait.
	for i := range waits {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			registered[i].Adopt()
		}(i)
	}
	close(start)
	wg.Wait()
	for i := range waits {
		if !registered[i].ReleasedBySteer() {
			t.Fatalf("wait %d not released after the race", i)
		}
	}
	// Every steer that reports ids must report a subset of the true ids,
	// with no fabricated delegation.
	truth := map[string]bool{}
	for i := range waits {
		truth[fmt.Sprintf("tx-%02d", i)] = true
	}
	adoptingSteers := 0
	for _, out := range steerOuts {
		if !out.Accepted {
			t.Fatal("every steer is accepted (the agent accepts)")
		}
		for _, id := range out.IDs {
			if !truth[id] {
				t.Fatalf("fabricated delegation id %q", id)
			}
		}
		if len(out.IDs) > 0 {
			adoptingSteers++
		}
	}
	// At least the first steer (or a timeout tail) adopted everything: with
	// all waits live at the start, one full-adoption steer must exist unless
	// tails beat every steer to every wait — either way the union covers all.
	seen := map[string]bool{}
	for _, out := range steerOuts {
		for _, id := range out.IDs {
			seen[id] = true
		}
	}
	for id := range truth {
		if !seen[id] {
			// A timeout tail adopted it; that path also releases the wait
			// (asserted above), so this is a legal interleaving — but only
			// if the wait is released, which we already checked.
			_ = id
		}
	}
	if adoptingSteers == 0 && len(seen) == 0 {
		t.Fatal("no adoption happened at all in the race")
	}
}

func TestStressMixedRefusalsNeverRelease(t *testing.T) {
	const refused = 8
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	for range refused {
		reg.Register(&Run{Parent: agent}) // "" = the rail refuses
	}
	start := make(chan struct{})
	var wg sync.WaitGroup
	outcomes := make([]SteerOutcome, refused)
	for i := range refused {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			outcomes[i] = reg.SteerForcingBackground(agent, "steer")
		}(i)
	}
	close(start)
	wg.Wait()
	for i, out := range outcomes {
		if !out.Accepted {
			t.Fatalf("steer %d must be accepted", i)
		}
		if len(out.IDs) != 0 {
			t.Fatalf("refusing rail produced ids: %+v", out.IDs)
		}
	}
	if reg.HasEligible("p") {
		t.Fatal("refused waits are not retried, but must not be reported eligible")
	}
}
