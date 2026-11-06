// glsl_msl: GLSL ES 1.00 / 3.00 shaders (as gl4es writes them) to Metal Shading
// Language, by rewriting the source rather than compiling it.
//
// Every global of the GLSL shader (inputs, varyings, outputs, plain globals, its
// samplers) becomes a member of one struct, and every function a member
// function, so the shader's code runs unchanged in form; uniforms live in a
// struct in a constant buffer and are reached through a pointer. Types,
// constructors and built-in functions are renamed to MSL's or to small helpers.
// The vertex and fragment entry points are written at link time, when the
// attribute locations are known.
//
// Part of mtlgles (spike, issue #288 item 5c). BSD-3-Clause.
#pragma once
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

enum { MG_VERTEX = 0, MG_FRAGMENT = 1 };

// one uniform leaf as OpenGL reports it: a basic type or an array of one
typedef struct mg_uniform {
    char name[96];      // "a", "s.m", "s[2].m" (arrays of a basic type without "[0]")
    unsigned gltype;    // GL_FLOAT_VEC4, GL_SAMPLER_2D, ...
    int count;          // array length (1: not an array)
    int offset, stride; // in the stage's uniform buffer (samplers: -1)
    int sampler_slot;   // samplers: the shader's texture slot (else -1)
} mg_uniform;

typedef struct mg_attrib {
    char name[64];
    unsigned gltype;
} mg_attrib;

typedef struct mg_shader mg_shader;

// NULL on a source it cannot take; the reason in log
mg_shader *mg_parse(const char *src, int stage, char *log, size_t log_size);
void mg_free(mg_shader *s);

int mg_uniform_count(const mg_shader *s);
const mg_uniform *mg_uniform_at(const mg_shader *s, int i);
int mg_uniform_size(const mg_shader *s);     // the stage's uniform buffer in bytes
int mg_attrib_count(const mg_shader *s);     // vertex shaders' inputs
const mg_attrib *mg_attrib_at(const mg_shader *s, int i);
int mg_sampler_cube(const mg_shader *s, int slot);   // the slot is a cube map

// The whole Metal source of a program: helpers, both stages' structs and the
// entry points mg_vmain and mg_fmain. attr_loc[i] is the location of the
// vertex shader's attribute i. Vertex buffers sit at buffer(0..15), the
// vertex stage's uniforms at buffer(16), the fragment stage's at buffer(16);
// a texture slot n at texture(n) and, when depth, texture(16 + n), sampler(n).
// malloc'd; NULL with the reason in log.
char *mg_link(const mg_shader *vs, const mg_shader *fs, const int *attr_loc, char *log, size_t log_size);

#ifdef __cplusplus
}
#endif
