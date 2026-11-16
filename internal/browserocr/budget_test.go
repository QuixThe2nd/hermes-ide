package browserocr

import (
	"math"
	"testing"
	"time"
)

// The ceiling must clear the deployed worst case by the required margin:
// this is the assertion that fails whenever someone "optimizes" the
// timeout back toward the old 5s suppression regime.
func TestBudgetHonorsMeasuredRuntime(t *testing.T) {
	if !BudgetOK(TimeoutSeconds) {
		t.Errorf("BudgetOK(TimeoutSeconds=%d) = false, want ceiling >= %vx%.2fs",
			TimeoutSeconds, MarginOverMeasured, MeasuredFullImageSeconds)
	}
	if BudgetOK(5) {
		t.Error("BudgetOK(5) = true, want the old 5s ceiling rejected")
	}
}

// EstimateSeconds must reproduce the anchor measurement exactly and scale
// linearly with pixel count in both directions.
func TestEstimateSecondsAnchorsAndScales(t *testing.T) {
	anchor := EstimateSeconds(MeasuredFrameWidth, MeasuredFrameHeight)
	if !approx(anchor, MeasuredFullImageSeconds) {
		t.Errorf("EstimateSeconds(%d,%d) = %.3f, want %.2f",
			MeasuredFrameWidth, MeasuredFrameHeight, anchor, MeasuredFullImageSeconds)
	}
	// Half the pixels must cost half the time.
	half := EstimateSeconds(MeasuredFrameWidth/2, MeasuredFrameHeight)
	if !approx(half, MeasuredFullImageSeconds/2) {
		t.Errorf("EstimateSeconds at half area = %.3f, want %.3f", half, MeasuredFullImageSeconds/2)
	}
	if got := EstimateSeconds(0, 100); got != 0 {
		t.Errorf("EstimateSeconds(0,100) = %.3f, want 0", got)
	}
}

// The measured frame scaled up by the required margin still fits under the
// ceiling, so the margin is real headroom over the anchor, not accounting.
// Frames far beyond that class (the capture path accepts up to 20 MP) keep
// failing closed by design: suppression, never delivery, on timeout.
func TestBudgetCoversMeasuredFrameWithMargin(t *testing.T) {
	scaled := EstimateSeconds(MeasuredFrameWidth*MarginOverMeasured, MeasuredFrameHeight)
	if float64(TimeoutSeconds) < scaled {
		t.Errorf("TimeoutSeconds=%d < %.1fs estimated for a %dx-margin frame",
			TimeoutSeconds, scaled, MarginOverMeasured)
	}
}

// Timeout is the duration form of the same ceiling, not a second policy.
func TestTimeoutDurationMatchesCeiling(t *testing.T) {
	if got := Timeout(); got != 30*time.Second {
		t.Errorf("Timeout() = %s, want %s", got, 30*time.Second)
	}
}

// Single-thread execution is part of the budget: the env must pin tesseract
// to one thread. The map is the exact subprocess environment override the
// Python side merges over build_subprocess_env().
func TestSingleThreadEnvPinsTesseract(t *testing.T) {
	if got := SingleThreadEnv["OMP_THREAD_LIMIT"]; got != "1" {
		t.Errorf("SingleThreadEnv[OMP_THREAD_LIMIT] = %q, want %q", got, "1")
	}
	if len(SingleThreadEnv) != 1 {
		t.Errorf("len(SingleThreadEnv) = %d, want exactly the thread pin", len(SingleThreadEnv))
	}
}

// The no-downscale rule exists because resampling destroys small markers:
// the smallest text tesseract still reads at full size must stay below the
// smallest text real pages render, so "OCR the original frame" is the only
// policy that cannot silently lose a marker.
func TestMarkerFloorBelowTypicalBodyText(t *testing.T) {
	if MarkerPixelFloor >= TypicalBodyTextPixels {
		t.Errorf("MarkerPixelFloor=%d >= TypicalBodyTextPixels=%d; the floor must sit under real body text",
			MarkerPixelFloor, TypicalBodyTextPixels)
	}
}

func approx(got, want float64) bool {
	return math.Abs(got-want) < 1e-9
}
