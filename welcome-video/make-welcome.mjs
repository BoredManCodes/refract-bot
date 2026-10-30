// Usage: node make-welcome.mjs <userId> [--post <channelId>]
// Needs DISCORD_TOKEN in the environment. Writes assets/avatar.png, renders renders/welcome-<userId>.mp4.
import { mkdirSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const token = process.env.DISCORD_TOKEN;
const [userId, flag, channelId] = process.argv.slice(2);
if (!token || !userId) {
  console.error("usage: DISCORD_TOKEN=... node make-welcome.mjs <userId> [--post <channelId>]");
  process.exit(1);
}

const api = "https://discord.com/api/v10";
const headers = { Authorization: `Bot ${token}`, "User-Agent": "DiscordBot (refract-bot, 1.0)" };

const user = await (await fetch(`${api}/users/${userId}`, { headers })).json();
if (!user.id) throw new Error(`user lookup failed: ${JSON.stringify(user)}`);
const name = user.global_name || user.username;

const avatarUrl = user.avatar
  ? `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png?size=512`
  : `https://cdn.discordapp.com/embed/avatars/${Number((BigInt(user.id) >> 22n) % 6n)}.png`;
const img = Buffer.from(await (await fetch(avatarUrl)).arrayBuffer());

mkdirSync(join(here, "assets"), { recursive: true });
mkdirSync(join(here, "renders"), { recursive: true });
writeFileSync(join(here, "assets", "avatar.png"), img);

// the CLI's file-based variables are broken (io.readFile), so pass JSON inline; strip cmd.exe metacharacters
const safeName = name.replace(/["%^&|<>\\]/g, "").slice(0, 28) || user.username;
const varsArg = `"${JSON.stringify({ name: safeName }).replace(/"/g, '\\"')}"`;
const out = join(here, "renders", `welcome-${user.id}.mp4`);
execFileSync(
  "npx",
  ["hyperframes", "render", "--variables", varsArg, "-o", out],
  { cwd: here, stdio: "inherit", shell: true },
);
console.log(out);

if (flag === "--post" && channelId) {
  const form = new FormData();
  form.append("payload_json", JSON.stringify({
    content: `Welcome to Refract SMP, <@${user.id}>!`,
    allowed_mentions: { users: [user.id] },
  }));
  form.append("files[0]", new Blob([await (await import("node:fs/promises")).readFile(out)], { type: "video/mp4" }), "welcome.mp4");
  const res = await fetch(`${api}/channels/${channelId}/messages`, { method: "POST", headers, body: form });
  console.log("post", res.status, res.ok ? "" : await res.text());
}
