#!/usr/bin/env python3
"""Smoke test for the 64-bit front end (tools/x86recomp/recomp64.py) without a
game: compiles smoke.c as a Win64 program with MinGW, translates it, builds the
C against the test runtime here (rt64.c, win64_min.c) and runs it. The output
is checked against values computed independently. Needs
x86_64-w64-mingw32-gcc, a host C compiler and capstone.
Usage: smoke64.py WORKDIR"""
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RECOMP = os.path.join(HERE, "..", "..", "tools", "x86recomp", "recomp64.py")

EXPECTED = """hash 4ec94ce7684ab996 three 38 8.250
tls 8 argc 1 ticks 1
big 0059a42e8a227727057edb237192610a
sse42 1738d4804f52f6ad
"""


def main():
    work = sys.argv[1]
    os.makedirs(work, exist_ok=True)
    mingw = shutil.which("x86_64-w64-mingw32-gcc")
    if not mingw:
        print("x86recomp64 smoke: skipped (no x86_64-w64-mingw32-gcc)")
        return
    cc = os.environ.get("CC", "cc")
    # cmpxchg16b in the translated code: an x86-64 host needs the flag, arm64 has it
    cx16 = ["-mcx16"] if platform.machine() in ("x86_64", "AMD64") else []
    # the same program twice: relocatable, and without a relocation directory
    # (the loader then maps it at its preferred base). Without fixups the code
    # pointers in data are found by scanning, so both must find the same functions.
    counts = {}
    for name, ldflags in (("smoke", []), ("smoke_fixed", ["-Wl,--disable-dynamicbase,--disable-reloc-section"])):
        exe = os.path.join(work, name + ".exe")
        subprocess.run([mingw, "-O2", "-I" + HERE, "-o", exe, os.path.join(HERE, "smoke.c")] + ldflags, check=True)
        out = os.path.join(work, "gen-" + name)
        r = subprocess.run([sys.executable, RECOMP, exe, out, "--name", name], capture_output=True, text=True)
        sys.stdout.write(r.stdout)
        if r.returncode:
            sys.stderr.write(r.stderr)
            sys.exit("recomp64.py failed")
        counts[name] = next((l.split()[0] for l in r.stdout.splitlines() if l.endswith("files")), None)
        bad = [l for l in r.stdout.splitlines() if l.startswith("      ")]
        if bad:
            sys.exit("untranslated instructions:\n" + "\n".join(bad))
        gens = sorted(os.path.join(out, f) for f in os.listdir(out) if f.endswith(".c"))
        binary = os.path.join(work, name + "64")
        subprocess.run([cc, "-O1", "-w", "-I" + HERE, "-DPROG=gen_program_" + name] + cx16 + gens +
                       [os.path.join(HERE, f) for f in ("rt64.c", "win64_min.c", "main64.c")] +
                       ["-lm", "-lpthread", "-o", binary], check=True)
        r = subprocess.run([binary, exe], capture_output=True, text=True)
        if r.returncode or r.stdout != EXPECTED:
            sys.stderr.write(r.stderr)
            sys.exit("%s: wrong output (exit %d):\n%s\nexpected:\n%s" % (name, r.returncode, r.stdout, EXPECTED))
    if counts["smoke"] != counts["smoke_fixed"]:
        sys.exit("function count differs without relocations: %s" % counts)
    print("x86recomp64 smoke: ok")


if __name__ == "__main__":
    main()
