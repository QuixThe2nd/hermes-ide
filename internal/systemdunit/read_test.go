package systemdunit

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadTextAcceptsPlainUTF8AndBOM(t *testing.T) {
	body := []byte("[Service]\nEnvironment=HERMES_HOME=/tmp/hermes\n")
	tests := []struct {
		name   string
		prefix []byte
	}{
		{name: "plain"},
		{name: "bom", prefix: utf8BOM},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "hermes-auto-update.service")
			data := append(append([]byte(nil), test.prefix...), body...)
			if err := os.WriteFile(path, data, 0o600); err != nil {
				t.Fatal(err)
			}
			got, err := ReadText(path)
			if err != nil {
				t.Fatal(err)
			}
			if got != string(body) {
				t.Fatalf("ReadText() = %q, want %q", got, body)
			}
		})
	}
}
