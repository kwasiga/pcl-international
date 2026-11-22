import discord

from bot.services.rules import RuleError


async def require_staff(db, member, *, admin=False):
    """Configured role hierarchy gates every command and component interaction."""
    if not isinstance(member, discord.Member):
        raise RuleError("Use this command inside your Discord server.")
    async with db.pool.acquire() as con:
        config = await con.fetchrow(
            "SELECT staff_role_id FROM guild_config WHERE guild_id=$1", member.guild.id
        )
    if config is None:
        # Bootstrap only: no role has been designated yet. Once configured, neither
        # server ownership nor Discord Administrator bypasses the configured role.
        if admin and (member.id == member.guild.owner_id or member.guild_permissions.administrator):
            return
        raise RuleError("The server owner or a Discord Administrator must run /setup first.")
    role = member.guild.get_role(config["staff_role_id"])
    if role is None:
        raise RuleError(
            "The designated admin role is missing. Restore the configured role before using the bot."
        )
    if member.top_role < role:
        raise RuleError("This command requires the designated admin role or a higher role.")
