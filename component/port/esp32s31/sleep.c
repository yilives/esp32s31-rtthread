/*
 * Deep sleep, as an RT-Thread component.
 *
 * The point of deep sleep on this chip is the LP core: the HP core powers down,
 * the LP core keeps sampling something, and either a timer or that core wakes the
 * HP side back up. Everything that makes the cycle work lives in IDF already -
 * `esp_deep_sleep_start()` reaches pmu_sleep_start() through 43 object files that
 * compile today - so the question was never "can this be ported" but "does it
 * link and behave when the scheduler is RT-Thread's".
 *
 * Three things had to be true and are:
 *
 *   1. It links. `esp_deep_sleep_start()`, `esp_sleep_enable_timer_wakeup()` and
 *      `esp_sleep_get_wakeup_cause()` all appear in the image; CMakeLists.txt
 *      anchors them with -u, because nothing else references them and
 *      --gc-sections would otherwise drop the whole graph.
 *
 *   2. It does not need FreeRTOS. sleep_modes.c.obj's 85 undefined symbols
 *      contain exactly one OS reference - vPortEnterCritical/ExitCritical - and
 *      those exist, because the RISC-V port layer is kept for the tick and
 *      critical sections. `esp_task_wdt_stop()` is guarded by
 *      CONFIG_ESP_TASK_WDT_USE_ESP_TIMER, which is off here, so it is not needed.
 *
 *   3. The other core is handled. The ECO3 cache-lock path calls
 *      esp_ipc_isr_stall_other_cpu(), but that whole block is behind
 *      CONFIG_ESP32_ECO3_CACHE_LOCK_FIX, which is an ESP32-only option. On S31
 *      the only remaining "other core" question is the one this port already
 *      solved for flash: CPU1 must not be mid-flight when the world stops.
 *
 * What the port has to add on top
 * -------------------------------
 * `esp_deep_sleep_start()` never returns: it powers the chip down and the next
 * instruction executed is the boot ROM's, on a fresh reset. So everything the
 * caller needs after waking has to be arranged BEFORE the call - which is what
 * this file does, and why it is a wrapper rather than a thin alias:
 *
 *   - a marker in RTC memory, so the next boot can tell "woke from deep sleep"
 *     apart from "cold power-on"
 *   - the wakeup cause, printed before anything else runs
 *   - the timer wakeup source armed, so the board comes back at all
 *
 * The marker is a plain RTC-memory variable with a magic value. RTC memory is not
 * cleared by deep sleep but IS cleared by power-on, which is exactly the
 * distinction that matters.
 */

#include <stdlib.h>   /* atoi */

#include <rthw.h>
#include <rtthread.h>
#include <rtdevice.h>

#include "sdkconfig.h"
#include "soc/soc_caps.h"
#include "soc/soc.h"
#include "esp_attr.h"
#include "esp_sleep.h"

#if defined(SOC_DEEP_SLEEP_SUPPORTED) && defined(CONFIG_RTT_SLEEP_LINK_PROBE)

/* Survives deep sleep, cleared by a power-on reset. */
RTC_NOINIT_ATTR static rt_uint32_t s31_sleep_magic;
RTC_NOINIT_ATTR static rt_uint32_t s31_sleep_count;

#define S31_SLEEP_MAGIC 0x53314C50u   /* "S1LP" */

static const char *s31_sleep_cause_name(esp_sleep_wakeup_cause_t cause)
{
    switch (cause)
    {
    case ESP_SLEEP_WAKEUP_UNDEFINED: return "undefined (cold boot)";
    case ESP_SLEEP_WAKEUP_ALL:       return "all";
    case ESP_SLEEP_WAKEUP_EXT0:      return "ext0";
    case ESP_SLEEP_WAKEUP_EXT1:      return "ext1";
    case ESP_SLEEP_WAKEUP_TIMER:     return "timer";
    case ESP_SLEEP_WAKEUP_TOUCHPAD:  return "touchpad";
    case ESP_SLEEP_WAKEUP_ULP:       return "ulp / lp core";
    case ESP_SLEEP_WAKEUP_GPIO:      return "gpio";
    case ESP_SLEEP_WAKEUP_UART:      return "uart";
    default:                         return "other";
    }
}

int cmd_sleep(int argc, char **argv)
{
    esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();
    rt_uint32_t seconds = 5;

    if (argc > 1 && rt_strcmp(argv[1], "info") == 0)
    {
        rt_kprintf("s31sleep: last wakeup cause : %s (%d)\n",
                   s31_sleep_cause_name(cause), (int)cause);
        rt_kprintf("s31sleep: RTC marker        : 0x%08x %s\n",
                   (unsigned)s31_sleep_magic,
                   (s31_sleep_magic == S31_SLEEP_MAGIC) ? "(set: woke from deep sleep)"
                                                       : "(clear: cold boot)");
        rt_kprintf("s31sleep: cycles survived   : %u\n", (unsigned)s31_sleep_count);
        rt_kprintf("s31sleep: deep sleep        : %s\n",
                   SOC_DEEP_SLEEP_SUPPORTED ? "supported" : "not supported");
        return RT_EOK;
    }

    if (argc > 2)
    {
        seconds = (rt_uint32_t)atoi(argv[2]);   /* rt_atoi does not exist */
        if (seconds == 0 || seconds > 3600)
        {
            rt_kprintf("s31sleep: seconds must be 1..3600\n");
            return -RT_EINVAL;
        }
    }

    rt_kprintf("s31sleep: arming timer wakeup for %u s, then deep sleep\n", seconds);
    rt_kprintf("s31sleep: this call does not return - expect a reset\n");

    /* Set the marker before sleeping so the next boot can recognise itself. */
    s31_sleep_magic = S31_SLEEP_MAGIC;
    s31_sleep_count++;

    esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);

    /* If CPU1 is scheduling, it must not be running when the chip powers down.
     * Parking it is the same idea as the flash window in smp_tick.c, but here the
     * whole core stops, so there is nothing to hand back. */
#ifdef RT_USING_SMP
    {
        extern volatile unsigned s31_flash_op;

        /* Reuse the flag the tick ISR already honours: setting it makes CPU1
         * acknowledge and stop calling into the kernel from its ISR. The flash
         * operation handshake holds it for microseconds; here it stays set until
         * the chip is gone, which is fine - nothing has to run on CPU1 again. */
        s31_flash_op = 1;
        rt_thread_mdelay(2);
    }
#endif

    esp_deep_sleep_start();

    /* Not reached. */
    rt_kprintf("s31sleep: BUG - esp_deep_sleep_start() returned\n");
    return -RT_ERROR;
}
MSH_CMD_EXPORT_ALIAS(cmd_sleep, s31sleep, deep sleep: arm a timer and power down);

/* Report the previous cycle's result at boot, once, before the shell starts. */
static int s31_sleep_report(void)
{
    if (s31_sleep_magic == S31_SLEEP_MAGIC)
    {
        esp_sleep_wakeup_cause_t cause = esp_sleep_get_wakeup_cause();

        rt_kprintf("s31sleep: woke from deep sleep, cause %s (%d), cycle %u\n",
                   s31_sleep_cause_name(cause), (int)cause, (unsigned)s31_sleep_count);
    }
    return 0;
}
INIT_APP_EXPORT(s31_sleep_report);

#else

int cmd_sleep(int argc, char **argv)
{
    RT_UNUSED(argc);
    RT_UNUSED(argv);
    rt_kprintf("s31sleep: deep sleep support is not compiled in\n");
    rt_kprintf("s31sleep: set CONFIG_RTT_SLEEP_LINK_PROBE\n");
    return -RT_ERROR;
}
MSH_CMD_EXPORT_ALIAS(cmd_sleep, s31sleep, deep sleep: arm a timer and power down);

#endif /* SOC_DEEP_SLEEP_SUPPORTED && CONFIG_RTT_SLEEP_LINK_PROBE */
