#!/usr/bin/env python3
"""Smoke test for tools/x86recomp without a game: compiles
tests/x86recomp_smoke.c as a 32-bit x86 ELF (gcc -m32), recompiles it and
checks every instruction was translated. (The C needs a core's runtime to
compile, so that is each core's build.) Needs gcc-multilib, capstone and
pyelftools.
Usage: x86recomp_smoke.py WORKDIR"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RECOMP = os.path.join(HERE, "..", "tools", "x86recomp", "recomp.py")


def main():
    work = sys.argv[1]
    os.makedirs(work, exist_ok=True)
    elf = os.path.join(work, "smoke.elf")
    subprocess.run(["gcc", "-m32", "-O2", "-fno-pie", "-no-pie", "-o", elf, os.path.join(HERE, "x86recomp_smoke.c")],
                   check=True)
    out = os.path.join(work, "gen")
    r = subprocess.run([sys.executable, RECOMP, elf, out], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode:
        sys.stderr.write(r.stderr)
        sys.exit("recomp.py failed")
    # Only the CET marker may be left untranslated (a no-op on the guest).
    bad = [l for l in r.stdout.splitlines() if l.startswith("      ") and "endbr32" not in l]
    if bad:
        sys.exit("untranslated instructions:\n" + "\n".join(bad))
    if not os.path.exists(os.path.join(out, "gen_000.c")):
        sys.exit("no gen_000.c written")
    print("x86recomp smoke: ok")


if __name__ == "__main__":
    main()
