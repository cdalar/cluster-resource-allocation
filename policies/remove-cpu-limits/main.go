// Kubewarden policy that removes CPU limits from containers (ADR-0003: CPU limits optional).
//
// Built with TinyGo to WebAssembly (waPC guest); see README.md.
package main

import (
	wapc "github.com/wapc/wapc-guest-tinygo"
)

func main() {
	wapc.RegisterFunctions(wapc.Functions{
		"validate":          validate,
		"validate_settings": validateSettings,
	})
}
