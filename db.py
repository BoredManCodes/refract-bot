import json
import sqlite3
import uuid
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    mc_name TEXT NOT NULL,
    answers TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    message_id INTEGER,
    decided_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS elections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    voting_open TEXT NOT NULL,
    voting_close TEXT NOT NULL,
    outgoing_id INTEGER,
    outgoing_stays_admin INTEGER NOT NULL DEFAULT 1,
    restrict_to TEXT,
    parent_id INTEGER,
    post_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS candidates (
    election_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    pitch TEXT NOT NULL,
    PRIMARY KEY (election_id, user_id)
);
CREATE TABLE IF NOT EXISTS head_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
-- Who voted and what was voted are kept apart on purpose: no shared key,
-- no timestamps, random ballot ids, WITHOUT ROWID so insert order is not stored.
CREATE TABLE IF NOT EXISTS voters (
    election_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (election_id, user_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ballots (
    id TEXT PRIMARY KEY,
    election_id INTEGER NOT NULL,
    first INTEGER, second INTEGER, third INTEGER
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS invite_joins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    inviter_id INTEGER,
    code TEXT,
    joined_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS departures (
    user_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    avatar TEXT,
    joined_at TEXT,
    left_at TEXT,
    kind TEXT NOT NULL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by INTEGER,
    token TEXT
);
CREATE TABLE IF NOT EXISTS ticket_members (
    ticket_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (ticket_id, user_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ticket_shares (
    ticket_id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS transcripts (
    token TEXT PRIMARY KEY,
    html TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dash_grants (
    kind TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    perm TEXT NOT NULL,
    PRIMARY KEY (kind, subject_id, perm)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS dash_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS infractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    mod_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    auto INTEGER NOT NULL DEFAULT 0,
    rule TEXT
);
CREATE INDEX IF NOT EXISTS infractions_user ON infractions (user_id);
CREATE TABLE IF NOT EXISTS sticky_roles (
    user_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    PRIMARY KEY (user_id, role_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS locks (
    channel_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    prev TEXT NOT NULL,
    PRIMARY KEY (channel_id, target_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    content TEXT NOT NULL,
    anonymous INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_by INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestion_votes (
    suggestion_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    vote INTEGER NOT NULL,
    PRIMARY KEY (suggestion_id, user_id)
) WITHOUT ROWID;
"""


def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)

    # settings
    def get(self, key, default=None):
        r = self.c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def get_int(self, key):
        v = self.get(key)
        return int(v) if v not in (None, "") else None

    def put(self, key, value):
        with self.c:
            self.c.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, str(value)))

    def clear(self, key):
        with self.c:
            self.c.execute("DELETE FROM settings WHERE key=?", (key,))

    def default(self, key, value):
        if value not in (None, "") and self.get(key) is None:
            self.put(key, value)

    # applications
    def pending_application(self, user_id):
        return self.c.execute("SELECT * FROM applications WHERE user_id=? AND status='pending'", (user_id,)).fetchone()

    def add_application(self, user_id, mc_name, answers):
        with self.c:
            cur = self.c.execute("INSERT INTO applications (user_id, mc_name, answers, created_at) VALUES (?,?,?,?)",
                                 (user_id, mc_name, json.dumps(answers), iso(now())))
        return cur.lastrowid

    def drop_application(self, app_id):
        with self.c:
            self.c.execute("DELETE FROM applications WHERE id=?", (app_id,))

    def set_application_message(self, app_id, message_id):
        with self.c:
            self.c.execute("UPDATE applications SET message_id=? WHERE id=?", (message_id, app_id))

    def application_by_message(self, message_id):
        return self.c.execute("SELECT * FROM applications WHERE message_id=?", (message_id,)).fetchone()

    def decide_application(self, app_id, status, by):
        with self.c:
            self.c.execute("UPDATE applications SET status=?, decided_by=? WHERE id=? AND status='pending'",
                           (status, by, app_id))

    # departures
    def add_departure(self, user_id, name, avatar, joined_at, left_at, kind, reason):
        with self.c:
            self.c.execute("INSERT OR REPLACE INTO departures (user_id, name, avatar, joined_at, left_at, kind, reason) "
                           "VALUES (?,?,?,?,?,?,?)", (user_id, name, avatar, joined_at, left_at, kind, reason))

    def has_departure(self, user_id):
        return self.c.execute("SELECT 1 FROM departures WHERE user_id=?", (user_id,)).fetchone() is not None

    def drop_departure(self, user_id):
        with self.c:
            self.c.execute("DELETE FROM departures WHERE user_id=?", (user_id,))

    def set_departure_kind(self, user_id, kind):
        with self.c:
            self.c.execute("UPDATE departures SET kind=?, reason=NULL WHERE user_id=?", (kind, user_id))

    def departures(self):
        return self.c.execute("SELECT * FROM departures ORDER BY left_at IS NULL, left_at DESC").fetchall()

    # tickets
    def open_ticket_for(self, user_id):
        return self.c.execute("SELECT * FROM tickets WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1",
                              (user_id,)).fetchone()

    def add_ticket(self, user_id, kind, topic):
        with self.c:
            cur = self.c.execute("INSERT INTO tickets (user_id, kind, topic, created_at) VALUES (?,?,?,?)",
                                 (user_id, kind, topic, iso(now())))
        return cur.lastrowid

    def set_ticket_channel(self, ticket_id, channel_id):
        with self.c:
            self.c.execute("UPDATE tickets SET channel_id=? WHERE id=?", (channel_id, ticket_id))

    def drop_ticket(self, ticket_id):
        with self.c:
            self.c.execute("DELETE FROM tickets WHERE id=?", (ticket_id,))

    def ticket_by_channel(self, channel_id):
        return self.c.execute("SELECT * FROM tickets WHERE channel_id=? AND status='open'", (channel_id,)).fetchone()

    def close_ticket(self, ticket_id, by, token):
        """False if someone else already closed it."""
        with self.c:
            cur = self.c.execute("UPDATE tickets SET status='closed', closed_at=?, closed_by=?, token=? "
                                 "WHERE id=? AND status='open'", (iso(now()), by, token, ticket_id))
        return cur.rowcount > 0

    def reopen_ticket(self, ticket_id):
        with self.c:
            self.c.execute("UPDATE tickets SET status='open', closed_at=NULL, closed_by=NULL, token=NULL WHERE id=?", (ticket_id,))

    def open_tickets(self):
        return self.c.execute("SELECT * FROM tickets WHERE status='open'").fetchall()

    def ticket_by_token(self, token):
        return self.c.execute("SELECT * FROM tickets WHERE token=? AND status='closed'", (token,)).fetchone()

    def add_ticket_members(self, ticket_id, user_ids):
        with self.c:
            self.c.executemany("INSERT OR IGNORE INTO ticket_members VALUES (?,?)", [(ticket_id, u) for u in user_ids])

    def is_ticket_member(self, ticket_id, user_id):
        return self.c.execute("SELECT 1 FROM ticket_members WHERE ticket_id=? AND user_id=?", (ticket_id, user_id)).fetchone() is not None

    def share_token(self, ticket_id):
        r = self.c.execute("SELECT token FROM ticket_shares WHERE ticket_id=?", (ticket_id,)).fetchone()
        return r["token"] if r else None

    def set_share(self, ticket_id, token):
        with self.c:
            self.c.execute("INSERT OR REPLACE INTO ticket_shares VALUES (?,?)", (ticket_id, token))

    def clear_share(self, ticket_id):
        with self.c:
            self.c.execute("DELETE FROM ticket_shares WHERE ticket_id=?", (ticket_id,))

    def ticket_by_share(self, share):
        return self.c.execute("SELECT t.* FROM tickets t JOIN ticket_shares s ON s.ticket_id=t.id WHERE s.token=?", (share,)).fetchone()

    def save_transcript(self, token, html):
        with self.c:
            self.c.execute("INSERT OR REPLACE INTO transcripts VALUES (?,?)", (token, html))

    def transcript(self, token):
        r = self.c.execute("SELECT html FROM transcripts WHERE token=?", (token,)).fetchone()
        return r["html"] if r else None

    def ticket(self, ticket_id):
        return self.c.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()

    def tickets(self, status=None, limit=200):
        if status:
            return self.c.execute("SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        return self.c.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # applications (dashboard)
    def application(self, app_id):
        return self.c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()

    def applications(self, status=None, limit=200):
        if status:
            return self.c.execute("SELECT * FROM applications WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        return self.c.execute("SELECT * FROM applications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def elections(self, limit=10):
        return self.c.execute("SELECT * FROM elections ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # dashboard access
    def grants(self):
        return self.c.execute("SELECT kind, subject_id, perm FROM dash_grants ORDER BY kind, subject_id, perm").fetchall()

    def add_grant(self, kind, subject_id, perm):
        with self.c:
            self.c.execute("INSERT OR IGNORE INTO dash_grants VALUES (?,?,?)", (kind, subject_id, perm))

    def remove_grant(self, kind, subject_id, perm):
        with self.c:
            self.c.execute("DELETE FROM dash_grants WHERE kind=? AND subject_id=? AND perm=?", (kind, subject_id, perm))

    def perms_for(self, user_id, role_ids):
        """Dashboard permissions granted to this user directly or through any of their roles."""
        marks = ",".join("?" * len(role_ids)) or "NULL"
        rows = self.c.execute(
            f"SELECT DISTINCT perm FROM dash_grants WHERE (kind='user' AND subject_id=?) OR (kind='role' AND subject_id IN ({marks}))",
            (user_id, *role_ids)).fetchall()
        return {r["perm"] for r in rows}

    def audit(self, user_id, action, detail=""):
        with self.c:
            self.c.execute("INSERT INTO dash_audit (user_id, action, detail, at) VALUES (?,?,?,?)",
                           (user_id, action, detail, iso(now())))

    def recent_audit(self, limit=100):
        return self.c.execute("SELECT * FROM dash_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # invites
    def add_invite_join(self, user_id, inviter_id, code):
        with self.c:
            self.c.execute("INSERT INTO invite_joins (user_id, inviter_id, code, joined_at) VALUES (?,?,?,?)",
                           (user_id, inviter_id, code, iso(now())))

    def last_invite_join(self, user_id):
        return self.c.execute("SELECT * FROM invite_joins WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()

    def invited_by(self, inviter_id):
        rows = self.c.execute("SELECT user_id FROM invite_joins WHERE inviter_id=? ORDER BY id DESC", (inviter_id,)).fetchall()
        return [r["user_id"] for r in rows]

    def top_inviters(self, limit=10):
        rows = self.c.execute("SELECT inviter_id, COUNT(*) AS n FROM invite_joins WHERE inviter_id IS NOT NULL "
                              "GROUP BY inviter_id ORDER BY n DESC LIMIT ?", (limit,)).fetchall()
        return [(r["inviter_id"], r["n"]) for r in rows]

    # head admin terms
    def change_head(self, new_id, old_id=None, old_since=None):
        """Close the outgoing term, open the new one. Returns (new's term number, outgoing's start or None)."""
        t = iso(now())
        with self.c:
            started = None
            if old_id and old_id != new_id:
                r = self.c.execute("SELECT started_at FROM head_terms WHERE user_id=? AND ended_at IS NULL "
                                   "ORDER BY id DESC LIMIT 1", (old_id,)).fetchone()
                started = r["started_at"] if r else old_since
                if started and not r:
                    self.c.execute("INSERT INTO head_terms (user_id, started_at) VALUES (?,?)", (old_id, started))
            self.c.execute("UPDATE head_terms SET ended_at=? WHERE ended_at IS NULL AND user_id!=?", (t, new_id))
            if not self.c.execute("SELECT 1 FROM head_terms WHERE user_id=? AND ended_at IS NULL", (new_id,)).fetchone():
                self.c.execute("INSERT INTO head_terms (user_id, started_at) VALUES (?,?)", (new_id, t))
            n = self.c.execute("SELECT COUNT(*) FROM head_terms WHERE user_id=?", (new_id,)).fetchone()[0]
        return n, started

    def current_term(self):
        return self.c.execute("SELECT user_id, started_at FROM head_terms WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()

    def term_count(self, user_id):
        return self.c.execute("SELECT COUNT(*) FROM head_terms WHERE user_id=?", (user_id,)).fetchone()[0]

    # elections
    def active_election(self):
        return self.c.execute("SELECT * FROM elections WHERE status IN ('nominations','voting','tied') "
                              "ORDER BY id DESC LIMIT 1").fetchone()

    def election(self, eid):
        return self.c.execute("SELECT * FROM elections WHERE id=?", (eid,)).fetchone()

    def add_election(self, status, voting_open, voting_close, outgoing_id, stays_admin, restrict_to=None, parent_id=None):
        with self.c:
            cur = self.c.execute(
                "INSERT INTO elections (status, voting_open, voting_close, outgoing_id, outgoing_stays_admin, restrict_to, parent_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (status, iso(voting_open), iso(voting_close), outgoing_id, int(stays_admin),
                 json.dumps(restrict_to) if restrict_to else None, parent_id))
        return cur.lastrowid

    def set_election(self, eid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.c:
            self.c.execute(f"UPDATE elections SET {cols} WHERE id=?", (*fields.values(), eid))

    def add_candidate(self, eid, user_id, pitch):
        with self.c:
            self.c.execute("INSERT INTO candidates VALUES (?,?,?) ON CONFLICT(election_id, user_id) DO UPDATE SET pitch=excluded.pitch",
                           (eid, user_id, pitch))

    def withdraw_candidate(self, eid, user_id):
        with self.c:
            cur = self.c.execute("DELETE FROM candidates WHERE election_id=? AND user_id=?", (eid, user_id))
        return cur.rowcount > 0

    def candidates(self, eid):
        rows = self.c.execute("SELECT user_id, pitch FROM candidates WHERE election_id=? ORDER BY user_id", (eid,)).fetchall()
        return [(r["user_id"], r["pitch"]) for r in rows]

    def copy_candidates(self, from_id, to_id, only):
        with self.c:
            for uid, pitch in self.candidates(from_id):
                if uid in only:
                    self.c.execute("INSERT INTO candidates VALUES (?,?,?)", (to_id, uid, pitch))

    def cast_vote(self, eid, user_id, picks):
        """One transaction: mark voter, store anonymous ballot. False if already voted."""
        picks = list(picks) + [None] * (3 - len(picks))
        try:
            with self.c:
                self.c.execute("INSERT INTO voters VALUES (?,?)", (eid, user_id))
                self.c.execute("INSERT INTO ballots VALUES (?,?,?,?,?)", (uuid.uuid4().hex, eid, *picks))
        except sqlite3.IntegrityError:
            return False
        return True

    def has_voted(self, eid, user_id):
        return self.c.execute("SELECT 1 FROM voters WHERE election_id=? AND user_id=?", (eid, user_id)).fetchone() is not None

    def ballots(self, eid):
        rows = self.c.execute("SELECT first, second, third FROM ballots WHERE election_id=?", (eid,)).fetchall()
        return [(r["first"], r["second"], r["third"]) for r in rows]

    # infractions
    def add_infraction(self, user_id, mod_id, kind, reason, expires=None, auto=False, rule=None):
        with self.c:
            cur = self.c.execute("INSERT INTO infractions (user_id, mod_id, kind, reason, created_at, expires_at, active, auto, rule) "
                                 "VALUES (?,?,?,?,?,?,?,?,?)",
                                 (user_id, mod_id, kind, reason, iso(now()), iso(expires) if expires else None,
                                  int(kind in ("warn", "mute", "ban")), int(auto), rule))
        return cur.lastrowid

    def infraction(self, case_id):
        return self.c.execute("SELECT * FROM infractions WHERE id=?", (case_id,)).fetchone()

    def infractions_for(self, user_id, limit=50):
        return self.c.execute("SELECT * FROM infractions WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()

    def infraction_counts(self, user_id):
        rows = self.c.execute("SELECT kind, COUNT(*) AS n FROM infractions WHERE user_id=? GROUP BY kind", (user_id,)).fetchall()
        return {r["kind"]: r["n"] for r in rows}

    def set_infraction_reason(self, case_id, reason):
        with self.c:
            return self.c.execute("UPDATE infractions SET reason=? WHERE id=?", (reason, case_id)).rowcount > 0

    def delete_infraction(self, case_id):
        with self.c:
            return self.c.execute("DELETE FROM infractions WHERE id=?", (case_id,)).rowcount > 0

    def end_infractions(self, user_id, kind):
        """Mark a member's running mutes, bans or warnings as over (unmuted, unbanned, cleared, replaced)."""
        with self.c:
            return self.c.execute("UPDATE infractions SET active=0 WHERE user_id=? AND kind=? AND active=1", (user_id, kind)).rowcount

    def end_infraction(self, case_id):
        with self.c:
            self.c.execute("UPDATE infractions SET active=0 WHERE id=?", (case_id,))

    def active_warns(self, user_id, since):
        return self.c.execute("SELECT COUNT(*) FROM infractions WHERE user_id=? AND kind='warn' AND active=1 "
                              "AND datetime(created_at) >= datetime(?)", (user_id, iso(since))).fetchone()[0]

    def clear_warns(self, user_id):
        return self.end_infractions(user_id, "warn")

    def due_expiries(self):
        return self.c.execute("SELECT * FROM infractions WHERE active=1 AND kind IN ('ban','mute') AND expires_at IS NOT NULL "
                              "AND datetime(expires_at) <= datetime(?)", (iso(now()),)).fetchall()

    def running_mute(self, user_id):
        return self.c.execute("SELECT * FROM infractions WHERE user_id=? AND kind='mute' AND active=1 AND expires_at IS NOT NULL "
                              "AND datetime(expires_at) > datetime(?) ORDER BY id DESC LIMIT 1", (user_id, iso(now()))).fetchone()

    # sticky roles
    def save_sticky(self, user_id, role_ids):
        with self.c:
            self.c.execute("DELETE FROM sticky_roles WHERE user_id=?", (user_id,))
            self.c.executemany("INSERT INTO sticky_roles VALUES (?,?)", [(user_id, r) for r in role_ids])

    def take_sticky(self, user_id):
        rows = self.c.execute("SELECT role_id FROM sticky_roles WHERE user_id=?", (user_id,)).fetchall()
        with self.c:
            self.c.execute("DELETE FROM sticky_roles WHERE user_id=?", (user_id,))
        return [r["role_id"] for r in rows]

    # channel locks
    def save_lock(self, channel_id, target_id, scope, prev):
        """Keeps the first saved state if the channel is already locked, so unlocking restores the real original."""
        with self.c:
            self.c.execute("INSERT OR IGNORE INTO locks VALUES (?,?,?,?)", (channel_id, target_id, scope, json.dumps(prev)))

    def locks(self, scope=None, channel_id=None):
        q, args = "SELECT * FROM locks WHERE 1=1", []
        if scope:
            q += " AND scope=?"
            args.append(scope)
        if channel_id:
            q += " AND channel_id=?"
            args.append(channel_id)
        return [{"channel_id": r["channel_id"], "target_id": r["target_id"], "scope": r["scope"], "prev": json.loads(r["prev"])}
                for r in self.c.execute(q, args).fetchall()]

    def drop_locks(self, scope=None, channel_id=None):
        q, args = "DELETE FROM locks WHERE 1=1", []
        if scope:
            q += " AND scope=?"
            args.append(scope)
        if channel_id:
            q += " AND channel_id=?"
            args.append(channel_id)
        with self.c:
            self.c.execute(q, args)

    # suggestions
    def add_suggestion(self, user_id, content, anonymous):
        with self.c:
            cur = self.c.execute("INSERT INTO suggestions (user_id, content, anonymous, created_at) VALUES (?,?,?,?)",
                                 (user_id, content, int(anonymous), iso(now())))
        return cur.lastrowid

    def set_suggestion_message(self, sid, channel_id, message_id):
        with self.c:
            self.c.execute("UPDATE suggestions SET channel_id=?, message_id=? WHERE id=?", (channel_id, message_id, sid))

    def drop_suggestion(self, sid):
        with self.c:
            self.c.execute("DELETE FROM suggestions WHERE id=?", (sid,))

    def suggestion(self, sid):
        return self.c.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()

    def suggestion_by_message(self, message_id):
        return self.c.execute("SELECT * FROM suggestions WHERE message_id=?", (message_id,)).fetchone()

    def decide_suggestion(self, sid, status, by, reason):
        with self.c:
            self.c.execute("UPDATE suggestions SET status=?, decided_by=?, reason=? WHERE id=?", (status, by, reason, sid))

    def vote_suggestion(self, sid, user_id, vote):
        """Same vote twice takes it back; the other vote switches. Returns the member's vote now (0 for none)."""
        cur = self.c.execute("SELECT vote FROM suggestion_votes WHERE suggestion_id=? AND user_id=?", (sid, user_id)).fetchone()
        with self.c:
            if cur and cur["vote"] == vote:
                self.c.execute("DELETE FROM suggestion_votes WHERE suggestion_id=? AND user_id=?", (sid, user_id))
                return 0
            self.c.execute("INSERT OR REPLACE INTO suggestion_votes VALUES (?,?,?)", (sid, user_id, vote))
        return vote

    def suggestion_votes(self, sid):
        r = self.c.execute("SELECT COALESCE(SUM(vote=1),0) AS up, COALESCE(SUM(vote=-1),0) AS down "
                           "FROM suggestion_votes WHERE suggestion_id=?", (sid,)).fetchone()
        return r["up"], r["down"]
