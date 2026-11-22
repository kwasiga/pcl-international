import discord

from bot.services.rules import RuleError


def validate_role(guild, role):
    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        raise RuleError("The bot needs Manage Roles permission.")
    if not role or role.is_default() or role.managed or role >= me.top_role:
        raise RuleError("Every country must be an existing role below the bot’s highest role.")


async def apply_roles(
    guild,
    user_id,
    managed_ids,
    target_id,
    reason,
    *,
    opt_in_role_id=None,
    respin_role_id=None,
    mark_respun=False,
    clear_respun=False,
    clear_country_ids=None,
):
    """Idempotent role saga: add target, remove others, fetch to verify remote state."""
    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        if target_id is None or clear_country_ids is not None:
            return  # An absent member has no server roles to remove.
        raise RuleError(
            "Player is absent; the role operation will retry after they return."
        ) from None
    if clear_country_ids is not None:
        remove_ids = set(clear_country_ids)
        if clear_respun and respin_role_id:
            remove_ids.add(respin_role_id)
        removals = [r for r in member.roles if r.id in remove_ids]
        for role in removals:
            validate_role(guild, role)
        for role in removals:
            await member.remove_roles(role, reason=reason)
        member = await guild.fetch_member(user_id)
        if any(r.id in remove_ids for r in member.roles):
            raise RuleError("Country clear verification failed; use /sync to retry.")
        return
    target = guild.get_role(target_id) if target_id else None
    if target_id:
        validate_role(guild, target)
    held = {role.id for role in member.roles}
    opt_in = guild.get_role(opt_in_role_id) if target_id and opt_in_role_id in held else None
    respin = guild.get_role(respin_role_id) if respin_role_id else None
    if mark_respun and not respin:
        raise RuleError("The Respun role is missing; retry to recreate it.")
    for role in (
        opt_in,
        respin if mark_respun or (clear_respun and respin_role_id in held) else None,
    ):
        if role:
            validate_role(guild, role)
    # Role management depends on the roles being changed, not the member's
    # highest role (or ownership). Validate every removal before making changes.
    for role in member.roles:
        if role.id in managed_ids and role.id != target_id:
            validate_role(guild, role)
    if target and target not in member.roles:
        await member.add_roles(target, reason=reason)
    for role in list(member.roles):
        if role.id in managed_ids and role.id != target_id:
            validate_role(guild, role)
            await member.remove_roles(role, reason=reason)
    member = await guild.fetch_member(user_id)
    actual = {r.id for r in member.roles} & set(managed_ids)
    if actual != ({target_id} if target_id else set()):
        raise RuleError("Discord role verification failed; the operation remains retryable.")

    if not (mark_respun or clear_respun or opt_in):
        return

    # Finish auxiliary roles before committing the assignment/history. Failures remain
    # in the same durable journal so a retry completes the whole intended role set.
    if mark_respun and respin_role_id not in {r.id for r in member.roles}:
        await member.add_roles(respin, reason=reason)
    if clear_respun and respin and respin in member.roles:
        await member.remove_roles(respin, reason=reason)
    if opt_in and opt_in in member.roles:
        await member.remove_roles(opt_in, reason=reason)
    member = await guild.fetch_member(user_id)
    actual = {r.id for r in member.roles}
    if (
        actual & set(managed_ids) != ({target_id} if target_id else set())
        or (target_id and opt_in_role_id in actual)
        or (mark_respun and respin_role_id not in actual)
        or (clear_respun and respin_role_id in actual)
    ):
        raise RuleError("Assignment role verification failed; the operation remains retryable.")
