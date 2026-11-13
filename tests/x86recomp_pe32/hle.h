// The test runtime's side of recomp.py's contract for a PE32: imports are
// called through the IAT, which the loader fills with magic addresses.
#pragma once
#include "cpu.h"
#include <stdio.h>
#include <stdlib.h>

#define ARG(i) rd32(c->r[ESP] + 4 + 4 * (i))
#define G(a) ((char *)(M + (uint32_t)(a)))

void hle_call(CPU *c, int import_index);

typedef struct gen_program {
    const char *name;
    const uint32_t *fn_addr;
    const guest_fn *fn_ptr;
    unsigned fn_count;
    const char *const *import_names;
    const uint32_t *import_got, *import_plt;
    unsigned import_count;
    const uint32_t *copy_addr;
    const char *const *copy_names;
    unsigned copy_count;
    const char *const *obj_names;
    const uint32_t *obj_addr;
    unsigned obj_count;
    const uint32_t *copy_size;
    const char *const *mod_file;
    const uint32_t *mod_base, *mod_init;
    unsigned mod_count;
    const char *const *export_names;
    const uint32_t *export_addr;
    unsigned export_count;
    const uint32_t *reloc_addr;
    const uint8_t *reloc_type;
    const char *const *reloc_sym;
    unsigned reloc_count;
} gen_program;
