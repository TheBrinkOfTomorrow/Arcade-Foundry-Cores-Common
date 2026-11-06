#!/bin/sh
# Translate a Win64 PE with recomp64 and build it against the test runtime.
#   tests/x86recomp64/run64.sh prog.exe [name] [args...]
# Leaves build/<name>/ with the generated C and the binary, then runs it.
set -e
here=$(cd "$(dirname "$0")" && pwd)
exe=$1; shift
name=${1:-$(basename "$exe" .exe)}; [ $# -gt 0 ] && shift
out=$here/build/$name
mkdir -p "$out"
[ -n "$REUSE_GEN" ] && [ -d "$out/gen" ] || python3 "$here/../../tools/x86recomp/recomp64.py" "$exe" "$out/gen" --name "$name"
: "${CC:=cc}"
case $(uname -m) in x86_64|amd64) CX16=-mcx16 ;; *) CX16= ;; esac
"$CC" -O1 -w $CX16 -I"$here" -DPROG=gen_program_$name "$out"/gen/*.c "$here/rt64.c" "$here/win64_min.c" "$here/main64.c" -lm -lpthread -o "$out/$name"
exec "$out/$name" "$exe" "$@"
