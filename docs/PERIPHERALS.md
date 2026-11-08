# 外设支持矩阵（PERIPHERALS.md）

**这张表是权威的。** 每一项都对应代码里的实现和一个可运行的验证方法。

图例：

| 符号 | 含义 |
|---|---|
| ✅ 完成 | 有 RT-Thread 设备接口 + 自检通过 |
| 🟡 部分 | 能跑，但接口或功能不完整 |
| ⬜ 未移植 | 硬件有，没写驱动 |
| ❌ 不支持 | 硬件没有 |

---

## 总览

| 外设 | 状态 | RT-Thread 接口 | 验证命令 |
|---|---|---|---|
| UART0 | ✅ | `rt_serial` | 控制台 |
| GPIO | ✅ | `rt_pin_ops` | `s31pinirq` |
| GPIO 中断 | ✅ | `rt_pin_attach_irq` | `s31test irq` / `nest` |
| I2C ×3 | ✅ | `rt_i2c_bus` | `i2cscan` |
| SPI | 🟡 | 轮询 | `s31spiloop` |
| ADC ×2 | ✅ | `rt_adc_device` | `s31adc` |
| LEDC PWM | ✅ | `rt_pwm_device` | `s31pwm` |
| MCPWM | ✅ | 直连 | `s31mcpwm` |
| GPTimer ×4 | ✅ | 直连 | `s31timer` |
| PCNT | ✅ | `rt_pulse_encoder_device` | `s31pcnt` |
| RMT | ✅ | 直连 | `s31rmt` |
| TWAI / CAN | ✅ | 直连 | `s31twai` |
| SYSTIMER | ✅ | tick 源 | `s31stat` |
| PSRAM | ✅ | `heap_caps_malloc` | `s31psram` |
| RGB LED | ✅ | 直连 | `s31led` |
| Wi-Fi | ✅ | FreeRTOS shim | `s31wifi` |
| BLE | ✅ | FreeRTOS shim | `s31ble` |
| USB OTG | 🟡 | 仅能力探针 | `s31usb` |
| I2S | ⬜ | — | — |
| SD/MMC | ⬜ | — | — |
| LCD / CAM | ⬜ | — | — |
| JPEG / PPA / DMA2D | ⬜ | — | — |
| Touch | ⬜ | — | — |
| 温度传感器 | ⬜ | — | — |
| Ethernet MAC | ⬜ | — | — |
| 硬件加密 | ⬜ | — | — |
| DAC | ❌ | — | S31 无 DAC |
| **deep sleep** | 🟡 | 直接（`esp_sleep`） | `s31sleep` |
| **LP core** | 🟡 | 直接（无 RTOS） | `s31lp` |

---

## 详细说明

### UART

**实现**：`port/esp32s31/serial.c`

UART0 注册为 `rt_serial` 设备 `uart0`，115200 8N1。

**已知限制**：波特率固定 115200。原因是 S31 的 `uart_ll.h` 缺
`uart_ll_get_sclk` / `uart_ll_set_sclk`，无法读当前时钟源频率；
要改需要直接操作 `HP_SYS_CLKRST` 寄存器。

**注意**：`rt_kprintf` 走 ROM putc，**不经过设备层**。
这是有意的——驱动出问题时控制台仍然可用，否则失去唯一调试通道。

### GPIO

**实现**：`port/esp32s31/gpio.c`

完整 `rt_pin_ops`：`pin_mode` / `pin_write` / `pin_read` /
`pin_attach_irq` / `pin_irq_enable` / `pin_irq_disable`。

**已知限制**：只用中断通道 0。`gpio_ll_intr_enable_on_core()` 硬编码
`GPIO_LL_INTR0_ENA`，IDF 自己留了 TODO（IDF-7995）。
通道 0 覆盖 GPIO0..31；超过 32 个独立中断源需要改 IDF。

### I2C

**实现**：`port/esp32s31/i2c.c`

3 条总线注册为 `rt_i2c_bus`。`i2cscan [bus]` 扫地址。

### SPI

**实现**：`port/esp32s31/spi.c`

🟡 **只做了轮询回环**，不是完整的 `rt_spi_bus`。
`s31spiloop` 需要把 MOSI 接到 MISO。

要做完整 SPI 设备接口，需要补 `rt_spi_ops` 的 `configure` / `xfer`。

### ADC

**实现**：`port/esp32s31/adc.c`

两个 SAR ADC 单元，各 8 通道，17 bit 原始码。注册为 `adc1` / `adc2`。

`s31adc <ch>` 读一次；`s31adcdbg` 在转换前后 dump 寄存器（排查用）。

### PWM（LEDC）

**实现**：`port/esp32s31/pwm.c`

注册为 `rt_pwm_device` `pwm0`，8 通道。

`s31pwm <ch> <gpio> <hz> <duty%>` 设置并输出。

**已知问题**：`pwm get 0` 会打印 `Duty cycle : 2147483647%`。
这是上游 `rt_drv_pwm.c` 的除零问题（记录在跑通手册里，未修，不影响功能）。

### MCPWM

**实现**：`port/esp32s31/mcpwm.c`

互补输出 + 死区，带自测量（用 PCNT 或 mcycle 量实际占空比）。

### GPTimer

**实现**：`port/esp32s31/gptimer.c`

4 个 TIMG 通道直连，`s31timer` 量中断频率。
通道 3 留给 CPU1 的 tick（见 `SMP.md`）。

### PCNT

**实现**：`port/esp32s31/pcnt.c`

注册为 `rt_pulse_encoder_device` `pcnt0`。
`s31pcnt` 用软件驱动边沿自测。

### RMT

**实现**：`port/esp32s31/rmt.c`

`s31rmt` 产生方波并用 PCNT + mcycle 双重验证。

### TWAI / CAN

**实现**：`port/esp32s31/twai.c`、`twai_cmd.c`

`s31twai` 在四个速率下自测。

**已知问题**：同一会话里重复跑会失败（0/4），冷启动后正常（4/4）。
原因未查明，已记录在日志里。

### PSRAM

**实现**：`port/esp32s31/board_extra.c`

16 MB Octal PSRAM，200 MHz。`s31psram [KB]` 写位置相关图案再读回。

**为什么 200 MHz**：250 MHz 是器件上限，200 留了余量。
40/100/200/250 四种速率都实测能跑（见 `sdkconfig.defaults` 注释）。

### RGB LED

**实现**：`port/esp32s31/board_extra.c`

板载 SK6805（WS2812 类）在 IO60。位拍方式，关中断 30 µs 发 24 bit。

**为什么不用 RMT**：为一个 LED 引入整套 RMT 抽象不划算；
位拍版本 20 行，错了就是颜色不对，不会静默失效。

### Wi-Fi

**实现**：`project/main/s31wifi.c` + `s31wifiscan.c`

跑在 FreeRTOS 兼容层上。

```
msh > s31wifi 你的SSID 你的密码
s31wifi: PASS, connected to "..." in 1160 ms
s31wifi: ip 10.0.0.119 mask 255.255.255.0 gw 10.0.0.1
```

`s31wifiscan` 列出可见 AP。

**限制**：只支持 2.4 GHz（`SOC_WIFI_SUPPORT_5G` 未定义，S31 是 2.4 GHz Wi-Fi 6）。

**双核注意**：Wi-Fi 与 CPU1 调度共存需要 flash 握手（`SMP.md` §5），
本版本已修好并验证。

### BLE

**实现**：`project/main/s31ble.c`

BLE 控制器 + NimBLE 主机，跑在兼容层上。`s31ble` 启动。

### USB OTG

**状态**：🟡 只有能力探针 `s31usb`。

**芯片能力（读 GHWCFG 寄存器得来）**：

| 项 | 值 |
|---|---|
| 端点 | 16（含 EP0） |
| IN 上限 | 7 |
| 等时端点 | **0** |
| 方向 | 全双向 |
| DMA | 内部 DMA |
| PHY | UTMI+ |

详细分析和移植步骤见 `USB.md`。

### 未移植的外设

这些硬件**存在**，但没有 RT-Thread 驱动：

| 外设 | 说明 | 建议 |
|---|---|---|
| I2S | `SOC_I2S_SUPPORTED` | IDF 有 `esp_driver_i2s`，可参考 |
| SD/MMC | `SOC_SDMMC_HOST_SUPPORTED` | IDF 有 `esp_driver_sdmmc` |
| LCD / CAM | `SOC_LCDCAM_SUPPORTED`，含 I80/RGB | 工作量大 |
| JPEG | `SOC_JPEG_CODEC_SUPPORTED` | 依赖 DMA2D |
| PPA / DMA2D | `SOC_PPA_SUPPORTED` / `SOC_DMA2D_SUPPORTED` | 图形加速 |
| Touch | `SOC_TOUCH_SENSOR_SUPPORTED` v3 | — |
| 温度传感器 | `SOC_TEMP_SENSOR_SUPPORTED` | 简单，十来行 |
| Ethernet MAC | `SOC_EMAC_SUPPORTED`，支持 1000M + PTP | 大工程 |
| 硬件加密 | AES/SHA/MPI/ECDSA/HMAC | 现在走软件 mbedtls |
| ADC DMA / 差分 | `SOC_ADC_DMA_SUPPORTED` | 高性能采样需要 |
| 低功耗 | `SOC_PM_SUPPORTED` / `SOC_DEEP_SLEEP_SUPPORTED` | 与 RT-Thread PM 框架对接 |

| ETM | `SOC_ETM_SUPPORTED` | 事件任务矩阵 |
| 并行 IO | `SOC_PARLIO_SUPPORTED` | — |
| Bitscrambler | `SOC_BITSCRAMBLER_SUPPORTED` | — |
| CORDIC | `SOC_CORDIC_SUPPORTED` | — |
| 模拟比较器 | `SOC_ANA_CMPR_SUPPORTED` | — |

### 硬件没有的

| 外设 | 证据 |
|---|---|
| DAC | `SOC_DAC_SUPPORTED` 未定义 |
| Wi-Fi 5 GHz | `SOC_WIFI_SUPPORT_5G` 未定义 |

---

## 怎么自己验证一个外设

三步：

```powershell
# 1. 编译烧录
.\tools\build.ps1 build
.\tools\build.ps1 -Port COM7 flash

# 2. 跑对应命令
python tools\board.py COM7 "s31adc 3"

# 3. 跑全量自检确认没影响别的
python tools\board.py COM7 "s31test"
```

**判断"通过"的标准**：命令输出里有明确的 `PASS` 或数值，
不是"没报错"。例如 `s31adc` 要给出具体码值，
`s31timer` 要给出实测频率。
