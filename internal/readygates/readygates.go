// Package readygates is the Go twin of the gateway's readiness gate
// (gateway/readygates.py ReadyGate). The Python function is what the runtime
// executes; deployments carry no Go toolchain, the same arrangement as every
// other internal/* twin. Both twins run the shared table in
// testdata/cases.json.
//
// Contract (shared with the Python twin):
//
//   - READY (sd_notify READY=1) means the gateway is genuinely serving:
//     every enabled platform adapter has connected, and cron + housekeeping
//     have started. These are the only subsystems that gate readiness.
//   - Optional boot work never gates readiness: MCP tool discovery for addon
//     servers (which may block up to 120s on an unreachable host) and
//     boot-path notification sends (restart notices, comeback notices,
//     obligation redelivery) run off the readiness path. A slow or failed
//     optional subsystem delays only its own effect, never the service.
//   - blocked_by lists the gating subsystems that are not yet ready, in a
//     stable order: "platforms", then "cron", then "housekeeping". Empty
//     when ready.
//   - mcp_discovery_state (pending/running/done/failed) and
//     boot_path_sends_state (pending/running/done) are observed but never
//     affect the verdict.
package readygates

// PlatformState describes one platform adapter's boot progress.
type PlatformState struct {
	Enabled   bool `json:"enabled"`
	Connected bool `json:"connected"`
}

// Input is the set of subsystem states observed at boot.
type Input struct {
	Platforms             []PlatformState `json:"platforms"`
	CronStarted           bool            `json:"cron_started"`
	HousekeepingStarted   bool            `json:"housekeeping_started"`
	McpDiscoveryState     string          `json:"mcp_discovery_state"`
	BootPathSendsState    string          `json:"boot_path_sends_state"`
}

// Result is the readiness verdict.
type Result struct {
	Ready     bool     `json:"ready"`
	BlockedBy []string `json:"blocked_by"`
}

// Decide reports whether the gateway is ready to serve.
func Decide(in Input) Result {
	blocked := []string{}
	platformsBlocked := false
	for _, p := range in.Platforms {
		if p.Enabled && !p.Connected {
			platformsBlocked = true
		}
	}
	if platformsBlocked {
		blocked = append(blocked, "platforms")
	}
	if !in.CronStarted {
		blocked = append(blocked, "cron")
	}
	if !in.HousekeepingStarted {
		blocked = append(blocked, "housekeeping")
	}
	if blocked == nil {
		blocked = []string{}
	}
	return Result{Ready: len(blocked) == 0, BlockedBy: blocked}
}
