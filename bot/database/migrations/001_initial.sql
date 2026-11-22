CREATE TABLE guild_config (
 guild_id BIGINT PRIMARY KEY, staff_role_id BIGINT NOT NULL, opt_in_role_id BIGINT NOT NULL,
 callups_channel_id BIGINT NOT NULL, logs_channel_id BIGINT NOT NULL,
 club_limit INTEGER NOT NULL DEFAULT 5 CHECK (club_limit > 0), revision BIGINT NOT NULL DEFAULT 1,
 resetting BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE country_roles (
 guild_id BIGINT NOT NULL REFERENCES guild_config, role_id BIGINT NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(guild_id,role_id)
);
CREATE TABLE club_roles (
 guild_id BIGINT NOT NULL REFERENCES guild_config, role_id BIGINT NOT NULL,
 enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(guild_id,role_id)
);
CREATE TABLE draft_cycles (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL REFERENCES guild_config,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','resetting','closed')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX one_open_cycle ON draft_cycles(guild_id) WHERE status <> 'closed';
CREATE TABLE players (
 guild_id BIGINT NOT NULL REFERENCES guild_config, user_id BIGINT NOT NULL,
 primary_club_role_id BIGINT, club_conflict BOOLEAN NOT NULL DEFAULT FALSE,
 current_country_role_id BIGINT, present BOOLEAN NOT NULL DEFAULT TRUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(guild_id,user_id)
);
CREATE TABLE waitlist_entries (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL REFERENCES guild_config,
 cycle_id BIGINT NOT NULL REFERENCES draft_cycles, user_id BIGINT NOT NULL,
 proposed_country_role_id BIGINT, registration_source TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','unassigned','blocked','absent','processing','failed','completed','removed')),
 reservation_active BOOLEAN NOT NULL DEFAULT FALSE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK(NOT reservation_active OR proposed_country_role_id IS NOT NULL)
);
CREATE UNIQUE INDEX one_waitlist_entry ON waitlist_entries(guild_id,cycle_id,user_id)
 WHERE status NOT IN ('completed','removed');
CREATE TABLE assignments (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL REFERENCES guild_config,
 cycle_id BIGINT NOT NULL REFERENCES draft_cycles, user_id BIGINT NOT NULL,
 country_role_id BIGINT NOT NULL, club_role_id BIGINT,
 status TEXT NOT NULL DEFAULT 'active', assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 ended_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX one_active_assignment ON assignments(guild_id,user_id) WHERE status='active';
CREATE TABLE assignment_history (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL REFERENCES guild_config,
 cycle_id BIGINT NOT NULL REFERENCES draft_cycles, user_id BIGINT NOT NULL,
 from_country_id BIGINT, to_country_id BIGINT, action TEXT NOT NULL, actor_id BIGINT,
 override_type TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE cycle_exclusions (
 guild_id BIGINT NOT NULL, cycle_id BIGINT NOT NULL REFERENCES draft_cycles,
 user_id BIGINT NOT NULL, country_role_id BIGINT NOT NULL,
 PRIMARY KEY(guild_id,cycle_id,user_id,country_role_id)
);
CREATE TABLE draft_batches (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL REFERENCES guild_config,
 cycle_id BIGINT NOT NULL REFERENCES draft_cycles, status TEXT NOT NULL DEFAULT 'processing',
 started_by BIGINT NOT NULL, started_at TIMESTAMPTZ NOT NULL DEFAULT now(), completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX one_running_batch ON draft_batches(guild_id) WHERE status='processing';
CREATE TABLE draft_batch_items (
 id BIGSERIAL PRIMARY KEY, batch_id BIGINT NOT NULL REFERENCES draft_batches,
 user_id BIGINT NOT NULL, target_country_id BIGINT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT, UNIQUE(batch_id,user_id)
);
CREATE TABLE role_operations (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL REFERENCES guild_config,
 cycle_id BIGINT NOT NULL REFERENCES draft_cycles, user_id BIGINT NOT NULL,
 from_country_id BIGINT, target_country_id BIGINT, action TEXT NOT NULL,
 actor_id BIGINT, override_type TEXT, batch_id BIGINT REFERENCES draft_batches,
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_unfinished_role_operation ON role_operations(guild_id,user_id)
 WHERE status IN ('pending','failed');
CREATE TABLE announcement_outbox (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL REFERENCES guild_config,
 batch_id BIGINT REFERENCES draft_batches, dedupe_key TEXT UNIQUE NOT NULL,
 channel_id BIGINT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 discord_message_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), sent_at TIMESTAMPTZ,
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
CREATE TABLE audit_logs (
 id BIGSERIAL PRIMARY KEY, guild_id BIGINT NOT NULL REFERENCES guild_config,
 actor_id BIGINT, target_user_id BIGINT, action TEXT NOT NULL, details JSONB NOT NULL DEFAULT '{}',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX waitlist_guild ON waitlist_entries(guild_id,status);
CREATE INDEX history_player ON assignment_history(guild_id,user_id,id DESC);
CREATE INDEX unfinished_operations ON role_operations(guild_id,status);
