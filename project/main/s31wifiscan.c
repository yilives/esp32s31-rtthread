/*
 * `s31wifiscan` - list what the board can actually see.
 *
 * Why this exists separately from s31wifi
 * ---------------------------------------
 * s31wifi brings the stack up and reports STA_START; it never scans. That is
 * enough to prove the OS layer works, but not enough to answer the question that
 * actually blocks M6: can this board see the network we want it to join?
 *
 * ESP32-S31 is a 2.4 GHz part. `SOC_WIFI_SUPPORT_5G` is defined for esp32c5 only
 * (components/soc/esp32c5/include/soc/soc_caps.h), not for esp32s31, and the
 * board guide says "2.4GHz Wi-Fi 6" in two places. So a 5 GHz-only AP is not
 * "hard to connect to" - it is invisible at the radio level, and no amount of
 * credential tweaking will change that. This command makes that measurable
 * instead of argued.
 *
 * The scan also reports the channel, which is what distinguishes the two bands in
 * practice: 1..14 is 2.4 GHz, 36..177 is 5 GHz. If the list contains only
 * channels in the low range, the radio is doing exactly what its caps say.
 */

#include <rtthread.h>

#include "sdkconfig.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#if CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM

#define S31_SCAN_MAX_AP   20
#define S31_SCAN_WAIT_MS  8000

static volatile int s_scan_done;

static void s31_scan_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    RT_UNUSED(arg);
    RT_UNUSED(data);

    if (base == WIFI_EVENT && id == WIFI_EVENT_SCAN_DONE)
    {
        s_scan_done = 1;
    }
}

static int cmd_wifiscan(int argc, char **argv)
{
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    wifi_scan_config_t scan;
    wifi_ap_record_t  *recs;
    uint16_t           count = 0;
    int                waited = 0;
    uint16_t           i;

    (void)argc;
    (void)argv;

    s_scan_done = 0;

    /* Bring the stack up. Every call is idempotent-safe: the same sequence runs in
     * s31wifi, and re-running it here means this command works whether or not
     * s31wifi ran first. */
    if (nvs_flash_init() != ESP_OK)
    {
        rt_kprintf("s31wifiscan: nvs_flash_init failed (continuing)\n");
    }
    if (esp_netif_init() != ESP_OK)
    {
        rt_kprintf("s31wifiscan: esp_netif_init failed\n");
        return -1;
    }
    /* ESP_ERR_INVALID_STATE just means the loop already exists, which is fine when
     * s31wifi ran first. Anything else would surface at the handler registration
     * below, which is where it actually matters. */
    (void)esp_event_loop_create_default();
    esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_SCAN_DONE, s31_scan_event, RT_NULL);

    if (esp_wifi_init(&cfg) != ESP_OK)
    {
        rt_kprintf("s31wifiscan: esp_wifi_init failed\n");
        return -1;
    }
    if (esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK)
    {
        rt_kprintf("s31wifiscan: set_mode failed\n");
        return -1;
    }
    if (esp_wifi_start() != ESP_OK)
    {
        rt_kprintf("s31wifiscan: esp_wifi_start failed\n");
        return -1;
    }

    /* The station has to be started before a scan is accepted. */
    while (!s_scan_done && waited < 2000)
    {
        rt_thread_mdelay(10);
        waited += 10;
    }

    rt_memset(&scan, 0, sizeof(scan));
    scan.show_hidden = 1;
    scan.scan_type   = WIFI_SCAN_TYPE_ACTIVE;

    rt_kprintf("s31wifiscan: scanning (2.4 GHz only - the S31 has no 5 GHz radio)\n");

    s_scan_done = 0;
    if (esp_wifi_scan_start(&scan, false) != ESP_OK)
    {
        rt_kprintf("s31wifiscan: scan_start failed\n");
        return -1;
    }

    waited = 0;
    while (!s_scan_done && waited < S31_SCAN_WAIT_MS)
    {
        rt_thread_mdelay(20);
        waited += 20;
    }

    if (!s_scan_done)
    {
        rt_kprintf("s31wifiscan: FAIL, no WIFI_EVENT_SCAN_DONE within %d ms\n",
                   S31_SCAN_WAIT_MS);
        return -1;
    }

    if (esp_wifi_scan_get_ap_num(&count) != ESP_OK)
    {
        rt_kprintf("s31wifiscan: get_ap_num failed\n");
        return -1;
    }

    if (count > S31_SCAN_MAX_AP)
    {
        count = S31_SCAN_MAX_AP;
    }

    recs = (wifi_ap_record_t *)rt_malloc(sizeof(wifi_ap_record_t) * count);
    if (recs == RT_NULL)
    {
        rt_kprintf("s31wifiscan: no memory for %d records\n", (int)count);
        return -1;
    }

    if (esp_wifi_scan_get_ap_records(&count, recs) != ESP_OK)
    {
        rt_kprintf("s31wifiscan: get_ap_records failed\n");
        rt_free(recs);
        return -1;
    }

    rt_kprintf("\n  # ch  rssi  bssid              ssid\n");
    rt_kprintf("  - --  ----  -----------------  ----\n");
    for (i = 0; i < count; i++)
    {
        rt_kprintf("  %d %2d  %4d  %02x:%02x:%02x:%02x:%02x:%02x  %s\n",
                   (int)i + 1, (int)recs[i].primary, (int)recs[i].rssi,
                   recs[i].bssid[0], recs[i].bssid[1], recs[i].bssid[2],
                   recs[i].bssid[3], recs[i].bssid[4], recs[i].bssid[5],
                   (const char *)recs[i].ssid);
    }

    rt_free(recs);

    rt_kprintf("\ns31wifiscan: %d AP(s), all on channels 1..14 = the 2.4 GHz band\n",
               (int)count);
    if (count == 0)
    {
        rt_kprintf("  Nothing heard. If the AP you want is 5 GHz-only, this board\n");
        rt_kprintf("  can never see it: SOC_WIFI_SUPPORT_5G is not defined for S31.\n");
    }

    return 0;
}
MSH_CMD_EXPORT_ALIAS(cmd_wifiscan, s31wifiscan, list visible 2.4 GHz APs);

#else /* !CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM */

static int cmd_wifiscan(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    rt_kprintf("s31wifiscan: needs CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM\n");
    return -1;
}
MSH_CMD_EXPORT_ALIAS(cmd_wifiscan, s31wifiscan, list visible 2.4 GHz APs);

#endif
