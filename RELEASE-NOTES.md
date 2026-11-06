# Release notes

Newest first. Each release is also on the repository's Releases page.

## 1.2.0 (2026-11-06)

**Arcade Foundry Cores: Common 1.2.0**

The first release of Arcade-Foundry-Cores-Common: the code the core repos and the
app share, kept once here. It ships with Insert Coin 1.2.0, which carries the
1.1.0 and 1.2.0 work together (there was no separate 1.1.0 release). Every core
repo and the app have it as a submodule (`common/` in the cores,
`cores/arcade-foundry-cores-common` in the app).

- **mtlgles:** OpenGL ES on Metal (`lib/mtlgles`), the renderer the Lindbergh
  games use on Mac and iPad from this release. gl4es turns the games' desktop
  OpenGL into OpenGL ES, and mtlgles draws that with Metal. The design is
  docs/design/MTLGLES.md.

- **Core interface:** afcore 5.3. 5.2 added `check_roms`, which checks a ROM
  set without starting the game (the app's Downloads shows the result); 5.3
  added `options` and `set_option`, a game's own settings that its menus don't
  have (the Lindbergh games' region).

- **Archives:** caps on what a zip, 7z or tar header can make the reader
  allocate (a zip's sizes, a tar's pax and long-name records).

- **Tools:** `tools/filelist/make_filelist.py` makes the per-game file lists the
  Lindbergh download check uses; `tools/games/afgames.py` reads every repo's
  `games.toml` (one list of games per repo, from which the game lists are
  generated); `tools/afic-venv` makes one shared Python environment per machine
  (`~/venvs/afic`) for every core's build tools, so nothing is installed into
  the system Python.

- **mtlgles:** Sega Race TV's cars on Metal are no longer invisible or
  painted with noise. mtlgles can now read a compressed (DXT) texture back,
  which the game does at boot to recolour its cars.

- **mtlgles:** Let's Go Jungle's 3D on Metal no longer comes out blocky
  and zoomed in. mtlgles now says it can draw into half-float textures,
  which stops gl4es resizing the game's scene texture to 84x64.

- **Debug switches:** `include/af_debug.h` reads the cores' developer
  switches from the environment under one prefix, `AF_DEBUG_` followed by the
  old name (`AF_DEBUG_LB_PERF`, `AF_DEBUG_CH_VERBOSE`). The old names still
  work in this release and go in the next. The full list is
  docs/DEBUG-SWITCHES.md; mtlgles reads its switches this way too.

- **New repository:** the code several core repos shared, kept once here.
  - `include/afcore.h`, the core interface, now lives here only.
  - ROM set archives: the zip reader and the 7z reader that Model 1/2 and
    Classic 2D each carried a copy of (`afc::open_archive`, `afc::Zip`).
  - Tar unpacking for games kept as one file (`afc::TarUnpacker`, and
    `afcommon/tar_c.h` for C and Swift), from the Linux and Apple front ends'
    two separate versions.
  - `tools/x86recomp`, the x86 static recompiler, one copy for Lindbergh and
    Chihiro (it had been copied into both; Chihiro's had grown XBE support,
    more instructions and SSE1 conversions). Lindbergh games translate as before
    apart from instructions that were untranslated stubs and now translate.
  - Tests that run without ROMs: zip (stored, fixed and dynamic deflate,
    a bad CRC), 7z (LZMA, LZMA2, solid) and tar (GNU long names, pax,
    links, files outside the game folder, a cut-off download), and an x86recomp
    smoke test on a small 32-bit program.
- x86recomp: `bswap` now translates, and a library in a subfolder keeps its
  path (both from the Initial D 4 work in the Lindbergh repo).
