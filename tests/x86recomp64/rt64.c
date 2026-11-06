// The smallest runtime recomp64's output needs: the loader (map the PE's
// sections, apply its relocations, bind its imports to host handlers, set up
// TLS and a TEB), the dispatcher for indirect calls, host -> guest calls and
// the helpers generated code calls. A core's runtime grows from this.
#define _GNU_SOURCE
#include "hle64.h"
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <fenv.h>
#include <sys/mman.h>
#include <pthread.h>

int hle_verbose;
int hle_trace;
const gen_program64 *hle_main_program;

// ---- registry ---------------------------------------------------------------
#define MAX_HLE 4096
static hle_entry hle_tab[MAX_HLE];
static int hle_n;
static hle_object_entry obj_tab[256];
static int obj_n;

static int dll_eq(const char *a, const char *b)
{
    // "KERNEL32.dll" == "kernel32"; the api-ms-win-* names map onto their real library by the caller
    for (; *a && *b; a++, b++) {
        char x = *a | 0x20, y = *b | 0x20;
        if (x == '.' || y == '.') break;
        if (x != y) return 0;
    }
    return (!*a || *a == '.') && (!*b || *b == '.');
}

void hle_register(const hle_entry *t)
{
    for (; t->name; t++) {
        if (hle_n < MAX_HLE) hle_tab[hle_n++] = *t;
    }
}

void hle_register_objects(const hle_object_entry *t)
{
    for (; t->name; t++) obj_tab[obj_n++] = *t;
}

hle_fn hle_find(const char *dll, const char *name)
{
    hle_fn any = NULL;
    for (int i = 0; i < hle_n; i++) {
        if (strcmp(hle_tab[i].name, name)) continue;
        if (!hle_tab[i].dll || !dll || dll_eq(hle_tab[i].dll, dll)) return hle_tab[i].fn;
        if (!any) any = hle_tab[i].fn;
    }
    return any;
}

static void *obj_find(const char *dll, const char *name)
{
    for (int i = 0; i < obj_n; i++)
        if (!strcmp(obj_tab[i].name, name) && (!obj_tab[i].dll || !dll || dll_eq(obj_tab[i].dll, dll))) return obj_tab[i].object;
    return NULL;
}

// ---- magic addresses: host functions callable from guest code --------------------
#define MAX_MAGIC 8192
static hle_fn magic_fns[MAX_MAGIC];
static const char *magic_names[MAX_MAGIC];
static int magic_n;
static uint8_t *magic_base;

static void magic_init(void)
{
    if (magic_base) return;
    magic_base = mmap(NULL, MAX_MAGIC * 16, PROT_READ, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (magic_base == MAP_FAILED) { perror("mmap"); exit(1); }
}

uint64_t magic_register(hle_fn fn, const char *name)
{
    magic_init();
    for (int i = 0; i < magic_n; i++)
        if (magic_fns[i] == fn) return (uint64_t)(uintptr_t)(magic_base + 16 * i);
    if (magic_n >= MAX_MAGIC) { fprintf(stderr, "too many host functions\n"); exit(1); }
    magic_fns[magic_n] = fn;
    magic_names[magic_n] = name;
    return (uint64_t)(uintptr_t)(magic_base + 16 * magic_n++);
}

uint64_t hle_import_address(const char *dll, const char *name)
{
    hle_fn fn = hle_find(dll, name);
    if (!fn) return 0;
    char *nm = malloc(strlen(dll) + strlen(name) + 2);
    sprintf(nm, "%s!%s", dll, name);
    return magic_register(fn, nm);
}

static int magic_index(uint64_t addr)
{
    if (!magic_base) return -1;
    uint64_t b = (uint64_t)(uintptr_t)magic_base;
    if (addr < b || addr >= b + 16 * (uint64_t)magic_n) return -1;
    return (int)((addr - b) / 16);
}

// ---- programs and the dispatcher ----------------------------------------------------
#define MAX_PROGS 32
static const gen_program64 *progs[MAX_PROGS];
static int nprogs;
static uint64_t *import_magic[MAX_PROGS];   // per program: what each import slot holds

guest_fn guest_lookup(uint64_t addr)
{
    for (int p = 0; p < nprogs; p++) {
        const gen_program64 *g = progs[p];
        uint64_t ib = *g->image_base_var;
        if (addr < ib || addr >= ib + g->size_of_image) continue;
        uint32_t rva = (uint32_t)(addr - ib);
        int lo = 0, hi = g->nfn - 1;
        while (lo <= hi) {
            int mid = (lo + hi) / 2;
            if (g->fn_rva[mid] == rva) return g->fn_ptr[mid];
            if (g->fn_rva[mid] < rva) lo = mid + 1; else hi = mid - 1;
        }
        return NULL;
    }
    return NULL;
}

static void unresolved_import(CPU *c);

void guest_dispatch(CPU *c, uint64_t addr)
{
    guest_fn f = guest_lookup(addr);
    if (f) { f(c); return; }
    int k = magic_index(addr);
    if (k >= 0) {
        if (hle_trace) fprintf(stderr, "[call %s]\n", magic_names[k] ? magic_names[k] : "?");
        magic_fns[k](c);
        c->r[RSP] += 8;
        return;
    }
    uint64_t from = rd64(c->r[RSP]);
    fprintf(stderr, "guest: call to unknown address 0x%llx (from 0x%llx)\n", (unsigned long long)addr, (unsigned long long)from);
    exit(70);
}

void hle_call(CPU *c, int i)
{
    const gen_program64 *g = hle_main_program;
    uint64_t m = import_magic[0][i];
    int k = magic_index(m);
    if (k < 0) {
        fprintf(stderr, "import %d (%s!%s) is a data object, not a function\n", i, g->import_dll[i], g->import_name[i]);
        exit(70);
    }
    if (hle_trace) fprintf(stderr, "[%s!%s]\n", g->import_dll[i], g->import_name[i]);
    if (magic_fns[k] == unresolved_import) {
        fprintf(stderr, "guest: unimplemented import %s!%s called (return to 0x%llx)\n", g->import_dll[i], g->import_name[i],
                (unsigned long long)rd64(c->r[RSP]));
        exit(71);
    }
    magic_fns[k](c);
    c->r[RSP] += 8;
}

static uint64_t unresolved_names_slot;
static void unresolved_import(CPU *c)
{
    // the slot the call came through is not known here; the trace names it
    fprintf(stderr, "guest: unimplemented import called (return to 0x%llx); run with HLE_TRACE=1 to see which\n",
            (unsigned long long)rd64(c->r[RSP]));
    exit(71);
}

void guest_unimpl(CPU *c, uint64_t addr, const char *what)
{
    fprintf(stderr, "guest: stop at 0x%llx (rva 0x%llx): %s\n", (unsigned long long)addr,
            (unsigned long long)(addr - *hle_main_program->image_base_var), what);
    exit(72);
}

void guest_divide_error(CPU *c, uint64_t addr)
{
    guest_unimpl(c, addr, "integer divide by zero");
}

void guest_cpuid(CPU *c, uint32_t *a, uint32_t *b, uint32_t *cc, uint32_t *d)
{
    uint32_t leaf = *a, sub = *cc;
    *a = *b = *cc = *d = 0;
    switch (leaf) {
    case 0: *a = 0xd; *b = 0x756e6547; *d = 0x49656e69; *cc = 0x6c65746e; break;   // GenuineIntel
    case 1:
        *a = 0x000306a9;      // family 6 model 0x3a
        *b = 0x00100800;
        // ecx: sse3, pclmul off, ssse3, fma off, cx16, sse4.1, sse4.2, popcnt; no avx, no osxsave
        *cc = (1u << 0) | (1u << 9) | (1u << 13) | (1u << 19) | (1u << 20) | (1u << 23);
        // edx: fpu tsc cx8 cmov mmx fxsr sse sse2
        *d = (1u << 0) | (1u << 4) | (1u << 8) | (1u << 15) | (1u << 23) | (1u << 24) | (1u << 25) | (1u << 26);
        break;
    case 7: break;            // no avx2, no bmi
    case 0x80000000: *a = 0x80000008; break;
    case 0x80000001: *d = (1u << 29) | (1u << 20) | (1u << 11); *cc = 1u << 0; break;   // lm, nx, syscall; lahf
    case 0x80000002: memcpy(a, "Arca", 4); memcpy(b, "de F", 4); memcpy(cc, "ound", 4); memcpy(d, "ry x", 4); break;
    case 0x80000003: memcpy(a, "86-6", 4); memcpy(b, "4 gu", 4); memcpy(cc, "est ", 4); memcpy(d, "    ", 4); break;
    case 0x80000004: memcpy(a, "    ", 4); memcpy(b, "    ", 4); memcpy(cc, "    ", 4); memcpy(d, "   ", 4); break;
    default: break;
    }
    (void)sub;
}

uint64_t guest_rdtsc(CPU *c)
{
    (void)c;
#if defined(__x86_64__)
    uint32_t lo, hi;
    __asm__ volatile("rdtsc" : "=a"(lo), "=d"(hi));
    return ((uint64_t)hi << 32) | lo;
#elif defined(__aarch64__)
    uint64_t v;
    __asm__ volatile("mrs %0, cntvct_el0" : "=r"(v));
    return v * 24;
#else
    return 0;
#endif
}

// ---- rounding helpers --------------------------------------------------------------
static int64_t round_mode(double v, int rc)
{
    switch (rc & 3) {
    case 0: return (int64_t)nearbyint(v);    // nearest-even (host default)
    case 1: return (int64_t)floor(v);
    case 2: return (int64_t)ceil(v);
    default: return (int64_t)trunc(v);
    }
}
int64_t x87_round(CPU *c, double v) { return round_mode(v, (c->fcw >> 10) & 3); }
int64_t sse_round(CPU *c, double v) { return round_mode(v, (c->mxcsr >> 13) & 3); }
double sse_roundd(CPU *c, double v, int imm)
{
    int rc = (imm & 4) ? (c->mxcsr >> 13) & 3 : imm & 3;
    switch (rc) {
    case 0: return nearbyint(v);
    case 1: return floor(v);
    case 2: return ceil(v);
    default: return trunc(v);
    }
}

double x87_load80(uint64_t a)
{
    uint8_t b[10];
    memcpy(b, GP(a), 10);
    uint64_t m; memcpy(&m, b, 8);
    uint16_t se; memcpy(&se, b + 8, 2);
    int sign = se >> 15, e = se & 0x7fff;
    if (e == 0 && m == 0) return sign ? -0.0 : 0.0;
    if (e == 0x7fff) return (m << 1) ? NAN : (sign ? -INFINITY : INFINITY);
    double v = ldexp((double)m, e - 16383 - 63);
    return sign ? -v : v;
}

void x87_store80(uint64_t a, double v)
{
    uint8_t b[10] = {0};
    int sign = signbit(v) ? 1 : 0;
    v = fabs(v);
    uint64_t m = 0; int e = 0;
    if (v == 0) { e = 0; m = 0; }
    else if (isinf(v)) { e = 0x7fff; m = 1ull << 63; }
    else if (isnan(v)) { e = 0x7fff; m = 3ull << 62; }
    else {
        int ex; double f = frexp(v, &ex);   // v = f * 2^ex, f in [0.5,1)
        m = (uint64_t)ldexp(f, 64);
        e = ex - 1 + 16383;
    }
    memcpy(b, &m, 8);
    uint16_t se = (uint16_t)(e | (sign << 15));
    memcpy(b + 8, &se, 2);
    memcpy(GP(a), b, 10);
}

void x87_fnsave(CPU *c, uint64_t a) { memset(GP(a), 0, 108); wr16(a, c->fcw); wr16(a + 4, fstatus(c)); c->fcw = 0x37f; c->fsw = 0; c->ftop = 0; }
void x87_frstor(CPU *c, uint64_t a) { c->fcw = rd16(a); c->fsw = rd16(a + 4); c->ftop = (c->fsw >> 11) & 7; }
void x87_fxam(CPU *c)
{
    double v = ST(0);
    c->fsw &= ~0x4700;
    if (signbit(v)) c->fsw |= 0x0200;
    if (isnan(v)) c->fsw |= 0x0100;
    else if (isinf(v)) c->fsw |= 0x0500;
    else if (v == 0) c->fsw |= 0x4000;
    else c->fsw |= 0x0400;
}

// ---- TEB ------------------------------------------------------------------------------
// Offsets the game's code reaches through gs: 0x08 StackBase, 0x10 StackLimit,
// 0x30 Self, 0x40 ProcessId, 0x48 ThreadId, 0x58 ThreadLocalStoragePointer,
// 0x60 ProcessEnvironmentBlock, 0x68 LastErrorValue, 0x1480 TlsSlots[64],
// 0x1780 TlsExpansionSlots.
#define TEB_SIZE 0x1900
static uint8_t *peb;
static uint32_t next_tid = 0x1000;
static pthread_mutex_t tls_lock = PTHREAD_MUTEX_INITIALIZER;

static uint8_t *tls_block_new(void)
{
    const gen_program64 *g = hle_main_program;
    uint64_t ib = *g->image_base_var;
    size_t raw = g->tls_raw_end - g->tls_raw_start, n = raw + g->tls_zero_fill;
    if (!g->tls_index_rva) return NULL;
    uint8_t *b = calloc(1, n ? n : 1);
    if (raw) memcpy(b, GP(ib + g->tls_raw_start), raw);
    return b;
}

uint64_t teb_create(CPU *c, uint64_t stack_base, uint64_t stack_size)
{
    uint8_t *teb = calloc(1, TEB_SIZE);
    uint64_t t = (uint64_t)(uintptr_t)teb;
    wr64(t + 0x08, stack_base);
    wr64(t + 0x10, stack_base - stack_size);
    wr64(t + 0x30, t);
    wr64(t + 0x40, 0x4a0);
    pthread_mutex_lock(&tls_lock);
    wr64(t + 0x48, next_tid += 4);
    pthread_mutex_unlock(&tls_lock);
    if (!peb) peb = calloc(1, 0x7c8);
    wr64(t + 0x60, (uint64_t)(uintptr_t)peb);
    // the static TLS array: one slot per module (only the main program here)
    uint64_t *tlsp = calloc(8, sizeof *tlsp);
    tlsp[0] = (uint64_t)(uintptr_t)tls_block_new();
    wr64(t + 0x58, (uint64_t)(uintptr_t)tlsp);
    c->gs_base = t;
    return t;
}

void teb_set_last_error(CPU *c, uint32_t e) { wr32(c->gs_base + 0x68, e); }
uint32_t teb_get_last_error(CPU *c) { return rd32(c->gs_base + 0x68); }

uint64_t guest_stack_alloc(uint64_t size)
{
    size = (size + 0xffff) & ~0xffffull;
    uint8_t *s = mmap(NULL, size + 0x10000, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (s == MAP_FAILED) { perror("stack"); exit(1); }
    mprotect(s, 0x10000, PROT_NONE);   // guard
    return (uint64_t)(uintptr_t)(s + 0x10000 + size);
}

// ---- host -> guest -----------------------------------------------------------------------
static void return_magic(CPU *c) { (void)c; }

uint64_t guest_call(CPU *c, uint64_t addr, int nargs, const uint64_t *args)
{
    static uint64_t ret_magic;
    if (!ret_magic) ret_magic = magic_register(return_magic, "<return to host>");
    CPU saved = *c;
    uint64_t rsp = c->r[RSP] & ~0xfull;
    int nstack = nargs > 4 ? nargs - 4 : 0;
    rsp -= 8 * (uint64_t)((nstack + 1) & ~1);   // keep 16-byte alignment at the call
    for (int i = 4; i < nargs; i++) wr64(rsp + 32 + 8 * (uint64_t)(i - 4), args[i]);
    rsp -= 32;                                   // shadow space
    if (nargs > 0) c->r[RCX] = args[0];
    if (nargs > 1) c->r[RDX] = args[1];
    if (nargs > 2) c->r[R8] = args[2];
    if (nargs > 3) c->r[R9] = args[3];
    for (int i = 0; i < 4 && i < nargs; i++) { c->xmm[i].q[0] = args[i]; c->xmm[i].q[1] = 0; }
    rsp -= 8;
    wr64(rsp, ret_magic);
    c->r[RSP] = rsp;
    guest_fn f = guest_lookup(addr);
    if (f) f(c);
    else {
        int k = magic_index(addr);
        if (k < 0) { fprintf(stderr, "guest_call: no function at 0x%llx\n", (unsigned long long)addr); exit(70); }
        magic_fns[k](c);
    }
    uint64_t r = c->r[RAX];
    xmm_t x0 = c->xmm[0];
    *c = saved;
    c->r[RAX] = r;
    c->xmm[0] = x0;
    return r;
}

// ---- the loader --------------------------------------------------------------------------------
static int sec_index_of(const gen_program64 *g, uint32_t rva)
{
    for (int i = 0; i < g->nsec; i++)
        if (rva >= g->sec_rva[i] && rva < g->sec_rva[i] + g->sec_vsize[i]) return i;
    return -1;
}

int load_program(const gen_program64 *g, const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); return -1; }
    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *file = malloc(len);
    if (fread(file, 1, len, f) != (size_t)len) { perror("read"); return -1; }
    fclose(f);
    size_t size = (g->size_of_image + 0xffff) & ~0xffffull;
    // a program without a relocation directory must sit at its preferred base
    uint32_t pe0 = *(uint32_t *)(file + 0x3c);
    uint32_t ndirs0 = *(uint32_t *)(file + pe0 + 24 + 108);
    int relocatable = ndirs0 > 5 && *(uint32_t *)(file + pe0 + 24 + 112 + 8 * 5 + 4) != 0;
    uint8_t *img;
    if (relocatable) img = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    else {
        void *want = (void *)(uintptr_t)g->preferred_base;
        img = mmap(want, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (img != MAP_FAILED && img != want) {
            munmap(img, size);
            fprintf(stderr, "cannot map the program at its base 0x%llx (it has no relocations)\n", (unsigned long long)g->preferred_base);
            return -1;
        }
    }
    if (img == MAP_FAILED) { perror("mmap image"); return -1; }
    uint64_t ib = (uint64_t)(uintptr_t)img;
    *g->image_base_var = ib;
    // headers, then each section's file bytes at its RVA
    uint32_t pe = *(uint32_t *)(file + 0x3c);
    uint16_t nsec = *(uint16_t *)(file + pe + 6), opt_size = *(uint16_t *)(file + pe + 20);
    uint32_t hdr_size = *(uint32_t *)(file + pe + 24 + 60);
    memcpy(img, file, hdr_size < (uint32_t)len ? hdr_size : (uint32_t)len);
    const uint8_t *sh = file + pe + 24 + opt_size;
    for (int i = 0; i < nsec; i++, sh += 40) {
        uint32_t vsize = *(uint32_t *)(sh + 8), rva = *(uint32_t *)(sh + 12), rawsize = *(uint32_t *)(sh + 16), rawptr = *(uint32_t *)(sh + 20);
        if (!rawsize) continue;
        uint32_t n = rawsize < vsize || !vsize ? rawsize : vsize;
        if (rawptr + n > (uint32_t)len) n = len - rawptr;
        memcpy(img + rva, file + rawptr, n);
    }
    // base relocations
    const uint8_t *opt = file + pe + 24;
    uint32_t ndirs = *(uint32_t *)(opt + 108);
    uint64_t delta = ib - g->preferred_base;
    if (ndirs > 5 && delta) {
        uint32_t rrva = *(uint32_t *)(opt + 112 + 8 * 5), rsize = *(uint32_t *)(opt + 112 + 8 * 5 + 4);
        uint32_t d = rrva, end = rrva + rsize;
        while (d + 8 <= end) {
            uint32_t page = *(uint32_t *)(img + d), bsize = *(uint32_t *)(img + d + 4);
            if (bsize < 8) break;
            for (uint32_t i = 8; i + 1 < bsize; i += 2) {
                uint16_t e = *(uint16_t *)(img + d + i);
                int t = e >> 12; uint32_t off = e & 0xfff;
                if (t == 10) { uint64_t v; memcpy(&v, img + page + off, 8); v += delta; memcpy(img + page + off, &v, 8); }
                else if (t == 3) { uint32_t v; memcpy(&v, img + page + off, 4); v += (uint32_t)delta; memcpy(img + page + off, &v, 4); }
            }
            d += bsize;
        }
    }
    free(file);
    // imports: every slot gets a magic address for its handler, or a data object
    int p = nprogs++;
    progs[p] = g;
    if (p == 0) hle_main_program = g;
    import_magic[p] = calloc(g->nimports ? g->nimports : 1, sizeof(uint64_t));
    int missing = 0;
    for (int i = 0; i < g->nimports; i++) {
        const char *name = g->import_name[i];
        char ordname[32];
        if (!name || !*name) { snprintf(ordname, sizeof ordname, "#%u", g->import_ordinal[i]); name = ordname; }
        void *obj = obj_find(g->import_dll[i], name);
        uint64_t v;
        if (obj) v = (uint64_t)(uintptr_t)obj;
        else {
            hle_fn fn = hle_find(g->import_dll[i], name);
            if (!fn) {
                if (hle_verbose) fprintf(stderr, "import not provided: %s!%s\n", g->import_dll[i], name);
                missing++;
                // each unresolved import gets its own slot so the trace can name it
                char *nm = malloc(strlen(g->import_dll[i]) + strlen(name) + 2);
                sprintf(nm, "%s!%s", g->import_dll[i], name);
                magic_init();
                if (magic_n >= MAX_MAGIC) { fprintf(stderr, "too many host functions\n"); exit(1); }
                magic_fns[magic_n] = unresolved_import;
                magic_names[magic_n] = nm;
                v = (uint64_t)(uintptr_t)(magic_base + 16 * magic_n++);
            } else {
                char *nm = malloc(strlen(g->import_dll[i]) + strlen(name) + 2);
                sprintf(nm, "%s!%s", g->import_dll[i], name);
                v = magic_register(fn, nm);
            }
        }
        import_magic[p][i] = v;
        wr64(ib + g->import_slot[i], v);
    }
    if (hle_verbose && missing) fprintf(stderr, "%d of %d imports not provided\n", missing, g->nimports);
    // the security cookie must not be its default
    if (g->security_cookie_rva) wr64(ib + g->security_cookie_rva, 0x2b992ddfa232ull ^ (uint64_t)(uintptr_t)&missing);
    // TLS index: slot 0 of the static TLS array
    if (g->tls_index_rva) wr32(ib + g->tls_index_rva, 0);
    return 0;
}

int run_program(const gen_program64 *g, int argc, char **argv)
{
    (void)argc; (void)argv;
    CPU *c = calloc(1, sizeof *c);
    c->fcw = 0x27f;
    c->mxcsr = 0x1f80;
    uint64_t stack_size = g->stack_reserve ? g->stack_reserve : 1 << 20;
    uint64_t top = guest_stack_alloc(stack_size);
    c->r[RSP] = top - 0x100;
    teb_create(c, top, stack_size);
    uint64_t ib = *g->image_base_var;
    // TLS callbacks with DLL_PROCESS_ATTACH
    for (int i = 0; i < g->ntls_callbacks; i++) {
        uint64_t args[3] = { ib, 1, 0 };
        guest_call(c, ib + g->tls_callbacks[i], 3, args);
    }
    uint64_t r = guest_call(c, ib + g->entry_rva, 0, NULL);
    return (int)r;
}

// ---- printf with guest varargs -------------------------------------------------------------
static int vformat(CPU *c, char *out, size_t n, const char *fmt, int first, uint64_t va)
{
    size_t o = 0;
    int k = first;
#define NEXT() (va ? (va += 8, rd64(va - 8)) : ARG(k++))
#define PUT(ch) do { if (o + 1 < n) out[o] = (ch); o++; } while (0)
    while (*fmt) {
        if (*fmt != '%') { PUT(*fmt++); continue; }
        const char *start = fmt++;
        if (*fmt == '%') { PUT('%'); fmt++; continue; }
        char spec[64]; int si = 0;
        spec[si++] = '%';
        while (*fmt && strchr("-+ #0", *fmt)) spec[si++] = *fmt++;
        if (*fmt == '*') { si += sprintf(spec + si, "%d", (int)NEXT()); fmt++; }
        while (*fmt >= '0' && *fmt <= '9') spec[si++] = *fmt++;
        if (*fmt == '.') {
            spec[si++] = *fmt++;
            if (*fmt == '*') { si += sprintf(spec + si, "%d", (int)NEXT()); fmt++; }
            while (*fmt >= '0' && *fmt <= '9') spec[si++] = *fmt++;
        }
        int bits = 32, wide = 0;
        while (*fmt && strchr("hlLqjztIw", *fmt)) {
            if (*fmt == 'l' && fmt[1] == 'l') { bits = 64; fmt += 2; continue; }
            if (*fmt == 'I' && fmt[1] == '6' && fmt[2] == '4') { bits = 64; fmt += 3; continue; }
            if (*fmt == 'I' && fmt[1] == '3' && fmt[2] == '2') { bits = 32; fmt += 3; continue; }
            if (*fmt == 'I' || *fmt == 'z' || *fmt == 't' || *fmt == 'j' || *fmt == 'q') bits = 64;
            if (*fmt == 'l') wide = 1;   // %ls: wide string (an int stays 32 bits on Win64)
            if (*fmt == 'w') wide = 1;
            fmt++;
        }
        char conv = *fmt++;
        char tmp[512];
        int len = 0;
        switch (conv) {
        case 'd': case 'i':
            spec[si++] = 'l'; spec[si++] = 'l'; spec[si++] = 'd'; spec[si] = 0;
            len = snprintf(tmp, sizeof tmp, spec, bits == 64 ? (long long)NEXT() : (long long)(int32_t)NEXT());
            break;
        case 'u': case 'x': case 'X': case 'o':
            spec[si++] = 'l'; spec[si++] = 'l'; spec[si++] = conv; spec[si] = 0;
            len = snprintf(tmp, sizeof tmp, spec, bits == 64 ? (unsigned long long)NEXT() : (unsigned long long)(uint32_t)NEXT());
            break;
        case 'p':
            spec[si++] = 'l'; spec[si++] = 'l'; spec[si++] = 'X'; spec[si] = 0;
            len = snprintf(tmp, sizeof tmp, "%016llX", (unsigned long long)NEXT());
            break;
        case 'c':
            if (wide) { uint32_t ch = (uint32_t)NEXT(); len = snprintf(tmp, sizeof tmp, "%c", ch < 128 ? (int)ch : '?'); }
            else { spec[si++] = 'c'; spec[si] = 0; len = snprintf(tmp, sizeof tmp, spec, (int)NEXT()); }
            break;
        case 's': {
            uint64_t p = NEXT();
            if (!p) { len = snprintf(tmp, sizeof tmp, "(null)"); break; }
            if (wide || conv == 'S') {
                const uint16_t *w = GP(p);
                int m = 0;
                while (w[m] && m < 500) { tmp[m] = w[m] < 128 ? (char)w[m] : '?'; m++; }
                tmp[m] = 0; len = m;
            } else {
                spec[si++] = 's'; spec[si] = 0;
                len = snprintf(tmp, sizeof tmp, spec, (const char *)GP(p));
            }
            break;
        }
        case 'S': {
            uint64_t p = NEXT();
            const uint16_t *w = GP(p);
            int m = 0;
            while (p && w[m] && m < 500) { tmp[m] = w[m] < 128 ? (char)w[m] : '?'; m++; }
            tmp[m] = 0; len = m;
            break;
        }
        case 'f': case 'F': case 'e': case 'E': case 'g': case 'G': case 'a': case 'A':
            spec[si++] = conv; spec[si] = 0;
            // a floating-point vararg travels in the integer register too
            len = snprintf(tmp, sizeof tmp, spec, u2d(NEXT()));
            break;
        case 'n':
            (void)NEXT();
            break;
        default:
            // unknown: copy the directive through
            len = (int)(fmt - start);
            memcpy(tmp, start, len);
            break;
        }
        if (len >= (int)sizeof tmp) len = sizeof tmp - 1;
        for (int i = 0; i < len; i++) PUT(tmp[i]);
    }
    if (n) out[o < n ? o : n - 1] = 0;
    return (int)o;
#undef PUT
#undef NEXT
}

int guest_vformat(CPU *c, char *out, size_t n, const char *fmt, int first) { return vformat(c, out, n, fmt, first, 0); }
int guest_vformat_va(CPU *c, char *out, size_t n, const char *fmt, uint64_t va_list_ptr) { return vformat(c, out, n, fmt, 0, va_list_ptr); }
