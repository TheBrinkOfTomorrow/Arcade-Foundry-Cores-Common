#!/usr/bin/env python3
"""Smoke test for the PE32 front end of tools/x86recomp/recomp.py (32-bit
Windows programs) without a game: builds smoke.c with MinGW for i686,
translates it, compiles the C against the test runtime here (rt32.c) and runs
it. The output is checked against values computed independently below.
Then does the same with simd.c, checked against the same source run natively.
Needs i686-w64-mingw32-gcc, a host C compiler, capstone and pyelftools.
Usage: smoke_pe32.py WORKDIR"""
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RECOMP = os.path.join(HERE, "..", "..", "tools", "x86recomp", "recomp.py")


def expected():
    h = 0xcbf29ce484222325
    for b in b"the quick brown fox jumps over the lazy dog":
        h = ((h ^ b) * 0x100000001b3) & (2 ** 64 - 1)
    picks = {0: 10, 1: 22, 2: 31, 3: 47, 4: 5, 5: 61, 6: 7}
    s = sum(picks.get(k, -1) for k in range(-1, 8))
    big = 9876543210123
    x = 2.5
    return ("hash %016x sum %d calls %d %d %d %d %d\ndiv %d %d x87 %d sse %d ss 2b\n"
            % (h, s, 9 - 4, 5 + 6 * 2, 7 * 6, 12 * 12, 2 * 3 + 3, big // 77, big % 77,
               int(((x * 0.5 + 1.25) * x - 3.0) * x * 1000), int((3.0 * 0.5 + 3.0 / 0.5) * 1000)))


def translate_and_run(work, mingw, cc, name, cflags=()):
    """Build NAME.c for i686 Windows without a C runtime, translate it, compile
    the C against rt32.c and run it. Returns its stdout."""
    exe = os.path.join(work, name + ".exe")
    # no C runtime: the only imports are GetStdHandle, WriteFile and ExitProcess
    subprocess.run([mingw, "-O2", "-nostdlib", "-Wl,-e,_start@0", *cflags, "-o", exe, os.path.join(HERE, name + ".c"),
                    "-lkernel32", "-lgcc"], check=True)
    out = os.path.join(work, "gen_" + name)
    r = subprocess.run([sys.executable, RECOMP, exe, out, "--name", name], capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode:
        sys.stderr.write(r.stderr)
        sys.exit("recomp.py failed on " + name)
    if "unsupported" in r.stdout:
        sys.exit("untranslated instructions in " + name)
    run = os.path.join(work, name + "_run")
    srcs = [os.path.join(out, f) for f in sorted(os.listdir(out)) if f.endswith(".c")]
    subprocess.run([cc, "-O1", "-w", "-I" + HERE, "-I" + out, "-DPROGRAM=gen_program_" + name, "-o", run,
                    os.path.join(HERE, "rt32.c")] + srcs + ["-lm"], check=True)
    got = subprocess.run([run, exe], capture_output=True, text=True)
    if got.returncode != 0:
        sys.stderr.write(got.stderr)
        sys.exit("%s exited with %d" % (name, got.returncode))
    return got.stdout


def main():
    work = sys.argv[1]
    os.makedirs(work, exist_ok=True)
    mingw = shutil.which("i686-w64-mingw32-gcc")
    if not mingw:
        print("x86recomp PE32 smoke: skipped (no i686-w64-mingw32-gcc)")
        return
    cc = os.environ.get("CC", "cc")
    got = translate_and_run(work, mingw, cc, "smoke")
    if got != expected():
        sys.exit("wrong output:\n%s\nexpected:\n%s" % (got, expected()))
    sys.stdout.write(got)
    # SIMD: the reference is the same source run natively, so an x86 host only
    if platform.machine().lower() not in ("x86_64", "amd64", "i386", "i686"):
        print("simd: skipped (needs an x86 host for the native reference)")
        print("x86recomp PE32 smoke: ok")
        return
    native = os.path.join(work, "simd_native")
    subprocess.run([cc, "-O2", "-DNATIVE", "-msse4.1", "-mmmx", "-o", native, os.path.join(HERE, "simd.c")], check=True)
    want = subprocess.run([native], capture_output=True, text=True, check=True).stdout
    got = translate_and_run(work, mingw, cc, "simd", ("-msse4.1",))
    if got != want:
        diff = [g + "  (native " + w.split()[1] + ")" for g, w in zip(got.splitlines(), want.splitlines()) if g != w]
        sys.exit("SIMD results differ from native:\n" + "\n".join(diff or [got]))
    print("simd: %d results match native" % len(want.splitlines()))
    print("x86recomp PE32 smoke: ok")


if __name__ == "__main__":
    main()
