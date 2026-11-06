// mtlgles: a small OpenGL ES 3.0 subset on Metal, the one gl4es needs to draw
// the Lindbergh games (spike, issue #288 item 5c). One context at a time,
// current on one thread at a time. The GL entry points are not exported as
// symbols: callers look them up by name with mtlgles_proc ("glDrawElements").
//
// Coordinates: the vertex stage negates y, so Metal texture row 0 is OpenGL's
// row 0 (the bottom): uploads, sampling, viewports, scissors, glReadPixels and
// gl_FragCoord all keep OpenGL's numbers, and front faces are swapped.
//
// Objective-C: the Metal calls autorelease some objects (command buffers,
// encoders, descriptors). A caller drawing from a thread with no autorelease
// pool should drain one once a frame (objc_autoreleasePoolPush/Pop), or they
// are kept until the thread ends.
//
// BSD-3-Clause. Kept free of the Lindbergh runtime so it can move to the
// shared Common repository.
#pragma once
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mtlgles_context mtlgles_context;

// device: an id<MTLDevice>, or NULL for the system default
mtlgles_context *mtlgles_create(void *device);
void mtlgles_make_current(mtlgles_context *ctx);   // NULL: none on this thread
void mtlgles_destroy(mtlgles_context *ctx);
void *mtlgles_proc(const char *name);              // NULL: not implemented (logged once)

// counters since the last call, as text: draws, pipelines made, passes,
// command buffers, bytes streamed, CPU time in GL calls
void mtlgles_stats(char *buf, size_t size);

// where its messages go (default: stderr)
void mtlgles_set_log(void (*log)(const char *msg));

#ifdef __cplusplus
}
#endif
