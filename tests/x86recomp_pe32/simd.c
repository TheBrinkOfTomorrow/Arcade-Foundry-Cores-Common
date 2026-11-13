// SSE/SSSE3/SSE4.1 and MMX instructions the 32-bit Windows games use, checked
// two ways by smoke_pe32.py: built for the host and run natively (the
// reference), and built by i686 MinGW, translated by recomp.py and run on
// rt32.c. Both print the same hex dump of every result.
#include <stdint.h>
#include <string.h>
#include <immintrin.h>

static char buf[4096];
static int pos;
static void put(const char *s) { while (*s) buf[pos++] = *s++; }
static void dump(const char *name, const void *p, int n)
{
    put(name); put(" ");
    for (int i = 0; i < n; i++) { uint8_t v = ((const uint8_t *)p)[i]; buf[pos++] = "0123456789abcdef"[v >> 4]; buf[pos++] = "0123456789abcdef"[v & 15]; }
    put("\n");
}
#define D(name, v) do { __typeof__(v) t_ = (v); dump(name, &t_, sizeof t_); } while (0)

// volatile inputs so nothing folds at compile time
static volatile uint8_t in8[32] = { 0, 1, 2, 127, 128, 129, 200, 255, 3, 250, 90, 160, 17, 64, 191, 254,
                                    255, 4, 130, 100, 126, 128, 60, 2, 250, 9, 70, 161, 33, 66, 190, 1 };
static volatile int32_t in32[8] = { 7, -9, 2147483000, -2147483000, 100000, -1, 0, 31 };
static volatile float inf[8] = { 1.5f, -2.25f, 3.75f, 1e9f, 0.5f, 4.0f, -8.5f, 2.5f };
static volatile double ind[4] = { 2.0, -9.75, 1e10, 0.25 };

static __m128i ld(int off) { uint8_t t[16]; for (int i = 0; i < 16; i++) t[i] = in8[(i + off) & 31]; __m128i v; memcpy(&v, t, 16); return v; }
static __m64 ld64(int off) { uint8_t t[8]; for (int i = 0; i < 8; i++) t[i] = in8[(i + off) & 31]; __m64 v; memcpy(&v, t, 8); return v; }

__attribute__((target("sse4.1"), noinline)) void simd_run(void)
{
    __m128i a = ld(0), b = ld(16);
    __m128i i32 = _mm_setr_epi32(in32[0], in32[1], in32[2], in32[3]);
    __m128i j32 = _mm_setr_epi32(in32[4], in32[5], in32[6], in32[7]);
    __m128 f = _mm_setr_ps(inf[0], inf[1], inf[2], inf[3]);
    __m128 g = _mm_setr_ps(inf[4], inf[5], inf[6], inf[7]);
    __m128d d = _mm_setr_pd(ind[0], ind[1]);
    __m128d e = _mm_setr_pd(ind[2], ind[3]);
    D("paddusb", _mm_adds_epu8(a, b));
    D("paddusw", _mm_adds_epu16(a, b));
    D("psubusb", _mm_subs_epu8(a, b));
    D("psubusw", _mm_subs_epu16(a, b));
    D("paddsb", _mm_adds_epi8(a, b));
    D("psubsb", _mm_subs_epi8(a, b));
    D("pcmpgtb", _mm_cmpgt_epi8(a, b));
    D("pcmpgtw", _mm_cmpgt_epi16(a, b));
    D("pavgw", _mm_avg_epu16(a, b));
    D("pminub", _mm_min_epu8(a, b));
    D("pmaxub", _mm_max_epu8(a, b));
    D("psadbw", _mm_sad_epu8(a, b));
    D("pminsd", _mm_min_epi32(i32, j32));
    D("pmaxsd", _mm_max_epi32(i32, j32));
    D("pmulld", _mm_mullo_epi32(i32, j32));
    D("pabsd", _mm_abs_epi32(i32));
    D("palignr5", _mm_alignr_epi8(a, b, 5));
    D("palignr20", _mm_alignr_epi8(a, b, 20));
    D("pblendvb", _mm_blendv_epi8(a, b, _mm_setr_epi8(-1, 0, -128, 1, 0x7f, -2, 0, 0, -1, -1, 3, -100, 0, 0x80, 0x40, -1)));
    D("pinsrb", _mm_insert_epi8(a, in32[0], 9));
    D("cvtdq2pd", _mm_cvtepi32_pd(i32));
    D("cvtps2dq", _mm_cvtps_epi32(f));
    D("cvttpd2dq", _mm_cvttpd_epi32(d));
    D("sqrtpd", _mm_sqrt_pd(_mm_setr_pd(ind[0], ind[3])));
    D("shufpd", _mm_shuffle_pd(d, e, 2));
    D("minpd", _mm_min_pd(d, e));
    D("maxpd", _mm_max_pd(d, e));
    D("addsubps", _mm_addsub_ps(f, g));
    D("movsldup", _mm_moveldup_ps(f));
    D("movshdup", _mm_movehdup_ps(f));
    // MMX forms of the byte ops
    __m64 m = ld64(3), n = ld64(19);
    D("mmx_paddsb", _mm_adds_pi8(m, n));
    D("mmx_psubusb", _mm_subs_pu8(m, n));
    D("mmx_pcmpgtb", _mm_cmpgt_pi8(m, n));
    D("mmx_psadbw", _mm_sad_pu8(m, n));
    _mm_empty();
}

#ifdef NATIVE
#include <stdio.h>
int main(void) { simd_run(); fwrite(buf, 1, pos, stdout); return 0; }
#else
#include <windows.h>
void __stdcall start(void)
{
    simd_run();
    DWORD n;
    WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), buf, pos, &n, NULL);
    ExitProcess(0);
}
#endif
