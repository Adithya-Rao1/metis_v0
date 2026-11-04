-- Metis baseline schema. Security-critical: see docs/DB_SCHEMA.md and docs/THREAT_MODEL.md.
-- Every user-owned table: UUID pk, NOT NULL owner_id, RLS enabled and FORCED.
-- Per-request identity: SET LOCAL metis.user_id = '<uuid>' (set by the API after authentication).


-- ---------- helper ----------
CREATE FUNCTION current_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('metis.user_id', true), '')::uuid
$$;

-- ---------- users ----------
CREATE TABLE app_user (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email_lower     text NOT NULL UNIQUE CHECK (email_lower = lower(email_lower) AND length(email_lower) BETWEEN 3 AND 254),
  password_hash   text NOT NULL,
  email_verified_at timestamptz,
  is_edu          boolean NOT NULL DEFAULT false,
  tier            text NOT NULL DEFAULT 'free' CHECK (tier IN ('free','edu','paid')),
  failed_logins   integer NOT NULL DEFAULT 0 CHECK (failed_logins >= 0),
  locked_until    timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz
);
ALTER TABLE app_user ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_user FORCE ROW LEVEL SECURITY;
CREATE POLICY app_user_self ON app_user USING (id = current_user_id()) WITH CHECK (id = current_user_id());

-- ---------- one-time tokens (email verify, password reset) ----------
CREATE TABLE auth_token (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  purpose     text NOT NULL CHECK (purpose IN ('verify_email','reset_password')),
  token_hash  text NOT NULL UNIQUE CHECK (length(token_hash) = 64),
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE auth_token ENABLE ROW LEVEL SECURITY;
ALTER TABLE auth_token FORCE ROW LEVEL SECURITY;
CREATE POLICY auth_token_owner ON auth_token USING (owner_id = current_user_id()) WITH CHECK (owner_id = current_user_id());

-- ---------- terms and consent (append-only) ----------
CREATE TABLE terms_acceptance (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id      uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  document      text NOT NULL CHECK (document IN ('terms','privacy','disclaimer')),
  version       text NOT NULL CHECK (length(version) BETWEEN 1 AND 32),
  accepted_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON terms_acceptance (owner_id, document, accepted_at DESC);
ALTER TABLE terms_acceptance ENABLE ROW LEVEL SECURITY;
ALTER TABLE terms_acceptance FORCE ROW LEVEL SECURITY;
CREATE POLICY terms_owner ON terms_acceptance USING (owner_id = current_user_id()) WITH CHECK (owner_id = current_user_id());

CREATE TABLE consent (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  scope       text NOT NULL CHECK (scope IN ('train_models','share_dataset','analytics','contact')),
  granted_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz,
  CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);
CREATE UNIQUE INDEX consent_one_active ON consent (owner_id, scope) WHERE revoked_at IS NULL;
ALTER TABLE consent ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent FORCE ROW LEVEL SECURITY;
CREATE POLICY consent_owner ON consent USING (owner_id = current_user_id()) WITH CHECK (owner_id = current_user_id());

-- ---------- projects, compounds, predictions, measurements, notes ----------
CREATE TABLE project (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON project (owner_id);

CREATE TABLE compound (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id     uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  project_id   uuid NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  smiles       text NOT NULL CHECK (length(smiles) BETWEEN 1 AND 2000),
  canonical_smiles text NOT NULL CHECK (length(canonical_smiles) BETWEEN 1 AND 2000),
  label        text CHECK (length(label) <= 200),
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON compound (owner_id);
CREATE INDEX ON compound (project_id);

-- Cap: at most 10 compounds per project (v1 limit, spec 3).
CREATE FUNCTION enforce_compound_cap() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext(NEW.project_id::text));
  IF (SELECT count(*) FROM compound WHERE project_id = NEW.project_id) >= 10 THEN
    RAISE EXCEPTION 'compound cap (10) reached for project' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER compound_cap BEFORE INSERT ON compound FOR EACH ROW EXECUTE FUNCTION enforce_compound_cap();

-- Child owner must equal parent owner (prevents cross-tenant attachment).
CREATE FUNCTION enforce_compound_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.owner_id <> (SELECT owner_id FROM project WHERE id = NEW.project_id) THEN
    RAISE EXCEPTION 'owner mismatch with project' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER compound_owner BEFORE INSERT OR UPDATE ON compound FOR EACH ROW EXECUTE FUNCTION enforce_compound_owner();

CREATE TABLE prediction (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id       uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  compound_id    uuid NOT NULL REFERENCES compound(id) ON DELETE CASCADE,
  endpoint       text NOT NULL CHECK (length(endpoint) BETWEEN 1 AND 64),
  model_version_id uuid NOT NULL,
  ensemble_n     integer NOT NULL CHECK (ensemble_n IN (3,5,10,20)),
  kind           text NOT NULL CHECK (kind IN ('regression','classification')),
  value          double precision NOT NULL,
  interval_low   double precision,
  interval_high  double precision,
  probability    double precision CHECK (probability BETWEEN 0 AND 1),
  coverage       double precision NOT NULL CHECK (coverage > 0 AND coverage < 1),
  sample_size    integer NOT NULL CHECK (sample_size > 0),
  method_note    text NOT NULL CHECK (length(method_note) > 0),
  uncertainty_breakdown jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (kind <> 'regression' OR (interval_low IS NOT NULL AND interval_high IS NOT NULL AND interval_low <= interval_high)),
  CHECK (kind <> 'classification' OR probability IS NOT NULL)
);
CREATE INDEX ON prediction (owner_id);
CREATE INDEX ON prediction (compound_id);

CREATE TABLE measurement (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  compound_id uuid NOT NULL REFERENCES compound(id) ON DELETE CASCADE,
  endpoint    text NOT NULL CHECK (length(endpoint) BETWEEN 1 AND 64),
  value       double precision NOT NULL,
  unit        text CHECK (length(unit) <= 32),
  assay       text CHECK (length(assay) <= 200),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON measurement (owner_id);

CREATE TABLE note (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  project_id  uuid NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  body        text NOT NULL CHECK (length(body) <= 20000),
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON note (owner_id);

-- ---------- grants (sharing; default owner-only) ----------
CREATE TABLE grant_ (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id      uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  grantee_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  permission    text NOT NULL CHECK (permission IN ('view','comment','edit')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  revoked_at    timestamptz,
  CHECK (grantee_id <> owner_id)
);
CREATE UNIQUE INDEX grant_one_active ON grant_ (project_id, grantee_id) WHERE revoked_at IS NULL;

-- ---------- models (global, not user-owned; publish gate) ----------
CREATE TABLE model_version (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  endpoint        text NOT NULL CHECK (length(endpoint) BETWEEN 1 AND 64),
  version         text NOT NULL,
  status          text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published','retired')),
  datasets        jsonb NOT NULL DEFAULT '[]',
  licenses_cleared boolean NOT NULL DEFAULT false,
  limitations     text,
  split_type      text,
  benchmark       jsonb,
  coverage_by_n   jsonb,
  weights_sha256  text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (endpoint, version),
  -- A model cannot be published without data links, cleared licenses, limitations, split, benchmark, coverage (spec 6.5).
  CHECK (status <> 'published' OR (
    jsonb_array_length(datasets) > 0 AND licenses_cleared AND limitations IS NOT NULL AND length(limitations) > 0
    AND split_type IS NOT NULL AND benchmark IS NOT NULL AND coverage_by_n IS NOT NULL AND weights_sha256 IS NOT NULL))
);
ALTER TABLE prediction ADD FOREIGN KEY (model_version_id) REFERENCES model_version(id);

-- ---------- deletion tracking ----------
CREATE TABLE deletion_request (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id      uuid NOT NULL,   -- intentionally no FK: survives the user's row
  scope         text NOT NULL CHECK (scope IN ('account','project')),
  requested_at  timestamptz NOT NULL DEFAULT now(),
  rows_purged_at timestamptz,
  backups_purge_due timestamptz NOT NULL DEFAULT (now() + interval '30 days'),
  backups_purged_at timestamptz
);

-- ---------- audit log (append-only) ----------
CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  at          timestamptz NOT NULL DEFAULT now(),
  actor_id    uuid,
  action      text NOT NULL CHECK (length(action) BETWEEN 1 AND 64),
  resource    text,
  detail      jsonb NOT NULL DEFAULT '{}'
);

-- ---------- RLS for tenant tables ----------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['project','compound','prediction','measurement','note','grant_'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I USING (owner_id = current_user_id()) WITH CHECK (owner_id = current_user_id())', t||'_owner', t);
  END LOOP;
END $$;
-- Grantees may read shared projects and their children (view or higher), read-only at DB level.
CREATE POLICY project_shared_read ON project FOR SELECT USING (EXISTS (
  SELECT 1 FROM grant_ g WHERE g.project_id = project.id AND g.grantee_id = current_user_id() AND g.revoked_at IS NULL));
CREATE POLICY compound_shared_read ON compound FOR SELECT USING (EXISTS (
  SELECT 1 FROM grant_ g WHERE g.project_id = compound.project_id AND g.grantee_id = current_user_id() AND g.revoked_at IS NULL));
CREATE POLICY grant_grantee_read ON grant_ FOR SELECT USING (grantee_id = current_user_id());

-- ---------- append-only enforcement via triggers (defense beyond privileges) ----------
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '42501'; END $$;
CREATE TRIGGER audit_no_update BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER terms_no_update BEFORE UPDATE ON terms_acceptance FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- ---------- roles and privileges ----------
-- Roles are created by deploy tooling (NOLOGIN here; login roles inherit). Names: metis_app, metis_readonly.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='metis_app') THEN CREATE ROLE metis_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='metis_readonly') THEN CREATE ROLE metis_readonly NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF;
END $$;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO metis_app, metis_readonly;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON auth_token, consent, project, compound, prediction, measurement, note, grant_, deletion_request TO metis_app;
-- app_user: no direct INSERT (sign-up goes through auth_register); UPDATE only on password_hash;
-- tier / is_edu / lockout / verification fields change only through definer functions.
GRANT SELECT, DELETE ON app_user TO metis_app;
GRANT UPDATE (password_hash) ON app_user TO metis_app;
GRANT SELECT, INSERT ON terms_acceptance TO metis_app;
GRANT SELECT, INSERT ON audit_log TO metis_app;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO metis_app;
GRANT SELECT ON model_version TO metis_app;
-- Read-only role (backups/analytics): no structures, notes, or credentials.
GRANT SELECT ON model_version, deletion_request TO metis_readonly;
GRANT EXECUTE ON FUNCTION current_user_id() TO metis_app, metis_readonly;

-- ---------- pre-authentication functions (SECURITY DEFINER, minimal surface) ----------
-- The app role cannot read app_user without a user id, so login/sign-up go through these.
-- Each pins search_path and takes only what it needs.
CREATE FUNCTION auth_register(p_email text, p_password_hash text, p_terms_version text, p_is_edu boolean)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE uid uuid;
BEGIN
  IF p_terms_version IS NULL OR length(p_terms_version) = 0 THEN
    RAISE EXCEPTION 'terms acceptance required' USING ERRCODE = '23514';
  END IF;
  INSERT INTO app_user (email_lower, password_hash, is_edu, tier)
    VALUES (lower(p_email), p_password_hash, p_is_edu, CASE WHEN p_is_edu THEN 'edu' ELSE 'free' END)
    RETURNING id INTO uid;
  INSERT INTO terms_acceptance (owner_id, document, version)
    SELECT uid, d, p_terms_version FROM unnest(ARRAY['terms','privacy','disclaimer']) AS d;
  INSERT INTO audit_log (actor_id, action, resource) VALUES (uid, 'register', 'app_user:' || uid);
  RETURN uid;
END $$;

CREATE FUNCTION auth_lookup(p_email text)
RETURNS TABLE (id uuid, password_hash text, email_verified boolean, locked_until timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS $$
  SELECT id, password_hash, email_verified_at IS NOT NULL, locked_until
  FROM app_user WHERE email_lower = lower(p_email) AND deleted_at IS NULL
$$;

CREATE FUNCTION auth_record_login(p_user uuid, p_success boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
  IF p_success THEN
    UPDATE app_user SET failed_logins = 0, locked_until = NULL WHERE id = p_user;
  ELSE
    UPDATE app_user SET failed_logins = failed_logins + 1,
      locked_until = CASE WHEN failed_logins + 1 >= 5 THEN now() + interval '15 minutes' ELSE locked_until END
    WHERE id = p_user;
  END IF;
  INSERT INTO audit_log (actor_id, action, resource) VALUES (p_user, CASE WHEN p_success THEN 'login_ok' ELSE 'login_fail' END, 'app_user:' || p_user);
END $$;

-- Single-use token consumption: returns the owner only if valid, unexpired, unused, right purpose.
CREATE FUNCTION auth_consume_token(p_token_hash text, p_purpose text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE uid uuid;
BEGIN
  UPDATE auth_token SET used_at = now()
   WHERE token_hash = p_token_hash AND purpose = p_purpose AND used_at IS NULL AND expires_at > now()
   RETURNING owner_id INTO uid;
  IF uid IS NOT NULL AND p_purpose = 'verify_email' THEN
    UPDATE app_user SET email_verified_at = now() WHERE id = uid;
  END IF;
  RETURN uid;
END $$;

REVOKE ALL ON FUNCTION auth_register(text,text,text,boolean), auth_lookup(text), auth_record_login(uuid,boolean), auth_consume_token(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_register(text,text,text,boolean), auth_lookup(text), auth_record_login(uuid,boolean), auth_consume_token(text,text) TO metis_app;

-- Re-acceptance: current accepted version per document.
CREATE VIEW terms_current WITH (security_invoker = true) AS
  SELECT DISTINCT ON (owner_id, document) owner_id, document, version, accepted_at
  FROM terms_acceptance ORDER BY owner_id, document, accepted_at DESC;
GRANT SELECT ON terms_current TO metis_app;

-- Definer functions run as a dedicated NOLOGIN role that bypasses RLS but holds only the table
-- privileges these functions need (FORCE RLS would otherwise apply to a table owner).
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='metis_auth') THEN CREATE ROLE metis_auth NOLOGIN NOSUPERUSER BYPASSRLS; END IF;
END $$;
GRANT USAGE ON SCHEMA public TO metis_auth;
GRANT SELECT, INSERT, UPDATE ON app_user TO metis_auth;
GRANT SELECT, UPDATE ON auth_token TO metis_auth;
GRANT INSERT ON terms_acceptance TO metis_auth;
GRANT INSERT ON audit_log TO metis_auth;
GRANT USAGE ON SEQUENCE audit_log_id_seq TO metis_auth;
GRANT EXECUTE ON FUNCTION current_user_id() TO metis_auth;
ALTER FUNCTION auth_register(text,text,text,boolean) OWNER TO metis_auth;
ALTER FUNCTION auth_lookup(text) OWNER TO metis_auth;
ALTER FUNCTION auth_record_login(uuid,boolean) OWNER TO metis_auth;
ALTER FUNCTION auth_consume_token(text,text) OWNER TO metis_auth;
