// The test runtime's copy of recomp.py's contract (a core keeps its own):
// guest CPU state and the primitives generated code is written against.
// Guest memory is one 4 GB window at M; a guest address is an offset into it.
#pragma once
#include <stdint.h>
#include <string.h>
#include <math.h>

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
    uint64_t q;
} mmx_t;

typedef struct CPU {
    // eax ecx edx ebx esp ebp esi edi, in encoding order
    uint32_t r[8];
    uint32_t df;
    uint32_t eflags_id;   // EFLAGS.ID: software toggles it to detect cpuid
    // x87: physical registers, top-of-stack index, control and status words
    double st[8];
    uint32_t ftop;
    uint16_t fcw, fsw;
    xmm_t xmm[8];
    mmx_t mm[8];
    uint32_t mxcsr;
    // set by a guest function that returns through a different path than call
    uint32_t thread_index;
    uint32_t fs_base;     // Windows and the Xbox: fs points at the thread's TEB / KPCR
    // PE32: the host frames of functions an SEH handler may jump back into
    struct resume_frame *resume_top;
    uint32_t resume_to;   // the address a resumed frame calls
    uint32_t resume_entered;   // set by guest_resumed: the __except block it calls
} CPU;

extern uint8_t *M;

#define EAX 0
#define ECX 1
#define EDX 2
#define EBX 3
#define ESP 4
#define EBP 5
#define ESI 6
#define EDI 7

static inline uint8_t rd8(uint32_t a) { return M[a]; }
static inline uint16_t rd16(uint32_t a) { uint16_t v; memcpy(&v, M + a, 2); return v; }
static inline uint32_t rd32(uint32_t a) { uint32_t v; memcpy(&v, M + a, 4); return v; }
static inline uint64_t rd64(uint32_t a) { uint64_t v; memcpy(&v, M + a, 8); return v; }
static inline float rdf32(uint32_t a) { float v; memcpy(&v, M + a, 4); return v; }
static inline double rdf64(uint32_t a) { double v; memcpy(&v, M + a, 8); return v; }
static inline void wr8(uint32_t a, uint8_t v) { M[a] = v; }
static inline void wr16(uint32_t a, uint16_t v) { memcpy(M + a, &v, 2); }
static inline void wr32(uint32_t a, uint32_t v) { memcpy(M + a, &v, 4); }
static inline void wr64(uint32_t a, uint64_t v) { memcpy(M + a, &v, 8); }
static inline void wrf32(uint32_t a, float v) { memcpy(M + a, &v, 4); }
static inline void wrf64(uint32_t a, double v) { memcpy(M + a, &v, 8); }
static inline void rd128(xmm_t *x, uint32_t a) { memcpy(x, M + a, 16); }
static inline void wr128(uint32_t a, const xmm_t *x) { memcpy(M + a, x, 16); }

static inline uint32_t f2u(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
static inline float u2f(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }
static inline uint64_t d2u(double d) { uint64_t u; memcpy(&u, &d, 8); return u; }
static inline double u2d(uint64_t u) { double d; memcpy(&d, &u, 8); return d; }

static inline uint32_t parity8(uint32_t v) { return !__builtin_parity(v & 0xff); }

// 80-bit extended values in memory (fld/fstp tbyte) are converted to double.
double x87_load80(uint32_t a);
void x87_store80(uint32_t a, double v);

#define ST(i) c->st[(c->ftop + (i)) & 7]
static inline void fpush(CPU *c, double v) { c->ftop = (c->ftop - 1) & 7; c->st[c->ftop] = v; }
static inline void fpop(CPU *c) { c->ftop = (c->ftop + 1) & 7; }
static inline uint16_t fstatus(CPU *c) { return (uint16_t)((c->fsw & ~0x3800) | ((c->ftop & 7) << 11)); }
// C0/C2/C3 from a compare of a and b; unordered sets all three
static inline void fcompare(CPU *c, double a, double b) {
    c->fsw &= ~0x4500;
    if (a != a || b != b) c->fsw |= 0x4500;
    else if (a < b) c->fsw |= 0x0100;
    else if (a == b) c->fsw |= 0x4000;
}
// integer conversion with the rounding mode in the control word
int64_t x87_round(CPU *c, double v);
void x87_fxam(CPU *c);
void x87_fnsave(CPU *c, uint32_t a);
void x87_frstor(CPU *c, uint32_t a);
int64_t sse_round(CPU *c, double v);
static inline int32_t sse_cvt32(CPU *c, double v) {
    if (!(v >= -2147483648.0 && v < 2147483648.0)) return (int32_t)0x80000000;
    return (int32_t)sse_round(c, v);
}
static inline int32_t sse_cvtt32(double v) {
    if (!(v >= -2147483648.0 && v < 2147483648.0)) return (int32_t)0x80000000;
    return (int32_t)v;
}
// fist/fistp: out of range is the integer indefinite. For 64 bits only the
// double is checked: -(int64_t)2^63 is undefined, and clang on arm64 made the
// check always fail, so every fistp m64 stored the indefinite (HotD4's CRI
// sound turned all its send levels to 0 that way).
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

// SSE compare predicates (cmpXXss/sd, imm 0-7)
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

// PE32 frames an SEH handler can resume (recomp.py "SEH frames"): a function
// that links an SEH registration into fs:[0] records its host frame on entry.
// An __except block (an entry starting mov esp,[ebp-x]) that guest_resumed
// did not call, and a jump that leaves esp above its own function's entry
// esp (C++'s jump to a catch continuation), run on the wrong host stack:
// guest_resume longjmps to the innermost recorded frame still live for the
// guest, which calls the target (guest_resumed) and returns as itself, as
// Windows resumes on the establisher's stack.
#include <setjmp.h>
#if defined(_WIN32)
#define RESUME_SETJMP(b) setjmp(b)
#define RESUME_LONGJMP(b) longjmp(b, 1)
#else
#define RESUME_SETJMP(b) _setjmp(b)      // no signal mask: no system call per entry
#define RESUME_LONGJMP(b) _longjmp(b, 1)
#endif
typedef struct resume_frame {
    jmp_buf jb;
    uint32_t esp;         // esp at entry (the return address): the guest frame lies below
    struct resume_frame *prev;
} resume_frame;
static inline void resume_push(struct CPU *c, resume_frame *f) {
    f->esp = c->r[4]; f->prev = c->resume_top; c->resume_top = f;
}
// the innermost recorded frame has returned, as far as the guest stack goes
static inline int resume_dead(struct CPU *c, uint32_t esp) { return c->resume_top && esp > c->resume_top->esp; }
void guest_resume(struct CPU *c, uint32_t target);   // longjmp to the innermost live frame, which runs target
void guest_resumed(struct CPU *c);                    // in that frame: call c->resume_to

// Calls between generated code, the dispatcher and the host-side libraries.
typedef void (*guest_fn)(CPU *c);
guest_fn guest_lookup(uint32_t addr);
void guest_dispatch(CPU *c, uint32_t addr);   // call addr; return address already pushed
void guest_jump(CPU *c, uint32_t addr, uint32_t esp0);   // PE32 indirect jmp (esp0: the function's entry esp): a tail call, a return, or a resume (above)
void guest_fault(CPU *c, uint32_t code, uint32_t eip);   // PE32: a CPU exception (division) for SEH
uint32_t guest_call(CPU *c, uint32_t addr, int nargs, const uint32_t *args);  // host -> guest
void guest_unimpl(CPU *c, uint32_t addr, const char *what);
void guest_port_out(CPU *c, uint16_t port, uint32_t value, int size);
uint32_t guest_port_in(CPU *c, uint16_t port, int size);
void guest_cpuid(CPU *c, uint32_t *a, uint32_t *b, uint32_t *cc, uint32_t *d);
uint64_t guest_rdtsc(CPU *c);
void guest_bad_return(CPU *c, uint32_t from, uint32_t expected, uint32_t got);

#ifdef __cplusplus
}
#endif
