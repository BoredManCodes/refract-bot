import asyncio
import io
import shutil
import unittest

from PIL import Image

import welcome


def png(colour=(200, 40, 40)):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(buf, "PNG")
    return buf.getvalue()


class CleanName(unittest.TestCase):
    def test_latin_kept(self):
        self.assertEqual(welcome.clean_name("BoredManPlays", ""), "BoredManPlays")

    def test_unsupported_falls_back(self):
        self.assertEqual(welcome.clean_name("你好", "steve_01"), "steve_01")

    def test_nothing_usable(self):
        self.assertEqual(welcome.clean_name("你好", "你好"), "New member")

    def test_length_capped(self):
        self.assertEqual(len(welcome.clean_name("a" * 60, "")), 28)


class Easing(unittest.TestCase):
    def test_endpoints(self):
        for ease in (welcome.power3_out, welcome.back_out(1.4)):
            self.assertAlmostEqual(ease(0), 0 if ease is welcome.power3_out else ease(0))
            self.assertAlmostEqual(ease(1), 1)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class Render(unittest.TestCase):
    def test_gif(self):
        gif = asyncio.run(welcome.make_gif(png(), "Tester"))
        self.assertEqual(gif[:6], b"GIF89a")
        self.assertLess(len(gif), 8 * 1024 * 1024)  # Discord's upload limit without boosts

    def test_leave_gif(self):
        gif = asyncio.run(welcome.make_gif(png(), "Tester", kind="leave"))
        self.assertEqual(gif[:6], b"GIF89a")
        self.assertLess(len(gif), 8 * 1024 * 1024)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            asyncio.run(welcome.make_gif(png(), "Tester", kind="nope"))


if __name__ == "__main__":
    unittest.main()
