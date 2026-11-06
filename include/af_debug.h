/*
 * The cores' debug and developer switches, read from the environment.
 *
 * Every switch is AF_DEBUG_ followed by the name the core used before, so
 * Lindbergh's LB_PERF is AF_DEBUG_LB_PERF and Chihiro's CH_VERBOSE is
 * AF_DEBUG_CH_VERBOSE. docs/DEBUG-SWITCHES.md lists them all. They are for
 * developers: a player never needs one, and nothing a player sees depends
 * on them.
 *
 * The old name without the prefix is still read when the new one is not
 * set, for one release (1.3.0), so scripts written for it keep working.
 * 1.4.0 drops it.
 *
 * Usage: replace getenv("LB_PERF") with af_debug_env("LB_PERF"). C, C++
 * and Objective-C; header only.
 */
#ifndef AF_DEBUG_H
#define AF_DEBUG_H

#include <stdio.h>
#include <stdlib.h>

static inline const char *af_debug_env(const char *name)
{
    char key[128];
    int n = snprintf(key, sizeof key, "AF_DEBUG_%s", name);
    const char *v = (n > 0 && n < (int)sizeof key) ? getenv(key) : NULL;
    return v ? v : getenv(name);   /* the old name, until 1.4.0 */
}

#endif
