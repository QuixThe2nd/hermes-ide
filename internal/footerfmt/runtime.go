package footerfmt

// Package footerfmt: visibility rule for the runtime-metadata footer.
//
// Go twin of the switch the gateway applies before appending the footer to a
// turn's final reply (gateway/run_turn.py::_runtime_footer_enabled). The
// resolution order is fixed and matches the display resolver:
//
//  1. A per-channel override wins outright, either direction. An explicit
//     false suppresses the footer even when the display setting says on; an
//     explicit true keeps it even when the display setting says off.
//  2. Otherwise the platform display setting decides
//     (display.platforms.<platform>.runtime_footer).
//  3. Otherwise the global display setting decides (display.runtime_footer).
//  4. With nothing configured the footer renders: on by default.
//
// Pointers, not plain bools: nil means "not configured", which is a different
// answer from an explicit false. A keeper that collapsed the two would silently
// turn every unconfigured channel off, or force every configured-off channel
// back on.

// RuntimeFooterEnabled reports whether the footer renders for one conversation
// from its channel override and its resolved display setting, either of which may
// be unset (nil).
func RuntimeFooterEnabled(channelOverride, displaySetting *bool) bool {
	if channelOverride != nil {
		return *channelOverride
	}
	if displaySetting != nil {
		return *displaySetting
	}
	return true
}

// ResolveRuntimeFooter applies the full chain in the order the gateway reads it:
// the channel override, then the platform display setting, then the global one.
// Splitting the two display levels here keeps the Python resolver's
// platform-before-global precedence under test on the Go side too.
func ResolveRuntimeFooter(channelOverride, platformSetting, globalSetting *bool) bool {
	if channelOverride != nil {
		return *channelOverride
	}
	switch {
	case platformSetting != nil:
		return *platformSetting
	case globalSetting != nil:
		return *globalSetting
	default:
		return true
	}
}

// RuntimeFooterSuppressed is RuntimeFooterEnabled negated. The gateway call site
// reads better as a suppression test, and keeping both directions here means the
// twin cannot drift into double negations at a call site.
func RuntimeFooterSuppressed(channelOverride, displaySetting *bool) bool {
	return !RuntimeFooterEnabled(channelOverride, displaySetting)
}

// BoolPtr is the small helper the tests and any Go caller use to express an
// explicit configured value. Nil stays reserved for "not configured".
func BoolPtr(value bool) *bool {
	return &value
}
