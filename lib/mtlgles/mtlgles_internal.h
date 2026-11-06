// mtlgles internals, shared by its source files. Manual retain/release
// (built with -fno-objc-arc). BSD-3-Clause.
#pragma once
#import <Metal/Metal.h>
#include "glsl_msl.h"
#include "mtlgles.h"
#include "mtlgles_enums.h"
#include <stdatomic.h>
#include <pthread.h>

#define MG_MAX_ATTRIBS 16
#define MG_MAX_UNITS 16
#define MG_MAX_COLOR 4
#define MG_UNIFORM_BUF 16
#define MG_DEPTHMASK_BUF 17

// an attribute array in a buffer object that Metal cannot read as it is
// (unnormalised integers, a stride not a multiple of 4), repacked as floats
typedef struct mg_repack {
    size_t ptr, stride;    // the attribute's offset and stride in the buffer
    GLint size;
    GLenum type;
    GLboolean normalized;
    uint64_t gen;          // the buffer's contents it was made from
    int nverts;
    id<MTLBuffer> mb;
    struct mg_repack *next;
} mg_repack;

typedef struct mg_buffer {
    GLuint name;
    id<MTLBuffer> mb;      // shared storage: the CPU's copy is the GPU's
    size_t size;
    GLenum usage;
    uint64_t last_use;     // the command buffer that last read or wrote it
    int mapped;
    size_t map_off, map_len;
    uint64_t gen;          // counts changes to the contents (the repacks' key)
    mg_repack *repacks;
} mg_buffer;

typedef struct mg_texture {
    GLuint name;
    GLenum target;         // GL_TEXTURE_2D or GL_TEXTURE_CUBE_MAP
    id<MTLTexture> mt;
    int w, h, levels;
    GLenum internal;       // as asked (for the swizzles and queries)
    MTLPixelFormat fmt;
    int depth, stencil;
    int block;             // compressed (BC): bytes per 4x4 block, else 0
    GLenum min_filter, mag_filter, wrap_s, wrap_t;
    float min_lod, max_lod;
    int base_level, max_level;
    uint64_t last_use;
    // the sampler last made for it (owned by the context's dictionary) and its key
    id<MTLSamplerState> smp;
    struct mg_sampler_key { GLenum min_filter, mag_filter, wrap_s, wrap_t; int mip; float min_lod, max_lod; } smp_key;
} mg_texture;

typedef struct mg_renderbuffer {
    GLuint name;
    id<MTLTexture> mt;
    int w, h;
    GLenum internal;
    MTLPixelFormat fmt;
} mg_renderbuffer;

typedef struct mg_attachment {
    GLenum type;           // GL_TEXTURE, GL_RENDERBUFFER or GL_NONE
    GLuint name;
    int level, face;
} mg_attachment;

typedef struct mg_framebuffer {
    GLuint name;
    mg_attachment color[MG_MAX_COLOR], depth, stencil;
    GLenum draw_buffers[MG_MAX_COLOR];
    GLenum read_buffer;
} mg_framebuffer;

typedef struct mg_shader_obj {
    GLuint name;
    GLenum type;
    char *source;
    mg_shader *parsed;
    char log[1024];
    int compiled, delete_pending, refs;
} mg_shader_obj;

// a uniform as a program reports it: both stages' places
typedef struct mg_prog_uniform {
    char name[100];        // as glGetActiveUniform gives it ("a[0]" for arrays)
    char base[96];         // without [0]
    GLenum type;
    int count;
    int loc;               // the first element's location
    int off[2], stride[2]; // per stage (-1: not in it)
    int slot[2];           // samplers: the stage's texture slot (-1: none)
} mg_prog_uniform;

typedef struct mg_program {
    GLuint name;
    GLuint shaders[2];     // vertex, fragment
    int linked, delete_pending;
    char log[4096];
    // after linking
    id<MTLFunction> vfn, ffn;
    struct mg_compile *job;    // the Metal compile still running (nil: done or none)
    mg_prog_uniform *uni;
    int nuni, nloc;
    int *loc_uni, *loc_elem;   // location -> uniform, element
    uint8_t *ub[2];            // the stages' uniform buffers (CPU copies)
    int ub_size[2];
    int ub_dirty[2];
    int unit[2][MG_MAX_UNITS]; // per stage and slot: the texture unit (glUniform1i)
    int nslot[2];
    int slot_cube[2][MG_MAX_UNITS];
    char attr_name[MG_MAX_ATTRIBS][64];
    GLenum attr_type[MG_MAX_ATTRIBS];
    int attr_loc[MG_MAX_ATTRIBS];
    int nattr;
    struct { char name[64]; int loc; } bound[MG_MAX_ATTRIBS];  // glBindAttribLocation
    int nbound;
    NSMutableDictionary *pipelines;   // key (NSData) -> id<MTLRenderPipelineState>
} mg_program;

typedef struct mg_attrib_state {
    int enabled;
    GLint size;
    GLenum type;
    GLboolean normalized;
    GLsizei stride;
    const void *ptr;       // an offset when buffer is set
    GLuint buffer;
    float value[4];        // glVertexAttrib4f, for a disabled array
} mg_attrib_state;

typedef struct mg_sync {
    uint64_t serial;
} mg_sync;

// an occlusion query: the samples it counted, one visibility result slot per
// render pass it spanned
typedef struct mg_query {
    GLuint name;
    GLenum target;
    unsigned *slots;
    int nslots, cap;
    uint64_t serial;   // the command buffer of its last slot
} mg_query;

// objects by name: a growing array of pointers
typedef struct mg_table {
    void **p;
    GLuint n, cap, next;
} mg_table;

struct mtlgles_context {
    id<MTLDevice> dev;
    id<MTLCommandQueue> queue;
    id<MTLCommandBuffer> cmd;         // being recorded (nil: none yet)
    id<MTLRenderCommandEncoder> enc;  // the open render pass (nil: none)
    id<MTLBlitCommandEncoder> blit;   // the open blit pass (nil: none)
    // uploads to textures the current command buffer has not used: their own
    // command buffer, committed first, so they need not end a render pass
    id<MTLCommandBuffer> upload_cmd;
    id<MTLBlitCommandEncoder> upload_blit;
    uint64_t serial;                  // the command buffer being recorded
    _Atomic uint64_t done;            // the last one the GPU finished
    pthread_mutex_t done_lock;
    pthread_cond_t done_cond;

    // streaming memory for client arrays, indices and big uniform sets
    id<MTLBuffer> stream;
    size_t stream_off, stream_size;

    mg_table buffers, textures, renderbuffers, framebuffers, shaders, programs, queries;
    GLuint bound_array, bound_element, bound_pack, bound_unpack, bound_copy_read, bound_copy_write;
    GLuint bound_rb, draw_fb, read_fb, program;
    int active_unit;
    GLuint unit_2d[MG_MAX_UNITS], unit_cube[MG_MAX_UNITS];
    mg_attrib_state attr[MG_MAX_ATTRIBS];

    // fixed state
    int blend, cull, depth_test, stencil_test, scissor_test, poly_offset, discard, dither;
    GLenum blend_src_rgb, blend_dst_rgb, blend_src_a, blend_dst_a, blend_eq_rgb, blend_eq_a;
    float blend_color[4];
    GLboolean color_mask[4], depth_mask;
    GLenum depth_func, cull_face, front_face;
    float depth_near, depth_far;
    float clear_color[4], clear_depth;
    GLint clear_stencil;
    struct { GLenum func, fail, zfail, zpass; GLint ref; GLuint mask, write; } stencil[2];
    GLint viewport[4], scissor[4];
    float poly_factor, poly_units;
    GLint pack_align, unpack_align, unpack_row_length;
    GLenum error;

    // the open pass's target, to tell when a draw needs another
    GLuint pass_fb;
    int pass_w, pass_h;
    MTLPixelFormat pass_color[MG_MAX_COLOR], pass_depth, pass_stencil;
    int pass_ncolor;
    // a clear asked for before the pass began: done as its load action
    GLbitfield pending_clear;
    float pending_color[4], pending_depth;
    GLint pending_stencil;
    GLuint pending_fb;

    // occlusion queries: every pass gets the visibility buffer once there is one
    id<MTLBuffer> vis_buf;
    unsigned vis_next;                // the next slot
    mg_query *cur_query;              // the active one (NULL: none)
    int pass_vis;                     // the open pass has the visibility buffer
    uint64_t vis_pass;                // the pass (n_passes) counting into the active query's slot (0: none)

    // what the open pass has bound already
    id<MTLRenderPipelineState> cur_pipe;
    id<MTLDepthStencilState> cur_ds;
    GLuint cur_ub_prog;

    // caches
    NSMutableDictionary *ds_states;      // key -> id<MTLDepthStencilState>
    NSMutableDictionary *samplers;       // key -> id<MTLSamplerState>
    id<MTLTexture> dummy_2d, dummy_cube;
    id<MTLLibrary> util_lib;             // clears and scaled framebuffer blits
    NSMutableDictionary *blit_pipes;
    NSMutableDictionary *clear_pipes;

    // the default framebuffer (name 0): a small target, the game draws to its own
    id<MTLTexture> fb0_color, fb0_depth;
    id<MTLBuffer> depth_copy;            // depth+stencil to depth copies go through it

    // counters for mtlgles_stats
    uint64_t n_draws, n_pipes, n_passes, n_cmds, n_stream_bytes, n_uploads, n_waits, n_repacks;
    double t_gl;
    int trace_depth;                     // MTLGLES_TRACE_DEPTH: depth attachments and sampler slots
    int bc;                              // the device samples BC1-3 (DXT1/3/5): S3TC is offered
};

extern __thread mtlgles_context *mg_cur;
void mg_log(const char *fmt, ...);
void mg_error(GLenum e);

// mtlgles.m
void mg_end_pass(mtlgles_context *c);
void mg_end_blit(mtlgles_context *c);
id<MTLCommandBuffer> mg_cmd(mtlgles_context *c);
id<MTLBlitCommandEncoder> mg_blit(mtlgles_context *c);
id<MTLBlitCommandEncoder> mg_upload_blit(mtlgles_context *c);
void mg_flush(mtlgles_context *c);
void mg_defer(mtlgles_context *c, id obj);   // release once the GPU is past the current command buffer
void mg_wait(mtlgles_context *c, uint64_t serial);
int mg_busy(mtlgles_context *c, uint64_t serial);
size_t mg_stream(mtlgles_context *c, const void *data, size_t size, size_t align, id<MTLBuffer> *buf);
void *mg_get(mg_table *t, GLuint name);
GLuint mg_gen(mg_table *t, void *obj);
void mg_put(mg_table *t, GLuint name, void *obj);
void mg_del(mg_table *t, GLuint name);

// mtlgles_objects.m
mg_texture *mg_tex_bound(mtlgles_context *c, GLenum target);
int mg_fb_size(mtlgles_context *c, GLuint fb, int *w, int *h);
id<MTLTexture> mg_fb_color(mtlgles_context *c, GLuint fb, int i, int *level, int *slice);
id<MTLTexture> mg_fb_depth(mtlgles_context *c, GLuint fb, int stencil, int *level);
id<MTLSamplerState> mg_sampler_for(mtlgles_context *c, mg_texture *t);
void *mg_objects_proc(const char *name);
void mg_objects_init(mtlgles_context *c);

// mtlgles_program.m
mg_program *mg_prog(mtlgles_context *c, GLuint name);
int mg_prog_functions(mg_program *p);      // waits for the program's Metal compile
void mg_prog_cancel(mg_program *p);        // drops a compile still running
void *mg_program_proc(const char *name);

// mtlgles_draw.m
void *mg_draw_proc(const char *name);
void mg_draw_init(mtlgles_context *c);
void mg_resolve_clear(mtlgles_context *c);
void mg_query_free(mtlgles_context *c);
