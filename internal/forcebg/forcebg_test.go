package forcebg

import (
	"strings"
	"sync"
	"testing"
)

// every string below must byte-match the Python twin; the shared-table
// contract test (tests/tools/test_forcebg_go_contract.py) proves it end to
// end against the compiled binary, these unit tests pin the invariants.

func TestEntryNoticeNamesDelegationAndNeverStopped(t *testing.T) {
	notice := EntryNotice("deleg_abc123")
	for _, want := range []string{"deleg_abc123", "NOT stopped", "completion message", "steer"} {
		if !strings.Contains(notice, want) {
			t.Fatalf("entry notice missing %q: %s", want, notice)
		}
	}
}

func TestBusyReceiptSingleAndBatch(t *testing.T) {
	one := BusyReceipt([]string{"d1"}, "make it red")
	if !strings.Contains(one, "delegation d1") {
		t.Fatalf("receipt missing id: %s", one)
	}
	if strings.Contains(one, "delegations") {
		t.Fatalf("singular receipt must not claim a plural: %s", one)
	}
	batch := BusyReceipt([]string{"d1", "d2"}, "pivot")
	if !strings.Contains(batch, "delegations d1, d2") {
		t.Fatalf("batch receipt missing joined ids: %s", batch)
	}
	for _, want := range []string{"moved the running agents to the background", "```"} {
		if !strings.Contains(batch, want) {
			t.Fatalf("batch receipt missing %q: %s", want, batch)
		}
	}
}

func TestBusyReceiptCapsInjectedEcho(t *testing.T) {
	long := strings.Repeat("x", InjectedCap+50)
	got := BusyReceipt([]string{"d1"}, long)
	shown := strings.SplitN(got, "```\n", 2)[1]
	shown = strings.TrimSuffix(shown, "\n```")
	if len(shown) > InjectedCap+3 {
		t.Fatalf("echo not capped: %d runes", len([]rune(shown)))
	}
	if !strings.HasSuffix(shown, "...") {
		t.Fatalf("capped echo must end with ellipsis marker: %q", shown[len(shown)-10:])
	}
	at := BusyReceipt([]string{"d1"}, strings.Repeat("y", InjectedCap))
	if strings.Contains(at, "...") {
		t.Fatalf("echo exactly at the cap must not be truncated")
	}
}

func TestRegistrationGateMatrix(t *testing.T) {
	base := Agent{Key: "p", HasSteer: true}
	cases := []struct {
		name string
		cfg  Config
		deep int
		want bool
	}{
		{"default registers", DefaultConfig(), 0, true},
		{"kill switch off", Config{SteerForceBackground: false, DeliverySupported: true}, 0, false},
		{"delivery incapable", Config{SteerForceBackground: true, DeliverySupported: false}, 0, false},
		{"child depth", DefaultConfig(), 1, false},
		{"orchestrator depth", DefaultConfig(), 2, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			reg := NewRegistry(tc.cfg)
			parent := base
			parent.DelegateDepth = tc.deep
			if w := reg.Register(&Run{Parent: parent, AdoptID: "d"}); (w != nil) != tc.want {
				t.Fatalf("register = %v, want %v", w != nil, tc.want)
			}
			if got := reg.LiveCount("p"); got != boolToInt(tc.want) {
				t.Fatalf("live count = %d, want %v", got, tc.want)
			}
		})
	}
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func TestSteerRefusedAdoptsNothing(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true, SteerRefused: true}
	w := reg.Register(&Run{Parent: agent, AdoptID: "d1"})
	if w == nil {
		t.Fatal("wait should register")
	}
	out := reg.SteerForcingBackground(agent, "change of plans")
	if out.Accepted {
		t.Fatal("steer refusal must not report acceptance")
	}
	if len(out.IDs) != 0 {
		t.Fatalf("refused steer adopted %v", out.IDs)
	}
	if !reg.HasEligible("p") {
		t.Fatal("the wait must survive a refused steer unchanged")
	}
}

func TestEmptySteerTextRefused(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	reg.Register(&Run{Parent: agent, AdoptID: "d1"})
	if out := reg.SteerForcingBackground(agent, "   "); out.Accepted || len(out.IDs) != 0 {
		t.Fatalf("blank steer must be refused, got %+v", out)
	}
}

func TestRailRefusalKeepsWaitBlocking(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	w := reg.Register(&Run{Parent: agent}) // AdoptID "" = rail refuses
	out := reg.SteerForcingBackground(agent, "nudge")
	if !out.Accepted || len(out.IDs) != 0 {
		t.Fatalf("refusal must degrade to plain steer: %+v", out)
	}
	// The wait stays BLOCKED (no release, plain blocking semantics survive to the
	// timeout); it is merely not retried by later steers, exactly like the runtime.
	if w.ReleasedBySteer() {
		t.Fatal("refused adoption must not release the wait")
	}
	if reg.HasEligible("p") {
		t.Fatal("a refused wait is not retried by later steers")
	}
}

func TestOneSteerAdoptsEveryLiveWait(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	for _, id := range []string{"d1", "d2"} {
		reg.Register(&Run{Parent: agent, AdoptID: id})
	}
	out := reg.SteerForcingBackground(agent, "all of you, background")
	if len(out.IDs) != 2 || out.IDs[0] != "d1" || out.IDs[1] != "d2" {
		t.Fatalf("batch adoption wrong: %+v", out.IDs)
	}
	if reg.HasEligible("p") {
		t.Fatal("adopted waits are no longer live")
	}
}

func TestAdoptIdempotentAcrossCallers(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	w := reg.Register(&Run{Parent: agent, AdoptID: "deleg-once"})
	const callers = 8
	ids := make([]string, callers)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := range callers {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			ids[i] = w.Adopt()
		}(i)
	}
	close(start)
	wg.Wait()
	for i, id := range ids {
		if id != "deleg-once" {
			t.Fatalf("caller %d observed %q, want deleg-once", i, id)
		}
	}
	if !w.ReleasedBySteer() {
		t.Fatal("successful adoption must release the wait")
	}
}

func TestConcurrentSteersSingleDispatchSameVerdict(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	reg.Register(&Run{Parent: agent, AdoptID: "deleg-x"})
	const steers = 4
	outs := make([]SteerOutcome, steers)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := range steers {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			outs[i] = reg.SteerForcingBackground(agent, "steer")
		}(i)
	}
	close(start)
	wg.Wait()
	distinct := map[string]bool{}
	for _, out := range outs {
		if !out.Accepted {
			t.Fatal("every racing steer is accepted")
		}
		for _, id := range out.IDs {
			distinct[id] = true
		}
	}
	if len(distinct) != 1 || !distinct["deleg-x"] {
		t.Fatalf("racing steers must observe exactly one delegation, got %v", distinct)
	}
}

func TestCompletedWaitLeavesNoPhantom(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p", HasSteer: true}
	w := reg.Register(&Run{Parent: agent, AdoptID: "d1"})
	w.Complete()
	if reg.HasEligible("p") {
		t.Fatal("a settled worker must not register as releasable")
	}
	out := reg.SteerForcingBackground(agent, "hello")
	if !out.Accepted || len(out.IDs) != 0 {
		t.Fatalf("nothing to adopt after completion: %+v", out)
	}
}

func TestSteerWithoutSteerMethod(t *testing.T) {
	reg := NewRegistry(DefaultConfig())
	agent := Agent{Key: "p"} // HasSteer false
	reg.Register(&Run{Parent: agent, AdoptID: "d1"})
	out := reg.SteerForcingBackground(agent, "hello")
	if out.Accepted {
		t.Fatal("an agent without steer() must refuse")
	}
	if !reg.HasEligible("p") {
		t.Fatal("the wait must keep blocking")
	}
}

func TestSummaryVerdicts(t *testing.T) {
	cases := []struct {
		out  SteerOutcome
		want string
	}{
		{SteerOutcome{}, "steer refused; nothing adopted"},
		{SteerOutcome{Accepted: true}, "steer accepted; adoption refused (children keep blocking)"},
		{SteerOutcome{Accepted: true, IDs: []string{"a"}}, "steer accepted; force-backgrounded 1 delegation(s): a"},
	}
	for _, tc := range cases {
		if got := Summary(tc.out); got != tc.want {
			t.Fatalf("summary = %q, want %q", got, tc.want)
		}
	}
}

func TestSteeredQueueReceipt(t *testing.T) {
	got := SteeredQueueReceipt(" (2 min elapsed)")
	want := "⏩ Steered into current run (2 min elapsed). Your message will be injected at a safe point in this run."
	if got != want {
		t.Fatalf("queue steer receipt = %q, want %q", got, want)
	}
}
