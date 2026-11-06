// mtlgles: the context, command buffers, fixed state and queries.
// BSD-3-Clause.
#include "mtlgles_internal.h"
#include "af_debug.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

__thread mtlgles_context *mg_cur;
static void (*log_fn)(const char *);

void mtlgles_set_log(void (*log)(const char *msg)) { log_fn = log; }

void mg_log(const char *fmt, ...)
{
    char small[1024], *buf = small;
    va_list ap, ap2;
    va_start(ap, fmt);
    va_copy(ap2, ap);
    int n = vsnprintf(small, sizeof small, fmt, ap);
    va_end(ap);
    if (n >= (int)sizeof small && (buf = malloc((size_t)n + 1)))   // whole: MTLGLES_DUMP's sources
        vsnprintf(buf, (size_t)n + 1, fmt, ap2);
    else if (!buf) buf = small;
    va_end(ap2);
    if (log_fn) log_fn(buf);
    else fputs(buf, stderr);
    if (buf != small) free(buf);
}

void mg_error(GLenum e)
{
    if (mg_cur && !mg_cur->error) mg_cur->error = e;
}

// ---- names --------------------------------------------------------------------------------------
void *mg_get(mg_table *t, GLuint name) { return name && name < t->n ? t->p[name] : NULL; }

void mg_put(mg_table *t, GLuint name, void *obj)
{
    if (name >= t->cap) {
        GLuint cap = name * 2 + 64;
        t->p = realloc(t->p, sizeof(void *) * cap);
        memset(t->p + t->cap, 0, sizeof(void *) * (cap - t->cap));
        t->cap = cap;
    }
    if (name >= t->n) t->n = name + 1;
    t->p[name] = obj;
}

GLuint mg_gen(mg_table *t, void *obj)
{
    GLuint name = t->next ? t->next : 1;
    while (mg_get(t, name)) name++;
    t->next = name + 1;
    mg_put(t, name, obj);
    return name;
}

void mg_del(mg_table *t, GLuint name)
{
    if (name && name < t->n) t->p[name] = NULL;
    if (name && name < t->next) t->next = name;
}

// ---- command buffers ----------------------------------------------------------------------------
id<MTLCommandBuffer> mg_cmd(mtlgles_context *c)
{
    if (!c->cmd) {
        c->cmd = [[c->queue commandBufferWithUnretainedReferences] retain];
        c->serial++;
        c->n_cmds++;
        c->cur_pipe = nil;
        // a fresh stream block per command buffer: the last one is still in use
        if (c->stream) {
            mg_defer(c, c->stream);
            c->stream = nil;
        }
    }
    return c->cmd;
}

// Unretained references: the objects a command buffer uses must outlive it.
// Deleted objects are released only once the GPU is past them (mg_defer).
static NSMutableArray *deferred;   // [serial, object] pairs
static pthread_mutex_t defer_lock = PTHREAD_MUTEX_INITIALIZER;

void mg_defer(mtlgles_context *c, id obj)
{
    if (!obj) return;
    pthread_mutex_lock(&defer_lock);
    if (!deferred) deferred = [[NSMutableArray alloc] init];
    // made with alloc, not a literal: an autoreleased pair would keep obj (a
    // 4 MB streaming block, a staging buffer) alive after collect drops it,
    // until the caller's autorelease pool drains, which on a plain pthread is
    // never (HotD4 on an M1 iPad grew about 5 MB a frame to 5 GB)
    NSNumber *s = [[NSNumber alloc] initWithUnsignedLongLong:c->serial];
    NSArray *pair = [[NSArray alloc] initWithObjects:s, obj, nil];
    [deferred addObject:pair];
    [pair release];
    [s release];
    pthread_mutex_unlock(&defer_lock);
    [obj release];
}

static void collect(mtlgles_context *c)
{
    uint64_t done = atomic_load(&c->done);
    pthread_mutex_lock(&defer_lock);
    NSUInteger keep = 0, n = deferred.count;
    for (NSUInteger i = 0; i < n; i++) {
        NSArray *e = deferred[i];
        if ([e[0] unsignedLongLongValue] > done) break;
        keep = i + 1;
    }
    if (keep) [deferred removeObjectsInRange:NSMakeRange(0, keep)];
    pthread_mutex_unlock(&defer_lock);
}

void mg_end_pass(mtlgles_context *c)
{
    if (c->enc) {
        [c->enc endEncoding];
        [c->enc release];
        c->enc = nil;
    }
    c->pass_fb = (GLuint)-1;
    c->cur_pipe = nil;
    c->cur_ds = nil;
    c->cur_ub_prog = 0;
}

void mg_end_blit(mtlgles_context *c)
{
    if (c->blit) {
        [c->blit endEncoding];
        [c->blit release];
        c->blit = nil;
    }
}

id<MTLBlitCommandEncoder> mg_blit(mtlgles_context *c)
{
    // a pending clear goes first, also when a blit encoder is open: a copy
    // into a framebuffer cleared since must not be wiped by the clear later
    if (c->pending_clear) mg_resolve_clear(c);
    mg_end_pass(c);
    if (!c->blit) c->blit = [[mg_cmd(c) blitCommandEncoder] retain];
    return c->blit;
}

id<MTLBlitCommandEncoder> mg_upload_blit(mtlgles_context *c)
{
    mg_cmd(c);   // staging memory is released with the command buffer after the uploads
    if (!c->upload_cmd) {
        c->upload_cmd = [[c->queue commandBufferWithUnretainedReferences] retain];
        c->upload_blit = [[c->upload_cmd blitCommandEncoder] retain];
    }
    return c->upload_blit;
}

// MTLGLES_GPU_CAPTURE=seconds[,flushes]: a Metal GPU capture (a .gputrace for
// Xcode) of the command buffers of that many flushes (4) after that many
// seconds, to MTLGLES_GPU_CAPTURE_FILE (mtlgles.gputrace); needs
// MTL_CAPTURE_ENABLED=1. The core flushes about once a frame.
static void gpu_capture(mtlgles_context *c)
{
    static int state = -1, flushes = 4, left;
    static double at, t0;
    if (state < 0) {
        const char *e = af_debug_env("MTLGLES_GPU_CAPTURE");
        state = e ? 0 : 2;
        if (e) sscanf(e, "%lf,%d", &at, &flushes);
    }
    if (state == 2) return;
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    double now = ts.tv_sec + ts.tv_nsec * 1e-9;
    if (!t0) t0 = now;
    MTLCaptureManager *m = [MTLCaptureManager sharedCaptureManager];
    if (state == 0 && now - t0 >= at) {
        const char *f = af_debug_env("MTLGLES_GPU_CAPTURE_FILE");
        MTLCaptureDescriptor *d = [[MTLCaptureDescriptor alloc] init];
        d.captureObject = c->queue;
        d.destination = MTLCaptureDestinationGPUTraceDocument;
        d.outputURL = [NSURL fileURLWithPath:[NSString stringWithUTF8String:f ? f : "mtlgles.gputrace"]];
        NSError *err = nil;
        if ([m startCaptureWithDescriptor:d error:&err]) {
            mg_log("mtlgles: GPU capture started (%d flushes)\n", flushes);
            state = 1, left = flushes;
        } else {
            mg_log("mtlgles: GPU capture: %s\n", err.localizedDescription.UTF8String);
            state = 2;
        }
        [d release];
    } else if (state == 1 && --left <= 0) {
        [m stopCapture];
        mg_log("mtlgles: GPU capture written\n");
        state = 2;
    }
}

void mg_flush(mtlgles_context *c)
{
    mg_resolve_clear(c);
    mg_end_pass(c);
    mg_end_blit(c);
    if (c->upload_cmd) {   // before the commands that read what it uploads
        [c->upload_blit endEncoding];
        [c->upload_blit release];
        [c->upload_cmd commit];
        [c->upload_cmd release];
        c->upload_blit = nil;
        c->upload_cmd = nil;
    }
    if (!c->cmd) return;
    uint64_t serial = c->serial;
    mtlgles_context *cc = c;
    [c->cmd addCompletedHandler:^(id<MTLCommandBuffer> b) {
        if (b.error) {   // a GPU fault or time-out: everything in it is lost
            static int told;
            if (told++ < 10) mg_log("mtlgles: command buffer %llu failed: %s\n", (unsigned long long)serial, b.error.localizedDescription.UTF8String);
        }
        pthread_mutex_lock(&cc->done_lock);
        if (atomic_load(&cc->done) < serial) atomic_store(&cc->done, serial);
        pthread_cond_broadcast(&cc->done_cond);
        pthread_mutex_unlock(&cc->done_lock);
    }];
    [c->cmd commit];
    [c->cmd release];
    c->cmd = nil;
    collect(c);
    gpu_capture(c);
}

// whether the GPU has yet to finish command buffer `serial`
int mg_busy(mtlgles_context *c, uint64_t serial)
{
    return serial && serial > atomic_load(&c->done);
}

void mg_wait(mtlgles_context *c, uint64_t serial)
{
    if (!mg_busy(c, serial)) return;
    if (c->cmd && serial >= c->serial) mg_flush(c);
    c->n_waits++;
    pthread_mutex_lock(&c->done_lock);
    while (atomic_load(&c->done) < serial) pthread_cond_wait(&c->done_cond, &c->done_lock);
    pthread_mutex_unlock(&c->done_lock);
    collect(c);
}

// data copied into this command buffer's streaming memory: its offset there
size_t mg_stream(mtlgles_context *c, const void *data, size_t size, size_t align, id<MTLBuffer> *buf)
{
    mg_cmd(c);
    size_t off = (c->stream_off + align - 1) / align * align;
    if (!c->stream || off + size > c->stream_size) {
        // a new block; the command buffer keeps the old one until it is done
        if (c->stream) mg_defer(c, c->stream);
        size_t sz = 4 << 20;
        while (sz < size) sz *= 2;
        c->stream = [c->dev newBufferWithLength:sz options:MTLResourceStorageModeShared];
        c->stream_size = sz;
        off = 0;
    }
    if (data) memcpy((uint8_t *)c->stream.contents + off, data, size);
    c->stream_off = off + size;
    c->n_stream_bytes += size;
    *buf = c->stream;
    return off;
}

// ---- context ------------------------------------------------------------------------------------
mtlgles_context *mtlgles_create(void *device)
{
    mtlgles_context *c = calloc(1, sizeof *c);
    c->dev = device ? [(id<MTLDevice>)device retain] : MTLCreateSystemDefaultDevice();
    if (!c->dev) {
        free(c);
        return NULL;
    }
    c->queue = [c->dev newCommandQueueWithMaxCommandBufferCount:64];
    pthread_mutex_init(&c->done_lock, NULL);
    pthread_cond_init(&c->done_cond, NULL);
    c->blend_src_rgb = c->blend_src_a = GL_ONE;
    c->blend_dst_rgb = c->blend_dst_a = GL_ZERO;
    c->blend_eq_rgb = c->blend_eq_a = GL_FUNC_ADD;
    c->color_mask[0] = c->color_mask[1] = c->color_mask[2] = c->color_mask[3] = 1;
    c->depth_mask = 1;
    c->depth_func = GL_LESS;
    c->trace_depth = af_debug_env("MTLGLES_TRACE_DEPTH") != NULL;
    // BC on every Mac, on iPads from the M1 (iPadOS 16.4), not on older iPhones and Apple TVs
    if (@available(macOS 11.0, iOS 16.4, tvOS 16.4, *)) c->bc = c->dev.supportsBCTextureCompression;
    if (af_debug_env("MTLGLES_NO_BC")) c->bc = 0;
    c->cull_face = GL_BACK;
    c->front_face = GL_CCW;
    c->depth_far = 1;
    c->clear_depth = 1;
    c->dither = 1;
    for (int i = 0; i < 2; i++) {
        c->stencil[i].func = GL_ALWAYS;
        c->stencil[i].fail = c->stencil[i].zfail = c->stencil[i].zpass = GL_KEEP;
        c->stencil[i].mask = c->stencil[i].write = 0xffffffffu;
    }
    for (int i = 0; i < MG_MAX_ATTRIBS; i++) c->attr[i].value[3] = 1, c->attr[i].size = 4, c->attr[i].type = GL_FLOAT;
    c->pack_align = c->unpack_align = 4;
    c->pass_fb = (GLuint)-1;
    c->ds_states = [[NSMutableDictionary alloc] init];
    c->samplers = [[NSMutableDictionary alloc] init];
    c->clear_pipes = [[NSMutableDictionary alloc] init];
    c->blit_pipes = [[NSMutableDictionary alloc] init];
    // the default framebuffer: 16x16, the games draw into their own
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                                                 width:16 height:16 mipmapped:NO];
    d.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    d.storageMode = MTLStorageModePrivate;
    c->fb0_color = [c->dev newTextureWithDescriptor:d];
    d.pixelFormat = MTLPixelFormatDepth32Float_Stencil8;
    c->fb0_depth = [c->dev newTextureWithDescriptor:d];
    c->viewport[2] = c->scissor[2] = 16;
    c->viewport[3] = c->scissor[3] = 16;
    mg_objects_init(c);
    mg_draw_init(c);
    mg_log("mtlgles: %s\n", c->dev.name.UTF8String);
    return c;
}

void mtlgles_make_current(mtlgles_context *ctx) { mg_cur = ctx; }

void mtlgles_destroy(mtlgles_context *c)
{
    if (!c) return;
    mg_flush(c);
    mg_wait(c, c->serial);
    // objects: their Metal parts
    for (GLuint i = 0; i < c->buffers.n; i++) {
        mg_buffer *b = c->buffers.p[i];
        if (!b) continue;
        for (mg_repack *r = b->repacks, *n; r; r = n) n = r->next, [r->mb release], free(r);
        [b->mb release], free(b);
    }
    for (GLuint i = 0; i < c->textures.n; i++) {
        mg_texture *t = c->textures.p[i];
        if (t) [t->mt release], free(t);
    }
    for (GLuint i = 0; i < c->renderbuffers.n; i++) {
        mg_renderbuffer *r = c->renderbuffers.p[i];
        if (r) [r->mt release], free(r);
    }
    for (GLuint i = 0; i < c->framebuffers.n; i++) free(c->framebuffers.p[i]);
    for (GLuint i = 0; i < c->shaders.n; i++) {
        mg_shader_obj *s = c->shaders.p[i];
        if (s) mg_free(s->parsed), free(s->source), free(s);
    }
    for (GLuint i = 0; i < c->programs.n; i++) {
        mg_program *p = c->programs.p[i];
        if (!p) continue;
        mg_prog_cancel(p);
        [p->vfn release];
        [p->ffn release];
        [p->pipelines release];
        free(p->uni), free(p->loc_uni), free(p->loc_elem), free(p->ub[0]), free(p->ub[1]);
        free(p);
    }
    free(c->buffers.p), free(c->textures.p), free(c->renderbuffers.p), free(c->framebuffers.p);
    free(c->shaders.p), free(c->programs.p);
    mg_query_free(c);
    [c->stream release];
    [c->ds_states release];
    [c->samplers release];
    [c->clear_pipes release];
    [c->blit_pipes release];
    [c->dummy_2d release];
    [c->dummy_cube release];
    [c->util_lib release];
    [c->fb0_color release];
    [c->fb0_depth release];
    [c->depth_copy release];
    [c->queue release];
    [c->dev release];
    pthread_mutex_lock(&defer_lock);
    [deferred removeAllObjects];
    pthread_mutex_unlock(&defer_lock);
    if (mg_cur == c) mg_cur = NULL;
    free(c);
}

void mtlgles_stats(char *buf, size_t size)
{
    mtlgles_context *c = mg_cur;
    if (!c) {
        if (size) buf[0] = 0;
        return;
    }
    snprintf(buf, size, "mtlgles: %llu draws, %llu passes, %llu command buffers, %llu new pipelines, "
             "%llu KB streamed, %llu uploads, %llu waits, %llu arrays repacked",
             (unsigned long long)c->n_draws, (unsigned long long)c->n_passes, (unsigned long long)c->n_cmds,
             (unsigned long long)c->n_pipes, (unsigned long long)(c->n_stream_bytes >> 10),
             (unsigned long long)c->n_uploads, (unsigned long long)c->n_waits, (unsigned long long)c->n_repacks);
    c->n_draws = c->n_passes = c->n_cmds = c->n_pipes = c->n_stream_bytes = c->n_uploads = c->n_waits = c->n_repacks = 0;
}

// ---- fixed state --------------------------------------------------------------------------------
#define C mtlgles_context *c = mg_cur; if (!c) return
#define CR(v) mtlgles_context *c = mg_cur; if (!c) return v

static int *cap(mtlgles_context *c, GLenum e)
{
    switch (e) {
    case GL_BLEND: return &c->blend;
    case GL_CULL_FACE: return &c->cull;
    case GL_DEPTH_TEST: return &c->depth_test;
    case GL_STENCIL_TEST: return &c->stencil_test;
    case GL_SCISSOR_TEST: return &c->scissor_test;
    case GL_POLYGON_OFFSET_FILL: return &c->poly_offset;
    case GL_RASTERIZER_DISCARD: return &c->discard;
    case GL_DITHER: return &c->dither;
    }
    return NULL;
}
static void glEnable_(GLenum e) { C; int *p = cap(c, e); if (p) *p = 1; }
static void glDisable_(GLenum e) { C; int *p = cap(c, e); if (p) *p = 0; }
static GLboolean glIsEnabled_(GLenum e) { CR(0); int *p = cap(c, e); return p ? (GLboolean)*p : 0; }
static void glBlendFunc_(GLenum s, GLenum d) { C; c->blend_src_rgb = c->blend_src_a = s; c->blend_dst_rgb = c->blend_dst_a = d; }
static void glBlendFuncSeparate_(GLenum sr, GLenum dr, GLenum sa, GLenum da)
{
    C;
    c->blend_src_rgb = sr, c->blend_dst_rgb = dr, c->blend_src_a = sa, c->blend_dst_a = da;
}
static void glBlendEquation_(GLenum e) { C; c->blend_eq_rgb = c->blend_eq_a = e; }
static void glBlendEquationSeparate_(GLenum r, GLenum a) { C; c->blend_eq_rgb = r, c->blend_eq_a = a; }
static void glBlendColor_(GLfloat r, GLfloat g, GLfloat b, GLfloat a)
{
    C;
    c->blend_color[0] = r, c->blend_color[1] = g, c->blend_color[2] = b, c->blend_color[3] = a;
}
static void glColorMask_(GLboolean r, GLboolean g, GLboolean b, GLboolean a)
{
    C;
    c->color_mask[0] = r, c->color_mask[1] = g, c->color_mask[2] = b, c->color_mask[3] = a;
}
static void glDepthFunc_(GLenum f) { C; c->depth_func = f; }
static void glDepthMask_(GLboolean m) { C; c->depth_mask = m; }
// both clamped to [0, 1], as OpenGL does (HotD4 clears its depth with glClearDepth(10000))
static float clamp01(float v) { return v < 0 ? 0 : v > 1 ? 1 : v; }
static void glDepthRangef_(GLfloat n, GLfloat f) { C; c->depth_near = clamp01(n), c->depth_far = clamp01(f); }
static void glClearColor_(GLfloat r, GLfloat g, GLfloat b, GLfloat a)
{
    C;
    c->clear_color[0] = r, c->clear_color[1] = g, c->clear_color[2] = b, c->clear_color[3] = a;
}
static void glClearDepthf_(GLfloat d) { C; c->clear_depth = clamp01(d); }
static void glClearStencil_(GLint s) { C; c->clear_stencil = s; }
static void stencil_faces(GLenum face, int *a, int *b)
{
    *a = face == GL_BACK ? 1 : 0;
    *b = face == GL_FRONT ? 0 : 1;
}
static void glStencilFuncSeparate_(GLenum face, GLenum func, GLint ref, GLuint mask)
{
    C;
    int a, b;
    stencil_faces(face, &a, &b);
    for (int i = a; i <= b; i++) c->stencil[i].func = func, c->stencil[i].ref = ref, c->stencil[i].mask = mask;
}
static void glStencilFunc_(GLenum func, GLint ref, GLuint mask) { glStencilFuncSeparate_(GL_FRONT_AND_BACK, func, ref, mask); }
static void glStencilOpSeparate_(GLenum face, GLenum f, GLenum zf, GLenum zp)
{
    C;
    int a, b;
    stencil_faces(face, &a, &b);
    for (int i = a; i <= b; i++) c->stencil[i].fail = f, c->stencil[i].zfail = zf, c->stencil[i].zpass = zp;
}
static void glStencilOp_(GLenum f, GLenum zf, GLenum zp) { glStencilOpSeparate_(GL_FRONT_AND_BACK, f, zf, zp); }
static void glStencilMaskSeparate_(GLenum face, GLuint m)
{
    C;
    int a, b;
    stencil_faces(face, &a, &b);
    for (int i = a; i <= b; i++) c->stencil[i].write = m;
}
static void glStencilMask_(GLuint m) { glStencilMaskSeparate_(GL_FRONT_AND_BACK, m); }
static void glCullFace_(GLenum f) { C; c->cull_face = f; }
static void glFrontFace_(GLenum f) { C; c->front_face = f; }
static void glLineWidth_(GLfloat w) { (void)w; }
static void glPolygonOffset_(GLfloat f, GLfloat u) { C; c->poly_factor = f, c->poly_units = u; }
static void glViewport_(GLint x, GLint y, GLsizei w, GLsizei h)
{
    C;
    c->viewport[0] = x, c->viewport[1] = y, c->viewport[2] = w, c->viewport[3] = h;
}
static void glScissor_(GLint x, GLint y, GLsizei w, GLsizei h)
{
    C;
    c->scissor[0] = x, c->scissor[1] = y, c->scissor[2] = w, c->scissor[3] = h;
}
static void glPixelStorei_(GLenum p, GLint v)
{
    C;
    if (p == GL_PACK_ALIGNMENT) c->pack_align = v;
    else if (p == GL_UNPACK_ALIGNMENT) c->unpack_align = v;
    else if (p == GL_UNPACK_ROW_LENGTH) c->unpack_row_length = v;
}
static void glHint_(GLenum t, GLenum m) { (void)t, (void)m; }
static void glSampleCoverage_(GLfloat v, GLboolean i) { (void)v, (void)i; }
static GLenum glGetError_(void)
{
    CR(GL_NO_ERROR);
    GLenum e = c->error;
    c->error = GL_NO_ERROR;
    return e;
}
static void glFlush_(void) { C; mg_flush(c); }
static void glFinish_(void)
{
    C;
    uint64_t s = c->serial;
    mg_flush(c);
    mg_wait(c, s);
}
static void glReleaseShaderCompiler_(void) {}

// ---- sync objects -------------------------------------------------------------------------------
static void *glFenceSync_(GLenum cond, GLbitfield flags)
{
    (void)cond, (void)flags;
    CR(NULL);
    mg_sync *s = calloc(1, sizeof *s);
    // a fence after an empty command buffer is the last one's
    s->serial = c->cmd ? c->serial : c->serial;
    return s;
}
static GLenum glClientWaitSync_(void *sync, GLbitfield flags, GLuint64 timeout)
{
    CR(GL_WAIT_FAILED);
    mg_sync *s = sync;
    if (!s) return GL_WAIT_FAILED;
    if (!mg_busy(c, s->serial)) return GL_ALREADY_SIGNALED;
    if ((flags & GL_SYNC_FLUSH_COMMANDS_BIT) && c->cmd && s->serial >= c->serial) mg_flush(c);
    if (!timeout) return GL_TIMEOUT_EXPIRED;
    if (c->cmd && s->serial >= c->serial) mg_flush(c);
    struct timespec until;
    clock_gettime(CLOCK_REALTIME, &until);
    uint64_t ns = until.tv_nsec + timeout;
    until.tv_sec += (time_t)(ns / 1000000000ull);
    until.tv_nsec = (long)(ns % 1000000000ull);
    pthread_mutex_lock(&c->done_lock);
    int r = 0;
    while (atomic_load(&c->done) < s->serial && r == 0) r = pthread_cond_timedwait(&c->done_cond, &c->done_lock, &until);
    pthread_mutex_unlock(&c->done_lock);
    return mg_busy(c, s->serial) ? GL_TIMEOUT_EXPIRED : GL_CONDITION_SATISFIED;
}
static void glWaitSync_(void *sync, GLbitfield flags, GLuint64 timeout) { (void)sync, (void)flags, (void)timeout; }
static void glDeleteSync_(void *sync) { free(sync); }
static GLboolean glIsSync_(void *sync) { return sync != NULL; }
static void glGetSynciv_(void *sync, GLenum p, GLsizei n, GLsizei *len, GLint *v)
{
    C;
    mg_sync *s = sync;
    if (len) *len = 1;
    if (!s || n < 1) return;
    if (p == GL_SYNC_STATUS) *v = mg_busy(c, s->serial) ? GL_UNSIGNALED : GL_SIGNALED;
    else if (p == GL_OBJECT_TYPE) *v = GL_SYNC_FENCE;
    else if (p == GL_SYNC_CONDITION) *v = GL_SYNC_GPU_COMMANDS_COMPLETE;
    else *v = 0;
}

// ---- queries ------------------------------------------------------------------------------------
static const char *extensions =
    "GL_OES_element_index_uint GL_OES_packed_depth_stencil GL_OES_depth24 GL_OES_depth32 GL_OES_depth_texture "
    "GL_OES_texture_npot GL_EXT_texture_format_BGRA8888 GL_OES_rgb8_rgba8 GL_OES_standard_derivatives "
    "GL_EXT_shader_texture_lod GL_EXT_blend_minmax GL_OES_texture_float GL_OES_texture_half_float "
    "GL_OES_fbo_render_mipmap GL_EXT_draw_buffers GL_OES_mapbuffer GL_EXT_map_buffer_range "
    "GL_EXT_read_format_bgra GL_OES_texture_float_linear GL_OES_texture_half_float_linear GL_EXT_occlusion_query_boolean "
    // half-float colour attachments are renderable (RGBA16Float); without it gl4es recreates
    // a float texture attached to a framebuffer as RGBA8, and that path resizes whichever
    // texture is really bound (Let's Go Jungle's 800x600 scene became its 84x64 chain)
    "GL_EXT_color_buffer_half_float";

static const char *extensions_bc =
    "GL_EXT_texture_compression_s3tc";
// the extensions this context offers: S3TC where the device has BC
static const char *ext_str(void)
{
    static char *with_bc;
    mtlgles_context *c = mg_cur;
    if (!c || !c->bc) return extensions;
    if (!with_bc) {
        with_bc = malloc(strlen(extensions) + strlen(extensions_bc) + 2);
        sprintf(with_bc, "%s %s", extensions, extensions_bc);
    }
    return with_bc;
}

static const GLubyte *glGetString_(GLenum n)
{
    switch (n) {
    case GL_VENDOR: return (const GLubyte *)"mtlgles";
    case GL_RENDERER: return (const GLubyte *)"mtlgles on Metal";
    case GL_VERSION: return (const GLubyte *)"OpenGL ES 3.0 mtlgles";
    case GL_SHADING_LANGUAGE_VERSION: return (const GLubyte *)"OpenGL ES GLSL ES 3.00";
    case GL_EXTENSIONS: return (const GLubyte *)ext_str();
    }
    mg_error(GL_INVALID_ENUM);
    return NULL;
}

static const GLubyte *glGetStringi_(GLenum n, GLuint i)
{
    static char words[64][48];
    static int nwords;
    if (n != GL_EXTENSIONS) return NULL;
    if (!nwords) {
        const char *p = ext_str();
        while (*p && nwords < 64) {
            int len = 0;
            while (p[len] && p[len] != ' ') len++;
            snprintf(words[nwords++], 48, "%.*s", len, p);
            p += len;
            while (*p == ' ') p++;
        }
    }
    return i < (GLuint)nwords ? (const GLubyte *)words[i] : NULL;
}

// one query's values, as integers or floats; n: how many
static int query(mtlgles_context *c, GLenum p, GLint *iv, float *fv)
{
    int n = 1;
#define I1(x) (iv[0] = (GLint)(x), fv[0] = (float)(x))
#define I2(a, b) (iv[0] = (a), iv[1] = (b), fv[0] = (float)(a), fv[1] = (float)(b), n = 2)
#define I4(a, b, cc, d) (iv[0] = (a), iv[1] = (b), iv[2] = (cc), iv[3] = (d), fv[0] = (float)(a), fv[1] = (float)(b), \
                        fv[2] = (float)(cc), fv[3] = (float)(d), n = 4)
    switch (p) {
    case GL_MAX_TEXTURE_SIZE: case GL_MAX_RENDERBUFFER_SIZE: case GL_MAX_CUBE_MAP_TEXTURE_SIZE: I1(8192); break;
    case GL_MAX_VIEWPORT_DIMS: I2(8192, 8192); break;
    case GL_MAX_TEXTURE_IMAGE_UNITS: case GL_MAX_VERTEX_TEXTURE_IMAGE_UNITS: I1(MG_MAX_UNITS); break;
    case GL_MAX_COMBINED_TEXTURE_IMAGE_UNITS: I1(MG_MAX_UNITS); break;
    case GL_MAX_VERTEX_ATTRIBS: I1(MG_MAX_ATTRIBS); break;
    case GL_MAX_VARYING_VECTORS: I1(31); break;
    case GL_MAX_VERTEX_UNIFORM_VECTORS: case GL_MAX_FRAGMENT_UNIFORM_VECTORS: I1(1024); break;
    case GL_MAX_DRAW_BUFFERS: case GL_MAX_COLOR_ATTACHMENTS: I1(MG_MAX_COLOR); break;
    case GL_MAX_SAMPLES: I1(1); break;
    case GL_MAX_TEXTURE_LOD_BIAS: I1(16); break;
    case GL_MAJOR_VERSION: I1(3); break;
    case GL_MINOR_VERSION: I1(0); break;
    case GL_NUM_EXTENSIONS: {
        int k = 1;
        for (const char *s = ext_str(); *s; s++) k += *s == ' ';
        I1(k);
        break;
    }
    case GL_NUM_COMPRESSED_TEXTURE_FORMATS: case GL_NUM_SHADER_BINARY_FORMATS: case GL_NUM_PROGRAM_BINARY_FORMATS:
        I1(0); break;
    case GL_SHADER_COMPILER: I1(1); break;
    case GL_ALIASED_POINT_SIZE_RANGE: I2(1, 511); break;
    case GL_ALIASED_LINE_WIDTH_RANGE: I2(1, 1); break;
    case GL_SUBPIXEL_BITS: I1(8); break;
    case GL_RED_BITS: case GL_GREEN_BITS: case GL_BLUE_BITS: case GL_ALPHA_BITS: case GL_STENCIL_BITS: I1(8); break;
    case GL_DEPTH_BITS: I1(24); break;
    case GL_SAMPLE_BUFFERS: I1(0); break;
    case GL_SAMPLES: I1(0); break;
    case GL_IMPLEMENTATION_COLOR_READ_FORMAT: I1(GL_RGBA); break;
    case GL_IMPLEMENTATION_COLOR_READ_TYPE: I1(GL_UNSIGNED_BYTE); break;
    case GL_ARRAY_BUFFER_BINDING: I1(c->bound_array); break;
    case GL_ELEMENT_ARRAY_BUFFER_BINDING: I1(c->bound_element); break;
    case GL_PIXEL_PACK_BUFFER_BINDING: I1(c->bound_pack); break;
    case GL_PIXEL_UNPACK_BUFFER_BINDING: I1(c->bound_unpack); break;
    case GL_FRAMEBUFFER_BINDING: I1(c->draw_fb); break;
    case GL_READ_FRAMEBUFFER_BINDING: I1(c->read_fb); break;
    case GL_RENDERBUFFER_BINDING: I1(c->bound_rb); break;
    case GL_CURRENT_PROGRAM: I1(c->program); break;
    case GL_ACTIVE_TEXTURE: I1(GL_TEXTURE0 + c->active_unit); break;
    case GL_TEXTURE_BINDING_2D: I1(c->unit_2d[c->active_unit]); break;
    case GL_TEXTURE_BINDING_CUBE_MAP: I1(c->unit_cube[c->active_unit]); break;
    case GL_VERTEX_ARRAY_BINDING: I1(0); break;
    case GL_READ_BUFFER: I1(c->read_fb ? GL_COLOR_ATTACHMENT0 : GL_BACK); break;
    case GL_DRAW_BUFFER0: I1(c->draw_fb ? GL_COLOR_ATTACHMENT0 : GL_BACK); break;
    case GL_VIEWPORT: I4(c->viewport[0], c->viewport[1], c->viewport[2], c->viewport[3]); break;
    case GL_SCISSOR_BOX: I4(c->scissor[0], c->scissor[1], c->scissor[2], c->scissor[3]); break;
    case GL_COLOR_WRITEMASK: I4(c->color_mask[0], c->color_mask[1], c->color_mask[2], c->color_mask[3]); break;
    case GL_DEPTH_WRITEMASK: I1(c->depth_mask); break;
    case GL_DEPTH_FUNC: I1(c->depth_func); break;
    case GL_CULL_FACE_MODE: I1(c->cull_face); break;
    case GL_FRONT_FACE: I1(c->front_face); break;
    case GL_BLEND_SRC_RGB: I1(c->blend_src_rgb); break;
    case GL_BLEND_DST_RGB: I1(c->blend_dst_rgb); break;
    case GL_BLEND_SRC_ALPHA: I1(c->blend_src_a); break;
    case GL_BLEND_DST_ALPHA: I1(c->blend_dst_a); break;
    case GL_BLEND_EQUATION_RGB: I1(c->blend_eq_rgb); break;
    case GL_BLEND_EQUATION_ALPHA: I1(c->blend_eq_a); break;
    case GL_STENCIL_FUNC: I1(c->stencil[0].func); break;
    case GL_STENCIL_REF: I1(c->stencil[0].ref); break;
    case GL_STENCIL_VALUE_MASK: I1((GLint)c->stencil[0].mask); break;
    case GL_STENCIL_WRITEMASK: I1((GLint)c->stencil[0].write); break;
    case GL_STENCIL_FAIL: I1(c->stencil[0].fail); break;
    case GL_STENCIL_PASS_DEPTH_FAIL: I1(c->stencil[0].zfail); break;
    case GL_STENCIL_PASS_DEPTH_PASS: I1(c->stencil[0].zpass); break;
    case GL_STENCIL_CLEAR_VALUE: I1(c->clear_stencil); break;
    case GL_PACK_ALIGNMENT: I1(c->pack_align); break;
    case GL_UNPACK_ALIGNMENT: I1(c->unpack_align); break;
    case GL_UNPACK_ROW_LENGTH: I1(c->unpack_row_length); break;
    case GL_BLEND: case GL_CULL_FACE: case GL_DEPTH_TEST: case GL_STENCIL_TEST: case GL_SCISSOR_TEST:
    case GL_POLYGON_OFFSET_FILL: case GL_RASTERIZER_DISCARD: case GL_DITHER:
        I1(*cap(c, p)); break;
    case GL_POLYGON_OFFSET_FACTOR: iv[0] = (GLint)c->poly_factor, fv[0] = c->poly_factor; break;
    case GL_POLYGON_OFFSET_UNITS: iv[0] = (GLint)c->poly_units, fv[0] = c->poly_units; break;
    case GL_DEPTH_CLEAR_VALUE: iv[0] = (GLint)c->clear_depth, fv[0] = c->clear_depth; break;
    case GL_COLOR_CLEAR_VALUE: case GL_BLEND_COLOR: {
        const float *v = p == GL_BLEND_COLOR ? c->blend_color : c->clear_color;
        for (int i = 0; i < 4; i++) fv[i] = v[i], iv[i] = (GLint)v[i];
        n = 4;
        break;
    }
    case GL_DEPTH_RANGE: fv[0] = c->depth_near, fv[1] = c->depth_far, iv[0] = 0, iv[1] = 1, n = 2; break;
    default:
        mg_log("mtlgles: glGet 0x%x not known\n", p);
        mg_error(GL_INVALID_ENUM);
        iv[0] = 0, fv[0] = 0;
        return 0;
    }
    return n;
#undef I1
#undef I2
#undef I4
}
static void glGetIntegerv_(GLenum p, GLint *v)
{
    C;
    GLint iv[16];
    float fv[16];
    int n = query(c, p, iv, fv);
    for (int i = 0; i < n; i++) v[i] = iv[i];
}
static void glGetFloatv_(GLenum p, GLfloat *v)
{
    C;
    GLint iv[16];
    float fv[16];
    int n = query(c, p, iv, fv);
    for (int i = 0; i < n; i++) v[i] = fv[i];
}
static void glGetBooleanv_(GLenum p, GLboolean *v)
{
    C;
    GLint iv[16];
    float fv[16];
    int n = query(c, p, iv, fv);
    for (int i = 0; i < n; i++) v[i] = iv[i] != 0;
}
static void glGetShaderPrecisionFormat_(GLenum shader, GLenum prec, GLint *range, GLint *precision)
{
    (void)shader;
    if (prec >= GL_LOW_FLOAT && prec <= GL_LOW_FLOAT + 2) range[0] = 127, range[1] = 127, *precision = 23;
    else range[0] = 31, range[1] = 30, *precision = 0;
}

// ---- entry points by name -----------------------------------------------------------------------
#define E(n) {#n, (void *)n##_}
static const struct { const char *name; void *fn; } entries[] = {
    E(glEnable), E(glDisable), E(glIsEnabled), E(glBlendFunc), E(glBlendFuncSeparate), E(glBlendEquation),
    E(glBlendEquationSeparate), E(glBlendColor), E(glColorMask), E(glDepthFunc), E(glDepthMask), E(glDepthRangef),
    E(glClearColor), E(glClearDepthf), E(glClearStencil), E(glStencilFunc), E(glStencilFuncSeparate),
    E(glStencilOp), E(glStencilOpSeparate), E(glStencilMask), E(glStencilMaskSeparate), E(glCullFace),
    E(glFrontFace), E(glLineWidth), E(glPolygonOffset), E(glViewport), E(glScissor), E(glPixelStorei), E(glHint),
    E(glSampleCoverage), E(glGetError), E(glFlush), E(glFinish), E(glReleaseShaderCompiler), E(glFenceSync),
    E(glClientWaitSync), E(glWaitSync), E(glDeleteSync), E(glIsSync), E(glGetSynciv), E(glGetString),
    E(glGetStringi), E(glGetIntegerv), E(glGetFloatv), E(glGetBooleanv), E(glGetShaderPrecisionFormat),
};

void *mtlgles_proc(const char *name)
{
    for (size_t i = 0; i < sizeof entries / sizeof entries[0]; i++)
        if (!strcmp(entries[i].name, name)) return entries[i].fn;
    void *p = mg_objects_proc(name);
    if (!p) p = mg_program_proc(name);
    if (!p) p = mg_draw_proc(name);
    if (!p) {
        // OES/EXT names of core functions
        size_t n = strlen(name);
        char base[128];
        if (n > 3 && n < sizeof base && (!strcmp(name + n - 3, "OES") || !strcmp(name + n - 3, "EXT"))) {
            memcpy(base, name, n - 3);
            base[n - 3] = 0;
            return mtlgles_proc(base);
        }
        mg_log("mtlgles: no %s\n", name);
    }
    return p;
}
