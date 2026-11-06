#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include "sse42_str.h"

typedef int (*op_fn)(int, int);
static int add(int a, int b) { return a + b; }
static int sub(int a, int b) { return a - b; }
static int mul(int a, int b) { return a * b; }
static op_fn ops[] = { add, sub, mul };

__attribute__((noinline)) static const char *name_of(int k)
{
    switch (k) {
    case 0: return "zero"; case 1: return "one"; case 2: return "two"; case 3: return "three";
    case 4: return "four"; case 5: return "five"; case 6: return "six"; case 7: return "seven";
    default: return "many";
    }
}

__attribute__((noinline)) static uint64_t mix(uint64_t x)
{
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL; x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL; x ^= x >> 33;
    return x;
}

__attribute__((noinline)) static double poly(double x, float y)
{
    return 3.0 * x * x + (double)y * x - 1.5;
}

static __thread int tls_counter = 7;

int main(int argc, char **argv)
{
    char buf[256];
    uint64_t h = 0;
    for (int i = 0; i < 10; i++) h = mix(h + i);
    int n = snprintf(buf, sizeof buf, "hash %016llx %s %d %.3f\n", (unsigned long long)h, name_of(argc + 2),
                     ops[argc % 3](40, 2), poly(1.5, 2.0f));
    DWORD w;
    WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), buf, n, &w, NULL);
    tls_counter += argc;
    printf("tls %d argc %d ticks %lu\n", tls_counter, argc, (unsigned long)(GetTickCount() != 0));
    __int128 big = (__int128)h * (__int128)0x123456789abcdefULL;
    printf("big %016llx%016llx\n", (unsigned long long)(big >> 64), (unsigned long long)big);
    printf("sse42 %016llx\n", (unsigned long long)sse42_hash());
    return 0;
}
