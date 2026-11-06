#!/bin/sh
# Builds msvc.c the MSVC way without Visual Studio: clang for the
# x86_64-pc-windows-msvc target, linked by lld-link with no C runtime
# (kernel32 import library from kernel32.def). Then: run64.sh msvc.exe msvc
set -e
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-$here/build/msvc-src}
mkdir -p "$out"
llvm-dlltool -m i386:x86-64 -d "$here/kernel32.def" -l "$out/kernel32.lib"
clang --target=x86_64-pc-windows-msvc -O2 -fms-extensions -ffreestanding -fno-builtin -c "$here/msvc.c" -o "$out/msvc.obj"
lld-link /nologo /subsystem:console /entry:mainCRTStartup /nodefaultlib /out:"$out/msvc.exe" "$out/msvc.obj" "$out/kernel32.lib"
echo "$out/msvc.exe"
