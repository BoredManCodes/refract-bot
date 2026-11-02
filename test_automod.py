import unittest
from datetime import timedelta

import automod
from db import DB, now


def cfg(**rules):
    c = automod.load(None)
    c["enabled"] = True
    for name, patch in rules.items():
        c["rules"][name].update(patch)
    return c


def msg(content="hello", i=1, author=10, channel=5, roles=(), mentions=0, attachments=0, ts=1000.0):
    return automod.Msg(i, author, channel, frozenset(roles), content, mentions, attachments, ts)


def check(c, m, tracker=None):
    return automod.evaluate(c, m, tracker or automod.Tracker())


class Durations(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(automod.parse_duration("90s"), 90)
        self.assertEqual(automod.parse_duration("10m"), 600)
        self.assertEqual(automod.parse_duration("1h30m"), 5400)
        self.assertEqual(automod.parse_duration("1w"), 604800)
        self.assertEqual(automod.parse_duration(" 2 D "), 172800)

    def test_rejects(self):
        for bad in ("", "10", "abc", "5x", "0m", "1h-"):
            with self.assertRaises(ValueError):
                automod.parse_duration(bad)

    def test_format(self):
        self.assertEqual(automod.fmt_duration(5400), "1h30m")
        self.assertEqual(automod.fmt_duration(90061), "1d1h1m")
        self.assertEqual(automod.fmt_duration(0), "0s")


class Config(unittest.TestCase):
    def test_defaults_and_merge(self):
        c = automod.load('{"enabled":true,"rules":{"spam":{"limit":9},"bogus":{"x":1}},"junk":1}')
        self.assertTrue(c["enabled"])
        self.assertEqual(c["rules"]["spam"]["limit"], 9)
        self.assertEqual(c["rules"]["spam"]["seconds"], 5)
        self.assertNotIn("bogus", c["rules"])
        self.assertNotIn("junk", c)

    def test_bad_json(self):
        self.assertFalse(automod.load("{nope")["enabled"])
        self.assertFalse(automod.load("[1]")["enabled"])

    def test_defaults_not_shared(self):
        automod.load(None)["rules"]["words"]["list"].append("x")
        self.assertEqual(automod.load(None)["rules"]["words"]["list"], [])


class Words(unittest.TestCase):
    def test_whole_word_only(self):
        self.assertIsNone(automod.blocked_word("a classic assassin", ["ass"]))
        self.assertEqual(automod.blocked_word("you ass", ["ass"]), "ass")

    def test_evasion(self):
        for text in ("b4dword", "BADWORD", "baaadword", "bådword", "ba​dword"):
            self.assertEqual(automod.blocked_word(text, ["badword"]), "badword", text)

    def test_wildcard(self):
        self.assertEqual(automod.blocked_word("badwords here", ["badword*"]), "badword*")

    def test_loose_matches_inside_and_spaced(self):
        self.assertEqual(automod.blocked_word("xxbadwordxx", ["~badword"]), "~badword")
        self.assertEqual(automod.blocked_word("b.a.d w-o-r-d", ["~badword"]), "~badword")

    def test_empty_entry_ignored(self):
        self.assertIsNone(automod.blocked_word("anything", ["", "~", "  "]))


class Links(unittest.TestCase):
    def test_domains(self):
        self.assertEqual(automod.domains("see https://www.Example.com/a?b=1 and bad.gg/x"), ["example.com", "bad.gg"])
        self.assertEqual(automod.domains("notes.txt and hello.world"), [])

    def test_subdomain_match(self):
        self.assertTrue(automod.domain_matches("a.evil.com", ["evil.com"]))
        self.assertFalse(automod.domain_matches("notevil.com", ["evil.com"]))

    def test_invites(self):
        self.assertEqual(automod.invite_codes("join discord.gg/abc123 or https://discord.com/invite/XyZ"), ["abc123", "XyZ"])

    def test_blocklist_rule(self):
        c = cfg(links={"block": ["evil.com"]})
        self.assertEqual(check(c, msg("go to http://evil.com/x")).rule, "links")
        self.assertIsNone(check(c, msg("go to https://good.org")))

    def test_allowlist_rule(self):
        c = cfg(links={"mode": "allowlist", "allow": ["good.org"]})
        self.assertEqual(check(c, msg("https://other.net")).rule, "links")
        self.assertIsNone(check(c, msg("https://sub.good.org/page")))

    def test_invite_rule(self):
        self.assertEqual(check(cfg(), msg("discord.gg/spam")).rule, "invites")
        self.assertIsNone(check(cfg(invites={"allow": ["spam"]}), msg("discord.gg/spam")))


class Rules(unittest.TestCase):
    def test_master_switch(self):
        c = cfg()
        c["enabled"] = False
        self.assertIsNone(check(c, msg("discord.gg/x")))

    def test_mentions(self):
        c = cfg(mentions={"limit": 3})
        self.assertIsNone(check(c, msg(mentions=3)))
        self.assertEqual(check(c, msg(mentions=4)).rule, "mentions")

    def test_emojis(self):
        c = cfg(emojis={"limit": 3})
        self.assertEqual(check(c, msg("<:a:1><:b:2><a:c:3><:d:4>")).rule, "emojis")
        self.assertIsNone(check(c, msg("<:a:1><:b:2>")))

    def test_caps(self):
        c = cfg(caps={"limit": 70, "min_length": 10})
        self.assertEqual(check(c, msg("STOP SHOUTING AT EVERYONE")).rule, "caps")
        self.assertIsNone(check(c, msg("HI THERE")))  # under min length
        self.assertIsNone(check(c, msg("Normal sentence with a Few Capitals")))
        self.assertIsNone(check(c, msg("https://EXAMPLE.COM/PATH <@12345> short")))

    def test_newlines(self):
        c = cfg(newlines={"limit": 5})
        self.assertEqual(check(c, msg("\n".join("x" * 6))).rule, "newlines")
        self.assertIsNone(check(c, msg("\n".join("x" * 5))))

    def test_zalgo(self):
        z = "h" + "̀́̂̃̄̅̆̇̈̉" + "i"
        self.assertEqual(check(cfg(), msg(z)).rule, "zalgo")
        self.assertIsNone(check(cfg(), msg("café naïve")))

    def test_spam_collects_burst(self):
        c, t = cfg(spam={"limit": 3, "seconds": 5}), automod.Tracker()
        hits = [check(c, msg(f"m{i}", i=i, ts=1000 + i), t) for i in range(4)]
        self.assertEqual([h and h.rule for h in hits], [None, None, None, "spam"])
        self.assertEqual({d[1] for d in hits[3].delete}, {0, 1, 2, 3})

    def test_spam_window_expires(self):
        c, t = cfg(spam={"limit": 3, "seconds": 5}), automod.Tracker()
        for i in range(4):
            self.assertIsNone(check(c, msg(f"m{i}", i=i, ts=1000 + i * 10), t))

    def test_spam_is_per_author(self):
        c, t = cfg(spam={"limit": 2, "seconds": 5}), automod.Tracker()
        for a in range(5):
            self.assertIsNone(check(c, msg("hi", i=a, author=a, ts=1000), t))

    def test_duplicates(self):
        c, t = cfg(duplicates={"limit": 3, "seconds": 30}, spam={"enabled": False}), automod.Tracker()
        hits = [check(c, msg("Buy   NOW", i=i, ts=1000 + i), t) for i in range(3)]
        self.assertEqual(hits[2].rule, "duplicates")
        self.assertEqual(len(hits[2].delete), 3)
        self.assertIsNone(check(c, msg("something else", i=9, ts=1004), t))

    def test_attachments(self):
        c, t = cfg(attachments={"limit": 4, "seconds": 10}, spam={"enabled": False}), automod.Tracker()
        self.assertIsNone(check(c, msg("", i=1, attachments=3, ts=1000), t))
        hit = check(c, msg("", i=2, attachments=2, ts=1002), t)
        self.assertEqual(hit.rule, "attachments")
        self.assertEqual({d[1] for d in hit.delete}, {1, 2})

    def test_exempt_channel_and_role(self):
        c = cfg()
        c["exempt_channels"] = [5]
        self.assertIsNone(check(c, msg("discord.gg/x")))
        c = cfg()
        c["exempt_roles"] = [77]
        self.assertIsNone(check(c, msg("discord.gg/x", roles=[77])))
        self.assertIsNotNone(check(c, msg("discord.gg/x", roles=[78])))

    def test_per_rule_exemption(self):
        c = cfg(invites={"channels": [5]})
        self.assertIsNone(check(c, msg("discord.gg/x", channel=5)))
        self.assertIsNotNone(check(c, msg("discord.gg/x", channel=6)))

    def test_disabled_rule(self):
        self.assertIsNone(check(cfg(invites={"enabled": False}), msg("discord.gg/x")))

    def test_hit_carries_action(self):
        h = check(cfg(mentions={"limit": 1, "action": "ban", "duration": 99}), msg(mentions=2))
        self.assertEqual((h.action, h.duration), ("ban", 99))


class Gate(unittest.TestCase):
    G = {"min_age_days": 7, "action": "kick", "no_avatar": True}

    def test_age(self):
        self.assertIn("2.0 days", automod.gate_reason(self.G, 2.0, True))
        self.assertIsNone(automod.gate_reason(self.G, 7.0, True))

    def test_avatar(self):
        self.assertIn("profile picture", automod.gate_reason(self.G, 30, False))
        self.assertIsNone(automod.gate_reason({**self.G, "no_avatar": False}, 30, False))

    def test_off(self):
        self.assertIsNone(automod.gate_reason({"min_age_days": 0, "action": "kick", "no_avatar": False}, 0, False))


class Raid(unittest.TestCase):
    def test_join_rate(self):
        j = automod.JoinRate()
        self.assertFalse(j.hit(0, 3, 10))
        self.assertFalse(j.hit(5, 3, 10))
        self.assertTrue(j.hit(9, 3, 10))
        j.reset()
        self.assertFalse(j.hit(10, 3, 10))

    def test_slow_joins_never_trigger(self):
        j = automod.JoinRate()
        self.assertFalse(any(j.hit(t * 11, 2, 10) for t in range(10)))


class Escalation(unittest.TestCase):
    def test_exact_count_only(self):
        steps = automod.load(None)["escalation"]
        self.assertEqual(automod.pick_escalation(steps, 3)["action"], "mute")
        self.assertIsNone(automod.pick_escalation(steps, 4))
        self.assertEqual(automod.pick_escalation(steps, 7)["action"], "kick")


class Store(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def test_infractions_and_warn_window(self):
        d = self.db
        a = d.add_infraction(1, 9, "warn", "first")
        d.add_infraction(1, 9, "warn", "second")
        d.add_infraction(2, 9, "warn", "other user")
        self.assertEqual(d.active_warns(1, now() - timedelta(days=30)), 2)
        self.assertEqual(d.active_warns(1, now() + timedelta(days=1)), 0)
        d.end_infraction(a)
        self.assertEqual(d.active_warns(1, now() - timedelta(days=30)), 1)
        self.assertEqual(d.clear_warns(1), 1)
        self.assertEqual(d.infraction_counts(1), {"warn": 2})
        self.assertEqual([r["reason"] for r in d.infractions_for(1)], ["second", "first"])

    def test_edit_and_delete(self):
        cid = self.db.add_infraction(1, 9, "note", "a")
        self.assertTrue(self.db.set_infraction_reason(cid, "b"))
        self.assertEqual(self.db.infraction(cid)["reason"], "b")
        self.assertTrue(self.db.delete_infraction(cid))
        self.assertFalse(self.db.delete_infraction(cid))

    def test_expiries(self):
        d = self.db
        d.add_infraction(1, 9, "ban", "tmp", expires=now() - timedelta(seconds=5))
        d.add_infraction(2, 9, "ban", "later", expires=now() + timedelta(hours=1))
        d.add_infraction(3, 9, "ban", "perm")
        d.add_infraction(4, 9, "warn", "w", expires=now() - timedelta(seconds=5))
        self.assertEqual([r["user_id"] for r in d.due_expiries()], [1])
        d.end_infractions(1, "ban")
        self.assertEqual(d.due_expiries(), [])

    def test_running_mute(self):
        d = self.db
        d.add_infraction(1, 9, "mute", "x", expires=now() + timedelta(hours=1))
        self.assertIsNotNone(d.running_mute(1))
        d.end_infractions(1, "mute")
        self.assertIsNone(d.running_mute(1))

    def test_sticky_roles(self):
        d = self.db
        d.save_sticky(1, [10, 11])
        d.save_sticky(1, [12])
        self.assertEqual(d.take_sticky(1), [12])
        self.assertEqual(d.take_sticky(1), [])

    def test_locks_keep_first_state(self):
        d = self.db
        d.save_lock(5, 1, "channel", {"send_messages": None})
        d.save_lock(5, 1, "server", {"send_messages": False})
        d.save_lock(6, 1, "server", {"send_messages": True})
        self.assertEqual(d.locks(channel_id=5)[0]["prev"], {"send_messages": None})
        self.assertEqual({x["channel_id"] for x in d.locks("server")}, {6})
        d.drop_locks("server")
        self.assertEqual({x["channel_id"] for x in d.locks()}, {5})

    def test_suggestion_votes(self):
        d = self.db
        sid = d.add_suggestion(1, "more cows", False)
        self.assertEqual(d.vote_suggestion(sid, 2, 1), 1)
        self.assertEqual(d.vote_suggestion(sid, 3, -1), -1)
        self.assertEqual(d.suggestion_votes(sid), (1, 1))
        self.assertEqual(d.vote_suggestion(sid, 2, -1), -1)
        self.assertEqual(d.suggestion_votes(sid), (0, 2))
        self.assertEqual(d.vote_suggestion(sid, 2, -1), 0)
        self.assertEqual(d.suggestion_votes(sid), (0, 1))

    def test_suggestion_lookup_and_decision(self):
        d = self.db
        sid = d.add_suggestion(1, "x", True)
        d.set_suggestion_message(sid, 50, 60)
        self.assertEqual(d.suggestion_by_message(60)["id"], sid)
        d.decide_suggestion(sid, "approved", 9, "good")
        s = d.suggestion(sid)
        self.assertEqual((s["status"], s["decided_by"], s["reason"], s["anonymous"]), ("approved", 9, "good", 1))


if __name__ == "__main__":
    unittest.main()
