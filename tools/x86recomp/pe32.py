"""PE32 (32-bit x86 Windows) images for recomp.py.

The same interface as recomp.Image and xbe.XbeImage, for an exe or DLL built
for i386 (machine 0x14c, optional-header magic 0x10b): the Sega PC, RingWide
and Europa-R games. Addresses are virtual addresses at the image's preferred
base; the runtime maps the image there (32-bit guest memory is one 4 GB
window, so the base is always free).

Function discovery is the one MSVC programs without symbols already use
(xbe.Discovery), seeded with what a PE adds: its exports, its SafeSEH
handler table and its TLS callbacks. Imports are bound by the runtime's
loader, which reads the import directory itself and fills the IAT with
callable addresses; the translated code calls through the IAT like any
other function pointer.
"""
import bisect
import os
import struct

MACHINE_I386 = 0x14c
DIR_EXPORT, DIR_IMPORT, DIR_RESOURCE, DIR_EXCEPTION, DIR_SECURITY, DIR_BASERELOC = range(6)
DIR_DEBUG, DIR_ARCH, DIR_GLOBALPTR, DIR_TLS, DIR_LOADCONFIG, DIR_BOUND, DIR_IAT, DIR_DELAY = range(6, 14)
SEC_EXEC = 0x20000000
SEC_CODE = 0x20
SEC_DISCARDABLE = 0x02000000


class PE32Error(Exception):
    pass


def is_pe32(path):
    """A PE32 for i386 (not a PE32+, not another machine)."""
    with open(path, 'rb') as f:
        d = f.read(0x400)
    if d[:2] != b'MZ' or len(d) < 0x40:
        return False
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    if pe + 0x1a > len(d) or d[pe:pe + 4] != b'PE\0\0':
        return False
    machine = struct.unpack_from('<H', d, pe + 4)[0]
    magic = struct.unpack_from('<H', d, pe + 0x18)[0]
    return machine == MACHINE_I386 and magic == 0x10b


class PE32Image:
    """Sections, entry point, exports, imports, SafeSEH handlers and TLS
    callbacks of a PE32, laid out at its preferred base."""

    is_xbe = False
    is_pe32 = True

    def __init__(self, path):
        d = open(path, 'rb').read()
        self.raw = d
        self.file = os.path.basename(path)
        self.base = 0
        if d[:2] != b'MZ':
            raise PE32Error('%s: not an MZ file' % path)
        pe = struct.unpack_from('<I', d, 0x3c)[0]
        if d[pe:pe + 4] != b'PE\0\0':
            raise PE32Error('%s: no PE header' % path)
        machine, nsec, self.timestamp, _, _, opt_size, self.characteristics = struct.unpack_from('<HHIIIHH', d, pe + 4)
        if machine != MACHINE_I386:
            raise PE32Error('%s: not i386 (machine 0x%x)' % (path, machine))
        opt = pe + 24
        magic = struct.unpack_from('<H', d, opt)[0]
        if magic != 0x10b:
            raise PE32Error('%s: not PE32 (magic 0x%x)' % (path, magic))
        entry_rva = struct.unpack_from('<I', d, opt + 16)[0]
        self.image_base = struct.unpack_from('<I', d, opt + 28)[0]
        self.image_size = struct.unpack_from('<I', d, opt + 56)[0]
        self.header_size = struct.unpack_from('<I', d, opt + 60)[0]
        ndirs = struct.unpack_from('<I', d, opt + 92)[0]
        self.dirs = [struct.unpack_from('<II', d, opt + 96 + 8 * i) for i in range(min(ndirs, 16))]
        self.dirs += [(0, 0)] * (16 - len(self.dirs))
        self.sections = []
        so = opt + opt_size
        for i in range(nsec):
            name, vsize, rva, rsize, rptr = struct.unpack_from('<8sIIII', d, so + 40 * i)
            flags = struct.unpack_from('<I', d, so + 40 * i + 36)[0]
            self.sections.append(dict(name=name.rstrip(b'\0').decode('latin1'), rva=rva, vsize=vsize,
                                      rsize=rsize, rptr=rptr, flags=flags))
        ib = self.image_base
        self.entry = ib + entry_rva if entry_rva else 0
        self.exec_secs = []
        self.data_secs = []
        for s in self.sections:
            n = min(s['vsize'] or s['rsize'], s['rsize'])
            data = d[s['rptr']:s['rptr'] + n]
            if s['flags'] & (SEC_EXEC | SEC_CODE):
                self.exec_secs.append((ib + s['rva'], ib + s['rva'] + len(data), data, s['name']))
            elif data and not s['flags'] & SEC_DISCARDABLE:      # .reloc, debug sections
                self.data_secs.append((ib + s['rva'], data, s['name']))
        self.exec_secs.sort()
        self._lows = [lo for lo, hi, dd, nm in self.exec_secs]   # patch() keeps the bounds
        self._his = [hi for lo, hi, dd, nm in self.exec_secs]
        self.exports = self._exports()               # (name, va); "#n" for an ordinal-only export
        self.import_table = self._imports()          # (iat va, dll, name or "#n")
        self.seh_handlers = self._safeseh()
        self.tls_callbacks = self._tls_callbacks()
        self.reloc_sites = self._reloc_sites()       # VAs of the words the loader would fix up (HIGHLOW)
        # what recomp.Image has for an ELF; the runtime binds a PE's imports itself
        self.syms = {}
        self.imports = []
        self.plt_by_addr = {}
        self.copies = []
        self.copy_size = {}
        self.relocs = []
        self.init = 0
        self.pltgot = 0
        self.objects = {}
        self.func_by_name = {n: a for n, a in self.exports if not n.startswith('#')}
        self.ref_bias = [0]
        self.libs = []

    # ---- reading at virtual addresses ----
    def _off(self, rva):
        for s in self.sections:
            if s['rva'] <= rva < s['rva'] + max(s['vsize'], s['rsize']):
                o = rva - s['rva']
                return s['rptr'] + o if o < s['rsize'] else None
        return rva if rva < self.header_size else None

    def _u32(self, rva):
        o = self._off(rva)
        return struct.unpack_from('<I', self.raw, o)[0] if o is not None and o + 4 <= len(self.raw) else 0

    def _u16(self, rva):
        o = self._off(rva)
        return struct.unpack_from('<H', self.raw, o)[0] if o is not None else 0

    def _cstr(self, rva):
        o = self._off(rva)
        if o is None:
            return ''
        e = self.raw.index(b'\0', o)
        return self.raw[o:e].decode('latin1')

    def word(self, va):
        return self._u32(va - self.image_base) if va >= self.image_base else None

    def section_name(self, va):
        rva = va - self.image_base
        for s in self.sections:
            if s['rva'] <= rva < s['rva'] + s['vsize']:
                return s['name']
        return None

    # ---- directories ----
    def _exports(self):
        rva, size = self.dirs[DIR_EXPORT]
        if not rva:
            return []
        ib = self.image_base
        _, _, _, _, _, obase, nfuncs, nnames, afuncs, anames, aords = struct.unpack_from('<IIHHIIIIIII', self.raw, self._off(rva))
        names = {}
        for i in range(nnames):
            names[self._u16(aords + 2 * i)] = self._cstr(self._u32(anames + 4 * i))
        out = []
        for i in range(nfuncs):
            f = self._u32(afuncs + 4 * i)
            if not f or rva <= f < rva + size:       # empty, or forwarded to another DLL
                continue
            out.append((names.get(i, '#%d' % (obase + i)), ib + f))
        return out

    def _imports(self):
        rva, _ = self.dirs[DIR_IMPORT]
        out = []
        if not rva:
            return out
        ib = self.image_base
        while True:
            oft, _, _, name_rva, ft = struct.unpack_from('<IIIII', self.raw, self._off(rva))
            if not name_rva and not ft:
                break
            dll = self._cstr(name_rva)
            look = oft or ft
            k = 0
            while True:
                v = self._u32(look + 4 * k)
                if not v:
                    break
                name = '#%d' % (v & 0xffff) if v & 0x80000000 else self._cstr(v + 2)
                out.append((ib + ft + 4 * k, dll, name))
                k += 1
            rva += 20
        return out

    def _safeseh(self):
        """IMAGE_LOAD_CONFIG_DIRECTORY32: SEHandlerTable (a VA) at 0x40,
        SEHandlerCount at 0x44; the table holds handler RVAs."""
        rva, size = self.dirs[DIR_LOADCONFIG]
        if not rva:
            return []
        lsize = self._u32(rva)
        if lsize < 0x48:
            return []
        table, count = self._u32(rva + 0x40), self._u32(rva + 0x44)
        if not table or count > 1000000:
            return []
        ib = self.image_base
        return [ib + self._u32(table - ib + 4 * i) for i in range(count)]

    def _tls_callbacks(self):
        rva, _ = self.dirs[DIR_TLS]
        if not rva:
            return []
        cb = self._u32(rva + 12)           # AddressOfCallBacks (a VA)
        out = []
        while cb:
            v = self.word(cb)
            if not v:
                break
            out.append(v)
            cb += 4
        return out

    def _reloc_sites(self):
        """The base relocation table's 32-bit fixups (type 3): every word in
        the image that holds an address. A DLL keeps one; a fixed-base exe
        (Daytona.exe) has none."""
        rva, size = self.dirs[DIR_BASERELOC]
        if not rva or not size:
            return []
        o = self._off(rva)
        if o is None:
            return []
        out, end, ib = [], o + size, self.image_base
        while o + 8 <= end:
            page, bsize = struct.unpack_from('<II', self.raw, o)
            if bsize < 8:
                break
            for k in range(8, bsize - 1, 2):
                e = struct.unpack_from('<H', self.raw, o + k)[0]
                if e >> 12 == 3:
                    out.append(ib + page + (e & 0xfff))
            o += bsize
        return out

    def reloc_code_refs(self):
        """(site, target) for each fixed-up word that points into code."""
        out = []
        for a in self.reloc_sites:
            v = self.word(a)
            if v and self.in_exec(v):
                out.append((a, v))
        return out

    def _gcc_ctor_lists(self):
        """MinGW keeps __CTOR_LIST__/__DTOR_LIST__ in .text: -1, the
        constructors (16-byte aligned functions), 0."""
        out = set()
        for lo, hi, data, name in self.exec_secs:
            i = data.find(b'\xff\xff\xff\xff')
            while i >= 0:
                if i % 4 == 0:
                    fns, j = [], i + 4
                    while j + 4 <= len(data):
                        v = struct.unpack_from('<I', data, j)[0]
                        if not (v % 16 == 0 and self.in_exec(v)):
                            break
                        fns.append(v)
                        j += 4
                    if fns and j + 4 <= len(data) and struct.unpack_from('<I', data, j)[0] == 0:
                        out.update(fns)
                i = data.find(b'\xff\xff\xff\xff', i + 1)
        return out

    def extra_starts(self):
        """Function starts a PE names besides its entry point."""
        s = set(a for n, a in self.exports) | set(self.seh_handlers) | set(self.tls_callbacks) | self._gcc_ctor_lists()
        return sorted(a for a in s if self.in_exec(a))

    # ---- the recomp.Image interface ----
    def add_library(self, lib):
        raise PE32Error('a PE32 is translated on its own; its DLLs are programs of their own')

    def patch(self, addr, data):
        for k, (lo, hi, d, name) in enumerate(self.exec_secs):
            if lo <= addr and addr + len(data) <= hi:
                b = bytearray(d)
                b[addr - lo:addr - lo + len(data)] = data
                self.exec_secs[k] = (lo, hi, bytes(b), name)
                return
        raise PE32Error('patch outside code: 0x%x' % addr)

    def _sec_index(self, a):
        k = bisect.bisect_right(self._lows, a) - 1
        return k if k >= 0 and a < self._his[k] else -1

    def in_exec(self, a):
        k = bisect.bisect_right(self._lows, a) - 1
        return k >= 0 and a < self._his[k]

    def sec_of(self, a):
        k = self._sec_index(a)
        if k < 0:
            return None
        lo, hi, data, name = self.exec_secs[k]
        return lo, hi, data
