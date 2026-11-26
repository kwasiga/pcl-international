CREATE TABLE player_bans (
 guild_id BIGINT NOT NULL REFERENCES guild_config,
 user_id BIGINT NOT NULL,
 player_name TEXT NOT NULL,
 reason TEXT NOT NULL,
 actor_id BIGINT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(guild_id,user_id)
);
