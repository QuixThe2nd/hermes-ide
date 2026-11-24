package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/QuixThe2nd/hermes-ide/internal/readygates"
)

func main() {
	var in readygates.Input
	dec := json.NewDecoder(os.Stdin)
	if err := dec.Decode(&in); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	res := readygates.Decide(in)
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(res); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
