-- Wave 3 hardening (integrator-owned). Closes the DB-side items left open by the Wave 2 QA fixes
-- (F-60, F-61 residual, F-65, session_revocation growth, password reset vs. lock). See docs/DB_SCHEMA.md.
-- Every function below is SECURITY DEFINER with a pinned search_path, EXECUTE revoked from PUBLIC and granted to
-- metis_app only. Each one enforces its own safety floor inside the function, so a caller cannot widen what it deletes
-- or unlock accounts without proof.
-- Deleting rows across users needs BYPASSRLS, so the three purge functions are owned by a NEW narrow role,
-- metis_janitor. metis_auth (reachable by anonymous traffic through the pre-login functions) gets NO new privilege:
-- it still cannot delete users (tests/security/test_schema_redteam.py::test_metis_auth_role_is_minimal).
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='metis_janitor') THEN
    CREATE ROLE metis_janitor NOLOGIN NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION BYPASSRLS;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO metis_janitor;
GRANT INSERT ON audit_log TO metis_janitor;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO metis_janitor;

-- ---------- F-65: rate-limit buckets are purged through a function, never by a bare DELETE ----------
-- The floor (2 hours) is longer than every configured window (max 1 hour); the caller cannot shorten it, so the
-- app cannot wipe live buckets to reset its own rate limits.
CREATE FUNCTION rate_limit_purge() RETURNS integer LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
  WITH d AS (DELETE FROM rate_limit_bucket WHERE window_start < now() - interval '2 hours' RETURNING 1)
  SELECT count(*)::integer FROM d
$$;
GRANT SELECT (window_start), DELETE ON rate_limit_bucket TO metis_janitor;
REVOKE DELETE ON rate_limit_bucket FROM metis_app;
REVOKE ALL ON FUNCTION rate_limit_purge() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rate_limit_purge() TO metis_app;
ALTER FUNCTION rate_limit_purge() OWNER TO metis_janitor;

-- ---------- session_revocation growth: drop rows whose token has expired anyway ----------
CREATE FUNCTION session_purge_expired() RETURNS integer LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
  WITH d AS (DELETE FROM session_revocation WHERE expires_at < now() RETURNING 1)
  SELECT count(*)::integer FROM d
$$;
GRANT SELECT (expires_at), DELETE ON session_revocation TO metis_janitor;
REVOKE ALL ON FUNCTION session_purge_expired() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION session_purge_expired() TO metis_app;
ALTER FUNCTION session_purge_expired() OWNER TO metis_janitor;

-- ---------- F-60: a lapsed lock restarts the failure count (no more permanent re-lock by one request) ----------
-- Replaces the function from 0001 (ownership and grants are kept by CREATE OR REPLACE).
-- While a lock is active, a failure changes nothing and a success cannot clear it.
CREATE OR REPLACE FUNCTION auth_record_login(p_user uuid, p_success boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF p_success THEN
    UPDATE app_user SET failed_logins = 0, locked_until = NULL
     WHERE id = p_user AND (locked_until IS NULL OR locked_until < now());
  ELSE
    -- locked_until IS NOT NULL here means the lock has lapsed (the WHERE excludes active locks): count from zero.
    UPDATE app_user SET
      failed_logins = (CASE WHEN locked_until IS NULL THEN failed_logins ELSE 0 END) + 1,
      locked_until  = CASE WHEN (CASE WHEN locked_until IS NULL THEN failed_logins ELSE 0 END) + 1 >= 5
                           THEN now() + interval '15 minutes' ELSE NULL END
     WHERE id = p_user AND (locked_until IS NULL OR locked_until < now());
  END IF;
  IF NOT FOUND THEN RETURN; END IF;
  INSERT INTO audit_log (actor_id, action, resource)
    VALUES (p_user, CASE WHEN p_success THEN 'login_ok' ELSE 'login_fail' END, 'app_user:' || p_user);
END $$;

-- ---------- password reset: the only way to clear a lock is possession of a valid reset token ----------
-- Consumes the single-use token, sets the new hash, clears the lock and counters, audits. Returns the user id,
-- or NULL (no information about why) for an unknown/used/expired token or an unverified/deleted account.
CREATE FUNCTION auth_reset_password(p_token_hash text, p_new_password_hash text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE uid uuid;
BEGIN
  uid := auth_consume_token(p_token_hash, 'reset_password');
  IF uid IS NULL THEN RETURN NULL; END IF;
  UPDATE app_user SET password_hash = p_new_password_hash, failed_logins = 0, locked_until = NULL
   WHERE id = uid AND deleted_at IS NULL AND email_verified_at IS NOT NULL;
  IF NOT FOUND THEN RETURN NULL; END IF;
  INSERT INTO audit_log (actor_id, action, resource) VALUES (uid, 'password_reset', 'app_user:' || uid);
  RETURN uid;
END $$;
REVOKE ALL ON FUNCTION auth_reset_password(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_reset_password(text, text) TO metis_app;
ALTER FUNCTION auth_reset_password(text, text) OWNER TO metis_auth;

-- ---------- F-61: unverified sign-ups expire ----------
-- Deletes accounts whose email was never verified. The minimum age is floored at 1 day inside the function, so a
-- caller cannot delete a registration that is still waiting for its verification mail. Cascades remove any rows.
CREATE FUNCTION auth_purge_unverified(p_min_age interval DEFAULT interval '7 days')
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE n integer;
BEGIN
  WITH d AS (
    DELETE FROM app_user
     WHERE email_verified_at IS NULL AND deleted_at IS NULL
       AND created_at < now() - GREATEST(COALESCE(p_min_age, interval '7 days'), interval '1 day')
    RETURNING id)
  INSERT INTO audit_log (actor_id, action, resource)
    SELECT NULL, 'purge_unverified', 'app_user:' || id FROM d;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
GRANT SELECT (id, email_verified_at, deleted_at, created_at), DELETE ON app_user TO metis_janitor;
REVOKE ALL ON FUNCTION auth_purge_unverified(interval) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_purge_unverified(interval) TO metis_app;
ALTER FUNCTION auth_purge_unverified(interval) OWNER TO metis_janitor;
