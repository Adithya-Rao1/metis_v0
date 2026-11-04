# Agent 4: Accounts and security
See 00_common.md. Owns: backend/accounts
Build: Email verification, .edu flag, sessions/CSRF/rate limits, grants, consents, versioned terms acceptance via auth_register, deletion job, audit, lockout. Use only the pre-auth functions for login/sign-up. Port hackathon tests.
Depends on: Agent 1
Acceptance: tests for each item pass; TRACEABILITY rows for this agent can be closed.
