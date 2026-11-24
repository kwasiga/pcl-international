import secrets
from collections import defaultdict


class RuleError(Exception):
    """A safe, user-facing explanation of an invalid operation."""


class BotPausedError(RuleError):
    def __init__(self):
        super().__init__(
            "PCL International is paused. Pending work is preserved. "
            "An admin must use /resume before actions can continue."
        )


RESPIN_LIMIT = 3


def respin_notice(used, *, override=False):
    remaining = max(0, RESPIN_LIMIT - used)
    if not remaining:
        if not override:
            raise RuleError(
                f"This player has no rerolls left ({used}/{RESPIN_LIMIT} used this draft cycle). "
                "An admin can use /respin with override_limit:True to explicitly approve an extra reroll."
            )
        return (
            f"**ADMIN OVERRIDE — no rerolls remaining ({used}/{RESPIN_LIMIT} used).**\n"
            "Confirming approves an extra reroll beyond the limit. This override will be logged."
        )
    notice = f"Rerolls used: **{used}/{RESPIN_LIMIT}** — **{remaining} remaining**."
    if remaining == 1:
        notice += (
            "\n**Warning: this is their THIRD AND FINAL reroll. "
            "After it succeeds, they will have 0 rerolls left.**"
        )
    else:
        notice += f" After this reroll succeeds: **{remaining - 1} remaining**."
    return notice


def primary_club(held, configured, saved=None):
    matches = set(held) & set(configured)
    if saved in matches:
        return saved, False
    if len(matches) == 1:
        return next(iter(matches)), False
    return None, len(matches) > 1


def choose_country(
    countries,
    population,
    club_counts,
    club,
    limit,
    excluded=(),
    chooser=None,
    *,
    country_cap=None,
    balance=True,
    goalkeeper=False,
    goalkeeper_counts=None,
):
    eligible = [
        c
        for c in countries
        if c not in set(excluded)
        and (country_cap is None or population.get(c, 0) < country_cap)
        and (club is None or club_counts.get((c, club), 0) < limit)
    ]
    if not eligible:
        return None
    if goalkeeper:
        uncovered = [c for c in eligible if (goalkeeper_counts or {}).get(c, 0) == 0]
        if uncovered:
            eligible = uncovered
    if not balance:
        # Respins stay random within the eligible pool, including the GK
        # coverage preference, without automatically filling the smallest team.
        return (chooser or secrets.choice)(eligible)
    smallest = min(population.get(c, 0) for c in eligible)
    candidates = [c for c in eligible if population.get(c, 0) == smallest]
    return (chooser or secrets.choice)(candidates)


def occupancy(rows):
    population, clubs = defaultdict(int), defaultdict(int)
    for row in rows:
        population[row["country"]] += 1
        if row["club"] is not None:
            clubs[row["country"], row["club"]] += 1
    return population, clubs


def goalkeeper_groups(members, goalkeeper_role, countries):
    """Crosscheck actual GK/country roles, including players not recorded in the DB."""
    countries = set(countries)
    grouped, unassigned = {cid: set() for cid in countries}, set()
    for member in members:
        held = {role.id for role in member.roles}
        if member.bot or goalkeeper_role not in held:
            continue
        teams = held & countries
        for country in teams:
            grouped[country].add(member.id)
        if not teams:
            unassigned.add(member.id)
    return {cid: sorted(players) for cid, players in grouped.items()}, sorted(unassigned)


def callup_chunks(grouped, limit=1900, *, country_names=None):
    """Country headings repeat on split; every player is emitted exactly once."""
    title = "**PCL INTERNATIONAL DRAFT — OFFICIAL CALL-UPS**"
    chunks, text = [], title
    for country, players in sorted(grouped.items()):
        name = (country_names or {}).get(country, "Country")
        heading = f"\n\n**{name} - <@&{country}>**"
        if len(text + heading) > limit:
            chunks.append(text)
            text = title
        text += heading
        for player in players:
            line = f"\n**<@{player}>**"
            if len(text + line) > limit:
                chunks.append(text)
                text = title + heading
            text += line
    if text != title:
        chunks.append(text)
    return chunks


SETUP_LABELS = {
    "staff_role_id": "admin role",
    "opt_in_role_id": "opt-in role",
    "callups_channel_id": "call-ups channel",
    "logs_channel_id": "staff logs channel",
}


def missing_setup(values):
    missing = [label for key, label in SETUP_LABELS.items() if not values.get(key)]
    if not values.get("countries"):
        missing.append("country roles")
    return missing
