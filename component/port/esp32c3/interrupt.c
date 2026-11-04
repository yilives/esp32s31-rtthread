/*
 * Derived from rt-esp32c3 port/interrupt.c (standalone port working tree).
 * Change: retain only rt_hw_interrupt_is_disabled; interrupt disable/enable
 * are provided by port/common/context_gcc.S.
 */
/* SPDX-License-Identifier: Apache-2.0
 * ESP32-C3 single-hart interrupt masking for kernel software atomics.
 */
#include <rthw.h>
#include <rtthread.h>





rt_bool_t rt_hw_interrupt_is_disabled(void)
{
    rt_ubase_t status;
    __asm__ volatile ("csrr %0, mstatus" : "=r" (status) :: "memory");
    return (status & 8) == 0;
}
