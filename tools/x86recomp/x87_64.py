"""x87 translation for recomp64.py: the same decoding as recomp.py's, over
64-bit addresses. MSVC x64 code uses it only for long double in the C runtime."""
from capstone.x86 import X86_OP_MEM


class X87Gen64:
    def gen(self, fg, ins, opc, modrm):
        mod, reg, rm = modrm >> 6, (modrm >> 3) & 7, modrm & 7
        ops = ins.operands
        if mod != 3:
            mop = [o for o in ops if o.type == X86_OP_MEM][0]
            a = fg.addr(ins, mop)
            arith = {0: '+', 1: '*', 4: '-', 5: 'r-', 6: '/', 7: 'r/'}
            if opc in (0xd8, 0xdc, 0xda, 0xde):
                if opc == 0xd8:
                    v = 'rdf32(%s)' % a
                elif opc == 0xdc:
                    v = 'rdf64(%s)' % a
                elif opc == 0xda:
                    v = '(double)(int32_t)rd32(%s)' % a
                else:
                    v = '(double)(int16_t)rd16(%s)' % a
                if reg in (2, 3):
                    return 'fcompare(c,ST(0),%s);%s' % (v, 'fpop(c);' if reg == 3 else '')
                o = arith[reg]
                if o.startswith('r'):
                    return 'ST(0)=%s%sST(0);' % (v, o[1])
                return 'ST(0)=ST(0)%s%s;' % (o, v)
            if opc == 0xd9:
                if reg == 0:
                    return 'fpush(c,rdf32(%s));' % a
                if reg == 2:
                    return 'wrf32(%s,(float)ST(0));' % a
                if reg == 3:
                    return 'wrf32(%s,(float)ST(0));fpop(c);' % a
                if reg == 5:
                    return 'c->fcw=rd16(%s);' % a
                if reg == 7:
                    return 'wr16(%s,c->fcw);' % a
            if opc == 0xdb:
                if reg == 0:
                    return 'fpush(c,(double)(int32_t)rd32(%s));' % a
                if reg == 1:
                    return 'wr32(%s,(uint32_t)(int32_t)(ST(0)>=-2147483648.0&&ST(0)<2147483648.0?(int32_t)ST(0):(int32_t)0x80000000));fpop(c);' % a
                if reg == 2:
                    return 'wr32(%s,(uint32_t)x87_int(c,ST(0),32));' % a
                if reg == 3:
                    return 'wr32(%s,(uint32_t)x87_int(c,ST(0),32));fpop(c);' % a
                if reg == 5:
                    return 'fpush(c,x87_load80(%s));' % a
                if reg == 7:
                    return 'x87_store80(%s,ST(0));fpop(c);' % a
            if opc == 0xdd:
                if reg == 0:
                    return 'fpush(c,rdf64(%s));' % a
                if reg == 1:      # fisttp m64 (SSE3): truncate, out of range is the integer indefinite
                    return 'wr64(%s,(uint64_t)(ST(0)>=-9223372036854775808.0&&ST(0)<9223372036854775808.0?(int64_t)ST(0):(int64_t)0x8000000000000000ull));fpop(c);' % a
                if reg == 2:
                    return 'wrf64(%s,ST(0));' % a
                if reg == 3:
                    return 'wrf64(%s,ST(0));fpop(c);' % a
                if reg == 7:
                    return 'wr16(%s,fstatus(c));' % a
                if reg == 4:
                    return 'x87_frstor(c,%s);' % a
                if reg == 6:
                    return 'x87_fnsave(c,%s);' % a
            if opc == 0xdf:
                if reg == 0:
                    return 'fpush(c,(double)(int16_t)rd16(%s));' % a
                if reg == 1:      # fisttp m16
                    return 'wr16(%s,(uint16_t)(int16_t)(ST(0)>-32769.0&&ST(0)<32768.0?(int16_t)ST(0):(int16_t)0x8000));fpop(c);' % a
                if reg == 2:
                    return 'wr16(%s,(uint16_t)x87_int(c,ST(0),16));' % a
                if reg == 3:
                    return 'wr16(%s,(uint16_t)x87_int(c,ST(0),16));fpop(c);' % a
                if reg == 5:
                    return 'fpush(c,(double)(int64_t)rd64(%s));' % a
                if reg == 7:
                    return 'wr64(%s,(uint64_t)x87_int(c,ST(0),64));fpop(c);' % a
            raise fg.rc.Unsupported('x87 %02x /%d' % (opc, reg))
        i = rm
        if opc == 0xd8:
            if reg in (2, 3):
                return 'fcompare(c,ST(0),ST(%d));%s' % (i, 'fpop(c);' if reg == 3 else '')
            return {0: 'ST(0)=ST(0)+ST(%d);', 1: 'ST(0)=ST(0)*ST(%d);', 4: 'ST(0)=ST(0)-ST(%d);',
                    5: 'ST(0)=ST(%d)-ST(0);', 6: 'ST(0)=ST(0)/ST(%d);', 7: 'ST(0)=ST(%d)/ST(0);'}[reg] % i
        if opc in (0xdc, 0xde):
            pop = 'fpop(c);' if opc == 0xde else ''
            if opc == 0xde and reg == 3 and i == 1:
                return 'fcompare(c,ST(0),ST(1));fpop(c);fpop(c);'
            if reg in (2, 3):
                raise fg.rc.Unsupported('x87 dc/de compare')
            return ({0: 'ST(%d)=ST(%d)+ST(0);', 1: 'ST(%d)=ST(%d)*ST(0);', 4: 'ST(%d)=ST(0)-ST(%d);',
                     5: 'ST(%d)=ST(%d)-ST(0);', 6: 'ST(%d)=ST(0)/ST(%d);', 7: 'ST(%d)=ST(%d)/ST(0);'}[reg] % (i, i)) + pop
        if opc == 0xd9 and modrm == 0xe5:
            return 'x87_fxam(c);'
        if opc == 0xd9:
            if reg == 0:
                return '{double t=ST(%d);fpush(c,t);}' % i
            if reg == 1:
                return '{double t=ST(0);ST(0)=ST(%d);ST(%d)=t;}' % (i, i)
            x = {0xd0: ';', 0xe0: 'ST(0)=-ST(0);', 0xe1: 'ST(0)=fabs(ST(0));', 0xe4: 'fcompare(c,ST(0),0.0);',
                 0xe8: 'fpush(c,1.0);', 0xe9: 'fpush(c,3.321928094887362347870);', 0xea: 'fpush(c,1.442695040888963407360);',
                 0xeb: 'fpush(c,3.141592653589793238463);', 0xec: 'fpush(c,0.301029995663981195214);',
                 0xed: 'fpush(c,0.693147180559945309417);', 0xee: 'fpush(c,0.0);',
                 0xf0: 'ST(0)=exp2(ST(0))-1.0;', 0xf1: 'ST(1)=ST(1)*log2(ST(0));fpop(c);',
                 0xf2: 'ST(0)=tan(ST(0));fpush(c,1.0);', 0xf3: 'ST(1)=atan2(ST(1),ST(0));fpop(c);',
                 0xf8: 'ST(0)=fmod(ST(0),ST(1));c->fsw&=~0x400;', 0xf5: 'ST(0)=remainder(ST(0),ST(1));c->fsw&=~0x400;',
                 0xfa: 'ST(0)=sqrt(ST(0));', 0xfb: '{double v=ST(0);ST(0)=sin(v);fpush(c,cos(v));}',
                 0xfc: 'ST(0)=(double)x87_round(c,ST(0));', 0xfd: 'ST(0)=ldexp(ST(0),(int)trunc(ST(1)));',
                 0xfe: 'ST(0)=sin(ST(0));', 0xff: 'ST(0)=cos(ST(0));', 0xf6: 'c->ftop=(c->ftop-1)&7;',
                 0xf7: 'c->ftop=(c->ftop+1)&7;', 0xf9: 'ST(1)=ST(1)*log2(ST(0)+1.0);fpop(c);'}.get(modrm)
            if x:
                return x
        if opc == 0xda and modrm == 0xe9:
            return 'fcompare(c,ST(0),ST(1));fpop(c);fpop(c);'
        if opc in (0xda, 0xdb) and modrm < 0xe0:
            # fcmovb/e/be/u, and fcmovnb/ne/nbe/nu with db
            cond = ('CF', 'ZF', '(CF|ZF)', 'PF')[reg]
            return 'if(%s%s)ST(0)=ST(%d);' % ('!' if opc == 0xdb else '', cond, i)
        if opc == 0xdb:
            if modrm == 0xe2:
                return 'c->fsw&=0x7f00;'
            if modrm == 0xe3:
                return 'c->fcw=0x37f;c->fsw=0;c->ftop=0;'
            if 0xe8 <= modrm <= 0xf7:
                return '{double a=ST(0),b=ST(%d);if(a!=a||b!=b){ZF=PF=CF=1;}else{ZF=a==b;CF=a<b;PF=0;}OF=SF=0;}' % i
        if opc == 0xdd:
            if reg == 0:
                return ';'
            if reg == 2:
                return 'ST(%d)=ST(0);' % i
            if reg == 3:
                return 'ST(%d)=ST(0);fpop(c);' % i
            if reg == 4:
                return 'fcompare(c,ST(0),ST(%d));' % i
            if reg == 5:
                return 'fcompare(c,ST(0),ST(%d));fpop(c);' % i
        if opc == 0xdf:
            if modrm == 0xe0:
                return 'rax=(rax&~0xffffull)|fstatus(c);'
            if 0xe8 <= modrm <= 0xf7:
                return '{double a=ST(0),b=ST(%d);if(a!=a||b!=b){ZF=PF=CF=1;}else{ZF=a==b;CF=a<b;PF=0;}OF=SF=0;fpop(c);}' % i
        raise fg.rc.Unsupported('x87 %02x %02x' % (opc, modrm))
