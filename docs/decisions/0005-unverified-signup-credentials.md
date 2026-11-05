# 0005 Credentials of an unverified sign-up (2026-11, Wave 4, closes F-122)

## Problem
`POST /auth/register` stored the password hash chosen by the first caller for an address and never changed it while the
address was unverified. Anyone could register `victim@x` with a password they knew; when the real owner later signed up
(which only re-sent a verification mail) and verified, the account's password was still the attacker's, and the owner's
own choice was silently ignored (pre-hijacking). The owner could always recover through `/auth/forgot`, and unverified
rows expire after 7 days, but that is not a fix.

## Options
1. **Store no usable credential until verification**, and make the verification step set the password. Strongest, but
   `POST /auth/verify` would then require a password and the web client, the emailed link and every existing test
   helper (`register`, then `verify {token}`) change at once.
2. **Replace the stored hash when the owner registers again** and retire the verification links mailed earlier. The
   newest registration wins. No contract change.
3. Do nothing and rely on the reset flow.

## Decision
Both 2 and an opt-in form of 1, because 2 alone leaves a race (an attacker who registers again after the owner
overwrites the owner's hash) that only a password typed at verification time removes.

- Registering an address that exists and is **not verified** now runs `auth_replace_unverified(user, new_hash)`
  (migration 0006): the hash becomes the newest registrant's, failed-login counters reset, every unused
  `verify_email` token of that account is retired, a fresh token is mailed to the address owner, and an audit row
  `register_replace` (user id only) is written. The response is unchanged, so nothing reveals that the address was
  already known. A **verified** account is never touched (`auth_replace_unverified` refuses it in SQL).
- `POST /auth/verify` accepts an optional `password`. When present, the hash is replaced in the same transaction that
  consumes the token (`auth_verify_email`), after the usual password policy checks (including the email-derived
  rule, which rolls the token back on failure). The person holding the mailbox is the only one who has the token, so
  the password that survives is one they typed at the moment they proved ownership.
- Sessions cannot exist before verification (`/auth/login` answers `email_not_verified`), so no session needs revoking.

## What this does not solve
- Without the `password` field on verify, the surviving password is the newest registrant's. An attacker who keeps
  re-registering right before the owner clicks can still win that race; each attempt mails the owner, is rate limited
  per address (3 per hour by default), and is audited. The web client should therefore send the password on the
  verification page (handoff to the web agent). Until it does, the residual risk is a rate-limited race, not a
  permanent squat.
- An attacker can still make verification mails arrive at an address that did not ask for them (the 3 per hour limit
  applies).

## Consequences
- Tests: `tests/accounts/test_accounts_flow.py::test_reregister_unverified_...` changed from "keeps the hash" to
  "newest registration wins"; QA's `TestSquatting` strict-xfail is now a regression test.
- No schema column changes; two functions owned by `metis_auth` (EXECUTE for `metis_app` only).
