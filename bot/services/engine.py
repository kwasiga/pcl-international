"""Guild-serialized draft state machine and durable Discord role operation journal."""

import json
import logging
import secrets
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord

from bot.services.audit import audit_messages
from bot.services.permissions import require_staff
from bot.services.roles import apply_roles, validate_role
from bot.services.rules import (
    RuleError,
    PlayerBannedError,
    BotPausedError,
    callup_chunks,
    choose_country,
    occupancy,
    primary_club,
    SETUP_LABELS,
    RESPIN_LIMIT,
    respin_notice,
    PAUSE_FEATURES,
    check_paused,
    operation_feature,
)

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, db, bot):
        self.db, self.bot = db, bot

    async def config(
        self, con, gid, *, allow_reset=False, allow_paused=False, required=(), feature=None
    ):
        cfg = await con.fetchrow("SELECT * FROM guild_config WHERE guild_id=$1", gid)
        if not cfg:
            raise RuleError("An Administrator must run /setup first.")
        if not allow_paused:
            check_paused(cfg, feature)
        if cfg["resetting"] and not allow_reset:
            raise RuleError("A reset is in progress. Use /status or /sync to inspect recovery.")
        missing = [SETUP_LABELS.get(key, key) for key in required if not cfg[key]]
        if missing:
            raise RuleError("Finish these settings in /setup first: " + ", ".join(missing) + ".")
        return cfg

    async def is_paused(self, con, gid, *, feature=None):
        if feature:
            return bool(
                await con.fetchval(
                    "SELECT paused OR $2=ANY(paused_features) FROM guild_config WHERE guild_id=$1",
                    gid,
                    feature,
                )
            )
        return bool(await con.fetchval("SELECT paused FROM guild_config WHERE guild_id=$1", gid))

    async def set_paused(self, guild, actor, paused, *, feature="all"):
        if feature != "all" and feature not in PAUSE_FEATURES:
            raise RuleError("Choose a feature from the pause options.")
        # Deliberately avoid the long-running guild lock so /pause can interrupt
        # a draft between players rather than waiting for the entire batch.
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await self.config(con, guild.id, allow_reset=True, allow_paused=True)
                admin = guild.get_member(actor) or await guild.fetch_member(actor)
                await require_staff(self.db, admin, admin=True, connection=con)
                if feature == "all":
                    changed = await con.fetchval(
                        "UPDATE guild_config SET paused=$2,paused_by=CASE WHEN $2 THEN $3::bigint ELSE NULL END,"
                        "paused_at=CASE WHEN $2 THEN now() ELSE NULL END,"
                        "paused_features=CASE WHEN $2 THEN paused_features ELSE '{}'::text[] END,"
                        "revision=revision+1,updated_at=now() "
                        "WHERE guild_id=$1 AND (paused <> $2 OR (NOT $2 AND cardinality(paused_features)>0)) RETURNING guild_id",
                        guild.id,
                        paused,
                        actor,
                    )
                else:
                    changed = await con.fetchval(
                        "UPDATE guild_config SET paused_features=CASE WHEN $3 "
                        "THEN array_append(paused_features,$2::text) ELSE array_remove(paused_features,$2::text) END,"
                        "revision=revision+1,updated_at=now() "
                        "WHERE guild_id=$1 AND ($2=ANY(paused_features)) <> $3 RETURNING guild_id",
                        guild.id,
                        feature,
                        paused,
                    )
                if changed:
                    if feature == "all":
                        await self.audit(
                            con, guild.id, "bot paused" if paused else "bot resumed", actor
                        )
                    else:
                        await self.audit(
                            con,
                            guild.id,
                            "feature paused" if paused else "feature resumed",
                            actor,
                            feature=PAUSE_FEATURES[feature],
                        )
                return bool(changed)

    async def defer_member_event(self, con, gid, uid, restore=False):
        await con.execute(
            "INSERT INTO paused_member_events(guild_id,user_id,needs_restore) VALUES($1,$2,$3) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET "
            "needs_restore=paused_member_events.needs_restore OR EXCLUDED.needs_restore,"
            "version=paused_member_events.version+1",
            gid,
            uid,
            restore,
        )

    async def defer_event(self, member, *, restore=False):
        async with self.db.guild(member.guild.id) as con:
            await self.defer_member_event(con, member.guild.id, member.id, restore)

    @asynccontextmanager
    async def event_connection(self, gid, connection=None):
        if connection is not None:
            yield connection
        else:
            async with self.db.guild(gid) as con:
                yield con

    async def replay_member_events(self, con, guild):
        rows = await con.fetch(
            "SELECT * FROM paused_member_events WHERE guild_id=$1 ORDER BY user_id", guild.id
        )
        for row in rows:
            if await self.is_paused(con, guild.id):
                return
            try:
                member = guild.get_member(row["user_id"])
                if member is None:
                    await self.member_left(
                        SimpleNamespace(guild=guild, id=row["user_id"]), connection=con
                    )
                elif row["needs_restore"]:
                    await self.member_joined(member, connection=con)
                else:
                    await self.refresh_player(con, member)
                    cfg = await self.config(con, guild.id, allow_reset=True)
                    if any(role.id == cfg["opt_in_role_id"] for role in member.roles):
                        try:
                            await self.auto_draft_member(con, member)
                        except BotPausedError:
                            raise
                        except RuleError:
                            pass  # An unfinished operation still owns this player's reservation.
            except BotPausedError as exc:
                if not exc.feature:
                    raise
                continue  # Retain this event and process other unpaused features.
            await con.execute(
                "DELETE FROM paused_member_events WHERE guild_id=$1 AND user_id=$2 AND version=$3",
                guild.id,
                row["user_id"],
                row["version"],
            )
        if rows:
            await self.config(con, guild.id, allow_reset=True)
            await self.reconcile_waitlist(con, guild)

    async def cycle(self, con, gid):
        return await con.fetchval(
            "SELECT id FROM draft_cycles WHERE guild_id=$1 AND status <> 'closed'", gid
        )

    async def role_ids(self, con, gid, kind, enabled=False):
        if kind not in ("country", "club"):
            raise ValueError("Unknown role kind")
        rows = await con.fetch(
            f"SELECT role_id FROM {kind}_roles WHERE guild_id=$1"
            + (" AND enabled" if enabled else ""),
            gid,
        )
        return {r["role_id"] for r in rows}

    async def audit(self, con, gid, action, actor=None, user=None, **details):
        audit_id = await con.fetchval(
            "INSERT INTO audit_logs(guild_id,actor_id,target_user_id,action,details) "
            "VALUES($1,$2,$3,$4,$5::jsonb) RETURNING id",
            gid,
            actor,
            user,
            action,
            json.dumps(details),
        )
        cfg = await con.fetchrow("SELECT logs_channel_id FROM guild_config WHERE guild_id=$1", gid)
        if cfg and cfg["logs_channel_id"]:
            for index, body in enumerate(audit_messages(action, actor, user, details)):
                key = f"audit:{audit_id}" if index == 0 else f"audit:{audit_id}:part:{index}"
                await self.outbox(con, gid, key, cfg["logs_channel_id"], body)

    async def outbox(self, con, gid, key, channel, payload, batch=None):
        await con.execute(
            "INSERT INTO announcement_outbox "
            "(guild_id,dedupe_key,channel_id,payload,batch_id) VALUES($1,$2,$3,$4,$5) "
            "ON CONFLICT(dedupe_key) DO NOTHING",
            gid,
            key,
            channel,
            payload,
            batch,
        )

    async def refresh_player(self, con, member):
        gid, uid = member.guild.id, member.id
        await con.execute(
            "INSERT INTO players(guild_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING", gid, uid
        )
        saved = await con.fetchval(
            "SELECT primary_club_role_id FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid
        )
        clubs = await self.role_ids(con, gid, "club", True)
        club, conflict = primary_club([r.id for r in member.roles], clubs, saved)
        await con.execute(
            "UPDATE players SET primary_club_role_id=$3,club_conflict=$4,present=TRUE,updated_at=now() "
            "WHERE guild_id=$1 AND user_id=$2",
            gid,
            uid,
            club,
            conflict,
        )
        await self.expire_club_bypasses(con, gid, uid)
        return club, conflict

    async def expire_club_bypasses(self, con, gid, uid):
        await con.execute(
            "DELETE FROM club_limit_bypasses b USING players p WHERE b.guild_id=$1 AND b.user_id=$2 "
            "AND p.guild_id=b.guild_id AND p.user_id=b.user_id AND ("
            "b.country_role_id IS DISTINCT FROM p.current_country_role_id OR "
            "b.club_role_id IS DISTINCT FROM p.primary_club_role_id OR "
            "NOT EXISTS(SELECT 1 FROM draft_cycles c WHERE c.id=b.cycle_id AND c.status='active'))",
            gid,
            uid,
        )

    async def has_club_bypass(self, con, gid, uid, country, club):
        return bool(
            await con.fetchval(
                "SELECT 1 FROM club_limit_bypasses b JOIN players p ON p.guild_id=b.guild_id AND p.user_id=b.user_id "
                "JOIN draft_cycles c ON c.id=b.cycle_id AND c.status='active' "
                "WHERE b.guild_id=$1 AND b.user_id=$2 AND b.country_role_id=$3 AND b.club_role_id=$4 "
                "AND p.current_country_role_id=b.country_role_id AND p.primary_club_role_id=b.club_role_id",
                gid,
                uid,
                country,
                club,
            )
        )

    async def set_club_bypass(self, member, actor, enabled=True, reason="Club changed after draft"):
        gid, uid = member.guild.id, member.id
        async with self.db.guild(gid) as con:
            async with con.transaction():
                await self.config(con, gid, feature="assignments")
                await self.idle_player(con, gid, uid)
                if not enabled:
                    row = await con.fetchrow(
                        "DELETE FROM club_limit_bypasses WHERE guild_id=$1 AND user_id=$2 "
                        "RETURNING country_role_id,club_role_id",
                        gid,
                        uid,
                    )
                    if not row:
                        raise RuleError("This player has no club-limit bypass.")
                    country, club = row["country_role_id"], row["club_role_id"]
                else:
                    reason = reason.strip()
                    if not reason or len(reason) > 500:
                        raise RuleError("Enter a bypass reason between 1 and 500 characters.")
                    await self.check_not_banned(con, gid, uid)
                    member = await member.guild.fetch_member(uid)
                    club, conflict = await self.refresh_player(con, member)
                    if conflict:
                        raise RuleError("Select the player's primary club with /club set first.")
                    country = await con.fetchval(
                        "SELECT current_country_role_id FROM players WHERE guild_id=$1 AND user_id=$2",
                        gid,
                        uid,
                    )
                    held = {role.id for role in member.roles}
                    if not country or country not in held:
                        raise RuleError(
                            "The player must have a saved country and still hold its role. Use /assign to reconcile first."
                        )
                    if not club or club not in held:
                        raise RuleError("The player must hold an enabled league club role.")
                    await con.execute(
                        "INSERT INTO club_limit_bypasses(guild_id,user_id,cycle_id,country_role_id,club_role_id,actor_id,reason) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(guild_id,user_id) DO UPDATE SET "
                        "cycle_id=$3,country_role_id=$4,club_role_id=$5,actor_id=$6,reason=$7,created_at=now()",
                        gid,
                        uid,
                        await self.cycle(con, gid),
                        country,
                        club,
                        actor,
                        reason,
                    )
                await self.audit(
                    con,
                    gid,
                    "club-limit bypass enabled" if enabled else "club-limit bypass removed",
                    actor,
                    uid,
                    country=country,
                    club=club,
                    **({"reason": reason} if enabled else {}),
                )
                return country, club

    async def counts(self, con, gid, exclude_user=None, *, guild=None):
        if guild is not None and not guild.chunked:
            await guild.chunk()
        # Include durable pending operations, even if Discord applied a role before a crash.
        # UNION avoids counting the same user/target reservation twice.
        rows = await con.fetch(
            """
          WITH seats AS (
            SELECT user_id,current_country_role_id AS country FROM players
              WHERE guild_id=$1 AND current_country_role_id IS NOT NULL
            UNION
            SELECT user_id,proposed_country_role_id FROM waitlist_entries
              WHERE guild_id=$1 AND reservation_active
            UNION
            SELECT user_id,target_country_id FROM role_operations
              WHERE guild_id=$1 AND status IN ('pending','failed') AND target_country_id IS NOT NULL
          ) SELECT s.user_id,s.country,p.primary_club_role_id AS club,
            EXISTS(SELECT 1 FROM club_limit_bypasses b JOIN draft_cycles c ON c.id=b.cycle_id AND c.status='active'
              WHERE b.guild_id=p.guild_id AND b.user_id=p.user_id AND b.country_role_id=s.country
              AND b.country_role_id=p.current_country_role_id AND b.club_role_id=p.primary_club_role_id) AS club_bypass
          FROM seats s
          JOIN players p ON p.guild_id=$1 AND p.user_id=s.user_id
          WHERE ($2::bigint IS NULL OR s.user_id <> $2)
        """,
            gid,
            exclude_user,
        )
        if guild is None:
            return occupancy(rows)
        countries = await self.role_ids(con, gid, "country")
        configured_clubs = await self.role_ids(con, gid, "club", True)
        # Merge actual country-role holders with saved/reserved places. A player
        # can occupy a place before staff register them or queued events run.
        seats = {(row["user_id"], row["country"]): row["club"] for row in rows}
        bypasses = {
            (row["user_id"], row["country"], row["club"])
            for row in rows
            if row.get("club_bypass", False)
        }
        saved_clubs = {row["user_id"]: row["club"] for row in rows}
        members = {member.id: member for member in guild.members}
        for member in guild.members:
            if member.bot or member.id == exclude_user:
                continue
            held = {role.id for role in member.roles}
            club, _ = primary_club(held, configured_clubs, saved_clubs.get(member.id))
            for country in held & countries:
                seats[member.id, country] = club
        counted = []
        for (uid, country), club in seats.items():
            member = members.get(uid)
            held = {role.id for role in member.roles} if member else set()
            exempt = (uid, country, club) in bypasses and country in held and club in held
            counted.append({"country": country, "club": club, "club_bypass": exempt})
        return occupancy(counted)

    async def flag_capacity(self, con, gid, actor=None, *, guild=None):
        cfg = await self.config(con, gid, allow_reset=True)
        population, clubs = await self.counts(con, gid, guild=guild)
        violations = [
            {"country": country, "club": club, "count": count, "limit": cfg["club_limit"]}
            for (country, club), count in clubs.items()
            if count > cfg["club_limit"]
        ]
        if cfg.get("country_cap") is not None:
            violations.extend(
                {"country": country, "count": count, "limit": cfg["country_cap"]}
                for country, count in population.items()
                if count > cfg["country_cap"]
            )
        if violations:
            await self.audit(con, gid, "capacity violations", actor, violations=violations)
        return violations

    async def goalkeeper_counts(self, con, guild, goalkeeper_role, countries, exclude_user=None):
        keepers = {
            member.id: member
            for member in guild.members
            if not member.bot
            and member.id != exclude_user
            and any(role.id == goalkeeper_role for role in member.roles)
        }
        if not keepers:
            return {}
        occupants = defaultdict(set)
        # Actual country roles also cover GKs who were assigned outside the bot.
        for uid, member in keepers.items():
            for cid in {role.id for role in member.roles} & set(countries):
                occupants[cid].add(uid)
        rows = await con.fetch(
            "WITH seats AS ("
            "SELECT user_id,current_country_role_id AS country FROM players "
            "WHERE guild_id=$1 AND current_country_role_id IS NOT NULL "
            "UNION SELECT user_id,proposed_country_role_id FROM waitlist_entries "
            "WHERE guild_id=$1 AND reservation_active "
            "UNION SELECT user_id,target_country_id FROM role_operations "
            "WHERE guild_id=$1 AND status IN ('pending','failed') AND target_country_id IS NOT NULL"
            ") SELECT user_id,country FROM seats WHERE user_id=ANY($2::bigint[])",
            guild.id,
            sorted(keepers),
        )
        for row in rows:
            if row["user_id"] in keepers and row["country"] in countries:
                occupants[row["country"]].add(row["user_id"])
        return {cid: len(players) for cid, players in occupants.items()}

    async def available(self, con, guild, uid, *, excluded=(), balance=True):
        cfg = await self.config(con, guild.id)
        player = await con.fetchrow(
            "SELECT * FROM players WHERE guild_id=$1 AND user_id=$2", guild.id, uid
        )
        if player["club_conflict"]:
            raise RuleError("Select the player’s primary club with /club set first.")
        countries = await self.role_ids(con, guild.id, "country", True)
        manageable = set()
        for cid in countries:
            try:
                validate_role(guild, guild.get_role(cid))
                manageable.add(cid)
            except RuleError:
                pass
        pop, clubs = await self.counts(con, guild.id, uid, guild=guild)
        goalkeeper_role = cfg.get("international_gk_role_id")
        member = guild.get_member(uid)
        goalkeeper = bool(
            goalkeeper_role and member and any(role.id == goalkeeper_role for role in member.roles)
        )
        keeper_counts = (
            await self.goalkeeper_counts(con, guild, goalkeeper_role, countries, uid)
            if goalkeeper
            else None
        )
        return choose_country(
            manageable,
            pop,
            clubs,
            player["primary_club_role_id"],
            cfg["club_limit"],
            excluded,
            country_cap=cfg.get("country_cap"),
            balance=balance,
            goalkeeper=goalkeeper,
            goalkeeper_counts=keeper_counts,
        )

    async def idle_player(self, con, gid, uid):
        if await con.fetchval(
            "SELECT 1 FROM role_operations WHERE guild_id=$1 AND user_id=$2 "
            "AND status IN ('pending','failed')",
            gid,
            uid,
        ):
            raise RuleError("This player has an unfinished role operation. Run /sync to retry it.")

    async def is_banned(self, con, gid, uid):
        return bool(
            await con.fetchval(
                "SELECT 1 FROM player_bans WHERE guild_id=$1 AND user_id=$2", gid, uid
            )
        )

    async def check_not_banned(self, con, gid, uid):
        if await self.is_banned(con, gid, uid):
            raise PlayerBannedError()

    async def ban_player(self, member, actor, reason="Inactive"):
        if member.bot:
            raise RuleError("Bots cannot participate in the player draft.")
        reason = reason.strip()
        if not reason or len(reason) > 500:
            raise RuleError("Enter a ban reason between 1 and 500 characters.")
        gid, uid = member.guild.id, member.id
        async with self.db.guild(gid) as con:
            async with con.transaction():
                await self.config(con, gid, feature="assignments")
                if await self.is_banned(con, gid, uid):
                    raise RuleError(
                        "This player is already banned. Use /bans or /sync to check their status."
                    )
                opid = await self.prepare_operation(con, member.guild, uid, None, "ban", actor)
                await con.execute(
                    "INSERT INTO player_bans(guild_id,user_id,player_name,reason,actor_id) VALUES($1,$2,$3,$4,$5)",
                    gid,
                    uid,
                    str(member),
                    reason,
                    actor,
                )
                await con.execute(
                    "UPDATE waitlist_entries SET status='removed',reservation_active=FALSE,"
                    "proposed_country_role_id=NULL,updated_at=now() WHERE guild_id=$1 AND user_id=$2 "
                    "AND status NOT IN ('completed','removed')",
                    gid,
                    uid,
                )
                await self.audit(con, gid, "player banned", actor, uid, reason=reason)
            # The ban is durable before Discord changes, so role failures cannot
            # make the player eligible again. The removal remains retryable.
            return await self.execute_operation(con, member.guild, opid)

    async def unban_player(self, guild, uid, actor):
        async with self.db.guild(guild.id) as con:
            async with con.transaction():
                await self.config(con, guild.id, feature="assignments")
                await self.idle_player(con, guild.id, uid)
                removed = await con.fetchval(
                    "DELETE FROM player_bans WHERE guild_id=$1 AND user_id=$2 RETURNING user_id",
                    guild.id,
                    uid,
                )
                if not removed:
                    raise RuleError("This player is not on the ban list.")
                await self.audit(con, guild.id, "player unbanned", actor, uid)

    async def enqueue(self, con, member, source="manual", actor=None, *, refresh=False):
        gid, uid = member.guild.id, member.id
        await self.config(con, gid)
        await self.check_not_banned(con, gid, uid)
        await self.idle_player(con, gid, uid)
        club, conflict = await self.refresh_player(con, member)
        if await con.fetchval(
            "SELECT current_country_role_id FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid
        ):
            if refresh:
                return "active"
            raise RuleError("This player already has an active country assignment.")
        actual = {r.id for r in member.roles} & await self.role_ids(con, gid, "country")
        # Existing roles without database assignments require explicit /assign reconciliation.
        conflict = conflict or bool(actual)
        existing = await con.fetchrow(
            "SELECT * FROM waitlist_entries WHERE guild_id=$1 AND user_id=$2 "
            "AND status NOT IN ('completed','removed')",
            gid,
            uid,
        )
        if existing and not refresh:
            raise RuleError("This player is already on the waitlist.")
        if existing and existing["status"] in ("processing", "failed"):
            return existing["status"]
        if existing:
            await con.execute(
                "UPDATE waitlist_entries SET reservation_active=FALSE WHERE id=$1", existing["id"]
            )
        country = None
        if not conflict:
            reserved = existing["proposed_country_role_id"] if existing else None
            if reserved:
                countries = await self.role_ids(con, gid, "country", True)
                # Revalidate the same seat without drawing a different valid team.
                country = await self.available(
                    con, member.guild, uid, excluded=countries - {reserved}, balance=False
                )
            if country is None:
                country = await self.available(con, member.guild, uid)
        status = "blocked" if conflict else ("pending" if country else "unassigned")
        if existing:
            await con.execute(
                "UPDATE waitlist_entries SET proposed_country_role_id=$2,status=$3,"
                "reservation_active=$4,updated_at=now() WHERE id=$1",
                existing["id"],
                country,
                status,
                country is not None,
            )
        else:
            await con.execute(
                "INSERT INTO waitlist_entries(guild_id,cycle_id,user_id,proposed_country_role_id,"
                "registration_source,status,reservation_active) VALUES($1,$2,$3,$4,$5,$6,$7)",
                gid,
                await self.cycle(con, gid),
                uid,
                country,
                source,
                status,
                country is not None,
            )
        if not existing or (
            existing["status"] != status or existing["proposed_country_role_id"] != country
        ):
            await self.audit(
                con,
                gid,
                "waitlist updated" if existing else "waitlist registration",
                actor,
                uid,
                status=status,
                country=country,
                club=club,
            )
        return status

    async def register(self, member, source="manual", actor=None):
        async with self.db.guild(member.guild.id) as con:
            async with con.transaction():
                return await self.enqueue(con, member, source, actor)

    async def auto_draft_member(self, con, member):
        """Called with the guild lock held, never inside an outer transaction."""
        gid, uid = member.guild.id, member.id
        async with con.transaction():
            cfg = await self.config(con, gid)
            status = await self.enqueue(con, member, "opt_in", refresh=True)
        if status != "pending" or not cfg.get("callups_channel_id"):
            return status
        if "draft" in (cfg.get("paused_features") or ()):
            return "pending"
        try:
            # Commit the reservation first, then journal before touching Discord.
            # A failure preparing the operation leaves the player retryable.
            async with con.transaction():
                row = await con.fetchrow(
                    "SELECT * FROM waitlist_entries WHERE guild_id=$1 AND user_id=$2 "
                    "AND status='pending' AND reservation_active",
                    gid,
                    uid,
                )
                if not row:
                    return "pending"
                opid = await self.prepare_operation(
                    con,
                    member.guild,
                    uid,
                    row["proposed_country_role_id"],
                    "draft",
                    None,
                )
        except BotPausedError as exc:
            if not exc.feature:
                raise
            return "pending"
        except RuleError as exc:
            await self.audit(con, gid, "automatic draft delayed", user=uid, error=str(exc))
            return "pending"
        try:
            return "assigned" if await self.execute_operation(con, member.guild, opid) else "failed"
        except BotPausedError as exc:
            if not exc.feature:
                raise
            return "processing"

    async def process_auto_drafts(self, guild):
        """Drain existing opt-ins after restart/resume or when capacity changes."""
        async with self.db.guild(guild.id) as con:
            if not await con.fetchval("SELECT 1 FROM guild_config WHERE guild_id=$1", guild.id):
                return
            cfg = await self.config(con, guild.id, allow_reset=True, allow_paused=True)
            if (
                cfg.get("paused")
                or cfg["resetting"]
                or "draft" in (cfg.get("paused_features") or ())
            ):
                return
            rows = await con.fetch(
                "SELECT w.user_id,w.registration_source FROM waitlist_entries w "
                "WHERE w.guild_id=$1 AND w.status IN ('pending','blocked','unassigned') "
                "AND NOT EXISTS (SELECT 1 FROM player_bans b WHERE b.guild_id=w.guild_id AND b.user_id=w.user_id) "
                "ORDER BY w.id",
                guild.id,
            )
            goalkeeper = cfg.get("international_gk_role_id")

            def is_goalkeeper(row):
                member = guild.get_member(row["user_id"])
                return bool(member and any(role.id == goalkeeper for role in member.roles))

            for row in sorted(rows, key=lambda row: not is_goalkeeper(row)):
                member = guild.get_member(row["user_id"])
                if not member or member.bot:
                    continue
                if row["registration_source"] != "opt_in" and not any(
                    role.id == cfg["opt_in_role_id"] for role in member.roles
                ):
                    continue
                try:
                    await self.auto_draft_member(con, member)
                except BotPausedError:
                    return
                except RuleError:
                    # Bans, unfinished operations and conflicts do not stall others.
                    continue

    async def remove_waitlist(self, member, actor):
        async with self.db.guild(member.guild.id) as con:
            await self.config(con, member.guild.id)
            await self.idle_player(con, member.guild.id, member.id)
            result = await con.execute(
                "UPDATE waitlist_entries SET status='removed',reservation_active=FALSE,updated_at=now() "
                "WHERE guild_id=$1 AND user_id=$2 AND status NOT IN ('completed','removed')",
                member.guild.id,
                member.id,
            )
            await self.audit(con, member.guild.id, "waitlist removal", actor, member.id)
            return result != "UPDATE 0"

    async def reconcile_waitlist(self, con, guild, *, prioritize_goalkeepers=False):
        # Preserve registration order within each group when preparing GK places first.
        rows = await con.fetch(
            "SELECT * FROM waitlist_entries WHERE guild_id=$1 "
            "AND status IN ('pending','blocked','unassigned','absent') "
            "AND NOT EXISTS (SELECT 1 FROM player_bans b WHERE b.guild_id=waitlist_entries.guild_id "
            "AND b.user_id=waitlist_entries.user_id) ORDER BY id",
            guild.id,
        )
        if not rows:
            return
        if prioritize_goalkeepers:
            cfg = await self.config(con, guild.id)
            goalkeeper_role = cfg.get("international_gk_role_id")
            if goalkeeper_role:

                def is_goalkeeper(row):
                    member = guild.get_member(row["user_id"])
                    return bool(member and any(role.id == goalkeeper_role for role in member.roles))

                rows = sorted(rows, key=lambda row: not is_goalkeeper(row))
        for row in rows:
            member = guild.get_member(row["user_id"])
            if not member:
                await con.execute(
                    "UPDATE waitlist_entries SET status='absent',reservation_active=FALSE,proposed_country_role_id=NULL,"
                    "updated_at=now() WHERE id=$1",
                    row["id"],
                )
            else:
                await self.enqueue(con, member, row["registration_source"], refresh=True)

    async def save_config(self, guild, values, actor, expected_revision=None):
        countries, clubs = set(values["countries"]), set(values["clubs"])
        goalkeeper = values.get("international_gk_role_id")
        if goalkeeper:
            role = guild.get_role(goalkeeper)
            if not role or role.is_default():
                raise RuleError("Select an existing International GK role other than @everyone.")
            if goalkeeper in countries | clubs | {
                values.get("staff_role_id"),
                values.get("opt_in_role_id"),
            }:
                raise RuleError(
                    "The International GK role must be separate from country, club, staff and opt-in roles."
                )
        if values.get("country_cap") is not None and values["country_cap"] < 1:
            raise RuleError("Country player cap must be positive, or blank for no cap.")
        if values["club_limit"] < 1:
            raise RuleError("Club limit must be positive.")
        for role_id in countries:
            validate_role(guild, guild.get_role(role_id))
        if countries & clubs or values["opt_in_role_id"] in countries | clubs:
            raise RuleError("Country, club and opt-in roles must be separate.")
        for key in ("staff_role_id", "opt_in_role_id"):
            if not values.get(key):
                continue
            role = guild.get_role(values[key])
            if not role or role.is_default():
                raise RuleError("Select an existing role other than @everyone.")
        if values.get("opt_in_role_id"):
            validate_role(guild, guild.get_role(values["opt_in_role_id"]))
        for cid in clubs:
            if not guild.get_role(cid) or guild.get_role(cid).is_default():
                raise RuleError("A selected club role no longer exists.")
        for key in ("callups_channel_id", "logs_channel_id"):
            if not values.get(key):
                continue
            channel = guild.get_channel(values.get(key))
            if not isinstance(channel, discord.TextChannel):
                raise RuleError("Select text channels for call-ups and staff logs.")
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages and perms.read_message_history):
                raise RuleError(
                    "The bot needs View Channel, Send Messages and Read Message History in both channels."
                )
        async with self.db.guild(guild.id) as con:
            async with con.transaction():
                old = await con.fetchrow("SELECT * FROM guild_config WHERE guild_id=$1", guild.id)
                if old and old.get("paused", False):
                    raise BotPausedError()
                if goalkeeper:
                    known_teams = await self.role_ids(con, guild.id, "country")
                    known_teams |= await self.role_ids(con, guild.id, "club")
                    if goalkeeper in known_teams:
                        raise RuleError(
                            "The International GK role must be separate from country and club roles, including disabled teams."
                        )
                if (
                    old
                    and old["respin_role_id"] is not None
                    and old["respin_role_id"]
                    in countries
                    | clubs
                    | {
                        values["opt_in_role_id"],
                        values["staff_role_id"],
                        goalkeeper,
                    }
                ):
                    raise RuleError(
                        "The bot's Respun marker must stay separate from country, club, staff, opt-in and International GK roles."
                    )
                if old and old["revision"] != expected_revision:
                    raise RuleError("Setup changed while this panel was open. Reopen /setup.")
                if old and (
                    old["resetting"]
                    or await con.fetchval(
                        "SELECT 1 FROM role_operations WHERE guild_id=$1 AND status IN ('pending','failed')",
                        guild.id,
                    )
                ):
                    raise RuleError(
                        "Resolve running or failed role operations before changing setup."
                    )
                await con.execute(
                    """INSERT INTO guild_config(guild_id,staff_role_id,opt_in_role_id,
                  callups_channel_id,logs_channel_id,club_limit,country_cap,international_gk_role_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                  ON CONFLICT(guild_id) DO UPDATE SET staff_role_id=$2,opt_in_role_id=$3,
                  callups_channel_id=$4,logs_channel_id=$5,club_limit=$6,country_cap=$7,international_gk_role_id=$8,
                  revision=guild_config.revision+1,updated_at=now()""",
                    guild.id,
                    values["staff_role_id"],
                    values["opt_in_role_id"],
                    values["callups_channel_id"],
                    values["logs_channel_id"],
                    values["club_limit"],
                    values.get("country_cap"),
                    goalkeeper,
                )
                for kind, ids in [("country", countries), ("club", clubs)]:
                    await con.execute(
                        f"UPDATE {kind}_roles SET enabled=FALSE WHERE guild_id=$1", guild.id
                    )
                    for role_id in ids:
                        await con.execute(
                            f"INSERT INTO {kind}_roles(guild_id,role_id) VALUES($1,$2) "
                            "ON CONFLICT(guild_id,role_id) DO UPDATE SET enabled=TRUE",
                            guild.id,
                            role_id,
                        )
                if not await self.cycle(con, guild.id):
                    await con.execute("INSERT INTO draft_cycles(guild_id) VALUES($1)", guild.id)
                for member in guild.members:
                    if member.bot:
                        continue
                    if await con.fetchval(
                        "SELECT 1 FROM players WHERE guild_id=$1 AND user_id=$2",
                        guild.id,
                        member.id,
                    ):
                        await self.refresh_player(con, member)
                await self.reconcile_waitlist(con, guild)
                for member in guild.members:
                    if member.bot or not any(
                        r.id == values["opt_in_role_id"] for r in member.roles
                    ):
                        continue
                    known = await con.fetchval(
                        "SELECT current_country_role_id IS NOT NULL OR EXISTS("
                        "SELECT 1 FROM waitlist_entries WHERE guild_id=$1 AND user_id=$2 AND status NOT IN ('completed','removed')) "
                        "FROM players WHERE guild_id=$1 AND user_id=$2",
                        guild.id,
                        member.id,
                    )
                    if not known and not await con.fetchval(
                        "SELECT 1 FROM assignment_history WHERE guild_id=$1 AND user_id=$2",
                        guild.id,
                        member.id,
                    ):
                        try:
                            await self.enqueue(con, member, "opt_in")
                        except PlayerBannedError:
                            continue
                await self.flag_capacity(con, guild.id, actor, guild=guild)
                await self.audit(
                    con,
                    guild.id,
                    "configuration saved",
                    actor,
                    countries=sorted(countries),
                    clubs=sorted(clubs),
                    club_limit=values["club_limit"],
                    country_cap=values.get("country_cap"),
                    international_gk_role_id=goalkeeper,
                )

    async def prepare_operation(
        self,
        con,
        guild,
        uid,
        target,
        action,
        actor,
        *,
        override=False,
        batch=None,
        respin_override=False,
    ):
        gid = guild.id
        cfg = await self.config(
            con,
            gid,
            allow_reset=action == "reset",
            required=("callups_channel_id",) if target else (),
            feature=operation_feature(action),
        )
        if target:
            await self.check_not_banned(con, gid, uid)
        await self.idle_player(con, gid, uid)
        member = guild.get_member(uid)
        if member:
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                member = None
        if member:
            await self.refresh_player(con, member)
        elif target:
            raise RuleError("The player is no longer in the server.")
        player = await con.fetchrow(
            "SELECT * FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid
        )
        if not player:
            await con.execute(
                "INSERT INTO players(guild_id,user_id,present) VALUES($1,$2,$3)",
                gid,
                uid,
                member is not None,
            )
            player = await con.fetchrow(
                "SELECT * FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid
            )
        if action == "remove" and not player["current_country_role_id"]:
            raise RuleError("This player has no active country assignment to remove.")
        if target:
            if target not in await self.role_ids(con, gid, "country", True):
                raise RuleError("Choose an enabled configured national team.")
            validate_role(guild, guild.get_role(target))
            if player["club_conflict"]:
                raise RuleError("Resolve multiple club roles with /club set first.")
            population, clubs = await self.counts(con, gid, uid, guild=guild)
            if (
                cfg.get("country_cap") is not None
                and player["current_country_role_id"] != target
                and population.get(target, 0) >= cfg["country_cap"]
            ):
                raise RuleError(
                    "That country is at its player cap, including reserved places. Choose another country or raise the cap in /setup."
                )
            club = player["primary_club_role_id"]
            exempt = player["current_country_role_id"] == target and await self.has_club_bypass(
                con, gid, uid, target, club
            )
            if (
                club
                and clubs.get((target, club), 0) >= cfg["club_limit"]
                and not override
                and not exempt
            ):
                raise RuleError(
                    "Club limit reached. Reopen /assign to explicitly confirm an override."
                )
        opid = await con.fetchval(
            """INSERT INTO role_operations(guild_id,cycle_id,user_id,
            from_country_id,target_country_id,action,actor_id,override_type,batch_id)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
            gid,
            await self.cycle(con, gid),
            uid,
            player["current_country_role_id"],
            target,
            action,
            actor,
            ",".join(
                kind
                for kind, enabled in (
                    ("club_capacity", override),
                    ("respin_limit", respin_override),
                )
                if enabled
            )
            or None,
            batch,
        )
        # A manual assignment replaces any existing pending reservation atomically.
        await con.execute(
            "UPDATE waitlist_entries SET status='processing',reservation_active=FALSE,updated_at=now() "
            "WHERE guild_id=$1 AND user_id=$2 AND status NOT IN ('completed','removed')",
            gid,
            uid,
        )
        return opid

    async def ensure_respin_role(self, con, guild):
        cfg = await self.config(con, guild.id, allow_reset=True, allow_paused=True)
        role = guild.get_role(cfg["respin_role_id"]) if cfg["respin_role_id"] else None
        if role is None:
            if not guild.me or not guild.me.guild_permissions.manage_roles:
                raise RuleError("The bot needs Manage Roles to create the Respun role.")
            # Reuse a safe same-name role after a crash between Discord creation and
            # persisting its ID, rather than leaving duplicate marker roles behind.
            reserved = await self.role_ids(con, guild.id, "country")
            reserved |= await self.role_ids(con, guild.id, "club")
            reserved |= {
                cfg["opt_in_role_id"],
                cfg["staff_role_id"],
                cfg.get("international_gk_role_id"),
            }
            role = next(
                (
                    r
                    for r in guild.roles
                    if r.name == "Respun"
                    and r.id not in reserved
                    and not r.managed
                    and r.permissions.value == 0
                ),
                None,
            )
            if role is None:
                role = await guild.create_role(
                    name="Respun",
                    colour=discord.Colour.purple(),
                    permissions=discord.Permissions.none(),
                    hoist=False,
                    mentionable=False,
                    reason="PCL International: confirmed respin marker",
                )
            await con.execute(
                "UPDATE guild_config SET respin_role_id=$2 WHERE guild_id=$1", guild.id, role.id
            )
        validate_role(guild, role)
        return role.id

    async def execute_operation(self, con, guild, opid):
        op = await con.fetchrow("SELECT * FROM role_operations WHERE id=$1", opid)
        if op["status"] == "completed":
            return True
        cfg = await self.config(con, guild.id, allow_reset=True, allow_paused=True)
        check_paused(cfg, operation_feature(op.get("action", "")))
        if op.get("target_country_id"):
            await self.check_not_banned(con, guild.id, op["user_id"])
        try:
            already_respun = await con.fetchval(
                "SELECT has_respun FROM players WHERE guild_id=$1 AND user_id=$2",
                guild.id,
                op["user_id"],
            )
            mark_respun = (
                op["clear_country_ids"] is None
                and bool(op["target_country_id"])
                and (op["action"].startswith("respin") or already_respun)
            )
            respin_role_id = (
                await self.ensure_respin_role(con, guild) if mark_respun else cfg["respin_role_id"]
            )
            await apply_roles(
                guild,
                op["user_id"],
                await self.role_ids(con, guild.id, "country"),
                op["target_country_id"],
                f"PCL International: {op['action']}",
                opt_in_role_id=cfg["opt_in_role_id"],
                respin_role_id=respin_role_id,
                mark_respun=mark_respun,
                clear_respun=op["action"] in {"remove", "reset", "clear", "ban"},
                clear_opt_in=op["action"] == "ban",
                clear_country_ids=op["clear_country_ids"],
            )
        except (discord.HTTPException, RuleError) as exc:
            async with con.transaction():
                await con.execute(
                    "UPDATE role_operations SET status='failed',attempts=attempts+1,last_error=$2,"
                    "updated_at=now() WHERE id=$1",
                    opid,
                    str(exc)[:1000],
                )
                await con.execute(
                    "UPDATE waitlist_entries SET status='failed',updated_at=now() "
                    "WHERE guild_id=$1 AND user_id=$2 AND status='processing'",
                    guild.id,
                    op["user_id"],
                )
                if op["batch_id"]:
                    await con.execute(
                        "UPDATE draft_batch_items SET status='failed',attempts=attempts+1,last_error=$3 "
                        "WHERE batch_id=$1 AND user_id=$2",
                        op["batch_id"],
                        op["user_id"],
                        str(exc)[:1000],
                    )
                if op["attempts"] == 0:
                    await self.audit(
                        con,
                        guild.id,
                        "role operation failed",
                        op["actor_id"],
                        op["user_id"],
                        operation=opid,
                        error=str(exc)[:1000],
                    )
            return False
        if op["action"] in {"restore", "role_cleanup", "clear_extra"}:
            async with con.transaction():
                await con.execute(
                    "UPDATE role_operations SET status='completed',attempts=attempts+1,last_error=NULL,updated_at=now() WHERE id=$1",
                    opid,
                )
                await self.audit(
                    con,
                    guild.id,
                    "returning member restored"
                    if op["action"] == "restore"
                    else "assignment roles updated",
                    user=op["user_id"],
                    country=op["target_country_id"],
                )
            return True
        # Commit only after the remote role set has been fetched and verified.
        async with con.transaction():
            gid, uid, target = guild.id, op["user_id"], op["target_country_id"]
            await con.execute(
                "UPDATE assignments SET status='ended',ended_at=now() WHERE guild_id=$1 "
                "AND user_id=$2 AND status='active'",
                gid,
                uid,
            )
            if target:
                await con.execute(
                    "INSERT INTO assignments(guild_id,cycle_id,user_id,country_role_id,club_role_id) "
                    "SELECT $1,$2,$3,$4,primary_club_role_id FROM players WHERE guild_id=$1 AND user_id=$3",
                    gid,
                    op["cycle_id"],
                    uid,
                    target,
                )
                await con.execute(
                    "INSERT INTO cycle_exclusions VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
                    gid,
                    op["cycle_id"],
                    uid,
                    target,
                )
            elif op["action"] in {"remove", "clear", "ban"}:
                await con.execute(
                    "DELETE FROM cycle_exclusions WHERE guild_id=$1 AND user_id=$2 AND cycle_id=$3",
                    gid,
                    uid,
                    op["cycle_id"],
                )
            await con.execute(
                "UPDATE players SET current_country_role_id=$3,has_respun=$4,updated_at=now() WHERE guild_id=$1 AND user_id=$2",
                gid,
                uid,
                target,
                mark_respun,
            )
            await self.expire_club_bypasses(con, gid, uid)
            await con.execute(
                "INSERT INTO assignment_history(guild_id,cycle_id,user_id,from_country_id,to_country_id,"
                "action,actor_id,override_type) VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
                gid,
                op["cycle_id"],
                uid,
                op["from_country_id"],
                target,
                op["action"],
                op["actor_id"],
                op["override_type"],
            )
            if op["action"] != "clear":
                await con.execute(
                    "UPDATE waitlist_entries SET status='completed',reservation_active=FALSE,updated_at=now() "
                    "WHERE guild_id=$1 AND user_id=$2 AND status NOT IN ('completed','removed')",
                    gid,
                    uid,
                )
            await con.execute(
                "UPDATE role_operations SET status='completed',attempts=attempts+1,last_error=NULL,"
                "updated_at=now() WHERE id=$1",
                opid,
            )
            if op["batch_id"]:
                await con.execute(
                    "UPDATE draft_batch_items SET status='completed',attempts=attempts+1,last_error=NULL "
                    "WHERE batch_id=$1 AND user_id=$2",
                    op["batch_id"],
                    uid,
                )
            if target:
                cfg = await self.config(con, gid, allow_reset=True, allow_paused=True)
                old = op["from_country_id"]
                if old:
                    previous = f" from <@&{old}>" if old != target else ""
                    body = f"**<@{uid}> has been reassigned{previous} to <@&{target}>!**"
                else:
                    body = f"**<@{uid}> has been assigned to <@&{target}>!**"
                await self.outbox(con, gid, f"operation:{opid}", cfg["callups_channel_id"], body)
            await self.audit(
                con,
                gid,
                op["action"],
                op["actor_id"],
                uid,
                previous=op["from_country_id"],
                country=target,
                override=op["override_type"],
                operation=opid,
            )
        return True

    async def assign(
        self,
        member,
        country,
        actor,
        *,
        override=False,
        expected=None,
        expected_cycle=None,
        action="manual",
        respin_override=False,
        expected_respins=None,
    ):
        async with self.db.guild(member.guild.id) as con:
            async with con.transaction():
                await self.config(con, member.guild.id, feature=operation_feature(action))
                if (
                    expected_cycle is not None
                    and await self.cycle(con, member.guild.id) != expected_cycle
                ):
                    raise RuleError("The draft cycle changed. Open a new confirmation.")
                await self.refresh_player(con, member)
                current = await con.fetchval(
                    "SELECT current_country_role_id FROM players WHERE guild_id=$1 AND user_id=$2",
                    member.guild.id,
                    member.id,
                )
                if expected is not None and current != expected:
                    raise RuleError("The player’s country changed. Request a new proposal.")
                limit_overridden = False
                if action.startswith("respin"):
                    if not current or current == country:
                        raise RuleError(
                            "The player must have a different active country to respin."
                        )
                    cycle = await self.cycle(con, member.guild.id)
                    count = await self.respins_used(con, member.guild.id, member.id, cycle)
                    if expected_respins is not None and count != expected_respins:
                        raise RuleError("The reroll count changed. Run /respin again to review it.")
                    respin_notice(count, override=respin_override)
                    if respin_override:
                        admin = member.guild.get_member(actor) or await member.guild.fetch_member(
                            actor
                        )
                        await require_staff(self.db, admin, admin=True, connection=con)
                    limit_overridden = respin_override and count >= RESPIN_LIMIT
                    used = await con.fetchval(
                        "SELECT 1 FROM cycle_exclusions WHERE guild_id=$1 AND cycle_id=$2 "
                        "AND user_id=$3 AND country_role_id=$4",
                        member.guild.id,
                        cycle,
                        member.id,
                        country,
                    )
                    if used and action != "respin_exception":
                        raise RuleError(
                            "This player already represented that country. Request a new proposal."
                        )
                    if action == "respin_exception":
                        unused = await self.respin_target(con, member)
                        if unused:
                            raise RuleError(
                                "An unused country is now available. Request a new random respin."
                            )
                opid = await self.prepare_operation(
                    con,
                    member.guild,
                    member.id,
                    country,
                    action,
                    actor,
                    override=override,
                    respin_override=limit_overridden,
                )
            return await self.execute_operation(con, member.guild, opid)

    async def respins_used(self, con, gid, uid, cycle):
        return await con.fetchval(
            "SELECT count(*) FROM assignment_history WHERE guild_id=$1 AND user_id=$2 "
            "AND cycle_id=$3 AND action IN ('respin','respin_exception')",
            gid,
            uid,
            cycle,
        )

    async def respin_target(self, con, member):
        gid, uid = member.guild.id, member.id
        current = await con.fetchval(
            "SELECT current_country_role_id FROM players WHERE guild_id=$1 AND user_id=$2", gid, uid
        )
        if not current:
            raise RuleError("This player has no active country assignment.")
        used = await con.fetch(
            "SELECT country_role_id FROM cycle_exclusions WHERE guild_id=$1 AND cycle_id=$2 AND user_id=$3",
            gid,
            await self.cycle(con, gid),
            uid,
        )
        return await self.available(
            con,
            member.guild,
            uid,
            excluded={current} | {r["country_role_id"] for r in used},
            balance=False,
        )

    async def propose_respin(self, member, *, override_limit=False):
        async with self.db.guild(member.guild.id) as con:
            await self.config(con, member.guild.id, feature="respins")
            await self.check_not_banned(con, member.guild.id, member.id)
            await self.idle_player(con, member.guild.id, member.id)
            await self.refresh_player(con, member)
            cycle = await self.cycle(con, member.guild.id)
            used = await self.respins_used(con, member.guild.id, member.id, cycle)
            respin_notice(used, override=override_limit)
            current = await con.fetchval(
                "SELECT current_country_role_id FROM players WHERE guild_id=$1 AND user_id=$2",
                member.guild.id,
                member.id,
            )
            target = await self.respin_target(con, member)
            exceptions = []
            if target is None:
                countries = await self.role_ids(con, member.guild.id, "country", True)
                for cid in sorted(countries - {current}):
                    # Evaluate capacity without relaxing the club rule.
                    if (
                        await self.available(
                            con,
                            member.guild,
                            member.id,
                            excluded=countries - {cid},
                            balance=False,
                        )
                        == cid
                    ):
                        exceptions.append(cid)
            return current, target, exceptions, cycle, used

    async def needs_override(self, member, target):
        async with self.db.guild(member.guild.id) as con:
            cfg = await self.config(con, member.guild.id, feature="assignments")
            await self.check_not_banned(con, member.guild.id, member.id)
            club, conflict = await self.refresh_player(con, member)
            if conflict:
                raise RuleError("Resolve this player’s primary club first with /club set.")
            if target not in await self.role_ids(con, member.guild.id, "country", True):
                raise RuleError("Choose an enabled configured country role.")
            population, clubs = await self.counts(
                con, member.guild.id, member.id, guild=member.guild
            )
            current = await con.fetchval(
                "SELECT current_country_role_id FROM players WHERE guild_id=$1 AND user_id=$2",
                member.guild.id,
                member.id,
            )
            if (
                cfg.get("country_cap") is not None
                and current != target
                and population.get(target, 0) >= cfg["country_cap"]
            ):
                raise RuleError(
                    "That country is at its player cap, including reserved places. Choose another country or raise the cap in /setup."
                )
            exempt = current == target and await self.has_club_bypass(
                con, member.guild.id, member.id, target, club
            )
            return (
                club is not None
                and clubs.get((target, club), 0) >= cfg["club_limit"]
                and not exempt
            )

    async def draft(self, guild, actor, *, progress=None):
        async def report(text):
            if progress:
                await progress(text)

        await report("Waiting for any ongoing server operation to finish…")
        async with self.db.guild(guild.id) as con:
            async with con.transaction():
                await self.config(con, guild.id, required=("callups_channel_id",), feature="draft")
                if await con.fetchval(
                    "SELECT 1 FROM draft_batches WHERE guild_id=$1 AND status='processing'",
                    guild.id,
                ):
                    raise RuleError("An earlier draft is still running or recovering. Run /sync.")
                if not guild.chunked:
                    await report("Loading server members before checking GK coverage…")
                    await guild.chunk()
                await report("Checking player eligibility and country capacity…")
                await self.reconcile_waitlist(con, guild, prioritize_goalkeepers=True)
                rows = await con.fetch(
                    "SELECT * FROM waitlist_entries WHERE guild_id=$1 AND status='pending' ORDER BY id",
                    guild.id,
                )
                if not rows:
                    raise RuleError("There are no eligible pending players.")
                batch = await con.fetchval(
                    "INSERT INTO draft_batches(guild_id,cycle_id,started_by) VALUES($1,$2,$3) RETURNING id",
                    guild.id,
                    await self.cycle(con, guild.id),
                    actor,
                )
                # Freeze the entire batch and persist all operations before the first Discord call.
                log.info("Preparing draft in guild %s: %s players", guild.id, len(rows))
                for index, row in enumerate(rows, 1):
                    await report(f"Preparing assignments: **{index}/{len(rows)} players**…")
                    await self.prepare_operation(
                        con,
                        guild,
                        row["user_id"],
                        row["proposed_country_role_id"],
                        "draft",
                        actor,
                        batch=batch,
                    )
                    await con.execute(
                        "INSERT INTO draft_batch_items(batch_id,user_id,target_country_id) VALUES($1,$2,$3)",
                        batch,
                        row["user_id"],
                        row["proposed_country_role_id"],
                    )
            ops = await con.fetch(
                "SELECT id FROM role_operations WHERE batch_id=$1 ORDER BY id", batch
            )
            succeeded = 0
            for index, op in enumerate(ops, 1):
                await report(
                    f"Assigning country roles: **{index - 1}/{len(ops)} processed**, "
                    f"**{succeeded} successful**, **{index - 1 - succeeded} awaiting retry**…"
                )
                succeeded += await self.execute_operation(con, guild, op["id"])
            await self.finish_batches(con, guild)
            log.info(
                "Draft finished in guild %s: %s successful, %s awaiting retry",
                guild.id,
                succeeded,
                len(ops) - succeeded,
            )
            return succeeded, len(ops) - succeeded

    async def finish_batches(self, con, guild):
        if await self.is_paused(con, guild.id):
            return
        batches = await con.fetch(
            "SELECT id FROM draft_batches WHERE guild_id=$1 AND status='processing'", guild.id
        )
        for batch in batches:
            # Assignment recovery never publishes squads. Staff reveal them explicitly.
            if not await con.fetchval(
                "SELECT 1 FROM role_operations WHERE batch_id=$1 AND status <> 'completed'",
                batch["id"],
            ):
                await con.execute(
                    "UPDATE draft_batches SET status='completed',completed_at=now() WHERE id=$1",
                    batch["id"],
                )

    async def release_country(self, guild, actor, country=None):
        async with self.db.guild(guild.id) as con:
            async with con.transaction():
                cfg = await self.config(
                    con, guild.id, required=("callups_channel_id",), feature="draft"
                )
                cycle = await self.cycle(con, guild.id)
                managed = await self.role_ids(con, guild.id, "country")
                if country is not None and country not in managed:
                    raise RuleError("Choose a configured national team role.")
                # Existing batch-player sentinels also prevent replaying pre-upgrade call-ups.
                rows = await con.fetch(
                    "SELECT o.id,o.batch_id,o.user_id,o.target_country_id FROM role_operations o "
                    "JOIN players p ON p.guild_id=o.guild_id AND p.user_id=o.user_id "
                    "WHERE o.guild_id=$1 AND o.cycle_id=$2 AND (o.batch_id IS NOT NULL OR o.action='draft') "
                    "AND o.status='completed' AND p.present "
                    "AND p.current_country_role_id=o.target_country_id "
                    "AND NOT EXISTS (SELECT 1 FROM announcement_outbox a "
                    "WHERE a.dedupe_key='batch-player:' || o.id::text) "
                    "AND NOT EXISTS (SELECT 1 FROM role_operations pending "
                    "WHERE pending.guild_id=o.guild_id AND pending.status <> 'completed' "
                    "AND (pending.target_country_id=o.target_country_id "
                    "OR pending.from_country_id=o.target_country_id)) ORDER BY o.id",
                    guild.id,
                    cycle,
                )
                ready = sorted(
                    {r["target_country_id"] for r in rows}
                    & {rid for rid in managed if guild.get_role(rid)}
                )
                if country is not None:
                    if country not in ready:
                        raise RuleError(
                            "That country has no unreleased draft squad ready. Check opt-ins "
                            "or use /sync if role changes are awaiting recovery."
                        )
                elif ready:
                    country = secrets.choice(ready)
                else:
                    raise RuleError(
                        "No unreleased squads are ready. Check opt-ins or use /sync "
                        "if role changes are awaiting recovery."
                    )
                roster = await con.fetch(
                    "SELECT user_id FROM players WHERE guild_id=$1 AND present "
                    "AND current_country_role_id=$2 ORDER BY user_id",
                    guild.id,
                    country,
                )
                # Verify the current squad rather than announcing a stale frozen batch.
                for player in roster:
                    member = await guild.fetch_member(player["user_id"])
                    if country not in {role.id for role in member.roles}:
                        raise RuleError(
                            "A squad member's country role differs from the saved assignment. "
                            "Run /sync and resolve the discrepancy before revealing this squad."
                        )
                pending = [r for r in rows if r["target_country_id"] == country]
                players = [r["user_id"] for r in roster]
                name = discord.utils.escape_markdown(
                    discord.utils.escape_mentions(guild.get_role(country).name)
                )
                for index, chunk in enumerate(
                    callup_chunks({country: players}, country_names={country: name})
                ):
                    await self.outbox(
                        con,
                        guild.id,
                        f"batch:country:{cycle}:{country}:{pending[0]['id']}:{index}",
                        cfg["callups_channel_id"],
                        chunk,
                    )
                for row in pending:
                    await con.execute(
                        "INSERT INTO announcement_outbox(guild_id,batch_id,dedupe_key,channel_id,payload,status) "
                        "VALUES($1,$2,$3,$4,'','marker') ON CONFLICT DO NOTHING",
                        guild.id,
                        row["batch_id"],
                        f"batch-player:{row['id']}",
                        cfg["callups_channel_id"],
                    )
                await self.audit(
                    con,
                    guild.id,
                    "country squad released",
                    actor,
                    country=country,
                    players=len(players),
                )
                return country, len(players)

    async def set_club(self, member, role, actor):
        async with self.db.guild(member.guild.id) as con:
            async with con.transaction():
                await self.config(con, member.guild.id)
                await self.idle_player(con, member.guild.id, member.id)
                if role not in member.roles or role.id not in await self.role_ids(
                    con, member.guild.id, "club", True
                ):
                    raise RuleError("The player must currently hold that enabled club role.")
                await self.refresh_player(con, member)
                await con.execute(
                    "UPDATE players SET primary_club_role_id=$3,club_conflict=FALSE WHERE guild_id=$1 AND user_id=$2",
                    member.guild.id,
                    member.id,
                    role.id,
                )
                await self.expire_club_bypasses(con, member.guild.id, member.id)
                await self.reconcile_waitlist(con, member.guild)
                await self.flag_capacity(con, member.guild.id, actor, guild=member.guild)
                await self.audit(
                    con, member.guild.id, "primary club selected", actor, member.id, club=role.id
                )

    async def clear_countries(self, guild, actor, country=None, expected_cycle=None):
        async with self.db.guild(guild.id) as con:
            async with con.transaction():
                await self.config(con, guild.id, feature="assignments")
                cycle = await self.cycle(con, guild.id)
                if expected_cycle is not None and cycle != expected_cycle:
                    raise RuleError("The draft cycle changed. Reopen /clear.")
                managed = await self.role_ids(con, guild.id, "country")
                if country is not None and country not in managed:
                    raise RuleError("Choose a configured national team role.")
                selected = {country} if country is not None else managed
                if await con.fetchval(
                    "SELECT 1 FROM role_operations WHERE guild_id=$1 "
                    "AND status IN ('pending','failed')",
                    guild.id,
                ):
                    raise RuleError(
                        "Resolve unfinished role changes with /sync before clearing countries."
                    )
                players = {
                    p["user_id"]: p["current_country_role_id"]
                    for p in await con.fetch(
                        "SELECT user_id,current_country_role_id FROM players WHERE guild_id=$1",
                        guild.id,
                    )
                }
                users = {uid for uid, cid in players.items() if cid in selected}
                users |= {m.id for m in guild.members if any(r.id in selected for r in m.roles)}
                operations = []
                for uid in sorted(users):
                    await con.execute(
                        "INSERT INTO players(guild_id,user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
                        guild.id,
                        uid,
                    )
                    current = players.get(uid)
                    target = current if current not in selected else None
                    action = "clear_extra" if target else "clear"
                    opid = await con.fetchval(
                        "INSERT INTO role_operations(guild_id,cycle_id,user_id,from_country_id,target_country_id,action,actor_id,clear_country_ids) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                        guild.id,
                        cycle,
                        uid,
                        current,
                        target,
                        action,
                        actor,
                        sorted(selected),
                    )
                    operations.append(opid)
                await self.audit(
                    con,
                    guild.id,
                    "country clear started",
                    actor,
                    countries=sorted(selected),
                    players=len(users),
                )
            success = 0
            for opid in operations:
                success += await self.execute_operation(con, guild, opid)
            await self.audit(
                con,
                guild.id,
                "country clear finished",
                actor,
                countries=sorted(selected),
                successful=success,
                failed=len(operations) - success,
            )
            return success, len(operations) - success

    async def start_reset(self, guild, actor, expected_cycle=None):
        async with self.db.guild(guild.id) as con:
            async with con.transaction():
                cfg = await self.config(con, guild.id)
                if expected_cycle is not None and await self.cycle(con, guild.id) != expected_cycle:
                    raise RuleError("The draft cycle changed. Reopen /reset.")
                if await con.fetchval(
                    "SELECT 1 FROM role_operations WHERE guild_id=$1 AND status IN ('pending','failed')",
                    guild.id,
                ):
                    raise RuleError(
                        "Resolve unfinished role operations with /sync before resetting."
                    )
                managed = await self.role_ids(con, guild.id, "country")
                users = {
                    r["user_id"]
                    for r in await con.fetch(
                        "SELECT user_id FROM players WHERE guild_id=$1 AND current_country_role_id IS NOT NULL",
                        guild.id,
                    )
                }
                users |= {
                    m.id
                    for m in guild.members
                    if any(r.id in managed or r.id == cfg["respin_role_id"] for r in m.roles)
                }
                for uid in users:
                    await self.prepare_operation(con, guild, uid, None, "reset", actor)
                await con.execute(
                    "UPDATE guild_config SET resetting=TRUE WHERE guild_id=$1", guild.id
                )
                await con.execute(
                    "UPDATE draft_cycles SET status='resetting' WHERE guild_id=$1 AND status='active'",
                    guild.id,
                )
                await con.execute(
                    "UPDATE waitlist_entries SET status='removed',reservation_active=FALSE WHERE guild_id=$1 AND status <> 'completed'",
                    guild.id,
                )
                await self.audit(
                    con,
                    guild.id,
                    "reset started",
                    actor,
                    players=len(users),
                    opt_in=cfg["opt_in_role_id"],
                )
            await self.recover_locked(con, guild)
            return not await con.fetchval(
                "SELECT resetting FROM guild_config WHERE guild_id=$1", guild.id
            )

    async def finish_reset(self, con, guild):
        cfg = await self.config(con, guild.id, allow_reset=True, allow_paused=True)
        if cfg.get("paused", False) or not cfg["resetting"]:
            return
        if await con.fetchval(
            "SELECT 1 FROM role_operations WHERE guild_id=$1 AND action='reset' AND status <> 'completed'",
            guild.id,
        ):
            return
        async with con.transaction():
            await con.execute(
                "UPDATE draft_cycles SET status='closed',completed_at=now() WHERE guild_id=$1 AND status='resetting'",
                guild.id,
            )
            await con.execute("INSERT INTO draft_cycles(guild_id) VALUES($1)", guild.id)
            await con.execute("DELETE FROM cycle_exclusions WHERE guild_id=$1", guild.id)
            await con.execute("DELETE FROM club_limit_bypasses WHERE guild_id=$1", guild.id)
            await con.execute("UPDATE guild_config SET resetting=FALSE WHERE guild_id=$1", guild.id)
            for member in guild.members:
                if not member.bot and any(r.id == cfg["opt_in_role_id"] for r in member.roles):
                    try:
                        await self.enqueue(con, member, "opt_in")
                    except PlayerBannedError:
                        continue
            await self.audit(con, guild.id, "reset completed")

    async def recover_locked(self, con, guild):
        if await self.is_paused(con, guild.id):
            return
        ops = await con.fetch(
            "SELECT id FROM role_operations WHERE guild_id=$1 AND status IN ('pending','failed') ORDER BY id",
            guild.id,
        )
        try:
            for op in ops:
                try:
                    await self.execute_operation(con, guild, op["id"])
                except BotPausedError as exc:
                    if not exc.feature:
                        return
                    # A paused feature must not stall recovery of unrelated actions.
            await self.finish_batches(con, guild)
            await self.finish_reset(con, guild)
            if not await self.is_paused(con, guild.id) and not await con.fetchval(
                "SELECT resetting FROM guild_config WHERE guild_id=$1", guild.id
            ):
                await self.replay_member_events(con, guild)
        except BotPausedError:
            return  # A new pause preserves all remaining operations/events for /resume.

    async def recover(self, guild):
        async with self.db.guild(guild.id) as con:
            if await con.fetchval("SELECT 1 FROM guild_config WHERE guild_id=$1", guild.id):
                await self.recover_locked(con, guild)

    async def member_update(self, before, after, *, connection=None):
        async with self.event_connection(after.guild.id, connection) as con:
            cfg = await con.fetchrow("SELECT * FROM guild_config WHERE guild_id=$1", after.guild.id)
            if not cfg or after.bot:
                return
            if cfg.get("paused", False):
                await self.defer_member_event(con, after.guild.id, after.id)
                return
            if cfg["resetting"]:
                return
            old, new = {r.id for r in before.roles}, {r.id for r in after.roles}
            if old == new:
                return
            async with con.transaction():
                clubs = await self.role_ids(con, after.guild.id, "club")
                known = await con.fetchval(
                    "SELECT 1 FROM players WHERE guild_id=$1 AND user_id=$2",
                    after.guild.id,
                    after.id,
                )
                club_changed = old & clubs != new & clubs
                if club_changed and known:
                    await self.refresh_player(con, after)
                    await self.reconcile_waitlist(con, after.guild)
                    await self.flag_capacity(con, after.guild.id, guild=after.guild)
                    await self.audit(
                        con,
                        after.guild.id,
                        "club roles changed",
                        user=after.id,
                    )
                managed = await self.role_ids(con, after.guild.id, "country")
                if (old & managed) != (new & managed):
                    await con.execute(
                        "DELETE FROM club_limit_bypasses WHERE guild_id=$1 AND user_id=$2 "
                        "AND NOT (country_role_id=ANY($3::bigint[]))",
                        after.guild.id,
                        after.id,
                        sorted(new & managed),
                    )
                    active_op = await con.fetchval(
                        "SELECT 1 FROM role_operations WHERE guild_id=$1 AND user_id=$2 "
                        "AND status IN ('pending','failed')",
                        after.guild.id,
                        after.id,
                    )
                    saved = await con.fetchval(
                        "SELECT current_country_role_id FROM players WHERE guild_id=$1 AND user_id=$2",
                        after.guild.id,
                        after.id,
                    )
                    if not active_op and new & managed != ({saved} if saved else set()):
                        await self.audit(
                            con,
                            after.guild.id,
                            "country role discrepancy",
                            user=after.id,
                            expected=saved,
                            actual=sorted(new & managed),
                        )
            if cfg["opt_in_role_id"] in new - old:
                try:
                    return await self.auto_draft_member(con, after) == "assigned"
                except BotPausedError:
                    raise
                except RuleError:
                    pass  # Already assigned, banned or owned by an unfinished operation.
            return False

    async def member_left(self, member, *, connection=None):
        async with self.event_connection(member.guild.id, connection) as con:
            if await self.is_paused(con, member.guild.id):
                await self.defer_member_event(con, member.guild.id, member.id)
                return
            async with con.transaction():
                await con.execute(
                    "DELETE FROM club_limit_bypasses WHERE guild_id=$1 AND user_id=$2",
                    member.guild.id,
                    member.id,
                )
                await con.execute(
                    "UPDATE players SET present=FALSE WHERE guild_id=$1 AND user_id=$2",
                    member.guild.id,
                    member.id,
                )
                await con.execute(
                    "UPDATE waitlist_entries SET status='absent',reservation_active=FALSE,"
                    "proposed_country_role_id=NULL,updated_at=now() WHERE guild_id=$1 AND user_id=$2 "
                    "AND status IN ('pending','blocked','unassigned')",
                    member.guild.id,
                    member.id,
                )

    async def member_joined(self, member, *, connection=None):
        async with self.event_connection(member.guild.id, connection) as con:
            cfg = await con.fetchrow(
                "SELECT * FROM guild_config WHERE guild_id=$1", member.guild.id
            )
            if not cfg or member.bot:
                return
            if cfg.get("paused", False):
                await self.defer_member_event(con, member.guild.id, member.id, True)
                return
            if cfg["resetting"]:
                return
            await self.refresh_player(con, member)
            op = await con.fetchval(
                "SELECT id FROM role_operations WHERE guild_id=$1 AND user_id=$2 AND status IN ('pending','failed')",
                member.guild.id,
                member.id,
            )
            if op:
                await self.execute_operation(con, member.guild, op)
                return
            if await self.is_banned(con, member.guild.id, member.id):
                return
            saved = await con.fetchval(
                "SELECT current_country_role_id FROM players WHERE guild_id=$1 AND user_id=$2",
                member.guild.id,
                member.id,
            )
            if saved:
                # Returning members alone receive automatic restoration, never ordinary updates.
                async with con.transaction():
                    opid = await con.fetchval(
                        "INSERT INTO role_operations(guild_id,cycle_id,user_id,from_country_id,target_country_id,action) "
                        "VALUES($1,$2,$3,$4,$4,'restore') RETURNING id",
                        member.guild.id,
                        await self.cycle(con, member.guild.id),
                        member.id,
                        saved,
                    )
                await self.execute_operation(con, member.guild, opid)
            else:
                existing = await con.fetchval(
                    "SELECT 1 FROM waitlist_entries WHERE guild_id=$1 AND user_id=$2 AND status='absent'",
                    member.guild.id,
                    member.id,
                )
                if existing or any(r.id == cfg["opt_in_role_id"] for r in member.roles):
                    if any(r.id == cfg["opt_in_role_id"] for r in member.roles):
                        return await self.auto_draft_member(con, member) == "assigned"
                    async with con.transaction():
                        await self.enqueue(con, member, "opt_in", refresh=True)

    async def flush_outbox(self, guild):
        async with self.db.guild(guild.id) as con:
            if await self.is_paused(con, guild.id, feature="announcements"):
                return
            rows = await con.fetch(
                "SELECT * FROM announcement_outbox WHERE guild_id=$1 AND status='pending' ORDER BY id LIMIT 50",
                guild.id,
            )
            for row in rows:
                if await self.is_paused(con, guild.id, feature="announcements"):
                    return
                try:
                    channel = guild.get_channel(row["channel_id"]) or await self.bot.fetch_channel(
                        row["channel_id"]
                    )
                    public = row["dedupe_key"].startswith(("operation:", "batch:"))
                    message = await channel.send(
                        row["payload"],
                        allowed_mentions=discord.AllowedMentions(
                            everyone=False, users=public, roles=public, replied_user=False
                        ),
                    )
                    await con.execute(
                        "UPDATE announcement_outbox SET status='sent',discord_message_id=$2,sent_at=now(),"
                        "attempts=attempts+1,last_error=NULL WHERE id=$1",
                        row["id"],
                        message.id,
                    )
                except discord.HTTPException as exc:
                    await con.execute(
                        "UPDATE announcement_outbox SET attempts=attempts+1,last_error=$2 WHERE id=$1",
                        row["id"],
                        str(exc)[:1000],
                    )
                    log.warning("Outbox delivery failed for guild %s item %s", guild.id, row["id"])

    async def cleanup_assignment_roles(self, con, guild, member, player, cfg):
        target = player["current_country_role_id"]
        if not target:
            return
        held = {r.id for r in member.roles}
        needs_marker = player["has_respun"] and cfg["respin_role_id"] not in held
        if cfg["opt_in_role_id"] not in held and not needs_marker:
            return
        if await self.is_paused(con, guild.id):
            raise BotPausedError()
        if await con.fetchval(
            "SELECT 1 FROM role_operations WHERE guild_id=$1 AND user_id=$2 "
            "AND status IN ('pending','failed')",
            guild.id,
            member.id,
        ):
            return
        # Called only when country roles match the saved assignment. Never restores
        # a country that staff manually removed while the player remained present.
        async with con.transaction():
            opid = await con.fetchval(
                "INSERT INTO role_operations(guild_id,cycle_id,user_id,from_country_id,target_country_id,action) "
                "VALUES($1,$2,$3,$4,$4,'role_cleanup') RETURNING id",
                guild.id,
                await self.cycle(con, guild.id),
                member.id,
                target,
            )
        await self.execute_operation(con, guild, opid)

    async def sync(self, guild, actor=None):
        await self.recover(guild)
        issues = []
        async with self.db.guild(guild.id) as con:
            cfg = await self.config(con, guild.id, allow_reset=True)
            managed = await self.role_ids(con, guild.id, "country")
            for rid in managed:
                try:
                    validate_role(guild, guild.get_role(rid))
                except RuleError as exc:
                    issues.append(f"<@&{rid}>: {exc}")
            for key in ("callups_channel_id", "logs_channel_id"):
                ch = guild.get_channel(cfg[key])
                if not ch or not ch.permissions_for(guild.me).send_messages:
                    issues.append(f"{key}: missing channel or Send Messages permission.")
            players = {
                p["user_id"]: p
                for p in await con.fetch("SELECT * FROM players WHERE guild_id=$1", guild.id)
            }
            for member in guild.members:
                await self.config(con, guild.id, allow_reset=True)
                if member.id in players:
                    await self.refresh_player(con, member)
                actual = {r.id for r in member.roles} & managed
                expected = players.get(member.id, {}).get("current_country_role_id")
                if actual != ({expected} if expected else set()):
                    issues.append(
                        f"<@{member.id}>: saved {f'<@&{expected}>' if expected else 'none'}; roles {', '.join(f'<@&{rid}>' for rid in sorted(actual)) or 'none'}"
                    )
                elif not cfg["resetting"] and member.id in players:
                    await self.cleanup_assignment_roles(con, guild, member, players[member.id], cfg)
            for uid in players:
                if not guild.get_member(uid):
                    await con.execute(
                        "UPDATE players SET present=FALSE WHERE guild_id=$1 AND user_id=$2",
                        guild.id,
                        uid,
                    )
                    await con.execute(
                        "UPDATE waitlist_entries SET status='absent',reservation_active=FALSE WHERE guild_id=$1 AND user_id=$2 AND status IN ('pending','blocked','unassigned')",
                        guild.id,
                        uid,
                    )
            if not cfg["resetting"]:
                async with con.transaction():
                    await self.reconcile_waitlist(con, guild)
                    for member in guild.members:
                        if member.bot or not any(
                            r.id == cfg["opt_in_role_id"] for r in member.roles
                        ):
                            continue
                        if member.id not in players:
                            try:
                                await self.enqueue(con, member, "opt_in")
                            except PlayerBannedError:
                                continue
            pop, clubs = await self.counts(con, guild.id, guild=guild)
            if cfg.get("country_cap") is not None:
                issues.extend(
                    f"<@&{country}>: {count}/{cfg['country_cap']} players including reserved places — over country cap."
                    for country, count in pop.items()
                    if count > cfg["country_cap"]
                )
            for (country, club), count in clubs.items():
                if count > cfg["club_limit"]:
                    issues.append(
                        f"<@&{country}> / <@&{club}>: {count}/{cfg['club_limit']} seats (including reservations)."
                    )
            for p in await con.fetch(
                "SELECT user_id FROM players WHERE guild_id=$1 AND club_conflict", guild.id
            ):
                issues.append(f"<@{p['user_id']}>: primary club requires staff selection.")
            failures = await con.fetch(
                "SELECT id,user_id,last_error FROM role_operations WHERE guild_id=$1 AND status='failed'",
                guild.id,
            )
            issues.extend(f"<@{r['user_id']}>: {r['last_error']}" for r in failures)
            await self.audit(
                con,
                guild.id,
                "synchronization reviewed",
                actor,
                issue_count=len(issues),
                issues=issues[:20],
            )
        return issues
