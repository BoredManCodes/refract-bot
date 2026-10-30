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
| /adminlog | staff | Post an action and reason to #admin-actions. |
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
