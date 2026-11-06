"""PE32+ (x86-64 Windows) images for recomp64.py.

Reads what the translator and the loader need from an exe or DLL: sections,
the entry point, imports (bound and delay-loaded), exports, base relocations,
the unwind table (.pdata) and TLS. Addresses are RVAs: image-relative, so the
runtime can place the image anywhere.
"""
import bisect
import re
import struct

IMAGE_FILE_MACHINE_AMD64 = 0x8664
DIR_EXPORT, DIR_IMPORT, DIR_RESOURCE, DIR_EXCEPTION, DIR_SECURITY, DIR_BASERELOC = range(6)
DIR_DEBUG, DIR_ARCH, DIR_GLOBALPTR, DIR_TLS, DIR_LOADCONFIG, DIR_BOUND, DIR_IAT, DIR_DELAY = range(6, 14)

SEC_CODE = 0x20
SEC_INITDATA = 0x40
SEC_UNINIT = 0x80
SEC_EXEC = 0x20000000
SEC_READ = 0x40000000
SEC_WRITE = 0x80000000


class PEError(Exception):
    pass


class Section:
    def __init__(self, name, rva, vsize, raw, flags):
        self.name = name
        self.rva = rva
        self.vsize = vsize
        self.data = raw          # file bytes; shorter than vsize when the rest is zero
        self.flags = flags

    @property
    def end(self):
        return self.rva + max(self.vsize, len(self.data))

    @property
    def exec(self):
        return bool(self.flags & SEC_EXEC)

    @property
    def write(self):
        return bool(self.flags & SEC_WRITE)


def is_pe64(path):
    with open(path, 'rb') as f:
        h = f.read(0x40)
        if len(h) < 0x40 or h[:2] != b'MZ':
            return False
        off = struct.unpack_from('<I', h, 0x3c)[0]
        f.seek(off)
        s = f.read(6)
        return s[:4] == b'PE\0\0' and struct.unpack_from('<H', s, 4)[0] == IMAGE_FILE_MACHINE_AMD64


class PEImage:
    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            self.raw = f.read()
        raw = self.raw
        if raw[:2] != b'MZ':
            raise PEError('not a PE file')
        pe = struct.unpack_from('<I', raw, 0x3c)[0]
        if raw[pe:pe + 4] != b'PE\0\0':
            raise PEError('no PE signature')
        (machine, nsec, self.timestamp, symptr, nsyms, opt_size, self.characteristics) = struct.unpack_from('<HHIIIHH', raw, pe + 4)
        if machine != IMAGE_FILE_MACHINE_AMD64:
            raise PEError('not x86-64 (machine 0x%x)' % machine)
        opt = pe + 24
        magic = struct.unpack_from('<H', raw, opt)[0]
        if magic != 0x20b:
            raise PEError('not PE32+')
        self.entry = struct.unpack_from('<I', raw, opt + 16)[0]
        self.image_base = struct.unpack_from('<Q', raw, opt + 24)[0]
        self.section_align, self.file_align = struct.unpack_from('<II', raw, opt + 32)
        self.size_of_image = struct.unpack_from('<I', raw, opt + 56)[0]
        self.size_of_headers = struct.unpack_from('<I', raw, opt + 60)[0]
        self.subsystem, self.dll_characteristics = struct.unpack_from('<HH', raw, opt + 68)
        self.stack_reserve, self.stack_commit, self.heap_reserve, self.heap_commit = struct.unpack_from('<QQQQ', raw, opt + 72)
        ndirs = struct.unpack_from('<I', raw, opt + 108)[0]
        self.dirs = []
        for i in range(ndirs):
            self.dirs.append(struct.unpack_from('<II', raw, opt + 112 + 8 * i))
        self.is_dll = bool(self.characteristics & 0x2000)
        # sections
        self.sections = []
        sh = opt + opt_size
        for i in range(nsec):
            name, vsize, rva, rawsize, rawptr, _, _, _, _, flags = struct.unpack_from('<8sIIIIIIHHI', raw, sh + 40 * i)
            name = name.rstrip(b'\0').decode('latin-1')
            data = raw[rawptr:rawptr + min(rawsize, vsize if vsize else rawsize)] if rawsize else b''
            self.sections.append(Section(name, rva, vsize or rawsize, data, flags))
        self.sections.sort(key=lambda s: s.rva)
        self._sec_rvas = [s.rva for s in self.sections]
        self.headers = raw[:self.size_of_headers]
        self.exec_secs = [s for s in self.sections if s.exec]
        self._read_imports()
        self._read_delay_imports()
        self._read_exports()
        self._read_relocs()
        self._read_pdata()
        self._read_tls()
        self._read_load_config()

    # ---- memory ----------------------------------------------------------
    def sec_of(self, rva):
        k = bisect.bisect_right(self._sec_rvas, rva) - 1
        if k >= 0 and rva < self.sections[k].end:
            return self.sections[k]
        return None

    def in_exec(self, rva):
        s = self.sec_of(rva)
        return s is not None and s.exec and rva < s.rva + len(s.data)

    def in_image(self, rva):
        return 0 <= rva < self.size_of_image

    def read(self, rva, n):
        """n bytes at rva (zero past a section's file data)."""
        if rva < self.size_of_headers:
            d = self.headers[rva:rva + n]
            return d + b'\0' * (n - len(d))
        s = self.sec_of(rva)
        if s is None:
            raise PEError('read outside image: 0x%x' % rva)
        off = rva - s.rva
        d = s.data[off:off + n]
        return d + b'\0' * (n - len(d))

    def u8(self, rva): return self.read(rva, 1)[0]
    def u16(self, rva): return struct.unpack('<H', self.read(rva, 2))[0]
    def u32(self, rva): return struct.unpack('<I', self.read(rva, 4))[0]
    def u64(self, rva): return struct.unpack('<Q', self.read(rva, 8))[0]

    def cstr(self, rva, limit=4096):
        s = self.sec_of(rva)
        if s is None:
            return ''
        off = rva - s.rva
        end = s.data.find(b'\0', off, off + limit)
        if end < 0:
            end = min(len(s.data), off + limit)
        return s.data[off:end].decode('latin-1')

    def patch(self, rva, data):
        s = self.sec_of(rva)
        if s is None or rva + len(data) > s.rva + len(s.data):
            raise PEError('patch outside file data: 0x%x' % rva)
        b = bytearray(s.data)
        off = rva - s.rva
        b[off:off + len(data)] = data
        s.data = bytes(b)

    # ---- imports ------------------------------------------------------------
    def _read_imports(self):
        """self.imports: list of (iat_rva, dll, name, ordinal or None), in IAT order."""
        self.imports = []
        self.import_dlls = []
        rva, size = self.dirs[DIR_IMPORT] if len(self.dirs) > DIR_IMPORT else (0, 0)
        if not rva:
            return
        d = rva
        while True:
            ilt, ts, fwd, name_rva, iat = struct.unpack('<IIIII', self.read(d, 20))
            if not ilt and not iat and not name_rva:
                break
            dll = self.cstr(name_rva)
            self.import_dlls.append(dll)
            self._thunks(ilt or iat, iat, dll, self.imports)
            d += 20

    def _thunks(self, lookup, iat, dll, out):
        k = 0
        while True:
            e = self.u64(lookup + 8 * k)
            if not e:
                break
            if e >> 63:
                out.append((iat + 8 * k, dll, None, e & 0xffff))
            else:
                hint_rva = e & 0x7fffffff
                out.append((iat + 8 * k, dll, self.cstr(hint_rva + 2), None))
            k += 1

    def _read_delay_imports(self):
        """Delay-loaded imports are resolved like the others: (iat_rva, dll, name, ordinal)."""
        self.delay_imports = []
        self.delay_dlls = []
        rva, size = self.dirs[DIR_DELAY] if len(self.dirs) > DIR_DELAY else (0, 0)
        if not rva:
            return
        d = rva
        while True:
            attrs, name_rva, hmod, iat, ilt, biat, uiat, ts = struct.unpack('<IIIIIIII', self.read(d, 32))
            if not name_rva:
                break
            if not (attrs & 1):
                # pre-VC7 style: fields are VAs
                name_rva -= self.image_base & 0xffffffff
                iat -= self.image_base & 0xffffffff
                ilt -= self.image_base & 0xffffffff
            dll = self.cstr(name_rva)
            self.delay_dlls.append(dll)
            self._thunks(ilt, iat, dll, self.delay_imports)
            d += 32

    # ---- exports ---------------------------------------------------------------
    def _read_exports(self):
        """self.exports: list of (name or None, ordinal, rva, forwarder or None)."""
        self.exports = []
        self.export_name = ''
        rva, size = self.dirs[DIR_EXPORT] if len(self.dirs) > DIR_EXPORT else (0, 0)
        if not rva:
            return
        (flags, ts, maj, mnr, name_rva, base, nfunc, nnames, funcs, names, ords) = struct.unpack('<IIHHIIIIIII', self.read(rva, 40))
        self.export_name = self.cstr(name_rva)
        by_ord = {}
        for i in range(nfunc):
            f = self.u32(funcs + 4 * i)
            if f:
                fwd = self.cstr(f) if rva <= f < rva + size else None
                by_ord[base + i] = (f, fwd)
        named = {}
        for i in range(nnames):
            n = self.cstr(self.u32(names + 4 * i))
            o = base + self.u16(ords + 2 * i)
            named[o] = n
        for o, (f, fwd) in sorted(by_ord.items()):
            self.exports.append((named.get(o), o, f, fwd))

    # ---- relocations -----------------------------------------------------------
    def _read_relocs(self):
        """self.relocs: sorted RVAs of 64-bit absolute addresses the loader fixes up
        (IMAGE_REL_BASED_DIR64); other kinds are counted in self.reloc_other."""
        self.relocs = []
        self.reloc_other = 0
        self._reloc_set = set()
        rva, size = self.dirs[DIR_BASERELOC] if len(self.dirs) > DIR_BASERELOC else (0, 0)
        # a program without a relocation directory can only run at its preferred base
        self.relocatable = bool(rva)
        if not rva:
            return
        end = rva + size
        d = rva
        while d + 8 <= end:
            page, bsize = struct.unpack('<II', self.read(d, 8))
            if bsize < 8:
                break
            ents = self.read(d + 8, bsize - 8)
            for i in range(0, len(ents) - 1, 2):
                e = struct.unpack_from('<H', ents, i)[0]
                t, off = e >> 12, e & 0xfff
                if t == 10:
                    self.relocs.append(page + off)
                elif t != 0:
                    self.reloc_other += 1
            d += bsize
        self.relocs.sort()
        self._reloc_set = set(self.relocs)

    def has_reloc(self, rva):
        return rva in self._reloc_set

    # ---- unwind table ----------------------------------------------------------
    def _read_pdata(self):
        """self.functions: sorted list of (begin, end, unwind_rva) from .pdata, with
        chained entries (function fragments) resolved to their parent in
        self.fragment_parent: begin -> parent begin."""
        self.pdata = []
        self.fragment_parent = {}
        rva, size = self.dirs[DIR_EXCEPTION] if len(self.dirs) > DIR_EXCEPTION else (0, 0)
        if not rva:
            return
        n = size // 12
        for i in range(n):
            b, e, u = struct.unpack('<III', self.read(rva + 12 * i, 12))
            if b and e > b:
                self.pdata.append((b, e, u))
        self.pdata.sort()
        # UNWIND_INFO: version:3 flags:5 (0x4 = chained), prolog size, count, frame
        for b, e, u in self.pdata:
            parent = self._chain_parent(u)
            if parent is not None:
                self.fragment_parent[b] = parent

    def _chain_parent(self, u, depth=0):
        hdr = self.read(u, 4)
        flags = hdr[0] >> 3
        if not (flags & 4) or depth > 8:
            return None
        count = hdr[2]
        # the chained RUNTIME_FUNCTION follows the codes (aligned to 4 bytes)
        off = u + 4 + ((count + 1) & ~1) * 2
        b, e, u2 = struct.unpack('<III', self.read(off, 12))
        p = self._chain_parent(u2, depth + 1)
        return p if p is not None else b

    def unwind_flags(self, u):
        return self.read(u, 1)[0] >> 3

    def unwind_handler(self, u):
        """(handler rva, data rva) for an unwind info with an exception/termination
        handler, else None."""
        hdr = self.read(u, 4)
        flags = hdr[0] >> 3
        if not (flags & 3):
            return None
        count = hdr[2]
        off = u + 4 + ((count + 1) & ~1) * 2
        return self.u32(off), off + 4

    def prolog_size(self, u):
        return self.read(u, 2)[1]

    def seh_scopes(self, u, lo, hi):
        """The C scope table ([(begin, end, filter, target)], RVAs) of a function
        whose language handler is __C_specific_handler, directly or through
        __GSHandlerCheck_SEH; None for other handlers (C++, GS only) and for
        functions without one. filter is 1 for EXCEPTION_EXECUTE_HANDLER, else a
        filter funclet; target is 0 for a __finally scope (filter is then the
        termination handler)."""
        h = self.unwind_handler(u)
        if h is None:
            return None
        hr, dr = h
        if not self._handler_is_seh(hr):
            return None
        try:
            n = self.u32(dr)
            if n == 0 or n > 4096:
                return None
            out = []
            for i in range(n):
                b, e, f, t = struct.unpack('<IIII', self.read(dr + 4 + 16 * i, 16))
                if not (lo <= b < e <= hi) or (t and not lo <= t < hi) or (f != 1 and not self.in_exec(f)):
                    return None
                out.append((b, e, f, t))
            return out
        except PEError:
            return None

    def _handler_is_seh(self, hr):
        """Does the language handler at hr end up in __C_specific_handler? It is
        the import's thunk (`jmp [__imp___C_specific_handler]`), or
        __GSHandlerCheck_SEH, which tail-jumps to it after checking the cookie.
        The decision is cached per handler address."""
        cache = self.__dict__.setdefault('_seh_handlers', {})
        if hr in cache:
            return cache[hr]
        res = self._refs_c_specific_handler(hr, 0)
        cache[hr] = res
        return res

    def _refs_c_specific_handler(self, rva, depth):
        if depth > 2 or not self.in_exec(rva):
            return False
        from capstone import Cs, CS_ARCH_X86, CS_MODE_64
        md = self.__dict__.get('_md_lite')
        if md is None:
            md = self.__dict__['_md_lite'] = Cs(CS_ARCH_X86, CS_MODE_64)
        slots = self.iat_slots()
        sec = self.sec_of(rva)
        data = sec.data[rva - sec.rva:rva - sec.rva + 256]
        n = 0
        for addr, size, mn, ops in md.disasm_lite(data, rva):
            n += 1
            m = re.match(r'(?:qword ptr )?\[rip \+ (0x[0-9a-f]+)\]', ops)
            if m and mn in ('jmp', 'call'):
                slot = addr + size + int(m.group(1), 16)
                imp = slots.get(slot)
                if imp and imp[1] == '__C_specific_handler':
                    return True
            elif mn == 'jmp' and ops.startswith('0x'):
                if self._refs_c_specific_handler(int(ops, 16), depth + 1):
                    return True
            if mn in ('ret', 'int3') or n > 48:
                break
            if mn == 'jmp':
                break
        # statically linked CRT: the name survives in the export table only by luck;
        # accept a direct jump target that is itself an exported __C_specific_handler
        return any(nm == '__C_specific_handler' and r == rva for nm, o, r, fwd in self.exports)

    # ---- TLS ----------------------------------------------------------------------
    def _read_tls(self):
        self.tls = None
        rva, size = self.dirs[DIR_TLS] if len(self.dirs) > DIR_TLS else (0, 0)
        if not rva:
            return
        start, end, index_va, callbacks_va, zero_fill, chars = struct.unpack('<QQQQII', self.read(rva, 40))
        base = self.image_base
        cbs = []
        if callbacks_va:
            k = 0
            while True:
                cb = self.u64(callbacks_va - base + 8 * k)
                if not cb:
                    break
                cbs.append(cb - base)
                k += 1
        self.tls = {
            'raw_start': start - base if start else 0,
            'raw_end': end - base if end else 0,
            'index': index_va - base if index_va else 0,
            'callbacks': cbs,
            'zero_fill': zero_fill,
            'align': 1 << (((chars >> 20) & 0xf) - 1) if (chars >> 20) & 0xf else 1,
        }

    def _read_load_config(self):
        self.security_cookie = 0
        rva, size = self.dirs[DIR_LOADCONFIG] if len(self.dirs) > DIR_LOADCONFIG else (0, 0)
        if not rva or size < 0x60:
            return
        cookie_va = self.u64(rva + 0x58)
        if cookie_va:
            self.security_cookie = cookie_va - self.image_base

    # ---- helpers for the translator --------------------------------------------
    def iat_slots(self):
        """rva -> (dll, name, ordinal) for every import slot, bound and delay-loaded."""
        d = {}
        for iat, dll, name, o in self.imports:
            d[iat] = (dll, name, o)
        for iat, dll, name, o in self.delay_imports:
            d[iat] = (dll, name, o)
        return d

    def code_pointers_in_data(self):
        """RVAs of code that absolute pointers in the data sections point at
        (function pointers, vtables, TLS callbacks): every DIR64 fixup whose
        value lands in an executable section."""
        out = set()
        base = self.image_base
        for r in self.relocs:
            s = self.sec_of(r)
            if s is None or s.exec:
                continue
            v = self.u64(r)
            t = v - base
            if 0 <= t < self.size_of_image and self.in_exec(t):
                out.add(t)
        return out
