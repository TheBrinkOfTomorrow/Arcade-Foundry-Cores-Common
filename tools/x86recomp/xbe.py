"""Xbox executables (XBE) for the recompiler.

An XBE is the Xbox's executable: a header, a table of sections loaded at
fixed addresses, and a thunk table the loader fills with kernel addresses by
ordinal. A retail or arcade game has no symbols, so its functions are found
by following calls from the entry point and the code addresses its data and
instructions hold (vtables, callbacks, thread routines); see find_starts().
"""
import array
import bisect
import collections
import os
import re
import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_32
from capstone.x86 import X86_OP_IMM, X86_OP_MEM

# RECOMP_WHY=0xADDR: sane() says why it rejects a start there
_WHY = int(os.environ['RECOMP_WHY'], 16) if os.environ.get('RECOMP_WHY') else None

# (entry point XOR, kernel thunk XOR) for each signing key
KEYS = {
    'retail': (0xA8FC57AB, 0x5B6D40B6),
    'debug': (0x94859D4B, 0xEFB1F152),
    'chihiro': (0x40B5C16E, 0x2290059D),
}

# Every XBE section is marked executable, so code is told from data by name:
# the SDK links each of its libraries into a code section of its own name
# (D3D, DSOUND, XPP, ...); these are the data ones.
DATA_SECTIONS = {'.rdata', '.data', '.data1', '.bss', '.tls', '.idata', 'DOLBY', 'XON_RD', '.XTLID'}


def is_code_section(name):
    return not (name in DATA_SECTIONS or name.startswith('$$') or name.startswith('.data')
                or name.startswith('.rdata'))


def u32(d, o):
    return struct.unpack_from('<I', d, o)[0]


def is_xbe(path):
    with open(path, 'rb') as f:
        return f.read(4) == b'XBEH'


class XbeImage:
    """The same interface as recomp.Image, for an XBE."""

    is_xbe = True

    def __init__(self, path, code_sections=None):
        import os
        d = open(path, 'rb').read()
        self.file = os.path.basename(path)
        self.base = 0
        self.raw = d
        hbase = u32(d, 0x104)
        self.image_base = hbase
        self.image_size = u32(d, 0x10C)
        self.header_size = u32(d, 0x108)

        def cstr(va):
            o = va - hbase
            return d[o:d.index(b'\0', o)].decode('latin1')
        self.sections = []
        nsec, secaddr = u32(d, 0x11C), u32(d, 0x120)
        for i in range(nsec):
            o = secaddr - hbase + i * 0x38
            fl, va, vs, ra, rs, na = struct.unpack_from('<6I', d, o)
            self.sections.append(dict(name=cstr(na), flags=fl, va=va, vsize=vs, raw=ra, rsize=rs))
        ep, th = u32(d, 0x128), u32(d, 0x158)
        self.key = None
        for k, (ek, tk) in KEYS.items():
            if hbase <= ep ^ ek < hbase + self.image_size and hbase <= th ^ tk < hbase + self.image_size:
                self.key, self.entry, self.thunk = k, ep ^ ek, th ^ tk
                break
        if self.key is None:
            raise Exception('%s: no known key decodes the entry point' % path)
        code = set(code_sections) if code_sections else None
        self.exec_secs = []
        self.data_secs = []
        for s in self.sections:
            data = d[s['raw']:s['raw'] + s['rsize']]
            if (s['name'] in code) if code else is_code_section(s['name']):
                self.exec_secs.append((s['va'], s['va'] + s['rsize'], data, s['name']))
            else:
                self.data_secs.append((s['va'], data, s['name']))
        self.exec_secs.sort()
        # kernel imports by ordinal, in thunk table order
        self.kernel_imports = []
        a = self.thunk
        while True:
            v = self.word(a)
            if not v:
                break
            self.kernel_imports.append((a, v & 0x7fffffff))
            a += 4
        # what recomp.Image has for an ELF; an XBE has none of it
        self.syms = {}
        self.imports = []
        self.plt_by_addr = {}
        self.copies = []
        self.copy_size = {}
        self.relocs = []
        self.exports = []
        self.init = 0
        self.pltgot = 0
        self.objects = {}
        self.func_by_name = {}
        self.ref_bias = [0]
        self.libs = []

    def word(self, va):
        for s in self.sections:
            if s['va'] <= va < s['va'] + s['rsize'] - 3:
                return u32(self.raw, s['raw'] + va - s['va'])
        return None

    def section_name(self, va):
        for s in self.sections:
            if s['va'] <= va < s['va'] + s['vsize']:
                return s['name']
        return None

    def add_library(self, lib):
        raise Exception('an XBE has no shared libraries')

    def patch(self, addr, data):
        for k, (lo, hi, d, name) in enumerate(self.exec_secs):
            if lo <= addr and addr + len(data) <= hi:
                b = bytearray(d)
                b[addr - lo:addr - lo + len(data)] = data
                self.exec_secs[k] = (lo, hi, bytes(b), name)
                return
        raise Exception('patch outside code: 0x%x' % addr)

    def _sec_index(self, a):
        if getattr(self, '_lows_n', None) != len(self.exec_secs):
            self._lows = [lo for lo, hi, d, n in self.exec_secs]
            self._lows_n = len(self.exec_secs)
        k = bisect.bisect_right(self._lows, a) - 1
        return k if k >= 0 and a < self.exec_secs[k][1] else -1

    def in_exec(self, a):
        return self._sec_index(a) >= 0

    def sec_of(self, a):
        k = self._sec_index(a)
        if k < 0:
            return None
        lo, hi, data, name = self.exec_secs[k]
        return lo, hi, data


# ---- function discovery ---------------------------------------------------------

# instructions a 32-bit Xbox game never runs: decoding into one means the
# bytes are data, not code
_BAD = {'arpl', 'bound', 'outsd', 'outsb', 'outsw', 'insd', 'insb', 'insw', 'lcall', 'ljmp', 'popal', 'pushal',
        'aaa', 'aas', 'daa', 'das', 'aam', 'aad', 'into', 'int1', 'salc', 'xlatb', 'les', 'lds', 'retf', 'iretd',
        'int', 'hlt', 'sldt', 'sgdt', 'sidt', 'lsl', 'lar', 'verr', 'verw', 'enter', 'icebp'}


# a CRT x87 dispatch descriptor (packed): +0 type byte, +1 name NUL-padded to
# 12 bytes, +0xd argument count, +0xe flags, +0x10 the handlers, any alignment
_TRANDISP = re.compile(rb'[\x01-\x0f][a-z][a-z0-9]{1,10}\x00')

def words(data):
    """The little-endian 32-bit words at offsets 0, 4, 8, ... of data."""
    w = array.array('I')
    assert w.itemsize == 4
    w.frombytes(bytes(data[:len(data) // 4 * 4]))
    if sys.byteorder != 'little':
        w.byteswap()
    return w


def _is_term(mnem):
    return mnem in ('ret', 'retf', 'jmp', 'int3', 'hlt', 'ud2', 'iretd')


# multi-byte nops GNU as pads functions with (the bytes that end the padding)
GCC_FILLERS = tuple(bytes.fromhex(h) for h in (
    '8db42600000000', '8dbc2700000000', '8db600000000', '8dbf00000000', '8d742600', '8d7600',
    '89f6', '89ff', '0f1f00', '0f1f4000', '0f1f440000', '660f1f440000', '0f1f8000000000',
    '0f1f840000000000', '660f1f840000000000', '662e0f1f840000000000'))


class Discovery:
    """Recursive descent over an XBE's code.

    Starts: the entry point, direct call targets, and code addresses found in
    data or in instruction immediates that are not inside code already reached
    (a pointer into reached code is a switch label, not a function). Jump
    tables (jmp [reg*4+table]) are found while walking, and their entries are
    labels of the function that jumps through them."""

    def __init__(self, img):
        self.img = img
        self.md = Cs(CS_ARCH_X86, CS_MODE_32)
        self.md.detail = True
        self.reached = {}       # instruction address -> size
        self.owner = {}         # instruction address -> function start
        self.starts = set()
        self.jtables = {}       # jmp address -> targets
        self.jt_ranges = []     # (lo, hi) bytes of tables inside code
        self._jt_seen = set()   # jt_ranges as a set: each range is listed once
        self._jt_index = None   # (len, lows, running max of highs) for in_table
        self._bt_done = 0       # reached addresses byte_tables has decoded
        self._bt_seen = set()
        self._bt_vs = []        # their byte tables' addresses (movzx disp)
        self._bt_len = {}       # a byte table's length from its switch's cmp bound
        self.imm_refs = set()   # code addresses in instruction immediates and displacements
        self.fn_ptrs = set()    # code addresses pushed or moved as immediates: function pointers
        self.weak = set()       # starts guessed from immediates and data, not reached by a call
        self.rejected = set()   # guessed starts that decode across another function's instructions
        self.collided = set()   # PE32: starts whose walk ran into the middle of a reached instruction
        self._pe32 = getattr(img, 'is_pe32', False)
        # speed only (no effect on results): bytes strictly inside a reached
        # instruction, and what sane() needs of each decoded address, over
        # the span of the code sections
        secs = img.exec_secs
        self._span_lo = secs[0][0] if secs else 0
        span = (max(hi for lo, hi, d, n in secs) - self._span_lo) if secs else 0
        if 0 < span <= (256 << 20):
            self._inside = bytearray(span)
            # kept when run() starts discovery over: it depends on the bytes only
            old = getattr(self, '_sinfo', None)
            self._sinfo = old if old is not None and len(old) == span else array.array('Q', bytes(8 * span))
        else:
            self._inside = self._sinfo = None

    def _after_dbg(self, ins):
        """An int3 straight after int 0x2d (the kernel debug service) is
        skipped when no debugger is attached: code carries on after it."""
        if ins.mnemonic != 'int3':
            return False
        p = self._decode(ins.address - 2)
        return p is not None and bytes(p.bytes) == b'\xcd\x2d'

    def _decode(self, a):
        sec = self.img.sec_of(a)
        if not sec:
            return None
        lo, hi, data = sec
        for ins in self.md.disasm(data[a - lo:min(a - lo + 16, hi - lo)], a, 1):
            return ins
        return None

    def walk(self, start):
        """Reach one function; returns new call targets."""
        calls = set()
        work = [start]
        while work:
            a = work.pop()
            prev = None
            while a not in self.reached:
                if self._pe32 and self._in_reached(a):
                    # a PE32: decoding has run into the middle of an instruction
                    # already reached, so this is not code (a constant taken for
                    # a pointer, falling into Daytona's 0xfe01f0 off its
                    # instruction boundaries); stop rather than reach bytes
                    # another function owns
                    self.collided.add(start)
                    break
                ins = self._decode(a)
                if ins is None:
                    break
                self.reached[a] = ins.size
                if self._inside is not None:
                    o = a - self._span_lo
                    self._inside[o + 1:o + ins.size] = b'\x01' * (ins.size - 1)
                self.owner[a] = start
                mn = ins.mnemonic
                for op in ins.operands:
                    if op.type == X86_OP_IMM:
                        v = op.imm & 0xffffffff
                        if mn not in ('call', 'jmp') and not mn.startswith('j') and self.img.in_exec(v):
                            self.imm_refs.add(v)
                            if mn in ('push', 'mov'):
                                self.fn_ptrs.add(v)
                    elif op.type == X86_OP_MEM and not op.mem.base and op.mem.disp:
                        v = op.mem.disp & 0xffffffff
                        if self.img.in_exec(v) and not (op.mem.index and mn == 'jmp'):
                            self.imm_refs.add(v)
                nxt = a + ins.size
                if mn == 'call':
                    op = ins.operands[0]
                    if op.type == X86_OP_IMM:
                        t = op.imm & 0xffffffff
                        if t != nxt and self.img.in_exec(t):
                            calls.add(t)
                    a = nxt
                    continue
                if mn == 'jmp':
                    op = ins.operands[0]
                    if op.type == X86_OP_IMM:
                        t = op.imm & 0xffffffff
                        if self.img.in_exec(t):
                            if t in self.starts:
                                pass        # a tail call
                            else:
                                work.append(t)
                    elif op.type == X86_OP_MEM and op.mem.index and op.mem.scale == 4 and not op.mem.base:
                        neg = prev is not None and prev.mnemonic == 'neg' and prev.operands[0].type == 1 \
                            and prev.operands[0].reg == op.mem.index
                        targets = self.table(ins, op.mem.disp & 0xffffffff, neg)
                        self.jtables[a] = targets
                        work.extend(targets)
                    break
                if mn.startswith('j') or mn.startswith('loop') or mn == 'jecxz':
                    op = ins.operands[0]
                    if op.type == X86_OP_IMM:
                        t = op.imm & 0xffffffff
                        if self.img.in_exec(t):
                            work.append(t)
                    a = nxt
                    continue
                if _is_term(mn) and not self._after_dbg(ins):
                    break
                prev = ins
                a = nxt
        return calls

    def table(self, ins, tlo, neg=False):
        """The entries of a jmp [reg*4+tlo] table: code addresses until one is
        not. An index that starts at 1 leaves a first word that is not one
        (MSVC's memcpy); after neg reg the entries run down from tlo."""
        out = []
        a = tlo
        v = self.img.word(a)
        if (v is None or not self.img.in_exec(v)) and self.img.in_exec(self.img.word(a + 4) or 0):
            a += 4
        lo = a
        # an index from a byte table just before (movzx r, byte [r + bt];
        # jmp [r*4 + tlo]): MSVC puts that byte table right after the
        # pointers, so the pointers end where it begins (Daytona 2017's
        # window procedure 0x422340: its index bytes 00 05 05 01 were read
        # as a seventh entry, 0x1050500, inside another function)
        bt = self._byte_table_before(ins)
        while len(out) < 4096:
            if bt is not None and a == bt:
                break
            v = self.img.word(a)
            if v is None or not self.img.in_exec(v):
                break
            out.append(v)
            a += 4
        if neg:
            while len(out) < 4096:
                v = self.img.word(lo - 4)
                if v is None or not self.img.in_exec(v) or (lo - 4) in self.reached:
                    break
                lo -= 4
                out.insert(0, v)
        if self.img.in_exec(lo):
            self._add_range(lo, a)
        return out

    def _byte_table_before(self, ins):
        """The byte index table read by a movzx up to three instructions
        before the table jump, or None."""
        p = ins.address
        for _ in range(3):
            q = next((p - k for k in range(1, 16) if self.reached.get(p - k) == k), None)
            if q is None:
                return None
            pi = self._decode(q)
            if pi is not None and pi.mnemonic == 'movzx' and len(pi.operands) == 2:
                op = pi.operands[1]
                if op.type == X86_OP_MEM and op.size == 1 and self.img.in_exec(op.mem.disp & 0xffffffff):
                    return op.mem.disp & 0xffffffff
            p = q
        return None

    def _add_range(self, lo, hi):
        if (lo, hi) not in self._jt_seen:
            self._jt_seen.add((lo, hi))
            self.jt_ranges.append((lo, hi))

    def byte_tables(self):
        """movzx r, byte ptr [r + disp] with disp in code: index tables (MSVC
        switches with many cases mapping to few targets). Their extent runs to
        the next reached instruction or the first byte too big to be a case.
        Called every round: each instruction is decoded once (reached only
        grows), and each table's extent is measured again against what is
        reached now, as before."""
        reached = sorted(self.reached)
        if len(self.reached) != self._bt_done:
            for a in self.reached:
                if a in self._bt_seen:
                    continue
                self._bt_seen.add(a)
                ins = self._decode(a)
                if ins.mnemonic != 'movzx':
                    continue
                op = ins.operands[1]
                if op.type != X86_OP_MEM or op.mem.base == 0 and op.mem.index == 0:
                    continue
                v = op.mem.disp & 0xffffffff
                if not self.img.in_exec(v) or op.size != 1:
                    continue
                self._bt_vs.append(v)
                # the switch's bound: cmp r, N; ja default a few instructions
                # before makes the table N + 1 bytes. Without it the extent
                # guess ran one byte into Daytona's 0x8479d0 (32 c0 c3, a
                # return-false virtual right after 0x847890's table)
                p = a
                for _ in range(4 if getattr(self.img, 'is_pe32', False) else 0):   # PE32 only: XBE/ELF output stays as shipped
                    q = next((p - k for k in range(1, 16) if self.reached.get(p - k) == k), None)
                    if q is None:
                        break
                    pi = self._decode(q)
                    if pi is not None and pi.mnemonic == 'cmp' and len(pi.operands) == 2 and \
                            pi.operands[1].type == X86_OP_IMM and 0 <= pi.operands[1].imm < 4096:
                        n = pi.operands[1].imm + 1
                        self._bt_len[v] = min(self._bt_len.get(v, n), n)
                        break
                    p = q
            self._bt_done = len(self.reached)
        for v in self._bt_vs:
            k = bisect.bisect_right(reached, v)
            hi = reached[k] if k < len(reached) else self.img.sec_of(v)[1]
            hi = min(hi, v + 4096)
            # its entries are case numbers, small: the first byte that is not
            # one (padding, an opcode) ends it, so the functions that follow an
            # unreached stretch are not taken for table
            lo_s, _, data = self.img.sec_of(v)
            e = v
            if v in self._bt_len:
                hi = min(hi, v + self._bt_len[v])
            while e < hi and data[e - lo_s] < 0x80:
                e += 1
            self._add_range(v, max(e, v + 1))

    def in_table(self, a):
        # any range with lo <= a < hi: among the ranges starting at or below
        # a, the largest end decides
        ix = self._jt_index
        if ix is None or ix[0] != len(self.jt_ranges):
            rs = sorted(self.jt_ranges)
            his, m = [], -1
            for lo, hi in rs:
                m = max(m, hi)
                his.append(m)
            ix = self._jt_index = (len(self.jt_ranges), [lo for lo, hi in rs], his)
        k = bisect.bisect_right(ix[1], a) - 1
        return k >= 0 and ix[2][k] > a

    def entry_like(self, a):
        """A reached address a pointer names that may be a function's entry:
        16-byte aligned (callbacks are), or the instruction before it does
        not fall into it with live flags (a return, a jump, a call that does
        not return, padding). Crazy Taxi pushes flag words such as 0x3000c
        that land inside its water-region loop at 0x2ff30: taken as starts,
        they split the loop between test eax,eax and its je."""
        pe32 = getattr(self.img, 'is_pe32', False)
        if pe32:
            # a PE32's data is full of words such as 0x01010101 that land in
            # code: inside an instruction, or right after a call in the middle
            # of a long function (Daytona's initializer 0xfe01f0 was split at
            # 0x1010100, the last byte of a call, and 0x1010103); alignment
            # proves nothing there
            if self._in_reached(a):
                return False
        elif a % 16 == 0:
            return True
        for k in range(1, 16):
            if self.reached.get(a - k) == k:
                p = self._decode(a - k)
                ends = ('ret', 'retf', 'jmp', 'int3', 'nop', 'hlt') if pe32 else \
                    ('ret', 'retf', 'jmp', 'call', 'int3', 'nop', 'hlt')
                return p is not None and (p.mnemonic in ends or p.mnemonic.startswith('ret'))
        return True     # nothing reached ends here: after padding or data

    def plausible_start(self, a, loose=False):
        """A code address not reached so far that looks like a function: after
        padding (int3, nop), a return or a jump, or at the start of a section."""
        if a in self.reached or self.in_table(a):
            return False
        sec = self.img.sec_of(a)
        if not sec:
            return False
        lo, hi, data = sec
        # not inside a reached instruction
        if self._in_reached(a):
            return False
        head = data[a - lo:a - lo + 8]
        if len(head) == 8 and all(0x20 <= b < 0x7f for b in head):
            return False        # text
        if not self.sane(a):
            return False
        if a == lo or loose:
            return True
        if self.in_table(a - 4) or self.in_table(a - 1):   # right after a switch table
            return True
        # right after a switch table not found yet, its jump being in a function
        # nothing reached so far (VC3 0x57d70 after 0x57960's table at 0x57d2c,
        # which only 0x57d70 refers to): the two words before are labels a
        # little before it, and the word at it is not one (the table ended)
        if a % 4 == 0 and a - lo >= 8 and a + 4 <= hi:
            w0, w1, w2 = u32(data, a - lo), u32(data, a - lo - 4), u32(data, a - lo - 8)
            if all(a - 0x2000 <= w < a for w in (w1, w2)) and not self.img.in_exec(w0):
                return True
        # a 16-byte aligned address the code pushes or stores: a callback,
        # wherever it sits (after a byte table, after data); a constant that
        # only looks like one is dropped later if it decodes across real code
        if a % 16 == 0 and a in self.fn_ptrs:
            return True
        p = data[a - lo - 1]
        if p in (0xcc, 0x90, 0xc3):
            return True
        # a PE32 from GCC pads with multi-byte nops (lea esi,[esi+eiz*1+0] ...)
        if a % 16 == 0 and getattr(self.img, 'is_pe32', False) and \
                any(data[max(0, a - lo - len(f)):a - lo] == f for f in GCC_FILLERS):
            return True
        if a - lo >= 3 and data[a - lo - 3] == 0xc2:      # ret imm16
            return True
        # after an unconditional jump: jmp rel32 / rel8
        if a - lo >= 5 and data[a - lo - 5] == 0xe9:
            return True
        if a - lo >= 2 and data[a - lo - 2] == 0xeb:
            return True
        return False

    def _in_reached(self, a):
        """a lies strictly inside an instruction already reached."""
        if self._inside is not None:
            o = a - self._span_lo
            if 0 <= o < len(self._inside):
                return self._inside[o] != 0
        for k in range(1, 16):
            n = self.reached.get(a - k)
            if n and n > k:
                return True
        return False

    # sane()'s view of one address, which depends only on the bytes there:
    # what it does to the walk, packed as 1 | flow << 1 | size << 4 | target << 8
    _S_FAIL, _S_NEXT, _S_END, _S_JCC, _S_CALL = 1, 2, 3, 4, 5

    def _sane_info(self, a):
        tab = self._sinfo
        o = a - self._span_lo
        if tab is not None and 0 <= o < len(tab) and tab[o]:
            return tab[o]
        v = self._sane_info_slow(a)
        if tab is not None and 0 <= o < len(tab):
            tab[o] = v
        return v

    def _sane_info_slow(self, a):
        F = 1 | self._S_FAIL << 1
        ins = self._decode(a)
        if ins is None:
            return F
        mn = ins.mnemonic
        if mn in _BAD or mn.startswith('rep ') and mn[4:] in _BAD:
            return F
        # far pointers, a run of zero bytes (add [eax], al), and
        # inc/dec esp are data being read as code
        if any(op.type == X86_OP_MEM and op.size == 6 for op in ins.operands):
            return F
        if ins.bytes[:2] == b'\x00\x00' or ins.bytes[:1] in (b'\x44', b'\x4c'):
            return F
        if ins.operands and any(op.type == 1 and ins.reg_name(op.reg) in ('cs', 'ds', 'es', 'ss', 'gs')
                                for op in ins.operands):
            return F
        size = ins.size
        if mn == 'jmp' or mn.startswith('ret') or mn == 'int3' and not self._after_dbg(ins):
            if mn == 'jmp' and ins.operands[0].type == X86_OP_IMM:
                t = ins.operands[0].imm & 0xffffffff
                if not self.img.in_exec(t):
                    return F
            return 1 | self._S_END << 1 | size << 4
        if (mn.startswith('j') or mn == 'call') and ins.operands[0].type == X86_OP_IMM:
            t = ins.operands[0].imm & 0xffffffff
            if not self.img.in_exec(t):
                return F
            if mn != 'call':
                return 1 | self._S_JCC << 1 | size << 4 | t << 8
            return 1 | self._S_CALL << 1 | size << 4
        return 1 | self._S_NEXT << 1 | size << 4

    def sane(self, start, limit=4000):
        """Decoding from start reaches only ordinary instructions, never the
        middle of an instruction already reached."""
        seen = set()
        work = [start]
        reached = self.reached
        while work and len(seen) < limit:
            a = work.pop()
            while a not in seen:
                if a in reached:
                    break
                if self._in_reached(a):
                    if _WHY == start:
                        print('sane(0x%x): 0x%x is inside a reached instruction' % (start, a))
                    return False
                v = self._sane_info(a)
                flow = (v >> 1) & 7
                if flow == self._S_FAIL:
                    if _WHY == start:
                        print('sane(0x%x): fails at 0x%x: %s' % (start, a, self._bytes(a, 12).hex()))
                    return False
                seen.add(a)
                if flow == self._S_END:
                    break
                if flow == self._S_JCC:
                    work.append(v >> 8)
                a += (v >> 4) & 15
        return True

    def data_code_refs(self):
        refs = set()
        # data sections, and the SDK's code sections too: XPP keeps its
        # drivers' tables of entry points among its code (.text is left out:
        # its words there are switch tables, whose targets are not functions)
        secs = list(self.img.data_secs) + [(lo, data, name) for lo, hi, data, name in self.img.exec_secs
                                           if name != '.text']
        in_exec = self.img.in_exec
        for lo, data, name in secs:
            refs.update(v for v in words(data) if in_exec(v))
        return refs

    def pointer_table_refs(self):
        """A PE32's tables of function pointers in its data: runs of at least
        three aligned words that all point into code (zeros may sit between
        them), such as the C runtime's static initializer list (__xc_a ..
        __xc_z, which _initterm calls one by one) and C++ vtables. Their
        targets are functions even when nothing before them is padding (Visual
        C++ packs functions without it). Shorter runs are left alone: an SEH
        scope table's filter and handler pair points into the middle of
        functions."""
        refs = set()
        in_exec = self.img.in_exec
        for lo, data, name in self.img.data_secs:
            w = words(data)
            run = []
            for v in list(w) + [1]:
                if v and in_exec(v):
                    run.append(v)
                elif v == 0 and run:
                    continue
                else:
                    if len(run) >= 3:
                        refs.update(run)
                    run = []
            # the C runtime's x87 dispatch descriptors (exp, fmod, atan2: a
            # name, a flag word, then four handlers) are #pragma pack(1), so
            # their pointers are unaligned; Daytona's exp handler 0xc62df1 at
            # 0x13f7d8a was missed. Only that layout counts: unaligned words
            # in general are mostly text (UTF-16 "a\0n\0" is 0x6e0061, inside
            # .text)
            for m in _TRANDISP.finditer(data):
                t = m.start()
                if t + 0x20 > len(data) or any(data[m.end():t + 0xd]):
                    continue        # the name is NUL-padded to 12 bytes
                ptrs = struct.unpack_from('<4I', data, t + 0x10)
                if all(in_exec(v) for v in ptrs):
                    refs.update(ptrs)
        return refs

    def code_data_refs(self):
        """Words in XPP (the USB stack) that lie outside reached code: its
        drivers' tables. What they point at inside XPP is taken as a function
        if it decodes sanely, whatever precedes it. (Other sections hold
        unreached code whose bytes, read as words, point anywhere.)"""
        refs = set()
        for lo, hi, data, name in self.img.exec_secs:
            if name != 'XPP':
                continue
            for off in range(0, len(data) - 3, 4):
                a = lo + off
                if any(self.reached.get(a - k, 0) > k for k in range(0, 15)) or \
                        any(a + k in self.reached for k in range(1, 4)):
                    continue
                v = struct.unpack_from('<I', data, off)[0]
                if lo <= v < hi:
                    refs.add(v)
        return refs

    def run(self, extra=()):
        """Discover, then drop guessed starts whose code decodes across the
        instructions of a function reached by calls (a constant such as
        0x40005 that happens to point into code), and discover again without
        them: their walk would otherwise split real functions mid-instruction.
        Each pass rejects at least one more guess, so this settles; a pass
        that still overlaps after the last round keeps its starts rather than
        coming back empty (a reset after the final pass once left nothing)."""
        rejected = set()
        for i in range(self.MAX_PASSES):
            if i:
                self.__init__(self.img)
                self.rejected = rejected
            self._run(extra)
            bad = self.overlapping_weak()
            self.passes = i + 1
            if not bad:
                break
            rejected = self.rejected | bad
            if os.environ.get('RECOMP_VERBOSE'):
                print(f'discovery pass {i + 1}: {len(self.starts)} starts, '
                      f'{len(bad)} overlapping guesses rejected', file=sys.stderr)
        else:
            print(f'discovery: {len(bad)} guessed starts still overlap after '
                  f'{self.MAX_PASSES} passes; keeping them', file=sys.stderr)
        return self.starts

    MAX_PASSES = 64

    def _in_data_sec(self, a):
        return any(lo <= a < lo + len(data) for lo, data, name in self.img.data_secs)

    def _bytes(self, a, n):
        sec = self.img.sec_of(a)
        if not sec:
            return b''
        lo, hi, data = sec
        return bytes(data[a - lo:a - lo + n])

    def entry_shaped(self, a):
        """Where a function starts in a Windows build: anchored, or a
        hot-patchable entry (mov edi,edi after five bytes of int3 or nop)."""
        if self.anchored(a):
            return True
        sec = self.img.sec_of(a)
        if not sec:
            return False
        lo, hi, data = sec
        o = a - lo
        return o >= 5 and data[o:o + 2] == b'\x8b\xff' and data[o - 5:o] in (b'\xcc' * 5, b'\x90' * 5)

    def anchored(self, a):
        """16-byte aligned right after int3 or nop padding or a return."""
        sec = self.img.sec_of(a)
        if not sec or a % 16:
            return False
        lo, hi, data = sec
        return a > lo and data[a - lo - 1] in (0xcc, 0x90, 0xc3)

    def padded(self, a):
        """int3 or a return right before, or at least two nops."""
        sec = self.img.sec_of(a)
        if not sec:
            return False
        lo, hi, data = sec
        o = a - lo
        if o < 1:
            return False
        p = data[o - 1]
        return p in (0xcc, 0xc3) or (p == 0x90 and o >= 2 and data[o - 2] in (0xcc, 0x90, 0xc3))

    def start_score(self, a):
        sec = self.img.sec_of(a)
        if not sec:
            return 0
        lo, hi, data = sec
        score = 1 if a % 16 == 0 else 0
        if a > lo and data[a - lo - 1] in (0xcc, 0x90, 0xc3):
            score += 2
        elif a - lo >= 3 and data[a - lo - 3] == 0xc2:
            score += 2
        elif a - lo >= 5 and data[a - lo - 5] == 0xe9 or a - lo >= 2 and data[a - lo - 2] == 0xeb:
            score += 2
        return score

    def overlapping_weak(self):
        bad = self.collided & self.weak
        for a, n in self.reached.items():
            oa = self.owner.get(a)
            for k in range(1, n):
                ob = self.owner.get(a + k)
                if ob is None:
                    continue
                if ob == oa:
                    if oa in self.weak:
                        bad.add(oa)   # its own code decodes across itself: not code
                    continue
                wa, wb = oa in self.weak, ob in self.weak
                if oa == ob:
                    continue
                if wa and not wb:
                    bad.add(oa)
                elif wb and not wa:
                    bad.add(ob)
                elif wa and wb:
                    # MSVC aligns functions to 16 and pads before them
                    sa, sb = self.start_score(oa), self.start_score(ob)
                    if sa != sb:
                        bad.add(ob if sa > sb else oa)
                    elif ob == a + k:
                        # a tie: the one that starts inside the other's
                        # instruction is the guess that is not code
                        bad.add(ob)
        return bad

    def _run(self, extra=()):
        pending = {self.img.entry} | set(extra)
        drefs = self.data_code_refs()
        trefs = self.pointer_table_refs() if getattr(self.img, 'is_pe32', False) else set()
        # a table entry that also sits where a function would (16-aligned, after
        # padding or a return) is taken as certain, like a call target: a guess
        # elsewhere that decodes across it must not get it rejected (Daytona's
        # static initializer 0xfe01f0, a 130 KB run of constructor calls)
        # A lone 0x90 before it is not enough here: it is often the last byte
        # of an instruction (Daytona 2017: a data word 0x6015d0 that equals the
        # middle of movq [ecx + 0x24b390], xmm0 at 0x6015cb, taken as certain,
        # split a function and its loop became recursion until the stack ran out)
        pending |= {a for a in trefs if self.anchored(a) and self.padded(a) and self.sane(a)}
        # a PE32 with relocations (a DLL): every relocated word is an address.
        # One that lands where a function would start is certain; one kept in
        # data is a loose start like a table entry. Targets of words in code
        # that are not function-shaped are left alone: switch tables and
        # labels. (d3dcompiler_46.dll keeps its vtables in .text: without this
        # only 29% of its code was reached.)
        rrefs = set()
        self._reloc_code_targets = set()
        if getattr(self.img, 'reloc_sites', None):
            in_data = self._in_data_sec
            for site, t in self.img.reloc_code_refs():
                if self.entry_shaped(t):
                    # a hot-patch prologue (mov edi,edi; push ebp; mov ebp,esp)
                    # where a function starts is taken even when sane() is not
                    # sure: d3dcompiler_46's 0x10232ec0, a virtual method with
                    # an EH3 frame after a byte table in .text, was lost so
                    if t not in self.rejected and (self.sane(t) or self._bytes(t, 5) == b'\x8b\xff\x55\x8b\xec'):
                        pending.add(t)
                elif in_data(site):
                    rrefs.add(t)
                else:
                    # a word in code: a pointer table kept in .text with zero
                    # words between entries (d3dcompiler_46's __xi_ list:
                    # 0x1025eab6 after int3 padding, unaligned), or an
                    # immediate; a start only where a function would begin
                    # (plausible_start, strict)
                    self._reloc_code_targets.add(t)
            trefs = trefs | rrefs
            # runs of three or more relocated words in code: tables kept in
            # .text (vtables), taken once nothing reached covers them
            sites = sorted(a for a, t in self.img.reloc_code_refs() if not in_data(a))
            run, code_runs = [], []
            for a in sites + [None]:
                if run and a == run[-1] + 4:
                    run.append(a)
                    continue
                if len(run) >= 3:
                    code_runs.append(run)
                run = [a]
            self._code_runs = code_runs
        pe32 = getattr(self.img, 'is_pe32', False)
        rounds = 0
        while pending:
            rounds += 1
            while pending:
                a = pending.pop()
                if a in self.starts:
                    continue
                if a in self.reached and self.owner.get(a) != a:
                    # a call into the middle of reached code: a second entry
                    # of another function; still a start for naming
                    self.starts.add(a)
                    continue
                if pe32 and self._in_reached(a):
                    continue    # inside an instruction: a guess's call, not code
                self.starts.add(a)
                for t in self.walk(a):
                    if t not in self.starts:
                        pending.add(t)
            self.byte_tables()
            # a function pointer into code already reached as part of another
            # function (a DPC routine reached by falling through): its own start,
            # if it looks like an entry (entry_like); a constant that only lands
            # on an instruction boundary must not split a function, whose flags
            # would not reach the second half
            for a in sorted(self.fn_ptrs):
                if a not in self.starts and a in self.reached and a not in self.rejected \
                        and self.entry_like(a):
                    pending.add(a)
                    self.weak.add(a)
            for a in sorted(drefs | self.imm_refs):
                if a not in self.starts and a not in self.rejected and self.plausible_start(a):
                    pending.add(a)
                    self.weak.add(a)
            for a in sorted(trefs):
                if a not in self.starts and a not in self.rejected and self.plausible_start(a, loose=True):
                    pending.add(a)
                    self.weak.add(a)
            for a in sorted(self._reloc_code_targets):
                if a not in self.starts and a not in self.rejected and self.plausible_start(a):
                    pending.add(a)
                    self.weak.add(a)
            for run in getattr(self, '_code_runs', ()):
                if any(x in self.reached or self._in_reached(x) or self._in_reached(x + 3) for x in run):
                    continue
                for x in run:
                    a = self.img.word(x)
                    if a not in self.starts and a not in self.rejected and self.plausible_start(a, loose=True):
                        pending.add(a)
                        self.weak.add(a)
            for a in sorted(self.code_data_refs()):
                if a not in self.starts and a not in self.rejected and self.plausible_start(a, loose=True):
                    pending.add(a)
                    self.weak.add(a)
        self.rounds = rounds
        self.split_out_of_extent()
        return self.starts

    def split_out_of_extent(self):
        """Code a function reached by jumping away from itself (an
        incremental-link thunk, a shared tail) that lies past the next
        function's start: that code is a function of its own, entered by a
        tail jump."""
        while True:
            st = sorted(self.starts)
            new = set()
            prev_out = False
            for a in sorted(self.reached):
                o = self.owner.get(a)
                k = bisect.bisect_right(st, a) - 1
                inside = k >= 0 and st[k] == o
                if not inside and o is not None and not prev_out:
                    new.add(a)
                prev_out = not inside
            new -= self.starts
            if not new:
                return
            self.starts |= new
