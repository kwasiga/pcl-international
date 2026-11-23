ALTER TABLE guild_config ADD COLUMN paused BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE guild_config ADD COLUMN paused_by BIGINT;
ALTER TABLE guild_config ADD COLUMN paused_at TIMESTAMPTZ;

CREATE TABLE paused_member_events (
 guild_id BIGINT NOT NULL REFERENCES guild_config,
 user_id BIGINT NOT NULL,
 needs_restore BOOLEAN NOT NULL DEFAULT FALSE,
 version BIGINT NOT NULL DEFAULT 1,
 PRIMARY KEY(guild_id,user_id)
);
