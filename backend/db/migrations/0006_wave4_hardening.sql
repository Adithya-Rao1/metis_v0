-- Wave 4 hardening (integrator-owned). Same rules as 0001..0005: every SECURITY DEFINER function pins its search_path, has
-- EXECUTE revoked from PUBLIC and granted only to the roles named below, and is owned by a NOLOGIN role that holds just the
-- table privileges the function needs. See docs/DB_SCHEMA.md and docs/decisions/0005-unverified-signup-credentials.md.

-- ---------- F-122: an unverified sign-up must not keep the first caller's password ----------
-- Re-registering an address whose email is still unverified replaces the stored hash with the newest registrant's and
-- retires the verification links mailed earlier, so the last registration (the owner's, who holds the mailbox) wins.
-- A verified or deleted account is never touched. No password hash is returned or logged.
CREATE FUNCTION auth_replace_unverified(p_user uuid, p_password_hash text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  UPDATE app_user SET password_hash = p_password_hash, failed_logins = 0, locked_until = NULL
   WHERE id = p_user AND email_verified_at IS NULL AND deleted_at IS NULL;
  IF NOT FOUND THEN RETURN false; END IF;
  UPDATE auth_token SET used_at = now() WHERE owner_id = p_user AND purpose = 'verify_email' AND used_at IS NULL;
  INSERT INTO audit_log (actor_id, action, resource) VALUES (p_user, 'register_replace', 'app_user:' || p_user);
  RETURN true;
END $$;
REVOKE ALL ON FUNCTION auth_replace_unverified(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_replace_unverified(uuid, text) TO metis_app;
ALTER FUNCTION auth_replace_unverified(uuid, text) OWNER TO metis_auth;

-- Verification that can also set the password: the person holding the mailbox types a password on the verification page,
-- so a password chosen by whoever registered first is never the one that survives. NULL keeps the registered hash.
CREATE FUNCTION auth_verify_email(p_token_hash text, p_new_password_hash text DEFAULT NULL) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE uid uuid;
BEGIN
  uid := auth_consume_token(p_token_hash, 'verify_email');
  IF uid IS NULL THEN RETURN NULL; END IF;
  IF p_new_password_hash IS NOT NULL THEN
    UPDATE app_user SET password_hash = p_new_password_hash, failed_logins = 0, locked_until = NULL WHERE id = uid;
    INSERT INTO audit_log (actor_id, action, resource) VALUES (uid, 'verify_set_password', 'app_user:' || uid);
  END IF;
  RETURN uid;
END $$;
REVOKE ALL ON FUNCTION auth_verify_email(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_verify_email(text, text) TO metis_app;
ALTER FUNCTION auth_verify_email(text, text) OWNER TO metis_auth;

-- ---------- F-125: outstanding reset tokens are retired ----------
-- A completed reset retires every other unused reset token of the account; the account owner's password change retires
-- them through auth_retire_reset_tokens (only for the identity set on the connection). A second "forgot" deliberately does
-- not retire the first token: a user who asks twice because the first mail was slow can still use either mail, and the
-- first successful use kills the other.
CREATE OR REPLACE FUNCTION auth_reset_password(p_token_hash text, p_new_password_hash text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE uid uuid;
BEGIN
  uid := auth_consume_token(p_token_hash, 'reset_password');
  IF uid IS NULL THEN RETURN NULL; END IF;
  UPDATE app_user SET password_hash = p_new_password_hash, failed_logins = 0, locked_until = NULL
   WHERE id = uid AND deleted_at IS NULL AND email_verified_at IS NOT NULL;
  IF NOT FOUND THEN RETURN NULL; END IF;
  UPDATE auth_token SET used_at = now() WHERE owner_id = uid AND purpose = 'reset_password' AND used_at IS NULL;
  INSERT INTO audit_log (actor_id, action, resource) VALUES (uid, 'password_reset', 'app_user:' || uid);
  RETURN uid;
END $$;

CREATE FUNCTION auth_retire_reset_tokens(p_owner uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE n integer;
BEGIN
  IF p_owner IS NULL OR p_owner IS DISTINCT FROM current_user_id() THEN RETURN 0; END IF;  -- only your own tokens
  UPDATE auth_token SET used_at = now() WHERE owner_id = p_owner AND purpose = 'reset_password' AND used_at IS NULL;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION auth_retire_reset_tokens(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_retire_reset_tokens(uuid) TO metis_app;
ALTER FUNCTION auth_retire_reset_tokens(uuid) OWNER TO metis_auth;

-- ---------- F-126: jobs belong to a process, and the process proves it is alive ----------
-- worker_id = the API process that runs the job; heartbeat_at is refreshed by that process while the job is queued/running.
-- Another process fails only rows whose heartbeat is stale (or that carry no worker, i.e. rows from before this migration),
-- never a job whose owner process is alive.
ALTER TABLE job ADD COLUMN worker_id uuid, ADD COLUMN heartbeat_at timestamptz;
GRANT UPDATE (heartbeat_at) ON job TO metis_app;

-- ---------- F-129: the 30-day backup purge list, usable by the roles that need it ----------
-- deletion_request has FORCE RLS keyed on the owner, so a plain SELECT by an operator role returned no rows. Two narrow
-- SECURITY DEFINER functions replace it. metis_retention owns them (no login, no access to anything else but audit inserts);
-- metis_operator is the NOLOGIN group role deployers grant to the person who does the purge.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metis_retention') THEN
    CREATE ROLE metis_retention NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION BYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'metis_operator') THEN
    CREATE ROLE metis_operator NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO metis_retention, metis_operator;
GRANT SELECT (id, scope, requested_at, backups_purge_due, backups_purged_at), UPDATE (backups_purged_at) ON deletion_request TO metis_retention;
GRANT INSERT ON audit_log TO metis_retention;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO metis_retention;

-- Requests whose backup purge is due. No owner id and no user data: only what the operator needs to act.
CREATE FUNCTION deletion_due_for_purge()
RETURNS TABLE (id uuid, scope text, requested_at timestamptz, backups_purge_due timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT d.id, d.scope, d.requested_at, d.backups_purge_due FROM deletion_request d
   WHERE d.backups_purged_at IS NULL AND d.backups_purge_due < now() ORDER BY d.backups_purge_due, d.id
$$;
REVOKE ALL ON FUNCTION deletion_due_for_purge() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deletion_due_for_purge() TO metis_readonly, metis_operator;
ALTER FUNCTION deletion_due_for_purge() OWNER TO metis_retention;

-- Record that the backups for one request were purged. Refuses a request that is not due yet or already recorded
-- (returns false), so the 30-day promise cannot be marked done early. Audited with no actor and the request id only.
CREATE FUNCTION deletion_mark_purged(p_id uuid) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  UPDATE deletion_request SET backups_purged_at = now()
   WHERE id = p_id AND backups_purged_at IS NULL AND backups_purge_due < now();
  IF NOT FOUND THEN RETURN false; END IF;
  INSERT INTO audit_log (actor_id, action, resource) VALUES (NULL, 'backups_purged', 'deletion_request:' || p_id);
  RETURN true;
END $$;
REVOKE ALL ON FUNCTION deletion_mark_purged(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION deletion_mark_purged(uuid) TO metis_operator;
ALTER FUNCTION deletion_mark_purged(uuid) OWNER TO metis_retention;

-- metis_readonly already holds SELECT on deletion_request; without a policy it saw nothing. It may now read the rows that
-- are due (and nothing else), so the original review query works too. Writes still go through deletion_mark_purged().
CREATE POLICY deletion_due_readonly ON deletion_request FOR SELECT TO metis_readonly
  USING (backups_purged_at IS NULL AND backups_purge_due < now());
