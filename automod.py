"""Moderation logic with no discord.py in it: durations, config, and the message rule checks.

modtools.py feeds it plain `Msg` records and acts on the `Hit` it returns.
"""
import copy
import json
import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field

RULES = ("spam", "duplicates", "mentions", "emojis", "caps", "newlines", "attachments", "invites", "links", "words", "zalgo")
ACTIONS = ("delete", "warn", "mute", "kick", "ban")
MAX_TIMEOUT = 28 * 86400 - 60  # Discord rejects timeouts of 28 days or more

# `limit` means a different thing per rule: messages (spam), copies (duplicates), mentions, emojis,
# percent capitals (caps), lines (newlines), files (attachments), combining marks (zalgo).
RULE_HELP = {
    "spam": "Too many messages in a short time",
    "duplicates": "The same message repeated",
    "mentions": "Too many user or role mentions in one message",
    "emojis": "Too many emojis in one message",
    "caps": "Too much capital lettering",
    "newlines": "Too many line breaks in one message",
    "attachments": "Too many files in a short time",
    "invites": "Discord invite links",
    "links": "Links to blocked domains (or to anything outside the allowed list)",
    "words": "Blocked words and phrases",
    "zalgo": "Stacked combining marks (glitch text)",
}

DEFAULTS = {
    "enabled": False,
    "dm": True,                # tell the member why they were punished
    "notice_seconds": 8,       # how long the in-channel notice stays; 0 for none
    "exempt_channels": [],
    "exempt_roles": [],
    "warn_window_days": 30,    # warnings older than this stop counting towards escalation
    "escalation": [{"count": 3, "action": "mute", "seconds": 3600},
                   {"count": 5, "action": "mute", "seconds": 86400},
                   {"count": 7, "action": "kick", "seconds": 0}],
    "rules": {
        "spam": {"enabled": True, "action": "mute", "duration": 300, "limit": 5, "seconds": 5},
        "duplicates": {"enabled": True, "action": "delete", "duration": 300, "limit": 3, "seconds": 30},
        "mentions": {"enabled": True, "action": "mute", "duration": 600, "limit": 5},
        "emojis": {"enabled": True, "action": "delete", "duration": 300, "limit": 12},
        "caps": {"enabled": True, "action": "delete", "duration": 300, "limit": 70, "min_length": 12},
        "newlines": {"enabled": True, "action": "delete", "duration": 300, "limit": 12},
        "attachments": {"enabled": True, "action": "mute", "duration": 300, "limit": 6, "seconds": 10},
        "invites": {"enabled": True, "action": "delete", "duration": 300, "allow": []},
        "links": {"enabled": True, "action": "delete", "duration": 300, "mode": "blocklist",
                  "block": ["grabify.link", "iplogger.org", "iplogger.com", "2no.co", "yip.su"], "allow": []},
        "words": {"enabled": True, "action": "warn", "duration": 300, "list": []},
        "zalgo": {"enabled": True, "action": "delete", "duration": 300, "limit": 8},
    },
    "joingate": {"min_age_days": 0, "action": "kick", "no_avatar": False},
    "raid": {"enabled": True, "joins": 8, "seconds": 20, "action": "kick", "minutes": 15, "lockdown": False},
}


def load(raw):
    """Stored JSON (or None) on top of DEFAULTS; unknown keys are dropped, missing ones filled in."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        stored = json.loads(raw) if raw else {}
    except ValueError:
        stored = {}
    if not isinstance(stored, dict):
        return cfg
    for key, value in stored.items():
        if key in ("rules", "joingate", "raid") and isinstance(value, dict):
            for sub, subval in value.items():
                if key == "rules":
                    if sub in cfg["rules"] and isinstance(subval, dict):
                        cfg["rules"][sub].update(subval)
                elif sub in cfg[key]:
                    cfg[key][sub] = subval
        elif key in cfg:
            cfg[key] = value
    return cfg


def dump(cfg):
    return json.dumps(cfg, separators=(",", ":"))


# ---------- durations ----------

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DUR = re.compile(r"(\d+)\s*([smhdw])", re.I)


def parse_duration(text):
    """'90s', '10m', '2h', '7d', '1w', '1h30m' -> seconds. ValueError when it is not a duration."""
    s = (text or "").strip().lower().replace(" ", "")
    if not s:
        raise ValueError("empty duration")
    parts = _DUR.findall(s)
    if not parts or "".join(n + u for n, u in parts) != s:
        raise ValueError(f"'{text}' is not a duration; use things like 30m, 2h, 7d, 1w or 1h30m")
    total = sum(int(n) * _UNITS[u] for n, u in parts)
    if total <= 0:
        raise ValueError("duration must be more than zero")
    return total


def fmt_duration(seconds):
    seconds = int(seconds)
    out = []
    for name, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            out.append(f"{seconds // size}{name}")
            seconds %= size
    return "".join(out[:3]) or "0s"


# ---------- text handling ----------

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "i"})
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'|]+|\b(?:[a-z0-9-]+\.)+(?:com|net|org|gg|io|co|xyz|me|tv|ly|link|info|biz|us|uk|de|ru|cn|tk|ml|ga|cf|gq|top|site|online|club|shop|app|dev|ai|cc|to|fm|sh|gift|ws|vip|live)\b(?:/[^\s<>\"'|]*)?", re.I)
_INVITE = re.compile(r"(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|discord\.me|invite\.gg)/+([\w-]+)", re.I)
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
_UNICODE_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿\U0001F000-\U0001F2FF]")
_MARKUP = re.compile(r"<[@#:][^>]*>|https?://\S+")


def normalize(text):
    """Lower case, accents and zero-width characters dropped, common look-alike digits and symbols folded to letters."""
    t = unicodedata.normalize("NFKD", text or "").translate(_ZERO_WIDTH)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower().translate(_LEET)


def squeeze(text):
    """Collapse runs of the same character so 'heeello' and 'hello' compare equal."""
    return re.sub(r"(.)\1+", r"\1", text)


def word_pattern(entry):
    """Blocked-word entry -> compiled regex over squeezed normalised text.
    Plain entries match whole words (`*` is a wildcard). A leading `~` matches anywhere, even inside other words
    and with punctuation or spaces in between the letters."""
    loose = entry.startswith("~")
    core = squeeze(normalize(entry.lstrip("~").strip()))
    if not core:
        return None
    if loose:
        letters = [re.escape(c) for c in core if c.isalnum()]
        if not letters:
            return None
        return re.compile(r"[\W_]*".join(letters))
    body = "".join(r"\w*" if c == "*" else re.escape(c) for c in core)
    return re.compile(rf"(?<!\w){body}(?!\w)")


_pattern_cache = {}


def blocked_word(text, entries):
    """First blocked entry found in `text`, or None."""
    squeezed = squeeze(normalize(text))
    for entry in entries:
        if entry not in _pattern_cache:
            if len(_pattern_cache) > 2000:
                _pattern_cache.clear()
            _pattern_cache[entry] = word_pattern(entry)
        pat = _pattern_cache[entry]
        if pat and pat.search(squeezed):
            return entry
    return None


def domains(text):
    out = []
    for m in _URL.finditer(text or ""):
        host = re.sub(r"^(?:https?://)?(?:www\.)?", "", m.group(0), flags=re.I)
        host = re.split(r"[/?#:]", host, maxsplit=1)[0].lower().strip(".")
        if host:
            out.append(host)
    return out


def domain_matches(host, entries):
    return any(host == e or host.endswith("." + e) for e in (x.lower().strip().lstrip(".") for x in entries))


def invite_codes(text):
    return [m.group(1) for m in _INVITE.finditer(text or "")]


def emoji_count(text):
    return len(_CUSTOM_EMOJI.findall(text or "")) + len(_UNICODE_EMOJI.findall(text or ""))


def caps_percent(text, min_length):
    letters = [c for c in _MARKUP.sub("", text or "") if c.isalpha()]
    if len(letters) < min_length:
        return 0
    return round(100 * sum(c.isupper() for c in letters) / len(letters))


def combining_marks(text):
    return sum(unicodedata.category(c) == "Mn" for c in unicodedata.normalize("NFC", text or ""))


# ---------- message checks ----------

@dataclass
class Msg:
    id: int
    author: int
    channel: int
    roles: frozenset
    content: str
    mentions: int
    attachments: int
    ts: float


@dataclass
class Hit:
    rule: str
    reason: str
    action: str
    duration: int
    delete: list = field(default_factory=list)  # [(channel_id, message_id)]


class Tracker:
    """Short per-author memory of recent messages for the rate based rules."""

    def __init__(self, keep=120.0):
        self.keep = keep
        self.users = {}

    def add(self, m):
        q = self.users.setdefault(m.author, deque())
        q.append((m.ts, m.channel, m.id, squeeze(normalize(m.content)).strip(), m.attachments))
        while q and m.ts - q[0][0] > self.keep:
            q.popleft()

    def window(self, author, now, seconds):
        return [e for e in self.users.get(author, ()) if now - e[0] <= seconds]

    def forget(self, author):
        self.users.pop(author, None)

    def prune(self, now):
        for a in [a for a, q in self.users.items() if not q or now - q[-1][0] > self.keep]:
            del self.users[a]


def exempt(cfg, rule_cfg, m):
    if m.channel in cfg["exempt_channels"] or m.channel in rule_cfg.get("channels", []):
        return True
    return bool(m.roles & set(cfg["exempt_roles"]) or m.roles & set(rule_cfg.get("roles", [])))


def evaluate(cfg, m, tracker):
    """The first rule `m` breaks, as a Hit, or None. Records the message in `tracker` either way."""
    tracker.add(m)
    if not cfg["enabled"]:
        return None
    rules = cfg["rules"]
    content = m.content or ""

    def hit(name, reason, delete=None):
        r = rules[name]
        return Hit(name, reason, r["action"], int(r.get("duration", 300)), delete or [(m.channel, m.id)])

    def live(name):
        r = rules[name]
        return r if r.get("enabled") and not exempt(cfg, r, m) else None

    if (r := live("invites")):
        bad = [c for c in invite_codes(content) if c.lower() not in {a.lower() for a in r.get("allow", [])}]
        if bad:
            return hit("invites", "Posted a Discord invite link")
    if (r := live("links")):
        for host in domains(content):
            listed = domain_matches(host, r["block"] if r["mode"] == "blocklist" else r["allow"])
            if (r["mode"] == "blocklist" and listed) or (r["mode"] == "allowlist" and not listed):
                return hit("links", f"Posted a blocked link ({host})")
    if (r := live("words")):
        found = blocked_word(content, r["list"])
        if found:
            return hit("words", "Used a blocked word")
    if (r := live("mentions")) and m.mentions > r["limit"]:
        return hit("mentions", f"Mentioned {m.mentions} users or roles in one message")
    if (r := live("emojis")) and emoji_count(content) > r["limit"]:
        return hit("emojis", "Too many emojis")
    if (r := live("caps")) and caps_percent(content, r["min_length"]) >= r["limit"]:
        return hit("caps", "Too many capital letters")
    if (r := live("newlines")) and content.count("\n") + 1 > r["limit"]:
        return hit("newlines", "Too many line breaks")
    if (r := live("zalgo")) and combining_marks(content) > r["limit"]:
        return hit("zalgo", "Glitch text")
    if (r := live("spam")):
        recent = tracker.window(m.author, m.ts, r["seconds"])
        if len(recent) > r["limit"]:
            return hit("spam", f"Sent {len(recent)} messages in {r['seconds']} seconds", [(e[1], e[2]) for e in recent])
    if (r := live("duplicates")) and content.strip():
        key = squeeze(normalize(content)).strip()
        same = [e for e in tracker.window(m.author, m.ts, r["seconds"]) if e[3] == key]
        if len(same) >= r["limit"]:
            return hit("duplicates", f"Repeated the same message {len(same)} times", [(e[1], e[2]) for e in same])
    if (r := live("attachments")) and m.attachments:
        recent = tracker.window(m.author, m.ts, r["seconds"])
        files = sum(e[4] for e in recent)
        if files > r["limit"]:
            return hit("attachments", f"Sent {files} files in {r['seconds']} seconds", [(e[1], e[2]) for e in recent if e[4]])
    return None


# ---------- warnings, join gate, raids ----------

def pick_escalation(steps, warn_count):
    """The step that fires at exactly this many warnings, or None."""
    return next((s for s in steps if s["count"] == warn_count), None)


def gate_reason(gate, age_days, has_avatar):
    """Why a new member fails the join gate, or None."""
    if gate["min_age_days"] and age_days < gate["min_age_days"]:
        return f"Account is {age_days:.1f} days old; this server needs {gate['min_age_days']}"
    if gate["no_avatar"] and not has_avatar:
        return "Account has no profile picture"
    return None


class JoinRate:
    """True from `hit` when `joins` or more members arrived within `seconds`."""

    def __init__(self):
        self.times = deque()

    def hit(self, now, joins, seconds):
        self.times.append(now)
        while self.times and now - self.times[0] > seconds:
            self.times.popleft()
        return len(self.times) >= joins

    def reset(self):
        self.times.clear()
