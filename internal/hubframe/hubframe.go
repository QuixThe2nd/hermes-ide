// Package hubframe is the Go twin of the Discord hub stream-limit contract
// in plugins/platforms/discord/hub_client.py: the newline-framed protocol
// between the gateway-side hub client and the out-of-process Discord
// transport hub, and the single cap on how large one frame may be before
// the reader rejects it.
//
// asyncio's StreamReader caps lines at 64 KiB by default; a hub frame above
// that raised LimitOverrunError and DROPPED the frame on large Discord
// payloads (embeds, long messages, attachment batches), so the Python
// client opens its unix socket with a 16 MiB limit instead
// (_HUB_STREAM_LIMIT). This package states the same contract for Go
// consumers and keeps the two constants in lockstep through the shared
// testdata table (testdata/hubframe_cases.json), which the Python contract
// test (tests/plugins/platforms/discord/test_hubframe_contract.py) also
// reads: Python limit == table == StreamLimit, each pinned by its own
// side's tests. Production Python never shells out to Go — repo
// deployments (pip, docker) carry no Go toolchain, the same arrangement as
// internal/servingidentity and the other internal/* twins.
package hubframe

import (
	"bufio"
	"errors"
	"io"
)

// StreamLimit is the maximum accepted size, in bytes, of one frame's
// content — the bytes before the terminating newline. It MUST equal
// _HUB_STREAM_LIMIT in plugins/platforms/discord/hub_client.py; the shared
// testdata table pins both, and the table-driven tests reject any frame
// whose content exceeds it.
const StreamLimit = 16 * 1024 * 1024

// ErrFrameTooLarge reports a frame whose content exceeds StreamLimit. The
// reader rejects it as soon as the excess is visible, without buffering the
// remainder — the Go analogue of asyncio's LimitOverrunError.
var ErrFrameTooLarge = errors.New("hubframe: frame content exceeds StreamLimit")

// ReadFrame reads one newline-terminated frame from r and returns its
// content without the trailing newline, mirroring the Python client's
// `await reader.readline()` followed by a JSON decode of the line.
//
// A trailing chunk at EOF without a newline is returned as-is (asyncio
// readline yields the partial tail too); a clean EOF with nothing buffered
// surfaces as io.EOF. Frames whose content exceeds StreamLimit fail with
// ErrFrameTooLarge.
func ReadFrame(r *bufio.Reader) ([]byte, error) {
	var buf []byte
	for {
		chunk, err := r.ReadSlice('\n')
		buf = append(buf, chunk...)
		contentLen := len(buf)
		if err == nil {
			contentLen-- // the trailing '\n' is framing, not content
		}
		if contentLen > StreamLimit {
			return nil, ErrFrameTooLarge
		}
		switch {
		case errors.Is(err, bufio.ErrBufferFull):
			continue
		case err == nil:
			return buf[:len(buf)-1], nil
		case errors.Is(err, io.EOF) && len(buf) > 0:
			return buf, nil
		default:
			return nil, err
		}
	}
}
