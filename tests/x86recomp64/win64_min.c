// The few kernel32 and msvcrt entry points a small compiler-built Win64
// program needs: enough to start up, print and exit. A core's Windows layer
// is a separate, far larger thing; this is the test harness's.
#define _GNU_SOURCE
#include "hle64.h"
#include <errno.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include <sys/mman.h>

// ---- kernel32 ---------------------------------------------------------------------
#define HANDLE_FD(fd) ((uint64_t)0x100 + (uint64_t)(fd))

static void k_GetStdHandle(CPU *c) {
    int32_t which = (int32_t)ARG(0);
    RET(which == -10 ? HANDLE_FD(0) : which == -11 ? HANDLE_FD(1) : which == -12 ? HANDLE_FD(2) : (uint64_t)-1);
}
static void k_WriteFile(CPU *c) {
    uint64_t h = ARG(0); const void *buf = ARGP(1); uint32_t n = (uint32_t)ARG(2); uint32_t *written = ARGP(3);
    if (h < 0x100 || h > 0x100 + 1024) { teb_set_last_error(c, 6); RET(0); }   // ERROR_INVALID_HANDLE
    ssize_t w = write((int)(h - 0x100), buf, n);
    if (w < 0) { teb_set_last_error(c, 29); RET(0); }                            // ERROR_WRITE_FAULT
    if (written) *written = (uint32_t)w;
    RET(1);
}
static void k_GetTickCount(CPU *c) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    RET((uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000));
}
static void k_GetLastError(CPU *c) { RET(teb_get_last_error(c)); }
static void k_SetLastError(CPU *c) { teb_set_last_error(c, (uint32_t)ARG(0)); RET(0); }
static void k_Sleep(CPU *c) { usleep((useconds_t)ARG(0) * 1000); RET(0); }
static void k_ret0(CPU *c) { RET(0); }
static void k_ret1(CPU *c) { RET(1); }
// critical sections: one thread in the harness, so entering never waits
static void k_InitializeCriticalSection(CPU *c) { memset(ARGP(0), 0, 40); RET(0); }
// TLS: slots live in the TEB at 0x1480
static uint32_t tls_next;
static void k_TlsAlloc(CPU *c) { RET(tls_next < 64 ? tls_next++ : 0xffffffffu); }
static void k_TlsGetValue(CPU *c) { uint32_t i = (uint32_t)ARG(0); teb_set_last_error(c, 0); RET(i < 64 ? rd64(c->gs_base + 0x1480 + 8 * i) : 0); }
static void k_TlsSetValue(CPU *c) { uint32_t i = (uint32_t)ARG(0); if (i >= 64) RET(0); wr64(c->gs_base + 0x1480 + 8 * i, ARG(1)); RET(1); }
static void k_VirtualProtect(CPU *c) { uint32_t *old = ARGP(3); if (old) *old = 0x40; RET(1); }   // PAGE_EXECUTE_READWRITE before
static void k_VirtualQuery(CPU *c) {
    // MEMORY_BASIC_INFORMATION: one committed read-write page of the image
    uint64_t a = ARG(0); uint8_t *mbi = ARGP(1); uint64_t n = ARG(2);
    if (n < 48) RET(0);
    const gen_program64 *g = hle_main_program;
    uint64_t ib = *g->image_base_var;
    int in_image = a >= ib && a < ib + g->size_of_image;
    uint64_t v;
    v = a & ~0xfffull; memcpy(mbi + 0, &v, 8);                 // BaseAddress
    v = in_image ? ib : (a & ~0xfffull); memcpy(mbi + 8, &v, 8); // AllocationBase
    uint32_t u;
    u = 0x80; memcpy(mbi + 16, &u, 4);                           // AllocationProtect: PAGE_EXECUTE_WRITECOPY
    v = in_image ? ib + g->size_of_image - (a & ~0xfffull) : 0x1000; memcpy(mbi + 24, &v, 8);  // RegionSize
    u = 0x1000; memcpy(mbi + 32, &u, 4);                         // State: MEM_COMMIT
    u = 0x04; memcpy(mbi + 36, &u, 4);                           // Protect: PAGE_READWRITE
    u = in_image ? 0x1000000 : 0x20000; memcpy(mbi + 40, &u, 4); // Type: MEM_IMAGE / MEM_PRIVATE
    RET(48);
}
static void k_MultiByteToWideChar(CPU *c) {
    const uint8_t *s = ARGP(2); int n = (int)(int32_t)ARG(3); uint16_t *w = ARGP(4); int wn = (int)(int32_t)ARG(5);
    if (!s) RET(0);
    if (n < 0) n = (int)strlen((const char *)s) + 1;
    if (wn == 0) RET(n);
    if (n > wn) { teb_set_last_error(c, 122); RET(0); }          // ERROR_INSUFFICIENT_BUFFER
    for (int i = 0; i < n; i++) w[i] = s[i];
    RET(n);
}
static void k_WideCharToMultiByte(CPU *c) {
    const uint16_t *w = ARGP(2); int n = (int)(int32_t)ARG(3); uint8_t *s = ARGP(4); int sn = (int)(int32_t)ARG(5);
    if (!w) RET(0);
    if (n < 0) { n = 0; while (w[n]) n++; n++; }
    if (sn == 0) RET(n);
    if (n > sn) { teb_set_last_error(c, 122); RET(0); }
    for (int i = 0; i < n; i++) s[i] = w[i] < 256 ? (uint8_t)w[i] : '?';
    RET(n);
}

static void k_ExitProcess(CPU *c) { fflush(stdout); exit((int)(uint32_t)ARG(0)); }
static void k_GetSystemTimeAsFileTime(CPU *c) {
    struct timespec ts; clock_gettime(CLOCK_REALTIME, &ts);
    uint64_t ft = (uint64_t)ts.tv_sec * 10000000ull + ts.tv_nsec / 100 + 116444736000000000ull;
    wr64(ARG(0), ft); RET(0);
}
static void k_GetModuleHandleW(CPU *c) { RET(ARG(0) ? 0 : *hle_main_program->image_base_var); }
static void k_GetProcAddress(CPU *c) {
    // only names this harness provides; a NULL means "not there", as on a Windows without it
    const char *name = ARGS(1);
    if ((uint64_t)(uintptr_t)name < 0x10000) RET(0);
    uint64_t a = hle_import_address("kernel32", name);
    if (!a && hle_verbose) fprintf(stderr, "GetProcAddress(%s): not provided\n", name);
    RET(a);
}
static void k_LoadLibraryW(CPU *c) { if (hle_verbose) fprintf(stderr, "LoadLibraryW: not provided\n"); teb_set_last_error(c, 126); RET(0); }
static void k_GetThreadId(CPU *c) { RET(rd32(c->gs_base + 0x48)); }
static void k_GetCurrentThreadId(CPU *c) { RET(rd32(c->gs_base + 0x48)); }
static void k_GetCurrentProcessId(CPU *c) { RET(0x1234); }
static void k_RaiseException(CPU *c) { fprintf(stderr, "guest: RaiseException(0x%08x)\n", (uint32_t)ARG(0)); exit(73); }
static void k_RtlStop(CPU *c) { guest_unimpl(c, rd64(c->r[RSP]), "structured exception handling (RtlUnwindEx/RtlVirtualUnwind) is not in the test runtime"); }
static void k_FormatMessageA(CPU *c) {
    uint32_t flags = (uint32_t)ARG(0), id = (uint32_t)ARG(2); char *buf = ARGP(4); uint32_t n = (uint32_t)ARG(5);
    char tmp[128]; int len = snprintf(tmp, sizeof tmp, "error %u", id);
    if (flags & 0x100) { char *m = malloc(len + 1); memcpy(m, tmp, len + 1); *(uint64_t *)buf = (uint64_t)(uintptr_t)m; RET(len); }
    if (!buf || n == 0) RET(0);
    if ((uint32_t)len >= n) len = n - 1;
    memcpy(buf, tmp, len); buf[len] = 0; RET(len);
}
static void k_LocalFree(CPU *c) { free(ARGP(0)); RET(0); }
static void k_SleepConditionVariableCS(CPU *c) { RET(1); }   // one thread: nobody to wait for

static const hle_entry kernel32_tab[] = {
    {"kernel32", "ExitProcess", k_ExitProcess},
    {"kernel32", "GetSystemTimeAsFileTime", k_GetSystemTimeAsFileTime},
    {"kernel32", "GetModuleHandleW", k_GetModuleHandleW},
    {"kernel32", "GetModuleHandleA", k_GetModuleHandleW},
    {"kernel32", "GetProcAddress", k_GetProcAddress},
    {"kernel32", "LoadLibraryW", k_LoadLibraryW},
    {"kernel32", "LoadLibraryA", k_LoadLibraryW},
    {"kernel32", "GetThreadId", k_GetThreadId},
    {"kernel32", "GetCurrentThreadId", k_GetCurrentThreadId},
    {"kernel32", "GetCurrentProcessId", k_GetCurrentProcessId},
    {"kernel32", "RaiseException", k_RaiseException},
    {"kernel32", "RtlCaptureContext", k_RtlStop},
    {"kernel32", "RtlLookupFunctionEntry", k_RtlStop},
    {"kernel32", "RtlUnwindEx", k_RtlStop},
    {"kernel32", "RtlVirtualUnwind", k_RtlStop},
    {"kernel32", "FormatMessageA", k_FormatMessageA},
    {"kernel32", "LocalFree", k_LocalFree},
    {"kernel32", "CloseHandle", k_ret1},
    {"kernel32", "TryEnterCriticalSection", k_ret1},
    {"kernel32", "InitializeConditionVariable", k_ret0},
    {"kernel32", "WakeConditionVariable", k_ret0},
    {"kernel32", "WakeAllConditionVariable", k_ret0},
    {"kernel32", "SleepConditionVariableCS", k_SleepConditionVariableCS},
    {"kernel32", "GetStdHandle", k_GetStdHandle},
    {"kernel32", "WriteFile", k_WriteFile},
    {"kernel32", "GetTickCount", k_GetTickCount},
    {"kernel32", "GetLastError", k_GetLastError},
    {"kernel32", "SetLastError", k_SetLastError},
    {"kernel32", "Sleep", k_Sleep},
    {"kernel32", "InitializeCriticalSection", k_InitializeCriticalSection},
    {"kernel32", "InitializeCriticalSectionAndSpinCount", k_InitializeCriticalSection},
    {"kernel32", "EnterCriticalSection", k_ret0},
    {"kernel32", "LeaveCriticalSection", k_ret0},
    {"kernel32", "DeleteCriticalSection", k_ret0},
    {"kernel32", "IsDBCSLeadByteEx", k_ret0},
    {"kernel32", "SetUnhandledExceptionFilter", k_ret0},
    {"kernel32", "TlsAlloc", k_TlsAlloc},
    {"kernel32", "TlsGetValue", k_TlsGetValue},
    {"kernel32", "TlsSetValue", k_TlsSetValue},
    {"kernel32", "TlsFree", k_ret1},
    {"kernel32", "VirtualProtect", k_VirtualProtect},
    {"kernel32", "VirtualQuery", k_VirtualQuery},
    {"kernel32", "MultiByteToWideChar", k_MultiByteToWideChar},
    {"kernel32", "WideCharToMultiByte", k_WideCharToMultiByte},
    {NULL, NULL, NULL}
};
void win64_register_kernel32(void) { hle_register(kernel32_tab); }

// ---- msvcrt -----------------------------------------------------------------------
// FILE as msvcrt lays it out on x64 (48 bytes): _ptr, _cnt, _base, _flag, _file, _charbuf, _bufsiz, _tmpfname
#define IOB_SIZE 48
static uint8_t iob[3 * IOB_SIZE];
static int iob_fd(uint64_t fp) {
    uint64_t b = (uint64_t)(uintptr_t)iob;
    if (fp >= b && fp < b + sizeof iob) return (int)((fp - b) / IOB_SIZE);
    return -1;
}
static int32_t m_errno_val, m_fmode, m_commode;
static uint64_t m_initenv;
static char decimal_point[] = ".", empty[] = "";
static struct { char *decimal_point, *thousands_sep, *grouping, *int_curr_symbol, *currency_symbol, *mon_decimal_point, *mon_thousands_sep, *mon_grouping, *positive_sign, *negative_sign; } lconv_min;

static void m_iob_func(CPU *c) {
    for (int i = 0; i < 3; i++) { int32_t fd = i; memcpy(iob + i * IOB_SIZE + 28, &fd, 4); }
    RETP(iob);
}
static void m_errno(CPU *c) { RETP(&m_errno_val); }
static void m_getmainargs(CPU *c) {
    // (int *argc, char ***argv, char ***envp, int dowildcard, _startupinfo *)
    extern int hle_argc; extern char **hle_argv;
    int32_t *argc = ARGP(0); uint64_t *argv = ARGP(1); uint64_t *envp = ARGP(2);
    uint64_t *v = calloc(hle_argc + 1, 8);
    for (int i = 0; i < hle_argc; i++) v[i] = (uint64_t)(uintptr_t)hle_argv[i];
    static uint64_t env0;
    if (argc) *argc = hle_argc;
    if (argv) *argv = (uint64_t)(uintptr_t)v;
    if (envp) *envp = (uint64_t)(uintptr_t)&env0;
    m_initenv = (uint64_t)(uintptr_t)&env0;
    RET(0);
}
static void m_initterm(CPU *c) {
    uint64_t p = ARG(0), end = ARG(1);
    for (; p < end; p += 8) { uint64_t f = rd64(p); if (f) guest_call(c, f, 0, NULL); }
    RET(0);
}
static void m_initterm_e(CPU *c) {
    uint64_t p = ARG(0), end = ARG(1);
    for (; p < end; p += 8) { uint64_t f = rd64(p); if (f) { uint64_t r = guest_call(c, f, 0, NULL); if ((int32_t)r) RET(r); } }
    RET(0);
}
static void m_onexit(CPU *c) { RET(ARG(0)); }
static void m_ret0(CPU *c) { RET(0); }
static void m_ret1(CPU *c) { RET(1); }
static void m_exit(CPU *c) { fflush(stdout); exit((int)(int32_t)ARG(0)); }
static void m_abort(CPU *c) { (void)c; fflush(stdout); fprintf(stderr, "guest: abort()\n"); exit(134); }
static void m_amsg_exit(CPU *c) { fprintf(stderr, "guest: runtime error %d\n", (int)ARG(0)); exit(255); }
static void m_localeconv(CPU *c) {
    lconv_min.decimal_point = decimal_point;
    lconv_min.thousands_sep = lconv_min.grouping = lconv_min.int_curr_symbol = lconv_min.currency_symbol = empty;
    lconv_min.mon_decimal_point = lconv_min.mon_thousands_sep = lconv_min.mon_grouping = lconv_min.positive_sign = lconv_min.negative_sign = empty;
    RETP(&lconv_min);
}
static void m_malloc(CPU *c) { RETP(malloc(ARG(0) ? ARG(0) : 1)); }
static void m_calloc(CPU *c) { RETP(calloc(ARG(0) ? ARG(0) : 1, ARG(1) ? ARG(1) : 1)); }
static void m_realloc(CPU *c) { RETP(realloc(ARGP(0), ARG(1))); }
static void m_free(CPU *c) { free(ARGP(0)); RET(0); }
static void m_memcpy(CPU *c) { memcpy(ARGP(0), ARGP(1), ARG(2)); RET(ARG(0)); }
static void m_memmove(CPU *c) { memmove(ARGP(0), ARGP(1), ARG(2)); RET(ARG(0)); }
static void m_memset(CPU *c) { memset(ARGP(0), (int)ARG(1), ARG(2)); RET(ARG(0)); }
static void m_memcmp(CPU *c) { RET((uint32_t)memcmp(ARGP(0), ARGP(1), ARG(2))); }
static void m_strlen(CPU *c) { RET(strlen(ARGS(0))); }
static void m_strcmp(CPU *c) { RET((uint32_t)strcmp(ARGS(0), ARGS(1))); }
static void m_strncmp(CPU *c) { RET((uint32_t)strncmp(ARGS(0), ARGS(1), ARG(2))); }
static void m_strerror(CPU *c) { RETP(strerror((int)ARG(0))); }
static void m_wcslen(CPU *c) { const uint16_t *w = ARGP(0); uint64_t n = 0; while (w[n]) n++; RET(n); }
static int file_write(uint64_t fp, const void *buf, size_t n) {
    int fd = iob_fd(fp);
    if (fd < 0) return -1;
    return (int)write(fd, buf, n);
}
static void m_fwrite(CPU *c) {
    size_t sz = ARG(1), n = ARG(2);
    int w = file_write(ARG(3), ARGP(0), sz * n);
    RET(w < 0 ? 0 : (sz ? (uint64_t)w / sz : 0));
}
static void m_fputc(CPU *c) { uint8_t ch = (uint8_t)ARG(0); RET(file_write(ARG(1), &ch, 1) == 1 ? ch : (uint32_t)-1); }
static void m_fprintf(CPU *c) {
    char buf[4096];
    int n = guest_vformat(c, buf, sizeof buf, ARGS(1), 2);
    if (n >= (int)sizeof buf) n = sizeof buf - 1;
    RET((uint32_t)file_write(ARG(0), buf, n));
}
static void m_vfprintf(CPU *c) {
    char buf[4096];
    int n = guest_vformat_va(c, buf, sizeof buf, ARGS(1), ARG(2));
    if (n >= (int)sizeof buf) n = sizeof buf - 1;
    RET((uint32_t)file_write(ARG(0), buf, n));
}
static void m_printf(CPU *c) {
    char buf[4096];
    int n = guest_vformat(c, buf, sizeof buf, ARGS(0), 1);
    if (n >= (int)sizeof buf) n = sizeof buf - 1;
    RET((uint32_t)write(1, buf, n));
}
static void m_puts(CPU *c) { const char *s = ARGS(0); write(1, s, strlen(s)); write(1, "\n", 1); RET(0); }
static void m_C_specific_handler(CPU *c) { guest_unimpl(c, rd64(c->r[RSP]), "__C_specific_handler: an exception was raised"); }

static void m_getenv(CPU *c) { (void)ARGS(0); RET(0); }   // an empty environment
static void m_setlocale(CPU *c) { static char cl[] = "C"; RETP(cl); }
static void m_memchr(CPU *c) { RETP(memchr(ARGP(0), (int)ARG(1), ARG(2))); }
static void m_strchr(CPU *c) { RETP(strchr(ARGS(0), (int)ARG(1))); }
static void m_strrchr(CPU *c) { RETP(strrchr(ARGS(0), (int)ARG(1))); }
static void m_strstr(CPU *c) { RETP(strstr(ARGS(0), ARGS(1))); }
static void m_strcoll(CPU *c) { RET((uint32_t)strcmp(ARGS(0), ARGS(1))); }
static void m_strxfrm(CPU *c) {
    char *d = ARGP(0); const char *s = ARGS(1); size_t n = ARG(2), l = strlen(s);
    if (d && n) { size_t k = l < n - 1 ? l : n - 1; memcpy(d, s, k); d[k] = 0; }
    RET(l);
}
static void m_strncpy(CPU *c) { strncpy(ARGP(0), ARGS(1), ARG(2)); RET(ARG(0)); }
static void m_strcpy(CPU *c) { strcpy(ARGP(0), ARGS(1)); RET(ARG(0)); }
static void m_strtoul(CPU *c) {
    char *end; unsigned long v = strtoul(ARGS(0), &end, (int)ARG(2));
    if (ARG(1)) wr64(ARG(1), (uint64_t)(uintptr_t)end);
    RET((uint32_t)v);
}
static void m_strtol(CPU *c) {
    char *end; long v = strtol(ARGS(0), &end, (int)ARG(2));
    if (ARG(1)) wr64(ARG(1), (uint64_t)(uintptr_t)end);
    RET((uint32_t)(int32_t)v);
}
static void m_strtod(CPU *c) {
    char *end; double v = strtod(ARGS(0), &end);
    if (ARG(1)) wr64(ARG(1), (uint64_t)(uintptr_t)end);
    RETD(v);
}
static void m_atoi(CPU *c) { RET((uint32_t)atoi(ARGS(0))); }
static void m_fputs(CPU *c) { const char *s = ARGS(0); RET(file_write(ARG(1), s, strlen(s)) < 0 ? (uint32_t)-1 : 0); }
static void m_towlower(CPU *c) { uint32_t w = (uint32_t)ARG(0); RET(w < 128 && w >= 'A' && w <= 'Z' ? w + 32 : w); }
static void m_towupper(CPU *c) { uint32_t w = (uint32_t)ARG(0); RET(w < 128 && w >= 'a' && w <= 'z' ? w - 32 : w); }
static void m_iswctype(CPU *c) {
    // msvcrt ctype bits: 1 upper, 2 lower, 4 digit, 8 space, 0x10 punct, 0x20 control, 0x40 blank, 0x80 hex, 0x100 alpha
    uint32_t w = (uint32_t)ARG(0), m = (uint32_t)ARG(1), b = 0;
    if (w >= 128) RET(0);
    if (w >= 'A' && w <= 'Z') b |= 0x101; if (w >= 'a' && w <= 'z') b |= 0x102; if (w >= '0' && w <= '9') b |= 4;
    if (w == ' ' || (w >= 9 && w <= 13)) b |= 8; if (w == ' ' || w == 9) b |= 0x40; if (w < 32 || w == 127) b |= 0x20;
    if ((w >= '!' && w <= '/') || (w >= ':' && w <= '@') || (w >= '[' && w <= '`') || (w >= '{' && w <= '~')) b |= 0x10;
    if ((w >= '0' && w <= '9') || (w >= 'a' && w <= 'f') || (w >= 'A' && w <= 'F')) b |= 0x80;
    RET(b & m ? 1 : 0);
}
static void m_strftime(CPU *c) { char *d = ARGP(0); if (ARG(1)) d[0] = 0; RET(0); }
static void m_wcscoll(CPU *c) {
    const uint16_t *a = ARGP(0), *b = ARGP(1);
    while (*a && *a == *b) a++, b++;
    RET((uint32_t)((int)*a - (int)*b));
}
static void m_wcsxfrm(CPU *c) {
    uint16_t *d = ARGP(0); const uint16_t *s = ARGP(1); size_t n = ARG(2), l = 0;
    while (s[l]) l++;
    if (d && n) { size_t k = l < n - 1 ? l : n - 1; memcpy(d, s, 2 * k); d[k] = 0; }
    RET(l);
}
static void m_read(CPU *c) { RET((uint32_t)read((int)ARG(0), ARGP(1), (uint32_t)ARG(2))); }
static void m_write(CPU *c) { RET((uint32_t)write((int)ARG(0), ARGP(1), (uint32_t)ARG(2))); }
static void m_isatty(CPU *c) { RET((uint32_t)isatty((int)ARG(0))); }
static void m_fflush(CPU *c) { RET(0); }

static const hle_entry msvcrt_tab[] = {
    {"msvcrt", "getenv", m_getenv},
    {"msvcrt", "setlocale", m_setlocale},
    {"msvcrt", "memchr", m_memchr},
    {"msvcrt", "strchr", m_strchr},
    {"msvcrt", "strrchr", m_strrchr},
    {"msvcrt", "strstr", m_strstr},
    {"msvcrt", "strcoll", m_strcoll},
    {"msvcrt", "strxfrm", m_strxfrm},
    {"msvcrt", "strncpy", m_strncpy},
    {"msvcrt", "strcpy", m_strcpy},
    {"msvcrt", "strtoul", m_strtoul},
    {"msvcrt", "strtol", m_strtol},
    {"msvcrt", "strtod", m_strtod},
    {"msvcrt", "atoi", m_atoi},
    {"msvcrt", "fputs", m_fputs},
    {"msvcrt", "towlower", m_towlower},
    {"msvcrt", "towupper", m_towupper},
    {"msvcrt", "iswctype", m_iswctype},
    {"msvcrt", "strftime", m_strftime},
    {"msvcrt", "wcsftime", m_strftime},
    {"msvcrt", "wcscoll", m_wcscoll},
    {"msvcrt", "wcsxfrm", m_wcsxfrm},
    {"msvcrt", "_read", m_read},
    {"msvcrt", "_write", m_write},
    {"msvcrt", "_isatty", m_isatty},
    {"msvcrt", "fflush", m_fflush},
    {"msvcrt", "__iob_func", m_iob_func},
    {"msvcrt", "_errno", m_errno},
    {"msvcrt", "__getmainargs", m_getmainargs},
    {"msvcrt", "_initterm", m_initterm},
    {"msvcrt", "_initterm_e", m_initterm_e},
    {"msvcrt", "_onexit", m_onexit},
    {"msvcrt", "__set_app_type", m_ret0},
    {"msvcrt", "__setusermatherr", m_ret0},
    {"msvcrt", "_lock", m_ret0},
    {"msvcrt", "_unlock", m_ret0},
    {"msvcrt", "_cexit", m_ret0},
    {"msvcrt", "signal", m_ret0},
    {"msvcrt", "___lc_codepage_func", m_ret0},
    {"msvcrt", "___mb_cur_max_func", m_ret1},
    {"msvcrt", "exit", m_exit},
    {"msvcrt", "_exit", m_exit},
    {"msvcrt", "abort", m_abort},
    {"msvcrt", "_amsg_exit", m_amsg_exit},
    {"msvcrt", "localeconv", m_localeconv},
    {"msvcrt", "malloc", m_malloc},
    {"msvcrt", "calloc", m_calloc},
    {"msvcrt", "realloc", m_realloc},
    {"msvcrt", "free", m_free},
    {"msvcrt", "memcpy", m_memcpy},
    {"msvcrt", "memmove", m_memmove},
    {"msvcrt", "memset", m_memset},
    {"msvcrt", "memcmp", m_memcmp},
    {"msvcrt", "strlen", m_strlen},
    {"msvcrt", "strcmp", m_strcmp},
    {"msvcrt", "strncmp", m_strncmp},
    {"msvcrt", "strerror", m_strerror},
    {"msvcrt", "wcslen", m_wcslen},
    {"msvcrt", "fwrite", m_fwrite},
    {"msvcrt", "fputc", m_fputc},
    {"msvcrt", "fprintf", m_fprintf},
    {"msvcrt", "vfprintf", m_vfprintf},
    {"msvcrt", "printf", m_printf},
    {"msvcrt", "puts", m_puts},
    {"msvcrt", "__C_specific_handler", m_C_specific_handler},
    {NULL, NULL, NULL}
};
static const hle_object_entry msvcrt_objects[] = {
    {"msvcrt", "__initenv", &m_initenv},
    {"msvcrt", "_fmode", &m_fmode},
    {"msvcrt", "_commode", &m_commode},
    {NULL, NULL, NULL}
};
void win64_register_msvcrt(void) { hle_register(msvcrt_tab); hle_register_objects(msvcrt_objects); }
