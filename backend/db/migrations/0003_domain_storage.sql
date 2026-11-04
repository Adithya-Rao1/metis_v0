-- Wave 2 domain storage (integrator-owned): ranking config, conformers, durable jobs, compound ordering.

-- Ranking config per project (validated by the API against RankingConfig; DB bounds size and type).
ALTER TABLE project ADD COLUMN ranking_config jsonb
  CHECK (ranking_config IS NULL OR (jsonb_typeof(ranking_config) = 'object' AND pg_column_size(ranking_config) < 16384));
GRANT UPDATE (ranking_config, name) ON project TO metis_app;

-- Stored conformer (computed, never experimental) and the method that produced it.
ALTER TABLE compound
  ADD COLUMN conformer_molblock text CHECK (conformer_molblock IS NULL OR length(conformer_molblock) < 200000),
  ADD COLUMN conformer_method text CHECK (conformer_method IS NULL OR length(conformer_method) BETWEEN 1 AND 200),
  ADD COLUMN seq bigint GENERATED ALWAYS AS IDENTITY,
  ADD CONSTRAINT conformer_pair CHECK ((conformer_molblock IS NULL) = (conformer_method IS NULL));
GRANT UPDATE (conformer_molblock, conformer_method, label) ON compound TO metis_app;

-- Durable jobs. Owner-scoped; the worker runs as the app role with the owner's identity set per job.
CREATE TABLE job (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id     uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  project_id   uuid NOT NULL,
  kind         text NOT NULL CHECK (kind IN ('predict','dockprep')),
  state        text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','succeeded','failed','cancelled')),
  ensemble_n   integer CHECK (ensemble_n IN (3,5,10,20)),
  progress     real NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
  error_code   text CHECK (error_code IS NULL OR length(error_code) <= 64),  -- fixed codes only; never structure content
  estimated_seconds real CHECK (estimated_seconds IS NULL OR (estimated_seconds >= 0 AND estimated_seconds < 'Infinity'::float8)),
  created_at   timestamptz NOT NULL DEFAULT now(),
  started_at   timestamptz,
  finished_at  timestamptz,
  UNIQUE (id, owner_id),
  FOREIGN KEY (project_id, owner_id) REFERENCES project(id, owner_id) ON DELETE CASCADE
);
CREATE INDEX ON job (owner_id, created_at DESC);
ALTER TABLE job ENABLE ROW LEVEL SECURITY;
ALTER TABLE job FORCE ROW LEVEL SECURITY;
CREATE POLICY job_owner ON job USING (owner_id = current_user_id()) WITH CHECK (owner_id = current_user_id());
GRANT SELECT, INSERT, UPDATE (state, progress, error_code, started_at, finished_at), DELETE ON job TO metis_app;

ALTER TABLE prediction ADD COLUMN job_id uuid,
  ADD FOREIGN KEY (job_id, owner_id) REFERENCES job(id, owner_id) ON DELETE SET NULL (job_id);
