// A minimal runtime for the PE32 smoke test: maps the program into the 4 GB
// guest window at its preferred base, binds its imports to host functions
// through magic addresses in the IAT, and runs its entry point. Enough for
// smoke.c (GetStdHandle, WriteFile, ExitProcess); a core's runtime is the
// real thing (WinPC: Cores-Common lib/win32, to come).
#define _GNU_SOURCE
#include "hle.h"
#include <stdarg.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

uint8_t *M;
extern const gen_program PROGRAM;

#define MAGIC_BASE 0xf0000000u
#define STACK_TOP 0xa0000000u
#define TEB 0xe0000000u

typedef void (*host_fn)(CPU *c);
typedef struct { const char *name; host_fn fn; int nargs; } host_import;

static void k_GetStdHandle(CPU *c) { c->r[EAX] = ARG(0); }
static void k_WriteFile(CPU *c)
{
    uint32_t n = ARG(2);
    ssize_t w = write(ARG(0) == (uint32_t)-11 ? 1 : 2, G(ARG(1)), n);
    if (ARG(3)) wr32(ARG(3), w < 0 ? 0 : (uint32_t)w);
    c->r[EAX] = w >= 0;
}
static void k_ExitProcess(CPU *c) { fflush(stdout); _exit((int)ARG(0)); }
static const host_import imports[] = {
    {"GetStdHandle", k_GetStdHandle, 1}, {"WriteFile", k_WriteFile, 5}, {"ExitProcess", k_ExitProcess, 1},
};
#define NIMP (sizeof imports / sizeof *imports)

void guest_unimpl(CPU *c, uint32_t addr, const char *what) { fprintf(stderr, "unimplemented at 0x%x: %s\n", addr, what); _exit(3); }
void hle_call(CPU *c, int i) { guest_unimpl(c, 0, "hle_call (a PE32 has no PLT)"); }

guest_fn guest_lookup(uint32_t a)
{
    unsigned lo = 0, hi = PROGRAM.fn_count;
    while (lo < hi) { unsigned m = (lo + hi) / 2; if (PROGRAM.fn_addr[m] < a) lo = m + 1; else hi = m; }
    return lo < PROGRAM.fn_count && PROGRAM.fn_addr[lo] == a ? PROGRAM.fn_ptr[lo] : NULL;
}
// a call to addr, its return address already pushed
void guest_dispatch(CPU *c, uint32_t addr)
{
    if (addr >= MAGIC_BASE && (addr - MAGIC_BASE) / 16 < NIMP) {
        const host_import *h = &imports[(addr - MAGIC_BASE) / 16];
        h->fn(c);
        c->r[ESP] += 4 + 4 * h->nargs;        // stdcall: the callee pops its arguments
        return;
    }
    guest_fn f = guest_lookup(addr);
    if (!f) guest_unimpl(c, addr, "no translated function here");
    f(c);
}
void guest_jump(CPU *c, uint32_t addr, uint32_t esp0) { guest_dispatch(c, addr); }   // no SEH here: a tail call
void guest_resume(CPU *c, uint32_t target) { guest_unimpl(c, target, "SEH resume (no SEH in this test runtime)"); }
void guest_resumed(CPU *c) { guest_unimpl(c, c->resume_to, "SEH resume"); }
void guest_fault(CPU *c, uint32_t code, uint32_t eip) { fprintf(stderr, "exception 0x%x at 0x%x\n", code, eip); _exit(3); }
void guest_bad_return(CPU *c, uint32_t from, uint32_t expected, uint32_t got) { fprintf(stderr, "bad return in 0x%x\n", from); _exit(3); }
void guest_cpuid(CPU *c, uint32_t *a, uint32_t *b, uint32_t *cc, uint32_t *d) { *a = *b = *cc = *d = 0; }
uint64_t guest_rdtsc(CPU *c) { return 0; }
void guest_port_out(CPU *c, uint16_t port, uint32_t value, int size) { guest_unimpl(c, port, "out"); }
uint32_t guest_port_in(CPU *c, uint16_t port, int size) { guest_unimpl(c, port, "in"); return 0; }
void guest_debug_service(CPU *c, uint32_t a, uint32_t b, uint32_t d) {}
int64_t x87_round(CPU *c, double v) { return (int64_t)__builtin_nearbyint(v); }
int64_t sse_round(CPU *c, double v) { return (int64_t)__builtin_nearbyint(v); }
double x87_load80(uint32_t a) { guest_unimpl(0, a, "fld tbyte"); return 0; }
void x87_store80(uint32_t a, double v) { guest_unimpl(0, a, "fstp tbyte"); }

static uint32_t u32at(const uint8_t *p) { uint32_t v; memcpy(&v, p, 4); return v; }

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "usage: %s program.exe\n", argv[0]); return 2; }
    M = mmap(NULL, 1ull << 32, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (M == MAP_FAILED) { perror("mmap"); return 2; }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror(argv[1]); return 2; }
    static uint8_t file[16 << 20];
    size_t n = fread(file, 1, sizeof file, f);
    fclose(f);
    uint32_t pe = u32at(file + 0x3c), opt = pe + 24;
    uint16_t nsec; memcpy(&nsec, file + pe + 6, 2);
    uint16_t optsz; memcpy(&optsz, file + pe + 20, 2);
    uint32_t base = u32at(file + opt + 28), entry = u32at(file + opt + 16), hdrsz = u32at(file + opt + 60);
    memcpy(M + base, file, hdrsz < n ? hdrsz : n);
    for (int i = 0; i < nsec; i++) {
        const uint8_t *s = file + opt + optsz + 40 * i;
        uint32_t vsize = u32at(s + 8), rva = u32at(s + 12), rsize = u32at(s + 16), rptr = u32at(s + 20);
        uint32_t k = rsize < vsize || !vsize ? rsize : vsize;
        if (rptr + k <= n) memcpy(M + base + rva, file + rptr, k);
    }
    // imports: each IAT slot gets the magic address of its host function
    for (uint32_t d = base + u32at(file + opt + 96 + 8 * 1); rd32(d + 12); d += 20) {
        uint32_t look = base + (rd32(d) ? rd32(d) : rd32(d + 16)), iat = base + rd32(d + 16);
        for (; rd32(look); look += 4, iat += 4) {
            uint32_t v = rd32(look);
            const char *name = v & 0x80000000u ? "" : G(base + v + 2);
            unsigned k = 0;
            while (k < NIMP && strcmp(imports[k].name, name)) k++;
            if (k == NIMP) { fprintf(stderr, "import %s not provided\n", name); return 2; }
            wr32(iat, MAGIC_BASE + 16 * k);
        }
    }
    static CPU cpu;
    CPU *c = &cpu;
    c->fs_base = TEB;
    wr32(TEB + 0x18, TEB);
    c->fcw = 0x37f;
    c->mxcsr = 0x1f80;
    c->r[ESP] = STACK_TOP - 16;
    c->r[ESP] -= 4; wr32(c->r[ESP], 0xfffffff0u);     // a return address nothing reaches: start ends in ExitProcess
    guest_fn ep = guest_lookup(base + entry);
    if (!ep) { fprintf(stderr, "no translated entry point 0x%x\n", base + entry); return 2; }
    ep(c);
    fprintf(stderr, "the entry point returned\n");
    return 1;
}
