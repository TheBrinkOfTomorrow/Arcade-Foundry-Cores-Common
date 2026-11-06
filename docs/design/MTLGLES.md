# mtlgles: OpenGL ES on Metal

The renderer the Lindbergh cores use on Apple platforms (app issue #288,
item 5c). This document gives the design as it stands and why it was
chosen. Debugging notes, the full bug history and the debug switches are in
the developer notes, which don't ship with releases.

## Purpose and scope

The Lindbergh games draw with desktop OpenGL 2.1 plus NVIDIA's vertex and
fragment program extensions. On Linux, Mesa runs that directly. On Apple,
desktop OpenGL is deprecated on the Mac and absent on iOS and tvOS, and
OpenGL ES is deprecated on iOS and tvOS. Metal is the only API Apple keeps
developing, and the only one on Apple TV that will last.

mtlgles is a small OpenGL ES 3.0 subset written directly on Metal. It is the
subset gl4es needs: gl4es (MIT, used unchanged apart from our fixes) turns
the games' desktop GL into OpenGL ES, and mtlgles runs that OpenGL ES on
Metal.

In scope:

- everything gl4es calls while drawing the four Lindbergh games in the app:
  The House of the Dead 4, OutRun 2 SP SDX, Let's Go Jungle! and Sega Race TV
- macOS and iOS/iPadOS now, tvOS later (Lindbergh on Apple TV is planned for
  1.4.0, on the Apple TV 4K third generation and later)

Out of scope:

- being a general OpenGL ES implementation. Entry points nothing calls
  aren't written, and a missing one is logged once by name.
- Chihiro, whose renderer is its own and gets a native Metal backend
- the Direct3D boards (RingEdge, RingWide, Europa-R, Nu, ALLS)

## Requirements

1. The four games look as they do on native OpenGL on the same Mac:
   geometry, lighting, shadows, reflections, post effects, glare.
2. Full speed (60 fps) in play on an M1 iPad and on Apple-silicon Macs.
3. No change to the games or the cores' runtime above gl4es. The same
   runtime and gl4es fixes serve Mesa, Apple's OpenGL ES and mtlgles.
4. Opt-in until it has passed on the Mac and the iPad, then the default,
   with the old path kept as a fallback for one release.
5. No code or data derived from the games is shipped (rules.md #7). That
   rules out shipping precompiled game shaders.

## Design

### Where it sits

```
game (desktop GL 2.1, NV/ARB vertex and fragment programs, GLSL 1.x)
  → Lindbergh runtime: NV programs to ARB, GLSL fixes, depth+stencil
    bookkeeping, occlusion queries (hle_gfx.c, nv_vertex_program.c, glsl_pp.c)
  → gl4es: desktop GL to OpenGL ES 3 (pinned commit, our fixes applied at
    configure time by cmake/gl4es_fixes.cmake)
  → mtlgles: OpenGL ES 3 to Metal (this library)
  → Metal
```

The core looks up every GL entry point by name through `mtlgles_proc`, the
same way it looks them up in Apple's OpenGL ES framework, so switching
between the two is a build option in the core (`LB_MTLGLES`, on by default
for every Apple core) and nothing else. The library is built only when a core asks for it (`AFC_MTLGLES`,
target `afc::mtlgles`).

### Why our own layer

Two other ways were tried on The House of the Dead 4 on an M4 Pro Mac, with
the game paced to 60 fps:

| Path | Average fps |
|---|---|
| Apple's desktop OpenGL (deprecated) | 57.2 |
| gl4es on ANGLE's Metal backend | 44.7 |
| gl4es on mtlgles | 59.9 |

ANGLE lost most of its time in `glCopyTexSubImage2D` (11-12 ms a frame),
which the games use for their post effects, and it is a large dependency
to carry for one board. mtlgles is about 5,300 lines and only has to
support what the games use, so a copy can become a GPU blit and the
pipeline cache can be keyed on exactly the state that matters.

### The library

| File | What it holds |
|---|---|
| `mtlgles.m` | context, state, glGet, frame flush, sync objects, GPU capture |
| `mtlgles_objects.m` | buffers, textures (formats, uploads, copies), framebuffers, blits |
| `mtlgles_draw.m` | vertex attributes, render passes, pipeline and depth-stencil states, draws, clears, occlusion queries |
| `mtlgles_program.m` | shader and program objects, uniforms, Metal library compiles |
| `glsl_msl.c` | GLSL ES to Metal Shading Language translation and linking |

The public header has five calls: create, make current, destroy, look up an
entry point, and a stats line. One context is current on one thread at a
time.

The caller drains an autorelease pool once a frame on the drawing thread.
Metal's command buffers, encoders and descriptors are autoreleased, and the
Lindbergh games draw from plain pthreads that have no pool of their own.
Without one, the M1 iPad grew about 5 MB a frame until iOS ended the game
at 5 GB. mtlgles' own deferred releases don't use autoreleased objects, so
they're freed when the GPU finishes, whatever the caller does.

### Coordinates

The vertex stage negates y and maps GL's clip z to Metal's
(`z' = (z + w) / 2`). With y negated, Metal's texture row 0 is GL's row 0,
so uploads, sampling, viewports, scissors, `glReadPixels` and
`gl_FragCoord` all keep GL's numbers, and only the front-face winding is
swapped. `gl_PointCoord` is flipped to match GL's upper-left sprite origin.

### Shaders

`glsl_msl.c` translates each GLSL ES shader to MSL when a program links:

- A shader's globals and functions become members of a struct, so the MSL
  can pass uniforms and varyings around without globals.
- Uniforms live in one struct in a buffer. Up to 512 vectors are allowed,
  because gl4es makes each ARB program parameter its own uniform and
  OutRun 2 SP's programs use 215.
- Varyings link by name. A fragment varying `x_N` also takes element N of a
  vertex array `x`, which is how gl4es's own shaders meet ARB-program ones.
- When a shader defines a function with a built-in's name (HotD4 defines its
  own `pow(float, int)`), a forwarding template sends
  every other call to Metal's built-in, so the shader's overload can't hide
  it and recurse.
- Texture helpers go into a program's MSL only for the sampler kinds it uses.
  Most of the cost of a cold Metal compile is in texture templates, so this
  cut OutRun 2 SP's first-boot compiles from about 157 s to about 50 s.
- Depth textures are read through `texture2d<float>`. A per-draw mask tells
  the shader which samplers return GL's (d, d, d, 1).

Metal libraries compile in the background. A draw waits only for the
programs its pipeline needs.

### State and drawing

- **Pipelines** are cached on the vertex formats and strides, the
  attachment formats, the blend state, the colour mask and whether the
  fragment discards. Making them costs well under a millisecond each, so a
  Metal binary archive wouldn't save anything worth having.
- **Render passes** stay open across draws to the same targets. Clears become
  the next pass's load action, unless a scissor or mask means they must be
  drawn. A copy or blit ends the pass. That was measured on HotD4 and
  OutRun 2 SP: about one pass break a frame and well under 0.1 ms of extra
  memory traffic, so copies stay outside passes.
- **Uploads** to a texture the current command buffer hasn't used go through
  a separate upload command buffer, so they don't split the frame. Per-draw
  data (client arrays, uniforms) goes into a streaming buffer. Objects
  deleted while the GPU still uses them are freed when it has finished.
- **Disabled vertex attributes** are constant-step attributes holding the
  `glVertexAttrib*` value.
- **Polygon offset** applies to triangles only, as in GL. Metal's depth bias
  would also move points and lines.
- **Occlusion queries** use Metal's visibility results. `GL_SAMPLES_PASSED`
  gives real counts, which OutRun 2 SP needs because its sun glare's
  strength is the visible pixel count divided by 12000.

### Textures

- DXT1/3/5 stay compressed as BC1-3 where the GPU samples them (Apple
  silicon Macs, M1 and later iPads), with gl4es passing them through. A
  compressed level can be read back (`glGetCompressedTexImage`, desktop GL's
  call, not ES's), block for block: Race TV reads its cars' DXT textures at
  boot and recolours them, and painted them with noise while it was refused. Resident
  texture memory dropped from 184/348/442 MB to 58/120/166 MB (OutRun 2 SP
  race, HotD4 stage 1, Race TV attract), and Race TV's boot lost 1.7 s of
  decoding.
- Formats with no alpha read alpha 1, as in GL.
- Depth and depth+stencil textures can be render targets, depth-copied by
  blit, and sampled. A packed depth+stencil texture is also attached as the
  stencil, which the games' shadow volumes need.
- Non-square mip levels are `max(1, size >> level)` on each side.
- Half-float colour textures (RGBA16Float) are render targets, and mtlgles
  offers `GL_EXT_color_buffer_half_float` to say so. Without it, gl4es
  recreates a float texture attached to a framebuffer as RGBA8, and that
  path resizes whichever texture is really bound, not the attached one.
  Let's Go Jungle's 800x600 scene came out as an 84x64 texture stretched
  to full screen (Common #33). Any extension gl4es checks before choosing a
  fallback has to be offered whenever mtlgles supports the feature, because
  gl4es's fallbacks are only tested on drivers that lack it.

## Data and settings

mtlgles keeps no files. Metal's own shader cache keeps compiled libraries
between runs, which is why only a game's first boot on a device is slow
(see Open items).

Environment variables for development, all off by default: shader and MSL
dumps, synchronous compiles, cold-compile timing, no BC textures, iPad-style
boolean queries on the Mac, and Metal GPU capture. The developer notes list
them.

## Platforms

| Platform | State |
|---|---|
| macOS (Apple silicon) | Default since 4 November 2026; all four games match native OpenGL |
| iOS / iPadOS | Default since 4 November 2026; all four games at 60 fps on the M1 iPad |
| tvOS | Planned for 1.4.0, with Lindbergh's tvOS build (4K third generation and later) |
| Linux, Windows | Not used; Mesa runs desktop GL directly |

Metal is the default for every Apple core. Apple's OpenGL ES (iOS) and
desktop OpenGL (macOS) stay selectable for one release: `-DLB_MTLGLES=OFF`
in the core, or `build_cores.py --lindbergh-renderer gl` in the app.

### The M1 iPad comparison

M1 iPad Pro, games paced to 60 fps. Metal copies each finished frame back
to the app, and Apple's OpenGL ES hands it over as an IOSurface, which is
how the app runs each of them.

| Game | Metal fps | OpenGL ES fps | Metal memory peak | OpenGL ES memory peak | First sound, warm (Metal / OpenGL ES) |
|---|---|---|---|---|---|
| The House of the Dead 4 | 60 | 60 | 957 MB | 1012 MB | 10.8 / 13.9 s |
| OutRun 2 SP SDX | 60 | 60 | 769 MB | 653 MB | 15.1 / 15.1 s |
| Let's Go Jungle! | 60 | 60 | 1058 MB | 1274 MB | 16.3 / 16.7 s |
| Sega Race TV | 60 | 44-45 | 1489 MB | 1172 MB | 28.1 / about 440 s |

- Metal's frame copy costs 0.1-0.3 ms, and the IOSurface hand-over on
  OpenGL ES costs 0.1-0.3 ms, so the copy doesn't matter.
- Race TV is CPU-bound in gl4es's emulation of `glMultiDrawElements` on
  both paths. A sub-draw costs 0.35 ms on Metal and 0.78-0.92 ms on Apple's
  OpenGL ES, which is the difference between 60 fps and 44-45 fps, and
  between a 3-4 minute and a 7 minute boot.
- On the first launch after an install, Metal's shader cache is empty.
  OutRun 2 SP then takes 61 s to first sound, building 381 pipelines in its
  first 300 frames, and Race TV takes 83 s. Later launches take the warm
  times in the table.
- With the per-frame pool, HotD4 stayed under 957 MB for 9000 frames.

### The M4 iPad and the real app

On the M4 iPad Pro, Race TV's boot to its first attract frame takes about
66 s cold and 30 s warm on Metal, and 68 s and 50 s on Apple's OpenGL ES.
On Metal, the worst cold frame (106 ms) comes from pipeline compiles. Warm,
the worst frame is 12.7 ms, and `glMultiDrawElements` costs 1.4-4.6 ms a
frame, against 11-38 ms on OpenGL ES.

HotD4 in the Insert Coin app on Metal runs at about 8-11 fps for its first
5 s while shaders compile, then at 60 fps on both iPads. The game thread
takes 5-6 ms a frame on the M4 and 7-9 ms on the M1. On the M4, memory is
steady at about 855 MB and peaks at 993 MB in the demo.

## Testing

- Scripted runs of each game on the Mac: attract, coin, start and play,
  with frames compared against native OpenGL on the same machine. This
  covers HotD4 stage 1, an OutRun 2 SP race on the coast (glare, shadows,
  reflections), Let's Go Jungle's 3D and Race TV's boot and attract.
- Metal API validation, which runs clean.
- An Xcode GPU capture of any frame that draws wrong, before any logging.
  It reads pixel values before and after a draw, the pipeline's blend and
  write masks, and the bound textures.
- The same runs on an M1 iPad: looks, frame time, memory, and boot time cold
  and warm, against Apple's OpenGL ES (see The M1 iPad comparison).
- The gl4es fixes replay in order on a pristine gl4es
  (`tools/replayfix.py` in the Lindbergh repository), and a configure stops
  if one no longer matches.

## Open items

- **First boot is slow.** On a device whose Metal shader cache doesn't yet
  hold a game's shaders, each program compiles at its first draw: about
  50 s in all for OutRun 2 SP, during which it runs slowly. Later boots
  are fast. No Metal API keeps a source-compiled library across runs, a
  binary archive only keeps pipelines, and shipping the games' shaders is
  ruled out (rules.md #7). Accepted.
- **Frames are read back.** The core copies each finished frame to the
  app, at 0.1-0.3 ms a frame on the M1 iPad. On Apple's OpenGL ES the frame
  is handed over as an IOSurface without a copy. mtlgles could do the same
  with an IOSurface-backed texture. It's a small gain, so it isn't planned.
- **Race TV's boot**: gl4es emulates `glMultiDrawElements` by copying
  indices and vertices on the CPU. On Metal that now costs 1.4-4.6 ms a
  warm boot frame on the M4 iPad, so a gl4es fix isn't needed and was
  dropped. Only the old OpenGL ES path, which is kept for one release, is
  still slowed by it.
- **gl4es's float-attachment fallback** binds only when its own record of
  unit 0 differs, so it can resize the wrong texture on any driver without
  `GL_EXT_color_buffer_half_float`. mtlgles avoids it by offering the
  extension. A gl4es fix (always bind) is possible but not needed now.
- **tvOS:** the Lindbergh tvOS build itself (1.4.0).

## Decisions

- **Our own layer, not ANGLE** (3 November 2026, after the HotD4 spike):
  faster, much smaller, and it can make copies GPU blits.
- **Keep gl4es on top**: its fixes serve every OpenGL ES driver, Apple's
  and ours, and the same runtime runs everywhere.
- **Lives in Cores-Common**, opt-in, so any other core drawing through gl4es
  can use it without depending on Lindbergh.
- **Metal the default on iOS and macOS** (4 November 2026, Lindbergh #86,
  after the M1 iPad comparison): it matches Apple's OpenGL ES in three
  games and beats it in Race TV, and Apple keeps developing only Metal. The
  old path stays selectable for one release.
- **No precompiled game shaders**: they would be derived from the games
  (rules.md #7), so the first-boot compile stays.
- **Copies stay outside render passes**: measured as not worth it.
- **BSD-3** for mtlgles, MIT for gl4es: both compatible with every core's
  licence.
