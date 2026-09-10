# prunebot

A Discord bot that retires dead accounts on a two-stage clock:

1. Members who post **fewer than `min_messages` in a rolling `window_days`** get an
   **inactive role** and a warning DM.
2. If they are still flagged **`kick_after_days`** later without reverifying, they
   are **kicked**.
3. A **whitelist** of members and roles is permanently exempt — never flagged,
   never kicked.

Defaults: nothing posted in 30 days → flagged; 90 days later → kicked.

Posting again does **not** clear the flag. Clearing it is a deliberate act: the
member runs `/verify` or presses the button in their warning DM, or a moderator
pardons them, whitelists them, or simply takes the role off by hand.

---

## The thing to understand first

Discord has no API for "how many messages has this user sent recently". Message
activity exists only as a live event stream plus per-channel history you can page
through. So this bot:

- counts messages as they arrive, into **daily per-user buckets**, and
- on first run, **backfills** by scanning channel history to reconstruct the window.

Until that backfill completes, flagging and kicking are hard-blocked. Otherwise
the first sweep would flag your entire server.

This also means the bot only sees what it is running for. If the container is down
for a week, that week looks silent for everyone. `restart: unless-stopped` and the
shutdown flush exist for that reason.

---

## Safety

This bot removes real people from a real community on a timer. The design assumes
it will be misconfigured at some point.

| Mechanism | What it does |
|---|---|
| **`dry_run` (ships on)** | Works out everything, audits it, changes nothing. |
| **Plan then execute** | `/prune preview` renders the exact object a real run executes, so a preview cannot disagree with reality. |
| **Circuit breaker** | If more than 25% of evaluable members would be flagged (or 5% kicked), the **whole sweep aborts** and does nothing. |
| **Per-sweep caps** | At most 25 flags and 5 kicks per run; the rest roll over. |
| **Backfill gate** | No flagging or kicking until history covers a full window. |
| **Uptime gate** | No sweep within 10 minutes of connecting, while the member cache is still loading. |
| **Audit trail** | Every action is written to SQLite *and* posted to an audit channel, dry-run entries included. |
| **Kill switches** | `PRUNE_PANIC=1` env var → `/prune config set safety.dry_run true` → `[sweep] enabled = false`. |

Startup validation refuses to run if the config could act wrongly or act silently:
kicking enabled with no audit channel, retention shorter than the window, a kick
deadline inside the window, `dry_run = false` with no role configured.

---

## Setup

### 1. Create the Discord application

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → **Reset Token**, copy it into `.env` as `DISCORD_TOKEN`.
3. **Bot** → **Privileged Gateway Intents** → enable **Server Members Intent**.
   This is the only privileged intent needed. **Message Content is not required** —
   the bot reads author ids and timestamps, never message text.
4. **Installation** (left sidebar):
   - **Installation Contexts**: tick **Guild Install**, untick **User Install**.
     A user install has no server context, so the bot could not see members or
     assign roles.
   - **Default Install Settings → Guild Install**:
     - **Scopes**: `bot`, `applications.commands`
       (`applications.commands` is what registers `/prune` and `/verify`; without
       it the bot joins but has no commands)
     - **Permissions**: the six in the table below
   - **Install Link**: *Discord Provided Link*, then open it to add the bot.

   Equivalently, if you prefer the older **OAuth2 → URL Generator** path, or just
   want a link — substitute your Application ID from **General Information**:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot+applications.commands&permissions=268520450
   ```

| Permission | Why |
|---|---|
| **Manage Roles** | Add and remove the inactive role |
| **Kick Members** | The whole point. Omit it and run with `kicking.enabled = false` if you only want flagging |
| **View Channels** | See the channels it counts messages in |
| **Read Message History** | The backfill; without it there is nothing to count |
| **Send Messages** | Post to the audit channel |
| **Embed Links** | The audit posts are embeds, and fail silently without this |

   Warning DMs need no permission — a bot may DM anyone it shares a server with.

### 2. Prepare the server

1. Create the inactive role (e.g. `Inactive`).
2. **Server Settings → Roles: drag the bot's own role ABOVE the inactive role**,
   and above everyone it may need to kick. A bot can only manage roles below its
   own. This is the single most common cause of the bot silently doing nothing.
3. Create a private moderator channel for the audit log.
4. With Developer Mode on, right-click to copy the ids for the server, the role,
   and the audit channel.

### 3. Configure

```bash
cp config.example.toml config.toml
cp .env.example .env
```

Fill in `DISCORD_TOKEN` in `.env`, and in `config.toml` the four ids marked
REQUIRED: `bot.guild_ids`, `flagging.inactive_role_id`, `audit.channel_id`.
Leave `safety.dry_run = true` and `kicking.enabled = false` for now.

### 4. Run

```bash
docker compose up -d --build
```

```bash
docker compose logs -f prune-bot
```

On first start the bot connects, syncs its commands, and begins the history
backfill in the background, reporting progress to the audit channel.

---

## Rolling it out

Do not skip the soak. The previews are how you find out whether your thresholds
are right for *your* server, and they cost nothing.

1. **Day 0.** Deploy with `dry_run = true`. Wait for `Backfill complete` in the
   audit channel.
2. **Day 0.** Run `/prune preview`. Look at who would be flagged. If the list
   contains people who are obviously part of the community, your thresholds are
   wrong, not them — widen the window or add them to the whitelist.
3. **Day 0.** Whitelist your moderators (`/prune whitelist add @Moderator`), any
   other roles that should never be pruned, and any individuals you want kept.
4. **Days 1–30.** Read `/prune preview` every few days. It should be stable and
   unsurprising. This is also the window during which live message counting fills
   in, so the numbers get more trustworthy, not less.
5. **Day 30.** Set `audit.channel_id`, then `/prune config set safety.dry_run false`.
   Flagging starts. Kicking is still off.
6. **Day 30.** Watch the first real flags land. Check that warning DMs arrive and
   that the button works.
7. **Day 60+.** Only once you are happy: set `kicking.enabled = true` in
   `config.toml` and restart, with `max_kicks_per_sweep = 1` for the first week.

At any point, `/prune config set safety.dry_run true` stops everything immediately.

---

## Commands

`/verify` — no permission gate, for everyone. Clears the caller's own inactive role.

Everything else lives under `/prune`, which requires **Manage Server**:

| Command | Purpose |
|---|---|
| `/prune status [member]` | Message count in the window, a per-day sparkline, current state, when the kick is due, and **every** exemption that applies |
| `/prune preview` | What the next sweep would do. Changes nothing |
| `/prune run [dry_run]` | Run a sweep now. A live run shows the numbers and asks for confirmation first |
| `/prune whitelist add <target> [reason]` | Permanently exempt a **member or role** — one mentionable picker handles both. Unflags them immediately |
| `/prune whitelist remove <target>` / `list` | Manage the whitelist. `list` marks entries whose user or role no longer exists |
| **Whitelist User** (right-click → Apps) | Same as `whitelist add`, from a member's context menu |
| `/prune pardon <member> [days] [reason]` | Temporary protection, and unflags. Contrast the whitelist, which is permanent |
| `/prune config show` / `set` / `clear` | Inspect settings; change the runtime-safe subset |
| `/prune backfill [channel] [force]` | Rescan history to rebuild counts |
| `/prune stats` | Population, how many are below threshold, backfill status, dry-run state |
| `/prune history <member>` | Everything the bot has done to one member |
| `/prune backup` | `VACUUM INTO` a timestamped copy inside the data volume |
| `/prune sync` | Re-sync slash commands (bot owner only) |

### Whitelist vs pardon

They are different tools and admins reach for the wrong one:

- **Whitelist** is permanent and role-aware. `@Moderator` on the whitelist exempts
  everyone who currently holds that role, automatically, forever.
- **Pardon** is a dated reprieve — "give them another month".

`/prune status` shows which one is protecting a given member.


### Who can run the moderator commands

By default, anyone with **Manage Server**. To grant it to a moderator role instead
(or as well), use `[access]` in `config.toml`:

```toml
[access]
role_ids = [111111111111111111]   # your Moderators role
user_ids = []                     # individuals, if a role is overkill
allow_manage_guild = true         # false = ONLY the lists above, admins included
```

Roles are evaluated live, so granting the role grants access and removing it takes
it away. The **guild owner is always allowed**, so a bad allowlist cannot lock you
out of your own bot.

`/verify` is deliberately *not* covered — it is the escape hatch for the people
being pruned, and has to work for everyone.

One wrinkle worth knowing: Discord's own command-visibility hint can only key off
built-in permissions, so once you list a role here the bot stops restricting
visibility — otherwise `/prune` would be hidden from exactly the moderators you
just granted. Access is still enforced by the bot, and non-allowed members get a
private, deliberately vague refusal — it never names the permitted roles or users,
because otherwise anyone could enumerate your moderators by running a command they
cannot use. If you want to tidy up who merely *sees* the commands, Server Settings
→ Integrations → (the bot) → Commands does that per-role.

---

### Announcing flags in a channel

Besides the warning DM, the bot can post publicly whenever it flags someone:

```toml
[flagging]
announce_channel_id = 111111111111111111
announce_ping       = true    # false mentions them without pinging

[messages]
announce_body = "{mention} has been marked inactive. Run **/verify** to keep your place - otherwise you go {deadline}."
```

Three channels, three jobs, easy to confuse:

| Setting | When it posts | Audience |
|---|---|---|
| `flagging.announce_channel_id` | every flag | members |
| `flagging.warn_channel_id` | only when a DM **fails** | the one member |
| `audit.channel_id` | every action, dry runs included | moderators |

The bot refuses to start if the announcement and audit channels are the same --
they have opposite audiences, and mixing them shows members every action the bot
takes.

**Announcements are never sent during a dry run.** Telling people they have been
flagged when nothing actually happened would be worse than staying quiet. A failed
announcement also never undoes the flag: by that point the role is already on.

### Customising what members read

Everything a member sees lives in `[messages]` in `config.toml` — no code edit
needed. Titles, bodies and the button label:

```toml
[messages]
button_label = "I'm still here"
warn_title   = "You have been marked inactive"
warn_body    = """
You have not {threshold} in **{guild}** in the last {window_days} days.
Press the button below to keep your place, or you'll be removed {deadline}.
"""
```

| Placeholder | Renders as |
|---|---|
| `{guild}` | the server name |
| `{member}` / `{mention}` | their display name / a ping |
| `{threshold}` | "posted anything", or "posted at least 3 messages" |
| `{window_days}` `{min_messages}` `{days_left}` `{kick_after_days}` | the numbers |
| `{deadline}` | a live Discord countdown, e.g. "in 3 months" |
| `{deadline_date}` | an absolute date |

Three messages are templated: the initial warning (`warn_*`), the final notice
(`final_*`), and the DM sent just before removal (`kick_*`). Bodies accept Discord
markdown.

Templates are **validated at startup**, so a typo like `{membr}` stops the bot with
a message naming the bad placeholder — rather than raising mid-sweep while trying
to DM a real person. For a literal brace, double it: `{{` or `}}`.

Separately, `kicking.kick_reason_template` sets the reason recorded in Discord's
own audit log. That one is not seen by the member.

---

## Development

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]" && .venv/Scripts/python.exe -m pytest -q
```

The decision engine is pure: `src/prunebot/domain/` never imports `discord` and
performs no I/O, and every function takes `now` explicitly rather than reading the
clock. That is what lets the test suite simulate months of a member's lifecycle in
milliseconds, with no network and no clock freezing.

`services/discord_gateway.py` is the only module that mutates Discord, and
`services/actions.py` is the only path to it — so dry-run, caps, throttling and the
audit trail each exist in exactly one place and cannot be forgotten at a call site.
`tests/conftest.py` provides a `FakeGateway` implementing the same Protocol.

```
domain/       pure decision engine (no discord, no I/O)
  eligibility.py   evaluate(snapshot, policy, now) -> Decision
  planner.py       caps + circuit breaker -> SweepPlan
services/     the Discord-facing half
  actions.py       the dry-run / cap / throttle / audit chokepoint
  sweep.py         reconcile -> snapshot -> plan -> execute
cogs/         listeners and commands
db/           schema and every SQL statement in the project
```

## Deploying to spooky

```bash
make deploy
```

Syncs the tree to `~/git/discord-prune-bot` and rebuilds. `.env` and `config.toml`
are deliberately excluded — they live on the host and are never committed.

---

## Things worth knowing

- **The kick clock runs from when the warning was delivered**, not from when the
  role was applied. Nobody is kicked on a timer that started before they were told.
- **A closed DM is an ordinary outcome.** With the shipped
  `require_warning_before_kick = false`, a member with DMs closed is still kicked on
  schedule; the failure is recorded in the audit log either way. Set it to `true`
  and an undelivered warning never starts the clock, so they are flagged but never
  kicked.
- **A moderator removing the role is read as a pardon**, so the next sweep does not
  simply put it straight back on.
- **Leaving and rejoining does not shed the flag** (`restore_flag_on_rejoin`), but
  the clock restarts and they are warned again, so it costs an honest returner
  nothing.
- **Thread and forum messages count**, and resolve to their parent channel for
  include/exclude, so you configure channels once. Channel lists are **ids**, not
  names — listing a parent channel covers every thread inside it.
- **Activity is bucketed per UTC day**, so storage is bounded by population ×
  retention regardless of how chatty the server is. A server posting a million
  messages a month costs the same as one posting ten thousand.
