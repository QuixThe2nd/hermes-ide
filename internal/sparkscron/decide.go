package sparkscron

import "fmt"

// errf is the package's single error constructor (no error wrapping needed:
// every error here is a terminal verdict for the caller to log).
func errf(format string, args ...any) error {
	return fmt.Errorf("sparkscron: "+format, args...)
}

// Inputs are the per-profile facts the Python bridge gathers. They are all
// booleans or lists because the bridge must not need to know the job name,
// the flag path, or the marker semantics to fill them in.
type Inputs struct {
	// JobNames is every job name in the profile's store, including
	// disabled/paused records. The canonical name is matched here so the
	// name constant never leaves this package.
	JobNames []string `json:"job_names"`
	// Seeded is true when the bridge's marker file exists: this seeder
	// created the job here before, so its absence now means the user
	// deleted it.
	Seeded bool `json:"seeded"`
	// CronEnabled is plugins.entries.hermes_starts.settings.sparks_cron
	// resolved for this profile (default true).
	CronEnabled bool `json:"sparks_cron"`
	// PrereqOK is whether the plugin's own prerequisite holds for this
	// profile (a configured Discord bot token — without it the woken agent
	// has no start_conversation tool and every wake is wasted model spend).
	PrereqOK bool `json:"prereq_ok"`
}

// Verdict actions.
const (
	ActionCreate = "create" // seed the default job now
	ActionSkip   = "skip"   // do nothing this pass
)

// Verdict reasons (ActionSkip only).
const (
	ReasonPrereqMissing    = "prerequisite-missing" // no usable token for this profile
	ReasonDisabledByConfig = "disabled-by-config"   // sparks_cron=false for this profile
	ReasonJobAmbiguous     = "job-ambiguous"        // 2+ jobs already carry the name
	ReasonJobExists        = "job-exists"           // exactly one job carries the name
	ReasonDeletedByUser    = "deleted-by-user"      // seeded before, job since deleted
)

// Verdict is the decision for one profile home.
type Verdict struct {
	Action string `json:"action"`
	Reason string `json:"reason,omitempty"`
	// Job is set only for ActionCreate: the record to pass to
	// cron.jobs.create_job.
	Job *Spec `json:"job,omitempty"`
}

// Decide resolves one profile's facts to a verdict.
//
// The gate order is load-bearing:
//
//  1. prerequisite, 2. config opt-out — both dominate everything: a profile
//     that cannot or must not run the wake must not gain a job record even
//     once;
//  3. ambiguity — two jobs with the name means operator state we do not
//     understand; touching either is guessing;
//  4. existence — an existing job (any schedule, any prompt, paused or
//     disabled) is authoritative and byte-identical afterwards;
//  5. the deletion marker — respected after existence so a hand-created job
//     that is later deleted can be reseeded as the default (the marker only
//     records what THIS seeder created).
func Decide(in Inputs) Verdict {
	if !in.PrereqOK {
		return skip(ReasonPrereqMissing)
	}
	if !in.CronEnabled {
		return skip(ReasonDisabledByConfig)
	}
	matches := 0
	for _, name := range in.JobNames {
		if name == JobName {
			matches++
		}
	}
	if matches > 1 {
		return skip(ReasonJobAmbiguous)
	}
	if matches == 1 {
		return skip(ReasonJobExists)
	}
	if in.Seeded {
		return skip(ReasonDeletedByUser)
	}
	spec := DefaultSpec()
	if err := spec.Validate(); err != nil {
		// Unreachable unless this package is broken; the CLI surfaces it
		// as an error exit rather than seeding a malformed record.
		return Verdict{Action: ActionSkip, Reason: "invalid-spec: " + err.Error()}
	}
	return Verdict{Action: ActionCreate, Job: &spec}
}

func skip(reason string) Verdict {
	return Verdict{Action: ActionSkip, Reason: reason}
}
