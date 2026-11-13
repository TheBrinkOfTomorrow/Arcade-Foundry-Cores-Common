// A 32-bit Windows program for the PE32 front end of tools/x86recomp/recomp.py,
// built by MinGW without its C runtime (-nostdlib), so the only imports are
// GetStdHandle, WriteFile and ExitProcess. What it prints is checked against
// values computed independently (smoke_pe32.py).
#include <windows.h>
#include <stdint.h>

static char buf[512];
static int pos;
static void put(const char *s) { while (*s) buf[pos++] = *s++; }
static void hex(uint64_t v, int digits) { for (int i = digits - 1; i >= 0; i--) buf[pos++] = "0123456789abcdef"[(v >> (4 * i)) & 15]; }
static void dec(int64_t v) { char t[24]; int n = 0; if (v < 0) { buf[pos++] = '-'; v = -v; } do t[n++] = '0' + v % 10; while (v /= 10); while (n) buf[pos++] = t[--n]; }

// FNV-1a over a buffer, 64-bit arithmetic on a 32-bit machine
static uint64_t fnv(const uint8_t *p, int n) { uint64_t h = 0xcbf29ce484222325ull; for (int i = 0; i < n; i++) { h ^= p[i]; h *= 0x100000001b3ull; } return h; }
// a switch MSVC and gcc turn into a jump table
__attribute__((noinline)) static int pick(int k) { switch (k) { case 0: return 10; case 1: return 22; case 2: return 31; case 3: return 47; case 4: return 5; case 5: return 61; case 6: return 7; default: return -1; } }
// calling conventions the Windows games use
__attribute__((noinline)) int __stdcall sub_std(int a, int b) { return a - b; }
__attribute__((noinline)) int __fastcall add_fast(int a, int b) { return a + b * 2; }
struct obj { int v; };
__attribute__((noinline, thiscall)) static int obj_get(struct obj *o, int k) { return o->v * k; }
// a function pointer in data, and an exported function
static int sq(int x) { return x * x; }
static int (*volatile fp)(int) = sq;
__declspec(dllexport) int exported(int a, int b) { return a * 3 + b; }
// floating point: x87 (the default for i686) and SSE2
__attribute__((noinline)) static double x87_poly(double x) { return ((x * 0.5 + 1.25) * x - 3.0) * x; }
__attribute__((noinline, target("sse2"), optimize("O2"))) static float sse_mix(float a, float b) { return a * b + a / b; }
// what MSVC and old CRTs emit: 'rep ret' (F3 C3) and reading a segment register
__attribute__((naked, noinline)) static int seg_ss(void) { __asm__("xorl %eax, %eax\n\tmovw %ss, %ax\n\trep ret"); }

void __stdcall start(void)
{
    static const char text[] = "the quick brown fox jumps over the lazy dog";
    uint64_t h = fnv((const uint8_t *)text, sizeof text - 1);
    int sum = 0;
    for (int k = -1; k < 8; k++) sum += pick(k);
    struct obj o = { 7 };
    volatile int64_t big = 9876543210123LL;
    put("hash "); hex(h, 16);
    put(" sum "); dec(sum);
    put(" calls "); dec(sub_std(9, 4)); put(" "); dec(add_fast(5, 6)); put(" "); dec(obj_get(&o, 6)); put(" "); dec(fp(12)); put(" "); dec(exported(2, 3));
    put("\ndiv "); dec(big / 77); put(" "); dec(big % 77);
    put(" x87 "); dec((int64_t)(x87_poly(2.5) * 1000)); put(" sse "); dec((int64_t)(sse_mix(3.0f, 0.5f) * 1000));
    put(" ss "); hex(seg_ss(), 2);
    put("\n");
    DWORD n;
    WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), buf, pos, &n, NULL);
    ExitProcess(0);
}
