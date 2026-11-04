cmake_minimum_required(VERSION 3.16)

# Where the unmodified upstream RT-Thread checkout lives.
#
# Resolution order, first hit wins:
#   1. -DRTTHREAD_ROOT=<path> on the idf.py command line
#   2. the RTTHREAD_ROOT environment variable
#   3. ../rt-thread next to this project (the layout the quick start creates)
#
# The component only needs src/, include/, components/ and libcpu/ from that tree.
# Everything target specific lives in esp-idf/components/rtthread/port/, not here.
if(NOT DEFINED RTTHREAD_ROOT OR "${RTTHREAD_ROOT}" STREQUAL "")
    if(DEFINED ENV{RTTHREAD_ROOT} AND NOT "$ENV{RTTHREAD_ROOT}" STREQUAL "")
        set(RTTHREAD_ROOT "$ENV{RTTHREAD_ROOT}" CACHE PATH "RT-Thread checkout")
    else()
        get_filename_component(_rtt_default "${CMAKE_CURRENT_LIST_DIR}/../rt-thread" ABSOLUTE)
        set(RTTHREAD_ROOT "${_rtt_default}" CACHE PATH "RT-Thread checkout")
    endif()
endif()

if(NOT EXISTS "${RTTHREAD_ROOT}/src/thread.c")
    message(FATAL_ERROR
        "RTTHREAD_ROOT does not look like an RT-Thread checkout:\n"
        "  ${RTTHREAD_ROOT}\n"
        "Expected to find src/thread.c there.\n"
        "Run tools/fetch-upstream.ps1 (or .sh) to clone the supported revision,\n"
        "or pass -DRTTHREAD_ROOT=<path>.")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(s31_rtt_test)
