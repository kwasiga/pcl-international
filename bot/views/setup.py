import discord

from bot.services.permissions import require_staff
from bot.services.rules import POSITION_GROUPS, RuleError, missing_setup
from bot.views.common import StaffView, reply_error


class ClubLimitModal(discord.ui.Modal, title="Club limit"):
    value = discord.ui.TextInput(label="Maximum club players per country", max_length=5)

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.value.default = str(panel.values["club_limit"])

    async def on_submit(self, interaction):
        if not await self.panel.interaction_check(interaction):
            return
        try:
            limit = int(self.value.value)
            if limit < 1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "Enter a positive whole number.", ephemeral=True
            )
        self.panel.values["club_limit"] = limit
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)

    async def on_error(self, interaction, error):
        await reply_error(interaction, error)


class SetupPanel(StaffView):
    def __init__(self, engine, owner, guild, cfg, countries, clubs, positions=None):
        super().__init__(engine.db, owner, admin=True, timeout=600)
        self.engine, self.guild, self.revision = engine, guild, cfg["revision"] if cfg else None
        self.values = {
            k: cfg[k] if cfg else None
            for k in ("staff_role_id", "opt_in_role_id", "callups_channel_id", "logs_channel_id")
        }
        self.values.update(
            countries=set(countries), clubs=set(clubs), club_limit=cfg["club_limit"] if cfg else 5
        )
        self.values.update(
            {f"pos_{group}": set((positions or {}).get(group, ())) for group in POSITION_GROUPS}
        )
        self.section = "overview"
        self.build()

    def embed(self):
        v = self.values

        def role(rid):
            return f"<@&{rid}>" if rid else "Not selected"

        def channel(cid):
            return f"<#{cid}>" if cid else "Not selected"

        e = discord.Embed(title="PCL International — Setup", color=0x2689D9)
        e.description = (
            "Save any section now and return later to finish setup.\n"
            "Add or remove countries/clubs in batches of up to 25; repeat as needed."
        )
        e.add_field(
            name="Staff and participation",
            value=f"Admin minimum: {role(v['staff_role_id'])}\nOpt-in: {role(v['opt_in_role_id'])}",
            inline=False,
        )
        e.add_field(
            name="Channels",
            value=f"Call-ups: {channel(v['callups_channel_id'])}\nStaff logs: {channel(v['logs_channel_id'])}",
            inline=False,
        )
        for key in ("countries", "clubs"):
            text = ", ".join(role(r) for r in sorted(v[key])) or "None"
            e.add_field(name=f"{key.title()} ({len(v[key])})", value=text[:1000], inline=False)
        for group in POSITION_GROUPS:
            ids = v[f"pos_{group}"]
            text = ", ".join(role(r) for r in sorted(ids)) or "Not configured"
            e.add_field(name=f"{group} position roles ({len(ids)})", value=text[:500], inline=False)
        e.set_footer(text=f"Club limit: {v['club_limit']} • Balanced random • Unlimited respins")
        return e

    def build(self):
        self.clear_items()
        nav = discord.ui.Select(
            placeholder="Setup section",
            row=0,
            options=[
                discord.SelectOption(label=label, value=key, default=self.section == key)
                for key, label in [
                    ("overview", "Overview"),
                    ("staff", "Staff and opt-in roles"),
                    ("countries", "National team roles"),
                    ("clubs", "League club roles"),
                    ("channels", "Channels"),
                    ("pos_GK", "Positions — Goalkeepers"),
                    ("pos_DEF", "Positions — Defenders"),
                    ("pos_MID", "Positions — Midfielders"),
                    ("pos_ATT", "Positions — Attackers"),
                ]
            ],
        )

        async def navigate(i):
            self.section = nav.values[0]
            self.build()
            await i.response.edit_message(embed=self.embed(), view=self)

        nav.callback = navigate
        self.add_item(nav)
        if self.section == "staff":
            self.role_selector("Minimum admin role", "staff_role_id", 1)
            self.role_selector("International opt-in role", "opt_in_role_id", 2)
        elif self.section in ("countries", "clubs") or self.section.startswith("pos_"):
            self.role_selector("Add roles (repeat for more than 25)", self.section, 1, "add")
            self.role_selector("Disable/remove selected roles", self.section, 2, "remove")
        elif self.section == "channels":
            self.channel_selector("National team call-ups channel", "callups_channel_id", 1)
            self.channel_selector("Private staff logs channel", "logs_channel_id", 2)
        limit = discord.ui.Button(label="Club limit", row=3)

        async def edit_limit(i):
            await i.response.send_modal(ClubLimitModal(self))

        limit.callback = edit_limit
        self.add_item(limit)
        save = discord.ui.Button(label="Save", style=discord.ButtonStyle.success, row=4)

        async def commit(i):
            await i.response.defer(ephemeral=True)
            await require_staff(self.db, i.user, admin=True)
            if not self.guild.chunked:
                await self.guild.chunk()
            await self.engine.save_config(self.guild, self.values, i.user.id, self.revision)
            self.stop()
            missing = missing_setup(self.values)
            message = "Settings saved. You can reopen /setup to continue."
            if missing:
                message += " Still unconfigured: " + ", ".join(missing) + "."
            else:
                message += " Use /waitlist view to review the draft."
            await i.edit_original_response(
                content=message,
                embed=None,
                view=None,
            )

        save.callback = commit
        self.add_item(save)
        cancel = discord.ui.Button(label="Cancel", row=4)

        async def abandon(i):
            self.stop()
            await i.response.edit_message(content="Setup canceled.", embed=None, view=None)

        cancel.callback = abandon
        self.add_item(cancel)

    def role_selector(self, placeholder, key, row, mode=None):
        selector = discord.ui.RoleSelect(
            placeholder=placeholder, min_values=1, max_values=25 if mode else 1, row=row
        )

        async def selected(i):
            ids = {role.id for role in selector.values}
            if any(role.is_default() for role in selector.values):
                raise RuleError("@everyone cannot be selected.")
            if mode == "add":
                self.values[key] |= ids
            elif mode == "remove":
                self.values[key] -= ids
            else:
                self.values[key] = next(iter(ids))
            await i.response.edit_message(embed=self.embed(), view=self)

        selector.callback = selected
        self.add_item(selector)

    def channel_selector(self, placeholder, key, row):
        selector = discord.ui.ChannelSelect(
            placeholder=placeholder, channel_types=[discord.ChannelType.text], row=row
        )

        async def selected(i):
            self.values[key] = selector.values[0].id
            await i.response.edit_message(embed=self.embed(), view=self)

        selector.callback = selected
        self.add_item(selector)
