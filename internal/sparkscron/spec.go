// Package sparkscron is the single owner of the default inbox-sparks cron
// job's specification and seeding decision.
//
// The hermes_starts plugin ships a periodic wake so every install gets
// agent-initiated conversations without a hand-created job. The Python side
// (plugins/hermes_starts/sparks_cron.py) only gathers per-profile facts —
// which job names exist in the store, whether the seeding marker is present,
// the plugins.entries.hermes_starts.settings.sparks_cron flag, and whether
// the plugin's prerequisite (a Discord bot token) holds — and executes the
// verdict this package returns. It never decides and never carries the spec:
// there is exactly one copy of the job name, schedule, prompt, and toolset
// list, and exactly one create/skip table.
//
// Design constraints encoded here:
//
//   - An existing job named inbox-sparks is NEVER modified, duplicated, or
//     "repaired", whatever its schedule, prompt, or toolsets are. The
//     maintainer's live job carries a hand-tuned 17k-char prompt; a seeded
//     default that overwrote it would destroy operator work.
//   - Two or more jobs already carrying the name is an operator-state
//     anomaly: skip rather than guess which one is "the" job.
//   - A job this seeder created and the user then deleted must stay deleted
//     (the marker the Python side writes after a successful create is the
//     signal), and plugins.entries.hermes_starts.settings.sparks_cron=false
//     opts a profile out entirely.
//   - The job's toolsets are the generic, always-present ones. Host-specific
//     toolsets (e.g. tabctl) are deliberately absent: a default job must
//     resolve its toolset on every install.
//   - The job attaches no skills: a default job must not reference a skill
//     that may not exist on the install (hermes-starts ships only on the
//     maintainer's host). The prompt is self-contained.
package sparkscron

// JobName is the canonical cron job name looked for and seeded.
const JobName = "inbox-sparks"

// Schedule is the default wake cadence: every 120 minutes.
const Schedule = "every 120m"

// Deliver keeps run output local: the wake itself posts nothing to any
// channel — start_conversation is the only thing that talks to the user, so
// a no-opening run cannot spam anything.
const Deliver = "local"

// Toolsets is the enabled_toolsets list for the seeded job. Order matters
// only for readability; hermes_starts is first because it is the point of
// the job. Every other entry is a generic built-in toolset.
var Toolsets = []string{
	"hermes_starts",
	"file",
	"terminal",
	"session_search",
	"skills",
	"web",
}

// Prompt is the complete, self-contained wake prompt. Generic by contract:
// no maintainer names, channel ids, host paths, host-specific tool names, or
// skill references. [SILENT] is the scheduler's delivery-suppression marker
// for the common nothing-to-say run.
const Prompt = `You are waking up on a schedule to decide whether Hermes should START a conversation with the user — not to answer one.

Look over recent activity for anything worth opening a conversation about: recent conversations and session history, finished job results, anything you noticed while working. The bar is honest and substantive — an observation, idea, question, joke, compliment, piece of feedback, disagreement, or a genuine check-in. Something worth texting first, in your own voice.

Do NOT open a conversation for:
- task status, progress, or results of work you (or another session) are running — that belongs in the task's own thread;
- anything already discussed, asked, or offered recently — check existing conversations and recent starts first, and continue the existing thread instead of opening a new one;
- a question or verification you can answer yourself with the tools you have;
- manufactured or filler openings — silence is always valid and is the common case.

For each genuine opening, call the start_conversation tool (fields: kind, message, next_move, tone). The message must carry its own content in plain words, address the user in the second person ("you"), and lead with the point.

When nothing clears the bar, reply with exactly [SILENT] and nothing else.`

// Spec is the job record the Python side passes to cron.jobs.create_job
// after this package decides to seed.
type Spec struct {
	Name            string   `json:"name"`
	Schedule        string   `json:"schedule"`
	Skills          []string `json:"skills"`
	Deliver         string   `json:"deliver"`
	EnabledToolsets []string `json:"enabled_toolsets"`
	Prompt          string   `json:"prompt"`
}

// DefaultSpec returns the job to seed.
func DefaultSpec() Spec {
	return Spec{
		Name:            JobName,
		Schedule:        Schedule,
		Skills:          []string{},
		Deliver:         Deliver,
		EnabledToolsets: append([]string(nil), Toolsets...),
		Prompt:          Prompt,
	}
}

// Validate reports whether the spec is internally consistent. The Python
// side refuses to create a job from a spec that fails these invariants, so a
// bad build can never write a malformed or empty job record into the store.
func (s Spec) Validate() error {
	if s.Name != JobName {
		return errf("spec name %q must be %q", s.Name, JobName)
	}
	if s.Schedule == "" {
		return errf("schedule must not be empty")
	}
	if s.Deliver != Deliver {
		return errf("deliver %q must be %q so a no-opening run cannot reach any channel", s.Deliver, Deliver)
	}
	if len(s.Skills) != 0 {
		return errf("skills must be empty: the default job must not reference a skill that may not ship, got %v", s.Skills)
	}
	seen := make(map[string]bool, len(s.EnabledToolsets))
	for _, ts := range s.EnabledToolsets {
		if ts == "" || seen[ts] {
			return errf("enabled_toolsets must be non-empty and unique, got %v", s.EnabledToolsets)
		}
		seen[ts] = true
	}
	if !seen["hermes_starts"] {
		return errf("enabled_toolsets must include hermes_starts, got %v", s.EnabledToolsets)
	}
	if s.Prompt == "" {
		return errf("prompt must not be empty")
	}
	return nil
}
