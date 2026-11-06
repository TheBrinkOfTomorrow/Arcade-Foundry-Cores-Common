/*
 * The tar unpacker of afcommon/tar.h for C and Swift: fed as the bytes
 * arrive, into <dst>.unpacking, put in place of dst by afc_tar_finish.
 *
 *   afc_tar *t = afc_tar_begin("/path/ROMs/hotd4a", names, 2, "disk0");
 *   while (more) if (afc_tar_feed(t, buf, n) != 0) break;   // afc_tar_error(t)
 *   afc_tar_finish(t);                                       // 0 when the game is in place
 *   afc_tar_free(t);                                         // removes the staging folder unless finished
 */
#ifndef AFCOMMON_TAR_C_H
#define AFCOMMON_TAR_C_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct afc_tar afc_tar;

/* top_names: folder names (any case) dropped when they are the tar's top
 * folder; required: an entry finish needs at the top, or NULL. Never NULL;
 * when the staging folder cannot be made, every later call fails with the
 * reason in afc_tar_error. */
afc_tar *afc_tar_begin(const char *dst, const char *const *top_names, size_t top_count, const char *required);
int afc_tar_feed(afc_tar *t, const void *data, size_t size); /* 0, or -1: see afc_tar_error */
int afc_tar_finish(afc_tar *t);                               /* 0, or -1: see afc_tar_error */
const char *afc_tar_error(const afc_tar *t);
void afc_tar_free(afc_tar *t);

#ifdef __cplusplus
}
#endif

#endif
