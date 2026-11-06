/* SPDX-License-Identifier: Apache-2.0 */
/*
 * ESP32-S31 kernel-only RT-Thread configuration.
 *
 * The upstream RT-Thread checkout supplies src/ and include/ unmodified, so this
 * component owns the target configuration. Do not add kernel headers to this
 * directory - rtconfig.h must remain the only header, so upstream headers are
 * not shadowed.
 *
 * Keep common settings aligned with port/esp32c3/rtconfig.h and
 * port/esp32c6/rtconfig.h.
 */
#ifndef RT_CONFIG_H__
#define RT_CONFIG_H__

/* ESP32-S31: RV32 with a 128-bit SIMD datapath on one core, two harts,
 * machine mode, CLIC interrupt controller. */
#define ARCH_RISCV
#define ARCH_RISCV32
#define SOC_ESPRESSIF
#define SOC_ESP32_S31

/* Both cores are configured and both run the scheduler.
 *
 * rt_hw_secondary_cpu_up() starts CPU1, it takes _cpus_lock, and
 * rt_system_scheduler_start() picks its idle thread. From there CPU1 has its own
 * tick (GPTimer channel 3, see smp_tick.c), its own interrupt stack, its own CLIC
 * threshold, and takes the IPI used by rt_hw_ipi_send(). `s31smp` reports which
 * core is running what, and `cpu1probe` is a thread bound to CPU1 that proves it.
 *
 * The race that used to be noted here (CPU0 editing the ready table while CPU1
 * called rt_system_scheduler_start()) was a real lock-discipline bug in this port,
 * not a limitation of the hardware: the fix is to take _cpus_lock first and let
 * rt_system_scheduler_start() release it.
 *
 * RT_SMP_CPU1_SCHEDULES below is the switch that selects between "CPU1 schedules"
 * and the older "CPU1 parks in wfi"; the parked behaviour is kept because it is a
 * useful fallback when debugging. */
/* The RISC-V port provides rt_hw_atomic_* (atomic_riscv.c). Selecting
 * them keeps every rt_atomic_* call lock-free; the software fallback
 * disables interrupts, which in an SMP build means taking the global
 * spinlock. See the note in CMakeLists.txt. */
#define RT_USING_HW_ATOMIC
#define RT_USING_SMP
#define RT_CPUS_NR                      2
#define RT_USING_SMP_SLAVE
#define RT_USING_SMP_SLAVE_LOCK

/* Let CPU1 run the scheduler instead of parking in wfi.
 *
 * Three things had to exist first and now do: CPU1's own tick, the IPI receive
 * side, and the FPU/trap-ordering fix in rt_hw_secondary_cpu_irq_enable(). Without
 * the tick, rt_sched_tick_increase() never runs on this core and its idle thread
 * can never be preempted, so scheduling there would be a no-op.
 *
 * Verified with this on: s31test 36/36 (three consecutive runs), s31smp reporting
 * SCHEDULING, cpu1probe executing on cpu 1, and s31wifi connecting and getting an
 * IP. That last one needed the flash cross-core handshake described below; before
 * it, Wi-Fi on two cores reset the board.
 *
 * Undefine this to return to the parked-core behaviour. That path is still useful:
 * it removes CPU1 from every scheduling and interrupt question, which is what you
 * want when bisecting a problem that might involve the second core.
 */
#define RT_SMP_CPU1_SCHEDULES
/*
 * Two things that used to be listed here as open are now closed, and the notes are
 * kept because both were subtle enough to be worth not rediscovering:
 *
 *   1. "s31wifi resets the board with this on". Cause: spi_flash's
 *      disable_interrupts_caches_and_other_cpu() has its multi-core half compiled
 *      out under CONFIG_FREERTOS_UNICORE=y (verified by disassembly - six
 *      instructions, no IPC, and esp_ipc_call_* is absent from the ELF). So CPU0
 *      turned its flash cache off while CPU1 kept fetching from flash, and CPU1's
 *      tick handler calls rt_tick_increase(), which lives in flash. MCAUSE 0x19.
 *      Fixed with a weak hook pair the component overrides:
 *      spi_flash_other_core_stop()/resume(), acknowledged from IRAM by CPU1's tick
 *      ISR before it touches anything in flash. See smp_tick.c.
 *
 *      Binding every shim task to CPU0 (port/freertos/ft_task.c) was the first
 *      hypothesis. It is still the right thing to do - IDF components are not
 *      reentrant across cores - but on its own it changed the failure from
 *      CPU_LOCKUP_RESET to SW_CPU_RESET rather than fixing it.
 *
 *   2. "CPU1 does not dispatch even though it reaches the scheduler". Cause:
 *      mstatus.FS was 0 on CPU1, and the trap prologue saves f0..f31
 *      unconditionally. The first fsw was an illegal instruction, so the core died
 *      inside its own trap handler - which is why traps were taken (the spin loop
 *      stopped) but the handler never ran (isr_runs stayed 0) and mintstatus.MIL
 *      read 0. Fixed in rt_hw_secondary_cpu_irq_enable().
 */

/* Experiment switch: skip CPU1's tick, to isolate what stops its spin loop. */
/* #define RT_SMP_CPU1_NO_TICK */

/* Deliberate fault on CPU1, to test its trap path in isolation. */
/* #define RT_SMP_CPU1_FAULT_TEST */


#define RT_NAME_MAX                     12
/* RV32 psABI requires a 16-byte aligned stack. */
#define RT_ALIGN_SIZE                   16
#define RT_THREAD_PRIORITY_32
#define RT_THREAD_PRIORITY_MAX          32
/* Must stay equal to CONFIG_FREERTOS_HZ: the tick comes from the IDF SYSTIMER
 * alarm and xPortSysTickHandler() calls rt_tick_increase() once per alarm. */
#define RT_TICK_PER_SECOND              1000
#define IDLE_THREAD_STACK_SIZE          1024

/* Real system heap: kservice.c -> rt_smem_init() in mem.c. The memory region
 * itself comes from IDF_RTTHREAD_HEAP_SIZE (__heap_start__/__heap_end__). */
#define RT_USING_HEAP
#define RT_USING_SMALL_MEM
#define RT_USING_SMALL_MEM_AS_HEAP

#define RT_USING_SEMAPHORE
#define RT_USING_MUTEX
/* Event groups. port/freertos/ft_event.c maps xEventGroup* onto rt_event, and
 * esp_wifi/esp_event/lwip all use event groups, so this is not optional. */
#define RT_USING_EVENT

/* Device framework + serial v1 + pin.
 *
 * components/drivers/core/device.c is part of this component's kernel source
 * list, components/drivers/serial/dev_serial.c supplies
 * rt_hw_serial_register(), and port/esp32s31/gpio.c registers the "pin"
 * device. Only the v1 serial API is used: the tty/posix half needs
 * RT_USING_SMART.
 *
 * With RT_USING_DEVICE on, finsh switches from rt_hw_console_getchar() to
 * reading a console device (components/finsh/shell.c), which is why main()
 * binds the shell to "uart0".
 */
#define RT_USING_DEVICE
#define RT_USING_SERIAL
#define RT_USING_SERIAL_V1
#define RT_SERIAL_RB_BUFSZ              64
#define RT_USING_PIN

/* I2C bus device class (components/drivers/i2c). port/esp32s31/i2c.c registers
 * "i2c0" through it, which is what makes rt_i2c_transfer() and the `i2cscan`
 * command available.
 *
 * RT_USING_I2C_BUSSING used to sit here. Nothing reads it: dev_i2c_core.c gates
 * on RT_USING_DM only, rtdevice.h gates the header on RT_USING_I2C, none of the
 * 466 upstream BSP rtconfig.h files define it, and a whole-tree search of the
 * kernel finds zero references. A switch no code consults is worse than no
 * switch - it reads as "master/slave support is configured" while the only
 * thing that decides is RT_USING_I2C. Removed. */
#define RT_USING_I2C
#define RT_USING_SPI

/* ADC device class (components/drivers/adc). port/esp32s31/adc.c registers
 * "adc1"/"adc2" through it, which is what makes rt_adc_enable() and
 * rt_adc_read() available. */
#define RT_USING_ADC

/* PWM device class (components/drivers/misc/rt_drv_pwm.c). The LEDC driver
 * registers "pwm0" through it, which is what makes rt_pwm_set()/rt_pwm_enable()
 * and msh's `pwm` command available. */
#define RT_USING_PWM

/* Pulse encoder device class (components/drivers/misc/pulse_encoder.c).
 *
 * This port's PCNT driver used to say "RT-Thread has no pulse-counter class" and
 * exposed a private C API instead. The claim was wrong: 5.4.0 ships the class, and
 * pulse_encoder.c even has a !RT_USING_DEVICE_OPS branch, which is this port's
 * configuration.
 *
 * Enabling it lets the PCNT driver register "pcnt0" as a real rt_device: the
 * count is readable with rt_device_read() and PULSE_ENCODER_CMD_* works - the same
 * shape as pwm0 and adc1. */
#define RT_USING_PULSE_ENCODER

/* Console and shell.
 *
 * FINSH_USING_SYMTAB needs __fsymtab_start/__fsymtab_end and the .rti_fn*
 * table; both are emitted by esp32s31/sections.ld.in under
 * CONFIG_IDF_RTOS_RTTHREAD.
 */
#define RT_USING_CONSOLE
#define RT_CONSOLEBUF_SIZE              256
/* INIT_*_EXPORT() expands to nothing without this, which would leave the
 * .rti_fn table empty and stop finsh_system_init() from ever running. */
#define RT_USING_COMPONENTS_INIT
#define RT_USING_FINSH
#define FINSH_USING_MSH
/* Built-in commands (ps / free, and `bind` on multi-core) live behind this
 * switch in msh.c:60. Without it they are compiled out silently: the shell still
 * works and `help` still answers, it just lists fewer commands - and the two a
 * bring-up image actually needs (`free` to watch the heap, `ps` to see what is
 * running) are exactly the ones that vanish. Nothing else in this port implies
 * it, so it has to be spelled out. */
#define MSH_USING_BUILT_IN_COMMANDS
#define FINSH_THREAD_NAME               "tshell"
#define FINSH_THREAD_PRIORITY           20
#define FINSH_THREAD_STACK_SIZE         4096
#define FINSH_USING_HISTORY
#define FINSH_HISTORY_LINES             5
#define FINSH_USING_SYMTAB
#define FINSH_USING_DESCRIPTION
/* Command option matching, needed for any command that dispatches on a sub-word.
 *
 * Without it finsh.h defines
 *
 *     #define MSH_OPT_ID_GET(fun) ((int)(-1UL))
 *
 * so the switch in rt_drv_pwm.c's pwm_list() can never match its own enum, and
 * `pwm probe pwm0` answers "Please using 'pwm probe <device name>' first." even
 * though pwm0 is registered and visible in `list device`. Measured on the board.
 *
 * cmd.c carries a fallback implementation behind #ifndef, which is why `list` keeps
 * working either way - rt_drv_pwm.c does not, so the pwm command is dead without
 * this. 431 of 466 upstream BSPs enable it and the Kconfig default is y, so this is
 * the mainstream setting rather than an addition.
 *
 * dvfs_cmd.c is the only other consumer and is not built here. */
#define FINSH_USING_OPTION_COMPLETION
#define FINSH_CMD_SIZE                  80
#define FINSH_ARG_MAX                   8

/* Interrupt nesting on ESP32-S31 is done in HARDWARE: soc_caps.h defines
 * SOC_INT_CLIC_SUPPORTED and SOC_INT_HW_NESTED_SUPPORTED, so the trap path in
 * vectors_rtthread.S does not touch the interrupt threshold itself - the CLIC
 * raises mintstatus on entry and restores it on mret. It only has to save this
 * level's mcause before returning, because nested interrupts overwrite it.
 *
 * Do not define SOC_INT_HW_NESTED_SUPPORTED here. It is an SoC capability read
 * from soc/soc_caps.h by the assembly and by C code that branches on it.
 */

/* Still undefined on purpose, not by accident. Each of these needs a component
 * that this image does not link, so defining it would compile code whose
 * dependencies are absent rather than add a feature:
 *
 *   RT_USING_SOCKET / RT_USING_LIBC  - the lwIP port and newlib are not wired in
 *   RT_USING_DFS                      - no filesystem, so no /dev and no ymodem
 *   RT_USING_SMART                    - that is the microkernel variant
 *
 * Devices, the shell and the console ARE enabled above; the earlier version of this
 * comment claimed otherwise and had gone stale, which is worse than saying nothing:
 * a reader cannot tell whether a missing macro is a decision or an oversight. */
/* kservice.c compiles the generic backtrace walker unconditionally. */
#define RT_BACKTRACE_LEVEL_MAX_NR       8

#endif /* RT_CONFIG_H__ */
