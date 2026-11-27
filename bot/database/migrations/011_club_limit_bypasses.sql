CREATE TABLE club_limit_bypasses (
 guild_id BIGINT NOT NULL REFERENCES guild_config,
 user_id BIGINT NOT NULL,
 cycle_id BIGINT NOT NULL REFERENCES draft_cycles,
 country_role_id BIGINT NOT NULL,
 club_role_id BIGINT NOT NULL,
 actor_id BIGINT NOT NULL,
 reason TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(guild_id,user_id),
 FOREIGN KEY(guild_id,user_id) REFERENCES players(guild_id,user_id)
);
