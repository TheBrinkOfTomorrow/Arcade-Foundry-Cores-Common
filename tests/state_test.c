/* afcommon/state.h: write, measure, read back, refuse what is wrong. */
#include "afcommon/state.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures;
#define CHECK(c)                                                         \
    do {                                                                 \
        if (!(c)) {                                                      \
            fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #c); \
            ++failures;                                                  \
        }                                                                \
    } while (0)

static size_t write_state(unsigned char *buf, size_t cap, afc_state_writer *w) {
    unsigned char ram[300];
    for (int i = 0; i < 300; ++i) ram[i] = (unsigned char)i;
    afc_state_begin(w, buf, cap, "daytona", 0x010203, 12345);
    afc_state_chunk(w, "CPU ");
    afc_state_put_u32(w, 0xdeadbeef);
    afc_state_put_u64(w, 0x0123456789abcdefull);
    afc_state_chunk(w, "RAM ");
    afc_state_put(w, ram, sizeof ram);
    afc_state_chunk(w, "NONE"); /* empty chunk */
    return afc_state_end(w);
}

int main(void) {
    afc_state_writer w;
    char err[128];

    /* measure with no buffer, then write into exactly that */
    CHECK(write_state(NULL, 0, &w) == 0);
    const size_t need = w.need;
    unsigned char *buf = malloc(need);
    CHECK(write_state(buf, need - 1, &w) == 0 && w.need == need);
    const size_t n = write_state(buf, need, &w);
    CHECK(n == need);

    afc_state_info info;
    CHECK(afc_state_info_read(buf, n, &info, err, sizeof err) == 0);
    CHECK(strcmp(info.romset, "daytona") == 0 && info.core_version == 0x010203 && info.frame == 12345);

    afc_state_reader r;
    CHECK(afc_state_open(&r, buf, n, "daytona", 0x010203, err, sizeof err) == 0);
    unsigned char ram[300];
    CHECK(afc_state_find(&r, "RAM ") == 300);
    CHECK(afc_state_get(&r, ram, 300) == 0 && ram[299] == (unsigned char)299);
    CHECK(afc_state_get(&r, ram, 1) == -1); /* past the chunk */
    uint32_t a = 0;
    uint64_t b = 0;
    CHECK(afc_state_find(&r, "CPU ") == 12);
    CHECK(afc_state_get_u32(&r, &a) == 0 && a == 0xdeadbeef);
    CHECK(afc_state_get_u64(&r, &b) == 0 && b == 0x0123456789abcdefull);
    CHECK(afc_state_left(&r) == 0);
    CHECK(afc_state_find(&r, "NONE") == 0);
    CHECK(afc_state_find(&r, "GONE") == -1);
    CHECK(afc_state_get(&r, ram, 1) == -1); /* nothing found */

    /* refused: another set, another core version, a damaged byte, cut short */
    CHECK(afc_state_open(&r, buf, n, "srallyc", 0, err, sizeof err) == -1 && strstr(err, "daytona"));
    CHECK(afc_state_open(&r, buf, n, NULL, 0x010204, err, sizeof err) == -1 && strstr(err, "1.2.3"));
    CHECK(afc_state_open(&r, buf, n, NULL, 0, err, sizeof err) == 0);
    buf[40] ^= 1;
    CHECK(afc_state_open(&r, buf, n, NULL, 0, err, sizeof err) == -1 && strstr(err, "CRC"));
    buf[40] ^= 1;
    CHECK(afc_state_info_read(buf, n - 1, NULL, err, sizeof err) == -1);
    CHECK(afc_state_info_read(buf, 3, NULL, err, sizeof err) == -1);
    CHECK(afc_state_info_read("not a state at all, really....", 30, NULL, err, sizeof err) == -1);

    free(buf);
    if (failures) return 1;
    puts("state: ok");
    return 0;
}
