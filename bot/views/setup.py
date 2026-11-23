import discord

from bot.services.permissions import require_staff
from bot.services.rules import RuleError, missing_setup
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


class CountryCapModal(discord.ui.Modal, title="Players per country"):
    value = discord.ui.TextInput(
        label="Maximum players (blank = no cap)", required=False, max_length=5
    )

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.value.default = str(panel.values["country_cap"] or "")

    async def on_submit(self, interaction):
        if not await self.panel.interaction_check(interaction):
            return
        try:
            cap = int(self.value.value.strip()) if self.value.value.strip() else None
            if cap is not None and cap < 1:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "Enter a positive whole number, or leave blank for no cap.", ephemeral=True
            )
        self.panel.values["country_cap"] = cap
        await interaction.response.edit_message(embed=self.panel.embed(), view=self.panel)

    async def on_error(self, interaction, error):
        await reply_error(interaction, error)


class SetupPanel(StaffView):
    def __init__(self, engine, owner, guild, cfg, countries, clubs):
        super().__init__(engine.db, owner, admin=True, timeout=600)
        self.engine, self.guild, self.revision = engine, guild, cfg["revision"] if cfg else None
        self.values = {
            k: cfg[k] if cfg else None
            for k in ("staff_role_id", "opt_in_role_id", "callups_channel_id", "logs_channel_id")
        }
        self.values.update(
            countries=set(countries), clubs=set(clubs), club_limit=cfg["club_limit"] if cfg else 5
        )
        self.values["country_cap"] = cfg.get("country_cap") if cfg else None
        self.values["international_gk_role_id"] = (
            cfg.get("international_gk_role_id") if cfg else None
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
        e.add_field(
            name="International GK role",
            value=f"{role(v['international_gk_role_id'])}\nUsed by /goalkeepers to list GKs by country and without a country.",
            inline=False,
        )
        e.set_footer(
            text=f"Country cap: {v['country_cap'] or 'Unlimited'} • Club limit: {v['club_limit']} • Balanced random"
        )
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
                    ("goalkeepers", "International GK role"),
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
        elif self.section in ("countries", "clubs"):
            self.role_selector("Add roles (repeat for more than 25)", self.section, 1, "add")
            self.role_selector("Disable/remove selected roles", self.section, 2, "remove")
        elif self.section == "channels":
            self.channel_selector("National team call-ups channel", "callups_channel_id", 1)
            self.channel_selector("Private staff logs channel", "logs_channel_id", 2)
        elif self.section == "goalkeepers":
            self.role_selector(
                "International GK role (optional)", "international_gk_role_id", 1, allow_clear=True
            )
        limit = discord.ui.Button(label="Club limit", row=3)

        async def edit_limit(i):
            await i.response.send_modal(ClubLimitModal(self))

        limit.callback = edit_limit
        self.add_item(limit)
        cap_button = discord.ui.Button(label="Country player cap", row=3)

        async def edit_cap(i):
            await i.response.send_modal(CountryCapModal(self))

        cap_button.callback = edit_cap
        self.add_item(cap_button)
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

    def role_selector(self, placeholder, key, row, mode=None, *, allow_clear=False):
        selector = discord.ui.RoleSelect(
            placeholder=placeholder,
            min_values=0 if allow_clear else 1,
            max_values=25 if mode else 1,
            row=row,
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
                self.values[key] = next(iter(ids), None)
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
