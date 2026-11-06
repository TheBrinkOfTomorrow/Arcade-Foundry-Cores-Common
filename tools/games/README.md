# Game manifest

`afgames.py` reads `games.toml`, the list of games kept once per repo:

- Each core repo's `games.toml`: every ROM set the core builds, with its title,
  year, board, version, core version, controls and link play.
- The app's `games.toml`: the sets the app lists, in the library's order,
  with the words it shows (hints, GAMES.md's notes).

Each repo's `scripts/gen_games.py` writes the files that come from it (CMake
set lists, core version headers, the app's catalog, the Linux game table,
`build_cores.py`'s lists, `GAMES.md`'s tables). Adding a game is a line in
the core's `games.toml` (and the app's), then:

    python3 scripts/gen_games.py          # rewrite the generated files
    python3 scripts/gen_games.py --check  # what CI runs: fail if out of date

The keys are documented in `afgames.py` (`CORE_KEYS`, `APP_KEYS`, `BOARDS`,
`CONTROLS`). Python 3.11 or later.
