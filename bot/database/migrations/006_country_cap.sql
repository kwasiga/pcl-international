ALTER TABLE guild_config ADD COLUMN country_cap INTEGER CHECK (country_cap > 0);
