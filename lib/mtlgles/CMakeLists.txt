# mtlgles: an OpenGL ES 3.0 subset on Metal, the one gl4es needs to draw the
# Lindbergh games. BSD-3-Clause. Apple only; built when AFC_MTLGLES is on.
enable_language(OBJC)
add_library(mtlgles STATIC glsl_msl.c mtlgles.m mtlgles_objects.m mtlgles_program.m mtlgles_draw.m)
add_library(afc::mtlgles ALIAS mtlgles)
target_include_directories(mtlgles PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(mtlgles PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../../include) # af_debug.h
set_source_files_properties(mtlgles.m mtlgles_objects.m mtlgles_program.m mtlgles_draw.m PROPERTIES
  COMPILE_FLAGS "-fno-objc-arc")
target_compile_options(mtlgles PRIVATE -O2 -Wall -Wno-unused-function)
set_target_properties(mtlgles PROPERTIES POSITION_INDEPENDENT_CODE ON) # linked into the cores' frameworks
target_link_libraries(mtlgles PUBLIC "-framework Metal" "-framework Foundation")
