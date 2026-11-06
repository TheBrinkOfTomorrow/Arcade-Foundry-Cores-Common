// glsl_msl: GLSL ES to Metal Shading Language by rewriting (see glsl_msl.h).
// Part of mtlgles (spike, issue #288 item 5c). BSD-3-Clause.
#include "glsl_msl.h"
#include "af_debug.h"
#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// ---- growing strings ----------------------------------------------------------------------------
typedef struct { char *p; size_t n, cap; } sb;
static void sb_putn(sb *b, const char *s, size_t n)
{
    if (b->n + n + 1 > b->cap) {
        b->cap = (b->n + n + 1) * 2 + 256;
        b->p = realloc(b->p, b->cap);
    }
    memcpy(b->p + b->n, s, n);
    b->n += n;
    b->p[b->n] = 0;
}
static void sb_puts(sb *b, const char *s) { sb_putn(b, s, strlen(s)); }
static void sb_printf(sb *b, const char *fmt, ...)
{
    char tmp[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof tmp, fmt, ap);
    va_end(ap);
    if (n < (int)sizeof tmp) {
        sb_putn(b, tmp, (size_t)n);
        return;
    }
    char *big = malloc((size_t)n + 1);
    va_start(ap, fmt);
    vsnprintf(big, (size_t)n + 1, fmt, ap);
    va_end(ap);
    sb_putn(b, big, (size_t)n);
    free(big);
}
static const char *sb_str(sb *b) { return b->p ? b->p : ""; }

// ---- tokens -------------------------------------------------------------------------------------
enum { TK_IDENT, TK_NUM, TK_PUNCT, TK_PP, TK_END };
typedef struct { int kind, pos, len, ws, wslen; } tok;
typedef struct { tok *t; int n, cap; const char *src; } toks;

static void tk_add(toks *ts, int kind, int pos, int len, int ws, int wslen)
{
    if (ts->n == ts->cap) ts->cap = ts->cap * 2 + 256, ts->t = realloc(ts->t, sizeof(tok) * (size_t)ts->cap);
    ts->t[ts->n++] = (tok){kind, pos, len, ws, wslen};
}

static void tokenize(toks *ts, const char *s)
{
    ts->src = s;
    int i = 0, n = (int)strlen(s), bol = 1;
    while (1) {
        int ws = i;
        while (i < n) {
            if (s[i] == '\n') bol = 1, i++;
            else if (isspace((unsigned char)s[i])) i++;
            else if (s[i] == '/' && s[i + 1] == '/') { while (i < n && s[i] != '\n') i++; }
            else if (s[i] == '/' && s[i + 1] == '*') {
                i += 2;
                while (i < n && !(s[i] == '*' && s[i + 1] == '/')) i++;
                i = i + 2 > n ? n : i + 2;
            } else break;
        }
        int wslen = i - ws;
        if (i >= n) { tk_add(ts, TK_END, n, 0, ws, wslen); return; }
        int st = i;
        if (s[i] == '#' && bol) {   // a whole preprocessor line, continuations included
            while (i < n && s[i] != '\n') {
                if (s[i] == '\\' && s[i + 1] == '\n') i++;
                i++;
            }
            tk_add(ts, TK_PP, st, i - st, ws, wslen);
            continue;
        }
        bol = 0;
        if (isalpha((unsigned char)s[i]) || s[i] == '_') {
            while (i < n && (isalnum((unsigned char)s[i]) || s[i] == '_')) i++;
            tk_add(ts, TK_IDENT, st, i - st, ws, wslen);
        } else if (isdigit((unsigned char)s[i]) || (s[i] == '.' && isdigit((unsigned char)s[i + 1]))) {
            while (i < n && (isalnum((unsigned char)s[i]) || s[i] == '.' || s[i] == '_' ||
                             ((s[i] == '+' || s[i] == '-') && (s[i - 1] == 'e' || s[i - 1] == 'E') &&
                              !(s[st] == '0' && (s[st + 1] == 'x' || s[st + 1] == 'X')))))
                i++;
            tk_add(ts, TK_NUM, st, i - st, ws, wslen);
        } else {
            tk_add(ts, TK_PUNCT, st, 1, ws, wslen);
            i++;
        }
    }
}

static int tk_is(const toks *ts, int k, const char *w)
{
    const tok *t = &ts->t[k];
    if (t->kind == TK_END) return 0;
    return (int)strlen(w) == t->len && !memcmp(ts->src + t->pos, w, (size_t)t->len);
}
static void tk_text(const toks *ts, int k, char *out, size_t size)
{
    const tok *t = &ts->t[k];
    size_t n = (size_t)t->len < size - 1 ? (size_t)t->len : size - 1;
    memcpy(out, ts->src + t->pos, n);
    out[n] = 0;
}

// ---- types --------------------------------------------------------------------------------------
typedef struct { const char *glsl, *msl; unsigned gl; int size, align, sampler; } btype;
enum { S_NONE, S_2D, S_CUBE, S_SHADOW };
static const btype btypes[] = {
    {"void", "void", 0, 0, 1, 0},
    {"float", "float", 0x1406, 4, 4, 0}, {"vec2", "float2", 0x8B50, 8, 8, 0},
    {"vec3", "float3", 0x8B51, 16, 16, 0}, {"vec4", "float4", 0x8B52, 16, 16, 0},
    {"int", "int", 0x1404, 4, 4, 0}, {"ivec2", "int2", 0x8B53, 8, 8, 0},
    {"ivec3", "int3", 0x8B54, 16, 16, 0}, {"ivec4", "int4", 0x8B55, 16, 16, 0},
    {"uint", "uint", 0x1405, 4, 4, 0}, {"uvec2", "uint2", 0x8DC6, 8, 8, 0},
    {"uvec3", "uint3", 0x8DC7, 16, 16, 0}, {"uvec4", "uint4", 0x8DC8, 16, 16, 0},
    {"bool", "bool", 0x8B56, 1, 1, 0}, {"bvec2", "bool2", 0x8B57, 2, 2, 0},
    {"bvec3", "bool3", 0x8B58, 4, 4, 0}, {"bvec4", "bool4", 0x8B59, 4, 4, 0},
    {"mat2", "float2x2", 0x8B5A, 16, 8, 0}, {"mat3", "float3x3", 0x8B5B, 48, 16, 0},
    {"mat4", "float4x4", 0x8B5C, 64, 16, 0},
    {"sampler2D", "mg_s2d", 0x8B5E, 0, 0, S_2D}, {"samplerCube", "mg_scube", 0x8B60, 0, 0, S_CUBE},
    {"sampler2DShadow", "mg_s2d", 0x8B62, 0, 0, S_SHADOW},
};
static const btype *find_btype(const char *name)
{
    for (size_t i = 0; i < sizeof btypes / sizeof btypes[0]; i++)
        if (!strcmp(btypes[i].glsl, name)) return &btypes[i];
    return NULL;
}

// words MSL (C++) keeps for itself that GLSL leaves free: renamed with mg_r_
static const char *reserved[] = {
    "half", "texture", "sampler", "vertex", "fragment", "kernel", "constant", "device", "thread", "threadgroup",
    "using", "namespace", "new", "delete", "class", "this", "template", "operator", "auto", "char", "short", "long",
    "signed", "unsigned", "union", "enum", "goto", "static", "extern", "register", "volatile", "typedef", "sizeof",
    "public", "private", "protected", "friend", "virtual", "explicit", "mutable", "inline", "asm", "try", "catch",
    "throw", "typename", "and", "or", "xor", "not", "bitand", "bitor", "compl", "and_eq", "or_eq", "xor_eq",
    "not_eq", "default", "double", "access", "metal", "size_t", "ptrdiff_t", "nullptr", "static_cast", "const_cast",
    "reinterpret_cast", "dynamic_cast", "typeid", "alignas", "alignof", "decltype", "noexcept", "constexpr",
    "static_assert", "thread_local", "char16_t", "char32_t", "wchar_t", "export", "float2", "float3", "float4",
    "int2", "int3", "int4", "uint2", "uint3", "uint4", "bool2", "bool3", "bool4", "float2x2", "float3x3",
    "float4x4", "ushort", "uchar", "vec", "matrix", "array", "rsqrt", "saturate", "fmod", "atan2", "rint",
    "sample", "discard_fragment", "dfdx", "dfdy", "level", "bias", "gradient2d", "min", "max", "clamp", "mix",
    "step", "smoothstep", "select", "fract", "position", "depth", "packed_float3", NULL,
};
static int is_reserved(const char *w)
{
    for (int i = 0; reserved[i]; i++)
        if (!strcmp(reserved[i], w)) return 1;
    return 0;
}

// GLSL built-in functions that MSL names otherwise, or has a helper for
typedef struct { const char *glsl, *vs, *fs; } bfunc;
static const bfunc bfuncs[] = {
    {"texture2D", "mg_texture2DLod0", "mg_texture2D"}, {"texture2DProj", "mg_texture2DProjLod0", "mg_texture2DProj"},
    {"texture2DLod", "mg_texture2DLod", "mg_texture2DLod"}, {"texture2DLodEXT", "mg_texture2DLod", "mg_texture2DLod"},
    {"texture2DProjLod", "mg_texture2DProjLod", "mg_texture2DProjLod"},
    {"texture2DProjLodEXT", "mg_texture2DProjLod", "mg_texture2DProjLod"},
    {"texture2DGradEXT", "mg_texture2DGrad", "mg_texture2DGrad"},
    {"textureCube", "mg_textureCubeLod0", "mg_textureCube"}, {"textureCubeLod", "mg_textureCubeLod", "mg_textureCubeLod"},
    {"textureCubeLodEXT", "mg_textureCubeLod", "mg_textureCubeLod"},
    {"texture", "mg_textureLod0", "mg_texture"}, {"textureLod", "mg_textureLod", "mg_textureLod"},
    {"textureProj", "mg_texture2DProjLod0", "mg_texture2DProj"}, {"shadow2D", "mg_shadow2D", "mg_shadow2D"},
    {"shadow2DProj", "mg_shadow2DProj", "mg_shadow2DProj"}, {"shadow2DEXT", "mg_shadow2D", "mg_shadow2D"},
    {"mod", "mg_mod", "mg_mod"}, {"atan", "mg_atan", "mg_atan"}, {"inversesqrt", "rsqrt", "rsqrt"},
    {"dFdx", "dfdx", "dfdx"}, {"dFdy", "dfdy", "dfdy"}, {"lessThan", "mg_lessThan", "mg_lessThan"},
    {"lessThanEqual", "mg_lessThanEqual", "mg_lessThanEqual"}, {"greaterThan", "mg_greaterThan", "mg_greaterThan"},
    {"greaterThanEqual", "mg_greaterThanEqual", "mg_greaterThanEqual"}, {"equal", "mg_equal", "mg_equal"},
    {"notEqual", "mg_notEqual", "mg_notEqual"}, {"not", "mg_not", "mg_not"},
    {"matrixCompMult", "mg_matrixCompMult", "mg_matrixCompMult"}, {"radians", "mg_radians", "mg_radians"},
    {"degrees", "mg_degrees", "mg_degrees"}, {"min", "mg_min", "mg_min"}, {"max", "mg_max", "mg_max"},
    {"clamp", "mg_clamp", "mg_clamp"}, {"mix", "mg_mix", "mg_mix"}, {"step", "mg_step", "mg_step"},
    {"smoothstep", "mg_smoothstep", "mg_smoothstep"}, {"roundEven", "rint", "rint"}, {"fract", "fract", "fract"},
    {"discard", "discard_fragment()", "discard_fragment()"},
    {"gl_MaxVertexAttribs", "16", "16"}, {"gl_MaxTextureImageUnits", "16", "16"},
    {"gl_MaxVertexTextureImageUnits", "16", "16"}, {"gl_MaxCombinedTextureImageUnits", "32", "32"},
    {"gl_MaxDrawBuffers", "4", "4"}, {"gl_MaxVaryingVectors", "31", "31"},
    {"gl_MaxVertexUniformVectors", "1024", "1024"}, {"gl_MaxFragmentUniformVectors", "1024", "1024"},
};

// ---- the parsed shader --------------------------------------------------------------------------
#define MAXV 128
#define MAXU 512   // uniforms: gl4es makes each ARB env/local parameter one (OutRun 2 SP: 215)
typedef struct { char name[64], type[64]; int count, arr; } field;   // arr: declared as an array (even [1])
typedef struct { char name[64]; field f[48]; int nf; int size, align; } ustruct;
typedef struct { char name[64], type[64]; int count, flat, loc, arr; } var;

struct mg_shader {
    int stage, es3;
    sb pre;        // structs, consts, #defines: before the stage's struct
    sb body;       // the members of the stage's struct: globals and functions
    sb undefs;     // #undef of the stage's #defines, after it
    ustruct st[32];
    int nst;
    var uni[MAXU];   // uniforms (not samplers), as declared
    int nuni;
    var samp[16];
    int nsamp;
    var attr[32];
    int nattr;
    var vary[64];
    int nvary;
    var out[8];
    int nout;
    struct { char name[64]; int value; } konst[64];
    int nkonst;
    mg_uniform leaf[1024];
    int nleaf;
    int ubo_size;
    mg_attrib attrs[32];
    int writes_psize, uses_fragcoord, uses_frontfacing, uses_pointcoord, uses_fragdata, uses_fragcolor;
    char err[256];
};

static ustruct *find_struct(mg_shader *s, const char *name)
{
    for (int i = 0; i < s->nst; i++)
        if (!strcmp(s->st[i].name, name)) return &s->st[i];
    return NULL;
}
static int is_uniform(const mg_shader *s, const char *name)
{
    for (int i = 0; i < s->nuni; i++)
        if (!strcmp(s->uni[i].name, name)) return 1;
    return 0;
}
static int is_struct_field(const mg_shader *s, const char *name)
{
    for (int i = 0; i < s->nst; i++)
        for (int k = 0; k < s->st[i].nf; k++)
            if (!strcmp(s->st[i].f[k].name, name)) return 1;
    return 0;
}

static void out_name(const char *w, char *o, size_t size)
{
    if (!strcmp(w, "main")) snprintf(o, size, "mg_main");
    else if (is_reserved(w)) snprintf(o, size, "mg_r_%s", w);
    else snprintf(o, size, "%s", w);
}

// the MSL type of a GLSL type name (basic or a struct)
static const char *msl_type(const char *glsl)
{
    const btype *b = find_btype(glsl);
    return b ? b->msl : glsl;
}

// ---- rewriting token ranges ---------------------------------------------------------------------
typedef struct {
    mg_shader *s;
    const toks *ts;
    int native_ctor;   // constructors as MSL's own (constant initializers)
    int no_uniforms;   // uniform names stay (the uniform struct's own declarations)
    unsigned char paren[256];
    int depth;
} rw;

static int next_real(const toks *ts, int k)
{
    return k + 1 < ts->n ? k + 1 : k;
}

static void rewrite_pp(rw *r, sb *o, const char *line, int len);

static void rewrite_tok(rw *r, sb *o, int k, int keep_ws)
{
    const toks *ts = r->ts;
    const tok *t = &ts->t[k];
    if (keep_ws) sb_putn(o, ts->src + t->ws, (size_t)t->wslen);
    if (t->kind == TK_END) return;
    if (t->kind == TK_PP) {
        rewrite_pp(r, o, ts->src + t->pos, t->len);
        return;
    }
    char w[128];
    tk_text(ts, k, w, sizeof w);
    if (t->kind == TK_PUNCT) {
        if (w[0] == '(') {
            if (r->depth < 255) r->paren[r->depth] = 0;
            r->depth++;
        } else if (w[0] == ')' && r->depth > 0) {
            r->depth--;
            if (r->depth < 255 && r->paren[r->depth]) {
                sb_puts(o, "}");
                return;
            }
        }
        sb_puts(o, w);
        return;
    }
    if (t->kind == TK_NUM) {
        // GLSL ES 3's unsigned suffix is MSL's too; 1.0e5 and the like pass
        sb_puts(o, w);
        return;
    }
    int prev_dot = k > 0 && tk_is(ts, k - 1, ".") && ts->t[k].wslen == 0;
    int next_paren = tk_is(ts, next_real(ts, k), "(");
    mg_shader *s = r->s;
    if (!strcmp(w, "highp") || !strcmp(w, "mediump") || !strcmp(w, "lowp")) return;
    if (prev_dot) {
        // a swizzle in stpq is xyzw to MSL (unless it names a struct field)
        int stpq = 1;
        for (char *c = w; *c; c++)
            if (!strchr("stpq", *c)) stpq = 0;
        if (stpq && strlen(w) <= 4 && !is_struct_field(s, w)) {
            for (char *c = w; *c; c++) *c = *c == 's' ? 'x' : *c == 't' ? 'y' : *c == 'p' ? 'z' : 'w';
            sb_puts(o, w);
            return;
        }
        char on[160];
        out_name(w, on, sizeof on);
        sb_puts(o, strcmp(w, "main") ? on : w);
        return;
    }
    const btype *b = find_btype(w);
    if (b) {
        if (next_paren && !b->sampler) {
            if (r->native_ctor || !strcmp(w, "float") || !strcmp(w, "int") || !strcmp(w, "uint") || !strcmp(w, "bool"))
                sb_puts(o, b->msl);
            else
                sb_printf(o, "mg_%s", w);
        } else {
            sb_puts(o, b->msl);
        }
        return;
    }
    if (next_paren && find_struct(s, w)) {
        // a struct's constructor: aggregate initialisation in MSL
        sb_puts(o, w);
        int nk = next_real(ts, k);
        sb_putn(o, ts->src + ts->t[nk].ws, (size_t)ts->t[nk].wslen);
        sb_puts(o, "{");
        if (r->depth < 255) r->paren[r->depth] = 1;
        r->depth++;
        // the "(" is consumed here: mark it so the caller skips it
        ((tok *)&ts->t[nk])->kind = TK_END + 1;   // skipped once
        return;
    }
    for (size_t i = 0; i < sizeof bfuncs / sizeof bfuncs[0]; i++)
        if (!strcmp(bfuncs[i].glsl, w)) {
            sb_puts(o, s->stage == MG_VERTEX ? bfuncs[i].vs : bfuncs[i].fs);
            return;
        }
    if (!strncmp(w, "gl_", 3)) {
        if (!strcmp(w, "gl_Position")) sb_puts(o, "mg_Position");
        else if (!strcmp(w, "gl_PointSize")) sb_puts(o, "mg_PointSize"), s->writes_psize = 1;
        else if (!strcmp(w, "gl_FragColor")) sb_puts(o, "mg_FragData[0]"), s->uses_fragcolor = 1;
        else if (!strcmp(w, "gl_FragData")) sb_puts(o, "mg_FragData"), s->uses_fragdata = 1;
        else if (!strcmp(w, "gl_FragCoord")) sb_puts(o, "mg_FragCoord"), s->uses_fragcoord = 1;
        else if (!strcmp(w, "gl_FrontFacing")) sb_puts(o, "mg_FrontFacing"), s->uses_frontfacing = 1;
        else if (!strcmp(w, "gl_PointCoord")) sb_puts(o, "mg_PointCoord"), s->uses_pointcoord = 1;
        else sb_printf(o, "mg_%s", w + 3);
        return;
    }
    char on[160];
    out_name(w, on, sizeof on);
    if (!r->no_uniforms && is_uniform(s, w)) {
        sb_printf(o, "(Up->%s)", on);
        return;
    }
    sb_puts(o, on);
}

static void rewrite_range(rw *r, sb *o, int a, int b, int keep_first_ws)
{
    for (int k = a; k < b; k++) {
        if (r->ts->t[k].kind == TK_END + 1) {   // a "(" a struct constructor took
            ((tok *)&r->ts->t[k])->kind = TK_PUNCT;
            continue;
        }
        rewrite_tok(r, o, k, k > a || keep_first_ws);
    }
}

// a preprocessor line: #define bodies are rewritten like code
static void rewrite_pp(rw *r, sb *o, const char *line, int len)
{
    char *l = malloc((size_t)len + 1);
    memcpy(l, line, (size_t)len);
    l[len] = 0;
    const char *p = l + 1;
    while (*p == ' ' || *p == '\t') p++;
    if (!strncmp(p, "version", 7) || !strncmp(p, "extension", 9) || !strncmp(p, "pragma", 6) ||
        !strncmp(p, "line", 4)) {
        free(l);
        return;
    }
    toks sub = {0};
    tokenize(&sub, p);
    rw r2 = *r;
    r2.ts = &sub;
    r2.depth = 0;
    sb_puts(o, "#");
    rewrite_range(&r2, o, 0, sub.n, 1);
    free(sub.t);
    free(l);
}

// ---- layout of uniforms (MSL's natural layout) --------------------------------------------------
static int type_layout(mg_shader *s, const char *type, int *align)
{
    const btype *b = find_btype(type);
    if (b) {
        *align = b->align;
        return b->size;
    }
    ustruct *u = find_struct(s, type);
    if (!u) {
        *align = 4;
        return 4;
    }
    *align = u->align;
    return u->size;
}

static void layout_struct(mg_shader *s, ustruct *u)
{
    int off = 0, al = 1;
    for (int i = 0; i < u->nf; i++) {
        int a, sz = type_layout(s, u->f[i].type, &a);
        off = (off + a - 1) / a * a;
        off += sz * u->f[i].count;
        if (a > al) al = a;
    }
    u->align = al;
    u->size = (off + al - 1) / al * al;
}

static void add_leaf(mg_shader *s, const char *name, const char *type, int count, int arr, int offset)
{
    const btype *b = find_btype(type);
    if (b) {
        if (s->nleaf >= 1024) return;
        mg_uniform *l = &s->leaf[s->nleaf++];
        snprintf(l->name, sizeof l->name, "%s", name);
        l->gltype = b->gl;
        l->count = count;
        l->offset = offset;
        l->stride = b->size;
        l->sampler_slot = -1;
        return;
    }
    ustruct *u = find_struct(s, type);
    if (!u) return;
    for (int i = 0; i < count; i++) {
        char base[96];
        if (arr) snprintf(base, sizeof base, "%s[%d]", name, i);
        else snprintf(base, sizeof base, "%s", name);
        int off = 0;
        for (int f = 0; f < u->nf; f++) {
            int a, sz = type_layout(s, u->f[f].type, &a);
            off = (off + a - 1) / a * a;
            char full[96];
            snprintf(full, sizeof full, "%s.%s", base, u->f[f].name);
            add_leaf(s, full, u->f[f].type, u->f[f].count, u->f[f].arr, offset + i * u->size + off);
            off += sz * u->f[f].count;
        }
    }
}

// ---- global statements --------------------------------------------------------------------------
// GLSL built-ins MSL calls by the same name. A shader's own overload of one
// (HotD4: float pow(float f, int a) { return pow(f, float(a)); }) is a member
// of the stage's struct, which hides metal's for every call in it: the
// overload then called itself for ever, and the fragment wrote no colour (its
// depth was written already). A forwarding member template next to it gives
// GLSL's overload resolution back: exact user overloads first, metal's for the rest
static const char *same_name_builtins[] = {
    "pow", "exp", "exp2", "log", "log2", "sqrt", "abs", "sign", "floor", "ceil", "sin", "cos", "tan", "asin",
    "acos", "length", "distance", "dot", "cross", "normalize", "reflect", "refract", "faceforward", NULL,
};
static void forward_builtin(mg_shader *s, const char *name)
{
    for (int i = 0; same_name_builtins[i]; i++) {
        if (strcmp(same_name_builtins[i], name)) continue;
        char fw[160];
        snprintf(fw, sizeof fw, "    template<typename... A> auto %s(A... a) { return metal::%s(a...); }\n", name, name);
        if (!strstr(sb_str(&s->body), fw)) sb_puts(&s->body, fw);
        return;
    }
}

static int fail(mg_shader *s, const char *msg, const toks *ts, int k)
{
    char w[64] = "";
    if (k >= 0 && k < ts->n) tk_text(ts, k, w, sizeof w);
    snprintf(s->err, sizeof s->err, "%s (at \"%s\")", msg, w);
    return -1;
}

static int match(const toks *ts, int k, const char *open, const char *close)
{
    int d = 0;
    for (; k < ts->n && ts->t[k].kind != TK_END; k++) {
        if (tk_is(ts, k, open)) d++;
        else if (tk_is(ts, k, close) && --d == 0) return k;
    }
    return -1;
}

// an array size: an integer expression of numbers and constants declared before
// (HotD4: "[ ( 1) ]", gl4es: "[_gl4es_MaxLights]"); *k moves past what it read
static int size_expr(mg_shader *s, const toks *ts, int *k, int b);
static int size_atom(mg_shader *s, const toks *ts, int *k, int b)
{
    if (*k >= b) return -1;
    if (tk_is(ts, *k, "(")) {
        ++*k;
        int v = size_expr(s, ts, k, b);
        if (*k >= b || !tk_is(ts, *k, ")")) return -1;
        ++*k;
        return v;
    }
    char w[64];
    tk_text(ts, *k, w, sizeof w);
    ++*k;
    if (isdigit((unsigned char)w[0])) return atoi(w);
    for (int i = 0; i < s->nkonst; i++)
        if (!strcmp(s->konst[i].name, w)) return s->konst[i].value;
    return -1;
}
static int size_term(mg_shader *s, const toks *ts, int *k, int b)
{
    int v = size_atom(s, ts, k, b);
    while (v >= 0 && *k < b && (tk_is(ts, *k, "*") || tk_is(ts, *k, "/"))) {
        int mul = tk_is(ts, *k, "*");
        ++*k;
        int r = size_atom(s, ts, k, b);
        if (r < 0 || (!mul && !r)) return -1;
        v = mul ? v * r : v / r;
    }
    return v;
}
static int size_expr(mg_shader *s, const toks *ts, int *k, int b)
{
    int v = size_term(s, ts, k, b);
    while (v >= 0 && *k < b && (tk_is(ts, *k, "+") || tk_is(ts, *k, "-"))) {
        int add = tk_is(ts, *k, "+");
        ++*k;
        int r = size_term(s, ts, k, b);
        if (r < 0) return -1;
        v = add ? v + r : v - r;
    }
    return v;
}
static int array_size(mg_shader *s, const toks *ts, int a, int b)
{
    int k = a, v = size_expr(s, ts, &k, b);
    return k == b ? v : -1;
}

static int is_qual(const toks *ts, int k)
{
    static const char *q[] = {"uniform", "attribute", "varying", "in", "out", "inout", "const", "flat", "smooth",
                              "centroid", "invariant", "highp", "mediump", "lowp", "layout", NULL};
    for (int i = 0; q[i]; i++)
        if (tk_is(ts, k, q[i])) return 1;
    return 0;
}

// a struct definition at k ("struct"): recorded and written to pre; returns the token after "}"
static int parse_struct(mg_shader *s, rw *r, int k)
{
    const toks *ts = r->ts;
    if (s->nst >= 32) return fail(s, "too many structs", ts, k);
    ustruct *u = &s->st[s->nst];
    memset(u, 0, sizeof *u);
    if (ts->t[k + 1].kind != TK_IDENT) return fail(s, "anonymous struct", ts, k);
    tk_text(ts, k + 1, u->name, sizeof u->name);
    if (!tk_is(ts, k + 2, "{")) return fail(s, "struct without a body", ts, k + 2);
    int end = match(ts, k + 2, "{", "}");
    if (end < 0) return fail(s, "unclosed struct", ts, k);
    s->nst++;
    sb_printf(&s->pre, "struct %s {", u->name);
    int i = k + 3;
    while (i < end) {
        while (i < end && (tk_is(ts, i, "highp") || tk_is(ts, i, "mediump") || tk_is(ts, i, "lowp"))) i++;
        if (i >= end) break;
        char type[64];
        tk_text(ts, i, type, sizeof type);
        i++;
        while (i < end) {
            if (u->nf >= 48) return fail(s, "struct too big", ts, i);
            field *f = &u->f[u->nf++];
            snprintf(f->type, sizeof f->type, "%s", type);
            tk_text(ts, i, f->name, sizeof f->name);
            f->count = 1;
            i++;
            if (tk_is(ts, i, "[")) {
                int c = match(ts, i, "[", "]");
                f->count = array_size(s, ts, i + 1, c);
                f->arr = 1;
                if (f->count < 1) return fail(s, "struct array size", ts, i);
                i = c + 1;
            }
            char on[96];
            out_name(f->name, on, sizeof on);
            if (f->arr) sb_printf(&s->pre, " %s %s[%d];", msl_type(type), on, f->count);
            else sb_printf(&s->pre, " %s %s;", msl_type(type), on);
            if (tk_is(ts, i, ",")) { i++; continue; }
            if (tk_is(ts, i, ";")) { i++; break; }
            return fail(s, "struct field", ts, i);
        }
    }
    sb_puts(&s->pre, " };\n");
    layout_struct(s, u);
    return end + 1;
}

// a parameter list (from "(" to its ")"): in/out/inout to MSL references
static void write_params(mg_shader *s, rw *r, sb *o, int a, int b)
{
    const toks *ts = r->ts;
    sb_puts(o, "(");
    int i = a + 1, first = 1;
    while (i < b) {
        int e = i, d = 0;
        while (e < b && !(d == 0 && tk_is(ts, e, ","))) {
            if (tk_is(ts, e, "(") || tk_is(ts, e, "[")) d++;
            if (tk_is(ts, e, ")") || tk_is(ts, e, "]")) d--;
            e++;
        }
        int ref = 0, cnst = 0, k = i;
        while (k < e && (is_qual(ts, k))) {
            if (tk_is(ts, k, "out") || tk_is(ts, k, "inout")) ref = 1;
            if (tk_is(ts, k, "const")) cnst = 1;
            k++;
        }
        if (k < e && !(e - k == 1 && tk_is(ts, k, "void"))) {
            char type[64], name[64] = "", on[96] = "";
            tk_text(ts, k, type, sizeof type);
            if (k + 1 < e) {
                tk_text(ts, k + 1, name, sizeof name);
                out_name(name, on, sizeof on);
            }
            if (!first) sb_puts(o, ", ");
            first = 0;
            char arr[32] = "";
            if (k + 2 < e && tk_is(ts, k + 2, "[")) {
                int c = match(ts, k + 2, "[", "]");
                snprintf(arr, sizeof arr, "[%d]", array_size(s, ts, k + 3, c));
            }
            if (ref && arr[0]) sb_printf(o, "thread %s (&%s)%s", msl_type(type), on, arr);
            else if (ref) sb_printf(o, "thread %s& %s", msl_type(type), on);
            else sb_printf(o, "%s%s %s%s", cnst ? "const " : "", msl_type(type), on, arr);
        }
        i = e + 1;
    }
    sb_puts(o, ")");
}

static int parse_global(mg_shader *s, rw *r, int k)
{
    const toks *ts = r->ts;
    int uniform = 0, attribute = 0, varying = 0, in = 0, out = 0, cnst = 0, flat = 0, loc = -1;
    int start = k;
    while (is_qual(ts, k)) {
        if (tk_is(ts, k, "uniform")) uniform = 1;
        else if (tk_is(ts, k, "attribute")) attribute = 1;
        else if (tk_is(ts, k, "varying")) varying = 1;
        else if (tk_is(ts, k, "in")) in = 1;
        else if (tk_is(ts, k, "out")) out = 1;
        else if (tk_is(ts, k, "const")) cnst = 1;
        else if (tk_is(ts, k, "flat")) flat = 1;
        else if (tk_is(ts, k, "invariant") && ts->t[k + 1].kind == TK_IDENT && tk_is(ts, k + 2, ";")) return k + 3;
        else if (tk_is(ts, k, "layout")) {
            int c = match(ts, k + 1, "(", ")");
            if (c < 0) return fail(s, "layout", ts, k);
            for (int j = k + 2; j < c; j++)
                if (tk_is(ts, j, "location") && tk_is(ts, j + 1, "=")) {
                    char w[16];
                    tk_text(ts, j + 2, w, sizeof w);
                    loc = atoi(w);
                }
            k = c;
        }
        k++;
    }
    if (tk_is(ts, k, "precision")) {
        while (k < ts->n && !tk_is(ts, k, ";")) k++;
        return k + 1;
    }
    if (tk_is(ts, k, "struct")) {
        int e = parse_struct(s, r, k);
        if (e < 0) return -1;
        if (tk_is(ts, e, ";")) return e + 1;
        k = e - 1;   // declarators follow: treat the struct's name as the type
        char nm[64];
        snprintf(nm, sizeof nm, "%s", s->st[s->nst - 1].name);
        // fall through with the type given by name
        int i = e;
        while (i < ts->n && !tk_is(ts, i, ";")) i++;
        // rare (struct S {..} x;): declare the variables as members
        sb_printf(&s->body, "    %s", nm);
        rewrite_range(r, &s->body, e, i, 1);
        sb_puts(&s->body, ";\n");
        (void)start;
        return i + 1;
    }
    if (ts->t[k].kind != TK_IDENT) return fail(s, "expected a declaration", ts, k);
    char type[64];
    tk_text(ts, k, type, sizeof type);
    int tk = k;
    k++;
    if (ts->t[k].kind != TK_IDENT) return fail(s, "expected a name", ts, k);
    // a function
    if (tk_is(ts, k + 1, "(")) {
        int pe = match(ts, k + 1, "(", ")");
        if (pe < 0) return fail(s, "unclosed parameters", ts, k);
        if (tk_is(ts, pe + 1, ";")) return pe + 2;   // a prototype: member functions need none
        if (!tk_is(ts, pe + 1, "{")) return fail(s, "expected a function body", ts, pe + 1);
        int be = match(ts, pe + 1, "{", "}");
        if (be < 0) return fail(s, "unclosed function", ts, pe + 1);
        char name[64], on[96];
        tk_text(ts, k, name, sizeof name);
        out_name(name, on, sizeof on);
        forward_builtin(s, on);
        sb_printf(&s->body, "    %s %s", msl_type(type), on);
        write_params(s, r, &s->body, k + 1, pe);
        sb_puts(&s->body, " ");
        r->depth = 0;
        rewrite_range(r, &s->body, pe + 1, be + 1, 0);
        sb_puts(&s->body, "\n");
        return be + 1;
    }
    (void)tk;
    // variables: name [N] (= init), ...
    int i = k;
    while (1) {
        char name[64], on[96];
        tk_text(ts, i, name, sizeof name);
        out_name(name, on, sizeof on);
        int count = 1, isarr = 0;
        i++;
        if (tk_is(ts, i, "[")) {
            int c = match(ts, i, "[", "]");
            if (c < 0) return fail(s, "array", ts, i);
            count = array_size(s, ts, i + 1, c);
            isarr = 1;
            if (count < 1) return fail(s, "array size", ts, i);
            i = c + 1;
        }
        int init_a = -1, init_b = -1;
        if (tk_is(ts, i, "=")) {
            init_a = i + 1;
            int d = 0;
            while (i < ts->n && ts->t[i].kind != TK_END) {
                if (tk_is(ts, i, "(") || tk_is(ts, i, "[") || tk_is(ts, i, "{")) d++;
                if (tk_is(ts, i, ")") || tk_is(ts, i, "]") || tk_is(ts, i, "}")) d--;
                if (d == 0 && (tk_is(ts, i, ",") || tk_is(ts, i, ";"))) break;
                i++;
            }
            init_b = i;
        }
        const btype *b = find_btype(type);
        int vertex = s->stage == MG_VERTEX;
        var v = {{0}, {0}, count, flat, loc, isarr};
        snprintf(v.name, sizeof v.name, "%s", name);
        snprintf(v.type, sizeof v.type, "%s", type);
        char arr[24] = "";
        if (isarr) snprintf(arr, sizeof arr, "[%d]", count);
        if (uniform) {
            if (b && b->sampler) {
                if (s->nsamp >= 16) return fail(s, "too many samplers", ts, i);
                s->samp[s->nsamp++] = v;
                sb_printf(&s->body, "    %s %s%s;\n", b->msl, on, arr);
            } else {
                if (s->nuni >= MAXU) return fail(s, "too many uniforms", ts, i);
                s->uni[s->nuni++] = v;
            }
        } else if (attribute || (in && vertex)) {
            if (s->nattr >= 32) return fail(s, "too many attributes", ts, i);
            s->attr[s->nattr++] = v;
            sb_printf(&s->body, "    %s %s%s;\n", msl_type(type), on, arr);
        } else if (varying || (out && vertex) || (in && !vertex)) {
            if (s->nvary >= 64) return fail(s, "too many varyings", ts, i);
            s->vary[s->nvary++] = v;
            sb_printf(&s->body, "    %s %s%s;\n", msl_type(type), on, arr);
        } else if (out) {
            if (s->nout >= 8) return fail(s, "too many outputs", ts, i);
            if (v.loc < 0) v.loc = s->nout;
            s->out[s->nout++] = v;
            sb_printf(&s->body, "    %s %s%s;\n", msl_type(type), on, arr);
        } else if (cnst) {
            if (init_a >= 0 && init_b == init_a + 1 && ts->t[init_a].kind == TK_NUM && s->nkonst < 64) {
                snprintf(s->konst[s->nkonst].name, 64, "%s", name);
                char w[32];
                tk_text(ts, init_a, w, sizeof w);
                s->konst[s->nkonst++].value = atoi(w);
            }
            sb_printf(&s->pre, "constant %s %s%s", msl_type(type), on, arr);
            if (init_a >= 0) {
                sb_puts(&s->pre, " =");
                rw r2 = *r;
                r2.native_ctor = 1;
                r2.depth = 0;
                rewrite_range(&r2, &s->pre, init_a, init_b, 1);
            }
            sb_puts(&s->pre, ";\n");
        } else {
            sb_printf(&s->body, "    %s %s%s", msl_type(type), on, arr);
            if (init_a >= 0) {
                sb_puts(&s->body, " =");
                r->depth = 0;
                rewrite_range(r, &s->body, init_a, init_b, 1);
            }
            sb_puts(&s->body, ";\n");
        }
        if (tk_is(ts, i, ",")) {
            i++;
            continue;
        }
        if (tk_is(ts, i, ";")) return i + 1;
        return fail(s, "expected ; after a declaration", ts, i);
    }
}

mg_shader *mg_parse(const char *src, int stage, char *log, size_t log_size)
{
    mg_shader *s = calloc(1, sizeof *s);
    s->stage = stage;
    toks ts = {0};
    tokenize(&ts, src);
    rw r = {s, &ts, 0, 0, {0}, 0};
    int k = 0, ok = 1;
    while (k < ts.n && ts.t[k].kind != TK_END) {
        if (ts.t[k].kind == TK_PP) {
            const char *p = ts.src + ts.t[k].pos + 1;
            while (*p == ' ' || *p == '\t') p++;
            if (!strncmp(p, "version", 7) && strstr(p, "300")) s->es3 = 1;
            if (!strncmp(p, "define", 6)) {
                rewrite_tok(&r, &s->pre, k, 0);
                sb_puts(&s->pre, "\n");
                const char *n = p + 6;
                while (*n == ' ' || *n == '\t') n++;
                int len = 0;
                while (isalnum((unsigned char)n[len]) || n[len] == '_') len++;
                // "#define N 8": a constant array sizes can name (gl4es's _gl4es_MaxLights)
                const char *v = n + len;
                while (*v == ' ' || *v == '\t') v++;
                if (isdigit((unsigned char)*v) && len < 64 && s->nkonst < 64) {
                    snprintf(s->konst[s->nkonst].name, 64, "%.*s", len, n);
                    s->konst[s->nkonst++].value = atoi(v);
                }
                sb_puts(&s->undefs, "#undef ");
                sb_putn(&s->undefs, n, (size_t)len);
                sb_puts(&s->undefs, "\n");
            } else {
                sb_puts(&s->body, "\n");
                rewrite_tok(&r, &s->body, k, 0);
                sb_puts(&s->body, "\n");
            }
            k++;
            continue;
        }
        if (tk_is(&ts, k, ";")) {
            k++;
            continue;
        }
        k = parse_global(s, &r, k);
        if (k < 0) {
            ok = 0;
            break;
        }
    }
    free(ts.t);
    if (!ok) {
        snprintf(log, log_size, "ERROR: %s\n", s->err);
        mg_free(s);
        return NULL;
    }
    // the uniform buffer's layout
    int off = 0, al = 16;
    for (int i = 0; i < s->nuni; i++) {
        int a, sz = type_layout(s, s->uni[i].type, &a);
        off = (off + a - 1) / a * a;
        add_leaf(s, s->uni[i].name, s->uni[i].type, s->uni[i].count, s->uni[i].arr, off);
        off += sz * s->uni[i].count;
    }
    s->ubo_size = (off + al - 1) / al * al;
    if (!s->ubo_size) s->ubo_size = 16;
    for (int i = 0; i < s->nsamp && s->nleaf < 1024; i++) {
        mg_uniform *l = &s->leaf[s->nleaf++];
        snprintf(l->name, sizeof l->name, "%s", s->samp[i].name);
        l->gltype = find_btype(s->samp[i].type)->gl;
        l->count = 1;
        l->offset = l->stride = -1;
        l->sampler_slot = i;
    }
    for (int i = 0; i < s->nattr; i++) {
        snprintf(s->attrs[i].name, sizeof s->attrs[i].name, "%s", s->attr[i].name);
        const btype *b = find_btype(s->attr[i].type);
        s->attrs[i].gltype = b ? b->gl : 0x8B52;
    }
    if (log_size) log[0] = 0;
    return s;
}

void mg_free(mg_shader *s)
{
    if (!s) return;
    free(s->pre.p);
    free(s->body.p);
    free(s->undefs.p);
    free(s);
}

int mg_uniform_count(const mg_shader *s) { return s->nleaf; }
const mg_uniform *mg_uniform_at(const mg_shader *s, int i) { return &s->leaf[i]; }
int mg_uniform_size(const mg_shader *s) { return s->ubo_size; }
int mg_attrib_count(const mg_shader *s) { return s->nattr; }
const mg_attrib *mg_attrib_at(const mg_shader *s, int i) { return &s->attrs[i]; }
int mg_sampler_cube(const mg_shader *s, int slot)
{
    return slot >= 0 && slot < s->nsamp && !strcmp(s->samp[slot].type, "samplerCube");
}

// ---- linking: the Metal source ------------------------------------------------------------------
// in three parts: Metal compiles the texture helpers slowly (on a cold cache
// about 600 ms a program with them, 20-50 ms without), so a program gets
// those for the sampler kinds its stages have only
static const char *prelude_head =
    "#include <metal_stdlib>\n"
    "using namespace metal;\n";
static const char *prelude_2d =
    "struct mg_s2d { texture2d<float> t; sampler s; bool depth; };\n"
    "static inline float4 mg_d4(float d) { return float4(d, d, d, 1.0); }\n"
    "static inline float4 mg_texture2D(mg_s2d s, float2 c) { return s.depth ? mg_d4(s.t.sample(s.s, c).x) : s.t.sample(s.s, c); }\n"
    "static inline float4 mg_texture2D(mg_s2d s, float2 c, float b) { return s.depth ? mg_d4(s.t.sample(s.s, c).x) : s.t.sample(s.s, c, bias(b)); }\n"
    "static inline float4 mg_texture2DLod(mg_s2d s, float2 c, float l) { return s.depth ? mg_d4(s.t.sample(s.s, c, level(l)).x) : s.t.sample(s.s, c, level(l)); }\n"
    "static inline float4 mg_texture2DLod0(mg_s2d s, float2 c) { return mg_texture2DLod(s, c, 0.0); }\n"
    "static inline float4 mg_texture2DLod0(mg_s2d s, float2 c, float b) { return mg_texture2DLod(s, c, 0.0); }\n"
    "static inline float4 mg_texture2DProj(mg_s2d s, float3 c) { return mg_texture2D(s, c.xy / c.z); }\n"
    "static inline float4 mg_texture2DProj(mg_s2d s, float4 c) { return mg_texture2D(s, c.xy / c.w); }\n"
    "static inline float4 mg_texture2DProj(mg_s2d s, float3 c, float b) { return mg_texture2D(s, c.xy / c.z, b); }\n"
    "static inline float4 mg_texture2DProj(mg_s2d s, float4 c, float b) { return mg_texture2D(s, c.xy / c.w, b); }\n"
    "static inline float4 mg_texture2DProjLod(mg_s2d s, float3 c, float l) { return mg_texture2DLod(s, c.xy / c.z, l); }\n"
    "static inline float4 mg_texture2DProjLod(mg_s2d s, float4 c, float l) { return mg_texture2DLod(s, c.xy / c.w, l); }\n"
    "static inline float4 mg_texture2DProjLod0(mg_s2d s, float3 c) { return mg_texture2DLod(s, c.xy / c.z, 0.0); }\n"
    "static inline float4 mg_texture2DProjLod0(mg_s2d s, float4 c) { return mg_texture2DLod(s, c.xy / c.w, 0.0); }\n"
    "static inline float4 mg_texture2DGrad(mg_s2d s, float2 c, float2 x, float2 y) { return s.t.sample(s.s, c, gradient2d(x, y)); }\n"
    "static inline float4 mg_texture(mg_s2d s, float2 c) { return mg_texture2D(s, c); }\n"
    "static inline float4 mg_texture(mg_s2d s, float2 c, float b) { return mg_texture2D(s, c, b); }\n"
    "static inline float4 mg_textureLod(mg_s2d s, float2 c, float l) { return mg_texture2DLod(s, c, l); }\n"
    "static inline float4 mg_textureLod0(mg_s2d s, float2 c) { return mg_texture2DLod(s, c, 0.0); }\n"
    "static inline float4 mg_shadow2D(mg_s2d s, float3 c) { float d = s.t.sample(s.s, c.xy, level(0.0)).x; return float4(c.z <= d ? 1.0 : 0.0); }\n"
    "static inline float4 mg_shadow2DProj(mg_s2d s, float4 c) { return mg_shadow2D(s, c.xyz / c.w); }\n";
static const char *prelude_cube =
    "struct mg_scube { texturecube<float> t; sampler s; };\n"
    "static inline float4 mg_textureCube(mg_scube s, float3 c) { return s.t.sample(s.s, c); }\n"
    "static inline float4 mg_textureCube(mg_scube s, float3 c, float b) { return s.t.sample(s.s, c, bias(b)); }\n"
    "static inline float4 mg_textureCubeLod(mg_scube s, float3 c, float l) { return s.t.sample(s.s, c, level(l)); }\n"
    "static inline float4 mg_textureCubeLod0(mg_scube s, float3 c) { return s.t.sample(s.s, c, level(0.0)); }\n"
    "static inline float4 mg_texture(mg_scube s, float3 c) { return mg_textureCube(s, c); }\n"
    "static inline float4 mg_texture(mg_scube s, float3 c, float b) { return mg_textureCube(s, c, b); }\n"
    "static inline float4 mg_textureLod(mg_scube s, float3 c, float l) { return mg_textureCubeLod(s, c, l); }\n"
    "static inline float4 mg_textureLod0(mg_scube s, float3 c) { return mg_textureCubeLod0(s, c); }\n";
static const char *prelude =
    // GLSL's mixed scalar and vector forms
    "template<typename T> static inline T mg_mod(T x, T y) { return x - y * floor(x / y); }\n"
    "template<typename T> static inline T mg_min(T a, T b) { return min(a, b); }\n"
    "template<typename T> static inline T mg_max(T a, T b) { return max(a, b); }\n"
    "template<typename T> static inline T mg_clamp(T x, T a, T b) { return clamp(x, a, b); }\n"
    "template<typename T> static inline T mg_mix(T x, T y, T a) { return mix(x, y, a); }\n"
    "template<typename T> static inline T mg_step(T e, T x) { return step(e, x); }\n"
    "template<typename T> static inline T mg_smoothstep(T a, T b, T x) { return smoothstep(a, b, x); }\n"
    "#define MG_VS(V, S) \\\n"
    "static inline V mg_mod(V x, S y) { return mg_mod(x, V(y)); } \\\n"
    "static inline V mg_min(V a, S b) { return min(a, V(b)); } \\\n"
    "static inline V mg_max(V a, S b) { return max(a, V(b)); } \\\n"
    "static inline V mg_clamp(V x, S a, S b) { return clamp(x, V(a), V(b)); } \\\n"
    "static inline V mg_mix(V x, V y, S a) { return mix(x, y, V(a)); } \\\n"
    "static inline V mg_step(S e, V x) { return step(V(e), x); } \\\n"
    "static inline V mg_smoothstep(S a, S b, V x) { return smoothstep(V(a), V(b), x); }\n"
    "MG_VS(float2, float) MG_VS(float3, float) MG_VS(float4, float)\n"
    "#define MG_IS(V, S) \\\n"
    "static inline V mg_min(V a, S b) { return min(a, V(b)); } \\\n"
    "static inline V mg_max(V a, S b) { return max(a, V(b)); } \\\n"
    "static inline V mg_clamp(V x, S a, S b) { return clamp(x, V(a), V(b)); }\n"
    "MG_IS(int2, int) MG_IS(int3, int) MG_IS(int4, int)\n"
    "static inline float mg_atan(float y, float x) { return atan2(y, x); }\n"
    "template<typename T> static inline T mg_atan(T y, T x) { return atan2(y, x); }\n"
    "template<typename T> static inline T mg_atan(T x) { return atan(x); }\n"
    "template<typename T> static inline T mg_radians(T d) { return d * 0.017453292519943295; }\n"
    "template<typename T> static inline T mg_degrees(T r) { return r * 57.29577951308232; }\n"
    "#define MG_CMP(N, OP) template<typename T> static inline auto N(T a, T b) { return a OP b; }\n"
    "MG_CMP(mg_lessThan, <) MG_CMP(mg_lessThanEqual, <=) MG_CMP(mg_greaterThan, >) MG_CMP(mg_greaterThanEqual, >=)\n"
    "MG_CMP(mg_equal, ==) MG_CMP(mg_notEqual, !=)\n"
    "template<typename T> static inline T mg_not(T a) { return !a; }\n"
    "static inline float2x2 mg_matrixCompMult(float2x2 a, float2x2 b) { return float2x2(a[0] * b[0], a[1] * b[1]); }\n"
    "static inline float3x3 mg_matrixCompMult(float3x3 a, float3x3 b) { return float3x3(a[0] * b[0], a[1] * b[1], a[2] * b[2]); }\n"
    "static inline float4x4 mg_matrixCompMult(float4x4 a, float4x4 b) { return float4x4(a[0] * b[0], a[1] * b[1], a[2] * b[2], a[3] * b[3]); }\n"
    // constructors: GLSL's, where MSL's differ (one argument: broadcast or truncate)
    "#define MG_VEC(N, T, B) \\\n"
    "template<typename... A> static inline T##N mg_##B##N(A... a) { return T##N(a...); } \\\n"
    "static inline T##N mg_##B##N(float x) { return T##N(T(x)); } \\\n"
    "static inline T##N mg_##B##N(int x) { return T##N(T(x)); } \\\n"
    "static inline T##N mg_##B##N(uint x) { return T##N(T(x)); } \\\n"
    "static inline T##N mg_##B##N(bool x) { return T##N(T(x)); }\n"
    "MG_VEC(2, float, vec) MG_VEC(3, float, vec) MG_VEC(4, float, vec)\n"
    "MG_VEC(2, int, ivec) MG_VEC(3, int, ivec) MG_VEC(4, int, ivec)\n"
    "MG_VEC(2, uint, uvec) MG_VEC(3, uint, uvec) MG_VEC(4, uint, uvec)\n"
    "MG_VEC(2, bool, bvec) MG_VEC(3, bool, bvec) MG_VEC(4, bool, bvec)\n"
    "static inline float2 mg_vec2(float3 v) { return v.xy; }\n"
    "static inline float2 mg_vec2(float4 v) { return v.xy; }\n"
    "static inline float3 mg_vec3(float4 v) { return v.xyz; }\n"
    "static inline int2 mg_ivec2(float3 v) { return int2(v.xy); }\n"
    "static inline int2 mg_ivec2(float4 v) { return int2(v.xy); }\n"
    "static inline int3 mg_ivec3(float4 v) { return int3(v.xyz); }\n"
    "static inline float2x2 mg_mat2(float s) { return float2x2(s); }\n"
    "static inline float2x2 mg_mat2(float2 a, float2 b) { return float2x2(a, b); }\n"
    "static inline float2x2 mg_mat2(float a, float b, float c, float d) { return float2x2(float2(a, b), float2(c, d)); }\n"
    "static inline float2x2 mg_mat2(float2x2 m) { return m; }\n"
    "static inline float2x2 mg_mat2(float3x3 m) { return float2x2(m[0].xy, m[1].xy); }\n"
    "static inline float2x2 mg_mat2(float4x4 m) { return float2x2(m[0].xy, m[1].xy); }\n"
    "static inline float3x3 mg_mat3(float s) { return float3x3(s); }\n"
    "static inline float3x3 mg_mat3(float3 a, float3 b, float3 c) { return float3x3(a, b, c); }\n"
    "static inline float3x3 mg_mat3(float a, float b, float c, float d, float e, float f, float g, float h, float i) "
    "{ return float3x3(float3(a, b, c), float3(d, e, f), float3(g, h, i)); }\n"
    "static inline float3x3 mg_mat3(float3x3 m) { return m; }\n"
    "static inline float3x3 mg_mat3(float4x4 m) { return float3x3(m[0].xyz, m[1].xyz, m[2].xyz); }\n"
    "static inline float3x3 mg_mat3(float2x2 m) { return float3x3(float3(m[0], 0), float3(m[1], 0), float3(0, 0, 1)); }\n"
    "static inline float4x4 mg_mat4(float s) { return float4x4(s); }\n"
    "static inline float4x4 mg_mat4(float4 a, float4 b, float4 c, float4 d) { return float4x4(a, b, c, d); }\n"
    "static inline float4x4 mg_mat4(float a, float b, float c, float d, float e, float f, float g, float h, float i, "
    "float j, float k, float l, float m, float n, float o, float p) "
    "{ return float4x4(float4(a, b, c, d), float4(e, f, g, h), float4(i, j, k, l), float4(m, n, o, p)); }\n"
    "static inline float4x4 mg_mat4(float4x4 m) { return m; }\n"
    "static inline float4x4 mg_mat4(float3x3 m) { return float4x4(float4(m[0], 0), float4(m[1], 0), float4(m[2], 0), float4(0, 0, 0, 1)); }\n";

// the struct of uniforms of one stage, in the layout the leaves were given
static void write_ustruct(const mg_shader *s, sb *o)
{
    sb_puts(o, "struct U {");
    if (!s->nuni) sb_puts(o, " float4 mg_none;");
    for (int i = 0; i < s->nuni; i++) {
        char on[96];
        out_name(s->uni[i].name, on, sizeof on);
        if (s->uni[i].arr) sb_printf(o, " %s %s[%d];", msl_type(s->uni[i].type), on, s->uni[i].count);
        else sb_printf(o, " %s %s;", msl_type(s->uni[i].type), on);
    }
    sb_puts(o, " };\n");
}

static void write_stage(const mg_shader *s, sb *o, const char *ns)
{
    sb_printf(o, "namespace %s {\n", ns);
    sb_puts(o, sb_str((sb *)&s->pre));
    write_ustruct(s, o);
    sb_puts(o, "struct G {\n    constant U *Up;\n");
    if (s->stage == MG_VERTEX) {
        sb_puts(o, "    float4 mg_Position = float4(0.0);\n    float mg_PointSize = 1.0;\n");
    } else {
        sb_puts(o, "    float4 mg_FragData[4];\n    float4 mg_FragCoord;\n    bool mg_FrontFacing;\n"
                   "    float2 mg_PointCoord;\n");
    }
    sb_puts(o, sb_str((sb *)&s->body));
    sb_puts(o, "};\n");
    sb_puts(o, sb_str((sb *)&s->undefs));
    sb_printf(o, "} // namespace %s\n", ns);
}

static const var *find_var(const var *v, int n, const char *name)
{
    for (int i = 0; i < n; i++)
        if (!strcmp(v[i].name, name)) return &v[i];
    return NULL;
}

// a fragment varying "x_N" that the vertex shader writes as element N of an
// array "x": gl4es's own fragment shader reads its texture coordinates so
// (_gl4es_TexCoord_0) when an ARB vertex program writes them as an array
static const var *vs_array_elem(const var *vary, int n, const char *name)
{
    const char *u = strrchr(name, '_');
    if (!u || !u[1] || strspn(u + 1, "0123456789") != strlen(u + 1)) return NULL;
    char base[96];
    snprintf(base, sizeof base, "%.*s", (int)(u - name), name);
    const var *v = find_var(vary, n, base);
    return v && (v->count > 1 || v->arr) && atoi(u + 1) < v->count ? v : NULL;
}

static void write_sampler_params(const mg_shader *s, sb *o)
{
    for (int i = 0; i < s->nsamp; i++) {
        if (!strcmp(s->samp[i].type, "samplerCube"))
            sb_printf(o, ",\n    texturecube<float> t%d [[texture(%d)]], sampler s%d [[sampler(%d)]]", i, i, i, i);
        else
            sb_printf(o, ",\n    texture2d<float> t%d [[texture(%d)]], sampler s%d [[sampler(%d)]]", i, i, i, i);
    }
}

static void write_sampler_init(const mg_shader *s, sb *o)
{
    for (int i = 0; i < s->nsamp; i++) {
        char on[96];
        out_name(s->samp[i].name, on, sizeof on);
        if (!strcmp(s->samp[i].type, "samplerCube")) sb_printf(o, "    g.%s.t = t%d; g.%s.s = s%d;\n", on, i, on, i);
        else
            sb_printf(o, "    g.%s.t = t%d; g.%s.s = s%d; g.%s.depth = (mg_depth >> %d) & 1;\n", on, i, on, i, on, i);
    }
}

char *mg_link(const mg_shader *vs, const mg_shader *fs, const int *attr_loc, char *log, size_t log_size)
{
    if (!vs || !fs || vs->stage != MG_VERTEX || fs->stage != MG_FRAGMENT) {
        snprintf(log, log_size, "ERROR: a program needs a vertex and a fragment shader\n");
        return NULL;
    }
    sb st = {0};
    write_stage(vs, &st, "mg_vs");
    write_stage(fs, &st, "mg_fs");
    sb o = {0};
    sb_puts(&o, prelude_head);
    if (strstr(sb_str(&st), "mg_s2d")) sb_puts(&o, prelude_2d);
    if (strstr(sb_str(&st), "mg_scube")) sb_puts(&o, prelude_cube);
    sb_puts(&o, prelude);
    sb_puts(&o, sb_str(&st));
    free(st.p);

    // vertex inputs and outputs
    sb_puts(&o, "struct mg_VIn {");
    for (int i = 0; i < vs->nattr; i++) {
        char on[96];
        out_name(vs->attr[i].name, on, sizeof on);
        sb_printf(&o, " %s %s [[attribute(%d)]];", msl_type(vs->attr[i].type), on, attr_loc ? attr_loc[i] : i);
    }
    if (!vs->nattr) sb_puts(&o, " float4 mg_none [[attribute(15)]];");
    sb_puts(&o, " };\n");
    sb_puts(&o, "struct mg_VOut {\n    float4 mg_pos [[position]];\n");
    if (vs->writes_psize) sb_puts(&o, "    float mg_psize [[point_size]];\n");
    // only the varyings the fragment shader declares: gl4es declares many
    // (gl_TexCoord[8], both colours, fog...) and Metal limits the
    // interpolated components between the stages
    for (int i = 0; i < vs->nvary; i++) {
        const var *v = &vs->vary[i];
        int whole = find_var(fs->vary, fs->nvary, v->name) != NULL;
        char on[96];
        out_name(v->name, on, sizeof on);
        for (int e = 0; e < v->count; e++) {
            char nm[112], en[112];
            snprintf(en, sizeof en, "%s_%d", v->name, e);
            if (!whole && !((v->count > 1 || v->arr) && find_var(fs->vary, fs->nvary, en))) continue;
            if ((v->count > 1 || v->arr)) snprintf(nm, sizeof nm, "%s_%d", on, e);
            else snprintf(nm, sizeof nm, "%s", on);
            sb_printf(&o, "    %s %s [[user(%s)]]%s;\n", msl_type(v->type), nm, nm, v->flat ? " [[flat]]" : "");
        }
    }
    sb_puts(&o, "};\n");
    sb_puts(&o, "vertex mg_VOut mg_vmain(mg_VIn in [[stage_in]], constant mg_vs::U *U [[buffer(16)]], "
                "constant uint &mg_depth [[buffer(17)]]");
    write_sampler_params(vs, &o);
    sb_puts(&o, ")\n{\n    mg_vs::G g;\n    g.Up = U;\n");
    for (int i = 0; i < vs->nattr; i++) {
        char on[96];
        out_name(vs->attr[i].name, on, sizeof on);
        sb_printf(&o, "    g.%s = in.%s;\n", on, on);
    }
    write_sampler_init(vs, &o);
    sb_puts(&o, "    g.mg_main();\n    mg_VOut o;\n    o.mg_pos = g.mg_Position;\n"
                // OpenGL's clip z (-w..w) to Metal's (0..w); y flipped so rows are OpenGL's (row 0 at the bottom)
                "    o.mg_pos.z = (o.mg_pos.z + o.mg_pos.w) * 0.5;\n    o.mg_pos.y = -o.mg_pos.y;\n");
    if (vs->writes_psize) sb_puts(&o, "    o.mg_psize = g.mg_PointSize;\n");
    for (int i = 0; i < vs->nvary; i++) {
        const var *v = &vs->vary[i];
        int whole = find_var(fs->vary, fs->nvary, v->name) != NULL;
        char on[96];
        out_name(v->name, on, sizeof on);
        if ((v->count > 1 || v->arr)) {
            for (int e = 0; e < v->count; e++) {
                char en[112];
                snprintf(en, sizeof en, "%s_%d", v->name, e);
                if (whole || find_var(fs->vary, fs->nvary, en)) sb_printf(&o, "    o.%s_%d = g.%s[%d];\n", on, e, on, e);
            }
        } else if (whole)
            sb_printf(&o, "    o.%s = g.%s;\n", on, on);
    }
    sb_puts(&o, "    return o;\n}\n");

    // fragment inputs (the vertex shader's outputs it reads) and outputs
    sb_puts(&o, "struct mg_FIn {\n    float4 mg_fc [[position]];\n");
    for (int i = 0; i < fs->nvary; i++) {
        const var *v = &fs->vary[i];
        const var *w = find_var(vs->vary, vs->nvary, v->name);
        if (!w && !(v->count > 1 || v->arr)) w = vs_array_elem(vs->vary, vs->nvary, v->name);
        if (!w) continue;
        char on[96];
        out_name(v->name, on, sizeof on);
        for (int e = 0; e < v->count; e++) {
            char nm[112];
            if ((v->count > 1 || v->arr)) snprintf(nm, sizeof nm, "%s_%d", on, e);
            else snprintf(nm, sizeof nm, "%s", on);
            sb_printf(&o, "    %s %s [[user(%s)]]%s;\n", msl_type(w->type), nm, nm, w->flat ? " [[flat]]" : "");
        }
    }
    sb_puts(&o, "};\n");
    int ncol = fs->nout ? 0 : (fs->uses_fragdata ? 4 : 1);
    for (int i = 0; i < fs->nout; i++)
        if (fs->out[i].loc + 1 > ncol) ncol = fs->out[i].loc + 1;
    sb_puts(&o, "struct mg_FOut {");
    for (int i = 0; i < ncol; i++) sb_printf(&o, " float4 c%d [[color(%d)]];", i, i);
    sb_puts(&o, " };\n");
    sb_puts(&o, "fragment mg_FOut mg_fmain(mg_FIn in [[stage_in]], constant mg_fs::U *U [[buffer(16)]], "
                "constant uint &mg_depth [[buffer(17)]]");
    if (fs->uses_frontfacing) sb_puts(&o, ", bool mg_ff [[front_facing]]");
    if (fs->uses_pointcoord) sb_puts(&o, ", float2 mg_pc [[point_coord]]");
    write_sampler_params(fs, &o);
    sb_puts(&o, ")\n{\n    mg_fs::G g;\n    g.Up = U;\n    g.mg_FragCoord = in.mg_fc;\n"
                "    g.mg_FragData[0] = g.mg_FragData[1] = g.mg_FragData[2] = g.mg_FragData[3] = float4(0.0);\n");
    if (fs->uses_frontfacing) sb_puts(&o, "    g.mg_FrontFacing = !mg_ff;   // y is flipped: the winding too\n");
    // OpenGL's point sprite origin is the upper left on the screen; y is flipped, so Metal's upper left is OpenGL's lower left
    if (fs->uses_pointcoord) sb_puts(&o, "    g.mg_PointCoord = float2(mg_pc.x, 1.0 - mg_pc.y);\n");
    for (int i = 0; i < fs->nvary; i++) {
        const var *v = &fs->vary[i];
        char on[96];
        out_name(v->name, on, sizeof on);
        if (!find_var(vs->vary, vs->nvary, v->name) && ((v->count > 1 || v->arr) || !vs_array_elem(vs->vary, vs->nvary, v->name))) {
            // a varying the vertex shader does not write reads as zero (OpenGL
            // would not link): worth knowing when a draw comes out black
            if (af_debug_env("MTLGLES_TRACE_LINK")) fprintf(stderr, "mtlgles: varying %s %s[%d] is not written by the vertex shader\n", v->type, v->name, v->count);
            sb_printf(&o, "    g.%s = {};\n", on);
            continue;
        }
        if ((v->count > 1 || v->arr))
            for (int e = 0; e < v->count; e++) sb_printf(&o, "    g.%s[%d] = in.%s_%d;\n", on, e, on, e);
        else
            sb_printf(&o, "    g.%s = in.%s;\n", on, on);
    }
    write_sampler_init(fs, &o);
    sb_puts(&o, "    g.mg_main();\n    mg_FOut o;\n");
    if (fs->nout) {
        for (int i = 0; i < ncol; i++) sb_printf(&o, "    o.c%d = float4(0.0);\n", i);
        for (int i = 0; i < fs->nout; i++) {
            char on[96];
            out_name(fs->out[i].name, on, sizeof on);
            const btype *b = find_btype(fs->out[i].type);
            int comps = b ? (int)(b->size <= 4 ? 1 : b->size == 8 ? 2 : 4) : 4;
            if (comps == 4) sb_printf(&o, "    o.c%d = float4(g.%s);\n", fs->out[i].loc, on);
            else if (comps == 1) sb_printf(&o, "    o.c%d = float4(g.%s, 0.0, 0.0, 1.0);\n", fs->out[i].loc, on);
            else sb_printf(&o, "    o.c%d = float4(float2(g.%s), 0.0, 1.0);\n", fs->out[i].loc, on);
        }
    } else {
        for (int i = 0; i < ncol; i++) sb_printf(&o, "    o.c%d = g.mg_FragData[%d];\n", i, i);
    }
    sb_puts(&o, "    return o;\n}\n");
    if (log_size) log[0] = 0;
    return o.p;
}
