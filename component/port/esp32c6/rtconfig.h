/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ESP32-C6 kernel-only RT-Thread configuration.
 *
 * Moved here from the standalone rt-esp32c6 port checkout (Option A): the
 * upstream checkout supplies src/ and include/ unmodified, so this component
 * owns the target configuration. Do not add kernel headers to this directory -
 * rtconfig.h must remain the only header, so upstream headers are not shadowed.
 *
 * Keep common settings aligned with port/esp32c3/rtconfig.h.
 */
#ifndef RT_CONFIG_H__
#define RT_CONFIG_H__

/* ESP32-C6: RV32IMAC, one application CPU, no FPU and no SMP. */
#define ARCH_RISCV
#define ARCH_RISCV32
#define SOC_ESPRESSIF
#define SOC_ESP32_C6
#define RT_CPUS_NR                      1
#define RT_NAME_MAX                     12
/* RV32 psABI requires a 16-byte aligned stack. */
#define RT_ALIGN_SIZE                   16
#define RT_THREAD_PRIORITY_32
#define RT_THREAD_PRIORITY_MAX          32
#define RT_TICK_PER_SECOND              1000
#define IDLE_THREAD_STACK_SIZE          1024

/* Real system heap: kservice.c -> rt_smem_init() in mem.c. */
#define RT_USING_HEAP
#define RT_USING_SMALL_MEM
#define RT_USING_SMALL_MEM_AS_HEAP
/* In addition to target/version/backtrace settings, C6 also
 * builds the semaphore and mutex parts of src/ipc.c. Nothing in either IDF
 * tree calls rt_sem_*()/rt_mutex_*() yet.
 */
#define RT_USING_SEMAPHORE
#define RT_USING_MUTEX

/* Interrupt nesting is maintained in software: vectors_rtthread.S reads
 * soc/soc_caps.h (not this header), and the C6 uses the PLIC rather than CLIC,
 * so SOC_INT_HW_NESTED_SUPPORTED is undefined for it just as for the C3. The
 * trap path therefore saves, raises and restores the interrupt threshold in
 * software and only switches threads at the outermost IRQ exit, and
 * rt_interrupt_nest is the upstream 4-byte rt_atomic_t in src/irq.c loaded with
 * lw. The previous port checkout defined SOC_INT_HW_NESTED_SUPPORTED here and
 * guarded it with #error; that claim was wrong and the define was inert for
 * the trap path, and keeping it would only mislead C code that branches on it.
 *
 * No networking, filesystem, Finsh, devices, console or components: those
 * RT_USING_* macros stay undefined rather than being defined as 0. Atomic
 * operations use RT-Thread's interrupt-protected UP implementation, so
 * RT_USING_STDC_ATOMIC / RT_USING_HW_ATOMIC stay off.
 */
#define RT_VER_NUM                      0x50301
/* kservice.c compiles the generic backtrace walker unconditionally. */
#define RT_BACKTRACE_LEVEL_MAX_NR       8

#endif /* RT_CONFIG_H__ */
