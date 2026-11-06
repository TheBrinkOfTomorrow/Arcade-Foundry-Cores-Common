# Arcade Foundry Cores: Common

Code that several Arcade Foundry core repos share, kept here once instead of
copied into each. Each core repo uses this repo as a git submodule at
`common/` and builds it with `add_subdirectory(common)`.

| Target | What | Used by |
| --- | --- | --- |
| `afc::afcore` | `include/afcore.h`, the game core interface every core exports and every front end loads | every core, the front ends |
| `afc::afcommon` | ROM set and game archives: zip (own inflate, no zlib), 7z (the LZMA SDK) and tar (`include/afcommon/`) | Model 1/2, Classic 2D; the Linux and Apple front ends (tar) |
| `afc::mtlgles` | (opt-in, `AFC_MTLGLES`, Apple only) an OpenGL ES 3.0 subset on Metal, the one gl4es needs: `lib/mtlgles/` | Lindbergh with `LB_MTLGLES` (macOS, iOS) |
| `tools/x86recomp/` | The static recompiler for 32-bit x86 programs: Linux ELF (Lindbergh) and Xbox XBE (Chihiro) to portable C. Python, run by the core's build | Lindbergh, Chihiro |
| `tools/afic-venv/` | The one shared Python venv per machine (`~/venvs/afic`): `requirements.txt` lists every module the repos use, `setup.sh` makes it | every repo |

## Archives

- `afc::open_archive(path)` opens a ROM set as zip or 7z, chosen by the
  file's signature, and reads members by name with their CRC checked.
- `afc::Zip` is the zip reader on its own (stored and deflated members).
- `afc::TarUnpacker` unpacks a game kept as one tar (a Lindbergh game as
  `<set>.tar`) as the bytes arrive, into `<dst>.unpacking`, and puts it in
  place on `finish()`. It refuses anything that would land outside the
  destination and leaves out macOS's `._*`, `__MACOSX` and `.DS_Store`.
  `afcommon/tar_c.h` is the same for C and Swift, and `afcommon/zip_c.h` reads a zip member from C.

7z needs the 7-Zip LZMA SDK's `C/` folder. A core that already fetches it
points `AFC_LZMA_DIR` at it (or defines a `lzma7z` target first, as Model
1/2 does); without it, a 7z set gets a clear error and zip still works.

## x86recomp

`python3 common/tools/x86recomp/recomp.py GAME.elf OUTDIR --name NAME` (see
its `--help`). It writes C that calls the core's own runtime (`guest_*`,
`hle_*`, `ovr_*`, the x87 and SSE helpers), so each core keeps its runtime
and calls the one recompiler. Needs `capstone` and `pyelftools`, from the
shared venv ([tools/afic-venv](tools/afic-venv/README.md)). XBE-only
behaviour (discovery, MSVC setjmp, the kernel's `fs`, a memory barrier on
each loop's back edge) is switched on by the image being an XBE, so a
Lindbergh ELF translates as it did in the Lindbergh repo.

## mtlgles

`lib/mtlgles/` takes the OpenGL ES 3.0 calls gl4es makes and draws them with
Metal; GLSL ES becomes Metal Shading Language by rewriting (`glsl_msl.c`).
It exports no GL symbols: the caller looks the entry points up with
`mtlgles_proc("glDrawElements")` and hands them to gl4es. Built only when a
core sets `AFC_MTLGLES` before `add_subdirectory(common)`; nothing builds it
by default. Developer notes: `docs/MTLGLES-DEVELOPER.md` (left out of the
public repo).

## Debug switches

`include/af_debug.h`: `af_debug_env("LB_PERF")` reads a core's debug switch
from the environment as `AF_DEBUG_LB_PERF`, or by its old name until 1.4.0.
Every core's switches are listed in [docs/DEBUG-SWITCHES.md](docs/DEBUG-SWITCHES.md).

## Building on its own

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
```

The tests make their own zip, tar and 7z files with Python's `zipfile`,
`tarfile` and (when installed) `py7zr`, so no ROM is needed. For the 7z
tests, fetch the SDK into `extern/lzma` (see `THIRD_PARTY.md`) and make the
shared venv, which has `py7zr` (`tools/afic-venv/setup.sh`, see
[its README](tools/afic-venv/README.md)).

## Using it from a core repo

```sh
git submodule add https://github.com/TheBrinkOfTomorrow/Arcade-Foundry-Cores-Common common
```

```cmake
set(AFC_LZMA_DIR ${CMAKE_SOURCE_DIR}/extern/lzma/C)  # optional: 7z sets
add_subdirectory(common)
target_link_libraries(runtime PUBLIC afc::afcommon afc::afcore)
```

## Licence

BSD-3-Clause (`LICENSE`). Third-party code is listed in `THIRD_PARTY.md`.
No ROM data is in this repository.
