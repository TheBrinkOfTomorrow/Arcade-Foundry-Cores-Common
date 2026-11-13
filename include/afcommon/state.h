/*
 * Save states (afcore.h 5.4): the bytes a core's save_state writes and its
 * load_state reads, one layout for every core, so a front end or a tool can
 * read the header without the core.
 *
 * Layout, all numbers little-endian:
 *
 *   "AFST"  u32 format (AFC_STATE_FORMAT)  u32 core_version  u64 frame
 *   u32 romset length, the romset's bytes (no terminator)
 *   chunks: 4-byte tag, u32 length, that many bytes ...
 *   u32 CRC-32 of everything before it
 *
 * A core writes its chunks with a writer and reads them back with a reader;
 * a chunk the reader doesn't ask for is skipped, a chunk it asks for that
 * is missing is an error the core reports. Tags are four characters, by
 * convention upper case and space-padded ("I960", "RAM ", "M3  ").
 *
 *   afc_state_writer w;
 *   afc_state_begin(&w, buf, size, "daytona", core_version, frames);
 *   afc_state_chunk(&w, "RAM ");
 *   afc_state_put(&w, ram, sizeof ram);
 *   size_t n = afc_state_end(&w);              // 0: buf too small (w.need says how big)
 *
 *   afc_state_reader r;
 *   if (afc_state_open(&r, buf, n, "daytona", core_version, err, sizeof err) != 0) return -1;
 *   if (afc_state_find(&r, "RAM ") != sizeof ram || afc_state_get(&r, ram, sizeof ram) != 0) ...
 */
#ifndef AFCOMMON_STATE_H
#define AFCOMMON_STATE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AFC_STATE_FORMAT 1u
#define AFC_STATE_ROMSET_MAX 63

/* The header, read without the core (afc_state_info). */
typedef struct afc_state_info {
    uint32_t format;
    uint32_t core_version;                  /* AFCORE_MAKE_VERSION */
    uint64_t frame;                         /* frames since power-on, as the core counts them */
    char romset[AFC_STATE_ROMSET_MAX + 1];
} afc_state_info;

typedef struct afc_state_writer {
    uint8_t *buf;
    size_t cap;
    size_t len;   /* bytes written so far (counted on past cap, so need is right) */
    size_t chunk; /* offset of the open chunk's length, or 0 */
    size_t need;  /* after afc_state_end: the bytes the state takes */
} afc_state_writer;

typedef struct afc_state_reader {
    const uint8_t *buf;
    size_t end;   /* end of the chunks (before the CRC) */
    size_t first; /* offset of the first chunk */
    size_t pos;   /* read position in the found chunk */
    size_t limit; /* end of the found chunk, or 0 when none is found */
    afc_state_info info;
} afc_state_reader;

/* Writing. Nothing fails until afc_state_end: writes past cap are counted,
 * not stored, so a core can call it with a NULL buffer to measure. */
void afc_state_begin(afc_state_writer *w, void *buf, size_t cap, const char *romset, uint32_t core_version, uint64_t frame);
void afc_state_chunk(afc_state_writer *w, const char tag[4]); /* ends the open chunk, starts this one */
void afc_state_put(afc_state_writer *w, const void *data, size_t size);
void afc_state_put_u32(afc_state_writer *w, uint32_t v);
void afc_state_put_u64(afc_state_writer *w, uint64_t v);
/* Ends the last chunk and adds the CRC. Returns the length, or 0 when it
 * did not fit (w->need is the size it needs). */
size_t afc_state_end(afc_state_writer *w);

/* Reads the header and checks the CRC and the chunks' lengths: 0, or -1
 * with the reason in err. */
int afc_state_info_read(const void *data, size_t size, afc_state_info *info, char *err, size_t err_size);

/* Reading. open checks as afc_state_info_read, then that the state is this
 * romset's and this core_version's (romset NULL or core_version 0: not
 * checked). */
int afc_state_open(afc_state_reader *r, const void *data, size_t size, const char *romset, uint32_t core_version,
                   char *err, size_t err_size);
/* Finds the first chunk with this tag: its length, or -1 when there is none. */
long long afc_state_find(afc_state_reader *r, const char tag[4]);
/* Reads from the found chunk: 0, or -1 past its end (nothing is read then). */
int afc_state_get(afc_state_reader *r, void *out, size_t size);
int afc_state_get_u32(afc_state_reader *r, uint32_t *v);
int afc_state_get_u64(afc_state_reader *r, uint64_t *v);
/* The found chunk's bytes not yet read. */
size_t afc_state_left(const afc_state_reader *r);

#ifdef __cplusplus
}
#endif

#endif
