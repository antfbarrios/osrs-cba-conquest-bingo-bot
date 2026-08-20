# OSRS Bingo Drop Submission Bot

A Discord bot for running an OSRS bingo. Players submit drops with `/submit`,
including a screenshot; submissions post to a mod review channel with
Approve/Reject buttons.

Setup (review channel, mod role, teams, sheets) is all done live with
Discord commands (`/setreviewchannel`, `/addteam`, etc.) — no file editing
or restarts needed for any of it. See "Configure" below.

## What it does

- `/submit` — run **inside a team's own channel**. The bot detects which
  team is submitting from the channel itself. Players fill in, in order:
  **region** (fixed list, e.g. Asgarnia, Kandarin...), **source**
  (autocompletes to only that region's bosses/skills/minigames), **drop**
  (autocompletes to only that source's possible drops), a screenshot, and
  optional **RSN**. Example: `/submit Asgarnia Kree'arra Armadyl armour`.
- **RSN** defaults to the submitter's Discord display name if left blank.
  Set it explicitly when submitting a drop on someone else's behalf.
- Region → source → drop is validated at each step against `regions.py`
  (generated from your bingo sheet), so a submission can't reference a
  combination that doesn't actually exist on the sheet.
- Every submission is posted as an embed (with the screenshot) in one
  central "pending review" channel, with **Approve** / **Reject** buttons.
- Only users with the "Manage Server" permission, or a configurable mod
  role, can click Approve/Reject.
- When a mod clicks **Approve** or **Reject**, the pending post in that
  team's own channel gets **edited in place** to show the decision (green
  or red) — one message per submission throughout its whole lifecycle,
  not a separate post for each status change.
- **On Approve**, the bot also increments that exact drop's "Drops
  obtained" cell in that **team's own** live Google Sheet by 1 (each team
  has a separate sheet — optional, skipped if Sheets isn't configured).
  The sheet's own "Points gained" formula recalculates automatically since
  the bot never touches that column.
- `/scoreboard` — public, ranks teams by **Regions Conquered**: for each
  region, whichever team has the higher total points (via `drop_points.py`)
  from approved drops in that region "conquers" it. A tie means neither
  team gets credit for that region (shown separately as "Tied regions"),
  and a region nobody has scored in yet doesn't count toward anyone.
- `/playerscoreboard` — public, flat **top 30 players** ranked by total
  points regardless of team (RSN, points, drop count, with their team
  shown alongside), using `drop_points.py` (generated from the sheet's
  "Points per" column).
- `/mydrops` — shows you (privately) your own recent submissions and
  their status.
- `/pending` — **mods only**, lists everything currently awaiting review
  with a jump link straight to each one in the review channel.
- `/help` — shows every command in a formatted list (grouped for
  everyone vs. mods only).
- **A command per region** (`/asgarnia`, `/kandarin`, etc., generated from
  `regions.py`) — shows every team's point total in that specific region,
  ranked, with a 🤝 for a tied lead instead of a medal.
- `/allregions` — the same per-region breakdown for every region at once,
  laid out as a grid, plus a "Closest Contests" summary at the top.
- `/map` — a generated image showing all 10 regions colored by whichever
  team currently controls them (same "single unambiguous leader" logic as
  `/scoreboard`'s Regions Conquered — gray means neutral/tied/unclaimed).
  It's a stylized, roughly-geography-inspired layout — not a copy of the
  actual in-game map. Comes with a region-picker dropdown underneath for
  quick details on any one region, since Discord itself can't make the
  image clickable.
- `/closest` — standalone view of every region ranked by how tight the
  race for the lead currently is (smallest point gap first; a gap of 0
  means tied), plus a separate "Available" list of any regions nobody has
  scored in yet.
- `/team <name>` — drills into one team: total points, approved drop
  count, which regions they've conquered, and their top 5 players.
- `/recent` — live feed of the 15 most recently approved drops across all
  teams, newest first, with relative timestamps.
- `/export` — **mods only**, sends the entire submission history
  (every status, every field) as a downloadable CSV file.
- `/undoapproval` — **mods only**. Run it with no arguments to get a
  dropdown of the 5 most recently approved submissions, or start typing in
  the `submission` field to search by drop/team/RSN name and jump straight
  to something older (e.g. a bad approval spotted a day later). Either way,
  picking one fully reverses that approval: status goes back to pending
  (and it re-enters the review queue, so it can be approved/rejected
  again), both the review-channel and team-channel messages revert to the
  gold "PENDING" look with buttons re-enabled, and — if Sheets sync is on
  for that team — the "Drops obtained" cell decrements by 1 to undo what
  the original approval added.
- `/setreviewchannel`, `/setmodrole`, `/addteam`, `/renameteam`,
  `/removeteam`, `/setteamsheet`, `/listconfig` — **mods only**. All of
  the bot's setup (review channel, mod role, every team's channel and
  sheet) is configured live with these, no file edits or restarts. See
  "Configure" above.
- `/resetcompetition` — **mods only**. Wipes every submission and every
  setting (review channel, mod role, all teams/sheets) in one shot, after
  requiring an exact confirmation phrase. Automatically backs up the old
  database to a timestamped file on the server first, so it's recoverable
  if needed.
- `/submithelp` — a step-by-step walkthrough of how to use `/submit`,
  what each field does, and the pet-vs-regular-item duplicate rules.
- `/rules` — shows the competition rules, set live via `/setrules`
  (mods only) — no redeploy needed to update them mid-event.
- `/obtainedpets` — shows every pet already claimed, grouped by team (or
  filtered to one team). Handy for players to check before submitting,
  since pets can only be claimed once per team (see below).
- `/regiondrops <region>` — lists every source, drop, and point value for
  one region straight from `regions.py`/`drop_points.py` — useful for
  players double-checking a drop's worth, or spot-checking a data update.
- `/missions` — shows currently active missions and each team's mission
  win count. `/submitmission` submits proof for one (run in your team's
  channel). `/newmission` (mods only) announces a new one. See "Missions"
  above for how the race/scoring works.
- `/setrsn` — save your RuneScape name once; `/submit` will default to it
  automatically instead of your Discord display name, unless you
  explicitly type a different one (e.g. submitting on someone else's
  behalf).
- `/firstblood` — shows which team scored the very first approved drop
  in each region.
- `/firstblooddetailed` — the same, but broken down per individual
  source rather than per region.
- `/compare <team1> <team2>` — head-to-head between exactly two teams:
  total points, how many regions each currently leads (only counting
  those two teams, ignoring anyone else), and a region-by-region
  breakdown.
- `/notifyme <on/off>` — opt in to get DM'd the moment your own
  submission is approved or rejected, instead of needing to check
  `/mydrops`. Off by default; failed/blocked DMs are silently ignored so
  they never affect the review itself.
- `/timeleft` — shows time remaining until `EVENT_END_DATE` (optional
  `.env` setting; the command says so plainly if it isn't configured).

**Just for fun** (no gameplay effect, purely flavor):
- `/whatshouldido` — a random source to go farm, with unhinged flavor text.
- `/qotd` — quote of the day. Same quote for everyone, deterministic per
  day (rotates at midnight UTC), rather than re-rolling every call.
- `/fortunecookie` — cracks open a random fortune from an original,
  hand-written list, with fake "lucky numbers" for authenticity.
- `/8ball <question>` — OSRS-flavored magic 8-ball.
- `/luck` — rolls a random luck percentage with deliberately mismatched
  flavor text.
- `/excuse` — random excuse generator for a dry streak.
- `/copium` — a supportive (unhinged) pep talk.
- `/roast [target]` — lightly roasts yourself or a teammate; harmless
  game-performance jokes only.
- `/blessing` — a completely fake buff, no mechanical effect.
- `/gamble` — cosmetic slot-machine roll, no real stakes or currency.
- **Duplicate heads-up (regular items)**: if a team already has an
  *approved* submission of the exact same region+source+drop, the new
  pending post shows a note with the count. This is informational only —
  it doesn't block or auto-reject anything, since tiles aren't
  one-time-only and teams are expected to farm the same drop repeatedly
  for points.
- **Pet duplicate block**: unlike regular items, **pets can only be
  claimed for points once per team** (per the sheet's blanket pet rule —
  matches any drop name starting with "Pet", including specifically-named
  variants like "Pet (Prime)"). If a team already has that exact pet
  approved, trying to approve a second one is refused outright with an
  explanation — rejecting it is unaffected, and the same pet from a
  *different region* is tracked separately and can still be claimed there.
- **Real overall scoring**: a drop's point value (from `drop_points.py`)
  only determines who **controls** a region — whichever team has the most
  points there. Controlling a region is worth a fixed number of **board
  points** (set in `main.py` as `REGION_BOARD_POINTS`, different per
  region), and a team's true "overall score" — shown in `/scoreboard`,
  `/team`, and `/compare` — is the sum of board points from every region
  it controls, plus mission bonus points (see below). A **tied** region
  (two teams with equal points) means **nobody** gets its board points,
  even though it still shows as "tied" rather than unclaimed. Raw summed
  drop points are still shown too (in `/team`, labeled "Raw Drop Points")
  as a farming-activity stat, but that number is **not** the real score.
- **Missions**: a daily (or twice-daily on weekends) challenge any team
  can race to complete first, for a flat **+50 overall-score bonus**,
  regardless of region. `/newmission <description>` (mods only)
  announces one to a dedicated mission channel (`/setmissionchannel`,
  mods only) and starts the clock —
  it stays open until a team wins it, even after a newer mission has
  started, so more than one can be active simultaneously. Players run
  `/submitmission` (in their team's channel, picking which active
  mission from autocomplete) to submit proof; it goes through the normal
  review channel. **The race is handled automatically**: the first
  approval wins it — mission points get added to that team, and every
  other still-pending submission for that same mission is instantly
  auto-rejected with a note explaining another team got there first,
  regardless of the order mods happen to click through the queue.
  `/missions` shows what's currently active plus a mission-wins
  leaderboard per team. `/cancelmission` (mods only) calls one off —
  useful for a test run — auto-rejecting any pending submissions for it
  with an explanatory note; a cancelled mission never counts as a win.
- **Starting screenshot requirements**: 16 specific tiles (Mahogany Homes,
  Tempoross, Wintertodt, various Clue tiers, etc.) require a "before"
  screenshot on file before a team can `/submit` for them — mods compare
  it against the eventual submission (e.g. proving searches were at zero
  beforehand). `/submit` **hard-blocks** with a clear error if nothing
  approved is on file yet for that tile. Players use
  `/startingscreenshot` (run in their team's channel, same pattern as
  `/submit` — or from anywhere by passing `team:` explicitly, e.g. from
  the review channel itself) to send one in; it goes through its own
  approve/reject flow. **Each team can have its own dedicated review
  channel** (`/setteamstartingscreenshotchannel`, mods only) so one team's
  screenshots don't pile up in another team's channel — any team without
  one set falls back to a single shared channel
  (`/setstartingscreenshotchannel`). Also posts to the submitting team's
  own channel, same as regular submissions. `/screenshotstatus` lets
  anyone check what's cleared/pending/missing for a team before they hit
  the block. The full requirement list lives in `main.py` as
  `STARTING_SCREENSHOT_REQUIREMENTS` — edit it directly if the tiles change.
- **Stale submission nudge**: every `STALE_CHECK_MINUTES` (default 10),
  the bot checks for anything that's been pending longer than
  `STALE_MINUTES` (default 30) and pings the mod role about it in the
  review channel, with a jump link. Each submission only gets nudged once,
  ever — approving/rejecting it, or it just sitting there after the
  nudge, won't trigger a second ping.
- All submissions (pending, approved, rejected) are saved to a local SQLite
  database (`bingo.db`) so you have a permanent record — useful for
  `/scoreboard`, custom reports, or archiving after the competition ends.

## 1. Create the Discord application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Go to **Bot** → **Reset Token** → copy the token. This goes in `.env` as `DISCORD_TOKEN`.
3. Under **Bot**, you do NOT need to enable any Privileged Gateway Intents (Message Content, Server Members, Presence) — this bot doesn't use them.
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History`
   - Copy the generated URL, open it, and invite the bot to your server.

## 2. Configure

```bash
cd osrs-bingo-bot
cp .env.example .env
```

Fill in `.env`:
- `DISCORD_TOKEN` — from step 1.
- `GUILD_ID` (optional but recommended during setup/testing) — right-click
  your server icon → Copy Server ID. Makes slash commands appear
  instantly instead of waiting up to an hour for global sync.

That's it for `.env` — **the review channel, mod role, and every team's
channel/sheet are configured live with Discord commands**, not files.
Everything below happens after you `python main.py` and the bot is online.

**Fastest path:** run `/setupwizard` in Discord — it walks through the
review channel, mod role, and adding teams interactively with dropdowns
and a quick popup for each team's name, showing you a live summary as you
go. The sections below describe the same things as individual commands,
if you'd rather do them one at a time or need to fix just one part later.

### Set up the review channel and mod role

1. Create/pick a channel for the pending-review queue.
2. Run `/setreviewchannel` inside it (or anywhere, passing `channel:` to
   pick a different one).
3. Create a role in your server for mods (any name), assign it to
   whoever should approve/reject drops.
4. Run `/setmodrole` and pick that role. Anyone with "Manage Server"
   permission can also use mod commands regardless of role.
5. If you're using starting-screenshot requirements (see below), either
   run `/setstartingscreenshotchannel` once as a shared fallback channel,
   or run `/setteamstartingscreenshotchannel` per team for each one to
   have its own dedicated channel (recommended — keeps teams from seeing
   each other's screenshots).

Both take effect immediately — no restart needed.

### Set up teams

1. Create one text channel per team (e.g. `#team-1`).
2. Run `/addteam` — give it the exact team name and pick that channel.
   Repeat per team.
3. `/submit` only works inside a channel added this way — the bot uses it
   to know which team is submitting, so players never type a team name.
4. Run `/listconfig` anytime to see everything currently set up — review
   channel, mod role, and every team with its channel and whether a
   sheet is attached.

Made a mistake, or a team's channel changes? Just run `/addteam` again
with the same team name — it overwrites the old channel. Need to change
the team's *name* instead (and keep its history intact)? Use
`/renameteam` — it updates the channel/sheet mapping AND every past
submission (pending, approved, rejected) to the new name, so nothing gets
split across two names in `/scoreboard`, `/export`, etc. `/removeteam`
deletes one entirely.

### Set up Google Sheets sync (optional)

Skip this section if you don't want approvals to auto-update sheets —
everything else works fine without it. Each team has their own separate
sheet, so the service account needs to be shared with every one of them.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) →
   create a project (or use an existing one).
2. **APIs & Services → Library** → search "Google Sheets API" → Enable.
3. **APIs & Services → Credentials** → Create Credentials → Service
   Account → give it any name → Create and Continue → skip the optional
   role/access steps → Done.
4. Click into the new service account → **Keys** tab → Add Key → Create
   New Key → JSON → this downloads a `.json` file. Move it into your
   project folder (e.g. `C:\Dev\osrs-bingo-bot\service-account.json`) —
   **never commit or share this file**, it's a login credential.
5. Open the JSON file and copy the `client_email` value (looks like
   `something@your-project.iam.gserviceaccount.com`).
6. For **each team's** Google Sheet: open it → **Share** → paste that same
   email in → give it **Editor** access → Send (it'll warn the email can't
   receive notifications — that's fine, ignore it). Every team's sheet
   needs this done individually.
7. In `.env`, set:
   - `GOOGLE_SERVICE_ACCOUNT_FILE` — path to the JSON file from step 4.
   - `GOOGLE_WORKSHEET_NAME` — the exact tab name at the bottom of the
     sheet (e.g. `Sheet4`), assumed to be the same across every team's copy.
8. Restart the bot (this part still needs `.env`, so it's the one restart
   you do need). Then run `/setteamsheet` per team — paste the sheet's ID
   (the long string in its URL between `/d/` and `/edit`). The team must
   already exist (via `/addteam`) first.
9. Approve a test submission and check that team's sheet — its "Drops
   obtained" cell should tick up by 1, and "Points gained" should update
   right along with it. `/listconfig` shows which teams have a sheet
   attached at a glance.

**Important:** if a sheet's overall column layout changes later (a region
added/removed, or its columns shifted — not just rows), you'll need to
regenerate `region_columns.py` and `regions.py`. Adding/removing/
reordering individual drop *rows* is safe and needs no changes, since the
bot looks up the correct row by matching Source+Drop text each time rather
than a fixed row number.

## 3. Install & run

```bash
pip install -r requirements.txt
python main.py
```

You should see `Logged in as <your bot name>` in the console. Try
`/submit` in your server.

**For `/map`'s image quality:** it looks for the DejaVu Sans font, which
most Linux servers already have. If `/map`'s text renders as a tiny
fallback font, install it explicitly: `apt install fonts-dejavu-core`
(no restart needed, just re-run `/map` after).

## How review works

1. A player runs `/submit` inside their team's channel, picks a region,
   then a source (filtered to that region), then a drop (filtered to that
   source), attaches a screenshot, and optionally sets an RSN (otherwise
   it defaults to their Discord display name). The bot knows which team it
   is from the channel.
2. They get an ephemeral (only-they-can-see) confirmation, and the same
   pending embed also posts publicly in their team's channel so teammates
   can see a submission is awaiting review.
3. The submission posts in your review channel as an embed with the image,
   colored gold ("PENDING"), with Approve/Reject buttons.
4. A mod clicks Approve or Reject. The embed in the review channel updates
   to green/red, shows who reviewed it, and the buttons disable. The mod
   gets a private confirmation too.
5. Either way, that same pending post in the team's channel gets **edited**
   to show the decision — approved (green) or rejected (red) — rather than
   a new message being added, so there's exactly one message per
   submission that updates live as its status changes. (If that original
   message was deleted, the bot posts a fresh one as a fallback.)
6. If approved (and Google Sheets sync is configured for that team), the
   bot also increments that drop's "Drops obtained" cell in that team's
   live sheet by 1.

Submissions are matched to their review message internally, so this works
correctly even with many pending submissions at once.

## Moving to a new server or competition

Everything below is specific to one Discord server and one bingo event.
When you set this up for a new server, or run a new competition on the
same server, work through this checklist — miss one and the symptom is
usually submissions going to the wrong channel, the wrong team's sheet
getting updated, or `/submit` not showing up at all.

**Moving to a genuinely new server** (different Discord server, e.g.
retiring a test server for the real one): the bot's `.env`/`GUILD_ID`
change, but review channel, mod role, and every team are all reconfigured
live with `/setreviewchannel`, `/setmodrole`, `/addteam`, and
`/setteamsheet` once the bot is running there — no file edits needed for
any of that. `/listconfig` shows you everything currently set, so you can
confirm the new setup is correct before announcing it's live.

**Starting a new competition on the same server** (same channels/teams,
fresh scoring): the config from `/listconfig` usually doesn't need to
change at all — it's `bingo.db` (item 4 below) that needs resetting.

| # | What | How |
|---|---|---|
| 1 | Server (for fast command sync) | `.env` → `GUILD_ID`. Right-click the new server's icon → Copy Server ID. Requires a restart. |
| 2 | Review channel, mod role, teams, sheets | `/setupwizard` (interactive, recommended), or individually: `/setreviewchannel`, `/setmodrole`, `/addteam` (per team), `/setteamsheet` (per team, if using Sheets). All live, no restart. Run `/listconfig` after to confirm. |
| 3 | Starting screenshot channel(s) | `/setstartingscreenshotchannel` (shared fallback) and/or `/setteamstartingscreenshotchannel` per team, if you're using starting-screenshot requirements. |
| 4 | Mission announcement channel | `/setmissionchannel`, if you're using missions. |
| 5 | Sheet sharing | Share every new team's sheet with your service account's email as Editor — sharing the old competition's sheets doesn't carry over. |
| 6 | Submission history + config | `/resetcompetition` (mods only) wipes all submissions AND every setting from #2/#3 in one go, auto-backing up the old data to a timestamped file on the server first. Requires typing an exact confirmation phrase. If you want to keep the same review channel/teams/sheets and only clear drop history, that's not what this does — ask if you want a "keep config, clear submissions only" option built instead. |
| 7 | Region/source/drop list | `regions.py` + `region_columns.py` + `drop_points.py`. Only needed if the new competition's bingo sheet has a different structure (different regions, sources/drops, point values, or shifted columns). Send me the new sheet and I'll regenerate all three — no changes needed if it's the same template with just new team copies. |

Things that usually **don't** need to change:
- `DISCORD_TOKEN` — same bot application can be invited to multiple
  servers, no need to create a new bot each time (unless you specifically
  want a separate bot identity).
- `GOOGLE_SERVICE_ACCOUNT_FILE` / the service account itself — one
  service account can be shared with any number of sheets across any
  number of competitions, it just needs to be invited (see #3) each time.
- `GOOGLE_WORKSHEET_NAME` — only changes if the new sheet template uses a
  different tab name than the current one.



## Extending it later

- **Fun stats report**: run `python report.py` anytime (bot doesn't need
  to be stopped first) — it reads `bingo.db` and writes a
  `drops_report_<timestamp>.md` file covering most-farmed sources, most
  active submitters, mod approval speed, submission timing patterns, and
  team head-to-head totals/rejection rates. No extra setup needed, it only
  uses what's already in the database.
- **Keep in sync with the sheet**: `regions.py`, `region_columns.py`, and
  `drop_points.py`
  are generated from your bingo spreadsheet. Adding/removing/reordering
  drop *rows* needs no changes at all. If a *region or column* changes,
  edit those two files directly, or ask to regenerate them from an
  updated sheet.

## Deploying updates to your server

Once the bot is running on a server (see hosting notes below), use
`deploy.ps1` instead of manually copying files over every time.

1. Open `deploy.ps1` and set `$ServerIP` at the top to your server's IP
   address (one-time setup).
2. Whenever you have code changes to push, run from PowerShell inside your
   project folder:
   ```
   .\deploy.ps1
   ```
   This copies every code file to the server, reinstalls dependencies (in
   case `requirements.txt` changed), restarts the `bingo-bot` service, and
   prints its status so you can confirm it came back up cleanly.

It deliberately **never touches** `.env`, `bingo.db`, or your service
account `.json` key on the server — those stay as configured there and
won't get overwritten by whatever's in your local folder.

### Pulling the live database down locally

If you want to inspect the real submission data locally, run `report.py`
against live data, or just take a manual backup, use `sync-db.ps1`:
```
.\sync-db.ps1
```
Same one-time `$ServerIP` setup as the other scripts. This is **one-way,
server → local, only** — it downloads the server's `bingo.db` and
overwrites your local copy (after automatically backing up whatever was
there first). It never pushes your local copy back up, for the same
reason `deploy.ps1` excludes `bingo.db` entirely: the server's copy is
the real, live data, and a local copy is always at risk of being stale.

### Just restarting (no file changes)

If you only changed something directly on the server (`.env` edited via
`nano`, etc.) or the bot just needs a bounce, use `restart.ps1` instead —
same one-time `$ServerIP` setup, then:
```
.\restart.ps1
```
It restarts the service and prints its status, without copying any files.
Note: review channel/mod role/team changes made via `/setreviewchannel`,
`/addteam`, etc. take effect immediately and never need this — it's only
for `.env` changes or a plain bounce.

## Notes on hosting

This runs as a long-lived process (`python main.py`) — it needs to stay
running to respond to commands. For 24/7 uptime you'll want to run it on a
small VPS, a Raspberry Pi, or a host like Railway/Fly.io/a $5 DigitalOcean
droplet, using something like `systemd`, `pm2`, or `screen`/`tmux` to keep
it alive and restart it if it crashes. Happy to help set that up once
you've picked a host.
