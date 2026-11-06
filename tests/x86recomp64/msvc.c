// Freestanding Win64 program compiled the MSVC way (clang --target
// x86_64-pc-windows-msvc, linked by lld-link without a C runtime), for
// the recompiler's MSVC code patterns: image-relative jump tables, __try,
// struct returns, intrinsics.
typedef unsigned long long u64; typedef unsigned int u32; typedef unsigned char u8;
__declspec(dllimport) void *__stdcall GetStdHandle(u32);
__declspec(dllimport) int __stdcall WriteFile(void *, const void *, u32, u32 *, void *);
__declspec(dllimport) __declspec(noreturn) void __stdcall ExitProcess(u32);
__declspec(dllimport) u32 __stdcall GetTickCount(void);

int _fltused = 1;
void *memcpy(void *d, const void *s, u64 n) { u8 *dd = d; const u8 *ss = s; while (n--) *dd++ = *ss++; return d; }
void *memset(void *d, int v, u64 n) { u8 *dd = d; while (n--) *dd++ = (u8)v; return d; }

static char out[4096]; static u32 outn;
static void put(const char *s) { while (*s) out[outn++] = *s++; }
static void putu(u64 v, int base, int width) {
    char b[32]; int i = 0;
    do { int d = (int)(v % base); b[i++] = (char)(d < 10 ? '0' + d : 'a' + d - 10); v /= base; } while (v);
    while (i < width) b[i++] = '0';
    while (i) out[outn++] = b[--i];
}
static void puti(long long v) { if (v < 0) { put("-"); putu((u64)-v, 10, 0); } else putu((u64)v, 10, 0); }

typedef struct { double x, y, z; } vec3;
typedef struct { int a; short b; char c; } small3;
__declspec(noinline) static vec3 cross(vec3 a, vec3 b) { vec3 r = { a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x }; return r; }
__declspec(noinline) static small3 mk(int k) { small3 s = { k * 3, (short)(k * 5), (char)('a' + k % 26) }; return s; }

__declspec(noinline) static const char *day(int d) {
    switch (d) {
    case 0: return "sun"; case 1: return "mon"; case 2: return "tue"; case 3: return "wed";
    case 4: return "thu"; case 5: return "fri"; case 6: return "sat";
    case 10: return "ten"; case 11: return "eleven"; case 12: return "twelve"; case 13: return "thirteen";
    case 20: return "twenty"; case 21: return "twenty-one"; case 30: return "thirty";
    default: return "?";
    }
}
__declspec(noinline) static int op(int k, int a, int b) {
    switch (k & 15) {
    case 0: return a + b; case 1: return a - b; case 2: return a * b; case 3: return a ^ b; case 4: return a | b;
    case 5: return a & ~b; case 6: return a << (b & 7); case 7: return a >> (b & 7); case 8: return (a > b) ? a : b;
    case 9: return (a < b) ? a : b; case 10: return -a; case 11: return ~b; case 12: return a * 7 + b;
    case 13: return (int)((u32)a >> 3); case 14: return b - 2 * a; default: return 0;
    }
}
__declspec(noinline) static u64 rot(u64 x, int n) { return (x << n) | (x >> (64 - n)); }
__declspec(noinline) static int tryit(int d) {
    int r = 0;
    __try { r = 100 / (d + 1); r += 5; }
    __except (1) { r = -1; }
    __try { r *= 2; }
    __finally { r += 1; }
    return r;
}
__declspec(noinline) static double poly(double x) { return ((2.5 * x - 1.25) * x + 0.5) * x - 3.0; }

void mainCRTStartup(void) {
    u64 h = 0x9e3779b97f4a7c15ull;
    int acc = 0;
    for (int i = 0; i < 40; i++) { h = rot(h ^ (u64)i * 0x100000001b3ull, 13) * 0xff51afd7ed558ccdull; acc = op((int)(h >> 60), acc, (int)(h & 0xff)); }
    put("h "); putu(h, 16, 16); put(" acc "); puti(acc); put("\n");
    put("days");
    for (int i = -1; i < 32; i++) { put(" "); put(day(i)); }
    put("\n");
    vec3 a = { 1, 2, 3 }, b = { -4, 5.5, 0.25 }; vec3 r = cross(a, b);
    put("cross "); puti((long long)(r.x * 1000)); put(" "); puti((long long)(r.y * 1000)); put(" "); puti((long long)(r.z * 1000)); put("\n");
    small3 s = mk(29);
    put("small "); puti(s.a); put(" "); puti(s.b); put(" "); out[outn++] = s.c; put("\n");
    put("try "); puti(tryit(3)); put(" "); puti(tryit(19)); put("\n");
    put("poly "); puti((long long)(poly(3.5) * 1000000)); put(" tick "); puti(GetTickCount() != 0); put("\n");
    u32 w;
    WriteFile(GetStdHandle((u32)-11), out, outn, &w, 0);
    ExitProcess(0);
}
