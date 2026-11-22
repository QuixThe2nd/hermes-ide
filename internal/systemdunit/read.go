// Package systemdunit reads user-editable systemd unit files.
package systemdunit

import (
	"bytes"
	"os"
)

var utf8BOM = []byte{0xef, 0xbb, 0xbf}

// ReadText strips one leading UTF-8 BOM and otherwise returns the file unchanged.
func ReadText(path string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return string(bytes.TrimPrefix(data, utf8BOM)), nil
}
