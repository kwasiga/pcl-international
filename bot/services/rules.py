import secrets
from collections import defaultdict


class RuleError(Exception):
    """A safe, user-facing explanation of an invalid operation."""


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
    player_positions=(),
    position_counts=None,
):
    eligible = [
        c
        for c in countries
        if c not in set(excluded) and (club is None or club_counts.get((c, club), 0) < limit)
    ]
    if not eligible:
        return None
    smallest = min(population.get(c, 0) for c in eligible)
    candidates = [c for c in eligible if population.get(c, 0) == smallest]
    positions = set(player_positions)
    if positions and position_counts:
        # Each versatile player contributes one total player, divided equally among
        # their positions. Prefer countries needing this mix among equally small teams.
        weight = 1 / len(positions)
        totals = {p: sum(position_counts.get((c, p), 0) for c in countries) for p in positions}
        scores = {
            c: sum(
                weight * position_counts.get((c, p), 0) / (totals[p] + weight) for p in positions
            )
            for c in candidates
        }
        best = min(scores.values())
        candidates = [c for c in candidates if abs(scores[c] - best) < 1e-9]
    return (chooser or secrets.choice)(candidates)


def occupancy(rows):
    population, clubs = defaultdict(int), defaultdict(int)
    for row in rows:
        population[row["country"]] += 1
        if row["club"] is not None:
            clubs[row["country"], row["club"]] += 1
    return population, clubs


POSITION_GROUPS = ("GK", "DEF", "MID", "ATT")


def player_positions(held_roles, mapping):
    held = set(held_roles)
    return sorted({group for role, group in mapping.items() if role in held})


def position_occupancy(rows):
    counts = defaultdict(float)
    for row in rows:
        positions = set(row["positions"])
        for group in positions:
            counts[row["country"], group] += 1 / len(positions)
    return counts


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
