// SSE4.2 string compares for the smoke test: every aggregation, polarity and
// element format of pcmpistri/pcmpestri/pcmpistrm/pcmpestrm over a few
// inputs, folded into one hash. Shared by smoke.c (translated) and the
// native check that produced its expected value.
#include <nmmintrin.h>
#include <stdint.h>
#include <string.h>

__attribute__((target("sse4.2"))) static uint64_t sse42_hash(void)
{
    static const char strs[6][17] = {"hello, world!!!!", "hello\0wor\0ld!!!!", "aaaabbbbccccdddd", "0123456789abcdef", "ab", ""};
    static const char sets[4][17] = {"lo!", "09az", "ab", "hello, world!!!!"};
    uint64_t h = 1469598103934665603ull;
#define MIX(v) h = (h ^ (uint64_t)(v)) * 1099511628211ull
    for (int si = 0; si < 6; si++)
        for (int ai = 0; ai < 4; ai++) {
            __m128i b = _mm_loadu_si128((const __m128i *)strs[si]);
            __m128i a = _mm_loadu_si128((const __m128i *)sets[ai]);
#define CASE(IMM) do { \
        MIX(_mm_cmpistri(a, b, IMM)); MIX(_mm_cmpistrc(a, b, IMM) | _mm_cmpistrz(a, b, IMM) << 1 | _mm_cmpistrs(a, b, IMM) << 2 | _mm_cmpistro(a, b, IMM) << 3); \
        { __m128i m = _mm_cmpistrm(a, b, IMM); uint64_t q[2]; memcpy(q, &m, 16); MIX(q[0]); MIX(q[1]); } \
        for (int la = 0; la <= 17; la += 3) for (int lb = 0; lb <= 17; lb += 5) { \
            MIX(_mm_cmpestri(a, la, b, lb, IMM)); MIX(_mm_cmpestri(a, -la, b, lb, IMM)); \
            MIX(_mm_cmpestrc(a, la, b, lb, IMM) | _mm_cmpestrz(a, la, b, lb, IMM) << 1 | _mm_cmpestrs(a, la, b, lb, IMM) << 2 | _mm_cmpestro(a, la, b, lb, IMM) << 3); \
            { __m128i m = _mm_cmpestrm(a, la, b, lb, IMM); uint64_t q[2]; memcpy(q, &m, 16); MIX(q[0]); MIX(q[1]); } } } while (0)
            // formats 0-3, aggregations 0-3, polarities 0-3, both index/mask selections
            CASE(0x00); CASE(0x01); CASE(0x02); CASE(0x03); CASE(0x04); CASE(0x05); CASE(0x06); CASE(0x07);
            CASE(0x08); CASE(0x09); CASE(0x0a); CASE(0x0b); CASE(0x0c); CASE(0x0d); CASE(0x0e); CASE(0x0f);
            CASE(0x10); CASE(0x14); CASE(0x18); CASE(0x1c); CASE(0x20); CASE(0x24); CASE(0x28); CASE(0x2c);
            CASE(0x30); CASE(0x34); CASE(0x38); CASE(0x3c); CASE(0x40); CASE(0x44); CASE(0x48); CASE(0x4c);
            CASE(0x50); CASE(0x58); CASE(0x60); CASE(0x68); CASE(0x70); CASE(0x78); CASE(0x7c); CASE(0x7d);
        }
    return h;
}
