from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.permissions import require_staff
from bot.services.rules import POSITION_GROUPS, RuleError
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
        await require_staff(self.db, i.user, admin=admin)
        await i.response.defer(ephemeral=True)

    async def pages(self, i, title, lines):
        view = Pages(self.db, i.user.id, title, lines)
        await i.followup.send(embed=view.embed(), view=view, ephemeral=True)
        view.message = await i.original_response()

    async def confirm(self, i, text, action, admin=False):
        view = Confirmation(self.db, i.user.id, action, admin=admin)
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
            mapping = await self.engine.position_mapping(con, i.guild_id)
            positions = {
                group: {rid for rid, pos in mapping.items() if pos == group}
                for group in POSITION_GROUPS
            }
        panel = SetupPanel(self.engine, i.user.id, i.guild, cfg, countries, clubs, positions)
        await i.followup.send(embed=panel.embed(), view=panel, ephemeral=True)
        panel.message = await i.original_response()

    @waitlist.command(name="view", description="View pending, blocked and absent players")
    async def waitlist_view(self, i: discord.Interaction):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT w.*,p.primary_club_role_id,p.position_groups FROM waitlist_entries w JOIN players p "
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
            lines.append(
                f"<@{r['user_id']}> — {club} → {country} — **{r['status']}**"
                + (f" — {'/'.join(r['position_groups'])}" if r["position_groups"] else "")
            )
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

    @draft.command(name="run", description="Assign pending players; hold squad announcements")
    async def draft_run(self, i: discord.Interaction):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            await self.engine.config(con, i.guild_id)
            counts = await con.fetch(
                "SELECT status,count(*) AS n FROM waitlist_entries WHERE guild_id=$1 "
                "AND status NOT IN ('completed','removed') GROUP BY status",
                i.guild_id,
            )
        text = "Confirm the official draft? All eligible pending players will be processed.\n"
        text += "\n".join(f"**{r['status']}:** {r['n']}" for r in counts) or "No pending players."
        text += "\nEligibility and capacity are checked again before freezing the batch."
        text += "\nSquad announcements are held until you use /draft country."

        async def run(click):
            success, failed = await self.engine.draft(i.guild, click.user.id)
            result = f"Draft finished: **{success} successful**, **{failed} awaiting retry**."
            if success:
                result += " Use /draft country to reveal one country's full squad at a time."
            if failed:
                result += (
                    " Check the staff logs, resolve the reported issue, then run /sync to retry."
                )
            return result

        await self.confirm(i, text, run)

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

    @app_commands.command(description="Propose an unlimited respin; apply only after confirmation")
    @app_commands.guild_only()
    async def respin(self, i: discord.Interaction, player: discord.Member):
        await self.check(i)
        current, target, exceptions = await self.engine.propose_respin(player)
        async with self.db.pool.acquire() as con:
            cycle = await self.engine.cycle(con, i.guild_id)

        async def apply(click, country, action="respin"):
            ok = await self.engine.assign(
                player,
                country,
                click.user.id,
                expected=current,
                expected_cycle=cycle,
                action=action,
            )
            return (
                "Respin completed. Call-up queued."
                if ok
                else "Respin recorded for recovery. Use /sync after resolving the role error."
            )

        if target:

            async def run(click):
                return await apply(click, target)

            await self.confirm(
                i,
                f"**Respin {player.mention}**\nCurrent: <@&{current}>\nProposed: <@&{target}>\nNo roles change until confirmed.",
                run,
            )
        elif exceptions:
            # Paginated role selection avoids Discord's 25-option limit.
            view = ExceptionPicker(self.db, i.user.id, i.guild, exceptions, apply)
            await i.followup.send(
                "No unused eligible country remains. Staff may explicitly select a previously represented country below. Club limits still apply.",
                view=view,
                ephemeral=True,
            )
            view.message = await i.original_response()
        else:
            raise RuleError("No other country currently has eligible club capacity.")

    @app_commands.command(
        name="remove", description="Remove a country assignment and preserve permanent history"
    )
    @app_commands.guild_only()
    async def remove_player(self, i: discord.Interaction, player: discord.Member):
        await self.check(i)

        async def run(click):
            ok = await self.engine.assign(player, None, click.user.id, action="remove")
            return (
                "Country removed. History preserved; player was not requeued."
                if ok
                else "Removal awaiting recovery. Use /sync."
            )

        await i.followup.send(await run(i), ephemeral=True)

    @club.command(name="set", description="Choose a primary club role the player currently holds")
    async def club_set(self, i: discord.Interaction, player: discord.Member, club: discord.Role):
        await self.check(i)
        await self.engine.set_club(player, club, i.user.id)
        await i.followup.send(
            f"{player.mention}: primary club is {club.mention}. Existing country preserved. Use /sync to review capacity.",
            ephemeral=True,
        )

    @app_commands.command(description="View a country’s active roster grouped by primary club")
    @app_commands.guild_only()
    async def roster(self, i: discord.Interaction, country: discord.Role):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            cfg = await self.engine.config(con, i.guild_id, allow_reset=True)
            if country.id not in await self.engine.role_ids(con, i.guild_id, "country"):
                raise RuleError("Choose a configured national team role.")
            rows = await con.fetch(
                "SELECT user_id,primary_club_role_id,club_conflict,present,position_groups FROM players "
                "WHERE guild_id=$1 AND current_country_role_id=$2 ORDER BY primary_club_role_id NULLS LAST,user_id",
                i.guild_id,
                country.id,
            )
        groups = defaultdict(list)
        for r in rows:
            groups[r["primary_club_role_id"]].append(r)
        lines = [f"**Total active players: {len(rows)}**"]
        if cfg.get("country_cap") is not None:
            lines.append(
                f"Country cap: **{cfg['country_cap']}**"
                + (" — **OVER CAP**" if len(rows) > cfg["country_cap"] else "")
            )
        for club, players in groups.items():
            label = f"<@&{club}>" if club else "Unaffiliated / unresolved"
            warning = " — **OVER CLUB LIMIT**" if club and len(players) > cfg["club_limit"] else ""
            lines.append(f"\n**{label} ({len(players)})**{warning}")
            lines.extend(
                f"<@{p['user_id']}>"
                + (f" — {'/'.join(p['position_groups'])}" if p["position_groups"] else "")
                + (" — absent" if not p["present"] else "")
                + (" — club conflict" if p["club_conflict"] else "")
                for p in players
            )
        await self.pages(i, f"{country.name} — Active roster", lines)

    @app_commands.command(description="View permanent national assignment history")
    @app_commands.guild_only()
    async def history(self, i: discord.Interaction, player: discord.Member):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            rows = await con.fetch(
                "SELECT * FROM assignment_history WHERE guild_id=$1 AND user_id=$2 ORDER BY id DESC",
                i.guild_id,
                player.id,
            )
        lines = []
        for r in rows:
            old = f"<@&{r['from_country_id']}>" if r["from_country_id"] else "None"
            new = f"<@&{r['to_country_id']}>" if r["to_country_id"] else "None"
            actor = f"<@{r['actor_id']}>" if r["actor_id"] else "System"
            lines.append(
                f"<t:{int(r['created_at'].timestamp())}:f> • Cycle {r['cycle_id']} • **{r['action']}**\n{old} → {new} • {actor}"
                + (" • capacity override" if r["override_type"] else "")
            )
        await self.pages(i, f"{player.display_name} — History", lines)

    @app_commands.command(description="View draft statistics and unfinished operations")
    @app_commands.guild_only()
    async def status(self, i: discord.Interaction):
        await self.check(i)
        async with self.db.pool.acquire() as con:
            cfg = await self.engine.config(con, i.guild_id, allow_reset=True)
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
            _, occupancy = await self.engine.counts(con, i.guild_id)
            violations = sum(n > cfg["club_limit"] for n in occupancy.values())
        lines = [
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
    def __init__(self, db, owner, guild, countries, apply):
        super().__init__(db, owner)
        self.guild, self.countries, self.apply = guild, countries, apply
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
                content=f"Confirm previous-country exception: <@&{target}>? Club capacity will be checked again.",
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
