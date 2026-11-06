# Debug switches

Environment variables the cores read, for developers checking or debugging a
core. A player never needs one: nothing in the app's settings depends on
them, and an unset switch is always the normal behaviour.

Every switch is `AF_DEBUG_` followed by the name the core used before
(`AF_DEBUG_LB_PERF`, `AF_DEBUG_CH_VERBOSE`). Code reads them with
`af_debug_env("LB_PERF")` from `include/af_debug.h`, which also accepts the
old name without the prefix. The old names keep working in 1.3.0 and go in
1.4.0, so scripts should move to the `AF_DEBUG_` names now.

A new switch is read the same way: `af_debug_env("<BOARD>_<NAME>")`, and a
line here in the same pull request. The developer tools (`core_test`,
`m2native`, `m3run`, the Classic 2D trace tools, Supermodel's reference
runner) are not shipped and keep their own variables.

Values: "1" turns a switch on (any value does, unless the table says
otherwise); "file" and "dir" are paths.

## Lindbergh (Arcade-Foundry-Cores-Lindbergh)

| Switch | Effect |
|---|---|
| `AF_DEBUG_LB_VERBOSE=1` | Log file and device access. |
| `AF_DEBUG_LB_PERF=1` | Per-frame timings. |
| `AF_DEBUG_LB_SHOT_EVERY=N` | Save a PPM of every Nth frame. |
| `AF_DEBUG_LB_SHOT_FBO=N` | Screenshots read the game's framebuffer object N instead of the screen. |
| `AF_DEBUG_LB_GL_TRACE=N` or `N-M` | Log every GL and library call during frame N (or frames N to M); frame 0 is everything before the first swap. |
| `AF_DEBUG_LB_GL_CHECK=1` | Check `glGetError` after every GL call and log the calls that raise errors. |
| `AF_DEBUG_LB_SHADER_DUMP=1` | Write each shader after the GLSL fixes, and each ARB program, to files. |
| `AF_DEBUG_LB_NO_SURFACES=1` | Copy each frame back through the CPU instead of handing the GPU surface to the front end. |
| `AF_DEBUG_LB_SYNC_READ=1` | iOS: read frames back synchronously instead of through the asynchronous ring. |
| `AF_DEBUG_LB_READ_BGRA=1` | iOS: read frames back as BGRA, to time that path. |
| `AF_DEBUG_LB_INPUT_SCRIPT=file` | Play scripted input (the command list is in the Lindbergh README). |
| `AF_DEBUG_LB_AUDIO_WAV=file` | Also write the mixed sound to a WAV file. |
| `AF_DEBUG_LB_VOICELOG=1` | Log the sound voices playing, once a second. |
| `AF_DEBUG_LB_FRAME_CLOCK=1` | Mix 1/60 s of sound per video frame instead of using the audio device (repeatable captures). |
| `AF_DEBUG_LB_SLEEPLOG=1` | Log the game's sleeps of 0.1 s or more, with the caller. |
| `AF_DEBUG_LB_DIPSW=0x60` | The DIP switch byte (default 0x60: 1280x768, horizontal). |
| `AF_DEBUG_LB_SAVE_DIR=dir` | Where `eeprom.bin` and `sram.bin` live. |

## Chihiro (Arcade-Foundry-Cores-Chihiro)

| Switch | Effect |
|---|---|
| `AF_DEBUG_CH_VERBOSE=1` (2, 3) | Log more, the higher the number. |
| `AF_DEBUG_CH_TRACE=1` | The stand-alone runner: log every kernel call with its caller and arguments. |
| `AF_DEBUG_CH_PERF=1` | Timings once a second. |
| `AF_DEBUG_CH_BIOS=path` | chihiro.zip, or a folder of its files, when it is not beside the game. |
| `AF_DEBUG_CH_SHOT=dir` | Save a PPM of every Nth frame into dir. |
| `AF_DEBUG_CH_SHOT_EVERY=N` | N for `AF_DEBUG_CH_SHOT` (default 60). |
| `AF_DEBUG_CH_NOGPU=1` | Run without drawing. |
| `AF_DEBUG_CH_NOSOUND=1` | Run without sound (HotD3 then spins in DirectSound). |
| `AF_DEBUG_CH_WAV=file` | Also write the sound to a WAV file. |
| `AF_DEBUG_CH_PBTRACE=file` | Log every GPU method. |
| `AF_DEBUG_CH_PBDUMP=file` | Write the first GPU push buffer to a file. |
| `AF_DEBUG_CH_INPUT=300:coin1,330:start1,...` | Scripted input, frame:action. |
| `AF_DEBUG_CH_INPUT_NEXT=...` | The stand-alone runner: the script for the run after the game restarts itself. |
| `AF_DEBUG_CH_TESTMODE=1` | Start in the game's test menu. |
| `AF_DEBUG_CH_ALLCORES=1` | Turn the one-processor mode off. |
| `AF_DEBUG_CH_STACKSCAN=hex` | With `AF_DEBUG_CH_VERBOSE` above 3: a crude backtrace of a waiting thread from that address. |

## Model 1 and Model 2 (Arcade-Foundry-Cores-Model12)

| Switch | Effect |
|---|---|
| `AF_DEBUG_M2_STRICT=1` | Stop at code that was not recompiled instead of falling back to the interpreter (lockstep runs, seed hunting). Model 1 too. |
| `AF_DEBUG_M2_RASTER_THREADS=N` | How many threads draw the 3D (1 to 8). |
| `AF_DEBUG_M2RUN_VERBOSE=1` | Log the geometry FIFO status reads. |
| `AF_DEBUG_M2REPLAY_DUMP_DIR=dir` | Replay checks: write our copy of a memory region that differs from MAME's dump. |
| `AF_DEBUG_M2NATIVE_MISMATCH_LOG=file` | Lockstep checks: log each TGP value that differs from MAME's. |

## Model 3 (Arcade-Foundry-Cores-Model3)

| Switch | Effect |
|---|---|
| `AF_DEBUG_PPC_SKIP=N` | The PowerPC wait-loop skip mask (1 RAM polls, 2 decrementer, 4 countdown, 8 timebase; default 15, 0 off). |
| `AF_DEBUG_ATTRACT_RTC_START=seconds` | Start the board's clock at this Unix time (the attract mode follows the clock; test runs fix it). |
| `AF_DEBUG_ATTRACT_M3_LOG=1` | Print Supermodel's log to stderr. |
| `AF_DEBUG_ATTRACT_M3_QUADS=0` or `1` | Draw quads (1) or triangles (0); Apple's OpenGL builds default to triangles. |
| `AF_DEBUG_ATTRACT_M3_TIMING=1` | Apple: per-frame timing of the Metal path. |
| `AF_DEBUG_ATTRACT_M3_SYNC=1` | Apple: synchronous Metal frames. |

## Classic 2D (Arcade-Foundry-Cores-Classic2D)

| Switch | Effect |
|---|---|
| `AF_DEBUG_C2D_INTERP=all` | Run every 68000 instruction in the interpreter instead of the recompiled code. |
| `AF_DEBUG_M1TRACE_V25_PCLOG=file` | Log the V25 sound CPU's program counter. |

## mtlgles (this repo, lib/mtlgles)

| Switch | Effect |
|---|---|
| `AF_DEBUG_MTLGLES_DUMP=1` | Log the source of shaders that fail and the MSL of every program. |
| `AF_DEBUG_MTLGLES_SYNC_COMPILE=1` | Compile Metal libraries on the calling thread (the default is in the background). |
| `AF_DEBUG_MTLGLES_COLD_COMPILE=1` | Make each run's Metal sources new to Metal's shader cache, to time a first boot. |
| `AF_DEBUG_MTLGLES_TRACE_DEPTH=1` | Log depth copies, depth texture formats and depth clamps. |
| `AF_DEBUG_MTLGLES_TRACE_LINK=1` | Log fragment varyings the vertex shader doesn't write. |
| `AF_DEBUG_MTLGLES_NO_BC=1` | Offer no S3TC, so gl4es decodes DXT to RGBA. |
| `AF_DEBUG_MTLGLES_BOOLEAN_QUERIES=1` | Refuse `GL_SAMPLES_PASSED` as OpenGL ES does, to try the boolean-query path on the Mac. |
| `AF_DEBUG_MTLGLES_GPU_CAPTURE=seconds[,flushes]` | Write a GPU trace document of that many flushes (default 4), starting that many seconds in. |
| `AF_DEBUG_MTLGLES_GPU_CAPTURE_FILE=file` | Where the capture goes (default `mtlgles.gputrace`). |
