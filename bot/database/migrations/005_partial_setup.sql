ALTER TABLE guild_config ALTER COLUMN staff_role_id DROP NOT NULL;
ALTER TABLE guild_config ALTER COLUMN opt_in_role_id DROP NOT NULL;
ALTER TABLE guild_config ALTER COLUMN callups_channel_id DROP NOT NULL;
ALTER TABLE guild_config ALTER COLUMN logs_channel_id DROP NOT NULL;
