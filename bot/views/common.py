import asyncio
import logging
import time
from contextlib import suppress

import discord

from bot.services.permissions import require_staff
from bot.services.rules import RuleError

log = logging.getLogger(__name__)


async def reply_error(interaction, error, *, replace_original=False):
    if isinstance(error, RuleError):
        message = str(error)
    else:
        log.error("Interaction failed (%s)", type(error).__name__, exc_info=error)
        message = "The operation could not finish. Check /status or /sync before retrying."
    if interaction.response.is_done():
        if replace_original:
            try:
                await interaction.edit_original_response(content=message, view=None, embed=None)
                return
            except discord.HTTPException:
                pass
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


class StaffView(discord.ui.View):
    def __init__(self, db, owner, *, admin=False, timeout=180):
        super().__init__(timeout=timeout)
        self.db, self.owner, self.admin = db, owner, admin
        self.message = None

    async def interaction_check(self, interaction):
        try:
            if interaction.user.id != self.owner:
                raise RuleError("Only the staff member who opened this panel can use it.")
            await require_staff(self.db, interaction.user, admin=self.admin)
            return True
        except RuleError as exc:
            await reply_error(interaction, exc)
            return False

    async def on_error(self, interaction, error, item):
        await reply_error(interaction, error)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Confirmation(StaffView):
    def __init__(self, db, owner, action, *, admin=False, progress=False):
        super().__init__(db, owner, admin=admin)
        self.action, self.claimed = action, False
        self.progress = progress
        self.progress_text = "Starting draft…"

    async def report_progress(self, text):
        self.progress_text = text

    async def show_progress(self, interaction):
        started = time.monotonic()
        while True:
            try:
                await interaction.edit_original_response(
                    content=f"{self.progress_text}\nElapsed: {int(time.monotonic() - started)}s. "
                    "Large drafts may take several minutes. Do not start another draft.",
                    view=None,
                    embed=None,
                )
            except discord.HTTPException:
                log.warning("Could not update draft progress; processing continues", exc_info=True)
                return
            await asyncio.sleep(5)

    async def on_error(self, interaction, error, item):
        await reply_error(interaction, error, replace_original=True)

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if self.claimed:
            return await interaction.response.send_message(
                "This confirmation was already used.", ephemeral=True
            )
        self.claimed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Working…", view=None, embed=None)
        self.stop()
        reporter = asyncio.create_task(self.show_progress(interaction)) if self.progress else None
        try:
            # Recheck permission changes after panel creation.
            await require_staff(self.db, interaction.user, admin=self.admin)
            text = (
                await self.action(interaction, self.report_progress)
                if self.progress
                else await self.action(interaction)
            )
        finally:
            if reporter:
                reporter.cancel()
                with suppress(asyncio.CancelledError):
                    await reporter
        await interaction.edit_original_response(content=text, view=None, embed=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if self.claimed:
            return await interaction.response.send_message(
                "This confirmation was already used.", ephemeral=True
            )
        self.claimed = True
        self.stop()
        await interaction.response.edit_message(
            content="Canceled. No changes were made.", view=None, embed=None
        )


class Pages(StaffView):
    def __init__(self, db, owner, title, lines, *, staff_only=True):
        super().__init__(db, owner)
        self.staff_only = staff_only
        self.title, self.index = title, 0
        self.pages = []
        page = ""
        for line in lines or ["No records."]:
            line = str(line)[:1800]
            if len(page) + len(line) + 1 > 3500:
                self.pages.append(page)
                page = ""
            page += line + "\n"
        self.pages.append(page)

    async def interaction_check(self, interaction):
        if self.staff_only:
            return await super().interaction_check(interaction)
        if interaction.user.id != self.owner:
            await reply_error(
                interaction, RuleError("Only the member who opened this roster can use its pages.")
            )
            return False
        return True

    def embed(self):
        e = discord.Embed(title=self.title, description=self.pages[self.index], color=0x2689D9)
        e.set_footer(text=f"PCL International • Page {self.index + 1}/{len(self.pages)}")
        return e

    @discord.ui.button(label="Previous")
    async def previous(self, interaction, button):
        self.index = (self.index - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next")
    async def next(self, interaction, button):
        self.index = (self.index + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.embed(), view=self)
