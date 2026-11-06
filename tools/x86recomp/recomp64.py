#!/usr/bin/env python3
"""Static recompiler: x86-64 Windows PE (exe or DLL) -> portable C.

The 64-bit sibling of recomp.py. Every guest function becomes
`void <prog>_F_<rva>(CPU *c)`; registers live in C locals and are written back
to the CPU struct around calls; flags are C locals the compiler drops when
nothing reads them. Guest memory is host memory: a guest address is a host
pointer, so the runtime hands the game its own allocations. The image is
placed by the loader wherever it likes: generated code reaches it through
`IB_<prog>`, the image base, plus an RVA.

Imports (bound and delay-loaded) go through `hle_call(c, index)`, entered the
way the import would be: return address at rsp, arguments in rcx, rdx, r8, r9
and xmm0-3 and above the shadow space. Indirect calls go through
guest_dispatch().

A function with a C scope table (__try) runs inside a handler frame
(seh_frame) the runtime's dispatcher can re-enter at an __except block; a
function that calls _setjmp gets the same frame, with no scopes, so that the
runtime's longjmp can re-enter it at the instruction after the call.

Usage: recomp64.py GAME.exe OUTDIR [--name NAME] [--per-file N]
"""
import argparse
import bisect
import collections
import os
import re
import struct
import sys

from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_REG, X86_OP_IMM, X86_OP_MEM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pe import PEImage, PEError  # noqa: E402

R64 = ['rax', 'rcx', 'rdx', 'rbx', 'rsp', 'rbp', 'rsi', 'rdi',
       'r8', 'r9', 'r10', 'r11', 'r12', 'r13', 'r14', 'r15']
R32 = ['eax', 'ecx', 'edx', 'ebx', 'esp', 'ebp', 'esi', 'edi',
       'r8d', 'r9d', 'r10d', 'r11d', 'r12d', 'r13d', 'r14d', 'r15d']
R16 = ['ax', 'cx', 'dx', 'bx', 'sp', 'bp', 'si', 'di',
       'r8w', 'r9w', 'r10w', 'r11w', 'r12w', 'r13w', 'r14w', 'r15w']
R8L = ['al', 'cl', 'dl', 'bl', 'spl', 'bpl', 'sil', 'dil',
       'r8b', 'r9b', 'r10b', 'r11b', 'r12b', 'r13b', 'r14b', 'r15b']
R8H = ['ah', 'ch', 'dh', 'bh']
UT = {8: 'uint8_t', 16: 'uint16_t', 32: 'uint32_t', 64: 'uint64_t'}
ST = {8: 'int8_t', 16: 'int16_t', 32: 'int32_t', 64: 'int64_t'}
SEGS = ('cs', 'ds', 'es', 'fs', 'gs', 'ss')

COND = {
    'o': 'OF', 'no': '!OF', 'b': 'CF', 'ae': '!CF', 'e': 'ZF', 'ne': '!ZF',
    'be': '(CF|ZF)', 'a': '(!CF&&!ZF)', 's': 'SF', 'ns': '!SF', 'p': 'PF',
    'np': '!PF', 'l': '(SF!=OF)', 'ge': '(SF==OF)', 'le': '(ZF||SF!=OF)',
    'g': '(!ZF&&SF==OF)',
}
CC_ALIAS = {'c': 'b', 'nae': 'b', 'nc': 'ae', 'nb': 'ae', 'z': 'e', 'nz': 'ne',
            'na': 'be', 'nbe': 'a', 'pe': 'p', 'po': 'np', 'nge': 'l',
            'nl': 'ge', 'ng': 'le', 'nle': 'g'}

SAVE = ''.join('c->r[%d]=%s;' % (i, r) for i, r in enumerate(R64)) + 'c->df=DF;'
LOAD = ''.join('%s=c->r[%d];' % (r, i) for i, r in enumerate(R64)) + 'DF=c->df;'
DECL = 'uint64_t ' + ','.join(R64) + ';uint32_t DF;uint32_t CF=0,ZF=0,SF=0,OF=0,PF=0;'


def cc_of(suffix):
    suffix = CC_ALIAS.get(suffix, suffix)
    return COND.get(suffix)


def imm(v, bits):
    v &= (1 << bits) - 1
    return '(%s)0x%x%s' % (UT[bits], v, 'ull' if bits == 64 else 'u')


class Unsupported(Exception):
    pass


def regref(name):
    """(local, kind) for a GPR name; kind in 64/32/16/'8l'/'8h'."""
    if name in R64:
        return name, 64
    if name in R32:
        return R64[R32.index(name)], 32
    if name in R16:
        return R64[R16.index(name)], 16
    if name in R8L:
        return R64[R8L.index(name)], '8l'
    if name in R8H:
        return R64[R8H.index(name)], '8h'
    return None, None


class Acc:
    """rax/eax/ax/al as a register operand for FuncGen.wr"""

    def __init__(self, bits, reg='rax'):
        self.type = X86_OP_REG
        self.size = bits // 8
        i = R64.index(reg)
        self.reg = {8: R8L[i], 16: R16[i], 32: R32[i], 64: R64[i]}[bits]


# the C runtime's setjmp entry points (MSVC: _setjmp(buf, frame); the compiler
# emits the intrinsic names; setjmpex the same with unwinding on longjmp)
SETJMP_IMPORTS = ('_setjmp', '__intrinsic_setjmp', '_setjmpex', '__intrinsic_setjmpex')


class FuncGen:
    def __init__(self, rc, start, ranges, insns, entries):
        self.rc = rc
        self.start = start
        self.ranges = ranges
        self.insns = insns
        self.entries = entries
        self.out = []
        self.tmp = 0
        self.labels = set()
        self.ib = rc.IB
        # (scope table, prolog end) when this function has a __try: see seh_site()
        self.seh = rc.seh.get(start)
        # the return addresses of its _setjmp calls: longjmp re-enters the body there,
        # through a frame like the __try one (no scopes when the function has none)
        self.setjmp_returns = []
        for ins in insns:
            if ins.mnemonic.split(' ')[-1] == 'call' and ins.operands and self.setjmp_import(ins, ins.operands[0]):
                self.setjmp_returns.append(ins.address + ins.size)
        if self.setjmp_returns and not self.seh:
            self.seh = ([], None)

    # ---- operands -------------------------------------------------------
    def imgaddr(self, rva):
        return '(%s+0x%xull)' % (self.ib, rva)

    def addr(self, ins, op):
        m = op.mem
        parts = []
        seg = ins.reg_name(m.segment) if m.segment else None
        if seg == 'gs':
            parts.append('c->gs_base')
        elif seg == 'fs':
            raise Unsupported('fs segment')
        narrow = False
        if m.base:
            bn = ins.reg_name(m.base)
            if bn == 'rip':
                rva = (ins.address + ins.size + m.disp) & 0xffffffffffffffff
                if not parts:
                    return self.imgaddr(rva)
                parts.append(self.imgaddr(rva))
            else:
                loc, kind = regref(bn)
                if loc is None:
                    raise Unsupported('base ' + bn)
                if kind == 32:
                    narrow = True
                parts.append(loc)
        if m.index:
            idx, kind = regref(ins.reg_name(m.index))
            if idx is None:
                raise Unsupported('index')
            if kind == 32:
                narrow = True
            parts.append(idx if m.scale == 1 else '%s*%dull' % (idx, m.scale))
        if m.disp or not parts:
            d = m.disp & 0xffffffffffffffff
            if m.disp < 0:
                parts.append('-0x%xull' % (-m.disp))
            else:
                parts.append('0x%xull' % d)
        e = '+'.join(parts).replace('+-', '-')
        if narrow:
            return '(uint64_t)(uint32_t)(%s)' % e
        return '(uint64_t)(%s)' % e

    def rd(self, ins, op, bits=None):
        bits = bits or op.size * 8
        if op.type == X86_OP_REG:
            rn = op.reg if isinstance(op.reg, str) else ins.reg_name(op.reg)
            loc, kind = regref(rn)
            if loc is None:
                raise Unsupported('reg ' + rn)
            if kind == 64:
                return loc
            if kind == 32:
                return '(uint32_t)' + loc
            if kind == 16:
                return '(uint16_t)' + loc
            if kind == '8l':
                return '(uint8_t)' + loc
            return '(uint8_t)(%s>>8)' % loc
        if op.type == X86_OP_IMM:
            # an immediate that holds an image address (a DIR64 fixup covers it)
            if op.size == 8 and ins.size >= 8 and self.rc.img.has_reloc(ins.address + ins.size - 8):
                return '(uint64_t)' + self.imgaddr((op.imm - self.rc.img.image_base) & 0xffffffffffffffff)
            return imm(op.imm, bits)
        if op.type == X86_OP_MEM:
            return 'rd%d(%s)' % (bits, self.addr(ins, op))
        raise Unsupported('operand')

    def wr(self, ins, op, val, bits=None):
        bits = bits or op.size * 8
        if op.type == X86_OP_REG:
            rn = op.reg if isinstance(op.reg, str) else ins.reg_name(op.reg)
            loc, kind = regref(rn)
            if loc is None:
                raise Unsupported('reg ' + rn)
            if kind == 64:
                return '%s=(uint64_t)(%s);' % (loc, val)
            if kind == 32:
                return '%s=(uint32_t)(%s);' % (loc, val)
            if kind == 16:
                return '%s=(%s&~0xffffull)|(uint16_t)(%s);' % (loc, loc, val)
            if kind == '8l':
                return '%s=(%s&~0xffull)|(uint8_t)(%s);' % (loc, loc, val)
            return '%s=(%s&~0xff00ull)|((uint64_t)(uint8_t)(%s)<<8);' % (loc, loc, val)
        if op.type == X86_OP_MEM:
            return 'wr%d(%s,(%s)(%s));' % (bits, self.addr(ins, op), UT[bits], val)
        raise Unsupported('write operand')

    def szf(self, r, bits):
        return 'ZF=(%s)==0;SF=(uint32_t)(((%s)>>%d)&1);PF=parity8((uint32_t)(%s));' % (r, r, bits - 1, r)

    def t(self):
        self.tmp += 1
        return 't%d' % self.tmp

    def locked(self, ins):
        return ins.mnemonic.startswith('lock ') or b'\xf0' in ins.bytes[:ins.bytes.index(ins.opcode[0])]

    def ptr(self, bits, a):
        return '(%s *)(uintptr_t)(%s)' % (UT[bits], a)

    # ---- control flow helpers -------------------------------------------
    def inside(self, target):
        return target in self.inset

    # Instructions that read their first operand without writing it
    NO_WRITE = frozenset(('cmp', 'test', 'bt', 'push', 'comiss', 'comisd', 'ucomiss', 'ucomisd', 'ptest', 'nop', 'prefetcht0',
                          'prefetcht1', 'prefetcht2', 'prefetchnta', 'prefetchw', 'clflush'))

    def spin_loop(self, target):
        """Is the backward jump to target closing a loop that only reads memory: no call and
        no store between target and here? A spin-wait such as `l: mov eax,[x]; test eax,eax;
        jne l` is one. Its loads are plain C reads, which the compiler may hoist out of the
        loop (nothing in it can change memory, as far as C knows), so another thread's store
        would never be seen. A compiler barrier at the back edge makes each pass reload."""
        if target > self.cur:
            return False
        for ins in self.insns:
            if ins.address < target or ins.address > self.cur:
                continue
            mn = ins.mnemonic.split()[-1]
            if mn.startswith('call') or mn.startswith(('movs', 'stos')) and ins.mnemonic.startswith('rep'):
                return False
            ops = ins.operands
            if ops and ops[0].type == X86_OP_MEM and mn not in self.NO_WRITE and not mn.startswith(('j', 'lea', 'prefetch')):
                return False
        return True

    def goto(self, target):
        if self.inside(target):
            self.labels.add(target)
            if self.spin_loop(target):
                return '{__asm__ volatile("" ::: "memory");goto L_%x;}' % target
            return 'goto L_%x;' % target
        return self.tailcall(target)

    def callname(self, target):
        return self.rc.fn_name(target)

    def tailcall(self, target):
        name = self.callname(target)
        if name is None:
            return '{%s guest_unimpl(c,%s,"tail jump to unknown 0x%x"); return;}' % (SAVE, self.imgaddr(self.cur), target)
        return '{%s %s(c); return;}' % (SAVE, name)

    def setjmp_import(self, ins, op):
        """Does this call operand reach a setjmp import: through the IAT, or through
        a thunk (`jmp [__imp__setjmp]`, MinGW's way and MSVC's without dllimport)?"""
        imp = self.import_at(ins, op)
        if imp is None and op.type == X86_OP_IMM:
            t = op.imm & 0xffffffffffffffff
            b = self.rc.img.read(t, 6) if self.rc.img.in_exec(t) else b''
            if len(b) == 6 and b[:2] == b'\xff\x25':
                imp = self.rc.iat.get((t + 6 + struct.unpack('<i', b[2:])[0]) & 0xffffffffffffffff)
        return imp is not None and (self.rc.import_list[imp][2] or '') in SETJMP_IMPORTS

    def import_at(self, ins, op):
        """The import an indirect operand through the IAT names, or None."""
        if op.type != X86_OP_MEM or not op.mem.base or ins.reg_name(op.mem.base) != 'rip' or op.mem.index:
            return None
        rva = (ins.address + ins.size + op.mem.disp) & 0xffffffffffffffff
        return self.rc.iat.get(rva)

    # ---- translation ------------------------------------------------------
    def gen(self):
        self.inset = set(i.address for i in self.insns)
        body = []
        for ins in self.insns:
            self.cur = ins.address
            try:
                code = self.seh_site(ins) + self.one(ins)
            except Unsupported as e:
                self.rc.unsupported[ins.mnemonic + ' ' + str(e)] += 1
                code = '%s guest_unimpl(c,%s,"%s %s"); return;' % (SAVE, self.imgaddr(ins.address), ins.mnemonic, ins.op_str.replace('"', ''))
            body.append((ins.address, code))
        for ins in self.insns:
            for a in self.rc.jtables.get(ins.address, ()):
                if a in self.inset:
                    self.labels.add(a)
        for e in self.entries:
            self.labels.add(e)
        cases = list(self.entries)
        if self.seh:
            # an __except block is entered from the dispatcher, through the wrapper;
            # so is the instruction after a _setjmp call, by longjmp
            for b, e, f, t in self.seh[0]:
                if t and t in self.inset and t not in cases:
                    cases.append(t)
                    self.labels.add(t)
            for t in self.setjmp_returns:
                if t in self.inset and t not in cases:
                    cases.append(t)
                    self.labels.add(t)
        out = []
        hooks = [e for e in self.entries if e in self.rc.entry_hooks]
        if hooks:
            out.append('void hle_entry_hook(CPU *c, uint64_t rva);')
        hook = lambda e: 'hle_entry_hook(c,0x%xull);' % e if e in self.rc.entry_hooks else ''
        multi = len(self.entries) > 1 or bool(self.seh)
        fname = 'B_%x' % self.start if multi else self.rc.fn_name(self.start)
        if self.seh:
            out.append('static const seh_scope SC_%x[]={%s};' % (self.start, ','.join('{0x%xu,0x%xu,0x%xu,0x%xu}' % sc for sc in self.seh[0]) or '{0,0,0,0}'))
            out.append('static void %s(CPU *c, uint64_t entry, seh_frame *sf){' % fname)
        elif multi:
            out.append('static void %s(CPU *c, uint64_t entry){' % fname)
        else:
            out.append('void %s(CPU *c){%s' % (fname, hook(self.start)))
        out.append(DECL + LOAD)
        if multi:
            out.append('switch(entry){' + ''.join('case 0x%xull: goto L_%x;' % (e, e) for e in cases) + '}')
        ends = {ins.address: ins.address + ins.size for ins in self.insns}
        prev_end = None
        for a, code in body:
            if prev_end is not None and a != prev_end:
                # a gap between this function's pieces: nothing runs across it
                out.append('%s guest_unimpl(c,%s,"fell off a fragment"); return;' % (SAVE, self.imgaddr(prev_end)))
            if a in self.labels:
                out.append('L_%x:' % a)
            out.append(code)
            prev_end = ends[a]
        end = self.insns[-1].address + self.insns[-1].size if self.insns else self.start
        if self.callname(end):
            out.append('%s %s(c); return;' % (SAVE, self.callname(end)))
        else:
            out.append(SAVE + 'guest_unimpl(c,%s,"fell off end"); return;' % self.imgaddr(end))
        out.append('}')
        if self.seh:
            for e in self.entries:
                out.append('void %s(CPU *c){' % self.rc.fn_name(e) + hook(e) + 'seh_frame sf;sf.prev=c->seh_top;sf.scopes=SC_%x;sf.nscopes=%d;sf.base=%s;sf.ip=0;sf.frame=0;c->seh_top=&sf;'
                           'uint64_t entry=sigsetjmp(sf.jb,0)?sf.target:0x%xull;%s(c,entry,&sf);c->seh_top=sf.prev;}'
                           % (self.start, len(self.seh[0]), self.ib, e, fname))
        elif multi:
            for e in self.entries:
                out.append('void %s(CPU *c){%s%s(c,0x%xull);}' % (self.rc.fn_name(e), hook(e), fname, e))
        return '\n'.join(out)

    SEH_REGS = (3, 4, 5, 6, 7, 12, 13, 14, 15)   # rbx rsp rbp rsi rdi r12-r15

    def seh_site(self, ins):
        """In a function with a __try: before anything that can raise (a call, a
        division) record where we are and the registers this frame holds, so the
        dispatcher can find the scope and resume the __except block with them;
        at the end of the prolog record the establisher frame."""
        if not self.seh:
            return ''
        mn = ins.mnemonic.split(' ')[-1]
        code = ''
        if ins.address == self.seh[1]:
            code = 'sf->frame=rsp;'
        if mn == 'call':
            # across a call only the nonvolatile registers survive to the __except block
            code += 'sf->ip=0x%xu;' % ins.address + ''.join('sf->r[%d]=%s;' % (i, R64[i]) for i in self.SEH_REGS)
        elif mn in ('div', 'idiv', 'int3', 'ud2', 'int'):
            # a fault in this frame keeps every register, and the compiler relies on it
            code += 'sf->ip=0x%xu;' % ins.address + ''.join('sf->r[%d]=%s;' % (i, R64[i]) for i in range(16))
        return code

    def one(self, ins):
        mn = ins.mnemonic
        ops = ins.operands
        prefix = None
        if mn.startswith(('rep ', 'repe ', 'repne ', 'repz ', 'repnz ')):
            prefix, mn = mn.split(' ', 1)
            # only the string instructions repeat; elsewhere the prefix is padding
            # (AMD's "rep ret" idiom, "rep nop") and the handler takes none
            if not mn.startswith(('movs', 'stos', 'lods', 'scas', 'cmps', 'ins', 'outs')):
                prefix = None
        if mn.startswith(('bnd ', 'lock ', 'notrack ')):
            mn = mn.split(' ', 1)[1]
        b0 = ins.bytes
        k = 0
        while k < len(b0) - 1 and b0[k] in (0x66, 0x67, 0xf2, 0xf3, 0x26, 0x2e, 0x36, 0x3e, 0x64, 0x65, 0xf0) or (0x40 <= b0[k] <= 0x4f and k < len(b0) - 1):
            k += 1
        opc = b0[k]
        if 0xd8 <= opc <= 0xdf and not mn.startswith(('v',)):
            return self.rc.x87.gen(self, ins, opc, b0[k + 1])
        if mn in ('movsd', 'cmpsd') and len(ops) == 2 and ops[0].type == X86_OP_MEM and ops[1].type == X86_OP_MEM:
            mn = mn + '_str'
        if mn in ('movsq', 'stosq', 'lodsq', 'scasq', 'cmpsq'):
            pass
        h = getattr(self, 'i_' + mn.replace(' ', '_'), None)
        if h:
            return h(ins, ops) if prefix is None else h(ins, ops, prefix)
        if mn.startswith('j') and cc_of(mn[1:]):
            return 'if(%s) %s' % (cc_of(mn[1:]), self.goto(ops[0].imm & 0xffffffffffffffff))
        if mn.startswith('set') and cc_of(mn[3:]):
            return self.wr(ins, ops[0], cc_of(mn[3:]) + '?1:0', 8)
        if mn.startswith('cmov') and cc_of(mn[4:]):
            # a 32-bit cmov zero-extends its destination even when it does not move
            if ops[0].size == 4:
                return '{uint32_t v=%s;if(%s)v=(uint32_t)(%s);%s}' % (self.rd(ins, ops[0]), cc_of(mn[4:]), self.rd(ins, ops[1]), self.wr(ins, ops[0], 'v'))
            return 'if(%s){%s}' % (cc_of(mn[4:]), self.wr(ins, ops[0], self.rd(ins, ops[1])))
        sse = self.rc.sse.gen(self, ins, mn, ops)
        if sse is not None:
            return sse
        raise Unsupported('no handler')

    # ---- integer ----------------------------------------------------------
    def i_nop(self, ins, ops):
        return ';'

    i_fnop = i_nop
    i_prefetchnta = i_nop
    i_prefetcht0 = i_nop
    i_prefetcht1 = i_nop
    i_prefetcht2 = i_nop
    i_prefetchw = i_nop
    i_sfence = i_nop
    i_lfence = i_nop
    i_mfence = i_nop
    i_emms = i_nop
    i_pause = i_nop
    i_endbr64 = i_nop
    i_wait = i_nop
    i_fwait = i_nop

    def i_mov(self, ins, ops):
        if ops[0].type == X86_OP_REG and ins.reg_name(ops[0].reg) in SEGS:
            raise Unsupported('segment write')
        if ops[1].type == X86_OP_REG and ins.reg_name(ops[1].reg) in SEGS:
            raise Unsupported('segment read')
        return self.wr(ins, ops[0], self.rd(ins, ops[1], ops[0].size * 8))

    def i_movabs(self, ins, ops):
        return self.i_mov(ins, ops)

    def i_movzx(self, ins, ops):
        return self.wr(ins, ops[0], self.rd(ins, ops[1]))

    def i_movsx(self, ins, ops):
        b = ops[1].size * 8
        return self.wr(ins, ops[0], '(%s)(%s)(%s)' % (UT[ops[0].size * 8], ST[b], self.rd(ins, ops[1])))

    i_movsxd = i_movsx

    def i_lea(self, ins, ops):
        return self.wr(ins, ops[0], self.addr(ins, ops[1]))

    def i_xchg(self, ins, ops):
        b = ops[0].size * 8
        u = UT[b]
        if ops[0].type == X86_OP_MEM or ops[1].type == X86_OP_MEM:
            mem, reg = (ops[0], ops[1]) if ops[0].type == X86_OP_MEM else (ops[1], ops[0])
            # xchg with memory is always locked
            return '{%s v=%s;v=__atomic_exchange_n(%s,v,__ATOMIC_SEQ_CST);%s}' % (
                u, self.rd(ins, reg), self.ptr(b, self.addr(ins, mem)), self.wr(ins, reg, 'v'))
        t = self.t()
        return '{%s %s=%s;%s%s}' % (u, t, self.rd(ins, ops[0]), self.wr(ins, ops[0], self.rd(ins, ops[1])), self.wr(ins, ops[1], t))

    def i_xadd(self, ins, ops):
        b = ops[0].size * 8
        u = UT[b]
        flags = 'CF=r<a;OF=(uint32_t)(((~(a^b)&(a^r))>>%d)&1);' % (b - 1) + self.szf('r', b)
        if ops[0].type == X86_OP_MEM:
            return '{%s b=%s,a=__atomic_fetch_add(%s,b,__ATOMIC_SEQ_CST),r=(%s)(a+b);%s%s}' % (
                u, self.rd(ins, ops[1]), self.ptr(b, self.addr(ins, ops[0])), u, flags, self.wr(ins, ops[1], 'a'))
        return '{%s a=%s,b=%s,r=(%s)(a+b);%s%s%s}' % (
            u, self.rd(ins, ops[0]), self.rd(ins, ops[1]), u, flags, self.wr(ins, ops[1], 'a'), self.wr(ins, ops[0], 'r'))

    def i_push(self, ins, ops):
        b = ops[0].size * 8
        if ops[0].type == X86_OP_IMM:
            b = 16 if 0x66 in ins.bytes[:ins.bytes.index(ins.opcode[0])] else 64
            v = imm(ops[0].imm, b)
        else:
            v = self.rd(ins, ops[0])
            if b == 32:
                raise Unsupported('push r32')
        if b == 16:
            return '{uint16_t t=%s;rsp-=2;wr16(rsp,t);}' % v
        return '{uint64_t t=%s;rsp-=8;wr64(rsp,t);}' % v

    def i_pop(self, ins, ops):
        b = ops[0].size * 8
        if b == 16:
            return '{uint16_t t=rd16(rsp);rsp+=2;%s}' % self.wr(ins, ops[0], 't')
        return '{uint64_t t=rd64(rsp);rsp+=8;%s}' % self.wr(ins, ops[0], 't')

    def i_leave(self, ins, ops):
        return 'rsp=rbp;rbp=rd64(rsp);rsp+=8;'

    def i_ret(self, ins, ops):
        n = 8 + (ops[0].imm if ops else 0)
        return 'rsp+=%du;%s return;' % (n, SAVE)

    def i_call(self, ins, ops):
        nxt = ins.address + ins.size
        push = 'rsp-=8;wr64(rsp,%s);' % self.imgaddr(nxt)
        op = ops[0]
        if op.type == X86_OP_IMM:
            target = op.imm & 0xffffffffffffffff
            if target == nxt:
                return push
            name = self.callname(target)
            if name is None:
                return '%s%s guest_unimpl(c,%s,"call to unknown 0x%x"); return;' % (push, SAVE, self.imgaddr(ins.address), target)
            return '%s%s %s(c);%s' % (push, SAVE, name, LOAD)
        imp = self.import_at(ins, op)
        if imp is not None:
            return '%s%s hle_call(c,%d);%s' % (push, SAVE, imp, LOAD)
        t = self.rd(ins, op, 64)
        return '{uint64_t t=%s;%s%s guest_dispatch(c,t);%s}' % (t, push, SAVE, LOAD)

    def i_jmp(self, ins, ops):
        op = ops[0]
        if op.type == X86_OP_IMM:
            return self.goto(op.imm & 0xffffffffffffffff)
        imp = self.import_at(ins, op)
        if imp is not None:
            # a tail call through the IAT: the import returns to our caller
            return '{%s hle_call(c,%d); return;}' % (SAVE, imp)
        t = self.rd(ins, op, 64)
        refs = self.rc.jtables.get(ins.address, ())
        cases = ''.join('case 0x%xull: goto L_%x;' % (a, a) for a in sorted(set(refs)) if a in self.inset)
        if cases:
            return '{uint64_t t=%s;switch(t-%s){%sdefault:%s guest_dispatch(c,t);return;}}' % (t, self.ib, cases, SAVE)
        return '{uint64_t t=%s;%s guest_dispatch(c,t);return;}' % (t, SAVE)

    def arith(self, ins, ops, kind):
        b = ops[0].size * 8
        a = self.rd(ins, ops[0])
        s = self.rd(ins, ops[1], b) if len(ops) > 1 else None
        u = UT[b]
        msb = b - 1
        lock = self.locked(ins) and ops[0].type == X86_OP_MEM and kind in ('add', 'sub', 'and', 'or', 'xor')
        if lock:
            fn = {'add': '__atomic_fetch_add', 'sub': '__atomic_fetch_sub', 'and': '__atomic_fetch_and',
                  'or': '__atomic_fetch_or', 'xor': '__atomic_fetch_xor'}[kind]
            pre = '{%s b=%s,a=%s(%s,b,__ATOMIC_SEQ_CST),r;' % (u, s, fn, self.ptr(b, self.addr(ins, ops[0])))
        else:
            pre = '{%s a=%s,b=%s,r;' % (u, a, s) if s else None
        if kind == 'add':
            body = pre + 'r=(%s)(a+b);CF=r<a;OF=(uint32_t)(((~(a^b)&(a^r))>>%d)&1);' % (u, msb) + self.szf('r', b)
        elif kind in ('sub', 'cmp'):
            body = pre + 'r=(%s)(a-b);CF=a<b;OF=(uint32_t)((((a^b)&(a^r))>>%d)&1);' % (u, msb) + self.szf('r', b)
        elif kind == 'adc':
            body = pre + 'uint32_t ci=CF;r=(%s)(a+b+ci);CF=ci?r<=a:r<a;OF=(uint32_t)(((~(a^b)&(a^r))>>%d)&1);' % (u, msb) + self.szf('r', b)
        elif kind == 'sbb':
            body = pre + 'uint32_t ci=CF;r=(%s)(a-b-ci);CF=ci?a<=b:a<b;OF=(uint32_t)((((a^b)&(a^r))>>%d)&1);' % (u, msb) + self.szf('r', b)
        elif kind in ('and', 'test'):
            body = pre + 'r=a&b;CF=0;OF=0;' + self.szf('r', b)
        elif kind == 'or':
            body = pre + 'r=a|b;CF=0;OF=0;' + self.szf('r', b)
        elif kind == 'xor':
            if ops[0].type == X86_OP_REG and ops[1].type == X86_OP_REG and ops[0].reg == ops[1].reg:
                return self.wr(ins, ops[0], '0') + 'CF=0;OF=0;ZF=1;SF=0;PF=1;'
            body = pre + 'r=a^b;CF=0;OF=0;' + self.szf('r', b)
        else:
            raise Unsupported(kind)
        if kind not in ('cmp', 'test') and not lock:
            body += self.wr(ins, ops[0], 'r')
        return body + '}'

    def i_add(self, ins, ops): return self.arith(ins, ops, 'add')
    def i_sub(self, ins, ops): return self.arith(ins, ops, 'sub')
    def i_cmp(self, ins, ops): return self.arith(ins, ops, 'cmp')
    def i_adc(self, ins, ops): return self.arith(ins, ops, 'adc')
    def i_sbb(self, ins, ops): return self.arith(ins, ops, 'sbb')
    def i_and(self, ins, ops): return self.arith(ins, ops, 'and')
    def i_or(self, ins, ops): return self.arith(ins, ops, 'or')
    def i_xor(self, ins, ops): return self.arith(ins, ops, 'xor')
    def i_test(self, ins, ops): return self.arith(ins, ops, 'test')

    def incdec(self, ins, ops, sign):
        b = ops[0].size * 8
        u = UT[b]
        if self.locked(ins) and ops[0].type == X86_OP_MEM:
            fn = '__atomic_fetch_add' if sign == '+' else '__atomic_fetch_sub'
            pre = '{%s a=%s(%s,1,__ATOMIC_SEQ_CST),r=(%s)(a%s1);' % (u, fn, self.ptr(b, self.addr(ins, ops[0])), u, sign)
            post = ''
        else:
            pre = '{%s a=%s,r=(%s)(a%s1);' % (u, self.rd(ins, ops[0]), u, sign)
            post = self.wr(ins, ops[0], 'r')
        of = 'OF=r==(%s)((%s)1<<%d);' % (u, u, b - 1) if sign == '+' else 'OF=a==(%s)((%s)1<<%d);' % (u, u, b - 1)
        return pre + of + self.szf('r', b) + post + '}'

    def i_inc(self, ins, ops): return self.incdec(ins, ops, '+')
    def i_dec(self, ins, ops): return self.incdec(ins, ops, '-')

    def i_neg(self, ins, ops):
        b = ops[0].size * 8
        u = UT[b]
        return '{%s a=%s,r=(%s)(0-a);CF=a!=0;OF=a==(%s)((%s)1<<%d);%s%s}' % (u, self.rd(ins, ops[0]), u, u, u, b - 1, self.szf('r', b), self.wr(ins, ops[0], 'r'))

    def i_not(self, ins, ops):
        return self.wr(ins, ops[0], '~' + self.rd(ins, ops[0]))

    def i_bswap(self, ins, ops):
        b = ops[0].size * 8
        return self.wr(ins, ops[0], '__builtin_bswap%d(%s)' % (b, self.rd(ins, ops[0])))

    def shift(self, ins, ops, kind):
        b = ops[0].size * 8
        u = UT[b]
        mask = 63 if b == 64 else 31
        if len(ops) > 1:
            if ops[1].type == X86_OP_IMM:
                cnt = '%du' % (ops[1].imm & mask)
                if ops[1].imm & mask == 0:
                    return ';'
            else:
                cnt = '(rcx&%du)' % mask
        else:
            cnt = '1u'
        a = self.rd(ins, ops[0])
        m = b - 1
        if kind == 'shl':
            core = 'r=(%s)(a<<n);CF=n<=%d?(uint32_t)((a>>(%d-n))&1):0;OF=(uint32_t)(((r>>%d)&1)^CF);' % (u, b, b, m)
        elif kind == 'shr':
            core = 'r=(%s)(a>>n);CF=n<=%d?(uint32_t)((a>>(n-1))&1):0;OF=(uint32_t)((a>>%d)&1);' % (u, b, m)
        elif kind == 'sar':
            core = 'r=(%s)((%s)a>>(n>%d?%d:n));CF=(uint32_t)(((%s)a>>(n>%d?%d:n-1))&1);OF=0;' % (u, ST[b], m, m, ST[b], m, m)
        elif kind == 'rol':
            core = 'uint32_t k=n%%%d;r=(%s)(k?(a<<k)|(a>>(%d-k)):a);CF=(uint32_t)(r&1);OF=(uint32_t)(((r>>%d)&1)^CF);' % (b, u, b, m)
            return '{%s a=%s,r;uint32_t n=%s;if(n){%s%s}}' % (u, a, cnt, core, self.wr(ins, ops[0], 'r'))
        elif kind == 'ror':
            core = 'uint32_t k=n%%%d;r=(%s)(k?(a>>k)|(a<<(%d-k)):a);CF=(uint32_t)((r>>%d)&1);OF=(uint32_t)(((r>>%d)^(r>>%d))&1);' % (b, u, b, m, m, m - 1)
            return '{%s a=%s,r;uint32_t n=%s;if(n){%s%s}}' % (u, a, cnt, core, self.wr(ins, ops[0], 'r'))
        else:
            raise Unsupported(kind)
        return '{%s a=%s,r;uint32_t n=%s;if(n){%s%s%s}}' % (u, a, cnt, core, self.szf('r', b), self.wr(ins, ops[0], 'r'))

    def i_shl(self, ins, ops): return self.shift(ins, ops, 'shl')
    def i_sal(self, ins, ops): return self.shift(ins, ops, 'shl')
    def i_shr(self, ins, ops): return self.shift(ins, ops, 'shr')
    def i_sar(self, ins, ops): return self.shift(ins, ops, 'sar')
    def i_rol(self, ins, ops): return self.shift(ins, ops, 'rol')
    def i_ror(self, ins, ops): return self.shift(ins, ops, 'ror')

    def dshift(self, ins, ops, left):
        b = ops[0].size * 8
        u = UT[b]
        mask = 63 if b == 64 else 31
        cnt = '%du' % (ops[2].imm & mask) if ops[2].type == X86_OP_IMM else '(rcx&%du)' % mask
        if left:
            core = 'r=(%s)((a<<n)|(s>>(%d-n)));CF=(uint32_t)((a>>(%d-n))&1);' % (u, b, b)
        else:
            core = 'r=(%s)((a>>n)|(s<<(%d-n)));CF=(uint32_t)((a>>(n-1))&1);' % (u, b)
        return '{%s a=%s,s=%s,r;uint32_t n=%s;if(n){%sOF=(uint32_t)(((r^a)>>%d)&1);%s%s}}' % (
            u, self.rd(ins, ops[0]), self.rd(ins, ops[1]), cnt, core, b - 1, self.szf('r', b), self.wr(ins, ops[0], 'r'))

    def i_shld(self, ins, ops): return self.dshift(ins, ops, True)
    def i_shrd(self, ins, ops): return self.dshift(ins, ops, False)

    def i_jrcxz(self, ins, ops):
        return 'if(!rcx) %s' % self.goto(ops[0].imm & 0xffffffffffffffff)

    def i_jecxz(self, ins, ops):
        return 'if(!(uint32_t)rcx) %s' % self.goto(ops[0].imm & 0xffffffffffffffff)

    def bscan(self, ins, ops, rev):
        b = ops[0].size * 8
        u = UT[b]
        if rev:
            idx = '%d-__builtin_clzll((uint64_t)v)' % 63
        else:
            idx = '__builtin_ctzll((uint64_t)v)'
        return '{%s v=%s;ZF=v==0;if(v){%s}}' % (u, self.rd(ins, ops[1]), self.wr(ins, ops[0], idx))

    def i_bsf(self, ins, ops): return self.bscan(ins, ops, False)
    def i_bsr(self, ins, ops): return self.bscan(ins, ops, True)

    def i_tzcnt(self, ins, ops):
        b = ops[0].size * 8
        return '{%s v=%s;CF=v==0;uint32_t r=v?__builtin_ctzll((uint64_t)v):%d;ZF=r==0;%s}' % (UT[b], self.rd(ins, ops[1]), b, self.wr(ins, ops[0], 'r'))

    def i_lzcnt(self, ins, ops):
        b = ops[0].size * 8
        return '{%s v=%s;CF=v==0;uint32_t r=v?__builtin_clzll((uint64_t)v)-%d:%d;ZF=r==0;%s}' % (UT[b], self.rd(ins, ops[1]), 64 - b, b, self.wr(ins, ops[0], 'r'))

    def i_popcnt(self, ins, ops):
        b = ops[0].size * 8
        return '{%s v=%s;uint32_t r=__builtin_popcountll((uint64_t)v);CF=OF=SF=PF=0;ZF=v==0;%s}' % (UT[b], self.rd(ins, ops[1]), self.wr(ins, ops[0], 'r'))

    def rotc(self, ins, ops, left):
        b = ops[0].size * 8
        u = UT[b]
        mask = 63 if b == 64 else 31
        cnt = '1u' if len(ops) == 1 else ('%du' % (ops[1].imm & mask) if ops[1].type == X86_OP_IMM else '(rcx&%du)' % mask)
        if left:
            step = 'uint32_t o=(uint32_t)((a>>%d)&1);a=(%s)((a<<1)|CF);CF=o;' % (b - 1, u)
            of = 'OF=(uint32_t)(((a>>%d)&1)^CF);' % (b - 1)
        else:
            step = 'uint32_t o=(uint32_t)(a&1);OF=(uint32_t)(((a>>%d)&1)^CF);a=(%s)((a>>1)|((%s)CF<<%d));CF=o;' % (b - 1, u, u, b - 1)
            of = ''
        return '{%s a=%s;uint32_t n=%s%%%d;for(uint32_t k=0;k<n;k++){%s}%s%s}' % (
            u, self.rd(ins, ops[0]), cnt, b + 1, step, of, self.wr(ins, ops[0], 'a'))

    def i_rcl(self, ins, ops): return self.rotc(ins, ops, True)
    def i_rcr(self, ins, ops): return self.rotc(ins, ops, False)

    def i_cmpxchg(self, ins, ops):
        b = ops[0].size * 8
        u = UT[b]
        acc = self.rd(ins, Acc(b))
        flags = 'r=(%s)(e-cur);CF=e<cur;OF=(uint32_t)((((e^cur)&(e^r))>>%d)&1);%s' % (u, b - 1, self.szf('r', b))
        if ops[0].type == X86_OP_MEM:
            return ('{%s e=%s,cur=e,nv=%s,r;__atomic_compare_exchange_n(%s,&cur,nv,0,__ATOMIC_SEQ_CST,__ATOMIC_SEQ_CST);'
                    '%s if(!ZF){%s}}' % (u, acc, self.rd(ins, ops[1]), self.ptr(b, self.addr(ins, ops[0])), flags,
                                        self.wr(ins, Acc(b), 'cur')))
        return '{%s e=%s,cur=%s,r;%s if(ZF){%s}else{%s}}' % (
            u, acc, self.rd(ins, ops[0]), flags, self.wr(ins, ops[0], self.rd(ins, ops[1])), self.wr(ins, Acc(b), 'cur'))

    def i_cmpxchg16b(self, ins, ops):
        return ('{__uint128_t e=((__uint128_t)rdx<<64)|rax,nv=((__uint128_t)rcx<<64)|rbx,cur=e;'
                'ZF=__atomic_compare_exchange_n((__uint128_t *)(uintptr_t)(%s),&cur,nv,0,__ATOMIC_SEQ_CST,__ATOMIC_SEQ_CST);'
                'if(!ZF){rax=(uint64_t)cur;rdx=(uint64_t)(cur>>64);}}' % self.addr(ins, ops[0]))

    def i_cmpxchg8b(self, ins, ops):
        return ('{uint64_t e=((uint64_t)(uint32_t)rdx<<32)|(uint32_t)rax,nv=((uint64_t)(uint32_t)rcx<<32)|(uint32_t)rbx,cur=e;'
                'ZF=__atomic_compare_exchange_n((uint64_t *)(uintptr_t)(%s),&cur,nv,0,__ATOMIC_SEQ_CST,__ATOMIC_SEQ_CST);'
                'if(!ZF){rax=(uint32_t)cur;rdx=(uint32_t)(cur>>32);}}' % self.addr(ins, ops[0]))

    def loop(self, ins, ops, cond):
        return 'rcx--;if(rcx%s) %s' % (cond, self.goto(ops[0].imm & 0xffffffffffffffff))

    def i_loop(self, ins, ops): return self.loop(ins, ops, '')
    def i_loope(self, ins, ops): return self.loop(ins, ops, '&&ZF')
    def i_loopne(self, ins, ops): return self.loop(ins, ops, '&&!ZF')

    def bitop(self, ins, ops, kind):
        b = ops[0].size * 8
        u = UT[b]
        if ops[0].type == X86_OP_MEM and ops[1].type == X86_OP_REG:
            # the bit offset may reach beyond the operand
            n = '(%s)%s' % (ST[b], self.rd(ins, ops[1], b))
            a = self.addr(ins, ops[0])
            pre = '{int64_t n=%s;uint64_t ba=%s+(uint64_t)(n>>3);uint32_t bit=(uint32_t)(n&7);uint8_t v=rd8(ba);CF=(v>>bit)&1;' % (n, a)
            if kind == 'bt':
                return pre + '}'
            if self.locked(ins):
                fn = {'bts': '__atomic_fetch_or', 'btr': '__atomic_fetch_and', 'btc': '__atomic_fetch_xor'}[kind]
                m = {'bts': '(uint8_t)(1u<<bit)', 'btr': '(uint8_t)~(1u<<bit)', 'btc': '(uint8_t)(1u<<bit)'}[kind]
                return pre + 'v=%s((uint8_t *)(uintptr_t)ba,%s,__ATOMIC_SEQ_CST);CF=(v>>bit)&1;}' % (fn, m)
            new = {'bts': 'v|(1u<<bit)', 'btr': 'v&~(1u<<bit)', 'btc': 'v^(1u<<bit)'}[kind]
            return pre + 'wr8(ba,(uint8_t)(%s));}' % new
        n = '(%s&%du)' % (self.rd(ins, ops[1], b) if ops[1].type != X86_OP_IMM else imm(ops[1].imm, 32), b - 1)
        pre = '{%s a=%s;uint32_t n=%s;CF=(uint32_t)((a>>n)&1);' % (u, self.rd(ins, ops[0]), n)
        if kind == 'bt':
            return pre + '}'
        new = {'bts': 'a|((%s)1<<n)' % u, 'btr': 'a&~((%s)1<<n)' % u, 'btc': 'a^((%s)1<<n)' % u}[kind]
        if self.locked(ins) and ops[0].type == X86_OP_MEM:
            fn = {'bts': '__atomic_fetch_or', 'btr': '__atomic_fetch_and', 'btc': '__atomic_fetch_xor'}[kind]
            m = {'bts': '((%s)1<<n)' % u, 'btr': '~((%s)1<<n)' % u, 'btc': '((%s)1<<n)' % u}[kind]
            return '{uint32_t n=%s;%s a=%s(%s,%s,__ATOMIC_SEQ_CST);CF=(uint32_t)((a>>n)&1);}' % (
                n, u, fn, self.ptr(b, self.addr(ins, ops[0])), m)
        return pre + self.wr(ins, ops[0], new) + '}'

    def i_bt(self, ins, ops): return self.bitop(ins, ops, 'bt')
    def i_bts(self, ins, ops): return self.bitop(ins, ops, 'bts')
    def i_btr(self, ins, ops): return self.bitop(ins, ops, 'btr')
    def i_btc(self, ins, ops): return self.bitop(ins, ops, 'btc')

    def i_mul(self, ins, ops):
        b = ops[0].size * 8
        s = self.rd(ins, ops[0])
        if b == 64:
            return '{__uint128_t r=(__uint128_t)rax*(__uint128_t)%s;rax=(uint64_t)r;rdx=(uint64_t)(r>>64);CF=OF=rdx!=0;}' % s
        if b == 32:
            return '{uint64_t r=(uint64_t)(uint32_t)rax*(uint64_t)%s;rax=(uint32_t)r;rdx=(uint32_t)(r>>32);CF=OF=rdx!=0;}' % s
        if b == 16:
            return '{uint32_t r=(uint32_t)(uint16_t)rax*(uint32_t)%s;%s%sCF=OF=(r>>16)!=0;}' % (
                s, self.wr(ins, Acc(16), 'r'), self.wr(ins, Acc(16, 'rdx'), 'r>>16'))
        return '{uint32_t r=(uint32_t)(uint8_t)rax*(uint32_t)%s;%sCF=OF=(r>>8)!=0;}' % (s, self.wr(ins, Acc(16), 'r'))

    def i_imul(self, ins, ops):
        if len(ops) == 1:
            b = ops[0].size * 8
            s = self.rd(ins, ops[0])
            if b == 64:
                return '{__int128 r=(__int128)(int64_t)rax*(__int128)(int64_t)%s;rax=(uint64_t)r;rdx=(uint64_t)((__uint128_t)r>>64);CF=OF=r!=(__int128)(int64_t)r;}' % s
            if b == 32:
                return '{int64_t r=(int64_t)(int32_t)rax*(int64_t)(int32_t)%s;rax=(uint32_t)r;rdx=(uint32_t)((uint64_t)r>>32);CF=OF=r!=(int64_t)(int32_t)r;}' % s
            if b == 16:
                return '{int32_t r=(int32_t)(int16_t)rax*(int32_t)(int16_t)%s;%s%sCF=OF=r!=(int32_t)(int16_t)r;}' % (
                    s, self.wr(ins, Acc(16), '(uint32_t)r'), self.wr(ins, Acc(16, 'rdx'), '(uint32_t)r>>16'))
            return '{int32_t r=(int32_t)(int8_t)rax*(int32_t)(int8_t)%s;%sCF=OF=r!=(int32_t)(int8_t)r;}' % (s, self.wr(ins, Acc(16), '(uint32_t)r'))
        b = ops[0].size * 8
        if len(ops) == 2:
            x, y = self.rd(ins, ops[0]), self.rd(ins, ops[1])
        else:
            x, y = self.rd(ins, ops[1]), self.rd(ins, ops[2], b)
        if b == 64:
            return '{__int128 r=(__int128)(int64_t)%s*(__int128)(int64_t)%s;CF=OF=r!=(__int128)(int64_t)r;%s}' % (x, y, self.wr(ins, ops[0], '(uint64_t)r'))
        if b == 32:
            return '{int64_t r=(int64_t)(int32_t)%s*(int64_t)(int32_t)%s;CF=OF=r!=(int64_t)(int32_t)r;%s}' % (x, y, self.wr(ins, ops[0], '(uint32_t)r'))
        return '{int32_t r=(int32_t)(int16_t)%s*(int32_t)(int16_t)%s;CF=OF=r!=(int32_t)(int16_t)r;%s}' % (x, y, self.wr(ins, ops[0], '(uint32_t)r'))

    def i_div(self, ins, ops):
        b = ops[0].size * 8
        s = self.rd(ins, ops[0])
        if b == 64:
            return '{__uint128_t n=((__uint128_t)rdx<<64)|rax;uint64_t d=%s;if(!d){%s guest_divide_error(c,%s);return;}rax=(uint64_t)(n/d);rdx=(uint64_t)(n%%d);}' % (s, SAVE, self.imgaddr(self.cur))
        if b == 32:
            return '{uint64_t n=((uint64_t)(uint32_t)rdx<<32)|(uint32_t)rax;uint32_t d=%s;if(!d){%s guest_divide_error(c,%s);return;}rax=(uint32_t)(n/d);rdx=(uint32_t)(n%%d);}' % (s, SAVE, self.imgaddr(self.cur))
        if b == 16:
            return '{uint32_t n=(((uint32_t)rdx&0xffff)<<16)|((uint32_t)rax&0xffff);uint32_t d=%s;%s%s}' % (
                s, self.wr(ins, Acc(16), 'n/d'), self.wr(ins, Acc(16, 'rdx'), 'n%d'))
        return '{uint32_t n=(uint32_t)rax&0xffff;uint32_t d=%s;%s}' % (s, self.wr(ins, Acc(16), '((n/d)&0xff)|(((n%d)&0xff)<<8)'))

    def i_idiv(self, ins, ops):
        b = ops[0].size * 8
        s = self.rd(ins, ops[0])
        if b == 64:
            return '{__int128 n=(__int128)(((__uint128_t)rdx<<64)|rax);int64_t d=(int64_t)%s;if(!d){%s guest_divide_error(c,%s);return;}rax=(uint64_t)(int64_t)(n/d);rdx=(uint64_t)(int64_t)(n%%d);}' % (s, SAVE, self.imgaddr(self.cur))
        if b == 32:
            return '{int64_t n=(int64_t)(((uint64_t)(uint32_t)rdx<<32)|(uint32_t)rax);int32_t d=(int32_t)%s;if(!d){%s guest_divide_error(c,%s);return;}rax=(uint32_t)(int32_t)(n/d);rdx=(uint32_t)(int32_t)(n%%d);}' % (s, SAVE, self.imgaddr(self.cur))
        if b == 16:
            return '{int32_t n=(int32_t)((((uint32_t)rdx&0xffff)<<16)|((uint32_t)rax&0xffff));int32_t d=(int16_t)%s;%s%s}' % (
                s, self.wr(ins, Acc(16), '(uint32_t)(n/d)'), self.wr(ins, Acc(16, 'rdx'), '(uint32_t)(n%d)'))
        return '{int32_t n=(int16_t)rax;int32_t d=(int8_t)%s;%s}' % (s, self.wr(ins, Acc(16), '((uint32_t)(n/d)&0xff)|(((uint32_t)(n%d)&0xff)<<8)'))

    def i_cqo(self, ins, ops): return 'rdx=(uint64_t)((int64_t)rax>>63);'
    def i_cdq(self, ins, ops): return 'rdx=(uint32_t)((int32_t)(uint32_t)rax>>31);'
    def i_cdqe(self, ins, ops): return 'rax=(uint64_t)(int64_t)(int32_t)(uint32_t)rax;'
    def i_cwde(self, ins, ops): return 'rax=(uint32_t)(int32_t)(int16_t)rax;'
    def i_cwd(self, ins, ops): return self.wr(ins, Acc(16, 'rdx'), '((int16_t)rax<0)?0xffffu:0u')
    def i_cbw(self, ins, ops): return self.wr(ins, Acc(16), '(uint16_t)(int16_t)(int8_t)rax')
    def i_cld(self, ins, ops): return 'DF=0;'
    def i_std(self, ins, ops): return 'DF=1;'
    def i_clc(self, ins, ops): return 'CF=0;'
    def i_stc(self, ins, ops): return 'CF=1;'
    def i_cmc(self, ins, ops): return 'CF^=1;'

    def i_sahf(self, ins, ops):
        return '{uint32_t h=((uint32_t)rax>>8)&0xff;CF=h&1;PF=(h>>2)&1;ZF=(h>>6)&1;SF=(h>>7)&1;}'

    def i_lahf(self, ins, ops):
        return 'rax=(rax&~0xff00ull)|((uint64_t)(CF|(PF<<2)|(ZF<<6)|(SF<<7)|2u)<<8);'

    def i_pushfq(self, ins, ops):
        return '{uint64_t f=CF|2u|(PF<<2)|(ZF<<6)|(SF<<7)|(DF<<10)|(OF<<11)|0x200u|c->eflags_id;rsp-=8;wr64(rsp,f);}'

    def i_popfq(self, ins, ops):
        return '{uint64_t f=rd64(rsp);rsp+=8;CF=f&1;PF=(f>>2)&1;ZF=(f>>6)&1;SF=(f>>7)&1;DF=(f>>10)&1;OF=(f>>11)&1;c->eflags_id=(uint32_t)(f&0x200000u);}'

    def i_cpuid(self, ins, ops):
        return '{uint32_t a=(uint32_t)rax,b,cc=(uint32_t)rcx,d;guest_cpuid(c,&a,&b,&cc,&d);rax=a;rbx=b;rcx=cc;rdx=d;}'

    def i_rdtsc(self, ins, ops):
        return '{uint64_t v=guest_rdtsc(c);rax=(uint32_t)v;rdx=(uint32_t)(v>>32);}'

    def i_rdtscp(self, ins, ops):
        return '{uint64_t v=guest_rdtsc(c);rax=(uint32_t)v;rdx=(uint32_t)(v>>32);rcx=0;}'

    def i_rdrand(self, ins, ops):
        b = ops[0].size * 8
        return '{uint64_t v=guest_rdtsc(c)*0x9e3779b97f4a7c15ull;v^=v>>29;CF=1;OF=SF=ZF=PF=0;%s}' % self.wr(ins, ops[0], '(%s)v' % UT[b])

    i_rdseed = i_rdrand

    def i_xgetbv(self, ins, ops):
        return '{rax=(uint32_t)rcx==0?3u:0u;rdx=0;}'

    def i_hlt(self, ins, ops):
        return '%s guest_unimpl(c,%s,"hlt"); return;' % (SAVE, self.imgaddr(ins.address))

    def i_int3(self, ins, ops):
        return '%s guest_unimpl(c,%s,"int3"); return;' % (SAVE, self.imgaddr(ins.address))

    i_ud2 = i_int3

    def i_int(self, ins, ops):
        return '%s guest_unimpl(c,%s,"int 0x%x"); return;' % (SAVE, self.imgaddr(ins.address), ops[0].imm & 0xff)

    def i_syscall(self, ins, ops):
        return '%s guest_unimpl(c,%s,"syscall"); return;' % (SAVE, self.imgaddr(ins.address))

    # string instructions: rdi / rsi, flat memory
    def strop(self, ins, size, prefix, kind):
        b = size * 8
        step = 'int64_t st=DF?-%d:%d;' % (size, size)
        acc = self.rd(ins, Acc(b))
        if kind == 'movs':
            one = 'wr%d(rdi,rd%d(rsi));rsi+=st;rdi+=st;' % (b, b)
        elif kind == 'stos':
            one = 'wr%d(rdi,%s);rdi+=st;' % (b, acc)
        elif kind == 'lods':
            one = self.wr(ins, Acc(b), 'rd%d(rsi)' % b) + 'rsi+=st;'
        elif kind == 'scas':
            one = '{%s a=%s,bb=rd%d(rdi),r=(%s)(a-bb);CF=a<bb;OF=(uint32_t)((((a^bb)&(a^r))>>%d)&1);%s}rdi+=st;' % (UT[b], acc, b, UT[b], b - 1, self.szf('r', b))
        elif kind == 'cmps':
            one = '{%s a=rd%d(rsi),bb=rd%d(rdi),r=(%s)(a-bb);CF=a<bb;OF=(uint32_t)((((a^bb)&(a^r))>>%d)&1);%s}rsi+=st;rdi+=st;' % (UT[b], b, b, UT[b], b - 1, self.szf('r', b))
        if prefix is None:
            return '{%s%s}' % (step, one)
        if kind in ('movs', 'stos', 'lods'):
            if kind == 'movs':
                return '{%sif(!DF&&rcx){uint64_t n=rcx*%du;memmove((void *)(uintptr_t)rdi,(void *)(uintptr_t)rsi,n);rsi+=n;rdi+=n;rcx=0;}else while(rcx){%srcx--;}}' % (step, size, one)
            if kind == 'stos' and size == 1:
                return '{%sif(!DF&&rcx){memset((void *)(uintptr_t)rdi,(int)(uint8_t)rax,rcx);rdi+=rcx;rcx=0;}else while(rcx){%srcx--;}}' % (step, one)
            return '{%swhile(rcx){%srcx--;}}' % (step, one)
        cond = 'ZF' if prefix in ('rep', 'repe', 'repz') else '!ZF'
        return '{%swhile(rcx){%srcx--;if(!(%s))break;}}' % (step, one, cond)

    def mkstr(kind, size):
        def f(self, ins, ops, prefix=None):
            return self.strop(ins, size, prefix, kind)
        return f

    i_movsb = mkstr('movs', 1)
    i_movsw = mkstr('movs', 2)
    i_movsd_str = mkstr('movs', 4)
    i_movsq = mkstr('movs', 8)
    i_stosb = mkstr('stos', 1)
    i_stosw = mkstr('stos', 2)
    i_stosd = mkstr('stos', 4)
    i_stosq = mkstr('stos', 8)
    i_lodsb = mkstr('lods', 1)
    i_lodsw = mkstr('lods', 2)
    i_lodsd = mkstr('lods', 4)
    i_lodsq = mkstr('lods', 8)
    i_scasb = mkstr('scas', 1)
    i_scasw = mkstr('scas', 2)
    i_scasd = mkstr('scas', 4)
    i_scasq = mkstr('scas', 8)
    i_cmpsb = mkstr('cmps', 1)
    i_cmpsw = mkstr('cmps', 2)
    i_cmpsd_str = mkstr('cmps', 4)
    i_cmpsq = mkstr('cmps', 8)


class Recompiler:
    Unsupported = Unsupported

    def __init__(self, img, name='game'):
        self.img = img
        self.unsupported = collections.Counter()
        from sse64 import SSEGen64
        from x87_64 import X87Gen64
        self.sse = SSEGen64()
        self.x87 = X87Gen64()
        self.md = Cs(CS_ARCH_X86, CS_MODE_64)
        self.md.detail = True
        self.md0 = Cs(CS_ARCH_X86, CS_MODE_64)
        self.entries = {}      # function start -> set of entry addresses
        self.names = {}        # entry address -> C name
        self.prog = name
        self.F = name + '_F_%x'
        self.IB = 'IB_' + name
        self.extra_starts = ()
        self.entry_hooks = set()   # function RVAs whose entry calls hle_entry_hook
        slots = img.iat_slots()
        self.iat = {}          # iat slot rva -> import index
        self.import_list = []  # (slot rva, dll, name, ordinal)
        for rva in sorted(slots):
            dll, nm, o = slots[rva]
            self.iat[rva] = len(self.import_list)
            self.import_list.append((rva, dll, nm, o))

    def fn_name(self, a):
        return self.names.get(a)

    # ---- discovery ----------------------------------------------------------
    def find_functions(self):
        """Functions from the unwind table, plus leaf functions (no unwind info)
        found as the targets of calls, tail jumps and code pointers in data."""
        img = self.img
        self.ranges = collections.defaultdict(list)   # start -> [(lo, hi)]
        self.weak = set()                              # lea-taken code addresses, see below
        pd_starts = set()
        covered = []     # sorted (lo, hi, owner)
        for b, e, u in img.pdata:
            owner = img.fragment_parent.get(b, b)
            self.ranges[owner].append((b, e))
            pd_starts.add(owner)
            covered.append((b, e, owner))
        # functions with a C scope table (__try): start -> (scopes, prolog end RVA).
        # clang puts the filter and __finally funclets inside the parent's unwind
        # range without entries of their own; they become functions here, and the
        # parent's range is cut around them
        self.seh = {}
        cuts = collections.defaultdict(set)
        for b, e, u in img.pdata:
            if b in img.fragment_parent:
                continue
            lo = min(r[0] for r in self.ranges[b])
            hi = max(r[1] for r in self.ranges[b])
            sc = img.seh_scopes(u, lo, hi)
            if not sc:
                continue
            self.seh[b] = (sc, b + img.prolog_size(u))
            for sb, se, f, t in sc:
                if f != 1 and any(lo2 <= f < hi2 for lo2, hi2 in self.ranges[b]) and f != b:
                    cuts[b].add(f)
        for owner, at in cuts.items():
            pieces = []
            for lo, hi in self.ranges[owner]:
                marks = sorted(a for a in at if lo < a < hi)
                prev = lo
                for a in marks:
                    pieces.append((prev, a, owner if prev == lo and lo == min(r[0] for r in self.ranges[owner]) or prev not in at else prev))
                    prev = a
                pieces.append((prev, hi, prev if prev in at else owner))
            self.ranges[owner] = []
            covered = [cv for cv in covered if cv[2] != owner]
            for lo, hi, own in pieces:
                self.ranges[own].append((lo, hi))
                covered.append((lo, hi, own))
                pd_starts.add(own)
        covered.sort()
        self.cov_lo = [c[0] for c in covered]
        self.covered = covered
        starts = set(pd_starts)
        starts.add(img.entry)
        for n, o, rva, fwd in img.exports:
            if fwd is None and img.in_exec(rva):
                starts.add(rva)
        if img.tls:
            starts.update(img.tls['callbacks'])
        starts.update(img.code_pointers_in_data())
        starts.update(self.extra_starts)
        for sc, _ in self.seh.values():
            starts.update(f for sb, se, f, t in sc if f != 1)      # filter and __finally funclets
        starts = set(a for a in starts if img.in_exec(a))
        # leaf functions: anything called that no unwind range covers
        self.leaf_starts = set(a for a in starts if self.owner_of(a) is None)

        def close(work):
            """Follow calls, jumps and lea from the code of `work` (and of what
            they find) until nothing new turns up; returns the starts added."""
            seen = set()
            added = set()
            work = sorted(work)
            while work:
                new = set()
                for a in work:
                    if a in seen:
                        continue
                    seen.add(a)
                    for lo, hi in self.ranges_of(a, starts):
                        for t, is_call in self.branch_targets(lo, hi):
                            if t in starts or not img.in_exec(t):
                                continue
                            o = self.owner_of(t)
                            if o is None:
                                # a call target is a function; a jump target is one
                                # unless it is this leaf's own later block
                                if not is_call and self.owner_of(a) is None and lo <= t < hi:
                                    continue
                                starts.add(t)
                                self.leaf_starts.add(t)
                                new.add(t)
                work = sorted(new)
                added |= new
            return added

        def leaf_extents():
            # a leaf's extent: up to the next function start or unwind range
            # (recomputed whenever a start appears, since one may fall inside)
            for a in self.leaf_starts:
                self.ranges.pop(a, None)
            bounds = sorted(set(starts) | set(self.cov_lo) | set(c[1] for c in covered))
            for a in starts:
                if a in self.ranges:
                    continue
                k = bisect.bisect_right(bounds, a)
                sec = img.sec_of(a)
                hi = sec.rva + len(sec.data)
                if k < len(bounds):
                    hi = min(hi, bounds[k])
                self.ranges[a] = [(a, hi)]

        close(starts)
        # code addresses only a lea takes (callbacks): a function of its own when
        # no range holds it (a gap after an unwind-table function, as with code
        # built without unwind tables); inside a range it becomes an entry of
        # that function in run(), where instruction boundaries are known. Such a
        # function's own code is then followed too (the game's boot 18: an RHI
        # thunk reached only by lea tail-jumped to a leaf nothing else named),
        # until no lea target and no branch target is new.
        # ... unless the range is a leaf's provisional extent and the address is
        # not an instruction boundary of its linear decode: a leaf's extent runs
        # to the next known start, so it swallows what follows the leaf's code
        # (its inline jump table, the int3 padding, then a getter only a lea
        # names; the game's boot 22), and the decode stops at the table's bytes
        # before it ever reaches the getter
        decoded = {}

        def boundaries(lo, hi):
            if (lo, hi) not in decoded:
                sec = img.sec_of(lo)
                decoded[(lo, hi)] = set(addr for addr, size, m, o in self.md0.disasm_lite(sec.data[lo - sec.rva:hi - sec.rva], lo))
            return decoded[(lo, hi)]
        gaps = []
        while True:
            leaf_extents()
            if os.environ.get('RECOMP64_NO_LEA_GAPS'):   # debugging switch
                break
            rlist = sorted((lo, hi, a) for a in self.ranges for lo, hi in self.ranges[a])
            rlo = [r[0] for r in rlist]
            new_gaps = []
            for t in sorted(self.weak):
                if t in starts:
                    continue
                k = bisect.bisect_right(rlo, t) - 1
                if k >= 0 and rlist[k][0] <= t < rlist[k][1]:
                    lo, hi, a = rlist[k]
                    if a not in self.leaf_starts or t in boundaries(lo, hi):
                        continue
                new_gaps.append(t)
            if not new_gaps:
                break
            for t in new_gaps:
                starts.add(t)
                self.leaf_starts.add(t)
            gaps += new_gaps
            leaf_extents()
            close(new_gaps)
        self.starts = sorted(starts)
        self.gap_starts = gaps      # for --report
        for a in self.ranges:
            self.ranges[a].sort()

    def write_report(self, path):
        """Diagnostics for the lea-taken addresses: every function that only a
        lea reaches (with the range before it and its own extent, and how the
        code at it starts) and every function with extra entries."""
        rlist = sorted((lo, hi, a) for a in self.ranges for lo, hi in self.ranges[a])
        rlo = [r[0] for r in rlist]
        with open(path, 'w') as fh:
            fh.write('# %d lea-taken addresses, %d gap functions, %d functions with extra entries\n' % (
                len(self.weak), len(self.gap_starts),
                sum(1 for es in self.entries.values() if len(es) > 1)))
            fh.write('# gap functions: rva extent-end previous-range-owner previous-range-end first-instructions\n')
            for t in sorted(self.gap_starts):
                k = bisect.bisect_right(rlo, t) - 1
                prev = ''
                while k >= 0:
                    lo, hi, own = rlist[k]
                    if lo != t:
                        prev = '%x %x' % (own, hi)
                        break
                    k -= 1
                sec = self.img.sec_of(t)
                code = sec.data[t - sec.rva:min(t - sec.rva + 32, len(sec.data))]
                ins = ['%s %s' % (m, o) for _, _, m, o in self.md0.disasm_lite(code, t)][:3]
                fh.write('gap %x %x %s | %s\n' % (t, self.ranges[t][-1][1], prev or '- -', '; '.join(ins)))
            fh.write('# extra entries: function rva, then its entries\n')
            for f in sorted(self.entries):
                es = sorted(self.entries[f] - {f})
                if es:
                    fh.write('entries %x %s\n' % (f, ' '.join('%x' % e for e in es)))

    def owner_of(self, a):
        k = bisect.bisect_right(self.cov_lo, a) - 1
        if k >= 0 and self.covered[k][0] <= a < self.covered[k][1]:
            return self.covered[k][2]
        return None

    def ranges_of(self, a, starts):
        if a in self.ranges:
            return self.ranges[a]
        # provisional leaf extent during discovery: to the next unwind range
        # or the next known start (a call target found so far)
        sec = self.img.sec_of(a)
        hi = sec.rva + len(sec.data)
        k = bisect.bisect_right(self.cov_lo, a)
        if k < len(self.cov_lo):
            hi = min(hi, self.cov_lo[k])
        nxt = min((t for t in starts if t > a), default=None)
        if nxt is not None:
            hi = min(hi, nxt)
        return [(a, hi)]

    class _Span:
        def __init__(self, lo, hi):
            self.lo, self.hi = lo, hi

        def __contains__(self, t):
            return self.lo <= t < self.hi

    def branch_targets(self, lo, hi):
        """Direct branch targets of the code in [lo, hi), decoded linearly; the
        lea-taken addresses go to self.weak. A leaf's inline jump table
        (MSVC: `lea rb,[__ImageBase]; mov r32,[rb+idx*4+tbl]; add; jmp r`, the
        table right after the switch's code) is skipped, not decoded: its RVAs
        read as instructions (`eb 6d` is a jmp rel8) and made starts out of
        the middle of the next leaf's instructions (the game's boot 23)."""
        sec = self.img.sec_of(lo)
        out = []
        span = self._Span(lo, hi)
        tables = []      # (start, end) byte ranges of inline jump tables
        lea = {}         # reg -> rva it holds
        pos = lo
        while pos < hi:
            stop = False
            for addr, size, mnem, opstr in self.md0.disasm_lite(sec.data[pos - sec.rva:hi - sec.rva], pos):
                for tb, te in tables:
                    if tb <= addr < te:
                        pos = te
                        stop = True
                        break
                if stop:
                    break
                pos = addr + size
                if mnem == 'call' or mnem == 'jmp' or (mnem[0] == 'j' and opstr.startswith('0x')) or mnem.startswith('loop'):
                    # a conditional jump out of the function is a tail call too (MSVC:
                    # `cmp eax,1; je once_init; ret` with the target a leaf in a pdata gap)
                    try:
                        out.append((int(opstr, 16), mnem == 'call'))
                    except ValueError:
                        pass
                    continue
                if mnem == 'lea':
                    m = re.match(r'(\w+), \[rip ([+-]) (0x[0-9a-f]+)\]', opstr)
                    if m:
                        lea[m.group(1)] = (addr + size + (int(m.group(3), 16) if m.group(2) == '+' else -int(m.group(3), 16))) & 0xffffffffffffffff
                    t = self.lea_target(addr, size, mnem, opstr)
                    if t is not None:
                        self.weak.add(t)
                    continue
                if mnem in ('mov', 'movsxd'):
                    m = re.match(r'\w+, dword ptr \[(\w+) \+ \w+\*4( \+ (0x[0-9a-f]+))?\]', opstr)
                    if m and m.group(1) in lea:
                        base = lea[m.group(1)]
                        tbl = base + (int(m.group(3), 16) if m.group(3) else 0)
                        if lo <= tbl < hi:
                            n = len(self.read_table(tbl, base, span))
                            if n:
                                tables.append((tbl, tbl + 4 * n))
            else:
                break   # decoded to the end (or stopped at undecodable bytes)
        return out

    def lea_target(self, addr, size, mnem, opstr):
        """The code address a rip-relative lea takes, or None: a callback (thread
        start, atexit, comparator) handed to a library, which nothing calls
        directly. It becomes an entry of the function whose range holds it when
        it is an instruction boundary there; it never starts a function of its
        own, because a wrong guess would cut a real function in two (the game's
        third boot: 5,696 such starts, and FMallocBinned2 split)."""
        if mnem != 'lea' or '[rip ' not in opstr:
            return None
        m = re.search(r'\[rip ([+-]) (0x[0-9a-f]+)\]', opstr)
        if not m:
            return None
        t = addr + size + (int(m.group(2), 16) if m.group(1) == '+' else -int(m.group(2), 16))
        return t if self.img.in_exec(t) else None

    # ---- jump tables ------------------------------------------------------------
    def find_jump_tables(self):
        """MSVC: `lea rb,[__ImageBase]; mov r32,[rb+idx*4+tbl]; add r,rb; jmp r`
        with 32-bit RVAs in the table (in .rdata). GCC: `lea rb,[rip+tbl];
        movsxd r,[rb+idx*4]; add r,rb; jmp r` with offsets relative to the
        table. Entries are read while they lead into the function."""
        self.jtables = {}
        img = self.img
        for a in self.starts:
            rngs = self.ranges[a]
            inset = set()
            insns = []
            for lo, hi in rngs:
                sec = img.sec_of(lo)
                for ins in self.md.disasm(sec.data[lo - sec.rva:hi - sec.rva], lo):
                    insns.append(ins)
            if not any(i.mnemonic == 'jmp' and i.operands and i.operands[0].type == X86_OP_REG for i in insns):
                continue
            inset = set(i.address for i in insns)
            lea = {}        # reg -> rva it holds after `lea reg,[rip+x]`
            tables = []     # (table rva, base kind) kind: 'ib' or table-relative rva
            for ins in insns:
                ops = ins.operands
                if ins.mnemonic == 'lea' and len(ops) == 2 and ops[1].type == X86_OP_MEM and ops[1].mem.base and ins.reg_name(ops[1].mem.base) == 'rip':
                    lea[ins.reg_name(ops[0].reg)] = (ins.address + ins.size + ops[1].mem.disp) & 0xffffffffffffffff
                    continue
                if ins.mnemonic in ('mov', 'movsxd') and len(ops) == 2 and ops[1].type == X86_OP_MEM and ops[1].mem.index and ops[1].mem.scale == 4 and ops[1].mem.base:
                    bn = ins.reg_name(ops[1].mem.base)
                    if bn in lea:
                        tables.append((lea[bn] + ops[1].mem.disp, lea[bn]))
            if not tables:
                continue
            targets = set()
            for tbl, base in tables:
                targets |= self.read_table(tbl, base, inset)
            if targets:
                for ins in insns:
                    if ins.mnemonic == 'jmp' and ins.operands and ins.operands[0].type == X86_OP_REG:
                        self.jtables[ins.address] = sorted(targets)

    def read_table(self, tbl, base, inset, cap=4096):
        img = self.img
        if img.sec_of(tbl) is None:
            return set()
        best = set()
        for rel in (base, 0):
            out = set()
            n = 0
            while n < cap:
                try:
                    e = img.u32(tbl + 4 * n)
                except PEError:
                    break
                t = (rel + (e - (1 << 32) if (rel and e >= 1 << 31) else e)) & 0xffffffffffffffff
                if t not in inset:
                    break
                out.add(t)
                n += 1
            if len(out) > len(best):
                best = out
        return best

    # ---- passes -------------------------------------------------------------------
    def disasm(self, a):
        img = self.img
        out = []
        for lo, hi in self.ranges[a]:
            sec = img.sec_of(lo)
            data = sec.data[lo - sec.rva:hi - sec.rva]
            pos = lo
            for ins in self.md.disasm(data, lo):
                out.append(ins)
                pos = ins.address + ins.size
            # undecodable bytes: stop this piece here
        return out

    def run(self, outdir, per_file, nfiles=None):
        self.find_functions()
        self.find_jump_tables()
        # pass 1: instruction boundaries and direct branch targets
        bounds = {}
        branches = {}
        for a in self.starts:
            addrs = []
            br = []
            for lo, hi in self.ranges[a]:
                sec = self.img.sec_of(lo)
                for addr, size, mnem, opstr in self.md0.disasm_lite(sec.data[lo - sec.rva:hi - sec.rva], lo):
                    addrs.append(addr)
                    for t in self.jtables.get(addr, ()):
                        br.append(('jmp', t))
                    if mnem == 'call' or mnem.startswith('j'):
                        try:
                            br.append((mnem, int(opstr, 16)))
                        except ValueError:
                            pass
                    else:
                        t = self.lea_target(addr, size, mnem, opstr)
                        if t is not None:
                            br.append(('lea', t))
            bounds[a] = set(addrs)
            branches[a] = br
        self.ninsns = {a: len(bounds[a]) for a in self.starts}
        owner = {}
        for a in self.starts:
            for lo, hi in self.ranges[a]:
                owner[(lo, hi)] = a
        rlist = sorted(owner)
        rlo = [r[0] for r in rlist]

        def fn_of(t):
            k = bisect.bisect_right(rlo, t) - 1
            if k >= 0 and rlist[k][0] <= t < rlist[k][1]:
                return owner[rlist[k]]
            return None
        for a in self.starts:
            self.entries[a] = {a}
        for a in self.starts:
            for mnem, t in branches[a]:
                if mnem == 'lea' and os.environ.get('RECOMP64_NO_LEA_ENTRIES'):   # debugging switch
                    continue
                f = fn_of(t)
                if f is None or f == a:
                    continue
                if t in bounds[f]:
                    self.entries[f].add(t)
        for f, es in self.entries.items():
            for e in es:
                self.names[e] = self.F % e
        os.makedirs(outdir, exist_ok=True)
        if nfiles:
            # exactly the file count the build lists: functions spread evenly over them
            per_file = max(1, -(-len(self.starts) // nfiles))
        files = []
        chunk = []
        decls = []
        for a in self.starts:
            for e in sorted(self.entries[a]):
                decls.append('void %s(CPU *c);' % (self.F % e))
        hdr = ['#include "cpu64.h"', '#include "hle64.h"', 'extern uint64_t %s;' % self.IB] + decls
        with open(os.path.join(outdir, 'gen.h'), 'w') as fh:
            fh.write('\n'.join(hdr) + '\n')
        count = 0
        for a in self.starts:
            insns = self.disasm(a)
            if not insns:
                chunk.append('void %s(CPU *c){guest_unimpl(c,%s+0x%xull,"empty function");}' % (self.F % a, self.IB, a))
                continue
            fg = FuncGen(self, a, self.ranges[a], insns, sorted(self.entries[a]))
            chunk.append(fg.gen())
            count += len(insns)
            del insns, fg
            if len(chunk) >= per_file:
                files.append(chunk)
                chunk = []
        if chunk:
            files.append(chunk)
        for k, ch in enumerate(files):
            with open(os.path.join(outdir, 'gen_%03d.c' % k), 'w') as fh:
                fh.write('#include "gen.h"\n')
                fh.write('\n'.join(ch) + '\n')
        self.write_tables(outdir)
        return count, len(files)

    def write_tables(self, outdir):
        img = self.img
        out = ['#include "gen.h"', 'uint64_t %s;' % self.IB]
        tab = []
        for a in self.starts:
            for e in sorted(self.entries[a]):
                tab.append((e, self.F % e))
        tab.sort()
        exports = [(n or '', o, rva) for n, o, rva, fwd in img.exports if fwd is None]
        tls = img.tls or {'raw_start': 0, 'raw_end': 0, 'index': 0, 'callbacks': [], 'zero_fill': 0}
        secs = [(s.name, s.rva, max(s.vsize, len(s.data)), len(s.data), s.flags) for s in img.sections]
        arrays = [
            ('const uint32_t', 'fn_rva', ['0x%xu' % a for a, _ in tab]),
            ('const guest_fn', 'fn_ptr', [n for _, n in tab]),
            ('const uint32_t', 'import_slot', ['0x%xu' % r for r, d, n, o in self.import_list] or ['0']),
            ('const char *const', 'import_dll', ['"%s"' % d for r, d, n, o in self.import_list] or ['""']),
            ('const char *const', 'import_name', ['"%s"' % (n or '') for r, d, n, o in self.import_list] or ['""']),
            ('const uint32_t', 'import_ordinal', ['%d' % (o or 0) for r, d, n, o in self.import_list] or ['0']),
            ('const char *const', 'export_name', ['"%s"' % n for n, o, rva in exports] or ['""']),
            ('const uint32_t', 'export_ordinal', ['%d' % o for n, o, rva in exports] or ['0']),
            ('const uint32_t', 'export_rva', ['0x%xu' % rva for n, o, rva in exports] or ['0']),
            ('const uint32_t', 'tls_callbacks', ['0x%xu' % a for a in tls['callbacks']] or ['0']),
            ('const char *const', 'sec_name', ['"%s"' % n for n, r, v, f, fl in secs]),
            ('const uint32_t', 'sec_rva', ['0x%xu' % r for n, r, v, f, fl in secs]),
            ('const uint32_t', 'sec_vsize', ['0x%xu' % v for n, r, v, f, fl in secs]),
            ('const uint32_t', 'sec_fsize', ['0x%xu' % f for n, r, v, f, fl in secs]),
            ('const uint32_t', 'sec_flags', ['0x%xu' % fl for n, r, v, f, fl in secs]),
        ]
        for t, n, vals in arrays:
            out.append('static %s %s[] = {%s};' % (t, n, ','.join(vals)))
        out.append('const gen_program64 gen_program_%s = {"%s", "%s", &%s, 0x%xull, 0x%xu, 0x%xu, %d, '
                   'fn_rva, fn_ptr, %d, import_slot, import_dll, import_name, import_ordinal, %d, '
                   'export_name, export_ordinal, export_rva, %d, '
                   '0x%xu, 0x%xu, 0x%xu, 0x%xu, tls_callbacks, %d, 0x%xu, '
                   'sec_name, sec_rva, sec_vsize, sec_fsize, sec_flags, %d, 0x%xull, 0x%xull};'
                   % (self.prog, self.prog, os.path.basename(img.path), self.IB, img.image_base, img.size_of_image,
                      img.entry, 1 if img.is_dll else 0,
                      len(tab), len(self.import_list), len(exports),
                      tls['raw_start'], tls['raw_end'], tls['index'], tls['zero_fill'], len(tls['callbacks']),
                      img.security_cookie, len(secs), img.stack_reserve, img.stack_commit))
        with open(os.path.join(outdir, 'gen_tables.c'), 'w') as fh:
            fh.write('\n'.join(out) + '\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pe')
    ap.add_argument('out')
    ap.add_argument('--per-file', type=int, default=400, help='functions per gen_NNN.c file (ignored with --files)')
    ap.add_argument('--name', default='game', help='program name: prefixes its C names, and its table is gen_program_<name>')
    ap.add_argument('--files', type=int, help='write exactly this many gen_NNN.c files (the build lists them up front)')
    ap.add_argument('--starts', help='file of extra function start RVAs (hex), one per line')
    ap.add_argument('--patches', help='file of "rva hexbytes" lines applied before translation')
    ap.add_argument('--entry-hooks', help='file of function RVAs (hex) whose entry calls hle_entry_hook(c, rva) first (for tracing; the runtime provides it)')
    ap.add_argument('--report', help='write the lea-taken addresses (gap functions and extra entries) to this file (or set RECOMP64_REPORT)')
    a = ap.parse_args()
    img = PEImage(a.pe)
    if a.patches:
        for line in open(a.patches):
            line = line.split('#')[0].strip()
            if line:
                where, hexb = line.split(None, 1)
                img.patch(int(where, 16), bytes.fromhex(hexb.replace(' ', '')))
    rc = Recompiler(img, a.name)
    if a.entry_hooks:
        rc.entry_hooks = {int(l.split('#')[0].split()[0], 16) for l in open(a.entry_hooks) if l.split('#')[0].strip()}
    if a.starts:
        rc.extra_starts = [int(l.split('#')[0].split()[0], 16) for l in open(a.starts) if l.split('#')[0].strip()]
    n, files = rc.run(a.out, a.per_file, a.files)
    report = a.report or os.environ.get('RECOMP64_REPORT')
    if report:
        rc.write_report(report)
    print('%d functions (%d from the unwind table, %d leaf), %d instructions, %d imports, %d files' % (
        len(rc.starts), len(rc.starts) - len(rc.leaf_starts), len(rc.leaf_starts), n, len(rc.import_list), files))
    if a.files is not None:
        if files > a.files:
            sys.exit('recomp64: %d files written but the build expects %d; raise GEN_COUNT' % (files, a.files))
        for k in range(files, a.files):
            with open(os.path.join(a.out, 'gen_%03d.c' % k), 'w') as fh:
                fh.write('/* empty */\n')
    if rc.unsupported:
        print('unsupported (%d kinds, %d sites):' % (len(rc.unsupported), sum(rc.unsupported.values())))
        for k, v in rc.unsupported.most_common(80):
            print('  %6d %s' % (v, k))


if __name__ == '__main__':
    main()
