// Runs one translated program: main64 <pe-file> [args...]
// Built with -DPROG=gen_program_<name> for the module recomp64 produced.
#include "hle64.h"
#include <stdlib.h>

extern const gen_program64 PROG;
int hle_argc;
char **hle_argv;

int main(int argc, char **argv)
{
    if (argc < 2) { fprintf(stderr, "usage: %s <pe-file> [args]\n", argv[0]); return 2; }
    hle_verbose = getenv("HLE_VERBOSE") != NULL;
    hle_trace = getenv("HLE_TRACE") != NULL;
    hle_argc = argc - 1;
    hle_argv = argv + 1;
    win64_register_kernel32();
    win64_register_msvcrt();
    if (load_program(&PROG, argv[1])) return 1;
    return run_program(&PROG, hle_argc, hle_argv);
}
