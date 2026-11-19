// Package profileprovision owns the decision table for provisioning the
// deep_research worker profile: when an existing profile must be left alone,
// when a missing one may be cloned (and from where), which names can never be
// provisioned, and how a failed creation is classified.
//
// The table deliberately mirrors the guards hermes_cli.profiles.create_profile
// itself enforces (profile id regex, reserved names, the built-in "default")
// so an unprovisionable name is refused BEFORE any filesystem action, and a
// caller that ignores the refusal still fails safely inside create_profile.
//
// The executable form is cmd/profileprovision; plugins/deep_research/provision.py
// pipes one state record per decision through it (the jevnotices bridge
// pattern) and executes the returned plan with the real profile code.
package profileprovision

import (
	"fmt"
	"regexp"
)

// profileIDRegex is hermes_cli.profiles._PROFILE_ID_RE: the on-disk profile
// id charset. Duplicated here so the table can refuse a bad name before
// create_profile is ever invoked.
var profileIDRegex = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// reservedNames is hermes_cli.profiles._RESERVED_NAMES (minus "default",
// which gets its own clearer refusal): ids that collide with the Hermes
// installation or common system binaries.
var reservedNames = map[string]bool{
	"hermes": true,
	"test":   true,
	"tmp":    true,
	"root":   true,
	"sudo":   true,
}

// State is the gathered runtime fact base for one provisioning decision.
// Python supplies every field; this package never touches the filesystem.
type State struct {
	// Name is the configured deep_research.worker_profile.
	Name string
	// Exists reports whether the profile directory is live (present and
	// carrying identity, not tombstoned) — hermes_cli.profiles.profile_exists.
	Exists bool
	// ActiveProfile is the creating session's profile name ("default",
	// a named profile id, or "custom" for any other HERMES_HOME path).
	ActiveProfile string
	// ActiveIsNamed reports whether ActiveProfile is literally
	// <profiles-root>/<ActiveProfile> (get_active_profile_name returned a
	// named id, not "default"/"custom"). Only then is the name a usable
	// clone source; otherwise the clone must resolve the active home itself.
	ActiveIsNamed bool
}

// Plan is the decision for one State. Action is one of:
//
//	skip    — the profile exists; never recreate a live profile
//	create  — clone it via create_profile with CloneConfig
//	refuse  — the name cannot be provisioned; Code/Reason say why
type Plan struct {
	Action string `json:"action"`
	// CloneConfig is true iff Action == "create": the clone copies the
	// source's config files, installed skills, and memory identity.
	CloneConfig bool `json:"clone_config"`
	// CloneFrom names the clone source profile, or "" when create_profile
	// must resolve the active home itself (active is "default", "custom",
	// or not a valid named id).
	CloneFrom string `json:"clone_from"`
	Code      string `json:"code"`
	Reason    string `json:"reason"`
}

// Actions and outcomes, mirrored on the Python side by string comparison —
// the shared table in testdata pins the exact JSON both sides speak.
const (
	ActionSkip   = "skip"
	ActionCreate = "create"
	ActionRefuse = "refuse"

	OutcomeProvisioned = "provisioned"
	OutcomeFailed      = "failed"

	CodeProvisionFailed = "worker_profile_provision_failed"
	CodeInvalidProfile  = "invalid_worker_profile"
)

// PlanProvision applies the provisioning decision table.
//
// Rule order is load-bearing: an existing profile always wins (the operator
// may deliberately point worker_profile at "default" or any live profile),
// and only a profile that must be CREATED is name-validated.
func PlanProvision(s State) Plan {
	if s.Exists {
		return Plan{Action: ActionSkip}
	}
	if s.Name == "" {
		return Plan{
			Action: ActionRefuse,
			Code:   CodeInvalidProfile,
			Reason: "worker profile name is empty",
		}
	}
	if s.Name == "default" {
		return Plan{
			Action: ActionRefuse,
			Code:   CodeInvalidProfile,
			Reason: "cannot create the built-in default profile",
		}
	}
	if !profileIDRegex.MatchString(s.Name) {
		return Plan{
			Action: ActionRefuse,
			Code:   CodeInvalidProfile,
			Reason: fmt.Sprintf(
				"worker profile name %q is not a valid profile id (lowercase letters, numbers, '-', '_')",
				s.Name),
		}
	}
	if reservedNames[s.Name] {
		return Plan{
			Action: ActionRefuse,
			Code:   CodeInvalidProfile,
			Reason: fmt.Sprintf("worker profile name %q is reserved", s.Name),
		}
	}
	return Plan{
		Action:      ActionCreate,
		CloneConfig: true,
		CloneFrom:   cloneSource(s),
	}
}

// cloneSource picks the clone source for a create: the active profile's name
// when that name resolves to a real directory under profiles/ (a multiplexed
// gateway session cloning its own profile), else "" so create_profile falls
// back to the active home — which also covers "default" and custom HERMES_HOME
// deployments, where no named source exists.
func cloneSource(s State) string {
	if !s.ActiveIsNamed || s.ActiveProfile == "" || s.ActiveProfile == "default" {
		return ""
	}
	if !profileIDRegex.MatchString(s.ActiveProfile) || reservedNames[s.ActiveProfile] {
		return ""
	}
	return s.ActiveProfile
}

// Failure is one creation attempt's outcome, reported for classification.
type Failure struct {
	Name string
	// Race is true when create_profile raised FileExistsError: another
	// process created the same profile between the plan and the create.
	Race bool
	// Error is the exception text of a non-race failure.
	Error string
}

// Outcome classifies a failed creation attempt.
type Outcome struct {
	Outcome string `json:"outcome"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

// ClassifyFailure turns a creation failure into the tool-facing verdict.
//
// A lost creation race is success: the goal was "the profile exists", and it
// does. Anything else is a provisioning failure the caller reports verbatim
// while the delegate_research tool itself stays visible.
func ClassifyFailure(f Failure) Outcome {
	if f.Race {
		return Outcome{Outcome: OutcomeProvisioned}
	}
	return Outcome{
		Outcome: OutcomeFailed,
		Code:    CodeProvisionFailed,
		Message: fmt.Sprintf("could not provision worker profile %q: %s", f.Name, f.Error),
	}
}
