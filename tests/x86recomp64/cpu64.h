// Guest CPU state and the primitives recomp64's generated code is written
// against. Guest memory is host memory: a guest address is a host pointer.
// This copy is the test harness's; a core's runtime carries its own.
#pragma once
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <setjmp.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef union {
    float f[4];
    double d[2];
    uint8_t b[16];
    int8_t sb[16];
    uint16_t w[8];
    int16_t sw[8];
    uint32_t u[4];
    int32_t i[4];
    uint64_t q[2];
    int64_t sq[2];
} xmm_t;

typedef union {
    float f[2];
    uint8_t b[8];
    uint16_t w[4];
    int16_t sw[4];
    uint32_t u[2];
    int32_t i[2];
    int8_t sb[8];
    uint64_t q;
} mmx_t;

typedef struct CPU {
    // rax rcx rdx rbx rsp rbp rsi rdi r8..r15, in encoding order
    uint64_t r[16];
    uint32_t df;
    uint32_t eflags_id;
    double st[8];
    uint32_t ftop;
    uint16_t fcw, fsw;
    xmm_t xmm[16];
    mmx_t mm[8];
    uint32_t mxcsr;
    uint64_t gs_base;     // this thread's TEB
    uint32_t thread_index;
    struct seh_frame *seh_top;   // innermost guest function with a __try on this thread
} CPU;

// Structured exception handling. A function with a C scope table pushes a
// seh_frame on entry and records, before every call (and anything else that
// can fault), its instruction address and the registers the frame holds.
// The runtime's dispatcher walks the chain from seh_top, evaluates the
// filters, runs the __finally handlers and siglongjmps to the frame whose
// __except takes the exception, which continues at that scope's target.
typedef struct seh_scope { uint32_t begin, end, filter, target; } seh_scope;
typedef struct seh_frame {
    struct seh_frame *prev;
    const seh_scope *scopes;
    int nscopes;
    uint64_t base;        // the image base the scope RVAs are relative to
    uint64_t ip;          // RVA of the call or fault site last entered
    uint64_t frame;       // the establisher frame: rsp after the prolog (0 until then)
    uint64_t r[16];       // the registers at that site
    uint64_t target;      // set by the dispatcher: the RVA to continue at
    sigjmp_buf jb;
} seh_frame;

enum { RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI, R8, R9, R10, R11, R12, R13, R14, R15 };

#define GP(a) ((void *)(uintptr_t)(a))
static inline uint8_t rd8(uint64_t a) { return *(uint8_t *)GP(a); }
static inline uint16_t rd16(uint64_t a) { uint16_t v; memcpy(&v, GP(a), 2); return v; }
static inline uint32_t rd32(uint64_t a) { uint32_t v; memcpy(&v, GP(a), 4); return v; }
static inline uint64_t rd64(uint64_t a) { uint64_t v; memcpy(&v, GP(a), 8); return v; }
static inline float rdf32(uint64_t a) { float v; memcpy(&v, GP(a), 4); return v; }
static inline double rdf64(uint64_t a) { double v; memcpy(&v, GP(a), 8); return v; }
static inline void wr8(uint64_t a, uint8_t v) { *(uint8_t *)GP(a) = v; }
static inline void wr16(uint64_t a, uint16_t v) { memcpy(GP(a), &v, 2); }
static inline void wr32(uint64_t a, uint32_t v) { memcpy(GP(a), &v, 4); }
static inline void wr64(uint64_t a, uint64_t v) { memcpy(GP(a), &v, 8); }
static inline void wrf32(uint64_t a, float v) { memcpy(GP(a), &v, 4); }
static inline void wrf64(uint64_t a, double v) { memcpy(GP(a), &v, 8); }
static inline void rd128(xmm_t *x, uint64_t a) { memcpy(x, GP(a), 16); }
static inline void wr128(uint64_t a, const xmm_t *x) { memcpy(GP(a), x, 16); }

static inline uint32_t f2u(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
static inline float u2f(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
static inline uint64_t d2u(double d) { uint64_t u; memcpy(&u, &d, 8); return u; }
static inline double u2d(uint64_t u) { double d; memcpy(&d, &u, 8); return d; }

static inline uint32_t parity8(uint32_t v) { return !__builtin_parity(v & 0xff); }

// x87 (the C runtime's long double paths)
double x87_load80(uint64_t a);
void x87_store80(uint64_t a, double v);
void x87_fnsave(CPU *c, uint64_t a);
void x87_frstor(CPU *c, uint64_t a);
void x87_fxam(CPU *c);
#define ST(i) c->st[(c->ftop + (i)) & 7]
static inline void fpush(CPU *c, double v) { c->ftop = (c->ftop - 1) & 7; c->st[c->ftop] = v; }
static inline void fpop(CPU *c) { c->ftop = (c->ftop + 1) & 7; }
static inline uint16_t fstatus(CPU *c) { return (uint16_t)((c->fsw & ~0x3800) | ((c->ftop & 7) << 11)); }
static inline void fcompare(CPU *c, double a, double b) {
    c->fsw &= ~0x4500;
    if (a != a || b != b) c->fsw |= 0x4500;
    else if (a < b) c->fsw |= 0x0100;
    else if (a == b) c->fsw |= 0x4000;
}
int64_t x87_round(CPU *c, double v);
int64_t sse_round(CPU *c, double v);     // by MXCSR.RC
static inline int32_t sse_cvt32(CPU *c, double v) {
    if (!(v >= -2147483648.0 && v < 2147483648.0)) return (int32_t)0x80000000;
    return (int32_t)sse_round(c, v);
}
static inline int32_t sse_cvtt32(double v) {
    if (!(v >= -2147483648.0 && v < 2147483648.0)) return (int32_t)0x80000000;
    return (int32_t)v;
}
static inline int64_t sse_cvt64(CPU *c, double v) {
    if (!(v >= -9223372036854775808.0 && v < 9223372036854775808.0)) return INT64_MIN;
    return sse_round(c, v);
}
static inline int64_t sse_cvtt64(double v) {
    if (!(v >= -9223372036854775808.0 && v < 9223372036854775808.0)) return INT64_MIN;
    return (int64_t)v;
}
// roundss/sd/ps/pd: imm bit 2 = use MXCSR, else bits 0-1 = mode
double sse_roundd(CPU *c, double v, int imm);
static inline float sse_roundf(CPU *c, float v, int imm) { return (float)sse_roundd(c, v, imm); }
static inline int64_t x87_int(CPU *c, double v, int bits) {
    if (bits == 64) {
        if (!(v >= -9223372036854775808.0 && v < 9223372036854775808.0)) return INT64_MIN;
        return x87_round(c, v);
    }
    int64_t lim = bits == 16 ? 32768 : 2147483648LL;
    if (!(v >= -(double)lim && v < (double)lim)) return bits == 16 ? -32768 : (int64_t)(int32_t)0x80000000;
    int64_t r = x87_round(c, v);
    if (r < -lim || r >= lim) return bits == 16 ? -32768 : (int64_t)(int32_t)0x80000000;
    return r;
}
static inline int sse_cmp(double a, double b, int p) {
    switch (p & 7) {
    case 0: return a == b;
    case 1: return a < b;
    case 2: return a <= b;
    case 3: return a != a || b != b;
    case 4: return !(a == b);
    case 5: return !(a < b);
    case 6: return !(a <= b);
    default: return !(a != a || b != b);
    }
}
static inline float sse_minf(float a, float b) { return a < b ? a : b; }
static inline float sse_maxf(float a, float b) { return a > b ? a : b; }
static inline double sse_mind(double a, double b) { return a < b ? a : b; }
static inline double sse_maxd(double a, double b) { return a > b ? a : b; }
static inline int16_t sat16(int32_t v) { return v > 32767 ? 32767 : v < -32768 ? -32768 : (int16_t)v; }

// SSE4.2 string compare (pcmpistri/pcmpestri/pcmpistrm/pcmpestrm): a is the
// first operand, b the second; la/lb are explicit lengths or -1 for implicit
// (nul-terminated). Returns the flags as CF | ZF<<1 | SF<<2 | OF<<3 and the
// index in *idx; with mask != NULL writes the *strm result there.
static inline uint32_t sse_pcmpstr(const xmm_t *a, const xmm_t *b, int la, int lb, int imm, uint32_t *idx, xmm_t *mask)
{
    int words = imm & 1, sgn = imm & 2, n = words ? 8 : 16;
    int64_t ea[16], eb[16];
    for (int i = 0; i < n; i++) {
        if (words) { ea[i] = sgn ? (int64_t)a->sw[i] : a->w[i]; eb[i] = sgn ? (int64_t)b->sw[i] : b->w[i]; }
        else { ea[i] = sgn ? (int64_t)a->sb[i] : a->b[i]; eb[i] = sgn ? (int64_t)b->sb[i] : b->b[i]; }
    }
    if (la < 0) { la = n; for (int i = 0; i < n; i++) if (ea[i] == 0) { la = i; break; } }
    if (lb < 0) { lb = n; for (int i = 0; i < n; i++) if (eb[i] == 0) { lb = i; break; } }
    if (la > n) la = n;
    if (lb > n) lb = n;
    int agg = (imm >> 2) & 3;
    uint32_t res = 0;
    for (int j = 0; j < n; j++) {
        int bit = 0;
        if (agg == 0) {                      // equal any: b[j] in the set a
            for (int i = 0; i < la && j < lb; i++) if (ea[i] == eb[j]) { bit = 1; break; }
        } else if (agg == 1) {               // ranges: a holds (lo, hi) pairs
            for (int i = 0; i + 1 < la && j < lb; i += 2) if (eb[j] >= ea[i] && eb[j] <= ea[i + 1]) { bit = 1; break; }
        } else if (agg == 2) {               // equal each
            bit = (j < la && j < lb) ? ea[j] == eb[j] : (j >= la && j >= lb);
        } else {                             // equal ordered: a at b[j..]
            bit = 1;
            for (int i = 0; i < la; i++) {
                int k = j + i;
                if (k >= n) break;                       // past the end: not part of the comparison
                if (k >= lb || ea[i] != eb[k]) { bit = 0; break; }
            }
        }
        if (bit) res |= 1u << j;
    }
    int pol = (imm >> 4) & 3;
    uint32_t all = n == 16 ? 0xffffu : 0xffu;
    if (pol == 1) res = ~res & all;
    else if (pol == 3) res ^= (lb >= n ? all : ((1u << lb) - 1));
    uint32_t ix = n;
    if (res) ix = (imm & 0x40) ? 31 - (uint32_t)__builtin_clz(res) : (uint32_t)__builtin_ctz(res);
    *idx = ix;
    if (mask) {
        memset(mask, 0, sizeof *mask);
        if (imm & 0x40) {
            for (int j = 0; j < n; j++) if (res >> j & 1) { if (words) mask->w[j] = 0xffff; else mask->b[j] = 0xff; }
        } else mask->u[0] = res;
    }
    return (res != 0) | (lb < n) << 1 | (la < n) << 2 | (res & 1) << 3;
}

// Calls between generated code, the dispatcher and the host-side libraries.
typedef void (*guest_fn)(CPU *c);
guest_fn guest_lookup(uint64_t addr);
void guest_dispatch(CPU *c, uint64_t addr);     // call addr; return address already pushed
void guest_unimpl(CPU *c, uint64_t addr, const char *what);
void guest_divide_error(CPU *c, uint64_t addr);
void guest_cpuid(CPU *c, uint32_t *a, uint32_t *b, uint32_t *cc, uint32_t *d);
uint64_t guest_rdtsc(CPU *c);

// What recomp64 writes to gen_tables.c for one translated module.
typedef struct {
    const char *name;             // the --name
    const char *file;             // the PE's file name
    uint64_t *image_base_var;     // IB_<name>: the loader stores the mapped base here
    uint64_t preferred_base;
    uint32_t size_of_image;
    uint32_t entry_rva;
    int is_dll;
    const uint32_t *fn_rva;
    const guest_fn *fn_ptr;
    int nfn;
    const uint32_t *import_slot;  // IAT slot RVAs, in import index order
    const char *const *import_dll;
    const char *const *import_name;
    const uint32_t *import_ordinal;
    int nimports;
    const char *const *export_name;
    const uint32_t *export_ordinal;
    const uint32_t *export_rva;
    int nexports;
    uint32_t tls_raw_start, tls_raw_end, tls_index_rva, tls_zero_fill;
    const uint32_t *tls_callbacks;
    int ntls_callbacks;
    uint32_t security_cookie_rva;
    const char *const *sec_name;
    const uint32_t *sec_rva, *sec_vsize, *sec_fsize, *sec_flags;
    int nsec;
    uint64_t stack_reserve, stack_commit;
} gen_program64;

#ifdef __cplusplus
}
#endif
