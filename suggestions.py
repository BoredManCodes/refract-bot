"""Member suggestions: /suggest posts an embed with vote buttons; staff decide with /suggestion and the result lands in a decision log."""
import time
from datetime import datetime

import discord
from discord import app_commands

from db import now

bot = db = None
_is_staff = _is_support = None
_last = {}  # user id -> time of their last suggestion

COOLDOWN = 60
STATUS = {"pending": (discord.Colour.blurple(), "Pending"), "approved": (discord.Colour.green(), "Approved"),
          "denied": (discord.Colour.red(), "Denied"), "considered": (discord.Colour.orange(), "Considered"),
          "implemented": (discord.Colour.teal(), "Implemented")}


def init(client, database, is_staff, is_support):
    global bot, db, _is_staff, _is_support
    bot, db, _is_staff, _is_support = client, database, is_staff, is_support


def is_anonymous():
    return db.get("suggest_anonymous") == "1"


def suggestion_embed(row, guild):
    colour, label = STATUS[row["status"]]
    e = discord.Embed(title=f"Suggestion #{row['id']}", description=row["content"], colour=colour,
                      timestamp=datetime.fromisoformat(row["created_at"]))
    if row["anonymous"]:
        e.set_author(name="Anonymous")
    else:
        m = guild.get_member(row["user_id"])
        e.set_author(name=m.display_name if m else "Former member", icon_url=m.display_avatar.url if m else None)
    if row["status"] != "pending":
        e.add_field(name=label, value=(row["reason"] or "No reason given")[:1000], inline=False)
    return e


class SuggestionView(discord.ui.View):
    """The two vote buttons. Registered once with default labels so old posts keep working after a restart."""

    def __init__(self, up=0, down=0, closed=False):
        super().__init__(timeout=None)
        self.up.label, self.down.label = str(up), str(down)
        self.up.disabled = self.down.disabled = closed

    async def vote(self, interaction, value):
        row = db.suggestion_by_message(interaction.message.id)
        if row is None or row["status"] != "pending":
            return await interaction.response.send_message("Voting on this suggestion is closed.", ephemeral=True)
        db.vote_suggestion(row["id"], interaction.user.id, value)
        up, down = db.suggestion_votes(row["id"])
        await interaction.response.edit_message(embed=suggestion_embed(row, interaction.guild), view=SuggestionView(up, down))

    @discord.ui.button(emoji="\N{THUMBS UP SIGN}", style=discord.ButtonStyle.secondary, custom_id="sug:up")
    async def up(self, interaction, button):
        await self.vote(interaction, 1)

    @discord.ui.button(emoji="\N{THUMBS DOWN SIGN}", style=discord.ButtonStyle.secondary, custom_id="sug:down")
    async def down(self, interaction, button):
        await self.vote(interaction, -1)


@app_commands.command(name="suggest", description="Suggest an idea for the server")
@app_commands.describe(idea="What you would like to see")
async def suggest(interaction: discord.Interaction, idea: app_commands.Range[str, 5, 1500]):
    ch = interaction.guild.get_channel(db.get_int("suggestions_channel_id") or 0)
    if ch is None:
        return await interaction.response.send_message("Suggestions are not set up yet.", ephemeral=True)
    wait = COOLDOWN - (time.monotonic() - _last.get(interaction.user.id, -COOLDOWN))
    if wait > 0 and not _is_support(interaction.user):
        return await interaction.response.send_message(f"Wait {int(wait) + 1} seconds before suggesting again.", ephemeral=True)
    _last[interaction.user.id] = time.monotonic()
    sid = db.add_suggestion(interaction.user.id, idea, is_anonymous())
    try:
        msg = await ch.send(embed=suggestion_embed(db.suggestion(sid), interaction.guild), view=SuggestionView())
    except discord.HTTPException:
        db.drop_suggestion(sid)
        return await interaction.response.send_message("I could not post in the suggestions channel.", ephemeral=True)
    db.set_suggestion_message(sid, ch.id, msg.id)
    await interaction.response.send_message(f"Posted as suggestion #{sid}: {msg.jump_url}", ephemeral=True)


group = app_commands.Group(name="suggestion", description="Handle member suggestions")


async def decide(interaction, number, status, reason):
    if not _is_support(interaction.user):
        return await interaction.response.send_message("Not allowed.", ephemeral=True)
    row = db.suggestion(number)
    if row is None:
        return await interaction.response.send_message("No such suggestion.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    db.decide_suggestion(number, status, interaction.user.id, reason[:1000])
    row = db.suggestion(number)
    guild = interaction.guild
    up, down = db.suggestion_votes(number)
    ch = guild.get_channel(row["channel_id"] or 0)
    jump = ""
    if ch is not None:
        try:
            msg = await ch.fetch_message(row["message_id"])
            await msg.edit(embed=suggestion_embed(row, guild), view=SuggestionView(up, down, closed=True))
            jump = msg.jump_url
        except discord.HTTPException:
            pass
    colour, label = STATUS[status]
    e = discord.Embed(title=f"Suggestion #{number} {label.lower()}", colour=colour, timestamp=now(), description=row["content"][:1500])
    e.add_field(name="Decided by", value=interaction.user.mention)
    e.add_field(name="Votes", value=f"{up} up, {down} down")
    e.add_field(name="Suggested by", value=f"<@{row['user_id']}>" if not row["anonymous"] else "Anonymous")
    if reason:
        e.add_field(name="Reason", value=reason[:1000], inline=False)
    if jump:
        e.add_field(name="Original", value=f"[Jump]({jump})", inline=False)
    log = guild.get_channel(db.get_int("suggestion_log_channel_id") or 0)
    if log is not None:
        try:
            await log.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
    author = guild.get_member(row["user_id"])
    if author is not None:
        try:
            await author.send(embed=discord.Embed(title=f"Your suggestion was {label.lower()}", colour=colour, description=row["content"][:1500])
                              .add_field(name="Reason", value=reason[:1000] or "No reason given", inline=False))
        except discord.HTTPException:
            pass
    await interaction.followup.send(f"Suggestion #{number} marked {label.lower()}.", ephemeral=True)


def _decision(status):
    async def cmd(interaction: discord.Interaction, number: int, reason: str = ""):
        await decide(interaction, number, status, reason)
    return cmd


for _name, _status, _text in (("approve", "approved", "Approve a suggestion"), ("deny", "denied", "Deny a suggestion"),
                              ("consider", "considered", "Mark a suggestion as under consideration"),
                              ("implement", "implemented", "Mark a suggestion as done")):
    group.command(name=_name, description=_text)(_decision(_status))


@group.command(name="info", description="Who wrote a suggestion, even an anonymous one")
async def info(interaction: discord.Interaction, number: int):
    if not _is_support(interaction.user):
        return await interaction.response.send_message("Not allowed.", ephemeral=True)
    row = db.suggestion(number)
    if row is None:
        return await interaction.response.send_message("No such suggestion.", ephemeral=True)
    up, down = db.suggestion_votes(number)
    await interaction.response.send_message(f"#{number} by <@{row['user_id']}>, {row['status']}, {up} up and {down} down.", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


@group.command(name="config", description="Suggestion channels and anonymous mode (admin)")
@app_commands.describe(channel="Where suggestions are posted", log_channel="Where decisions are logged",
                       anonymous="Hide the author's name on new suggestions")
async def config(interaction: discord.Interaction, channel: discord.TextChannel = None, log_channel: discord.TextChannel = None,
                 anonymous: bool = None):
    if not _is_staff(interaction.user):
        return await interaction.response.send_message("Not allowed.", ephemeral=True)
    if channel:
        db.put("suggestions_channel_id", channel.id)
    if log_channel:
        db.put("suggestion_log_channel_id", log_channel.id)
    if anonymous is not None:
        db.put("suggest_anonymous", "1" if anonymous else "0")
    ch, lg = db.get_int("suggestions_channel_id"), db.get_int("suggestion_log_channel_id")
    await interaction.response.send_message(
        f"Suggestions: {f'<#{ch}>' if ch else 'not set'}. Decision log: {f'<#{lg}>' if lg else 'not set'}. "
        f"Anonymous: {'yes' if is_anonymous() else 'no'}.", ephemeral=True)


COMMANDS = (suggest, group)


def register(tree, guild):
    bot.add_view(SuggestionView())
    for cmd in COMMANDS:
        tree.add_command(cmd, guild=guild)
