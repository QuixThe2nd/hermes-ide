package forcebg

import (
	"strings"
	"testing"
	"time"
)

// AwaitRelease is the twin of tools.delegate_tool_forcebg.wait_until_released;
// these tests pin the loop's three exits and the refusal non-exit.

func TestAwaitReleaseReturnsOnSuccessfulAdoption(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	w := reg.Register(&Run{Parent: agent, AdoptID: "d1"})
	if w == nil {
		t.Fatal("wait should register")
	}
	settled := make(chan struct{})
	done := make(chan string, 1)
	go func() { done <- AwaitRelease(w, settled, 10*time.Second) }()
	out := reg.SteerForcingBackground(agent, "go")
	if !out.Accepted || len(out.IDs) != 1 {
		t.Fatalf("steer should adopt one wait: %+v", out)
	}
	select {
	case verdict := <-done:
		if verdict != "released" {
			t.Fatalf("verdict = %q, want released", verdict)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("AwaitRelease did not return after a successful adoption")
	}
}

func TestAwaitReleaseReturnsOnSettledWorker(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	w := reg.Register(&Run{Parent: agent, AdoptID: "d1"})
	settled := make(chan struct{})
	done := make(chan string, 1)
	go func() { done <- AwaitRelease(w, settled, 10*time.Second) }()
	close(settled)
	select {
	case verdict := <-done:
		if verdict != "settled" {
			t.Fatalf("verdict = %q, want settled", verdict)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("AwaitRelease did not return after the worker settled")
	}
}

func TestAwaitReleaseHonorsTheConfiguredCap(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	w := reg.Register(&Run{Parent: agent, AdoptID: "d1"})
	settled := make(chan struct{}) // never closes
	start := time.Now()
	verdict := AwaitRelease(w, settled, 40*time.Millisecond)
	if verdict != "timeout" {
		t.Fatalf("verdict = %q, want timeout", verdict)
	}
	if elapsed := time.Since(start); elapsed < 30*time.Millisecond {
		t.Fatalf("cap returned too early: %v", elapsed)
	}
}

func TestAwaitReleaseWaitsOutARailRefusal(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	w := reg.Register(&Run{Parent: agent}) // AdoptID "" = rail refuses
	settled := make(chan struct{})
	done := make(chan string, 1)
	// A short cap proves the loop did NOT exit on the refusal wake itself.
	go func() { done <- AwaitRelease(w, settled, 150*time.Millisecond) }()
	time.Sleep(20 * time.Millisecond)
	out := reg.SteerForcingBackground(agent, "nudge")
	if !out.Accepted || len(out.IDs) != 0 {
		t.Fatalf("refusal must adopt nothing: %+v", out)
	}
	select {
	case verdict := <-done:
		if verdict != "timeout" {
			t.Fatalf("refusal wake must not end the wait, got %q", verdict)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the cap must still end a refused wait")
	}
}

func TestAwaitReleaseNoCapBlocksUntilSettled(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	w := reg.Register(&Run{Parent: agent, AdoptID: "d1"})
	settled := make(chan struct{})
	done := make(chan string, 1)
	go func() { done <- AwaitRelease(w, settled, -1) }()
	select {
	case <-done:
		t.Fatal("no-cap wait must block until settle or release")
	case <-time.After(60 * time.Millisecond):
	}
	close(settled)
	select {
	case verdict := <-done:
		if verdict != "settled" {
			t.Fatalf("verdict = %q, want settled", verdict)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("no-cap wait did not return after settle")
	}
}

func TestUnregisterRemovesOnlyThatWait(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	keep := reg.Register(&Run{Parent: agent, AdoptID: "d-keep"})
	drop := reg.Register(&Run{Parent: agent, AdoptID: "d-drop"})
	if reg.LiveCount("p") != 2 {
		t.Fatalf("live = %d, want 2", reg.LiveCount("p"))
	}
	reg.Unregister(drop)
	if reg.LiveCount("p") != 1 {
		t.Fatalf("live after unregister = %d, want 1", reg.LiveCount("p"))
	}
	out := reg.SteerForcingBackground(agent, "go")
	if len(out.IDs) != 1 || out.IDs[0] != "d-keep" {
		t.Fatalf("unregistered wait must not be adopted: %+v", out.IDs)
	}
	if !keep.ReleasedBySteer() {
		t.Fatal("the surviving wait must still release")
	}
}

func TestAgentsAreIsolatedByKey(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	alice := Agent{Key: "alice", HasSteer: true}
	bob := Agent{Key: "bob", HasSteer: true}
	reg.Register(&Run{Parent: alice, AdoptID: "d-alice"})
	reg.Register(&Run{Parent: bob, AdoptID: "d-bob"})
	out := reg.SteerForcingBackground(alice, "only alice's children")
	if len(out.IDs) != 1 || out.IDs[0] != "d-alice" {
		t.Fatalf("a steer must touch only its own agent's waits: %+v", out.IDs)
	}
	if reg.LiveCount("bob") != 1 {
		t.Fatal("bob's wait must be untouched by alice's steer")
	}
}

func TestUnicodeEchoCapCountsRunesLikePython(t *testing.T) {
	// Python caps len() = code points; the twin must cap runes, not bytes,
	// or a unicode-heavy echo truncates at a different visible length.
	segment := "héllo→🌍"                 // 7 runes, 14 bytes
	long := strings.Repeat(segment, 250) // 1750 runes
	got := BusyReceipt([]string{"d1"}, long)
	body := strings.SplitN(got, "```\n", 2)[1]
	body = strings.TrimSuffix(body, "\n```")
	runes := len([]rune(body))
	if runes > InjectedCap+3 {
		t.Fatalf("echo not rune-capped: %d runes", runes)
	}
	if !strings.HasSuffix(body, "...") {
		t.Fatalf("capped echo must end with ellipsis: %q", body[len(body)-10:])
	}
}

func TestReceiptOfExactlyCapRunesKeptWhole(t *testing.T) {
	text := strings.Repeat("é", InjectedCap) // 2 bytes per rune
	got := BusyReceipt([]string{"d1"}, text)
	if strings.Contains(got, "...") {
		t.Fatal("an echo exactly at the cap must not be truncated")
	}
	if !strings.Contains(got, strings.Repeat("é", 64)) {
		t.Fatal("echo body missing")
	}
}
