"""Xbox executables (XBE) for the recompiler.

An XBE is the Xbox's executable: a header, a table of sections loaded at
fixed addresses, and a thunk table the loader fills with kernel addresses by
ordinal. A retail or arcade game has no symbols, so its functions are found
by following calls from the entry point and the code addresses its data and
instructions hold (vtables, callbacks, thread routines); see find_starts().
"""
import bisect
import collections
import struct

from capstone import Cs, CS_ARCH_X86, CS_MODE_32
from capstone.x86 import X86_OP_IMM, X86_OP_MEM

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


def _is_term(mnem):
    return mnem in ('ret', 'retf', 'jmp', 'int3', 'hlt', 'ud2', 'iretd')


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
        self.imm_refs = set()   # code addresses in instruction immediates and displacements
        self.fn_ptrs = set()    # code addresses pushed or moved as immediates: function pointers
        self.weak = set()       # starts guessed from immediates and data, not reached by a call
        self.rejected = set()   # guessed starts that decode across another function's instructions

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
                ins = self._decode(a)
                if ins is None:
                    break
                self.reached[a] = ins.size
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
        while len(out) < 4096:
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
            self.jt_ranges.append((lo, a))
        return out

    def byte_tables(self):
        """movzx r, byte ptr [r + disp] with disp in code: index tables (MSVC
        switches with many cases mapping to few targets). Their extent runs to
        the next reached instruction or the first byte too big to be a case."""
        reached = sorted(self.reached)
        for a in list(self.reached):
            ins = self._decode(a)
            if ins.mnemonic != 'movzx':
                continue
            op = ins.operands[1]
            if op.type != X86_OP_MEM or op.mem.base == 0 and op.mem.index == 0:
                continue
            v = op.mem.disp & 0xffffffff
            if not self.img.in_exec(v) or op.size != 1:
                continue
            k = bisect.bisect_right(reached, v)
            hi = reached[k] if k < len(reached) else self.img.sec_of(v)[1]
            hi = min(hi, v + 4096)
            # its entries are case numbers, small: the first byte that is not
            # one (padding, an opcode) ends it, so the functions that follow an
            # unreached stretch are not taken for table
            lo_s, _, data = self.img.sec_of(v)
            e = v
            while e < hi and data[e - lo_s] < 0x80:
                e += 1
            self.jt_ranges.append((v, max(e, v + 1)))

    def in_table(self, a):
        for lo, hi in self.jt_ranges:
            if lo <= a < hi:
                return True
        return False

    def entry_like(self, a):
        """A reached address a pointer names that may be a function's entry:
        16-byte aligned (callbacks are), or the instruction before it does
        not fall into it with live flags (a return, a jump, a call that does
        not return, padding). Crazy Taxi pushes flag words such as 0x3000c
        that land inside its water-region loop at 0x2ff30: taken as starts,
        they split the loop between test eax,eax and its je."""
        if a % 16 == 0:
            return True
        for k in range(1, 16):
            if self.reached.get(a - k) == k:
                p = self._decode(a - k)
                return p is not None and (p.mnemonic in ('ret', 'retf', 'jmp', 'call', 'int3', 'nop', 'hlt')
                                          or p.mnemonic.startswith('ret'))
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
        for k in range(1, 16):
            n = self.reached.get(a - k)
            if n and n > k:
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
        if a - lo >= 3 and data[a - lo - 3] == 0xc2:      # ret imm16
            return True
        # after an unconditional jump: jmp rel32 / rel8
        if a - lo >= 5 and data[a - lo - 5] == 0xe9:
            return True
        if a - lo >= 2 and data[a - lo - 2] == 0xeb:
            return True
        return False

    def sane(self, start, limit=4000):
        """Decoding from start reaches only ordinary instructions, never the
        middle of an instruction already reached."""
        seen = set()
        work = [start]
        while work and len(seen) < limit:
            a = work.pop()
            while a not in seen:
                if a in self.reached:
                    break
                for k in range(1, 16):
                    n = self.reached.get(a - k)
                    if n and n > k:
                        return False
                ins = self._decode(a)
                if ins is None:
                    return False
                seen.add(a)
                mn = ins.mnemonic
                if mn in _BAD or mn.startswith('rep ') and mn[4:] in _BAD:
                    return False
                # far pointers, a run of zero bytes (add [eax], al), and
                # inc/dec esp are data being read as code
                if any(op.type == X86_OP_MEM and op.size == 6 for op in ins.operands):
                    return False
                if ins.bytes[:2] == b'\x00\x00' or ins.bytes[:1] in (b'\x44', b'\x4c'):
                    return False
                if ins.operands and any(op.type == 1 and ins.reg_name(op.reg) in ('cs', 'ds', 'es', 'ss', 'gs')
                                        for op in ins.operands):
                    return False
                nxt = a + ins.size
                if mn == 'jmp' or mn.startswith('ret') or mn == 'int3' and not self._after_dbg(ins):
                    if mn == 'jmp' and ins.operands[0].type == X86_OP_IMM:
                        t = ins.operands[0].imm & 0xffffffff
                        if not self.img.in_exec(t):
                            return False
                    break
                if (mn.startswith('j') or mn == 'call') and ins.operands[0].type == X86_OP_IMM:
                    t = ins.operands[0].imm & 0xffffffff
                    if not self.img.in_exec(t):
                        return False
                    if mn != 'call':
                        work.append(t)
                a = nxt
        return True

    def data_code_refs(self):
        refs = set()
        # data sections, and the SDK's code sections too: XPP keeps its
        # drivers' tables of entry points among its code (.text is left out:
        # its words there are switch tables, whose targets are not functions)
        secs = list(self.img.data_secs) + [(lo, data, name) for lo, hi, data, name in self.img.exec_secs
                                           if name != '.text']
        for lo, data, name in secs:
            for off in range(0, len(data) - 3, 4):
                v = struct.unpack_from('<I', data, off)[0]
                if self.img.in_exec(v):
                    refs.add(v)
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
        them: their walk would otherwise split real functions mid-instruction."""
        for _ in range(6):
            self._run(extra)
            bad = self.overlapping_weak()
            if not bad:
                break
            rejected = self.rejected | bad
            self.__init__(self.img)
            self.rejected = rejected
        return self.starts

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
        bad = set()
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
