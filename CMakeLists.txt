cmake_minimum_required(VERSION 3.20)
project(arcade_foundry_cores_common LANGUAGES C CXX)

# Code several Arcade Foundry core repos share, used as a git submodule
# (extern/common in each core repo) through add_subdirectory:
#
#   afcore    the core interface header, include/afcore.h (INTERFACE target)
#   afcommon  ROM set and game archives: zip (own inflate), 7z (the LZMA
#             SDK, when AFC_LZMA_DIR has it) and tar (include/afcommon/)
#   mtlgles   (opt-in, AFC_MTLGLES, Apple only) an OpenGL ES 3.0 subset on
#             Metal under gl4es (lib/mtlgles)
#
# Standalone (cmake -S . -B build) it also builds and runs the tests.

if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    set(AFC_TOP_LEVEL ON)
else()
    set(AFC_TOP_LEVEL OFF)
endif()
option(AFC_TESTS "Build the Cores-Common tests" ${AFC_TOP_LEVEL})
# The 7-Zip LZMA SDK's C/ folder (public-domain files only). The core repos
# fetch it already (Model12 into extern/lzma); point this at their copy.
set(AFC_LZMA_DIR "${CMAKE_CURRENT_SOURCE_DIR}/extern/lzma/C" CACHE PATH "7-Zip LZMA SDK C/ folder, for 7z ROM sets")

add_library(afcore INTERFACE)
add_library(afc::afcore ALIAS afcore)
target_include_directories(afcore INTERFACE ${CMAKE_CURRENT_SOURCE_DIR}/include)

add_library(afcommon STATIC src/zip.cpp src/archive.cpp src/tar.cpp)
add_library(afc::afcommon ALIAS afcommon)
target_include_directories(afcommon PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_compile_features(afcommon PUBLIC cxx_std_17)
set_target_properties(afcommon PROPERTIES POSITION_INDEPENDENT_CODE ON) # linked into the cores' shared libraries

set(AFC_7Z OFF)
if(TARGET lzma7z)
    set(AFC_7Z ON)
    # A core that builds the SDK itself (Model12): use its library.
    target_link_libraries(afcommon PRIVATE lzma7z)
    target_compile_definitions(afcommon PRIVATE AFC_HAVE_7Z)
elseif(EXISTS ${AFC_LZMA_DIR}/7zArcIn.c)
    set(L ${AFC_LZMA_DIR})
    add_library(afc_lzma7z STATIC
        ${L}/7zArcIn.c ${L}/7zDec.c ${L}/7zBuf.c ${L}/7zCrc.c ${L}/7zCrcOpt.c
        ${L}/7zStream.c ${L}/7zAlloc.c ${L}/Alloc.c ${L}/LzmaDec.c ${L}/Lzma2Dec.c
        ${L}/Bra.c ${L}/Bra86.c ${L}/BraIA64.c ${L}/Bcj2.c ${L}/Delta.c
        ${L}/CpuArch.c ${L}/Ppmd7.c ${L}/Ppmd7Dec.c)
    target_include_directories(afc_lzma7z SYSTEM PUBLIC ${L})
    target_compile_definitions(afc_lzma7z PRIVATE Z7_PPMD_SUPPORT) # 7z's PPMd method too, not only LZMA/LZMA2
    set_target_properties(afc_lzma7z PROPERTIES POSITION_INDEPENDENT_CODE ON)
    if(NOT MSVC)
        target_compile_options(afc_lzma7z PRIVATE -w)
    endif()
    set(AFC_7Z ON)
    target_link_libraries(afcommon PRIVATE afc_lzma7z)
    target_compile_definitions(afcommon PRIVATE AFC_HAVE_7Z)
else()
    message(STATUS "Cores-Common: no LZMA SDK in ${AFC_LZMA_DIR}: 7z ROM sets unsupported")
endif()

# mtlgles: off unless a core asks for it (Lindbergh's LB_MTLGLES)
option(AFC_MTLGLES "Build mtlgles, the OpenGL ES 3.0 subset on Metal (Apple only)" OFF)
if(AFC_MTLGLES)
    if(NOT APPLE)
        message(FATAL_ERROR "AFC_MTLGLES needs Metal (macOS, iOS)")
    endif()
    add_subdirectory(lib/mtlgles)
endif()

if(AFC_TESTS)
    enable_testing()
    find_package(Python3 REQUIRED COMPONENTS Interpreter)
    add_executable(afcommon_test tests/afcommon_test.cpp)
    target_link_libraries(afcommon_test PRIVATE afcommon afcore)
    if(AFC_7Z)
        target_compile_definitions(afcommon_test PRIVATE AFC_HAVE_7Z) # needs py7zr for the 7z fixtures
    endif()
    # Fixtures made by Python's zipfile and tarfile, so the readers are
    # checked against another implementation's output.
    add_test(NAME fixtures COMMAND Python3::Interpreter ${CMAKE_CURRENT_SOURCE_DIR}/tests/make_fixtures.py
             ${CMAKE_CURRENT_BINARY_DIR}/fixtures)
    set_tests_properties(fixtures PROPERTIES FIXTURES_SETUP archives)
    add_test(NAME afcommon COMMAND afcommon_test ${CMAKE_CURRENT_BINARY_DIR}/fixtures)
    set_tests_properties(afcommon PROPERTIES FIXTURES_REQUIRED archives)
    add_executable(af_debug_test tests/af_debug_test.c)
    target_link_libraries(af_debug_test PRIVATE afcore)
    add_test(NAME af_debug COMMAND af_debug_test)
    # Every C file a front end includes must compile as C.
    add_library(afcommon_c_headers OBJECT tests/c_headers.c)
    target_include_directories(afcommon_c_headers PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
endif()
