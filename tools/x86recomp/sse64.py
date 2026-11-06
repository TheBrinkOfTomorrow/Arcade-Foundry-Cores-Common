"""SSE through SSE4.1 translation for recomp64.py (x86-64: 16 xmm registers,
64-bit integer forms, host-pointer memory). Grown from sse.py."""
from capstone.x86 import X86_OP_REG, X86_OP_IMM, X86_OP_MEM

CMP_PRED = {'eq': 0, 'lt': 1, 'le': 2, 'unord': 3, 'neq': 4, 'nlt': 5, 'nle': 6, 'ord': 7}

# packed integer ops: field, lanes per 128 bits, C expression of a and b
PINT = {
    'paddb': ('b', 16, '(uint8_t)(a+b)'),
    'paddw': ('w', 8, '(uint16_t)(a+b)'),
    'paddd': ('u', 4, 'a+b'),
    'paddq': ('q', 2, 'a+b'),
    'psubb': ('b', 16, '(uint8_t)(a-b)'),
    'psubw': ('w', 8, '(uint16_t)(a-b)'),
    'psubd': ('u', 4, 'a-b'),
    'psubq': ('q', 2, 'a-b'),
    'paddsw': ('sw', 8, 'sat16((int32_t)a+(int32_t)b)'),
    'psubsw': ('sw', 8, 'sat16((int32_t)a-(int32_t)b)'),
    'pmulhw': ('sw', 8, '(int16_t)(((int32_t)a*(int32_t)b)>>16)'),
    'pmullw': ('sw', 8, '(int16_t)((int32_t)a*(int32_t)b)'),
    'pcmpeqb': ('b', 16, 'a==b?0xffu:0u'),
    'pcmpeqw': ('w', 8, 'a==b?0xffffu:0u'),
    'pcmpeqd': ('u', 4, 'a==b?0xffffffffu:0u'),
    'pcmpgtd': ('i', 4, 'a>b?-1:0'),
    'pand': ('q', 2, 'a&b'),
    'por': ('q', 2, 'a|b'),
    'pxor': ('q', 2, 'a^b'),
    'pandn': ('q', 2, '~a&b'),
    'andps': ('u', 4, 'a&b'), 'andpd': ('u', 4, 'a&b'),
    'orps': ('u', 4, 'a|b'), 'orpd': ('u', 4, 'a|b'),
    'xorps': ('u', 4, 'a^b'), 'xorpd': ('u', 4, 'a^b'),
    'andnps': ('u', 4, '~a&b'), 'andnpd': ('u', 4, '~a&b'),
    'addps': ('f', 4, 'a+b'), 'subps': ('f', 4, 'a-b'), 'mulps': ('f', 4, 'a*b'), 'divps': ('f', 4, 'a/b'),
    'minps': ('f', 4, 'sse_minf(a,b)'), 'maxps': ('f', 4, 'sse_maxf(a,b)'),
    'addpd': ('d', 2, 'a+b'), 'subpd': ('d', 2, 'a-b'), 'mulpd': ('d', 2, 'a*b'), 'divpd': ('d', 2, 'a/b'),
    'pavgb': ('b', 16, '(uint8_t)((a+b+1)>>1)'),
    'pminsw': ('sw', 8, 'a<b?a:b'), 'pmaxsw': ('sw', 8, 'a>b?a:b'),
    'pminub': ('b', 16, 'a<b?a:b'), 'pmaxub': ('b', 16, 'a>b?a:b'),
    'pcmpgtb': ('sb', 16, 'a>b?-1:0'), 'pcmpgtw': ('sw', 8, 'a>b?-1:0'),
    'paddusb': ('b', 16, '(uint8_t)(a+b<a?255:a+b)'), 'paddusw': ('w', 8, '(uint16_t)((uint32_t)a+b>65535?65535:a+b)'),
    'psubusb': ('b', 16, '(uint8_t)(a<b?0:a-b)'), 'psubusw': ('w', 8, '(uint16_t)(a<b?0:a-b)'),
    'paddsb': ('sb', 16, '(int8_t)(a+b>127?127:a+b<-128?-128:a+b)'), 'psubsb': ('sb', 16, '(int8_t)(a-b>127?127:a-b<-128?-128:a-b)'),
    'pavgw': ('w', 8, '(uint16_t)((a+b+1)>>1)'),
    'pmulhuw': ('w', 8, '(uint16_t)(((uint32_t)a*b)>>16)'),
    'pmuludq': ('q', 2, '(uint64_t)(uint32_t)a*(uint32_t)b'),
    'pcmpeqq': ('q', 2, 'a==b?~0ull:0'), 'pcmpgtq': ('sq', 2, 'a>b?-1:0'),
    'pminsb': ('sb', 16, 'a<b?a:b'), 'pmaxsb': ('sb', 16, 'a>b?a:b'),
    'pminuw': ('w', 8, 'a<b?a:b'), 'pmaxuw': ('w', 8, 'a>b?a:b'),
    'pminsd': ('i', 4, 'a<b?a:b'), 'pmaxsd': ('i', 4, 'a>b?a:b'),
    'pminud': ('u', 4, 'a<b?a:b'), 'pmaxud': ('u', 4, 'a>b?a:b'),
    'pmulld': ('i', 4, '(int32_t)((uint32_t)a*(uint32_t)b)'),
    'phaddw': None, 'phaddd': None,
    'addsubps': None, 'addsubpd': None,
    'psignb': ('sb', 16, 'b<0?-a:b==0?0:a'), 'psignw': ('sw', 8, 'b<0?-a:b==0?0:a'), 'psignd': ('i', 4, 'b<0?-a:b==0?0:a'),
}
SCALAR = {
    'addss': ('f', 'a+b'), 'subss': ('f', 'a-b'), 'mulss': ('f', 'a*b'), 'divss': ('f', 'a/b'),
    'minss': ('f', 'sse_minf(a,b)'), 'maxss': ('f', 'sse_maxf(a,b)'), 'sqrtss': ('f', 'sqrtf(b)'),
    'rcpss': ('f', '1.0f/b'), 'rsqrtss': ('f', '1.0f/sqrtf(b)'),
    'addsd': ('d', 'a+b'), 'subsd': ('d', 'a-b'), 'mulsd': ('d', 'a*b'), 'divsd': ('d', 'a/b'),
    'minsd': ('d', 'sse_mind(a,b)'), 'maxsd': ('d', 'sse_maxd(a,b)'), 'sqrtsd': ('d', 'sqrt(b)'),
}


class SSEGen64:
    def kind(self, ins, op):
        if op.type != X86_OP_REG:
            return None, None
        n = ins.reg_name(op.reg)
        if n.startswith('xmm'):
            return 'x', int(n[3:])
        if n.startswith('mm'):
            return 'm', int(n[2:])
        return None, None

    def vec_of(self, fg, ins, op, width):
        """Temp S holding the operand: width 16 (xmm) or 8 (mmx)."""
        k, n = self.kind(ins, op)
        t = 'xmm_t' if width == 16 else 'mmx_t'
        if k == 'x':
            return 'xmm_t S=c->xmm[%d];' % n
        if k == 'm':
            return 'mmx_t S=c->mm[%d];' % n
        if op.type == X86_OP_MEM:
            a = fg.addr(ins, op)
            if op.size == 16:
                return 'xmm_t S;rd128(&S,%s);' % a
            if op.size == 8:
                return '%s S;memset(&S,0,sizeof S);memcpy(&S,(void *)(uintptr_t)(%s),8);' % (t, a)
            if op.size == 4:
                return '%s S;memset(&S,0,sizeof S);memcpy(&S,(void *)(uintptr_t)(%s),4);' % (t, a)
            if op.size == 2:
                return '%s S;memset(&S,0,sizeof S);memcpy(&S,(void *)(uintptr_t)(%s),2);' % (t, a)
        if op.type == X86_OP_REG:
            if op.size == 8:
                return '%s S;memset(&S,0,sizeof S);S.q[0]=%s;' % (t, fg.rd(ins, op)) if width == 16 else '%s S;S.q=%s;' % (t, fg.rd(ins, op))
            return '%s S;memset(&S,0,sizeof S);S.u[0]=%s;' % (t, fg.rd(ins, op))
        raise Exception('vec operand')

    def dst(self, ins, op):
        k, n = self.kind(ins, op)
        if k == 'x':
            return 'c->xmm[%d]' % n, 16
        if k == 'm':
            return 'c->mm[%d]' % n, 8
        return None, None

    def gen(self, fg, ins, mn, ops):
        # the recompiler's own exception class, whichever module object holds it
        # (recomp64 runs as a script, so importing it here would make a second copy)
        global Unsupported
        Unsupported = fg.rc.Unsupported
        h = getattr(self, 'g_' + mn, None)
        if h:
            return '{' + h(fg, ins, ops) + '}'
        if mn in PINT and PINT[mn] is not None:
            field, lanes, expr = PINT[mn]
            D, w = self.dst(ins, ops[0])
            if D is None:
                raise Unsupported('pint dst')
            if w == 8:
                lanes //= 2
                if field == 'f' or field == 'd':
                    raise Unsupported('mmx float')
                if field == 'q':
                    return '{%s{uint64_t a=%s.q,b=S.q;%s.q=%s;}}' % (self.vec_of(fg, ins, ops[1], 8), D, D, expr)
            return '{%sfor(int k=0;k<%d;k++){__typeof__(S.%s[0]) a=%s.%s[k],b=S.%s[k];%s.%s[k]=%s;}}' % (
                self.vec_of(fg, ins, ops[1], w), lanes, field, D, field, field, D, field, expr)
        if mn in SCALAR:
            field, expr = SCALAR[mn]
            D, w = self.dst(ins, ops[0])
            return '{%s{__typeof__(S.%s[0]) a=%s.%s[0],b=S.%s[0];%s.%s[0]=%s;}}' % (
                self.vec_of(fg, ins, ops[1], 16), field, D, field, field, D, field, expr)
        for suf, field in (('ss', 'f'), ('sd', 'd'), ('ps', 'f'), ('pd', 'd')):
            if mn.startswith('cmp') and mn.endswith(suf) and mn[3:-2] in CMP_PRED:
                p = CMP_PRED[mn[3:-2]]
                D, w = self.dst(ins, ops[0])
                lanes = 1 if suf[0] == 's' else (4 if field == 'f' else 2)
                ufield = 'u' if field == 'f' else 'q'
                ones = '0xffffffffu' if field == 'f' else '0xffffffffffffffffull'
                return '{%sfor(int k=0;k<%d;k++){%s.%s[k]=sse_cmp(%s.%s[k],S.%s[k],%d)?%s:0;}}' % (
                    self.vec_of(fg, ins, ops[1], 16), lanes, D, ufield, D, field, field, p, ones)
        return None

    # ---- moves --------------------------------------------------------------
    def mov128(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        if D is not None:
            return '%s%s=S;' % (self.vec_of(fg, ins, ops[1], w), D)
        S, w = self.dst(ins, ops[1])
        if w == 16:
            return 'wr128(%s,&%s);' % (fg.addr(ins, ops[0]), S)
        return 'wr64(%s,%s.q);' % (fg.addr(ins, ops[0]), S)

    g_movaps = g_movapd = g_movdqa = g_movdqu = g_movups = g_movupd = g_lddqu = mov128
    g_movntdq = g_movntps = g_movntq = mov128

    def g_movss(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        if D is not None:
            if ops[1].type == X86_OP_MEM:
                return '%s.u[0]=rd32(%s);%s.u[1]=%s.u[2]=%s.u[3]=0;' % (D, fg.addr(ins, ops[1]), D, D, D)
            S, _ = self.dst(ins, ops[1])
            return '%s.u[0]=%s.u[0];' % (D, S)
        S, _ = self.dst(ins, ops[1])
        return 'wr32(%s,%s.u[0]);' % (fg.addr(ins, ops[0]), S)

    def g_movsd(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        if D is not None:
            if ops[1].type == X86_OP_MEM:
                return '%s.q[0]=rd64(%s);%s.q[1]=0;' % (D, fg.addr(ins, ops[1]), D)
            S, _ = self.dst(ins, ops[1])
            return '%s.q[0]=%s.q[0];' % (D, S)
        S, _ = self.dst(ins, ops[1])
        return 'wr64(%s,%s.q[0]);' % (fg.addr(ins, ops[0]), S)

    def g_movq(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        if D is not None and ops[1].type == X86_OP_REG and self.kind(ins, ops[1])[0] is None:
            # movq xmm, r64
            v = fg.rd(ins, ops[1], 64)
            if w == 16:
                return '{uint64_t v=%s;memset(&%s,0,16);%s.q[0]=v;}' % (v, D, D)
            return '{uint64_t v=%s;%s.q=v;}' % (v, D)
        if D is None and ops[0].type == X86_OP_REG:
            S, sw = self.dst(ins, ops[1])
            return fg.wr(ins, ops[0], S + ('.q[0]' if sw == 16 else '.q'), 64)
        if D is not None:
            if w == 16:
                if ops[1].type == X86_OP_MEM:
                    return '%s.q[0]=rd64(%s);%s.q[1]=0;' % (D, fg.addr(ins, ops[1]), D)
                S, sw = self.dst(ins, ops[1])
                if sw == 16:
                    return '%s.q[0]=%s.q[0];%s.q[1]=0;' % (D, S, D)
                return '%s.q[0]=%s.q;%s.q[1]=0;' % (D, S, D)
            if ops[1].type == X86_OP_MEM:
                return '%s.q=rd64(%s);' % (D, fg.addr(ins, ops[1]))
            S, sw = self.dst(ins, ops[1])
            return '%s.q=%s;' % (D, S + ('.q[0]' if sw == 16 else '.q'))
        S, sw = self.dst(ins, ops[1])
        return 'wr64(%s,%s);' % (fg.addr(ins, ops[0]), S + ('.q[0]' if sw == 16 else '.q'))

    def g_movd(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        if D is not None:
            v = fg.rd(ins, ops[1], 32)
            if w == 16:
                return '{uint32_t v=%s;memset(&%s,0,16);%s.u[0]=v;}' % (v, D, D)
            return '{uint32_t v=%s;%s.q=v;}' % (v, D)
        S, sw = self.dst(ins, ops[1])
        return fg.wr(ins, ops[0], '%s.u[0]' % S, 32)

    def g_movhlps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        S, _ = self.dst(ins, ops[1])
        return '%s.q[0]=%s.q[1];' % (D, S)

    def g_movlhps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        S, _ = self.dst(ins, ops[1])
        return '%s.q[1]=%s.q[0];' % (D, S)

    def g_movhps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        if D is not None:
            return '%s.q[1]=rd64(%s);' % (D, fg.addr(ins, ops[1]))
        S, _ = self.dst(ins, ops[1])
        return 'wr64(%s,%s.q[1]);' % (fg.addr(ins, ops[0]), S)

    g_movhpd = g_movhps

    def g_movlps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        if D is not None:
            return '%s.q[0]=rd64(%s);' % (D, fg.addr(ins, ops[1]))
        S, _ = self.dst(ins, ops[1])
        return 'wr64(%s,%s.q[0]);' % (fg.addr(ins, ops[0]), S)

    g_movlpd = g_movlps

    # ---- compares and conversions ---------------------------------------------
    def comi(self, fg, ins, ops, field):
        D, _ = self.dst(ins, ops[0])
        return '%s{__typeof__(S.%s[0]) a=%s.%s[0],b=S.%s[0];if(a!=a||b!=b){ZF=PF=CF=1;}else{ZF=a==b;CF=a<b;PF=0;}OF=SF=0;}' % (
            self.vec_of(fg, ins, ops[1], 16), field, D, field, field)

    def g_comiss(self, fg, ins, ops): return self.comi(fg, ins, ops, 'f')
    def g_ucomiss(self, fg, ins, ops): return self.comi(fg, ins, ops, 'f')
    def g_comisd(self, fg, ins, ops): return self.comi(fg, ins, ops, 'd')
    def g_ucomisd(self, fg, ins, ops): return self.comi(fg, ins, ops, 'd')

    def g_cvtsi2ss(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        if ops[1].size == 8:
            return '%s.f[0]=(float)(int64_t)%s;' % (D, fg.rd(ins, ops[1], 64))
        return '%s.f[0]=(float)(int32_t)%s;' % (D, fg.rd(ins, ops[1], 32))

    def g_cvtsi2sd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        if ops[1].size == 8:
            return '%s.d[0]=(double)(int64_t)%s;' % (D, fg.rd(ins, ops[1], 64))
        return '%s.d[0]=(double)(int32_t)%s;' % (D, fg.rd(ins, ops[1], 32))

    def to_int(self, fg, ins, ops, field, trunc):
        v = self.vec_of(fg, ins, ops[1], 16)
        if ops[0].size == 8:
            f = 'sse_cvtt64(S.%s[0])' % field if trunc else 'sse_cvt64(c,S.%s[0])' % field
            return v + fg.wr(ins, ops[0], '(uint64_t)' + f, 64)
        f = 'sse_cvtt32(S.%s[0])' % field if trunc else 'sse_cvt32(c,S.%s[0])' % field
        return v + fg.wr(ins, ops[0], '(uint32_t)' + f, 32)

    def g_cvttss2si(self, fg, ins, ops): return self.to_int(fg, ins, ops, 'f', True)
    def g_cvtss2si(self, fg, ins, ops): return self.to_int(fg, ins, ops, 'f', False)
    def g_cvttsd2si(self, fg, ins, ops): return self.to_int(fg, ins, ops, 'd', True)
    def g_cvtsd2si(self, fg, ins, ops): return self.to_int(fg, ins, ops, 'd', False)

    def g_cvtss2sd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.d[0]=(double)S.f[0];' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_cvtsd2ss(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.f[0]=(float)S.d[0];' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_cvtps2pd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.d[0]=(double)S.f[0];%s.d[1]=(double)S.f[1];' % (self.vec_of(fg, ins, ops[1], 16), D, D)

    def g_cvtpd2ps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.f[0]=(float)S.d[0];%s.f[1]=(float)S.d[1];%s.q[1]=0;' % (self.vec_of(fg, ins, ops[1], 16), D, D, D)

    # SSE1's MMX conversions (the Pentium III has no SSE2)
    def g_cvtps2pi(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<2;k++)%s.i[k]=sse_cvt32(c,S.f[k]);' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_cvttps2pi(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<2;k++)%s.i[k]=sse_cvtt32(S.f[k]);' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_cvtpi2ps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<2;k++)%s.f[k]=(float)S.i[k];' % (self.vec_of(fg, ins, ops[1], 8), D)

    def g_cvtdq2ps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<4;k++)%s.f[k]=(float)S.i[k];' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_cvttps2dq(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<4;k++)%s.i[k]=sse_cvtt32(S.f[k]);' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_rcpps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<4;k++)%s.f[k]=1.0f/S.f[k];' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_rsqrtps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<4;k++)%s.f[k]=1.0f/sqrtf(S.f[k]);' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_sqrtps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<4;k++)%s.f[k]=sqrtf(S.f[k]);' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_haddps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sxmm_t A=%s;%s.f[0]=A.f[0]+A.f[1];%s.f[1]=A.f[2]+A.f[3];%s.f[2]=S.f[0]+S.f[1];%s.f[3]=S.f[2]+S.f[3];' % (
            self.vec_of(fg, ins, ops[1], 16), D, D, D, D, D)

    # ---- shuffles -----------------------------------------------------------------
    def g_shufps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        i = ops[2].imm
        return '%sxmm_t A=%s;%s.u[0]=A.u[%d];%s.u[1]=A.u[%d];%s.u[2]=S.u[%d];%s.u[3]=S.u[%d];' % (
            self.vec_of(fg, ins, ops[1], 16), D, D, i & 3, D, (i >> 2) & 3, D, (i >> 4) & 3, D, (i >> 6) & 3)

    def g_pshufd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        i = ops[2].imm
        return '%s%s.u[0]=S.u[%d];%s.u[1]=S.u[%d];%s.u[2]=S.u[%d];%s.u[3]=S.u[%d];' % (
            self.vec_of(fg, ins, ops[1], 16), D, i & 3, D, (i >> 2) & 3, D, (i >> 4) & 3, D, (i >> 6) & 3)

    def g_pshuflw(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        i = ops[2].imm
        return '%s%s=S;%s.w[0]=S.w[%d];%s.w[1]=S.w[%d];%s.w[2]=S.w[%d];%s.w[3]=S.w[%d];' % (
            self.vec_of(fg, ins, ops[1], 16), D, D, i & 3, D, (i >> 2) & 3, D, (i >> 4) & 3, D, (i >> 6) & 3)

    def g_pshufhw(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        i = ops[2].imm
        return '%s%s=S;%s.w[4]=S.w[%d];%s.w[5]=S.w[%d];%s.w[6]=S.w[%d];%s.w[7]=S.w[%d];' % (
            self.vec_of(fg, ins, ops[1], 16), D, D, 4 + (i & 3), D, 4 + ((i >> 2) & 3), D, 4 + ((i >> 4) & 3), D, 4 + ((i >> 6) & 3))

    def g_pshufw(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        i = ops[2].imm
        return '%s%s.w[0]=S.w[%d];%s.w[1]=S.w[%d];%s.w[2]=S.w[%d];%s.w[3]=S.w[%d];' % (
            self.vec_of(fg, ins, ops[1], 8), D, i & 3, D, (i >> 2) & 3, D, (i >> 4) & 3, D, (i >> 6) & 3)

    def g_unpcklpd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.q[1]=S.q[0];' % (self.vec_of(fg, ins, ops[1], 16), D)

    g_punpcklqdq = g_unpcklpd

    def g_haddpd(self, fg, ins, ops):
        # SSE3: each half is the sum of a pair (the destination's, then the source's)
        D, _ = self.dst(ins, ops[0])
        return '%s{double h=%s.d[0]+%s.d[1];%s.d[1]=S.d[0]+S.d[1];%s.d[0]=h;}' % (
            self.vec_of(fg, ins, ops[1], 16), D, D, D, D)

    def g_unpckhpd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.q[0]=%s.q[1];%s.q[1]=S.q[1];' % (self.vec_of(fg, ins, ops[1], 16), D, D, D)

    g_punpckhqdq = g_unpckhpd

    def g_unpcklps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sxmm_t A=%s;%s.u[0]=A.u[0];%s.u[1]=S.u[0];%s.u[2]=A.u[1];%s.u[3]=S.u[1];' % (self.vec_of(fg, ins, ops[1], 16), D, D, D, D, D)

    def g_unpckhps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sxmm_t A=%s;%s.u[0]=A.u[2];%s.u[1]=S.u[2];%s.u[2]=A.u[3];%s.u[3]=S.u[3];' % (self.vec_of(fg, ins, ops[1], 16), D, D, D, D, D)

    def punpckl(self, fg, ins, ops, field, n):
        D, w = self.dst(ins, ops[0])
        lanes = n if w == 16 else n // 2
        T = 'xmm_t' if w == 16 else 'mmx_t'
        return '%s%s A=%s;for(int k=0;k<%d;k++){%s.%s[2*k]=A.%s[k];%s.%s[2*k+1]=S.%s[k];}' % (
            self.vec_of(fg, ins, ops[1], w), T, D, lanes // 2, D, field, field, D, field, field)

    def g_punpcklbw(self, fg, ins, ops): return self.punpckl(fg, ins, ops, 'b', 16)
    def g_punpcklwd(self, fg, ins, ops): return self.punpckl(fg, ins, ops, 'w', 8)
    def g_punpckldq(self, fg, ins, ops): return self.punpckl(fg, ins, ops, 'u', 4)

    def punpckh(self, fg, ins, ops, field, n):
        D, w = self.dst(ins, ops[0])
        lanes = n if w == 16 else n // 2
        T = 'xmm_t' if w == 16 else 'mmx_t'
        return '%s%s A=%s;for(int k=0;k<%d;k++){%s.%s[2*k]=A.%s[%d+k];%s.%s[2*k+1]=S.%s[%d+k];}' % (
            self.vec_of(fg, ins, ops[1], w), T, D, lanes // 2, D, field, field, lanes // 2, D, field, field, lanes // 2)

    def g_punpckhbw(self, fg, ins, ops): return self.punpckh(fg, ins, ops, 'b', 16)
    def g_punpckhwd(self, fg, ins, ops): return self.punpckh(fg, ins, ops, 'w', 8)
    def g_punpckhdq(self, fg, ins, ops): return self.punpckh(fg, ins, ops, 'u', 4)

    def g_packuswb(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        n = 8 if w == 16 else 4
        T = 'xmm_t' if w == 16 else 'mmx_t'
        sat = '(uint8_t)(v<0?0:v>255?255:v)'
        return ('%s%s A=%s;for(int k=0;k<%d;k++){int v=A.sw[k];%s.b[k]=%s;v=S.sw[k];%s.b[%d+k]=%s;}' %
                (self.vec_of(fg, ins, ops[1], w), T, D, n, D, sat, D, n, sat))

    def g_packsswb(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        n = 8 if w == 16 else 4
        T = 'xmm_t' if w == 16 else 'mmx_t'
        sat = '(int8_t)(v<-128?-128:v>127?127:v)'
        return ('%s%s A=%s;for(int k=0;k<%d;k++){int v=A.sw[k];%s.sb[k]=%s;v=S.sw[k];%s.sb[%d+k]=%s;}' %
                (self.vec_of(fg, ins, ops[1], w), T, D, n, D, sat, D, n, sat))

    def g_packssdw(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        if w == 16:
            return '%sxmm_t A=%s;for(int k=0;k<4;k++){%s.sw[k]=sat16(A.i[k]);%s.sw[4+k]=sat16(S.i[k]);}' % (self.vec_of(fg, ins, ops[1], 16), D, D, D)
        return '%smmx_t A=%s;for(int k=0;k<2;k++){%s.sw[k]=sat16(A.i[k]);%s.sw[2+k]=sat16(S.i[k]);}' % (self.vec_of(fg, ins, ops[1], 8), D, D, D)

    def g_pmaddwd(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        n = 4 if w == 16 else 2
        T = 'xmm_t' if w == 16 else 'mmx_t'
        return '%s%s A=%s;for(int k=0;k<%d;k++)%s.i[k]=(int32_t)((uint32_t)((int32_t)A.sw[2*k]*S.sw[2*k])+(uint32_t)((int32_t)A.sw[2*k+1]*S.sw[2*k+1]));' % (
            self.vec_of(fg, ins, ops[1], w), T, D, n, D)

    def shiftv(self, fg, ins, ops, field, bits, kind):
        D, w = self.dst(ins, ops[0])
        lanes = (128 // bits) if w == 16 else (64 // bits)
        if ops[1].type == X86_OP_IMM:
            cnt = 'uint64_t n=%du;' % ops[1].imm
        else:
            cnt = '%suint64_t n=S.q%s;' % (self.vec_of(fg, ins, ops[1], w), '[0]' if w == 16 else '')
        if kind == 'l':
            e = 'n>=%d?0:(%s.%s[k]<<n)' % (bits, D, field)
        elif kind == 'r':
            e = 'n>=%d?0:(%s.%s[k]>>n)' % (bits, D, field)
        else:
            e = '%s.%s[k]>>(n>=%d?%d:n)' % (D, field, bits, bits - 1)
        if w == 8 and bits == 64:
            e = e.replace('.q[k]', '.q')
            return '%s%s.q=%s;' % (cnt, D, e)
        return '%sfor(int k=0;k<%d;k++)%s.%s[k]=%s;' % (cnt, lanes, D, field, e)

    def g_psllq(self, fg, ins, ops): return self.shiftv(fg, ins, ops, 'q', 64, 'l')
    def g_psrlq(self, fg, ins, ops): return self.shiftv(fg, ins, ops, 'q', 64, 'r')
    def g_pslld(self, fg, ins, ops): return self.shiftv(fg, ins, ops, 'u', 32, 'l')
    def g_psrld(self, fg, ins, ops): return self.shiftv(fg, ins, ops, 'u', 32, 'r')
    def g_psrad(self, fg, ins, ops): return self.shiftv(fg, ins, ops, 'i', 32, 'a')
    def g_psllw(self, fg, ins, ops): return self.shiftv(fg, ins, ops, 'w', 16, 'l')
    def g_psrlw(self, fg, ins, ops): return self.shiftv(fg, ins, ops, 'w', 16, 'r')
    def g_psraw(self, fg, ins, ops): return self.shiftv(fg, ins, ops, 'sw', 16, 'a')

    def g_psrldq(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        n = min(ops[1].imm, 16)
        return 'xmm_t A=%s;memset(&%s,0,16);memcpy(%s.b,A.b+%d,%d);' % (D, D, D, n, 16 - n)

    def g_pslldq(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        n = min(ops[1].imm, 16)
        return 'xmm_t A=%s;memset(&%s,0,16);memcpy(%s.b+%d,A.b,%d);' % (D, D, D, n, 16 - n)

    def g_pextrw(self, fg, ins, ops):
        S, w = self.dst(ins, ops[1])
        n = ops[2].imm & (7 if w == 16 else 3)
        return fg.wr(ins, ops[0], '(uint32_t)%s.w[%d]' % (S, n), 32)

    def g_pinsrw(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        n = ops[2].imm & (7 if w == 16 else 3)
        return '%s.w[%d]=(uint16_t)%s;' % (D, n, fg.rd(ins, ops[1], 16 if ops[1].type == X86_OP_MEM else 32))

    def g_pmovmskb(self, fg, ins, ops):
        S, w = self.dst(ins, ops[1])
        return '{uint32_t m=0;for(int k=0;k<%d;k++)m|=((%s.b[k]>>7)&1u)<<k;%s}' % (w, S, fg.wr(ins, ops[0], 'm', 32))

    def g_movmskpd(self, fg, ins, ops):
        S, _ = self.dst(ins, ops[1])
        return fg.wr(ins, ops[0], '(uint32_t)((%s.q[0]>>63)|((%s.q[1]>>63)<<1))' % (S, S), 32)

    def g_movmskps(self, fg, ins, ops):
        S, _ = self.dst(ins, ops[1])
        return fg.wr(ins, ops[0], '(uint32_t)((%s.u[0]>>31)|((%s.u[1]>>31)<<1)|((%s.u[2]>>31)<<2)|((%s.u[3]>>31)<<3))' % (S, S, S, S), 32)

    def g_stmxcsr(self, fg, ins, ops):
        return 'wr32(%s,c->mxcsr);' % fg.addr(ins, ops[0])

    def g_ldmxcsr(self, fg, ins, ops):
        return 'c->mxcsr=rd32(%s);' % fg.addr(ins, ops[0])

    def g_fxsave(self, fg, ins, ops):
        return 'memset((void *)(uintptr_t)(%s),0,512);wr16(%s,c->fcw);wr32(%s+24u,c->mxcsr);' % (fg.addr(ins, ops[0]), fg.addr(ins, ops[0]), fg.addr(ins, ops[0]))

    def g_fxrstor(self, fg, ins, ops):
        return 'c->fcw=rd16(%s);c->mxcsr=rd32(%s+24u);' % (fg.addr(ins, ops[0]), fg.addr(ins, ops[0]))

    # ---- x86-64 era additions ---------------------------------------------------
    def g_movddup(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.q[0]=S.q[0];%s.q[1]=S.q[0];' % (self.vec_of(fg, ins, ops[1], 16), D, D)

    def g_movsldup(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.u[0]=%s.u[1]=S.u[0];%s.u[2]=%s.u[3]=S.u[2];' % (self.vec_of(fg, ins, ops[1], 16), D, D, D, D)

    def g_movshdup(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.u[0]=%s.u[1]=S.u[1];%s.u[2]=%s.u[3]=S.u[3];' % (self.vec_of(fg, ins, ops[1], 16), D, D, D, D)

    def g_pshufb(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        n = w
        T = 'xmm_t' if w == 16 else 'mmx_t'
        return '%s%s A=%s;for(int k=0;k<%d;k++)%s.b[k]=(S.b[k]&0x80)?0:A.b[S.b[k]&%d];' % (self.vec_of(fg, ins, ops[1], w), T, D, n, D, n - 1)

    def g_palignr(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        n = ops[2].imm
        if w != 16:
            raise Unsupported('palignr mmx')
        return ('%s{uint8_t t[32];memcpy(t,&S,16);memcpy(t+16,&%s,16);uint8_t r[16];for(int k=0;k<16;k++)r[k]=%d+k<32?t[%d+k]:0;memcpy(&%s,r,16);}'
                % (self.vec_of(fg, ins, ops[1], 16), D, n, n, D))

    def pabs(self, fg, ins, ops, field, lanes):
        D, w = self.dst(ins, ops[0])
        if w != 16:
            lanes //= 2
        return '%sfor(int k=0;k<%d;k++)%s.%s[k]=S.%s[k]<0?-S.%s[k]:S.%s[k];' % (self.vec_of(fg, ins, ops[1], w), lanes, D, field, field, field, field)

    def g_pabsb(self, fg, ins, ops): return self.pabs(fg, ins, ops, 'sb', 16)
    def g_pabsw(self, fg, ins, ops): return self.pabs(fg, ins, ops, 'sw', 8)
    def g_pabsd(self, fg, ins, ops): return self.pabs(fg, ins, ops, 'i', 4)

    def phadd(self, fg, ins, ops, field, lanes, op):
        D, w = self.dst(ins, ops[0])
        if w != 16:
            raise Unsupported('phadd mmx')
        h = lanes // 2
        return ('%sxmm_t A=%s;for(int k=0;k<%d;k++){%s.%s[k]=A.%s[2*k]%sA.%s[2*k+1];%s.%s[%d+k]=S.%s[2*k]%sS.%s[2*k+1];}'
                % (self.vec_of(fg, ins, ops[1], 16), D, h, D, field, field, op, field, D, field, h, field, op, field))

    def g_phaddw(self, fg, ins, ops): return self.phadd(fg, ins, ops, 'w', 8, '+')
    def g_phaddd(self, fg, ins, ops): return self.phadd(fg, ins, ops, 'u', 4, '+')
    def g_phsubw(self, fg, ins, ops): return self.phadd(fg, ins, ops, 'w', 8, '-')
    def g_phsubd(self, fg, ins, ops): return self.phadd(fg, ins, ops, 'u', 4, '-')
    def g_hsubps(self, fg, ins, ops): return self.phadd(fg, ins, ops, 'f', 4, '-')

    def g_addsubps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.f[0]-=S.f[0];%s.f[1]+=S.f[1];%s.f[2]-=S.f[2];%s.f[3]+=S.f[3];' % (self.vec_of(fg, ins, ops[1], 16), D, D, D, D)

    def g_addsubpd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.d[0]-=S.d[0];%s.d[1]+=S.d[1];' % (self.vec_of(fg, ins, ops[1], 16), D, D)

    def g_pmaddubsw(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        return ('%sxmm_t A=%s;for(int k=0;k<8;k++)%s.sw[k]=sat16((int32_t)A.b[2*k]*S.sb[2*k]+(int32_t)A.b[2*k+1]*S.sb[2*k+1]);'
                % (self.vec_of(fg, ins, ops[1], 16), D, D))

    def g_pmulhrsw(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<8;k++)%s.sw[k]=(int16_t)((((int32_t)%s.sw[k]*S.sw[k])>>14)+1>>1);' % (self.vec_of(fg, ins, ops[1], 16), D, D)

    def g_ptest(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sZF=((%s.q[0]&S.q[0])|(%s.q[1]&S.q[1]))==0;CF=((~%s.q[0]&S.q[0])|(~%s.q[1]&S.q[1]))==0;OF=SF=PF=0;' % (
            self.vec_of(fg, ins, ops[1], 16), D, D, D, D)

    def g_pextrb(self, fg, ins, ops):
        S, _ = self.dst(ins, ops[1])
        return fg.wr(ins, ops[0], '(uint32_t)%s.b[%d]' % (S, ops[2].imm & 15), 32 if ops[0].type == X86_OP_REG else 8)

    def g_pextrd(self, fg, ins, ops):
        S, _ = self.dst(ins, ops[1])
        return fg.wr(ins, ops[0], '%s.u[%d]' % (S, ops[2].imm & 3), 32)

    def g_pextrq(self, fg, ins, ops):
        S, _ = self.dst(ins, ops[1])
        return fg.wr(ins, ops[0], '%s.q[%d]' % (S, ops[2].imm & 1), 64)

    def g_pinsrb(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s.b[%d]=(uint8_t)%s;' % (D, ops[2].imm & 15, fg.rd(ins, ops[1], 8 if ops[1].type == X86_OP_MEM else 32))

    def g_pinsrd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s.u[%d]=(uint32_t)%s;' % (D, ops[2].imm & 3, fg.rd(ins, ops[1], 32))

    def g_pinsrq(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s.q[%d]=(uint64_t)%s;' % (D, ops[2].imm & 1, fg.rd(ins, ops[1], 64))

    def g_extractps(self, fg, ins, ops):
        S, _ = self.dst(ins, ops[1])
        return fg.wr(ins, ops[0], '%s.u[%d]' % (S, ops[2].imm & 3), 32)

    def g_insertps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        i = ops[2].imm
        src = (i >> 6) & 3 if ops[1].type == X86_OP_REG else 0
        dst = (i >> 4) & 3
        zmask = i & 15
        v = self.vec_of(fg, ins, ops[1], 16)
        z = ''.join('%s.u[%d]=0;' % (D, k) for k in range(4) if zmask & (1 << k))
        return '%s%s.u[%d]=S.u[%d];%s' % (v, D, dst, src, z)

    def blend(self, fg, ins, ops, field, lanes):
        D, _ = self.dst(ins, ops[0])
        i = ops[2].imm
        return self.vec_of(fg, ins, ops[1], 16) + ''.join('%s.%s[%d]=S.%s[%d];' % (D, field, k, field, k) for k in range(lanes) if i & (1 << k))

    def g_blendps(self, fg, ins, ops): return self.blend(fg, ins, ops, 'u', 4)
    def g_blendpd(self, fg, ins, ops): return self.blend(fg, ins, ops, 'q', 2)
    def g_pblendw(self, fg, ins, ops): return self.blend(fg, ins, ops, 'w', 8)

    def blendv(self, fg, ins, ops, field, lanes, mfield, shift):
        D, _ = self.dst(ins, ops[0])
        # the mask is xmm0 (implicit), or the third operand when capstone lists it
        m = 'c->xmm[0]' if len(ops) < 3 else self.dst(ins, ops[2])[0]
        return '%sfor(int k=0;k<%d;k++)if(%s.%s[k]>>%d)%s.%s[k]=S.%s[k];' % (self.vec_of(fg, ins, ops[1], 16), lanes, m, mfield, shift, D, field, field)

    def g_blendvps(self, fg, ins, ops): return self.blendv(fg, ins, ops, 'u', 4, 'u', 31)
    def g_blendvpd(self, fg, ins, ops): return self.blendv(fg, ins, ops, 'q', 2, 'q', 63)
    def g_pblendvb(self, fg, ins, ops): return self.blendv(fg, ins, ops, 'b', 16, 'b', 7)

    def pmov(self, fg, ins, ops, sfield, dfield, lanes, srcbytes):
        D, _ = self.dst(ins, ops[0])
        if ops[1].type == X86_OP_MEM:
            v = 'xmm_t S;memset(&S,0,16);memcpy(&S,(void *)(uintptr_t)(%s),%d);' % (fg.addr(ins, ops[1]), srcbytes)
        else:
            v = self.vec_of(fg, ins, ops[1], 16)
        return '%sxmm_t A=S;for(int k=0;k<%d;k++)%s.%s[k]=A.%s[k];' % (v, lanes, D, dfield, sfield)

    def g_pmovzxbw(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'b', 'w', 8, 8)
    def g_pmovzxbd(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'b', 'u', 4, 4)
    def g_pmovzxbq(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'b', 'q', 2, 2)
    def g_pmovzxwd(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'w', 'u', 4, 8)
    def g_pmovzxwq(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'w', 'q', 2, 4)
    def g_pmovzxdq(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'u', 'q', 2, 8)
    def g_pmovsxbw(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'sb', 'sw', 8, 8)
    def g_pmovsxbd(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'sb', 'i', 4, 4)
    def g_pmovsxbq(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'sb', 'sq', 2, 2)
    def g_pmovsxwd(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'sw', 'i', 4, 8)
    def g_pmovsxwq(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'sw', 'sq', 2, 4)
    def g_pmovsxdq(self, fg, ins, ops): return self.pmov(fg, ins, ops, 'i', 'sq', 2, 8)

    def g_packusdw(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        sat = '(uint16_t)(v<0?0:v>65535?65535:v)'
        return '%sxmm_t A=%s;for(int k=0;k<4;k++){int32_t v=A.i[k];%s.w[k]=%s;v=S.i[k];%s.w[4+k]=%s;}' % (
            self.vec_of(fg, ins, ops[1], 16), D, D, sat, D, sat)

    def g_pmuldq(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sxmm_t A=%s;%s.sq[0]=(int64_t)A.i[0]*S.i[0];%s.sq[1]=(int64_t)A.i[2]*S.i[2];' % (self.vec_of(fg, ins, ops[1], 16), D, D, D)

    def rounds(self, fg, ins, ops, field, lanes, scalar):
        D, _ = self.dst(ins, ops[0])
        m = ops[2].imm
        fn = 'sse_roundd' if field == 'd' else 'sse_roundf'
        v = self.vec_of(fg, ins, ops[1], 16)
        if scalar:
            return '%s%s.%s[0]=%s(c,S.%s[0],%d);' % (v, D, field, fn, field, m)
        return '%sfor(int k=0;k<%d;k++)%s.%s[k]=%s(c,S.%s[k],%d);' % (v, lanes, D, field, fn, field, m)

    def g_roundss(self, fg, ins, ops): return self.rounds(fg, ins, ops, 'f', 1, True)
    def g_roundsd(self, fg, ins, ops): return self.rounds(fg, ins, ops, 'd', 1, True)
    def g_roundps(self, fg, ins, ops): return self.rounds(fg, ins, ops, 'f', 4, False)
    def g_roundpd(self, fg, ins, ops): return self.rounds(fg, ins, ops, 'd', 2, False)

    def g_dpps(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        m = ops[2].imm
        terms = '+'.join('A.f[%d]*S.f[%d]' % (k, k) for k in range(4) if m & (0x10 << k)) or '0.0f'
        outs = ''.join('%s.f[%d]=%s;' % (D, k, 'dp' if m & (1 << k) else '0.0f') for k in range(4))
        return '%sxmm_t A=%s;float dp=%s;%s' % (self.vec_of(fg, ins, ops[1], 16), D, terms, outs)

    def g_cvtdq2pd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.d[0]=(double)S.i[0];%s.d[1]=(double)S.i[1];' % (self.vec_of(fg, ins, ops[1], 16), D, D)

    def g_cvtpd2dq(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.i[0]=sse_cvt32(c,S.d[0]);%s.i[1]=sse_cvt32(c,S.d[1]);%s.q[1]=0;' % (self.vec_of(fg, ins, ops[1], 16), D, D, D)

    def g_cvttpd2dq(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.i[0]=sse_cvtt32(S.d[0]);%s.i[1]=sse_cvtt32(S.d[1]);%s.q[1]=0;' % (self.vec_of(fg, ins, ops[1], 16), D, D, D)

    def g_cvtps2dq(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%sfor(int k=0;k<4;k++)%s.i[k]=sse_cvt32(c,S.f[k]);' % (self.vec_of(fg, ins, ops[1], 16), D)

    def g_shufpd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        i = ops[2].imm
        return '%sxmm_t A=%s;%s.q[0]=A.q[%d];%s.q[1]=S.q[%d];' % (self.vec_of(fg, ins, ops[1], 16), D, D, i & 1, D, (i >> 1) & 1)

    def g_sqrtpd(self, fg, ins, ops):
        D, _ = self.dst(ins, ops[0])
        return '%s%s.d[0]=sqrt(S.d[0]);%s.d[1]=sqrt(S.d[1]);' % (self.vec_of(fg, ins, ops[1], 16), D, D)

    def g_movnti(self, fg, ins, ops):
        return fg.wr(ins, ops[0], fg.rd(ins, ops[1]))

    def g_psadbw(self, fg, ins, ops):
        D, w = self.dst(ins, ops[0])
        if w != 16:
            raise Unsupported('psadbw mmx')
        return ('%sxmm_t A=%s;{uint32_t s0=0,s1=0;for(int k=0;k<8;k++){s0+=A.b[k]>S.b[k]?A.b[k]-S.b[k]:S.b[k]-A.b[k];'
                's1+=A.b[8+k]>S.b[8+k]?A.b[8+k]-S.b[8+k]:S.b[8+k]-A.b[8+k];}%s.q[0]=s0;%s.q[1]=s1;}' % (self.vec_of(fg, ins, ops[1], 16), D, D, D))

    def pcmpstr(self, fg, ins, ops, explicit, to_mask):
        D, w = self.dst(ins, ops[0])
        la = lb = '-1'
        if explicit:   # |eax|, |edx| (|rax|, |rdx| with REX.W) saturated; the helper clamps to the element count
            wide = any(0x48 <= x <= 0x4f for x in ins.bytes[:ins.bytes.index(ins.opcode[0])])
            T = 'int64_t' if wide else 'int32_t'
            la = '(%s)rax<0?(%s)rax<-16?16:-(int)(%s)rax:(%s)rax>16?16:(int)(%s)rax' % (T, T, T, T, T)
            lb = '(%s)rdx<0?(%s)rdx<-16?16:-(int)(%s)rdx:(%s)rdx>16?16:(int)(%s)rdx' % (T, T, T, T, T)
        out = 'c->xmm[0]' if to_mask else 'NULL'
        set_idx = '' if to_mask else 'rcx=ix;'
        return '{%suint32_t ix,f=sse_pcmpstr(&%s,&S,%s,%s,%d,&ix,%s);%sCF=f&1;ZF=(f>>1)&1;SF=(f>>2)&1;OF=(f>>3)&1;PF=0;}' % (
            self.vec_of(fg, ins, ops[1], 16), D, la, lb, ops[2].imm & 0xff, '&' + out if to_mask else 'NULL', set_idx)

    def g_pcmpistri(self, fg, ins, ops): return self.pcmpstr(fg, ins, ops, False, False)
    def g_pcmpestri(self, fg, ins, ops): return self.pcmpstr(fg, ins, ops, True, False)
    def g_pcmpistrm(self, fg, ins, ops): return self.pcmpstr(fg, ins, ops, False, True)
    def g_pcmpestrm(self, fg, ins, ops): return self.pcmpstr(fg, ins, ops, True, True)
