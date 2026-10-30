import asyncio
import hmac
import io
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import discord
import aiohttp
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import welcome
from db import DB, iso, now
from tally import tally

load_dotenv()
log = logging.getLogger("refract")

GUILD_ID = int(os.environ["GUILD_ID"])
OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()}
MIN_AGE_DAYS = int(os.environ.get("MIN_AGE_DAYS", "30"))
CYCLE_DAYS = int(os.environ.get("CYCLE_DAYS", "90"))
MAX_CANDIDATES = 25  # select menu limit
API_TOKEN = os.environ.get("BOT_API_TOKEN", "")  # shared with the website Worker
API_PORT = int(os.environ.get("BOT_API_PORT", "8791"))

db = DB(os.environ.get("DB_PATH", "refract.db"))
db.default("admin_role_id", os.environ.get("ADMIN_ROLE_ID"))
db.default("member_role_id", os.environ.get("MEMBER_ROLE_ID"))
db.default("linked_role_id", os.environ.get("LINKED_ROLE_ID"))
db.default("announcements_channel_id", os.environ.get("ANNOUNCEMENTS_CHANNEL_ID"))
db.default("welcome_channel_id", os.environ.get("WELCOME_CHANNEL_ID"))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True  # public #general preview on the website
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
GUILD = discord.Object(GUILD_ID)


# ---------- helpers ----------

def role(guild, key):
    rid = db.get_int(key)
    return guild.get_role(rid) if rid else None


def channel(guild, key):
    cid = db.get_int(key)
    return guild.get_channel(cid) if cid else None


def is_owner(m):
    return m.id == m.guild.owner_id or m.id in OWNER_IDS


def is_staff(m):
    ids = {db.get_int("admin_role_id"), db.get_int("head_role_id")}
    return is_owner(m) or any(r.id in ids for r in m.roles)


def ineligible_reason(m):
    """None if member may apply, run or vote."""
    if m.bot:
        return "Bots cannot take part."
    if m.joined_at is None or now() - m.joined_at < timedelta(days=MIN_AGE_DAYS):
        return f"You need to have been in the server for {MIN_AGE_DAYS} days."
    linked = db.get_int("linked_role_id")
    if linked and not any(r.id == linked for r in m.roles):
        return "Link your Minecraft account first (DiscordSRV `/discord link` in game)."
    return None


def head_admin(guild):
    r = role(guild, "head_role_id")
    return r.members[0] if r and r.members else None


async def post(guild, key, **kwargs):
    ch = channel(guild, key)
    if ch is None:
        log.warning("channel %s not set; run /setup", key)
        return None
    return await ch.send(allowed_mentions=discord.AllowedMentions(roles=True, users=True), **kwargs)


def name_of(guild, uid):
    m = guild.get_member(uid)
    return m.display_name if m else f"<unknown {uid}>"


async def staff_only(interaction, owner=False):
    ok = is_owner(interaction.user) if owner else is_staff(interaction.user)
    if not ok:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
    return ok


# ---------- staff applications ----------

class ApplyModal(discord.ui.Modal, title="Staff application"):
    mc = discord.ui.TextInput(label="Minecraft username", max_length=16)
    why = discord.ui.TextInput(label="Why do you want to be staff?", style=discord.TextStyle.paragraph, max_length=1000)
    exp = discord.ui.TextInput(label="Moderation or admin experience", style=discord.TextStyle.paragraph,
                               max_length=1000, required=False)
    hours = discord.ui.TextInput(label="Hours per week you can be online", max_length=50)

    async def on_submit(self, interaction):
        if db.pending_application(interaction.user.id):
            return await interaction.response.send_message("You already have a pending application.", ephemeral=True)
        answers = {"Why": self.why.value, "Experience": self.exp.value or "None given", "Weekly hours": self.hours.value}
        app_id = db.add_application(interaction.user.id, self.mc.value, answers)
        embed = discord.Embed(title=f"Application #{app_id}: {interaction.user.display_name}")
        embed.add_field(name="Minecraft", value=self.mc.value, inline=False)
        for k, v in answers.items():
            embed.add_field(name=k, value=v[:1024], inline=False)
        embed.add_field(name="Joined server", value=discord.utils.format_dt(interaction.user.joined_at, "R"))
        msg = await post(interaction.guild, "applications_channel_id", embed=embed, view=DecisionView())
        if msg is None:
            return await interaction.response.send_message("Applications channel is not set up. Tell an admin.", ephemeral=True)
        db.set_application_message(app_id, msg.id)
        await msg.create_thread(name=f"app-{app_id}-{interaction.user.display_name}"[:90])
        await interaction.response.send_message("Application sent. Staff will reply here or by DM.", ephemeral=True)


def decided_render(message, applicant_id, accept, by):
    """Banner text and embed for a decided application message."""
    verdict = "ACCEPTED" if accept else "DENIED"
    icon = "✅" if accept else "❌"
    embed = message.embeds[0]
    title = re.sub(r"^(✅|❌) (ACCEPTED|DENIED) \| ", "", embed.title or "")
    embed.colour = discord.Colour.green() if accept else discord.Colour.red()
    embed.title = f"{icon} {verdict} | {title}"[:256]
    embed.set_footer(text=f"{verdict.capitalize()} by {by.display_name}")
    banner = f"## {icon} {verdict}\n<@{applicant_id}>'s application was {verdict.lower()} by {by.mention}."
    return banner, embed


class DecisionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def decide(self, interaction, accept):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        app = db.application_by_message(interaction.message.id)
        if app is None or app["status"] != "pending":
            return await interaction.response.send_message("Already decided or not found.", ephemeral=True)
        if app["user_id"] == interaction.user.id:
            return await interaction.response.send_message("You cannot decide your own application.", ephemeral=True)
        # role/DM/chat calls can outlast the 3s interaction window
        await interaction.response.defer()
        db.decide_application(app["id"], "accepted" if accept else "denied", interaction.user.id)
        applicant = interaction.guild.get_member(app["user_id"])
        if accept and applicant:
            staff = role(interaction.guild, "staff_role_id")
            if staff:
                await applicant.add_roles(staff, reason=f"Application #{app['id']} accepted")
        if applicant:
            try:
                await applicant.send(
                    "Your Refract SMP staff application was **accepted**. Welcome to the team! Head to #staff-chat to say hi."
                    if accept else "Your Refract SMP staff application was **denied**. You can apply again later with /apply.")
            except discord.Forbidden:
                pass
        if accept and applicant:
            chat = channel(interaction.guild, "staff_chat_channel_id") or discord.utils.find(
                lambda c: c.name.endswith("staff-chat"), interaction.guild.text_channels)
            if chat:
                await chat.send(f"Welcome to the team, {applicant.mention}! Your staff application was accepted.",
                                allowed_mentions=discord.AllowedMentions(users=True))
        banner, embed = decided_render(interaction.message, app["user_id"], accept, interaction.user)
        await interaction.edit_original_response(content=banner, embed=embed, view=None)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="app:accept")
    async def accept(self, interaction, button):
        await self.decide(interaction, True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="app:deny")
    async def deny(self, interaction, button):
        await self.decide(interaction, False)


@tree.context_menu(name="Fix application message", guild=GUILD)
async def fix_application_message(interaction, message: discord.Message):
    """Re-sync an application message with the database: banner, embed, and remove stale buttons."""
    if not await staff_only(interaction):
        return
    app = db.application_by_message(message.id)
    if app is None or message.author.id != bot.user.id or not message.embeds:
        return await interaction.response.send_message("Not an application message.", ephemeral=True)
    if app["status"] == "pending":
        await message.edit(view=DecisionView())
        return await interaction.response.send_message("Still pending; buttons restored.", ephemeral=True)
    by = interaction.guild.get_member(app["decided_by"]) or interaction.user
    banner, embed = decided_render(message, app["user_id"], app["status"] == "accepted", by)
    await message.edit(content=banner, embed=embed, view=None)
    await interaction.response.send_message(f"Fixed: {app['status']}.", ephemeral=True)


@tree.command(name="apply", description="Apply for staff", guild=GUILD)
async def apply(interaction: discord.Interaction):
    # No tenure or link gate: staff are recruited from outside the server.
    await interaction.response.send_modal(ApplyModal())


# ---------- elections ----------

class PitchModal(discord.ui.Modal, title="Run for Head Admin"):
    pitch = discord.ui.TextInput(label="Your pitch to the server", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, interaction):
        el = db.active_election()
        if el is None or el["status"] != "nominations":
            return await interaction.response.send_message("Nominations are not open.", ephemeral=True)
        if len(db.candidates(el["id"])) >= MAX_CANDIDATES and interaction.user.id not in dict(db.candidates(el["id"])):
            return await interaction.response.send_message("Candidate list is full.", ephemeral=True)
        db.add_candidate(el["id"], interaction.user.id, self.pitch.value)
        await post(interaction.guild, "elections_channel_id",
                   content=f"**{interaction.user.display_name}** is running for Head Admin.",
                   embed=discord.Embed(description=self.pitch.value))
        await interaction.response.send_message("You are on the ballot. Use /withdraw to leave.", ephemeral=True)


class NominationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Run for Head Admin", style=discord.ButtonStyle.primary, custom_id="el:run")
    async def run(self, interaction, button):
        why = ineligible_reason(interaction.user)
        if why:
            return await interaction.response.send_message(why, ephemeral=True)
        await interaction.response.send_modal(PitchModal())


class PickSelect(discord.ui.Select):
    def __init__(self, slot, label, options):
        super().__init__(placeholder=label, options=options, min_values=1, max_values=1)
        self.slot = slot

    async def callback(self, interaction):
        self.view.picks[self.slot] = int(self.values[0])
        await interaction.response.defer()


class BallotView(discord.ui.View):
    def __init__(self, eid, cands):
        super().__init__(timeout=300)
        self.eid = eid
        self.need = min(3, len(cands))
        self.picks = [None] * 3
        options = [discord.SelectOption(label=n[:100], value=str(uid)) for uid, n in cands]
        for i, label in enumerate(("1st choice (3 points)", "2nd choice (2 points)", "3rd choice (1 point)")[:self.need]):
            self.add_item(PickSelect(i, label, options))

    @discord.ui.button(label="Submit vote", style=discord.ButtonStyle.success, row=4)
    async def submit(self, interaction, button):
        picks = self.picks[:self.need]
        if None in picks or len(set(picks)) != len(picks):
            return await interaction.response.send_message("Fill every choice with a different person.", ephemeral=True)
        el = db.election(self.eid)
        if el["status"] != "voting":
            return await interaction.response.edit_message(content="Voting has closed.", view=None)
        if not db.cast_vote(self.eid, interaction.user.id, picks):
            return await interaction.response.edit_message(content="You already voted.", view=None)
        await interaction.response.edit_message(content="Vote recorded. It cannot be changed.", view=None)


class VoteLauncher(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cast your vote", style=discord.ButtonStyle.primary, custom_id="el:vote")
    async def vote(self, interaction, button):
        el = db.active_election()
        if el is None or el["status"] != "voting":
            return await interaction.response.send_message("Voting is not open.", ephemeral=True)
        why = ineligible_reason(interaction.user)
        if why:
            return await interaction.response.send_message(why, ephemeral=True)
        if db.has_voted(el["id"], interaction.user.id):
            return await interaction.response.send_message("You already voted.", ephemeral=True)
        cands = [(uid, name_of(interaction.guild, uid)) for uid, _ in db.candidates(el["id"]) if uid != interaction.user.id]
        if len(cands) < 1:
            return await interaction.response.send_message("Not enough candidates to vote on.", ephemeral=True)
        random.shuffle(cands)  # no ordering advantage
        await interaction.response.send_message(
            "Pick up to three different people. You cannot vote for yourself. Votes are anonymous and final.",
            view=BallotView(el["id"], cands), ephemeral=True)


election = app_commands.Group(name="election", description="Head Admin election")


async def open_nominations(guild, nom_days=14, vote_days=7, stays_admin=True):
    if db.active_election():
        return None
    vopen = now() + timedelta(days=nom_days)
    eid = db.add_election("nominations", vopen, vopen + timedelta(days=vote_days),
                          head_admin(guild).id if head_admin(guild) else None, stays_admin)
    member = role(guild, "member_role_id")
    msg = await post(guild, "elections_channel_id",
                     content=f"{member.mention if member else ''} Head Admin nominations are open.",
                     embed=discord.Embed(
                         title="Head Admin election",
                         description=f"Nominations close {discord.utils.format_dt(vopen, 'F')}. "
                                     f"Voting runs {vote_days} days after that. Ballots are 1st, 2nd and 3rd choice, anonymous. "
                                     f"Eligible: in the server {MIN_AGE_DAYS}+ days with a linked Minecraft account."),
                     view=NominationView())
    if msg:
        db.set_election(eid, post_message_id=msg.id)
    return eid


@election.command(name="start", description="Open nominations now (owner)")
@app_commands.describe(nomination_days="Days nominations stay open", voting_days="Days voting stays open",
                       outgoing_stays_admin="False demotes the outgoing Head Admin to player")
async def el_start(interaction: discord.Interaction, nomination_days: int = 14, voting_days: int = 7,
                   outgoing_stays_admin: bool = True):
    if not await staff_only(interaction, owner=True):
        return
    eid = await open_nominations(interaction.guild, nomination_days, voting_days, outgoing_stays_admin)
    await interaction.response.send_message(f"Election #{eid} opened." if eid else "An election is already running.",
                                            ephemeral=True)


@election.command(name="schedule", description="Auto-open nominations on a date, YYYY-MM-DD UTC (owner)")
async def el_schedule(interaction: discord.Interaction, date: str):
    if not await staff_only(interaction, owner=True):
        return
    try:
        when = datetime(int(date[:4]), int(date[5:7]), int(date[8:10]), tzinfo=timezone.utc)
    except ValueError:
        return await interaction.response.send_message("Use YYYY-MM-DD.", ephemeral=True)
    db.put("next_nominations", iso(when))
    await interaction.response.send_message(f"Nominations will open {discord.utils.format_dt(when, 'F')}.", ephemeral=True)


@election.command(name="sethead", description="Set the Head Admin by hand and announce it (owner)")
@app_commands.describe(member="The new Head Admin",
                       outgoing_since="YYYY-MM-DD the outgoing Head Admin started, if not already logged")
async def el_sethead(interaction: discord.Interaction, member: discord.Member, outgoing_since: str = ""):
    if not await staff_only(interaction, owner=True):
        return
    since = None
    if outgoing_since:
        try:
            since = iso(datetime(int(outgoing_since[:4]), int(outgoing_since[5:7]), int(outgoing_since[8:10]),
                                 tzinfo=timezone.utc))
        except ValueError:
            return await interaction.response.send_message("Use YYYY-MM-DD.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    outgoing = head_admin(interaction.guild)
    term, started = await swap_head(interaction.guild, outgoing, member, True, since)
    announced = await announce_head(interaction.guild, member, outgoing, term, started)
    await interaction.followup.send(
        f"{member.display_name} is Head Admin (term {term})."
        + ("" if announced else " No announcements channel found, nothing posted."), ephemeral=True)


@election.command(name="runoff", description="Open a 48 hour runoff between the tied candidates (owner)")
async def el_runoff(interaction: discord.Interaction):
    if not await staff_only(interaction, owner=True):
        return
    el = db.active_election()
    if el is None or el["status"] != "tied":
        return await interaction.response.send_message("No tied election.", ephemeral=True)
    tied = json.loads(el["restrict_to"])
    new = db.add_election("voting", now(), now() + timedelta(hours=48), el["outgoing_id"],
                          el["outgoing_stays_admin"], tied, el["id"])
    db.copy_candidates(el["id"], new, set(tied))
    db.set_election(el["id"], status="closed")
    await announce_voting(interaction.guild, new)
    await interaction.response.send_message("Runoff open.", ephemeral=True)


@election.command(name="status", description="Show the current election")
async def el_status(interaction: discord.Interaction):
    el = db.active_election()
    if el is None:
        return await interaction.response.send_message("No election running.", ephemeral=True)
    cands = ", ".join(name_of(interaction.guild, u) for u, _ in db.candidates(el["id"])) or "none yet"
    await interaction.response.send_message(f"Election #{el['id']} ({el['status']}). Candidates: {cands}.", ephemeral=True)


@tree.command(name="withdraw", description="Leave the Head Admin ballot", guild=GUILD)
async def withdraw(interaction: discord.Interaction):
    el = db.active_election()
    ok = el is not None and el["status"] == "nominations" and db.withdraw_candidate(el["id"], interaction.user.id)
    await interaction.response.send_message("Withdrawn." if ok else "Nothing to withdraw.", ephemeral=True)


async def announce_voting(guild, eid):
    el = db.election(eid)
    close = datetime.fromisoformat(el["voting_close"])
    lines = [f"**{name_of(guild, u)}**: {pitch[:300]}" for u, pitch in db.candidates(eid)]
    member = role(guild, "member_role_id")
    msg = await post(guild, "elections_channel_id",
                     content=f"{member.mention if member else ''} Voting is open until {discord.utils.format_dt(close, 'F')}.",
                     embed=discord.Embed(title="Candidates", description="\n\n".join(lines) or "None"),
                     view=VoteLauncher())
    if msg:
        db.set_election(eid, post_message_id=msg.id)


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


async def announce_head(guild, new, outgoing, term, started):
    ch = channel(guild, "announcements_channel_id") or discord.utils.get(guild.text_channels, name="announcements")
    if ch is None:
        return False
    embed = discord.Embed(title="New Head Admin", colour=discord.Colour.gold(), timestamp=now(),
                          description=f"{new.mention} is the new Head Admin of Refract SMP. Congratulations, and good luck.")
    embed.set_thumbnail(url=new.display_avatar.url)
    embed.add_field(name="Term", value=f"{ordinal(term)} term as Head Admin", inline=False)
    if outgoing and outgoing.id != new.id:
        text = f"Thank you {outgoing.mention} for your time as Head Admin. You stay on the admin team. We wish you all the best."
        if started:
            begun = datetime.fromisoformat(started)
            text += f"\nServed {discord.utils.format_dt(begun, 'D')} to {discord.utils.format_dt(now(), 'D')} ({(now() - begun).days} days)."
        embed.add_field(name="Outgoing Head Admin", value=text, inline=False)
    await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    return True


async def swap_head(guild, outgoing, winner, stays_admin, outgoing_since=None):
    """Swap roles, log the term change. Returns (winner's term number, outgoing's start or None)."""
    head, admin = role(guild, "head_role_id"), role(guild, "admin_role_id")
    if outgoing and outgoing.id != winner.id:
        await outgoing.remove_roles(head, reason="Term ended")
        if stays_admin and admin:
            await outgoing.add_roles(admin, reason="Term ended, stays Admin")
    if admin and admin in winner.roles:
        await winner.remove_roles(admin, reason="Now Head Admin")
    await winner.add_roles(head, reason="Won Head Admin election")
    return db.change_head(winner.id, outgoing.id if outgoing else None, outgoing_since)


async def finish_election(guild, el):
    rows, top = tally(db.ballots(el["id"]), [u for u, _ in db.candidates(el["id"])])
    total = len(db.ballots(el["id"]))
    lines = [f"{i + 1}. {name_of(guild, r['id'])}: {r['points']} points ({r['firsts']} first-place votes)"
             for i, r in enumerate(rows)]
    body = "\n".join(lines) or "No candidates."
    if len(top) == 1:
        winner = guild.get_member(top[0])
        outgoing = guild.get_member(el["outgoing_id"]) if el["outgoing_id"] else None
        await swap_head(guild, outgoing, winner, bool(el["outgoing_stays_admin"]))
        db.set_election(el["id"], status="closed")
        db.put("next_nominations", iso(now() + timedelta(days=max(CYCLE_DAYS - 21, 1))))
        await post(guild, "elections_channel_id", content=f"{winner.mention} is the new Head Admin.",
                   embed=discord.Embed(title=f"Results ({total} votes)", description=body))
    elif len(top) > 1:
        db.set_election(el["id"], status="tied", restrict_to=json.dumps(top))
        await post(guild, "elections_channel_id",
                   embed=discord.Embed(title=f"Tie after tie-breakers ({total} votes)", description=body +
                                       f"\n\nTied: {', '.join(name_of(guild, u) for u in top)}. The Owner will open a 48 hour runoff."))
    else:
        db.set_election(el["id"], status="closed")
        await post(guild, "elections_channel_id", content="Election closed with no votes. The Owner will decide how to proceed.")


@tasks.loop(minutes=1)
async def scheduler():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    try:
        el = db.active_election()
        t = now()
        if el is None:
            nxt = db.get("next_nominations")
            if nxt and t >= datetime.fromisoformat(nxt):
                db.put("next_nominations", "")
                await open_nominations(guild)
        elif el["status"] == "nominations" and t >= datetime.fromisoformat(el["voting_open"]):
            if len(db.candidates(el["id"])) < 2:
                db.set_election(el["id"], status="closed")
                await post(guild, "elections_channel_id",
                           content="Election cancelled: fewer than two candidates. The Owner will decide how to proceed.")
            else:
                db.set_election(el["id"], status="voting")
                await announce_voting(guild, el["id"])
        elif el["status"] == "voting" and t >= datetime.fromisoformat(el["voting_close"]):
            await finish_election(guild, el)
    except Exception:
        log.exception("scheduler tick failed")


# ---------- transparency log ----------

@tree.command(name="adminlog", description="Post an admin action to the public log", guild=GUILD)
@app_commands.describe(action="What was done", reason="Why", admin="Who did it, if not you (owner only)")
async def adminlog(interaction: discord.Interaction, action: str, reason: str, admin: discord.Member = None):
    staff = db.get_int("staff_role_id")
    if not is_staff(interaction.user) and not any(r.id == staff for r in interaction.user.roles):
        return await interaction.response.send_message("Not allowed.", ephemeral=True)
    if admin and admin != interaction.user and not is_owner(interaction.user):
        return await interaction.response.send_message("Only the owner can log for someone else.", ephemeral=True)
    who = admin or interaction.user
    embed = discord.Embed(title=action[:256], description=reason[:2000], timestamp=now())
    embed.set_author(name=who.display_name, icon_url=who.display_avatar.replace(size=128, format="png").url)
    await post(interaction.guild, "log_channel_id", embed=embed)
    await interaction.response.send_message("Logged.", ephemeral=True)


# ---------- mod log (owner-only) ----------

def command_line(data):
    """Rebuild '/name sub opt:value' from raw interaction data."""
    parts = [data["name"]]
    opts = data.get("options", [])
    while opts and opts[0].get("type") in (1, 2):  # subcommand or group
        parts.append(opts[0]["name"])
        opts = opts[0].get("options", [])
    parts += [f"{o['name']}:{o.get('value')}" for o in opts]
    return "/" + " ".join(parts)


# ---------- invite tracking ----------

# code -> (uses, max_uses, inviter_id). Discord has no "invite used" event, so each join is
# matched by diffing use counts against this snapshot.
invite_cache = {}
invite_lock = asyncio.Lock()


async def fetch_invites(guild):
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        log.warning("missing Manage Server permission; invite tracking off")
        return None
    return {i.code: (i.uses or 0, i.max_uses or 0, i.inviter.id if i.inviter else None) for i in invites}


async def match_invite(guild):
    """Return (code, inviter_id) of the invite the newest member used, or (None, None) if unclear."""
    async with invite_lock:
        after = await fetch_invites(guild)
        if after is None:
            return None, None
        before = dict(invite_cache)
        invite_cache.clear()
        invite_cache.update(after)
    used = [c for c, (uses, _, _) in after.items() if uses > before.get(c, (0, 0, None))[0]]
    # Single-use or capped invites vanish the moment they hit their limit.
    used += [c for c, (uses, cap, _) in before.items() if c not in after and cap and uses == cap - 1]
    if len(used) != 1:
        return None, None
    code = used[0]
    return code, (after.get(code) or before[code])[2]


@bot.event
async def on_invite_create(invite):
    if invite.guild and invite.guild.id == GUILD_ID:
        invite_cache[invite.code] = (invite.uses or 0, invite.max_uses or 0, invite.inviter.id if invite.inviter else None)


invites_group = app_commands.Group(name="invites", description="Invite tracking")


@invites_group.command(name="who", description="Who invited a member")
async def invites_who(interaction: discord.Interaction, member: discord.Member):
    if not await staff_only(interaction):
        return
    row = db.last_invite_join(member.id)
    if row is None:
        text = f"No invite recorded for {member.mention}."
    elif row["inviter_id"] is None:
        text = f"{member.mention} joined on {discord.utils.format_dt(datetime.fromisoformat(row['joined_at']), 'D')}; the invite could not be identified."
    else:
        text = f"{member.mention} was invited by <@{row['inviter_id']}> (code `{row['code']}`)."
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@invites_group.command(name="top", description="Members who brought in the most joins")
async def invites_top(interaction: discord.Interaction):
    if not await staff_only(interaction):
        return
    rows = db.top_inviters()
    text = "\n".join(f"{i}. <@{uid}>: {n}" for i, (uid, n) in enumerate(rows, 1)) or "Nothing recorded yet."
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


_welcome_tasks = set()


async def welcome_gif(member, kind="welcome"):
    avatar = await member.display_avatar.replace(size=512, format="png").read()
    return await welcome.make_gif(avatar, member.display_name, member.name, kind)


async def send_welcome(member, channel_key="welcome_channel_id", kind="welcome"):
    if kind == "welcome":
        content, text, colour = member.mention, f"Welcome to Refract SMP, {member.mention}!", 0xB57CFF  # mention in content so it pings
    else:
        safe = discord.utils.escape_mentions(discord.utils.escape_markdown(member.display_name))
        content, text, colour = None, f"{safe} left without a trace.", 0xFF5C7A
    try:
        gif = await welcome_gif(member, kind)
        embed = discord.Embed(description=text, colour=colour)
        embed.set_image(url=f"attachment://{kind}.gif")
        await post(member.guild, channel_key, content=content, embed=embed,
                   file=discord.File(io.BytesIO(gif), f"{kind}.gif"))
    except Exception:
        log.exception("%s gif failed for %s", kind, member.id)


def spawn_welcome(member, kind):
    task = asyncio.create_task(send_welcome(member, kind=kind))  # render takes seconds; keep the event handler free
    _welcome_tasks.add(task)
    task.add_done_callback(_welcome_tasks.discard)


@bot.event
async def on_member_join(member):
    if member.guild.id != GUILD_ID:
        return
    code, inviter_id = await match_invite(member.guild)
    db.drop_departure(member.id)
    if not member.bot:
        db.add_invite_join(member.id, inviter_id, code)
    embed = discord.Embed(title="Member joined", colour=discord.Colour.green(), timestamp=now(),
                          description=f"{member.mention} ({member})")
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
    embed.add_field(name="Invited by", value=f"<@{inviter_id}> (`{code}`)" if inviter_id else
                    (f"Unknown (`{code}`)" if code else "Unknown"))
    embed.set_footer(text=f"ID {member.id}")
    await post(member.guild, "modlog_channel_id", embed=embed)
    if not member.bot:
        spawn_welcome(member, "welcome")


@bot.event
async def on_member_remove(member):
    if member.guild.id != GUILD_ID:
        return
    embed = discord.Embed(title="Member left", colour=discord.Colour.red(), timestamp=now(),
                          description=f"{member.mention} ({member})")
    if member.joined_at:
        embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
    roles = [r.mention for r in member.roles if not r.is_default()]
    if roles:
        embed.add_field(name="Roles", value=" ".join(roles)[:1024], inline=False)
    embed.set_footer(text=f"ID {member.id}")
    await post(member.guild, "modlog_channel_id", embed=embed)
    if not member.bot:
        await record_departure(member)
        spawn_welcome(member, "leave")


async def record_departure(member):
    """Work out whether a removal was a ban, a kick or the member leaving, and store it for the members page."""
    kind, reason = "left", None
    try:
        await asyncio.sleep(2)  # let the audit log catch up
        try:
            ban = await member.guild.fetch_ban(discord.Object(id=member.id))
            kind, reason = "banned", ban.reason
        except discord.NotFound:
            async for e in member.guild.audit_logs(limit=10, action=discord.AuditLogAction.kick):
                if e.target and e.target.id == member.id and (now() - e.created_at).total_seconds() < 30:
                    kind, reason = "kicked", e.reason
                    break
    except discord.HTTPException:
        log.warning("could not read ban or audit log to classify departure of %s", member.id)
    db.add_departure(member.id, member.display_name, member.display_avatar.replace(size=128, format="png").url,
                     iso(member.joined_at) if member.joined_at else None, iso(now()), kind, reason)


@bot.event
async def on_member_unban(guild, user):
    if guild.id == GUILD_ID and db.has_departure(user.id):
        db.set_departure_kind(user.id, "unbanned")


async def backfill_bans(guild):
    """Add anyone on Discord's ban list who is not already recorded. Join time is unknown; the ban time comes from the audit log when it still has it."""
    when = {}
    try:
        async for e in guild.audit_logs(limit=None, action=discord.AuditLogAction.ban):
            if e.target and e.target.id not in when:
                when[e.target.id] = e.created_at
    except discord.HTTPException:
        log.warning("audit log unavailable for ban backfill")
    added = 0
    try:
        async for entry in guild.bans(limit=None):
            u = entry.user
            if u.bot or db.has_departure(u.id):
                continue
            db.add_departure(u.id, u.display_name, u.display_avatar.replace(size=128, format="png").url, None,
                             iso(when[u.id]) if u.id in when else None, "banned", entry.reason)
            added += 1
    except discord.HTTPException:
        log.warning("could not read the ban list")
    log.info("ban backfill added %s", added)


@bot.event
async def on_interaction(interaction):
    if interaction.type is not discord.InteractionType.application_command or interaction.guild is None:
        return
    if interaction.guild.id != GUILD_ID:
        return
    embed = discord.Embed(title="Command used", description=f"`{command_line(interaction.data)[:1000]}`",
                          timestamp=now())
    embed.set_author(name=f"{interaction.user} ({interaction.user.id})", icon_url=interaction.user.display_avatar.url)
    if interaction.channel:
        embed.add_field(name="Channel", value=f"<#{interaction.channel.id}>")
    await post(interaction.guild, "modlog_channel_id", embed=embed)


# ---------- website API (the Worker calls in; loopback only, exposed by a tunnel) ----------

_media_src = {}    # media key -> (discord url, kind); filled as the chat preview is built
_media_bytes = {}  # media key -> (body, content type); small in-memory copy so Discord is hit once
MEDIA_MAX = 8 * 1024 * 1024


def _reg(key, url, kind):
    _media_src[key] = (url, kind)
    return {"id": key, "kind": kind}


def chat_media(m):
    """Image/gif/video on a message as {"id", "kind"}, or None. The site loads it through /api/media/<id>, never from Discord.
    Rich embeds are sent separately by chat_embeds; this only covers attachments and link unfurls."""
    for n, a in enumerate(m.attachments):
        ctype = a.content_type or ""
        if ctype.startswith(("image/", "video/")):
            return _reg(f"{m.id}.a{n}", a.url, "video" if ctype.startswith("video/") else "image")
    for n, e in enumerate(m.embeds):
        if e.type == "rich":
            continue
        if e.video and e.video.proxy_url:  # gifv embeds (Klipy, Tenor, Giphy) animate as a muted mp4
            return _reg(f"{m.id}.u{n}", e.video.proxy_url, "video")
        img = e.image or e.thumbnail
        if img and img.proxy_url:
            return _reg(f"{m.id}.u{n}", img.proxy_url, "image")
    return None


def chat_more_media(m, first):
    """Attachments beyond the first one chat_media picked."""
    out = []
    for n, a in enumerate(m.attachments):
        ctype = a.content_type or ""
        if ctype.startswith(("image/", "video/")) and not (first and first["id"] == f"{m.id}.a{n}"):
            out.append(_reg(f"{m.id}.a{n}", a.url, "video" if ctype.startswith("video/") else "image"))
    return out[:8]


def chat_embeds(m):
    """Rich embeds (bots, webhooks) with every visible part: title, text, fields, author, footer, images."""
    out = []
    for n, e in enumerate(m.embeds):
        if e.type != "rich":
            continue
        img = e.image if e.image and e.image.proxy_url else None
        thumb = e.thumbnail if e.thumbnail and e.thumbnail.proxy_url else None
        out.append({
            "title": e.title, "url": e.url, "description": (e.description or "")[:400] or None,
            "color": f"#{e.colour.value:06x}" if e.colour else None,
            "author": {"name": e.author.name, "icon": e.author.icon_url} if e.author and e.author.name else None,
            "fields": [{"name": f.name[:100], "value": f.value[:200], "inline": f.inline} for f in e.fields[:10]],
            "image": _reg(f"{m.id}.e{n}i", img.proxy_url, "image") if img else None,
            "thumbnail": _reg(f"{m.id}.e{n}t", thumb.proxy_url, "image") if thumb else None,
            "footer": e.footer.text if e.footer and e.footer.text else None,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        })
    return out[:4]


async def api_media(request):
    mid = request.match_info["id"]
    if not re.fullmatch(r"\d+\.[aue]\d+[it]?", mid):
        return web.Response(status=404)
    if mid not in _media_bytes:
        src = _media_src.get(mid)
        if not src:
            return web.Response(status=404)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.get(src[0]) as r:
                if r.status != 200 or int(r.headers.get("Content-Length", 0)) > MEDIA_MAX:
                    return web.Response(status=502)
                body = b""
                async for chunk in r.content.iter_chunked(64 * 1024):  # content.read(n) returns a partial body
                    body += chunk
                    if len(body) > MEDIA_MAX:
                        break
                ctype = r.headers.get("Content-Type", "").split(";")[0]
        if len(body) > MEDIA_MAX or not ctype.startswith(("image/", "video/")):
            return web.Response(status=502)
        if len(_media_bytes) >= 80:
            _media_bytes.pop(next(iter(_media_bytes)))
        _media_bytes[mid] = (body, ctype)
    body, ctype = _media_bytes[mid]
    return web.Response(body=body, content_type=ctype, headers={"Cache-Control": "public, max-age=86400"})


async def build_status(guild):
    head_m = head_admin(guild)
    head = None
    if head_m:
        term = db.current_term()
        head = {"name": head_m.display_name,
                "avatar": head_m.display_avatar.replace(size=128, format="png").url,
                "term": db.term_count(head_m.id) or None,
                "since": term["started_at"] if term and term["user_id"] == head_m.id else None}
    election = None
    el = db.active_election()
    if el and el["status"] == "nominations":
        election = {"label": "Voting opens in", "target": el["voting_open"]}
    elif el and el["status"] == "voting":
        election = {"label": "Voting closes in", "target": el["voting_close"]}
    elif el:
        election = {"label": "Tie. Runoff pending", "target": None}
    elif db.get("next_nominations"):
        election = {"label": "Nominations open in", "target": db.get("next_nominations")}
    linked = role(guild, "linked_role_id")
    team = {m.id for key in ("head_role_id", "admin_role_id", "staff_role_id") if (r := role(guild, key)) for m in r.members}
    staff_list, listed = [], set()
    ranks = [("owner", [m for m in guild.members if is_owner(m)])] + [
        (tag, r.members if (r := role(guild, key)) else []) for tag, key in (("head", "head_role_id"), ("admin", "admin_role_id"), ("staff", "staff_role_id"))]
    for tag, members in ranks:
        for m in sorted(members, key=lambda x: x.display_name.lower()):
            if m.bot or m.id in listed:
                continue
            listed.add(m.id)
            staff_list.append({"name": m.display_name, "role": tag, "avatar": m.display_avatar.replace(size=128, format="png").url})
    entries = []
    log_ch = channel(guild, "log_channel_id")
    if log_ch:
        async for m in log_ch.history(limit=5):
            for e in m.embeds:
                entries.append({"action": e.title, "reason": e.description,
                                "by": e.author.name if e.author else None,
                                "avatar": e.author.icon_url if e.author else None, "when": m.created_at.isoformat()})
    general = []
    gch = discord.utils.find(lambda c: c.name.endswith("general"), guild.text_channels)
    if gch and gch.permissions_for(guild.default_role).view_channel:  # only ever publish a public channel
        ids = {k: db.get_int(k) for k in ("head_role_id", "admin_role_id", "staff_role_id")}
        async for m in gch.history(limit=60):
            text = m.clean_content.strip()
            media = chat_media(m)
            if media:  # the link was only there to unfurl into the media, so hide it
                text = re.sub(r"https?://\S+", "", text).strip()
            embeds = chat_embeds(m)
            if not (text or media or embeds):
                continue
            reply = None
            ref = m.reference.resolved if m.reference else None
            if isinstance(ref, discord.Message):  # a deleted original arrives as DeletedReferencedMessage and is skipped
                reply = {"who": ref.author.display_name,
                         "text": (ref.clean_content.strip() or ("image" if ref.attachments or ref.embeds else ""))[:80]}
            tag = next((n for n, k in (("head", "head_role_id"), ("admin", "admin_role_id"), ("staff", "staff_role_id"))
                        if ids[k] and any(r.id == ids[k] for r in getattr(m.author, "roles", []))), "")
            general.append({"who": m.author.display_name, "role": tag,
                            "avatar": m.author.display_avatar.replace(size=128, format="png").url, "text": text[:240],
                            "bot": m.author.bot, "media": media, "more_media": chat_more_media(m, media),
                            "embeds": embeds, "reply": reply, "when": m.created_at.isoformat()})
            if len(general) == 30:
                break
        general = general[::-1]
    return {"members": guild.member_count, "linked": len(linked.members) if linked else None, "team": len(team),
            "staff": staff_list, "head": head, "election": election, "log": entries[:5], "general": general}


async def handle_web_application(guild, item):
    uid = int(item["user_id"])
    if db.pending_application(uid):
        return "pending"
    member = guild.get_member(uid)
    name = member.display_name if member else item["username"]
    answers = {"Why": item["why"], "Experience": item["exp"] or "None given", "Weekly hours": item["hours"]}
    app_id = db.add_application(uid, item["mc"], answers)
    try:
        embed = discord.Embed(title=f"Application #{app_id}: {name}")
        embed.add_field(name="Minecraft", value=item["mc"], inline=False)
        for k, v in answers.items():
            embed.add_field(name=k, value=v[:1024], inline=False)
        embed.add_field(name="Joined server",
                        value=discord.utils.format_dt(member.joined_at, "R") if member and member.joined_at else "Not in the server yet")
        embed.set_footer(text="Sent from the website")
        msg = await post(guild, "applications_channel_id", embed=embed, view=DecisionView())
        if msg is None:
            raise RuntimeError("applications channel not set")
        db.set_application_message(app_id, msg.id)
        await msg.create_thread(name=f"app-{app_id}-{name}"[:90])
    except Exception:
        db.drop_application(app_id)
        raise
    return "ok"


_status_cache = {"at": 0.0, "body": None}
_members_cache = {"at": 0.0, "body": None}


def build_members(guild):
    head, admin, staff = (role(guild, k) for k in ("head_role_id", "admin_role_id", "staff_role_id"))
    members = []
    for m in guild.members:
        if m.bot:
            continue
        tag = "owner" if is_owner(m) else "head" if head and head in m.roles else "admin" if admin and admin in m.roles \
            else "staff" if staff and staff in m.roles else ""
        members.append({"name": m.display_name, "avatar": m.display_avatar.replace(size=64, format="png").url,
                        "joined": iso(m.joined_at) if m.joined_at else None, "role": tag})
    members.sort(key=lambda x: x["joined"] or "9")
    departed = [{"name": r["name"], "avatar": r["avatar"], "joined": r["joined_at"], "left": r["left_at"],
                 "kind": r["kind"], "reason": r["reason"]} for r in db.departures()]
    return {"members": members, "departed": departed, "generated": iso(now())}


@web.middleware
async def api_auth(request, handler):
    given = request.headers.get("Authorization", "").removeprefix("Bearer ")
    if not API_TOKEN or not hmac.compare_digest(given.encode(), API_TOKEN.encode()):
        return web.json_response({"error": "unauthorised"}, status=401)
    return await handler(request)


async def api_status(request):
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return web.json_response({"error": "not ready"}, status=503)
    if time.monotonic() - _status_cache["at"] > 20:  # spare Discord's rate limits when the site is busy
        body = await build_status(guild)
        body["generated"] = iso(now())
        _status_cache.update(at=time.monotonic(), body=body)
    return web.json_response(_status_cache["body"])


async def api_members(request):
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return web.json_response({"error": "not ready"}, status=503)
    if time.monotonic() - _members_cache["at"] > 60:
        _members_cache.update(at=time.monotonic(), body=build_members(guild))
    return web.json_response(_members_cache["body"])


async def api_application(request):
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return web.json_response({"error": "The bot is starting up. Try again in a minute."}, status=503)
    try:
        item = await request.json()
        item = {k: str(item.get(k, "")).strip() for k in ("user_id", "username", "mc", "why", "exp", "hours")}
        int(item["user_id"])
    except Exception:
        return web.json_response({"error": "Bad request."}, status=400)
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", item["mc"]) or not item["why"] or len(item["why"]) > 1000             or len(item["exp"]) > 1000 or not item["hours"] or len(item["hours"]) > 50:
        return web.json_response({"error": "Check the form and try again."}, status=400)
    try:
        result = await handle_web_application(guild, item)
    except Exception:
        log.exception("web application failed")
        return web.json_response({"error": "Could not post the application. Try again shortly."}, status=500)
    if result == "pending":
        return web.json_response({"error": "You already have an application waiting for review."}, status=409)
    return web.json_response({"ok": True})


async def api_welcome(request):
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return web.json_response({"error": "not ready"}, status=503)
    try:
        body = await request.json()
        user_id = int(body["user_id"])
        post_it = bool(body.get("post", False))
        kind = body.get("kind", "welcome")
        if kind not in ("welcome", "leave"):
            raise ValueError(kind)
    except Exception:
        return web.json_response({"error": "Bad request."}, status=400)
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.NotFound:
        return web.json_response({"error": "Not a member of this server."}, status=404)
    if post_it:
        await send_welcome(member, kind=kind)
        return web.json_response({"ok": True})
    try:
        gif = await welcome_gif(member, kind)
    except Exception:
        log.exception("welcome gif failed for %s", user_id)
        return web.json_response({"error": "Could not render the GIF."}, status=500)
    return web.Response(body=gif, content_type="image/gif")


async def start_api():
    app = web.Application(middlewares=[api_auth], client_max_size=16 * 1024)
    app.add_routes([web.get("/status", api_status), web.get("/members", api_members), web.post("/applications", api_application),
                    web.post("/welcome", api_welcome), web.get("/media/{id}", api_media)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", API_PORT).start()
    log.info("website API on 127.0.0.1:%s", API_PORT)


# ---------- self-update ----------

async def run(*args):
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


@tree.command(name="update", description="Pull the latest code and restart (owner)", guild=GUILD)
async def update(interaction: discord.Interaction):
    if not await staff_only(interaction, owner=True):
        return
    await interaction.response.defer()
    _, old = await run("git", "rev-parse", "HEAD")
    code, out = await run("git", "pull", "--ff-only")
    if code != 0:
        return await interaction.followup.send(f"git pull failed:\n```{out[-1500:]}```")
    _, new = await run("git", "rev-parse", "HEAD")
    if new == old:
        return await interaction.followup.send("Already up to date.")
    _, changed = await run("git", "diff", "--name-only", old, new)
    if "requirements.txt" in changed.split():
        code, out = await run(sys.executable, "-m", "pip", "install", "-r", "requirements.txt")
        if code != 0:
            await run("git", "reset", "--hard", old)
            return await interaction.followup.send(f"pip install failed, rolled back:\n```{out[-1500:]}```")
    code, out = await run(sys.executable, "-m", "py_compile", "bot.py", "db.py", "tally.py", "welcome.py")
    if code != 0:
        await run("git", "reset", "--hard", old)
        return await interaction.followup.send(f"New code does not compile, rolled back:\n```{out[-1500:]}```")
    _, summary = await run("git", "log", "--oneline", f"{old}..{new}")
    await interaction.followup.send(f"Updated to `{new[:7]}`, restarting.\n```{summary[-1500:]}```")
    await bot.close()
    os.execv(sys.executable, [sys.executable, *sys.argv])


# ---------- setup ----------

@tree.command(name="setup", description="Create Head Admin role and bot channels (owner)", guild=GUILD)
async def setup(interaction: discord.Interaction):
    if not await staff_only(interaction, owner=True):
        return
    await interaction.response.defer(ephemeral=True)
    g = interaction.guild
    admin = role(g, "admin_role_id")
    # swap_head strips Admin from the winner, so Head Admin must carry at least Admin's permissions.
    head = role(g, "head_role_id") or await g.create_role(
        name="Head Admin", colour=discord.Colour.gold(), hoist=True,
        permissions=admin.permissions if admin else discord.Permissions.none())
    db.put("head_role_id", head.id)
    staff = role(g, "staff_role_id") or await g.create_role(name="Staff", colour=discord.Colour.blue(), hoist=True)
    db.put("staff_role_id", staff.id)
    staff_ow = {g.default_role: discord.PermissionOverwrite(view_channel=False), head: discord.PermissionOverwrite(view_channel=True)}
    if admin:
        staff_ow[admin] = discord.PermissionOverwrite(view_channel=True)
    public_ro = {g.default_role: discord.PermissionOverwrite(send_messages=False, create_public_threads=False)}
    # Only the bot and OWNER_IDS can see it; the guild owner sees everything anyway.
    modlog_ow = {g.default_role: discord.PermissionOverwrite(view_channel=False),
                 g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
    for uid in OWNER_IDS:
        m = g.get_member(uid)
        if m:
            modlog_ow[m] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
    made = []
    for key, name, ow in (("applications_channel_id", "staff-applications", staff_ow),
                          ("elections_channel_id", "elections", public_ro),
                          ("log_channel_id", "admin-actions", public_ro),
                          ("modlog_channel_id", "mod-log", modlog_ow)):
        if channel(g, key) is None:
            ch = await g.create_text_channel(name, overwrites=ow)
            db.put(key, ch.id)
            made.append(ch.mention)
    await interaction.followup.send(f"Done. Roles: {head.mention} {staff.mention}. Created: {', '.join(made) or 'nothing new'}.", ephemeral=True)


@bot.event
async def setup_hook():
    bot.add_view(DecisionView())
    bot.add_view(NominationView())
    bot.add_view(VoteLauncher())
    tree.add_command(election, guild=GUILD)
    tree.add_command(invites_group, guild=GUILD)
    await tree.sync(guild=GUILD)
    scheduler.start()
    if API_TOKEN:
        await start_api()


@bot.event
async def on_ready():
    log.info("logged in as %s", bot.user)
    guild = bot.get_guild(GUILD_ID)
    if guild and (snapshot := await fetch_invites(guild)) is not None:
        invite_cache.clear()
        invite_cache.update(snapshot)
    if guild and not getattr(bot, "_backfilled", False):
        bot._backfilled = True
        await backfill_bans(guild)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bot.run(os.environ["DISCORD_TOKEN"])
