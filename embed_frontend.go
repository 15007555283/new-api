//go:build !no_frontend

package main

import "embed"

//go:embed web/dist
var buildFS embed.FS

//go:embed web/dist/index.html
var indexPage []byte
