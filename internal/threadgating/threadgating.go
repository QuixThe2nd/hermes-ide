// Package threadgating mirrors the Discord free-response mention bypass rule:
// a message is free-response (no @mention needed) when its channel is listed
// in free_response_channels. A thread inherits channel policy from its parent
// but NOT the free-response exemption: a thread is free-response only when its
// own id is listed.
package threadgating

// FreeResponseMatch reports whether message qualifies as free-response.
// Wildcard "*" in freeChannels exempts everything. For a thread, only the
// thread's own id (channelKeys[0]) counts; parent membership does not.
func FreeResponseMatch(isThread bool, channelKeys []string, freeChannels []string) bool {
	for _, fc := range freeChannels {
		if fc == "*" {
			return true
		}
	}
	if isThread {
		if len(channelKeys) == 0 {
			return false
		}
		own := channelKeys[0]
		for _, fc := range freeChannels {
			if fc == own {
				return true
			}
		}
		return false
	}
	set := make(map[string]struct{}, len(channelKeys))
	for _, k := range channelKeys {
		set[k] = struct{}{}
	}
	for _, fc := range freeChannels {
		if _, ok := set[fc]; ok {
			return true
		}
	}
	return false
}
