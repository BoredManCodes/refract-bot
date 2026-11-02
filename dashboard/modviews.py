"""Dashboard pages for moderation: cases, suggestions, AutoMod, lockdown and raid mode, log channels.

The work itself lives in modtools.py and suggestions.py; `ctx.mod` and `ctx.suggestions` are those modules.
"""
import json
from datetime import timedelta

import discord
from fastapi import APIRouter, Depends, Form, Request

import automod
from db import now
from .views import audit, back, ctx_of, guild_of, render, uname
from .core import Denied, Sess, need, resolve_member

router = APIRouter()

CASE_KINDS = ("warn", "mute", "unmute", "kick", "ban", "unban", "note")
DECISIONS = ("approved", "denied", "considered", "implemented")
SUGGESTION_STATUS = ("pending", "approved", "denied", "considered", "implemented", "all")
CASE_PAGE = 100


# ---------- helpers ----------

def to_int(text):
    """'' -> None, '12' -> 12, anything else ValueError."""
    text = (text or "").strip()
    return int(text) if text else None


async def find_user(request, text):
    """A server member, or for a pasted id a user who may have left. None when nothing matches."""
    member = resolve_member(guild_of(request), text)
    if member is not None:
        return member
    text = (text or "").strip().strip("<>@!")
    if text.isdigit():
        try:
            return await ctx_of(request).bot.fetch_user(int(text))
        except discord.HTTPException:
            return None
    return None


def case_rows(ctx, guild, rows):
    out = []
    for r in rows:
        out.append({"id": r["id"], "kind": r["kind"], "user_id": r["user_id"], "user": uname(guild, r["user_id"]),
                    "mod": "AutoMod" if r["auto"] and r["mod_id"] == guild.me.id else uname(guild, r["mod_id"]),
                    "rule": r["rule"] if r["auto"] else "", "reason": r["reason"], "created": r["created_at"],
                    "expires": r["expires_at"], "active": bool(r["active"]) and r["kind"] in ("warn", "mute", "ban")})
    return out


def duration_text(seconds):
    return automod.fmt_duration(seconds) if seconds else "never"


def user_label(guild, user):
    m = guild.get_member(user.id)
    return m.display_name if m else str(user)


# ---------- cases ----------

@router.get("/moderation")
async def moderation_page(request: Request, kind: str = "", q: str = "", s: Sess = Depends(need("moderation.view"))):
    ctx, guild = ctx_of(request), guild_of(request)
    kind = kind if kind in CASE_KINDS else ""
    user_id, searched = None, q.strip()
    if searched:
        target = resolve_member(guild, searched)
        user_id = target.id if target else int(searched) if searched.isdigit() else -1
    rows = case_rows(ctx, guild, ctx.db.infractions_recent(kind or None, user_id, CASE_PAGE))
    week = ctx.db.infraction_summary(now() - timedelta(days=7))
    return render(request, s, "moderation.html", rows=rows, kind=kind, q=q, kinds=CASE_KINDS, week=week,
                  can_act=s.access.can("moderation.act"), can_delete=s.access.can("moderation.admin"), full=len(rows) == CASE_PAGE)


@router.get("/moderation/user/{user_id}")
async def moderation_user(request: Request, user_id: int, s: Sess = Depends(need("moderation.view"))):
    ctx, guild = ctx_of(request), guild_of(request)
    member = guild.get_member(user_id)
    name = member.display_name if member else None
    if member is None:
        try:
            name = str(await ctx.bot.fetch_user(user_id))
        except discord.HTTPException:
            name = f"Unknown ({user_id})"
    return render(request, s, "mod_user.html", uid=user_id, name=name, member=member,
                  rows=case_rows(ctx, guild, ctx.db.infractions_for(user_id, 200)), counts=ctx.db.infraction_counts(user_id),
                  kinds=CASE_KINDS, can_act=s.access.can("moderation.act"), can_delete=s.access.can("moderation.admin"))


@router.post("/moderation/act")
async def moderation_act(request: Request, user: str = Form(""), kind: str = Form(""), duration: str = Form(""),
                         reason: str = Form(""), delete_days: str = Form("0"), dm: str = Form("1"),
                         s: Sess = Depends(need("moderation.act", post=True))):
    ctx, guild, mod = ctx_of(request), guild_of(request), ctx_of(request).mod
    reason = reason.strip()
    target = await find_user(request, user)
    if kind not in CASE_KINDS:
        return back(request, "/moderation", "err", "Pick an action.")
    if target is None or target.bot:
        return back(request, "/moderation", "err", "No single member matches that. Use their user ID or exact username.")
    if kind in ("warn", "note") and not reason:
        return back(request, "/moderation", "err", f"A {kind} needs a reason.")
    here = f"/moderation/user/{target.id}"
    if kind == "note":
        case = ctx.db.add_infraction(target.id, s.member.id, "note", reason[:500])
        await mod.log_to(guild, "moderation", mod.case_embed(ctx.db.infraction(case), guild))
        audit(request, s, "mod.note", f"{target} ({target.id}) case #{case}")
        return back(request, here, "ok", f"Note added as case #{case}.")
    try:
        seconds = automod.parse_duration(duration) if duration.strip() else (3600 if kind == "mute" else None)
        days = max(0, min(int(delete_days or 0), 7))
    except ValueError as exc:
        return back(request, here, "err", str(exc))
    try:
        case, extra = await mod.apply(guild, kind, target, s.member, reason, seconds=seconds, delete_seconds=days * 86400, dm=dm == "1")
    except mod.ModError as exc:
        return back(request, here, "err", str(exc))
    audit(request, s, f"mod.{kind}", f"{target} ({target.id}) case #{case}")
    return back(request, here, "ok", f"{user_label(guild, target)} was {mod.PAST[kind]}. Case #{case}.{extra}")


@router.post("/moderation/case/{case_id}/reason")
async def case_reason(request: Request, case_id: int, reason: str = Form(""), s: Sess = Depends(need("moderation.act", post=True))):
    ctx = ctx_of(request)
    row = ctx.db.infraction(case_id)
    if row is None:
        raise Denied(404, "No such case.")
    if reason.strip():
        ctx.db.set_infraction_reason(case_id, reason.strip()[:500])
        audit(request, s, "mod.reason", f"case #{case_id}")
    return back(request, f"/moderation/user/{row['user_id']}", "ok", f"Case #{case_id} updated.")


@router.post("/moderation/case/{case_id}/delete")
async def case_delete(request: Request, case_id: int, s: Sess = Depends(need("moderation.admin", post=True))):
    ctx, guild, mod = ctx_of(request), guild_of(request), ctx_of(request).mod
    row = ctx.db.infraction(case_id)
    if row is None:
        raise Denied(404, "No such case.")
    ctx.db.delete_infraction(case_id)
    await mod.log_to(guild, "moderation", discord.Embed(
        title=f"Case #{case_id} deleted", colour=discord.Colour.light_grey(), timestamp=now(),
        description=f"{row['kind']} of <@{row['user_id']}> removed by {s.member.mention} (dashboard).\nReason was: {mod.clip(row['reason'], 500)}"))
    audit(request, s, "mod.case_delete", f"case #{case_id} ({row['kind']} of {row['user_id']})")
    return back(request, f"/moderation/user/{row['user_id']}", "ok", f"Case #{case_id} deleted.")


@router.post("/moderation/user/{user_id}/clearwarns")
async def clear_warns(request: Request, user_id: int, s: Sess = Depends(need("moderation.act", post=True))):
    ctx, guild, mod = ctx_of(request), guild_of(request), ctx_of(request).mod
    n = ctx.db.clear_warns(user_id)
    await mod.log_to(guild, "moderation", discord.Embed(
        title="Warnings cleared", colour=discord.Colour.green(), timestamp=now(),
        description=f"{n} warning(s) for <@{user_id}> cleared by {s.member.mention} (dashboard)."))
    audit(request, s, "mod.clearwarns", f"{user_id}: {n}")
    return back(request, f"/moderation/user/{user_id}", "ok", f"Cleared {n} active warning(s). They stay in the history.")


# ---------- suggestions ----------

@router.get("/suggestions")
async def suggestions_page(request: Request, status: str = "pending", s: Sess = Depends(need("moderation.view"))):
    ctx, guild = ctx_of(request), guild_of(request)
    status = status if status in SUGGESTION_STATUS else "pending"
    rows = []
    for r in ctx.db.suggestion_list(None if status == "all" else status, 100):
        up, down = ctx.db.suggestion_votes(r["id"])
        rows.append({"id": r["id"], "content": r["content"], "status": r["status"], "author": uname(guild, r["user_id"]),
                     "anonymous": bool(r["anonymous"]), "created": r["created_at"], "reason": r["reason"], "up": up, "down": down,
                     "decided_by": uname(guild, r["decided_by"]) if r["decided_by"] else ""})
    return render(request, s, "suggestions.html", rows=rows, status=status, statuses=SUGGESTION_STATUS, decisions=DECISIONS,
                  can_act=s.access.can("moderation.act"))


@router.post("/suggestions/{number}/{status}")
async def suggestion_decide(request: Request, number: int, status: str, reason: str = Form(""),
                            s: Sess = Depends(need("moderation.act", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    if status not in DECISIONS:
        raise Denied(404, "No such decision.")
    label = await ctx.suggestions.settle(guild, s.member, number, status, reason.strip())
    if label is None:
        raise Denied(404, "No such suggestion.")
    audit(request, s, f"suggestion.{status}", f"#{number}")
    return back(request, "/suggestions?status=all", "ok", f"Suggestion #{number} marked {label.lower()}.")


# ---------- AutoMod ----------

def exemptions(c, guild):
    """Every channel and role AutoMod skips: (rule or 'all', 'channel'|'role', id, label)."""
    out = []

    def add(rule, kind, ids):
        for i in ids:
            thing = guild.get_channel(i) if kind == "channel" else guild.get_role(i)
            label = (f"#{thing.name}" if kind == "channel" else thing.name) if thing else f"Deleted ({i})"
            out.append((rule, kind, i, label))
    add("all", "channel", c["exempt_channels"])
    add("all", "role", c["exempt_roles"])
    for name in automod.RULES:
        add(name, "channel", c["rules"][name].get("channels", []))
        add(name, "role", c["rules"][name].get("roles", []))
    return out


@router.get("/automod")
async def automod_page(request: Request, s: Sess = Depends(need("moderation.admin"))):
    ctx, guild = ctx_of(request), guild_of(request)
    c = ctx.mod.cfg()
    return render(request, s, "automod.html", c=c, rules=automod.RULES, help=automod.RULE_HELP, actions=automod.ACTIONS,
                  units=ctx.mod.LIMIT_UNITS, fmt=automod.fmt_duration, steps=sorted(c["escalation"], key=lambda x: x["count"]),
                  exempt=exemptions(c, guild), roles=[r for r in reversed(guild.roles) if not r.is_default()],
                  channels=list(guild.text_channels))


def save_cfg(request, c):
    ctx_of(request).mod.save(c)


@router.post("/automod/toggle")
async def automod_toggle(request: Request, enabled: str = Form("0"), s: Sess = Depends(need("moderation.admin", post=True))):
    c = ctx_of(request).mod.cfg()
    c["enabled"] = enabled == "1"
    save_cfg(request, c)
    audit(request, s, "automod.toggle", "on" if c["enabled"] else "off")
    return back(request, "/automod", "ok", f"AutoMod is {'on' if c['enabled'] else 'off'}.")


@router.post("/automod/options")
async def automod_options(request: Request, dm: str = Form("1"), notice_seconds: str = Form(""), warn_window_days: str = Form(""),
                          s: Sess = Depends(need("moderation.admin", post=True))):
    c = ctx_of(request).mod.cfg()
    try:
        notice, window = to_int(notice_seconds), to_int(warn_window_days)
    except ValueError:
        return back(request, "/automod", "err", "Numbers only.")
    if (notice is not None and not 0 <= notice <= 60) or (window is not None and not 1 <= window <= 365):
        return back(request, "/automod", "err", "Notice is 0 to 60 seconds; the warning window is 1 to 365 days.")
    c["dm"] = dm == "1"
    if notice is not None:
        c["notice_seconds"] = notice
    if window is not None:
        c["warn_window_days"] = window
    save_cfg(request, c)
    audit(request, s, "automod.options", f"dm={c['dm']} notice={c['notice_seconds']} window={c['warn_window_days']}")
    return back(request, "/automod", "ok", "Saved.")


@router.post("/automod/rule/{name}")
async def automod_rule(request: Request, name: str, enabled: str = Form(""), action: str = Form(""), duration: str = Form(""),
                       limit: str = Form(""), seconds: str = Form(""), mode: str = Form(""),
                       s: Sess = Depends(need("moderation.admin", post=True))):
    c = ctx_of(request).mod.cfg()
    try:
        automod.set_rule(c, name, enabled=(enabled == "1") if enabled else None, action=action or None, duration=duration.strip() or None,
                         limit=to_int(limit), seconds=to_int(seconds), mode=mode or None)
    except ValueError as exc:
        return back(request, "/automod", "err", f"{name}: {exc}")
    save_cfg(request, c)
    audit(request, s, "automod.rule", f"{name}: {json.dumps(c['rules'][name], separators=(',', ':'))[:300]}")
    return back(request, "/automod", "ok", f"Saved the {name} rule.")


LISTS = {"word": ("words", "list"), "blocked_link": ("links", "block"), "allowed_link": ("links", "allow"),
         "allowed_invite": ("invites", "allow")}


@router.post("/automod/list")
async def automod_list(request: Request, kind: str = Form(""), value: str = Form(""), op: str = Form("add"),
                       s: Sess = Depends(need("moderation.admin", post=True))):
    if kind not in LISTS:
        raise Denied(400, "Unknown list.")
    c = ctx_of(request).mod.cfg()
    rule, key = LISTS[kind]
    lst = c["rules"][rule][key]
    value = value.strip()
    if kind in ("blocked_link", "allowed_link"):
        value = (automod.domains(value) or [value.lower().removeprefix("www.").strip("/")])[0]
    elif kind == "word":
        value = value.lower()
    if not value:
        return back(request, "/automod", "err", "Nothing to add.")
    match = next((x for x in lst if x.lower() == value.lower()), None)
    if op == "remove":
        if match is None:
            return back(request, "/automod", "err", "That is not on the list.")
        lst.remove(match)
    elif match is None:
        lst.append(value)
    save_cfg(request, c)
    audit(request, s, f"automod.{op}", f"{kind} (word values are not logged)" if kind == "word" else f"{kind}: {value}")
    return back(request, "/automod", "ok", f"{'Removed' if op == 'remove' else 'Added'}. {len(lst)} entries on the {kind.replace('_', ' ')} list.")


@router.post("/automod/ladder")
async def automod_ladder(request: Request, warnings: str = Form(""), action: str = Form(""), duration: str = Form("1h"),
                         remove: str = Form(""), s: Sess = Depends(need("moderation.admin", post=True))):
    c = ctx_of(request).mod.cfg()
    try:
        count = to_int(warnings)
        if count is None:
            raise ValueError("Enter the number of warnings")
        if remove == "1":
            c["escalation"] = [x for x in c["escalation"] if x["count"] != count]
        else:
            automod.set_step(c, count, action, duration or "1h")
    except ValueError as exc:
        return back(request, "/automod", "err", str(exc))
    save_cfg(request, c)
    audit(request, s, "automod.ladder", f"{count}: {'removed' if remove == '1' else action}")
    return back(request, "/automod", "ok", "Saved.")


@router.post("/automod/exempt")
async def automod_exempt(request: Request, kind: str = Form(""), target_id: str = Form(""), rule: str = Form("all"),
                         remove: str = Form(""), s: Sess = Depends(need("moderation.admin", post=True))):
    guild = guild_of(request)
    if kind not in ("channel", "role") or not target_id.isdigit() or (rule != "all" and rule not in automod.RULES):
        raise Denied(400, "Bad exemption.")
    ident = int(target_id)
    if remove != "1" and (guild.get_channel(ident) if kind == "channel" else guild.get_role(ident)) is None:
        return back(request, "/automod", "err", "That does not exist in this server.")
    mod = ctx_of(request).mod
    c = mod.cfg()
    mod.exempt_edit(c, rule, f"{kind}s", ident, remove == "1")
    save_cfg(request, c)
    audit(request, s, "automod.exempt", f"{'remove' if remove == '1' else 'add'} {kind} {ident} for {rule}")
    return back(request, "/automod", "ok", "Saved.")


# ---------- lockdown, raid mode, join gate ----------

def lockdown_view(ctx, guild):
    info = ctx.mod.lockdown_info()
    if not info:
        return None
    return {"by": uname(guild, info.get("by")), "at": info.get("at"), "reason": info.get("reason") or "",
            "channels": len({r["channel_id"] for r in ctx.db.locks("server")})}


@router.get("/lockdown")
async def lockdown_page(request: Request, s: Sess = Depends(need("moderation.admin"))):
    ctx, guild = ctx_of(request), guild_of(request)
    c = ctx.mod.cfg()
    raid = ctx.mod.raid_state()
    exempt = [(i, guild.get_channel(i)) for i in sorted(ctx.mod.lockdown_exempt())]
    locked = sorted({r["channel_id"] for r in ctx.db.locks("channel")})
    return render(request, s, "lockdown.html", lockdown=lockdown_view(ctx, guild), raid=raid, c=c, past=ctx.mod.PAST,
                  exempt=[(i, f"#{ch.name}" if ch else f"Deleted ({i})") for i, ch in exempt],
                  channels=list(guild.text_channels), single_locks=[(i, guild.get_channel(i)) for i in locked])


@router.post("/lockdown/start")
async def lockdown_start(request: Request, reason: str = Form(""), announce: str = Form("1"),
                         s: Sess = Depends(need("moderation.admin", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    if ctx.mod.lockdown_info():
        return back(request, "/lockdown", "err", "The server is already locked down.")
    locked, failed = await ctx.mod.lockdown_server(guild, s.member, reason.strip() or "Lockdown from the dashboard", announce == "1")
    audit(request, s, "lockdown.start", f"{locked} locked, {failed} failed: {reason.strip()[:100]}")
    return back(request, "/lockdown", "ok" if not failed else "err", f"Locked {locked} channels." + (f" {failed} failed; check my permissions there." if failed else ""))


@router.post("/lockdown/lift")
async def lockdown_lift(request: Request, reason: str = Form(""), s: Sess = Depends(need("moderation.admin", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    if not ctx.mod.lockdown_info():
        return back(request, "/lockdown", "err", "The server is not locked down.")
    done = await ctx.mod.lift_lockdown(guild, s.member, reason.strip() or "Lifted from the dashboard")
    audit(request, s, "lockdown.lift", f"{done} restored")
    return back(request, "/lockdown", "ok", f"Restored {done} channels.")


@router.post("/lockdown/exempt")
async def lockdown_exempt(request: Request, channel_id: str = Form(""), remove: str = Form(""),
                          s: Sess = Depends(need("moderation.admin", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    ident = int(channel_id) if channel_id.isdigit() else None
    if ident is None or (remove != "1" and guild.get_channel(ident) is None):
        return back(request, "/lockdown", "err", "Pick a channel.")
    ids = ctx.mod.lockdown_exempt()
    ids.discard(ident) if remove == "1" else ids.add(ident)
    ctx.db.put("lockdown_exempt", json.dumps(sorted(ids)))
    audit(request, s, "lockdown.exempt", f"{'remove' if remove == '1' else 'add'} {ident}")
    return back(request, "/lockdown", "ok", "Saved.")


@router.post("/raid/start")
async def raid_start(request: Request, action: str = Form("kick"), minutes: str = Form("30"), lockdown: str = Form(""),
                     s: Sess = Depends(need("moderation.admin", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    try:
        mins = to_int(minutes) or 0
    except ValueError:
        return back(request, "/lockdown", "err", "Minutes must be a number.")
    if action not in ("kick", "ban", "mute") or not 0 <= mins <= 1440:
        return back(request, "/lockdown", "err", "Pick kick, ban or mute, and 0 to 1440 minutes (0 means until turned off).")
    await ctx.mod.start_raid(guild, f"Turned on by {s.member.mention} (dashboard).", action, mins, lockdown == "1", s.member)
    audit(request, s, "raid.start", f"{action} for {mins or 'no limit'} min" + (", with lockdown" if lockdown == "1" else ""))
    return back(request, "/lockdown", "ok", "Raid mode is on.")


@router.post("/raid/stop")
async def raid_stop(request: Request, s: Sess = Depends(need("moderation.admin", post=True))):
    ok = await ctx_of(request).mod.stop_raid(guild_of(request), s.member)
    audit(request, s, "raid.stop", "ok" if ok else "was not on")
    return back(request, "/lockdown", "ok" if ok else "err", "Raid mode is off." if ok else "Raid mode was not on.")


@router.post("/raid/config")
async def raid_config(request: Request, enabled: str = Form("1"), joins: str = Form(""), seconds: str = Form(""), minutes: str = Form(""),
                      action: str = Form(""), lockdown: str = Form("0"), s: Sess = Depends(need("moderation.admin", post=True))):
    c = ctx_of(request).mod.cfg()
    try:
        values = {"joins": (to_int(joins), 2, 100), "seconds": (to_int(seconds), 2, 600), "minutes": (to_int(minutes), 1, 1440)}
    except ValueError:
        return back(request, "/lockdown", "err", "Numbers only.")
    for key, (val, low, high) in values.items():
        if val is None:
            continue
        if not low <= val <= high:
            return back(request, "/lockdown", "err", f"{key} must be between {low} and {high}.")
        c["raid"][key] = val
    if action:
        if action not in ("kick", "ban", "mute"):
            return back(request, "/lockdown", "err", "Pick kick, ban or mute.")
        c["raid"]["action"] = action
    c["raid"]["enabled"], c["raid"]["lockdown"] = enabled == "1", lockdown == "1"
    save_cfg(request, c)
    audit(request, s, "raid.config", json.dumps(c["raid"], separators=(",", ":")))
    return back(request, "/lockdown", "ok", "Saved.")


@router.post("/joingate")
async def joingate(request: Request, min_age_days: str = Form(""), action: str = Form(""), no_avatar: str = Form("0"),
                   s: Sess = Depends(need("moderation.admin", post=True))):
    c = ctx_of(request).mod.cfg()
    try:
        age = to_int(min_age_days)
    except ValueError:
        return back(request, "/lockdown", "err", "Days must be a number.")
    if age is not None:
        if not 0 <= age <= 365:
            return back(request, "/lockdown", "err", "Days must be 0 to 365.")
        c["joingate"]["min_age_days"] = age
    if action:
        if action not in ("kick", "ban", "mute"):
            return back(request, "/lockdown", "err", "Pick kick, ban or mute.")
        c["joingate"]["action"] = action
    c["joingate"]["no_avatar"] = no_avatar == "1"
    save_cfg(request, c)
    audit(request, s, "joingate", json.dumps(c["joingate"], separators=(",", ":")))
    return back(request, "/lockdown", "ok", "Saved.")


# ---------- log channels, sticky roles, suggestions setup ----------

@router.get("/modlogs")
async def modlogs_page(request: Request, s: Sess = Depends(need("moderation.admin"))):
    ctx, guild = ctx_of(request), guild_of(request)
    mod = ctx.mod
    cats = [(k, text, ctx.db.get_int(f"logch_{k}"), k in mod.QUIET) for k, text in mod.LOG_CATEGORIES.items()]
    excluded = [(i, guild.get_role(i)) for i in json.loads(ctx.db.get("sticky_exclude") or "[]")]
    return render(request, s, "modlogs.html", cats=cats, channels=list(guild.text_channels), roles=[r for r in reversed(guild.roles) if not r.is_default()],
                  sticky=ctx.db.get("sticky") == "1", excluded=[(i, r.name if r else f"Deleted ({i})") for i, r in excluded],
                  suggest_channel=ctx.db.get_int("suggestions_channel_id"), suggest_log=ctx.db.get_int("suggestion_log_channel_id"),
                  anonymous=ctx.db.get("suggest_anonymous") == "1")


@router.post("/modlogs")
async def modlogs_save(request: Request, s: Sess = Depends(need("moderation.admin", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    form = await request.form()
    changed = []
    for key in ctx.mod.LOG_CATEGORIES:
        field = f"logch_{key}"
        if field not in form:
            continue
        raw = str(form[field]).strip()
        old = ctx.db.get_int(field)
        if not raw:
            if old is not None:
                ctx.db.clear(field)
                changed.append(key)
            continue
        if not raw.isdigit() or not isinstance(guild.get_channel(int(raw)), discord.TextChannel):
            return back(request, "/modlogs", "err", f"{key} points at a channel that does not exist. Nothing saved.")
        if int(raw) != old:
            ctx.db.put(field, int(raw))
            changed.append(key)
    audit(request, s, "modlogs.edit", ", ".join(changed) or "no change")
    return back(request, "/modlogs", "ok", f"Saved ({len(changed)} changed).")


@router.post("/modlogs/sticky")
async def sticky_save(request: Request, enabled: str = Form("0"), exclude_role: str = Form(""), remove_role: str = Form(""),
                      s: Sess = Depends(need("moderation.admin", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    ctx.db.put("sticky", "1" if enabled == "1" else "0")
    ids = set(json.loads(ctx.db.get("sticky_exclude") or "[]"))
    if exclude_role.isdigit() and guild.get_role(int(exclude_role)):
        ids.add(int(exclude_role))
    if remove_role.isdigit():
        ids.discard(int(remove_role))
    ctx.db.put("sticky_exclude", json.dumps(sorted(ids)))
    audit(request, s, "sticky.edit", f"{'on' if enabled == '1' else 'off'}, {len(ids)} excluded")
    return back(request, "/modlogs", "ok", "Saved.")


@router.post("/modlogs/suggestions")
async def suggestions_save(request: Request, channel_id: str = Form(""), log_channel_id: str = Form(""), anonymous: str = Form("0"),
                           s: Sess = Depends(need("moderation.admin", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    for key, raw in (("suggestions_channel_id", channel_id.strip()), ("suggestion_log_channel_id", log_channel_id.strip())):
        if not raw:
            ctx.db.clear(key)
        elif raw.isdigit() and isinstance(guild.get_channel(int(raw)), discord.TextChannel):
            ctx.db.put(key, int(raw))
        else:
            return back(request, "/modlogs", "err", "Pick text channels from the list. Nothing saved.")
    ctx.db.put("suggest_anonymous", "1" if anonymous == "1" else "0")
    audit(request, s, "suggestions.config", f"anonymous={anonymous == '1'}")
    return back(request, "/modlogs", "ok", "Saved.")
