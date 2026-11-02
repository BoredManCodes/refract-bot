"""Ticket transcripts: plain message snapshots in, one standalone Discord-styled HTML page out.

A snapshot is a dict so this module never touches discord.py:
  {"id", "created" (ISO), "edited" (bool), "content", "system" (text or None),
   "author": {"id", "name", "avatar", "colour" (#rrggbb or None), "bot"},
   "reply": {"name", "colour", "avatar", "content"} or None,
   "attachments": [{"name", "url", "size", "image" (bool)}],
   "embeds": [{"title", "url", "description", "color", "author", "fields", "image", "thumbnail", "footer", "timestamp"}],
   "components": [[{"label", "style"}]],
   "reactions": [{"emoji", "url" (custom emoji image or None), "count"}]}
ctx resolves mentions: {"users": {id: name}, "roles": {id: (name, colour)}, "channels": {id: name}}.
"""
import hashlib
import hmac
import html
import json
import re
import time
from datetime import datetime, timezone

GROUP_SECONDS = 7 * 60  # Discord stacks messages from one author sent within seven minutes

CSS = """
:root{--bg:#313338;--bg2:#2b2d31;--bg3:#1e1f22;--text:#dbdee1;--head:#f2f3f5;--muted:#949ba4;--link:#00a8fc;--hover:#2e3035;
--divider:#3f4147;--quote:#4e5058;--blurple:#5865f2}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text)}
body{font-family:'gg sans','Noto Sans','Helvetica Neue',Helvetica,Arial,sans-serif;font-size:16px;line-height:1.375rem;
-webkit-font-smoothing:antialiased}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.top{background:var(--bg);border-bottom:1px solid var(--bg3);box-shadow:0 1px 0 rgba(2,2,2,.2);padding:12px 16px;display:flex;
align-items:center;gap:8px;position:sticky;top:0;z-index:2;min-height:48px}
.top .hash{color:var(--muted);font-size:24px;line-height:24px;font-weight:400}
.top .name{color:var(--head);font-weight:600;font-size:16px}
.top .sep{width:1px;height:24px;background:#3f4147;margin:0 8px}
.top .topic{color:#b5bac1;font-size:14px;font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.info{margin:16px;padding:16px;background:var(--bg2);border-radius:8px;display:grid;
grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px 24px;max-width:900px}
.info b{display:block;color:#b5bac1;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.02em;margin-bottom:2px}
.info span{color:var(--head);font-size:14px;word-break:break-word}
.messages{padding-bottom:24px}
.msg{position:relative;padding:2px 48px 2px 72px;min-height:1.375rem;margin-top:1.0625rem}
.msg.compact{margin-top:0}
.msg:hover{background:var(--hover)}
.msg .avatar{position:absolute;left:16px;top:2px;width:40px;height:40px;border-radius:50%;margin-top:calc(4px - 2px);background:var(--bg3);
object-fit:cover}
.msg .stamp{position:absolute;left:0;width:56px;text-align:right;font-size:.6875rem;line-height:1.375rem;color:var(--muted);display:none}
.msg.compact:hover .stamp{display:block}
.head{line-height:1.375rem;display:block;overflow:hidden}
.author{color:var(--head);font-weight:500;font-size:1rem;margin-right:.25rem}
.author:hover{text-decoration:underline;cursor:pointer}
.tag{display:inline-flex;align-items:center;background:var(--blurple);color:#fff;font-size:.625rem;line-height:.9375rem;font-weight:500;
border-radius:.1875rem;padding:0 .275rem;margin-right:.25rem;height:.9375rem;vertical-align:.075em;text-transform:uppercase}
.time{color:var(--muted);font-size:.75rem;font-weight:500;margin-left:.25rem;vertical-align:baseline}
.content{white-space:pre-wrap;word-wrap:break-word;overflow-wrap:break-word;font-weight:400;color:var(--text)}
.edited{color:var(--muted);font-size:.625rem;margin-left:.25rem;user-select:none}
.reply{position:relative;display:flex;align-items:center;gap:4px;margin:0 0 2px;font-size:.875rem;color:#b5bac1;white-space:nowrap;
overflow:hidden;line-height:1.125rem}
.reply:before{content:"";position:absolute;left:-36px;top:50%;width:33px;height:10px;margin-top:-1px;border-left:2px solid #4e5058;
border-top:2px solid #4e5058;border-top-left-radius:6px}
.reply img{width:16px;height:16px;border-radius:50%}
.reply .rn{font-weight:500;opacity:.64;margin-right:2px}
.reply .rt{overflow:hidden;text-overflow:ellipsis;color:#b5bac1}
.sys{color:var(--muted);font-size:1rem}
.sys .arrow{display:inline-block;width:16px;margin-right:6px;color:#23a559;font-weight:700}
.day{display:flex;align-items:center;margin:1.5rem 1rem .5rem 1rem;border-top:1px solid var(--divider);height:0;justify-content:center}
.day span{background:var(--bg);color:var(--muted);font-size:.75rem;font-weight:600;line-height:.8125rem;padding:2px 4px;margin-top:-1px}
strong{font-weight:700;color:inherit}u{text-decoration:underline}s{text-decoration:line-through}
code{font-family:Consolas,'Andale Mono WT','Andale Mono','Lucida Console','Lucida Sans Typewriter','DejaVu Sans Mono',
'Bitstream Vera Sans Mono','Liberation Mono',Monaco,'Courier New',monospace}
code.inline{background:var(--bg2);padding:.2em;margin:-.2em 0;border-radius:3px;font-size:85%;white-space:pre-wrap;text-indent:0}
pre{margin:6px 0 0;max-width:90%;background:var(--bg2);border:1px solid var(--bg3);border-radius:4px;padding:.5em;white-space:pre-wrap;
font-size:.875rem;line-height:1.125rem}
pre code{background:none;padding:0;margin:0;font-size:.875rem;white-space:pre-wrap;display:block}
.quote{display:flex;margin:0}.quote:before{content:"";width:4px;border-radius:4px;background:var(--quote);flex-shrink:0;margin-right:12px}
.quote div{padding:0 8px 0 0}
h1,h2,h3{margin:8px 0 0;color:var(--head);font-weight:700;line-height:1.375em}
h1{font-size:1.5rem}h2{font-size:1.25rem}h3{font-size:1rem}
.sub{font-size:.75rem;line-height:1rem;color:var(--muted)}
ul.l{margin:4px 0 0;padding-left:1.5em}
.mention{background:rgba(88,101,242,.3);color:#c9cdfb;border-radius:3px;padding:0 2px;font-weight:500;unicode-bidi:plaintext}
.mention:hover{background:var(--blurple);color:#fff;cursor:pointer}
.timestamp{background:rgba(78,80,88,.48);border-radius:3px;padding:0 2px}
.spoiler{background:#1e1f22;color:transparent;border-radius:3px;cursor:pointer;user-select:none}
.spoiler.on{background:rgba(78,80,88,.48);color:inherit;user-select:auto}
.spoiler:not(.on) *{opacity:0}
img.emoji{width:1.375em;height:1.375em;vertical-align:bottom;object-fit:contain;margin:0 .05em;display:inline-block}
.jumbo img.emoji,.jumbo .uni{width:3rem;height:3rem;font-size:2.5rem;line-height:3rem}
.att{margin-top:4px}
.att img.media{display:block;max-width:min(400px,100%);max-height:300px;border-radius:8px;object-fit:contain;background:var(--bg2)}
.file{display:flex;align-items:center;gap:8px;background:var(--bg2);border:1px solid var(--bg3);border-radius:4px;padding:10px;max-width:432px;
margin-top:4px;font-size:.875rem}
.file .fi{width:30px;height:40px;border-radius:3px;background:#4e5058;flex:none}
.file small{display:block;color:var(--muted);font-size:.75rem}
.embed{display:grid;grid-template-columns:auto;margin-top:8px;max-width:520px;background:var(--bg2);border:1px solid rgba(46,46,51,.6);
border-left:4px solid var(--divider);border-radius:4px;padding:8px 16px 16px 12px;white-space:normal}
.embed .row{display:flex;gap:16px}.embed .main{min-width:0;flex:1}
.embed .eauthor{display:flex;align-items:center;margin-top:8px;font-size:.875rem;font-weight:600;color:var(--head)}
.embed .eauthor img{width:24px;height:24px;border-radius:50%;margin-right:8px}
.embed .etitle{margin-top:8px;font-size:1rem;font-weight:600;color:var(--head)}
.embed .etitle a{color:var(--link)}
.embed .edesc{margin-top:8px;font-size:.875rem;font-weight:400;white-space:pre-wrap;color:#dbdee1}
.embed .fields{display:grid;grid-template-columns:repeat(12,1fr);gap:8px;margin-top:8px}
.embed .field{grid-column:1/13;min-width:0;font-size:.875rem}
.embed .field.inline{grid-column:span 4}
.embed .fn{font-weight:600;color:var(--head);margin-bottom:2px}
.embed .fv{font-weight:400;white-space:pre-wrap;color:#dbdee1}
.embed .thumb{width:80px;height:80px;border-radius:4px;object-fit:cover;margin-top:8px;flex:none}
.embed .eimg{display:block;max-width:100%;max-height:300px;border-radius:4px;margin-top:16px;object-fit:contain}
.embed .efoot{display:flex;align-items:center;margin-top:8px;font-size:.75rem;font-weight:500;color:#b5bac1}
.embed .efoot i{font-style:normal;margin:0 4px}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:32px;padding:2px 16px;border-radius:3px;font-size:.875rem;
font-weight:500;color:#fff;line-height:16px;user-select:none}
.btn.primary{background:#5865f2}.btn.secondary{background:#4e5058}.btn.success{background:#248046}.btn.danger{background:#da373c}
.btn.link{background:#4e5058}
.rx{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.rx div{display:flex;align-items:center;gap:6px;height:24px;padding:0 6px;border-radius:8px;background:var(--bg2);border:1px solid transparent;
color:#b5bac1;font-size:.875rem;font-weight:500}
.rx img{width:16px;height:16px}
.end{margin:24px 16px 0;padding-top:16px;border-top:1px solid var(--divider);color:var(--muted);font-size:.75rem}
::-webkit-scrollbar{width:16px}::-webkit-scrollbar-track{background:var(--bg2)}
::-webkit-scrollbar-thumb{background:#1a1b1e;border:4px solid var(--bg2);border-radius:8px}
@media(max-width:600px){.msg{padding-right:16px}.embed .field.inline{grid-column:1/13}}
"""

JS = """
document.querySelectorAll('.spoiler').forEach(function(e){e.addEventListener('click',function(){e.classList.add('on')})});
document.querySelectorAll('time[datetime]').forEach(function(t){
  var d=new Date(t.getAttribute('datetime'));if(isNaN(d))return;
  var k=t.getAttribute('data-f'),o;
  if(k==='s')o={hour:'numeric',minute:'2-digit'};
  else if(k==='d')o={day:'2-digit',month:'2-digit',year:'numeric'};
  else if(k==='D')o={dateStyle:'long'};
  else if(k==='t')o={timeStyle:'short'};
  else if(k==='T')o={timeStyle:'medium'};
  else if(k==='F')o={dateStyle:'full',timeStyle:'short'};
  else o={dateStyle:'long',timeStyle:'short'};
  t.textContent=d.toLocaleString(undefined,o);t.title=d.toLocaleString(undefined,{dateStyle:'full',timeStyle:'medium'});
});
"""

_FRAG = "\x00{}\x00"
_URL = re.compile(r"https?://[^\s<>\x00]+")
_EMOJI_ONLY = re.compile(r"^(?:\s|<a?:\w+:\d+>|[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍])+$")
_STYLES = {1: "primary", 2: "secondary", 3: "success", 4: "danger", 5: "link"}


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def safe_url(u):
    u = str(u or "")
    return u if u.startswith(("https://", "http://", "data:image/")) else ""


def parse_dt(s):
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def rgb(colour):
    c = (colour or "").lstrip("#")
    if len(c) != 6:
        return None
    try:
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        return None


def _time_tag(dt, kind):
    fallback = {"d": "%d/%m/%Y", "D": "%B %d, %Y", "t": "%I:%M %p", "T": "%I:%M:%S %p", "F": "%A, %B %d, %Y %I:%M %p",
                "s": "%I:%M %p"}.get(kind, "%B %d, %Y %I:%M %p")
    return f'<time datetime="{dt.isoformat()}" data-f="{kind}">{esc(dt.strftime(fallback).lstrip("0"))} UTC</time>'


def render_markdown(text, ctx, embed=False):
    """Discord markdown to HTML. Everything user-supplied is escaped before any tag is added."""
    ctx = ctx or {}
    users, roles, channels = ctx.get("users", {}), ctx.get("roles", {}), ctx.get("channels", {})
    frags = []

    def hold(h):
        frags.append(h)
        return _FRAG.format(len(frags) - 1)

    text = str(text or "")
    text = re.sub(r"```(?:([\w+#-]+)\n)?(.*?)```",
                  lambda m: hold(f"<pre><code>{esc(m.group(2).strip(chr(10)))}</code></pre>"), text, flags=re.S)
    text = re.sub(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)",
                  lambda m: hold(f'<code class="inline">{esc(m.group(2).strip())}</code>'), text, flags=re.S)
    text = esc(text)

    def user(m):
        return hold(f'<span class="mention">@{esc(users.get(m.group(1), "Unknown User"))}</span>')

    def role(m):
        name, colour = roles.get(m.group(1), ("Unknown Role", None))
        c = rgb(colour)
        if not c:
            return hold(f'<span class="mention">@{esc(name)}</span>')
        return hold(f'<span class="mention" style="color:rgb{c};background:rgba({c[0]},{c[1]},{c[2]},.1)">@{esc(name)}</span>')

    def emoji(m):
        ext = "gif" if m.group(1) else "webp"
        return hold(f'<img class="emoji" alt=":{m.group(2)}:" title=":{m.group(2)}:" '
                    f'src="https://cdn.discordapp.com/emojis/{m.group(3)}.{ext}?size=48">')

    def stamp(m):
        try:
            dt = datetime.fromtimestamp(int(m.group(1)), timezone.utc)
        except (ValueError, OverflowError, OSError):
            return m.group(0)
        return hold(f'<span class="timestamp">{_time_tag(dt, m.group(2) or "f")}</span>')

    text = re.sub(r"&lt;@!?(\d+)&gt;", user, text)
    text = re.sub(r"&lt;@&amp;(\d+)&gt;", role, text)
    text = re.sub(r"&lt;#(\d+)&gt;", lambda m: hold(f'<span class="mention">#{esc(channels.get(m.group(1), "unknown"))}</span>'), text)
    text = re.sub(r"&lt;(a?):(\w+):(\d+)&gt;", emoji, text)
    text = re.sub(r"&lt;t:(-?\d+)(?::([tTdDfFR]))?&gt;", stamp, text)
    text = re.sub(r"@(everyone|here)\b", lambda m: hold(f'<span class="mention">@{m.group(1)}</span>'), text)

    def link(url, label=None):
        trail = ""
        while url and url[-1] in ".,;:!?)'\"" and not (url[-1] == ")" and "(" in url):
            trail = url[-1] + trail
            url = url[:-1]
        raw = html.unescape(url)
        if not safe_url(raw):
            return (label or url) + trail
        return hold(f'<a href="{esc(raw)}" target="_blank" rel="noopener noreferrer">{label or url}</a>') + trail

    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", lambda m: link(m.group(2), m.group(1)), text)
    text = re.sub(r"&lt;(https?://[^\s]+?)&gt;", lambda m: link(m.group(1)), text)
    text = _URL.sub(lambda m: link(m.group(0)), text)

    def inline(t):
        t = re.sub(r"\|\|(.+?)\|\|", r'<span class="spoiler">\1</span>', t, flags=re.S)
        t = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", t, flags=re.S)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t, flags=re.S)
        t = re.sub(r"__(.+?)__", r"<u>\1</u>", t, flags=re.S)
        t = re.sub(r"(?<!\*)\*(?![\s*])(.+?)(?<![\s*])\*(?!\*)", r"<em>\1</em>", t, flags=re.S)
        t = re.sub(r"(?<![\w])_(?![\s_])(.+?)(?<![\s_])_(?![\w])", r"<em>\1</em>", t, flags=re.S)
        t = re.sub(r"~~(.+?)~~", r"<s>\1</s>", t, flags=re.S)
        return t

    # block level, line by line; headings, lists and subtext only count at the start of a line
    out, quote, lines = [], [], text.split("\n")

    def flush_quote():
        if quote:
            out.append('<blockquote class="quote"><div>' + "\n".join(inline(q) for q in quote) + "</div></blockquote>")
            quote.clear()

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("&gt;&gt;&gt; "):
            flush_quote()
            quote.extend([ln[len("&gt;&gt;&gt; "):], *lines[i + 1:]])
            break
        if ln.startswith("&gt; "):
            quote.append(ln[5:])
            i += 1
            continue
        flush_quote()
        h = re.match(r"(#{1,3}) (.+)$", ln)
        s = re.match(r"-# (.+)$", ln)
        b = re.match(r"[-*] (.+)$", ln)
        if h:
            out.append(f"<h{len(h.group(1))}>{inline(h.group(2))}</h{len(h.group(1))}>")
        elif s:
            out.append(f'<div class="sub">{inline(s.group(1))}</div>')
        elif b:
            items = [b.group(1)]
            while i + 1 < len(lines) and (nb := re.match(r"[-*] (.+)$", lines[i + 1])):
                items.append(nb.group(1))
                i += 1
            out.append('<ul class="l">' + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
        else:
            out.append(inline(ln) + "\n" if i < len(lines) - 1 else inline(ln))
        i += 1
    flush_quote()
    result = "".join(out)
    # a heading/list/quote/code block already ends its own line
    result = re.sub(r"(</(?:h\d|ul|div|blockquote|pre)>)\n", r"\1", result)
    # frags can nest (a link holding a held code span), so restore until none are left
    for _ in range(4):
        result = re.sub("\x00(\\d+)\x00", lambda m: frags[int(m.group(1))], result)
    return result


def _name_style(colour):
    return f' style="color:{esc(colour)}"' if rgb(colour) else ""


def _embed(e, ctx):
    border = e.get("color") if rgb(e.get("color")) else None
    parts = []
    if e.get("author") and e["author"].get("name"):
        icon = safe_url(e["author"].get("icon"))
        parts.append('<div class="eauthor">' + (f'<img src="{esc(icon)}" alt="">' if icon else "") + esc(e["author"]["name"]) + "</div>")
    if e.get("title"):
        t = esc(e["title"])
        u = safe_url(e.get("url"))
        if u:
            t = f'<a href="{esc(u)}" target="_blank" rel="noopener noreferrer">{t}</a>'
        parts.append(f'<div class="etitle">{t}</div>')
    if e.get("description"):
        parts.append(f'<div class="edesc">{render_markdown(e["description"], ctx, True)}</div>')
    if e.get("fields"):
        fl = "".join(f'<div class="field{" inline" if f.get("inline") else ""}"><div class="fn">{esc(f["name"])}</div>'
                     f'<div class="fv">{render_markdown(f["value"], ctx, True)}</div></div>' for f in e["fields"])
        parts.append(f'<div class="fields">{fl}</div>')
    thumb = safe_url(e.get("thumbnail"))
    main = f'<div class="main">{"".join(parts)}</div>'
    row = f'<div class="row">{main}' + (f'<img class="thumb" src="{esc(thumb)}" alt="">' if thumb else "") + "</div>"
    img = safe_url(e.get("image"))
    foot = []
    if e.get("footer"):
        foot.append(esc(e["footer"]))
    dt = parse_dt(e.get("timestamp"))
    if dt:
        foot.append(_time_tag(dt, "x"))
    style = f' style="border-left-color:{esc(border)}"' if border else ""
    return (f'<div class="embed"{style}>{row}' + (f'<img class="eimg" src="{esc(img)}" alt="">' if img else "")
            + ('<div class="efoot">' + "<i>•</i>".join(foot) + "</div>" if foot else "") + "</div>")


def _message(m, ctx, compact):
    a = m.get("author") or {}
    dt = parse_dt(m.get("created"))
    cls = "msg compact" if compact else "msg"
    short = _time_tag(dt, "s") if dt else ""
    if m.get("system"):
        return (f'<div class="{cls}" id="m{esc(m.get("id"))}"><span class="stamp">{short}</span>'
                f'<div class="content sys"><span class="arrow">→</span>{render_markdown(m["system"], ctx)} '
                f'<span class="time">{_time_tag(dt, "x") if dt else ""}</span></div></div>')
    body = []
    r = m.get("reply")
    reply = ""
    if r is not None:
        ico = safe_url(r.get("avatar"))
        reply = ('<div class="reply">' + (f'<img src="{esc(ico)}" alt="">' if ico else "")
                 + f'<span class="rn"{_name_style(r.get("colour"))}>@{esc(r.get("name"))}</span>'
                 + f'<span class="rt">{render_markdown((r.get("content") or "Click to see attachment")[:120], ctx)}</span></div>')
    if m.get("content"):
        jumbo = " jumbo" if _EMOJI_ONLY.match(m["content"]) else ""
        edited = '<span class="edited">(edited)</span>' if m.get("edited") else ""
        body.append(f'<div class="content{jumbo}">{render_markdown(m["content"], ctx)}{edited}</div>')
    for att in m.get("attachments") or []:
        url = safe_url(att.get("url"))
        if att.get("image") and url:
            body.append(f'<div class="att"><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                        f'<img class="media" src="{esc(url)}" alt="{esc(att.get("name"))}"></a></div>')
        else:
            size = att.get("size") or 0
            nice = f"{size / 1048576:.2f} MB" if size >= 1048576 else f"{max(size, 1) / 1024:.1f} KB"
            link = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(att.get("name"))}</a>' if url else esc(att.get("name"))
            body.append(f'<div class="file"><div class="fi"></div><div>{link}<small>{nice}</small></div></div>')
    for e in m.get("embeds") or []:
        body.append(_embed(e, ctx))
    for row in m.get("components") or []:
        body.append('<div class="btns">' + "".join(
            f'<span class="btn {_STYLES.get(b.get("style"), "secondary")}">{esc(b.get("label"))}</span>' for b in row) + "</div>")
    if m.get("reactions"):
        body.append('<div class="rx">' + "".join(
            "<div>" + (f'<img src="{esc(safe_url(x["url"]))}" alt="">' if x.get("url") else f'<span class="uni">{esc(x["emoji"])}</span>')
            + f"<span>{x['count']}</span></div>" for x in m["reactions"]) + "</div>")
    if compact:
        return f'<div class="{cls}" id="m{esc(m.get("id"))}"><span class="stamp">{short}</span>{reply}{"".join(body)}</div>'
    avatar = safe_url(a.get("avatar"))
    tag = '<span class="tag">App</span>' if a.get("bot") else ""
    head = (f'<span class="head"><span class="author"{_name_style(a.get("colour"))}>{esc(a.get("name"))}</span>{tag}'
            f'<span class="time">{_time_tag(dt, "x") if dt else ""}</span></span>')
    return (f'<div class="{cls}" id="m{esc(m.get("id"))}">'
            + (f'<img class="avatar" src="{esc(avatar)}" alt="">' if avatar else '<div class="avatar"></div>')
            + f'{reply}{head}{"".join(body)}</div>')


def render_transcript(meta, messages, ctx=None):
    """meta: {"title", "channel", "info": [(label, value)], "footer"}. Returns a full HTML document."""
    out, prev, prev_day = [], None, None
    for m in messages:
        dt = parse_dt(m.get("created"))
        day = dt.date() if dt else None
        if day and day != prev_day:
            out.append(f'<div class="day"><span>{_time_tag(dt, "D")}</span></div>')
            prev_day, prev = day, None
        a = m.get("author") or {}
        pdt = parse_dt(prev.get("created")) if prev else None
        compact = bool(
            prev and dt and pdt and not m.get("reply") and not m.get("system") and not prev.get("system")
            and (prev.get("author") or {}).get("id") == a.get("id") and 0 <= (dt - pdt).total_seconds() < GROUP_SECONDS)
        out.append(_message(m, ctx, compact))
        prev = m
    info = "".join(f"<div><b>{esc(k)}</b><span>{esc(v)}</span></div>" for k, v in meta.get("info", []))
    title = esc(meta.get("title", "Ticket transcript"))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer">'
            f'<title>{title}</title>'
            f'<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            f'<link href="https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">'
            f'<style>{CSS}</style></head><body>'
            f'<div class="top"><span class="hash">#</span><span class="name">{esc(meta.get("channel", "ticket"))}</span>'
            f'<span class="sep"></span><span class="topic">{title}</span></div>'
            f'<div class="info">{info}</div><div class="messages">{"".join(out)}</div>'
            f'<div class="end">{esc(meta.get("footer", ""))}</div><script>{JS}</script></body></html>')


# ---------- web access: signed cookies and the share bar ----------

def sign(secret, value):
    """value|mac, for cookies and OAuth state. secret is bytes."""
    mac = hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{value}|{mac}"


def unsign(secret, signed):
    value, _, mac = str(signed or "").rpartition("|")
    good = hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()[:32]
    return value if value and hmac.compare_digest(mac.encode(), good.encode()) else None


def make_session(secret, user_id, ttl=7 * 86400, at=None):
    return sign(secret, f"u{user_id}.{int((at or time.time()) + ttl)}")


def read_session(secret, cookie, at=None):
    """User id from a session cookie, or None if forged or expired."""
    value = unsign(secret, cookie)
    m = re.fullmatch(r"u(\d+)\.(\d+)", value or "")
    if not m or int(m.group(2)) < (at or time.time()):
        return None
    return int(m.group(1))


def message_page(title, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><title>{esc(title)}</title><style>{CSS}'
            f'.box{{max-width:420px;margin:20vh auto;padding:24px;background:var(--bg2);border-radius:8px;text-align:center}}'
            f'.box h1{{font-size:20px;margin:0 0 8px}}.box p{{color:#b5bac1;margin:0 0 16px;font-size:14px}}'
            f'.box a.go{{display:inline-block;background:#5865f2;color:#fff;border-radius:3px;padding:8px 20px;font-weight:500}}'
            f'.box a.go:hover{{background:#4752c4;text-decoration:none}}</style></head><body><div class="box"><h1>{esc(title)}</h1>{body}</div></body></html>')


SHARE_BAR = r"""<div id="sb" style="position:fixed;right:16px;bottom:16px;z-index:5;background:#2b2d31;border:1px solid #1e1f22;border-radius:8px;
padding:10px 12px;box-shadow:0 8px 16px rgba(0,0,0,.24);font-size:14px;display:flex;gap:8px;align-items:center;max-width:calc(100vw - 32px)">
<span id="sbt" style="color:#b5bac1"></span>
<input id="sbu" readonly style="display:none;background:#1e1f22;border:0;border-radius:3px;color:#dbdee1;padding:6px 8px;width:260px;max-width:40vw">
<button id="sbc" class="btn primary" style="display:none;border:0;cursor:pointer">Copy</button>
<button id="sbs" class="btn primary" style="border:0;cursor:pointer"></button></div>
<script>(function(){var s=%(state)s,b=document.getElementById('sbs'),u=document.getElementById('sbu'),c=document.getElementById('sbc'),t=document.getElementById('sbt');
function draw(){if(s.url){t.textContent='Anyone with the link can view';u.style.display=c.style.display='';u.value=s.url;b.textContent='Stop sharing';b.className='btn danger'}
else{t.textContent='Only ticket members can view';u.style.display=c.style.display='none';b.textContent='Share link';b.className='btn primary'}}
function call(p){return fetch(location.pathname.replace(/\/$/,'')+p,{method:'POST',headers:{'X-Requested-With':'transcript'},credentials:'same-origin'}).then(function(r){return r.json()})}
b.onclick=function(){b.disabled=true;call(s.url?'/unshare':'/share').then(function(j){s.url=j.url||null;draw()}).catch(function(){t.textContent='Failed, try again'}).then(function(){b.disabled=false})};
c.onclick=function(){u.select();navigator.clipboard?navigator.clipboard.writeText(u.value):document.execCommand('copy');c.textContent='Copied';setTimeout(function(){c.textContent='Copy'},1500)};
draw()})()</script>"""


def with_share_bar(page, share_url):
    state = json.dumps({"url": share_url}).replace("<", "\\u003c")
    return page.replace("</body>", (SHARE_BAR % {"state": state}) + "</body>", 1)
