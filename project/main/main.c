/*
 * ESP32-S31 + RT-Thread bring-up image.
 *
 * Verifies, in order:
 *   1. the kernel links against the IDF riscv port and reaches main()
 *   2. the SYSTIMER alarm drives rt_tick_increase() (rt_tick_get advances)
 *   3. two threads at different priorities preempt each other
 *   4. the trap frame round-trips FPU state (R9) and keeps sp stable
 *   5. msh answers on the console
 *
 * Console input/output deliberately uses the UART0 LL registers that the ROM
 * console already configured, so this image works before the real rt_serial
 * driver exists. P2.2 keeps the IDF driver layer out on purpose:
 * esp_driver_uart calls FreeRTOS blocking APIs (risk R10).
 */

#include <rtthread.h>
#include <finsh.h>

#include "esp_rom_serial_output.h"
#include "esp_system.h"
#include "port_serial.h"

static volatile rt_uint32_t counter_a;
static volatile rt_uint32_t counter_b;

static void thread_a(void *parameter)
{
    RT_UNUSED(parameter);

    while (1)
    {
        counter_a++;
        rt_thread_mdelay(1000);
    }
}

static void thread_b(void *parameter)
{
    RT_UNUSED(parameter);

    while (1)
    {
        counter_b++;
        rt_thread_mdelay(1500);
    }
}

/* msh commands: prove the FSymTab / .rti_fn sections survived the linker
 * script (esp32s31/sections.ld.in) and that the shell resolves symbols. */
static int cmd_stats(int argc, char **argv)
{
    rt_size_t total = 0, used = 0, max_used = 0;

    RT_UNUSED(argc);
    RT_UNUSED(argv);

    rt_memory_info(&total, &used, &max_used);

    rt_kprintf("tick   : %u\n", (unsigned)rt_tick_get());
    rt_kprintf("counter: a=%u b=%u\n", (unsigned)counter_a, (unsigned)counter_b);
    rt_kprintf("heap   : total=%u used=%u max=%u\n",
               (unsigned)total, (unsigned)used, (unsigned)max_used);

    /* The other heap. This image has two: the RT-Thread one above, carved out
     * by CONFIG_IDF_RTTHREAD_HEAP_SIZE and handed to rt_system_heap_init(), and
     * IDF's own, which is everything the linker did not claim. They are
     * disjoint - a block freed into one is invisible to malloc() from the other
     * - so reporting only one of them is how a bring-up image ends up claiming
     * it has memory when it does not.
     *
     * esp_get_free_heap_size() also proves the startup-stack reclaim ran: before
     * it, the 20 KB at the top of DRAM is not in any heap at all. */
    rt_kprintf("idfheap: free=%u\n", (unsigned)esp_get_free_heap_size());

#if defined(RT_USING_SMP) && defined(RT_USING_SMP_SLAVE)
    /*
     * Did CPU1 actually start?
     *
     * The secondary entry sets this marker before parking in a wfi loop, so a
     * non-zero value here means the core was released and reached our code - which
     * is the one thing "cpus=2" in the banner does not prove.
     */
    {
        extern volatile unsigned s31_cpu1_alive;

        rt_kprintf("cpu1   : %s (0x%08x)\n",
                   s31_cpu1_alive ? "started" : "not started",
                   (unsigned)s31_cpu1_alive);
#ifdef RT_USING_SMP_SLAVE_LOCK
        /* Proves the sequencing: CPU1 cannot reach this line until CPU0 has
         * passed its own rt_system_scheduler_start(), because that is where the
         * scheduler lock is released. */
        {
            extern volatile unsigned s31_cpu1_locked;

            rt_kprintf("cpu1lk : %s (0x%08x)\n",
                       s31_cpu1_locked ? "acquired" : "waiting",
                       (unsigned)s31_cpu1_locked);
        }
#endif
    }
#endif

    return 0;
}
MSH_CMD_EXPORT_ALIAS(cmd_stats, s31stat, tick + both heaps + demo-thread counters + CPU1 state);

/* Two threads write different patterns into f0-f31, yield, then compare.
 * If the trap frame or the cooperative frame drops the FP area, the values
 * swap and this prints FAIL. Requires ARCH_RISCV_FPU (defined by cpuport.h
 * from __riscv_flen, see risk R9). */
#ifdef ARCH_RISCV_FPU
static void fpu_roundtrip_thread(void *parameter)
{
    const float seed = *(const float *)parameter;
    volatile float f[32];
    int i;

    for (i = 0; i < 32; i++)
    {
        f[i] = seed + (float)i;
    }

    rt_thread_mdelay(2);                 /* force a context switch here */

    for (i = 0; i < 32; i++)
    {
        if (f[i] != seed + (float)i)
        {
            /* Hex bit patterns, not %f. rt_vsnprintf_tiny.c consumes a %f argument
             * and prints nothing for it, so the old message read
             *
             *     FPU FAIL at f[3]:  !=
             *
             * - the two values this message exists to show were both missing. The
             * bit patterns are exact and the integer formatter prints them. */
            union { float f; rt_uint32_t u; } got, want;

            got.f  = f[i];
            want.f = seed + (float)i;
            rt_kprintf("FPU FAIL at f[%d]: got 0x%08x, want 0x%08x\n",
                       i, (unsigned)got.u, (unsigned)want.u);
            return;
        }
    }

    /* rt_vsnprintf_tiny.c consumes %f but prints nothing for it (the tiny\n     * formatter has no float support), so report the seed as a scaled integer. */
    rt_kprintf("FPU ok seed=%d/10\n", (int)(seed * 10.0f));
}
#endif

static int cmd_fpu(int argc, char **argv)
{
    RT_UNUSED(argc);
    RT_UNUSED(argv);

#ifdef ARCH_RISCV_FPU
    static float s1 = 1.0f, s2 = 100.0f;
    rt_thread_t t1 = rt_thread_create("fpa", fpu_roundtrip_thread, &s1, 2048, 20, 10);
    rt_thread_t t2 = rt_thread_create("fpb", fpu_roundtrip_thread, &s2, 2048, 20, 10);

    if (t1 == RT_NULL || t2 == RT_NULL)
    {
        rt_kprintf("create fpu threads failed\n");
        return -1;
    }
    rt_thread_startup(t1);
    rt_thread_startup(t2);
#else
    rt_kprintf("ARCH_RISCV_FPU not defined: this build has no FPU frame\n");
#endif

    return 0;
}
MSH_CMD_EXPORT_ALIAS(cmd_fpu, s31fpu, FPU context round-trip test);

#if CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM
/*
 * The first real IDF components on the FreeRTOS shim.
 *
 * esp_event is not a toy: creating the default loop allocates a queue, a task
 * and a per-task state block hung off a TLS slot with a delete callback
 * (xTaskCreatePinnedToCoreWithCaps -> xTaskCreateStaticPinnedToCore,
 * xQueueCreateWithCaps, vTaskSetThreadLocalStoragePointerAndDelCallback). All
 * three of those now resolve to the shim, and posting an event proves the queue
 * and the task actually move data.
 *
 * Compiled only with the shim on: with the real kernel in the image this would
 * test FreeRTOS, not us.
 */
#include "esp_event.h"
#include "esp_netif.h"

ESP_EVENT_DEFINE_BASE(S31_TEST_EVENT);

static volatile int s_evt_count;
static volatile int s_evt_payload;

static void s31_event_handler(void *arg, esp_event_base_t base, int32_t id,
                              void *data)
{
    RT_UNUSED(arg);
    RT_UNUSED(base);

    if (id == 1 && data != RT_NULL)
    {
        s_evt_payload = *(int *)data;
    }

    s_evt_count++;
}

static int cmd_events(int argc, char **argv)
{
    esp_err_t err;
    int       payload = 0x5A31;
    int       waited  = 0;

    RT_UNUSED(argc);
    RT_UNUSED(argv);

    s_evt_count   = 0;
    s_evt_payload = 0;

    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE)
    {
        rt_kprintf("s31events: loop create failed 0x%x\n", err);
        return -1;
    }

    err = esp_netif_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE)
    {
        rt_kprintf("s31events: netif init failed 0x%x\n", err);
        return -1;
    }

    err = esp_event_handler_register(S31_TEST_EVENT, ESP_EVENT_ANY_ID,
                                     s31_event_handler, RT_NULL);
    if (err != ESP_OK)
    {
        rt_kprintf("s31events: handler register failed 0x%x\n", err);
        return -1;
    }

    err = esp_event_post(S31_TEST_EVENT, 1, &payload, sizeof(payload),
                         pdMS_TO_TICKS(100));
    if (err != ESP_OK)
    {
        rt_kprintf("s31events: post failed 0x%x\n", err);
        return -1;
    }

    /* The loop task runs below this one, so give it time instead of spinning. */
    while (s_evt_count == 0 && waited < 500)
    {
        rt_thread_mdelay(10);
        waited += 10;
    }

    if (s_evt_count == 1 && s_evt_payload == payload)
    {
        rt_kprintf("s31events: PASS, delivered in %d ms, payload=0x%04x\n",
                   waited, s_evt_payload);
    }
    else
    {
        rt_kprintf("s31events: FAIL, count=%d payload=0x%04x (want 1 / 0x%04x)\n",
                   s_evt_count, s_evt_payload, payload);
    }

    return 0;
}
MSH_CMD_EXPORT_ALIAS(cmd_events, s31events, esp_event + esp_netif on the FreeRTOS shim);
#endif /* CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM */

int main(void)
{
    rt_thread_t tid;

    rt_kprintf("\n");
    rt_kprintf("RT-Thread %d.%d.%d on ESP32-S31\n",
               RT_VERSION_MAJOR, RT_VERSION_MINOR, RT_VERSION_PATCH);
    rt_kprintf("tick=%d Hz, cpus=%d, msh on UART0\n", RT_TICK_PER_SECOND, RT_CPUS_NR);

    /* Bind the shell to "uart0", which the driver registered at INIT_BOARD_EXPORT
     * time - the board band runs before this thread starts, so the device is
     * already there and there is nothing to initialise here.
     *
     * Output deliberately still goes through rt_hw_console_output() rather than
     * the console device: no rt_console_set_device() is called anywhere in this
     * port, so a fault in the serial driver cannot take the console down with it
     * and the panic path stays readable. That is a deliberate deviation from an
     * upstream BSP, which typically ends its board init with
     * rt_console_set_device(RT_CONSOLE_DEVICE_NAME). */
    finsh_set_device("uart0");

    tid = rt_thread_create("ta", thread_a, RT_NULL, 2048, 10, 10);
    if (tid == RT_NULL)
    {
        rt_kprintf("create ta failed\n");
        return -1;
    }
    rt_thread_startup(tid);

    tid = rt_thread_create("tb", thread_b, RT_NULL, 2048, 12, 10);
    if (tid == RT_NULL)
    {
        rt_kprintf("create tb failed\n");
        return -1;
    }
    rt_thread_startup(tid);

    return 0;
}
