# Refract SMP bot

Staff applications, quarterly Head Admin elections, support tickets, moderation and suggestions.

## Commands
| Command | Who | What |
|---|---|---|
| /apply | member | Staff application form. Posts to #staff-applications with Accept/Deny. |
| /election start | owner | Open nominations now. |
| /election schedule YYYY-MM-DD | owner | Auto-open nominations on a date. |
| /election sethead | owner | Appoint the first Head Admin. |
| /election runoff | owner | 48 hour runoff after a tie. |
| /election status | anyone | Show candidates. |
| /withdraw | candidate | Leave the ballot during nominations. |
| /adminlog | staff | Post an action and reason to #admin-actions. Owner can set `admin` to log it under another admin's name. |
| /ticket panel | owner | Post the "Open a ticket" button in the current channel. |
| /setup | owner | Create the Head Admin role and the four channels (incl. private #mod-log). |
| /update | owner | Pull the latest code (rolls back if pip or compile fails) and restart if anything changed. |
| /restart | owner | Restart the bot without updating. |

After an election closes with a winner, the bot schedules the next one (`CYCLE_DAYS` minus 21 days).

## DiscordSRV
Map the Head Admin and Admin Discord roles to LuckPerms groups in DiscordSRV's synchronization.yml.
The bot only changes Discord roles.

## Not built
Playtime gate. Needs a data source (Plan, LuckPerms or a stats plugin).

## Deploy (BTS)
    cd /root/refract-bot && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env   # fill DISCORD_TOKEN
    cp refract-bot.service /etc/systemd/system/ && systemctl enable --now refract-bot
Enable the Server Members intent on the application in the Discord developer portal.

## Welcome GIFs
On member join the bot posts a prism-themed GIF with the member's avatar and name in `welcome_channel_id` (`WELCOME_CHANNEL_ID`).
`POST /welcome` on the website API with `{"user_id": "...", "post": false}` returns the GIF; `"post": true` posts it to the channel instead.
On member leave the bot posts a goodbye GIF (the avatar sprints out of the prism) to the same channel; pass `"kind": "leave"` to `/welcome` to get or post it manually.
`welcome/template_leave.mp4` is the template reversed: `ffmpeg -i welcome/template.mp4 -vf reverse -an -c:v libx264 -crf 16 -pix_fmt yuv420p welcome/template_leave.mp4`.
Runtime needs `ffmpeg` on PATH (`apt install ffmpeg`) and Pillow. The template is `welcome/template.mp4`;
to change the design edit `welcome-video/index.html` and re-render with `npx hyperframes render --variables '{"bare":true}' -o ../welcome/template.mp4`.

## Tickets
The panel button opens a form (Game or Discord, plus a description). The bot makes `ticket-0001-name` under the Support category (found by name, or created by `/setup`), visible to the opener and Admin/Head Admin/Staff.
The first message has an embed, a red Close Ticket button (opener or staff, with a confirm step) and an Add User button (anyone in the ticket can add people).
On close the bot saves an HTML transcript styled like Discord's dark theme, posts it to `#ticket-logs` (created by `/setup`) with the file attached, DMs it to the opener, then deletes the channel.
Images are copied into the page (up to 4 MB each, 12 MB total) because Discord attachment links expire.
Transcripts need a Discord login (OAuth2 `identify`). Only people who sent a message in the ticket, were added to it, or opened it can view one, plus staff. Anyone viewing has a "Share link" button that creates a public link (`/s/<token>`) which opens without login; "Stop sharing" revokes it.
Setup: set `TRANSCRIPT_BASE_URL` to the public prefix that the Worker/tunnel maps to the bot API's `/transcripts` (so `<base>/login`, `<base>/callback`, `<base>/<token>` and `<base>/s/<token>` all reach the bot), set `DISCORD_CLIENT_SECRET`, and add `<TRANSCRIPT_BASE_URL>/callback` under OAuth2 redirects in the developer portal.
Run `/setup` once after updating so `#ticket-logs` exists, then `/ticket panel`.

## Moderation
Code: `automod.py` (rule logic, no Discord), `modtools.py` (cases, AutoMod, raid mode, lockdown, logs), `suggestions.py`.
The bot needs Kick, Ban, Moderate Members, Manage Messages, Manage Channels, Manage Roles, Manage Server and View Audit Log, and its role must sit above the roles it moderates. Mod commands work for Staff, Admin and Head Admin; settings commands need Admin or above (`(admin)` below).

| Command | What |
|---|---|
| /warn, /mute, /unmute, /kick, /ban, /unban, /note | Cases. Durations are `30m`, `2h`, `7d`, `1h30m`. `/mute` is a Discord timeout (28 days at most). `/ban duration:7d` is a temporary ban that lifts itself. The member is DMed first. |
| /history, /case view, /case reason, /case delete (admin), /clearwarns | Full infraction history with active/expired state. |
| /purge | Delete recent messages, filtered by member, text, bots, files or links. |
| /slowmode, /lock, /unlock | Per channel. Locking denies @everyone and any role that explicitly lets people talk, keeps Staff roles talking, and unlocking restores the exact previous overwrites. |
| /lockdown start/end/status/exempt (admin) | Locks every public text channel at once with a notice; saved overwrites survive a restart. |
| /raidmode on/off/config/status/sweep (admin) | Raid mode kicks, bans or mutes every new member. Starts by itself when N members join within S seconds (default 8 in 20) and can lock the server too. `sweep` kicks recent joiners who have no roles, after a confirm button. |
| /joingate set/status (admin) | Account age limit (days) and optional "no profile picture" check; action is kick, ban, or mute until the account is old enough. |
| /automod status/toggle/rule/add/remove/list/exempt_channel/exempt_role/warn_ladder/options | See below. |
| /logs set/reset/status | Pick a channel per log type. |
| /sticky set/exclude (admin) | Restore a member's roles when they rejoin. |
| /suggest, /suggestion approve/deny/consider/implement/info/config | Suggestions with vote buttons, anonymous mode, a decision log, and a DM to the author. |

**AutoMod** is off until `/automod toggle enabled:true`. Rules: spam, duplicates, mentions, emojis, caps, newlines, attachments, invites, links, words, zalgo. Each has its own on/off, action (delete only, warn, mute, kick, ban), mute length and threshold; channel and role exemptions apply to all rules or one. Staff roles are never checked. The links rule is a blocklist (default: a few IP-logger domains) or an allowlist. The words list starts empty; entries match whole words with look-alike characters, accents and stretched letters folded (`b4dw0rd`, `baaadword`); `bad*` is a wildcard, `~bad` matches inside other words and across punctuation. Warnings count towards a ladder (default: 3 warns mute 1h, 5 mute 24h, 7 kick) over the last 30 days.

**Logs** are split by type (messages, members, roles, moderation, invites, voice, channels, automod, commands). Anything without its own channel goes to `#mod-log`; voice stays off until given a channel. Deleted and edited message text comes from the bot's cache of the last 5000 messages, so messages from before a restart show without text. Bans, unbans and timeouts done in Discord's own UI are added to the history too.

`/setup` now also creates `#suggestions` and `#suggestion-log`.

## Web dashboard
Runs inside the bot process (FastAPI, Jinja2, htmx, Tabler; Authlib for the Discord OAuth2 login). Off unless `DASH_BASE_URL`, `DISCORD_CLIENT_SECRET` and `SESSION_SECRET` are set. Generate the secret with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`; it signs the login cookies, so it must differ from `BOT_API_TOKEN`, which the website also holds.
It listens on `127.0.0.1:DASH_PORT` (default 8792); put a Cloudflare Tunnel or reverse proxy in front of it and set `DASH_BASE_URL` to that public URL. Add `<DASH_BASE_URL>/auth/callback` under OAuth2 redirects in the developer portal. The login only asks for `identify`; membership and roles come from the bot's own view of the server.

Access: the server owner (and anyone in `OWNER_IDS`) can do everything. Everyone else needs a grant from the owner under **Permissions**, given to a role or to one member (a member gets the union of their own grants and their roles' grants):

| Permission | Allows |
|---|---|
| tickets.view | Browse tickets, read open conversations and closed transcripts |
| tickets.manage | Open a ticket for a member, reply, add or remove people, close, post the panel, make or revoke share links (includes view) |
| applications.review | Read and accept or deny staff applications |
| elections.manage | Start, schedule and run elections, appoint the Head Admin |
| members.view | Member list and invite leaderboard |
| adminlog.post | Post to the public admin log |
| settings.edit | Choose which roles and channels the bot uses |
| moderation.view | Moderation cases, a member's full history, and suggestions |
| moderation.act | Warn, mute, kick, ban, unban, add notes, edit case reasons, clear warnings, decide suggestions (includes view) |
| moderation.admin | AutoMod rules, lists, warning ladder and exemptions; lockdown, raid mode and join gate; log channels, sticky roles and suggestion setup; delete cases (includes act and view) |

The moderation pages use the same code as the slash commands, so the hierarchy checks, DMs, mod log embeds and timed expiries behave the same. The action form passes through the dashboard member's own roles: they cannot moderate someone whose top role is not below theirs, and nobody can moderate the owner or a role above the bot. Starting a lockdown from the page waits until every channel is done, which can take a while on a big server.

Permissions, the audit log and System (Update and Restart buttons, same as /update and /restart) are owner-only. Every change made from the dashboard is written to the audit log. Forms carry a CSRF token and sessions last 7 days; access is re-checked against the live server roles on every request, so removing a role takes effect at once.
