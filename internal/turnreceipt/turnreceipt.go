// Package turnreceipt renders the one-page HTML transcript of a finished turn.
//
// The gateway writes this page before it deletes a turn's temporary progress
// bubbles, so the chat keeps a record of the steps the user watched go by.
// This is the canonical renderer. gateway/turn_receipt.py is the runtime twin
// (deployments carry no Go toolchain); testdata/receipt.json is the shared
// fixture both sides must satisfy, and the parity test pins them byte-equal.
//
// Contract:
//   - Control characters other than newline and tab are stripped, then every
//     interpolated value is HTML-escaped. Stripping first means no control
//     byte can split an escape sequence.
//   - The page is standalone: inline CSS, no script, no external requests.
//   - Order is the user message, one bubble per progress row in input order,
//     then the final reply. A part that is blank after trimming is omitted.
package turnreceipt

import (
	"strings"
	"unicode"
)

// Input is one turn's transcript. The JSON names match the Python twin.
type Input struct {
	UserMessage string   `json:"user_message"`
	Rows        []string `json:"rows"`
	FinalReply  string   `json:"final_reply"`
}

// stylesheet is inline on purpose: the page must read with no network access.
// The Python twin carries an identical copy, which the parity test enforces.
const stylesheet = `
body{margin:0;background:#313338;color:#dbdee1;font:15px/1.4 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.meta{margin:0;font-size:12px;color:#949ba4;text-align:center}
.bubble{padding:8px 12px;border-radius:8px;background:#2b2d31;white-space:pre-wrap;overflow-wrap:anywhere}
.user{align-self:flex-end;max-width:80%;background:#5865f2;color:#fff}
.progress{font:13px/1.35 ui-monospace,monospace;background:#1e1f22;color:#b5bac1}
.reply{border-left:3px solid #5865f2}
`

var escaper = strings.NewReplacer(
	"&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;", "'", "&#39;",
)

// clean strips control characters (Unicode category Cc) except newline and tab.
func clean(text string) string {
	stripped := strings.Map(func(r rune) rune {
		if unicode.IsControl(r) && r != '\n' && r != '\t' {
			return -1
		}
		return r
	}, text)
	return escaper.Replace(stripped)
}

// Render returns the complete standalone HTML document for one turn.
func Render(in Input) string {
	var b strings.Builder
	b.WriteString("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n")
	b.WriteString("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n")
	b.WriteString("<meta name=\"color-scheme\" content=\"dark\">\n<title>Turn transcript</title>\n<style>")
	b.WriteString(stylesheet)
	b.WriteString("</style>\n</head>\n<body>\n<main>\n<p class=\"meta\">Turn transcript</p>\n")
	bubble := func(class, text string) {
		if strings.TrimSpace(text) == "" {
			return
		}
		b.WriteString("<div class=\"bubble " + class + "\">" + clean(text) + "</div>\n")
	}
	bubble("user", in.UserMessage)
	for _, row := range in.Rows {
		bubble("progress", row)
	}
	bubble("reply", in.FinalReply)
	b.WriteString("</main>\n</body>\n</html>\n")
	return b.String()
}
