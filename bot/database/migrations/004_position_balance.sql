CREATE TABLE position_roles (
 guild_id BIGINT NOT NULL REFERENCES guild_config, role_id BIGINT NOT NULL,
 position_group TEXT NOT NULL CHECK(position_group IN ('GK','DEF','MID','ATT')),
 PRIMARY KEY(guild_id,role_id)
);
ALTER TABLE players ADD COLUMN position_groups TEXT[] NOT NULL DEFAULT '{}';
