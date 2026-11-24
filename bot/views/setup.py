import discord

from bot.services.permissions import require_staff
from bot.services.rules import RuleError, missing_setup
from bot.views.common import StaffView, reply_error


class SetupValueModal(discord.ui.Modal):
    def __init__(self, panel):
        super().__init__(timeout=600)
        self.panel = panel

    async def check_submission(self, interaction):
        # Modal submissions have the same short acknowledgement deadline as commands.
        # Close the form before waiting on a potentially busy database.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.panel.closed or self.panel.is_finished():
            raise RuleError("This setup panel has expired or was closed. Run /setup again.")
        if interaction.user.id != self.panel.owner:
            raise RuleError("Only the staff member who opened this panel can use it.")
        await require_staff(self.panel.db, interaction.user, admin=True)

    async def apply_value(self, interaction, key, value, description):
        message = self.panel.message or getattr(interaction, "message", None)
        if message is None:
            raise RuleError("The setup message is unavailable. Run /setup again.")
        previous = self.panel.values[key]
        self.panel.values[key] = value
        try:
            # This is the setup message, not the modal's deferred confirmation message.
            await message.edit(embed=self.panel.embed(), view=self.panel)
        except Exception:
            self.panel.values[key] = previous
            raise
        await interaction.edit_original_response(
            content=f"{description}. Return to the setup panel and press **Save** to apply it."
        )

    async def on_error(self, interaction, error):
        await reply_error(interaction, error, replace_original=True)


class ClubLimitModal(SetupValueModal, title="Club limit"):
    value = discord.ui.TextInput(label="Maximum club players per country", max_length=5)

    def __init__(self, panel):
        super().__init__(panel)
        self.value.default = str(panel.values["club_limit"])

    async def on_submit(self, interaction):
        await self.check_submission(interaction)
        try:
            limit = int(self.value.value)
            if limit < 1:
                raise ValueError
        except ValueError:
            return await interaction.edit_original_response(
                content="Enter a positive whole number. Reopen Club limit to try again."
            )
        await self.apply_value(interaction, "club_limit", limit, f"Club limit set to **{limit}**")


class CountryCapModal(SetupValueModal, title="Players per country"):
    value = discord.ui.TextInput(
        label="Maximum players (blank = no cap)", required=False, max_length=5
    )

    def __init__(self, panel):
        super().__init__(panel)
        self.value.default = str(panel.values["country_cap"] or "")

    async def on_submit(self, interaction):
        await self.check_submission(interaction)
        try:
            cap = int(self.value.value.strip()) if self.value.value.strip() else None
            if cap is not None and cap < 1:
                raise ValueError
        except ValueError:
            return await interaction.edit_original_response(
                content="Enter a positive whole number, or leave blank for no cap. "
                "Reopen Country player cap to try again."
            )
        description = (
            f"Country player cap set to **{cap}**"
            if cap is not None
            else "Country player cap removed"
        )
        await self.apply_value(interaction, "country_cap", cap, description)


class SetupPanel(StaffView):
    def __init__(self, engine, owner, guild, cfg, countries, clubs):
        super().__init__(engine.db, owner, admin=True, timeout=600)
        self.closed = False
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
        self.role_pages = {"countries": 0, "clubs": 0}
        self.build()

    def stop(self):
        self.closed = True
        super().stop()

    async def on_timeout(self):
        self.closed = True
        await super().on_timeout()

    def embed(self):
        v = self.values

        def role(rid):
            return f"<@&{rid}>" if rid else "Not selected"

        def channel(cid):
            return f"<#{cid}>" if cid else "Not selected"

        e = discord.Embed(title="PCL International — Setup", color=0x2689D9)
        e.description = (
            "Save any section now and return later to finish setup.\n"
            "Add roles or choose specific configured countries/clubs to remove, then press **Save**.\n"
            "Editing these lists preserves player assignments and history."
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
            self.removal_selector(self.section)
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

    def removal_selector(self, key):
        def role_name(rid):
            role = self.guild.get_role(rid)
            return role.name if role else f"Deleted role ({rid})"

        roles = sorted(self.values[key], key=lambda rid: (role_name(rid).casefold(), rid))
        pages = max(1, (len(roles) + 24) // 25)
        page = self.role_pages[key] = min(self.role_pages[key], pages - 1)
        visible = roles[page * 25 : (page + 1) * 25]
        selector = discord.ui.Select(
            placeholder=(
                f"Remove configured {key} ({page + 1}/{pages})"
                if roles
                else f"No configured {key} to remove"
            ),
            options=[
                discord.SelectOption(label=role_name(rid)[:100], value=str(rid)) for rid in visible
            ]
            or [discord.SelectOption(label="None configured", value="none")],
            max_values=len(visible) or 1,
            disabled=not roles,
            row=2,
        )

        async def remove(i):
            ids = {int(rid) for rid in selector.values}
            if not ids <= set(visible) or not ids <= self.values[key]:
                raise RuleError("This list changed. Reopen /setup and select a configured role.")
            self.values[key] -= ids
            self.build()
            await i.response.edit_message(embed=self.embed(), view=self)

        selector.callback = remove
        self.add_item(selector)
        if pages > 1:
            for label, step in (("Previous roles", -1), ("Next roles", 1)):
                button = discord.ui.Button(
                    label=label, row=3, disabled=not 0 <= page + step < pages
                )

                async def turn_page(i, step=step):
                    self.role_pages[key] = max(0, min(pages - 1, page + step))
                    self.build()
                    await i.response.edit_message(embed=self.embed(), view=self)

                button.callback = turn_page
                self.add_item(button)

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
            else:
                self.values[key] = next(iter(ids), None)
            self.build()
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
