# esp_event / esp_netif / esp_wifi are only called by the shim-only s31events
# and s31wifi commands. They are listed unconditionally anyway: with the shim off
# that code is compiled out and --gc-sections drops the components, so the
# default image contains zero symbols from them (verified with nm). A conditional
# REQUIRES looked tidier but CONFIG_* was not visible at the point this file is
# evaluated, so the block silently evaluated false and main.c then failed to find
# esp_event.h.
# WHOLE_ARCHIVE, because nothing references cmd_wifi(): the msh command table is
# section-based, so the linker has no reason to pull s31wifi.c.obj out of
# libmain.a and the command would silently never appear in the shell. A `-u
# cmd_wifi` from this component lands after libmain.a on the link line - IDF
# appends a component's own interface libraries at the end - so by the time the
# archive is scanned the symbol is not yet undefined and the hint does nothing.
# Forcing every object in is cheaper to reason about than ordering tricks, and
# there are only three.
#
# s31wifiscan.c was removed during the W59/W60 regression bisection because
# removing it made the suite pass. That diagnosis was wrong - the same log
# section exonerates it on the next page (the real cause was CPU1 calling
# rt_system_scheduler_start()) - but the file was never put back, so a working
# 193-line diagnostic sat on disk unused. It answers the one M6 question
# `s31wifi` cannot: what can the radio actually see.
idf_component_register(SRCS "main.c" "s31wifi.c" "s31ble.c" "s31wifiscan.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_hal_uart soc esp_event esp_netif esp_wifi nvs_flash
                       PRIV_REQUIRES bt
                       WHOLE_ARCHIVE)

# MSH_CMD_EXPORT casts the command function to `long (*)(void)`, which is the
# upstream finsh ABI; silence the cast warning instead of touching the macro.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wno-cast-function-type)
