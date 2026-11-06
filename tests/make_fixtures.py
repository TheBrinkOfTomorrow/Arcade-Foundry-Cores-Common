#!/usr/bin/env python3
"""Writes the archives tests/afcommon_test.cpp reads, made by Python's own
zipfile and tarfile so the readers are checked against another
implementation. Usage: make_fixtures.py OUT_DIR"""
import io
import os
import random
import sys
import struct
import tarfile
import zipfile
import zlib


def members():
    rnd = random.Random(1)
    text = b"".join(b"line %d of a ROM listing\n" % i for i in range(5000))
    return {
        "empty.bin": b"",
        "one.bin": b"\x42",
        "text.bin": text,  # compresses well: dynamic Huffman blocks, long matches
        "noise.bin": bytes(rnd.getrandbits(8) for _ in range(70000)),  # stored blocks inside deflate
        "mixed.bin": text[:3000] + bytes(rnd.getrandbits(8) for _ in range(3000)) + text[:3000],
    }


def write_zips(out):
    m = members()
    with open(os.path.join(out, "manifest.txt"), "w") as f:
        for name, data in m.items():
            f.write("%s %d %08x\n" % (name, len(data), zlib.crc32(data)))
    for kind, method in (("stored", zipfile.ZIP_STORED), ("deflated", zipfile.ZIP_DEFLATED)):
        with zipfile.ZipFile(os.path.join(out, kind + ".zip"), "w", method) as z:
            for name, data in m.items():
                z.writestr(name, data)
    # A fixed-Huffman deflate stream (level 1 on short data), as some zip tools write.
    with zipfile.ZipFile(os.path.join(out, "fixed.zip"), "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        z.writestr("short.bin", b"abcabcabcabc hello hello")
    # The stored data of one member changed: its CRC no longer matches.
    raw = bytearray(open(os.path.join(out, "stored.zip"), "rb").read())
    at = raw.find(b"line 0 of")
    raw[at] ^= 0xFF
    open(os.path.join(out, "badcrc.zip"), "wb").write(raw)
    # Central directories claiming more than the readers will allocate: past
    # the member cap, and more than deflate can make from the compressed size.
    for name, size in (("toobig.zip", 0x7FFFFFFF), ("ratio.zip", 50_000_000)):
        path = os.path.join(out, name)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("zeros.bin", bytes(1000))
        raw = bytearray(open(path, "rb").read())
        cd = raw.find(b"PK\x01\x02")
        struct.pack_into("<I", raw, cd + 24, size)
        open(path, "wb").write(raw)


def add(t, name, data=b"", kind=tarfile.REGTYPE, link="", mode=0o644):
    i = tarfile.TarInfo(name)
    i.type = kind
    i.mode = mode
    i.linkname = link
    i.size = len(data) if kind == tarfile.REGTYPE else 0
    t.addfile(i, io.BytesIO(data) if i.size else None)


def write_tar(path, fmt, entries):
    with tarfile.open(path, "w", format=fmt) as t:
        for e in entries:
            add(t, *e)


def write_tars(out):
    long_dir = "hotd4a/disk0/" + "/".join(["a_rather_long_folder_name"] * 6)
    game = [
        ("hotd4a", b"", tarfile.DIRTYPE, "", 0o755),
        ("hotd4a/disk0", b"", tarfile.DIRTYPE, "", 0o755),
        ("hotd4a/disk0/elf/hod4", b"\x7fELF" + bytes(2000), tarfile.REGTYPE, "", 0o755),
        ("hotd4a/disk0/readonly.bin", b"ro", tarfile.REGTYPE, "", 0o444),
        (long_dir + "/deep.txt", b"deep"),
        ("hotd4a/disk0/link_to_elf", b"", tarfile.SYMTYPE, "elf/hod4"),
        ("hotd4a/disk0/hard_copy", b"", tarfile.LNKTYPE, "hotd4a/disk0/elf/hod4"),
        ("hotd4a/disk0/._hod4", b"appledouble"),
        ("hotd4a/__MACOSX/x", b"finder"),
        ("hotd4a/disk0/.DS_Store", b"finder"),
    ]
    write_tar(os.path.join(out, "game-gnu.tar"), tarfile.GNU_FORMAT, game)
    write_tar(os.path.join(out, "game-pax.tar"), tarfile.PAX_FORMAT, game)
    # No top folder: the game folder's contents at the root, "./" in front.
    write_tar(os.path.join(out, "game-flat.tar"), tarfile.USTAR_FORMAT,
              [("./disk0", b"", tarfile.DIRTYPE, "", 0o755), ("./disk0/file.bin", b"flat")])
    write_tar(os.path.join(out, "escape-dotdot.tar"), tarfile.USTAR_FORMAT,
              [("disk0/../../evil", b"x")])
    write_tar(os.path.join(out, "escape-abs.tar"), tarfile.USTAR_FORMAT, [("/tmp/evil", b"x")])
    write_tar(os.path.join(out, "escape-symlink.tar"), tarfile.USTAR_FORMAT,
              [("disk0/out", b"", tarfile.SYMTYPE, "/tmp"), ("disk0/out/evil", b"x")])
    write_tar(os.path.join(out, "no-disk0.tar"), tarfile.USTAR_FORMAT, [("other/file", b"x")])
    whole = open(os.path.join(out, "game-gnu.tar"), "rb").read()
    open(os.path.join(out, "truncated.tar"), "wb").write(whole[:3000])
    open(os.path.join(out, "not-a-tar.tar"), "wb").write(b"PK\x03\x04" + bytes(1020))
    # A pax header claiming 4 MiB: refused before it is read into memory.
    h = bytearray(512)
    h[0:9] = b"PaxHeader"
    h[100:108] = b"0000644\0"
    h[124:136] = b"%011o\0" % (4 << 20)
    h[156:157] = b"x"
    h[257:263] = b"ustar\0"
    h[263:265] = b"00"
    h[148:156] = b" " * 8
    h[148:156] = b"%06o\0 " % sum(h)
    open(os.path.join(out, "huge-pax.tar"), "wb").write(bytes(h) + bytes(4 << 20) + bytes(1024))


def write_7z(out):
    """Solid LZMA2 and LZMA 7z sets, when py7zr is installed (CI installs it).
    No PPMd: py7zr writes 7 bytes of PPMd properties where 7-Zip writes 5,
    which the LZMA SDK (rightly) refuses."""
    try:
        import py7zr
    except ImportError:
        return
    m = members()
    for kind, filters in (("lzma2", [{"id": py7zr.FILTER_LZMA2, "preset": 7}]),
                          ("lzma", [{"id": py7zr.FILTER_LZMA, "preset": 7}])):
        with py7zr.SevenZipFile(os.path.join(out, kind + ".7z"), "w", filters=filters) as z:
            for name, data in m.items():
                z.writef(io.BytesIO(data), "set/" + name)  # a folder inside: dropped by the reader


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    write_zips(out)
    write_tars(out)
    write_7z(out)


if __name__ == "__main__":
    main()
