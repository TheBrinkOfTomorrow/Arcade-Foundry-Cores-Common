"""The game manifest: games.toml, one per core repo and one in the app.

A core repo's games.toml lists every ROM set its core builds, with the facts
about the game (title, year, board, controls, core version). The app's
games.toml lists the sets the app shows, in its order, with the words its
library and GAMES.md use. Each repo's scripts/gen_games.py reads them with
this module and writes the files that used to be edited by hand for every new
game; `gen_games.py --check` (run by CI) fails when one of them is out of date.

Generated text goes either into a file of its own or between two marker
lines in a hand-written file:

    // GAMES:BEGIN catalog (scripts/gen_games.py writes this part)
    ...
    // GAMES:END catalog

Needs Python 3.11 or later (tomllib).
"""

import argparse
import difflib
import os
import re
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python before 3.11
    sys.exit("gen_games.py needs Python 3.11 or later (tomllib)")

# The boards: id -> (the app's platform, the board's name). The platform is
# the library's filter (Platform in Catalog.swift); the name is what GAMES.md
# and the game's detail line call the board.
BOARDS = {
    "hangon": ("classic2d", "Hang-On board"),
    "outrun": ("classic2d", "Out Run board"),
    "xboard": ("classic2d", "X Board"),
    "system32": ("classic2d", "System 32"),
    "yboard": ("classic2d", "Y Board"),
    "model1": ("model1", "Model 1"),
    "model2": ("model2", "Model 2"),
    "model2a": ("model2", "Model 2A"),
    "model2b": ("model2", "Model 2B"),
    "model2c": ("model2", "Model 2C"),
    "model3": ("model3", "Model 3"),
    "lindbergh": ("lindbergh", "Lindbergh"),
    "chihiro": ("chihiro", "Chihiro"),
    "alls": ("alls", "ALLS"),
}

# How the game is played, which picks the controller layout and the controls
# list on the game's page.
CONTROLS = {
    "drive": "a wheel and pedals (or handlebars, or a stick and buttons that drive)",
    "flight": "a flight stick and throttle",
    "twin": "two sticks, each with a shot and a dash (Virtual-On)",
    "gun": "a light gun with off-screen reload",
    "turret": "a gun turret that draws its own sight (no crosshair)",
    "analog-gun": "Model 3's light guns: no off-screen sense, the second trigger reloads",
    "fighting": "a joystick and buttons (the fighting games, and the others played the same way)",
    "fishing": "a fishing rod and reel (Sega Bass Fishing, Get Bass)",
}

# A core repo's games.toml: [[game]] tables with these keys.
CORE_KEYS = {
    "set": "MAME's set name (required)",
    "title": "the game's title, the same for all its versions (required)",
    "year": "the year this version came out (required)",
    "board": "one of BOARDS (required)",
    "version": "which version: revision, region, cabinet (\"Revision A\", \"Export, Twin\")",
    "core_version": "the core's version x.y.z (afcore_api.core_version)",
    "controls": "one of CONTROLS (default drive)",
    "stick_y": "the left stick's forward and back is a second analog control",
    "gun_button": "the gun has a button besides the trigger",
    "players": "how many players can play on one machine (a second controller is player 2)",
    "link": "the core has link play",
    "devices": "other sets it needs beside it (MAME device sets)",
    "other_names": "other file names that may hold this set (MAME's parent, an old dump name)",
    "renamed_from": "the app's earlier names for the set (saves and settings move once)",
    "rom_notes": "what the ROM set is (GAMES.md's ROM notes, before the controls)",
    "build": "a table of whatever this core's build needs (program paths, defines)",
}

# The app's games.toml: [[game]] tables in the library's order.
APP_KEYS = {
    "set": "a set from one of the cores' games.toml (required)",
    "hint": "how to play it, shown after the detail on Apple devices",
    "linux_hint": "the same for the Linux app (gamepad words); default hint",
    "hidden": "left out of this release (the core stays)",
    "doc_title": "the Game column in GAMES.md, when the title alone does not tell versions apart",
    "doc_note": "said only in GAMES.md, after the hint (too long for the library's detail line)",
    "doc_status": "GAMES.md's column for a platform, when it is not the usual: {ios = \"Not in this release\"}",
}


class ManifestError(Exception):
    pass


def _load(path):
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ManifestError(f"{path}: {e}") from None
    games = data.get("game", [])
    if not isinstance(games, list):
        raise ManifestError(f"{path}: [[game]] tables expected")
    return games


def load_core(path):
    """A core repo's games.toml, checked: a list of dicts with every key of
    CORE_KEYS present (defaults filled in), in the file's order."""
    games, seen = [], set()
    for g in _load(path):
        where = f"{path}: {g.get('set', '?')}"
        for k in g:
            if k not in CORE_KEYS:
                raise ManifestError(f"{where}: unknown key {k!r} (known: {', '.join(CORE_KEYS)})")
        for k in ("set", "title", "year", "board"):
            if k not in g:
                raise ManifestError(f"{where}: {k} is required")
        if g["set"] in seen:
            raise ManifestError(f"{where}: listed twice")
        seen.add(g["set"])
        if g["board"] not in BOARDS:
            raise ManifestError(f"{where}: board {g['board']!r} is not one of {', '.join(BOARDS)}")
        if not isinstance(g["year"], int):
            raise ManifestError(f"{where}: year must be a number")
        g = {"version": "", "core_version": "1.0.0", "controls": "drive", "stick_y": False, "gun_button": False, "players": 1,
             "link": False, "devices": [], "other_names": [], "renamed_from": [], "rom_notes": "", "build": {}, **g}
        if g["controls"] not in CONTROLS:
            raise ManifestError(f"{where}: controls {g['controls']!r} is not one of {', '.join(CONTROLS)}")
        v = g["core_version"].split(".")
        if len(v) != 3 or not all(p.isdigit() for p in v):
            raise ManifestError(f"{where}: core_version must be x.y.z")
        g["platform"], g["board_name"] = BOARDS[g["board"]]
        games.append(g)
    return games


def load_app(path, cores):
    """The app's games.toml joined with the cores' games: a list of dicts,
    the core's keys plus APP_KEYS, in the app's order. `cores` maps a core's
    name to its loaded games."""
    by_set = {}
    for core, games in cores.items():
        for g in games:
            if g["set"] in by_set:
                raise ManifestError(f"{g['set']} is in two cores' games.toml")
            by_set[g["set"]] = {**g, "core": core}
    out, seen = [], set()
    for a in _load(path):
        where = f"{path}: {a.get('set', '?')}"
        for k in a:
            if k not in APP_KEYS:
                raise ManifestError(f"{where}: unknown key {k!r} (known: {', '.join(APP_KEYS)})")
        if "set" not in a:
            raise ManifestError(f"{where}: set is required")
        if a["set"] in seen:
            raise ManifestError(f"{where}: listed twice")
        seen.add(a["set"])
        if a["set"] not in by_set:
            raise ManifestError(f"{where}: no core's games.toml has it")
        a = {"hint": "", "doc_note": "", "hidden": False, "doc_status": {}, **a}
        a.setdefault("linux_hint", a["hint"])
        a.setdefault("doc_title", by_set[a["set"]]["title"])
        out.append({**by_set[a["set"]], **a})
    return out


def version_tuple(g):
    return tuple(int(p) for p in g["core_version"].split("."))


# Generated text.

def region(text, name, body, path, comment="//"):
    """`text` with the lines between its GAMES:BEGIN name and GAMES:END name
    markers replaced by `body` (lines, or a string ending in a newline)."""
    if not isinstance(body, str):
        body = "".join(line + "\n" for line in body)
    begin = f"GAMES:BEGIN {name}"
    end = f"GAMES:END {name}"
    lines = text.splitlines(keepends=True)
    b = [i for i, l in enumerate(lines) if re.search(rf"{begin}(\s|$)", l)]
    e = [i for i, l in enumerate(lines) if re.search(rf"{end}(\s|$)", l)]
    if len(b) != 1 or len(e) != 1 or e[0] < b[0]:
        raise ManifestError(f"{path}: needs one '{comment} {begin}' line and one '{comment} {end}' line after it")
    return "".join(lines[: b[0] + 1]) + body + "".join(lines[e[0]:])


class Output:
    """The files a generator writes: collected, then written (or, with
    --check, compared)."""

    def __init__(self, root):
        self.root = root
        self.files = {}

    def path(self, rel):
        return os.path.join(self.root, rel)

    def read(self, rel):
        """The file as it will be written: what this run put there, else
        what is on disk."""
        if rel in self.files:
            return self.files[rel]
        with open(self.path(rel), encoding="utf-8") as f:
            return f.read()

    def write(self, rel, text):
        self.files[rel] = text

    def region(self, rel, name, body, comment="//"):
        self.files[rel] = region(self.read(rel), name, body, rel, comment)

    def finish(self, check):
        stale = []
        for rel, text in self.files.items():
            try:
                with open(self.path(rel), encoding="utf-8") as f:
                    old = f.read()
            except FileNotFoundError:
                old = None
            if old == text:
                continue
            stale.append(rel)
            if check:
                sys.stdout.writelines(difflib.unified_diff((old or "").splitlines(keepends=True), text.splitlines(keepends=True),
                                                           f"a/{rel}", f"b/{rel}", n=1))
            else:
                os.makedirs(os.path.dirname(self.path(rel)) or ".", exist_ok=True)
                with open(self.path(rel), "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"wrote {rel}")
        if check and stale:
            print(f"\n{len(stale)} generated file(s) out of date with games.toml: run scripts/gen_games.py and commit them",
                  file=sys.stderr)
            return 1
        if not stale:
            print("generated files are up to date")
        return 0


def main(generate, root, doc=None):
    """A generator's command line: generate(out) fills an Output for `root`;
    --check compares instead of writing."""
    ap = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only compare: exit 1 when a generated file is out of date")
    args = ap.parse_args()
    out = Output(root)
    try:
        generate(out)
    except ManifestError as e:
        sys.exit(f"games.toml: {e}")
    sys.exit(out.finish(args.check))


# Formatting helpers.

def cmake_list(items):
    return " ".join(items)


def py_list(prefix, items, width=110, brackets="[]"):
    """Lines of `prefix` and a Python list (or, brackets "{}", set) literal
    of strings, wrapped under its first item."""
    out, line = [], prefix + brackets[0]
    pad = " " * len(line)
    for i, s in enumerate(items):
        piece = repr(s) + ("," if i + 1 < len(items) else brackets[1])
        if len(line) + len(piece) + 1 > width and not line.endswith(brackets[0]):
            out.append(line.rstrip())
            line = pad + piece
        else:
            line += ("" if line.endswith(brackets[0]) else " ") + piece
    if not items:
        line += brackets[1]
    out.append(line)
    return out


def c_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def swift_dict(pairs):
    """A Swift dictionary literal from (key literal, value literal) pairs."""
    return "[" + ", ".join(f"{k}: {v}" for k, v in pairs) + "]" if pairs else "[:]"


def swift_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
