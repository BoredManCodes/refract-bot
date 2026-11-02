import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import discord

import automod
import modtools
from db import DB, now


class Rank:
    def __init__(self, n):
        self.n = n

    def __ge__(self, other):
        return self.n >= other.n


def member(uid, rank=1, bot=False):
    m = MagicMock(spec=discord.Member)
    m.id, m.bot, m.top_role = uid, bot, Rank(rank)
    m.guild = None
    m.timeout, m.kick, m.send = AsyncMock(), AsyncMock(), AsyncMock()
    m.mention = f"<@{uid}>"
    m.__str__ = lambda self: f"user{uid}"
    return m


def make_guild(target):
    g = MagicMock()
    g.owner_id = 999
    g.me = member(1000, rank=50)
    g.ban, g.unban = AsyncMock(), AsyncMock()
    g.get_member = lambda uid: target if uid == target.id else None
    g.get_channel = lambda cid: None
    return g


def run(coro):
    return asyncio.run(coro)


class Cases(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        modtools.bot = MagicMock()
        modtools.bot.user.id = 1000
        modtools.db = self.db
        modtools._is_owner = lambda m: False
        modtools.GUILD_ID = 1
        self.target, self.mod = member(5, rank=1), member(6, rank=10)
        self.guild = make_guild(self.target)

    def apply(self, kind, user=None, mod=None, **kw):
        return run(modtools.apply(self.guild, kind, user or self.target, mod or self.mod, "because", **kw))

    def test_warn_records_and_dms(self):
        case, extra = self.apply("warn")
        row = self.db.infraction(case)
        self.assertEqual((row["kind"], row["user_id"], row["mod_id"], row["active"]), ("warn", 5, 6, 1))
        self.assertEqual(extra, "")
        self.target.send.assert_awaited_once()

    def test_third_warn_mutes(self):
        self.apply("warn")
        self.apply("warn")
        _, extra = self.apply("warn")
        self.assertIn("muted for 1h", extra)
        self.target.timeout.assert_awaited_once()
        self.assertEqual(self.target.timeout.await_args.args[0], timedelta(hours=1))
        mute = self.db.running_mute(5)
        self.assertEqual((mute["auto"], mute["rule"]), (1, "escalation"))

    def test_mute_caps_at_discord_limit(self):
        self.apply("mute", seconds=90 * 86400)
        self.assertEqual(self.target.timeout.await_args.args[0], timedelta(seconds=automod.MAX_TIMEOUT))

    def test_new_mute_replaces_running_one(self):
        self.apply("mute", seconds=600)
        first = self.db.running_mute(5)["id"]
        self.apply("mute", seconds=1200)
        self.assertFalse(self.db.infraction(first)["active"])

    def test_unmute_ends_mute(self):
        self.apply("mute", seconds=600)
        self.apply("unmute")
        self.assertIsNone(self.db.running_mute(5))
        self.target.timeout.assert_awaited_with(None, reason=unittest.mock.ANY)

    def test_tempban_has_expiry_and_unban_ends_it(self):
        user = MagicMock(spec=discord.User)
        user.id, user.send = 77, AsyncMock()
        self.guild.get_member = lambda uid: None
        case, _ = self.apply("ban", user=user, seconds=3600)
        self.assertIsNotNone(self.db.infraction(case)["expires_at"])
        self.assertEqual(self.guild.ban.await_args.kwargs["delete_message_seconds"], 0)
        self.apply("unban", user=user)
        self.assertEqual(self.db.due_expiries(), [])
        self.assertFalse(self.db.infraction(case)["active"])

    def test_kick_dms_before_removing(self):
        order = []
        self.target.send.side_effect = lambda **k: order.append("dm")
        self.target.kick.side_effect = lambda **k: order.append("kick")
        self.apply("kick")
        self.assertEqual(order, ["dm", "kick"])

    def test_cannot_punish_equal_or_higher(self):
        for rank in (10, 20):
            with self.assertRaises(modtools.ModError):
                self.apply("warn", user=member(7, rank=rank))
        self.assertEqual(self.db.infractions_for(7), [])

    def test_owner_can_outrank_check_but_not_the_bot(self):
        modtools._is_owner = lambda m: True
        self.apply("warn", user=member(8, rank=20))
        with self.assertRaises(modtools.ModError):
            self.apply("warn", user=member(9, rank=50))

    def test_cannot_target_guild_owner_or_self(self):
        with self.assertRaises(modtools.ModError):
            self.apply("kick", user=member(999, rank=1))
        with self.assertRaises(modtools.ModError):
            self.apply("warn", user=self.mod)

    def test_forbidden_becomes_mod_error_and_nothing_recorded(self):
        self.target.kick.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), "no")
        with self.assertRaises(modtools.ModError):
            self.apply("kick", dm=False)
        self.assertEqual(self.db.infractions_for(5), [])

    def test_mute_needs_a_member_in_the_server(self):
        self.guild.get_member = lambda uid: None
        user = MagicMock(spec=discord.User)
        user.id = 55
        with self.assertRaises(modtools.ModError):
            self.apply("mute", user=user, seconds=60)


class Lock(unittest.TestCase):
    """lock_channel then unlock_channel must leave the overwrites exactly as they were."""

    def setUp(self):
        self.db = DB(":memory:")
        modtools.db = self.db
        self.addCleanup(setattr, modtools, "role_of", modtools.role_of)

    def test_round_trip(self):
        everyone, speaker, staff = MagicMock(), MagicMock(), MagicMock()
        for r, i in ((everyone, 1), (speaker, 2), (staff, 3)):
            r.id, r.managed = i, False
        everyone.__class__ = speaker.__class__ = staff.__class__ = discord.Role
        state = {1: discord.PermissionOverwrite(view_channel=True), 2: discord.PermissionOverwrite(send_messages=True), 3: discord.PermissionOverwrite()}
        original = {k: v.pair() for k, v in state.items()}
        guild = MagicMock()
        guild.default_role = everyone
        guild.get_role = lambda rid: {1: everyone, 2: speaker, 3: staff}[rid]
        channel = MagicMock()
        channel.id, channel.guild = 50, guild
        channel.overwrites = {everyone: state[1], speaker: state[2]}
        channel.overwrites_for = lambda t: state.get(t.id, discord.PermissionOverwrite())

        async def set_permissions(target, overwrite, reason=None):
            if overwrite is None:
                state[target.id] = discord.PermissionOverwrite()
            else:
                state[target.id] = overwrite
        channel.set_permissions = set_permissions
        modtools.role_of = lambda g, key: staff if key == "staff_role_id" else None

        run(modtools.lock_channel(channel, "channel", "test"))
        self.assertIs(state[1].send_messages, False)
        self.assertIs(state[2].send_messages, False)  # a role that explicitly allowed talking is locked too
        self.assertIs(state[3].send_messages, True)   # staff keep talking
        run(modtools.unlock_channel(channel, self.db.locks(channel_id=50), "done"))
        for k, v in state.items():
            self.assertEqual(v.pair(), original[k], k)


class Raid(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        modtools.db = self.db

    def test_state_expires(self):
        import json
        from db import iso
        self.db.put("raidmode", json.dumps({"on": True, "action": "kick", "until": iso(now() - timedelta(seconds=1))}))
        self.assertFalse(modtools.raid_on())
        self.assertIsNone(self.db.get("raidmode"))
        self.db.put("raidmode", json.dumps({"on": True, "action": "kick", "until": None}))
        self.assertTrue(modtools.raid_on())


if __name__ == "__main__":
    unittest.main()
