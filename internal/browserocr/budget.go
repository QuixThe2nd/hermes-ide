// Package browserocr pins the privacy-OCR budget contract that
// tools/browser_progress.py implements: tesseract inspects the
// full-resolution screenshot (any downscaled copy garbles small marker
// text), runs single-threaded, and sits under a timeout with measured
// headroom over the worst observed full-image run.
//
// This package is the executable spec for that policy; the Python capture
// path mirrors these values and tests/tools/test_browser_progress.py asserts
// the same relationships from the caller's side.
package browserocr

import "time"

// MeasuredFullImageSeconds is the deployed tesseract runtime for a full
// 1536x735 Camofox PNG: 8.65s. The original 5s ceiling sat below it and
// suppressed every public page, so every budget decision is made against
// this number.
const MeasuredFullImageSeconds = 8.65

// MeasuredFrameWidth and MeasuredFrameHeight are the frame the runtime
// above was measured on.
const (
	MeasuredFrameWidth  = 1536
	MeasuredFrameHeight = 735
)

// MarginOverMeasured is the minimum required ratio between the timeout
// ceiling and MeasuredFullImageSeconds: a merely slow tesseract must
// complete instead of being reported unavailable, while a hung one is
// still killed and the image suppressed (fail closed).
const MarginOverMeasured = 3

// TimeoutSeconds is the subprocess ceiling the capture path passes to
// run_local_ocr_checked.
const TimeoutSeconds = 30

// SingleThreadEnv is the subprocess environment that keeps tesseract off
// its OpenMP pool: under load, default threading measured 16-25s on the
// same image that finished in 0.65s single-threaded, with byte-identical
// output.
var SingleThreadEnv = map[string]string{"OMP_THREAD_LIMIT": "1"}

// Timeout returns TimeoutSeconds as a duration.
func Timeout() time.Duration {
	return time.Duration(TimeoutSeconds) * time.Second
}

// BudgetOK reports whether a timeout ceiling honors the measured-runtime
// contract.
func BudgetOK(timeoutSeconds int) bool {
	return float64(timeoutSeconds) >= MarginOverMeasured*MeasuredFullImageSeconds
}

// EstimateSeconds scales MeasuredFullImageSeconds to another frame size,
// assuming tesseract cost grows linearly with pixel count.
func EstimateSeconds(width, height int) float64 {
	if width <= 0 || height <= 0 {
		return 0
	}
	measuredPixels := MeasuredFrameWidth * MeasuredFrameHeight
	return MeasuredFullImageSeconds * float64(width*height) / float64(measuredPixels)
}

// MarkerPixelFloor is the smallest rendered text height (pixels) that
// full-resolution tesseract still read in the recall measurements
// ("Sign in" at ~11px). Downscaled OCR copies garbled exactly this class
// of marker ("Sign in" -> "Sign ino"), which is why the policy inspects
// the original frame and never a resize. Typical UI body text (16px and
// up) must stay above this floor.
const MarkerPixelFloor = 11

// TypicalBodyTextPixels is the smallest body-text height the recall
// measurements treated as realistic page rendering.
const TypicalBodyTextPixels = 16
