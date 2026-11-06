# Third-party components

This project's own code is under the BSD-3-Clause licence (`LICENSE`). The
code below keeps its own licence and notices. Vendored checkouts live in
git-ignored `extern/` and are never committed.

| Component | Upstream | Commit | Licence | Use | Changes |
| --- | --- | --- | --- | --- | --- |
| 7-Zip LZMA SDK 26.03 (`C/`: `7zArcIn.c`, `7zDec.c`, `7zBuf.c`, `7zCrc.c`, `7zCrcOpt.c`, `7zStream.c`, `7zAlloc.c`, `Alloc.c`, `LzmaDec.c`, `Lzma2Dec.c`, `Bra.c`, `Bra86.c`, `BraIA64.c`, `Bcj2.c`, `Delta.c`, `CpuArch.c`, `Ppmd7.c`, `Ppmd7Dec.c`) | https://github.com/ip7z/7zip | `0766b733fe3e06dd2a7f9a3cfbf2108ac73abd17` | Public domain (the LZMA SDK files listed; none of 7-Zip's LGPL or unRAR code) | Compiled into `afc_lzma7z` when `AFC_LZMA_DIR` points at the SDK's `C/` folder: the 7z reader in `src/archive.cpp`. The core repos fetch it (Model 1/2: `scripts/setup.py` into `extern/lzma`). | None. Built with `Z7_PPMD_SUPPORT`. |
| Capstone (Python) | https://www.capstone-engine.org | pip package | BSD-3-Clause | x86 disassembly in `tools/x86recomp`. Not shipped. | — |
| pyelftools | https://github.com/eliben/pyelftools | pip package | Unlicense (public domain) | ELF parsing in `tools/x86recomp`. Not shipped. | — |
| py7zr (tests only) | https://pypi.org/project/py7zr/ | release from PyPI | LGPL-2.1-or-later | Run by `tests/make_fixtures.py` to write the 7z test archives, when installed. Not linked, not shipped. | — |

`src/zip.cpp` (zip reader, inflate per RFC 1951, CRC-32) and
`src/archive.cpp` came from Arcade-Foundry-Cores-Model12
(`src/runtime/zip.cpp`, `src/runtime/archive.cpp`), where Arcade-Foundry-
Cores-Classic2D also had a byte-identical copy. `src/tar.cpp` merges the
two tar unpackers of Arcade Foundry: Insert Coin (the Linux front end's
`unpack_tar` and the Apple front end's `GamePackage.Unpacker`). All of it is
this project's own code.

`lib/mtlgles/` came from Arcade-Foundry-Cores-Lindbergh (branch
`apple-renderer-spikes`, ba1df3a), where it was written. This project's own
code; it uses no third-party code (the OpenGL ES enum values in
`mtlgles_enums.h` are the Khronos registry's numbers).

`tools/x86recomp/` (recomp.py, sse.py, xbe.py) came from
Arcade-Foundry-Cores-Chihiro (af-next 50cff1b; xbe.py as of 58b6cc7), whose copy had grown from
Arcade-Foundry-Cores-Lindbergh's (af-next, last changed 72b3557). This
project's own code.
