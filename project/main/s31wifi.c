/*
 * `s31wifi` - bring up esp_wifi on the FreeRTOS shim.
 *
 * This is the first thing in the image that makes the shim do real work under a
 * real driver rather than a unit test. esp_wifi_init() ends up in
 * esp_adapter.c, whose wifi_osi_funcs_t has 128 entries: it creates a task
 * (xTaskCreatePinnedToCore), a counting semaphore, mutexes, queues, an event
 * group, arms esp_timer callbacks, and reads the MAC through efuse. Every one of
 * those lands on ft_* or on esp_timer, and if any of them is wrong the failure is
 * not a compile error - it is a hang or a silent no-op.
 *
 * So the command walks the sequence one step at a time and prints the error code
 * of whichever call refused, instead of a single "PASS/FAIL".
 * WIFI_EVENT_STA_START is the interesting one: the wifi task posts it after it
 * has come up and initialised the PHY, so receiving it means a thread created
 * through the shim actually ran, blocked on a queue, and woke up with data.
 *
 * The command itself is compiled unconditionally, because main/CMakeLists.txt
 * needs `-u cmd_wifi` to keep this object out of the archive (nothing else
 * references it, and the msh command table is section-based). Only the body is
 * conditional: with the shim off esp_wifi would be running on the real FreeRTOS
 * scheduler stacked on top of RT-Thread, which is not a configuration this port
 * supports, so the command says so instead of trying.
 */

#include <rtthread.h>

#include "sdkconfig.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_wifi_default.h"
#include "esp_netif_ip_addr.h"
#include "nvs_flash.h"

#define S31_WIFI_WAIT_MS    5000
#define S31_WIFI_CONNECT_MS 15000

#if CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM

static volatile int s_wifi_started;
static volatile int s_wifi_connected;
static volatile int s_wifi_got_ip;
static volatile int s_wifi_fail_reason;
static esp_netif_ip_info_t s_wifi_ipinfo;

/* How many times to retry after a disconnect. The stack itself retries internally for
 * some reasons (AUTH_EXPIRE etc.); this counter is for the command's own budget, so a
 * wrong password fails in a few seconds instead of hanging for the full timeout. */
#define S31_WIFI_MAX_RETRY 3
static volatile int s_wifi_retries;

static void s31_wifi_event(void *arg, esp_event_base_t base, int32_t id,
                           void *data)
{
    RT_UNUSED(arg);

    if (base == WIFI_EVENT)
    {
        switch (id)
        {
        case WIFI_EVENT_STA_START:
            s_wifi_started = 1;
            break;

        case WIFI_EVENT_STA_CONNECTED:
            s_wifi_connected = 1;
            break;

        case WIFI_EVENT_STA_DISCONNECTED:
        {
            /* `data` is a wifi_event_sta_disconnected_t; the reason code is the only
             * part worth keeping, and it is what distinguishes "wrong password" from
             * "AP not in range". */
            const wifi_event_sta_disconnected_t *d =
                (const wifi_event_sta_disconnected_t *)data;

            s_wifi_connected = 0;
            s_wifi_got_ip    = 0;
            s_wifi_fail_reason = d ? d->reason : -1;

            if (s_wifi_retries < S31_WIFI_MAX_RETRY)
            {
                s_wifi_retries++;
                esp_wifi_connect();
            }
            break;
        }

        default:
            break;
        }
    }
    else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP)
    {
        const ip_event_got_ip_t *e = (const ip_event_got_ip_t *)data;

        if (e)
        {
            /* Keep ip, netmask and gateway: the command prints all three, and copying
             * them here is the only place they exist - the event's `data` pointer is
             * only valid for the duration of this call. */
            s_wifi_ipinfo = e->ip_info;
            s_wifi_got_ip = 1;
        }
    }
}

/* Two variants on purpose.
 *
 * s31_step() is the strict one: anything but ESP_OK fails the command. It used
 * to accept ESP_ERR_INVALID_STATE as "already done" for every call, which is
 * exactly the kind of blanket tolerance that hides a real bug - see the
 * esp_event_handler_register() note below, where INVALID_STATE meant "there is
 * no default loop, so nothing was registered" and the command still printed ok.
 *
 * s31_step_idem() is for the calls that genuinely are idempotent and return
 * ESP_ERR_INVALID_STATE when this command has already run once (nvs_flash_init,
 * esp_netif_init, esp_wifi_init). Those are reported as "already done" and are
 * allowed; each one is listed explicitly at its call site. */
static int s31_step(const char *what, esp_err_t err)
{
    if (err == ESP_OK)
    {
        rt_kprintf("  %-26s ok\n", what);
        return 0;
    }

    rt_kprintf("  %-26s FAILED 0x%x (%s)\n", what, err, esp_err_to_name(err));
    return -1;
}

static int s31_step_idem(const char *what, esp_err_t err)
{
    if (err == ESP_OK)
    {
        rt_kprintf("  %-26s ok\n", what);
        return 0;
    }

    if (err == ESP_ERR_INVALID_STATE)
    {
        rt_kprintf("  %-26s ok (already done)\n", what);
        return 0;
    }

    rt_kprintf("  %-26s FAILED 0x%x (%s)\n", what, err, esp_err_to_name(err));
    return -1;
}

#endif /* CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM */

static int cmd_wifi(int argc, char **argv)
{
#if CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    wifi_config_t      conf;
    esp_netif_t       *netif;
    uint8_t            mac[6];
    int                waited = 0;
    const char        *ssid = RT_NULL;
    const char        *pass = RT_NULL;
    int                connect_mode;

    if (argc >= 3)
    {
        ssid = argv[1];
        pass = argv[2];
    }
    else if (argc == 2)
    {
        ssid = argv[1];
        pass = "";
    }

    connect_mode = (ssid != RT_NULL);

    if (rt_strlen(ssid ? ssid : "") > sizeof(conf.sta.ssid) ||
        rt_strlen(pass ? pass : "") > sizeof(conf.sta.password))
    {
        rt_kprintf("s31wifi: ssid up to %u bytes, password up to %u\n",
                   (unsigned)sizeof(conf.sta.ssid),
                   (unsigned)sizeof(conf.sta.password));
        return -1;
    }

    s_wifi_started   = 0;
    s_wifi_connected = 0;
    s_wifi_got_ip    = 0;
    s_wifi_fail_reason = 0;
    s_wifi_retries   = 0;
    rt_memset(&s_wifi_ipinfo, 0, sizeof(s_wifi_ipinfo));

    rt_kprintf("s31wifi: bringing up esp_wifi on the FreeRTOS shim\n");

    /* NVS before anything else. The PHY reads its RF calibration data from the
     * "phy" namespace and, without it, falls back to a full calibration - which
     * works but takes noticeably longer and cannot persist the result. It logs
     * "NVS has not been initialized. Call nvs_flash_init before starting
     * WiFi/BT." and carries on, so this is not fatal, only wasteful. */
    if (s31_step_idem("nvs_flash_init", nvs_flash_init()) != 0)
    {
        rt_kprintf("  (continuing: PHY will do a full calibration)\n");
    }

    /* netif next: esp_netif_create_default_wifi_sta() has to exist before the
     * wifi task starts, otherwise the interface attach in esp_wifi_start() has
     * nowhere to go. */
    if (s31_step_idem("esp_netif_init", esp_netif_init()) != 0)
    {
        return -1;
    }

    /* The default event loop, and it has to come before the handler
     * registration, not after.
     *
     * esp_event_handler_register() is a thin wrapper over the module-static
     * s_default_loop and returns ESP_ERR_INVALID_STATE when that pointer is
     * still NULL (default_event_loop.c:20). The first version of this command
     * registered the handler first and created the loop never - so the
     * registration "succeeded" only because ESP_ERR_INVALID_STATE was being
     * accepted as a benign code, and WIFI_EVENT_STA_START had nowhere to be
     * delivered. The command would have waited out its full timeout on real
     * hardware and blamed the wifi task.
     *
     * esp_wifi also posts its events onto this loop, so without it the wifi task
     * has no consumer for STA_START regardless of how healthy it is. */
    if (s31_step_idem("event loop create default",
                      esp_event_loop_create_default()) != 0)
    {
        return -1;
    }

    netif = esp_netif_create_default_wifi_sta();
    if (netif == RT_NULL)
    {
        rt_kprintf("  %-26s FAILED\n", "create default wifi sta");
        return -1;
    }
    rt_kprintf("  %-26s ok\n", "create default wifi sta");

    /* Strict now: with the loop in place there is no benign reason for
     * INVALID_STATE here. */
    if (s31_step("event handler register",
                 esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                            s31_wifi_event, RT_NULL)) != 0)
    {
        return -1;
    }

    /* IP_EVENT is a separate base with its own loop subscription. Registering only
     * WIFI_EVENT is the classic mistake: the connection succeeds, GOT_IP is never
     * delivered, and the command reports a timeout on a link that is actually up. */
    if (s31_step("ip handler register",
                 esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                            s31_wifi_event, RT_NULL)) != 0)
    {
        return -1;
    }

    /* This is the call that exercises the OS layer: it starts the wifi task and
     * the timer task, and allocates its queues through wifi_create_queue(). */
    if (s31_step_idem("esp_wifi_init", esp_wifi_init(&cfg)) != 0)
    {
        return -1;
    }

    if (s31_step("esp_wifi_set_mode", esp_wifi_set_mode(WIFI_MODE_STA)) != 0)
    {
        return -1;
    }

    rt_memset(&conf, 0, sizeof(conf));

    if (connect_mode)
    {
        /* Credentials come from the command line and are NOT persisted: a reboot
         * forgets them. There is no provisioning path in this image, and writing a
         * password into NVS behind the user's back would be worse than not storing it.
         *
         * threshold.authmode: WPA2_PSK for a non-empty password, OPEN otherwise. An
         * empty password with WPA2_PSK set makes the stack fail AUTH_EXPIRE against an
         * open AP, which reads like a wrong password. */
        rt_strncpy((char *)conf.sta.ssid, ssid, sizeof(conf.sta.ssid) - 1);
        rt_strncpy((char *)conf.sta.password, pass, sizeof(conf.sta.password) - 1);
        conf.sta.threshold.authmode =
            (pass[0] != '\0') ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
        conf.sta.pmf_cfg.capable = true;

        rt_kprintf("  connecting to \"%s\" (%s)\n", ssid,
                   (pass[0] != '\0') ? "WPA2-PSK" : "open");
    }

    if (s31_step("esp_wifi_set_config",
                 esp_wifi_set_config(WIFI_IF_STA, &conf)) != 0)
    {
        return -1;
    }

    if (s31_step("esp_wifi_start", esp_wifi_start()) != 0)
    {
        return -1;
    }

    /* The wifi task posts WIFI_EVENT_STA_START once it is actually running. */
    while (s_wifi_started == 0 && waited < S31_WIFI_WAIT_MS)
    {
        rt_thread_mdelay(10);
        waited += 10;
    }

    if (s_wifi_started == 0)
    {
        rt_kprintf("s31wifi: FAIL, no WIFI_EVENT_STA_START within %d ms\n",
                   S31_WIFI_WAIT_MS);
        return -1;
    }

    if (esp_wifi_get_mac(WIFI_IF_STA, mac) != ESP_OK)
    {
        rt_kprintf("s31wifi: started in %d ms, but MAC read failed\n", waited);
        return -1;
    }

    rt_kprintf("s31wifi: STA_START in %d ms, mac %02x:%02x:%02x:%02x:%02x:%02x\n",
               waited, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    if (!connect_mode)
    {
        /* No credentials given: bring-up only. Say exactly what was and was not
         * proven, so this cannot be mistaken for a working link. */
        rt_kprintf("s31wifi: PASS (interface up, not connected - pass <ssid> <pass> "
                   "to connect)\n");
        return 0;
    }

    /* Connect *after* STA_START. esp_wifi_connect() before the event returns
     * ESP_ERR_WIFI_NOT_STARTED, which is why this is not issued earlier. */
    if (s31_step("esp_wifi_connect", esp_wifi_connect()) != 0)
    {
        return -1;
    }

    waited = 0;
    while (s_wifi_got_ip == 0 && waited < S31_WIFI_CONNECT_MS)
    {
        rt_thread_mdelay(20);
        waited += 20;

        /* Stop early once the retry budget is spent: retrying a wrong password only
         * makes the failure slower to report. */
        if (s_wifi_retries >= S31_WIFI_MAX_RETRY &&
            s_wifi_connected == 0 && s_wifi_got_ip == 0)
        {
            break;
        }
    }

    if (s_wifi_got_ip == 0)
    {
        rt_kprintf("s31wifi: FAIL after %d ms: connected=%d, retries=%d, "
                   "last reason=%d\n",
                   waited, s_wifi_connected, s_wifi_retries, s_wifi_fail_reason);
        rt_kprintf("s31wifi: reason 15 = 4WAY_HANDSHAKE_TIMEOUT, 205 = AUTH_FAIL, "
                   "both mean a wrong password;\n");
        rt_kprintf("s31wifi: 201 = NO_AP_FOUND means the SSID is not visible "
                   "(try s31wifiscan).\n");
        return -1;
    }

    rt_kprintf("s31wifi: PASS, connected to \"%s\" in %d ms\n", ssid, waited);
    rt_kprintf("s31wifi: ip " IPSTR " mask " IPSTR " gw " IPSTR "\n",
               IP2STR(&s_wifi_ipinfo.ip),
               IP2STR(&s_wifi_ipinfo.netmask),
               IP2STR(&s_wifi_ipinfo.gw));

    return 0;

#else /* !CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM */

    RT_UNUSED(argc);
    RT_UNUSED(argv);

    rt_kprintf("s31wifi: needs CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM.\n");
    rt_kprintf("With the shim off, esp_wifi would run on the real FreeRTOS\n");
    rt_kprintf("scheduler stacked on RT-Thread, which this port does not support.\n");
    return -1;

#endif
}
MSH_CMD_EXPORT_ALIAS(cmd_wifi, s31wifi, bring up esp_wifi on the FreeRTOS shim);
