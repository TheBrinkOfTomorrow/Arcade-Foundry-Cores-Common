/* Save states' bytes: see include/afcommon/state.h. */
#include "afcommon/state.h"

#include <stdio.h>
#include <string.h>

static const uint8_t kMagic[4] = {'A', 'F', 'S', 'T'};
/* magic, format, core_version, frame, romset length */
#define HEADER_FIXED (4 + 4 + 4 + 8 + 4)

static uint32_t crc32_update(uint32_t crc, const uint8_t *p, size_t n) {
    static uint32_t table[256];
    static int ready;
    if (!ready) {
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t c = i;
            for (int k = 0; k < 8; ++k) c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            table[i] = c;
        }
        ready = 1;
    }
    crc = ~crc;
    while (n--) crc = table[(crc ^ *p++) & 0xff] ^ (crc >> 8);
    return ~crc;
}

static uint32_t rd32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }
static uint64_t rd64(const uint8_t *p) { return (uint64_t)rd32(p) | (uint64_t)rd32(p + 4) << 32; }
static void wr32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

static void fail(char *err, size_t err_size, const char *why) {
    if (err && err_size) snprintf(err, err_size, "%s", why);
}

/* ---- writing ---- */

void afc_state_put(afc_state_writer *w, const void *data, size_t size) {
    if (w->buf && w->len <= w->cap && size <= w->cap - w->len) memcpy(w->buf + w->len, data, size);
    w->len += size;
}

void afc_state_put_u32(afc_state_writer *w, uint32_t v) {
    uint8_t b[4];
    wr32(b, v);
    afc_state_put(w, b, 4);
}

void afc_state_put_u64(afc_state_writer *w, uint64_t v) {
    afc_state_put_u32(w, (uint32_t)v);
    afc_state_put_u32(w, (uint32_t)(v >> 32));
}

static int fits(const afc_state_writer *w) { return w->buf && w->len <= w->cap; }

static void close_chunk(afc_state_writer *w) {
    if (!w->chunk) return;
    if (fits(w)) wr32(w->buf + w->chunk, (uint32_t)(w->len - w->chunk - 4));
    w->chunk = 0;
}

void afc_state_begin(afc_state_writer *w, void *buf, size_t cap, const char *romset, uint32_t core_version, uint64_t frame) {
    memset(w, 0, sizeof *w);
    w->buf = (uint8_t *)buf;
    w->cap = cap;
    size_t n = romset ? strlen(romset) : 0;
    if (n > AFC_STATE_ROMSET_MAX) n = AFC_STATE_ROMSET_MAX;
    afc_state_put(w, kMagic, 4);
    afc_state_put_u32(w, AFC_STATE_FORMAT);
    afc_state_put_u32(w, core_version);
    afc_state_put_u64(w, frame);
    afc_state_put_u32(w, (uint32_t)n);
    afc_state_put(w, romset, n);
}

void afc_state_chunk(afc_state_writer *w, const char tag[4]) {
    close_chunk(w);
    afc_state_put(w, tag, 4);
    w->chunk = w->len;
    afc_state_put_u32(w, 0); /* the length, filled in when the chunk ends */
}

size_t afc_state_end(afc_state_writer *w) {
    close_chunk(w);
    w->need = w->len + 4;
    if (!w->buf || w->need > w->cap) return 0;
    afc_state_put_u32(w, crc32_update(0, w->buf, w->len));
    return w->len;
}

/* ---- reading ---- */

int afc_state_info_read(const void *data, size_t size, afc_state_info *info, char *err, size_t err_size) {
    const uint8_t *p = (const uint8_t *)data;
    if (!p || size < HEADER_FIXED + 4 || memcmp(p, kMagic, 4) != 0) {
        fail(err, err_size, "not a save state");
        return -1;
    }
    const size_t end = size - 4;
    if (crc32_update(0, p, end) != rd32(p + end)) {
        fail(err, err_size, "the save state is damaged (CRC)");
        return -1;
    }
    afc_state_info in;
    memset(&in, 0, sizeof in);
    in.format = rd32(p + 4);
    if (in.format != AFC_STATE_FORMAT) {
        fail(err, err_size, "save state format not known to this core");
        return -1;
    }
    in.core_version = rd32(p + 8);
    in.frame = rd64(p + 12);
    const uint32_t n = rd32(p + 20);
    if (n > AFC_STATE_ROMSET_MAX || HEADER_FIXED + (size_t)n > end) {
        fail(err, err_size, "the save state is damaged (header)");
        return -1;
    }
    memcpy(in.romset, p + HEADER_FIXED, n);
    /* every chunk's length within the bytes */
    for (size_t at = HEADER_FIXED + n; at < end;) {
        if (end - at < 8 || rd32(p + at + 4) > end - at - 8) {
            fail(err, err_size, "the save state is damaged (chunks)");
            return -1;
        }
        at += 8 + rd32(p + at + 4);
    }
    if (info) *info = in;
    return 0;
}

int afc_state_open(afc_state_reader *r, const void *data, size_t size, const char *romset, uint32_t core_version,
                   char *err, size_t err_size) {
    memset(r, 0, sizeof *r);
    if (afc_state_info_read(data, size, &r->info, err, err_size) != 0) return -1;
    if (romset && strcmp(romset, r->info.romset) != 0) {
        if (err && err_size) snprintf(err, err_size, "a save state of %s, not %s", r->info.romset, romset);
        return -1;
    }
    if (core_version && core_version != r->info.core_version) {
        if (err && err_size)
            snprintf(err, err_size, "a save state of core %u.%u.%u, not %u.%u.%u", (unsigned)(r->info.core_version >> 16),
                     (unsigned)(r->info.core_version >> 8 & 0xff), (unsigned)(r->info.core_version & 0xff),
                     (unsigned)(core_version >> 16), (unsigned)(core_version >> 8 & 0xff), (unsigned)(core_version & 0xff));
        return -1;
    }
    r->buf = (const uint8_t *)data;
    r->end = size - 4;
    r->first = HEADER_FIXED + strlen(r->info.romset);
    return 0;
}

long long afc_state_find(afc_state_reader *r, const char tag[4]) {
    r->pos = r->limit = 0;
    for (size_t at = r->first; at < r->end;) {
        const uint32_t n = rd32(r->buf + at + 4);
        if (memcmp(r->buf + at, tag, 4) == 0) {
            r->pos = at + 8;
            r->limit = r->pos + n;
            return n;
        }
        at += 8 + n;
    }
    return -1;
}

int afc_state_get(afc_state_reader *r, void *out, size_t size) {
    if (!r->limit || size > r->limit - r->pos) return -1;
    memcpy(out, r->buf + r->pos, size);
    r->pos += size;
    return 0;
}

int afc_state_get_u32(afc_state_reader *r, uint32_t *v) {
    uint8_t b[4];
    if (afc_state_get(r, b, 4) != 0) return -1;
    *v = rd32(b);
    return 0;
}

int afc_state_get_u64(afc_state_reader *r, uint64_t *v) {
    uint8_t b[8];
    if (afc_state_get(r, b, 8) != 0) return -1;
    *v = rd64(b);
    return 0;
}

size_t afc_state_left(const afc_state_reader *r) { return r->limit ? r->limit - r->pos : 0; }
