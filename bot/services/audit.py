"""Human-readable Discord audit messages; database details stay structured."""

ROLE_FIELDS = {
    "country",
    "club",
    "previous",
    "expected",
    "actual",
    "countries",
    "roles",
    "clubs",
    "opt_in",
    "respin_role",
    "country_role_id",
    "club_role_id",
    "from_country_id",
    "to_country_id",
}
USER_FIELDS = {"user", "player", "actor", "user_id", "target_user_id", "actor_id"}
LABELS = {
    "previous": "Previous country",
    "country": "Country",
    "club": "Club",
    "opt_in": "Opt-in role",
    "override": "Override",
    "operation": "Operation",
    "club_limit": "Club limit",
    "expected": "Expected country",
    "actual": "Actual countries",
}


def override_name(value):
    names = {"club_capacity": "club capacity", "respin_limit": "reroll limit"}
    return " + ".join(names.get(kind, kind) for kind in value.split(","))


def display_value(key, value):
    if value is None:
        return "Unaffiliated" if key == "club" else "None"
    if key == "override" and isinstance(value, str):
        return override_name(value)
    if isinstance(value, dict):
        return " • ".join(f"{label(k)}: {display_value(k, v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return ", ".join(display_value(key, item) for item in value) if value else "None"
    if isinstance(value, int) and not isinstance(value, bool):
        if key in ROLE_FIELDS:
            return f"<@&{value}>"
        if key in USER_FIELDS:
            return f"<@{value}>"
        if key == "operation":
            return f"#{value}"
    return str(value)


def label(key):
    return LABELS.get(key, key.replace("_", " ").capitalize())


def audit_messages(action, actor, user, details, limit=1900):
    header = f"**{action}** | Staff: {f'<@{actor}>' if actor else 'System'}"
    if user:
        header += f" | Player: <@{user}>"
    chunks, body = [], header
    for key, value in details.items():
        if key in {"operation", "operation_id"}:
            continue
        # Lists of structured violations/issues each get their own readable line.
        entries = value if isinstance(value, list) and key in {"violations", "issues"} else [value]
        for entry in entries:
            line = f"**{label(key)}:** {display_value(key, entry)}"
            # Split between complete mentions/words instead of truncating mention syntax.
            for index, word in enumerate(line.split(" ")):
                prefix = "\n" if index == 0 else " "
                if len(body) + len(prefix) + len(word) > limit:
                    chunks.append(body)
                    body = header
                    prefix = "\n"
                # Extremely long error tokens may be safely truncated; mention tokens are short.
                body += prefix + word[: limit - len(header) - 1]
    chunks.append(body)
    return chunks
