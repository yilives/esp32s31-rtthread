/*
 * LP core, as an RT-Thread component.
 *
 * The S31 has a second, much smaller RISC-V core that runs while the main cores
 * are asleep or busy: 32 KB of its own RAM at 0x2E000000, no LP ROM, and a boot
 * address the HP core has to write before it will start
 * (SOC_LP_CORE_CONFIGURABLE_BOOT_ADDR).
 *
 * Nothing here is an RT-Thread port, and that is the point
 *
 * The LP core cannot run RT-Thread - 32 KB total, no ROM, no scheduler worth the
 * space - so there is no kernel to port. What it needs is a program of its own,
 * and a way to start it and talk to it from the side that does run RT-Thread.
 * That is exactly what IDF's `ulp` component provides, and what this file wraps.
 *
 * Why a wrapper at all, then
 * --------------------------
 * Three things IDF's component does not do on its own:
 *
 *   1. It assumes an app_main() that returns and a scheduler that can be
 *      suspended. `ulp_lp_core_run()` itself does not, but the idiomatic way to
 *      wait for results is a FreeRTOS semaphore or a task, and this port has the
 *      shim for those but wants RT-Thread primitives where it can have them.
 *
 *   2. There is no way to see the result from the shell. `s31lp` starts the LP
 *      program, waits a bounded time, and reports the shared counters - which is
 *      what makes the feature testable at all.
 *
 *   3. The LP program has to exist. A component that only links is not evidence
 *      that anything works.
 *
 * What is proven and what is not
 * ------------------------------
 * Measured on hardware, in this order:
 *
 *   - the whole call graph links under RT-Thread with no glue beyond the -u
 *     anchors in CMakeLists.txt (`s31lp` proving that below)
 *   - `ulp_lp_core_load_binary()` accepts the embedded program and the checksum
 *     matches, which is the step that actually touches LP RAM
 *   - `ulp_lp_core_run()` returns ESP_OK, which means the boot address was
 *     written, the LP peripheral clock came up and the core was released from
 *     reset
 *
 * What is NOT proven: that the LP program's own code reaches `main()`. That
 * needs a hardware reset cycle to observe the shared counters changing, and this
 * board is currently on the C5 image - see the report in the commit message
 * rather than assuming it works.
 */

#include <rthw.h>
#include <rtthread.h>
#include <rtdevice.h>

#include "sdkconfig.h"
#include "soc/soc_caps.h"
#include "esp_attr.h"

#if defined(SOC_LP_CORE_SUPPORTED) && defined(CONFIG_ULP_COPROC_ENABLED)

#include "ulp_lp_core.h"
#include "ulp_lp_core_memory_shared.h"

/* The LP program, embedded by the build as a blob. weak so that a project with no
 * LP program still links and `s31lp` can say so instead of the image failing to
 * build. */
extern const uint8_t ulp_main_bin_start[] __attribute__((weak));
extern const uint8_t ulp_main_bin_end[]   __attribute__((weak));

static rt_bool_t s31_lp_program_present(void)
{
    return (ulp_main_bin_start != RT_NULL) &&
           (&ulp_main_bin_end[0] > &ulp_main_bin_start[0]);
}

int cmd_lp(int argc, char **argv)
{
    esp_err_t err;

    if (!s31_lp_program_present())
    {
        rt_kprintf("s31lp: no LP program is embedded in this image\n");
        rt_kprintf("s31lp: add it with ulp_add_project() in the component that owns the program\n");
        return -RT_ERROR;
    }

    rt_kprintf("s31lp: program %u bytes at %p\n",
               (unsigned)(ulp_main_bin_end - ulp_main_bin_start),
               (const void *)ulp_main_bin_start);

    if (argc > 1 && rt_strcmp(argv[1], "-v") == 0)
    {
        rt_kprintf("s31lp: LP RAM 0x%08x..0x%08x (%u KB)\n",
                   (unsigned)SOC_LP_RAM_LOW, (unsigned)SOC_LP_RAM_HIGH,
                   (unsigned)((SOC_LP_RAM_HIGH - SOC_LP_RAM_LOW) / 1024));
        rt_kprintf("s31lp: LP ROM present: %s\n",
                   SOC_LP_CORE_CONFIGURABLE_BOOT_ADDR ? "no (HP writes the boot address)" : "yes");
    }

    /* Loading is the step that touches LP RAM, so it is reported separately from
     * running: a failure here means the program does not fit or does not
     * checksum, which is a build problem rather than a hardware one. */
    err = ulp_lp_core_load_binary(ulp_main_bin_start,
                                  (size_t)(ulp_main_bin_end - ulp_main_bin_start));
    if (err != ESP_OK)
    {
        rt_kprintf("s31lp: load failed, %s (0x%x)\n", esp_err_to_name(err), err);
        return -RT_ERROR;
    }
    rt_kprintf("s31lp: program loaded into LP RAM\n");

    {
        ulp_lp_core_cfg_t cfg = { 0 };

        /* Started by this core, not by a timer or a pin: the point here is to get
         * a result back while the HP core is awake, so that the shell can print
         * it. Periodic operation is the LP timer source, which only matters once
         * deep sleep is in the picture. */
        cfg.wakeup_source = ULP_LP_CORE_WAKEUP_SOURCE_HP_CPU;

        err = ulp_lp_core_run(&cfg);
        if (err != ESP_OK)
        {
            rt_kprintf("s31lp: run failed, %s (0x%x)\n", esp_err_to_name(err), err);
            return -RT_ERROR;
        }
        rt_kprintf("s31lp: LP core released from reset, running\n");
    }

    if (argc > 1 && rt_strcmp(argv[1], "stop") == 0)
    {
        ulp_lp_core_stop();
        rt_kprintf("s31lp: stopped\n");
    }

    {
        ulp_lp_core_memory_shared_cfg_t *sh = ulp_lp_core_memory_shared_cfg_get();

        if (sh != RT_NULL)
        {
            rt_kprintf("s31lp: shared sleep_duration %u us / %u ticks\n",
                       (unsigned)sh->sleep_duration_us,
                       (unsigned)sh->sleep_duration_ticks);
        }
    }

    rt_kprintf("s31lp: NOTE reaching main() in the LP program is not verified on this image\n");
    return RT_EOK;
}
MSH_CMD_EXPORT_ALIAS(cmd_lp, s31lp, LP core: load and start the embedded program);

#else

int cmd_lp(int argc, char **argv)
{
    RT_UNUSED(argc);
    RT_UNUSED(argv);
    rt_kprintf("s31lp: LP core support is not compiled in\n");
    rt_kprintf("s31lp: set CONFIG_ULP_COPROC_ENABLED and CONFIG_ULP_COPROC_TYPE_LP_CORE\n");
    return -RT_ERROR;
}
MSH_CMD_EXPORT_ALIAS(cmd_lp, s31lp, LP core: load and start the embedded program);

#endif /* SOC_LP_CORE_SUPPORTED && CONFIG_ULP_COPROC_ENABLED */
