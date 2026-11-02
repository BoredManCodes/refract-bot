import base64
import json
import sqlite3
import unittest
from types import SimpleNamespace

import discord
import itsdangerous
from fastapi.testclient import TestClient

import modtools
from dashboard import create_app
from db import DB, SCHEMA

SECRET = "test-secret"
OWNER, STAFF, ROLEUSER, NOBODY = 1, 2, 3, 4
CSRF = "tok"


class ThreadSafeDB(DB):
    """TestClient runs the app on another thread; in the bot everything shares one event loop thread."""
    def __init__(self):
        self.c = sqlite3.connect(":memory:", check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)


class Role:
    def __init__(self, rid, name, default=False):
        self.id, self.name, self._default, self.managed = rid, name, default, False

    def is_default(self):
        return self._default


class Member:
    def __init__(self, guild, uid, name, roles=()):
        self.guild, self.id, self.name, self.display_name, self.global_name = guild, uid, name, name.title(), None
        self.roles, self.bot, self.joined_at = list(roles), False, None


class Guild:
    name = "Test"
    member_count = 4
    owner_id = OWNER

    def __init__(self):
        self.everyone = Role(100, "@everyone", True)
        self.mods = Role(200, "Mods")
        self.roles = [self.everyone, self.mods]
        self.members = [Member(self, OWNER, "owner"), Member(self, STAFF, "staffer"),
                        Member(self, ROLEUSER, "roleuser", [self.mods]), Member(self, NOBODY, "nobody")]
        self.text_channels, self.categories = [], []

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def fetch_member(self, uid):
        raise discord.HTTPException(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_channel(self, cid):
        return None


def cookie(**data):
    return itsdangerous.TimestampSigner(SECRET).sign(base64.b64encode(json.dumps(data).encode())).decode()


class DashboardTest(unittest.TestCase):
    async def version(self):
        return "abc1234 test commit"

    async def update(self):
        return self.update_result

    def setUp(self):
        self.restarts = []
        self.update_result = (True, "Updated to `def5678`", "def5678 something", True)
        self.db = ThreadSafeDB()
        self.guild = Guild()
        ctx = SimpleNamespace(bot=SimpleNamespace(application_id=1), db=self.db, guild=lambda: self.guild, owner_ids=set(),
                              base_url="http://localhost", client_secret="x", session_secret=SECRET, transcript_base_url="",
                              ticket_kinds={"game": ("Game", "d", None)}, head_admin=lambda g: None,
                              current_version=self.version, perform_update=self.update, schedule_restart=lambda: self.restarts.append(1))
        modtools.db = self.db
        ctx.mod = modtools
        self.client = TestClient(create_app(ctx), follow_redirects=False)

    def as_user(self, uid):
        self.client.cookies.set("dash_session", cookie(uid=uid, csrf=CSRF))

    def post(self, url, **data):
        return self.client.post(url, data={"csrf": CSRF, **data})

    def test_anonymous_is_sent_to_login(self):
        r = self.client.get("/tickets")
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/login"))

    def test_member_without_grants_is_refused(self):
        self.as_user(NOBODY)
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_owner_sees_everything(self):
        self.as_user(OWNER)
        for path in ("/", "/permissions", "/audit", "/settings", "/adminlog", "/members"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_role_grant_reaches_role_members_only(self):
        self.db.add_grant("role", self.guild.mods.id, "tickets.view")
        self.as_user(ROLEUSER)
        self.assertEqual(self.client.get("/tickets").status_code, 200)
        self.as_user(STAFF)
        self.assertEqual(self.client.get("/tickets").status_code, 403)

    def test_user_grant(self):
        self.db.add_grant("user", STAFF, "applications.review")
        self.as_user(STAFF)
        self.assertEqual(self.client.get("/applications").status_code, 200)
        self.assertEqual(self.client.get("/tickets").status_code, 403)

    def test_view_cannot_manage_but_manage_implies_view(self):
        self.db.add_grant("user", STAFF, "tickets.view")
        self.as_user(STAFF)
        self.assertEqual(self.post("/tickets/new", user="x", kind="game", topic="t").status_code, 403)
        self.db.add_grant("user", NOBODY, "tickets.manage")
        self.as_user(NOBODY)
        self.assertEqual(self.client.get("/tickets").status_code, 200)

    def test_post_needs_csrf_token(self):
        self.as_user(OWNER)
        r = self.client.post("/permissions/grant", data={"kind": "role", "role_id": "200", "perm": "tickets.view"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/permissions/grant", data={"csrf": "wrong", "kind": "role", "role_id": "200", "perm": "tickets.view"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.db.grants(), [])

    def test_only_owner_manages_permissions(self):
        self.db.add_grant("user", STAFF, "settings.edit")
        self.as_user(STAFF)
        self.assertEqual(self.client.get("/permissions").status_code, 403)
        self.assertEqual(self.post("/permissions/grant", kind="user", user="4", perm="settings.edit").status_code, 403)
        self.assertEqual(self.client.get("/audit").status_code, 403)

    def test_owner_grants_and_revokes_and_it_is_audited(self):
        self.as_user(OWNER)
        self.assertEqual(self.post("/permissions/grant", kind="role", role_id="200", perm="tickets.view").status_code, 303)
        self.assertEqual(self.post("/permissions/grant", kind="user", user="roleuser", perm="members.view").status_code, 303)
        self.assertEqual({(g["kind"], g["subject_id"], g["perm"]) for g in self.db.grants()},
                         {("role", 200, "tickets.view"), ("user", ROLEUSER, "members.view")})
        self.post("/permissions/revoke", kind="role", subject_id="200", perm="tickets.view")
        self.assertEqual(len(self.db.grants()), 1)
        self.assertEqual([r["action"] for r in self.db.recent_audit()], ["permissions.revoke", "permissions.grant", "permissions.grant"])

    def test_everyone_role_cannot_be_granted(self):
        self.as_user(OWNER)
        self.post("/permissions/grant", kind="role", role_id="100", perm="tickets.view")
        self.assertEqual(self.db.grants(), [])

    def test_unknown_permission_name_is_ignored(self):
        self.as_user(OWNER)
        self.post("/permissions/grant", kind="role", role_id="200", perm="owner")
        self.assertEqual(self.db.grants(), [])

    def test_owner_ids_env_counts_as_owner(self):
        ctx = self.client.app.state.ctx
        ctx.owner_ids = {NOBODY}
        self.as_user(NOBODY)
        self.assertEqual(self.client.get("/permissions").status_code, 200)

    def test_settings_rejects_ids_not_in_the_server(self):
        self.as_user(OWNER)
        r = self.post("/settings", admin_role_id="999")
        self.assertEqual(r.status_code, 303)
        self.assertIsNone(self.db.get_int("admin_role_id"))
        self.post("/settings", admin_role_id="200")
        self.assertEqual(self.db.get_int("admin_role_id"), 200)
        self.post("/settings", admin_role_id="")
        self.assertIsNone(self.db.get_int("admin_role_id"))

    def test_member_search_options_are_escaped(self):
        self.guild.members[3].display_name = "<b>x</b>"
        self.as_user(OWNER)
        body = self.client.get("/members/options", params={"q": "nobody"}).text
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", body)
        self.assertNotIn("<b>x", body)

    def test_session_of_someone_who_left_is_refused(self):
        self.as_user(999)
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_oauth_login_redirects_to_discord_with_our_callback(self):
        r = self.client.get("/auth/discord")
        self.assertEqual(r.status_code, 302)
        loc = r.headers["location"]
        self.assertTrue(loc.startswith("https://discord.com/oauth2/authorize?"))
        self.assertIn("client_id=1", loc)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%2Fauth%2Fcallback", loc)
        self.assertIn("scope=identify", loc)

    def test_every_page_renders_with_data(self):
        aid = self.db.add_application(ROLEUSER, "Steve", {"Why": "<b>because</b>"})
        tid = self.db.add_ticket(ROLEUSER, "game", "lost items")
        self.db.close_ticket(tid, OWNER, "tok123")
        self.db.save_transcript("tok123", "<html>transcript</html>")
        self.db.add_election("voting", __import__("db").now(), __import__("db").now(), None, True)
        self.as_user(OWNER)
        for path in (f"/tickets/{tid}", f"/tickets?status=closed", "/tickets?status=all&q=lost", "/applications?status=all",
                     "/elections", "/members?q=role", f"/tickets/{tid}/transcript"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
        self.assertIn("&lt;b&gt;because", self.client.get("/applications").text)
        self.assertIn("transcript", self.client.get(f"/tickets/{tid}/transcript").text)
        self.assertIn("frame-ancestors", self.client.get(f"/tickets/{tid}/transcript").headers["content-security-policy"])
        self.assertEqual(self.client.get("/tickets/999").status_code, 404)

    def test_flash_survives_the_polling_endpoint(self):
        tid = self.db.add_ticket(ROLEUSER, "game", "x")
        self.as_user(OWNER)
        self.post("/adminlog", action="", reason="")  # sets an error flash
        self.client.get(f"/tickets/{tid}/messages")
        self.assertIn("Fill in", self.client.get("/adminlog").text)

    def test_system_page_and_buttons_are_owner_only(self):
        self.db.add_grant("user", STAFF, "settings.edit")
        self.as_user(STAFF)
        self.assertEqual(self.client.get("/system").status_code, 403)
        self.assertEqual(self.post("/system/restart").status_code, 403)
        self.assertEqual(self.post("/system/update").status_code, 403)
        self.assertEqual(self.restarts, [])

    def test_restart_button_schedules_a_restart_and_is_audited(self):
        self.as_user(OWNER)
        page = self.client.get("/system").text
        self.assertIn("abc1234 test commit", page)
        self.assertEqual(self.post("/system/restart").status_code, 303)
        self.assertEqual(len(self.restarts), 1)
        self.assertEqual(self.db.recent_audit()[0]["action"], "system.restart")

    def test_update_restarts_only_when_code_changed(self):
        self.as_user(OWNER)
        self.post("/system/update")
        self.assertEqual(len(self.restarts), 1)
        self.assertIn("def5678 something", self.client.get("/system").text)
        self.update_result = (True, "Already up to date.", "", False)
        self.post("/system/update")
        self.assertEqual(len(self.restarts), 1)
        self.update_result = (False, "pip install failed, rolled back", "boom", False)
        self.post("/system/update")
        self.assertEqual(len(self.restarts), 1)

    def test_restart_needs_csrf(self):
        self.as_user(OWNER)
        self.assertEqual(self.client.post("/system/restart").status_code, 400)
        self.assertEqual(self.restarts, [])


if __name__ == "__main__":
    unittest.main()
