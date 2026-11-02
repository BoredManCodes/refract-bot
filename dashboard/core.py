import hmac
import secrets
from dataclasses import dataclass

import discord
from fastapi import Request

PERMS = {
    "tickets.view": "View tickets and transcripts",
    "tickets.manage": "Open, close, reply to and edit tickets, post the ticket panel (includes view)",
    "applications.review": "See and decide staff applications",
    "elections.manage": "Start, schedule and run elections, appoint the Head Admin",
    "members.view": "Browse members and invite stats",
    "adminlog.post": "Post to the public admin log",
    "settings.edit": "Change which roles and channels the bot uses",
}
IMPLIED_BY = {"tickets.view": {"tickets.manage"}}

NAV = [("/", "Overview", None),
       ("/tickets", "Tickets", "tickets.view"),
       ("/applications", "Applications", "applications.review"),
       ("/elections", "Elections", "elections.manage"),
       ("/members", "Members", "members.view"),
       ("/adminlog", "Admin log", "adminlog.post"),
       ("/settings", "Settings", "settings.edit"),
       ("/permissions", "Permissions", "owner"),
       ("/system", "System", "owner"),
       ("/audit", "Audit log", "owner")]


class Access:
    def __init__(self, owner, perms):
        self.owner, self.perms = owner, set(perms)

    @property
    def any(self):
        return self.owner or bool(self.perms)

    def can(self, perm):
        if perm is None:
            return True
        if perm == "owner":
            return self.owner
        return self.owner or perm in self.perms or bool(IMPLIED_BY.get(perm, set()) & self.perms)


def access_for(db, member, owner_ids):
    owner = member.id == member.guild.owner_id or member.id in owner_ids
    return Access(owner, db.perms_for(member.id, [r.id for r in member.roles if not r.is_default()]))


@dataclass
class Sess:
    member: discord.Member
    access: Access


class LoginRequired(Exception):
    pass


class Denied(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


async def load_member(ctx, uid):
    guild = ctx.guild()
    if guild is None:
        raise Denied(503, "The bot is starting up. Try again in a minute.")
    member = guild.get_member(uid)
    if member is None:
        try:
            member = await guild.fetch_member(uid)
        except discord.HTTPException:
            member = None
    return member


def need(perm=None, post=False):
    """Route dependency: logged in, allowed to do `perm` ('owner' for owners only), and for POSTs a valid CSRF token."""
    async def dep(request: Request):
        ctx = request.app.state.ctx
        uid = request.session.get("uid")
        if not uid:
            if request.method == "GET":
                request.session["next"] = request.url.path
            raise LoginRequired()
        member = await load_member(ctx, uid)
        if member is None:
            raise Denied(403, "You are not a member of this server.")
        access = access_for(ctx.db, member, ctx.owner_ids)
        if not access.any:
            raise Denied(403, "Your account has no dashboard access. Ask the server owner.")
        if not access.can(perm):
            raise Denied(403, "You do not have permission for this page.")
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(24)
        if post:
            form = await request.form()
            if not hmac.compare_digest(str(form.get("csrf", "")).encode(), request.session["csrf"].encode()):
                raise Denied(400, "The form expired. Go back, reload the page and try again.")
        return Sess(member, access)
    return dep


def flash(request, kind, message):
    request.session["flash"] = [kind, message]


def resolve_member(guild, text):
    """Member from an id, @mention, exact username/display name, or a unique prefix of one."""
    text = (text or "").strip().lstrip("@").strip("<>@!")
    if not text:
        return None
    if text.isdigit():
        return guild.get_member(int(text))
    low = text.lower()
    names = lambda m: {m.name.lower(), m.display_name.lower(), (m.global_name or "").lower()}
    exact = [m for m in guild.members if low in names(m)]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    starts = [m for m in guild.members if any(n.startswith(low) for n in names(m))]
    return starts[0] if len(starts) == 1 else None
