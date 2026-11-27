# PCL International

Discord national team draft management for PCL. Python 3.12+, discord.py, asyncpg, PostgreSQL, Docker, and Railway. No tournament, match-statistics, or bracket features are included.

Players opt in using an existing participation role. Receiving that role automatically assigns an eligible, balanced random country, removes the opt-in role, and announces the player in the call-ups channel. No `/draft run` is required for opt-ins. Players who cannot be assigned remain on the waitlist and retry automatically, normally within one minute after capacity or eligibility changes. `/draft run` remains available for staff-managed backlog; `/draft country` separately announces a full squad. Each player gets three successful respins per draft cycle, with explicit admin overrides available.

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
7. Start the bot, then run `/setup` as the server owner or a Discord Administrator for initial setup. Once configured, the designated admin role or a higher role is required for staff commands. Every server member can use `/roster`, `/goalkeepers`, and view their own `/history`.

Staff commands, including `/setup` and `/reset`, enforce the designated admin role and Discord role hierarchy at execution time. Discord Administrator permission and server ownership do not bypass this rule after initial setup. Slash-command visibility alone does not grant access. `/roster` is available to every server member and only accepts enabled country roles selected in `/setup`; its autocomplete lists those countries, and execution revalidates the selection. `/history` defaults to the invoking member's own history; looking up another player requires the designated admin role or higher. All commands and panels are server-only and private to the invoking member. Confirmation buttons recheck staff permissions.

## Setup panel

Use the section selector to configure:

- **Staff and opt-in roles:** one minimum admin role, one international participation role.
- **National teams:** add country roles in batches of up to 25. Repeat to configure more than 25. **Remove configured countries** lists only the current selection, with Previous/Next roles buttons for lists over 25. Select specific countries and press **Save** to disable them while preserving assignments/history.
- **League clubs:** add club roles or use **Remove configured clubs** to select specific clubs from the current list, then press **Save**. Other clubs and country assignments stay in place; no reset is needed. Deleted Discord roles also appear in the removal list so they can be removed from setup.
- **Channels:** an official call-ups text channel and a private staff logs text channel. Both need View Channel, Send Messages, and Read Message History.
- **Club limit:** positive integer, default **5** players from the same club per country.
- **Country player cap:** click **Country player cap**, enter the maximum total players per country, then Save. Leave blank for no cap (the initial default). Active assignments plus pending/unfinished reservations count toward the cap, including unaffiliated players. Drafts, respins, and manual assignments cannot add players to a full country; club-limit overrides do not override this total cap. Lowering it preserves existing players, reports over-cap countries, and recalculates pending proposals.
- **International GK role:** select the single existing role identifying international goalkeepers. It is optional for setup, required for `/goalkeepers`, and must be separate from country, club, staff, opt-in, and Respun roles. It also enables GK prioritization in the automatic draft and respins. The bot reads this role but never assigns or removes it. There are no GK/DEF/MID/ATT position mappings.

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
| `/respin player [override_limit]` | Staff | Randomly proposes an eligible country, with three rerolls per player per cycle. Warns before the third; `override_limit:True` explicitly approves an extra reroll with a logged admin confirmation. |
| `/assign player country` | Staff | Manual assignment/transfer. Exceeding the club limit requires explicit logged confirmation. |
| `/clear [country]` | Staff | Confirm removal of everyone from one country; omit country to clear all configured countries. Preserves history and the current cycle. |
| `/remove [player] [user_id]` | Staff | Select a current member, or paste the Discord user ID of someone who left. Use exactly one option. Clears country and current-cycle exclusions, preserves history/reroll usage, and does not requeue. Removed assignments are not restored on rejoin. |
| `/ban player [reason]` | Staff | Bans the player from international drafts, removes their country/opt-in/Respun roles and waitlist reservation, and preserves history. Default reason: Inactive. This is a draft ban, not a Discord server ban. |
| `/bans` | Staff | Private list of banned players with reason, staff member, date, and any pending role cleanup. Up to eight players per page. Available while paused. |
| `/unban player` | Staff | Removes the draft ban. Choose from banned-player autocomplete or enter a user ID, including departed players. Does not restore the previous country or automatically requeue them. |
| `/club set player club` | Staff | Saves a primary club the player currently holds. |
| `/bypass player [enabled] [reason]` | Staff | Exempts an already assigned player from the club limit for their current country/primary club. Defaults to enabled; use `enabled:False` to remove. For late club changes, not future country transfers. Player still counts toward the total country cap. Shows approved bypasses in `/roster` and logs the reason. |
| `/roster country` | Everyone in the server | Active roster grouped by club; pending reservations excluded. Choose from configured enabled countries only. Other roles and disabled/deleted countries are rejected. |
| `/country-counts` | Everyone in the server | Current country-role holder counts, eight countries per page, sorted by name. Includes players assigned outside the bot and countries with zero players. Shows the configured player cap and FULL/OVER CAP labels. Counts current members, with a unique player total across countries. Private and available while paused. |
| `/goalkeepers` | Everyone in the server | Lists up to eight countries per page with their International GK role holders, or **No GK**. **Unassigned goalkeepers** appear on separate pages with up to eight players each. Reads actual Discord roles, including members not in the draft database. GKs still holding a disabled country are labelled separately rather than misreported as unassigned. |
| `/history [player]` | Everyone for their own history; staff for others | Leave player blank to see your permanent dated assignment, transfer, removal, reset, and override history in this server. Each page shows remaining respins out of three for the current draft cycle at the bottom, including when history is empty. Only staff can select another player. |
| `/status` | Staff | Current cycle, waitlist states, enabled roles, capacity violations, unfinished operations, and queued messages. |
| `/pause [feature]` | Designated admin role or higher | Pause everything by default, or choose respins, drafts/squad releases, manual assignments/removals, or announcements. Persists across restarts. |
| `/resume [feature]` | Designated admin role or higher | Resume everything by default, or only the selected feature. Pending work continues automatically; other active pauses remain in effect. |
| `/sync` | Staff | Retries unfinished operations and reports current role/database discrepancies. |
| `/reset` | Designated admin role or higher | Explicit confirmation; clears managed country roles and rebuilds from current opt-ins in a new cycle. |

An authorized staff member has the designated admin role or a higher role in the server hierarchy. This applies to staff commands and panels, including setup and reset. Discord Administrators and the owner must also meet this minimum once configured. If the designated role is deleted, staff access is denied. Before initial setup only, the owner or a Discord Administrator may open setup. Every member may use `/roster` for configured countries, use `/country-counts` and `/goalkeepers`, view their own `/history`, and navigate their own pages. Other members cannot look up another player's history, use staff commands, or operate another member's panels.

`/pause` blocks drafts, rerolls (including limit overrides), manual assignments/removals, clearing/resetting countries, waitlist changes, club changes, setup saves, squad releases, `/sync`, automatic recovery, and queued messages. `/roster`, `/country-counts`, `/goalkeepers`, `/history`, `/status`, and waitlist viewing remain available with their normal access rules. Existing confirmation buttons recheck the pause when executed. A player role operation or message already in flight may finish safely; the next item waits. Pause does not wait for an entire running draft to finish and remains active after redeployment. No unfinished operation is marked failed merely because of a pause.

Member joins, departures, opt-ins, and other member role updates received while paused are retained in a durable queue. `/resume` lets maintenance continue frozen draft/reset operations, replay member events against current Discord roles, automatically assign eligible waiting opt-ins, and deliver queued announcements, normally within one minute. Staff-only waitlist entries without an opt-in role remain available for `/draft run`. Pause/resume are logged, preserve assignments/history/reroll usage, and do not start a new draft cycle.

To pause only one aspect, choose the optional **feature** on `/pause`:

| Feature | Pauses |
| --- | --- |
| `respins` | New respin proposals, confirmations (including admin overrides), and unfinished respin recovery. |
| `draft` | Automatic opt-in assignments, `/draft run`, `/draft country`, and unfinished draft role operations. Players can still join the waitlist and are assigned after resume. |
| `assignments` | Manual `/assign`, `/remove`, `/clear`, `/ban`, `/unban`, `/bypass`, and recovery of those role changes. |
| `announcements` | Queued call-ups and staff log delivery. Assignments continue and messages remain queued. |

For example, `/pause feature:respins` leaves drafting available; `/resume feature:respins` enables respins again. Several features can be paused at once. `/status` lists them. Feature pauses are checked again when confirmations run and before each pending role operation/message; unrelated recovery continues. A role operation/message already in progress may finish safely. A full bot pause takes precedence over individual settings; `/resume` without a feature clears both the full pause and all feature pauses. Setup, read-only reports, and unrelated commands keep their usual access rules during a feature-only pause.

## Assignment rules

- Staff may approve a club-limit bypass for someone who joined a club after receiving their country, using `/bypass player:@Player`. It applies only to that player's current country and primary club in the current cycle. For example, four players from one club with one approved bypass count as three toward a club limit of three; new players from that club still cannot join that full group. All four still count toward the country player cap. `/roster` shows actual players, bypassed players, and the count used for the club limit; `/status`, `/sync`, capacity warnings, and assignment eligibility use the same adjusted count. The bypass persists after restart, expires on country/primary-club change, country removal, server departure, or full reset, and does not consume a respin. Staff can revoke it with `enabled:False`; the approval/revocation is audited. It cannot be granted to an unassigned player, for an unheld country role, or without a valid primary club. Existing assignment overrides remain separate.
- Draft bans are saved per server and survive restarts and full resets. Banned players cannot opt in, be manually added to the waitlist, be drafted/assigned (including club overrides), respin, or have their old country restored on rejoin. `/ban` commits the ban and removes the waitlist reservation before starting its recoverable role cleanup. If cleanup fails, the ban remains active and `/bans` shows the pending cleanup; fix permissions and run `/sync`. Finish any existing role operation for the player before banning/unbanning them. `/unban` permits fresh opt-in or `/waitlist add`, preserves assignment history and reroll usage, and requires the same designated staff access as `/ban`. Neither command kicks or bans anyone from Discord.
- Count actual Discord country-role holders, active database assignments, and valid pending reservations together. Failed/incomplete role operations also reserve their target, preventing capacity from being reused during recovery. Each player counts once per country across these sources, including players assigned roles outside the bot. Capacity is checked when proposing a respin and again when confirming it; a country that fills up in between is rejected.
- Filter enabled, manageable countries by club capacity and the country player cap. For a player with the configured International GK role, prioritize eligible countries without a GK, then choose randomly among the smallest countries in that group. If every eligible country already has a GK, use normal size balancing. Outfield players always use normal size balancing. Old position mappings are ignored.
- Automatic opt-ins reserve and apply one country through the durable role journal. Repeated events do not cause another draw or consume a respin. Valid reservations stay unchanged across draft, sync, setup, and recovery checks; only an invalid reservation is redrawn. Unchanged rechecks do not produce duplicate registration logs. Pending automatic opt-ins are retried with GKs first, preserving registration order within each group. GK coverage includes actual country roles (including GKs not registered in the bot), saved assignments, pending reservations, and unfinished role-operation targets. Each currently present GK role holder counts once per country. Caps still apply, and valid existing reservations or active assignments are never displaced to make room for a later GK.
- Size balancing applies to automatic draft reservations. GK respins first prefer eligible unused countries without a GK and draw randomly among them, regardless of team size. If none are available, they draw randomly across all eligible unused countries. Outfield respins always draw randomly across all eligible unused countries. The smallest country or the country most recently vacated is not automatically chosen. Country caps, club limits, manageable enabled roles, and current/previous-country exclusions still apply. When only one country is eligible, it is the only possible result. Manual assignments stay staff-controlled. Legacy position tables/data are retained for compatibility but are no longer configured, updated, displayed, or used in selection.
- A member with one configured club uses it. A saved primary club remains valid while held and enabled. Multiple club roles without a valid primary block assignment until `/club set`; no club means unaffiliated and no club-specific cap.
- Respins exclude the current country and every country represented since the last removal in the current cycle. If no unused eligible country remains, staff may explicitly select a previously represented country; club limits still apply.
- Proposals and canceled confirmations never change roles or history. Capacity and current assignment are checked again on confirmation. Old-cycle respin/reset confirmations are rejected.
- Reroll usage counts successful `respin` and previous-country exception actions from permanent history in the current draft cycle, including actions completed before this limit was introduced. Initial drafts and manual assignments do not consume a reroll. Cancellations and failed operations do not count until recovery succeeds. Usage is checked again under the server lock before recording an operation; stale confirmations are rejected if the count changed. Removing, clearing, or re-registering a player does not replenish rerolls in the same cycle. A full reset starts a new cycle with three rerolls again.
- Every reroll confirmation shows the number used and remaining. At two used, it warns **THIRD AND FINAL reroll** before confirmation. At three or more used, normal rerolls are blocked with a **no rerolls left** explanation. Use `/respin player:@Player override_limit:True` for an explicitly confirmed extra reroll. The configured admin role or higher is required and rechecked at confirmation. Successful overrides are recorded as `respin_limit` in the operation, permanent history, and staff audit. Overrides do not relax country caps, club limits, or previous-country exception rules.
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

Public call-ups use plain text with actual role/user mentions, never embeds or DMs. Each successful automatic opt-in or `/draft run` assignment queues `**<@player> has been assigned to <@&country>!**` in the configured call-ups channel, after Discord roles are verified. Failed assignments announce only after successful recovery; normal retries do not repeat the individual message. `/draft country country:@Ghana` separately reveals Ghana's full current squad, including automatic assignments; omit the country to choose randomly among countries without a squad reveal yet. Individual assignment messages do not mark the full squad as revealed. Each invocation queues exactly one country, with long rosters split below Discord's message-length limit. The configured player cap controls squad size; there is no fixed 20-player requirement. Countries with unfinished role changes must recover before release. The bot verifies current members' country roles before queuing a reveal. Removed, transferred, absent, and prior-cycle draft players do not trigger a stale reveal. Once queued, the country cannot be released again for the same draft players, even after a restart; a later draft adding players allows an updated full-squad release. Legacy grouped draft call-ups are treated as squad reveals. Manual assignments and respins keep their individual call-ups.

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
5. Configure the test server with `/setup`, give a test player the opt-in role, and verify immediate country assignment, opt-in removal, and the call-up message. Check `/waitlist view` for players held by caps or conflicts; `/draft run` can still process staff-managed entries.
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
