#!/usr/bin/env python3
"""tools/filelist/make_filelist.py and include/afcommon/filecheck.h: a list
made from a game's tar (or folder) passes the same folder and catches a
wrong byte, a short file and a missing one. Needs a C compiler (cc)."""
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, 'tools', 'filelist', 'make_filelist.py')

CHECK_C = r'''
#include <stdio.h>
#include "list.h"
int main(int argc, char **argv) {
    (void)argc;
    char report[256];
    int r = afc_check_files(argv[1], afc_filelist, AFC_FILELIST_COUNT, report, sizeof report);
    printf("%d %s\n", r, report);
    return 0;
}
'''


class FileListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.game = os.path.join(self.tmp, 'games', 'hotd4a')
        files = {'disk0/elf/game.elf': os.urandom(200000), 'disk0/data/a "q".bin': b'x' * 70000,
                 'disk0/.hidden': b'h', 'disk0/empty': b''}
        for rel, data in files.items():
            p = os.path.join(self.game, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'wb') as f:
                f.write(data)
        self.tar = os.path.join(self.tmp, 'hotd4a.tar')
        with tarfile.open(self.tar, 'w') as t:
            t.add(self.game, arcname='hotd4a')

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def build(self, source):
        out = os.path.join(self.tmp, 'list.h')
        subprocess.run([sys.executable, TOOL, source, '-o', out], check=True, capture_output=True)
        c = os.path.join(self.tmp, 'check.c')
        with open(c, 'w') as f:
            f.write(CHECK_C)
        exe = os.path.join(self.tmp, 'check')
        subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror', '-I', os.path.join(ROOT, 'include'),
                        '-I', self.tmp, c, '-o', exe], check=True)
        with open(out) as f:
            self.assertIn('#define AFC_FILELIST_COUNT 4', f.read())
        return exe

    def check(self, exe):
        return subprocess.run([exe, self.game], check=True, capture_output=True, text=True).stdout.strip()

    def test_tar_and_folder(self):
        for source in (self.tar, self.game):
            exe = self.build(source)
            self.assertEqual(self.check(exe), '1')

    def test_problems(self):
        exe = self.build(self.tar)
        elf = os.path.join(self.game, 'disk0', 'elf', 'game.elf')
        with open(elf, 'r+b') as f:
            f.seek(1000)
            b = f.read(1)
            f.seek(1000)
            f.write(bytes([b[0] ^ 1]))
        self.assertEqual(self.check(exe), '0 disk0/elf/game.elf: wrong CRC')
        with open(elf, 'r+b') as f:
            f.truncate(100)
        self.assertEqual(self.check(exe), '0 disk0/elf/game.elf: wrong size')
        os.remove(elf)
        self.assertEqual(self.check(exe), '0 disk0/elf/game.elf: missing')


if __name__ == '__main__':
    unittest.main()
