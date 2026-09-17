CREATE TABLE IF NOT EXISTS jobs (
  id BIGSERIAL PRIMARY KEY,
  goal TEXT NOT NULL,
  repository TEXT NOT NULL,
  branch TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  priority INTEGER NOT NULL DEFAULT 0,
  current_phase TEXT,
  current_step BIGINT,
  iteration_count INTEGER NOT NULL DEFAULT 0,
  max_iterations INTEGER NOT NULL DEFAULT 100,
  human_notes TEXT,
  planner_worker_id TEXT,
  planner_lease_expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  paused_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS steps (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES jobs(id),
  sequence INTEGER NOT NULL,
  repository TEXT NOT NULL,
  branch TEXT NOT NULL,
  title TEXT NOT NULL,
  objective TEXT NOT NULL,
  rationale TEXT NOT NULL DEFAULT '',
  acceptance_criteria JSONB NOT NULL DEFAULT '[]',
  constraints JSONB NOT NULL DEFAULT '[]',
  suggested_files JSONB NOT NULL DEFAULT '[]',
  dependencies JSONB NOT NULL DEFAULT '[]',
  assigned_agent TEXT NOT NULL DEFAULT 'engineering-agent',
  status TEXT NOT NULL DEFAULT 'queued',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT,
  lease_expires_at TIMESTAMPTZ,
  starting_commit TEXT,
  resulting_commit TEXT,
  files_changed JSONB NOT NULL DEFAULT '[]',
  model_used TEXT,
  reviewer_feedback JSONB,
  coder_response JSONB,
  blocker TEXT,
  reviewer_worker_id TEXT,
  review_lease_expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  UNIQUE(job_id, sequence)
);

CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES jobs(id),
  step_id BIGINT REFERENCES steps(id),
  agent TEXT NOT NULL,
  event_type TEXT NOT NULL,
  structured_payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS command_runs (
  id BIGSERIAL PRIMARY KEY,
  step_id BIGINT NOT NULL REFERENCES steps(id),
  argv JSONB NOT NULL,
  stdout TEXT NOT NULL,
  stderr TEXT NOT NULL,
  exit_code INTEGER NOT NULL,
  duration_seconds DOUBLE PRECISION NOT NULL,
  timed_out BOOLEAN NOT NULL DEFAULT false,
  source TEXT NOT NULL DEFAULT 'engineering-agent',
  attempt INTEGER,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reviews (
  id BIGSERIAL PRIMARY KEY,
  job_id BIGINT NOT NULL REFERENCES jobs(id),
  step_id BIGINT NOT NULL REFERENCES steps(id),
  review_attempt INTEGER NOT NULL,
  reviewer_worker_id TEXT NOT NULL,
  model TEXT,
  provider TEXT,
  verdict TEXT,
  risk TEXT,
  confidence TEXT,
  summary TEXT,
  acceptance_matrix JSONB NOT NULL DEFAULT '[]',
  deterministic_checks JSONB NOT NULL DEFAULT '{}',
  diff_sha256 TEXT,
  usage JSONB,
  latency_seconds DOUBLE PRECISION,
  lease_expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ,
  UNIQUE(step_id, review_attempt)
);

CREATE TABLE IF NOT EXISTS review_issues (
  id BIGSERIAL PRIMARY KEY,
  review_id BIGINT NOT NULL REFERENCES reviews(id),
  step_id BIGINT NOT NULL REFERENCES steps(id),
  stable_issue_key TEXT NOT NULL,
  severity TEXT NOT NULL,
  category TEXT NOT NULL,
  file TEXT,
  line INTEGER,
  problem TEXT NOT NULL,
  evidence TEXT NOT NULL,
  requested_change TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_planning_idx
ON jobs (status, planner_lease_expires_at, priority DESC, id);
CREATE INDEX IF NOT EXISTS steps_claimable_idx
ON steps (status, lease_expires_at, id);
CREATE INDEX IF NOT EXISTS steps_reviewable_idx
ON steps (status, review_lease_expires_at, id);
