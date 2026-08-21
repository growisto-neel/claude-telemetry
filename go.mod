module github.com/growisto-neel/claude-telemetry

// 1.21 rather than something newer: the only thing that builds this is the
// release workflow and the occasional laptop, and every distro and Homebrew Go
// in circulation satisfies 1.21. Nothing here needs a later language feature.
go 1.21
