/*
 * `s31ble` - bring up the BLE controller and the NimBLE host on the shim.
 *
 * This is the Bluetooth counterpart of s31wifi: it walks the boot sequence one
 * step at a time and prints the error code of whichever call refused, instead of
 * one opaque PASS/FAIL.
 *
 * DO NOT initialise the controller here when NimBLE is enabled.
 * nimble_port_init() already does the whole controller sequence itself
 * (components/bt/host/nimble/nimble/porting/nimble/src/nimble_port.c:288-320:
 * esp_bt_controller_init + esp_bt_controller_enable, with a deinit on failure).
 * Driving it by hand first makes that second init return
 * ESP_ERR_INVALID_STATE ("BTDM controller init failed" / 0x103), which is exactly
 * what the first version of this command did.
 *
 * So the two paths are:
 *
 *   NimBLE host:   nvs_flash_init -> nimble_port_init()      (controller + host)
 *                  -> nimble_port_freertos_init()            (host event task)
 *                  -> ble_hs sync callback                   (address read-back)
 *
 *   controller only: mem_release -> esp_bt_controller_init -> enable
 *
 * nvs_flash_init comes first because the PHY reads its RF calibration from NVS:
 * without it the log says "NVS has not been initialized. Call nvs_flash_init
 * before starting WiFi/BT" and the PHY falls back to a full calibration. That
 * works but is slower, and it cannot persist the result.
 */

#include <rtthread.h>

#include "sdkconfig.h"

#if CONFIG_BT_ENABLED

#include "esp_err.h"
#include "esp_bt.h"
#include "nvs_flash.h"

#if CONFIG_BT_NIMBLE_ENABLED
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#endif

#define S31_BLE_WAIT_MS     3000

static int s31_step(const char *what, esp_err_t err)
{
    if (err == ESP_OK)
    {
        rt_kprintf("  %-28s ok\n", what);
        return 0;
    }

    rt_kprintf("  %-28s FAILED 0x%x (%s)\n", what, err, esp_err_to_name(err));
    return -1;
}

#if CONFIG_BT_NIMBLE_ENABLED

static volatile int s_ble_synced;
static volatile int s_ble_sync_rc;

static void s31_ble_on_sync(void)
{
    /* Runs in the NimBLE host task once the HCI link is up. Inferring the
     * address here is the documented "the host is really talking to the
     * controller" check: it reads the controller's own identity address, so a
     * dead HCI transport shows up as a failure here rather than as silence. */
    ble_addr_t addr;

    s_ble_sync_rc = ble_hs_id_infer_auto(0, &addr.type);
    if (s_ble_sync_rc == 0)
    {
        s_ble_sync_rc = ble_hs_id_copy_addr(addr.type, addr.val, RT_NULL);
    }

    s_ble_synced = 1;
}

static void s31_ble_on_reset(int reason)
{
    rt_kprintf("s31ble: host reset, reason=%d\n", reason);
}

/*
 * The NimBLE host task body.
 *
 * nimble_port_freertos_init() takes the *function to run*, not a parameter -
 * it passes it straight to xTaskCreatePinnedToCore. The first version of this
 * command passed NULL, and ft_task_create() correctly refused to create a task
 * with no entry point, so the host thread never existed and the sync callback
 * never fired: `s31ble` reported "host never synced" and `ps` showed no
 * nimble_host. That was a bug in this file, not in the port - and a useful
 * confirmation that the shim's NULL-entry check works.
 */
static void s31_ble_host_task(void *param)
{
    RT_UNUSED(param);

    nimble_port_run();

    /* Only reached after nimble_port_stop(); keeps the thread from returning
     * into a scheduler that still has it listed. */
    nimble_port_freertos_deinit();
}

#endif /* CONFIG_BT_NIMBLE_ENABLED */

static int cmd_ble(int argc, char **argv)
{
    int waited;

    RT_UNUSED(argc);
    RT_UNUSED(argv);

    rt_kprintf("s31ble: bringing up the BLE controller on the FreeRTOS shim\n");

    /* PHY calibration lives in NVS. Not fatal - the PHY falls back to a full
     * calibration - but reported, because the fallback is slow and cannot be
     * persisted. */
    if (s31_step("nvs_flash_init", nvs_flash_init()) != 0)
    {
        rt_kprintf("  (continuing: PHY will do a full calibration)\n");
    }

#if CONFIG_BT_NIMBLE_ENABLED

    /*
     * nimble_port_init() initialises AND enables the controller itself. Calling
     * esp_bt_controller_init() before it makes that second init return
     * ESP_ERR_INVALID_STATE (0x103), which is what the first version of this
     * command hit: "BLE_INIT: controller init failed" immediately after the
     * controller had already come up.
     */
    s_ble_synced  = 0;
    s_ble_sync_rc = -1;

    /* The callbacks have to be installed before the host task starts, or the
     * sync event fires into a NULL pointer. */
    ble_hs_cfg.sync_cb  = s31_ble_on_sync;
    ble_hs_cfg.reset_cb = s31_ble_on_reset;

    if (s31_step("nimble_port_init", nimble_port_init()) != 0)
    {
        return -1;
    }

    nimble_port_freertos_init(s31_ble_host_task);
    rt_kprintf("  %-28s ok\n", "nimble_port_freertos_init");

    waited = 0;
    while (s_ble_synced == 0 && waited < S31_BLE_WAIT_MS)
    {
        rt_thread_mdelay(10);
        waited += 10;
    }

    if (s_ble_synced == 0)
    {
        rt_kprintf("s31ble: FAIL, host never synced within %d ms "
                   "(no HCI response from the controller)\n", S31_BLE_WAIT_MS);
        return -1;
    }

    if (s_ble_sync_rc != 0)
    {
        rt_kprintf("s31ble: FAIL, host synced but the address read failed (%d)\n",
                   s_ble_sync_rc);
        return -1;
    }

    {
        uint8_t addr[6] = {0};

        (void)ble_hs_id_copy_addr(BLE_ADDR_PUBLIC, addr, RT_NULL);

        rt_kprintf("s31ble: PASS, host synced in %d ms, addr %02x:%02x:%02x:%02x:%02x:%02x\n",
                   waited, addr[5], addr[4], addr[3], addr[2], addr[1], addr[0]);
    }

    rt_kprintf("s31ble: controller up, NimBLE host running, not advertising\n");
    return 0;
#else
    {
        esp_bt_controller_config_t cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();

        /* BLE-only builds: the BR/EDR pools are never allocated, because
         * CONFIG_BT_CTRL_BREDR_ENABLE=n keeps libbredr_app.a out of the image. */
        (void)s31_step("mem_release (BR/EDR pools)",
                       esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT));

        if (s31_step("esp_bt_controller_init", esp_bt_controller_init(&cfg)) != 0)
        {
            return -1;
        }

        if (s31_step("esp_bt_controller_enable(BLE)",
                     esp_bt_controller_enable(ESP_BT_MODE_BLE)) != 0)
        {
            return -1;
        }

        rt_kprintf("s31ble: PASS, controller only (CONFIG_BT_NIMBLE_ENABLED is off)\n");
        return 0;
    }
#endif
}
MSH_CMD_EXPORT_ALIAS(cmd_ble, s31ble, bring up BLE controller and NimBLE host);

#else /* !CONFIG_BT_ENABLED */

static int cmd_ble(int argc, char **argv)
{
    RT_UNUSED(argc);
    RT_UNUSED(argv);

    rt_kprintf("s31ble: needs CONFIG_BT_ENABLED.\n");
    rt_kprintf("Enable it in sdkconfig.defaults (CONFIG_BT_ENABLED=y,\n");
    rt_kprintf("CONFIG_BT_NIMBLE_ENABLED=y, CONFIG_BT_CONTROLLER_ENABLED=y).\n");
    return -1;
}
MSH_CMD_EXPORT_ALIAS(cmd_ble, s31ble, bring up BLE controller and NimBLE host);

#endif /* CONFIG_BT_ENABLED */
