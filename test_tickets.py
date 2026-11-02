import unittest

import tickets
from db import DB

CTX = {"users": {"1": "Alice"}, "roles": {"9": ("Staff", "#3498db")}, "channels": {"5": "rules"}}


def md(s):
    return tickets.render_markdown(s, CTX)


def msg(i, author, created, content="hi", **kw):
    return {"id": i, "created": created, "content": content, "author": {"id": author, "name": f"u{author}", "bot": False}, **kw}


class Markdown(unittest.TestCase):
    def test_html_escaped(self):
        self.assertNotIn("<script>", md("<script>alert(1)</script>"))
        self.assertIn("&lt;script&gt;", md("<script>alert(1)</script>"))

    def test_bold_italic_underline_strike(self):
        self.assertEqual(md("**b**"), "<strong>b</strong>")
        self.assertEqual(md("*i*"), "<em>i</em>")
        self.assertEqual(md("__u__"), "<u>u</u>")
        self.assertEqual(md("~~s~~"), "<s>s</s>")
        self.assertEqual(md("***x***"), "<strong><em>x</em></strong>")

    def test_inline_code_not_formatted(self):
        self.assertEqual(md("`**no**`"), '<code class="inline">**no**</code>')

    def test_code_block_escaped(self):
        out = md("```py\nprint('<b>')\n```")
        self.assertIn("<pre><code>print(&#x27;&lt;b&gt;&#x27;)</code></pre>", out)

    def test_mentions_resolved(self):
        self.assertIn("@Alice", md("<@1>"))
        self.assertIn("@Alice", md("<@!1>"))
        self.assertIn("@Staff", md("<@&9>"))
        self.assertIn("#rules", md("<#5>"))
        self.assertIn("@Unknown User", md("<@2>"))

    def test_custom_emoji(self):
        self.assertIn("emojis/123.webp", md("<:wave:123>"))
        self.assertIn("emojis/123.gif", md("<a:wave:123>"))

    def test_link_safe(self):
        self.assertIn('href="https://example.com/a?b=1&amp;c=2"', md("see https://example.com/a?b=1&c=2."))
        self.assertNotIn("<a ", md("[x](javascript:alert(1))"))
        self.assertIn('<a href="https://x.io"', md("[site](https://x.io)"))

    def test_url_underscores_not_italic(self):
        self.assertNotIn("<em>", md("https://x.io/a_b_c"))

    def test_spoiler_and_quote(self):
        self.assertIn('class="spoiler"', md("||secret||"))
        self.assertIn('class="quote"', md("> hello\n> there"))

    def test_heading_and_list(self):
        self.assertIn("<h1>Big</h1>", md("# Big"))
        self.assertIn("<ul", md("- a\n- b"))

    def test_timestamp(self):
        self.assertIn("<time", md("<t:1700000000:R>"))


class Transcript(unittest.TestCase):
    META = {"title": "T", "channel": "ticket-1", "info": [("Opened by", "<b>x</b>")]}

    def render(self, msgs):
        return tickets.render_transcript(self.META, msgs, CTX)

    def test_groups_same_author_within_seven_minutes(self):
        out = self.render([msg(1, 1, "2026-11-02T01:00:00+00:00"), msg(2, 1, "2026-11-02T01:03:00+00:00"),
                           msg(3, 1, "2026-11-02T01:30:00+00:00"), msg(4, 2, "2026-11-02T01:31:00+00:00")])
        self.assertEqual(out.count('class="msg compact"'), 1)
        self.assertEqual(out.count('class="avatar"'), 3)

    def test_day_divider(self):
        out = self.render([msg(1, 1, "2026-11-02T23:59:00+00:00"), msg(2, 1, "2026-11-03T00:01:00+00:00")])
        self.assertEqual(out.count('class="day"'), 2)

    def test_meta_escaped(self):
        self.assertNotIn("<b>x</b>", self.render([]))

    def test_embed_attachment_button_reaction(self):
        m = msg(1, 1, "2026-11-02T01:00:00+00:00", embeds=[{"title": "Ticket", "description": "d", "color": "#57f287", "fields": [
            {"name": "n", "value": "v", "inline": True}], "footer": "f"}],
            attachments=[{"name": "a.png", "url": "https://cdn.discordapp.com/a.png", "image": True, "size": 10},
                         {"name": "b.zip", "url": "https://cdn.discordapp.com/b.zip", "image": False, "size": 2048}],
            components=[[{"label": "Close Ticket", "style": 4}]], reactions=[{"emoji": "👍", "count": 2, "url": None}])
        out = self.render([m])
        for needle in ("border-left-color:#57f287", "Close Ticket", 'class="btn danger"', "a.png", "b.zip", "2.0 KB", "👍"):
            self.assertIn(needle, out)

    def test_bad_colour_and_url_rejected(self):
        m = msg(1, 1, "2026-11-02T01:00:00+00:00", embeds=[{"title": "t", "url": "javascript:x", "color": "red;x"}])
        m["author"] = {"id": 1, "name": "n", "colour": "\"><script>", "avatar": "javascript:alert(1)"}
        out = self.render([m])
        self.assertNotIn("<script>alert", out)
        self.assertNotIn("javascript:", out)

    def test_reply_and_system(self):
        out = self.render([msg(1, 1, "2026-11-02T01:00:00+00:00", reply={"name": "Bob", "content": "orig"}),
                           msg(2, 1, "2026-11-02T01:01:00+00:00", content="", system="Bob joined")])
        self.assertIn("class=\"reply\"", out)
        self.assertIn("Bob joined", out)


class TicketDB(unittest.TestCase):
    def test_lifecycle(self):
        db = DB(":memory:")
        t = db.add_ticket(5, "game", "help")
        db.set_ticket_channel(t, 77)
        self.assertEqual(db.open_ticket_for(5)["id"], t)
        self.assertEqual(db.ticket_by_channel(77)["kind"], "game")
        self.assertTrue(db.close_ticket(t, 9, "tok"))
        self.assertFalse(db.close_ticket(t, 9, "tok2"))
        self.assertIsNone(db.ticket_by_channel(77))
        db.reopen_ticket(t)
        self.assertIsNotNone(db.ticket_by_channel(77))
        db.save_transcript("tok", "<p>x</p>")
        self.assertEqual(db.transcript("tok"), "<p>x</p>")
        self.assertIsNone(db.transcript("nope"))


class WebAccess(unittest.TestCase):
    def test_session_roundtrip_and_forgery(self):
        c = tickets.make_session(b"k", 42)
        self.assertEqual(tickets.read_session(b"k", c), 42)
        self.assertIsNone(tickets.read_session(b"other", c))
        self.assertIsNone(tickets.read_session(b"k", c.replace("u42", "u43")))
        self.assertIsNone(tickets.read_session(b"k", c, at=9e12))
        self.assertIsNone(tickets.read_session(b"k", None))

    def test_state_roundtrip(self):
        s = tickets.sign(b"k", "nonce:token")
        self.assertEqual(tickets.unsign(b"k", s), "nonce:token")
        self.assertIsNone(tickets.unsign(b"k", s + "0"))

    def test_share_bar_injected_and_escaped(self):
        out = tickets.with_share_bar("<body>x</body>", "https://a/s/</script>")
        self.assertIn("Share link", out)
        self.assertEqual(out.count("</script>"), 1)
        self.assertTrue(out.endswith("</body>"))


class ShareDB(unittest.TestCase):
    def test_members_and_share(self):
        db = DB(":memory:")
        t = db.add_ticket(5, "game", "x")
        db.close_ticket(t, 9, "tok")
        db.add_ticket_members(t, {5, 6})
        self.assertTrue(db.is_ticket_member(t, 6))
        self.assertFalse(db.is_ticket_member(t, 7))
        self.assertEqual(db.ticket_by_token("tok")["id"], t)
        self.assertIsNone(db.share_token(t))
        db.set_share(t, "sh")
        self.assertEqual(db.ticket_by_share("sh")["id"], t)
        db.clear_share(t)
        self.assertIsNone(db.ticket_by_share("sh"))



if __name__ == "__main__":
    unittest.main()
