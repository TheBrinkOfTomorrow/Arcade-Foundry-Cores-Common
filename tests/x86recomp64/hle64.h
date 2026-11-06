// Host-side stand-ins for the Windows libraries a 64-bit program imports.
// A handler is entered the way the import would be: the return address at
// rsp, the first four integer arguments in rcx, rdx, r8, r9 (floating-point
// ones in xmm0-3), the rest above the 32-byte shadow space. hle_call() pops
// the return address.
#pragma once
#include "cpu64.h"
#include <stdio.h>
#include <stdlib.h>

#ifdef __cplusplus
extern "C" {
#endif

static inline uint64_t hle_arg(CPU *c, int i) {
    switch (i) {
    case 0: return c->r[RCX];
    case 1: return c->r[RDX];
    case 2: return c->r[R8];
    case 3: return c->r[R9];
    default: return rd64(c->r[RSP] + 8 + 8 * (uint64_t)i);
    }
}
#define ARG(i) hle_arg(c, (i))
#define ARGP(i) ((void *)(uintptr_t)hle_arg(c, (i)))
#define ARGS(i) ((char *)(uintptr_t)hle_arg(c, (i)))
#define ARGF(i) ((i) < 4 ? c->xmm[(i)].f[0] : u2f((uint32_t)hle_arg(c, (i))))
#define ARGD(i) ((i) < 4 ? c->xmm[(i)].d[0] : u2d(hle_arg(c, (i))))
#define RET(v) do { c->r[RAX] = (uint64_t)(v); return; } while (0)
#define RETP(p) do { c->r[RAX] = (uint64_t)(uintptr_t)(p); return; } while (0)
#define RETF(v) do { c->xmm[0].f[0] = (float)(v); return; } while (0)
#define RETD(v) do { c->xmm[0].d[0] = (double)(v); return; } while (0)

typedef void (*hle_fn)(CPU *c);
typedef struct { const char *dll; const char *name; hle_fn fn; } hle_entry;
typedef struct { const char *dll; const char *name; void *object; } hle_object_entry;

void hle_call(CPU *c, int import_index);
void hle_register(const hle_entry *table);                 // {NULL,NULL,NULL} ends it
void hle_register_objects(const hle_object_entry *table);  // data imports (_iob, __initenv)
hle_fn hle_find(const char *dll, const char *name);
uint64_t hle_import_address(const char *dll, const char *name);  // a callable guest address for a host function, or 0
uint64_t magic_register(hle_fn fn, const char *name);     // a guest address that dispatches to fn
extern int hle_verbose;
extern int hle_trace;

// the program and its threads
extern const gen_program64 *hle_main_program;
uint64_t guest_call(CPU *c, uint64_t addr, int nargs, const uint64_t *args);  // host -> guest, Win64 ABI
uint64_t teb_create(CPU *c, uint64_t stack_base, uint64_t stack_size);         // a TEB for this thread, gs_base set
void teb_set_last_error(CPU *c, uint32_t e);
uint32_t teb_get_last_error(CPU *c);
uint64_t guest_stack_alloc(uint64_t size);   // returns the top
int load_program(const gen_program64 *p, const char *path);   // map, relocate, bind imports, TLS
int run_program(const gen_program64 *p, int argc, char **argv);  // the entry point on this thread; its exit code

// formatted output with Win64 varargs: conversions read ARG(first), ARG(first+1), ...
int guest_vformat(CPU *c, char *out, size_t n, const char *fmt, int first);
// the same from a Win64 va_list (a pointer to the first argument on the stack)
int guest_vformat_va(CPU *c, char *out, size_t n, const char *fmt, uint64_t va_list_ptr);

// the stand-in libraries
void win64_register_kernel32(void);
void win64_register_msvcrt(void);

#ifdef __cplusplus
}
#endif
