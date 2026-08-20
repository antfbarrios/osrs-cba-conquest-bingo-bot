import os
import logging
import csv
import io
import random
from typing import Optional, List
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import database
from regions import REGION_SOURCES
from drop_points import DROP_POINTS
import sheets_client
import map_generator

# team_channels.py / team_sheets.py are now LEGACY -- config lives in the
# database and is managed with /addteam, /removeteam, /setteamsheet, etc.
# These imports only exist to seed the database once, automatically, the
# first time this runs against a fresh install that still has them. If
# they've been deleted, that's fine too -- just start fresh with /addteam.
try:
    from team_channels import TEAM_CHANNELS as LEGACY_TEAM_CHANNELS
except ImportError:
    LEGACY_TEAM_CHANNELS = {}
try:
    from team_sheets import TEAM_SHEETS as LEGACY_TEAM_SHEETS
except ImportError:
    LEGACY_TEAM_SHEETS = {}

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # optional, speeds up command sync during dev
EVENT_END_DATE = os.getenv("EVENT_END_DATE", "").strip()

# Legacy .env fallbacks -- only used to seed the database on first run if it
# has no config yet. Once set via /setreviewchannel or /setmodrole, the
# database is authoritative and these are never consulted again.
_LEGACY_REVIEW_CHANNEL_ID = os.getenv("REVIEW_CHANNEL_ID", "0")
_LEGACY_MOD_ROLE_NAME = os.getenv("MOD_ROLE_NAME", "Bingo Mod")


def get_review_channel_id() -> int:
    value = database.get_config("review_channel_id")
    return int(value) if value else 0


def get_mod_role_name() -> str:
    return database.get_config("mod_role_name", default="Bingo Mod")


def get_starting_screenshot_channel_id() -> int:
    value = database.get_config("starting_screenshot_channel_id")
    return int(value) if value else 0


def get_mission_channel_id() -> int:
    value = database.get_config("mission_channel_id")
    return int(value) if value else 0


def migrate_legacy_config_if_needed():
    """One-time import from team_channels.py/team_sheets.py/.env into the
    database, but only for whichever pieces aren't already configured there.
    Safe to run on every startup -- it's a no-op once the database has its
    own values."""
    if database.get_config("review_channel_id") is None and _LEGACY_REVIEW_CHANNEL_ID != "0":
        database.set_config("review_channel_id", _LEGACY_REVIEW_CHANNEL_ID)
        log.info(f"Migrated REVIEW_CHANNEL_ID from .env into the database: {_LEGACY_REVIEW_CHANNEL_ID}")

    if database.get_config("mod_role_name") is None and _LEGACY_MOD_ROLE_NAME:
        database.set_config("mod_role_name", _LEGACY_MOD_ROLE_NAME)
        log.info(f"Migrated MOD_ROLE_NAME from .env into the database: {_LEGACY_MOD_ROLE_NAME}")

    if not database.get_all_teams():
        for channel_id, team_name in LEGACY_TEAM_CHANNELS.items():
            database.add_team(team_name, channel_id)
        for team_name, sheet_id in LEGACY_TEAM_SHEETS.items():
            database.set_team_sheet(team_name, sheet_id)
        if LEGACY_TEAM_CHANNELS:
            log.info(f"Migrated {len(LEGACY_TEAM_CHANNELS)} team(s) from team_channels.py/team_sheets.py into the database.")


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bingo-bot")

intents = discord.Intents.default()
# No privileged intents (message content, members, etc.) are needed --
# everything comes through slash command options and button interactions.

bot = commands.Bot(command_prefix="!", intents=intents)


def is_pet_drop(drop_name: str) -> bool:
    """Pets are stored as 'Pet' or a specifically-named variant like
    'Pet (Prime)' -- either way the drop name starts with 'Pet'."""
    return drop_name.startswith("Pet")


# Tiles that require a "before" screenshot on file before a team can submit
# points for them, so mods can compare against the eventual submission
# (e.g. Tempoross requires proof searches were at zero beforehand). Each
# entry is (region, label, description, [source names it covers]) -- a
# label can cover multiple sources (e.g. "Clues" covers every clue tier
# tracked in that region).
STARTING_SCREENSHOT_REQUIREMENTS = [
    ("Asgarnia", "Mahogany Homes", "Current points in Amy's shop.", ["Mahogany Homes"]),
    ("Desert", "Tempoross", "Current searches available. Must be zero.", ["Tempoross"]),
    ("Desert", "Guardians of the Rift",
     "Current searches available. Must be zero with 10 or less elemental or catalytic points.", ["Gotr"]),
    ("Fremennik", "Penguin Agility", "Current lap count.", ["Penguin Agility"]),
    ("Kandarin", "Barbarian Assault", "Current points in all roles. Must be under 500 per role.",
     ["Barbarian Assault"]),
    ("Kandarin", "Clues", 'Bank search for "Casket".', ["Elite Clues", "Hard Clues", "Medium Clues"]),
    ("Kandarin", "Chompies", "Current kc.", ["Chompies"]),
    ("Kourend", "Wintertodt", "Current searches available. Must be zero.", ["Wintertodt"]),
    ("Kourend", "Tithe Farm", "Current points in the reward shop.", ["Tithe Farm"]),
    ("Misthalin", "Bryo/Obor", 'Bank search for "Key". Must have zero mossy or giant keys.',
     ["Bryophyta", "Obor"]),
    ("Misthalin", "Clues", 'Bank search for "Casket".', ["Beginner Clues", "Easy Clues"]),
    ("Misthalin", "Underwater thieving/agility", 'Bank search for "Tear".', ["Underwater Agility"]),
    ("Tirannwn", "Prif laps", "Current lap count.", ["Prif agility course"]),
    ("Varlamore", "Mixology", "Current reward points.", ["Mixology"]),
    ("Varlamore", "Hunter Rumours", 'Bank search for "Sack". Must have zero rumour sacks of any tier.',
     ["Hunter Rumours"]),
    ("Wilderness", "Last Man Standing", "Current reward points.", ["Last Man Standing"]),
]

# (region, source) -> requirement label, for the /submit hard-block check
SOURCE_TO_REQUIREMENT = {}
for _req_region, _req_label, _req_desc, _req_sources in STARTING_SCREENSHOT_REQUIREMENTS:
    for _src in _req_sources:
        SOURCE_TO_REQUIREMENT[(_req_region, _src)] = _req_label

# label -> (region, description, [sources]), for /startingscreenshot and status lookups
REQUIREMENT_BY_LABEL = {
    label: (region, desc, sources) for region, label, desc, sources in STARTING_SCREENSHOT_REQUIREMENTS
}


def is_mod(member: discord.Member) -> bool:
    if member.guild_permissions.manage_guild:
        return True
    return any(role.name == get_mod_role_name() for role in member.roles)


def build_embed(submission: dict, status: str = "pending") -> discord.Embed:
    color = {
        "pending": discord.Color.gold(),
        "approved": discord.Color.green(),
        "rejected": discord.Color.red(),
    }[status]

    embed = discord.Embed(
        title=f"Drop Submission — {submission['drop_name']}",
        color=color,
    )
    embed.add_field(name="Team", value=submission["team_name"], inline=True)
    embed.add_field(name="Region", value=submission["region"], inline=True)
    embed.add_field(name="Source", value=submission["boss_name"], inline=True)
    embed.add_field(name="Drop", value=submission["drop_name"], inline=True)
    if submission.get("rsn"):
        embed.add_field(name="RSN", value=submission["rsn"], inline=True)
    if status == "pending" and submission.get("prior_approved_count"):
        count = submission["prior_approved_count"]
        if is_pet_drop(submission["drop_name"]):
            note = (
                f"This team already has this pet approved. Pets can only be claimed for points "
                f"**once per team** — approving this one will be blocked."
            )
        else:
            note = f"This team already has **{count}** approved submission(s) of this exact drop."
        embed.add_field(name="Heads up", value=note, inline=False)
    embed.set_image(url=submission["image_url"])
    embed.set_footer(text=f"Submitted by {submission['submitter_name']} • Status: {status.upper()}")
    return embed


def get_starting_screenshot_companion(submission: dict):
    """If this submission's source requires a starting screenshot, and an
    approved one is on file, returns a companion embed showing it -- so
    mods can compare against the submission without hunting through a
    different channel. Returns None if no requirement applies here, or
    (shouldn't normally happen, since /submit blocks first) none is
    approved yet."""
    required_label = SOURCE_TO_REQUIREMENT.get((submission["region"], submission["boss_name"]))
    if not required_label:
        return None
    entry = database.get_approved_starting_screenshot(submission["team_name"], required_label)
    if not entry:
        return None

    embed = discord.Embed(title=f"📸 Starting Screenshot — {required_label}", color=discord.Color.blue())
    embed.set_image(url=entry["image_url"])
    embed.set_footer(text="For comparison against the submission above")
    return embed


def build_submission_embeds(submission: dict, status: str) -> List[discord.Embed]:
    """The main submission embed, plus its starting-screenshot companion
    if this source requires one -- use this (with embeds=) everywhere a
    submission gets posted or edited, instead of build_embed() alone."""
    embeds = [build_embed(submission, status=status)]
    companion = get_starting_screenshot_companion(submission)
    if companion:
        embeds.append(companion)
    return embeds


class SubmissionReviewView(discord.ui.View):
    """Persistent view -- registered once at startup so the buttons keep
    working across bot restarts, on every submission message."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _handle(self, interaction: discord.Interaction, new_status: str):
        if not isinstance(interaction.user, discord.Member) or not is_mod(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to review submissions.", ephemeral=True
            )
            return

        submission = database.get_submission_by_message(interaction.message.id)
        if submission is None:
            await interaction.response.send_message(
                "Couldn't find this submission in the database.", ephemeral=True
            )
            return

        if submission["status"] != "pending":
            await interaction.response.send_message(
                f"This submission was already marked **{submission['status']}**.",
                ephemeral=True,
            )
            return

        # Pets can only be claimed for points ONCE per team, per the sheet's
        # blanket pet rule -- unlike regular items, which can be farmed
        # repeatedly. Block the approval outright rather than just warning,
        # since letting it through would double-count points that shouldn't
        # exist. Rejecting is unaffected (a rejected pet can be resubmitted).
        if new_status == "approved" and is_pet_drop(submission["drop_name"]):
            prior_count = database.count_approved_duplicates(
                submission["team_name"], submission["region"], submission["boss_name"], submission["drop_name"]
            )
            if prior_count > 0:
                await interaction.response.send_message(
                    f"⚠️ **{submission['team_name']}** already has an approved "
                    f"**{submission['drop_name']}** from **{submission['boss_name']}** "
                    f"({submission['region']}). Pets can only be claimed for points once per "
                    "team, so this can't be approved. Reject it instead if it's not legitimate, "
                    "or leave it pending if you need to double check first.",
                    ephemeral=True,
                )
                return

        database.update_status(submission["id"], new_status, str(interaction.user))
        submission["status"] = new_status

        embeds = build_submission_embeds(submission, status=new_status)
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(embeds=embeds, view=self)
        await interaction.followup.send(
            f"Marked **{new_status}** by {interaction.user.mention}.", ephemeral=True
        )

        if new_status == "approved" and sheets_client.is_enabled():
            sheet_id = database.get_sheet_by_team(submission["team_name"])
            if sheet_id is None:
                await interaction.followup.send(
                    f"⚠️ Approved, but no Google Sheet is configured for team "
                    f"**{submission['team_name']}** (add it to team_sheets.py) — not synced.",
                    ephemeral=True,
                )
            else:
                try:
                    found = sheets_client.increment_drop_count(
                        sheet_id, submission["region"], submission["boss_name"], submission["drop_name"]
                    )
                    if not found:
                        await interaction.followup.send(
                            f"⚠️ Approved, but couldn't find a matching row in the sheet for "
                            f"**{submission['boss_name']} / {submission['drop_name']}** in "
                            f"**{submission['region']}** — the sheet may have changed. "
                            "Update it manually for now.",
                            ephemeral=True,
                        )
                except sheets_client.SheetsNotConfigured:
                    pass  # Shouldn't happen given the is_enabled() check above, but just in case.
                except Exception as e:
                    log.exception("Failed to sync approval to Google Sheets")
                    await interaction.followup.send(
                        f"⚠️ Approved, but syncing to the sheet failed ({e}). Update it manually for now.",
                        ephemeral=True,
                    )

        if new_status in ("approved", "rejected"):
            team_channel_id = database.get_channel_by_team(submission["team_name"])
            team_channel = interaction.client.get_channel(team_channel_id) if team_channel_id else None
            team_message_id = submission.get("team_message_id")

            edited = False
            if team_channel is not None and team_message_id:
                try:
                    team_message = await team_channel.fetch_message(team_message_id)
                    await team_message.edit(embeds=embeds)
                    edited = True
                except discord.NotFound:
                    pass  # Original message was deleted; fall back to posting a new one below.

            if team_channel is not None and not edited:
                await team_channel.send(embeds=embeds)
            elif team_channel is None:
                await interaction.followup.send(
                    f"⚠️ No channel configured for team **{submission['team_name']}** "
                    "(add it to team_channels.py) -- this decision wasn't logged to a team channel.",
                    ephemeral=True,
                )

        # DM the submitter their result, if they've opted in via /notifyme.
        # Never lets a DM failure (blocked DMs, deleted account, etc.) affect
        # the review itself -- this is best-effort only.
        if new_status in ("approved", "rejected") and submission.get("submitter_id"):
            if database.get_notify_preference(submission["submitter_id"]):
                try:
                    user = interaction.client.get_user(
                        submission["submitter_id"]
                    ) or await interaction.client.fetch_user(submission["submitter_id"])
                    await user.send(
                        f"Your submission of **{submission['drop_name']}** ({submission['boss_name']}, "
                        f"{submission['region']}) was **{new_status}**.",
                        embeds=embeds,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass  # DMs closed or user unreachable; not worth surfacing to the mod.

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="bingo:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "approved")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="bingo:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "rejected")


def build_starting_screenshot_embed(entry: dict, status: str = "pending") -> discord.Embed:
    color = {
        "pending": discord.Color.gold(),
        "approved": discord.Color.green(),
        "rejected": discord.Color.red(),
    }[status]

    embed = discord.Embed(title=f"Starting Screenshot — {entry['requirement_label']}", color=color)
    embed.add_field(name="Team", value=entry["team_name"], inline=True)
    embed.add_field(name="Region", value=entry["region"], inline=True)
    embed.add_field(name="Requirement", value=entry["requirement_label"], inline=True)
    _, requirement_desc, _ = REQUIREMENT_BY_LABEL.get(entry["requirement_label"], (None, None, None))
    if requirement_desc:
        embed.add_field(name="Must show", value=requirement_desc, inline=False)
    embed.set_image(url=entry["image_url"])
    embed.set_footer(text=f"Submitted by {entry['submitter_name']} • Status: {status.upper()}")
    return embed


class StartingScreenshotReviewView(discord.ui.View):
    """Persistent view for the starting-screenshot review channel -- kept
    entirely separate from SubmissionReviewView (own table, own custom_ids)
    so the two flows can't collide with each other."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _handle(self, interaction: discord.Interaction, new_status: str):
        if not isinstance(interaction.user, discord.Member) or not is_mod(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to review starting screenshots.", ephemeral=True
            )
            return

        entry = database.get_starting_screenshot_by_message(interaction.message.id)
        if entry is None:
            await interaction.response.send_message(
                "Couldn't find this starting screenshot in the database.", ephemeral=True
            )
            return

        if entry["status"] != "pending":
            await interaction.response.send_message(
                f"This was already marked **{entry['status']}**.", ephemeral=True
            )
            return

        database.update_starting_screenshot_status(entry["id"], new_status, str(interaction.user))
        entry["status"] = new_status

        embed = build_starting_screenshot_embed(entry, status=new_status)
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"Marked **{new_status}** by {interaction.user.mention}.", ephemeral=True
        )

        team_channel_id = database.get_channel_by_team(entry["team_name"])
        team_channel = interaction.client.get_channel(team_channel_id) if team_channel_id else None
        team_message_id = entry.get("team_message_id")

        edited = False
        if team_channel is not None and team_message_id:
            try:
                team_message = await team_channel.fetch_message(team_message_id)
                await team_message.edit(embed=embed)
                edited = True
            except discord.NotFound:
                pass

        if team_channel is not None and not edited:
            await team_channel.send(embed=embed)
        elif team_channel is None:
            await interaction.followup.send(
                f"⚠️ No channel configured for team **{entry['team_name']}** -- this decision "
                "wasn't logged to a team channel.",
                ephemeral=True,
            )

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="startss:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "approved")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="startss:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "rejected")


def build_mission_embed(entry: dict, mission: dict, status: str = "pending") -> discord.Embed:
    color = {
        "pending": discord.Color.gold(),
        "approved": discord.Color.green(),
        "rejected": discord.Color.red(),
    }[status]

    embed = discord.Embed(title=f"🎯 Mission Submission — #{mission['id']}", color=color)
    embed.add_field(name="Team", value=entry["team_name"], inline=True)
    embed.add_field(name="Mission", value=mission["description"], inline=False)
    embed.set_image(url=entry["image_url"])
    footer = f"Submitted by {entry['submitter_name']} • Status: {status.upper()}"
    if status == "rejected" and (entry.get("reviewed_by") or "").startswith("auto"):
        footer = f"Auto-rejected — another team completed this mission first"
    embed.set_footer(text=footer)
    return embed


class MissionReviewView(discord.ui.View):
    """Persistent view for mission submissions. Approving one is a race:
    the FIRST approval completes the mission and awards the points, and
    every other still-pending submission for that same mission gets
    auto-rejected (with an explanatory note) since it's no longer possible
    for anyone else to win it."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _handle(self, interaction: discord.Interaction, new_status: str):
        if not isinstance(interaction.user, discord.Member) or not is_mod(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to review mission submissions.", ephemeral=True
            )
            return

        entry = database.get_mission_submission_by_message(interaction.message.id)
        if entry is None:
            await interaction.response.send_message(
                "Couldn't find this mission submission in the database.", ephemeral=True
            )
            return

        if entry["status"] != "pending":
            await interaction.response.send_message(
                f"This was already marked **{entry['status']}**.", ephemeral=True
            )
            return

        mission = database.get_mission(entry["mission_id"])
        if mission is None:
            await interaction.response.send_message(
                "Couldn't find the mission this submission belongs to.", ephemeral=True
            )
            return

        if new_status == "approved" and mission["status"] == "completed":
            await interaction.response.send_message(
                f"⚠️ This mission was already completed by **{mission['completed_by_team']}** "
                "before this could be approved. Reject it instead.",
                ephemeral=True,
            )
            return

        database.update_mission_submission_status(entry["id"], new_status, str(interaction.user))
        entry["status"] = new_status

        if new_status == "approved":
            database.complete_mission(mission["id"], entry["team_name"])
            mission["status"] = "completed"
            mission["completed_by_team"] = entry["team_name"]

        embed = build_mission_embed(entry, mission, status=new_status)
        for child in self.children:
            child.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(
            f"Marked **{new_status}** by {interaction.user.mention}."
            + (f" **{entry['team_name']}** wins the mission (+{MISSION_POINTS} pts)!" if new_status == "approved" else ""),
            ephemeral=True,
        )

        await self._update_team_message(interaction, entry, embed)

        # The race is over -- auto-reject every other still-pending
        # submission for this same mission, since nobody else can win it now.
        if new_status == "approved":
            others = database.get_pending_mission_submissions(mission["id"], exclude_id=entry["id"])
            for other in others:
                auto_reviewer = f"auto (won by {entry['team_name']})"
                database.update_mission_submission_status(other["id"], "rejected", auto_reviewer)
                other["status"] = "rejected"
                other["reviewed_by"] = auto_reviewer
                other_embed = build_mission_embed(other, mission, status="rejected")

                other_review_channel_id = other.get("channel_id")
                other_review_channel = (
                    interaction.client.get_channel(other_review_channel_id) if other_review_channel_id else None
                )
                if other_review_channel and other.get("message_id"):
                    try:
                        other_message = await other_review_channel.fetch_message(other["message_id"])
                        await other_message.edit(embed=other_embed, view=None)
                    except discord.NotFound:
                        pass

                await self._update_team_message(interaction, other, other_embed)

    async def _update_team_message(self, interaction: discord.Interaction, entry: dict, embed: discord.Embed):
        team_channel_id = database.get_channel_by_team(entry["team_name"])
        team_channel = interaction.client.get_channel(team_channel_id) if team_channel_id else None
        team_message_id = entry.get("team_message_id")
        if team_channel is not None and team_message_id:
            try:
                team_message = await team_channel.fetch_message(team_message_id)
                await team_message.edit(embed=embed)
            except discord.NotFound:
                pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="mission:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "approved")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="mission:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "rejected")


@bot.event
async def on_ready():
    database.init_db()
    migrate_legacy_config_if_needed()
    bot.add_view(SubmissionReviewView())  # re-register persistent buttons
    bot.add_view(StartingScreenshotReviewView())  # re-register persistent buttons
    bot.add_view(MissionReviewView())  # re-register persistent buttons

    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()

    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")

    if not check_stale_submissions.is_running():
        check_stale_submissions.start()


REGIONS = list(REGION_SOURCES.keys())

# Real overall scoring: drops earn REGION points (from drop_points.py), the
# team with the most region points in a region CONTROLS it, and controlling
# a region earns a fixed number of BOARD points -- a team's true "total
# score" is the sum of board points from every region it controls, plus
# mission bonus points, NOT a raw sum of every drop's point value.
REGION_BOARD_POINTS = {
    "Asgarnia": 500, "Desert": 500, "Kourend": 500, "Morytania": 500,
    "Fremennik": 400, "Tirannwn": 400, "Varlamore": 400,
    "Kandarin": 300, "Misthalin": 300, "Wilderness": 300,
}

MISSION_POINTS = 50


def compute_team_board_points(region_leaders: dict, all_teams) -> dict:
    """{team_name: board_points} -- sums REGION_BOARD_POINTS for every
    region that team currently controls (per region_leaders, from
    compute_region_leaders). Does NOT include mission points -- see
    compute_team_total_scores for the full overall score."""
    board_points = {team: 0 for team in all_teams}
    for region, leader in region_leaders.items():
        if leader:
            board_points[leader] = board_points.get(leader, 0) + REGION_BOARD_POINTS.get(region, 0)
    return board_points


def compute_team_total_scores(region_leaders: dict, all_teams) -> dict:
    """{team_name: total_score} -- board points from region control plus
    mission bonus points. This is the real "overall score" for a team."""
    board_points = compute_team_board_points(region_leaders, all_teams)
    mission_wins = database.get_mission_wins_by_team()
    return {
        team: board_points.get(team, 0) + mission_wins.get(team, 0) * MISSION_POINTS
        for team in all_teams
    }


async def boss_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    chosen_region = interaction.namespace.region
    sources = REGION_SOURCES.get(chosen_region, {})
    matches = [s for s in sources if current.lower() in s.lower()]
    return [app_commands.Choice(name=s, value=s) for s in matches[:25]]


async def drop_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    chosen_region = interaction.namespace.region
    chosen_boss = interaction.namespace.boss_name
    possible_drops = REGION_SOURCES.get(chosen_region, {}).get(chosen_boss, [])
    matches = [d for d in possible_drops if current.lower() in d.lower()]
    return [app_commands.Choice(name=d, value=d) for d in matches[:25]]


def _requires_mod(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and is_mod(interaction.user)


async def team_name_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    all_teams = sorted(t["team_name"] for t in database.get_all_teams())
    matches = [t for t in all_teams if current.lower() in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in matches[:25]]


@bot.tree.command(name="setreviewchannel", description="[Mods] Set the channel where submissions go for review")
@app_commands.describe(channel="The channel to use (defaults to the channel you run this in)")
async def setreviewchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    target = channel or interaction.channel
    database.set_config("review_channel_id", str(target.id))
    await interaction.response.send_message(f"Review channel set to {target.mention}.", ephemeral=True)


@bot.tree.command(
    name="setstartingscreenshotchannel",
    description="[Mods] Set the channel where starting screenshots go for review",
)
@app_commands.describe(channel="The channel to use (defaults to the channel you run this in)")
async def setstartingscreenshotchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    target = channel or interaction.channel
    database.set_config("starting_screenshot_channel_id", str(target.id))
    await interaction.response.send_message(f"Starting screenshot review channel set to {target.mention}.", ephemeral=True)


@bot.tree.command(name="setmissionchannel", description="[Mods] Set the channel where mission announcements are posted")
@app_commands.describe(channel="The channel to use (defaults to the channel you run this in)")
async def setmissionchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    target = channel or interaction.channel
    database.set_config("mission_channel_id", str(target.id))
    await interaction.response.send_message(f"Mission announcement channel set to {target.mention}.", ephemeral=True)


@bot.tree.command(name="setmodrole", description="[Mods] Set which role can approve/reject submissions")
@app_commands.describe(role="The role that should be treated as mods (in addition to Manage Server permission)")
async def setmodrole(interaction: discord.Interaction, role: discord.Role):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    database.set_config("mod_role_name", role.name)
    await interaction.response.send_message(
        f"Mod role set to **{role.name}**. Anyone with \"Manage Server\" permission can still use mod "
        "commands regardless of role.",
        ephemeral=True,
    )


@bot.tree.command(name="addteam", description="[Mods] Add or update a team's submission channel")
@app_commands.describe(team_name="Exact team name (must match what players will see)", channel="That team's channel")
async def addteam(interaction: discord.Interaction, team_name: str, channel: discord.TextChannel):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    team_name = team_name.strip()
    if not team_name:
        await interaction.response.send_message("Team name can't be empty.", ephemeral=True)
        return

    existing = database.get_channel_by_team(team_name)
    database.add_team(team_name, channel.id)
    if existing:
        await interaction.response.send_message(
            f"Updated **{team_name}** to use {channel.mention} (was previously a different channel).",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"Added **{team_name}** — {channel.mention}. Don't forget /setteamsheet if you're using "
            "Google Sheets sync for this team.",
            ephemeral=True,
        )


@bot.tree.command(name="removeteam", description="[Mods] Remove a team")
@app_commands.describe(team_name="Which team to remove")
@app_commands.autocomplete(team_name=team_name_autocomplete)
async def removeteam(interaction: discord.Interaction, team_name: str):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    removed = database.remove_team(team_name)
    if removed:
        await interaction.response.send_message(f"Removed **{team_name}**.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"**{team_name}** wasn't found. Check /listconfig for the exact current team names.",
            ephemeral=True,
        )


@bot.tree.command(name="renameteam", description="[Mods] Rename a team -- updates its config AND all historical submissions")
@app_commands.describe(old_name="The team's current name", new_name="What to rename it to")
@app_commands.autocomplete(old_name=team_name_autocomplete)
async def renameteam(interaction: discord.Interaction, old_name: str, new_name: str):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    new_name = new_name.strip()
    if not new_name:
        await interaction.response.send_message("New name can't be empty.", ephemeral=True)
        return

    result = database.rename_team(old_name, new_name)
    if result == "ok":
        await interaction.response.send_message(
            f"Renamed **{old_name}** → **{new_name}**. Its channel, sheet, and every past submission "
            "(pending, approved, and rejected) now use the new name.",
            ephemeral=True,
        )
    elif result == "name_taken":
        await interaction.response.send_message(
            f"**{new_name}** is already a different team's name. Pick something else, or "
            f"/removeteam the other one first if that's intentional.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"**{old_name}** wasn't found. Check /listconfig for the exact current team names.",
            ephemeral=True,
        )


@bot.tree.command(name="setteamsheet", description="[Mods] Set or update a team's Google Sheet ID")
@app_commands.describe(team_name="Which team", sheet_id="The sheet's ID (from its URL, between /d/ and /edit)")
@app_commands.autocomplete(team_name=team_name_autocomplete)
async def setteamsheet(interaction: discord.Interaction, team_name: str, sheet_id: str):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    sheet_id = sheet_id.strip()
    updated = database.set_team_sheet(team_name, sheet_id)
    if updated:
        await interaction.response.send_message(f"Sheet for **{team_name}** set.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"**{team_name}** isn't a configured team yet -- add it first with /addteam.",
            ephemeral=True,
        )


@bot.tree.command(
    name="setteamstartingscreenshotchannel",
    description="[Mods] Set a specific team's starting-screenshot review channel",
)
@app_commands.describe(
    team_name="Which team",
    channel="Where THIS team's starting screenshots should go for review (defaults to the channel you run this in)",
)
@app_commands.autocomplete(team_name=team_name_autocomplete)
async def setteamstartingscreenshotchannel(
    interaction: discord.Interaction, team_name: str, channel: Optional[discord.TextChannel] = None
):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    target = channel or interaction.channel
    updated = database.set_team_starting_screenshot_channel(team_name, target.id)
    if updated:
        await interaction.response.send_message(
            f"**{team_name}**'s starting screenshots will now go to {target.mention}.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"**{team_name}** isn't a configured team yet -- add it first with /addteam.",
            ephemeral=True,
        )


@bot.tree.command(name="listconfig", description="[Mods] Show the bot's current configuration")
async def listconfig(interaction: discord.Interaction):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    review_channel_id = get_review_channel_id()
    review_channel = interaction.client.get_channel(review_channel_id) if review_channel_id else None
    starting_ss_channel_id = get_starting_screenshot_channel_id()
    starting_ss_channel = interaction.client.get_channel(starting_ss_channel_id) if starting_ss_channel_id else None
    mission_channel_id = get_mission_channel_id()
    mission_channel = interaction.client.get_channel(mission_channel_id) if mission_channel_id else None
    mod_role_name = get_mod_role_name()
    teams = database.get_all_teams()

    embed = discord.Embed(title="⚙️ Current Configuration", color=discord.Color.blue())
    embed.add_field(
        name="Review channel",
        value=review_channel.mention if review_channel else "⚠️ Not set (use /setreviewchannel)",
        inline=False,
    )
    embed.add_field(
        name="Starting screenshot channel (fallback)",
        value=starting_ss_channel.mention if starting_ss_channel else "⚠️ Not set (use /setstartingscreenshotchannel)",
        inline=False,
    )
    embed.add_field(
        name="Mission announcement channel",
        value=mission_channel.mention if mission_channel else "⚠️ Not set (use /setmissionchannel)",
        inline=False,
    )
    embed.add_field(name="Mod role", value=f"**{mod_role_name}**", inline=False)
    embed.add_field(
        name="Google Sheets sync",
        value="✅ Enabled (service account configured)" if sheets_client.is_enabled() else "❌ Not configured",
        inline=False,
    )

    if teams:
        lines = []
        for t in teams:
            channel = interaction.client.get_channel(t["channel_id"])
            channel_desc = channel.mention if channel else f"⚠️ unknown channel ({t['channel_id']})"
            sheet_desc = "✅ sheet set" if t["sheet_id"] else "— no sheet"
            ss_channel_id = t.get("starting_screenshot_channel_id")
            if ss_channel_id:
                ss_channel = interaction.client.get_channel(ss_channel_id)
                ss_desc = f"✅ {ss_channel.mention}" if ss_channel else f"⚠️ unknown channel ({ss_channel_id})"
            else:
                ss_desc = "— using fallback" if starting_ss_channel else "— none set"
            lines.append(f"**{t['team_name']}** — {channel_desc} — {sheet_desc} — start-ss: {ss_desc}")
        embed.add_field(name=f"Teams ({len(teams)})", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Teams", value="⚠️ None configured yet (use /addteam)", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="regiondrops",
    description="List every source, drop, and point value for a region",
)
@app_commands.describe(region="Which region to inspect")
@app_commands.choices(region=[app_commands.Choice(name=r, value=r) for r in REGIONS])
async def regiondrops(interaction: discord.Interaction, region: str):
    sources = REGION_SOURCES.get(region)
    if not sources:
        await interaction.response.send_message(f"No data found for **{region}**.", ephemeral=True)
        return

    embed = discord.Embed(title=f"📋 {region} — Drop Data", color=discord.Color.dark_teal())
    total_drops = 0
    total_chars = 0
    truncated = False

    for source in sorted(sources):
        if truncated:
            break
        drops = sources[source]
        lines = [f"{drop} — {DROP_POINTS.get((region, source, drop), '?')} pts" for drop in sorted(drops)]
        value = "\n".join(lines)[:1024]
        # Discord caps total embed size around 6000 chars -- stop adding
        # fields before risking a failed send on a very data-heavy region.
        if total_chars + len(value) > 5000:
            truncated = True
            break
        embed.add_field(name=f"{source} ({len(drops)})", value=value, inline=True)
        total_chars += len(value)
        total_drops += len(drops)

    footer = f"{len(sources)} sources, {total_drops} drops total"
    if truncated:
        footer += " -- some sources omitted to fit Discord's size limit"
    embed.set_footer(text=footer)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="resetcompetition",
    description="[Mods] DANGER: wipes ALL submissions AND config (review channel, teams, sheets). Backs up first.",
)
@app_commands.describe(confirm="Type exactly: WIPE EVERYTHING -- this is how the command protects against accidents")
async def resetcompetition(interaction: discord.Interaction, confirm: str):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    if confirm != "WIPE EVERYTHING":
        await interaction.response.send_message(
            "Confirmation text didn't match, nothing was touched. To actually wipe everything "
            "(all submissions, review channel, mod role, every team, and their sheets), run this "
            "command again with `confirm` set to exactly: `WIPE EVERYTHING`",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    backup_path = database.wipe_everything()

    await interaction.followup.send(
        "✅ Everything has been wiped: all submissions, the review channel, mod role, and every "
        f"team's channel/sheet config.\n\nA backup of the old data was saved on the server at "
        f"`{backup_path}` before wiping, in case you need it back.\n\n"
        "To get running again, run: `/setreviewchannel`, `/setmodrole`, `/addteam` (per team), "
        "and `/setteamsheet` (per team, if using Sheets sync) -- then `/listconfig` to confirm.",
        ephemeral=True,
    )


def build_wizard_embed() -> discord.Embed:
    review_channel_id = get_review_channel_id()
    mod_role_name = get_mod_role_name()
    teams = database.get_all_teams()

    embed = discord.Embed(title="🧙 Bingo Setup Wizard", color=discord.Color.blurple())
    embed.add_field(
        name="1. Review channel",
        value=f"<#{review_channel_id}>" if review_channel_id else "❌ Not set",
        inline=False,
    )
    embed.add_field(
        name="2. Mod role",
        value=f"**{mod_role_name}**" if database.get_config("mod_role_name") else "⚠️ Not set yet (defaults to 'Bingo Mod')",
        inline=False,
    )
    if teams:
        lines = [
            f"**{t['team_name']}** — <#{t['channel_id']}> — {'✅ sheet attached' if t['sheet_id'] else '— no sheet'}"
            for t in teams
        ]
        embed.add_field(name=f"3. Teams ({len(teams)})", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="3. Teams", value="❌ None added yet", inline=False)
    embed.set_footer(text="Use the buttons below. Click Refresh after each step to update this view.")
    return embed


class WizardHubView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Set Review Channel", style=discord.ButtonStyle.primary, emoji="📢")
    async def set_review_channel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Pick the review channel:", view=WizardChannelPickView(purpose="review"), ephemeral=True
        )

    @discord.ui.button(label="Set Mod Role", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def set_mod_role_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Pick the mod role:", view=WizardRolePickView(), ephemeral=True)

    @discord.ui.button(label="Add Team", style=discord.ButtonStyle.success, emoji="➕")
    async def add_team_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Pick that team's channel (you'll be asked for its name next):",
            view=WizardChannelPickView(purpose="team"),
            ephemeral=True,
        )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=build_wizard_embed(), view=self)

    @discord.ui.button(label="Finish", style=discord.ButtonStyle.danger, emoji="✅")
    async def finish_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="Setup wizard closed. Run `/listconfig` anytime to review, or `/setupwizard` to reopen this.",
            embed=None,
            view=self,
        )


class WizardChannelPickView(discord.ui.View):
    """purpose is 'review' (sets the review channel directly) or 'team'
    (the channel picked here is then paired with a team name via modal)."""

    def __init__(self, purpose: str):
        super().__init__(timeout=120)
        self.purpose = purpose

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="Choose a channel")
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        if self.purpose == "review":
            database.set_config("review_channel_id", str(channel.id))
            await interaction.response.edit_message(content=f"✅ Review channel set to {channel.mention}.", view=None)
            await interaction.followup.send(embed=build_wizard_embed(), view=WizardHubView(), ephemeral=True)
        else:
            await interaction.response.send_modal(WizardTeamNameModal(channel))


class WizardRolePickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(cls=discord.ui.RoleSelect, placeholder="Choose a role")
    async def select_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        role = select.values[0]
        database.set_config("mod_role_name", role.name)
        await interaction.response.edit_message(content=f"✅ Mod role set to **{role.name}**.", view=None)
        await interaction.followup.send(embed=build_wizard_embed(), view=WizardHubView(), ephemeral=True)


class WizardTeamNameModal(discord.ui.Modal, title="Team Name"):
    team_name_input = discord.ui.TextInput(label="Team name", placeholder="e.g. Team 1", max_length=100)

    def __init__(self, channel: discord.TextChannel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        team_name = str(self.team_name_input.value).strip()
        if not team_name:
            await interaction.response.send_message("Team name can't be empty.", ephemeral=True)
            return
        database.add_team(team_name, self.channel.id)
        await interaction.response.send_message(
            f"✅ Added **{team_name}** — {self.channel.mention}.", ephemeral=True
        )
        await interaction.followup.send(embed=build_wizard_embed(), view=WizardHubView(), ephemeral=True)


@bot.tree.command(
    name="setupwizard",
    description="[Mods] Interactive step-by-step setup for review channel, mod role, and teams",
)
async def setupwizard(interaction: discord.Interaction):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.send_message(embed=build_wizard_embed(), view=WizardHubView(), ephemeral=True)


@bot.tree.command(name="submit", description="Submit a drop for the bingo (run this in your team's channel)")
@app_commands.describe(
    region="The bingo tile's region",
    boss_name="The source the drop came from (boss, skill, minigame, or clue tier)",
    drop_name="The name of the item that dropped",
    screenshot="Screenshot proof of the drop",
    rsn="The RuneScape name that got the drop (optional -- defaults to your Discord name; set this if submitting on someone else's behalf)",
)
@app_commands.choices(region=[app_commands.Choice(name=r, value=r) for r in REGIONS])
@app_commands.autocomplete(boss_name=boss_autocomplete, drop_name=drop_autocomplete)
async def submit(
    interaction: discord.Interaction,
    region: str,
    boss_name: str,
    drop_name: str,
    screenshot: discord.Attachment,
    rsn: Optional[str] = None,
):
    team_name = database.get_team_by_channel(interaction.channel_id)
    if team_name is None:
        await interaction.response.send_message(
            "This command only works inside your team's channel. "
            "Head to your team's channel and try again there.",
            ephemeral=True,
        )
        return

    sources_in_region = REGION_SOURCES.get(region)
    if sources_in_region is None:
        await interaction.response.send_message(
            f"**{region}** isn't a recognized region. Please pick one from the list.",
            ephemeral=True,
        )
        return

    possible_drops = sources_in_region.get(boss_name)
    if possible_drops is None:
        await interaction.response.send_message(
            f"**{boss_name}** isn't a recognized source for **{region}**. "
            "Please pick one from the autocomplete list as you type.",
            ephemeral=True,
        )
        return

    # Match case-insensitively so a manually-typed drop still works, but store
    # the canonical spelling from regions.py for consistent embeds/records.
    canonical_drop = next(
        (d for d in possible_drops if d.lower() == drop_name.strip().lower()), None
    )
    if canonical_drop is None:
        valid = ", ".join(possible_drops)
        await interaction.response.send_message(
            f"**{drop_name}** isn't a recognized drop for **{boss_name}** in **{region}**. "
            f"Please pick one from the autocomplete list. Valid drops: {valid}",
            ephemeral=True,
        )
        return
    drop_name = canonical_drop

    # Some tiles require a "before" screenshot on file before points can be
    # submitted at all, so mods can compare it against this submission.
    required_label = SOURCE_TO_REQUIREMENT.get((region, boss_name))
    if required_label and not database.has_approved_starting_screenshot(team_name, required_label):
        await interaction.response.send_message(
            f"⛔ **{boss_name}** requires an approved starting screenshot on file before you can "
            f"submit for it (requirement: **{required_label}**). Run `/startingscreenshot` first, "
            "wait for a mod to approve it, then come back and submit here. Check `/screenshotstatus` "
            "to see what's already on file for your team.",
            ephemeral=True,
        )
        return

    # Default RSN to their saved RSN (via /setrsn) if they have one, otherwise
    # their Discord display name -- unless they explicitly typed one here
    # (e.g. submitting a drop on someone else's behalf).
    if not rsn or not rsn.strip():
        rsn = database.get_user_rsn(interaction.user.id) or interaction.user.display_name
    else:
        rsn = rsn.strip()

    if not (screenshot.content_type and screenshot.content_type.startswith("image/")):
        await interaction.response.send_message(
            "That attachment doesn't look like an image. Please attach a screenshot.",
            ephemeral=True,
        )
        return

    review_channel_id = get_review_channel_id()
    if review_channel_id == 0:
        await interaction.response.send_message(
            "The bot isn't configured with a review channel yet. Ask an admin to run /setreviewchannel.",
            ephemeral=True,
        )
        return

    review_channel = interaction.client.get_channel(review_channel_id)
    if review_channel is None:
        await interaction.response.send_message(
            "Couldn't find the configured review channel. Ask an admin to run /setreviewchannel again.",
            ephemeral=True,
        )
        return

    prior_approved_count = database.count_approved_duplicates(team_name, region, boss_name, drop_name)

    submission_id = database.create_submission(
        team_name=team_name,
        region=region,
        boss_name=boss_name,
        drop_name=drop_name,
        rsn=rsn,
        image_url=screenshot.url,
        submitter_id=interaction.user.id,
        submitter_name=str(interaction.user),
    )
    submission = {
        "team_name": team_name,
        "region": region,
        "boss_name": boss_name,
        "drop_name": drop_name,
        "rsn": rsn,
        "image_url": screenshot.url,
        "submitter_name": str(interaction.user),
        "prior_approved_count": prior_approved_count,
    }

    embeds = build_submission_embeds(submission, status="pending")
    message = await review_channel.send(embeds=embeds, view=SubmissionReviewView())
    database.set_message_ref(submission_id, message.id, message.channel.id)

    team_message = await interaction.channel.send(embeds=embeds)
    database.set_team_message_ref(submission_id, team_message.id)

    await interaction.response.send_message(
        "Submission received! A mod will review it shortly.", ephemeral=True
    )


@bot.tree.command(name="setrsn", description="Save your RuneScape name so /submit uses it automatically")
@app_commands.describe(rsn="Your RuneScape display name")
async def setrsn(interaction: discord.Interaction, rsn: str):
    rsn = rsn.strip()
    if not rsn:
        await interaction.response.send_message("RSN can't be empty.", ephemeral=True)
        return
    database.set_user_rsn(interaction.user.id, rsn)
    await interaction.response.send_message(
        f"Saved! `/submit` will now default to **{rsn}** unless you type a different name.",
        ephemeral=True,
    )


async def requirement_label_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    matches = [label for label in REQUIREMENT_BY_LABEL if current.lower() in label.lower()]
    return [app_commands.Choice(name=label, value=label) for label in matches[:25]]


@bot.tree.command(
    name="startingscreenshot",
    description="Submit a 'before' screenshot for a tile that requires one",
)
@app_commands.describe(
    requirement="Which tile this is for",
    screenshot="Screenshot showing the required starting state",
    team="Optional: pick your team explicitly (needed if not running this in your team's channel)",
)
@app_commands.autocomplete(requirement=requirement_label_autocomplete, team=team_name_autocomplete)
async def startingscreenshot(
    interaction: discord.Interaction,
    requirement: str,
    screenshot: discord.Attachment,
    team: Optional[str] = None,
):
    if team is not None:
        all_teams = sorted(t["team_name"] for t in database.get_all_teams())
        if team not in all_teams:
            await interaction.response.send_message(
                f"**{team}** isn't a recognized team. Please pick one from the autocomplete list.",
                ephemeral=True,
            )
            return
        team_name = team
    else:
        team_name = database.get_team_by_channel(interaction.channel_id)
        if team_name is None:
            await interaction.response.send_message(
                "This only works automatically inside your team's channel. Either run it there, "
                "or pick your team explicitly with the `team` option to use it anywhere (e.g. in "
                "the starting screenshot review channel).",
                ephemeral=True,
            )
            return

    req = REQUIREMENT_BY_LABEL.get(requirement)
    if req is None:
        valid = ", ".join(REQUIREMENT_BY_LABEL.keys())
        await interaction.response.send_message(
            f"**{requirement}** isn't a recognized requirement. Please pick one from the "
            f"autocomplete list. Valid requirements: {valid}",
            ephemeral=True,
        )
        return
    region, description, _sources = req

    if not (screenshot.content_type and screenshot.content_type.startswith("image/")):
        await interaction.response.send_message(
            "That attachment doesn't look like an image. Please attach a screenshot.",
            ephemeral=True,
        )
        return

    channel_id = database.get_starting_screenshot_channel_by_team(team_name) or get_starting_screenshot_channel_id()
    if not channel_id:
        await interaction.response.send_message(
            "The bot isn't configured with a starting screenshot review channel yet for "
            f"**{team_name}** (or a fallback). Ask an admin to run "
            "/setteamstartingscreenshotchannel for your team, or /setstartingscreenshotchannel "
            "as a shared fallback.",
            ephemeral=True,
        )
        return

    review_channel = interaction.client.get_channel(channel_id)
    if review_channel is None:
        await interaction.response.send_message(
            "Couldn't find the configured starting screenshot channel. "
            "Ask an admin to run /setteamstartingscreenshotchannel (or /setstartingscreenshotchannel) again.",
            ephemeral=True,
        )
        return

    entry_id = database.create_starting_screenshot(
        team_name=team_name,
        requirement_label=requirement,
        region=region,
        image_url=screenshot.url,
        submitter_id=interaction.user.id,
        submitter_name=str(interaction.user),
    )
    entry = {
        "team_name": team_name,
        "requirement_label": requirement,
        "region": region,
        "image_url": screenshot.url,
        "submitter_name": str(interaction.user),
    }

    embed = build_starting_screenshot_embed(entry, status="pending")
    message = await review_channel.send(embed=embed, view=StartingScreenshotReviewView())
    database.set_starting_screenshot_message_ref(entry_id, message.id, message.channel.id)

    team_channel_id = database.get_channel_by_team(team_name)
    team_channel = interaction.client.get_channel(team_channel_id) if team_channel_id else None
    if team_channel is not None:
        team_message = await team_channel.send(embed=embed)
        database.set_starting_screenshot_team_message_ref(entry_id, team_message.id)

    await interaction.response.send_message(
        "Starting screenshot received! A mod will review it shortly.", ephemeral=True
    )


@bot.tree.command(
    name="screenshotstatus",
    description="See which starting-screenshot requirements your team has cleared, pending, or missing",
)
@app_commands.describe(team="Optional: check a different team (defaults to your own)")
@app_commands.autocomplete(team=team_name_autocomplete)
async def screenshotstatus(interaction: discord.Interaction, team: Optional[str] = None):
    if team is None:
        team = database.get_team_by_channel(interaction.channel_id)
        if team is None:
            await interaction.response.send_message(
                "Run this in your team's channel, or pass a `team` to check a specific one.",
                ephemeral=True,
            )
            return
    else:
        all_teams = sorted(t["team_name"] for t in database.get_all_teams())
        if team not in all_teams:
            await interaction.response.send_message(
                f"**{team}** isn't a recognized team. Please pick one from the autocomplete list.",
                ephemeral=True,
            )
            return

    status_by_label = database.get_starting_screenshots_for_team(team)

    icons = {"approved": "✅", "pending": "🟡", "rejected": "🔴"}
    lines = []
    for region, label, _desc, _sources in STARTING_SCREENSHOT_REQUIREMENTS:
        entry = status_by_label.get(label)
        if entry:
            icon = icons.get(entry["status"], "⚪")
            lines.append(f"{icon} **{label}** ({region}) — {entry['status']}")
        else:
            lines.append(f"⬜ **{label}** ({region}) — not submitted")

    embed = discord.Embed(title=f"📋 {team} — Starting Screenshot Status", color=discord.Color.blue())
    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="newmission",
    description="[Mods] Announce a new mission -- first team to complete it wins the bonus",
)
@app_commands.describe(description="What the mission/challenge is")
async def newmission(interaction: discord.Interaction, description: str):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    description = description.strip()
    if not description:
        await interaction.response.send_message("Mission description can't be empty.", ephemeral=True)
        return

    mission_channel_id = get_mission_channel_id()
    if mission_channel_id == 0:
        await interaction.response.send_message(
            "The bot isn't configured with a mission announcement channel yet. "
            "Ask an admin to run /setmissionchannel.",
            ephemeral=True,
        )
        return

    mission_channel = interaction.client.get_channel(mission_channel_id)
    if mission_channel is None:
        await interaction.response.send_message(
            "Couldn't find the configured mission announcement channel. "
            "Ask an admin to run /setmissionchannel again.",
            ephemeral=True,
        )
        return

    mission_id = database.create_mission(description, str(interaction.user))

    announce_embed = discord.Embed(
        title=f"🎯 New Mission — #{mission_id}",
        description=description,
        color=discord.Color.purple(),
    )
    announce_embed.add_field(
        name="How to claim it",
        value=(
            f"First team to complete this gets **+{MISSION_POINTS} points**. "
            f"Run `/submitmission` in your team's channel with proof once you've done it — "
            "stays open until someone wins it, even after the next mission starts."
        ),
        inline=False,
    )

    await mission_channel.send(
        content="@everyone",
        embed=announce_embed,
        allowed_mentions=discord.AllowedMentions(everyone=True),
    )

    await interaction.response.send_message(
        f"Mission #{mission_id} announced in {mission_channel.mention}.",
        ephemeral=True,
    )


async def active_mission_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    active = database.get_active_missions()
    matches = [m for m in active if current.lower() in m["description"].lower()]
    return [
        app_commands.Choice(name=f"#{m['id']} — {m['description'][:80]}", value=str(m["id"]))
        for m in matches[:25]
    ]


@bot.tree.command(
    name="submitmission",
    description="Submit proof of completing an active mission (run this in your team's channel)",
)
@app_commands.describe(
    mission="Which mission you completed",
    screenshot="Proof of completion",
)
@app_commands.autocomplete(mission=active_mission_autocomplete)
async def submitmission(interaction: discord.Interaction, mission: str, screenshot: discord.Attachment):
    team_name = database.get_team_by_channel(interaction.channel_id)
    if team_name is None:
        await interaction.response.send_message(
            "This command only works inside your team's channel. "
            "Head to your team's channel and try again there.",
            ephemeral=True,
        )
        return

    try:
        mission_id = int(mission)
    except ValueError:
        await interaction.response.send_message(
            "Please pick a mission from the autocomplete list.", ephemeral=True
        )
        return

    mission_row = database.get_mission(mission_id)
    if mission_row is None:
        await interaction.response.send_message(
            "That mission doesn't exist. Please pick one from the autocomplete list.", ephemeral=True
        )
        return

    if mission_row["status"] == "completed":
        await interaction.response.send_message(
            f"⛔ Mission #{mission_id} was already completed by "
            f"**{mission_row['completed_by_team']}** — nothing left to claim here.",
            ephemeral=True,
        )
        return

    if not (screenshot.content_type and screenshot.content_type.startswith("image/")):
        await interaction.response.send_message(
            "That attachment doesn't look like an image. Please attach a screenshot.",
            ephemeral=True,
        )
        return

    review_channel_id = get_review_channel_id()
    review_channel = interaction.client.get_channel(review_channel_id) if review_channel_id else None
    if review_channel is None:
        await interaction.response.send_message(
            "The bot isn't configured with a review channel yet. Ask an admin to run /setreviewchannel.",
            ephemeral=True,
        )
        return

    entry_id = database.create_mission_submission(
        mission_id=mission_id,
        team_name=team_name,
        image_url=screenshot.url,
        submitter_id=interaction.user.id,
        submitter_name=str(interaction.user),
    )
    entry = {
        "team_name": team_name,
        "image_url": screenshot.url,
        "submitter_name": str(interaction.user),
    }

    embed = build_mission_embed(entry, mission_row, status="pending")
    message = await review_channel.send(embed=embed, view=MissionReviewView())
    database.set_mission_submission_message_ref(entry_id, message.id, message.channel.id)

    team_message = await interaction.channel.send(embed=embed)
    database.set_mission_submission_team_message_ref(entry_id, team_message.id)

    await interaction.response.send_message(
        "Mission submission received! A mod will review it shortly.", ephemeral=True
    )


@bot.tree.command(name="missions", description="Show active missions and each team's mission wins")
async def missions(interaction: discord.Interaction):
    active = database.get_active_missions()
    wins = database.get_mission_wins_by_team()

    embed = discord.Embed(title="🎯 Missions", color=discord.Color.purple())
    if active:
        lines = [f"**#{m['id']}** — {m['description']}" for m in active]
        embed.add_field(name=f"Active ({len(active)})", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Active", value="No missions are open right now.", inline=False)

    if wins:
        ranked = sorted(wins.items(), key=lambda kv: kv[1], reverse=True)
        lines = [f"{MEDALS.get(i, f'{i + 1}.')} **{team}**: {count} win(s)" for i, (team, count) in enumerate(ranked)]
        embed.add_field(name="Mission Wins", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Mission Wins", value="No missions completed yet.", inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="cancelmission", description="[Mods] Cancel an active mission (e.g. for a test run, or a mistake)")
@app_commands.describe(mission="Which mission to cancel")
@app_commands.autocomplete(mission=active_mission_autocomplete)
async def cancelmission(interaction: discord.Interaction, mission: str):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    try:
        mission_id = int(mission)
    except ValueError:
        await interaction.response.send_message(
            "Please pick a mission from the autocomplete list.", ephemeral=True
        )
        return

    mission_row = database.get_mission(mission_id)
    if mission_row is None:
        await interaction.response.send_message(
            "That mission doesn't exist. Please pick one from the autocomplete list.", ephemeral=True
        )
        return

    if mission_row["status"] != "active":
        await interaction.response.send_message(
            f"Mission #{mission_id} is already **{mission_row['status']}**, nothing to cancel.",
            ephemeral=True,
        )
        return

    database.cancel_mission(mission_id)
    mission_row["status"] = "cancelled"

    pending = database.get_pending_mission_submissions(mission_id)
    for entry in pending:
        database.update_mission_submission_status(entry["id"], "rejected", "auto (mission cancelled)")
        entry["status"] = "rejected"
        entry["reviewed_by"] = "auto (mission cancelled)"
        rejected_embed = build_mission_embed(entry, mission_row, status="rejected")

        review_channel_id = entry.get("channel_id")
        review_channel = interaction.client.get_channel(review_channel_id) if review_channel_id else None
        if review_channel and entry.get("message_id"):
            try:
                message = await review_channel.fetch_message(entry["message_id"])
                await message.edit(embed=rejected_embed, view=None)
            except discord.NotFound:
                pass

        team_channel_id = database.get_channel_by_team(entry["team_name"])
        team_channel = interaction.client.get_channel(team_channel_id) if team_channel_id else None
        if team_channel and entry.get("team_message_id"):
            try:
                team_message = await team_channel.fetch_message(entry["team_message_id"])
                await team_message.edit(embed=rejected_embed)
            except discord.NotFound:
                pass

    await interaction.response.send_message(
        f"Mission #{mission_id} cancelled."
        + (f" {len(pending)} pending submission(s) auto-rejected." if pending else ""),
        ephemeral=True,
    )


MEDALS = {0: "🥇", 1: "🥈", 2: "🥉"}


def compute_team_region_points():
    """Returns {team_name: {region: total_points}} from every approved submission."""
    approved = database.get_approved_submissions()
    team_region_points = defaultdict(lambda: defaultdict(int))
    for row in approved:
        points = DROP_POINTS.get((row["region"], row["boss_name"], row["drop_name"]), 0)
        team_region_points[row["team_name"]][row["region"]] += points
    return team_region_points


def build_region_standings_lines(region: str, team_region_points, all_teams) -> list:
    """Ranked '{medal} **Team** — N pts' lines for one region, with a
    handshake emoji for ties at the top instead of a medal."""
    scores = {team: team_region_points[team].get(region, 0) for team in all_teams}
    max_points = max(scores.values()) if scores else 0
    leaders = [team for team, pts in scores.items() if pts == max_points] if max_points > 0 else []

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    lines = []
    for i, (team, points) in enumerate(ranked):
        if max_points == 0:
            prefix = "•"
        elif points == max_points and len(leaders) > 1:
            prefix = "🤝"
        else:
            prefix = MEDALS.get(i, f"{i + 1}.")
        lines.append(f"{prefix} **{team}** — {points} pts")
    return lines


def compute_closest_regions(team_region_points, all_teams, limit: int = 5):
    """Ranks claimed regions by how close the top two teams are, smallest
    gap first. A gap of 0 means tied for the lead. Unclaimed regions
    (nobody has scored) are excluded entirely -- there's no contest yet."""
    results = []
    for region in REGION_SOURCES:
        scores = {team: team_region_points[team].get(region, 0) for team in all_teams}
        max_points = max(scores.values())
        if max_points == 0:
            continue
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        leader, leader_pts = ranked[0]
        runner_up, runner_up_pts = ranked[1] if len(ranked) > 1 else (None, 0)
        gap = leader_pts - runner_up_pts
        results.append(
            {
                "region": region,
                "gap": gap,
                "leader": leader,
                "leader_pts": leader_pts,
                "runner_up": runner_up,
                "runner_up_pts": runner_up_pts,
            }
        )
    results.sort(key=lambda r: r["gap"])
    return results[:limit]


def get_unclaimed_regions(team_region_points, all_teams) -> list:
    """Regions where every team still has 0 points -- nobody's touched them yet."""
    unclaimed = []
    for region in REGION_SOURCES:
        scores = {team: team_region_points[team].get(region, 0) for team in all_teams}
        if max(scores.values()) == 0:
            unclaimed.append(region)
    return unclaimed


def format_closest_line(entry: dict) -> str:
    if entry["gap"] == 0:
        return f"🤝 **{entry['region']}** — tied at {entry['leader_pts']} pts ({entry['leader']} vs {entry['runner_up']})"
    return (
        f"**{entry['region']}** — {entry['gap']} pts apart "
        f"({entry['leader']} leads {entry['runner_up']}, {entry['leader_pts']} vs {entry['runner_up_pts']})"
    )


@bot.tree.command(name="scoreboard", description="Show team standings by regions conquered")
async def scoreboard(interaction: discord.Interaction):
    embed = discord.Embed(title="🏆 Bingo Scoreboard", color=discord.Color.gold())

    team_region_points = compute_team_region_points()
    all_teams = sorted(t["team_name"] for t in database.get_all_teams())
    region_leaders = compute_region_leaders(team_region_points, all_teams)

    # --- Overall score: this is the REAL total -- board points from every
    # region a team controls, plus mission bonus points. Not a raw sum of
    # every drop's point value.
    total_scores = compute_team_total_scores(region_leaders, all_teams)
    mission_wins = database.get_mission_wins_by_team()
    ranked_by_score = sorted(all_teams, key=lambda t: total_scores.get(t, 0), reverse=True)
    score_lines = []
    for i, team in enumerate(ranked_by_score):
        prefix = MEDALS.get(i, f"{i + 1}.")
        wins = mission_wins.get(team, 0)
        mission_note = f" (+{wins}×{MISSION_POINTS} mission)" if wins else ""
        score_lines.append(f"{prefix} **{team}**: {total_scores.get(team, 0)} pts{mission_note}")
    embed.add_field(name="🏅 Overall Score", value="\n".join(score_lines) if score_lines else "No teams yet.", inline=False)

    # --- Regions conquered: supporting stat -- how many regions each team
    # currently controls (a tie means nobody gets credit for that one).
    conquered_counts = {team: 0 for team in all_teams}
    tied_regions = [region for region, leader in region_leaders.items() if leader is None
                    and max(team_region_points[t].get(region, 0) for t in all_teams) > 0]
    for region, leader in region_leaders.items():
        if leader:
            conquered_counts[leader] += 1

    conquered_lines = []
    ranked_teams = sorted(conquered_counts, key=lambda t: conquered_counts[t], reverse=True)
    for i, team in enumerate(ranked_teams):
        prefix = MEDALS.get(i, f"{i + 1}.")
        region_word = "region" if conquered_counts[team] == 1 else "regions"
        conquered_lines.append(f"{prefix} **{team}**: {conquered_counts[team]} {region_word}")
    if tied_regions:
        conquered_lines.append(f"**Tied regions**: {len(tied_regions)} ({', '.join(tied_regions)})")

    embed.add_field(name="🗺️ Regions Conquered", value="\n".join(conquered_lines), inline=False)

    discord_file = None
    if all_teams:
        try:
            team_colors = map_generator.assign_team_colors(all_teams)
            buffer = map_generator.build_map_image(region_leaders, team_colors)
            discord_file = discord.File(buffer, filename="region_map.png")
            embed.set_image(url="attachment://region_map.png")
        except Exception as e:
            log.exception("Failed to generate map image for /scoreboard")

    if discord_file:
        await interaction.response.send_message(embed=embed, file=discord_file)
    else:
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="playerscoreboard", description="Show the top 30 players ranked by points, across all teams")
async def playerscoreboard(interaction: discord.Interaction):
    approved = database.get_approved_submissions()

    # Keyed by (team_name, rsn) so two different people who happen to share
    # an RSN on different teams don't get merged into one entry.
    player_stats = defaultdict(lambda: {"points": 0, "drops": 0})
    for row in approved:
        points = DROP_POINTS.get((row["region"], row["boss_name"], row["drop_name"]), 0)
        rsn = row["rsn"] or "Unknown"
        key = (row["team_name"], rsn)
        player_stats[key]["points"] += points
        player_stats[key]["drops"] += 1

    embed = discord.Embed(title="🏆 Player Scoreboard", color=discord.Color.gold())
    if not player_stats:
        embed.description = "No approved drops yet — get farming!"
    else:
        ranked = sorted(player_stats.items(), key=lambda kv: kv[1]["points"], reverse=True)[:30]
        lines = []
        for i, ((team, rsn), stats) in enumerate(ranked):
            prefix = MEDALS.get(i, f"{i + 1}.")
            lines.append(f"{prefix} **{rsn}** — {stats['points']} pts, {stats['drops']} drops ({team})")
        embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed)



@bot.tree.command(name="team", description="Show one team's overview: points, regions conquered, top players")
@app_commands.describe(team="Which team to look up")
@app_commands.autocomplete(team=team_name_autocomplete)
async def team_overview(interaction: discord.Interaction, team: str):
    all_teams = sorted(t["team_name"] for t in database.get_all_teams())
    if team not in all_teams:
        await interaction.response.send_message(
            f"**{team}** isn't a recognized team. Please pick one from the autocomplete list.",
            ephemeral=True,
        )
        return

    approved = database.get_approved_submissions()
    team_rows = [r for r in approved if r["team_name"] == team]

    raw_drop_points = sum(
        DROP_POINTS.get((r["region"], r["boss_name"], r["drop_name"]), 0) for r in team_rows
    )
    total_drops = len(team_rows)

    team_region_points = compute_team_region_points()
    region_leaders = compute_region_leaders(team_region_points, all_teams)
    conquered = [region for region, leader in region_leaders.items() if leader == team]

    total_scores = compute_team_total_scores(region_leaders, all_teams)
    overall_score = total_scores.get(team, 0)
    mission_wins = database.get_mission_wins_by_team().get(team, 0)

    player_stats = defaultdict(lambda: {"points": 0, "drops": 0})
    for r in team_rows:
        points = DROP_POINTS.get((r["region"], r["boss_name"], r["drop_name"]), 0)
        rsn = r["rsn"] or "Unknown"
        player_stats[rsn]["points"] += points
        player_stats[rsn]["drops"] += 1
    top_players = sorted(player_stats.items(), key=lambda kv: kv[1]["points"], reverse=True)[:5]

    embed = discord.Embed(title=f"📊 {team} Overview", color=discord.Color.blue())
    embed.add_field(name="Overall Score", value=str(overall_score), inline=True)
    embed.add_field(name="Mission Wins", value=f"{mission_wins} (+{mission_wins * MISSION_POINTS} pts)", inline=True)
    embed.add_field(name="Approved Drops", value=str(total_drops), inline=True)
    embed.add_field(
        name="Regions Conquered",
        value=f"{len(conquered)} ({', '.join(conquered)})" if conquered else "0",
        inline=True,
    )
    embed.add_field(name="Raw Drop Points", value=f"{raw_drop_points} (farming activity, not overall score)", inline=True)
    if top_players:
        lines = [f"**{rsn}** — {s['points']} pts, {s['drops']} drops" for rsn, s in top_players]
        embed.add_field(name="Top Players", value="\n".join(lines), inline=False)

    discord_file = None
    try:
        filtered_leaders = {region: (team if region in conquered else None) for region in REGION_SOURCES}
        team_colors = map_generator.assign_team_colors(all_teams)
        buffer = map_generator.build_map_image(filtered_leaders, team_colors)
        discord_file = discord.File(buffer, filename="region_map.png")
        embed.set_image(url="attachment://region_map.png")
    except Exception:
        log.exception("Failed to generate map image for /team")

    if discord_file:
        await interaction.response.send_message(embed=embed, file=discord_file)
    else:
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="compare", description="Head-to-head comparison between two teams")
@app_commands.describe(team1="First team", team2="Second team")
@app_commands.autocomplete(team1=team_name_autocomplete, team2=team_name_autocomplete)
async def compare(interaction: discord.Interaction, team1: str, team2: str):
    all_teams = sorted(t["team_name"] for t in database.get_all_teams())
    if team1 not in all_teams or team2 not in all_teams:
        await interaction.response.send_message(
            "Please pick two valid teams from the autocomplete list.", ephemeral=True
        )
        return
    if team1 == team2:
        await interaction.response.send_message("Pick two different teams to compare.", ephemeral=True)
        return

    team_region_points = compute_team_region_points()
    region_leaders = compute_region_leaders(team_region_points, all_teams)
    total_scores = compute_team_total_scores(region_leaders, all_teams)
    total1 = total_scores.get(team1, 0)
    total2 = total_scores.get(team2, 0)

    leads1 = leads2 = ties = 0
    region_lines = []
    filtered_leaders = {}
    for region in REGION_SOURCES:
        p1 = team_region_points[team1].get(region, 0)
        p2 = team_region_points[team2].get(region, 0)
        if p1 == 0 and p2 == 0:
            continue  # neither team has scored here -- not part of this head-to-head
        if p1 > p2:
            leads1 += 1
            region_lines.append(f"🔵 **{region}** — {team1} leads, {p1} vs {p2}")
            filtered_leaders[region] = team1
        elif p2 > p1:
            leads2 += 1
            region_lines.append(f"🔴 **{region}** — {team2} leads, {p2} vs {p1}")
            filtered_leaders[region] = team2
        else:
            ties += 1
            region_lines.append(f"🤝 **{region}** — tied, {p1} pts each")

    embed = discord.Embed(title=f"⚔️ {team1} vs {team2}", color=discord.Color.purple())
    embed.add_field(name=team1, value=f"{total1} pts overall", inline=True)
    embed.add_field(name=team2, value=f"{total2} pts overall", inline=True)
    embed.add_field(
        name="Region Leads (this head-to-head)",
        value=f"{team1}: {leads1} • {team2}: {leads2} • Tied: {ties}",
        inline=False,
    )
    if region_lines:
        embed.add_field(name="By Region", value="\n".join(region_lines)[:1024], inline=False)
    else:
        embed.add_field(name="By Region", value="Neither team has scored anywhere yet.", inline=False)

    discord_file = None
    try:
        team_colors = map_generator.assign_team_colors(all_teams)
        buffer = map_generator.build_map_image(filtered_leaders, team_colors)
        discord_file = discord.File(buffer, filename="region_map.png")
        embed.set_image(url="attachment://region_map.png")
    except Exception:
        log.exception("Failed to generate map image for /compare")

    if discord_file:
        await interaction.response.send_message(embed=embed, file=discord_file)
    else:
        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="mydrops", description="See your own recent submissions and their status")
async def mydrops(interaction: discord.Interaction):
    submissions = database.get_submissions_by_submitter(interaction.user.id, limit=15)

    embed = discord.Embed(title="Your Recent Submissions", color=discord.Color.blurple())
    if not submissions:
        embed.description = "You haven't submitted anything yet."
    else:
        status_icons = {"pending": "🟡", "approved": "🟢", "rejected": "🔴"}
        lines = []
        for s in submissions:
            icon = status_icons.get(s["status"], "⚪")
            lines.append(
                f"{icon} **{s['drop_name']}** ({s['boss_name']}, {s['region']}) — "
                f"{s['team_name']} — {s['status']}"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Showing your {len(submissions)} most recent submissions")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="notifyme", description="Get DM'd when your own submission gets approved or rejected")
@app_commands.describe(enabled="Turn DM notifications on or off")
async def notifyme(interaction: discord.Interaction, enabled: bool):
    database.set_notify_preference(interaction.user.id, enabled)
    state = "on" if enabled else "off"
    await interaction.response.send_message(f"DM notifications turned **{state}**.", ephemeral=True)


@bot.tree.command(name="recent", description="Show the most recently approved drops across all teams")
async def recent(interaction: discord.Interaction):
    submissions = database.get_recent_approved(limit=15)

    embed = discord.Embed(title="🟢 Recently Approved Drops", color=discord.Color.green())
    if not submissions:
        embed.description = "No approved drops yet — get farming!"
    else:
        lines = []
        for s in submissions:
            when = ""
            if s.get("reviewed_at"):
                try:
                    ts = int(datetime.fromisoformat(s["reviewed_at"]).timestamp())
                    when = f" — <t:{ts}:R>"
                except ValueError:
                    pass
            lines.append(
                f"**{s['drop_name']}** ({s['boss_name']}, {s['region']}) — "
                f"{s['team_name']} — {s['rsn']}{when}"
            )
        embed.description = "\n".join(lines)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="pending", description="[Mods] List all submissions awaiting review")
async def pending(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_mod(interaction.user):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
        return

    submissions = database.get_pending_submissions()

    embed = discord.Embed(title="⏳ Pending Submissions", color=discord.Color.gold())
    if not submissions:
        embed.description = "Nothing pending — you're all caught up!"
    else:
        lines = []
        for s in submissions:
            jump_link = ""
            if interaction.guild and s.get("message_id") and s.get("channel_id"):
                jump_link = f" — [jump](https://discord.com/channels/{interaction.guild.id}/{s['channel_id']}/{s['message_id']})"

            source_desc = s["boss_name"]
            if s.get("region"):
                source_desc += f", {s['region']}"

            lines.append(
                f"**{s['drop_name']}** ({source_desc}) — "
                f"{s['team_name']} — submitted by {s['submitter_name']}{jump_link}"
            )
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"{len(submissions)} pending, oldest first")

    await interaction.response.send_message(embed=embed, ephemeral=True)


WHATSHOULDIDO_FLAVOR = [
    "The bingo gods have spoken. Go kill **{source}** in **{region}** and bring back a **{drop}**, or don't come back at all.",
    "Today's mission, should you choose to accept it: **{source}** ({region}). Reward: **{drop}** (allegedly).",
    "Legends say **{source}** drops a **{drop}** once in a blue moon. Today's the day. Probably not, but today's the day.",
    "Your destiny is **{source}** in **{region}**. Your reward is **{drop}**. Your odds are questionable.",
    "Nobody has grinded **{source}** for a **{drop}** yet. Be the change you wish to see.",
    "The RNG spirits demand a sacrifice of your time at **{source}** ({region}). They're hoping for a **{drop}**.",
    "You rolled the dice and it landed on: **{source}**. Go get that **{drop}**, champion.",
    "A wise man once said: \"Have you tried **{source}**?\" Go find out what a **{drop}** looks like.",
    "Breaking news: **{source}** in **{region}** is calling your name. It's whispering about a **{drop}**.",
]


@bot.tree.command(name="whatshouldido", description="Get a random boss/source to go farm")
async def whatshouldido(interaction: discord.Interaction):
    region = random.choice(list(REGION_SOURCES.keys()))
    source = random.choice(list(REGION_SOURCES[region].keys()))
    drop = random.choice(REGION_SOURCES[region][source])

    flavor = random.choice(WHATSHOULDIDO_FLAVOR).format(source=source, region=region, drop=drop)

    embed = discord.Embed(title="🎲 What Should I Do?", description=flavor, color=discord.Color.random())
    await interaction.response.send_message(embed=embed)


EIGHT_BALL_ANSWERS = [
    "The Blade of Saeldor says yes.",
    "Zulrah says ask again after 500 more kills.",
    "Signs point to a dry streak.",
    "Absolutely. Manifest it.",
    "The RNG gods are silent on this one.",
    "Vorkath doesn't care about your question.",
    "Outlook hazier than Wilderness fog.",
    "It is decided: no.",
    "Ask the drop table, not me.",
    "Very doubtful, but stranger things have happened.",
    "The chat is typing... the answer is yes.",
    "Reply hazy, try grinding again.",
]


@bot.tree.command(name="8ball", description="Ask the magic 8-ball a question")
@app_commands.describe(question="What do you want to ask?")
async def eight_ball(interaction: discord.Interaction, question: str):
    answer = random.choice(EIGHT_BALL_ANSWERS)
    embed = discord.Embed(title="🎱 The Magic 8-Ball Speaks", color=discord.Color.dark_purple())
    embed.add_field(name="Question", value=question, inline=False)
    embed.add_field(name="Answer", value=answer, inline=False)
    await interaction.response.send_message(embed=embed)


LUCK_OUTCOMES = [
    "You will get 3 pets today.",
    "You will get a bronze arrow, and nothing else, ever again.",
    "Zulrah will finally give up the mutagen. Today only.",
    "You will step on every trapdoor in every dungeon.",
    "RNGesus is busy today. Try again tomorrow.",
    "You will 1-tick every prayer flick perfectly, for once.",
    "Someone will teleblock you mid-fight for no reason.",
    "Your next drop will be an untradeable you already have 12 of.",
    "You will get the drop... in the wrong world.",
    "Vorkath will finally give up the head. Or maybe not.",
]


@bot.tree.command(name="luck", description="Roll your luck for today")
async def luck(interaction: discord.Interaction):
    percent = random.randint(1, 100)
    outcome = random.choice(LUCK_OUTCOMES)
    embed = discord.Embed(
        title="🍀 Today's Luck",
        description=f"**{percent}%** luck.\n{outcome}",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


EXCUSES = [
    "The RNG seed was corrupted by a passing seagull.",
    "Jagex nerfed my drop rate specifically. I can feel it.",
    "I was one-ticking and it desynced.",
    "My ping spiked exactly when the loot rolled.",
    "I forgot to pray, so the drop table forgot me too.",
    "The kill counter reset itself out of spite.",
    "I accidentally killed it in the wrong game mode.",
    "The drop was there, I just didn't loot fast enough and a rat took it.",
    "My teammate left right before the loot dropped, taking my luck with them.",
    "I was today years old when I realized I had the wrong kill count tracked.",
]


@bot.tree.command(name="excuse", description="Generate an official excuse for your dry streak")
async def excuse(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎭 Official Excuse Generator",
        description=random.choice(EXCUSES),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed)


COPIUM_LINES = [
    "It's not bad luck, it's just dry streak season. Everyone has one. Yours is just longer.",
    "Statistically, you're due. That's how statistics work. Probably.",
    "The drop is coming. I can feel it. I have no basis for this but I believe it.",
    "Think of all the XP you're getting while you wait. That's basically the drop.",
    "You're not unlucky, the drop table is just shy.",
    "Every kill without a drop is one kill closer to the drop. This is not how probability works but it feels true.",
    "Manifestation is a real strategy. Unproven, but real.",
    "The real drop was the friends we made along the way. Sorry.",
]


@bot.tree.command(name="copium", description="Get a supportive (unhinged) pep talk for your dry streak")
async def copium(interaction: discord.Interaction):
    embed = discord.Embed(
        title="💨 Copium Dispensary",
        description=random.choice(COPIUM_LINES),
        color=discord.Color.light_grey(),
    )
    await interaction.response.send_message(embed=embed)


ROAST_LINES = [
    "{target} still hasn't gotten a single approved drop. Bold strategy.",
    "{target}'s bank is 90% junk and 10% hope.",
    "{target} once died to a level 2 rat. We don't talk about it. Except now.",
    "{target} thinks flicking prayers means switching tabs.",
    "{target} has more deaths than drops this event. Impressive, actually.",
    "{target} still uses the default cursor unironically.",
    "{target}'s clue scroll strategy is 'skip it'.",
    "{target} has been AFK at the bank for a suspicious amount of time.",
]


@bot.tree.command(name="roast", description="Lightly roast yourself or a teammate")
@app_commands.describe(target="Who to roast (leave blank to roast yourself)")
async def roast(interaction: discord.Interaction, target: Optional[discord.Member] = None):
    target = target or interaction.user
    line = random.choice(ROAST_LINES).format(target=target.display_name)
    embed = discord.Embed(title="🔥 Roast", description=line, color=discord.Color.dark_orange())
    await interaction.response.send_message(embed=embed)


BLESSINGS = [
    "+15% Drop Rate (not real, but manifest it)",
    "+50% XP from flexing on Discord",
    "Immunity to disconnects for the next hour (unverified)",
    "Guaranteed unique... in your dreams tonight",
    "+100% chance the next NPC says something nice to you",
    "Blessed with the RNG of a mid-tier streamer",
    "The next chest you open WILL be worth it. Maybe.",
    "Your next kill counts double. In spirit only.",
]


@bot.tree.command(name="blessing", description="Receive a completely fake blessing")
async def blessing(interaction: discord.Interaction):
    embed = discord.Embed(
        title="✨ You Have Been Blessed",
        description=random.choice(BLESSINGS),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="This blessing has no mechanical effect whatsoever.")
    await interaction.response.send_message(embed=embed)


SLOT_EMOJIS = ["🍒", "🍋", "🍇", "💎", "7️⃣", "🍀", "⭐"]


@bot.tree.command(name="gamble", description="Roll the slots (purely cosmetic, no real stakes)")
async def gamble(interaction: discord.Interaction):
    roll = [random.choice(SLOT_EMOJIS) for _ in range(3)]
    result_line = " ".join(roll)
    if roll[0] == roll[1] == roll[2]:
        outcome = "🎉 JACKPOT! (You win nothing. This is purely cosmetic.)"
    elif roll[0] == roll[1] or roll[1] == roll[2] or roll[0] == roll[2]:
        outcome = "So close. Still nothing."
    else:
        outcome = "Better luck next time."
    embed = discord.Embed(
        title="🎰 Gamble",
        description=f"# {result_line}\n{outcome}",
        color=discord.Color.magenta(),
    )
    await interaction.response.send_message(embed=embed)


QOTD_QUOTES = [
    '"The grind doesn\'t stop, but neither does the disappointment." — Anonymous Bingo Participant',
    '"99 rows of nothing later..." — Every Barrows run ever',
    '"It\'s not RNG, it\'s character building." — Someone coping',
    '"Zulrah has never loved anyone." — Ancient proverb',
    '"A wise skiller once said: just one more kill." — Narrator, 400 kills later',
    '"The real treasure was the tears we cried at Chambers of Xeric." — Raider wisdom',
    '"Drop rate is a social construct." — Someone with 0 drops',
    '"Behind every 1/5000 drop is a person who almost quit." — Motivational poster, probably',
    '"You miss 100% of the drops you don\'t grind for." — probably not Wayne Gretzky',
    '"Today\'s kill count is tomorrow\'s regret." — Efficiency scape handbook',
    '"May your loot beam be ever bright." — Old Bingo blessing',
    '"The bank is full, the heart is empty." — Someone with 2000 unidentified items',
    '"Persistence beats RNG. Eventually. Allegedly." — Grinder\'s Creed',
    '"Not every kill needs to drop something. Some kills just need to happen." — Zen bosser',
    '"The prayer flick you attempt today, you fail tomorrow too." — Combat wisdom',
]


@bot.tree.command(name="qotd", description="Quote of the day -- same for everyone, changes at midnight UTC")
async def qotd(interaction: discord.Interaction):
    today = datetime.now(timezone.utc).date()
    day_rng = random.Random(today.toordinal())  # separate instance -- doesn't disturb global randomness
    quote = day_rng.choice(QOTD_QUOTES)

    embed = discord.Embed(title="📜 Quote of the Day", description=quote, color=discord.Color.teal())
    embed.set_footer(text=today.strftime("%B %d, %Y"))
    await interaction.response.send_message(embed=embed)


FORTUNE_COOKIES = [
    # Wisdom-parody (sounds deep, isn't)
    "A closed loot chest is worth two in the imagination.",
    "He who grinds without a goal, grinds forever, and honestly, fair enough.",
    "The obstacle in your path is also your path. You are now lost.",
    "Not all who wander are lost. Some are just AFK.",
    "Success is a series of small kills, until it isn't.",
    "The best time to log off was an hour ago. The second best time is also now, actually.",
    # Absurdist / mundane predictions
    "You will trip over nothing today. Twice.",
    "Somewhere, a raccoon is smarter than you today. Not you specifically. Probably.",
    "You will be mildly inconvenienced by a Tuesday, regardless of the actual day.",
    "A stranger will compliment your shoes. This is your entire fortune. Enjoy it.",
    "Your socks do not match. This is fine. This was always fine.",
    "Beware of low-flying pigeons. They know something you don't.",
    "You will find a coin today. It will be a penny. Heads up, though.",
    "Today's forecast: chaotic, with a chance of nonsense.",
    "A printer, somewhere, is jamming specifically because you exist.",
    # Meta / self-aware
    "This fortune intentionally left vague. You're welcome.",
    "The fortune you seek is in another cookie.",
    "Congratulations, you have unlocked: reading a fortune cookie.",
    "This message was focus-tested on zero people.",
    "Warning: this fortune is not legally binding, emotionally or otherwise.",
    "404: Fortune not found. Please try again after a snack.",
    "You will regret reading this. Too late now.",
    # Gibberish
    "Blorp. Also, good things ahead, probably.",
    "The frog knows. Ask the frog.",
    "Seventeen geese disagree with your life choices.",
    "Ah yes. The thing. It approaches.",
    "Somewhere a wizard did something. It concerns you slightly.",
    "Static. Then a whisper: 'nice try.'",
    "The soup remembers.",
    # Ominous-but-harmless
    "You are being watched. It's just a pigeon. Relax.",
    "Something is coming. It's probably just Tuesday again.",
    "Doom approaches at a very leisurely pace.",
    "The void has no comment at this time.",
    # OSRS-flavored chaos
    "Your next drop is loading. Estimated time: unknown.",
    "The RNG gods have seen your prayer. They are ignoring it.",
    "A dry streak ends the moment you stop counting it. You will not stop counting it.",
    "You will get exactly one bronze arrow today. Cherish it.",
    "42% of drop rates are made up. This fortune is 100% real.*",
    "*not legally binding, see above",
]


@bot.tree.command(name="fortunecookie", description="Crack open a random fortune cookie")
async def fortunecookie(interaction: discord.Interaction):
    fortune = random.choice(FORTUNE_COOKIES)
    lucky_numbers = ", ".join(str(random.randint(1, 99)) for _ in range(6))

    embed = discord.Embed(title="🥠 Fortune Cookie", description=f"*{fortune}*", color=discord.Color.gold())
    embed.set_footer(text=f"Lucky numbers: {lucky_numbers}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="timeleft", description="Show how much time is left in the event")
async def timeleft(interaction: discord.Interaction):
    if not EVENT_END_DATE:
        await interaction.response.send_message(
            "No event end date is configured. Ask an admin to set EVENT_END_DATE in .env.",
            ephemeral=True,
        )
        return

    try:
        end_dt = datetime.fromisoformat(EVENT_END_DATE)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        await interaction.response.send_message(
            "EVENT_END_DATE is set but isn't a valid date. Ask an admin to fix it in .env "
            "(expected format: YYYY-MM-DD, or a full ISO timestamp).",
            ephemeral=True,
        )
        return

    now = datetime.now(timezone.utc)
    ts = int(end_dt.timestamp())
    if end_dt <= now:
        await interaction.response.send_message(f"⏰ The event ended <t:{ts}:R> (<t:{ts}:F>).")
    else:
        await interaction.response.send_message(f"⏳ Event ends <t:{ts}:R> (<t:{ts}:F>).")


@bot.tree.command(name="submithelp", description="Instructions for how to submit a drop")
async def submithelp(interaction: discord.Interaction):
    embed = discord.Embed(title="📸 How to Submit a Drop", color=discord.Color.blurple())
    embed.add_field(
        name="1. Go to your team's channel",
        value="`/submit` only works inside your own team's channel — the bot uses that to know which team you're on.",
        inline=False,
    )
    embed.add_field(
        name="2. Run /submit and fill in, in order",
        value=(
            "**region** — pick from the list\n"
            "**boss_name** — autocompletes to sources in that region\n"
            "**drop_name** — autocompletes to drops from that source\n"
            "**screenshot** — required, attach your proof\n"
            "**rsn** — optional, defaults to your saved RSN (`/setrsn`) or your Discord name"
        ),
        inline=False,
    )
    embed.add_field(
        name="3. Wait for review",
        value=(
            "Your team's channel gets a gold PENDING post right away. A mod approves or "
            "rejects it, and that same post updates to green/red — check `/mydrops` anytime "
            "to see your own submission history."
        ),
        inline=False,
    )
    embed.add_field(
        name="A few things to know",
        value=(
            "• You can resubmit the same **regular item** as many times as you actually get it.\n"
            "• **Pets** can only be claimed for points **once per team** — check `/obtainedpets` "
            "before submitting if you're not sure.\n"
            "• Submitting on someone else's behalf? Just type their RSN manually instead of "
            "leaving it blank."
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


class RulesModal(discord.ui.Modal, title="Set Competition Rules"):
    def __init__(self, current_text: str = ""):
        super().__init__()
        self.rules_input = discord.ui.TextInput(
            label="Rules text",
            style=discord.TextStyle.paragraph,
            placeholder="Paste the full rules here...",
            default=current_text[:4000] if current_text else None,
            max_length=4000,
            required=True,
        )
        self.add_item(self.rules_input)

    async def on_submit(self, interaction: discord.Interaction):
        database.set_config("rules_text", str(self.rules_input.value))
        await interaction.response.send_message("Rules updated. Run /rules to see how it looks.", ephemeral=True)


@bot.tree.command(name="setrules", description="[Mods] Set or update the competition rules shown by /rules")
async def setrules(interaction: discord.Interaction):
    if not _requires_mod(interaction):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
        return

    current_text = database.get_config("rules_text") or ""
    await interaction.response.send_modal(RulesModal(current_text))


@bot.tree.command(name="rules", description="Show the competition rules")
async def rules(interaction: discord.Interaction):
    rules_text = database.get_config("rules_text")
    if not rules_text:
        await interaction.response.send_message(
            "No rules have been set yet. A mod can add them with /setrules.", ephemeral=True
        )
        return

    embed = discord.Embed(title="📜 Competition Rules", description=rules_text[:4096], color=discord.Color.dark_gold())
    await interaction.response.send_message(embed=embed)


async def team_or_all_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    all_teams = sorted(t["team_name"] for t in database.get_all_teams())
    matches = [t for t in all_teams if current.lower() in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in matches[:25]]


@bot.tree.command(name="obtainedpets", description="Show every pet already claimed, grouped by team")
@app_commands.describe(team="Optional: only show one team's pets")
@app_commands.autocomplete(team=team_or_all_autocomplete)
async def obtainedpets(interaction: discord.Interaction, team: Optional[str] = None):
    pets = database.get_approved_pets()
    if team:
        pets = [p for p in pets if p["team_name"] == team]

    embed = discord.Embed(title="🐾 Obtained Pets", color=discord.Color.teal())
    if not pets:
        embed.description = "No pets claimed yet." if not team else f"**{team}** hasn't claimed any pets yet."
        await interaction.response.send_message(embed=embed)
        return

    by_team = defaultdict(list)
    for p in pets:
        by_team[p["team_name"]].append(p)

    for team_name in sorted(by_team):
        lines = [
            f"{p['boss_name']} ({p['region']}) — {p['drop_name']} — {p['rsn'] or 'Unknown'}"
            for p in sorted(by_team[team_name], key=lambda p: (p["region"], p["boss_name"]))
        ]
        value = "\n".join(lines)[:1024]
        embed.add_field(name=f"{team_name} ({len(by_team[team_name])})", value=value, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="List everything this bot can do")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 Bingo Bot Commands",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="For everyone",
        value=(
            "**/submit** — Submit a drop (run this in your team's channel)\n"
            "**/submithelp** — Step-by-step instructions for submitting\n"
            "**/rules** — Competition rules\n"
            "**/setrsn** — Save your RSN so /submit uses it automatically\n"
            "**/scoreboard** — Team standings by regions conquered\n"
            "**/playerscoreboard** — Top 30 players ranked by points\n"
            "**/allregions** — Team standings broken down by every region\n"
            "**/map** — Visual map of which team controls each region\n"
            "**/<region>** — e.g. /asgarnia, /kandarin — standings for just that region\n"
            "**/closest** — Regions with the tightest race between teams\n"
            "**/team <name>** — One team's overview: points, regions, top players\n"
            "**/compare <team1> <team2>** — Head-to-head between two teams\n"
            "**/obtainedpets** — Every pet already claimed, grouped by team\n"
            "**/regiondrops <region>** — Every source/drop/point value for a region"
        ),
        inline=False,
    )
    embed.add_field(
        name="Starting Screenshots",
        value=(
            "**/startingscreenshot** — Submit a 'before' screenshot for a tile that requires one\n"
            "**/screenshotstatus** — See what's cleared/pending/missing for your team"
        ),
        inline=False,
    )
    embed.add_field(
        name="Missions",
        value=(
            "**/missions** — See active missions and each team's mission wins\n"
            "**/submitmission** — Submit proof of completing an active mission"
        ),
        inline=False,
    )
    embed.add_field(
        name="More for everyone",
        value=(
            "**/firstblood** — Which team scored first in each region\n"
            "**/firstblooddetailed** — Same, but for every individual source\n"
            "**/recent** — The most recently approved drops across all teams\n"
            "**/mydrops** — Your own recent submissions and their status\n"
            "**/notifyme** — Get DM'd when your submission is reviewed\n"
            "**/timeleft** — Time remaining in the event\n"
            "**/help** — This list"
        ),
        inline=False,
    )
    embed.add_field(
        name="For fun",
        value=(
            "**/whatshouldido** — Random boss/source to go farm\n"
            "**/qotd** — Quote of the day\n"
            "**/fortunecookie** — Crack open a random fortune\n"
            "**/8ball <question>** — Ask the magic 8-ball\n"
            "**/luck** — Roll your luck for today\n"
            "**/excuse** — Generate an excuse for your dry streak\n"
            "**/copium** — A supportive (unhinged) pep talk\n"
            "**/roast [target]** — Lightly roast yourself or a teammate\n"
            "**/blessing** — A completely fake blessing\n"
            "**/gamble** — Cosmetic slot machine, no real stakes"
        ),
        inline=False,
    )
    embed.add_field(
        name="For mods",
        value=(
            "**/pending** — List every submission awaiting review, with jump links\n"
            "**/export** — Download the full submission history as a CSV file\n"
            "**/undoapproval** — Undo a recent approval (dropdown or search by name)\n"
            "**/newmission** — Announce a new mission to the mission channel\n"
            "**/cancelmission** — Cancel an active mission (e.g. for a test run)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Setup & config (mods)",
        value=(
            "**/setupwizard** — Interactive step-by-step setup (start here!)\n"
            "**/setreviewchannel** — Set the review queue channel\n"
            "**/setmodrole** — Set which role can approve/reject\n"
            "**/addteam** — Add or update a team's channel\n"
            "**/renameteam** — Rename a team (updates history too)\n"
            "**/removeteam** — Remove a team\n"
            "**/setteamsheet** — Attach a team's Google Sheet\n"
            "**/setstartingscreenshotchannel** — Set the shared fallback starting-screenshot channel\n"
            "**/setteamstartingscreenshotchannel** — Set one team's own starting-screenshot channel\n"
            "**/setmissionchannel** — Set the mission announcement channel\n"
            "**/setrules** — Set or update the competition rules\n"
            "**/listconfig** — See everything currently configured\n"
            "**/resetcompetition** — ⚠️ Wipe ALL submissions + config (auto-backs up first)"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="allregions", description="Show team standings broken down by every region")
async def allregions(interaction: discord.Interaction):
    team_region_points = compute_team_region_points()
    all_teams = sorted(t["team_name"] for t in database.get_all_teams())

    embed = discord.Embed(title="🗺️ All Regions", color=discord.Color.gold())

    closest = compute_closest_regions(team_region_points, all_teams, limit=3)
    if closest:
        embed.add_field(
            name="🔥 Closest Contests",
            value="\n".join(format_closest_line(e) for e in closest),
            inline=False,
        )

    for region in REGION_SOURCES:
        lines = build_region_standings_lines(region, team_region_points, all_teams)
        embed.add_field(name=region, value="\n".join(lines), inline=True)

    discord_file = None
    if all_teams:
        try:
            leaders = compute_region_leaders(team_region_points, all_teams)
            team_colors = map_generator.assign_team_colors(all_teams)
            buffer = map_generator.build_map_image(leaders, team_colors)
            discord_file = discord.File(buffer, filename="region_map.png")
            embed.set_image(url="attachment://region_map.png")
        except Exception:
            log.exception("Failed to generate map image for /allregions")

    if discord_file:
        await interaction.response.send_message(embed=embed, file=discord_file)
    else:
        await interaction.response.send_message(embed=embed)


def compute_region_leaders(team_region_points, all_teams) -> dict:
    """Returns {region: team_name_or_None} -- same 'single unambiguous
    leader, else neutral' logic as /scoreboard's Regions Conquered, used
    to color the /map image."""
    leaders = {}
    for region in REGION_SOURCES:
        scores = {team: team_region_points[team].get(region, 0) for team in all_teams}
        max_points = max(scores.values()) if scores else 0
        if max_points == 0:
            leaders[region] = None
            continue
        top_teams = [team for team, pts in scores.items() if pts == max_points]
        leaders[region] = top_teams[0] if len(top_teams) == 1 else None
    return leaders


class MapRegionSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=region) for region in REGION_SOURCES]
        super().__init__(placeholder="Pick a region for details...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        region = self.values[0]
        team_region_points = compute_team_region_points()
        all_teams = sorted(t["team_name"] for t in database.get_all_teams())
        lines = build_region_standings_lines(region, team_region_points, all_teams)

        embed = discord.Embed(title=f"🗺️ {region} Standings", color=discord.Color.gold())
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MapView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(MapRegionSelect())


@bot.tree.command(name="map", description="Show a map of which team controls each region")
async def map_command(interaction: discord.Interaction):
    team_region_points = compute_team_region_points()
    all_teams = sorted(t["team_name"] for t in database.get_all_teams())

    if not all_teams:
        await interaction.response.send_message(
            "No teams configured yet -- ask a mod to run /setupwizard or /addteam.", ephemeral=True
        )
        return

    leaders = compute_region_leaders(team_region_points, all_teams)
    team_colors = map_generator.assign_team_colors(all_teams)
    buffer = map_generator.build_map_image(leaders, team_colors)

    discord_file = discord.File(buffer, filename="region_map.png")
    await interaction.response.send_message(file=discord_file, view=MapView())


@bot.tree.command(name="closest", description="Show the regions with the tightest race between teams")
async def closest(interaction: discord.Interaction):
    team_region_points = compute_team_region_points()
    all_teams = sorted(t["team_name"] for t in database.get_all_teams())

    entries = compute_closest_regions(team_region_points, all_teams, limit=10)
    unclaimed = get_unclaimed_regions(team_region_points, all_teams)

    embed = discord.Embed(title="🔥 Closest Contests", color=discord.Color.orange())
    if not entries:
        embed.description = "No regions have any approved drops yet."
    else:
        embed.description = "\n".join(format_closest_line(e) for e in entries)

    if unclaimed:
        embed.add_field(
            name="🆕 Available (nobody has scored here yet)",
            value=", ".join(unclaimed),
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="firstblood", description="Show which team got the first approved drop in each region")
async def firstblood(interaction: discord.Interaction):
    first_per_region = database.get_first_approved_per_region()

    embed = discord.Embed(title="🩸 First Blood", color=discord.Color.red())
    lines = []
    for region in REGION_SOURCES:
        entry = first_per_region.get(region)
        if entry:
            lines.append(
                f"**{region}** — {entry['team_name']} "
                f"({entry['drop_name']}, {entry['rsn'] or 'Unknown'})"
            )
    embed.description = "\n".join(lines) if lines else "No approved drops yet."

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="firstblooddetailed",
    description="Show which team got the first approved drop from every individual source",
)
async def firstblooddetailed(interaction: discord.Interaction):
    first_per_source = database.get_first_approved_per_source()

    embed = discord.Embed(title="🩸 First Blood — Detailed", color=discord.Color.red())
    if not first_per_source:
        embed.description = "No approved drops yet."
        await interaction.response.send_message(embed=embed)
        return

    total_chars = 0
    truncated = False
    for region in REGION_SOURCES:
        if truncated:
            break
        lines = []
        for source in REGION_SOURCES[region]:
            entry = first_per_source.get((region, source))
            if entry:
                lines.append(f"**{source}**: {entry['team_name']} ({entry['drop_name']})")
        if not lines:
            continue
        value = "\n".join(lines)[:1024]
        # Discord caps total embed size around 6000 chars -- stop adding
        # fields before we'd risk hitting that, rather than letting the
        # send fail outright once there's enough data in a long-running event.
        if total_chars + len(value) > 5000:
            truncated = True
            break
        embed.add_field(name=region, value=value, inline=True)
        total_chars += len(value)

    if truncated:
        embed.set_footer(text="Some regions omitted to fit Discord's size limit -- try /firstblood for the summary view.")

    await interaction.response.send_message(embed=embed)


def _make_region_command(region_name: str):
    """Builds a standalone command callback for one region -- needed because
    each dynamically-registered slash command needs its own function object."""

    async def _region_command(interaction: discord.Interaction):
        team_region_points = compute_team_region_points()
        all_teams = sorted(t["team_name"] for t in database.get_all_teams())
        lines = build_region_standings_lines(region_name, team_region_points, all_teams)

        embed = discord.Embed(title=f"🗺️ {region_name} Standings", color=discord.Color.gold())
        embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed)

    _region_command.__name__ = f"region_{region_name.lower()}"
    return _region_command


# One slash command per region (e.g. /asgarnia, /kandarin, ...), generated
# from regions.py so this list stays in sync automatically if regions.py
# is ever regenerated from an updated sheet.
for _region_name in REGION_SOURCES:
    bot.tree.command(
        name=_region_name.lower(),
        description=f"Show team standings in {_region_name}",
    )(_make_region_command(_region_name))


@bot.tree.command(name="export", description="[Mods] Export the full submission history as a CSV file")
async def export(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_mod(interaction.user):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
        return

    submissions = database.get_all_submissions()

    buffer = io.StringIO()
    fieldnames = [
        "id", "team_name", "region", "boss_name", "drop_name", "rsn", "status",
        "submitter_name", "submitted_at", "reviewed_by", "reviewed_at", "notes",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in submissions:
        writer.writerow(row)

    buffer.seek(0)
    file_bytes = io.BytesIO(buffer.getvalue().encode("utf-8"))
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    discord_file = discord.File(file_bytes, filename=f"bingo_export_{timestamp}.csv")

    await interaction.response.send_message(
        f"Full submission history ({len(submissions)} rows):",
        file=discord_file,
        ephemeral=True,
    )


async def perform_undo_approval(submission: dict) -> list:
    """Given an approved submission dict, reverts it to pending, edits both
    Discord messages back to the pending look with buttons re-enabled, and
    decrements the sheet if configured. Returns a list of result strings
    describing what happened with each step, so the caller can report back
    exactly what did/didn't revert cleanly."""
    submission_id = submission["id"]
    database.revert_to_pending(submission_id)
    submission = dict(submission)
    submission["status"] = "pending"
    submission["reviewed_by"] = None
    submission["reviewed_at"] = None

    pending_embeds = build_submission_embeds(submission, status="pending")
    results = []

    # Revert the review-channel message and re-enable its buttons.
    review_channel_id = get_review_channel_id()
    review_channel = bot.get_channel(review_channel_id) if review_channel_id else None
    if review_channel and submission.get("message_id"):
        try:
            review_message = await review_channel.fetch_message(submission["message_id"])
            await review_message.edit(embeds=pending_embeds, view=SubmissionReviewView())
            results.append("✅ Review channel message reverted to pending")
        except discord.NotFound:
            results.append("⚠️ Review channel message no longer exists")
    else:
        results.append("⚠️ No review channel message on record")

    # Revert the team-channel message.
    team_channel_id = database.get_channel_by_team(submission["team_name"])
    team_channel = bot.get_channel(team_channel_id) if team_channel_id else None
    if team_channel and submission.get("team_message_id"):
        try:
            team_message = await team_channel.fetch_message(submission["team_message_id"])
            await team_message.edit(embeds=pending_embeds)
            results.append("✅ Team channel message reverted to pending")
        except discord.NotFound:
            results.append("⚠️ Team channel message no longer exists")
    else:
        results.append("⚠️ No team channel message on record")

    # Decrement the sheet, mirroring what approval incremented.
    if sheets_client.is_enabled():
        sheet_id = database.get_sheet_by_team(submission["team_name"])
        if sheet_id:
            try:
                found = sheets_client.decrement_drop_count(
                    sheet_id, submission["region"], submission["boss_name"], submission["drop_name"]
                )
                results.append("✅ Sheet decremented" if found else "⚠️ Couldn't find the matching sheet row")
            except Exception as e:
                log.exception("Failed to decrement sheet during undo")
                results.append(f"⚠️ Sheet decrement failed ({e})")
        else:
            results.append("⚠️ No sheet configured for this team")

    return results


class UndoApprovalSelect(discord.ui.Select):
    def __init__(self, submissions: list):
        options = []
        for s in submissions:
            label = s["drop_name"][:100]
            description = f"{s['team_name']} • {s['region']} • {s['rsn'] or 'Unknown'}"[:100]
            options.append(discord.SelectOption(label=label, description=description, value=str(s["id"])))
        super().__init__(placeholder="Choose an approval to undo...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        submission_id = int(self.values[0])
        submission = database.get_submission_by_id(submission_id)

        if submission is None or submission["status"] != "approved":
            await interaction.response.edit_message(
                content="That submission is no longer approved (maybe someone else already undid it). Nothing changed.",
                embed=None,
                view=None,
            )
            return

        # Acknowledge immediately (edits/sheet calls below can take longer
        # than Discord's 3-second interaction response window), then update
        # again with the final result once the work is done.
        await interaction.response.edit_message(content="Undoing...", embed=None, view=None)
        results = await perform_undo_approval(submission)
        await interaction.edit_original_response(
            content=(
                f"Undid the approval for **{submission['drop_name']}** "
                f"({submission['team_name']}). It's back in the pending queue.\n\n"
                + "\n".join(results)
            )
        )


class UndoApprovalView(discord.ui.View):
    def __init__(self, submissions: list):
        super().__init__(timeout=120)
        self.add_item(UndoApprovalSelect(submissions))


async def undo_submission_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[int]]:
    query = current.strip()
    matches = database.search_approved_submissions(query, limit=25) if query else database.get_recent_approved(limit=25)
    choices = []
    for s in matches:
        label = f"{s['drop_name']} — {s['team_name']} — {s['rsn'] or 'Unknown'}"[:100]
        choices.append(app_commands.Choice(name=label, value=s["id"]))
    return choices


@bot.tree.command(
    name="undoapproval",
    description="[Mods] Undo an approval -- pick from the 5 most recent, or search for a specific one",
)
@app_commands.describe(
    submission="Optional: search by drop/team/RSN name to find something older than the 5 most recent"
)
@app_commands.autocomplete(submission=undo_submission_autocomplete)
async def undoapproval(interaction: discord.Interaction, submission: Optional[int] = None):
    if not isinstance(interaction.user, discord.Member) or not is_mod(interaction.user):
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
        return

    if submission is not None:
        sub = database.get_submission_by_id(submission)
        if sub is None or sub["status"] != "approved":
            await interaction.response.send_message(
                "That submission isn't currently approved (already undone, rejected, or doesn't exist).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        results = await perform_undo_approval(sub)
        await interaction.followup.send(
            f"Undid the approval for **{sub['drop_name']}** ({sub['team_name']}). "
            f"It's back in the pending queue.\n\n" + "\n".join(results),
            ephemeral=True,
        )
        return

    recent = database.get_recent_approved(limit=5)
    if not recent:
        await interaction.response.send_message("There are no approved submissions to undo.", ephemeral=True)
        return

    await interaction.response.send_message(
        "Pick which approval to undo:", view=UndoApprovalView(recent), ephemeral=True
    )


# --- Stale pending submission nudge ---
# Every STALE_CHECK_MINUTES, pings the mod role for any submission that's
# been sitting pending for longer than STALE_MINUTES and hasn't been nudged
# before (nudged_at is set the moment it's flagged, so it only ever nudges
# once per submission, not repeatedly).
STALE_MINUTES = float(os.getenv("STALE_MINUTES", "30"))
STALE_CHECK_MINUTES = int(os.getenv("STALE_CHECK_MINUTES", "10"))


@tasks.loop(minutes=STALE_CHECK_MINUTES)
async def check_stale_submissions():
    stale = database.get_stale_pending(STALE_MINUTES)
    if not stale:
        return

    review_channel_id = get_review_channel_id()
    review_channel = bot.get_channel(review_channel_id) if review_channel_id else None
    if review_channel is None:
        return

    mod_role_name = get_mod_role_name()
    mod_mention = mod_role_name
    if review_channel.guild:
        role = discord.utils.get(review_channel.guild.roles, name=mod_role_name)
        if role:
            mod_mention = role.mention

    for submission in stale:
        jump_link = ""
        if submission.get("message_id") and submission.get("channel_id"):
            jump_link = (
                f" https://discord.com/channels/{review_channel.guild.id}/"
                f"{submission['channel_id']}/{submission['message_id']}"
            )
        else:
            jump_link = " (no message link on record -- check /pending or the database directly)"

        source_desc = submission["boss_name"]
        if submission.get("region"):
            source_desc += f", {submission['region']}"

        await review_channel.send(
            f"⏰ {mod_mention} — **{submission['drop_name']}** ({source_desc}) "
            f"from **{submission['team_name']}** has been pending "
            f"for over {STALE_MINUTES:.0f}m.{jump_link}"
        )
        database.mark_nudged(submission["id"])


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot.run(TOKEN)