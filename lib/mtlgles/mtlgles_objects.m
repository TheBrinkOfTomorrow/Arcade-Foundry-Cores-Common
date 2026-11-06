// mtlgles: buffers, textures, renderbuffers, framebuffers, reads and copies.
// BSD-3-Clause.
#include "mtlgles_internal.h"
#include <stdlib.h>
#include <string.h>

#define C mtlgles_context *c = mg_cur; if (!c) return
#define CR(v) mtlgles_context *c = mg_cur; if (!c) return v

// ---- buffers ------------------------------------------------------------------------------------
static GLuint *buffer_slot(mtlgles_context *c, GLenum target)
{
    switch (target) {
    case GL_ARRAY_BUFFER: return &c->bound_array;
    case GL_ELEMENT_ARRAY_BUFFER: return &c->bound_element;
    case GL_PIXEL_PACK_BUFFER: return &c->bound_pack;
    case GL_PIXEL_UNPACK_BUFFER: return &c->bound_unpack;
    case GL_COPY_READ_BUFFER: return &c->bound_copy_read;
    case GL_COPY_WRITE_BUFFER: return &c->bound_copy_write;
    }
    return NULL;
}
static mg_buffer *bound_buffer(mtlgles_context *c, GLenum target)
{
    GLuint *s = buffer_slot(c, target);
    return s ? mg_get(&c->buffers, *s) : NULL;
}

static void glGenBuffers_(GLsizei n, GLuint *out)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_buffer *b = calloc(1, sizeof *b);
        out[i] = b->name = mg_gen(&c->buffers, b);
    }
}
static void glDeleteBuffers_(GLsizei n, const GLuint *names)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_buffer *b = mg_get(&c->buffers, names[i]);
        if (!b) continue;
        GLuint *slots[] = {&c->bound_array, &c->bound_element, &c->bound_pack, &c->bound_unpack,
                           &c->bound_copy_read, &c->bound_copy_write};
        for (int k = 0; k < 6; k++)
            if (*slots[k] == names[i]) *slots[k] = 0;
        for (int k = 0; k < MG_MAX_ATTRIBS; k++)
            if (c->attr[k].buffer == names[i]) c->attr[k].buffer = 0;
        mg_defer(c, b->mb);
        for (mg_repack *r = b->repacks, *n; r; r = n) n = r->next, mg_defer(c, r->mb), free(r);
        mg_del(&c->buffers, names[i]);
        free(b);
    }
}
static void glBindBuffer_(GLenum target, GLuint name)
{
    C;
    GLuint *s = buffer_slot(c, target);
    if (!s) return mg_error(GL_INVALID_ENUM);
    if (name && !mg_get(&c->buffers, name)) {   // a name never generated: made here, as GL does
        mg_buffer *b = calloc(1, sizeof *b);
        b->name = name;
        mg_put(&c->buffers, name, b);
    }
    *s = name;
}
static GLboolean glIsBuffer_(GLuint name) { CR(0); return mg_get(&c->buffers, name) != NULL; }

// a new store; the old one is released once the GPU is past it
static void buffer_store(mtlgles_context *c, mg_buffer *b, size_t size, const void *data)
{
    if (b->mb) mg_defer(c, b->mb);
    size_t alloc = size < 16 ? 16 : size;
    b->mb = data ? [c->dev newBufferWithBytes:data length:size ? size : 1 options:MTLResourceStorageModeShared]
                 : [c->dev newBufferWithLength:alloc options:MTLResourceStorageModeShared];
    b->size = size;
    b->last_use = 0;
    b->gen++;
}
static void glBufferData_(GLenum target, GLsizeiptr size, const void *data, GLenum usage)
{
    C;
    mg_buffer *b = bound_buffer(c, target);
    if (!b) return mg_error(GL_INVALID_OPERATION);
    if (b->mb && b->size == (size_t)size && !mg_busy(c, b->last_use)) {
        if (data) memcpy(b->mb.contents, data, (size_t)size);
        b->gen++;
    } else {
        buffer_store(c, b, (size_t)size, data);
    }
    b->usage = usage;
}
static void glBufferSubData_(GLenum target, GLintptr off, GLsizeiptr size, const void *data)
{
    C;
    mg_buffer *b = bound_buffer(c, target);
    if (!b || !b->mb || (size_t)(off + size) > b->size) return mg_error(GL_INVALID_VALUE);
    if (mg_busy(c, b->last_use)) {
        // in use by the GPU: a copy to change (what was drawn keeps the old contents)
        id<MTLBuffer> old = [b->mb retain];
        buffer_store(c, b, b->size, old.contents);
        [old release];
    }
    memcpy((uint8_t *)b->mb.contents + off, data, (size_t)size);
    b->gen++;
}
static void *glMapBufferRange_(GLenum target, GLintptr off, GLsizeiptr len, GLbitfield access)
{
    CR(NULL);
    mg_buffer *b = bound_buffer(c, target);
    if (!b || !b->mb) {
        mg_error(GL_INVALID_OPERATION);
        return NULL;
    }
    if (mg_busy(c, b->last_use) && !(access & GL_MAP_UNSYNCHRONIZED_BIT)) {
        if ((access & GL_MAP_INVALIDATE_BUFFER_BIT) && !(access & GL_MAP_READ_BIT)) buffer_store(c, b, b->size, NULL);
        else mg_wait(c, b->last_use);
    }
    b->mapped = 1;
    b->map_off = (size_t)off, b->map_len = (size_t)len;
    b->gen++;
    return (uint8_t *)b->mb.contents + off;
}
static void *glMapBuffer_(GLenum target, GLenum access)
{
    (void)access;
    CR(NULL);
    mg_buffer *b = bound_buffer(c, target);
    return glMapBufferRange_(target, 0, b ? (GLsizeiptr)b->size : 0, GL_MAP_WRITE_BIT | GL_MAP_READ_BIT);
}
static GLboolean glUnmapBuffer_(GLenum target)
{
    CR(0);
    mg_buffer *b = bound_buffer(c, target);
    if (b) b->mapped = 0, b->gen++;   // written through the mapping
    return 1;
}
static void glFlushMappedBufferRange_(GLenum target, GLintptr off, GLsizeiptr len) { (void)target, (void)off, (void)len; }
static void glGetBufferParameteriv_(GLenum target, GLenum p, GLint *v)
{
    C;
    mg_buffer *b = bound_buffer(c, target);
    if (!b) return mg_error(GL_INVALID_OPERATION);
    *v = p == GL_BUFFER_SIZE ? (GLint)b->size : p == GL_BUFFER_USAGE ? (GLint)b->usage : p == GL_BUFFER_MAPPED ? b->mapped : 0;
}
static void glCopyBufferSubData_(GLenum rt, GLenum wt, GLintptr ro, GLintptr wo, GLsizeiptr size)
{
    C;
    mg_buffer *r = bound_buffer(c, rt), *w = bound_buffer(c, wt);
    if (!r || !w || !r->mb || !w->mb) return mg_error(GL_INVALID_OPERATION);
    [mg_blit(c) copyFromBuffer:r->mb sourceOffset:(NSUInteger)ro toBuffer:w->mb destinationOffset:(NSUInteger)wo
                          size:(NSUInteger)size];
    r->last_use = w->last_use = c->serial;
    w->gen++;
}

// ---- textures: formats --------------------------------------------------------------------------
enum { CV_NONE, CV_RGB8, CV_565, CV_4444, CV_5551, CV_RGB32F, CV_RGB16F };
typedef struct {
    MTLPixelFormat fmt;
    int src_bpp, dst_bpp, conv;
    int swizzle;                       // 0: none, 1: luminance, 2: alpha, 3: luminance+alpha, 4: alpha 1
    int depth, stencil;
    int block;                         // compressed: bytes per 4x4 block
} fmtinfo;

static int pick_format(GLenum internal, GLenum format, GLenum type, fmtinfo *f)
{
    memset(f, 0, sizeof *f);
    if (format == GL_DEPTH_COMPONENT || internal == GL_DEPTH_COMPONENT16 || internal == GL_DEPTH_COMPONENT24 ||
        internal == GL_DEPTH_COMPONENT32F || internal == GL_DEPTH_COMPONENT32_OES) {
        f->fmt = MTLPixelFormatDepth32Float;
        f->depth = 1;
        f->src_bpp = type == GL_UNSIGNED_SHORT ? 2 : 4;
        f->dst_bpp = 4;
        return 1;
    }
    if (format == GL_DEPTH_STENCIL || internal == GL_DEPTH24_STENCIL8 || internal == GL_DEPTH32F_STENCIL8) {
        f->fmt = MTLPixelFormatDepth32Float_Stencil8;
        f->depth = f->stencil = 1;
        f->src_bpp = 4;
        f->dst_bpp = 8;
        return 1;
    }
    switch (type) {
    case GL_UNSIGNED_BYTE:
        switch (format) {
        case GL_RGBA: f->fmt = MTLPixelFormatRGBA8Unorm, f->src_bpp = f->dst_bpp = 4; return 1;
        case GL_BGRA_EXT: f->fmt = MTLPixelFormatBGRA8Unorm, f->src_bpp = f->dst_bpp = 4; return 1;
        case GL_RGB: f->fmt = MTLPixelFormatRGBA8Unorm, f->src_bpp = 3, f->dst_bpp = 4, f->conv = CV_RGB8; return 1;
        case GL_LUMINANCE: f->fmt = MTLPixelFormatR8Unorm, f->src_bpp = f->dst_bpp = 1, f->swizzle = 1; return 1;
        case GL_ALPHA: f->fmt = MTLPixelFormatR8Unorm, f->src_bpp = f->dst_bpp = 1, f->swizzle = 2; return 1;
        case GL_LUMINANCE_ALPHA: f->fmt = MTLPixelFormatRG8Unorm, f->src_bpp = f->dst_bpp = 2, f->swizzle = 3; return 1;
        case GL_RED: f->fmt = MTLPixelFormatR8Unorm, f->src_bpp = f->dst_bpp = 1; return 1;
        case GL_RG: f->fmt = MTLPixelFormatRG8Unorm, f->src_bpp = f->dst_bpp = 2; return 1;
        }
        break;
    case GL_UNSIGNED_SHORT_5_6_5:
        f->fmt = MTLPixelFormatRGBA8Unorm, f->src_bpp = 2, f->dst_bpp = 4, f->conv = CV_565;
        return 1;
    case GL_UNSIGNED_SHORT_4_4_4_4:
        f->fmt = MTLPixelFormatRGBA8Unorm, f->src_bpp = 2, f->dst_bpp = 4, f->conv = CV_4444;
        return 1;
    case GL_UNSIGNED_SHORT_5_5_5_1:
        f->fmt = MTLPixelFormatRGBA8Unorm, f->src_bpp = 2, f->dst_bpp = 4, f->conv = CV_5551;
        return 1;
    case GL_FLOAT:
        if (format == GL_RGBA) { f->fmt = MTLPixelFormatRGBA32Float, f->src_bpp = f->dst_bpp = 16; return 1; }
        if (format == GL_RGB) { f->fmt = MTLPixelFormatRGBA32Float, f->src_bpp = 12, f->dst_bpp = 16, f->conv = CV_RGB32F; return 1; }
        if (format == GL_RED || format == GL_LUMINANCE) { f->fmt = MTLPixelFormatR32Float, f->src_bpp = f->dst_bpp = 4; return 1; }
        break;
    case GL_HALF_FLOAT:
    case GL_HALF_FLOAT_OES:
        if (format == GL_RGBA) { f->fmt = MTLPixelFormatRGBA16Float, f->src_bpp = f->dst_bpp = 8; return 1; }
        if (format == GL_RGB) { f->fmt = MTLPixelFormatRGBA16Float, f->src_bpp = 6, f->dst_bpp = 8, f->conv = CV_RGB16F; return 1; }
        break;
    }
    // sized formats with an unsized-looking call
    if (internal == GL_RGBA8 || internal == GL_RGB8) {
        f->fmt = MTLPixelFormatRGBA8Unorm, f->src_bpp = f->dst_bpp = 4;
        return 1;
    }
    return 0;
}

// GL reads alpha as 1 from a texture whose internal format has none, whatever
// the data held: gl4es (DXT1 decoded, GL_RGB storage) and games upload RGBA
// data into GL_RGB textures, and the alpha byte is often 0 (HotD4 normal maps)
static int no_alpha(GLenum internal)
{
    switch (internal) {
    case GL_RGB: case GL_RGB8: case GL_RGB565: case GL_RGB16F: case GL_RGB32F:
    case 0x8C41:   // GL_SRGB8
    case 0x83F0:   // GL_COMPRESSED_RGB_S3TC_DXT1_EXT
        return 1;
    }
    return 0;
}
static void force_alpha(const fmtinfo *f, uint8_t *px, int w, int h)
{
    size_t n = (size_t)w * (size_t)h;
    if (f->fmt == MTLPixelFormatRGBA8Unorm || f->fmt == MTLPixelFormatBGRA8Unorm)
        for (size_t i = 0; i < n; i++) px[4 * i + 3] = 0xff;
    else if (f->fmt == MTLPixelFormatRGBA16Float)
        for (size_t i = 0; i < n; i++) ((uint16_t *)px)[4 * i + 3] = 0x3c00;
    else if (f->fmt == MTLPixelFormatRGBA32Float)
        for (size_t i = 0; i < n; i++) ((float *)px)[4 * i + 3] = 1.0f;
}

// rows of the source (OpenGL's unpack rules) to tightly packed rows of the texture's format
static uint8_t *convert(mtlgles_context *c, const fmtinfo *f, const void *src, int w, int h)
{
    size_t row_px = c->unpack_row_length ? (size_t)c->unpack_row_length : (size_t)w;
    size_t stride = row_px * (size_t)f->src_bpp;
    size_t a = (size_t)(c->unpack_align ? c->unpack_align : 4);
    stride = (stride + a - 1) / a * a;
    uint8_t *dst = malloc((size_t)w * h * f->dst_bpp + 1);
    for (int y = 0; y < h; y++) {
        const uint8_t *s = (const uint8_t *)src + stride * (size_t)y;
        uint8_t *d = dst + (size_t)w * f->dst_bpp * (size_t)y;
        switch (f->conv) {
        case CV_NONE:
            if (f->depth && f->src_bpp == 2) {   // 16-bit depth to float
                for (int x = 0; x < w; x++) ((float *)d)[x] = ((const uint16_t *)s)[x] / 65535.0f;
            } else if (f->depth && f->stencil) {  // 24_8 to 32F_8 (rare: depth is drawn, not uploaded)
                for (int x = 0; x < w; x++) {
                    uint32_t v = ((const uint32_t *)s)[x];
                    ((float *)d)[2 * x] = (v >> 8) / 16777215.0f;
                    ((uint32_t *)d)[2 * x + 1] = v & 0xff;
                }
            } else if (f->depth && !f->stencil && f->src_bpp == 4 && 0) {
            } else {
                memcpy(d, s, (size_t)w * f->dst_bpp);
            }
            break;
        case CV_RGB8:
            for (int x = 0; x < w; x++) d[4 * x] = s[3 * x], d[4 * x + 1] = s[3 * x + 1], d[4 * x + 2] = s[3 * x + 2], d[4 * x + 3] = 255;
            break;
        case CV_565:
            for (int x = 0; x < w; x++) {
                uint16_t v = ((const uint16_t *)s)[x];
                d[4 * x] = (uint8_t)((v >> 11) * 255 / 31), d[4 * x + 1] = (uint8_t)(((v >> 5) & 63) * 255 / 63);
                d[4 * x + 2] = (uint8_t)((v & 31) * 255 / 31), d[4 * x + 3] = 255;
            }
            break;
        case CV_4444:
            for (int x = 0; x < w; x++) {
                uint16_t v = ((const uint16_t *)s)[x];
                d[4 * x] = (uint8_t)((v >> 12) * 17), d[4 * x + 1] = (uint8_t)(((v >> 8) & 15) * 17);
                d[4 * x + 2] = (uint8_t)(((v >> 4) & 15) * 17), d[4 * x + 3] = (uint8_t)((v & 15) * 17);
            }
            break;
        case CV_5551:
            for (int x = 0; x < w; x++) {
                uint16_t v = ((const uint16_t *)s)[x];
                d[4 * x] = (uint8_t)((v >> 11) * 255 / 31), d[4 * x + 1] = (uint8_t)(((v >> 6) & 31) * 255 / 31);
                d[4 * x + 2] = (uint8_t)(((v >> 1) & 31) * 255 / 31), d[4 * x + 3] = (v & 1) ? 255 : 0;
            }
            break;
        case CV_RGB32F:
            for (int x = 0; x < w; x++) {
                memcpy(d + 16 * x, s + 12 * x, 12);
                ((float *)(d + 16 * x))[3] = 1.0f;
            }
            break;
        case CV_RGB16F:
            for (int x = 0; x < w; x++) {
                memcpy(d + 8 * x, s + 6 * x, 6);
                ((uint16_t *)(d + 8 * x))[3] = 0x3c00;
            }
            break;
        }
    }
    return dst;
}

// ---- textures -----------------------------------------------------------------------------------
mg_texture *mg_tex_bound(mtlgles_context *c, GLenum target)
{
    GLuint name = target == GL_TEXTURE_CUBE_MAP || (target >= GL_TEXTURE_CUBE_MAP_POSITIVE_X && target < GL_TEXTURE_CUBE_MAP_POSITIVE_X + 6)
                      ? c->unit_cube[c->active_unit]
                      : c->unit_2d[c->active_unit];
    return mg_get(&c->textures, name);
}

static mg_texture *new_texture(GLuint name)
{
    mg_texture *t = calloc(1, sizeof *t);
    t->name = name;
    t->min_filter = GL_NEAREST_MIPMAP_LINEAR;
    t->mag_filter = GL_LINEAR;
    t->wrap_s = t->wrap_t = GL_REPEAT;
    t->min_lod = -1000, t->max_lod = 1000;
    t->max_level = 1000;
    return t;
}

static void glGenTextures_(GLsizei n, GLuint *out)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_texture *t = new_texture(0);
        out[i] = t->name = mg_gen(&c->textures, t);
    }
}
static void glDeleteTextures_(GLsizei n, const GLuint *names)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_texture *t = mg_get(&c->textures, names[i]);
        if (!t) continue;
        for (int u = 0; u < MG_MAX_UNITS; u++) {
            if (c->unit_2d[u] == names[i]) c->unit_2d[u] = 0;
            if (c->unit_cube[u] == names[i]) c->unit_cube[u] = 0;
        }
        mg_defer(c, t->mt);
        mg_del(&c->textures, names[i]);
        free(t);
    }
}
static void glBindTexture_(GLenum target, GLuint name)
{
    C;
    mg_texture *t = mg_get(&c->textures, name);
    if (name && !t) {
        t = new_texture(name);
        mg_put(&c->textures, name, t);
    }
    if (t && !t->target) t->target = target;
    if (target == GL_TEXTURE_CUBE_MAP) c->unit_cube[c->active_unit] = name;
    else c->unit_2d[c->active_unit] = name;
}
static GLboolean glIsTexture_(GLuint name) { CR(0); return mg_get(&c->textures, name) != NULL; }
static void glActiveTexture_(GLenum unit)
{
    C;
    int u = (int)(unit - GL_TEXTURE0);
    if (u < 0 || u >= MG_MAX_UNITS) return mg_error(GL_INVALID_ENUM);
    c->active_unit = u;
}

static int mip_count(int w, int h)
{
    int n = 1, m = w > h ? w : h;
    while (m > 1) m >>= 1, n++;
    return n;
}

// the texture's Metal storage for level `level` of w x h in format f (made again when it changes)
static int texture_storage(mtlgles_context *c, mg_texture *t, int level, int w, int h, const fmtinfo *f, GLenum internal)
{
    int w0 = w << level, h0 = h << level;
    if (t->mt && t->fmt == f->fmt && t->w == w0 && t->h == h0) return 1;
    if (t->mt && level > 0) {
        // a lower level of the texture there is: sizes are max(1, size >> level), so
        // a non-square texture's last levels (256x64: 2x1, 1x1) are one too (HotD4's
        // DXT textures, decoded level by level by gl4es, were made again at their
        // last level's size: every level but that one was then empty, alpha 0, and
        // the alpha test dropped most of stage 1 and the cutscenes' scenery)
        int lw = t->w >> level, lh = t->h >> level;
        if (t->fmt == f->fmt && level < t->levels && (lw ? lw : 1) == w && (lh ? lh : 1) == h) return 1;
    }
    if (t->mt) mg_defer(c, t->mt);
    int cube = t->target == GL_TEXTURE_CUBE_MAP;
    MTLTextureDescriptor *d = cube ? [MTLTextureDescriptor textureCubeDescriptorWithPixelFormat:f->fmt size:(NSUInteger)w0 mipmapped:YES]
                                   : [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:f->fmt width:(NSUInteger)(w0 ? w0 : 1)
                                                                                         height:(NSUInteger)(h0 ? h0 : 1) mipmapped:YES];
    d.mipmapLevelCount = (NSUInteger)mip_count(w0 ? w0 : 1, h0 ? h0 : 1);
    d.storageMode = MTLStorageModePrivate;
    // Metal: a swizzled or compressed texture cannot be a render target (sampled only)
    d.usage = MTLTextureUsageShaderRead | (f->swizzle || f->block ? 0 : MTLTextureUsageRenderTarget);
    if (f->swizzle == 1) d.swizzle = MTLTextureSwizzleChannelsMake(MTLTextureSwizzleRed, MTLTextureSwizzleRed, MTLTextureSwizzleRed, MTLTextureSwizzleOne);
    else if (f->swizzle == 2) d.swizzle = MTLTextureSwizzleChannelsMake(MTLTextureSwizzleZero, MTLTextureSwizzleZero, MTLTextureSwizzleZero, MTLTextureSwizzleRed);
    else if (f->swizzle == 3) d.swizzle = MTLTextureSwizzleChannelsMake(MTLTextureSwizzleRed, MTLTextureSwizzleRed, MTLTextureSwizzleRed, MTLTextureSwizzleGreen);
    else if (f->swizzle == 4) d.swizzle = MTLTextureSwizzleChannelsMake(MTLTextureSwizzleRed, MTLTextureSwizzleGreen, MTLTextureSwizzleBlue, MTLTextureSwizzleOne);
    t->mt = [c->dev newTextureWithDescriptor:d];
    if (!t->mt) {
        mg_log("mtlgles: cannot make a %dx%d texture (format %d)\n", w0, h0, (int)f->fmt);
        return 0;
    }
    t->w = w0, t->h = h0;
    t->levels = (int)d.mipmapLevelCount;
    t->fmt = f->fmt;
    t->internal = internal;
    t->depth = f->depth, t->stencil = f->stencil;
    t->block = f->block;
    t->last_use = 0;
    return 1;
}

// pixels into a region of a level: through a staging buffer and a blit, in
// the upload command buffer when the current one has not used the texture
static void texture_upload(mtlgles_context *c, mg_texture *t, int level, int face, int x, int y, int w, int h,
                           const uint8_t *px, int bpp)
{
    if (w <= 0 || h <= 0) return;
    size_t row = (size_t)w * bpp, size = row * h;
    id<MTLBuffer> staging = [c->dev newBufferWithBytes:px length:size options:MTLResourceStorageModeShared];
    id<MTLBlitCommandEncoder> b = t->last_use == c->serial && c->cmd ? mg_blit(c) : mg_upload_blit(c);
    MTLBlitOption opt = MTLBlitOptionNone;
    if (t->depth && t->stencil) opt = MTLBlitOptionDepthFromDepthStencil;
    [b copyFromBuffer:staging sourceOffset:0 sourceBytesPerRow:row sourceBytesPerImage:size
           sourceSize:MTLSizeMake((NSUInteger)w, (NSUInteger)h, 1) toTexture:t->mt destinationSlice:(NSUInteger)face
     destinationLevel:(NSUInteger)level destinationOrigin:MTLOriginMake((NSUInteger)x, (NSUInteger)y, 0) options:opt];
    mg_defer(c, staging);
    c->n_uploads++;
}

static int cube_face(GLenum target)
{
    return target >= GL_TEXTURE_CUBE_MAP_POSITIVE_X && target < GL_TEXTURE_CUBE_MAP_POSITIVE_X + 6
               ? (int)(target - GL_TEXTURE_CUBE_MAP_POSITIVE_X)
               : 0;
}

static void glTexImage2D_(GLenum target, GLint level, GLint internal, GLsizei w, GLsizei h, GLint border,
                          GLenum format, GLenum type, const void *data)
{
    (void)border;
    C;
    mg_texture *t = mg_tex_bound(c, target);
    if (!t) return mg_error(GL_INVALID_OPERATION);
    if (!t->target) t->target = cube_face(target) || target != GL_TEXTURE_2D ? GL_TEXTURE_CUBE_MAP : GL_TEXTURE_2D;
    fmtinfo f;
    if (!pick_format((GLenum)internal, format, type, &f)) {
        mg_log("mtlgles: texture format 0x%x/0x%x/0x%x not done\n", internal, format, type);
        return mg_error(GL_INVALID_ENUM);
    }
    if (!texture_storage(c, t, level, w, h, &f, (GLenum)internal)) return mg_error(GL_OUT_OF_MEMORY);
    mg_buffer *ub = mg_get(&c->buffers, c->bound_unpack);
    if (ub && ub->mb) data = (const uint8_t *)ub->mb.contents + (uintptr_t)data;
    if (!data || f.depth) return;   // depth textures are drawn into
    uint8_t *px = convert(c, &f, data, w, h);
    if (no_alpha(t->internal)) force_alpha(&f, px, w, h);
    texture_upload(c, t, level, cube_face(target), 0, 0, w, h, px, f.dst_bpp);
    free(px);
}
static void glTexSubImage2D_(GLenum target, GLint level, GLint x, GLint y, GLsizei w, GLsizei h, GLenum format,
                             GLenum type, const void *data)
{
    C;
    mg_texture *t = mg_tex_bound(c, target);
    if (!t || !t->mt) return mg_error(GL_INVALID_OPERATION);
    fmtinfo f;
    if (!pick_format(t->internal, format, type, &f)) return mg_error(GL_INVALID_ENUM);
    if (f.fmt != t->fmt) {
        // e.g. BGRA data into an RGBA texture: swap on the CPU
        if (!(f.fmt == MTLPixelFormatBGRA8Unorm && t->fmt == MTLPixelFormatRGBA8Unorm) &&
            !(f.fmt == MTLPixelFormatRGBA8Unorm && t->fmt == MTLPixelFormatBGRA8Unorm)) {
            mg_log("mtlgles: glTexSubImage2D format %d into %d\n", (int)f.fmt, (int)t->fmt);
            return mg_error(GL_INVALID_OPERATION);
        }
    }
    mg_buffer *ub = mg_get(&c->buffers, c->bound_unpack);
    if (ub && ub->mb) data = (const uint8_t *)ub->mb.contents + (uintptr_t)data;
    if (!data) return;
    uint8_t *px = convert(c, &f, data, w, h);
    if (f.fmt != t->fmt)
        for (size_t i = 0; i < (size_t)w * h; i++) {
            uint8_t s = px[4 * i];
            px[4 * i] = px[4 * i + 2], px[4 * i + 2] = s;
        }
    if (no_alpha(t->internal)) force_alpha(&f, px, w, h);
    texture_upload(c, t, level, cube_face(target), x, y, w, h, px, f.dst_bpp);
    free(px);
}
// ---- textures: S3TC (DXT1/3/5) as BC1-3, where the device samples them ----------------------------
// (DXT1 without alpha: a texel BC1 makes transparent reads opaque black in OpenGL)
static int pick_s3tc(mtlgles_context *c, GLenum internal, fmtinfo *f)
{
    memset(f, 0, sizeof *f);
    if (!c->bc) return 0;
    switch (internal) {
    case GL_COMPRESSED_RGB_S3TC_DXT1_EXT: f->fmt = MTLPixelFormatBC1_RGBA, f->block = 8, f->swizzle = 4; return 1;
    case GL_COMPRESSED_RGBA_S3TC_DXT1_EXT: f->fmt = MTLPixelFormatBC1_RGBA, f->block = 8; return 1;
    case GL_COMPRESSED_RGBA_S3TC_DXT3_EXT: f->fmt = MTLPixelFormatBC2_RGBA, f->block = 16; return 1;
    case GL_COMPRESSED_RGBA_S3TC_DXT5_EXT: f->fmt = MTLPixelFormatBC3_RGBA, f->block = 16; return 1;
    }
    return 0;
}
// blocks into a region of a level (a region of whole blocks, or one reaching the level's edge)
static void texture_upload_blocks(mtlgles_context *c, mg_texture *t, int level, int face, int x, int y, int w, int h,
                                  const uint8_t *data, int block)
{
    if (w <= 0 || h <= 0) return;
    size_t row = (size_t)((w + 3) / 4) * (size_t)block, size = row * (size_t)((h + 3) / 4);
    id<MTLBuffer> staging = [c->dev newBufferWithBytes:data length:size options:MTLResourceStorageModeShared];
    id<MTLBlitCommandEncoder> b = t->last_use == c->serial && c->cmd ? mg_blit(c) : mg_upload_blit(c);
    [b copyFromBuffer:staging sourceOffset:0 sourceBytesPerRow:row sourceBytesPerImage:size
           sourceSize:MTLSizeMake((NSUInteger)w, (NSUInteger)h, 1) toTexture:t->mt destinationSlice:(NSUInteger)face
     destinationLevel:(NSUInteger)level destinationOrigin:MTLOriginMake((NSUInteger)x, (NSUInteger)y, 0)];
    mg_defer(c, staging);
    c->n_uploads++;
}
static void glCompressedTexImage2D_(GLenum target, GLint level, GLenum internal, GLsizei w, GLsizei h, GLint border,
                                    GLsizei size, const void *data)
{
    (void)border;
    C;
    mg_texture *t = mg_tex_bound(c, target);
    if (!t) return mg_error(GL_INVALID_OPERATION);
    fmtinfo f;
    if (!pick_s3tc(c, internal, &f)) {
        mg_log("mtlgles: compressed texture 0x%x not done\n", internal);
        return mg_error(GL_INVALID_ENUM);
    }
    if (size < ((w + 3) / 4) * ((h + 3) / 4) * f.block) return mg_error(GL_INVALID_VALUE);
    if (!t->target) t->target = cube_face(target) || target != GL_TEXTURE_2D ? GL_TEXTURE_CUBE_MAP : GL_TEXTURE_2D;
    if (!texture_storage(c, t, level, w, h, &f, internal)) return mg_error(GL_OUT_OF_MEMORY);
    mg_buffer *ub = mg_get(&c->buffers, c->bound_unpack);
    if (ub && ub->mb) data = (const uint8_t *)ub->mb.contents + (uintptr_t)data;
    if (data) texture_upload_blocks(c, t, level, cube_face(target), 0, 0, w, h, data, f.block);
}
static void glCompressedTexSubImage2D_(GLenum target, GLint level, GLint x, GLint y, GLsizei w, GLsizei h,
                                       GLenum format, GLsizei size, const void *data)
{
    C;
    mg_texture *t = mg_tex_bound(c, target);
    fmtinfo f;
    if (!t || !t->mt || !t->block || !pick_s3tc(c, format, &f) || f.fmt != t->fmt || (x & 3) || (y & 3))
        return mg_error(GL_INVALID_OPERATION);
    if (size < ((w + 3) / 4) * ((h + 3) / 4) * f.block) return mg_error(GL_INVALID_VALUE);
    mg_buffer *ub = mg_get(&c->buffers, c->bound_unpack);
    if (ub && ub->mb) data = (const uint8_t *)ub->mb.contents + (uintptr_t)data;
    if (data) texture_upload_blocks(c, t, level, cube_face(target), x, y, w, h, data, f.block);
}
// OpenGL's (not ES's) read of a compressed level, block for block: gl4es asks for it
// when a DXT texture stayed compressed here and the game reads it back (Sega Race TV
// reads its cars' DXT textures at boot and recolours them; refused, the game kept
// what was in its buffer and painted the cars with it)
static void glGetCompressedTexImage_(GLenum target, GLint level, void *dst)
{
    C;
    mg_texture *t = mg_tex_bound(c, target);
    if (!t || !t->mt || !t->block || level < 0 || level >= t->levels) return mg_error(GL_INVALID_OPERATION);
    if (!dst) return;
    int w = t->w >> level, h = t->h >> level;
    if (w < 1) w = 1;
    if (h < 1) h = 1;
    size_t row = (size_t)((w + 3) / 4) * (size_t)t->block, size = row * (size_t)((h + 3) / 4);
    id<MTLBuffer> tmp = [c->dev newBufferWithLength:size options:MTLResourceStorageModeShared];
    [mg_blit(c) copyFromTexture:t->mt sourceSlice:(NSUInteger)cube_face(target) sourceLevel:(NSUInteger)level
                   sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake((NSUInteger)w, (NSUInteger)h, 1)
                       toBuffer:tmp destinationOffset:0 destinationBytesPerRow:row destinationBytesPerImage:size];
    t->last_use = c->serial;
    uint64_t s = c->serial;
    mg_flush(c);
    mg_wait(c, s);
    memcpy(dst, tmp.contents, size);
    [tmp release];
}

static void tex_param(mtlgles_context *c, GLenum target, GLenum p, float v)
{
    mg_texture *t = mg_tex_bound(c, target);
    if (!t) return mg_error(GL_INVALID_OPERATION);
    switch (p) {
    case GL_TEXTURE_MIN_FILTER: t->min_filter = (GLenum)v; break;
    case GL_TEXTURE_MAG_FILTER: t->mag_filter = (GLenum)v; break;
    case GL_TEXTURE_WRAP_S: t->wrap_s = (GLenum)v; break;
    case GL_TEXTURE_WRAP_T: t->wrap_t = (GLenum)v; break;
    case GL_TEXTURE_MIN_LOD: t->min_lod = v; break;
    case GL_TEXTURE_MAX_LOD: t->max_lod = v; break;
    case GL_TEXTURE_BASE_LEVEL: t->base_level = (int)v; break;
    case GL_TEXTURE_MAX_LEVEL: t->max_level = (int)v; break;
    default: break;   // wrap r, compare mode (gl4es compares in the shader), anisotropy
    }
}
static void glTexParameteri_(GLenum target, GLenum p, GLint v) { C; tex_param(c, target, p, (float)v); }
static void glTexParameterf_(GLenum target, GLenum p, GLfloat v) { C; tex_param(c, target, p, v); }
static void glTexParameteriv_(GLenum target, GLenum p, const GLint *v) { C; tex_param(c, target, p, (float)v[0]); }
static void glTexParameterfv_(GLenum target, GLenum p, const GLfloat *v) { C; tex_param(c, target, p, v[0]); }
static void glGetTexParameteriv_(GLenum target, GLenum p, GLint *v)
{
    C;
    mg_texture *t = mg_tex_bound(c, target);
    if (!t) return mg_error(GL_INVALID_OPERATION);
    *v = p == GL_TEXTURE_MIN_FILTER ? (GLint)t->min_filter : p == GL_TEXTURE_MAG_FILTER ? (GLint)t->mag_filter
       : p == GL_TEXTURE_WRAP_S ? (GLint)t->wrap_s : p == GL_TEXTURE_WRAP_T ? (GLint)t->wrap_t : 0;
}
static void glGenerateMipmap_(GLenum target)
{
    C;
    mg_texture *t = mg_tex_bound(c, target);
    if (!t || !t->mt || t->levels < 2 || t->depth || t->block) return;   // Metal makes no compressed mipmaps
    [mg_blit(c) generateMipmapsForTexture:t->mt];
    t->last_use = c->serial;
}

id<MTLSamplerState> mg_sampler_for(mtlgles_context *c, mg_texture *t)
{
    int mip = t->min_filter >= GL_NEAREST_MIPMAP_NEAREST && t->min_filter <= GL_LINEAR_MIPMAP_LINEAR && t->levels > 1;
    float maxl = t->max_lod;
    if (t->max_level < 1000 && (float)t->max_level < maxl) maxl = (float)t->max_level;
    if (!mip) maxl = 0;
    // the texture's own last sampler while its state is the same: the
    // dictionary's string key cost a snprintf and an NSString every slot of
    // every draw (4% of Sega Race TV's game thread)
    struct mg_sampler_key sk;
    memset(&sk, 0, sizeof sk);
    sk.min_filter = t->min_filter, sk.mag_filter = t->mag_filter, sk.wrap_s = t->wrap_s, sk.wrap_t = t->wrap_t;
    sk.mip = mip, sk.min_lod = t->min_lod < 0 ? 0 : t->min_lod, sk.max_lod = maxl;
    if (t->smp && !memcmp(&sk, &t->smp_key, sizeof sk)) return t->smp;
    memcpy(&t->smp_key, &sk, sizeof sk);
    char key[96];
    snprintf(key, sizeof key, "%x %x %x %x %d %g %g", t->min_filter, t->mag_filter, t->wrap_s, t->wrap_t, mip,
             t->min_lod < 0 ? 0 : t->min_lod, maxl);
    NSString *k = [NSString stringWithUTF8String:key];
    id<MTLSamplerState> s = c->samplers[k];
    if (s) return t->smp = s;
    MTLSamplerDescriptor *d = [[MTLSamplerDescriptor alloc] init];
    GLenum mn = t->min_filter;
    d.minFilter = (mn == GL_LINEAR || mn == GL_LINEAR_MIPMAP_NEAREST || mn == GL_LINEAR_MIPMAP_LINEAR)
                      ? MTLSamplerMinMagFilterLinear : MTLSamplerMinMagFilterNearest;
    d.magFilter = t->mag_filter == GL_LINEAR ? MTLSamplerMinMagFilterLinear : MTLSamplerMinMagFilterNearest;
    d.mipFilter = !mip ? MTLSamplerMipFilterNotMipmapped
                  : (mn == GL_NEAREST_MIPMAP_LINEAR || mn == GL_LINEAR_MIPMAP_LINEAR) ? MTLSamplerMipFilterLinear
                                                                                      : MTLSamplerMipFilterNearest;
    MTLSamplerAddressMode modes[3] = {MTLSamplerAddressModeRepeat, MTLSamplerAddressModeClampToEdge,
                                      MTLSamplerAddressModeMirrorRepeat};
    d.sAddressMode = modes[t->wrap_s == GL_CLAMP_TO_EDGE ? 1 : t->wrap_s == GL_MIRRORED_REPEAT ? 2 : 0];
    d.tAddressMode = modes[t->wrap_t == GL_CLAMP_TO_EDGE ? 1 : t->wrap_t == GL_MIRRORED_REPEAT ? 2 : 0];
    d.rAddressMode = MTLSamplerAddressModeClampToEdge;
    d.lodMinClamp = t->min_lod < 0 ? 0 : t->min_lod;
    d.lodMaxClamp = maxl;
    s = [c->dev newSamplerStateWithDescriptor:d];
    [d release];
    c->samplers[k] = s;
    [s release];
    return t->smp = s;
}

// ---- renderbuffers ------------------------------------------------------------------------------
static void glGenRenderbuffers_(GLsizei n, GLuint *out)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_renderbuffer *r = calloc(1, sizeof *r);
        out[i] = r->name = mg_gen(&c->renderbuffers, r);
    }
}
static void glDeleteRenderbuffers_(GLsizei n, const GLuint *names)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_renderbuffer *r = mg_get(&c->renderbuffers, names[i]);
        if (!r) continue;
        if (c->bound_rb == names[i]) c->bound_rb = 0;
        mg_defer(c, r->mt);
        mg_del(&c->renderbuffers, names[i]);
        free(r);
    }
}
static void glBindRenderbuffer_(GLenum target, GLuint name)
{
    (void)target;
    C;
    if (name && !mg_get(&c->renderbuffers, name)) {
        mg_renderbuffer *r = calloc(1, sizeof *r);
        r->name = name;
        mg_put(&c->renderbuffers, name, r);
    }
    c->bound_rb = name;
}
static GLboolean glIsRenderbuffer_(GLuint name) { CR(0); return mg_get(&c->renderbuffers, name) != NULL; }
static void glRenderbufferStorage_(GLenum target, GLenum internal, GLsizei w, GLsizei h)
{
    (void)target;
    C;
    mg_renderbuffer *r = mg_get(&c->renderbuffers, c->bound_rb);
    if (!r) return mg_error(GL_INVALID_OPERATION);
    MTLPixelFormat f;
    switch (internal) {
    case GL_DEPTH_COMPONENT16: case GL_DEPTH_COMPONENT24: case GL_DEPTH_COMPONENT32_OES: case GL_DEPTH_COMPONENT32F:
    case GL_DEPTH_COMPONENT:
        f = MTLPixelFormatDepth32Float;
        break;
    case GL_DEPTH24_STENCIL8: case GL_DEPTH32F_STENCIL8: case GL_DEPTH_STENCIL:
        f = MTLPixelFormatDepth32Float_Stencil8;
        break;
    case GL_STENCIL_INDEX8:
        f = MTLPixelFormatStencil8;
        break;
    case GL_RGBA16F: f = MTLPixelFormatRGBA16Float; break;
    case GL_RGBA32F: f = MTLPixelFormatRGBA32Float; break;
    case GL_BGRA8_EXT: f = MTLPixelFormatBGRA8Unorm; break;
    default: f = MTLPixelFormatRGBA8Unorm; break;
    }
    if (r->mt) mg_defer(c, r->mt);
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:f width:(NSUInteger)w
                                                                                height:(NSUInteger)h mipmapped:NO];
    d.storageMode = MTLStorageModePrivate;
    d.usage = MTLTextureUsageRenderTarget | MTLTextureUsageShaderRead;
    r->mt = [c->dev newTextureWithDescriptor:d];
    r->w = w, r->h = h, r->internal = internal, r->fmt = f;
}
static void glRenderbufferStorageMultisample_(GLenum target, GLsizei samples, GLenum internal, GLsizei w, GLsizei h)
{
    (void)samples;
    glRenderbufferStorage_(target, internal, w, h);
}
static void glGetRenderbufferParameteriv_(GLenum target, GLenum p, GLint *v)
{
    (void)target;
    C;
    mg_renderbuffer *r = mg_get(&c->renderbuffers, c->bound_rb);
    if (!r) return mg_error(GL_INVALID_OPERATION);
    *v = p == GL_RENDERBUFFER_WIDTH ? r->w : p == GL_RENDERBUFFER_HEIGHT ? r->h
       : p == GL_RENDERBUFFER_INTERNAL_FORMAT ? (GLint)r->internal : 0;
}

// ---- framebuffers -------------------------------------------------------------------------------
static void glGenFramebuffers_(GLsizei n, GLuint *out)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_framebuffer *f = calloc(1, sizeof *f);
        f->draw_buffers[0] = GL_COLOR_ATTACHMENT0;
        f->read_buffer = GL_COLOR_ATTACHMENT0;
        out[i] = f->name = mg_gen(&c->framebuffers, f);
    }
}
static void glDeleteFramebuffers_(GLsizei n, const GLuint *names)
{
    C;
    for (GLsizei i = 0; i < n; i++) {
        mg_framebuffer *f = mg_get(&c->framebuffers, names[i]);
        if (!f) continue;
        if (c->pending_clear && c->pending_fb == names[i]) mg_resolve_clear(c);
        if (c->pass_fb == names[i]) mg_end_pass(c);
        if (c->draw_fb == names[i]) c->draw_fb = 0;
        if (c->read_fb == names[i]) c->read_fb = 0;
        mg_del(&c->framebuffers, names[i]);
        free(f);
    }
}
static void glBindFramebuffer_(GLenum target, GLuint name)
{
    C;
    if (name && !mg_get(&c->framebuffers, name)) {
        mg_framebuffer *f = calloc(1, sizeof *f);
        f->name = name;
        f->draw_buffers[0] = GL_COLOR_ATTACHMENT0;
        f->read_buffer = GL_COLOR_ATTACHMENT0;
        mg_put(&c->framebuffers, name, f);
    }
    if (target == GL_FRAMEBUFFER || target == GL_DRAW_FRAMEBUFFER) c->draw_fb = name;
    if (target == GL_FRAMEBUFFER || target == GL_READ_FRAMEBUFFER) c->read_fb = name;
}
static GLboolean glIsFramebuffer_(GLuint name) { CR(0); return mg_get(&c->framebuffers, name) != NULL; }

static mg_framebuffer *target_fb(mtlgles_context *c, GLenum target)
{
    return mg_get(&c->framebuffers, target == GL_READ_FRAMEBUFFER ? c->read_fb : c->draw_fb);
}
static mg_attachment *attachment(mg_framebuffer *f, GLenum a)
{
    if (a >= GL_COLOR_ATTACHMENT0 && a < GL_COLOR_ATTACHMENT0 + MG_MAX_COLOR) return &f->color[a - GL_COLOR_ATTACHMENT0];
    if (a == GL_DEPTH_ATTACHMENT) return &f->depth;
    if (a == GL_STENCIL_ATTACHMENT) return &f->stencil;
    return NULL;
}
static id<MTLTexture> attachment_texture(mtlgles_context *c, const mg_attachment *a, int *w, int *h);
static void attach(mtlgles_context *c, GLenum target, GLenum a, GLenum type, GLuint name, int level, int face)
{
    mg_framebuffer *f = target_fb(c, target);
    if (!f) return mg_error(GL_INVALID_OPERATION);
    // a clear still pending on this framebuffer is for what's attached now:
    // done before the attachment changes (HotD4 clears a helper framebuffer,
    // then points it at another texture; the clear landed on the new one)
    if (c->pending_clear && c->pending_fb == f->name) mg_resolve_clear(c);
    if ((GLuint)c->pass_fb == f->name) mg_end_pass(c);
    if (a == GL_DEPTH_STENCIL_ATTACHMENT) {
        attach(c, target, GL_DEPTH_ATTACHMENT, type, name, level, face);
        attach(c, target, GL_STENCIL_ATTACHMENT, type, name, level, face);
        return;
    }
    mg_attachment *at = attachment(f, a);
    if (!at) return mg_error(GL_INVALID_ENUM);
    if (c->trace_depth && a != GL_STENCIL_ATTACHMENT && at == &f->depth) {
        int w = 0, h = 0;
        mg_attachment n = {name ? type : GL_NONE, name, level, face};
        id<MTLTexture> t = attachment_texture(c, &n, &w, &h);
        mg_log("mtlgles: fb %u depth <- %s %u level %d: %dx%d format %d\n", f->name,
               type == GL_RENDERBUFFER ? "renderbuffer" : "texture", name, level, w, h, t ? (int)t.pixelFormat : -1);
    }
    at->type = name ? type : GL_NONE;
    at->name = name;
    at->level = level;
    at->face = face;
}
static void glFramebufferTexture2D_(GLenum target, GLenum a, GLenum textarget, GLuint tex, GLint level)
{
    C;
    attach(c, target, a, 0x1702 /* GL_TEXTURE */, tex, level, cube_face(textarget));
}
static void glFramebufferRenderbuffer_(GLenum target, GLenum a, GLenum rbtarget, GLuint rb)
{
    (void)rbtarget;
    C;
    attach(c, target, a, GL_RENDERBUFFER, rb, 0, 0);
}

static id<MTLTexture> attachment_texture(mtlgles_context *c, const mg_attachment *a, int *w, int *h)
{
    if (a->type == GL_RENDERBUFFER) {
        mg_renderbuffer *r = mg_get(&c->renderbuffers, a->name);
        if (!r || !r->mt) return nil;
        if (w) *w = r->w, *h = r->h;
        return r->mt;
    }
    if (a->type == 0x1702) {
        mg_texture *t = mg_get(&c->textures, a->name);
        if (!t || !t->mt) return nil;
        if (w) *w = t->w >> a->level, *h = t->h >> a->level;
        if (w && !*w) *w = 1;
        if (h && !*h) *h = 1;
        t->last_use = c->serial;
        return t->mt;
    }
    return nil;
}

int mg_fb_size(mtlgles_context *c, GLuint fb, int *w, int *h)
{
    if (!fb) {
        *w = *h = 16;
        return 1;
    }
    mg_framebuffer *f = mg_get(&c->framebuffers, fb);
    if (!f) return 0;
    for (int i = 0; i < MG_MAX_COLOR; i++)
        if (attachment_texture(c, &f->color[i], w, h)) return 1;
    if (attachment_texture(c, &f->depth, w, h)) return 1;
    if (attachment_texture(c, &f->stencil, w, h)) return 1;
    return 0;
}

id<MTLTexture> mg_fb_color(mtlgles_context *c, GLuint fb, int i, int *level, int *slice)
{
    *level = *slice = 0;
    if (!fb) return i == 0 ? c->fb0_color : nil;
    mg_framebuffer *f = mg_get(&c->framebuffers, fb);
    if (!f) return nil;
    *level = f->color[i].level;
    *slice = f->color[i].face;
    return attachment_texture(c, &f->color[i], NULL, NULL);
}

id<MTLTexture> mg_fb_depth(mtlgles_context *c, GLuint fb, int stencil, int *level)
{
    *level = 0;
    if (!fb) return c->fb0_depth;
    mg_framebuffer *f = mg_get(&c->framebuffers, fb);
    if (!f) return nil;
    mg_attachment *a = stencil ? &f->stencil : &f->depth;
    *level = a->level;
    id<MTLTexture> t = attachment_texture(c, a, NULL, NULL);
    if (!t) return nil;
    MTLPixelFormat pf = t.pixelFormat;
    int has_depth = pf == MTLPixelFormatDepth32Float || pf == MTLPixelFormatDepth32Float_Stencil8 || pf == MTLPixelFormatDepth16Unorm;
    int has_stencil = pf == MTLPixelFormatStencil8 || pf == MTLPixelFormatDepth32Float_Stencil8;
    return (stencil ? has_stencil : has_depth) ? t : nil;
}

static GLenum glCheckFramebufferStatus_(GLenum target)
{
    CR(GL_FRAMEBUFFER_UNSUPPORTED);
    GLuint fb = target == GL_READ_FRAMEBUFFER ? c->read_fb : c->draw_fb;
    int w, h;
    if (!fb) return GL_FRAMEBUFFER_COMPLETE;
    return mg_fb_size(c, fb, &w, &h) ? GL_FRAMEBUFFER_COMPLETE : GL_FRAMEBUFFER_INCOMPLETE_MISSING_ATTACHMENT;
}
static void glGetFramebufferAttachmentParameteriv_(GLenum target, GLenum a, GLenum p, GLint *v)
{
    C;
    mg_framebuffer *f = target_fb(c, target);
    mg_attachment *at = f ? attachment(f, a == GL_DEPTH_STENCIL_ATTACHMENT ? GL_DEPTH_ATTACHMENT : a) : NULL;
    if (!at) return mg_error(GL_INVALID_OPERATION);
    *v = p == GL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE ? (GLint)at->type
       : p == GL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME ? (GLint)at->name : 0;
}
static void glDrawBuffers_(GLsizei n, const GLenum *bufs)
{
    C;
    mg_framebuffer *f = mg_get(&c->framebuffers, c->draw_fb);
    if (!f) return;
    if (c->pass_fb == f->name) mg_end_pass(c);
    for (int i = 0; i < MG_MAX_COLOR; i++) f->draw_buffers[i] = i < n ? bufs[i] : GL_NONE;
}
static void glReadBuffer_(GLenum b)
{
    C;
    mg_framebuffer *f = mg_get(&c->framebuffers, c->read_fb);
    if (f) f->read_buffer = b;
}
static void glDiscardFramebufferEXT_(GLenum target, GLsizei n, const GLenum *a) { (void)target, (void)n, (void)a; }
static void glInvalidateFramebuffer_(GLenum target, GLsizei n, const GLenum *a) { (void)target, (void)n, (void)a; }

// the read framebuffer's colour: the texture, its level and slice
static id<MTLTexture> read_color(mtlgles_context *c, int *level, int *slice)
{
    int i = 0;
    mg_framebuffer *f = mg_get(&c->framebuffers, c->read_fb);
    if (f && f->read_buffer >= GL_COLOR_ATTACHMENT0 && f->read_buffer < GL_COLOR_ATTACHMENT0 + MG_MAX_COLOR)
        i = (int)(f->read_buffer - GL_COLOR_ATTACHMENT0);
    return mg_fb_color(c, c->read_fb, i, level, slice);
}

// ---- reads and copies ---------------------------------------------------------------------------
static void glReadPixels_(GLint x, GLint y, GLsizei w, GLsizei h, GLenum format, GLenum type, void *dst)
{
    C;
    int level, slice;
    id<MTLTexture> src = read_color(c, &level, &slice);
    if (!src || w <= 0 || h <= 0) return mg_error(GL_INVALID_OPERATION);
    if (type != GL_UNSIGNED_BYTE || (format != GL_RGBA && format != GL_BGRA_EXT)) {
        mg_log("mtlgles: glReadPixels 0x%x/0x%x not done\n", format, type);
        return mg_error(GL_INVALID_ENUM);
    }
    int src_bgra = src.pixelFormat == MTLPixelFormatBGRA8Unorm;
    int swap = src_bgra != (format == GL_BGRA_EXT);
    size_t row = (size_t)w * 4, size = row * h;
    mg_buffer *pb = mg_get(&c->buffers, c->bound_pack);
    MTLOrigin o = MTLOriginMake((NSUInteger)x, (NSUInteger)y, 0);
    MTLSize sz = MTLSizeMake((NSUInteger)w, (NSUInteger)h, 1);
    if (pb && pb->mb) {
        // into the pixel pack buffer, on the GPU: read when mapped
        size_t off = (size_t)(uintptr_t)dst;
        if (off + size > pb->size) return mg_error(GL_INVALID_OPERATION);
        if (swap) {
            static int told;
            if (!told++) mg_log("mtlgles: glReadPixels to a buffer with red and blue swapped\n");
        }
        [mg_blit(c) copyFromTexture:src sourceSlice:(NSUInteger)slice sourceLevel:(NSUInteger)level sourceOrigin:o
                         sourceSize:sz toBuffer:pb->mb destinationOffset:off destinationBytesPerRow:row
              destinationBytesPerImage:size];
        pb->last_use = c->serial;
        return;
    }
    id<MTLBuffer> tmp = [c->dev newBufferWithLength:size options:MTLResourceStorageModeShared];
    [mg_blit(c) copyFromTexture:src sourceSlice:(NSUInteger)slice sourceLevel:(NSUInteger)level sourceOrigin:o
                     sourceSize:sz toBuffer:tmp destinationOffset:0 destinationBytesPerRow:row
          destinationBytesPerImage:size];
    uint64_t s = c->serial;
    mg_flush(c);
    mg_wait(c, s);
    size_t a = (size_t)(c->pack_align ? c->pack_align : 4), dst_row = (row + a - 1) / a * a;
    const uint8_t *p = tmp.contents;
    for (int r = 0; r < h; r++) {
        uint8_t *d = (uint8_t *)dst + dst_row * r;
        memcpy(d, p + row * r, row);
        if (swap)
            for (int i = 0; i < w; i++) {
                uint8_t t = d[4 * i];
                d[4 * i] = d[4 * i + 2], d[4 * i + 2] = t;
            }
    }
    [tmp release];
}

static void copy_region(mtlgles_context *c, id<MTLTexture> src, int slevel, int sslice, int sx, int sy,
                        id<MTLTexture> dst, int dlevel, int dslice, int dx, int dy, int w, int h)
{
    if (w <= 0 || h <= 0) return;
    if (src.pixelFormat == MTLPixelFormatDepth32Float_Stencil8 && dst.pixelFormat == MTLPixelFormatDepth32Float) {
        // the depth plane only: through a buffer (HotD4 copies its scene's depth for its depth of field)
        size_t row = (size_t)w * 4, size = row * (size_t)h;
        if (c->depth_copy.length < size) {
            if (c->depth_copy) mg_defer(c, c->depth_copy);
            c->depth_copy = [c->dev newBufferWithLength:size options:MTLResourceStorageModePrivate];
        }
        id<MTLBlitCommandEncoder> b = mg_blit(c);
        [b copyFromTexture:src sourceSlice:(NSUInteger)sslice sourceLevel:(NSUInteger)slevel
              sourceOrigin:MTLOriginMake((NSUInteger)sx, (NSUInteger)sy, 0) sourceSize:MTLSizeMake((NSUInteger)w, (NSUInteger)h, 1)
                  toBuffer:c->depth_copy destinationOffset:0 destinationBytesPerRow:row destinationBytesPerImage:size
                   options:MTLBlitOptionDepthFromDepthStencil];
        [b copyFromBuffer:c->depth_copy sourceOffset:0 sourceBytesPerRow:row sourceBytesPerImage:size
               sourceSize:MTLSizeMake((NSUInteger)w, (NSUInteger)h, 1) toTexture:dst destinationSlice:(NSUInteger)dslice
         destinationLevel:(NSUInteger)dlevel destinationOrigin:MTLOriginMake((NSUInteger)dx, (NSUInteger)dy, 0)];
        static int n;
        if (c->trace_depth && n++ % 300 == 0) {   // what arrived (waits for the GPU: debug only)
            id<MTLBuffer> r = [c->dev newBufferWithLength:size options:MTLResourceStorageModeShared];
            [mg_blit(c) copyFromTexture:dst sourceSlice:(NSUInteger)dslice sourceLevel:(NSUInteger)dlevel
                           sourceOrigin:MTLOriginMake((NSUInteger)dx, (NSUInteger)dy, 0) sourceSize:MTLSizeMake((NSUInteger)w, (NSUInteger)h, 1)
                               toBuffer:r destinationOffset:0 destinationBytesPerRow:row destinationBytesPerImage:size];
            uint64_t s = c->serial;
            mg_flush(c);
            mg_wait(c, s);
            const float *d = r.contents;
            float lo = 1e30f, hi = -1e30f;
            for (size_t i = 0; i < (size_t)w * h; i++) lo = d[i] < lo ? d[i] : lo, hi = d[i] > hi ? d[i] : hi;
            mg_log("mtlgles: depth copy %d (%dx%d): %g..%g\n", n - 1, w, h, lo, hi);
            [r release];
        }
        return;
    }
    if (src.pixelFormat != dst.pixelFormat) {
        static int told;
        if (!told++) mg_log("mtlgles: a copy between formats %d and %d (not done)\n", (int)src.pixelFormat, (int)dst.pixelFormat);
        return;
    }
    [mg_blit(c) copyFromTexture:src sourceSlice:(NSUInteger)sslice sourceLevel:(NSUInteger)slevel
                   sourceOrigin:MTLOriginMake((NSUInteger)sx, (NSUInteger)sy, 0)
                     sourceSize:MTLSizeMake((NSUInteger)w, (NSUInteger)h, 1) toTexture:dst
               destinationSlice:(NSUInteger)dslice destinationLevel:(NSUInteger)dlevel
              destinationOrigin:MTLOriginMake((NSUInteger)dx, (NSUInteger)dy, 0)];
}

static void glCopyTexSubImage2D_(GLenum target, GLint level, GLint dx, GLint dy, GLint x, GLint y, GLsizei w, GLsizei h)
{
    C;
    mg_texture *t = mg_tex_bound(c, target);
    int sl, ss = 0;
    // into a depth texture: from the depth attachment (OpenGL's rule; ES 3 has none)
    id<MTLTexture> src = t && t->depth ? mg_fb_depth(c, c->read_fb, 0, &sl) : read_color(c, &sl, &ss);
    if (!t || !t->mt || !src) return mg_error(GL_INVALID_OPERATION);
    // clamp to both
    int sw = (int)(src.width >> sl), sh = (int)(src.height >> sl);
    if (x < 0) dx -= x, w += x, x = 0;
    if (y < 0) dy -= y, h += y, y = 0;
    if (x + w > sw) w = sw - x;
    if (y + h > sh) h = sh - y;
    int tw = t->w >> level, th = t->h >> level;
    if (dx + w > tw) w = tw - dx;
    if (dy + h > th) h = th - dy;
    copy_region(c, src, sl, ss, x, y, t->mt, level, cube_face(target), dx, dy, w, h);
    t->last_use = c->serial;
}
static void glCopyTexImage2D_(GLenum target, GLint level, GLenum internal, GLint x, GLint y, GLsizei w, GLsizei h, GLint border)
{
    (void)border;
    C;
    mg_texture *t = mg_tex_bound(c, target);
    int sl, ss;
    id<MTLTexture> src = read_color(c, &sl, &ss);
    if (!t || !src) return mg_error(GL_INVALID_OPERATION);
    if (!t->target) t->target = GL_TEXTURE_2D;
    fmtinfo f;
    memset(&f, 0, sizeof f);
    f.fmt = src.pixelFormat;
    f.dst_bpp = 4;
    if (!texture_storage(c, t, level, w, h, &f, internal)) return mg_error(GL_OUT_OF_MEMORY);
    glCopyTexSubImage2D_(target, level, 0, 0, x, y, w, h);
}

// glBlitFramebuffer: a copy when nothing is scaled or flipped, else drawn (mtlgles_draw.m)
void mg_blit_scaled(mtlgles_context *c, id<MTLTexture> src, int slevel, int sslice, const int *s, id<MTLTexture> dst,
                    int dlevel, int dslice, const int *d, int linear);
static void glBlitFramebuffer_(GLint sx0, GLint sy0, GLint sx1, GLint sy1, GLint dx0, GLint dy0, GLint dx1, GLint dy1,
                               GLbitfield mask, GLenum filter)
{
    C;
    if (mask & GL_COLOR_BUFFER_BIT) {
        int sl, ss, dl, ds;
        id<MTLTexture> src = read_color(c, &sl, &ss);
        id<MTLTexture> dst = mg_fb_color(c, c->draw_fb, 0, &dl, &ds);
        if (!src || !dst) return mg_error(GL_INVALID_OPERATION);
        if (sx1 - sx0 == dx1 - dx0 && sy1 - sy0 == dy1 - dy0 && sx1 > sx0 && sy1 > sy0 && src.pixelFormat == dst.pixelFormat) {
            copy_region(c, src, sl, ss, sx0, sy0, dst, dl, ds, dx0, dy0, sx1 - sx0, sy1 - sy0);
        } else {
            int s[4] = {sx0, sy0, sx1, sy1}, d[4] = {dx0, dy0, dx1, dy1};
            mg_blit_scaled(c, src, sl, ss, s, dst, dl, ds, d, filter == GL_LINEAR);
        }
    }
    if (mask & (GL_DEPTH_BUFFER_BIT | GL_STENCIL_BUFFER_BIT)) {
        int sl, dl;
        id<MTLTexture> src = mg_fb_depth(c, c->read_fb, 0, &sl), dst = mg_fb_depth(c, c->draw_fb, 0, &dl);
        if (src && dst && sx1 - sx0 == dx1 - dx0 && sy1 - sy0 == dy1 - dy0)
            copy_region(c, src, sl, 0, sx0, sy0, dst, dl, 0, dx0, dy0, sx1 - sx0, sy1 - sy0);
    }
}

void mg_objects_init(mtlgles_context *c)
{
    MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                                                 width:1 height:1 mipmapped:NO];
    d.usage = MTLTextureUsageShaderRead;
    d.storageMode = MTLStorageModeShared;
    c->dummy_2d = [c->dev newTextureWithDescriptor:d];
    uint32_t black = 0xff000000u;
    [c->dummy_2d replaceRegion:MTLRegionMake2D(0, 0, 1, 1) mipmapLevel:0 withBytes:&black bytesPerRow:4];
    MTLTextureDescriptor *cd = [MTLTextureDescriptor textureCubeDescriptorWithPixelFormat:MTLPixelFormatRGBA8Unorm
                                                                                     size:1 mipmapped:NO];
    cd.usage = MTLTextureUsageShaderRead;
    cd.storageMode = MTLStorageModeShared;
    c->dummy_cube = [c->dev newTextureWithDescriptor:cd];
    for (NSUInteger f = 0; f < 6; f++)
        [c->dummy_cube replaceRegion:MTLRegionMake2D(0, 0, 1, 1) mipmapLevel:0 slice:f withBytes:&black
                         bytesPerRow:4 bytesPerImage:4];
}

#define E(n) {#n, (void *)n##_}
static const struct { const char *name; void *fn; } entries[] = {
    E(glGenBuffers), E(glDeleteBuffers), E(glBindBuffer), E(glIsBuffer), E(glBufferData), E(glBufferSubData),
    E(glMapBufferRange), E(glMapBuffer), E(glUnmapBuffer), E(glFlushMappedBufferRange), E(glGetBufferParameteriv),
    E(glCopyBufferSubData), E(glGenTextures), E(glDeleteTextures), E(glBindTexture), E(glIsTexture),
    E(glActiveTexture), E(glTexImage2D), E(glTexSubImage2D), E(glCompressedTexImage2D), E(glGetCompressedTexImage),
    E(glCompressedTexSubImage2D), E(glTexParameteri), E(glTexParameterf), E(glTexParameteriv),
    E(glTexParameterfv), E(glGetTexParameteriv), E(glGenerateMipmap), E(glGenRenderbuffers),
    E(glDeleteRenderbuffers), E(glBindRenderbuffer), E(glIsRenderbuffer), E(glRenderbufferStorage),
    E(glRenderbufferStorageMultisample), E(glGetRenderbufferParameteriv), E(glGenFramebuffers),
    E(glDeleteFramebuffers), E(glBindFramebuffer), E(glIsFramebuffer), E(glFramebufferTexture2D),
    E(glFramebufferRenderbuffer), E(glCheckFramebufferStatus), E(glGetFramebufferAttachmentParameteriv),
    E(glDrawBuffers), E(glReadBuffer), E(glDiscardFramebufferEXT), E(glInvalidateFramebuffer), E(glReadPixels),
    E(glCopyTexSubImage2D), E(glCopyTexImage2D), E(glBlitFramebuffer),
};

void *mg_objects_proc(const char *name)
{
    for (size_t i = 0; i < sizeof entries / sizeof entries[0]; i++)
        if (!strcmp(entries[i].name, name)) return entries[i].fn;
    return NULL;
}
