# x86recomp: the static x86 recompiler

`tools/x86recomp/`. This document describes how the recompiler turns a game's
x86 program into portable C, what the generated code expects from a core's
runtime, and why it is built this way.

## Purpose and scope

Several arcade boards ran an ordinary PC processor: Sega Lindbergh (32-bit
Linux on a Pentium 4), Sega Chihiro (an Xbox, 32-bit) and the newer
Windows-based boards (64-bit). Their games are x86 machine code. To run
them on an Apple Silicon Mac, an iPad, an Apple TV, a Raspberry Pi or an x86
Linux PC without an x86 emulator, the recompiler translates the program
ahead of time, at build time, into C. The core's build then compiles that C
for the target processor.

In scope:

- 32-bit Linux ELF programs and the shared libraries they ship (Lindbergh)
- 32-bit Xbox executables, XBE (Chihiro)
- 32-bit Windows programs and DLLs, PE32 (the WinPC games: Sega Amusements
  PC, RingWide, Europa-R; in development)
- 64-bit Windows programs and DLLs, PE32+ (in development)
- integer, x87, MMX and SSE through SSE4.2 instructions
- `setjmp`/`longjmp`, and Windows structured exception handling in the
  64-bit front end

Out of scope:

- running x86 code that was not seen at build time (no JIT; a game that
  generates code at run time would need one)
- self-modifying code
- the operating system: system calls, the C library, the graphics and sound
  APIs. These are the core's runtime (HLE: high-level emulation), not the
  recompiler's job.

## Requirements

1. One recompiler serves every x86 core. Each core keeps its own runtime and
   calls the shared tool.
2. The output is plain C that any C compiler builds (clang on the Apple
   platforms, GCC or clang on Linux). No inline assembly, no x86 intrinsics.
3. The game runs at full speed on every target device, the iPad included.
4. The translation is exact where games can tell: integer flags, x87
   rounding to integers, SSE comparisons with NaNs, the direction flag,
   unaligned memory.
5. Behaviour added for one kind of program never changes another kind's
   output. An ELF translates the same whether or not XBE or PE support
   exists (switched on by the image kind).
6. No game code or data goes into any repository. The recompiler runs on the
   user's own copy of the game during the build; its output is build output.
7. Anything it cannot translate becomes a clear run-time stop that names the
   address and the instruction, not a silent wrong result.

## Design

### Where it sits

```
game program (ELF, XBE, PE32 or PE32+) + the core's per-game lists
  -> recomp.py / recomp64.py (Python, capstone + pyelftools)
  -> gen.h, gen_000.c ... gen_NNN.c, gen_tables.c
  -> compiled with the core's runtime (cpu.h, hle.h and its HLE code)
  -> the core library (afcore_<set>)
```

`recomp.py` is the 32-bit front end (ELF and XBE); `xbe.py` reads XBEs and
finds their functions. `recomp64.py` is the 64-bit front end with `pe.py`.
`sse.py`/`sse64.py` and `x87_64.py` translate the SIMD and x87 instructions.

### Functions and control flow

Every guest function becomes one C function, `void <prog>_F_<addr>(CPU *c)`,
where `<prog>` is the program's name (so a game and its test program can be
linked into one library). Inside a function:

- **Registers are C locals.** The function loads `eax`...`edi` from the CPU
  struct on entry and writes them back before every call and return. The C
  compiler keeps them in host registers.
- **Flags are C locals too** (`CF`, `ZF`, `SF`, `OF`, `PF`). Each instruction
  writes the flags it defines, and the compiler deletes every flag computation
  that nothing reads, which is nearly all of them.
- **Branches inside the function are `goto`s** to labels at their targets.
- **A call** pushes the real return address onto the guest stack, then calls
  the target's C function directly. The guest's `ret` adjusts the stack and
  returns from the C function. Guest and host call stacks stay in step.
- **An indirect call or jump** (a function pointer, a vtable) goes through
  `guest_dispatch(c, addr)`. The runtime looks the address up in the sorted
  table of every function entry in `gen_tables.c`.
- **A switch** (`jmp [table + reg*4]`) becomes a C `switch` over the labels
  found in its jump table, with `guest_dispatch` as the default.
- **A second entry point** (a jump or call into the middle of another
  function, common in compiler-optimised code) makes that function a static
  body with an entry argument. Each entry gets a small named wrapper.
- **Code that runs past a function's end** continues in the next function by
  a tail call. Some compilers split one function into two this way.

The guest stack is real guest memory. Code that reads its own return
address, walks the stack or uses `call`/`pop` to find its own address works
unchanged.

### Finding the functions

Translation is static, so the recompiler must find every function before
the game runs:

- **ELF:** the program's symbol table names every function. A shared library
  names only its exports. Its other functions are found from its
  initialiser, the code pointers its relocations fix up, the static
  functions its position-independent code takes the address of
  (`lea reg, [ebx + offset]` with `ebx` holding the GOT) and the targets of
  its calls and jumps, repeated until nothing new turns up.
- **Stripped ELF** (no `.symtab`, 2 Spicy's Lindbergh programs): the
  program's functions are found like a library's, from the entry point,
  `.init`/`.fini`, the constructor and destructor tables, every `.eh_frame`
  FDE (parsed by recomp.py itself, since pyelftools rejects these), data
  words that point at a `push ebp; mov ebp, esp` prologue, vtables (an
  offset-to-top word, a typeinfo word or 0 for a build without RTTI, then
  code pointers), and immediates in `push`/`mov` that point into code, then
  by following calls. A data word or vtable entry is not a start when it
  lands inside a function an FDE describes, or in the middle of an
  instruction decoding from the known start before it (Harley-Davidson's
  rodata has 0, 0, then a word pointing into a constructor, which split it
  mid-instruction). 2 Spicy's game program gives 22,354 functions (23,008
  before that check, the same instructions), its test program 1,590;
  Harley-Davidson's 14,502.
- **XBE:** a retail or arcade XBE has no symbols. `xbe.py`'s discovery walks
  the code by recursive descent from the entry point, following calls, and
  adds the code addresses found in data (vtables, callback tables) and in
  instruction operands (function pointers passed as arguments). Guessed
  starts that decode across a function already reached by calls are
  rejected, and discovery runs again without them, so a constant that only
  looks like a code address cannot split a real function.
- **PE32+:** the unwind table (`.pdata`) lists every non-leaf function.
  Leaf functions are found from call and jump targets, code pointers in
  relocated data, `lea` targets, exports and TLS callbacks. A program with
  no relocation directory (a fixed-base build, or a decrypted memory dump
  such as Scarlet Dawn's `Hodzero-Win64-Shipping.exe`) has no fixups to
  find those data pointers, so every aligned 8-byte value in its mapped
  data sections that points into code is taken instead.

Jump tables are found from the instruction pattern that reads them: an
absolute table, a GOT-relative table in position-independent code, MSVC's
image-relative tables, or GCC's table-relative ones.

### Memory

32-bit programs: guest memory is one 4 GB window starting at the host
pointer `M`. A guest address is an offset into it, so every load is
`rd32(addr)`, which is `memcpy(&v, M + addr, 4)`. The compiler turns that
into one load; `memcpy` keeps unaligned accesses defined. The core reserves
the window at start-up. On iOS that needs the extended virtual addressing
entitlement.

64-bit programs: guest memory is host memory. A guest address is a host
pointer, and the runtime gives the game its own allocations. The image is
relocatable: code addresses are `IB_<prog> + rva`, and the runtime's loader
applies the program's relocations after mapping it.

### PE32 programs

`pe32.py` reads a 32-bit Windows exe or DLL (machine 0x14c, magic 0x10b) with
the same interface as the ELF and XBE readers. A PE32 is an MSVC program
without symbols, like an XBE, so it takes the same discovery
(`xbe.Discovery`) and MSVC rules (`msvc_image`), seeded with what a PE adds:
exports, the SafeSEH handler table and TLS callbacks. `fs` addresses the
thread's TEB (`c->fs_base`), as on the Xbox it addressed the KPCR. Imports are
not stubs: the runtime's loader reads the import directory, fills each IAT
slot with a callable (magic) address, and the translated `call [iat]` goes
through `guest_dispatch`; a `stdcall` import pops its own arguments. The
image runs at its preferred base, which is always free in the 4 GB window.
Two MSVC habits are handled for every PE32: `rep ret` (a `rep` prefix on a
non-string instruction is ignored, as the CPU does) and reading a segment
register, which gives the Win32 flat selectors (cs 0x23; ds, es, ss, gs
0x2b; fs 0x53).

Discovery for a PE32 also takes MinGW's habits: functions padded with
multi-byte nops (`lea esi,[esi+eiz*1+0]` and the like) count as plausible
starts after the padding, and `__CTOR_LIST__`/`__DTOR_LIST__` (-1, aligned
function addresses, 0, kept in `.text`) seed their constructors. Discardable
sections (`.reloc`, DWARF debug sections) are not scanned for code pointers.

Visual C++ packs functions without padding and names many only from data, so
a PE32's discovery leans on data more (found on Daytona, 10 Nov 2026):

- **Pointer tables.** A run of three or more aligned words in data that all
  point into code (zeros may sit between), such as the static initializer
  list `__xc_a..__xc_z` and vtables, gives loose starts; an entry that is
  16-byte aligned right after `int3`/`nop` padding or a return (`anchored`)
  is certain, like a call target, from the first pass. (A pair is left out:
  an SEH scope table's filter and handler point into functions.)
- **Constants that land in code.** Data is full of words such as 0x01010101
  that point into the middle of long functions. A pointer into reached code
  makes a second entry only on an instruction boundary after a return, jump,
  `int3`, `nop` or `hlt`; a call target inside an instruction is ignored; and
  a walk stops where it would decode from the middle of an instruction
  already reached (a guessed start that does so is rejected).
- **Settling.** Guessed starts whose code overlaps a function reached by calls
  are rejected and discovery starts over, up to 64 passes (each rejects at
  least one more); Daytona settles in 5. `RECOMP_VERBOSE=1` prints each pass.
- **Relocations** (a DLL, or any PE32 built with them; found on
  d3dcompiler_46.dll, 11 Nov 2026, an OS-style build with its vtables and
  `__xi_` list in `.text`): a relocated word that points where a function
  starts (after padding, a return or a jump: `plausible_start`) is a start,
  certain if it is 16-byte aligned or a hot-patch prologue (`mov edi,edi;
  push ebp; mov ebp,esp`, taken even where `sane()` would stop at the
  function's own jump table); runs of three or more relocated words in code
  give loose starts. `RECOMP_WHY=0xADDR` prints why `sane()` rejects a start.
- **Byte index tables** of a switch end at the switch's bound (`cmp r,N`
  before the `movzx`: N + 1 bytes), not at the first byte >= 0x80, which had
  swallowed the first byte of the function after one.
- **The C runtime's x87 dispatch descriptors** (`#pragma pack(1)`: a type
  byte, the name NUL-padded to 12 bytes, the argument count, a flag word,
  then four handler pointers at +0x10, at any alignment) seed their handlers
  (`exp`, `fmod`, `atan2` ...). Only that exact layout counts: taking every
  unaligned run of code pointers found 8,000 false starts in Daytona.

### PE32 structured exceptions

A 32-bit Windows program keeps its exception frames on its own stack, linked
from fs:[0], and its compiler's handler (`_except_handler3/4`, translated with
it) decides. The runtime (`lib/win32/seh.c`) only walks the list and calls the
handlers. The hard part is the handler's last step: it unwinds and then jumps
into an `__except` block with the establishing function's esp and ebp. In C,
that function is a host frame further up the host stack, below the
dispatcher's and handler's own frames. The jump has to land there:

- **Registered functions.** A function that writes `fs:[0]` (unless it is a
  helper such as `__SEH_prolog4`, which starts by pushing an address in code),
  one that calls such a helper, and one with an address that only data names
  (an `__except` block or a filter in a scope table, made an entry) gets a
  wrapper: it pushes a `resume_frame` (a host `jmp_buf` and the guest esp on
  entry) on the CPU's list, `_setjmp`s, and runs the body; on return it pops.
- **The jump.** An indirect `jmp` whose target is not a case of the function
  goes to `guest_jump(c, t, esp0)`, `esp0` being esp at the function's entry.
  If esp is above `esp0` the code is leaving its own frame: a return made with
  `jmp` when `t` is the return address at `esp0`; a tail call when the return
  address at `esp0` is now on top of the stack and `t` is a function or a
  stand-in (MSVC's `_CallMemberFunction` thunks: pop eax; pop ecx;
  xchg [esp],eax; jmp eax); otherwise a resume.
  `guest_resume` finds the innermost registered frame whose esp is at or above
  the guest's and `longjmp`s to it with the target; the wrapper then enters the
  body at that address (`guest_resumed`).
- **The except block's guard.** An entry whose instruction is
  `mov esp, [ebp-...]` (the block restoring its frame's esp) checks that it
  was entered by a resume (`c->resume_entered`); entered any other way, it
  resumes first. This catches a handler that reached it by a direct call.

`div` and `idiv` check for a zero divisor and call `guest_fault`, which
raises 0xc0000094 through the same dispatcher. None of this changes ELF or XBE
output: it is switched on by the image kind.

SIMD instructions are written out per instruction in `sse.py` (no
library). `tests/x86recomp_pe32/simd.c` checks them: the same source runs
natively on the x86 host and translated, and every result must match.
Libraries such as SIMDe or sse2neon could later supply the rare hard ones
(SSE4.2 string compares) or NEON speed on arm64.
An ELF translates exactly as before (checked: the smoke ELF's generated C is
byte-identical with and without the PE32 front end).

### Imports and HLE

The program's imports (PLT entries in an ELF, the thunk table in an XBE,
the import address table in a PE) become `hle_call(c, index)`. The
generated tables list each import's name, and the runtime binds the name to
its own implementation: the C library, pthreads, OpenGL, the board's
libraries. A function pointer to an import is a "magic" address that
`guest_dispatch` recognises and sends to the same host function.

A game function the runtime replaces is listed in the core's per-game
`overrides.txt`. Its translated body becomes a call to the runtime's
`ovr_<name>`. This is how a core replaces, for example, a security check or
a hardware poll, by name and without touching the program. A program with no
symbols names them by address: a line `0xaddress name` (Harley-Davidson's AM
library, found by its code in a program that has the same build with
symbols). `patches.txt`
holds byte patches applied before translation, by symbol plus offset or by
address.

### Floating point

- **x87** registers are `double`s in the CPU struct, with a top-of-stack
  index, control word and status word. Arithmetic is done in double
  precision, not 80-bit extended. Every game so far is fine with that. The
  conversions to integer use the control word's rounding mode and return
  the integer indefinite value for out-of-range inputs, as the hardware
  does. They are written so that no C undefined behaviour is involved: a
  compiler had folded an earlier version's range check away.
- **SSE** registers are 16-byte unions (`xmm_t`). Each instruction is
  translated lane by lane in plain C, which the compiler vectorises where it
  can. Comparisons keep x86's NaN results, and conversions keep x86's
  out-of-range results.
- The generated C is built with `-ffp-contract=off` (no fused multiply-add
  that the x86 code didn't have), `-fwrapv` (signed overflow wraps, as on
  x86) and `-fno-strict-aliasing`.

### Threads

Guest threads are host threads, each with its own `CPU` struct and guest
stack. `lock xadd` and `cmpxchg` with memory are host atomic operations for
every image kind. In a PE32 every locked read-modify-write of memory (`add`,
`sub`, `adc`, `sbb`, `and`, `or`, `xor`, `inc`, `dec`, `neg`, `not`) is a
compare-exchange loop and `xchg` with memory an atomic exchange; MinGW's
`InterlockedIncrement` is `lock add`, and four threads lost about 300 of
40,000 increments without it. The ELF and XBE output keep the old code for
those (Lindbergh's games and the Xbox ran fine; changing it would change
their output).

In an XBE, a loop's backward branch is `GUEST_LOOP()`. By default it is a
compiler barrier, so a loop polling a memory location the runtime writes
(a hardware register in the original) reads it again. A runtime can define
it to give up the processor there. Chihiro does this to run one guest thread
at a time, as the Xbox's single processor did.

### setjmp, longjmp and exceptions

A call to `setjmp` is translated in line: the host `jmp_buf` must live in
the frame of the function that called it. The guest `jmp_buf` gets the
registers it would hold, plus a pointer to the host one, where the runtime's
`longjmp` finds it. The 64-bit front end also gives each function with a C
scope table (`__try`) a handler frame. The runtime's exception dispatcher
can re-enter that frame at an `__except` block.

### Output size

The C is split into a fixed number of `gen_NNN.c` files (`--files`), so the
build can list them before translation runs and compile them in parallel.
The core's build compiles them at `-O1`, which measured as fast as `-O2`
and builds in two thirds of the time.

## Data and settings

The recompiler keeps no state. A core gives it, per game:

| File | What |
|---|---|
| `overrides.txt` | Function names the runtime implements as `ovr_<name>` |
| `patches.txt` | `symbol+offset hexbytes` or `0xaddress hexbytes` lines |
| `libs.txt` | Shared libraries translated with the program, with their load base |
| `--starts` | Extra function start addresses discovery missed |
| `--name` | The program's name, used for its C names and tables |

These hold names, addresses and our own byte values only, never game code.

Its outputs, all build output and never committed:

| File | What |
|---|---|
| `gen.h` | Declarations of every function and import stub |
| `gen_NNN.c` | The functions |
| `gen_tables.c` | `gen_program_<name>`: function addresses and pointers (sorted, for dispatch), import names and slots, copy relocations, data symbols the runtime looks up, libraries, exports and relocations to apply |

The generated code is written against the runtime's `cpu.h`: the `CPU`
struct, `M`, `rd8`...`rd64`/`wr8`...`wr64`, the x87 and SSE helpers,
`guest_dispatch`, `guest_unimpl`, `hle_call`. `tests/x86recomp64/cpu64.h`
is the same contract for 64-bit programs.

## Platforms

The recompiler is Python and runs wherever the core is built: macOS or
Linux, at build time, with `capstone` and `pyelftools`. Its output is the
same C for every target. The cores build it for:

- macOS (arm64) and Linux (x86-64; arm64 builds the same C)
- iPadOS (arm64; the 4 GB window needs the extended virtual addressing
  entitlement)
- tvOS: not yet. tvOS lacks the extended addressing entitlement, so the
  32-bit cores would need a smaller guest window.

## Testing

- `tests/x86recomp_smoke.py`: builds a small C program with `gcc -m32 -O2`,
  translates it, compiles the C with a minimal runtime, runs it and compares
  its output with independently computed values. Runs in CI.
- `tests/x86recomp_pe32/smoke_pe32.py`: the same for a MinGW-built 32-bit
  Windows program without a C runtime (`smoke.c`), run against the minimal
  loader in `rt32.c` (sections at the preferred base, IAT bound to magic
  addresses, `stdcall` imports pop their arguments). Runs in CI.
- `tests/win32/run_samples.py`: three 32-bit Windows programs (Visual
  C++-style `__try` frames built with clang; a MinGW C runtime program; MinGW
  threads) translated and run against `lib/win32`, output compared. By hand
  for now (GitHub-hosted CI is off for the private repos).
- `tests/x86recomp64/smoke64.py`: the same for a MinGW-built Win64 program.
  Runs in CI. `big.cpp` (C++ with the static standard library) and `msvc.c`
  (built the MSVC way) are larger samples to run by hand.
- The recompiler prints every instruction kind it could not translate, with
  a count. A game's translation is expected to list none in code that runs.
- Each core checks every game it ships through its test mode and a full
  game to game over. A recompiler change is checked against each core's
  games before its pin moves.

## Open items

1. 64-bit front end: C++ exceptions (`__CxxFrameHandler`) and the guest's own
   calls to `RtlUnwind*` are not translated yet.
2. 32-bit `pushal`, segment registers other than the Xbox kernel's `fs`, and
   a few rare instructions stop with `guest_unimpl`. The translation report
   lists them per game.
3. x87 is double precision, not 80-bit. A game that depends on extended
   precision would need a software 80-bit type.
4. Translation and compile time grow with the program. A large 64-bit game
   is minutes to translate and needs the C split across many files to
   compile in parallel.
5. A stripped ELF has no names: a core replaces its functions by address
   (`0xaddress name` in overrides.txt) and has to find those addresses
   itself.
6. Text relocations in non-PIC shared libraries (needed by a paused
   Lindbergh game) are in review.

## Decisions

- **Static translation to C, not a JIT or an interpreter.** iOS and tvOS
  do not allow a JIT for apps, and the C compiler's optimiser does the
  register allocation and flag elimination that a JIT would have to do
  itself.
- **One C function per guest function, with real guest stack frames.**
  Simple, debuggable, and it keeps code that inspects the stack working.
- **The host C compiler removes dead flags.** The recompiler does no flag
  liveness analysis of its own.
- **Guest memory is a fixed 4 GB window for 32-bit programs.** Every guest
  address is valid without a check, and one add turns it into a host
  pointer.
- **Image kind switches behaviour.** XBE-only and PE-only rules never change
  ELF output, so a Lindbergh build is unaffected by work for other boards.
- **The recompiler lives in Cores-Common.** Changes land here as their own
  pull request, then each core's pin moves. Cores never carry a private
  copy.
