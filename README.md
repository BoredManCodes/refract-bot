# Refract SMP bot

Staff applications and quarterly Head Admin elections.

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
