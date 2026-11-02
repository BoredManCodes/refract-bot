"""Moderation: cases and timed punishments, auto-moderation, join gate, raid mode, lockdown, split logging, sticky roles.

Pure rule logic is in automod.py. bot.py calls init() once, register() from setup_hook, and the on_* functions below
from the handlers it already owns (join, leave, unban, invite create); everything else is wired up by install().
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

import automod
from db import iso, now

log = logging.getLogger("refract.mod")

bot = db = None
GUILD_ID = 0
_is_owner = _is_staff = _is_support = None

tracker = automod.Tracker()
join_rate = automod.JoinRate()
_bot_acted = {}       # (kind, user id) -> monotonic time; keeps the audit listeners from recording our own actions twice
_auto_deleted = set()  # message ids AutoMod removed, so the deleted-message log stays quiet about them

LOG_CATEGORIES = {
    "messages": "Deleted and edited messages",
    "members": "Joins, leaves and nickname changes",
    "roles": "Role changes on members, role create and delete",
    "moderation": "Warns, mutes, kicks, bans and other cases",
    "invites": "Invites created and deleted",
    "voice": "Voice channel joins, leaves and moves",
    "channels": "Channel create and delete",
    "automod": "AutoMod hits and raid alerts",
    "commands": "Slash commands used",
}
QUIET = {"voice"}  # never falls back to #mod-log; off until given a channel

PAST = {"warn": "warned", "mute": "muted", "kick": "kicked", "ban": "banned", "unmute": "unmuted", "unban": "unbanned", "note": "noted"}
COLOURS = {"warn": discord.Colour.gold(), "mute": discord.Colour.orange(), "kick": discord.Colour.red(),
           "ban": discord.Colour.dark_red(), "unmute": discord.Colour.green(), "unban": discord.Colour.green(),
           "note": discord.Colour.light_grey()}
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "manage_messages",
             "kick_members", "ban_members", "moderate_members", "mention_everyone")
LOCK_PERMS = ("send_messages", "send_messages_in_threads", "create_public_threads", "create_private_threads", "add_reactions")


class ModError(Exception):
    """Something to tell the moderator, not a bug."""


def init(client, database, guild_id, is_owner, is_staff, is_support):
    global bot, db, GUILD_ID, _is_owner, _is_staff, _is_support
    bot, db, GUILD_ID = client, database, guild_id
    _is_owner, _is_staff, _is_support = is_owner, is_staff, is_support


# ---------- config and helpers ----------

def cfg():
    return automod.load(db.get("automod"))


def save(c):
    db.put("automod", automod.dump(c))


def clip(text, n=1000):
    text = str(text or "")
    return text if len(text) <= n else text[:n - 1] + "…"


def parse_when(value):
    return datetime.fromisoformat(value) if value else None


def role_of(guild, key):
    rid = db.get_int(key)
    return guild.get_role(rid) if rid else None


def protected_roles(guild):
    return [r for r in (role_of(guild, k) for k in ("admin_role_id", "head_role_id", "staff_role_id")) if r]


def who(u):
    return f"{u.mention} ({u} · {u.id})"


async def allow(interaction, level="mod"):
    m = interaction.user
    ok = _is_owner(m) if level == "owner" else _is_staff(m) if level == "admin" else _is_support(m)
    if not ok:
        await interaction.response.send_message("Not allowed.", ephemeral=True)
    return ok


async def fail(interaction, text):
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send(text, ephemeral=True)


async def log_to(guild, category, embed=None, content=None):
    cid = db.get_int(f"logch_{category}")
    if cid is None and category not in QUIET:
        cid = db.get_int("modlog_channel_id")
    ch = guild.get_channel(cid) if cid else None
    if ch is None:
        return None
    try:
        return await ch.send(content=content, embed=embed,
                             allowed_mentions=discord.AllowedMentions(roles=True, users=False))
    except discord.HTTPException:
        log.warning("could not write to the %s log channel", category)
        return None


async def audit_actor(guild, action, target_id, within=20):
    """The audit log entry for this action on this target, or None. Discord writes entries a moment late."""
    await asyncio.sleep(1.5)
    try:
        async for e in guild.audit_logs(limit=10, action=action):
            if e.target and e.target.id == target_id and (now() - e.created_at).total_seconds() <= within:
                return e
    except discord.HTTPException:
        pass
    return None


def acted(kind, user_id):
    _bot_acted[(kind, user_id)] = time.monotonic()
    for k in [k for k, t in _bot_acted.items() if time.monotonic() - t > 120]:
        del _bot_acted[k]


def bot_acted(kind, user_id, within=30):
    t = _bot_acted.get((kind, user_id))
    return t is not None and time.monotonic() - t < within


# ---------- cases ----------

def check_hierarchy(guild, moderator, member, auto):
    if member is None:
        return
    if member.id == guild.owner_id:
        raise ModError("The server owner cannot be moderated.")
    if member.id == bot.user.id:
        raise ModError("I will not do that to myself.")
    if not auto and moderator.id == member.id:
        raise ModError("You cannot do that to yourself.")
    if member.top_role >= guild.me.top_role:
        raise ModError("My highest role is not above theirs; move my role up.")
    if not auto and not _is_owner(moderator) and member.top_role >= moderator.top_role:
        raise ModError("Their highest role is not below yours.")


def case_embed(row, guild):
    kind = row["kind"]
    e = discord.Embed(title=f"Case #{row['id']} · {kind.title()}", colour=COLOURS.get(kind, discord.Colour.blurple()),
                      timestamp=parse_when(row["created_at"]), description=clip(row["reason"], 1500))
    e.add_field(name="Member", value=f"<@{row['user_id']}> (`{row['user_id']}`)")
    e.add_field(name="Moderator", value=f"<@{row['mod_id']}>" + (f" · AutoMod `{row['rule']}`" if row["auto"] and row["rule"] else ""))
    if row["expires_at"]:
        e.add_field(name="Expires", value=discord.utils.format_dt(parse_when(row["expires_at"]), "R"))
    if kind in ("warn", "mute", "ban") and not row["active"]:
        e.set_footer(text="No longer active")
    return e


async def dm_user(user, guild, kind, reason, seconds=None):
    e = discord.Embed(title=f"You were {PAST[kind]} in {guild.name}", colour=COLOURS[kind], description=clip(reason, 1500))
    if seconds:
        e.add_field(name="Duration", value=automod.fmt_duration(seconds))
    try:
        await user.send(embed=e)
    except discord.HTTPException:
        pass  # DMs closed


async def apply(guild, kind, user, moderator, reason, seconds=None, delete_seconds=0, auto=False, rule=None, dm=True):
    """Run one moderation action and record it. Returns (case id, extra text). Raises ModError."""
    reason = (reason or "").strip()[:500] or "No reason given"
    member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
    if kind in ("mute", "unmute", "kick") and member is None:
        raise ModError("That user is not in the server.")
    if kind in ("warn", "mute", "kick", "ban"):
        check_hierarchy(guild, moderator, member, auto)
    if kind == "mute":
        seconds = min(seconds or 3600, automod.MAX_TIMEOUT)
    audit = f"{moderator} ({moderator.id}): {reason}"[:512]
    expires = now() + timedelta(seconds=seconds) if seconds and kind in ("mute", "ban") else None
    acted(kind, user.id)
    if kind in ("kick", "ban") and dm:
        await dm_user(user, guild, kind, reason, seconds if kind == "ban" else None)
    try:
        if kind == "mute":
            await member.timeout(timedelta(seconds=seconds), reason=audit)
        elif kind == "unmute":
            await member.timeout(None, reason=audit)
        elif kind == "kick":
            await member.kick(reason=audit)
        elif kind == "ban":
            await guild.ban(user, reason=audit, delete_message_seconds=min(max(delete_seconds, 0), 604800))
        elif kind == "unban":
            await guild.unban(user, reason=audit)
    except discord.NotFound:
        raise ModError("That user is not banned." if kind == "unban" else "Could not find that user.")
    except discord.Forbidden:
        raise ModError("I do not have permission to do that.")
    except discord.HTTPException as exc:
        raise ModError(f"Discord refused: {exc.text}")
    if kind in ("mute", "ban"):
        db.end_infractions(user.id, kind)  # a new one replaces the running one
    if kind in ("unmute", "unban"):
        db.end_infractions(user.id, kind[2:])
    case = db.add_infraction(user.id, moderator.id, kind, reason, expires, auto, rule)
    if kind in ("warn", "mute") and dm and member is not None:
        await dm_user(member, guild, kind, reason, seconds if kind == "mute" else None)
    await log_to(guild, "moderation", case_embed(db.infraction(case), guild))
    extra = await escalate(guild, member) if kind == "warn" and member is not None else ""
    return case, extra


async def escalate(guild, member):
    c = cfg()
    count = db.active_warns(member.id, now() - timedelta(days=c["warn_window_days"]))
    step = automod.pick_escalation(c["escalation"], count)
    if not step:
        return ""
    action = step["action"]
    try:
        await apply(guild, action, member, guild.me, f"Reached {count} warnings", seconds=step.get("seconds") or None,
                    auto=True, rule="escalation", dm=c["dm"])
    except ModError as exc:
        return f" Reached {count} warnings but could not {action}: {exc}"
    span = f" for {automod.fmt_duration(step['seconds'])}" if action == "mute" and step.get("seconds") else ""
    return f" Reached {count} warnings, so they were {PAST[action]}{span}."


# ---------- AutoMod ----------

async def on_message(message):
    if message.guild is None or message.guild.id != GUILD_ID or message.author.bot:
        return
    if not isinstance(message.author, discord.Member) or not cfg()["enabled"] or _is_support(message.author):
        return
    m = automod.Msg(message.id, message.author.id, message.channel.id, frozenset(r.id for r in message.author.roles),
                    message.content, len(set(message.raw_mentions) | set(message.raw_role_mentions)),
                    len(message.attachments), time.time())
    hit = automod.evaluate(cfg(), m, tracker)
    if hit:
        await enforce(message, hit)


async def enforce(message, hit):
    guild, author, c = message.guild, message.author, cfg()
    by_channel = {}
    for ch_id, mid in hit.delete:
        by_channel.setdefault(ch_id, []).append(mid)
        _auto_deleted.add(mid)
    for ch_id, ids in by_channel.items():
        ch = guild.get_channel_or_thread(ch_id)
        if ch is None:
            continue
        try:
            if len(ids) > 1:
                await ch.delete_messages([discord.Object(i) for i in ids[:100]])
            else:
                await ch.get_partial_message(ids[0]).delete()
        except discord.HTTPException:
            pass
    tracker.forget(author.id)
    if len(_auto_deleted) > 5000:
        _auto_deleted.clear()
    outcome = "Deleted"
    if hit.action != "delete":
        seconds = hit.duration if hit.action == "mute" else None
        try:
            await apply(guild, hit.action, author, guild.me, f"AutoMod ({hit.rule}): {hit.reason}", seconds=seconds,
                        auto=True, rule=hit.rule, dm=c["dm"])
            outcome = f"Deleted and {PAST[hit.action]}"
        except ModError as exc:
            outcome = f"Deleted; could not {hit.action}: {exc}"
    if c["notice_seconds"] and hit.action not in ("kick", "ban"):
        try:
            await message.channel.send(f"{author.mention}, {hit.reason.lower()}.", delete_after=c["notice_seconds"],
                                       allowed_mentions=discord.AllowedMentions(users=[author]))
        except discord.HTTPException:
            pass
    e = discord.Embed(title=f"AutoMod: {hit.rule}", colour=discord.Colour.orange(), timestamp=now(),
                      description=clip(message.content, 900) or "(no text)")
    e.set_author(name=f"{author} ({author.id})", icon_url=author.display_avatar.url)
    e.add_field(name="Rule", value=hit.reason, inline=False)
    e.add_field(name="Channel", value=message.channel.mention)
    e.add_field(name="Outcome", value=outcome)
    await log_to(guild, "automod", e)


# ---------- joins, leaves, raid mode ----------

def raid_state():
    try:
        s = json.loads(db.get("raidmode") or "{}")
    except ValueError:
        s = {}
    until = parse_when(s.get("until"))
    if s.get("on") and until and until <= now():
        s = {}
        db.clear("raidmode")
    return s


def raid_on():
    return bool(raid_state().get("on"))


async def start_raid(guild, reason, action=None, minutes=None, lockdown=False, by=None):
    r = cfg()["raid"]
    action = action or r["action"]
    minutes = r["minutes"] if minutes is None else minutes
    db.put("raidmode", json.dumps({"on": True, "action": action, "reason": reason,
                                   "until": iso(now() + timedelta(minutes=minutes)) if minutes else None}))
    join_rate.reset()
    e = discord.Embed(title="Raid mode on", colour=discord.Colour.red(), timestamp=now(), description=reason)
    e.add_field(name="New members get", value=PAST[action])
    e.add_field(name="Ends", value=f"in {minutes} minutes" if minutes else "when staff turn it off")
    staff = role_of(guild, "staff_role_id") or role_of(guild, "admin_role_id")
    await log_to(guild, "automod", e, content=staff.mention if staff else None)
    if lockdown:
        await lockdown_server(guild, by or guild.me, f"Raid mode: {reason}", announce=True)


async def stop_raid(guild, by):
    if not raid_on():
        return False
    db.clear("raidmode")
    join_rate.reset()
    await log_to(guild, "automod", discord.Embed(title="Raid mode off", colour=discord.Colour.green(), timestamp=now(),
                                                 description=f"Turned off by {by.mention}." if by != guild.me else "Timed out."))
    return True


async def on_join(member):
    """Raid detection, raid mode, join gate, then sticky roles and mute persistence. True if the member was removed."""
    guild, c = member.guild, cfg()
    if member.bot:
        return False
    r = c["raid"]
    if r["enabled"] and join_rate.hit(time.time(), r["joins"], r["seconds"]) and not raid_on():
        await start_raid(guild, f"{r['joins']} or more members joined within {r['seconds']} seconds.", lockdown=r["lockdown"])
    state = raid_state()
    if state.get("on"):
        act = state.get("action", "kick")
        try:
            await apply(guild, act, member, guild.me, "Raid mode is on; the server is not taking new members right now.",
                        seconds=86400 if act == "mute" else None, auto=True, rule="raidmode")
            return act in ("kick", "ban")
        except ModError as exc:
            log.warning("raid mode could not %s %s: %s", act, member.id, exc)
    g = c["joingate"]
    if g["min_age_days"] or g["no_avatar"]:
        age = (now() - member.created_at).total_seconds() / 86400
        why = automod.gate_reason(g, age, member.avatar is not None)
        if why:
            act = g["action"]
            wait = max(60, int((g["min_age_days"] - age) * 86400)) if act == "mute" else None
            try:
                await apply(guild, act, member, guild.me, f"Join gate: {why}", seconds=wait, auto=True, rule="joingate")
                return act in ("kick", "ban")
            except ModError as exc:
                log.warning("join gate could not %s %s: %s", act, member.id, exc)
    await restore_member(member)
    return False


def sticky_ok(guild, role):
    if role.is_default() or role.managed or role >= guild.me.top_role or role in protected_roles(guild):
        return False
    if role.id in json.loads(db.get("sticky_exclude") or "[]"):
        return False
    return not any(getattr(role.permissions, p) for p in DANGEROUS)


async def restore_member(member):
    guild = member.guild
    if db.get("sticky") == "1":
        roles = [r for r in (guild.get_role(i) for i in db.take_sticky(member.id)) if r and sticky_ok(guild, r)]
        if roles:
            try:
                await member.add_roles(*roles, reason="Sticky roles", atomic=False)
            except discord.HTTPException:
                log.warning("could not restore roles for %s", member.id)
    mute = db.running_mute(member.id)
    if mute:
        left = parse_when(mute["expires_at"]) - now()
        try:
            await member.timeout(min(left, timedelta(seconds=automod.MAX_TIMEOUT)), reason="Mute carried over a rejoin")
        except discord.HTTPException:
            pass


def on_leave(member):
    if db.get("sticky") == "1" and not member.bot:
        db.save_sticky(member.id, [r.id for r in member.roles if sticky_ok(member.guild, r)])


async def on_unban(guild, user):
    if bot_acted("unban", user.id):
        return
    entry = await audit_actor(guild, discord.AuditLogAction.unban, user.id)
    db.end_infractions(user.id, "ban")
    case = db.add_infraction(user.id, entry.user.id if entry else 0, "unban", (entry and entry.reason) or "Unbanned in Discord")
    await log_to(guild, "moderation", case_embed(db.infraction(case), guild))


async def on_member_ban(guild, user):
    if guild.id != GUILD_ID or bot_acted("ban", user.id):
        return
    entry = await audit_actor(guild, discord.AuditLogAction.ban, user.id)
    db.end_infractions(user.id, "ban")
    case = db.add_infraction(user.id, entry.user.id if entry else 0, "ban", (entry and entry.reason) or "Banned in Discord")
    await log_to(guild, "moderation", case_embed(db.infraction(case), guild))


# ---------- lock and lockdown ----------

def lock_plan(channel):
    """[(role, deny)]: deny @everyone and any role that explicitly lets people talk; keep the staff roles talking."""
    g = channel.guild
    staff = protected_roles(g)
    plan = [(g.default_role, True)]
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Role) and target != g.default_role and not target.managed and target not in staff \
                and ow.send_messages is True:
            plan.append((target, True))
    return plan + [(r, False) for r in staff]


async def lock_channel(channel, scope, reason):
    for target, deny in lock_plan(channel):
        ow = channel.overwrites_for(target)
        db.save_lock(channel.id, target.id, scope, {p: getattr(ow, p) for p in LOCK_PERMS})
        for p in LOCK_PERMS if deny else ("send_messages", "send_messages_in_threads"):
            setattr(ow, p, False if deny else True)
        await channel.set_permissions(target, overwrite=ow, reason=reason)


async def unlock_channel(channel, rows, reason):
    for row in rows:
        target = channel.guild.get_role(row["target_id"])
        if target is None:
            continue
        ow = channel.overwrites_for(target)
        for p, v in row["prev"].items():
            setattr(ow, p, v)
        await channel.set_permissions(target, overwrite=None if ow.is_empty() else ow, reason=reason)


def lockdown_info():
    try:
        return json.loads(db.get("lockdown") or "null")
    except ValueError:
        return None


def lockdown_exempt():
    return set(json.loads(db.get("lockdown_exempt") or "[]"))


async def lockdown_server(guild, by, reason, announce=True):
    """Lock every public text channel. Returns (locked, failed)."""
    member_role = role_of(guild, "member_role_id")
    exempt = lockdown_exempt() | {db.get_int(k) for k in ("applications_channel_id", "tickets_log_channel_id", "modlog_channel_id")}
    locked = failed = 0
    notice = discord.Embed(title="Server locked down", colour=discord.Colour.red(), description=clip(reason, 500) or None)
    notice.set_footer(text="Staff will lift this as soon as it is safe.")
    for ch in guild.text_channels:
        if ch.id in exempt or db.locks("server", ch.id):
            continue
        sees = ch.permissions_for(guild.default_role).view_channel or (member_role and ch.permissions_for(member_role).view_channel)
        if not sees:
            continue
        try:
            await lock_channel(ch, "server", f"Lockdown by {by}: {reason}"[:512])
            locked += 1
            if announce:
                await ch.send(embed=notice)
        except discord.HTTPException:
            failed += 1
    db.put("lockdown", json.dumps({"by": by.id, "at": iso(now()), "reason": reason}))
    e = discord.Embed(title="Server lockdown", colour=discord.Colour.red(), timestamp=now(), description=clip(reason, 500) or None)
    e.add_field(name="By", value=by.mention)
    e.add_field(name="Channels locked", value=str(locked))
    await log_to(guild, "moderation", e)
    return locked, failed


async def lift_lockdown(guild, by, reason):
    rows = db.locks("server")
    done = 0
    for cid in sorted({r["channel_id"] for r in rows}):
        ch = guild.get_channel(cid)
        if ch is not None:
            try:
                await unlock_channel(ch, [r for r in rows if r["channel_id"] == cid], f"Lockdown lifted by {by}: {reason}"[:512])
                done += 1
            except discord.HTTPException:
                log.warning("could not unlock %s", cid)
    db.drop_locks("server")
    db.clear("lockdown")
    await log_to(guild, "moderation", discord.Embed(title="Lockdown lifted", colour=discord.Colour.green(), timestamp=now(),
                                                    description=f"By {by.mention}. {done} channels restored."))
    return done


# ---------- timers ----------

@tasks.loop(seconds=30)
async def timers():
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    for row in db.due_expiries():
        db.end_infraction(row["id"])
        if row["kind"] != "ban":
            continue  # Discord ends timeouts itself
        try:
            await guild.unban(discord.Object(id=row["user_id"]), reason=f"Temporary ban from case #{row['id']} ended")
            acted("unban", row["user_id"])
            case = db.add_infraction(row["user_id"], bot.user.id, "unban", f"Temporary ban (case #{row['id']}) ended", auto=True)
            await log_to(guild, "moderation", case_embed(db.infraction(case), guild))
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.warning("could not lift expired ban %s", row["id"])
    if db.get("raidmode") and not raid_state():  # raid_state clears it once it has timed out
        await log_to(guild, "automod", discord.Embed(title="Raid mode off", colour=discord.Colour.green(), timestamp=now(),
                                                     description="Timed out."))
    tracker.prune(time.time())


# ---------- logging events ----------

def user_embed(title, colour, user, description=None):
    e = discord.Embed(title=title, colour=colour, timestamp=now(), description=description)
    e.set_author(name=f"{user} ({user.id})", icon_url=user.display_avatar.url)
    return e


async def on_raw_message_delete(payload):
    if payload.guild_id != GUILD_ID or payload.message_id in _auto_deleted:
        _auto_deleted.discard(payload.message_id)
        return
    guild = bot.get_guild(GUILD_ID)
    m = payload.cached_message
    if m is not None and m.author.bot:
        return
    if m is None:
        e = discord.Embed(title="Message deleted", colour=discord.Colour.red(), timestamp=now(),
                          description="The message was not in the bot's cache, so its text is unknown.")
        e.add_field(name="Channel", value=f"<#{payload.channel_id}>")
    else:
        e = user_embed("Message deleted", discord.Colour.red(), m.author, clip(m.content) or "(no text)")
        e.add_field(name="Channel", value=m.channel.mention)
        if m.attachments:
            e.add_field(name="Files", value=clip("\n".join(a.filename for a in m.attachments), 1000), inline=False)
        entry = await audit_actor(guild, discord.AuditLogAction.message_delete, m.author.id, within=6)
        if entry and entry.user.id != m.author.id:
            e.add_field(name="Deleted by", value=entry.user.mention)
    e.set_footer(text=f"Message ID {payload.message_id}")
    await log_to(guild, "messages", e)


async def on_raw_bulk_message_delete(payload):
    if payload.guild_id != GUILD_ID:
        return
    ids = set(payload.message_ids) - _auto_deleted
    if not ids:
        return
    e = discord.Embed(title="Messages bulk deleted", colour=discord.Colour.red(), timestamp=now(),
                      description=f"{len(ids)} messages in <#{payload.channel_id}>")
    await log_to(bot.get_guild(GUILD_ID), "messages", e)


async def on_raw_message_edit(payload):
    if payload.guild_id != GUILD_ID:
        return
    before, after = payload.cached_message, payload.data.get("content")
    if before is None or after is None or before.author.bot or before.content == after:
        return
    e = user_embed("Message edited", discord.Colour.blue(), before.author)
    e.add_field(name="Before", value=clip(before.content) or "(empty)", inline=False)
    e.add_field(name="After", value=clip(after) or "(empty)", inline=False)
    e.add_field(name="Channel", value=f"{before.channel.mention} · [Jump]({before.jump_url})")
    await log_to(bot.get_guild(GUILD_ID), "messages", e)


async def on_member_update(before, after):
    if after.guild.id != GUILD_ID:
        return
    guild = after.guild
    added, removed = set(after.roles) - set(before.roles), set(before.roles) - set(after.roles)
    if added or removed:
        e = user_embed("Roles changed", discord.Colour.blurple(), after)
        if added:
            e.add_field(name="Added", value=" ".join(r.mention for r in added)[:1000])
        if removed:
            e.add_field(name="Removed", value=" ".join(r.mention for r in removed)[:1000])
        entry = await audit_actor(guild, discord.AuditLogAction.member_role_update, after.id, within=10)
        if entry:
            e.add_field(name="By", value=entry.user.mention, inline=False)
        await log_to(guild, "roles", e)
    if before.nick != after.nick:
        e = user_embed("Nickname changed", discord.Colour.blurple(), after)
        e.add_field(name="Before", value=before.nick or "(none)")
        e.add_field(name="After", value=after.nick or "(none)")
        await log_to(guild, "members", e)
    if after.timed_out_until and after.timed_out_until != before.timed_out_until and not bot_acted("mute", after.id):
        entry = await audit_actor(guild, discord.AuditLogAction.member_update, after.id, within=10)
        db.end_infractions(after.id, "mute")
        case = db.add_infraction(after.id, entry.user.id if entry else 0, "mute",
                                 (entry and entry.reason) or "Timed out in Discord", expires=after.timed_out_until)
        await log_to(guild, "moderation", case_embed(db.infraction(case), guild))


async def on_invite_create(invite):
    if invite.guild is None or invite.guild.id != GUILD_ID:
        return
    e = discord.Embed(title="Invite created", colour=discord.Colour.green(), timestamp=now(),
                      description=f"`{invite.code}` for {invite.channel.mention if invite.channel else 'unknown channel'}")
    if invite.inviter:
        e.add_field(name="By", value=invite.inviter.mention)
    e.add_field(name="Uses", value=str(invite.max_uses or "unlimited"))
    e.add_field(name="Expires", value=discord.utils.format_dt(invite.created_at + timedelta(seconds=invite.max_age), "R")
                if invite.max_age and invite.created_at else "never")
    await log_to(invite.guild, "invites", e)


async def on_invite_delete(invite):
    if invite.guild is None or invite.guild.id != GUILD_ID:
        return
    await log_to(invite.guild, "invites", discord.Embed(title="Invite deleted", colour=discord.Colour.red(), timestamp=now(),
                                                       description=f"`{invite.code}`"))


async def on_voice_state_update(member, before, after):
    if member.guild.id != GUILD_ID or before.channel == after.channel:
        return
    if before.channel is None:
        text, colour = f"joined {after.channel.mention}", discord.Colour.green()
    elif after.channel is None:
        text, colour = f"left {before.channel.mention}", discord.Colour.red()
    else:
        text, colour = f"moved from {before.channel.mention} to {after.channel.mention}", discord.Colour.blue()
    await log_to(member.guild, "voice", user_embed("Voice", colour, member, f"{member.mention} {text}"))


async def on_guild_channel_create(channel):
    if channel.guild.id == GUILD_ID:
        await log_to(channel.guild, "channels", discord.Embed(title="Channel created", colour=discord.Colour.green(),
                                                              timestamp=now(), description=f"{channel.mention} (`{channel.name}`)"))


async def on_guild_channel_delete(channel):
    if channel.guild.id == GUILD_ID:
        await log_to(channel.guild, "channels", discord.Embed(title="Channel deleted", colour=discord.Colour.red(),
                                                              timestamp=now(), description=f"`#{channel.name}`"))


async def on_guild_role_create(role):
    if role.guild.id == GUILD_ID:
        await log_to(role.guild, "roles", discord.Embed(title="Role created", colour=discord.Colour.green(),
                                                        timestamp=now(), description=f"{role.mention} (`{role.name}`)"))


async def on_guild_role_delete(role):
    if role.guild.id == GUILD_ID:
        await log_to(role.guild, "roles", discord.Embed(title="Role deleted", colour=discord.Colour.red(),
                                                        timestamp=now(), description=f"`{role.name}`"))


EVENTS = (on_message, on_raw_message_delete, on_raw_bulk_message_delete, on_raw_message_edit, on_member_update,
          on_invite_delete, on_voice_state_update, on_guild_channel_create, on_guild_channel_delete,
          on_guild_role_create, on_guild_role_delete, on_member_ban)


def install():
    """Attach the listeners bot.py does not already own and start the timers."""
    for fn in EVENTS:
        bot.event(fn)
    if not timers.is_running():
        timers.start()


# ---------- slash commands: cases ----------

def duration_arg(text, default=None):
    if not text:
        return default
    try:
        return automod.parse_duration(text)
    except ValueError as exc:
        raise ModError(str(exc))


async def run_case(interaction, kind, user, reason, seconds=None, delete_seconds=0, dm=True):
    await interaction.response.defer()
    try:
        case, extra = await apply(interaction.guild, kind, user, interaction.user, reason, seconds=seconds,
                                  delete_seconds=delete_seconds, dm=dm)
    except ModError as exc:
        return await interaction.followup.send(str(exc), ephemeral=True)
    span = f" for {automod.fmt_duration(min(seconds, automod.MAX_TIMEOUT))}" if kind == "mute" and seconds else \
        f" for {automod.fmt_duration(seconds)}" if kind == "ban" and seconds else ""
    await interaction.followup.send(f"{user.mention} was {PAST[kind]}{span}. Case #{case}.{extra}",
                                    allowed_mentions=discord.AllowedMentions.none())


@app_commands.command(name="warn", description="Warn a member")
@app_commands.describe(member="Who to warn", reason="Why")
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    if await allow(interaction):
        await run_case(interaction, "warn", member, reason)


@app_commands.command(name="mute", description="Time a member out")
@app_commands.describe(member="Who to mute", duration="How long: 30m, 2h, 7d (28 days at most)", reason="Why")
async def mute(interaction: discord.Interaction, member: discord.Member, duration: str = "1h", reason: str = ""):
    if not await allow(interaction):
        return
    try:
        seconds = duration_arg(duration)
    except ModError as exc:
        return await fail(interaction, str(exc))
    await run_case(interaction, "mute", member, reason, seconds=seconds)


@app_commands.command(name="unmute", description="End a member's timeout")
async def unmute(interaction: discord.Interaction, member: discord.Member, reason: str = ""):
    if await allow(interaction):
        await run_case(interaction, "unmute", member, reason)


@app_commands.command(name="kick", description="Kick a member")
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = ""):
    if await allow(interaction):
        await run_case(interaction, "kick", member, reason)


@app_commands.command(name="ban", description="Ban a user, for good or for a set time")
@app_commands.describe(user="Who to ban (can be someone who already left)", duration="Leave empty for permanent; 12h, 7d, 4w",
                       reason="Why", delete_days="Days of their messages to remove, 0 to 7")
async def ban(interaction: discord.Interaction, user: discord.User, duration: str = "", reason: str = "",
              delete_days: app_commands.Range[int, 0, 7] = 0):
    if not await allow(interaction):
        return
    try:
        seconds = duration_arg(duration)
    except ModError as exc:
        return await fail(interaction, str(exc))
    await run_case(interaction, "ban", user, reason, seconds=seconds, delete_seconds=delete_days * 86400)


@app_commands.command(name="unban", description="Unban a user")
@app_commands.describe(user_id="The user's ID")
async def unban(interaction: discord.Interaction, user_id: str, reason: str = ""):
    if not await allow(interaction):
        return
    try:
        user = await bot.fetch_user(int(user_id))
    except (ValueError, discord.NotFound):
        return await fail(interaction, "No user with that ID.")
    await run_case(interaction, "unban", user, reason)


@app_commands.command(name="note", description="Add a private note to a member's history")
async def note(interaction: discord.Interaction, user: discord.User, text: str):
    if not await allow(interaction):
        return
    case = db.add_infraction(user.id, interaction.user.id, "note", text[:500])
    await log_to(interaction.guild, "moderation", case_embed(db.infraction(case), interaction.guild))
    await interaction.response.send_message(f"Note added as case #{case}.", ephemeral=True)


@app_commands.command(name="history", description="A member's cases: warnings, mutes, kicks, bans, notes")
async def history(interaction: discord.Interaction, user: discord.User):
    if not await allow(interaction):
        return
    rows = db.infractions_for(user.id, 15)
    counts = db.infraction_counts(user.id)
    e = discord.Embed(title=f"History: {user}", colour=discord.Colour.blurple())
    e.set_thumbnail(url=user.display_avatar.url)
    e.description = " · ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in counts.items()) or "Clean record."
    for r in rows:
        flag = " (active)" if r["active"] and r["kind"] in ("warn", "mute", "ban") else ""
        auto = f" · AutoMod {r['rule']}" if r["auto"] and r["rule"] else ""
        e.add_field(name=f"#{r['id']} {r['kind']}{flag}{auto}",
                    value=f"{discord.utils.format_dt(parse_when(r['created_at']), 'R')} by <@{r['mod_id']}>\n{clip(r['reason'], 200)}",
                    inline=False)
    if sum(counts.values()) > len(rows):
        e.set_footer(text=f"Showing the latest {len(rows)} of {sum(counts.values())}")
    await interaction.response.send_message(embed=e, ephemeral=True)


case_group = app_commands.Group(name="case", description="Look up or edit a moderation case")


@case_group.command(name="view", description="Show one case")
async def case_view(interaction: discord.Interaction, number: int):
    if not await allow(interaction):
        return
    row = db.infraction(number)
    if row is None:
        return await fail(interaction, "No such case.")
    await interaction.response.send_message(embed=case_embed(row, interaction.guild), ephemeral=True)


@case_group.command(name="reason", description="Change a case's reason")
async def case_reason(interaction: discord.Interaction, number: int, reason: str):
    if not await allow(interaction):
        return
    if not db.set_infraction_reason(number, reason[:500]):
        return await fail(interaction, "No such case.")
    await interaction.response.send_message(f"Case #{number} updated.", ephemeral=True)


@case_group.command(name="delete", description="Remove a case from the record (admin)")
async def case_delete(interaction: discord.Interaction, number: int):
    if not await allow(interaction, "admin"):
        return
    row = db.infraction(number)
    if row is None:
        return await fail(interaction, "No such case.")
    db.delete_infraction(number)
    await log_to(interaction.guild, "moderation", discord.Embed(
        title=f"Case #{number} deleted", colour=discord.Colour.light_grey(), timestamp=now(),
        description=f"{row['kind']} of <@{row['user_id']}> removed by {interaction.user.mention}.\nReason was: {clip(row['reason'], 500)}"))
    await interaction.response.send_message(f"Case #{number} deleted.", ephemeral=True)


@app_commands.command(name="clearwarns", description="Stop a member's warnings counting towards auto-punishments")
async def clearwarns(interaction: discord.Interaction, member: discord.User):
    if not await allow(interaction):
        return
    n = db.clear_warns(member.id)
    await log_to(interaction.guild, "moderation", discord.Embed(
        title="Warnings cleared", colour=discord.Colour.green(), timestamp=now(),
        description=f"{n} warning(s) for {member.mention} cleared by {interaction.user.mention}."))
    await interaction.response.send_message(f"Cleared {n} active warning(s). They stay in the history.", ephemeral=True)


# ---------- slash commands: channel tools ----------

@app_commands.command(name="purge", description="Delete recent messages in this channel")
@app_commands.describe(amount="How many recent messages to look through, up to 500", member="Only this member's",
                       contains="Only messages containing this text", bots="Only bot messages",
                       attachments="Only messages with files", links="Only messages with links")
async def purge(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 500], member: discord.User = None,
                contains: str = "", bots: bool = False, attachments: bool = False, links: bool = False):
    if not await allow(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    needle = contains.lower()

    def check(m):
        return not m.pinned and (member is None or m.author.id == member.id) and (not bots or m.author.bot) \
            and (not needle or needle in m.content.lower()) and (not attachments or m.attachments) \
            and (not links or automod.domains(m.content))

    ids = []

    def tracked(m):
        ok = check(m)
        if ok:
            ids.append(m.id)
        return ok

    try:
        gone = await interaction.channel.purge(limit=amount, check=tracked, after=now() - timedelta(days=13, hours=23),
                                               oldest_first=False, bulk=True, reason=f"Purge by {interaction.user}")
    except discord.HTTPException as exc:
        return await interaction.followup.send(f"Discord refused: {exc.text}", ephemeral=True)
    _auto_deleted.update(ids)
    await log_to(interaction.guild, "moderation", discord.Embed(
        title="Purge", colour=discord.Colour.orange(), timestamp=now(),
        description=f"{interaction.user.mention} deleted {len(gone)} messages in {interaction.channel.mention}."))
    await interaction.followup.send(f"Deleted {len(gone)} messages (messages older than two weeks are skipped).", ephemeral=True)


@app_commands.command(name="slowmode", description="Set a channel's slowmode")
@app_commands.describe(seconds="0 turns it off; 21600 is the most", channel="Defaults to this channel")
async def slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600],
                   channel: discord.TextChannel = None):
    if not await allow(interaction):
        return
    channel = channel or interaction.channel
    await channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user}")
    await interaction.response.send_message(f"Slowmode in {channel.mention}: {seconds}s." if seconds else
                                            f"Slowmode off in {channel.mention}.")


@app_commands.command(name="lock", description="Stop members talking in a channel")
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None, reason: str = ""):
    if not await allow(interaction):
        return
    channel = channel or interaction.channel
    await interaction.response.defer()
    try:
        await lock_channel(channel, "channel", f"Locked by {interaction.user}: {reason}"[:512])
    except discord.HTTPException as exc:
        return await interaction.followup.send(f"Discord refused: {exc.text}", ephemeral=True)
    await channel.send(embed=discord.Embed(title="Channel locked", colour=discord.Colour.red(), description=clip(reason, 500) or None))
    await log_to(interaction.guild, "moderation", discord.Embed(
        title="Channel locked", colour=discord.Colour.red(), timestamp=now(),
        description=f"{channel.mention} by {interaction.user.mention}. {clip(reason, 300)}"))
    await interaction.followup.send(f"Locked {channel.mention}.", ephemeral=True)


@app_commands.command(name="unlock", description="Let members talk in a locked channel again")
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not await allow(interaction):
        return
    channel = channel or interaction.channel
    rows = db.locks(channel_id=channel.id)
    if not rows:
        return await fail(interaction, "That channel is not locked by the bot.")
    await interaction.response.defer()
    try:
        await unlock_channel(channel, rows, f"Unlocked by {interaction.user}")
    except discord.HTTPException as exc:
        return await interaction.followup.send(f"Discord refused: {exc.text}", ephemeral=True)
    db.drop_locks(channel_id=channel.id)
    await channel.send(embed=discord.Embed(title="Channel unlocked", colour=discord.Colour.green()))
    await log_to(interaction.guild, "moderation", discord.Embed(
        title="Channel unlocked", colour=discord.Colour.green(), timestamp=now(),
        description=f"{channel.mention} by {interaction.user.mention}."))
    await interaction.followup.send(f"Unlocked {channel.mention}.", ephemeral=True)


lockdown_group = app_commands.Group(name="lockdown", description="Lock every public channel at once")


@lockdown_group.command(name="start", description="Lock every public text channel (admin)")
@app_commands.describe(reason="Shown in each channel", announce="Post a notice in every locked channel")
async def lockdown_start(interaction: discord.Interaction, reason: str = "", announce: bool = True):
    if not await allow(interaction, "admin"):
        return
    await interaction.response.defer(ephemeral=True)
    locked, failed = await lockdown_server(interaction.guild, interaction.user, reason, announce)
    await interaction.followup.send(f"Locked {locked} channels." + (f" {failed} failed; check my Manage Channels permission." if failed else "")
                                    + " Lift it with `/lockdown end`.", ephemeral=True)


@lockdown_group.command(name="end", description="Restore every channel the lockdown locked (admin)")
async def lockdown_end(interaction: discord.Interaction, reason: str = ""):
    if not await allow(interaction, "admin"):
        return
    await interaction.response.defer(ephemeral=True)
    done = await lift_lockdown(interaction.guild, interaction.user, reason)
    await interaction.followup.send(f"Restored {done} channels.", ephemeral=True)


@lockdown_group.command(name="status", description="Is the server locked down")
async def lockdown_status(interaction: discord.Interaction):
    if not await allow(interaction):
        return
    info = lockdown_info()
    if not info:
        return await interaction.response.send_message("No lockdown running.", ephemeral=True)
    n = len({r["channel_id"] for r in db.locks("server")})
    await interaction.response.send_message(
        f"Locked down by <@{info['by']}> {discord.utils.format_dt(parse_when(info['at']), 'R')}: {n} channels. "
        f"Reason: {info.get('reason') or 'none given'}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@lockdown_group.command(name="exempt", description="Keep a channel out of lockdowns (admin)")
async def lockdown_exempt_cmd(interaction: discord.Interaction, channel: discord.TextChannel, remove: bool = False):
    if not await allow(interaction, "admin"):
        return
    ids = lockdown_exempt()
    ids.discard(channel.id) if remove else ids.add(channel.id)
    db.put("lockdown_exempt", json.dumps(sorted(ids)))
    await interaction.response.send_message(f"{channel.mention} {'is no longer' if remove else 'is now'} exempt from lockdowns.", ephemeral=True)


# ---------- slash commands: raid mode and join gate ----------

ACTION_CHOICES = [app_commands.Choice(name=a, value=a) for a in ("kick", "ban", "mute")]

raid_group = app_commands.Group(name="raidmode", description="Turn away new members during a raid")


@raid_group.command(name="on", description="Start raid mode: every new member gets the chosen action (admin)")
@app_commands.describe(action="What new members get", minutes="How long; 0 until turned off", lockdown="Also lock every channel")
@app_commands.choices(action=ACTION_CHOICES)
async def raid_start(interaction: discord.Interaction, action: app_commands.Choice[str] = None,
                     minutes: app_commands.Range[int, 0, 1440] = 30, lockdown: bool = False):
    if not await allow(interaction, "admin"):
        return
    await interaction.response.defer(ephemeral=True)
    await start_raid(interaction.guild, f"Turned on by {interaction.user.mention}.", action.value if action else None,
                     minutes, lockdown, interaction.user)
    await interaction.followup.send("Raid mode is on.", ephemeral=True)


@raid_group.command(name="off", description="Stop raid mode (admin)")
async def raid_stop(interaction: discord.Interaction):
    if not await allow(interaction, "admin"):
        return
    ok = await stop_raid(interaction.guild, interaction.user)
    await interaction.response.send_message("Raid mode is off." if ok else "Raid mode was not on.", ephemeral=True)


@raid_group.command(name="config", description="Set the automatic raid trigger (admin)")
@app_commands.describe(joins="This many joins...", seconds="...within this many seconds starts raid mode",
                       action="What new members get", minutes="How long raid mode lasts on its own",
                       lockdown="Also lock every channel when it triggers", enabled="Watch for raids at all")
@app_commands.choices(action=ACTION_CHOICES)
async def raid_config(interaction: discord.Interaction, joins: app_commands.Range[int, 2, 100] = None,
                      seconds: app_commands.Range[int, 2, 600] = None, action: app_commands.Choice[str] = None,
                      minutes: app_commands.Range[int, 1, 1440] = None, lockdown: bool = None, enabled: bool = None):
    if not await allow(interaction, "admin"):
        return
    c = cfg()
    for key, val in (("joins", joins), ("seconds", seconds), ("minutes", minutes), ("lockdown", lockdown), ("enabled", enabled),
                     ("action", action.value if action else None)):
        if val is not None:
            c["raid"][key] = val
    save(c)
    r = c["raid"]
    await interaction.response.send_message(
        f"Raid watch is {'on' if r['enabled'] else 'off'}: {r['joins']} joins in {r['seconds']}s starts raid mode for {r['minutes']} min; "
        f"new members get {PAST[r['action']]}{'; channels lock too' if r['lockdown'] else ''}.", ephemeral=True)


@raid_group.command(name="status", description="Is raid mode on")
async def raid_status(interaction: discord.Interaction):
    if not await allow(interaction):
        return
    s = raid_state()
    if not s.get("on"):
        return await interaction.response.send_message("Raid mode is off.", ephemeral=True)
    until = f", until {discord.utils.format_dt(parse_when(s['until']), 'R')}" if s.get("until") else ""
    await interaction.response.send_message(f"Raid mode is on: new members get {PAST[s['action']]}{until}.", ephemeral=True)


class ConfirmView(discord.ui.View):
    def __init__(self, user_id, run):
        super().__init__(timeout=60)
        self.user_id, self.run = user_id, run

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("Not yours.", ephemeral=True)
        self.stop()
        await interaction.response.edit_message(content="Working...", view=None)
        await self.run(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


@raid_group.command(name="sweep", description="Kick or ban recent joiners who have no roles (admin)")
@app_commands.describe(minutes="Everyone who joined this recently", action="kick or ban")
@app_commands.choices(action=ACTION_CHOICES[:2])
async def raid_sweep(interaction: discord.Interaction, minutes: app_commands.Range[int, 1, 180],
                     action: app_commands.Choice[str] = None):
    if not await allow(interaction, "admin"):
        return
    kind = action.value if action else "kick"
    cutoff = now() - timedelta(minutes=minutes)
    targets = [m for m in interaction.guild.members if not m.bot and m.joined_at and m.joined_at >= cutoff
               and len(m.roles) == 1 and not _is_support(m)]
    if not targets:
        return await interaction.response.send_message("Nobody without roles joined in that time.", ephemeral=True)

    async def go(i):
        done = 0
        for m in targets:
            try:
                await apply(i.guild, kind, m, i.user, f"Raid sweep: joined in the last {minutes} minutes", auto=False, dm=False)
                done += 1
            except ModError:
                pass
            await asyncio.sleep(0.4)
        await i.edit_original_response(content=f"{PAST[kind].title()} {done} of {len(targets)} members.")

    await interaction.response.send_message(f"This will {kind} {len(targets)} members who joined in the last {minutes} minutes "
                                            "and have no roles. Continue?", view=ConfirmView(interaction.user.id, go), ephemeral=True)


gate_group = app_commands.Group(name="joingate", description="Account age limit for new members")


@gate_group.command(name="set", description="Turn away accounts that are too new (admin)")
@app_commands.describe(min_account_age_days="Accounts younger than this fail; 0 turns the age check off",
                       action="kick, ban, or mute (timeout until the account is old enough)",
                       no_avatar="Also turn away accounts with no profile picture")
@app_commands.choices(action=ACTION_CHOICES)
async def gate_set(interaction: discord.Interaction, min_account_age_days: app_commands.Range[int, 0, 365] = None,
                   action: app_commands.Choice[str] = None, no_avatar: bool = None):
    if not await allow(interaction, "admin"):
        return
    c = cfg()
    if min_account_age_days is not None:
        c["joingate"]["min_age_days"] = min_account_age_days
    if action:
        c["joingate"]["action"] = action.value
    if no_avatar is not None:
        c["joingate"]["no_avatar"] = no_avatar
    save(c)
    await interaction.response.send_message(gate_text(c["joingate"]), ephemeral=True)


def gate_text(g):
    parts = []
    if g["min_age_days"]:
        parts.append(f"accounts under {g['min_age_days']} days old")
    if g["no_avatar"]:
        parts.append("accounts with no profile picture")
    return (f"Join gate: new members with {' or '.join(parts)} get {PAST[g['action']]}." if parts else "Join gate is off.")


@gate_group.command(name="status", description="Show the join gate")
async def gate_status(interaction: discord.Interaction):
    if await allow(interaction):
        await interaction.response.send_message(gate_text(cfg()["joingate"]), ephemeral=True)


# ---------- slash commands: AutoMod settings ----------

automod_group = app_commands.Group(name="automod", description="Auto-moderation settings")
RULE_CHOICES = [app_commands.Choice(name=r, value=r) for r in automod.RULES]
ACTION_ALL = [app_commands.Choice(name=a, value=a) for a in automod.ACTIONS]
LIST_KINDS = {"word": ("words", "list"), "blocked_link": ("links", "block"), "allowed_link": ("links", "allow"),
              "allowed_invite": ("invites", "allow")}
LIST_CHOICES = [app_commands.Choice(name=k, value=k) for k in LIST_KINDS]
LIMIT_UNITS = {"spam": "messages", "duplicates": "copies", "mentions": "mentions", "emojis": "emojis", "caps": "% capitals",
               "newlines": "lines", "attachments": "files", "zalgo": "marks"}


def rule_line(name, r):
    if not r["enabled"]:
        return f"`{name}`: off"
    bits = [r["action"] + (f" {automod.fmt_duration(r['duration'])}" if r["action"] == "mute" else "")]
    if "limit" in r:
        bits.append(f"over {r['limit']} {LIMIT_UNITS[name]}" + (f" in {r['seconds']}s" if "seconds" in r else ""))
    if name == "links":
        bits.append(f"{r['mode']} ({len(r['block' if r['mode'] == 'blocklist' else 'allow'])} listed)")
    if name == "words":
        bits.append(f"{len(r['list'])} listed")
    return f"`{name}`: " + ", ".join(bits)


@automod_group.command(name="status", description="Show every AutoMod rule")
async def automod_status(interaction: discord.Interaction):
    if not await allow(interaction):
        return
    c = cfg()
    e = discord.Embed(title=f"AutoMod is {'ON' if c['enabled'] else 'OFF'}", colour=discord.Colour.green() if c["enabled"] else discord.Colour.dark_grey())
    e.description = "\n".join(rule_line(n, c["rules"][n]) for n in automod.RULES)
    steps = ", ".join(f"{s['count']} warns: {s['action']}" + (f" {automod.fmt_duration(s['seconds'])}" if s["action"] == "mute" else "")
                      for s in sorted(c["escalation"], key=lambda s: s["count"])) or "none"
    e.add_field(name=f"Warning ladder (last {c['warn_window_days']} days)", value=steps, inline=False)
    if c["exempt_channels"] or c["exempt_roles"]:
        e.add_field(name="Exempt", value=" ".join([f"<#{i}>" for i in c["exempt_channels"]] + [f"<@&{i}>" for i in c["exempt_roles"]])[:1000], inline=False)
    e.set_footer(text="Staff are never checked. Change rules with /automod rule.")
    await interaction.response.send_message(embed=e, ephemeral=True)


@automod_group.command(name="toggle", description="Turn all of AutoMod on or off (admin)")
async def automod_toggle(interaction: discord.Interaction, enabled: bool):
    if not await allow(interaction, "admin"):
        return
    c = cfg()
    c["enabled"] = enabled
    save(c)
    await interaction.response.send_message(f"AutoMod is {'on' if enabled else 'off'}.", ephemeral=True)


@automod_group.command(name="rule", description="Change one AutoMod rule (admin)")
@app_commands.describe(rule="Which rule", enabled="On or off", action="What happens on a hit (the message is always deleted)",
                       duration="How long a mute lasts: 10m, 2h", limit="Threshold (see /automod status for the unit)",
                       seconds="Time window for spam, duplicates and attachments", mode="Links: block only listed domains, or allow only listed")
@app_commands.choices(rule=RULE_CHOICES, action=ACTION_ALL,
                      mode=[app_commands.Choice(name="blocklist", value="blocklist"), app_commands.Choice(name="allowlist", value="allowlist")])
async def automod_rule(interaction: discord.Interaction, rule: app_commands.Choice[str], enabled: bool = None,
                       action: app_commands.Choice[str] = None, duration: str = "", limit: app_commands.Range[int, 1, 1000] = None,
                       seconds: app_commands.Range[int, 1, 600] = None, mode: app_commands.Choice[str] = None):
    if not await allow(interaction, "admin"):
        return
    c = cfg()
    r = c["rules"][rule.value]
    try:
        if duration:
            r["duration"] = min(automod.parse_duration(duration), automod.MAX_TIMEOUT)
    except ValueError as exc:
        return await fail(interaction, str(exc))
    for key, val in (("limit", limit), ("seconds", seconds)):
        if val is not None:
            if key not in r:
                return await fail(interaction, f"The {rule.value} rule has no {key}.")
            r[key] = val
    if mode:
        if rule.value != "links":
            return await fail(interaction, "Only the links rule has a mode.")
        r["mode"] = mode.value
    if enabled is not None:
        r["enabled"] = enabled
    if action:
        r["action"] = action.value
    save(c)
    await interaction.response.send_message(rule_line(rule.value, r) + ("\nAutoMod itself is off; turn it on with `/automod toggle`." if not c["enabled"] else ""),
                                            ephemeral=True)


@automod_group.command(name="add", description="Add to a word or link list (admin)")
@app_commands.describe(kind="Which list", value="Word (use * as a wildcard, ~word to match inside other words), domain, or invite code")
@app_commands.choices(kind=LIST_CHOICES)
async def automod_add(interaction: discord.Interaction, kind: app_commands.Choice[str], value: str):
    if not await allow(interaction, "admin"):
        return
    c = cfg()
    rule, key = LIST_KINDS[kind.value]
    value = value.strip().lower() if kind.value != "allowed_invite" else value.strip()
    if kind.value.endswith("link"):
        hosts = automod.domains(value) or [value.removeprefix("www.").strip("/")]
        value = hosts[0]
    if not value:
        return await fail(interaction, "Nothing to add.")
    lst = c["rules"][rule][key]
    if value not in lst:
        lst.append(value)
    save(c)
    await interaction.response.send_message(f"Added to {kind.value} list ({len(lst)} entries).", ephemeral=True)


@automod_group.command(name="remove", description="Remove from a word or link list (admin)")
@app_commands.choices(kind=LIST_CHOICES)
async def automod_remove(interaction: discord.Interaction, kind: app_commands.Choice[str], value: str):
    if not await allow(interaction, "admin"):
        return
    c = cfg()
    rule, key = LIST_KINDS[kind.value]
    lst = c["rules"][rule][key]
    match = next((x for x in lst if x.lower() == value.strip().lower()), None)
    if match is None:
        return await fail(interaction, "That is not on the list.")
    lst.remove(match)
    save(c)
    await interaction.response.send_message(f"Removed. {len(lst)} entries left.", ephemeral=True)


@automod_group.command(name="list", description="Show a word or link list (admin)")
@app_commands.choices(kind=LIST_CHOICES)
async def automod_list(interaction: discord.Interaction, kind: app_commands.Choice[str]):
    if not await allow(interaction, "admin"):
        return
    rule, key = LIST_KINDS[kind.value]
    lst = cfg()["rules"][rule][key]
    await interaction.response.send_message(clip("\n".join(f"||{x}||" if kind.value == "word" else x for x in lst), 1900) or "Empty.",
                                            ephemeral=True)


EXEMPT_RULES = [app_commands.Choice(name="all rules", value="all")] + RULE_CHOICES


def exempt_edit(c, rule, key, ident, remove):
    lst = c[f"exempt_{key}"] if rule == "all" else c["rules"][rule].setdefault(key, [])
    if remove and ident in lst:
        lst.remove(ident)
    elif not remove and ident not in lst:
        lst.append(ident)


@automod_group.command(name="exempt_channel", description="Skip a channel for one rule or all (admin)")
@app_commands.choices(rule=EXEMPT_RULES)
async def automod_exempt_channel(interaction: discord.Interaction, channel: discord.TextChannel,
                                 rule: app_commands.Choice[str] = None, remove: bool = False):
    if not await allow(interaction, "admin"):
        return
    c, name = cfg(), rule.value if rule else "all"
    exempt_edit(c, name, "channels", channel.id, remove)
    save(c)
    await interaction.response.send_message(f"{channel.mention} {'is checked again' if remove else 'is skipped'} for {name}.", ephemeral=True)


@automod_group.command(name="exempt_role", description="Skip a role for one rule or all (admin)")
@app_commands.choices(rule=EXEMPT_RULES)
async def automod_exempt_role(interaction: discord.Interaction, role: discord.Role,
                              rule: app_commands.Choice[str] = None, remove: bool = False):
    if not await allow(interaction, "admin"):
        return
    c, name = cfg(), rule.value if rule else "all"
    exempt_edit(c, name, "roles", role.id, remove)
    save(c)
    await interaction.response.send_message(f"{role.mention} {'is checked again' if remove else 'is skipped'} for {name}.",
                                            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@automod_group.command(name="warn_ladder", description="Set what happens at a number of warnings (admin)")
@app_commands.describe(warnings="Fires when a member reaches exactly this many active warnings", action="What happens",
                       duration="For mute: 1h, 1d", remove="Delete this step instead")
@app_commands.choices(action=[app_commands.Choice(name=a, value=a) for a in ("mute", "kick", "ban")])
async def automod_ladder(interaction: discord.Interaction, warnings: app_commands.Range[int, 1, 50],
                         action: app_commands.Choice[str] = None, duration: str = "1h", remove: bool = False):
    if not await allow(interaction, "admin"):
        return
    c = cfg()
    c["escalation"] = [s for s in c["escalation"] if s["count"] != warnings]
    if not remove:
        if not action:
            return await fail(interaction, "Pick an action.")
        try:
            secs = min(automod.parse_duration(duration), automod.MAX_TIMEOUT) if action.value == "mute" else 0
        except ValueError as exc:
            return await fail(interaction, str(exc))
        c["escalation"].append({"count": warnings, "action": action.value, "seconds": secs})
    save(c)
    await interaction.response.send_message("Saved. See it in `/automod status`.", ephemeral=True)


@automod_group.command(name="options", description="Notices, DMs and the warning window (admin)")
@app_commands.describe(dm="DM members when AutoMod punishes them", notice_seconds="Seconds the in-channel notice stays; 0 for none",
                       warn_window_days="Warnings older than this stop counting towards the ladder")
async def automod_options(interaction: discord.Interaction, dm: bool = None, notice_seconds: app_commands.Range[int, 0, 60] = None,
                          warn_window_days: app_commands.Range[int, 1, 365] = None):
    if not await allow(interaction, "admin"):
        return
    c = cfg()
    for key, val in (("dm", dm), ("notice_seconds", notice_seconds), ("warn_window_days", warn_window_days)):
        if val is not None:
            c[key] = val
    save(c)
    await interaction.response.send_message(f"DMs {'on' if c['dm'] else 'off'}, notice {c['notice_seconds']}s, warnings count for {c['warn_window_days']} days.", ephemeral=True)


# ---------- slash commands: logs and sticky roles ----------

logs_group = app_commands.Group(name="logs", description="Where each kind of log goes")
LOG_CHOICES = [app_commands.Choice(name=k, value=k) for k in LOG_CATEGORIES]


@logs_group.command(name="set", description="Send one kind of log to a channel (admin)")
@app_commands.choices(category=LOG_CHOICES)
async def logs_set(interaction: discord.Interaction, category: app_commands.Choice[str], channel: discord.TextChannel):
    if not await allow(interaction, "admin"):
        return
    db.put(f"logch_{category.value}", channel.id)
    await interaction.response.send_message(f"{category.value} logs go to {channel.mention}.", ephemeral=True)


@logs_group.command(name="reset", description="Send one kind of log back to #mod-log (admin)")
@app_commands.choices(category=LOG_CHOICES)
async def logs_reset(interaction: discord.Interaction, category: app_commands.Choice[str]):
    if not await allow(interaction, "admin"):
        return
    db.clear(f"logch_{category.value}")
    await interaction.response.send_message(f"{category.value} logs follow #mod-log again." if category.value not in QUIET
                                            else f"{category.value} logs are off.", ephemeral=True)


@logs_group.command(name="status", description="Show where each log goes")
async def logs_status(interaction: discord.Interaction):
    if not await allow(interaction):
        return
    lines = []
    for k, text in LOG_CATEGORIES.items():
        cid = db.get_int(f"logch_{k}")
        where = f"<#{cid}>" if cid else "off" if k in QUIET else "#mod-log (default)"
        lines.append(f"`{k}` → {where}: {text}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


sticky_group = app_commands.Group(name="sticky", description="Give members their roles back when they rejoin")


@sticky_group.command(name="set", description="Turn sticky roles on or off (admin)")
async def sticky_set(interaction: discord.Interaction, enabled: bool):
    if not await allow(interaction, "admin"):
        return
    db.put("sticky", "1" if enabled else "0")
    await interaction.response.send_message(
        f"Sticky roles are {'on' if enabled else 'off'}. Admin, Head Admin, Staff, bot-managed roles and roles with moderation or "
        "management permissions are never restored.", ephemeral=True)


@sticky_group.command(name="exclude", description="Never restore a role (admin)")
async def sticky_exclude(interaction: discord.Interaction, role: discord.Role, remove: bool = False):
    if not await allow(interaction, "admin"):
        return
    ids = set(json.loads(db.get("sticky_exclude") or "[]"))
    ids.discard(role.id) if remove else ids.add(role.id)
    db.put("sticky_exclude", json.dumps(sorted(ids)))
    await interaction.response.send_message(f"{role.mention} {'can be' if remove else 'will not be'} restored.", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


COMMANDS = (warn, mute, unmute, kick, ban, unban, note, history, clearwarns, purge, slowmode, lock, unlock,
            case_group, lockdown_group, raid_group, gate_group, automod_group, logs_group, sticky_group)


def register(tree, guild):
    for cmd in COMMANDS:
        tree.add_command(cmd, guild=guild)
