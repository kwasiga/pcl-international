# PCL International

Discord national team draft management for PCL. Python 3.12+, discord.py, asyncpg, PostgreSQL, Docker, and Railway. No tournament, match-statistics, or bracket features are included.

Players opt in using an existing participation role. The bot reserves a balanced random country on a private waitlist. Staff confirm `/draft run` to apply national roles and announce each successful assignment in the call-ups channel. `/draft country` provides a separate full-squad announcement for one country at a time. Respins are unlimited; there are no change counters.

## Quick start

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` locally, then run:

```sh
python -m bot.main
```

Migrations run automatically before Discord connects. To apply migrations separately:

```sh
python -m bot.database.connection
```

The project can be installed and tested before receiving credentials. Connecting the bot requires both a Discord token and a PostgreSQL URL. An application/bot ID alone cannot log the bot in.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Bot token from the application's Bot page; keep it in `.env` or Railway variables. |
| `DISCORD_APPLICATION_ID` | Application ID; optional because Discord can discover it from the token. |
| `TEST_GUILD_ID` | Optional test server ID. Registers slash commands in that server for fast iteration. Leave blank for global command registration. |
| `DATABASE_URL` | Direct PostgreSQL connection URL. For Neon, choose the **direct/unpooled** hostname and retain `sslmode=require`. |
| `LOG_LEVEL` | Optional Python log level, default `INFO`. |
| `TEST_DATABASE_URL` | Optional database used only by integration tests. Never falls back to `DATABASE_URL`. |

Use a direct database URL. The engine holds PostgreSQL session advisory locks across committed journal entries and Discord calls; transaction-mode connection poolers cannot preserve these locks safely. Use one Railway replica initially. Additional replicas serialize mutations through the same direct database locks.

## Discord application setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create/select the application. Set its application and bot display names to **PCL International**.
2. On the Bot page, enable **Server Members Intent**. Message Content Intent and Presence Intent are unnecessary.
3. Copy the bot token into `.env` locally. Do not commit the token.
4. Invite using the `bot` and `applications.commands` scopes. Required permissions: **Manage Roles**, **View Channels**, **Send Messages**, and **Read Message History**. Bot Administrator permission is unnecessary.
5. Place the bot's highest role above every national team role and the opt-in role it will manage. National roles must be ordinary existing roles, not integration-managed roles.
6. Enable Discord Developer Mode to copy the application ID and test server ID. Set `TEST_GUILD_ID` for initial testing.
7. Start the bot, then run `/setup` as the server owner or a Discord Administrator for initial setup. Once configured, the designated admin role or a higher role is required for staff commands. `/roster` is available to every server member.

Staff commands, including `/setup` and `/reset`, enforce the designated admin role and Discord role hierarchy at execution time. Discord Administrator permission and server ownership do not bypass this rule after initial setup. Slash-command visibility alone does not grant access. `/roster` is available to every server member and only accepts enabled country roles selected in `/setup`; its autocomplete lists those countries, and execution revalidates the selection. All commands and panels are server-only and private to the invoking member. Confirmation buttons recheck staff permissions.

## Setup panel

Use the section selector to configure:

- **Staff and opt-in roles:** one minimum admin role, one international participation role.
- **National teams:** add country roles in batches of up to 25. Repeat to configure more than 25. The remove selector disables selected roles; it preserves assignments/history.
- **League clubs:** add or disable club roles in the same way.
- **Channels:** an official call-ups text channel and a private staff logs text channel. Both need View Channel, Send Messages, and Read Message History.
- **Club limit:** positive integer, default **5** players from the same club per country.
- **Country player cap:** click **Country player cap**, enter the maximum total players per country, then Save. Leave blank for no cap (the initial default). Active assignments plus pending/unfinished reservations count toward the cap, including unaffiliated players. Drafts, respins, and manual assignments cannot add players to a full country; club-limit overrides do not override this total cap. Lowering it preserves existing players, reports over-cap countries, and recalculates pending proposals.
- **Positions — Goalkeepers / Defenders / Midfielders / Attackers:** map existing position roles into GK, DEF, MID, and ATT. You can add multiple roles to a group (for example CB/LB/RB to DEF), in repeatable batches of 25. A position role belongs to one group, while a player can hold roles from multiple groups. Leave groups empty if not needed. No minimum counts or fixed formation are enforced.

Press **Save** to keep any partial configuration and return later. Only fields you supplied are validated; missing roles/channels do not block saving. The panel lists remaining settings. Until an admin role is chosen, the owner or a Discord Administrator can continue setup; other commands remain restricted. Drafts and manual assignments require a call-ups channel before changing country roles. Staff audit records are retained in PostgreSQL even without a logs channel. Existing opt-ins are scanned on initial setup. Disabled countries stop receiving new assignments. Disabling clubs preserves country assignments and recalculates club affiliation. Lowering the limit preserves existing players and flags over-capacity groups. Pending reservations are recalculated. Configuration saves are rejected while role changes or reset recovery are unfinished, and stale setup panels cannot overwrite newer changes.

## Commands

| Command | Access | Behavior |
| --- | --- | --- |
| `/setup` | Designated admin role or higher | Private setup panel. |
| `/waitlist view` | Staff | Paginated proposed countries, clubs, statuses, and totals. |
| `/waitlist add player` | Staff | Adds a member, even without the opt-in role. |
| `/waitlist remove player` | Staff | Releases a pending reservation without removing an active country. |
| `/draft run` | Staff | Shows counts and asks confirmation; assigns all eligible pending players in a durable batch and announces each successful assignment in the call-ups channel. |
| `/draft country [country]` | Staff | Publishes one country's full current squad. Select a country or omit it for a random unreleased squad. Repeating reveals the next country without changing assignments. |
| `/respin player` | Staff | Randomly proposes any eligible unused country with equal odds; roles change only after confirmation. |
| `/assign player country` | Staff | Manual assignment/transfer. Exceeding the club limit requires explicit logged confirmation. |
| `/clear [country]` | Staff | Confirm removal of everyone from one country; omit country to clear all configured countries. Preserves history and the current cycle. |
| `/remove player` | Staff | Clears country and current-cycle exclusions, preserves history, and does not requeue. |
| `/club set player club` | Staff | Saves a primary club the player currently holds. |
| `/roster country` | Everyone in the server | Active roster grouped by club; pending reservations excluded. Choose from configured enabled countries only. Other roles and disabled/deleted countries are rejected. |
| `/history player` | Staff | Permanent dated assignment, transfer, removal, reset, and override history. |
| `/status` | Staff | Current cycle, waitlist states, enabled roles, capacity violations, unfinished operations, and queued messages. |
| `/sync` | Staff | Retries unfinished operations and reports current role/database discrepancies. |
| `/reset` | Designated admin role or higher | Explicit confirmation; clears managed country roles and rebuilds from current opt-ins in a new cycle. |

An authorized staff member has the designated admin role or a higher role in the server hierarchy. This applies to staff commands and panels, including setup and reset. Discord Administrators and the owner must also meet this minimum once configured. If the designated role is deleted, staff access is denied. Before initial setup only, the owner or a Discord Administrator may open setup. Every member may use `/roster` for configured countries and navigate their own roster pages. Other members cannot use staff commands or another member's panels.

## Assignment rules

- Count active database assignments plus valid pending reservations. Failed/incomplete role operations also reserve their target, preventing capacity from being reused during recovery.
- Filter enabled countries by club capacity. Among eligible countries with the fewest total players/reservations, prefer countries that have less coverage in the incoming player's position groups, then choose randomly among equally good choices.
- Position coverage includes active players, pending reservations, and unfinished role operations. A multi-position player contributes one player's worth of coverage, split evenly across their distinct groups. Multiple roles in the same group are counted once. There are no fixed position minimums, quotas, or position-based eligibility blocks. Members without configured position roles follow normal size balancing.
- Size and position balancing apply to automatic draft reservations. Respins use an equal-chance random draw across all eligible unused countries, so the smallest country or the country most recently vacated is not automatically chosen. Country caps, club limits, manageable enabled roles, and current/previous-country exclusions still apply. When only one country is eligible, it is the only possible result. Manual assignments stay staff-controlled. Changes to position roles or setup recalculate pending proposals and coverage while preserving existing country assignments. Waitlist and roster views display known position groups.
- A member with one configured club uses it. A saved primary club remains valid while held and enabled. Multiple club roles without a valid primary block assignment until `/club set`; no club means unaffiliated and no club-specific cap.
- Respins exclude the current country and every country represented since the last removal in the current cycle. If no unused eligible country remains, staff may explicitly select a previously represented country; club limits still apply.
- Proposals and canceled confirmations never change roles or history. Capacity and current assignment are checked again on confirmation. Old-cycle respin/reset confirmations are rejected.
- Only `/assign` with explicit override confirmation may exceed the club limit.
- After a country assignment succeeds, the bot removes the opt-in role. Removing opt-in does not clear a waitlist entry or active country.
- A confirmed respin adds a purple **Respun** role. The bot creates this role automatically on the first confirmed respin and reuses it. It carries no permissions. Manual transfers retain the marker; `/remove` and `/reset` clear it. Returning respun players have the marker restored.
- `/sync` also removes leftover opt-in roles from already assigned players and adds missing current-cycle Respun markers, without duplicating assignment history or restoring missing country roles.
- Pending members who leave become absent and release their reservation. Active absent players retain their recorded assignment. Returning active players have their saved role restored through a recoverable operation.
- Country roles manually removed while a member remains in the server are reported rather than automatically restored. Use `/assign` to reconcile.

## Recovery and delivery

Database state is isolated by guild. Partial unique indexes enforce one active assignment and one unfinished role operation per player, one active waitlist entry per player/cycle, and one running batch per guild. Guild-scoped advisory locks serialize all mutations, including simultaneous opt-ins and multiple staff actions.

The role-operation journal commits before a Discord role change begins. Transfers add the target role, remove other managed country roles, fetch the member again to verify the final role set, and only then commit the active assignment, permanent history, and outgoing announcement. A crash between remote role changes and the final database commit is recovered idempotently by inspecting/reapplying the intended role set.

Failed role changes stay retryable. Other players in a draft continue. Recovery runs on startup, every minute, and through `/sync`. Failed operations block conflicting changes for that player. Fix permissions/hierarchy or returning-member issues, then run `/sync`.

Draft confirmations display preparation and assignment counts with elapsed time, refreshing every five seconds. Discord role changes and rate limits can make large batches take several minutes. Do not start a second draft while one is running. Errors replace the working message; `/status` shows unfinished operations. Events from the same server queue before acquiring a database connection, leaving connections available for staff commands during a draft. PostgreSQL session locks still serialize work across bot instances.

Public call-ups use plain text with actual role/user mentions, never embeds or DMs. Each successful `/draft run` assignment queues `**<@player> has been assigned to <@&country>!**` in the configured call-ups channel, after Discord roles are verified. Failed assignments announce only after successful recovery; normal retries do not repeat the individual message. `/draft country country:@Ghana` separately reveals Ghana's full current squad; omit the country to choose randomly among countries without a squad reveal yet. Individual assignment messages do not mark the full squad as revealed. Each invocation queues exactly one country, with long rosters split below Discord's message-length limit. The configured player cap controls squad size; there is no fixed 20-player requirement. Countries with unfinished role changes must recover before release. The bot verifies current members' country roles before queuing a reveal. Removed, transferred, absent, and prior-cycle draft players do not trigger a stale reveal. Once queued, the country cannot be released again for the same draft players, even after a restart; a later draft adding players allows an updated full-squad release. Legacy grouped draft call-ups are treated as squad reveals. Manual assignments and respins keep their individual call-ups.

Drafts, manual assignments, and respins share the same individual announcement wording: `has been assigned to` when the player has no saved country, or `has been reassigned from` the previous country `to` the new country. Reapplying the same country uses `has been reassigned to`. Player and country roles use actual mentions in every case.

Staff audit messages show staff/player mentions and country/club role mentions in readable fields. Audit mentions display without sending notifications. Internal operation numbers are omitted from staff-facing logs. An outbox retains outgoing public announcements and staff audit messages. Successful deliveries store message IDs; normal retries do not resend them. A crash after Discord accepts a message but before its ID is committed can still duplicate a message because Discord and PostgreSQL cannot share a transaction.

Reset removes **all configured bot-managed country roles**, including disabled roles and roles held by members without an active database assignment. Failed removals keep the reset frozen and retryable; the new cycle is created only after all removals complete. Permanent history survives. Manual-only waitlist registrations without the current opt-in role are not restored. Since successful assignments remove opt-in, previously assigned players must opt in again to enter the rebuilt waitlist.

## Tests

The default test suite uses standard `unittest` and the application dependencies:

```sh
python -m unittest discover -s tests -v
```

PostgreSQL integration tests skip unless `TEST_DATABASE_URL` is set. They create and drop a unique test schema in that database; use a separate test database with schema-creation privileges.

```sh
export TEST_DATABASE_URL='postgresql://user:password@localhost/pcl_test'
python -m unittest discover -s tests -v
```

Integration coverage includes concurrent opt-ins, reservations, draft partial failure/recovery, interrupted role transfers, outbox retries, respin exclusions/exceptions, explicit overrides, disabled roles, primary clubs, departures/rejoins, resets, and role discrepancies. Unit tests cover selection rules, club limits, role-transfer ordering, verification, permissions, confirmation ownership/cancellation, component limits, and slash-command serialization.

Optional development tools:

```sh
python -m pip install ruff pytest pytest-asyncio
ruff check bot tests
ruff format --check bot tests
```

## Railway deployment

1. Push this directory to your GitHub repository and create a Railway service from it.
2. Railway uses the included `Dockerfile`. Set `DISCORD_TOKEN`, `DISCORD_APPLICATION_ID`, and direct Neon `DATABASE_URL` as service variables. Set `TEST_GUILD_ID` for the initial test server.
3. Deploy one replica. No public HTTP domain or web port is required for this Discord gateway worker.
4. Startup applies migrations, registers slash commands, logs into Discord, and resumes unfinished operations. Inspect Railway logs for **PCL International connected**.
5. Configure the test server with `/setup`, add a few test players, review `/waitlist view`, and confirm `/draft run`.
6. Use `/draft country` for each squad, then check announcements, role assignment, `/roster`, `/history`, `/respin`, and `/sync`. Test reset only in the test server.
7. For production, remove `TEST_GUILD_ID` to register globally. Global commands can take time to appear; remove old test-server command copies through Discord's application command management if they shadow global commands.

Docker locally:

```sh
docker build -t pcl-international .
docker run --rm --env-file .env pcl-international
```

## Project layout

```text
bot/
  main.py                    Discord lifecycle, gateway events, recovery worker
  config.py                  Environment configuration
  commands.py                Slash commands and staff reports
  database/
    connection.py            Connection pool, migrations, guild locking
    migrations/001_initial.sql
  services/
    engine.py                Waitlist, draft, role journal, reset, sync, outbox
    rules.py                 Balanced selection, club resolution, call-up splitting
    roles.py                 Safe role transfers and remote verification
    permissions.py           Designated admin role hierarchy checks
  views/
    setup.py                 Setup selectors, channels, limit modal
    common.py                Confirmation, pagination, private panel ownership
tests/                       Unit and optional PostgreSQL integration tests
```

## Current validation

Command definitions and all five setup sections have been instantiated and serialized locally. Local unit tests and lint checks run without Discord credentials. Live Discord behavior, PostgreSQL integration, Neon connectivity, and Railway deployment must be verified with the test credentials before production use.
