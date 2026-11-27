from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.audit import override_name
from bot.services.permissions import require_staff
from bot.services.rules import (
    PAUSE_FEATURES,
    RESPIN_LIMIT,
    RuleError,
    goalkeeper_groups,
    respin_notice,
)
from bot.views.common import Confirmation, Pages, StaffView
from bot.views.setup import SetupPanel


class StaffCommands(commands.Cog):
    waitlist = app_commands.Group(
        name="waitlist", description="Review and manage pending national call-ups", guild_only=True
    )
    club = app_commands.Group(
        name="club", description="Manage primary league clubs", guild_only=True
    )
    draft = app_commands.Group(
        name="draft", description="Assign players and reveal national squads", guild_only=True
    )

    def __init__(self, bot):
        self.bot, self.engine, self.db = bot, bot.engine, bot.db

    async def cog_app_command_error(self, interaction, error):
        from bot.views.common import reply_error

        await reply_error(interaction, getattr(error, "original", error))

    async def check(self, i, admin=False):
        # Acknowledge first so a busy database cannot expire Discord's initial response.
        await i.response.defer(ephemeral=True)
        await require_staff(self.db, i.user, admin=admin)

    async def pages(self, i, title, lines, *, staff_only=True, footer=None, page_breaks=()):
        view = Pages(
            self.db,
            i.user.id,
            title,
            lines,
            staff_only=staff_only,
            footer=footer,
            page_breaks=page_breaks,
        )
        await i.followup.send(embed=view.embed(), view=view, ephemeral=True)
        view.message = await i.original_response()

    async def confirm(self, i, text, action, admin=False, *, progress=False):
        view = Confirmation(self.db, i.user.id, action, admin=admin, progress=progress)
        await i.followup.send(text, view=view, ephemeral=True)
        view.message = await i.original_response()

    @app_commands.command(name="setup", description="Configure PCL International")
    @app_commands.guild_only()
    async def setup_panel(self, i: discord.Interaction):
        await self.check(i, True)
        async with self.db.pool.acquire() as con:
            cfg = await con.fetchrow("SELECT * FROM guild_config WHERE guild_id=$1", i.guild_id)
            countries = await self.engine.role_ids(con, i.guild_id, "country", True)
            clubs = await self.engine.role_ids(con, i.guild_id, "club", True)
        panel = SetupPanel(self.engine, i.user.id, i.guild, cfg, countries, clubs)
        await i.followup.send(embed=panel.embed(), view=panel, ephemeral=True)
        panel.message = await i.original_response()

    @waitlist.command(name="view", description="View pending, blocked and absent players")
    async def waitlist_view(self, i: discord.Interaction):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT w.*,p.primary_club_role_id FROM waitlist_entries w JOIN players p "
                "USING(guild_id,user_id) WHERE w.guild_id=$1 AND w.status NOT IN ('removed','completed') ORDER BY w.id",
                i.guild_id,
            )
        totals = defaultdict(int)
        for r in rows:
            totals[r["status"]] += 1
        lines = [" • ".join(f"{s}: {n}" for s, n in sorted(totals.items())) or "Waitlist is empty."]
        for r in rows:
            club = (
                f"<@&{r['primary_club_role_id']}>"
                if r["primary_club_role_id"]
                else "Unaffiliated / unresolved"
            )
            country = (
                f"<@&{r['proposed_country_role_id']}>"
                if r["proposed_country_role_id"]
                else "No country"
            )
            lines.append(f"<@{r['user_id']}> — {club} → {country} — **{r['status']}**")
        await self.pages(i, "PCL International — Waitlist", lines)

    @waitlist.command(
        name="add", description="Add a player, including players without the opt-in role"
    )
    async def waitlist_add(self, i: discord.Interaction, player: discord.Member):
        await self.check(i)
        if player.bot:
            raise RuleError("Bots cannot enter the player draft.")
        status = await self.engine.register(player, "manual", i.user.id)
        await i.followup.send(f"{player.mention} added: **{status}**.", ephemeral=True)

    @waitlist.command(
        name="remove", description="Remove a pending player and release their reservation"
    )
    async def waitlist_remove(self, i: discord.Interaction, player: discord.Member):
        await self.check(i)
        removed = await self.engine.remove_waitlist(player, i.user.id)
        await i.followup.send(
            "Waitlist entry removed." if removed else "No pending entry found.", ephemeral=True
        )

    @draft.command(name="run", description="Assign pending players and announce each assignment")
    async def draft_run(self, i: discord.Interaction):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            await self.engine.config(con, i.guild_id, feature="draft")
            counts = await con.fetch(
                "SELECT status,count(*) AS n FROM waitlist_entries WHERE guild_id=$1 "
                "AND status NOT IN ('completed','removed') GROUP BY status",
                i.guild_id,
            )
        text = "Confirm the official draft? All eligible pending players will be processed.\n"
        text += "\n".join(f"**{r['status']}:** {r['n']}" for r in counts) or "No pending players."
        text += "\nEligibility and capacity are checked again before freezing the batch."
        text += "\nEach successful assignment will be announced in the call-ups channel."

        async def run(click, report):
            success, failed = await self.engine.draft(i.guild, click.user.id, progress=report)
            result = f"Draft finished: **{success} successful**, **{failed} awaiting retry**."
            if success:
                await report(
                    f"**{success} successful**, **{failed} awaiting retry**. "
                    "Publishing assignment announcements…"
                )
                await self.engine.flush_outbox(i.guild)
                result += (
                    " Successful assignments are queued for the call-ups channel. "
                    "Use /draft country for a separate full-squad announcement."
                )
            if failed:
                result += (
                    " Check the staff logs, resolve the reported issue, then run /sync to retry."
                )
            return result

        await self.confirm(i, text, run, progress=True)

    @draft.command(
        name="country", description="Reveal one full squad; omit country for a random one"
    )
    @app_commands.describe(country="Country to reveal; leave empty for a random unreleased squad")
    async def draft_country(self, i: discord.Interaction, country: discord.Role | None = None):
        await self.check(i)
        selected, count = await self.engine.release_country(
            i.guild, i.user.id, country.id if country else None
        )
        await self.engine.flush_outbox(i.guild)
        await i.followup.send(
            f"<@&{selected}>: **{count} players** queued for the call-ups channel. "
            "Run /draft country again for the next squad.",
            ephemeral=True,
        )

    @app_commands.command(description="Manually assign or transfer a player to a country")
    @app_commands.guild_only()
    async def assign(self, i: discord.Interaction, player: discord.Member, country: discord.Role):
        await self.check(i)
        if player.bot:
            raise RuleError("Bots cannot be assigned.")
        override = await self.engine.needs_override(player, country.id)

        async def execute(click):
            success = await self.engine.assign(player, country.id, click.user.id, override=override)
            return (
                "Assignment completed. Call-up queued."
                if success
                else "Role change needs recovery. Use /sync after fixing permissions."
            )

        if override:
            await self.confirm(
                i,
                f"**Club-capacity override:** assign {player.mention} to {country.mention} above the club limit? This override will be logged.",
                execute,
            )
        else:
            await i.followup.send(await execute(i), ephemeral=True)

    @app_commands.command(description="Propose a reroll; three per player per draft cycle")
    @app_commands.guild_only()
    @app_commands.describe(override_limit="Admin approval to exceed the three-reroll limit")
    async def respin(
        self, i: discord.Interaction, player: discord.Member, override_limit: bool = False
    ):
        await self.check(i)
        current, target, exceptions, cycle, used = await self.engine.propose_respin(
            player, override_limit=override_limit
        )
        notice = respin_notice(used, override=override_limit)

        async def apply(click, country, action="respin"):
            ok = await self.engine.assign(
                player,
                country,
                click.user.id,
                expected=current,
                expected_cycle=cycle,
                action=action,
                respin_override=override_limit,
                expected_respins=used,
            )
            return (
                f"Respin completed. **{max(0, RESPIN_LIMIT - used - 1)} rerolls remaining**. Call-up queued."
                if ok
                else "Respin recorded for recovery. It counts once recovery succeeds. "
                "Use /sync after resolving the role error."
            )

        if target:

            async def run(click):
                return await apply(click, target)

            await self.confirm(
                i,
                f"**Respin {player.mention}**\n{notice}\nCurrent: <@&{current}>\nProposed: <@&{target}>\nNo roles change until confirmed.",
                run,
            )
        elif exceptions:
            # Paginated role selection avoids Discord's 25-option limit.
            view = ExceptionPicker(self.db, i.user.id, i.guild, exceptions, apply, notice=notice)
            await i.followup.send(
                f"{notice}\nNo unused eligible country remains. Staff may explicitly select a previously represented country below. Club limits still apply.",
                view=view,
                ephemeral=True,
            )
            view.message = await i.original_response()
        else:
            raise RuleError(
                "No other country has space within the country player cap and club limit."
            )

    @app_commands.command(
        name="remove", description="Remove a country assignment and preserve permanent history"
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player="Select a player who is still in the server",
        user_id="For someone who left: paste their Discord user ID instead of selecting a player",
    )
    async def remove_player(
        self,
        i: discord.Interaction,
        player: discord.Member | None = None,
        user_id: str | None = None,
    ):
        await self.check(i)
        if (player is None) == (user_id is None):
            raise RuleError(
                "Select a player or provide user_id for someone who left. Use just one option."
            )
        if player is not None:
            uid = player.id
        else:
            value = user_id.strip().removeprefix("<@").removeprefix("!").removesuffix(">")
            if (
                not value.isascii()
                or not value.isdecimal()
                or len(value) > 19
                or not 0 < int(value) <= 2**63 - 1
            ):
                raise RuleError("Enter a valid Discord user ID in user_id.")
            uid = int(value)
        ok = await self.engine.remove_country(i.guild, uid, i.user.id)
        await i.followup.send(
            f"<@{uid}>'s country assignment removed. History preserved; player was not requeued."
            if ok
            else "Removal awaiting recovery. Use /sync.",
            ephemeral=True,
        )

    @app_commands.command(
        name="ban", description="Exclude an inactive player from international drafts"
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player="Player to remove from internationals", reason="Why the player is being excluded"
    )
    async def ban_player(
        self, i: discord.Interaction, player: discord.Member, reason: str = "Inactive"
    ):
        await self.check(i)
        removed = await self.engine.ban_player(player, i.user.id, reason)
        text = f"{player.mention} is **banned from international drafts**. "
        text += (
            "Country, opt-in, and Respun roles removed; waitlist entry removed."
            if removed
            else "Waitlist entry removed. Role removal is awaiting recovery; fix permissions and use /sync."
        )
        await i.followup.send(text, ephemeral=True)

    @app_commands.command(
        name="bans", description="View players excluded from international drafts"
    )
    @app_commands.guild_only()
    async def bans(self, i: discord.Interaction):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            await self.engine.config(con, i.guild_id, allow_reset=True, allow_paused=True)
            rows = await con.fetch(
                "SELECT b.*,EXISTS(SELECT 1 FROM role_operations o WHERE o.guild_id=b.guild_id "
                "AND o.user_id=b.user_id AND o.action='ban' AND o.status IN ('pending','failed')) AS removal_pending "
                "FROM player_bans b WHERE b.guild_id=$1 ORDER BY b.created_at DESC,b.user_id",
                i.guild_id,
            )
        lines = []
        for row in rows:
            name = discord.utils.escape_markdown(row["player_name"])
            reason = discord.utils.escape_markdown(row["reason"])
            pending = "\n**Role removal awaiting /sync.**" if row["removal_pending"] else ""
            lines.append(
                f"**<@{row['user_id']}>** ({name})\nReason: {reason}\n"
                f"By <@{row['actor_id']}> • <t:{int(row['created_at'].timestamp())}:d>{pending}"
            )
        await self.pages(
            i,
            "PCL International — Banned players",
            lines or ["No players are banned."],
            footer=f"Banned players: {len(rows)}",
            page_breaks=range(8, len(lines), 8),
        )

    @app_commands.command(
        name="unban", description="Allow a banned player to opt into internationals again"
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player="Choose a banned player, or enter their user ID (even after they leave)"
    )
    async def unban_player(self, i: discord.Interaction, player: str):
        await self.check(i)
        value = player.strip().removeprefix("<@").removeprefix("!").removesuffix(">")
        if (
            not value.isascii()
            or not value.isdecimal()
            or len(value) > 19
            or not 0 < int(value) <= 2**63 - 1
        ):
            raise RuleError("Choose a banned player from autocomplete, or enter their user ID.")
        uid = int(value)
        await self.engine.unban_player(i.guild, uid, i.user.id)
        await i.followup.send(
            f"<@{uid}> removed from the ban list. They can opt in again or be added with /waitlist add. "
            "Their previous country is not automatically restored.",
            ephemeral=True,
        )

    @unban_player.autocomplete("player")
    async def banned_players(self, i: discord.Interaction, current: str):
        if i.guild is None:
            return []
        try:
            await require_staff(self.db, i.user)
        except RuleError:
            return []
        async with self.db.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT user_id,player_name FROM player_bans WHERE guild_id=$1 "
                "AND (player_name ILIKE '%' || $2 || '%' OR user_id::text LIKE $2 || '%') "
                "ORDER BY player_name,user_id LIMIT 25",
                i.guild_id,
                current.strip(),
            )
        choices = []
        for row in rows:
            member = i.guild.get_member(row["user_id"])
            name = member.display_name if member else row["player_name"]
            choices.append(
                app_commands.Choice(
                    name=f"{name} ({row['user_id']})"[:100], value=str(row["user_id"])
                )
            )
        return choices

    @club.command(name="set", description="Choose a primary club role the player currently holds")
    async def club_set(self, i: discord.Interaction, player: discord.Member, club: discord.Role):
        await self.check(i)
        await self.engine.set_club(player, club, i.user.id)
        await i.followup.send(
            f"{player.mention}: primary club is {club.mention}. Existing country preserved. Use /sync to review capacity.",
            ephemeral=True,
        )

    @app_commands.command(
        name="bypass", description="Exempt a current player from their country's club limit"
    )
    @app_commands.guild_only()
    @app_commands.describe(
        player="Player already assigned to a country",
        enabled="True to grant the bypass; False to remove it",
        reason="Why this current country/club combination needs an exception",
    )
    async def bypass(
        self,
        i: discord.Interaction,
        player: discord.Member,
        enabled: bool = True,
        reason: str = "Club changed after draft",
    ):
        await self.check(i)
        country, club = await self.engine.set_club_bypass(player, i.user.id, enabled, reason)
        await i.followup.send(
            f"**Club-limit bypass {'enabled' if enabled else 'removed'}** for {player.mention} "
            f"in <@&{country}> / <@&{club}>. "
            "The player still counts toward the country's total player cap.",
            ephemeral=True,
        )

    @app_commands.command(description="View a country’s active roster grouped by primary club")
    @app_commands.guild_only()
    @app_commands.describe(country="Choose a national team configured in /setup")
    async def roster(self, i: discord.Interaction, country: str):
        await i.response.defer(ephemeral=True)
        if i.guild is None:
            raise RuleError("Use this command inside your Discord server.")
        try:
            country_id = int(country)
        except ValueError:
            raise RuleError("Choose a country from the configured national team list.") from None
        async with self.db.pool.acquire() as con:
            # Validate again at execution: typed values and outdated autocomplete
            # selections must never expose a roster outside this server's country list.
            configured = await self.engine.role_ids(con, i.guild_id, "country", True)
            role = i.guild.get_role(country_id)
            if country_id not in configured or role is None:
                raise RuleError("Choose a country from the configured national team list.")
            cfg = await self.engine.config(con, i.guild_id, allow_reset=True, allow_paused=True)
            rows = await con.fetch(
                "SELECT p.user_id,p.primary_club_role_id,p.club_conflict,p.present,"
                "EXISTS(SELECT 1 FROM club_limit_bypasses b JOIN draft_cycles c ON c.id=b.cycle_id AND c.status='active' "
                "WHERE b.guild_id=p.guild_id AND b.user_id=p.user_id AND b.country_role_id=p.current_country_role_id "
                "AND b.club_role_id=p.primary_club_role_id) AS club_bypass FROM players p "
                "WHERE p.guild_id=$1 AND p.current_country_role_id=$2 ORDER BY p.primary_club_role_id NULLS LAST,p.user_id",
                i.guild_id,
                country_id,
            )
        groups = defaultdict(list)
        bypassed = set()
        for r in rows:
            groups[r["primary_club_role_id"]].append(r)
            member = i.guild.get_member(r["user_id"])
            held = {role.id for role in member.roles} if member else set()
            if (
                r.get("club_bypass", False)
                and country_id in held
                and r["primary_club_role_id"] in held
            ):
                bypassed.add(r["user_id"])
        lines = [f"**Total active players: {len(rows)}**"]
        if cfg.get("country_cap") is not None:
            lines.append(
                f"Country cap: **{cfg['country_cap']}**"
                + (" — **OVER CAP**" if len(rows) > cfg["country_cap"] else "")
            )
        for club, players in groups.items():
            label = f"<@&{club}>" if club else "Unaffiliated / unresolved"
            exempt = sum(p["user_id"] in bypassed for p in players)
            warning = (
                " — **OVER CLUB LIMIT**"
                if club and len(players) - exempt > cfg["club_limit"]
                else ""
            )
            size = (
                f"{len(players)} players • {exempt} bypassed • {len(players) - exempt} counted"
                if exempt
                else str(len(players))
            )
            lines.append(f"\n**{label} ({size})**{warning}")
            lines.extend(
                f"<@{p['user_id']}>"
                + (" — absent" if not p["present"] else "")
                + (" — club conflict" if p["club_conflict"] else "")
                + (" — club-limit bypass" if p["user_id"] in bypassed else "")
                for p in players
            )
        await self.pages(i, f"{role.name} — Active roster", lines, staff_only=False)

    @roster.autocomplete("country")
    async def roster_countries(self, i: discord.Interaction, current: str):
        if i.guild is None:
            return []
        async with self.db.pool.acquire() as con:
            configured = await self.engine.role_ids(con, i.guild_id, "country", True)
        query = current.strip().casefold()
        roles = [
            role
            for rid in configured
            if (role := i.guild.get_role(rid)) is not None and query in role.name.casefold()
        ]
        return [
            app_commands.Choice(name=role.name[:100], value=str(role.id))
            for role in sorted(roles, key=lambda r: (r.name.casefold(), r.id))[:25]
        ]

    @app_commands.command(
        name="country-counts", description="Show current player counts for each configured country"
    )
    @app_commands.guild_only()
    async def country_counts(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        if i.guild is None:
            raise RuleError("Use this command inside your Discord server.")
        async with self.db.pool.acquire() as con:
            cfg = await self.engine.config(con, i.guild_id, allow_reset=True, allow_paused=True)
            configured = await self.engine.role_ids(con, i.guild_id, "country", True)
        members = i.guild.members if i.guild.chunked else await i.guild.chunk()
        countries = [role for rid in configured if (role := i.guild.get_role(rid)) is not None]
        players = {role.id: set() for role in countries}
        for member in members:
            if member.bot:
                continue
            for country in {role.id for role in member.roles} & players.keys():
                players[country].add(member.id)
        cap = cfg.get("country_cap")
        lines = []
        for role in sorted(countries, key=lambda r: (r.name.casefold(), r.id)):
            count = len(players[role.id])
            size = f"{count}/{cap}" if cap is not None else str(count)
            warning = " — **OVER CAP**" if cap is not None and count > cap else ""
            if cap is not None and count == cap:
                warning = " — **FULL**"
            label = "player" if count == 1 else "players"
            lines.append(f"<@&{role.id}> — **{size} {label}**{warning}")
        total = len(set().union(*players.values()))
        await self.pages(
            i,
            "PCL International — Country player counts",
            lines or ["No national teams configured. Add country roles in /setup."],
            staff_only=False,
            footer=f"Countries: {len(countries)} • Current players: {total}",
            page_breaks=range(8, len(lines), 8),
        )

    @app_commands.command(
        description="List international goalkeepers by country, plus unassigned GKs"
    )
    @app_commands.guild_only()
    async def goalkeepers(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        if i.guild is None:
            raise RuleError("Use this command inside your Discord server.")
        async with self.db.pool.acquire() as con:
            cfg = await self.engine.config(con, i.guild_id, allow_reset=True, allow_paused=True)
            goalkeeper = cfg.get("international_gk_role_id")
            if not goalkeeper or i.guild.get_role(goalkeeper) is None:
                raise RuleError("Select the International GK role in /setup first.")
            enabled = await self.engine.role_ids(con, i.guild_id, "country", True)
            managed = await self.engine.role_ids(con, i.guild_id, "country")
        members = i.guild.members if i.guild.chunked else await i.guild.chunk()
        grouped, unassigned = goalkeeper_groups(members, goalkeeper, managed | enabled)
        lines = [f"**International GK role:** <@&{goalkeeper}>"]
        page_breaks = set()
        country_count = 0
        countries = [role for rid in enabled if (role := i.guild.get_role(rid)) is not None]
        for role in sorted(countries, key=lambda r: (r.name.casefold(), r.id)):
            if country_count and country_count % 8 == 0:
                page_breaks.add(len(lines))
            country_count += 1
            lines.append(f"\n**<@&{role.id}>**")
            lines.extend(f"<@{uid}>" for uid in grouped[role.id])
            if not grouped[role.id]:
                lines.append("No GK")
        # A disabled country is still a team, so its GKs must not look unassigned.
        inactive = [
            role
            for rid in managed - enabled
            if grouped[rid] and (role := i.guild.get_role(rid)) is not None
        ]
        for role in sorted(inactive, key=lambda r: (r.name.casefold(), r.id)):
            if country_count and country_count % 8 == 0:
                page_breaks.add(len(lines))
            country_count += 1
            lines.append(f"\n**<@&{role.id}> — disabled country**")
            lines.extend(f"<@{uid}>" for uid in grouped[role.id])
        if country_count:
            page_breaks.add(len(lines))
        lines.append("\n**Unassigned goalkeepers**")
        for index, uid in enumerate(unassigned):
            if index and index % 8 == 0:
                page_breaks.add(len(lines))
                lines.append("\n**Unassigned goalkeepers — continued**")
            lines.append(f"<@{uid}>")
        if not unassigned:
            lines.append("None")
        total = len(set(unassigned).union(*(set(players) for players in grouped.values())))
        await self.pages(
            i,
            "PCL International — Goalkeepers",
            lines,
            staff_only=False,
            footer=f"International GKs: {total} • Unassigned: {len(unassigned)}",
            page_breaks=page_breaks,
        )

    @app_commands.command(description="View your transfer history; staff may look up other players")
    @app_commands.guild_only()
    @app_commands.describe(
        player="Leave blank for your own history; other players require admin access"
    )
    async def history(self, i: discord.Interaction, player: discord.Member | None = None):
        await i.response.defer(ephemeral=True)
        if i.guild is None:
            raise RuleError("Use this command inside your Discord server.")
        player = player or i.user
        staff_only = player.id != i.user.id
        if staff_only:
            await require_staff(self.db, i.user)
        async with self.db.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM assignment_history WHERE guild_id=$1 AND user_id=$2 ORDER BY id DESC",
                i.guild_id,
                player.id,
            )
            cycle = await self.engine.cycle(con, i.guild_id)
            used = await self.engine.respins_used(con, i.guild_id, player.id, cycle) if cycle else 0
        lines = []
        for r in rows:
            old = f"<@&{r['from_country_id']}>" if r["from_country_id"] else "None"
            new = f"<@&{r['to_country_id']}>" if r["to_country_id"] else "None"
            actor = f"<@{r['actor_id']}>" if r["actor_id"] else "System"
            lines.append(
                f"<t:{int(r['created_at'].timestamp())}:f> • Cycle {r['cycle_id']} • **{r['action']}**\n{old} → {new} • {actor}"
                + (f" • {override_name(r['override_type'])} override" if r["override_type"] else "")
            )
        await self.pages(
            i,
            f"{player.display_name} — History",
            lines,
            staff_only=staff_only,
            footer=f"Respins remaining: {max(0, RESPIN_LIMIT - used)}/{RESPIN_LIMIT}",
        )

    @app_commands.command(description="View draft statistics and unfinished operations")
    @app_commands.guild_only()
    async def status(self, i: discord.Interaction):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            cfg = await self.engine.config(con, i.guild_id, allow_reset=True, allow_paused=True)
            cycle = await self.engine.cycle(con, i.guild_id)
            active = await con.fetchval(
                "SELECT count(*) FROM players WHERE guild_id=$1 AND current_country_role_id IS NOT NULL",
                i.guild_id,
            )
            counts = await con.fetch(
                "SELECT status,count(*) n FROM waitlist_entries WHERE guild_id=$1 AND status NOT IN ('completed','removed') GROUP BY status",
                i.guild_id,
            )
            unfinished = await con.fetchval(
                "SELECT count(*) FROM role_operations WHERE guild_id=$1 AND status <> 'completed'",
                i.guild_id,
            )
            queued = await con.fetchval(
                "SELECT count(*) FROM announcement_outbox WHERE guild_id=$1 AND status='pending'",
                i.guild_id,
            )
            countries = len(await self.engine.role_ids(con, i.guild_id, "country", True))
            clubs = len(await self.engine.role_ids(con, i.guild_id, "club", True))
            _, occupancy = await self.engine.counts(con, i.guild_id, guild=i.guild)
            violations = sum(n > cfg["club_limit"] for n in occupancy.values())
        lines = [
            "Bot: **PAUSED** — use /resume to continue."
            if cfg.get("paused", False)
            else "Bot: **RUNNING**",
            "Paused features: "
            + (
                ", ".join(
                    PAUSE_FEATURES[feature]
                    for feature in PAUSE_FEATURES
                    if feature in (cfg.get("paused_features") or ())
                )
                or "None"
            ),
            f"Cycle: **{cycle}**" + (" — **RESET RECOVERY**" if cfg["resetting"] else ""),
            f"Active players: **{active}**",
            f"Player cap per country: **{cfg.get('country_cap') or 'Unlimited'}**",
            f"Enabled countries: **{countries}** • Clubs: **{clubs}**",
            f"Club limit: **{cfg['club_limit']}** • Capacity violations: **{violations}**",
            f"Unfinished role operations: **{unfinished}** • Queued messages: **{queued}**",
        ]
        lines.extend(f"{r['status']}: **{r['n']}**" for r in counts)
        lines.append("Use /sync for a current Discord/database discrepancy report.")
        await self.pages(i, "PCL International — Status", lines)

    @app_commands.command(description="Pause actions, role changes, and queued announcements")
    @app_commands.guild_only()
    @app_commands.describe(feature="Pause all actions or only one feature")
    @app_commands.choices(
        feature=[
            app_commands.Choice(name="All features", value="all"),
            *[app_commands.Choice(name=label, value=key) for key, label in PAUSE_FEATURES.items()],
        ]
    )
    async def pause(self, i: discord.Interaction, feature: str = "all"):
        await self.check(i, True)
        changed = await self.engine.set_paused(i.guild, i.user.id, True, feature=feature)
        if feature != "all":
            label = PAUSE_FEATURES[feature]
            return await i.followup.send(
                f"**{label} {'paused' if changed else 'already paused'}.** "
                "An operation already in progress may finish safely. "
                f"Use /resume feature:{feature} to continue this feature.",
                ephemeral=True,
            )
        await i.followup.send(
            ("**Bot paused.**" if changed else "**Bot is already paused.**")
            + " No new actions will start. A player operation already in progress may finish safely. "
            "Pending work is preserved; an admin can use /resume.",
            ephemeral=True,
        )

    @app_commands.command(description="Resume actions and pending work after a pause")
    @app_commands.guild_only()
    @app_commands.describe(feature="Resume everything or only one feature")
    @app_commands.choices(
        feature=[
            app_commands.Choice(name="All features", value="all"),
            *[app_commands.Choice(name=label, value=key) for key, label in PAUSE_FEATURES.items()],
        ]
    )
    async def resume(self, i: discord.Interaction, feature: str = "all"):
        await self.check(i, True)
        changed = await self.engine.set_paused(i.guild, i.user.id, False, feature=feature)
        self.bot.boot_reconciled.discard(i.guild_id)
        if feature != "all":
            label = PAUSE_FEATURES[feature]
            return await i.followup.send(
                f"**{label} {'resumed' if changed else 'already enabled'}.** "
                "Other active pauses still apply. Pending work will continue automatically.",
                ephemeral=True,
            )
        await i.followup.send(
            ("**Bot resumed.**" if changed else "**Bot is already running.**")
            + " Pending work, member updates, and queued announcements will continue automatically.",
            ephemeral=True,
        )

    @app_commands.command(
        name="sync", description="Retry unfinished operations and report role discrepancies"
    )
    @app_commands.guild_only()
    async def sync_roles(self, i: discord.Interaction):
        await self.check(i)
        if not i.guild.chunked:
            await i.guild.chunk()
        issues = await self.engine.sync(i.guild, i.user.id)
        await self.pages(
            i, f"Synchronization — {len(issues)} issues", issues or ["No discrepancies found."]
        )

    @app_commands.command(
        name="reset", description="Clear country roles and rebuild a fresh draft cycle"
    )
    @app_commands.guild_only()
    async def reset_all(self, i: discord.Interaction):
        await self.check(i, True)
        async with self.db.pool.acquire() as con:
            cycle = await self.engine.cycle(con, i.guild_id)

        async def run(click):
            if not i.guild.chunked:
                await i.guild.chunk()
            done = await self.engine.start_reset(i.guild, click.user.id, expected_cycle=cycle)
            return (
                "Reset complete. Fresh waitlist generated from current opt-ins; history preserved."
                if done
                else "Reset paused on role-removal failures. No conflicting draft will run. Fix permissions and use /sync."
            )

        await self.confirm(
            i,
            "**Full draft reset**\nRemove all configured country roles, clear active assignments, and rebuild from current opt-ins? Permanent history is preserved. Manual-only waitlist entries are not restored.",
            run,
            True,
        )

    @app_commands.command(
        name="clear", description="Remove everyone from one country, or all countries"
    )
    @app_commands.guild_only()
    async def clear_countries(self, i: discord.Interaction, country: discord.Role | None = None):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            await self.engine.config(con, i.guild_id)
            cycle = await self.engine.cycle(con, i.guild_id)
            managed = await self.engine.role_ids(con, i.guild_id, "country")
            if country is not None and country.id not in managed:
                raise RuleError("Choose a configured national team role.")
        scope = country.mention if country else "all configured countries"

        async def run(click):
            if not i.guild.chunked:
                await i.guild.chunk()
            success, failed = await self.engine.clear_countries(
                i.guild, click.user.id, country.id if country else None, expected_cycle=cycle
            )
            return (
                f"Cleared {scope}: **{success} successful**, **{failed} awaiting retry**. "
                "History preserved. No new draft cycle was started."
                + (" Use /sync to retry failures." if failed else "")
            )

        await self.confirm(
            i,
            f"**Clear {scope}?**\nRemove those country roles from everyone and clear matching active assignments? History is preserved, and players will not be automatically requeued. This does not start a new draft cycle.",
            run,
        )


class ExceptionPicker(StaffView):
    def __init__(self, db, owner, guild, countries, apply, *, notice=""):
        super().__init__(db, owner)
        self.guild, self.countries, self.apply = guild, countries, apply
        self.notice = notice
        self.page = 0
        self.build()

    def build(self):
        self.clear_items()
        ids = self.countries[self.page * 25 : (self.page + 1) * 25]
        select = discord.ui.Select(
            placeholder="Explicit previous-country exception",
            options=[
                discord.SelectOption(
                    label=self.guild.get_role(cid).name if self.guild.get_role(cid) else str(cid),
                    value=str(cid),
                )
                for cid in ids
            ],
        )

        async def choose(i):
            target = int(select.values[0])

            async def run(click):
                return await self.apply(click, target, "respin_exception")

            confirm = Confirmation(self.db, self.owner, run)
            await i.response.edit_message(
                content=f"{self.notice}\nConfirm previous-country exception: <@&{target}>? Club capacity will be checked again.",
                view=confirm,
            )
            confirm.message = await i.original_response()
            self.stop()

        select.callback = choose
        self.add_item(select)
        if len(self.countries) > 25:
            button = discord.ui.Button(label="Next countries")

            async def next_page(i):
                self.page = (self.page + 1) % ((len(self.countries) + 24) // 25)
                self.build()
                await i.response.edit_message(view=self)

            button.callback = next_page
            self.add_item(button)
        cancel = discord.ui.Button(label="Cancel")

        async def stop(i):
            self.stop()
            await i.response.edit_message(content="Respin canceled. No changes made.", view=None)

        cancel.callback = stop
        self.add_item(cancel)
