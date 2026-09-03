//go:build no_frontend

package main

import "embed"

// buildFS and indexPage are empty stubs when built with -tags no_frontend.
var buildFS embed.FS
var indexPage []byte
