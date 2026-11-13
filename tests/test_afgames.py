"""tools/games/afgames.py: loading, checking and regions."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "games"))
import afgames  # noqa: E402

CORE = """
[[game]]
set = "vr"
title = "Virtua Racing"
year = 1992
board = "model1"
link = true

[[game]]
set = "swa"
title = "Star Wars Arcade"
year = 1993
board = "model1"
controls = "flight"
core_version = "1.1.0"
"""


class Test(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_core(self):
        games = afgames.load_core(self.write(CORE))
        self.assertEqual([g["set"] for g in games], ["vr", "swa"])
        self.assertEqual(games[0]["platform"], "model1")
        self.assertEqual(games[0]["controls"], "drive")
        self.assertEqual(afgames.version_tuple(games[1]), (1, 1, 0))

    def test_bad(self):
        for bad in ('board = "model9"', 'colour = "red"', 'controls = "wheel"', 'core_version = "1.0"'):
            with self.subTest(bad=bad), self.assertRaises(afgames.ManifestError):
                afgames.load_core(self.write(f'[[game]]\nset = "x"\ntitle = "X"\nyear = 1990\nboard = "model1"\n{bad}\n'))
        with self.assertRaises(afgames.ManifestError):
            afgames.load_core(self.write(CORE + CORE))  # listed twice

    def test_pc(self):
        pc = '[[game]]\nset = "dcusans"\ntitle = "Daytona Championship USA"\nyear = 2019\nboard = "segapc"\n'
        games = afgames.load_core(self.write(pc + 'program = "Daytona.exe"\n'))
        self.assertEqual(games[0]["platform"], "pc")
        self.assertEqual(games[0]["program"], "Daytona.exe")
        self.assertEqual(afgames.load_core(self.write(CORE))[0]["program"], "")
        self.assertEqual(afgames.load_core(self.write(pc))[0]["program"], "")
        with self.assertRaises(afgames.ManifestError):  # only they have one
            afgames.load_core(self.write(CORE + 'program = "x.exe"\n'))

    def test_app(self):
        cores = {"model12": afgames.load_core(self.write(CORE))}
        app = afgames.load_app(self.write('[[game]]\nset = "swa"\nhint = "the left stick flies"\n'), cores)
        self.assertEqual(app[0]["core"], "model12")
        self.assertEqual(app[0]["linux_hint"], "the left stick flies")
        self.assertEqual(app[0]["doc_title"], "Star Wars Arcade")
        with self.assertRaises(afgames.ManifestError):
            afgames.load_app(self.write('[[game]]\nset = "nope"\n'), cores)

    def test_region(self):
        text = "a\n// GAMES:BEGIN x\nold\n// GAMES:END x\nb\n"
        self.assertEqual(afgames.region(text, "x", ["new", "lines"], "f"),
                         "a\n// GAMES:BEGIN x\nnew\nlines\n// GAMES:END x\nb\n")
        with self.assertRaises(afgames.ManifestError):
            afgames.region("no markers\n", "x", [], "f")
        # a name that starts another's (model2, model2a) is its own region
        text = "<!-- GAMES:BEGIN m2 -->\n<!-- GAMES:END m2 -->\n<!-- GAMES:BEGIN m2a -->\nx\n<!-- GAMES:END m2a -->\n"
        self.assertEqual(afgames.region(text, "m2", ["y"], "f"),
                         "<!-- GAMES:BEGIN m2 -->\ny\n<!-- GAMES:END m2 -->\n<!-- GAMES:BEGIN m2a -->\nx\n<!-- GAMES:END m2a -->\n")


if __name__ == "__main__":
    unittest.main()
