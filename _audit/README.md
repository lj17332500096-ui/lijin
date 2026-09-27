# Runtime audit evidence

This directory contains preserved, point-in-time evidence collected during the 2026-09 runtime audit: sanitized summaries, invariant probes, SQLite observations, and captured provider request/response payloads.

- Treat files here as historical evidence, not live configuration or production state.
- Provider payload JSON can contain workspace content and must not be copied into new reports without review and redaction.
- Keep original filenames and contents intact; add a dated subfolder for future audit rounds instead of overwriting evidence.
- Current conclusions and remediation guidance belong in `docs/audits/`.
