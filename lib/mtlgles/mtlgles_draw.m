// mtlgles: vertex arrays, render passes, clears and draws.
// BSD-3-Clause.
#include "mtlgles_internal.h"
#include "af_debug.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define C mtlgles_context *c = mg_cur; if (!c) return
#define CR(v) mtlgles_context *c = mg_cur; if (!c) return v

// ---- vertex attributes --------------------------------------------------------------------------
static void glEnableVertexAttribArray_(GLuint i) { C; if (i < MG_MAX_ATTRIBS) c->attr[i].enabled = 1; }
static void glDisableVertexAttribArray_(GLuint i) { C; if (i < MG_MAX_ATTRIBS) c->attr[i].enabled = 0; }
static void glVertexAttribPointer_(GLuint i, GLint size, GLenum type, GLboolean norm, GLsizei stride, const void *ptr)
{
    C;
    if (i >= MG_MAX_ATTRIBS) return mg_error(GL_INVALID_VALUE);
    mg_attrib_state *a = &c->attr[i];
    a->size = size, a->type = type, a->normalized = norm, a->stride = stride, a->ptr = ptr;
    a->buffer = c->bound_array;
}
static void attrib_value(GLuint i, float x, float y, float z, float w)
{
    C;
    if (i >= MG_MAX_ATTRIBS) return;
    float *v = c->attr[i].value;
    v[0] = x, v[1] = y, v[2] = z, v[3] = w;
}
static void glVertexAttrib1f_(GLuint i, GLfloat x) { attrib_value(i, x, 0, 0, 1); }
static void glVertexAttrib2f_(GLuint i, GLfloat x, GLfloat y) { attrib_value(i, x, y, 0, 1); }
static void glVertexAttrib3f_(GLuint i, GLfloat x, GLfloat y, GLfloat z) { attrib_value(i, x, y, z, 1); }
static void glVertexAttrib4f_(GLuint i, GLfloat x, GLfloat y, GLfloat z, GLfloat w) { attrib_value(i, x, y, z, w); }
static void glVertexAttrib1fv_(GLuint i, const GLfloat *v) { attrib_value(i, v[0], 0, 0, 1); }
static void glVertexAttrib2fv_(GLuint i, const GLfloat *v) { attrib_value(i, v[0], v[1], 0, 1); }
static void glVertexAttrib3fv_(GLuint i, const GLfloat *v) { attrib_value(i, v[0], v[1], v[2], 1); }
static void glVertexAttrib4fv_(GLuint i, const GLfloat *v) { attrib_value(i, v[0], v[1], v[2], v[3]); }
static void glGetVertexAttribiv_(GLuint i, GLenum p, GLint *v)
{
    C;
    if (i >= MG_MAX_ATTRIBS) return mg_error(GL_INVALID_VALUE);
    mg_attrib_state *a = &c->attr[i];
    switch (p) {
    case GL_VERTEX_ATTRIB_ARRAY_ENABLED: *v = a->enabled; break;
    case GL_VERTEX_ATTRIB_ARRAY_SIZE: *v = a->size; break;
    case GL_VERTEX_ATTRIB_ARRAY_STRIDE: *v = a->stride; break;
    case GL_VERTEX_ATTRIB_ARRAY_TYPE: *v = (GLint)a->type; break;
    case GL_VERTEX_ATTRIB_ARRAY_NORMALIZED: *v = a->normalized; break;
    case GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING: *v = (GLint)a->buffer; break;
    case GL_CURRENT_VERTEX_ATTRIB: for (int k = 0; k < 4; k++) v[k] = (GLint)a->value[k]; break;
    default: mg_error(GL_INVALID_ENUM);
    }
}
static void glGetVertexAttribfv_(GLuint i, GLenum p, GLfloat *v)
{
    C;
    if (i >= MG_MAX_ATTRIBS) return mg_error(GL_INVALID_VALUE);
    if (p == GL_CURRENT_VERTEX_ATTRIB) {
        memcpy(v, c->attr[i].value, 16);
        return;
    }
    GLint iv[4];
    glGetVertexAttribiv_(i, p, iv);
    *v = (GLfloat)iv[0];
}
static void glGetVertexAttribPointerv_(GLuint i, GLenum p, void **v)
{
    (void)p;
    C;
    if (i < MG_MAX_ATTRIBS) *v = (void *)c->attr[i].ptr;
}

// GL's (size, type, normalized) as a Metal vertex format, or Invalid (converted to floats then)
static MTLVertexFormat vertex_format(GLint size, GLenum type, GLboolean norm)
{
    static const MTLVertexFormat f32[] = {MTLVertexFormatFloat, MTLVertexFormatFloat2, MTLVertexFormatFloat3, MTLVertexFormatFloat4};
    static const MTLVertexFormat ub_n[] = {MTLVertexFormatUCharNormalized, MTLVertexFormatUChar2Normalized,
                                           MTLVertexFormatUChar3Normalized, MTLVertexFormatUChar4Normalized};
    static const MTLVertexFormat b_n[] = {MTLVertexFormatCharNormalized, MTLVertexFormatChar2Normalized,
                                          MTLVertexFormatChar3Normalized, MTLVertexFormatChar4Normalized};
    static const MTLVertexFormat us_n[] = {MTLVertexFormatUShortNormalized, MTLVertexFormatUShort2Normalized,
                                           MTLVertexFormatUShort3Normalized, MTLVertexFormatUShort4Normalized};
    static const MTLVertexFormat s_n[] = {MTLVertexFormatShortNormalized, MTLVertexFormatShort2Normalized,
                                          MTLVertexFormatShort3Normalized, MTLVertexFormatShort4Normalized};
    static const MTLVertexFormat h[] = {MTLVertexFormatHalf, MTLVertexFormatHalf2, MTLVertexFormatHalf3, MTLVertexFormatHalf4};
    if (size < 1 || size > 4) return MTLVertexFormatInvalid;
    switch (type) {
    case GL_FLOAT: return f32[size - 1];
    case GL_HALF_FLOAT: case GL_HALF_FLOAT_OES: return h[size - 1];
    case GL_UNSIGNED_BYTE: return norm ? ub_n[size - 1] : MTLVertexFormatInvalid;
    case GL_BYTE: return norm ? b_n[size - 1] : MTLVertexFormatInvalid;
    case GL_UNSIGNED_SHORT: return norm ? us_n[size - 1] : MTLVertexFormatInvalid;
    case GL_SHORT: return norm ? s_n[size - 1] : MTLVertexFormatInvalid;
    }
    return MTLVertexFormatInvalid;
}
static int type_size(GLenum type)
{
    switch (type) {
    case GL_BYTE: case GL_UNSIGNED_BYTE: return 1;
    case GL_SHORT: case GL_UNSIGNED_SHORT: case GL_HALF_FLOAT: case GL_HALF_FLOAT_OES: return 2;
    }
    return 4;
}
static float read_comp(const uint8_t *p, GLenum type, GLboolean norm)
{
    switch (type) {
    case GL_UNSIGNED_BYTE: return norm ? *p / 255.0f : *p;
    case GL_BYTE: return norm ? *(const int8_t *)p / 127.0f : *(const int8_t *)p;
    case GL_UNSIGNED_SHORT: return norm ? *(const uint16_t *)p / 65535.0f : *(const uint16_t *)p;
    case GL_SHORT: return norm ? *(const int16_t *)p / 32767.0f : *(const int16_t *)p;
    case GL_INT: return (float)*(const int32_t *)p;
    case GL_UNSIGNED_INT: return (float)*(const uint32_t *)p;
    case GL_FIXED: return *(const int32_t *)p / 65536.0f;
    }
    return *(const float *)p;
}

// ---- passes -------------------------------------------------------------------------------------
// (the depth and stencil formats mtlgles makes)
static int color_renderable(MTLPixelFormat f)
{
    return f != MTLPixelFormatDepth16Unorm && f != MTLPixelFormatDepth32Float && f != MTLPixelFormatStencil8 &&
           f != MTLPixelFormatDepth32Float_Stencil8 && f != MTLPixelFormatX32_Stencil8;
}

static void begin_pass_fb(mtlgles_context *c, GLuint fb)
{
    if (c->enc && c->pass_fb == fb && !(c->pending_clear && c->pending_fb == fb)) return;
    mg_end_pass(c);
    mg_end_blit(c);
    int w = 16, h = 16;
    mg_fb_size(c, fb, &w, &h);
    MTLRenderPassDescriptor *d = [MTLRenderPassDescriptor renderPassDescriptor];
    int clear = c->pending_clear && c->pending_fb == fb;
    mg_framebuffer *f = mg_get(&c->framebuffers, fb);
    c->pass_ncolor = 0;
    for (int i = 0; i < MG_MAX_COLOR; i++) {
        c->pass_color[i] = MTLPixelFormatInvalid;
        int level, slice;
        id<MTLTexture> t = mg_fb_color(c, fb, i, &level, &slice);
        if (f && f->draw_buffers[i] == GL_NONE && i > 0) t = nil;
        if (t && !color_renderable(t.pixelFormat)) {
            // a depth or stencil texture at a colour attachment: an incomplete
            // framebuffer to OpenGL, a failed assertion to Metal (OutRun 2 SP
            // in a race); the pass gets no colour target there
            static int told;
            if (told++ < 3) mg_log("mtlgles: framebuffer %u colour %d: texture %u is depth/stencil (format %d), not drawn to\n",
                                   fb, i, f ? f->color[i].name : 0, (int)t.pixelFormat);
            t = nil;
        }
        if (!t) continue;
        MTLRenderPassColorAttachmentDescriptor *a = d.colorAttachments[i];
        a.texture = t;
        a.level = (NSUInteger)level;
        a.slice = (NSUInteger)slice;
        a.storeAction = MTLStoreActionStore;
        if (clear && (c->pending_clear & GL_COLOR_BUFFER_BIT)) {
            a.loadAction = MTLLoadActionClear;
            a.clearColor = MTLClearColorMake(c->pending_color[0], c->pending_color[1], c->pending_color[2], c->pending_color[3]);
        } else {
            a.loadAction = MTLLoadActionLoad;
        }
        c->pass_color[i] = t.pixelFormat;
        c->pass_ncolor = i + 1;
    }
    int dl, sl;
    id<MTLTexture> dt = mg_fb_depth(c, fb, 0, &dl), st = mg_fb_depth(c, fb, 1, &sl);
    c->pass_depth = dt ? dt.pixelFormat : MTLPixelFormatInvalid;
    c->pass_stencil = st ? st.pixelFormat : MTLPixelFormatInvalid;
    if (dt) {
        d.depthAttachment.texture = dt;
        d.depthAttachment.level = (NSUInteger)dl;
        d.depthAttachment.storeAction = MTLStoreActionStore;
        d.depthAttachment.loadAction = clear && (c->pending_clear & GL_DEPTH_BUFFER_BIT) ? MTLLoadActionClear : MTLLoadActionLoad;
        d.depthAttachment.clearDepth = c->pending_depth;
    }
    if (st) {
        d.stencilAttachment.texture = st;
        d.stencilAttachment.level = (NSUInteger)sl;
        d.stencilAttachment.storeAction = MTLStoreActionStore;
        d.stencilAttachment.loadAction = clear && (c->pending_clear & GL_STENCIL_BUFFER_BIT) ? MTLLoadActionClear : MTLLoadActionLoad;
        d.stencilAttachment.clearStencil = (uint32_t)c->pending_stencil;
    }
    if (clear) c->pending_clear = 0;
    if (c->vis_buf) d.visibilityResultBuffer = c->vis_buf;
    c->enc = [[mg_cmd(c) renderCommandEncoderWithDescriptor:d] retain];
    c->pass_fb = fb;
    c->pass_w = w, c->pass_h = h;
    c->pass_vis = c->vis_buf != nil;
    c->vis_pass = 0;
    c->n_passes++;
}

static void begin_pass(mtlgles_context *c) { begin_pass_fb(c, c->draw_fb); }

// a clear no draw has followed yet: done now (before a read, copy or flush)
void mg_resolve_clear(mtlgles_context *c)
{
    if (!c->pending_clear) return;
    begin_pass_fb(c, c->pending_fb);
    mg_end_pass(c);
}

// ---- the utility library: clears inside a pass, scaled blits ------------------------------------
static const char *util_src =
    "#include <metal_stdlib>\n"
    "using namespace metal;\n"
    "struct CO { float4 pos [[position]]; float2 uv; };\n"
    "vertex CO mg_clear_v(uint id [[vertex_id]], constant float &z [[buffer(0)]]) {\n"
    "    float2 p = float2((id & 1) ? 3.0 : -1.0, (id & 2) ? 3.0 : -1.0);\n"
    "    CO o; o.pos = float4(p, z, 1.0); o.uv = p * 0.5 + 0.5; return o; }\n"
    "struct CF { float4 c0 [[color(0)]]; float4 c1 [[color(1)]]; float4 c2 [[color(2)]]; float4 c3 [[color(3)]]; };\n"
    "fragment CF mg_clear_f(constant float4 &col [[buffer(0)]]) { CF o; o.c0 = o.c1 = o.c2 = o.c3 = col; return o; }\n"
    "vertex CO mg_blit_v(uint id [[vertex_id]], constant float4 *r [[buffer(0)]]) {\n"
    "    float2 t = float2((id & 1) ? 1.0 : 0.0, (id & 2) ? 1.0 : 0.0);\n"
    "    CO o; o.pos = float4(mix(r[0].xy, r[0].zw, t), 0.0, 1.0); o.uv = mix(r[1].xy, r[1].zw, t); return o; }\n"
    "fragment float4 mg_blit_f(CO in [[stage_in]], texture2d<float> t [[texture(0)]], sampler s [[sampler(0)]]) {\n"
    "    return t.sample(s, in.uv, level(0.0)); }\n";

static id<MTLLibrary> util_lib(mtlgles_context *c)
{
    if (!c->util_lib) {
        NSError *e = nil;
        c->util_lib = [c->dev newLibraryWithSource:[NSString stringWithUTF8String:util_src] options:nil error:&e];
        if (!c->util_lib) mg_log("mtlgles: utility shaders: %s\n", e.localizedDescription.UTF8String);
    }
    return c->util_lib;
}

static MTLCompareFunction compare(GLenum f)
{
    switch (f) {
    case GL_NEVER: return MTLCompareFunctionNever;
    case GL_LESS: return MTLCompareFunctionLess;
    case GL_EQUAL: return MTLCompareFunctionEqual;
    case GL_LEQUAL: return MTLCompareFunctionLessEqual;
    case GL_GREATER: return MTLCompareFunctionGreater;
    case GL_NOTEQUAL: return MTLCompareFunctionNotEqual;
    case GL_GEQUAL: return MTLCompareFunctionGreaterEqual;
    }
    return MTLCompareFunctionAlways;
}
static MTLStencilOperation stencil_op(GLenum o)
{
    switch (o) {
    case GL_ZERO: return MTLStencilOperationZero;
    case GL_REPLACE: return MTLStencilOperationReplace;
    case GL_INCR: return MTLStencilOperationIncrementClamp;
    case GL_DECR: return MTLStencilOperationDecrementClamp;
    case GL_INVERT: return MTLStencilOperationInvert;
    case GL_INCR_WRAP: return MTLStencilOperationIncrementWrap;
    case GL_DECR_WRAP: return MTLStencilOperationDecrementWrap;
    }
    return MTLStencilOperationKeep;
}

static id<MTLDepthStencilState> depth_stencil(mtlgles_context *c, int depth_test, GLenum func, int write, int stencil)
{
    char key[160];
    int n = snprintf(key, sizeof key, "%d %x %d", depth_test, func, write);
    if (stencil)
        for (int i = 0; i < 2; i++)
            n += snprintf(key + n, sizeof key - (size_t)n, " %x %x %x %x %x %x", c->stencil[i].func, c->stencil[i].fail,
                          c->stencil[i].zfail, c->stencil[i].zpass, c->stencil[i].mask & 0xff, c->stencil[i].write & 0xff);
    NSString *k = [NSString stringWithUTF8String:key];
    id<MTLDepthStencilState> s = c->ds_states[k];
    if (s) return s;
    MTLDepthStencilDescriptor *d = [[MTLDepthStencilDescriptor alloc] init];
    d.depthCompareFunction = depth_test ? compare(func) : MTLCompareFunctionAlways;
    d.depthWriteEnabled = depth_test && write;
    if (stencil) {
        // OpenGL's front face is Metal's back (y is flipped)
        MTLStencilDescriptor *sd[2] = {[[MTLStencilDescriptor alloc] init], [[MTLStencilDescriptor alloc] init]};
        for (int i = 0; i < 2; i++) {
            sd[i].stencilCompareFunction = compare(c->stencil[i].func);
            sd[i].stencilFailureOperation = stencil_op(c->stencil[i].fail);
            sd[i].depthFailureOperation = stencil_op(c->stencil[i].zfail);
            sd[i].depthStencilPassOperation = stencil_op(c->stencil[i].zpass);
            sd[i].readMask = c->stencil[i].mask & 0xff;
            sd[i].writeMask = c->stencil[i].write & 0xff;
        }
        d.backFaceStencil = sd[0];
        d.frontFaceStencil = sd[1];
        [sd[0] release];
        [sd[1] release];
    }
    s = [c->dev newDepthStencilStateWithDescriptor:d];
    [d release];
    c->ds_states[k] = s;
    [s release];
    return s;
}

// the scissor box (or the whole target) as Metal wants it: inside the target
static MTLScissorRect scissor_rect(mtlgles_context *c)
{
    int x = 0, y = 0, w = c->pass_w, h = c->pass_h;
    if (c->scissor_test) {
        x = c->scissor[0], y = c->scissor[1], w = c->scissor[2], h = c->scissor[3];
        if (x < 0) w += x, x = 0;
        if (y < 0) h += y, y = 0;
        if (x > c->pass_w) x = c->pass_w;
        if (y > c->pass_h) y = c->pass_h;
        if (x + w > c->pass_w) w = c->pass_w - x;
        if (y + h > c->pass_h) h = c->pass_h - y;
        if (w < 0) w = 0;
        if (h < 0) h = 0;
    }
    return (MTLScissorRect){(NSUInteger)x, (NSUInteger)y, (NSUInteger)w, (NSUInteger)h};
}

// ---- occlusion queries: Metal visibility results ---------------------------------------------
// The open pass counts the samples its draws pass into a slot of the
// visibility buffer, a slot of the active query's own for each pass it spans
// (Metal writes a slot's count as the pass ends); the query's result is their
// sum once the GPU is done. OutRun 2 SP sizes its sun glare by the pixels of
// the sun a query finds visible.
#define MG_VIS_SLOTS 4096

static void query_count(mtlgles_context *c)
{
    mg_query *q = c->cur_query;
    if (!q || !c->enc || !c->pass_vis || c->vis_pass == c->n_passes) return;
    if (q->nslots == q->cap) {
        q->cap = q->cap ? q->cap * 2 : 4;
        q->slots = realloc(q->slots, sizeof *q->slots * (size_t)q->cap);
    }
    // the slot's last pass is far behind (a few queries a frame, thousands of slots)
    unsigned s = c->vis_next++ % MG_VIS_SLOTS;
    ((uint64_t *)c->vis_buf.contents)[s] = 0;
    q->slots[q->nslots++] = s;
    q->serial = c->serial;
    [c->enc setVisibilityResultMode:MTLVisibilityResultModeCounting offset:s * 8];
    c->vis_pass = c->n_passes;
}

static void query_pause(mtlgles_context *c)
{
    if (!c->enc || !c->vis_pass || c->vis_pass != c->n_passes) return;
    [c->enc setVisibilityResultMode:MTLVisibilityResultModeDisabled offset:0];
    c->vis_pass = 0;
}

static mg_query *query_obj(mtlgles_context *c, GLuint name) { return mg_get(&c->queries, name); }

static void glGenQueries_(GLsizei n, GLuint *out)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_query *q = calloc(1, sizeof *q);
        q->name = out[i] = mg_gen(&c->queries, q);
    }
}

static void glDeleteQueries_(GLsizei n, const GLuint *names)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_query *q = query_obj(c, names[i]);
        if (!q) continue;
        if (c->cur_query == q) query_pause(c), c->cur_query = NULL;
        mg_del(&c->queries, names[i]);
        free(q->slots), free(q);
    }
}

static GLboolean glIsQuery_(GLuint name) { CR(0); return query_obj(c, name) != NULL; }

static void glBeginQuery_(GLenum target, GLuint name)
{
    C;
    // MTLGLES_BOOLEAN_QUERIES: GL_SAMPLES_PASSED refused, as OpenGL ES does (Apple's
    // GLES on iOS answers the boolean queries only), to try that path on the Mac
    static int boolean_only = -1;
    if (boolean_only < 0) boolean_only = af_debug_env("MTLGLES_BOOLEAN_QUERIES") != NULL;
    if (target != GL_ANY_SAMPLES_PASSED && target != GL_ANY_SAMPLES_PASSED_CONSERVATIVE && (target != GL_SAMPLES_PASSED || boolean_only)) {
        mg_error(GL_INVALID_ENUM);
        return;
    }
    mg_query *q = query_obj(c, name);
    if (!q || c->cur_query) {
        mg_error(GL_INVALID_OPERATION);
        return;
    }
    if (!c->vis_buf) {
        c->vis_buf = [c->dev newBufferWithLength:MG_VIS_SLOTS * 8 options:MTLResourceStorageModeShared];
        memset(c->vis_buf.contents, 0, MG_VIS_SLOTS * 8);
    }
    if (c->enc && !c->pass_vis) mg_end_pass(c);   // a pass begun without the buffer
    q->target = target;
    q->nslots = 0;
    q->serial = 0;
    c->cur_query = q;
}

static void glEndQuery_(GLenum target)
{
    C;
    if (!c->cur_query || c->cur_query->target != target) {
        mg_error(GL_INVALID_OPERATION);
        return;
    }
    query_pause(c);
    c->cur_query = NULL;
}

static void glGetQueryiv_(GLenum target, GLenum p, GLint *v)
{
    C;
    if (p == GL_CURRENT_QUERY) *v = c->cur_query && c->cur_query->target == target ? (GLint)c->cur_query->name : 0;
    else mg_error(GL_INVALID_ENUM);
}

static void glGetQueryObjectuiv_(GLuint name, GLenum p, GLuint *v)
{
    C;
    mg_query *q = query_obj(c, name);
    if (!q || q == c->cur_query) {
        mg_error(GL_INVALID_OPERATION);
        return;
    }
    if (p == GL_QUERY_RESULT_AVAILABLE) {
        // a poll must see the result come: its command buffer goes to the GPU
        if (q->nslots && mg_busy(c, q->serial) && c->cmd && q->serial >= c->serial) mg_flush(c);
        *v = !q->nslots || !mg_busy(c, q->serial);
        return;
    }
    if (p != GL_QUERY_RESULT) {
        mg_error(GL_INVALID_ENUM);
        return;
    }
    if (q->nslots) mg_wait(c, q->serial);
    uint64_t n = 0;
    for (int i = 0; i < q->nslots; i++) n += ((const uint64_t *)c->vis_buf.contents)[q->slots[i]];
    *v = q->target == GL_SAMPLES_PASSED ? (GLuint)(n > 0xffffffffu ? 0xffffffffu : n) : n != 0;
}

void mg_query_free(mtlgles_context *c)
{
    for (GLuint i = 0; i < c->queries.n; i++) {
        mg_query *q = c->queries.p[i];
        if (q) free(q->slots), free(q);
    }
    free(c->queries.p);
    [c->vis_buf release];
}

// a clear inside an open pass, or one limited by the scissor or masks: drawn
static void clear_by_drawing(mtlgles_context *c, GLbitfield mask)
{
    begin_pass(c);
    query_pause(c);   // a clear passes no samples to a query
    int col = (mask & GL_COLOR_BUFFER_BIT) != 0;
    int dep = (mask & GL_DEPTH_BUFFER_BIT) && c->pass_depth != MTLPixelFormatInvalid && c->depth_mask;
    int ste = (mask & GL_STENCIL_BUFFER_BIT) && c->pass_stencil != MTLPixelFormatInvalid;
    int cm = col ? (c->color_mask[0] ? 1 : 0) | (c->color_mask[1] ? 2 : 0) | (c->color_mask[2] ? 4 : 0) | (c->color_mask[3] ? 8 : 0) : 0;
    char key[128];
    snprintf(key, sizeof key, "%d %d %d %d %d %d %d %d", (int)c->pass_color[0], (int)c->pass_color[1], (int)c->pass_color[2],
             (int)c->pass_color[3], (int)c->pass_depth, (int)c->pass_stencil, cm, dep);
    NSString *k = [NSString stringWithUTF8String:key];
    id<MTLRenderPipelineState> pipe = c->clear_pipes[k];
    if (!pipe) {
        id<MTLLibrary> lib = util_lib(c);
        MTLRenderPipelineDescriptor *d = [[MTLRenderPipelineDescriptor alloc] init];
        d.vertexFunction = [[lib newFunctionWithName:@"mg_clear_v"] autorelease];
        d.fragmentFunction = [[lib newFunctionWithName:@"mg_clear_f"] autorelease];
        for (int i = 0; i < MG_MAX_COLOR; i++) {
            d.colorAttachments[i].pixelFormat = c->pass_color[i];
            d.colorAttachments[i].writeMask = (MTLColorWriteMask)((cm & 1 ? MTLColorWriteMaskRed : 0) | (cm & 2 ? MTLColorWriteMaskGreen : 0) |
                                                                  (cm & 4 ? MTLColorWriteMaskBlue : 0) | (cm & 8 ? MTLColorWriteMaskAlpha : 0));
        }
        d.depthAttachmentPixelFormat = c->pass_depth;
        d.stencilAttachmentPixelFormat = c->pass_stencil;
        NSError *e = nil;
        pipe = [c->dev newRenderPipelineStateWithDescriptor:d error:&e];
        [d release];
        if (!pipe) {
            mg_log("mtlgles: clear pipeline: %s\n", e.localizedDescription.UTF8String);
            return;
        }
        c->clear_pipes[k] = pipe;
        [pipe release];
        c->n_pipes++;
    }
    id<MTLRenderCommandEncoder> e = c->enc;
    [e setRenderPipelineState:pipe];
    c->cur_pipe = nil;
    // depth: always pass, write the clear value; stencil: replace with the clear value
    MTLDepthStencilDescriptor *dd = [[MTLDepthStencilDescriptor alloc] init];
    dd.depthCompareFunction = MTLCompareFunctionAlways;
    dd.depthWriteEnabled = dep;
    if (ste) {
        MTLStencilDescriptor *s = [[MTLStencilDescriptor alloc] init];
        s.stencilCompareFunction = MTLCompareFunctionAlways;
        s.depthStencilPassOperation = MTLStencilOperationReplace;
        s.writeMask = c->stencil[0].write & 0xff;
        dd.frontFaceStencil = dd.backFaceStencil = s;
        [s release];
    }
    id<MTLDepthStencilState> ds = [c->dev newDepthStencilStateWithDescriptor:dd];
    [dd release];
    [e setDepthStencilState:ds];
    mg_defer(c, ds);   // the encoder keeps no reference to it
    c->cur_ds = nil;
    [e setStencilReferenceValue:(uint32_t)c->clear_stencil];
    [e setViewport:(MTLViewport){0, 0, (double)c->pass_w, (double)c->pass_h, 0, 1}];
    [e setScissorRect:scissor_rect(c)];
    [e setCullMode:MTLCullModeNone];
    [e setDepthBias:0 slopeScale:0 clamp:0];
    float z = c->clear_depth;
    [e setVertexBytes:&z length:4 atIndex:0];
    [e setFragmentBytes:c->clear_color length:16 atIndex:0];
    [e drawPrimitives:MTLPrimitiveTypeTriangleStrip vertexStart:0 vertexCount:4];
}

static void glClear_(GLbitfield mask)
{
    C;
    int w, h;
    mg_fb_size(c, c->draw_fb, &w, &h);
    int whole = !c->scissor_test ||
                (c->scissor[0] <= 0 && c->scissor[1] <= 0 && c->scissor[0] + c->scissor[2] >= w && c->scissor[1] + c->scissor[3] >= h);
    int masks = (!(mask & GL_COLOR_BUFFER_BIT) || (c->color_mask[0] && c->color_mask[1] && c->color_mask[2] && c->color_mask[3])) &&
                (!(mask & GL_DEPTH_BUFFER_BIT) || c->depth_mask) &&
                (!(mask & GL_STENCIL_BUFFER_BIT) || (c->stencil[0].write & 0xff) == 0xff);
    if (whole && masks) {
        // a load action of the next pass on this framebuffer (ending the open one)
        if (c->enc && c->pass_fb == c->draw_fb) mg_end_pass(c);
        if (c->pending_clear && c->pending_fb != c->draw_fb) mg_resolve_clear(c);   // the other target's clear first
        c->pending_fb = c->draw_fb;
        c->pending_clear |= mask;
        if (mask & GL_COLOR_BUFFER_BIT) memcpy(c->pending_color, c->clear_color, 16);
        if (mask & GL_DEPTH_BUFFER_BIT) c->pending_depth = c->clear_depth;
        if (mask & GL_STENCIL_BUFFER_BIT) c->pending_stencil = c->clear_stencil;
        return;
    }
    clear_by_drawing(c, mask);
}

// ---- draws --------------------------------------------------------------------------------------
static MTLBlendFactor blend_factor(GLenum f)
{
    switch (f) {
    case GL_ZERO: return MTLBlendFactorZero;
    case GL_ONE: return MTLBlendFactorOne;
    case GL_SRC_COLOR: return MTLBlendFactorSourceColor;
    case GL_ONE_MINUS_SRC_COLOR: return MTLBlendFactorOneMinusSourceColor;
    case GL_SRC_ALPHA: return MTLBlendFactorSourceAlpha;
    case GL_ONE_MINUS_SRC_ALPHA: return MTLBlendFactorOneMinusSourceAlpha;
    case GL_DST_ALPHA: return MTLBlendFactorDestinationAlpha;
    case GL_ONE_MINUS_DST_ALPHA: return MTLBlendFactorOneMinusDestinationAlpha;
    case GL_DST_COLOR: return MTLBlendFactorDestinationColor;
    case GL_ONE_MINUS_DST_COLOR: return MTLBlendFactorOneMinusDestinationColor;
    case GL_SRC_ALPHA_SATURATE: return MTLBlendFactorSourceAlphaSaturated;
    case GL_CONSTANT_COLOR: return MTLBlendFactorBlendColor;
    case GL_ONE_MINUS_CONSTANT_COLOR: return MTLBlendFactorOneMinusBlendColor;
    case GL_CONSTANT_ALPHA: return MTLBlendFactorBlendAlpha;
    case GL_ONE_MINUS_CONSTANT_ALPHA: return MTLBlendFactorOneMinusBlendAlpha;
    }
    return MTLBlendFactorOne;
}
static MTLBlendOperation blend_op(GLenum e)
{
    switch (e) {
    case GL_FUNC_SUBTRACT: return MTLBlendOperationSubtract;
    case GL_FUNC_REVERSE_SUBTRACT: return MTLBlendOperationReverseSubtract;
    case GL_MIN: return MTLBlendOperationMin;
    case GL_MAX: return MTLBlendOperationMax;
    }
    return MTLBlendOperationAdd;
}

// one attribute's source for this draw
typedef struct {
    MTLVertexFormat fmt;
    id<MTLBuffer> buf;
    size_t off, stride;
    int constant;
} vsrc;

// the pipeline for the program, the vertex layout and the blend and target state
static id<MTLRenderPipelineState> pipeline(mtlgles_context *c, mg_program *p, const vsrc *src)
{
    struct {
        uint32_t fmt[MG_MAX_ATTRIBS], stride[MG_MAX_ATTRIBS], constant;
        uint32_t color[MG_MAX_COLOR], depth, stencil;
        uint32_t blend, src_rgb, dst_rgb, src_a, dst_a, eq_rgb, eq_a, mask;
    } key;
    memset(&key, 0, sizeof key);
    for (int i = 0; i < p->nattr; i++) {
        key.fmt[i] = (uint32_t)src[i].fmt;
        key.stride[i] = (uint32_t)src[i].stride;
        key.constant |= (uint32_t)src[i].constant << i;
    }
    for (int i = 0; i < MG_MAX_COLOR; i++) key.color[i] = (uint32_t)c->pass_color[i];
    key.depth = (uint32_t)c->pass_depth, key.stencil = (uint32_t)c->pass_stencil;
    key.blend = (uint32_t)c->blend;
    if (c->blend) {
        key.src_rgb = c->blend_src_rgb, key.dst_rgb = c->blend_dst_rgb, key.src_a = c->blend_src_a;
        key.dst_a = c->blend_dst_a, key.eq_rgb = c->blend_eq_rgb, key.eq_a = c->blend_eq_a;
    }
    key.mask = (uint32_t)((c->color_mask[0] ? 1 : 0) | (c->color_mask[1] ? 2 : 0) | (c->color_mask[2] ? 4 : 0) |
                          (c->color_mask[3] ? 8 : 0) | (c->discard ? 16 : 0));
    NSData *k = [NSData dataWithBytes:&key length:sizeof key];
    id<MTLRenderPipelineState> ps = p->pipelines[k];
    if (ps) return ps;
    if (!mg_prog_functions(p)) return nil;
    MTLRenderPipelineDescriptor *d = [[MTLRenderPipelineDescriptor alloc] init];
    d.vertexFunction = p->vfn;
    d.fragmentFunction = p->ffn;
    MTLVertexDescriptor *vd = [MTLVertexDescriptor vertexDescriptor];
    for (int i = 0; i < p->nattr; i++) {
        int loc = p->attr_loc[i];
        vd.attributes[loc].format = src[i].fmt;
        vd.attributes[loc].offset = 0;
        vd.attributes[loc].bufferIndex = (NSUInteger)i;
        if (src[i].constant) {
            vd.layouts[i].stepFunction = MTLVertexStepFunctionConstant;
            vd.layouts[i].stepRate = 0;
            vd.layouts[i].stride = 16;
        } else {
            vd.layouts[i].stepFunction = MTLVertexStepFunctionPerVertex;
            vd.layouts[i].stride = src[i].stride;
        }
    }
    d.vertexDescriptor = vd;
    for (int i = 0; i < MG_MAX_COLOR; i++) {
        MTLRenderPipelineColorAttachmentDescriptor *a = d.colorAttachments[i];
        a.pixelFormat = c->pass_color[i];
        if (c->pass_color[i] == MTLPixelFormatInvalid) continue;
        a.writeMask = (MTLColorWriteMask)((c->color_mask[0] ? MTLColorWriteMaskRed : 0) | (c->color_mask[1] ? MTLColorWriteMaskGreen : 0) |
                                          (c->color_mask[2] ? MTLColorWriteMaskBlue : 0) | (c->color_mask[3] ? MTLColorWriteMaskAlpha : 0));
        a.blendingEnabled = c->blend;
        a.sourceRGBBlendFactor = blend_factor(c->blend_src_rgb);
        a.destinationRGBBlendFactor = blend_factor(c->blend_dst_rgb);
        a.sourceAlphaBlendFactor = blend_factor(c->blend_src_a);
        a.destinationAlphaBlendFactor = blend_factor(c->blend_dst_a);
        a.rgbBlendOperation = blend_op(c->blend_eq_rgb);
        a.alphaBlendOperation = blend_op(c->blend_eq_a);
    }
    d.depthAttachmentPixelFormat = c->pass_depth;
    d.stencilAttachmentPixelFormat = c->pass_stencil;
    d.rasterizationEnabled = !c->discard;
    NSError *e = nil;
    ps = [c->dev newRenderPipelineStateWithDescriptor:d error:&e];
    [d release];
    if (!ps) {
        mg_log("mtlgles: pipeline for program %u: %s\n", p->name * 2 + 1, e.localizedDescription.UTF8String);
        return nil;
    }
    p->pipelines[k] = ps;
    [ps release];
    c->n_pipes++;
    return ps;
}

// an attribute array in buffer b as floats, for vertices [0, need): the repack
// made from the buffer's current contents, or a new one of every vertex the
// buffer holds (NULL: the buffer holds fewer than need)
static mg_repack *repack(mtlgles_context *c, mg_buffer *b, const mg_attrib_state *a, size_t ptr, size_t stride, int need)
{
    size_t esize = (size_t)a->size * type_size(a->type);
    if (ptr + esize > b->size) return NULL;
    int nverts = (int)((b->size - ptr - esize) / stride + 1);
    if (nverts < need) return NULL;
    mg_repack *r = b->repacks;
    while (r && !(r->ptr == ptr && r->stride == stride && r->size == a->size && r->type == a->type && r->normalized == a->normalized))
        r = r->next;
    if (r && r->gen == b->gen) return r;
    if (!r) {
        r = calloc(1, sizeof *r);
        r->ptr = ptr, r->stride = stride, r->size = a->size, r->type = a->type, r->normalized = a->normalized;
        r->next = b->repacks;
        b->repacks = r;
    }
    if (r->mb) mg_defer(c, r->mb);   // draws made already keep the old one until done
    size_t out = (size_t)a->size * 4;
    r->mb = [c->dev newBufferWithLength:out * (size_t)nverts options:MTLResourceStorageModeShared];
    float *d = (float *)r->mb.contents;
    const uint8_t *base = (const uint8_t *)b->mb.contents + ptr;
    for (int v = 0; v < nverts; v++)
        for (int k = 0; k < a->size; k++)
            d[(size_t)v * a->size + k] = read_comp(base + stride * (size_t)v + (size_t)k * type_size(a->type), a->type, a->normalized);
    r->gen = b->gen, r->nverts = nverts;
    c->n_repacks++;
    return r;
}

// the vertices [first, first + count) of each array the program reads
static int vertex_sources(mtlgles_context *c, mg_program *p, int first, int count, vsrc *src)
{
    for (int i = 0; i < p->nattr; i++) {
        mg_attrib_state *a = &c->attr[p->attr_loc[i]];
        vsrc *s = &src[i];
        memset(s, 0, sizeof *s);
        if (!a->enabled) {
            s->constant = 1;
            s->fmt = MTLVertexFormatFloat4;
            s->off = mg_stream(c, a->value, 16, 16, &s->buf);
            continue;
        }
        int esize = a->size * type_size(a->type);
        size_t stride = a->stride ? (size_t)a->stride : (size_t)esize;
        MTLVertexFormat f = vertex_format(a->size, a->type, a->normalized);
        mg_buffer *b = a->buffer ? mg_get(&c->buffers, a->buffer) : NULL;
        const uint8_t *base = b && b->mb ? (const uint8_t *)b->mb.contents + (uintptr_t)a->ptr : (const uint8_t *)a->ptr;
        if (!base) return 0;
        int direct = b && b->mb && f != MTLVertexFormatInvalid && stride % 4 == 0 && ((uintptr_t)a->ptr % 4) == 0;
        if (direct) {
            // from the buffer object itself; first is added by the draw
            s->fmt = f;
            s->buf = b->mb;
            s->off = (size_t)(uintptr_t)a->ptr;
            s->stride = stride;
            b->last_use = c->serial;
            continue;
        }
        if (f != MTLVertexFormatInvalid && stride % 4 == 0) {
            // client memory: copied as it is, from `first` (from 0 when the
            // stream offset would go below the start of the stream)
            s->fmt = f;
            s->stride = stride;
            size_t bytes = stride * (size_t)(count - 1) + (size_t)esize;
            id<MTLBuffer> probe;
            size_t at = mg_stream(c, NULL, 0, 4, &probe);
            if (at >= stride * (size_t)first) {
                s->off = mg_stream(c, base + stride * (size_t)first, bytes, 4, &s->buf) - stride * (size_t)first;
                if (s->buf == probe) continue;   // else a new block began: copy from 0 below
            }
            s->off = mg_stream(c, base, bytes + stride * (size_t)first, 4, &s->buf);
            continue;
        }
        size_t out = (size_t)a->size * 4;
        static const MTLVertexFormat f32[] = {MTLVertexFormatFloat, MTLVertexFormatFloat2, MTLVertexFormatFloat3, MTLVertexFormatFloat4};
        if (b && b->mb) {
            // in a buffer object: repacked once per change to the buffer, all the
            // vertices it holds, and drawn from like a buffer (Sega Race TV's
            // arrays, repacked from 0 for every draw: 20-45 ms a frame)
            mg_repack *r = repack(c, b, a, (size_t)(uintptr_t)a->ptr, stride, first + count);
            if (r) {
                s->fmt = f32[a->size - 1];
                s->buf = r->mb;
                s->off = 0;
                s->stride = out;
                b->last_use = c->serial;
                continue;
            }
        }
        // repacked as floats from 0 (unnormalised integers, odd strides)
        int total = first + count;
        float *tmp = malloc(out * (size_t)total + 4);
        for (int v = 0; v < total; v++)
            for (int k = 0; k < a->size; k++)
                tmp[(size_t)v * a->size + k] = read_comp(base + stride * (size_t)v + (size_t)k * type_size(a->type), a->type, a->normalized);
        s->fmt = f32[a->size - 1];
        s->stride = out;
        s->off = mg_stream(c, tmp, out * (size_t)total, 4, &s->buf);
        free(tmp);
    }
    return 1;
}

// MTLGLES_TRACE_DEPTH: a sampler slot with no texture (the dummy, all 0) or a
// depth texture, each case once, with the framebuffer's own depth attachment
static void trace_slot(mtlgles_context *c, mg_program *p, int st, int slot, int unit, GLuint name, mg_texture *t)
{
    if (t && t->mt && !t->depth) return;
    static NSMutableSet *seen;
    if (!seen) seen = [[NSMutableSet alloc] init];
    NSString *k = [NSString stringWithFormat:@"%u %d %d %u %d", p->name, st, slot, name, (int)c->draw_fb];
    if ([seen containsObject:k]) return;
    [seen addObject:k];
    int dl;
    id<MTLTexture> att = mg_fb_depth(c, c->draw_fb, 0, &dl);
    if (!t || !t->mt)
        mg_log("mtlgles: program %u %s slot %d unit %d: texture %u %s (reads 0)\n", p->name, st ? "fragment" : "vertex",
               slot, unit, name, !name ? "none bound" : !t ? "unknown" : "without storage");
    else
        mg_log("mtlgles: program %u %s slot %d unit %d: depth texture %u %dx%d format %d%s (fb %u)\n", p->name,
               st ? "fragment" : "vertex", slot, unit, name, t->w, t->h, (int)t->fmt,
               att == t->mt ? ", also the pass's depth attachment" : "", c->draw_fb);
}

// A draw that samples a texture it also draws into (Primeval Hunt makes its
// blurred copies of a scene in one 2048x2048 texture from another part of it):
// the render pass would read the tiles it holds, not what is stored, and
// blocks come out wrong on a tile-based GPU. The pass is ended, the texture
// copied, the copy sampled instead, and a new pass loads where it left off.
// Only such draws pay for it.
static void feedback_copy(mtlgles_context *c, mg_program *p)
{
    c->feedback_src = c->feedback_copy = nil;
    id<MTLTexture> att[MG_MAX_COLOR];
    int natt = 0;
    for (int i = 0; i < MG_MAX_COLOR; i++) {
        int level, slice;
        id<MTLTexture> t = mg_fb_color(c, c->draw_fb, i, &level, &slice);
        if (t) att[natt++] = t;
    }
    if (!natt) return;
    for (int st = 0; st < 2; st++)
        for (int slot = 0; slot < p->nslot[st] && slot < MG_MAX_UNITS; slot++) {
            int unit = p->unit[st][slot];
            if (unit < 0 || unit >= MG_MAX_UNITS) unit = 0;
            if (p->slot_cube[st][slot]) continue;
            mg_texture *t = mg_get(&c->textures, c->unit_2d[unit]);
            if (!t || !t->mt) continue;
            for (int i = 0; i < natt; i++) {
                if (att[i] != t->mt) continue;
                if (!t->fb_copy || t->fb_copy.width != t->mt.width || t->fb_copy.height != t->mt.height ||
                    t->fb_copy.pixelFormat != t->mt.pixelFormat || t->fb_copy.mipmapLevelCount != t->mt.mipmapLevelCount) {
                    if (t->fb_copy) mg_defer(c, t->fb_copy);
                    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:t->mt.pixelFormat
                                                                                                 width:t->mt.width
                                                                                                height:t->mt.height
                                                                                             mipmapped:t->mt.mipmapLevelCount > 1];
                    d.mipmapLevelCount = t->mt.mipmapLevelCount;
                    d.usage = MTLTextureUsageShaderRead;
                    d.storageMode = MTLStorageModePrivate;
                    t->fb_copy = [c->dev newTextureWithDescriptor:d];
                }
                if (c->pending_clear && c->pending_fb == c->draw_fb) mg_resolve_clear(c);   // the copy has it
                mg_end_pass(c);
                [mg_blit(c) copyFromTexture:t->mt toTexture:t->fb_copy];
                c->feedback_src = t->mt;
                c->feedback_copy = t->fb_copy;
                return;
            }
        }
}

// everything a draw needs bound, then the vertex sources; 0: nothing to draw
static int prepare(mtlgles_context *c, GLenum mode, int first, int count, mg_program **out)
{
    mg_program *p = mg_prog(c, c->program);
    if (!p || !p->linked || count <= 0) return 0;
    feedback_copy(c, p);
    begin_pass(c);
    if (c->cur_query) query_count(c);
    else query_pause(c);
    id<MTLRenderCommandEncoder> e = c->enc;
    vsrc src[MG_MAX_ATTRIBS];
    if (!vertex_sources(c, p, first, count, src)) return 0;
    id<MTLRenderPipelineState> ps = pipeline(c, p, src);
    if (!ps) return 0;
    if (ps != c->cur_pipe) {
        [e setRenderPipelineState:ps];
        c->cur_pipe = ps;
    }
    for (int i = 0; i < p->nattr; i++) [e setVertexBuffer:src[i].buf offset:src[i].off atIndex:(NSUInteger)i];
    // fixed state
    int stencil = c->stencil_test && c->pass_stencil != MTLPixelFormatInvalid;
    id<MTLDepthStencilState> ds = depth_stencil(c, c->depth_test && c->pass_depth != MTLPixelFormatInvalid, c->depth_func,
                                                c->depth_mask, stencil);
    if (ds != c->cur_ds) {
        [e setDepthStencilState:ds];
        c->cur_ds = ds;
    }
    if (stencil) [e setStencilFrontReferenceValue:(uint32_t)c->stencil[1].ref backReferenceValue:(uint32_t)c->stencil[0].ref];
    // y is flipped: OpenGL's counter-clockwise front is clockwise here
    [e setFrontFacingWinding:c->front_face == GL_CCW ? MTLWindingClockwise : MTLWindingCounterClockwise];
    [e setCullMode:!c->cull ? MTLCullModeNone : c->cull_face == GL_FRONT ? MTLCullModeFront
                            : c->cull_face == GL_BACK ? MTLCullModeBack : MTLCullModeNone];
    if (c->cull && c->cull_face == GL_FRONT_AND_BACK) return 0;
    [e setViewport:(MTLViewport){(double)c->viewport[0], (double)c->viewport[1], (double)c->viewport[2],
                                 (double)c->viewport[3], c->depth_near, c->depth_far}];
    MTLScissorRect sr = scissor_rect(c);
    if (!sr.width || !sr.height) return 0;
    [e setScissorRect:sr];
    // GL_POLYGON_OFFSET_FILL offsets filled triangles only; Metal biases points and lines too
    if (c->poly_offset && mode >= GL_TRIANGLES) [e setDepthBias:c->poly_units slopeScale:c->poly_factor clamp:0];
    else [e setDepthBias:0 slopeScale:0 clamp:0];
    [e setBlendColorRed:c->blend_color[0] green:c->blend_color[1] blue:c->blend_color[2] alpha:c->blend_color[3]];
    // uniforms
    for (int st = 0; st < 2; st++) {
        if (p->ub_size[st] <= 4096) {
            if (st == 0) [e setVertexBytes:p->ub[0] length:(NSUInteger)p->ub_size[0] atIndex:MG_UNIFORM_BUF];
            else [e setFragmentBytes:p->ub[1] length:(NSUInteger)p->ub_size[1] atIndex:MG_UNIFORM_BUF];
        } else {
            id<MTLBuffer> b;
            size_t off = mg_stream(c, p->ub[st], (size_t)p->ub_size[st], 256, &b);
            if (st == 0) [e setVertexBuffer:b offset:off atIndex:MG_UNIFORM_BUF];
            else [e setFragmentBuffer:b offset:off atIndex:MG_UNIFORM_BUF];
        }
    }
    // textures: each sampler slot from its unit
    for (int st = 0; st < 2; st++) {
        uint32_t depthmask = 0;
        for (int slot = 0; slot < p->nslot[st] && slot < MG_MAX_UNITS; slot++) {
            int unit = p->unit[st][slot];
            if (unit < 0 || unit >= MG_MAX_UNITS) unit = 0;
            int cube = p->slot_cube[st][slot];
            mg_texture *t = mg_get(&c->textures, cube ? c->unit_cube[unit] : c->unit_2d[unit]);
            id<MTLTexture> tex = t && t->mt ? t->mt : nil;
            if (tex && tex == c->feedback_src) tex = c->feedback_copy;
            id<MTLSamplerState> smp = t && t->mt ? mg_sampler_for(c, t) : nil;
            if (t && t->mt) t->last_use = c->serial;
            if (!smp) {
                static mg_texture none = {0};
                if (!none.min_filter) none.min_filter = none.mag_filter = GL_NEAREST, none.wrap_s = none.wrap_t = GL_CLAMP_TO_EDGE, none.max_level = 1000, none.max_lod = 1000;
                smp = mg_sampler_for(c, &none);
            }
            if (c->trace_depth) trace_slot(c, p, st, slot, unit, cube ? c->unit_cube[unit] : c->unit_2d[unit], t);
            // a depth texture is read through the shader's texture2d<float> (depth in x);
            // the mask tells the shader to make that GL's (d, d, d, 1)
            id<MTLTexture> color = tex;
            if (cube) {
                if (!color) color = c->dummy_cube;
            } else if (tex && t->depth) {
                depthmask |= 1u << slot;
            } else if (!color) {
                color = c->dummy_2d;
            }
            if (st == 0) {
                [e setVertexTexture:color atIndex:(NSUInteger)slot];
                [e setVertexSamplerState:smp atIndex:(NSUInteger)slot];
            } else {
                [e setFragmentTexture:color atIndex:(NSUInteger)slot];
                [e setFragmentSamplerState:smp atIndex:(NSUInteger)slot];
            }
        }
        if (st == 0) [e setVertexBytes:&depthmask length:4 atIndex:MG_DEPTHMASK_BUF];
        else [e setFragmentBytes:&depthmask length:4 atIndex:MG_DEPTHMASK_BUF];
    }
    c->n_draws++;
    *out = p;
    return 1;
}

static MTLPrimitiveType prim(GLenum mode)
{
    switch (mode) {
    case GL_POINTS: return MTLPrimitiveTypePoint;
    case GL_LINES: return MTLPrimitiveTypeLine;
    case GL_LINE_STRIP: case GL_LINE_LOOP: return MTLPrimitiveTypeLineStrip;
    case GL_TRIANGLE_STRIP: return MTLPrimitiveTypeTriangleStrip;
    }
    return MTLPrimitiveTypeTriangle;
}

// indices as 16 or 32 bits in the stream: fans to triangles, loops closed
static size_t fix_indices(mtlgles_context *c, GLenum mode, const uint32_t *idx, int count, int *out_count,
                          id<MTLBuffer> *buf, int *wide)
{
    uint32_t maxv = 0;
    for (int i = 0; i < count; i++)
        if (idx[i] > maxv) maxv = idx[i];
    int n = count;
    uint32_t *tmp;
    if (mode == GL_TRIANGLE_FAN) {
        n = count >= 3 ? (count - 2) * 3 : 0;
        tmp = malloc(sizeof(uint32_t) * (size_t)(n + 1));
        for (int i = 0; i + 2 < count; i++) tmp[3 * i] = idx[0], tmp[3 * i + 1] = idx[i + 1], tmp[3 * i + 2] = idx[i + 2];
    } else if (mode == GL_LINE_LOOP) {
        n = count + 1;
        tmp = malloc(sizeof(uint32_t) * (size_t)n);
        memcpy(tmp, idx, sizeof(uint32_t) * (size_t)count);
        tmp[count] = idx[0];
    } else {
        tmp = malloc(sizeof(uint32_t) * (size_t)(count + 1));
        memcpy(tmp, idx, sizeof(uint32_t) * (size_t)count);
    }
    *out_count = n;
    size_t off;
    if (maxv < 65535) {
        uint16_t *s = malloc(sizeof(uint16_t) * (size_t)(n + 1));
        for (int i = 0; i < n; i++) s[i] = (uint16_t)tmp[i];
        off = mg_stream(c, s, sizeof(uint16_t) * (size_t)n, 4, buf);
        free(s);
        *wide = 0;
    } else {
        off = mg_stream(c, tmp, sizeof(uint32_t) * (size_t)n, 4, buf);
        *wide = 1;
    }
    free(tmp);
    return off;
}

static void glDrawArrays_(GLenum mode, GLint first, GLsizei count)
{
    C;
    mg_program *p;
    if (!prepare(c, mode, first, count, &p)) return;
    if (mode == GL_TRIANGLE_FAN || mode == GL_LINE_LOOP) {
        uint32_t *idx = malloc(sizeof(uint32_t) * (size_t)count);
        for (int i = 0; i < count; i++) idx[i] = (uint32_t)(first + i);
        id<MTLBuffer> b;
        int n, wide;
        size_t off = fix_indices(c, mode, idx, count, &n, &b, &wide);
        free(idx);
        if (n) [c->enc drawIndexedPrimitives:prim(mode) indexCount:(NSUInteger)n indexType:wide ? MTLIndexTypeUInt32 : MTLIndexTypeUInt16
                                 indexBuffer:b indexBufferOffset:off];
        return;
    }
    [c->enc drawPrimitives:prim(mode) vertexStart:(NSUInteger)first vertexCount:(NSUInteger)count];
}

static void glDrawElements_(GLenum mode, GLsizei count, GLenum type, const void *indices)
{
    C;
    if (count <= 0) return;
    mg_buffer *eb = c->bound_element ? mg_get(&c->buffers, c->bound_element) : NULL;
    const uint8_t *src = eb && eb->mb ? (const uint8_t *)eb->mb.contents + (uintptr_t)indices : (const uint8_t *)indices;
    if (!src) return;
    // the range of vertices it reads (client arrays are copied from it)
    uint32_t lo = 0xffffffffu, hi = 0;
    int isz = type == GL_UNSIGNED_BYTE ? 1 : type == GL_UNSIGNED_SHORT ? 2 : 4;
    for (int i = 0; i < count; i++) {
        uint32_t v = isz == 1 ? src[i] : isz == 2 ? ((const uint16_t *)src)[i] : ((const uint32_t *)src)[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
    }
    mg_program *p;
    if (!prepare(c, mode, (int)lo, (int)(hi - lo + 1), &p)) return;
    int direct = eb && eb->mb && type != GL_UNSIGNED_BYTE && mode != GL_TRIANGLE_FAN && mode != GL_LINE_LOOP &&
                 ((uintptr_t)indices % 4) == 0;
    if (direct) {
        eb->last_use = c->serial;
        [c->enc drawIndexedPrimitives:prim(mode) indexCount:(NSUInteger)count
                            indexType:type == GL_UNSIGNED_SHORT ? MTLIndexTypeUInt16 : MTLIndexTypeUInt32
                          indexBuffer:eb->mb indexBufferOffset:(NSUInteger)(uintptr_t)indices];
        return;
    }
    uint32_t *idx = malloc(sizeof(uint32_t) * (size_t)count);
    for (int i = 0; i < count; i++) idx[i] = isz == 1 ? src[i] : isz == 2 ? ((const uint16_t *)src)[i] : ((const uint32_t *)src)[i];
    id<MTLBuffer> b;
    int n, wide;
    size_t off = fix_indices(c, mode, idx, count, &n, &b, &wide);
    free(idx);
    if (n) [c->enc drawIndexedPrimitives:prim(mode) indexCount:(NSUInteger)n indexType:wide ? MTLIndexTypeUInt32 : MTLIndexTypeUInt16
                             indexBuffer:b indexBufferOffset:off];
}

static void glDrawRangeElements_(GLenum mode, GLuint start, GLuint end, GLsizei count, GLenum type, const void *indices)
{
    (void)start, (void)end;
    glDrawElements_(mode, count, type, indices);
}

// glBlitFramebuffer when it scales or flips: drawn with the utility shaders
void mg_blit_scaled(mtlgles_context *c, id<MTLTexture> src, int slevel, int sslice, const int *s, id<MTLTexture> dst,
                    int dlevel, int dslice, const int *d, int linear)
{
    (void)sslice;
    mg_end_pass(c);
    mg_end_blit(c);
    NSString *k = [NSString stringWithFormat:@"%d", (int)dst.pixelFormat];
    id<MTLRenderPipelineState> pipe = c->blit_pipes[k];
    if (!pipe) {
        id<MTLLibrary> lib = util_lib(c);
        MTLRenderPipelineDescriptor *pd = [[MTLRenderPipelineDescriptor alloc] init];
        pd.vertexFunction = [[lib newFunctionWithName:@"mg_blit_v"] autorelease];
        pd.fragmentFunction = [[lib newFunctionWithName:@"mg_blit_f"] autorelease];
        pd.colorAttachments[0].pixelFormat = dst.pixelFormat;
        NSError *e = nil;
        pipe = [c->dev newRenderPipelineStateWithDescriptor:pd error:&e];
        [pd release];
        if (!pipe) {
            mg_log("mtlgles: blit pipeline: %s\n", e.localizedDescription.UTF8String);
            return;
        }
        c->blit_pipes[k] = pipe;
        [pipe release];
    }
    MTLRenderPassDescriptor *rp = [MTLRenderPassDescriptor renderPassDescriptor];
    rp.colorAttachments[0].texture = dst;
    rp.colorAttachments[0].level = (NSUInteger)dlevel;
    rp.colorAttachments[0].slice = (NSUInteger)dslice;
    rp.colorAttachments[0].loadAction = MTLLoadActionLoad;
    rp.colorAttachments[0].storeAction = MTLStoreActionStore;
    id<MTLRenderCommandEncoder> e = [mg_cmd(c) renderCommandEncoderWithDescriptor:rp];
    double dw = (double)(dst.width >> dlevel), dh = (double)(dst.height >> dlevel);
    double sw = (double)(src.width >> slevel), sh = (double)(src.height >> slevel);
    // rows are OpenGL's in both (row 0 at the bottom = Metal row 0): clip y runs down in Metal, so negate
    float r[8] = {(float)(d[0] / dw * 2 - 1), (float)-(d[1] / dh * 2 - 1), (float)(d[2] / dw * 2 - 1), (float)-(d[3] / dh * 2 - 1),
                  (float)(s[0] / sw), (float)(s[1] / sh), (float)(s[2] / sw), (float)(s[3] / sh)};
    [e setRenderPipelineState:pipe];
    [e setVertexBytes:r length:sizeof r atIndex:0];
    [e setFragmentTexture:src atIndex:0];
    mg_texture none = {0};
    none.min_filter = none.mag_filter = linear ? GL_LINEAR : GL_NEAREST;
    none.wrap_s = none.wrap_t = GL_CLAMP_TO_EDGE;
    none.max_level = 1000, none.max_lod = 1000;
    [e setFragmentSamplerState:mg_sampler_for(c, &none) atIndex:0];
    [e drawPrimitives:MTLPrimitiveTypeTriangleStrip vertexStart:0 vertexCount:4];
    [e endEncoding];
}

void mg_draw_init(mtlgles_context *c) { (void)c; }

// ---- vertex array objects: one, the default (gl4es works without them) ---------------------------
static void glGenVertexArrays_(GLsizei n, GLuint *out) { for (GLsizei i = 0; i < n; i++) out[i] = 0; }
static void glBindVertexArray_(GLuint a) { (void)a; }
static void glDeleteVertexArrays_(GLsizei n, const GLuint *a) { (void)n, (void)a; }

#define E(n) {#n, (void *)n##_}
static const struct { const char *name; void *fn; } entries[] = {
    E(glEnableVertexAttribArray), E(glDisableVertexAttribArray), E(glVertexAttribPointer), E(glVertexAttrib1f),
    E(glVertexAttrib2f), E(glVertexAttrib3f), E(glVertexAttrib4f), E(glVertexAttrib1fv), E(glVertexAttrib2fv),
    E(glVertexAttrib3fv), E(glVertexAttrib4fv), E(glGetVertexAttribiv), E(glGetVertexAttribfv),
    E(glGetVertexAttribPointerv), E(glClear), E(glDrawArrays), E(glDrawElements), E(glDrawRangeElements),
    E(glGenVertexArrays), E(glBindVertexArray), E(glDeleteVertexArrays), E(glGenQueries), E(glDeleteQueries),
    E(glIsQuery), E(glBeginQuery), E(glEndQuery), E(glGetQueryiv), E(glGetQueryObjectuiv),
};

void *mg_draw_proc(const char *name)
{
    for (size_t i = 0; i < sizeof entries / sizeof entries[0]; i++)
        if (!strcmp(entries[i].name, name)) return entries[i].fn;
    return NULL;
}
