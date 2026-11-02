import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from fastapi.testclient import TestClient

import automod
import modtools
import suggestions
from dashboard import create_app
from test_dashboard import CSRF, NOBODY, OWNER, ROLEUSER, SECRET, STAFF, Guild, Member, ThreadSafeDB, cookie


Member.mention = property(lambda self: f"<@{self.id}>")


def channel(cid, name):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id, ch.name = cid, name
    return ch


class ModGuild(Guild):
    def __init__(self):
        super().__init__()
        self.me = Member(self, 1000, "bot")
        self.text_channels = [channel(300, "general"), channel(301, "logs")]

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)


class ModDashboardTest(unittest.TestCase):
    def setUp(self):
        self.db = ThreadSafeDB()
        self.guild = ModGuild()
        modtools.db = self.db
        self.user_found = SimpleNamespace(id=77, bot=False, __str__=lambda self: "gone#0")
        self.settle = AsyncMock(return_value="Approved")
        self.panel = AsyncMock()
        ctx = SimpleNamespace(
            bot=SimpleNamespace(application_id=1, fetch_user=AsyncMock(return_value=self.user_found)), db=self.db,
            guild=lambda: self.guild, owner_ids=set(), base_url="http://localhost", client_secret="x", session_secret=SECRET,
            transcript_base_url="", ticket_kinds={}, head_admin=lambda g: None, mod=modtools,
            suggestions=SimpleNamespace(settle=self.settle, post_panel=self.panel))
        self.client = TestClient(create_app(ctx), follow_redirects=False)
        self.as_user(OWNER)
        for name in ("log_to", "apply", "start_raid", "stop_raid", "lockdown_server", "lift_lockdown"):
            p = patch.object(modtools, name, AsyncMock())
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        self.apply.return_value = (9, "")
        self.lockdown_server.return_value = (4, 0)
        self.lift_lockdown.return_value = 4
        self.stop_raid.return_value = True

    def as_user(self, uid):
        self.client.cookies.clear()  # a response may have re-set the session cookie; a second one would be sent first
        self.client.cookies.set("dash_session", cookie(uid=uid, csrf=CSRF))

    def post(self, url, **data):
        return self.client.post(url, data={"csrf": CSRF, **data})

    def flash(self):
        return self.client.get("/").text

    # ---- access ----

    def test_view_sees_cases_but_cannot_act_or_configure(self):
        self.db.add_grant("user", STAFF, "moderation.view")
        self.as_user(STAFF)
        for path in ("/moderation", "/suggestions", f"/moderation/user/{ROLEUSER}"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.post("/moderation/act", user="3", kind="warn", reason="x").status_code, 403)
        self.assertEqual(self.post("/suggestions/1/approved").status_code, 403)
        for path in ("/automod", "/lockdown", "/modlogs"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertNotIn("Take an action", self.client.get("/moderation").text)

    def test_act_implies_view_and_admin_implies_act(self):
        self.db.add_grant("user", STAFF, "moderation.act")
        self.as_user(STAFF)
        self.assertEqual(self.client.get("/moderation").status_code, 200)
        self.assertEqual(self.post("/moderation/act", user="3", kind="warn", reason="x").status_code, 303)
        self.assertEqual(self.client.get("/automod").status_code, 403)
        self.assertEqual(self.post("/moderation/case/1/delete").status_code, 403)
        self.db.add_grant("user", NOBODY, "moderation.admin")
        self.as_user(NOBODY)
        self.assertEqual(self.client.get("/moderation").status_code, 200)
        self.assertEqual(self.post("/moderation/act", user="3", kind="warn", reason="x").status_code, 303)
        self.assertEqual(self.client.get("/automod").status_code, 200)

    def test_every_new_page_renders_for_the_owner(self):
        case = self.db.add_infraction(ROLEUSER, OWNER, "warn", "<b>spam</b>")
        self.db.add_infraction(ROLEUSER, 1000, "mute", "auto", auto=True, rule="spam")
        sid = self.db.add_suggestion(ROLEUSER, "more <i>cake</i>", True)
        self.db.vote_suggestion(sid, OWNER, 1)
        self.db.put("sticky_exclude", json.dumps([200]))
        c = modtools.cfg()
        c["rules"]["words"]["list"].append("badword")
        c["exempt_channels"].append(300)
        c["rules"]["spam"]["roles"] = [200]
        modtools.save(c)
        for path in ("/", "/moderation", "/moderation?kind=warn&q=roleuser", "/moderation?q=3", "/moderation?q=nobodyatall", f"/moderation/user/{ROLEUSER}",
                     "/suggestions", "/suggestions?status=all", "/automod", "/lockdown", "/modlogs"):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        body = self.client.get("/moderation").text
        self.assertIn("&lt;b&gt;spam", body)
        self.assertNotIn("<b>spam", body)
        self.assertIn("AutoMod", body)
        self.assertIn("more &lt;i&gt;cake", self.client.get("/suggestions").text)
        self.assertIn("badword", self.client.get("/automod").text)
        self.assertIsNotNone(case)

    def test_post_needs_csrf(self):
        for url in ("/moderation/act", "/automod/toggle", "/lockdown/start", "/raid/stop", "/modlogs"):
            self.assertEqual(self.client.post(url, data={}).status_code, 400, url)
        self.apply.assert_not_awaited()

    # ---- cases ----

    def test_warn_goes_through_apply_and_is_audited(self):
        r = self.post("/moderation/act", user="roleuser", kind="warn", reason="spamming")
        self.assertEqual((r.status_code, r.headers["location"]), (303, f"/moderation/user/{ROLEUSER}"))
        args, kw = self.apply.await_args
        self.assertEqual((args[1], args[2].id, args[3].id, args[4]), ("warn", ROLEUSER, OWNER, "spamming"))
        self.assertEqual(self.db.recent_audit()[0]["action"], "mod.warn")
        self.assertIn("Case #9", self.client.get(f"/moderation/user/{ROLEUSER}").text)

    def test_mute_defaults_to_an_hour_and_reads_durations(self):
        self.post("/moderation/act", user="3", kind="mute", reason="x")
        self.assertEqual(self.apply.await_args.kwargs["seconds"], 3600)
        self.post("/moderation/act", user="3", kind="mute", duration="2d", reason="x")
        self.assertEqual(self.apply.await_args.kwargs["seconds"], 2 * 86400)
        self.post("/moderation/act", user="3", kind="ban", reason="x", delete_days="9")
        self.assertEqual((self.apply.await_args.kwargs["seconds"], self.apply.await_args.kwargs["delete_seconds"]), (None, 7 * 86400))

    def test_bad_input_never_reaches_apply(self):
        self.post("/moderation/act", user="3", kind="mute", duration="soon", reason="x")
        self.post("/moderation/act", user="3", kind="explode", reason="x")
        self.post("/moderation/act", user="3", kind="warn", reason="  ")
        self.post("/moderation/act", user="nobody-like-this", kind="warn", reason="x")
        self.apply.assert_not_awaited()

    def test_mod_error_is_shown_not_raised(self):
        self.apply.side_effect = modtools.ModError("Their highest role is not below yours.")
        r = self.post("/moderation/act", user="3", kind="kick", reason="x")
        self.assertEqual(r.status_code, 303)
        self.assertIn("highest role", self.client.get("/moderation").text)
        self.assertEqual(self.db.recent_audit(), [])

    def test_ban_by_id_reaches_people_who_left(self):
        self.post("/moderation/act", user="77", kind="ban", reason="x")
        self.assertEqual(self.apply.await_args.args[2].id, 77)
        self.user_found.bot = True
        self.apply.reset_mock()
        self.post("/moderation/act", user="77", kind="ban", reason="x")
        self.apply.assert_not_awaited()

    def test_note_is_stored_without_apply(self):
        self.post("/moderation/act", user="3", kind="note", reason="watch this one")
        self.assertEqual([(r["kind"], r["reason"]) for r in self.db.infractions_for(ROLEUSER)], [("note", "watch this one")])
        self.apply.assert_not_awaited()

    def test_edit_reason_clear_warns_and_delete(self):
        c = self.db.add_infraction(ROLEUSER, OWNER, "warn", "old")
        self.post(f"/moderation/case/{c}/reason", reason="new")
        self.assertEqual(self.db.infraction(c)["reason"], "new")
        self.post(f"/moderation/user/{ROLEUSER}/clearwarns")
        self.assertFalse(self.db.infraction(c)["active"])
        self.post(f"/moderation/case/{c}/delete")
        self.assertIsNone(self.db.infraction(c))
        self.assertEqual([a["action"] for a in self.db.recent_audit()][:3], ["mod.case_delete", "mod.clearwarns", "mod.reason"])
        self.assertEqual(self.post("/moderation/case/999/delete").status_code, 404)

    # ---- suggestions ----

    def test_deciding_a_suggestion(self):
        sid = self.db.add_suggestion(ROLEUSER, "idea", False)
        self.assertEqual(self.post(f"/suggestions/{sid}/approved", reason="good one").status_code, 303)
        args = self.settle.await_args.args
        self.assertEqual((args[1].id, args[2], args[3], args[4]), (OWNER, sid, "approved", "good one"))
        self.assertEqual(self.post(f"/suggestions/{sid}/exploded").status_code, 404)
        self.settle.return_value = None
        self.assertEqual(self.post("/suggestions/99/denied").status_code, 404)

    def test_suggestion_panel_button(self):
        self.assertEqual(self.post("/suggestions/panel", channel_id="300").status_code, 303)
        self.panel.assert_awaited_once()
        self.assertEqual(self.panel.await_args.args[0].id, 300)
        self.post("/suggestions/panel", channel_id="999")
        self.assertEqual(self.panel.await_count, 1)
        self.assertIn("Post the suggestion panel", self.client.get("/suggestions").text)
        self.db.add_grant("user", STAFF, "moderation.act")
        self.as_user(STAFF)
        self.assertEqual(self.post("/suggestions/panel", channel_id="300").status_code, 403)
        self.assertNotIn("Post the suggestion panel", self.client.get("/suggestions").text)

    # ---- AutoMod ----

    def test_rule_edit_persists_and_bad_values_are_refused(self):
        self.post("/automod/rule/spam", enabled="0", action="kick", duration="2h", limit="9", seconds="12")
        r = modtools.cfg()["rules"]["spam"]
        self.assertEqual((r["enabled"], r["action"], r["duration"], r["limit"], r["seconds"]), (False, "kick", 7200, 9, 12))
        before = modtools.cfg()
        for bad in ({"limit": "0"}, {"limit": "x"}, {"action": "nuke"}, {"duration": "later"}, {"mode": "allowlist"}, {"seconds": "9999"}):
            self.post("/automod/rule/spam", **bad)
        self.assertEqual(modtools.cfg(), before)
        self.post("/automod/rule/links", mode="allowlist")
        self.assertEqual(modtools.cfg()["rules"]["links"]["mode"], "allowlist")
        self.assertEqual(self.post("/automod/rule/emojis", seconds="5").status_code, 303)
        self.assertEqual(modtools.cfg()["rules"]["emojis"], before["rules"]["emojis"])

    def test_mute_length_is_capped_at_the_discord_limit(self):
        self.post("/automod/rule/spam", duration="90d")
        self.assertEqual(modtools.cfg()["rules"]["spam"]["duration"], automod.MAX_TIMEOUT)

    def test_toggle_and_options(self):
        self.post("/automod/toggle", enabled="1")
        self.assertTrue(modtools.cfg()["enabled"])
        self.post("/automod/options", dm="0", notice_seconds="3", warn_window_days="14")
        c = modtools.cfg()
        self.assertEqual((c["dm"], c["notice_seconds"], c["warn_window_days"]), (False, 3, 14))
        self.post("/automod/options", dm="1", notice_seconds="999", warn_window_days="14")
        self.assertEqual(modtools.cfg()["notice_seconds"], 3)

    def test_lists_add_remove_and_normalise(self):
        self.post("/automod/list", kind="word", value="  BadWord ")
        self.post("/automod/list", kind="word", value="badword")
        self.assertEqual(modtools.cfg()["rules"]["words"]["list"], ["badword"])
        self.post("/automod/list", kind="blocked_link", value="https://www.Evil.example/path?x=1")
        self.assertIn("evil.example", modtools.cfg()["rules"]["links"]["block"])
        self.post("/automod/list", kind="word", value="BADWORD", op="remove")
        self.assertEqual(modtools.cfg()["rules"]["words"]["list"], [])
        self.assertEqual(self.post("/automod/list", kind="nonsense", value="x").status_code, 400)
        self.assertNotIn("badword", " ".join(a["detail"] for a in self.db.recent_audit()))

    def test_ladder_add_replace_remove(self):
        self.post("/automod/ladder", warnings="4", action="ban")
        self.post("/automod/ladder", warnings="3", action="kick")
        steps = {s["count"]: s for s in modtools.cfg()["escalation"]}
        self.assertEqual((steps[4]["action"], steps[3]["action"], steps[3]["seconds"]), ("ban", "kick", 0))
        self.post("/automod/ladder", warnings="4", remove="1")
        self.assertNotIn(4, {s["count"] for s in modtools.cfg()["escalation"]})
        before = modtools.cfg()
        self.post("/automod/ladder", warnings="0", action="mute")
        self.post("/automod/ladder", warnings="2", action="explode")
        self.assertEqual(modtools.cfg(), before)

    def test_exemptions_global_and_per_rule(self):
        self.post("/automod/exempt", kind="channel", target_id="300", rule="all")
        self.post("/automod/exempt", kind="role", target_id="200", rule="caps")
        c = modtools.cfg()
        self.assertEqual((c["exempt_channels"], c["rules"]["caps"]["roles"]), ([300], [200]))
        self.post("/automod/exempt", kind="channel", target_id="12345", rule="all")
        self.assertEqual(modtools.cfg()["exempt_channels"], [300])
        self.post("/automod/exempt", kind="channel", target_id="300", rule="all", remove="1")
        self.assertEqual(modtools.cfg()["exempt_channels"], [])
        self.assertEqual(self.post("/automod/exempt", kind="role", target_id="200", rule="nope").status_code, 400)

    # ---- lockdown, raid, join gate ----

    def test_lockdown_start_and_lift(self):
        self.post("/lockdown/start", reason="raid", announce="0")
        args = self.lockdown_server.await_args.args
        self.assertEqual((args[1].id, args[2], args[3]), (OWNER, "raid", False))
        self.db.put("lockdown", json.dumps({"by": OWNER, "at": "2026-11-02T10:00:00+00:00", "reason": "raid"}))
        page = self.client.get("/lockdown").text
        self.assertIn("locked down", page)
        self.lockdown_server.reset_mock()
        self.post("/lockdown/start")
        self.lockdown_server.assert_not_awaited()
        self.post("/lockdown/lift")
        self.lift_lockdown.assert_awaited_once()
        self.db.clear("lockdown")
        self.lift_lockdown.reset_mock()
        self.post("/lockdown/lift")
        self.lift_lockdown.assert_not_awaited()
        self.assertEqual([a["action"] for a in self.db.recent_audit()], ["lockdown.lift", "lockdown.start"])

    def test_lockdown_exempt_channels(self):
        self.post("/lockdown/exempt", channel_id="300")
        self.post("/lockdown/exempt", channel_id="12345")
        self.assertEqual(modtools.lockdown_exempt(), {300})
        self.post("/lockdown/exempt", channel_id="300", remove="1")
        self.assertEqual(modtools.lockdown_exempt(), set())

    def test_raid_start_stop_and_config(self):
        self.post("/raid/start", action="ban", minutes="15", lockdown="1")
        args = self.start_raid.await_args.args
        self.assertEqual(args[2:5], ("ban", 15, True))
        self.start_raid.reset_mock()
        self.post("/raid/start", action="explode", minutes="15")
        self.post("/raid/start", action="kick", minutes="99999")
        self.start_raid.assert_not_awaited()
        self.post("/raid/stop")
        self.stop_raid.assert_awaited_once()
        self.post("/raid/config", enabled="0", joins="12", seconds="30", minutes="20", action="mute", lockdown="1")
        r = modtools.cfg()["raid"]
        self.assertEqual((r["enabled"], r["joins"], r["seconds"], r["minutes"], r["action"], r["lockdown"]), (False, 12, 30, 20, "mute", True))
        self.post("/raid/config", joins="1")
        self.assertEqual(modtools.cfg()["raid"]["joins"], 12)

    def test_raid_page_shows_running_raid(self):
        self.db.put("raidmode", json.dumps({"on": True, "action": "kick", "until": None}))
        self.assertIn("Stays on until turned off", self.client.get("/lockdown").text)

    def test_join_gate(self):
        self.post("/joingate", min_age_days="7", action="ban", no_avatar="1")
        g = modtools.cfg()["joingate"]
        self.assertEqual((g["min_age_days"], g["action"], g["no_avatar"]), (7, "ban", True))
        self.post("/joingate", min_age_days="9999")
        self.post("/joingate", min_age_days="x")
        self.assertEqual(modtools.cfg()["joingate"]["min_age_days"], 7)

    # ---- logs, sticky, suggestion setup ----

    def test_log_channels(self):
        self.post("/modlogs", logch_messages="301", logch_voice="300")
        self.assertEqual((self.db.get_int("logch_messages"), self.db.get_int("logch_voice")), (301, 300))
        self.post("/modlogs", logch_messages="999")
        self.assertEqual(self.db.get_int("logch_messages"), 301)
        self.post("/modlogs", logch_messages="")
        self.assertIsNone(self.db.get_int("logch_messages"))
        self.assertEqual(self.db.get_int("logch_voice"), 300)

    def test_sticky_roles(self):
        self.post("/modlogs/sticky", enabled="1", exclude_role="200")
        self.assertEqual((self.db.get("sticky"), json.loads(self.db.get("sticky_exclude"))), ("1", [200]))
        self.post("/modlogs/sticky", enabled="0", exclude_role="999")
        self.assertEqual((self.db.get("sticky"), json.loads(self.db.get("sticky_exclude"))), ("0", [200]))
        self.post("/modlogs/sticky", enabled="0", remove_role="200")
        self.assertEqual(json.loads(self.db.get("sticky_exclude")), [])

    def test_suggestion_setup(self):
        self.post("/modlogs/suggestions", channel_id="300", log_channel_id="301", anonymous="1")
        self.assertEqual((self.db.get_int("suggestions_channel_id"), self.db.get_int("suggestion_log_channel_id"), self.db.get("suggest_anonymous")),
                         (300, 301, "1"))
        self.post("/modlogs/suggestions", channel_id="999", anonymous="0")
        self.assertEqual(self.db.get_int("suggestions_channel_id"), 300)


class Settle(unittest.TestCase):
    """suggestions.settle is shared by the slash commands and the dashboard."""

    def setUp(self):
        self.db = ThreadSafeDB()
        suggestions.init(MagicMock(), self.db, lambda m: True, lambda m: True)
        self.sid = self.db.add_suggestion(5, "more cake", False)
        self.post_msg = MagicMock()
        self.post_msg.edit, self.post_msg.jump_url = AsyncMock(), "https://x/jump"
        self.post_msg.thread = MagicMock()
        self.post_msg.thread.send, self.post_msg.thread.edit = AsyncMock(), AsyncMock()
        self.channel = MagicMock()
        self.channel.id, self.channel.fetch_message = 300, AsyncMock(return_value=self.post_msg)
        self.db.set_suggestion_message(self.sid, 300, 800)
        self.log = MagicMock()
        self.log.send = AsyncMock()
        self.author = MagicMock()
        self.author.send = AsyncMock()
        self.guild = MagicMock()
        self.guild.get_channel = lambda cid: {300: self.channel, 301: self.log}.get(cid)
        self.guild.get_member = lambda uid: self.author
        self.db.put("suggestion_log_channel_id", 301)
        self.by = MagicMock()
        self.by.id, self.by.mention = 9, "<@9>"

    def test_decision_is_recorded_posted_logged_and_sent(self):
        label = asyncio.run(suggestions.settle(self.guild, self.by, self.sid, "approved", "good one"))
        self.assertEqual(label, "Approved")
        row = self.db.suggestion(self.sid)
        self.assertEqual((row["status"], row["decided_by"], row["reason"]), ("approved", 9, "good one"))
        self.post_msg.edit.assert_awaited_once()
        self.post_msg.thread.edit.assert_awaited_once_with(archived=True, locked=True)
        self.log.send.assert_awaited_once()
        self.author.send.assert_awaited_once()

    def test_new_suggestion_gets_a_thread(self):
        self.db.put("suggestions_channel_id", 300)
        msg = MagicMock()
        msg.id, msg.jump_url, msg.create_thread = 801, "https://x/j", AsyncMock()
        self.channel.send = AsyncMock(return_value=msg)
        self.guild.get_member = lambda uid: None
        i = MagicMock()
        i.guild, i.user.id = self.guild, 42
        i.response.send_message = AsyncMock()
        asyncio.run(suggestions.submit(i, "a brand new idea"))
        name = msg.create_thread.await_args.kwargs["name"]
        self.assertTrue(name.startswith("Suggestion #"))

    def test_panel_posts_an_embed_with_the_button(self):
        ch = MagicMock()
        ch.send = AsyncMock()
        asyncio.run(suggestions.post_panel(ch))
        kw = ch.send.await_args.kwargs
        self.assertEqual(kw["embed"].title, "Got an idea?")
        self.assertEqual([c.custom_id for c in kw["view"].children], ["sug:new"])

    def test_unknown_suggestion_is_none_and_touches_nothing(self):
        self.assertIsNone(asyncio.run(suggestions.settle(self.guild, self.by, 999, "denied", "")))
        self.log.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
