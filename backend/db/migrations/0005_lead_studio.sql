-- Metis Lead Studio (Beta) storage: saved runs, manual-edit sessions (with steps) and scoring profiles.
-- Same rules as 0001..0004: UUID keys, NOT NULL owner_id FK ON DELETE CASCADE (account deletion removes everything),
-- RLS enabled AND forced on current_user_id(), CHECK size limits in the database, grants to metis_app only
-- (nothing for metis_readonly: these rows hold molecular structures). Per-user row caps are enforced by the API.
-- Timestamps are server-set: INSERT is granted on explicit columns that exclude created_at.

-- ---------- saved Lead Studio runs (the SSE event list of a finished run, stored as-is) ----------
CREATE TABLE lead_saved_run (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  title       text NOT NULL CHECK (length(title) BETWEEN 1 AND 120 AND btrim(title) <> '' AND title !~ '[[:cntrl:]]'),
  stage       text NOT NULL CHECK (stage IN ('binding','pharmacology')),
  target_id   text CHECK (target_id IS NULL OR (length(target_id) <= 64 AND target_id ~ '^[A-Za-z0-9_-]+$')),
  seed_smiles text CHECK (seed_smiles IS NULL OR length(seed_smiles) BETWEEN 1 AND 2000),
  headline    text CHECK (headline IS NULL OR length(headline) <= 300),
  events      jsonb NOT NULL CHECK (jsonb_typeof(events) = 'array' AND jsonb_array_length(events) BETWEEN 1 AND 5000
                                    AND pg_column_size(events) < 1900000),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id)
);
CREATE INDEX ON lead_saved_run (owner_id, created_at DESC);

-- ---------- manual-edit sessions and their steps ----------
CREATE TABLE lead_manual_session (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  seed_smiles text NOT NULL CHECK (length(seed_smiles) BETWEEN 1 AND 2000),
  target_id   text CHECK (target_id IS NULL OR (length(target_id) <= 64 AND target_id ~ '^[A-Za-z0-9_-]+$')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id)
);
CREATE INDEX ON lead_manual_session (owner_id, created_at DESC);

CREATE TABLE lead_manual_step (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id      uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  session_id    uuid NOT NULL,
  seq           integer NOT NULL CHECK (seq >= 0 AND seq < 500),
  parent_smiles text NOT NULL CHECK (length(parent_smiles) BETWEEN 1 AND 2000),
  child_smiles  text NOT NULL CHECK (length(child_smiles) BETWEEN 1 AND 2000),
  edit_id       text NOT NULL CHECK (length(edit_id) BETWEEN 1 AND 80 AND edit_id ~ '^[A-Za-z0-9_:.-]+$'),
  site          integer[] NOT NULL CHECK (cardinality(site) <= 64),
  reward_delta  double precision CHECK (reward_delta IS NULL OR reward_delta BETWEEN -1000000 AND 1000000),  -- also rejects NaN
  t             double precision NOT NULL DEFAULT 0 CHECK (t BETWEEN 0 AND 1000000000),
  UNIQUE (session_id, seq),
  FOREIGN KEY (session_id, owner_id) REFERENCES lead_manual_session (id, owner_id) ON DELETE CASCADE
);
CREATE INDEX ON lead_manual_step (owner_id);

-- ---------- scoring profiles (objectives, avoid-SMARTS, edit allow-list; validated by the API) ----------
CREATE TABLE lead_profile (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  name        text NOT NULL CHECK (length(name) BETWEEN 1 AND 80 AND btrim(name) <> '' AND name !~ '[[:cntrl:]]'),
  body        jsonb NOT NULL CHECK (jsonb_typeof(body) = 'object' AND pg_column_size(body) < 16384),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, owner_id)
);
CREATE INDEX ON lead_profile (owner_id, created_at DESC);

-- ---------- RLS: owner only, forced ----------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['lead_saved_run','lead_manual_session','lead_manual_step','lead_profile'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY %I ON %I USING (owner_id = current_user_id()) WITH CHECK (owner_id = current_user_id())', t || '_owner', t);
  END LOOP;
END $$;

-- ---------- privileges: metis_app only ----------
GRANT SELECT, DELETE ON lead_saved_run, lead_manual_session, lead_manual_step, lead_profile TO metis_app;
GRANT INSERT (owner_id, title, stage, target_id, seed_smiles, headline, events) ON lead_saved_run TO metis_app;
GRANT INSERT (owner_id, seed_smiles, target_id) ON lead_manual_session TO metis_app;
GRANT INSERT (owner_id, session_id, seq, parent_smiles, child_smiles, edit_id, site, reward_delta, t) ON lead_manual_step TO metis_app;
GRANT INSERT (owner_id, name, body) ON lead_profile TO metis_app;
GRANT UPDATE (name, body, updated_at) ON lead_profile TO metis_app;
