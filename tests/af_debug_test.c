/* af_debug_env: the AF_DEBUG_ name wins, the old name is the fallback. */
#include "af_debug.h"
#include <string.h>

int main(void)
{
    if (af_debug_env("AFC_TEST_SWITCH")) return 1;
    setenv("AFC_TEST_SWITCH", "old", 1);
    if (strcmp(af_debug_env("AFC_TEST_SWITCH"), "old") != 0) return 2;
    setenv("AF_DEBUG_AFC_TEST_SWITCH", "new", 1);
    if (strcmp(af_debug_env("AFC_TEST_SWITCH"), "new") != 0) return 3;
    puts("af_debug ok");
    return 0;
}
