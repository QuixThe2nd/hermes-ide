package hubframe

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// ErrReactionGateConfigPath reports a hub attach carrying a reload scope
// without the owning profile's config path. Substituting the hub's default
// profile here would silently watch and reload the wrong profile.
var ErrReactionGateConfigPath = errors.New("hubframe: reaction_gate_reload.config_path is required")

// ErrHermesHomeEmpty reports a hub attach that supplies hermes_home with no
// usable value. Substituting the hub process's own home would silently merge
// every attached profile's per-owner state into one file, so a supplied home
// must be non-empty after trimming.
var ErrHermesHomeEmpty = errors.New("hubframe: hermes_home must be non-empty when supplied")

// ReactionGateReloadScope is the immutable connect-time state captured by the
// owning gateway profile for a hub-created Discord adapter. Nil ManagedDir and
// ManagedEnv values represent an unmanaged scope; an empty, non-nil ManagedEnv
// is a managed scope whose snapshot currently contains no variables.
type ReactionGateReloadScope struct {
	ConfigPath string            `json:"config_path"`
	ManagedDir *string           `json:"managed_dir"`
	ManagedEnv map[string]string `json:"managed_env"`
}

// AttachParams is the profile-sensitive portion of a Discord hub attach
// request's params object. Other attach fields remain forward-compatible JSON
// and are intentionally not duplicated here.
type AttachParams struct {
	GenerationID       string                   `json:"generation_id"`
	Profile            string                   `json:"profile"`
	HermesHome         *string                  `json:"hermes_home"`
	ReactionGateReload *ReactionGateReloadScope `json:"reaction_gate_reload"`
}

// DecodeAttachParams decodes the params object of an attach frame. A missing
// or null reaction_gate_reload is the legacy path: the hub captures its local
// scope. A supplied scope must retain the owning profile's config path.
// Likewise a missing or null hermes_home keeps the hub-local default; a
// supplied home must be non-empty after trimming.
func DecodeAttachParams(frame []byte) (AttachParams, error) {
	var params AttachParams
	if err := json.Unmarshal(frame, &params); err != nil {
		return AttachParams{}, fmt.Errorf("hubframe: decode attach params: %w", err)
	}
	if scope := params.ReactionGateReload; scope != nil {
		scope.ConfigPath = strings.TrimSpace(scope.ConfigPath)
		if scope.ConfigPath == "" {
			return AttachParams{}, ErrReactionGateConfigPath
		}
	}
	if home := params.HermesHome; home != nil {
		*home = strings.TrimSpace(*home)
		if *home == "" {
			return AttachParams{}, ErrHermesHomeEmpty
		}
	}
	return params, nil
}

// ShouldAdoptReactionGateScope reports whether a cached Discord adapter may
// replace its reaction-gate watcher with an explicitly supplied owner scope.
// Legacy attaches without a scope never rebind. A named profile may take over
// a default-owned adapter (including a legacy adapter with no captured name),
// while default and sibling-profile reconnects cannot steal a named owner.
func ShouldAdoptReactionGateScope(
	currentProfile string,
	incomingProfile string,
	incomingScope *ReactionGateReloadScope,
) bool {
	if incomingScope == nil {
		return false
	}
	return shouldAdoptScopedOwner(currentProfile, incomingProfile)
}

// ShouldAdoptThreadScope reports whether a cached Discord adapter may rebind
// its thread-participation store to an explicitly supplied owner home. The
// rule matches ShouldAdoptReactionGateScope: a legacy attach without a home
// never rebinds, a named profile may take over a default-owned adapter, and
// default or sibling reconnects cannot steal a named owner.
func ShouldAdoptThreadScope(
	currentProfile string,
	incomingProfile string,
	incomingHome *string,
) bool {
	if incomingHome == nil {
		return false
	}
	return shouldAdoptScopedOwner(currentProfile, incomingProfile)
}

func shouldAdoptScopedOwner(currentProfile, incomingProfile string) bool {
	current := normalizeAttachProfile(currentProfile)
	incoming := normalizeAttachProfile(incomingProfile)
	return current == incoming || current == "default" && incoming != "default"
}

func normalizeAttachProfile(profile string) string {
	profile = strings.TrimSpace(profile)
	if profile == "" {
		return "default"
	}
	return profile
}
