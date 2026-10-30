"""Welcome GIF: composite a member's avatar and name onto the pre-rendered prism template.

welcome/template.mp4 is rendered once from welcome-video/ with the `bare` variable (no avatar, no name).
At runtime only Pillow and ffmpeg are needed. Easing and timings mirror the GSAP timeline in welcome-video/index.html.
"""
import asyncio
import io
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent / "welcome"
TEMPLATE = HERE / "template.mp4"
FONT_MEDIUM = HERE / "Outfit-500.ttf"
FONT_BOLD = HERE / "Outfit-700.ttf"

SRC_W, SRC_H = 1920, 1080  # the template's canvas; every measurement below is in these pixels
OUT_W = 960
FPS = 15
SCALE = OUT_W / SRC_W
OUT_H = round(SRC_H * SCALE)

AVATAR_CENTRE = (1330, 460)
AVATAR_SIZE = 452
TEXT_CENTRE_X = 1330
DIM = (169, 164, 201)
WHITE = (255, 255, 255)

_render_lock = asyncio.Semaphore(1)


def power3_out(p):
    return 1 - (1 - p) ** 3


def back_out(s):
    def ease(p):
        p -= 1
        return p * p * ((s + 1) * p + s) + 1
    return ease


def progress(t, start, dur):
    return min(1.0, max(0.0, (t - start) / dur))


def circle_avatar(data, size):
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    side = min(img.size)
    img = img.crop(((img.width - side) // 2, (img.height - side) // 2,
                    (img.width + side) // 2, (img.height + side) // 2)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    img.putalpha(mask.resize((size, size), Image.LANCZOS))
    return img


def glyph_ok(font, ch):
    if ch.isspace():
        return True
    missing = font.getmask("\U0010ffff")
    m = font.getmask(ch)
    return not (m.size == missing.size and bytes(m) == bytes(missing))


def clean_name(name, fallback):
    """Outfit's bundled subset covers Latin only; drop anything else, then fall back."""
    font = ImageFont.truetype(str(FONT_BOLD), 40)
    out = "".join(c for c in name if glyph_ok(font, c)).strip()[:28]
    return out if len(out) >= 2 else "".join(c for c in fallback if glyph_ok(font, c)).strip()[:28] or "New member"


def spaced_width(font, text, spacing):
    return sum(font.getlength(c) + spacing for c in text)


def draw_text(layer, text, font, spacing, centre_x, baseline, fill, alpha):
    draw = ImageDraw.Draw(layer)
    x = centre_x - spaced_width(font, text, spacing) / 2
    colour = (*fill, round(255 * alpha))
    for c in text:
        draw.text((x, baseline), c, font=font, fill=colour, anchor="ls")
        x += font.getlength(c) + spacing


class Overlay:
    def __init__(self, avatar_bytes, name):
        k = SCALE
        self.avatar = circle_avatar(avatar_bytes, round(AVATAR_SIZE * k * 2))  # kept at 2x so the scale-in stays sharp
        self.name = name
        self.k = k
        small = round(34 * k)
        self.label_font = ImageFont.truetype(str(FONT_MEDIUM), small)
        self.label_spacing = 0.32 * small
        user_px = max(56, min(104, int(1000 / (len(name) * 0.58))))
        self.user_px = user_px
        self.user_font = ImageFont.truetype(str(FONT_BOLD), round(user_px * k))

    def frame(self, base, t):
        k = self.k
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))

        e = progress(t, 2.1, 0.6)
        if e > 0:
            eased = back_out(1.4)(e)
            size = max(1, round(AVATAR_SIZE * k * (0.7 + 0.3 * eased)))
            av = self.avatar.resize((size, size), Image.LANCZOS)
            a = min(1.0, max(0.0, eased))
            if a < 1:
                av.putalpha(av.getchannel("A").point(lambda v: round(v * a)))
            layer.alpha_composite(av, (round(AVATAR_CENTRE[0] * k - size / 2), round(AVATAR_CENTRE[1] * k - size / 2)))

        for text, font, spacing, start, dur, top_baseline, colour in (
            ("WELCOME TO", self.label_font, self.label_spacing, 2.6, 0.5, 779, DIM),
            (self.name, self.user_font, -0.02 * self.user_px * k, 2.75, 0.6, 797 + 0.91 * self.user_px, WHITE),
            ("REFRACT SMP", self.label_font, self.label_spacing, 3.0, 0.5, 957, DIM),
        ):
            p = power3_out(progress(t, start, dur))
            if p > 0:
                draw_text(layer, text, font, spacing, TEXT_CENTRE_X * k, (top_baseline + 24 * (1 - p)) * k, colour, p)

        base.alpha_composite(layer)
        return base


def _render(avatar_bytes, name):
    overlay = Overlay(avatar_bytes, name)
    decode = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(TEMPLATE), "-vf", f"fps={FPS},scale={OUT_W}:{OUT_H}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE)
    encode = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{OUT_W}x{OUT_H}", "-r", str(FPS),
         "-i", "-", "-vf", "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
         "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
         "-loop", "0", "-f", "gif", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    frame_bytes = OUT_W * OUT_H * 3
    out = io.BytesIO()
    try:
        import threading
        reader = threading.Thread(target=lambda: out.write(encode.stdout.read()))
        reader.start()
        i = 0
        while True:
            raw = decode.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            base = Image.frombytes("RGB", (OUT_W, OUT_H), raw).convert("RGBA")
            encode.stdin.write(overlay.frame(base, i / FPS).convert("RGB").tobytes())
            i += 1
        encode.stdin.close()
        reader.join()
        if encode.wait() != 0 or decode.wait() != 0 or i == 0:
            raise RuntimeError("ffmpeg failed while building the welcome GIF")
    finally:
        for p in (decode, encode):
            if p.poll() is None:
                p.kill()
    return out.getvalue()


async def make_gif(avatar_bytes, name, fallback_name=""):
    """GIF bytes for one member. Renders run one at a time so a join burst cannot starve the bot."""
    name = clean_name(name, fallback_name)
    async with _render_lock:
        return await asyncio.to_thread(_render, avatar_bytes, name)
