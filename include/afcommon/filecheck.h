/*
 * Checks a game folder against a list of its files (path, size, CRC-32),
 * for the cores whose games are folders rather than MAME zips (Lindbergh,
 * Chihiro): afcore's check_roms. The lists are made by
 * tools/filelist/make_filelist.py from known-good copies of the games.
 * Header-only C (and C++), no allocation; reads each file in full.
 */
#ifndef AFCOMMON_FILECHECK_H
#define AFCOMMON_FILECHECK_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef struct afc_listed_file {
    const char *path; /* relative to the game folder, '/' separated */
    uint64_t size;
    uint32_t crc;     /* CRC-32 (zlib's) */
} afc_listed_file;

static inline void afc_filecheck_report(char *report, size_t size, const char *msg, const char *path)
{
    if (report && size) snprintf(report, size, "%s%s%s", path ? path : "", path ? ": " : "", msg);
}

/* 1 when every listed file is under root with its size and CRC, else 0 with
 * the first problem in report ("disk0/elf/hod4M.elf: wrong CRC"). Files not
 * on the list are ignored. */
static inline int afc_check_files(const char *root, const afc_listed_file *files, size_t count,
                                  char *report, size_t report_size)
{
    uint32_t table[256];
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++) c = c & 1 ? 0xedb88320u ^ (c >> 1) : c >> 1;
        table[i] = c;
    }
    unsigned char buf[1 << 16];
    char path[4096];
    for (size_t i = 0; i < count; i++) {
        const afc_listed_file *f = &files[i];
        if ((size_t)snprintf(path, sizeof path, "%s/%s", root, f->path) >= sizeof path) {
            afc_filecheck_report(report, report_size, "path too long", f->path);
            return 0;
        }
        FILE *fp = fopen(path, "rb");
        if (!fp) {
            afc_filecheck_report(report, report_size, "missing", f->path);
            return 0;
        }
        uint64_t got = 0;
        uint32_t crc = 0xffffffffu;
        size_t n;
        while ((n = fread(buf, 1, sizeof buf, fp)) > 0) {
            got += n;
            if (got > f->size) break;
            for (size_t j = 0; j < n; j++) crc = table[(crc ^ buf[j]) & 0xff] ^ (crc >> 8);
        }
        const int failed = ferror(fp);
        fclose(fp);
        if (failed) {
            afc_filecheck_report(report, report_size, "cannot be read", f->path);
            return 0;
        }
        if (got != f->size) {
            afc_filecheck_report(report, report_size, "wrong size", f->path);
            return 0;
        }
        if ((crc ^ 0xffffffffu) != f->crc) {
            afc_filecheck_report(report, report_size, "wrong CRC", f->path);
            return 0;
        }
    }
    afc_filecheck_report(report, report_size, "", NULL);
    return 1;
}

#endif
