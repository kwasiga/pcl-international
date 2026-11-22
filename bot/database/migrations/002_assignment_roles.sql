ALTER TABLE guild_config ADD COLUMN respin_role_id BIGINT;
ALTER TABLE players ADD COLUMN has_respun BOOLEAN NOT NULL DEFAULT FALSE;
-- Preserve current-cycle respin markers when upgrading an existing installation.
UPDATE players p SET has_respun = EXISTS (
 SELECT 1 FROM assignment_history h
 JOIN draft_cycles c ON c.id=h.cycle_id AND c.guild_id=h.guild_id AND c.status <> 'closed'
 WHERE h.guild_id=p.guild_id AND h.user_id=p.user_id
 AND h.action IN ('respin','respin_exception')
 AND h.id > COALESCE((SELECT max(r.id) FROM assignment_history r
   WHERE r.guild_id=p.guild_id AND r.user_id=p.user_id
   AND r.cycle_id=h.cycle_id AND r.action='remove'),0)
) WHERE p.current_country_role_id IS NOT NULL;
