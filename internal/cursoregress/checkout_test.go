package cursoregress

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

func diskFixture(path string) DiskCheckout {
	content := "print('worker result')\n"
	return DiskCheckout{
		Path:         path,
		IsGitRepo:    true,
		BaseAncestor: true,
		HeadIsBase:   false,
		ChangedPaths: []string{"src/main.py"},
		OnDiskSHA:    map[string]string{"src/main.py": shaOf(content)},
		GitBlobSHA:   map[string]string{"src/main.py": shaOf(content)},
		HeadSHA:      "1111111111111111111111111111111111111111",
		TreeSHA:      "2222222222222222222222222222222222222222",
		SourceBase:   testSourceBase,
	}
}

func TestParseWorkerCmdlineCollectsWorkerDirs(t *testing.T) {
	argv := []string{
		"/opt/agent", "worker", "--name", WorkerDiscoveryName,
		"--worker-dir", "/srv/worker-a", "--worker-dir=/srv/worker-b",
	}
	dirs, ok := ParseWorkerCmdline(argv, WorkerDiscoveryName)
	if !ok {
		t.Fatalf("worker command line must be recognized: %v", argv)
	}
	if len(dirs) != 2 || dirs[0] != "/srv/worker-a" || dirs[1] != "/srv/worker-b" {
		t.Fatalf("dirs = %v, want both spellings collected in order", dirs)
	}
}

func TestParseWorkerCmdlineRejectsNonWorkers(t *testing.T) {
	cases := map[string][]string{
		"no worker literal": {"/opt/agent", "--name", WorkerDiscoveryName, "--worker-dir", "/srv/w"},
		"no name flag":      {"/opt/agent", "worker", WorkerDiscoveryName, "--worker-dir", "/srv/w"},
		"other machine":     {"/opt/agent", "worker", "--name", "somebody-else", "--worker-dir", "/srv/w"},
		"no worker dir":     {"/opt/agent", "worker", "--name", WorkerDiscoveryName},
	}
	for name, argv := range cases {
		if dirs, ok := ParseWorkerCmdline(argv, WorkerDiscoveryName); ok {
			t.Fatalf("%s: must not be recognized (dirs=%v)", name, dirs)
		}
	}
}

func TestFlagValueBothSpellings(t *testing.T) {
	if v, ok := FlagValue([]string{"a", "--worker-dir", "/srv/w"}, "--worker-dir"); !ok || v != "/srv/w" {
		t.Fatalf("space form: got (%q,%v)", v, ok)
	}
	if v, ok := FlagValue([]string{"a", "--worker-dir=/srv/w"}, "--worker-dir"); !ok || v != "/srv/w" {
		t.Fatalf("equals form: got (%q,%v)", v, ok)
	}
	if _, ok := FlagValue([]string{"a", "--worker-dir"}, "--worker-dir"); ok {
		t.Fatalf("dangling flag must carry no value")
	}
	if _, ok := FlagValue([]string{"a"}, "--worker-dir"); ok {
		t.Fatalf("absent flag must carry no value")
	}
}

func TestMatchDiskRootAcceptsCleanCheckout(t *testing.T) {
	m, reason := MatchDiskRoot(diskFixture("/srv/worker"))
	if m == nil {
		t.Fatalf("clean checkout must match: %s", reason)
	}
	if m.Source != "disk" || m.CheckoutPath != "/srv/worker" {
		t.Fatalf("manifest = %+v", m)
	}
	if !HashesMatch(m.Files, map[string]string{"src/main.py": "print('worker result')\n"}) {
		t.Fatalf("manifest hashes must match the on-disk bytes")
	}
	if res := VerifyFinished(m, nil); !res.Success {
		t.Fatalf("disk manifest must verify: %+v", res)
	}
}

func TestMatchDiskRootFailsClosed(t *testing.T) {
	mutations := map[string]func(c *DiskCheckout){
		"not a git repo":     func(c *DiskCheckout) { c.IsGitRepo = false },
		"base not ancestor":  func(c *DiskCheckout) { c.BaseAncestor = false },
		"head equals base":   func(c *DiskCheckout) { c.HeadIsBase = true },
		"empty diff":         func(c *DiskCheckout) { c.ChangedPaths = nil },
		"dirty on-disk byte": func(c *DiskCheckout) { c.OnDiskSHA["src/main.py"] = shaOf("tampered\n") },
		"deleted on disk":    func(c *DiskCheckout) { delete(c.OnDiskSHA, "src/main.py") },
		"deleted at head":    func(c *DiskCheckout) { delete(c.GitBlobSHA, "src/main.py") },
		"unresolvable head":  func(c *DiskCheckout) { c.HeadSHA = "" },
		"unresolvable tree":  func(c *DiskCheckout) { c.TreeSHA = "short" },
		"branch name base":   func(c *DiskCheckout) { c.SourceBase = "main" },
	}
	for name, mutate := range mutations {
		c := diskFixture("/srv/worker")
		mutate(&c)
		m, reason := MatchDiskRoot(c)
		if m != nil {
			t.Fatalf("%s: must fail closed, got manifest %+v", name, m)
		}
		if reason == "" {
			t.Fatalf("%s: rejection must carry a reason", name)
		}
	}
}

func TestVerifyDiskSingleMatchVerifies(t *testing.T) {
	notAMatch := diskFixture("/srv/other")
	notAMatch.BaseAncestor = false
	result, mayFallBack := VerifyDisk([]DiskCheckout{notAMatch, diskFixture("/srv/worker")})
	if !result.Success || result.Manifest == nil {
		t.Fatalf("single match must verify from disk: %+v", result)
	}
	if result.Manifest.CheckoutPath != "/srv/worker" {
		t.Fatalf("manifest must name the matching root, got %q", result.Manifest.CheckoutPath)
	}
	if mayFallBack {
		t.Fatalf("a disk match resolves the gate; the stream fallback must not run")
	}
}

func TestVerifyDiskZeroMatchesAllowsStreamFallback(t *testing.T) {
	result, mayFallBack := VerifyDisk(nil)
	if result.Success {
		t.Fatalf("no roots must not verify")
	}
	if !mayFallBack {
		t.Fatalf("zero roots must allow the stream fallback")
	}
	if !strings.Contains(result.Error, MissingArtifactEgress) {
		t.Fatalf("error %q must contain %q", result.Error, MissingArtifactEgress)
	}
}

func TestVerifyDiskAmbiguousRootsRefuseStream(t *testing.T) {
	result, mayFallBack := VerifyDisk([]DiskCheckout{
		diskFixture("/srv/worker-a"),
		diskFixture("/srv/worker-b"),
	})
	if result.Success {
		t.Fatalf("ambiguous roots must not verify")
	}
	if mayFallBack {
		t.Fatalf("ambiguous roots must refuse the stream fallback")
	}
	for _, root := range []string{"/srv/worker-a", "/srv/worker-b"} {
		if !strings.Contains(result.Error, root) {
			t.Fatalf("error %q must name %q", result.Error, root)
		}
	}
	if !strings.Contains(result.Error, MissingArtifactEgress) {
		t.Fatalf("error %q must contain %q", result.Error, MissingArtifactEgress)
	}
}

func TestHashesMatchRequiresSetEquality(t *testing.T) {
	content := "x = 1\n"
	files := []ManifestFile{{Path: "a.py", SHA256: shaOf(content)}}
	if !HashesMatch(files, map[string]string{"a.py": content}) {
		t.Fatalf("exact sets must match")
	}
	if HashesMatch(files, map[string]string{}) {
		t.Fatalf("missing snapshot must fail")
	}
	if HashesMatch(files, map[string]string{"a.py": content, "b.py": content}) {
		t.Fatalf("extra snapshot must fail")
	}
	if HashesMatch(files, map[string]string{"a.py": "other\n"}) {
		t.Fatalf("byte mismatch must fail")
	}
}

func TestStreamURLShape(t *testing.T) {
	url := StreamURL("https://api.cursor.com/", "agent-1", "run-1")
	if url != "https://api.cursor.com/v1/agents/agent-1/runs/run-1/stream" {
		t.Fatalf("url = %q", url)
	}
	if err := VerifyRecoveryRequest("GET", url); err != nil {
		t.Fatalf("constructed url must satisfy the recovery guard: %v", err)
	}
}

func TestRecoverArtifactFailsClosedBeforeHTTP(t *testing.T) {
	calls := 0
	fetch := func(url string) ([]byte, error) { calls++; return nil, nil }
	result := RecoverArtifact(RecoveryReceipt{SourceBase: testSourceBase}, "https://api.cursor.com", fetch)
	if result.Success {
		t.Fatalf("receipt without cloud ids must not recover")
	}
	if calls != 0 {
		t.Fatalf("missing ids must perform zero HTTP calls, did %d", calls)
	}
	if !strings.Contains(result.Error, MissingArtifactEgress) {
		t.Fatalf("error %q must contain %q", result.Error, MissingArtifactEgress)
	}
}

func TestRecoverArtifactSingleReadOnlyGet(t *testing.T) {
	urls := []string{}
	fetch := func(url string) ([]byte, error) {
		urls = append(urls, url)
		return completeStream("src/app.py", "x = 1\n"), nil
	}
	result := RecoverArtifact(
		RecoveryReceipt{CloudAgentID: "agent-1", CloudRunID: "run-1", SourceBase: testSourceBase},
		"https://api.cursor.com",
		fetch,
	)
	if !result.Success {
		t.Fatalf("complete stream must recover: %+v", result)
	}
	if len(urls) != 1 || urls[0] != "https://api.cursor.com/v1/agents/agent-1/runs/run-1/stream" {
		t.Fatalf("recovery must issue exactly one stream GET, got %v", urls)
	}
	if result.Manifest.Source != "stream" {
		t.Fatalf("recovered manifest source = %q, want stream", result.Manifest.Source)
	}
}

func TestRecoverArtifactFetchFailureIsEgressFailure(t *testing.T) {
	result := RecoverArtifact(
		RecoveryReceipt{CloudAgentID: "agent-1", CloudRunID: "run-1", SourceBase: testSourceBase},
		"https://api.cursor.com",
		func(url string) ([]byte, error) { return nil, errors.New("boom") },
	)
	if result.Success {
		t.Fatalf("fetch failure must not recover")
	}
	if !strings.Contains(result.Error, MissingArtifactEgress) {
		t.Fatalf("error %q must contain %q", result.Error, MissingArtifactEgress)
	}
}

func TestCanonicalTreeSHAIsOrderIndependent(t *testing.T) {
	a := []ManifestFile{{Path: "b.py", SHA256: shaOf("1")}, {Path: "a.py", SHA256: shaOf("2")}}
	b := []ManifestFile{{Path: "a.py", SHA256: shaOf("2")}, {Path: "b.py", SHA256: shaOf("1")}}
	if CanonicalTreeSHA(a) != CanonicalTreeSHA(b) {
		t.Fatalf("canonical tree sha must sort before hashing")
	}
	want := SHA256Hex(fmt.Sprintf("a.py\n%s\nb.py\n%s\n", shaOf("2"), shaOf("1")))
	if CanonicalTreeSHA(a) != want {
		t.Fatalf("canonical tree sha must hash the sorted path\\nsha256\\n lines")
	}
}

func TestMatchDiskRootSortsMultiFileDiff(t *testing.T) {
	c := diskFixture("/srv/worker")
	c.ChangedPaths = []string{"z_last.py", "a_first.py", "m_mid.py"}
	c.OnDiskSHA = map[string]string{
		"z_last.py":  shaOf("z\n"),
		"a_first.py": shaOf("a\n"),
		"m_mid.py":   shaOf("m\n"),
	}
	c.GitBlobSHA = map[string]string{
		"z_last.py":  shaOf("z\n"),
		"a_first.py": shaOf("a\n"),
		"m_mid.py":   shaOf("m\n"),
	}
	m, reason := MatchDiskRoot(c)
	if m == nil {
		t.Fatalf("multi-file checkout must match: %s", reason)
	}
	want := []string{"a_first.py", "m_mid.py", "z_last.py"}
	for i, f := range m.Files {
		if f.Path != want[i] {
			t.Fatalf("files[%d].Path = %q, want %q (diff order must be sorted)", i, f.Path, want[i])
		}
	}
}
