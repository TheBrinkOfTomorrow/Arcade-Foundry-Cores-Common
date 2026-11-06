/*
 * The zip reader of afcommon/zip.h for C: a core written in C reading one
 * file from a ROM set (Chihiro's EEPROM dumps from chihiro.zip).
 *
 *   char err[256];
 *   afc_zip *z = afc_zip_open("/path/ROMs/chihiro.zip", err, sizeof err);
 *   long long n = z ? afc_zip_read(z, "ic10_g24lc64.bin", buf, sizeof buf, err, sizeof err) : -1;
 *   afc_zip_close(z);
 */
#ifndef AFCOMMON_ZIP_C_H
#define AFCOMMON_ZIP_C_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct afc_zip afc_zip;

/* NULL with the reason in err when the file is missing or not a zip. */
afc_zip *afc_zip_open(const char *path, char *err, size_t err_size);
/* Reads a member, named without its folder, in any case, into out: returns
 * the member's whole size (only out_size bytes of a bigger one are copied),
 * or -1 with the reason in err (missing, damaged, CRC wrong). */
long long afc_zip_read(const afc_zip *z, const char *name, void *out, size_t out_size, char *err, size_t err_size);
void afc_zip_close(afc_zip *z); /* NULL is fine */

#ifdef __cplusplus
}
#endif

#endif
