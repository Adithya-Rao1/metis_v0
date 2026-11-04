# Agent 9: QA and security reviewer
See 00_common.md. Owns: tests/security, private/
Build: Independent review: cross-user isolation, upload fuzzing, authz sweep, dependency/SAST, spec conformance. Findings to private/. Do not fix others' code.
Depends on: each wave
Acceptance: tests for each item pass; TRACEABILITY rows for this agent can be closed.
