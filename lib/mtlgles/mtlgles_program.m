// mtlgles: shaders, programs and uniforms. GLSL ES goes to Metal through
// glsl_msl; each program is one Metal library, compiled when it is linked.
// BSD-3-Clause.
#include "mtlgles_internal.h"
#include "af_debug.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define C mtlgles_context *c = mg_cur; if (!c) return
#define CR(v) mtlgles_context *c = mg_cur; if (!c) return v

// shaders and programs share one name space in GL: programs take odd names here, shaders even
static GLuint glCreateShader_(GLenum type)
{
    CR(0);
    mg_shader_obj *s = calloc(1, sizeof *s);
    s->type = type;
    GLuint n = mg_gen(&c->shaders, s);
    s->name = n;
    return n * 2;
}
static mg_shader_obj *shader(mtlgles_context *c, GLuint name) { return (name & 1) ? NULL : mg_get(&c->shaders, name / 2); }
mg_program *mg_prog(mtlgles_context *c, GLuint name) { return (name & 1) ? mg_get(&c->programs, name / 2) : NULL; }

static void free_shader(mtlgles_context *c, mg_shader_obj *s)
{
    mg_del(&c->shaders, s->name);
    mg_free(s->parsed);
    free(s->source);
    free(s);
}
static void glDeleteShader_(GLuint name)
{
    C;
    mg_shader_obj *s = shader(c, name);
    if (!s) return;
    if (s->refs) s->delete_pending = 1;
    else free_shader(c, s);
}
static GLboolean glIsShader_(GLuint name) { CR(0); return shader(c, name) != NULL; }
static void glShaderSource_(GLuint name, GLsizei n, const GLchar *const *strs, const GLint *lens)
{
    C;
    mg_shader_obj *s = shader(c, name);
    if (!s) return mg_error(GL_INVALID_VALUE);
    size_t total = 1;
    for (GLsizei i = 0; i < n; i++) total += lens && lens[i] >= 0 ? (size_t)lens[i] : strlen(strs[i]);
    free(s->source);
    s->source = malloc(total);
    size_t at = 0;
    for (GLsizei i = 0; i < n; i++) {
        size_t l = lens && lens[i] >= 0 ? (size_t)lens[i] : strlen(strs[i]);
        memcpy(s->source + at, strs[i], l);
        at += l;
    }
    s->source[at] = 0;
}
static void glCompileShader_(GLuint name)
{
    C;
    mg_shader_obj *s = shader(c, name);
    if (!s) return mg_error(GL_INVALID_VALUE);
    mg_free(s->parsed);
    s->parsed = mg_parse(s->source ? s->source : "", s->type == GL_VERTEX_SHADER ? MG_VERTEX : MG_FRAGMENT, s->log,
                         sizeof s->log);
    s->compiled = s->parsed != NULL;
    if (!s->compiled && af_debug_env("MTLGLES_DUMP")) mg_log("mtlgles: shader %u: %s\n%s\n", name, s->log, s->source);
}
static void glGetShaderiv_(GLuint name, GLenum p, GLint *v)
{
    C;
    mg_shader_obj *s = shader(c, name);
    if (!s) return mg_error(GL_INVALID_VALUE);
    switch (p) {
    case GL_SHADER_TYPE: *v = (GLint)s->type; break;
    case GL_COMPILE_STATUS: *v = s->compiled; break;
    case GL_DELETE_STATUS: *v = s->delete_pending; break;
    case GL_INFO_LOG_LENGTH: *v = s->log[0] ? (GLint)strlen(s->log) + 1 : 0; break;
    case GL_SHADER_SOURCE_LENGTH: *v = s->source ? (GLint)strlen(s->source) + 1 : 0; break;
    default: mg_error(GL_INVALID_ENUM);
    }
}
static void copy_out(const char *src, GLsizei size, GLsizei *len, GLchar *out)
{
    if (size <= 0) {
        if (len) *len = 0;
        return;
    }
    GLsizei n = (GLsizei)strlen(src);
    if (n > size - 1) n = size - 1;
    memcpy(out, src, (size_t)n);
    out[n] = 0;
    if (len) *len = n;
}
static void glGetShaderInfoLog_(GLuint name, GLsizei size, GLsizei *len, GLchar *log)
{
    C;
    mg_shader_obj *s = shader(c, name);
    if (s) copy_out(s->log, size, len, log);
}
static void glGetShaderSource_(GLuint name, GLsizei size, GLsizei *len, GLchar *out)
{
    C;
    mg_shader_obj *s = shader(c, name);
    if (s) copy_out(s->source ? s->source : "", size, len, out);
}

static GLuint glCreateProgram_(void)
{
    CR(0);
    mg_program *p = calloc(1, sizeof *p);
    p->name = mg_gen(&c->programs, p);
    p->pipelines = [[NSMutableDictionary alloc] init];
    return p->name * 2 + 1;
}
// ---- Metal compiles in the background ------------------------------------------------------------
// A link translates to MSL at once (the uniforms and attributes come from that)
// and hands the MSL to Metal's compiler without waiting; the first draw with the
// program waits for it. HotD4 links about 480 programs at boot: compiled one by
// one that took seconds on a cold Metal cache, in parallel it takes a fraction.
// MTLGLES_SYNC_COMPILE=1 compiles at link time as before.
typedef struct mg_compile {
    dispatch_semaphore_t done;
    id<MTLFunction> vfn, ffn;
    char err[512];
    _Atomic int refs;          // the program and the completion handler
} mg_compile;

static void compile_release(mg_compile *j)
{
    if (atomic_fetch_sub(&j->refs, 1) != 1) return;
    [j->vfn release];
    [j->ffn release];
    dispatch_release(j->done);
    free(j);
}

int mg_prog_functions(mg_program *p)
{
    mg_compile *j = p->job;
    if (j) {
        dispatch_semaphore_wait(j->done, DISPATCH_TIME_FOREVER);
        p->vfn = [j->vfn retain];
        p->ffn = [j->ffn retain];
        if (!p->vfn || !p->ffn) {
            mg_log("mtlgles: program %u does not compile: %s\n", p->name * 2 + 1, j->err[0] ? j->err : "no entry points");
            p->linked = 0;
        }
        p->job = NULL;
        compile_release(j);
    }
    return p->vfn && p->ffn;
}

void mg_prog_cancel(mg_program *p)
{
    if (p->job) compile_release(p->job);
    p->job = NULL;
}

static void unlink_program(mg_program *p)
{
    mg_prog_cancel(p);
    // command buffers keep no references: the pipelines and functions live on
    // until the GPU is past the commands that may use them (mg_defer)
    mtlgles_context *c = mg_cur;
    for (id ps in p->pipelines.allValues) {
        if (c) mg_defer(c, [ps retain]);
        if (c && c->cur_pipe == ps) c->cur_pipe = nil;
    }
    [p->pipelines removeAllObjects];
    if (c) mg_defer(c, p->vfn), mg_defer(c, p->ffn);
    else [p->vfn release], [p->ffn release];
    p->vfn = p->ffn = nil;
    free(p->uni), free(p->loc_uni), free(p->loc_elem), free(p->ub[0]), free(p->ub[1]);
    p->uni = NULL, p->loc_uni = p->loc_elem = NULL, p->ub[0] = p->ub[1] = NULL;
    p->nuni = p->nloc = p->nattr = 0;
    p->linked = 0;
}
static void glDeleteProgram_(GLuint name)
{
    C;
    mg_program *p = mg_prog(c, name);
    if (!p) return;
    for (int i = 0; i < 2; i++) {
        mg_shader_obj *s = mg_get(&c->shaders, p->shaders[i]);
        if (s && --s->refs == 0 && s->delete_pending) free_shader(c, s);
    }
    if (c->program == name) c->program = 0;
    unlink_program(p);
    [p->pipelines release];
    mg_del(&c->programs, p->name);
    free(p);
}
static GLboolean glIsProgram_(GLuint name) { CR(0); return mg_prog(c, name) != NULL; }
static void glAttachShader_(GLuint prog, GLuint sh)
{
    C;
    mg_program *p = mg_prog(c, prog);
    mg_shader_obj *s = shader(c, sh);
    if (!p || !s) return mg_error(GL_INVALID_VALUE);
    int i = s->type == GL_VERTEX_SHADER ? 0 : 1;
    mg_shader_obj *old = mg_get(&c->shaders, p->shaders[i]);
    if (old && --old->refs == 0 && old->delete_pending) free_shader(c, old);
    p->shaders[i] = s->name;
    s->refs++;
}
static void glDetachShader_(GLuint prog, GLuint sh)
{
    C;
    mg_program *p = mg_prog(c, prog);
    mg_shader_obj *s = shader(c, sh);
    if (!p || !s) return;
    for (int i = 0; i < 2; i++)
        if (p->shaders[i] == s->name) {
            p->shaders[i] = 0;
            if (--s->refs == 0 && s->delete_pending) free_shader(c, s);
        }
}
static void glBindAttribLocation_(GLuint prog, GLuint loc, const GLchar *name)
{
    C;
    mg_program *p = mg_prog(c, prog);
    if (!p || loc >= MG_MAX_ATTRIBS) return mg_error(GL_INVALID_VALUE);
    for (int i = 0; i < p->nbound; i++)
        if (!strcmp(p->bound[i].name, name)) {
            p->bound[i].loc = (int)loc;
            return;
        }
    if (p->nbound < MG_MAX_ATTRIBS) {
        snprintf(p->bound[p->nbound].name, 64, "%s", name);
        p->bound[p->nbound++].loc = (int)loc;
    }
}

static mg_prog_uniform *find_uniform(mg_program *p, const char *base)
{
    for (int i = 0; i < p->nuni; i++)
        if (!strcmp(p->uni[i].base, base)) return &p->uni[i];
    return NULL;
}

static void glLinkProgram_(GLuint prog)
{
    C;
    mg_program *p = mg_prog(c, prog);
    if (!p) return mg_error(GL_INVALID_VALUE);
    unlink_program(p);
    p->log[0] = 0;
    mg_shader_obj *vs = mg_get(&c->shaders, p->shaders[0]), *fs = mg_get(&c->shaders, p->shaders[1]);
    if (!vs || !fs || !vs->parsed || !fs->parsed) {
        snprintf(p->log, sizeof p->log, "ERROR: a compiled vertex and fragment shader are needed\n");
        return;
    }
    // attributes: their bound locations, then the free ones in order
    int used = 0;
    p->nattr = mg_attrib_count(vs->parsed);
    if (p->nattr > MG_MAX_ATTRIBS) p->nattr = MG_MAX_ATTRIBS;
    for (int i = 0; i < p->nattr; i++) {
        const mg_attrib *a = mg_attrib_at(vs->parsed, i);
        snprintf(p->attr_name[i], 64, "%s", a->name);
        p->attr_type[i] = a->gltype;
        p->attr_loc[i] = -1;
        for (int k = 0; k < p->nbound; k++)
            if (!strcmp(p->bound[k].name, a->name)) p->attr_loc[i] = p->bound[k].loc, used |= 1 << p->bound[k].loc;
    }
    for (int i = 0; i < p->nattr; i++)
        if (p->attr_loc[i] < 0) {
            int l = 0;
            while (used & (1 << l)) l++;
            p->attr_loc[i] = l;
            used |= 1 << l;
        }
    char log[1024];
    char *msl = mg_link(vs->parsed, fs->parsed, p->attr_loc, log, sizeof log);
    if (!msl) {
        snprintf(p->log, sizeof p->log, "%s", log);
        return;
    }
    if (af_debug_env("MTLGLES_DUMP")) mg_log("mtlgles: program %u\n%s\n", prog, msl);
    // MTLGLES_COLD_COMPILE=1: each run's sources made new to Metal's shader cache,
    // so every compile is a first one (what a first boot on a machine costs)
    static int cold = -1;
    if (cold < 0) cold = af_debug_env("MTLGLES_COLD_COMPILE") != NULL;
    if (cold) {
        char head[64];
        int n = snprintf(head, sizeof head, "// run %d %ld\n", (int)getpid(), (long)time(NULL));
        char *m = malloc(strlen(msl) + (size_t)n + 1);
        memcpy(m, head, (size_t)n);
        strcpy(m + n, msl);
        free(msl);
        msl = m;
    }
    NSError *err = nil;
    MTLCompileOptions *opt = [[MTLCompileOptions alloc] init];
    opt.fastMathEnabled = YES;
    static int sync = -1;
    if (sync < 0) sync = af_debug_env("MTLGLES_SYNC_COMPILE") != NULL;
    if (!sync) {
        mg_compile *j = calloc(1, sizeof *j);
        j->done = dispatch_semaphore_create(0);
        j->refs = 2;
        [c->dev newLibraryWithSource:[NSString stringWithUTF8String:msl] options:opt
                   completionHandler:^(id<MTLLibrary> lib, NSError *e) {
                       if (lib) {
                           j->vfn = [lib newFunctionWithName:@"mg_vmain"];
                           j->ffn = [lib newFunctionWithName:@"mg_fmain"];
                       } else {
                           snprintf(j->err, sizeof j->err, "%s", e.localizedDescription.UTF8String);
                       }
                       dispatch_semaphore_signal(j->done);
                       compile_release(j);
                   }];
        p->job = j;
    }
    id<MTLLibrary> lib = sync ? [c->dev newLibraryWithSource:[NSString stringWithUTF8String:msl] options:opt error:&err] : nil;
    [opt release];
    if (sync && !lib) {
        snprintf(p->log, sizeof p->log, "ERROR: Metal: %s\n", err.localizedDescription.UTF8String);
        mg_log("mtlgles: program %u does not compile: %s\n", prog, err.localizedDescription.UTF8String);
        if (!af_debug_env("MTLGLES_DUMP")) {
            // the source once, to see why
            static int shown;
            if (shown++ < 3) mg_log("%s\n", msl);
        }
        free(msl);
        return;
    }
    free(msl);
    if (lib) {
        p->vfn = [lib newFunctionWithName:@"mg_vmain"];
        p->ffn = [lib newFunctionWithName:@"mg_fmain"];
        [lib release];
    }
    // uniforms: both stages' leaves, merged by name
    int cap = mg_uniform_count(vs->parsed) + mg_uniform_count(fs->parsed);
    p->uni = calloc((size_t)cap + 1, sizeof *p->uni);
    for (int st = 0; st < 2; st++) {
        mg_shader *sh = st ? fs->parsed : vs->parsed;
        p->ub_size[st] = mg_uniform_size(sh);
        p->ub[st] = calloc(1, (size_t)p->ub_size[st]);
        p->ub_dirty[st] = 1;
        for (int i = 0; i < mg_uniform_count(sh); i++) {
            const mg_uniform *u = mg_uniform_at(sh, i);
            mg_prog_uniform *pu = find_uniform(p, u->name);
            if (!pu) {
                pu = &p->uni[p->nuni++];
                snprintf(pu->base, sizeof pu->base, "%s", u->name);
                snprintf(pu->name, sizeof pu->name, "%s%s", u->name, u->count > 1 ? "[0]" : "");
                pu->type = u->gltype;
                pu->count = u->count;
                pu->off[0] = pu->off[1] = pu->slot[0] = pu->slot[1] = -1;
            }
            pu->off[st] = u->offset;
            pu->stride[st] = u->stride;
            pu->slot[st] = u->sampler_slot;
        }
        p->nslot[st] = 0;
        for (int i = 0; i < MG_MAX_UNITS; i++) {
            p->unit[st][i] = 0;
            p->slot_cube[st][i] = mg_sampler_cube(sh, i);
        }
        for (int i = 0; i < mg_uniform_count(sh); i++)
            if (mg_uniform_at(sh, i)->sampler_slot >= 0) p->nslot[st]++;
    }
    // locations: one per array element
    p->nloc = 0;
    for (int i = 0; i < p->nuni; i++) p->uni[i].loc = p->nloc, p->nloc += p->uni[i].count;
    p->loc_uni = malloc(sizeof(int) * (size_t)(p->nloc + 1));
    p->loc_elem = malloc(sizeof(int) * (size_t)(p->nloc + 1));
    for (int i = 0; i < p->nuni; i++)
        for (int e = 0; e < p->uni[i].count; e++) p->loc_uni[p->uni[i].loc + e] = i, p->loc_elem[p->uni[i].loc + e] = e;
    // (a compile still running counts as linked; mg_prog_functions reports a failure)
    p->linked = p->job || (p->vfn && p->ffn);
    if (!p->linked) snprintf(p->log, sizeof p->log, "ERROR: no entry points\n");
}
static void glUseProgram_(GLuint prog)
{
    C;
    c->program = prog;
}
static void glValidateProgram_(GLuint prog) { (void)prog; }
static void glGetProgramiv_(GLuint prog, GLenum q, GLint *v)
{
    C;
    mg_program *p = mg_prog(c, prog);
    if (!p) return mg_error(GL_INVALID_VALUE);
    switch (q) {
    case GL_LINK_STATUS: *v = p->linked; break;
    case GL_VALIDATE_STATUS: *v = p->linked; break;
    case GL_DELETE_STATUS: *v = p->delete_pending; break;
    case GL_INFO_LOG_LENGTH: *v = p->log[0] ? (GLint)strlen(p->log) + 1 : 0; break;
    case GL_ATTACHED_SHADERS: *v = (p->shaders[0] != 0) + (p->shaders[1] != 0); break;
    case GL_ACTIVE_UNIFORMS: *v = p->nuni; break;
    case GL_ACTIVE_UNIFORM_MAX_LENGTH: {
        GLint m = 0;
        for (int i = 0; i < p->nuni; i++)
            if ((GLint)strlen(p->uni[i].name) + 1 > m) m = (GLint)strlen(p->uni[i].name) + 1;
        *v = m;
        break;
    }
    case GL_ACTIVE_ATTRIBUTES: *v = p->nattr; break;
    case GL_ACTIVE_ATTRIBUTE_MAX_LENGTH: {
        GLint m = 0;
        for (int i = 0; i < p->nattr; i++)
            if ((GLint)strlen(p->attr_name[i]) + 1 > m) m = (GLint)strlen(p->attr_name[i]) + 1;
        *v = m;
        break;
    }
    case GL_NUM_PROGRAM_BINARY_FORMATS: *v = 0; break;
    default: mg_error(GL_INVALID_ENUM);
    }
}
static void glGetProgramInfoLog_(GLuint prog, GLsizei size, GLsizei *len, GLchar *log)
{
    C;
    mg_program *p = mg_prog(c, prog);
    if (p) copy_out(p->log, size, len, log);
}
static void glGetAttachedShaders_(GLuint prog, GLsizei max, GLsizei *count, GLuint *out)
{
    C;
    mg_program *p = mg_prog(c, prog);
    GLsizei n = 0;
    if (p)
        for (int i = 0; i < 2 && n < max; i++)
            if (p->shaders[i]) out[n++] = p->shaders[i] * 2;
    if (count) *count = n;
}
static GLint glGetAttribLocation_(GLuint prog, const GLchar *name)
{
    CR(-1);
    mg_program *p = mg_prog(c, prog);
    if (!p || !p->linked) return -1;
    for (int i = 0; i < p->nattr; i++)
        if (!strcmp(p->attr_name[i], name)) return p->attr_loc[i];
    return -1;
}
static void glGetActiveAttrib_(GLuint prog, GLuint i, GLsizei size, GLsizei *len, GLint *n, GLenum *type, GLchar *name)
{
    C;
    mg_program *p = mg_prog(c, prog);
    if (!p || (int)i >= p->nattr) return mg_error(GL_INVALID_VALUE);
    copy_out(p->attr_name[i], size, len, name);
    if (n) *n = 1;
    if (type) *type = p->attr_type[i];
}
static void glGetActiveUniform_(GLuint prog, GLuint i, GLsizei size, GLsizei *len, GLint *n, GLenum *type, GLchar *name)
{
    C;
    mg_program *p = mg_prog(c, prog);
    if (!p || (int)i >= p->nuni) return mg_error(GL_INVALID_VALUE);
    copy_out(p->uni[i].name, size, len, name);
    if (n) *n = p->uni[i].count;
    if (type) *type = p->uni[i].type;
}
// "a", "a[0]", "a[3]", "s[1].m"
static GLint glGetUniformLocation_(GLuint prog, const GLchar *name)
{
    CR(-1);
    mg_program *p = mg_prog(c, prog);
    if (!p || !p->linked) return -1;
    mg_prog_uniform *u = find_uniform(p, name);
    if (u) return u->loc;
    size_t n = strlen(name);
    if (n > 3 && name[n - 1] == ']') {
        const char *b = strrchr(name, '[');
        char base[96];
        size_t bl = (size_t)(b - name);
        if (bl >= sizeof base) return -1;
        memcpy(base, name, bl);
        base[bl] = 0;
        int e = atoi(b + 1);
        u = find_uniform(p, base);
        if (u && e >= 0 && e < u->count) return u->loc + e;
    }
    return -1;
}

// ---- setting uniforms ---------------------------------------------------------------------------
// the size of one element in the uniform buffer's layout, and how values go there
static void put_values(mg_program *p, int st, const mg_prog_uniform *u, int elem, int count, const void *vals,
                       int is_int, int comps, int matrix)
{
    if (u->off[st] < 0) return;
    uint8_t *base = p->ub[st] + u->off[st];
    p->ub_dirty[st] = 1;
    for (int e = elem; e < elem + count && e < u->count; e++) {
        uint8_t *d = base + (size_t)u->stride[st] * e;
        const uint8_t *s = (const uint8_t *)vals + (size_t)(e - elem) * comps * 4 * (matrix ? comps : 1);
        if ((size_t)(d - p->ub[st]) >= (size_t)p->ub_size[st]) return;
        switch (u->type) {
        case GL_BOOL: case GL_BOOL_VEC2: case GL_BOOL_VEC3: case GL_BOOL_VEC4:
            for (int i = 0; i < comps; i++) d[i] = is_int ? ((const int *)s)[i] != 0 : ((const float *)s)[i] != 0;
            break;
        case GL_FLOAT_MAT2:   // two float2 columns: packed already
            memcpy(d, s, 16);
            break;
        case GL_FLOAT_MAT3:   // three float3 columns, 16 bytes apart
            for (int col = 0; col < 3; col++) memcpy(d + 16 * col, s + 12 * col, 12);
            break;
        case GL_FLOAT_MAT4:
            memcpy(d, s, 64);
            break;
        default:
            memcpy(d, s, (size_t)comps * 4);   // int and float alike: 4 bytes a component
            break;
        }
    }
}

static void set_uniform(GLint loc, int count, const void *vals, int is_int, int comps, int matrix)
{
    C;
    mg_program *p = mg_prog(c, c->program);
    if (!p || !p->linked || loc < 0) return;
    if (loc >= p->nloc) return mg_error(GL_INVALID_OPERATION);
    mg_prog_uniform *u = &p->uni[p->loc_uni[loc]];
    int elem = p->loc_elem[loc];
    if (u->slot[0] >= 0 || u->slot[1] >= 0) {   // a sampler: its texture unit
        int unit = is_int ? *(const int *)vals : (int)*(const float *)vals;
        for (int st = 0; st < 2; st++)
            if (u->slot[st] >= 0 && u->slot[st] < MG_MAX_UNITS) p->unit[st][u->slot[st]] = unit;
        return;
    }
    for (int st = 0; st < 2; st++) put_values(p, st, u, elem, count, vals, is_int, comps, matrix);
}

#define UF(n, comps) \
    static void glUniform##n##fv_(GLint l, GLsizei k, const GLfloat *v) { set_uniform(l, k, v, 0, comps, 0); } \
    static void glUniform##n##iv_(GLint l, GLsizei k, const GLint *v) { set_uniform(l, k, v, 1, comps, 0); }
UF(1, 1) UF(2, 2) UF(3, 3) UF(4, 4)
static void glUniform1f_(GLint l, GLfloat a) { set_uniform(l, 1, &a, 0, 1, 0); }
static void glUniform2f_(GLint l, GLfloat a, GLfloat b) { GLfloat v[] = {a, b}; set_uniform(l, 1, v, 0, 2, 0); }
static void glUniform3f_(GLint l, GLfloat a, GLfloat b, GLfloat cc) { GLfloat v[] = {a, b, cc}; set_uniform(l, 1, v, 0, 3, 0); }
static void glUniform4f_(GLint l, GLfloat a, GLfloat b, GLfloat cc, GLfloat d) { GLfloat v[] = {a, b, cc, d}; set_uniform(l, 1, v, 0, 4, 0); }
static void glUniform1i_(GLint l, GLint a) { set_uniform(l, 1, &a, 1, 1, 0); }
static void glUniform2i_(GLint l, GLint a, GLint b) { GLint v[] = {a, b}; set_uniform(l, 1, v, 1, 2, 0); }
static void glUniform3i_(GLint l, GLint a, GLint b, GLint cc) { GLint v[] = {a, b, cc}; set_uniform(l, 1, v, 1, 3, 0); }
static void glUniform4i_(GLint l, GLint a, GLint b, GLint cc, GLint d) { GLint v[] = {a, b, cc, d}; set_uniform(l, 1, v, 1, 4, 0); }
static void transpose_set(GLint l, GLsizei k, GLboolean tr, const GLfloat *v, int n)
{
    if (!tr) return set_uniform(l, k, v, 0, n, 1);
    GLfloat *t = malloc(sizeof(GLfloat) * (size_t)(n * n * k));
    for (int m = 0; m < k; m++)
        for (int i = 0; i < n; i++)
            for (int j = 0; j < n; j++) t[m * n * n + i * n + j] = v[m * n * n + j * n + i];
    set_uniform(l, k, t, 0, n, 1);
    free(t);
}
static void glUniformMatrix2fv_(GLint l, GLsizei k, GLboolean tr, const GLfloat *v) { transpose_set(l, k, tr, v, 2); }
static void glUniformMatrix3fv_(GLint l, GLsizei k, GLboolean tr, const GLfloat *v) { transpose_set(l, k, tr, v, 3); }
static void glUniformMatrix4fv_(GLint l, GLsizei k, GLboolean tr, const GLfloat *v) { transpose_set(l, k, tr, v, 4); }

static void glGetUniformfv_(GLuint prog, GLint loc, GLfloat *v)
{
    C;
    mg_program *p = mg_prog(c, prog);
    if (!p || loc < 0 || loc >= p->nloc) return mg_error(GL_INVALID_OPERATION);
    mg_prog_uniform *u = &p->uni[p->loc_uni[loc]];
    int st = u->off[0] >= 0 ? 0 : 1;
    if (u->off[st] < 0) return;
    const uint8_t *s = p->ub[st] + u->off[st] + (size_t)u->stride[st] * p->loc_elem[loc];
    int n = u->type == GL_FLOAT_MAT4 ? 16 : u->type == GL_FLOAT_VEC4 ? 4 : u->type == GL_FLOAT_VEC3 ? 3
          : u->type == GL_FLOAT_VEC2 ? 2 : 1;
    memcpy(v, s, (size_t)n * 4);
}
static void glGetUniformiv_(GLuint prog, GLint loc, GLint *v)
{
    GLfloat f[16];
    glGetUniformfv_(prog, loc, f);
    *v = (GLint)f[0];
}

#define E(n) {#n, (void *)n##_}
static const struct { const char *name; void *fn; } entries[] = {
    E(glCreateShader), E(glDeleteShader), E(glIsShader), E(glShaderSource), E(glCompileShader), E(glGetShaderiv),
    E(glGetShaderInfoLog), E(glGetShaderSource), E(glCreateProgram), E(glDeleteProgram), E(glIsProgram),
    E(glAttachShader), E(glDetachShader), E(glBindAttribLocation), E(glLinkProgram), E(glUseProgram),
    E(glValidateProgram), E(glGetProgramiv), E(glGetProgramInfoLog), E(glGetAttachedShaders),
    E(glGetAttribLocation), E(glGetActiveAttrib), E(glGetActiveUniform), E(glGetUniformLocation),
    E(glUniform1fv), E(glUniform2fv), E(glUniform3fv), E(glUniform4fv), E(glUniform1iv), E(glUniform2iv),
    E(glUniform3iv), E(glUniform4iv), E(glUniform1f), E(glUniform2f), E(glUniform3f), E(glUniform4f),
    E(glUniform1i), E(glUniform2i), E(glUniform3i), E(glUniform4i), E(glUniformMatrix2fv),
    E(glUniformMatrix3fv), E(glUniformMatrix4fv), E(glGetUniformfv), E(glGetUniformiv),
};

void *mg_program_proc(const char *name)
{
    for (size_t i = 0; i < sizeof entries / sizeof entries[0]; i++)
        if (!strcmp(entries[i].name, name)) return entries[i].fn;
    return NULL;
}
