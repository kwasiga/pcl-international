ALTER TABLE guild_config ADD COLUMN paused_features TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE guild_config ADD CONSTRAINT valid_paused_features CHECK (
 paused_features <@ ARRAY['respins','draft','assignments','announcements']::TEXT[]
);
