/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ESP32-C3 kernel-only RT-Thread configuration.
 *
 * Moved here from the standalone rt-esp32c3 port checkout (Option A): the
 * upstream checkout supplies src/ and include/ unmodified, so this component
 * owns the target configuration. Do not add kernel headers to this directory -
 * rtconfig.h must remain the only header, so upstream headers are not shadowed.
 *
 * Keep common settings aligned with port/esp32c6/rtconfig.h.
 */
#ifndef RT_CONFIG_H__
#define RT_CONFIG_H__

/* ESP32-C3: RV32IMC, one hart, machine mode. */
#define ARCH_RISCV
#define ARCH_RISCV32
#define SOC_ESPRESSIF
#define SOC_ESP32_C3
#define RT_CPUS_NR                      1
#define RT_NAME_MAX                     12
/* RV32 psABI requires a 16-byte aligned stack. */
#define RT_ALIGN_SIZE                   16
#define RT_THREAD_PRIORITY_32
#define RT_THREAD_PRIORITY_MAX          32
#define RT_TICK_PER_SECOND              1000
#define IDLE_THREAD_STACK_SIZE          1024
/* kservice.c compiles the generic backtrace walker unconditionally. */
#define RT_BACKTRACE_LEVEL_MAX_NR       32

#define RT_USING_HEAP
#define RT_USING_SMALL_MEM
#define RT_USING_SMALL_MEM_AS_HEAP

/* Interrupt nesting is maintained in software: vectors_rtthread.S reads
 * soc/soc_caps.h (not this header), and neither ESP32-C3 nor ESP32-C6 defines
 * SOC_INT_HW_NESTED_SUPPORTED, so the trap path saves, raises and restores the
 * interrupt threshold itself and only switches threads at the outermost IRQ
 * exit. rt_interrupt_nest is the upstream 4-byte rt_atomic_t in src/irq.c and
 * the vector handler loads it with lw. Do not define
 * SOC_INT_HW_NESTED_SUPPORTED here: it is an SoC capability, not a kernel
 * option, and defining it only misleads C code that branches on it.
 *
 * No SMP, C11 atomics or RISC-V A instructions. Tick and hard timers come from
 * clock.c and timer.c; the board timer ISR calls rt_tick_increase() once per
 * millisecond. No soft-timer thread, IPC, console, devices, Finsh, DFS or
 * networking. src/ipc.c is still compiled for the suspend-list helpers.
 */
#endif /* RT_CONFIG_H__ */
