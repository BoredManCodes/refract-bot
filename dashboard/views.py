import asyncio
import json
import secrets
from html import escape
from datetime import datetime, timedelta, timezone

import discord
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from db import now
from .core import NAV, PERMS, Denied, Sess, flash, need, resolve_member

router = APIRouter()

ROLE_SETTINGS = [("admin_role_id", "Admin role"), ("head_role_id", "Head Admin role"), ("staff_role_id", "Staff role"),
                 ("member_role_id", "Member role"), ("linked_role_id", "Linked Minecraft account role (optional)")]
CHANNEL_SETTINGS = [("announcements_channel_id", "Announcements"), ("welcome_channel_id", "Welcome and goodbye posts"),
                    ("applications_channel_id", "Staff applications"), ("elections_channel_id", "Elections"),
                    ("log_channel_id", "Public admin log"), ("modlog_channel_id", "Mod log"),
                    ("tickets_log_channel_id", "Ticket logs")]
CATEGORY_SETTINGS = [("support_category_id", "Ticket category")]
TRANSCRIPT_CSP = ("default-src 'none'; img-src https: data:; style-src 'unsafe-inline' https://fonts.googleapis.com; "
                  "font-src https://fonts.gstatic.com; script-src 'unsafe-inline'; frame-ancestors 'self'")


# ---------- helpers ----------

def ctx_of(request):
    return request.app.state.ctx


def guild_of(request):
    guild = ctx_of(request).guild()
    if guild is None:
        raise Denied(503, "The bot is starting up. Try again in a minute.")
    return guild


def render(request, s, template, http_status=200, **kw):
    visible = [(path, label) for path, label, perm in NAV if s.access.can(perm)] if s else []
    return request.app.state.templates.TemplateResponse(request, template, {
        "s": s, "nav": visible, "path": request.url.path, "csrf": request.session.get("csrf", ""),
        "flash": request.session.pop("flash", None), "perms": PERMS, **kw}, status_code=http_status)


def back(request, url, kind=None, message=None):
    if message:
        flash(request, kind, message)
    return RedirectResponse(url, status_code=303)


def uname(guild, uid):
    m = guild.get_member(int(uid)) if uid else None
    return m.display_name if m else f"Unknown ({uid})"


def parse_date(text):
    try:
        return datetime(int(text[:4]), int(text[5:7]), int(text[8:10]), tzinfo=timezone.utc)
    except ValueError:
        return None


def audit(request, s, action, detail=""):
    ctx_of(request).db.audit(s.member.id, action, detail)


# ---------- overview and member lookup ----------

@router.get("/")
async def index(request: Request, s: Sess = Depends(need())):
    ctx, guild = ctx_of(request), guild_of(request)
    cards = []
    if s.access.can("tickets.view"):
        cards.append(("Open tickets", len(ctx.db.open_tickets()), "/tickets"))
    if s.access.can("applications.review"):
        cards.append(("Pending applications", len(ctx.db.applications("pending")), "/applications"))
    if s.access.can("elections.manage"):
        el = ctx.db.active_election()
        cards.append(("Election", el["status"] if el else "none running", "/elections"))
    if s.access.can("members.view"):
        cards.append(("Members", guild.member_count, "/members"))
    if s.access.can("moderation.view"):
        cards.append(("Cases this week", sum(ctx.db.infraction_summary(now() - timedelta(days=7)).values()), "/moderation"))
        cards.append(("Pending suggestions", len(ctx.db.suggestion_list("pending")), "/suggestions"))
    if s.access.can("moderation.admin"):
        cards.append(("AutoMod", "on" if ctx.mod.cfg()["enabled"] else "off", "/automod"))
        state = "locked down" if ctx.mod.lockdown_info() else "raid mode" if ctx.mod.raid_state().get("on") else "normal"
        cards.append(("Server state", state, "/lockdown"))
    return render(request, s, "index.html", cards=cards, guild=guild)


@router.get("/members/options")
async def member_options(request: Request, q: str = "", s: Sess = Depends(need())):
    guild = guild_of(request)
    low = q.strip().lower()
    found = []
    if len(low) >= 2:
        for m in guild.members:
            if not m.bot and (low in m.name.lower() or low in m.display_name.lower()):
                found.append(m)
                if len(found) == 15:
                    break
    return HTMLResponse(option_tags(found))


def option_tags(members):
    return "".join(f'<option value="{m.id}">{escape(m.display_name)} ({escape(m.name)})</option>' for m in members)


# ---------- tickets ----------

def ticket_row(ctx, guild, t):
    return {"id": t["id"], "status": t["status"], "kind": ctx.ticket_kinds[t["kind"]][0] if t["kind"] in ctx.ticket_kinds else t["kind"],
            "opener": uname(guild, t["user_id"]), "topic": t["topic"], "created": t["created_at"], "closed": t["closed_at"]}


@router.get("/tickets")
async def tickets_page(request: Request, status: str = "open", q: str = "", s: Sess = Depends(need("tickets.view"))):
    ctx, guild = ctx_of(request), guild_of(request)
    status = status if status in ("open", "closed", "all") else "open"
    rows = [ticket_row(ctx, guild, t) for t in ctx.db.tickets(None if status == "all" else status, limit=500)]
    if q.strip():
        low = q.strip().lower()
        rows = [r for r in rows if low in r["opener"].lower() or low in r["topic"].lower() or low == str(r["id"])]
    channels = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages]
    return render(request, s, "tickets.html", rows=rows[:200], status=status, q=q, kinds=ctx.ticket_kinds, channels=channels,
                  can_manage=s.access.can("tickets.manage"))


@router.post("/tickets/new")
async def ticket_new(request: Request, user: str = Form(""), kind: str = Form(""), topic: str = Form(""),
                     s: Sess = Depends(need("tickets.manage", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    target = resolve_member(guild, user)
    topic = topic.strip()[:1000]
    if target is None or target.bot:
        return back(request, "/tickets", "err", "No single member matches that. Use their user ID or exact username.")
    if kind not in ctx.ticket_kinds or not topic:
        return back(request, "/tickets", "err", "Pick a type and describe the issue.")
    try:
        ch = await ctx.create_ticket(guild, target, kind, topic, opened_by=s.member)
    except ctx.TicketExists as e:
        return back(request, "/tickets", "err", f"{target.display_name} already has a ticket open (#{e.channel.name}).")
    except Exception:
        return back(request, "/tickets", "err", "Could not create the ticket. Check the bot log.")
    ticket = ctx.db.ticket_by_channel(ch.id)
    audit(request, s, "ticket.open", f"#{ticket['id']} for {target} ({target.id})")
    return back(request, f"/tickets/{ticket['id']}", "ok", f"Ticket opened for {target.display_name}.")


@router.post("/tickets/panel")
async def ticket_panel(request: Request, channel_id: str = Form(""), s: Sess = Depends(need("tickets.manage", post=True))):
    guild = guild_of(request)
    ch = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if not isinstance(ch, discord.TextChannel):
        return back(request, "/tickets", "err", "Pick a text channel.")
    await ctx_of(request).post_ticket_panel(ch)
    audit(request, s, "ticket.panel", f"#{ch.name}")
    return back(request, "/tickets", "ok", f"Panel posted in #{ch.name}.")


def load_ticket(request, ticket_id):
    t = ctx_of(request).db.ticket(ticket_id)
    if t is None:
        raise Denied(404, "No such ticket.")
    return t


async def channel_messages(channel, limit=60):
    out = []
    async for m in channel.history(limit=limit):
        text = m.clean_content
        if not (text or m.attachments or m.embeds):
            continue
        out.append({"author": m.author.display_name, "bot": m.author.bot, "avatar": m.author.display_avatar.replace(size=64, static_format="png").url,
                    "text": text, "when": m.created_at.isoformat(),
                    "files": [{"name": a.filename, "url": a.url, "image": (a.content_type or "").startswith("image/")} for a in m.attachments],
                    "embeds": [e.title or e.description or "embed" for e in m.embeds]})
    return out[::-1]


@router.get("/tickets/{ticket_id}")
async def ticket_page(request: Request, ticket_id: int, s: Sess = Depends(need("tickets.view"))):
    ctx, guild = ctx_of(request), guild_of(request)
    t = load_ticket(request, ticket_id)
    info = ticket_row(ctx, guild, t)
    info["closed_by"] = uname(guild, t["closed_by"]) if t["closed_by"] else None
    ch = guild.get_channel(t["channel_id"] or 0) if t["status"] == "open" else None
    members = []
    if ch is not None:
        for target in ch.overwrites:
            if isinstance(target, discord.Member) and not target.bot and ch.overwrites_for(target).view_channel:
                members.append({"id": target.id, "name": target.display_name, "opener": target.id == t["user_id"]})
    share = ctx.db.share_token(t["id"]) if t["status"] == "closed" else None
    return render(request, s, "ticket.html", t=info, channel=ch, members=members, can_manage=s.access.can("tickets.manage"),
                  has_transcript=bool(t["token"] and ctx.db.transcript(t["token"])),
                  share_url=f"{ctx.transcript_base_url}/s/{share}" if share and ctx.transcript_base_url else None,
                  can_share=bool(ctx.transcript_base_url), channel_missing=t["status"] == "open" and ch is None)


@router.get("/tickets/{ticket_id}/messages")
async def ticket_messages(request: Request, ticket_id: int, s: Sess = Depends(need("tickets.view"))):
    t = load_ticket(request, ticket_id)
    ch = guild_of(request).get_channel(t["channel_id"] or 0)
    msgs = await channel_messages(ch) if ch is not None and t["status"] == "open" else []
    return request.app.state.templates.TemplateResponse(request, "_messages.html", {"msgs": msgs})  # polled: must not eat the flash


@router.get("/tickets/{ticket_id}/transcript")
async def ticket_transcript(request: Request, ticket_id: int, s: Sess = Depends(need("tickets.view"))):
    ctx = ctx_of(request)
    t = load_ticket(request, ticket_id)
    page = ctx.db.transcript(t["token"]) if t["token"] else None
    if page is None:
        raise Denied(404, "This ticket has no transcript.")
    return HTMLResponse(page, headers={"Content-Security-Policy": TRANSCRIPT_CSP})


def open_ticket_channel(request, ticket_id):
    t = load_ticket(request, ticket_id)
    ch = guild_of(request).get_channel(t["channel_id"] or 0)
    if t["status"] != "open" or ch is None:
        raise Denied(409, "This ticket is closed or its channel is gone.")
    return t, ch


@router.post("/tickets/{ticket_id}/reply")
async def ticket_reply(request: Request, ticket_id: int, text: str = Form(""), s: Sess = Depends(need("tickets.manage", post=True))):
    t, ch = open_ticket_channel(request, ticket_id)
    text = text.strip()
    if text:
        await ch.send(f"**{s.member.display_name}** (dashboard): {text}"[:2000], allowed_mentions=discord.AllowedMentions(users=True))
        audit(request, s, "ticket.reply", f"#{t['id']}")
    return back(request, f"/tickets/{ticket_id}")


@router.post("/tickets/{ticket_id}/add")
async def ticket_add(request: Request, ticket_id: int, user: str = Form(""), s: Sess = Depends(need("tickets.manage", post=True))):
    t, ch = open_ticket_channel(request, ticket_id)
    target = resolve_member(guild_of(request), user)
    if target is None or target.bot:
        return back(request, f"/tickets/{ticket_id}", "err", "No single member matches that. Use their user ID or exact username.")
    added = await ctx_of(request).add_ticket_users(ch, [target], s.member)
    audit(request, s, "ticket.add_user", f"#{t['id']} {target} ({target.id})")
    return back(request, f"/tickets/{ticket_id}", "ok" if added else "err",
                f"Added {target.display_name}." if added else f"{target.display_name} already has access.")


@router.post("/tickets/{ticket_id}/remove")
async def ticket_remove(request: Request, ticket_id: int, user_id: str = Form(""), s: Sess = Depends(need("tickets.manage", post=True))):
    t, ch = open_ticket_channel(request, ticket_id)
    target = guild_of(request).get_member(int(user_id)) if user_id.isdigit() else None
    ok = target is not None and await ctx_of(request).remove_ticket_user(ch, target, s.member)
    if ok:
        audit(request, s, "ticket.remove_user", f"#{t['id']} {target} ({target.id})")
    return back(request, f"/tickets/{ticket_id}", "ok" if ok else "err",
                f"Removed {target.display_name}." if ok else "Could not remove that user (the opener stays).")


@router.post("/tickets/{ticket_id}/close")
async def ticket_close(request: Request, ticket_id: int, s: Sess = Depends(need("tickets.manage", post=True))):
    ctx = ctx_of(request)
    t = load_ticket(request, ticket_id)
    if t["status"] != "open":
        return back(request, "/tickets", "err", "Already closed.")
    ch = guild_of(request).get_channel(t["channel_id"] or 0)
    audit(request, s, "ticket.close", f"#{t['id']}")
    if ch is None:  # channel was deleted by hand
        ctx.db.close_ticket(t["id"], s.member.id, None)
        return back(request, "/tickets", "ok", f"Ticket #{t['id']} had no channel; marked closed.")
    task = asyncio.create_task(ctx.close_ticket(ch, s.member))  # saves the transcript, then deletes the channel
    tasks = request.app.state.tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return back(request, "/tickets?status=open", "ok", f"Closing ticket #{t['id']}. The transcript appears under Closed in a few seconds.")


@router.post("/tickets/{ticket_id}/share")
async def ticket_share(request: Request, ticket_id: int, on: str = Form("0"), s: Sess = Depends(need("tickets.manage", post=True))):
    ctx = ctx_of(request)
    t = load_ticket(request, ticket_id)
    if t["status"] != "closed" or not ctx.transcript_base_url:
        raise Denied(409, "Share links need a closed ticket and TRANSCRIPT_BASE_URL.")
    if on == "1":
        if ctx.db.share_token(t["id"]) is None:
            ctx.db.set_share(t["id"], secrets.token_urlsafe(24))
    else:
        ctx.db.clear_share(t["id"])
    audit(request, s, "ticket.share" if on == "1" else "ticket.unshare", f"#{t['id']}")
    return back(request, f"/tickets/{ticket_id}")


# ---------- applications ----------

@router.get("/applications")
async def applications_page(request: Request, status: str = "pending", s: Sess = Depends(need("applications.review"))):
    ctx, guild = ctx_of(request), guild_of(request)
    status = status if status in ("pending", "accepted", "denied", "all") else "pending"
    rows = []
    for a in ctx.db.applications(None if status == "all" else status, limit=100):
        rows.append({"id": a["id"], "user_id": a["user_id"], "name": uname(guild, a["user_id"]), "mc": a["mc_name"],
                     "answers": json.loads(a["answers"]), "status": a["status"], "created": a["created_at"],
                     "decided_by": uname(guild, a["decided_by"]) if a["decided_by"] else None})
    return render(request, s, "applications.html", rows=rows, status=status)


@router.post("/applications/{app_id}/{verdict}")
async def application_decide(request: Request, app_id: int, verdict: str, s: Sess = Depends(need("applications.review", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    app = ctx.db.application(app_id)
    if verdict not in ("accept", "deny") or app is None:
        raise Denied(404, "No such application.")
    if app["status"] != "pending":
        return back(request, "/applications", "err", "Already decided.")
    if app["user_id"] == s.member.id:
        return back(request, "/applications", "err", "You cannot decide your own application.")
    await ctx.decide_from_dashboard(guild, app, verdict == "accept", s.member)
    audit(request, s, f"application.{verdict}", f"#{app_id} {uname(guild, app['user_id'])}")
    return back(request, "/applications", "ok", f"Application #{app_id} {'accepted' if verdict == 'accept' else 'denied'}.")


# ---------- elections ----------

@router.get("/elections")
async def elections_page(request: Request, s: Sess = Depends(need("elections.manage"))):
    ctx, guild = ctx_of(request), guild_of(request)
    el = ctx.db.active_election()
    candidates = [{"name": uname(guild, u), "pitch": p} for u, p in ctx.db.candidates(el["id"])] if el else []
    head = ctx.head_admin(guild)
    return render(request, s, "elections.html", el=el, candidates=candidates, head=head.display_name if head else None,
                  next_nominations=ctx.db.get("next_nominations"),
                  history=[{"id": e["id"], "status": e["status"], "opens": e["voting_open"], "closes": e["voting_close"]}
                           for e in ctx.db.elections(8)])


@router.post("/elections/start")
async def election_start(request: Request, nomination_days: int = Form(14), voting_days: int = Form(7), stays_admin: str = Form(""),
                         s: Sess = Depends(need("elections.manage", post=True))):
    eid = await ctx_of(request).open_nominations(guild_of(request), max(1, min(nomination_days, 60)), max(1, min(voting_days, 30)), bool(stays_admin))
    audit(request, s, "election.start", f"#{eid}" if eid else "refused")
    return back(request, "/elections", "ok" if eid else "err", f"Election #{eid} opened." if eid else "An election is already running.")


@router.post("/elections/schedule")
async def election_schedule(request: Request, date: str = Form(""), s: Sess = Depends(need("elections.manage", post=True))):
    when = parse_date(date)
    if when is None:
        return back(request, "/elections", "err", "Use the date picker (YYYY-MM-DD).")
    ctx_of(request).db.put("next_nominations", when.isoformat())
    audit(request, s, "election.schedule", date)
    return back(request, "/elections", "ok", f"Nominations will open {date} (UTC).")


@router.post("/elections/runoff")
async def election_runoff(request: Request, s: Sess = Depends(need("elections.manage", post=True))):
    ok = await ctx_of(request).start_runoff(guild_of(request))
    audit(request, s, "election.runoff", "ok" if ok else "refused")
    return back(request, "/elections", "ok" if ok else "err", "Runoff open." if ok else "No tied election.")


@router.post("/elections/sethead")
async def election_sethead(request: Request, user: str = Form(""), since: str = Form(""),
                           s: Sess = Depends(need("elections.manage", post=True))):
    guild = guild_of(request)
    target = resolve_member(guild, user)
    if target is None or target.bot:
        return back(request, "/elections", "err", "No single member matches that. Use their user ID or exact username.")
    when = parse_date(since) if since else None
    if since and when is None:
        return back(request, "/elections", "err", "Use the date picker (YYYY-MM-DD).")
    term, announced = await ctx_of(request).set_head(guild, target, when.isoformat() if when else None)
    audit(request, s, "election.sethead", f"{target} ({target.id})")
    return back(request, "/elections", "ok", f"{target.display_name} is Head Admin (term {term})." + ("" if announced else " No announcements channel, nothing posted."))


# ---------- members ----------

@router.get("/members")
async def members_page(request: Request, q: str = "", s: Sess = Depends(need("members.view"))):
    ctx, guild = ctx_of(request), guild_of(request)
    low = q.strip().lower()
    rows = []
    for m in sorted((m for m in guild.members if not m.bot), key=lambda m: m.joined_at or datetime.max.replace(tzinfo=timezone.utc)):
        if low and low not in m.name.lower() and low not in m.display_name.lower() and low != str(m.id):
            continue
        join = ctx.db.last_invite_join(m.id)
        rows.append({"id": m.id, "name": m.display_name, "username": m.name, "joined": m.joined_at.isoformat() if m.joined_at else "",
                     "roles": [r.name for r in m.roles if not r.is_default()][-4:],
                     "invited_by": uname(guild, join["inviter_id"]) if join and join["inviter_id"] else ""})
        if len(rows) == 200:
            break
    top = [(uname(guild, uid), n) for uid, n in ctx.db.top_inviters(10)]
    return render(request, s, "members.html", rows=rows, q=q, top=top, total=guild.member_count)


# ---------- admin log ----------

@router.get("/adminlog")
async def adminlog_page(request: Request, s: Sess = Depends(need("adminlog.post"))):
    return render(request, s, "adminlog.html")


@router.post("/adminlog")
async def adminlog_post(request: Request, action: str = Form(""), reason: str = Form(""), admin: str = Form(""),
                        s: Sess = Depends(need("adminlog.post", post=True))):
    guild = guild_of(request)
    who = s.member
    if admin.strip():
        who = resolve_member(guild, admin)
        if who is None or who.bot:
            return back(request, "/adminlog", "err", "No single member matches that admin.")
        if who.id != s.member.id and not s.access.owner:
            return back(request, "/adminlog", "err", "Only the owner can log an action under someone else.")
    if not action.strip() or not reason.strip():
        return back(request, "/adminlog", "err", "Fill in the action and the reason.")
    msg = await ctx_of(request).post_adminlog(guild, who, action.strip(), reason.strip())
    if msg is None:
        return back(request, "/adminlog", "err", "The admin log channel is not set. See Settings.")
    audit(request, s, "adminlog.post", f"{action.strip()[:80]} (as {who})")
    return back(request, "/adminlog", "ok", "Logged.")


# ---------- settings ----------

@router.get("/settings")
async def settings_page(request: Request, s: Sess = Depends(need("settings.edit"))):
    ctx, guild = ctx_of(request), guild_of(request)
    roles = [r for r in reversed(guild.roles) if not r.is_default()]
    channels = [c for c in guild.text_channels]
    cats = list(guild.categories)
    fields = ([(k, label, "role", ctx.db.get_int(k)) for k, label in ROLE_SETTINGS]
              + [(k, label, "channel", ctx.db.get_int(k)) for k, label in CHANNEL_SETTINGS]
              + [(k, label, "category", ctx.db.get_int(k)) for k, label in CATEGORY_SETTINGS])
    return render(request, s, "settings.html", fields=fields, roles=roles, channels=channels, cats=cats)


@router.post("/settings")
async def settings_save(request: Request, s: Sess = Depends(need("settings.edit", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    form = await request.form()
    changed = []
    for key, _ in ROLE_SETTINGS + CHANNEL_SETTINGS + CATEGORY_SETTINGS:
        if key not in form:
            continue
        raw = str(form[key]).strip()
        old = ctx.db.get_int(key)
        if not raw:
            if old is not None:
                ctx.db.clear(key)
                changed.append(key)
            continue
        if not raw.isdigit():
            continue
        wanted = int(raw)
        valid = (guild.get_role(wanted) if key.endswith("role_id") else guild.get_channel(wanted))
        if valid is None or (key in dict(CATEGORY_SETTINGS) and not isinstance(valid, discord.CategoryChannel)) \
                or (key in dict(CHANNEL_SETTINGS) and not isinstance(valid, discord.TextChannel)):
            return back(request, "/settings", "err", f"{key} points at something that does not exist in this server. Nothing saved.")
        if wanted != old:
            ctx.db.put(key, wanted)
            changed.append(key)
    audit(request, s, "settings.edit", ", ".join(changed) or "no change")
    return back(request, "/settings", "ok", f"Saved ({len(changed)} changed).")


# ---------- permissions and audit (owner only) ----------

@router.get("/permissions")
async def permissions_page(request: Request, s: Sess = Depends(need("owner"))):
    ctx, guild = ctx_of(request), guild_of(request)
    grouped = {}
    for g in ctx.db.grants():
        grouped.setdefault((g["kind"], g["subject_id"]), []).append(g["perm"])
    subjects = []
    for (kind, sid), perms in grouped.items():
        if kind == "role":
            r = guild.get_role(sid)
            name = r.name if r else f"Deleted role ({sid})"
        else:
            name = uname(guild, sid)
        subjects.append({"kind": kind, "id": sid, "name": name, "perms": perms})
    roles = [r for r in reversed(guild.roles) if not r.is_default()]
    return render(request, s, "permissions.html", subjects=subjects, roles=roles)


@router.post("/permissions/grant")
async def permissions_grant(request: Request, kind: str = Form(""), role_id: str = Form(""), user: str = Form(""),
                            s: Sess = Depends(need("owner", post=True))):
    ctx, guild = ctx_of(request), guild_of(request)
    form = await request.form()
    picked = [p for p in form.getlist("perm") if p in PERMS]
    if not picked:
        return back(request, "/permissions", "err", "Tick at least one permission.")
    if kind == "role":
        target = guild.get_role(int(role_id)) if role_id.isdigit() else None
        if target is None or target.is_default():
            return back(request, "/permissions", "err", "Pick a role (not @everyone).")
        sid, label = target.id, f"role {target.name}"
    elif kind == "user":
        target = resolve_member(guild, user)
        if target is None or target.bot:
            return back(request, "/permissions", "err", "No single member matches that. Use their user ID or exact username.")
        sid, label = target.id, f"user {target.display_name}"
    else:
        return back(request, "/permissions", "err", "Pick role or user.")
    for p in picked:
        ctx.db.add_grant(kind, sid, p)
    audit(request, s, "permissions.grant", f"{label}: {', '.join(picked)}")
    return back(request, "/permissions", "ok", f"Granted to {label}.")


@router.post("/permissions/revoke")
async def permissions_revoke(request: Request, kind: str = Form(""), subject_id: str = Form(""), perm: str = Form(""),
                             s: Sess = Depends(need("owner", post=True))):
    if kind in ("role", "user") and subject_id.isdigit() and perm in PERMS:
        ctx_of(request).db.remove_grant(kind, int(subject_id), perm)
        audit(request, s, "permissions.revoke", f"{kind} {subject_id}: {perm}")
    return back(request, "/permissions", "ok", "Revoked.")


@router.get("/audit")
async def audit_page(request: Request, s: Sess = Depends(need("owner"))):
    ctx, guild = ctx_of(request), guild_of(request)
    rows = [{"who": uname(guild, r["user_id"]), "action": r["action"], "detail": r["detail"], "at": r["at"]} for r in ctx.db.recent_audit(200)]
    return render(request, s, "audit.html", rows=rows)


@router.get("/system")
async def system_page(request: Request, s: Sess = Depends(need("owner"))):
    return render(request, s, "system.html", version=await ctx_of(request).current_version(), output=request.session.pop("output", None))


@router.post("/system/update")
async def system_update(request: Request, s: Sess = Depends(need("owner", post=True))):
    ok, head, detail, changed = await ctx_of(request).perform_update()
    audit(request, s, "system.update", head)
    request.session["output"] = f"{head}\n{detail}".strip()
    if changed:
        ctx_of(request).schedule_restart()
        return back(request, "/system", "ok", "Updated. The bot restarts in a couple of seconds; reload this page after a few seconds.")
    return back(request, "/system", "ok" if ok else "err", head)


@router.post("/system/restart")
async def system_restart(request: Request, s: Sess = Depends(need("owner", post=True))):
    audit(request, s, "system.restart")
    ctx_of(request).schedule_restart()
    return back(request, "/system", "ok", "Restarting. Reload this page after a few seconds.")


@router.post("/logout")
async def logout(request: Request, s: Sess = Depends(need(post=True))):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
