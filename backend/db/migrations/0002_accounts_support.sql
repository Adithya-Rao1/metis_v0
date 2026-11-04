-- Wave 2 support for accounts: rate limits, session revocation, .edu tier upgrade, token lookup.
-- Integrator-owned. Security notes in docs/DB_SCHEMA.md.

-- Rate-limit buckets keyed by a hash (of ip+route or email+route); holds no tenant data or raw identifiers.
CREATE TABLE rate_limit_bucket (
  key_hash      text NOT NULL CHECK (length(key_hash) = 64),
  window_start  timestamptz NOT NULL,
  hits          integer NOT NULL DEFAULT 1 CHECK (hits >= 0),
  PRIMARY KEY (key_hash, window_start)
);
-- Atomic increment; returns the new hit count for the bucket.
CREATE FUNCTION rate_limit_hit(p_key_hash text, p_window_start timestamptz)
RETURNS integer LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  INSERT INTO rate_limit_bucket (key_hash, window_start) VALUES (p_key_hash, p_window_start)
  ON CONFLICT (key_hash, window_start) DO UPDATE SET hits = rate_limit_bucket.hits + 1
  RETURNING hits
$$;
REVOKE ALL ON FUNCTION rate_limit_hit(text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION rate_limit_hit(text, timestamptz) TO metis_app;
GRANT SELECT, INSERT, UPDATE ON rate_limit_bucket TO metis_auth;
ALTER FUNCTION rate_limit_hit(text, timestamptz) OWNER TO metis_auth;
REVOKE SELECT, INSERT, UPDATE, DELETE ON rate_limit_bucket FROM metis_app;  -- only via the function; purge job uses migration role
GRANT DELETE ON rate_limit_bucket TO metis_app;  -- expired-bucket cleanup only

-- Session revocation list (logout / password change). Rows are per user.
CREATE TABLE session_revocation (
  jti         uuid PRIMARY KEY,
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  expires_at  timestamptz NOT NULL,
  revoked_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON session_revocation (owner_id);
ALTER TABLE session_revocation ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_revocation FORCE ROW LEVEL SECURITY;
CREATE POLICY session_rev_owner ON session_revocation USING (owner_id = current_user_id()) WITH CHECK (owner_id = current_user_id());
GRANT SELECT, INSERT ON session_revocation TO metis_app;
-- Is a token id revoked? Callable before identity is known (the API checks this while authenticating).
CREATE FUNCTION session_is_revoked(p_jti uuid) RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$ SELECT EXISTS (SELECT 1 FROM session_revocation WHERE jti = p_jti) $$;
REVOKE ALL ON FUNCTION session_is_revoked(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION session_is_revoked(uuid) TO metis_app;
GRANT SELECT ON session_revocation TO metis_auth;
ALTER FUNCTION session_is_revoked(uuid) OWNER TO metis_auth;

-- .edu tier: only after email verification, and only if the stored address ends in .edu (checked here, not by the caller).
CREATE FUNCTION auth_upgrade_edu(p_user uuid) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE ok boolean;
BEGIN
  UPDATE app_user SET is_edu = true, tier = CASE WHEN tier = 'paid' THEN 'paid' ELSE 'edu' END
   WHERE id = p_user AND email_verified_at IS NOT NULL AND email_lower ~ '@([a-z0-9-]+\.)+edu$' AND deleted_at IS NULL;
  ok := FOUND;
  IF ok THEN INSERT INTO audit_log (actor_id, action, resource) VALUES (p_user, 'edu_upgrade', 'app_user:' || p_user); END IF;
  RETURN ok;
END $$;
REVOKE ALL ON FUNCTION auth_upgrade_edu(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_upgrade_edu(uuid) TO metis_app;
ALTER FUNCTION auth_upgrade_edu(uuid) OWNER TO metis_auth;

-- Password change by the account owner (UPDATE(password_hash) is already granted) also revokes nothing by itself;
-- the API records session_revocation rows. Lock/failed_login state stays inside auth_record_login.
