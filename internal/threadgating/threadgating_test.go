package threadgating

import "testing"

func TestFreeResponseMatch(t *testing.T) {
	cases := []struct {
		name         string
		isThread     bool
		channelKeys  []string
		freeChannels []string
		want         bool
	}{
		// Rule 1: channel itself listed -> free response.
		{"channel listed", false, []string{"chan-1"}, []string{"chan-1"}, true},
		{"channel not listed", false, []string{"chan-1"}, []string{"other"}, false},
		// Wildcard exempts everything.
		{"wildcard", false, []string{"chan-1"}, []string{"*"}, true},
		{"wildcard thread", true, []string{"thread-1"}, []string{"*"}, true},
		// Rule 2: thread parent listed does not exempt the thread.
		{"thread parent only", true, []string{"thread-1"}, []string{"parent-1"}, false},
		// Rule 3: thread own id listed -> free response.
		{"thread own listed", true, []string{"thread-1"}, []string{"thread-1"}, true},
		{"thread neither", true, []string{"thread-1"}, []string{"other"}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := FreeResponseMatch(c.isThread, c.channelKeys, c.freeChannels)
			if got != c.want {
				t.Errorf("FreeResponseMatch(%v, %v, %v) = %v, want %v",
					c.isThread, c.channelKeys, c.freeChannels, got, c.want)
			}
		})
	}
}
