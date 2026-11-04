# RT-Thread on ESP32-S31

[![RT-Thread](https://img.shields.io/badge/RT--Thread-5.4.0-blue)]()
[![ESP-IDF](https://img.shields.io/badge/ESP--IDF-6.1-red)]()
[![Target](https://img.shields.io/badge/target-ESP32--S31-green)]()
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)]()

把 **RT-Thread 5.4.0** 移植到 **ESP32-S31**（双核 RV32、CLIC、320 MHz、Wi-Fi 6 + BLE、
16 MB flash + 16 MB PSRAM），以 **ESP-IDF v6.1 组件**的形式存在，替换掉 FreeRTOS 内核。

**上板实测：36 项自检全过，双核都在跑线程，Wi-Fi 能在双核开启时连接并拿到 IP。**

---

## 这个仓库是什么

一个**只含移植代码**的仓库。不包含 ESP-IDF 源码，也不包含 RT-Thread 源码——
那两者按下面的方式引入，所以升级它们不需要改这个仓库。

```
s31-rtt-rtos/
├── component/              ← RT-Thread 组件，drop-in 到 esp-idf/components/rtthread/
│   ├── CMakeLists.txt
│   ├── linker.lf
│   ├── rtthread_stack_init.c
│   └── port/
│       ├── common/         与目标无关的 glue
│       ├── esp32s31/       ★ 本移植的原创部分（43 个文件）
│       ├── esp32c3/        其他目标
│       ├── esp32c5/
│       ├── esp32c6/
│       └── freertos/       FreeRTOS API 兼容层
├── patches/                ← 对 ESP-IDF 自身组件的挂钩（git apply）
│   └── 0001-idf-core-rtthread-hooks.patch
├── project/                ← 示例应用：自检 + Wi-Fi + BLE
├── tools/                  ← 安装、构建、烧录、验证脚本
└── docs/                   ← 移植说明、外设矩阵、排障
```

**为什么分成三块：**

| 组件 | 为什么不能合成一个 |
|---|---|
| `component/` | 纯粹是本移植，可以独立于 IDF 版本演进 |
| `patches/` | RT-Thread 要替换 FreeRTOS，必须改 IDF 内部的几个文件。做成 patch 才能**审查**和**升级** |
| `project/` | 应用是使用者的东西，不该和内核绑死 |

---

## 快速开始

### 0. 前置条件

- **ESP-IDF v6.1**（含 `esp32s31` 目标）
- 工具链：`cd <esp-idf>; .\install.ps1 esp32s31`
- Python `pyserial`（`tools/board.py` 需要）：`python -m pip install pyserial`

> **注意**：ESP32-S31 的 SoC 描述只存在于 ESP-IDF 的 S31 分支/fork 中。
> 官方 v6.1 release 不一定带 `esp32s31` target，先确认：
> `ls <esp-idf>/components/soc/esp32s31`

### 1. 拿代码

```bash
git clone https://github.com/yilives/s31-rtt-rtos.git
cd s31-rtt-rtos
```

### 2. 拉上游 RT-Thread

```powershell
.\tools\fetch-upstream.ps1
```

```bash
./tools/fetch-upstream.sh
```

会 clone 到 `./rt-thread`，并 checkout 到验证过的版本
`9e6dec3b14f5bf5514b0da829736c2485c38b0a7`（5.4.0，`--depth 1`，约 50 MB）。

**RT-Thread 保持零修改**——这个移植从不改它的源码。

### 3. 打补丁 + 装组件

```powershell
.\tools\apply-idf-patch.ps1    -IdfPath C:\esp\esp-idf
.\tools\install-component.ps1  -IdfPath C:\esp\esp-idf
```

需要改 IDF 的一共 10 个文件（`patches/0001-idf-core-rtthread-hooks.patch`，约 440 行）：

| 文件 | 改什么 |
|---|---|
| `components/freertos/Kconfig` | 加 `CONFIG_IDF_RTOS_RTTHREAD` 开关 |
| `components/freertos/CMakeLists.txt` | RT-Thread 模式下不编 FreeRTOS 内核 |
| `components/freertos/.../riscv/port.c` | cache-off 断言改成条件式（§下面有说明） |
| `components/esp_system/startup.c` | RT-Thread 模式下走 `entry()` |
| `components/esp_system/CMakeLists.txt` | 同上 |
| `components/esp_system/ld/*/sections.ld.in` | 中断栈 + RT-Thread 堆的段定义 |
| `components/riscv/vectors_rtthread.S` | **新增**：RT-Thread 的陷阱入口 |
| `components/spi_flash/cache_utils.c` | **新增**：flash 操作的跨核钩子 |

### 4. 构建

```powershell
.\tools\build.ps1 -IdfPath C:\esp\esp-idf build
```

```bash
export IDF_PATH=/opt/esp/esp-idf
./tools/build.sh
```

### 5. 烧录 + 验证

```powershell
python .\tools\probe_ports.py                      # 找 COM 口
.\tools\build.ps1 -IdfPath C:\esp\esp-idf -Port COM7 flash
python .\tools\board.py COM7 "s31test"
```

**期望**：

```
=== 36 passed, 0 failed ===
```

---

## 支持情况

### 内核

| 能力 | 状态 |
|---|---|
| 调度、线程、信号量、互斥量、事件、邮箱、消息队列 | ✅ |
| 软件定时器、动态/静态对象 | ✅ |
| **SMP 双核调度**（每核独立 tick、中断栈、IPI） | ✅ |
| CLIC 中断 + 硬件嵌套 | ✅ |
| 中断看门狗 | ✅ |
| FinSH / msh（22 条命令） | ✅ |
| 堆：内部 DRAM + PSRAM | ✅ |

### 外设

| 外设 | 接口 | 验证命令 |
|---|---|---|
| UART0 | `rt_serial` | 控制台 |
| GPIO / 引脚中断 | `rt_pin_ops` | `s31pinirq` |
| I2C ×3 | `rt_i2c_bus` | `i2cscan` |
| SPI | 轮询（🟡 非完整 `rt_spi_bus`） | `s31spiloop` |
| ADC ×2（17 bit） | `rt_adc_device` | `s31adc` |
| LEDC PWM ×8 | `rt_pwm_device` | `s31pwm` |
| MCPWM | 互补 + 死区 | `s31mcpwm` |
| GPTimer ×4 | 直连 | `s31timer` |
| PCNT | `rt_pulse_encoder_device` | `s31pcnt` |
| RMT | 直连 | `s31rmt` |
| TWAI / CAN | 直连 | `s31twai` |
| SYSTIMER | tick 源 | `s31stat` |
| PSRAM 16 MB | `heap_caps_malloc` | `s31psram` |
| RGB LED (SK6805) | 直连 | `s31led` |
| USB OTG | 🟡 仅能力探针 | `s31usb` |
| **未移植** | I2S / SD/MMC / LCD / CAM / Touch / 温度 / 以太 / 硬加密 / 低功耗 | 见 `docs/ROADMAP.md` |

### 无线

| 能力 | 状态 |
|---|---|
| Wi-Fi 2.4 GHz 连接 + 拿 IP | ✅ `s31wifi <ssid> <pass>` |
| Wi-Fi 扫描 | ✅ `s31wifiscan` |
| BLE 控制器 + NimBLE | ✅ `s31ble` |
| Wi-Fi 5 GHz | ❌ 硬件不支持 |

---

## 三个非显然的设计点

如果你只读一段，读这段。这三条都是**踩过坑之后**的结论。

### 1. `CONFIG_FREERTOS_UNICORE=y` 和「双核」不矛盾

```ini
CONFIG_FREERTOS_UNICORE=y
CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y
```

这两个宏描述的是「**FreeRTOS 是否在多个核上调度**」，不是「芯片有几个核」。

设成 `y` 让 IDF 不调用 `start_other_core()`，把第二个核留给 RT-Thread 自己启动。
**设成 `n` 会让 `startup.c` 走另一条分支，`entry()` 根本不被调用，整个启动流程就变了。**

实测符号表可以证明这一点：

```
start_other_core                  不存在
call_start_cpu1                   不存在
esp_startup_start_app_other_cores 不存在
rt_hw_secondary_cpu_up            存在   ← 本移植自己的
```

### 2. CPU1 必须先把 `mstatus.FS` 设成 3

陷阱入口在 `__riscv_flen == 32` 时**无条件保存 f0..f31**：

```asm
#if defined(__riscv_flen) && (__riscv_flen == 32)
    FPSTORE f0, (32 * REGBYTES + 0 * FPREG_BYTES)(sp)
```

它的注释假设「FS 在所有可运行上下文里都非零」——这对线程成立
（`rt_hw_stack_init_core()` 用 `mstatus=0x7880` 构造帧），**对 CPU1 不成立**：

```
CPU0 mstatus = 0x80006088   FS(14:13) = 3
CPU1 mstatus = 0x00000009   FS(14:13) = 0
```

`fsw` 在 `FS=0` 时是非法指令。**CPU1 在第一条 `fsw` 上死在自己的陷阱处理里。**

**现象**：陷阱被取（代码停止前进）、handler 一次没进（`isr_runs=0`）、
`mintstatus.MIL=0`（没进 handler 上下文）。**六个量全部正确，就是不动。**

修法见 `component/port/esp32s31/smp_tick.c` 的 `rt_hw_secondary_cpu_irq_enable()`。

### 3. flash 操作需要跨核握手

`spi_flash_disable_interrupts_caches_and_other_cpu()` 在这个构建里的反汇编是
**6 条指令**——没有 IPC，没有跨核握手：

```
op_lock → noniram_disable → disable_cache(0) → ret
```

因为 `CONFIG_FREERTOS_UNICORE=y` 把 IDF 的多核那半**编译掉了**，
`esp_ipc_call_*` 在 ELF 里根本不存在。

**后果**：CPU0 关 flash cache 时 CPU1 还在跑，CPU1 的 tick handler 调
`rt_tick_increase()`（在 flash 里）→ `MCAUSE 0x19` cache error。

补丁在 `cache_utils.c` 里加了两个 weak 钩子，组件提供强定义：

```c
/* CPU0，关 cache 前 */
spi_flash_other_core_stop();     /* 置标志，等 CPU1 应答 */

/* CPU1 的 tick ISR，第一条语句之后 */
if (s31_flash_op) { s31_flash_ack = 1; return; }   /* 不碰 flash */
```

两个变量在 DRAM（cache 关了也能读），握手在 ISR 最前面。

---

## 全部命令

| 命令 | 作用 |
|---|---|
| `s31test [子项]` | 36 项自检 |
| `s31stat` | tick、两个堆、demo 线程、CPU1 状态 |
| `s31smp` | 每核当前线程、空闲线程、中断栈、tick |
| `s31wifi [ssid pass]` | 起 Wi-Fi；给参数则连接并打印 IP |
| `s31wifiscan` | 列可见 2.4 GHz AP |
| `s31ble` | 起 BLE 控制器 + NimBLE |
| `s31psram [KB]` | PSRAM 写读验证 |
| `s31led <r> <g> <b>` | 板载 RGB LED |
| `s31pwm <ch> <gpio> <hz> <duty%>` | LEDC PWM |
| `s31mcpwm` / `s31timer` / `s31pcnt` / `s31rmt` / `s31twai` | 各外设自测 |
| `s31adc <ch>` / `i2cscan [bus]` / `s31spiloop` | ADC / I2C / SPI |
| `s31pinirq` / `s31intr [-a] [-m N]` | 引脚中断 / CLIC 线状态 |
| `s31usb` | USB 端点与 PHY 能力（读 GHWCFG） |
| `s31con` | 控制台状态 |

---

## 工具

| 脚本 | 作用 |
|---|---|
| `tools/fetch-upstream.ps1` / `.sh` | clone 上游 RT-Thread 到验证过的版本 |
| `tools/apply-idf-patch.ps1` | 给 IDF 打补丁（`-Check` 只检查，`-Reverse` 回退） |
| `tools/install-component.ps1` | 把 `component/` 装进 `$IDF_PATH/components/rtthread/` |
| `tools/build.ps1` / `.sh` | 构建 / 烧录（自动发现工具链） |
| `tools/probe_ports.py` | 列出串口，或探测某个口 |
| `tools/board.py` | 发 msh 命令并读回结果 |
| `tools/reset.py` | 复位并抓完整启动日志 |
| `tools/check_image.py` | 镜像检查（9 项，不需要板子） |
| `tools/check_rtt_style.py` | 代码规范（28 项，不需要板子） |

两个检查器值得解释：

**`check_image.py`** 检查链接产物，抓「能编译但跑不对」的问题：
flash-mapped LOAD 段数、三个上下文帧大小是否一致、陷阱出口 `mstatus` 是否带
`FS=3`、`__fsymtab`/`__rt_init` 是否非空、init 表顺序、向量表可达。

**`check_rtt_style.py`** 检查源码：每个 `.c` 是否都进了构建、
`MSH_CMD_EXPORT_ALIAS` 描述里不能有逗号（会导致编译失败）、文档章节号不重复、
文档里写的二进制大小与实际一致。

**规则：每个检查器都必须能失败。** 加一条检查时先故意破坏对应条件，
确认它真的报错，再恢复。**做不到失败的检查器比没有更糟。**

---

## 已知限制

### 未移植

I2S、SD/MMC、LCD、Camera、Touch、温度传感器、Ethernet MAC、硬件加密、
低功耗/deep sleep、LP core、ADC DMA。**完整清单和优先级见 `docs/ROADMAP.md`。**

**硬件没有的**：DAC（`SOC_DAC_SUPPORTED` 未定义）、Wi-Fi 5 GHz。

### 已移植但有缺口

- **SPI** 只有轮询回环，不是完整的 `rt_spi_bus`
- **UART 波特率**固定 115200（`uart_ll.h` 缺 `get_sclk`/`set_sclk`）
- **GPIO 中断**只用通道 0（IDF 自己的 TODO，IDF-7995）
- **USB** 只有能力探针，设备/主机栈未移植

### 已知问题

| 问题 | 状态 |
|---|---|
| `bind <运行中的线程> 1` 让线程卡死 | 未修，变通：创建时绑核 |
| `console set uart0` 后 shell 输入失效 | 未修，复位可恢复 |
| `pwm get 0` 打印 `2147483647%` | 上游除零 bug，不影响功能 |
| `s31twai` 同会话重复跑失败 | 冷启动正常，原因未查明 |

### 板级假设

默认针对 **ESP32-S31-WROOM-3**（16 MB flash + 16 MB Octal PSRAM，200 MHz）。
改板子看这张表：

| 项 | 改哪里 |
|---|---|
| Flash 大小 | `project/sdkconfig.defaults` 的 `CONFIG_ESPTOOLPY_FLASHSIZE` |
| PSRAM 模式/速率 | `CONFIG_SPIRAM_MODE_OCT` / `CONFIG_SPIRAM_SPEED_200M` |
| 分区表 | `project/partitions.csv` |
| 板载 LED 引脚 | `component/port/esp32s31/board_extra.c` |
| 控制台 UART | `CONFIG_ESP_CONSOLE_UART_DEFAULT` |

---

## 其他目标

`component/port/` 里除了 `esp32s31/` 还有：

| 目标 | 状态 |
|---|---|
| **ESP32-S31** | ✅ 上板验证，36/36，双核，Wi-Fi，BLE |
| ESP32-C3 | 单核，曾上板验证 |
| ESP32-C5 | CLIC 单核，能编译 |
| ESP32-C6 | 能编译链接，未上板 |
| ESP32-S3 | Xtensa 双核，代码在，未上板 |

**这个仓库目前的验证结论只对 S31 成立。**

---

## 文档

| 文件 | 内容 |
|---|---|
| `docs/PORTING.md` | 端口结构、启动流程、中断、时钟、内存、FreeRTOS 兼容层、GC-sections 的坑 |
| `docs/PERIPHERALS.md` | 外设矩阵（含未移植清单） |
| `docs/SMP.md` | 双核怎么跑通的，三个必须设置的 CSR，flash 跨核握手 |
| `docs/USB.md` | USB 选型分析与移植步骤 |
| `docs/ROADMAP.md` | 尚未移植的资源清单与优先级、已知 bug、方法学 |
| `docs/TROUBLESHOOTING.md` | 按现象排查（构建 / 烧录 / 运行时 / panic） |
| `docs/porting-log.md` | 116 节开发日志，**含所有失败尝试** |
| `docs/bringup-runbook.md` | 上板操作手册 |
| `docs/porting-plan.md` | 计划与风险登记（R1–R358） |

**开发日志是刻意保留「失败」的。** 记录哪些做法行不通比记录哪些行得通更有用——
它能防止下一个人重走同一条死路。

---

## 许可

本移植（`component/`、`patches/`、`project/`、`tools/`、`docs/`）：
**Apache-2.0**，见 `LICENSE`。

引入的第三方代码不属于本仓库，各自的许可：

| 组件 | 许可 |
|---|---|
| RT-Thread | Apache-2.0 |
| ESP-IDF | Apache-2.0 |

---

## 致谢

- **RT-Thread** —— 上游内核，**零修改**
- **Espressif ESP-IDF** —— 工具链、HAL、Wi-Fi/BLE 协议栈、S31 的 SoC 描述
