# LP core 与 deep sleep（LPCORE.md）

ESP32-S31 有两个大核（RV32 @320 MHz）**加一个 LP core**。这份文档讲清楚它是
什么、为什么不需要"移植 RT-Thread"、以及这个仓库提供了什么。

---

## 一、硬件是什么

从 `soc_caps.h` / `soc.h` 读出来的，不是查手册：

| 项 | 值 | 宏 |
|---|---|---|
| 架构 | RISC-V，16 个 PMP entry（128 字节粒度） | `SOC_LP_CORE_HAS_PMP` |
| 内存 | **32 KB**，`0x2E000000`..`0x2E008000` | `SOC_LP_RAM_LOW/HIGH` |
| **LP ROM** | **没有** | `SOC_LP_CORE_CONFIGURABLE_BOOT_ADDR` |
| 启动地址 | HP 必须写 `LP_RAM_BASE + 0x80` 到 `LP_SYS.lp_core_boot_addr` | 同上 |
| 外设 | LP UART / LP I2C / LP SPI / LP timer / ETM / touch / ADC | `SOC_LP_*_SUPPORTED` |
| 和 HP 通信 | LP mailbox + 一块共享内存 | `SOC_LP_MAILBOX_SUPPORTED` |

IDF 的注释直接点名了这个芯片：

```c
/* Chips without LP ROM but with a SW-configurable boot address register
 * (e.g. ESP32-S31) must set the boot address explicitly. The hardware
 * default points to SOC_RTC_DRAM_LOW (the interrupt vector table base),
 * not to reset_vector which is placed 0x80 bytes into LP RAM. */
```

---

## 二、为什么不需要"移植 RT-Thread"

**32 KB 总内存 + 无 ROM + 裸机启动** —— 这不是一个能跑 RTOS 的环境。

LP core 的运行模型是：

```
HP core（跑 RT-Thread）
   │
   ├─ ulp_lp_core_load_binary(lp_program, size)   ← 把程序写进 LP RAM
   ├─ ulp_lp_core_run(&cfg)                       ← 写启动地址、放开复位
   │
   └─ LP core 跑一段裸机程序（无调度器、无 RTOS、无 heap）
         │
         └─ 通过共享内存 / mailbox 回传结果
```

**所以"移植"在这里的含义是：让 IDF 的 `ulp` 组件在 RT-Thread 上能用。**
不是把 RT-Thread 塞进 LP core。

---

## 三、IDF 侧的支持本来就完整

| 项 | 位置 |
|---|---|
| 组件 | `components/ulp/lp_core/`，7 个 .c |
| API | `ulp_lp_core_run` / `load_binary` / `stop` / `sleep_start` / sw_intr |
| 独立工具链 | `cmake/toolchain-lp-core-riscv.cmake`（`-march=rv32imac_zicsr_zifencei_zaamo_zalrsc -mdiv`） |
| 独立链接脚本 | `ld/lp_core_riscv.ld` |
| Kconfig 显式支持 S31 | `range 32 32744 if IDF_TARGET_ESP32S31` |
| 示例 | `examples/system/ulp/lp_core/` 下 14 个 |

**打开就编：**

```ini
CONFIG_ULP_COPROC_ENABLED=y
CONFIG_ULP_COPROC_TYPE_LP_CORE=y
CONFIG_ULP_COPROC_RESERVE_MEM=8192
CONFIG_ULP_SHARED_MEM=1024
```

实测：打开后 7 个 `lp_core*.obj` 全部编出来，主镜像链接通过。

---

## 四、这个仓库加了什么

三个文件，都不是"移植"，是**胶水 + 验证入口**：

### `component/port/esp32s31/lp_core.c`

`s31lp` 命令：

```
msh > s31lp
s31lp: program N bytes at 0x...
s31lp: program loaded into LP RAM
s31lp: LP core released from reset, running
```

**分成三步报告**，因为失败位置不同含义完全不同：

| 步骤 | 失败意味着 |
|---|---|
| `load_binary` | 程序放不进 32 KB，或校验和不符 —— 构建问题 |
| `run` | 启动地址写不进、LP 时钟没起来 —— 硬件/配置问题 |
| 都成功 | 核心已经放开复位在跑 |

`ulp_main_bin_start` 用 `weak` 声明：没有 LP 程序的项目仍能链接，
`s31lp` 会说"没有嵌入程序"而不是让整个镜像链接失败。

### `component/port/esp32s31/sleep.c`

`s31sleep` 命令：

```
msh > s31sleep info
s31sleep: last wakeup cause : timer (4)
s31sleep: RTC marker        : 0x53314c50 (set: woke from deep sleep)
s31sleep: cycles survived   : 3

msh > s31sleep 10
s31sleep: arming timer wakeup for 10 s, then deep sleep
s31sleep: this call does not return - expect a reset
```

**为什么是包装而不是别名：** `esp_deep_sleep_start()` **不返回**——它把芯片断电，
下一条指令是 boot ROM 的。所以唤醒后需要的一切必须**在调用之前**安排好：

- RTC 内存里的标记（`RTC_NOINIT_ATTR`），让下次启动能区分"深睡唤醒"和"冷启动"
- 唤醒原因，在任何其他代码之前打印
- 定时唤醒源，否则板子醒不过来

标记是 `RTC_NOINIT_ATTR` 变量 + 魔数。RTC 内存**深睡不清、上电清**，
正好是需要区分的那个东西。

### `component/port/esp32s31/... CMakeLists.txt`

三处 `-u` 锚点 + 两个 Kconfig 开关。

---

## 五、deep sleep 为什么能直接链接

这是本轮最值得记的结论。**没有写任何 glue，它就链接了。**

审计过程：

| 检查 | 结果 |
|---|---|
| `sleep_modes.c` 编不编 | ✅ 编了（`SOC_LIGHT_SLEEP_SUPPORTED` 门是开的） |
| 它的 85 个未定义符号里，有几个 OS 相关 | **2 个**：`vPortEnterCritical` / `vPortExitCritical` |
| 这两个在镜像里吗 | ✅ 有（RISC-V 的 `port.c` 为 tick 和临界区保留着） |
| `esp_task_wdt_stop()` 需要吗 | ❌ 不需要（`CONFIG_ESP_TASK_WDT_USE_ESP_TIMER` 没开） |
| `esp_ipc_isr_stall_other_cpu()` 需要吗 | ❌ 不需要（`CONFIG_ESP32_ECO3_CACHE_LOCK_FIX` 是 ESP32 专用） |
| 43 个缺失符号在哪个对象里 | ✅ 全部在 `pmu_sleep.c` / `sleep_cpu.c` / `sleep_cache.c` 等，都编了 |

**结论：`sleep_modes.c` 唯一的内核依赖是两个临界区函数，而 RT-Thread 下保留的
FreeRTOS port 层正好提供它们。**

剩下的只是 `--gc-sections` 把整个调用图丢了（没人引用），加 `-u` 就拉进来。

实测结果：

```
esp_deep_sleep_start              ✓
esp_sleep_enable_timer_wakeup     ✓
esp_sleep_get_wakeup_cause        ✓
pmu_sleep_init / pmu_sleep_start  ✓
ulp_lp_core_run / load_binary     ✓
```

镜像从 1,533,824 B 涨到 1,552,928 B（+19 KB）。

---

## 六、双核与深睡

深睡要停住整个芯片，所以 CPU1 不能在飞。

IDF 的处理是 `esp_ipc_isr_stall_other_cpu()`，**但那段在整个 S31 构建里不编译**
（两个调用点分别被 `CONFIG_ESP32_ECO3_CACHE_LOCK_FIX` 和
`CONFIG_ESP_TASK_WDT_USE_ESP_TIMER` 挡着，两个都没开）。

**这个端口的做法**：复用 flash 握手那个标志。

```c
#ifdef RT_USING_SMP
    s31_flash_op = 1;      /* CPU1 的 tick ISR 看到就应答并返回，不碰内核 */
    rt_thread_mdelay(2);
#endif
esp_deep_sleep_start();
```

语义上说得通：那个标志的作用就是"别在 CPU1 上跑内核代码"，
深睡只是把时间窗口拉到无限长。

---

## 七、哪些验证过、哪些没有

**必须如实区分。**

### 已验证（在 x86 上用 `nm` 查符号）

| 项 | 证据 |
|---|---|
| `sleep_modes.c` 编译 | `.obj` 存在 |
| 它的内核依赖只有 2 个临界区函数 | 85 个 U 符号逐个比对 |
| 43 个缺失符号全部有对象提供 | 1373 个 `.obj` 全扫 |
| 加了锚点后能链接 | 镜像生成，9 个符号全部 `T` |
| LP core 组件编译 | 7 个 `.obj` |
| 两个命令进了镜像 | `cmd_lp` / `cmd_sleep` 是 `T` |
| init 表进了镜像 | `__rt_init_s31_sleep_report` 在表里（24 项） |

### 未验证（需要上板）

| 项 | 为什么没验 |
|---|---|
| `s31sleep` 真的能睡下去并按时醒 | 板子当前跑的是 C5 镜像 |
| `s31lp` 的 LP 程序真的跑到 `main()` | 同上 |
| 唤醒原因的读数对不对 | 同上 |

**热启动路径（加载、写启动地址、放开复位）在代码里是 `ESP_OK` 就报，
但"读数是 ESP_OK"和"LP core 真的执行了"是两件事** —— 后者要靠共享内存里的
计数器变化来证明，而那个需要一次复位循环。

---

## 八、怎么自己验证

```powershell
# 1. 打开两个开关（component 已带 Kconfig 选项）
#    CONFIG_ULP_COPROC_ENABLED=y
#    CONFIG_ULP_COPROC_TYPE_LP_CORE=y
#    CONFIG_RTT_SLEEP_LINK_PROBE=y
.\tools\build.ps1 -IdfPath C:\esp\esp-idf build
.\tools\build.ps1 -IdfPath C:\esp\esp-idf -Port COM7 flash

# 2. 深睡
python tools\board.py COM7 "s31sleep info"
python tools\board.py COM7 "s31sleep 10"
# 期望：复位，10 秒后启动日志里有
#   s31sleep: woke from deep sleep, cause timer (4), cycle 1

# 3. LP core
python tools\board.py COM7 "s31lp"
# 期望：loaded / released 两行
```

**判断"通过"的标准**：

- 深睡：`cycle` 递增，`cause` 是 `timer`，间隔对得上
- LP core：共享内存里的计数器变化（当前代码只打印 `sleep_duration`，
  要验证执行得自己加一个共享变量）

---

## 九、下一步

| 想做什么 | 要加什么 |
|---|---|
| 让 LP core 在深睡里继续跑 | LP 程序用 `ULP_LP_CORE_WAKEUP_SOURCE_LP_TIMER`，并配 `lp_timer_sleep_duration_us` |
| 让 LP core 采样后唤醒 HP | `esp_sleep_enable_ulp_wakeup()` + LP 侧调 `ulp_lp_core_wakeup_main_processor()` |
| 用 LP IO 当唤醒源 | `ULP_LP_CORE_WAKEUP_SOURCE_LP_IO` + LP 侧 `ulp_lp_core_gpio_*` |
| 减少深睡电流 | `CONFIG_ESP_SLEEP_POWER_DOWN_FLASH`，以及 PSRAM 的掉电策略 |

**LP 程序怎么写：** 参照 `examples/system/ulp/lp_core/gpio/main/ulp/main.c`，
用 `ulp_add_project()` 挂进 CMake，`ulp_lp_core_halt()` 或 `return 0` 结束。
