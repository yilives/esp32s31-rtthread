# RT-Thread 移植到 ESP32-S31 —— 实施方案

> **阅读提示**：本文档是开发过程的原始记录，其中的构建命令（如 idf_s31_env.sh、I:/RT_thread/...）是当时工作区的路径，**对本仓库不适用**。
> 要构建本仓库，请用 	ools/build.ps1，见 README.md。

> 编写时间：2026-10-26
> 目标芯片：**ESP32-S31**（双核 32-bit RISC-V，最高 320 MHz，CLIC 中断控制器）
> 基线代码：ESP-IDF **v6.1**（`<workspace>/esp32s31-sdk/esp-idf-v6.1`）、RT-Thread **v5.4.0**（`rt-thread`）
> 参考实现：`RT-Thread-packages/esp-idf` **rtt-dev**（`<esp-idf>`），已支持 ESP32-C3/C6

---

## 0. 结论先行

1. **路线选 B：在 ESP-IDF 内部用 `components/rtthread` 替换内核**，不新建 RT-Thread BSP。
   理由不是"省事"，而是 S31 的 Wi-Fi 6 / BLE 5.4 / 以太网全部是 IDF 闭源预编译库，
   路线 A（RT-Thread BSP + IDF 源码包）需要为 S31 手工重建数千行 IDF 源码清单，
   且 S31 是 IDF 的 **preview target**，源码清单会频繁变动，不可维护。

2. **这条路已经铺了 70%**。`rtt-dev` 分支不是"只给 C3 写的 demo"，它的中断入口
   `components/riscv/vectors_rtthread.S` **已经包含 CLIC 硬件嵌套分支**（为 C5/P4 预留），
   `components/esp_system/startup.c` **已经有 SMP 启动骨架**（`_cpus_lock` + `s_system_full_inited`）。
   S31 恰好落在这两个"已预留但未被使用"的分支上。

3. **工作量估计**：
   - 单核最小内核跑通（`rt_kprintf` + 线程 + tick）：**4～6 人日**
   - 加中断/设备框架/msh/UART/GPIO，形成可演示系统：**再 8～12 人日**
   - 双核 SMP：**再 3～5 人周**
   - Wi-Fi/BT：**不建议排期**（见 §8 风险 R6）

4. **最大的三个未知数**（不是代码量，是信息）：
   - S31 是 preview target，IDF 自身在 S31 上可能还有 bug；
   - 你手上有没有 S31 板子——没有硬件，P1 之后无法验证运行时行为；
   - S31 的 CLIC 与 IDF 中断矩阵的语义边界，必须实测确认（§6.3）。

### 0.1 选型决策记录（为什么是 S31，不是 S3 或 C6）

工作区里另有一份 `rtthread-esp32-s3-porting-analysis.md`，标的物是 **ESP32-S3（Xtensa LX7 + FPU）**，
本文是 **ESP32-S31（RISC-V 双核 + CLIC）**。两者名字只差一个字符，结论不能互相套用：
S3 的"最小内核 3~6 人周"是**从零写 Xtensa CPU 端口**；本文的"P1 4~6 人日"是**复用 RISC-V 端口**。

那份分析的决策结论是"优先 C6；必须 S3 才走路线 B"。实际选 S31，理由如下：

| 维度 | ESP32-C6 | ESP32-S3 | **ESP32-S31（选中）** |
|---|---|---|---|
| CPU | 单核 RISC-V 160 MHz | 双核 Xtensa LX7 240 MHz | **双核 RISC-V 320 MHz，单核 128-bit SIMD** |
| RT-Thread CPU 端口 | 现成 | **需从零写 Xtensa** | 复用 `libcpu/risc-v` |
| 无线 | Wi-Fi 6 + BLE + 802.15.4 | Wi-Fi + BLE5 | Wi-Fi 6 + BLE 5.4 + 802.15.4 + **千兆 EMAC** |
| 多媒体 | 无 | LCD/CAM | DVP 摄像头、JPEG 编解码、PPA、双 I2S |
| GPIO | 30 | 45 | **60** |
| 代价 | — | 端口成本最高 | **preview target、无任何 BSP、Wi-Fi 适配层空白** |

一句话：S31 是"**S3 的性能 + C3 的 CPU 架构**"——需要 S3 级算力与外设，又不想付 Xtensa 端口的代价，
所以选它，并走路线 B。

---

## 1. 目标与验收标准

| 阶段 | 交付物 | 验收标准（DoD） |
|---|---|---|
| P0 | 可编译的 IDF 基线 | `idf.py build` 通过，能烧到板子打印 `Hello`，确认 chip revision 与 flash/PSRAM 配置 |
| P1 | RT-Thread 单核内核 | 串口打印 RT-Thread banner；2 个线程按优先级正常切换；`rt_tick_get()` 对比 PC 计时 10 分钟误差 < 1%；连续跑 30 分钟不复位 |
| P2 | 中断 + 设备 + msh | UART 中断收发正常；GPIO 中断可触发；msh 可交互；`list_thread` / `free` 正常 |
| P3 | 双核 SMP | CPU1 上线；双核各自跑线程；`spinlock` 压测 24 小时无死锁；线程可跨核迁移 |
| P4 | 外设扩展（可选） | PSRAM、LCD、以太网、Wi-Fi/BT 按需 |

**非目标（本方案不做）**：不改 RT-Thread 主线仓库；不追求 DFS/lwIP；不承诺 Wi-Fi/BT 可用。

---

## 2. 现状盘点（全部有据可查）

### 2.1 S31 的硬件事实（来自 `components/soc/esp32s31/include/soc/soc_caps.h`）

```
SOC_CPU_CORES_NUM                2
SOC_HP_CPU_HAS_MULTIPLE_CORES    1
SOC_INT_CLIC_SUPPORTED          1     ← 用 CLIC，不是 PLIC
SOC_INT_HW_NESTED_SUPPORTED     1     ← 硬件中断嵌套
SOC_CPU_HAS_FLEXIBLE_INTC       1
SOC_SIMD_INSTRUCTION_SUPPORTED  1     ← 单核 128-bit SIMD，对齐 16 字节
SOC_XTAL_SUPPORT_40M            1
```

### 2.2 IDF v6.1 对 S31 的支持（已具备，无需补）

| 路径 | 作用 |
|---|---|
| `components/riscv/vectors_clic.S` | CLIC 向量表：`_vector_table`（非向量入口）+ `_mtvt_table`（48 项向量表） |
| `components/riscv/interrupt_clic.c` | CLIC 驱动：优先级、阈值、使能 |
| `components/riscv/interrupt.c:59` | `_global_interrupt_handler(sp, mcause)` —— 唯一 C 分发入口 |
| `components/riscv/interrupt.c:36` | `intr_handler_set(int_no, fn, arg)` —— ISR 注册表（`s_intr_handlers[core][int_no]`） |
| `components/esp_system/port/cpu_start.c:252` | CLIC 目标下调用 `esp_cpu_intr_set_mtvt_addr(&_mtvt_table)` |
| `components/esp_system/port/cpu_start.c:225` | `call_start_cpu1()` —— 双核启动入口 |
| `components/freertos/port_systick.c` | SYSTIMER alarm 产生 tick，`SYSTIMER_ALARM_OS_TICK_CORE0 + cpuid` |
| `components/riscv/include/riscv/csr_clic.h:16` | `RVHAL_INTR_ENABLE_THRESH = 0` |

### 2.3 现有 RT-Thread 侧资产

**可复用的（路线 B 的现成件）**：

| 文件 | 行数 | 说明 |
|---|---|---|
| `components/rtthread/port/common/context_gcc.S` | ~200 | 上下文切换汇编，已含 `RT_USING_SMP` 分支 |
| `components/rtthread/port/common/board.c` | 20 | 板级初始化：`vPortSetupTimer()` + `esprv_int_set_threshold()` |
| `components/rtthread/rtthread_stack_init.c` | ~50 | 栈初始化适配（含 picolibc TLS） |
| `components/rtthread/linker.lf` | ~25 | IRAM 放置规则（noflash） |
| `components/riscv/vectors_rtthread.S` | 226 | 中断入口，**已含 CLIC 硬件嵌套分支（第 141-147 行）** |
| `components/esp_system/startup.c:267` | — | 启动接管点（`entry()`），**已含 SMP 骨架（第 287-289 行 `s_system_full_inited` 释放）** |
| `components/freertos/port_systick.c:222` | — | `xPortSysTickHandler` 里已改为 `rt_tick_increase()` |

**必须新写的**：只有 `port/esp32s31/` 下的 2～3 个文件，加上 4 处既有文件的开关扩展。

**注意**：`libcpu/risc-v/common/pic/pic-riscv-clic.c`（533 行，CLIC 通用驱动）**不要用**。
它是给"裸 CLIC、无中断矩阵"的 SoC 准备的；S31 走 IDF 中断矩阵，两者同时管会打架（详见 §6.3）。

---

## 3. 路线对比与选型

| 维度 | 路线 A：RT-Thread BSP + IDF 源码包 | 路线 B：IDF 内替换内核（选中） |
|---|---|---|
| 构建系统 | SCons 主导，`SConstruct` 手写 IDF 源码清单 | CMake 主导，IDF 自己管 |
| S31 适配成本 | 需为 S31 重建 ~1400 行源码清单 + ~840 行固定 `sdkconfig.h` | 加 4 个分支即可 |
| 上游变动影响 | 每次 IDF 升级都要重抄清单 | 跟随 IDF，天然同步 |
| 内核完整性 | 完整 RT-Thread（msh/DFS/设备框架） | 需自己逐项接回 |
| 双核支持 | 需自己写 SMP 端口 | `startup.c` 已有骨架 |
| Wi-Fi/BT | 靠 FreeRTOS-Wrapper 顶住 | 同样靠 Wrapper，但至少与 IDF 同步 |
| 适合场景 | 要做产品级 BSP 长期维护 | **先跑通、再演进** |

**决策**：P1～P3 走 B。若后续要发布正式 BSP，再以 B 的成果反向补 A（此时已有实测过的端口代码，风险大幅下降）。

---

## 4. 目标架构

```
┌──────────────────────────────────────────────────────────┐
│  应用：main() / msh / 自己的线程                          │
├──────────────────────────────────────────────────────────┤
│  RT-Thread 内核 v5.4.0（src/ 原样编译，不改一行）          │
│    scheduler_up.c  →[P3]→ scheduler_mp.c                  │
│    timer.c / thread.c / ipc.c / kservice.c                │
├──────────────────────────────────────────────────────────┤
│  CPU 端口（本方案新写）                                    │
│    port/esp32s31/rtconfig.h     ← 目标配置                │
│    port/esp32s31/cpuport.c      ← 栈帧初始化              │
│    port/common/context_gcc.S    ← 上下文切换（复用）       │
│    components/riscv/vectors_rtthread.S ← 中断入口（改）    │
├──────────────────────────────────────────────────────────┤
│  ESP-IDF v6.1（ESP32-S31）                                │
│    CLIC 驱动 / 中断矩阵 / SYSTIMER / bootloader / 闭源射频 │
├──────────────────────────────────────────────────────────┤
│  ESP32-S31 硬件（双核 RISC-V + CLIC + 320MHz）            │
└──────────────────────────────────────────────────────────┘
```

启动路径（关键，P1 要改的就是这条链上的 3 个点）：

```
ROM bootloader
  → IDF 二级 bootloader
    → call_start_cpu0()                     [IDF]
      → do_core_init()                      [IDF]
        → do_secondary_init()               [IDF，单核模式下不启动 CPU1]
          → ★ startup.c:267 分支 → entry()  [RT-Thread 接管点]
            → rt_hw_board_init()            [port/common/board.c]
              → vPortSetupTimer()           [复用 IDF SYSTIMER alarm]
            → rt_application_init() → main  [RT-Thread]
            → rt_system_scheduler_start()
```

---

## 5. 分阶段实施计划

### P0 · 环境与基线（0.5～1 人日）

**目标**：证明"IDF v6.1 + S31 + 这块板子"这条链本身是通的。这一步不做，后面所有报错都无法归因。

**步骤**
1. 安装工具链（`riscv32-esp-elf`，`tools/tools.json` 中标记为 `always`，S31 与 C3/C6 共用）：
   ```bash
   cd esp32s31-sdk/esp-idf-v6.1
   install.bat esp32s31
   export.bat
   ```
2. 用 IDF 自带示例验证：
   ```bash
   cd examples/get-started/hello_world
   idf.py set-target esp32s31
   idf.py build flash monitor
   ```
3. 记录基线数据（后面全部要对比）：
   - `idf.py size` 的 IRAM/DRAM 占用
   - `esptool.py --chip esp32s31 flash_id` 的 flash 容量
   - chip revision、PSRAM 是否使能
   - 串口波特率、下载方式（USB-Serial-JTAG / UART）

**风险**：S31 在 `idf_py_actions/constants.py` 的 `PREVIEW_TARGETS` 里，`idf.py` 会提示 preview。
若 `install.bat esp32s31` 报错，退化方案：`install.bat esp32c6`（同为 riscv32-esp-elf），再 `set-target esp32s31`。

**DoD**：串口能打印 `Hello world!`，且 `idf.py size` 有稳定输出。

---

### P1 · 单核最小内核（4～6 人日）★核心阶段

**目标**：RT-Thread 在 CPU0 上跑起来，有 tick、有线程切换、有串口输出。

#### P1.1 打开 S31 的开关（4 处既有文件）

| 文件 | 位置 | 改动 |
|---|---|---|
| `components/freertos/Kconfig` | 605-607 | `depends on ... \|\| IDF_TARGET_ESP32S31` |
| `components/rtthread/CMakeLists.txt` | 12-21 | early-expansion 与 `FATAL_ERROR` 的目标列表加 `esp32s31` |
| `components/rtthread/CMakeLists.txt` | 118-130 | 增加 `elseif(CONFIG_IDF_TARGET_ESP32S31)` 分支，指向 `port/esp32s31` |
| `components/rtthread/linker.lf` | 15-18 | `rt_hw_interrupt_is_disabled` 的 s31 分支（走 `cpuport`，与 C6 同） |

#### P1.2 新建 `port/esp32s31/`

**`rtconfig.h`**（与实物 `port/esp32s31/rtconfig.h` 一致，关键项如下）

```c
#define ARCH_RISCV
#define ARCH_RISCV32
#define SOC_ESPRESSIF
#define SOC_ESP32_S31
#define RT_CPUS_NR                      1        /* P1/P2 单核；P3 改 2 */
#define RT_NAME_MAX                     12
#define RT_ALIGN_SIZE                   16       /* RV32 psABI */
#define RT_THREAD_PRIORITY_32
#define RT_THREAD_PRIORITY_MAX          32
#define RT_TICK_PER_SECOND              1000     /* 必须等于 CONFIG_FREERTOS_HZ */
#define IDLE_THREAD_STACK_SIZE          1024
#define RT_USING_HEAP
#define RT_USING_SMALL_MEM
#define RT_USING_SMALL_MEM_AS_HEAP
#define RT_USING_SEMAPHORE              /* P2 的设备框架要用 */
#define RT_USING_MUTEX
#define RT_BACKTRACE_LEVEL_MAX_NR       8
```

**关于 FPU（重要，见风险 R9；此处曾被误判，已按编译产物更正）**：
`ARCH_RISCV_FPU` / `ARCH_RISCV_FPU_S` 是 `legacy_common/cpuport.h:32-40` 依据
**工具链的 `__riscv_flen`** 自动定义的，而 `__riscv_flen` 来自 `-march`——
S31 的 ELF 属性 `Tag_RISCV_arch` 实测为 `rv32i2p1_m2p0_a2p1_f2p2_...`，即单精度 F 在编。
所以 **rtconfig.h 不需要、也不应该再定义这些宏**（重复定义反而冲突）。
`context_gcc.S.obj` 反汇编里有 32 条 `fsw` + 32 条 `flw`，说明协作式切换本身是保存浮点现场的。

真正的洞不在协作式路径，而在**中断帧**：它的尺寸与 FP 保存都缺失，已在 §R9 说明并修复。

**`cpuport.c`**（约 100 行）：直接复制 `port/esp32c6/cpuport.c`，只改注释。
它提供 `rt_hw_stack_init_core()` 和 `rt_hw_interrupt_is_disabled()`，与 SoC 无关。

#### P1.3 `_mtvt_table` —— 不需要新增（原假设已证伪）

**原判断**：CLIC 目标下 `cpu_start.c:252` 会调用 `esp_cpu_intr_set_mtvt_addr(&_mtvt_table)`，
而 `vectors_rtthread.S` 只定义了 `_interrupt_handler`，因此要自己补一张 48 项向量表。

**实测结论：不需要，而且补了会链接失败。**

- CLIC 目标下 `components/riscv/CMakeLists.txt:33-34` 会**无条件**编译 `vectors_clic.S`，
  即使 `CONFIG_IDF_RTOS_RTTHREAD=y`；
- `vectors_clic.S:68-120` 已经定义了 `_mtvt_table`；其 16–39、45–47 号槽位写的是 `_interrupt_handler`，
  但 **40/41 硬编码 `_panic_handler`，42/43 依配置，44 在 `CONFIG_ESP_IPC_ISR_ENABLE=y` 时是 `esp_ipc_isr_handler`**
  （完整表见 §6.3，勿按"16–47 全是 RT-Thread 入口"理解）；
  而 `vectors.S` 里同名符号被 `#ifndef CONFIG_IDF_RTOS_RTTHREAD` 包住（`vectors.S:429`），
  所以链接时 `_interrupt_handler` 唯一解析到 RT-Thread 的入口；
- 若在 `vectors_rtthread.S` 里再定义一次 → **duplicate symbol**。

**P1 的汇编改动因此只剩一处**：协作式切换栈帧的 MPP=M 修复（见 §P1.4）。P1 已落地为**无条件执行**，
不再新增任何目标宏。

#### P1.4 协作式栈帧的 MPP=M 修复（已落地：无条件执行）

**问题**：`port/common/context_gcc.S` 的 `rt_hw_context_switch()` 只写 MPIE，不写 MPP。
这样保存下来的帧如果被 tick ISR 选中恢复，`mret` 会带着 MPP=0 落进 U-mode 并触发异常。
所以汇编出口处必须把 MPP 强制置 M。

**曾提过的错误建议**：本文早期写"改成 `#if !SOC_INT_HW_NESTED_SUPPORTED` 按能力判断"。
**这是错的**——ESP32-S31 恰恰定义了 `SOC_INT_HW_NESTED_SUPPORTED=1`，却同样需要这段修复。
用该谓词会把唯一的新目标排除在外。这类"以为能按能力收敛、实际按需求收敛"的坑，只有回到
"这个修复解决的是什么问题"才能避开。

**实际落地**（`components/riscv/vectors_rtthread.S`，无条件执行）：

```asm
    /* 所有目标都是 M-mode only；协作式栈帧缺 MPP，必须补 M。
     * 不按目标/能力加 #if：谓词会漏掉 CLIC 目标。 */
    li    t1, 0x1800
    or    t0, t0, t1
    csrw  mstatus, t0
```

等价性说明：本文件只在 `CONFIG_IDF_RTOS_RTTHREAD=y` 且组件白名单（C3/C6/S31）内编译，
三个目标全是 M-mode only，所以"无条件"与"列三个目标宏"语义完全相同，但少了后续维护负担。
已重新构建验证：镜像大小不变（`0x2a980`）。

#### P1.5 sdkconfig.defaults

```
CONFIG_IDF_RTOS_RTTHREAD=y
CONFIG_FREERTOS_UNICORE=y            /* 关键：CPU1 不启动 */
CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y /* 必须与上一项一致，app_startup.c:51 有 #error 检查 */
CONFIG_FREERTOS_HZ=1000              /* 必须等于 RT_TICK_PER_SECOND */
```

`CONFIG_FREERTOS_UNICORE` 会让 `esp_system/Kconfig` 自动 `select ESP_SYSTEM_SINGLE_CORE_MODE`。
注意准确说法：`start_other_core()` 的**函数体是无条件编译的**（`cpu_start.c:308`），
被 `#if !CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE` 排除的是**调用点**（`cpu_start.c:718`）。
这个区别对 P3-a 很重要——函数还在，可以直接调，不必等 IDF 恢复调用点。
结果就是 **CPU1 保持复位状态**，正是 P1 想要的。

#### P1.6 应用侧最小验证代码

```c
/* main.c —— 注意 RT-Thread 接管后 main 是 RT-Thread 的 main 线程 */
#include <rtthread.h>
#include "esp_rom_serial_output.h"   /* esp_rom_output_putc，见 esp_rom/include/ */

void rt_hw_console_output(const char *str)   /* 组件 README 要求应用提供 */
{
    /* P1 用 IDF 的 ROM UART 输出，避免过早依赖 driver/uart */
    while (*str) { esp_rom_output_putc(*str++); }
}

static void thread_a(void *p) { while (1) { rt_kprintf("A %d\n", rt_tick_get()); rt_thread_mdelay(1000); } }
static void thread_b(void *p) { while (1) { rt_kprintf("B\n"); rt_thread_mdelay(1500); } }

int main(void)
{
    rt_kprintf("RT-Thread %s on ESP32-S31, tick=%d Hz\n", RT_VERSION, RT_TICK_PER_SECOND);
    rt_thread_t t1 = rt_thread_create("ta", thread_a, RT_NULL, 2048, 10, 10);
    rt_thread_t t2 = rt_thread_create("tb", thread_b, RT_NULL, 2048, 12, 10);
    rt_thread_startup(t1); rt_thread_startup(t2);
    return 0;
}
```

**P1 的调试顺序**（不要跳步，每步验证一个假设）：
1. 只打印 banner，不进调度器 → 验证栈、链接、串口
2. 单线程 `rt_thread_mdelay` → 验证 tick
3. 双线程不同优先级 → 验证上下文切换
4. 双线程同优先级 + 时间片 → 验证 `RT_SCHED_ROUND_ROBIN`
5. 连续跑 30 分钟 + `rt_tick_get()` 对表

**DoD**：上述 5 步全过。**这是整个项目最关键的一道坎**——过了 P1，剩下都是工程量。

---

### P2 · 中断、设备框架与 msh（8～12 人日）

#### P2.1 中断注册桥接（ABI 与编号，两处都必须先定死）

**抽象选边**：不要启用 RT-Thread 的 `libcpu/risc-v/common/pic/pic-riscv-clic.c`——
它直接管 CLIC 的 IE/IP 位，会和 IDF 的中断矩阵争同一批寄存器。正确做法是**薄封装 IDF 的注册表**，
让 `esp_intr_alloc` / `intr_handler_set` 保持唯一事实来源（Wi-Fi/BT 闭源库的中断也走这条路）。

##### 三个编号体系（最容易写错的地方）

| 编号 | 取值 | 出处 | 用途 |
|---|---|---|---|
| **ETS INUM**（CPU 中断线号） | 0..31 | `soc.h`：`ETS_UART0_INUM=5`、`ETS_GPIO_INUM=4`、`ETS_IPC_ISR_INUM=28` | **注册 API 要的就是它**（`esp_cpu.h:445` 注释 "Interrupt number (from 0 to 31)"） |
| mtvt 槽位 | INUM + 16 | `RV_EXTERNAL_INT_OFFSET=16`（`csr_clic.h:29`） | CPU 硬件跳转用，**不要传给注册 API** |
| mcause | INUM + 16 | 同上 | 分发时 `intr_get_item(mcause - 16)`（`interrupt.c:67`） |

即：**`rt_hw_interrupt_install(vector)` 的 `vector` = ETS INUM**。
传 mtvt 槽位（比如 UART0 传 21 而不是 5）的表现是"注册成功、但永远不进 ISR"，而且不报错——
所以封装里必须断言 `0 <= vector < SOC_CPU_INTR_NUM`。

##### ABI 差异：不能强转

```c
typedef void (*intr_handler_t)(void *);                 // riscv/interrupt.h:33
typedef void (*rt_isr_handler_t)(int vector, void *param); // rt-thread/include/rthw.h:109
// 分发点 interrupt.c:68 只传一个参数：
(*item->handler)(item->arg);
```

`intr_handler_set(v, (intr_handler_t)handler, param)` 是 **ABI 错误**：RT-Thread 风格的 ISR 会被
以 `(param, 垃圾)` 调用——`vector` 拿到的是 `param`，`param` 是随机值。必须用固定签名的**蹦床 + 上下文对象**：

```c
/* port/esp32s31/interrupt.c */
struct rtt_isr_ctx {
    rt_isr_handler_t fn;
    void            *param;
    int              vector;      /* ETS INUM */
};

static struct rtt_isr_ctx s_ctx[SOC_CPU_INTR_NUM];

/* 与 intr_handler_t 同签名，这是唯一允许被 intr_handler_set 直接登记的函数 */
static void rtt_trampoline(void *arg)
{
    struct rtt_isr_ctx *c = (struct rtt_isr_ctx *)arg;
    c->fn(c->vector, c->param);
}

rt_isr_handler_t rt_hw_interrupt_install(int vector, rt_isr_handler_t fn,
                                         void *param, const char *name)
{
    if (vector < 0 || vector >= SOC_CPU_INTR_NUM) {
        return RT_NULL;                 /* 传了 mtvt 槽位会在这里被挡下 */
    }
    rt_isr_handler_t old = s_ctx[vector].fn;

    s_ctx[vector] = (struct rtt_isr_ctx){ fn, param, vector };
    intr_handler_set(vector, rtt_trampoline, &s_ctx[vector]);
    return old;
}
```

注意 `intr_handler_get(vector)` 返回的是蹦床指针而不是原 handler，取旧 handler 要查侧表（如上）。

##### 共享中断：单槽位表不够用

`ETS_UART0_INUM == ETS_UART1_INUM == 5`——两根 UART 共用一条 CPU 中断线。
上面的单槽位实现只能"后者覆盖前者"，无法共享。因此：

- **首选**：驱动直接走 `esp_intr_alloc(source, flags, rtt_trampoline, ctx, &handle)`。
  `source` 是 `ETS_xxx_INTR_SOURCE`（矩阵源号，不是 INUM），由 IDF 负责选线、路由、共享链、优先级；
  此时蹦床的 `arg` 是调用方自己分配的 `struct rtt_isr_ctx*`，与上面共用同一个 `rtt_trampoline`。
- **兼容层**：`rt_hw_interrupt_install(vector=INUM, …)` 只用于"矩阵已由 IDF/ROM 路由好"的场景
  （例如 Wi-Fi 适配层的 `set_isr`）。

##### 使能 / 屏蔽 / 优先级

- `rt_hw_interrupt_mask/unmask` → `esprv_int_disable/enable(1U << inum)`（或 CLIC `INT_IE` 位）
- `rt_hw_interrupt_set_priority` → CLIC `INT_CTL`（8 位优先级）+ 阈值 `mintthresh`
- CLIC 下 `mcause` 含 `MINHV/MPP/MPIL`，已被 `VECTORS_MCAUSE_REASON_MASK = 0xfff` 掩掉，分发侧无需再处理

**工作量修正**：蹦床 + 侧表 + 断言 + 屏蔽/优先级映射 **≈ 120 行**（原先估 80 行偏乐观）。

#### P2.2 设备框架与驱动（方案已按实测修订）

**设备框架**：打开 `RT_USING_DEVICE` / `RT_USING_CONSOLE` / `RT_USING_FINSH`。

**UART —— 不能直接用 `esp_driver_uart`**（本轮新发现，推翻原方案）：

`components/esp_driver_uart/src/uart.c` 里有 `xSemaphoreTake` / `xSemaphoreGiveFromISR` /
`xQueueSendFromISR` / `xTaskGetTickCount`（4 个源文件里 3 个调 FreeRTOS 原语）。
在 RT-Thread 模式下 FreeRTOS 内核不参与构建，这些符号会 undefined。
当前镜像之所以链接通过，是因为**没有任何代码引用 `uart_driver_install`**——
`nm s31_rtt_test.elf | grep uart_driver_install` 的结果是 0，整个对象根本没被拉进来。
一旦 P2 真的去调它，立刻链接失败。

两条可行路径：

| 路径 | 做法 | 评价 |
|---|---|---|
| B1 | 接 `freertos-wrapper`（`queue.c`/`tasks.c`/`timers.c`/`event_groups.c` 映射到 `rt_*`），让 `esp_driver_uart` 原样可用 | 面大，ISR 语义要对齐（`FromISR` 系列、临界区），需单独评估 |
| **B2（推荐）** | 绕开 driver 层，直接用 `hal/uart_hal.h`（LL 层，无 FreeRTOS 依赖）实现 `rt_serial` 后端 | 代码量小、可控，代价是放弃 IDF 的 DMA/事件队列便利 |

**GPIO —— 可以用 driver 层**：`esp_driver_gpio/src/gpio.c` 本身 **0 处** FreeRTOS 调用，
直接封装成 `rt_pin_ops` 即可；只有中断分配/去抖等子功能可能牵涉 FreeRTOS，按需取舍。

**msh 必须有输入路径**：P1 只提供了 `rt_hw_console_output()`，msh 还需要
`rt_hw_console_getchar()`（或把 console 注册成设备走读路径）。没有它 msh 只能打印、不能交互，
P2 的 DoD 无法达成。

#### P2.3 关键验证

- UART 中断收发 1 MB 无误码
- GPIO 中断 1 kHz 触发 10 万次不丢
- `msh` 下 `list_thread` / `list_timer` / `free` 正常
- 中断里 `rt_thread_mdelay` 应触发断言（验证上下文判断正确）

**DoD**：msh 可交互，UART/GPIO 可用，连续运行 2 小时无异常。

---

### P3 · 双核 SMP（3～5 人周）

这是唯一"没有现成答案"的阶段。三个子问题：

#### P3.1 启动第二个核

**冲突点**：IDF 在 `do_secondary_init()` 阶段就启动了 CPU1 并等它到位（`cpu_start.c:321` `start_other_core()`），
而 RT-Thread 的 `rt_hw_secondary_cpu_up()` 期望由内核在调度器就绪后启动从核。

**两条路**：
- **P3-a（推荐先试）**：保持 `CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y` 让 IDF 不碰 CPU1，
  由 `rt_hw_secondary_cpu_up()` 调用 `esp_cpu_unstall(1)` + `ets_set_appcpu_boot_addr(call_start_cpu1_rtthread)`。
  需要自己写一个精简版 `call_start_cpu1`（不调用 IDF 的 `do_secondary_init`，只做 gp 初始化 + 设向量表 + 进 RT-Thread 从核入口）。
- **P3-b（备选）**：让 IDF 正常启动 CPU1，把 RT-Thread 的从核入口挂在 `do_secondary_init` 之后。
  改动小，但与 IDF 的启动顺序强耦合，且 `startup.c` 现有的 `s_system_full_inited` 机制要重新对齐。

#### P3.2 内核侧

- `RT_USING_SMP` + `RT_CPUS_NR=2`
- `kernel_srcs` 里 `scheduler_up.c` → `scheduler_mp.c`
- `context_gcc.S` 已有 SMP 分支，但要确认 `rt_hw_context_switch_interrupt` 的签名与 `vectors_rtthread.S` 的切换逻辑匹配
- **`vectors_rtthread.S` 的中断退出路径是 UP 专用**：它用 `rt_interrupt_from_thread` / `to_thread` 做切换，
  SMP 下要改成 `rt_hw_context_switch_interrupt(from, to, from_thread, to_thread)` 语义。**这是 P3 最大的代码量**。

#### P3.3 核间通信与 per-core tick

- IPI：复用 IDF `esp_crosscore_int`（S31 的 `ETS_IPC_ISR_INUM = 28`，见 `soc/esp32s31/include/soc/soc.h:223`；
  注意不是 `vectors_clic.S:115` 里那个 44——那是通用文件里的表项序号，不是 S31 的中断号）
- **IPC 绕过 RT-Thread 入口**：`vectors_clic.S:115` 的 44 号槽在 `CONFIG_ESP_IPC_ISR_ENABLE=y` 时指向
  `esp_ipc_isr_handler`，**不经过 `rt_interrupt_enter/leave`**。因此走 IPI 时 `rt_interrupt_nest` 不会计数，
  若 ISR 里调用了任何依赖"当前在中断上下文"的 RT-Thread API，判断会错。
  P3 二选一：把 44 号槽改指向 `_interrupt_handler` 并由 RT-Thread 分发，或关掉 `CONFIG_ESP_IPC_ISR_ENABLE`
  自建 IPI 通道。
- tick：`port_systick.c` 已支持 `SYSTIMER_ALARM_OS_TICK_CORE0 + cpuid`，但 `vPortSetupTimer()` 只在
  `cpuid==0` 时使能 counter，从核需要单独 `vSystimerSetup()` 一次
- 全局变量：`rt_interrupt_nest` 在 SMP 下是 per-cpu 的，向量里的 `lw` 要改成读 `mscratch`/`tp` 偏移

**DoD**：双核各跑 2 个线程，`spinlock` 压测 24h，线程迁移正常，`ps` 能看到两个核的 idle。

---

### P4 · 外设扩展（按需，不排期）

按优先级：PSRAM（`CONFIG_SPIRAM`，注意 S31 是 DDR PSRAM，最高 250 MHz）→ LCD/JPEG → 以太网 → Wi-Fi/BT。

---

## 6. 关键技术设计详解

### 6.1 为什么 CLIC 不是难点（修正上一轮的判断）

上一轮我说"难点是 CLIC"，实测代码后需要修正：

- RT-Thread **有** CLIC 驱动（`pic-riscv-clic.c`，533 行），但那是给裸 CLIC SoC 的；
- S31 的 CLIC 被 IDF **包在中断矩阵后面**：外设 → 矩阵 → CPU 中断线（16..47），
  软件只通过 `esp_intr_alloc` / `intr_handler_set` 操作；
- 因此正确做法是**放弃 RT-Thread 的 CLIC 驱动，只做薄封装**，反而比 C3 的 PLIC 路径更简单。

真正的难点是：**S31 的 CLIC 硬件嵌套（`SOC_INT_HW_NESTED_SUPPORTED=1`）与 RT-Thread 的
软件嵌套计数（`rt_interrupt_nest`）如何对齐**。`vectors_rtthread.S:141-147` 已有对应分支：

```asm
#if SOC_INT_HW_NESTED_SUPPORTED
    csrw  mcause, s1     /* 恢复本级入口的 mcause，mret 用它还原 mintstatus */
#endif
```

S31 会走这条路径。P1 必须**实测**：中断嵌套 3 层时 `rt_interrupt_nest` 是否为 3、退出是否正确。

### 6.2 栈帧布局

RT-Thread 的 RISC-V 端口用 32 个 `REGBYTES` 的帧（`0..31` 槽位），槽位约定：
`0=mepc, 1=ra, 2=mstatus, 3=gp, 4=tp, 5..31=x5..x31`。

`context_gcc.S` 相对上游的**唯一改动**是"gp 存到槽位 3"（见文件头注释），
`rtthread_stack_init.c` 再补 `gp`/`tp`（picolibc TLS）。S31 沿用，不需要改。

### 6.3 中断分发链路与向量表真实内容

```
外设 → 中断矩阵 → CLIC → CPU
                        ↓
   _mtvt_table[INUM + 16]   ← 复用 vectors_clic.S 的表，**不要重复定义**
                        ↓
        vectors_rtthread.S：存 32 寄存器 → 切中断栈 → rt_interrupt_enter()
                        ↓
   _global_interrupt_handler(sp, mcause & 0xfff)      [IDF interrupt.c:59]
                        ↓
   intr_get_item(mcause - 16) → 你的回调              [interrupt.c:67]
                        ↓
   rt_interrupt_leave() → 若 flag 置位则切栈 → mret
```

**`_mtvt_table` 的实际槽位内容**（`vectors_clic.S:71-118`，S31 下 `slot = INUM + 16`）：

| 槽位 | 入口 | 说明 |
|---|---|---|
| 0–15 | `_system_int_handler`（= `_panic_handler`） | 系统内部中断，不应发生 |
| 16–39 | `_interrupt_handler` | **自由中断，走 RT-Thread 入口** ✓ |
| 40 | `_panic_handler` | `ETS_INT_WDT_INUM`(24) |
| 41 | `_panic_handler` | `ETS_CACHEERR_INUM`(25) |
| 42 | `MEMPROT_ISR` = `_interrupt_handler` | `ETS_MEMPROT_ERR_INUM`(26) |
| 43 | `ASTDBG_ISR` | `ETS_ASSIST_DEBUG_INUM`(27)；开 `CONFIG_ESP_SYSTEM_HW_STACK_GUARD` 时改为 panic |
| 44 | `IPC_ISR_HANDLER` | `ETS_IPC_ISR_INUM`(28)；开 `CONFIG_ESP_IPC_ISR_ENABLE` 时是 `esp_ipc_isr_handler`，**绕过 RT-Thread 入口** |
| 45–47 | `_interrupt_handler` | 自由 |

> 验证时不要写"16–47 全是 `_interrupt_handler`"。40/41 是硬编码 panic，44 在开 IPC ISR 后会绕过
> `rt_interrupt_enter/leave`——P3 用 `esp_crosscore_int` 做 IPI 时，这条路径的 `rt_interrupt_nest`
> 是不完整的，必须单独处理（见 §P3.3）。

### 6.4 tick 的来源

`board.c` 调 `vPortSetupTimer()` → `vSystimerSetup()` → `systimer_hal_set_alarm_period(1000000/HZ)`，
ISR 里 `xPortSysTickHandler()` 被 rtt-dev 分支改成：

```c
#if CONFIG_IDF_RTOS_RTTHREAD
    rt_base_t level = rt_hw_interrupt_disable();
    rt_tick_increase();
    rt_hw_interrupt_enable(level);
    return pdFALSE;
#endif
```

**注意**：这条路径要求 `CONFIG_FREERTOS_HZ == RT_TICK_PER_SECOND`，否则 `rt_tick_get()` 与实际时间不符。
建议在 `rtconfig.h` 加编译期断言。

### 6.5 IRAM / cache 安全

`linker.lf` 已把 ISR 关键路径（`irq`、`context_gcc`、`rt_enter_critical`、`rt_thread_self`、
`rt_cpu_self`、`rt_hw_interrupt_is_disabled`）放进 IRAM（`noflash`）。
P2 接驱动时必须遵守：**任何在 cache 关闭期间可能被调用的代码都不能放 flash**，
否则会触发 IDF 的 cache-disabled 断言。

---

## 7. 改动文件清单（P1 已落地，行数为实测）

| # | 文件 | 类型 | 实测行数 | 风险 |
|---|---|---|---|---|
| 1 | `components/freertos/Kconfig` | 改 | +2/−2 | 低 |
| 2 | `components/rtthread/CMakeLists.txt` | 改 | +21/−8 | 低 |
| 3 | `components/riscv/vectors_rtthread.S` | 改 | +106/−1（中断帧 FP 布局 + MPP 修复） | **高**（R9 的修复点） |
| 4 | `components/esp_system/ld/esp32s31/sections.ld.in` | 改 | +51（中断栈/堆/符号表） | 中 |
| 5 | `components/rtthread/port/esp32s31/rtconfig.h` | 新 | 62 | 低 |
| 6 | `components/rtthread/port/esp32s31/cpuport.c` | 新 | 117 | 低（复制 C6） |
| 7 | `components/rtthread/port/esp32s31/interrupt.c` | 新（P2，已完成 `36fc7f4f`） | 131（蹦床+侧表+断言） | 中（ABI/编号已在代码注释里定死） |
| 8 | `components/esp_system/startup.c` | 改（P3） | ~30 | **高** |
| 9 | `sdkconfig.defaults` / 应用 `main.c` | 新 | 5 / 60 | 低 |

**P1 实测合计：6 个文件、+374/−11 行**（不含应用示例；含中断帧修复 `c948a462`）。
**P2 首批增量：3 个文件、+184/−4 行**（中断桥接 + 控制台/msh 接入，`36fc7f4f`）。
**P2 第二批：4 个文件、+202/−5 行**（UART0 串口驱动 + 设备框架接入，`a3c805fb`）。
这就是"C3 端口只有 171 行"这个数字的意义——在这个架构下，移植一颗同架构新芯片的成本是
"配置 + 一段链接脚本 + 一处汇编修复"，不是"重写内核"。

## 8. 风险登记册

| # | 风险 | 触发条件 | 影响 | 应对 |
|---|---|---|---|---|
| R1 | **S31 是 preview target** | `idf.py` 提示 preview；IDF 自身在 S31 上有未修复 bug | 高 | P0 先用 stock IDF 跑 hello_world 隔离问题；关注 `release/v6.1` 的 s31 提交 |
| R2 | **无硬件** | 手上没有 S31 板子 | ~~高~~ **已解除**（2026-10-30） | 硬件为 ESP32-S31 开发板（COM16 / 2 MB flash / 320 MHz 单核）。P1 之后不再受"无硬件"阻塞；但首次上板即暴露 6 个启动缺陷（§12.18），说明"有硬件"才是验证的起点。**风险转移为 R21** |
| R3 | 重复定义 `_mtvt_table` | 照旧版本文档自己补表 | 中（显性失败） | **不要定义**，复用 `vectors_clic.S` 的；链接期 duplicate symbol 会直接挡住 |
| R4 | CLIC 嵌套计数错乱 | 3 层嵌套时 `rt_interrupt_nest` 不归零 | 高 | P2 加断言 + GPIO 翻转配示波器观察 |
| R5 | 双核启动冲突 | IDF 与 RT-Thread 都去启动 CPU1 | 高 | 坚持 P3-a，`SINGLE_CORE_MODE=y` 期间不让 IDF 碰 CPU1 |
| R6 | **Wi-Fi/BLE 需要额外兼容层** | ~~射频闭源库内部依赖 FreeRTOS 语义~~ → **实测推翻**：7 个 Wi-Fi 库（4.0 MB）219 个外部符号、3 个 BLE 控制器库（4.7 MB）67 个外部符号，**FreeRTOS 引用均为 0**。耦合全部在可改的 C 源码里（`esp_adapter.c` 831 行 + IDF C 层 33 个 FreeRTOS API） | 中 | 见 `rtthread-esp32-s31-wifi-feasibility.md`：写 `ft_*` 兼容层（30 个 API），Wi-Fi 3~5 人周、BLE 再加 1~2 人周。剩余不可控项只有 PHY 校准与射频时序 |
| R7 | 中断里调调度器导致崩溃 | 应用/驱动代码不规范 | 中 | P2 的 DoD 明确要求"中断里 `rt_thread_mdelay` 触发断言" |
| R8 | 内存不足 | 512 KB SRAM，IDF 自身占用大 | 中 | P0 记录基线；RT-Thread 堆单独划分（`IDF_RTTHREAD_HEAP_SIZE`） |
| **R9** | **中断帧与协作帧尺寸不一致（两层，均已修）** | 协作帧/线程初始帧是 **256 B**（32 整数槽 + 32 FP 槽，`rt_hw_stack_frame_t` 与 `context_gcc.S` 一致），而中断帧只有 **128 B**。trap 出口无条件 `+128`，一旦恢复协作式保存的线程，**sp 就低 128 B**——纯整数线程 + 高频抢占同样会崩，不必等 float 出现；第二层才是中断路径不保存 `f0–f31`，浮点线程被抢占后串值 | **高** | **已修**（`c948a462`）：`vectors_rtthread.S` 入口分配 FP+整数共 256 B、保存 `f0–f31`，出口对称 `+256` 并恢复。验证方式见 §9 的"纯整数 + 强制抢占"与"FPU 现场"两条 |
| **R10** | **IDF 驱动层依赖 FreeRTOS 原语** | `esp_driver_uart/src/uart.c` 调 `xSemaphore*`/`xQueue*`/`xTaskGetTickCount`（4 个源文件里 3 个）；当前镜像没链接它所以未暴露 | **高** | P2 已走 LL 层（`hal/uart_hal.h`）自写 `rt_serial`；要回用 IDF 驱动层则走 `ft_*` 兼容层（可行性评估 §4） |
| **R11** | **没有硬件看门狗（比原判断轻）** | 原判断是"task WDT 5 s 会误复位"。实测：`esp_int_wdt_init()` 在 `app_startup.c:64` 的 `esp_startup_start_app()` 里，`CONFIG_ESP_TASK_WDT_INIT` 的初始化在 `main_task()`（`:197`）——**两者都是 FreeRTOS 专属路径，RT-Thread 模式下完全不执行**。bootloader WDT（9 s）只覆盖 bootloader，跳转前已由 IDF 关闭 | 低 | 不会误复位，也无需改 sdkconfig。**代价是整套系统没有硬件看门狗**：P3 之后要把喂狗接到 RT-Thread idle 线程并重新初始化 `esp_task_wdt`，或在 TIMG 上自建 WDT 驱动 |
| **R12** | **SIMD/协处理器状态未知** | `SOC_SIMD_INSTRUCTION_SUPPORTED=1`、`SOC_CPU_COPROC_NUM=3`；IDF 只保存 32 个 FPU 寄存器（`fsw`，32-bit） | 中 | 查 TRM 确认 SIMD 是否复用 FPU 寄存器组；若另有独立状态，需扩展 `context_gcc.S`。先用"写满寄存器再切换"的实验定位 |
| **R13** | **msh 无输入路径** | P1 只实现 `rt_hw_console_output()` | 中 | P2 补 `rt_hw_console_getchar()`，否则 msh 只能打印不能交互 |
| **R14** | **P3-a ROM API 部分未验证** | `ets_set_appcpu_boot_addr` 已确认在 ROM 导出表（`esp32s31.rom.ld:57` = `0x2f8000a8`）；`esp_cpu_unstall(1)` 在 SINGLE_CORE 构建下是否被裁剪未验 | 中 | P3 开工前用 `nm` 查 `libesp_hw_support.a`；先写 20 行"CPU1 起跳打印"冒烟实验 |
| **R15** | **中断矩阵复位全指向 CPU 线 0** | 每个 `INTERRUPT_CORE0_*_MAP` 复位值为 0。只 install 不 route 的表现是"注册成功、永不触发"，**不报错** | **高**（已修复） | 所有驱动必须 `rt_hw_intr_route(source, inum)` 后再 install；`port_intr.h` 已写明 |
| **R16** | **CLIC 优先级 0 等于被屏蔽** | `mintthresh` 为 0（编码 0x1F），外部中断复位级别也是 0（0x1F），`>` 判定不成立 | **高**（已修复） | `rt_hw_interrupt_install()` 现在补 1 级；需要嵌套时先调 `rt_hw_interrupt_set_priority()` |
| **R17** | **系统当前没有硬件中断嵌套** | UART0 与 GPIO 都是 1 级，同级别不可互相抢占 | 中 | R4 的验证必须先把两个源设成不同级别，否则测出来的"无嵌套"是配置造成的，不是实现问题 |
| **R18** | **`esp_ringbuf` 持临界区阻塞，与 RT-Thread 不兼容** | `ringbuf.c:816` 在 `portENTER_CRITICAL` 内调 `vTaskPlaceOnEventList`；RT-Thread 下该临界区同时锁调度器，`rt_sem_take` 会返回错误但线程已离开就绪队列 | **高**（判定不可行） | 外设驱动走 LL 层（现状）；影响面**不含 Wi-Fi/NimBLE**，详见可行性文档 §4.2 |
| **R19** | **兼容层里静默的"持锁阻塞"** | 任何在 `rt_enter_critical()`/IDF 临界区内要求阻塞的调用 | **高**（已缓解） | `ft_block_allowed()` 守卫：返回 `FT_ERR_STATE` 并打印一次；`s31test fbshim/guard` 覆盖 |
| **R20** | **flash 映射段数超过 bootloader 上限** | `SOC_MMU_DI_VADDR_SHARED=1` 时只允许 2 个 LOAD 段；任何"NOBITS 之后接 PROGBITS"的段布局都会多切一个段 | **高**（已修复） | 符号表并入 `.flash.rodata`；新增链接片段后要 `readelf -l` 数 LOAD 段，不能只看符号地址（§12.18 缺陷 1） |
| **R21** | **"调度器还没起来"这个窗口没有语义约定** | RT-Thread 在该窗口 `rt_thread_self()` 返回 NULL，而 FreeRTOS 对它有明确定义（加锁空操作、`taskSCHEDULER_NOT_STARTED`） | **高**（已修复 4 处） | 已补：堆初始化顺序、`ft_task_scheduler_state`、互斥量 owner 判定、`ft_ipc_take/give` 的 NULL 线程短路。**新增启动路径代码时须重新审视这一窗口**（§12.18） |
| **R22** | **恢复路径的 `mstatus` 语义没有单一来源** | `context_gcc.S` 与 `vectors_rtthread.S` 各自写自己的 mstatus 掩码；本次那个 `0x1800` vs `0x7800` 的不一致就是两者独立维护的结果（§12.19 第七个缺陷） | 中 | 两处现在都按 `RT_FPU_FRAME` 取 `0x7800`/`0x1800`，判据一致；`tools/check_image.py` 反汇编核对 trap 退出路径的立即数。长期修法是把"恢复一帧需要哪些 mstatus 位"抽成一个宏，但那是同步到上游之后的事 |
| **R23** | **句柄在对象死亡后仍被使用** | 线程从 entry `return` 后被 idle 线程回收（释放栈 + 线程对象），调用方再对其 `rt_thread_delete()` 就是双重释放。按 `stat == CLOSE` 判"已退出"也不可靠：那一刻对象已被释放并复用，读到的是新主人的字段 | **高**（已修复） | `port/freertos/ft_task.c` 的退出标记表：指针还唯一时记录，`ft_task_create()` 复用地址时清除，`ft_task_delete()` 命中记录则不动作。**凡是"拿句柄去删/停/改"的路径都要重新审视这一条** |
| **R24** | **兼容层静态表在 SMP 下不互斥** | `ft_tls` / `s_notify` / `s_exited` / `s_pending` 都是单份静态表，只用 `rt_hw_interrupt_disable()` 保护；SMP 下那只是关本核中断，两个核同时写会互相覆盖 | **P3 前置**（未修） | 换成 `rt_spin_lock_irqsave()` 或改成 per-CPU 表。**P3 开工前必须先做这一步**，否则双核起来后 TLS 和高水位查询会随机错 |
| **R25** | **sdkconfig 与 sdkconfig.defaults 不同步 -> 构建不可复现** | `CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM` 只写在 sdkconfig 里、没进 defaults；删掉 sdkconfig 重新生成后链接立刻失败（`ft_task_notify` / `ft_notify_forget` 未定义） | 已修 | defaults 是唯一可复现的配置来源。检查法：**删掉 sdkconfig 后必须仍能构建** |

> 已核对并**证伪**的一条：评审怀疑 `driver/uart` 的 RX timeout 依赖 `esp_timer`。
> 实测 `esp_driver_uart` 全组件只有 `test_apps/` 引用 `esp_timer`，
> `uart.c` 的 RX 超时是硬件 `UART_TOUT`（`uart_hal_set_rx_timeout`）。
> 真正的依赖是 R10 的 FreeRTOS 原语，不是 `esp_timer`。

## 9. 验证矩阵

| 层级 | 验证项 | 手段 | 通过标准 |
|---|---|---|---|
| 编译 | 全量构建 | `idf.py build` | 0 error |
| 链接 | 符号完整性 | `nm` 查 `_mtvt_table`/`_interrupt_handler`/`__stack_end__`/`__heap_start__` | 全部有定义；`_mtvt_table` 地址低 6 位为 0 |
| 链接 | 向量表槽位 | dump `_mtvt_table` 前 64 个字 | 16–39、45–47 = `_interrupt_handler`；40/41 = `_panic_handler`（**不是全表一致**） |
| 静态 | 栈用量 | `idf.py size-components` + `-fstack-usage` | 中断栈 < 40 KB 的 60% |
| 运行 | 启动 | 串口 | banner + 版本号正确 |
| 运行 | tick 精度 | `s31test tick`（SYSTIMER + CPU 周期双时钟源） | `1000 us/tick`，误差 < 5% |
| 运行 | 自检汇总 | `s31test`（6 项一起跑） | `6 passed, 0 failed` |
| 运行 | 上下文切换 | 双线程 1000 次切换 | 无栈溢出、无异常 |
| **运行** | **纯整数 + 强制抢占** | 两线程纯整数循环，1 kHz tick 强制抢占 10 万次，校验栈哨兵与返回地址 | **sp 不漂移**（专打 R9 第一层：帧尺寸不一致） |
| **运行** | **FPU 现场** | 两个线程各写满 `f0–f31` 再校验，交替 10 万次 | 值不串（R9 的验证手段） |
| **运行** | **SIMD 现场** | 两个线程各写 SIMD 累加器后切换 | 值不串（R12） |
| 运行 | 中断 | `s31test irq <pin>`（软件置位，免接线） | 200/200 |
| 运行 | 中断（外接） | GPIO 1 kHz × 10 万次 | 计数一致，无丢失 |
| 运行 | 嵌套 | 3 层嵌套中断 | `rt_interrupt_nest` 归零 |
| **运行** | **WDT** | 不做任何喂狗，静置 60 s | 不复位（R11 的实测结论：RT-Thread 下 WDT 未初始化）。若将来接了 WDT，此项改回"喂狗可阻止复位" |
| 压力 | 长跑 | 24 小时 | 不复位，`free` 无持续下降 |
| 双核 | SMP | spinlock 压测 | 24 小时无死锁 |

## 10. 里程碑

| 里程碑 | 内容 | 累计 |
|---|---|---|
| M0 | IDF 基线在 S31 上跑通 | 1 天 |
| M1 | RT-Thread banner + 单线程 tick | 3 天 |
| M2 | 双线程切换稳定（**P1 完成**） | 5～6 天 |
| M3 | 中断 + UART/GPIO + msh（**P2 完成**） | 3.5～5 周 |
| M4 | 双核 SMP（**P3 完成**） | 6.5～10 周 |
| M5 | 外设扩展 / Wi-Fi-BLE（独立里程碑） | 按需 |

## 11. 需要你确认的 4 件事

1. **硬件**：手上有没有 ESP32-S31 板子？型号是哪个（Function-CoreBoard-1 / Korvo-1 / Mosaico / 自制）？
   没有板子的话，P1 之后只能做编译验证，方案要相应缩水。
2. **改动范围**：允许直接改 `<esp-idf>`（rtt-dev 分叉）吗？还是希望另开一个分支/副本？
3. **目标形态**：先做"能跑的最小系统"（P1），还是直接奔"可用 BSP"（P1+P2）？
4. **Wi-Fi/BT 是否硬需求**？如果是，整个方案的工作量要翻倍，且成功率不高，需要单独立项评估。

---

## 附：为什么不用路线 A（一句话）

路线 A 的本质是"用 SCons 手工复刻 IDF 的构建图"。C3 的 `SConscript` 已经 1410 行、
`sdkconfig.h` 固化 841 行，而 S31 是 preview target，源码清单还会变。
在没有跑通任何一行 RT-Thread 代码之前，先花两周维护一份会过期的源码清单，是错误的下注顺序。

---

## 12. 实施进度（2026-10-26，含评审回写）

**P1 的编码部分已完成，当时状态：编译 + 链接通过，未上板。**（2026-10-30 已上板，见 §12.18）

- 分支：`esp-idf-rtt` → `rtt-s31`，提交 `84439686`（评审后又追加一处汇编改动，见下）
- 验证工程：`project/`，产物 `s31_rtt_test.bin` 171 KB
- ELF 级验证：`_mtvt_table` @ `0x2f000040`（64 字节对齐）；**槽位 16–39、45–47 → `_interrupt_handler`**，
  40/41 → `_panic_handler`（**不是全表一致**，详见 §6.3）；中断栈 40 KB + 堆 40 KB；`rtthread_startup`/`entry` 就位
- 详细记录与踩坑清单：`rtthread-esp32-s31-porting-log.md`

**评审后修正的 5 件事**（均已回写本文）：

1. §P2.1 重写：蹦床 + 侧表，明确 `vector` = **ETS INUM**（mtvt 槽位 = INUM+16），共享中断走 `esp_intr_alloc`；
2. §6.3 图注与 §R3 与 §P1.3 对齐，删除"P1 新增 48 项"、"`.rept` 表"等过期表述；
3. 向量表槽位按 40/41/42/43/44 实况改写，§9 验证矩阵同步（不再按"全表一致"判断）；
4. §P1.4 改口：**代码已改为无条件执行**，并说明为什么 `!SOC_INT_HW_NESTED_SUPPORTED` 谓词是错的；
5. 行号回写（`cpu_start.c:252`、`startup.c:267/287-289`、`CMakeLists.txt:22`），`rtconfig.h` 样例与实物对齐。

**方案被实测推翻/新增的三点**：

1. `_mtvt_table` 不需要新增（`vectors_clic.S` 已提供，重复定义会 duplicate symbol）；
2. CLIC 不是最大难点，"两套中断抽象选边 + ABI 对齐"才是；
3. **R9/R10 经二次核对后翻转**（详见 §8）：
   - R9 不是"rtconfig.h 没开 FPU 宏"（宏由 `__riscv_flen` 自动定义，协作式路径有 32 条 `fsw`），
     而是**中断帧比协作帧少 128 B**且不保存 `f0–f31`；已修并重新构建验证（32/32 对称、入口 −256/出口 +256）；
   - R10 不是"链接期 undefined"，而是**链接成功、运行时挂死**（`libfreertos.a` 无条件编译、符号齐全，
     阻塞 API 等不到调度）。P2 的 UART 方案维持"走 LL 层"。

**当前唯一阻塞项**：没有 ESP32-S31 硬件。tick / 上下文切换 / CLIC 嵌套 / FPU 现场四件事
都无法验证，P1 不能判定为完成。

> 该阻塞项已于 2026-10-30 解除（硬件：ESP32-S31 开发板，COM16）。首次上板连续暴露 6 个
> 启动缺陷，详见 §12.18。R2 的状态随之从"阻塞"改为"已解除，但运行验证才开始"。

### 12.1 P2 首批增量（2026-10-27，`36fc7f4f`）

已完成并编译验证：

- `port/esp32s31/interrupt.c`（131 行）：`rt_hw_interrupt_install/uninstall`、
  `mask/umask`、`set_priority`；蹦床 + 侧表解决 ABI 差异；`vector` 断言为 ETS INUM。
- 控制台与 shell：`RT_USING_CONSOLE`、`RT_USING_FINSH` + MSH、`RT_USING_COMPONENTS_INIT`。
- 应用侧：`rt_hw_console_getchar()` 走 UART0 LL（不依赖 `esp_driver_uart`），
  空 FIFO 时 `rt_thread_mdelay(1)` 让出 CPU（否则 tshell 会忙轮询饿死 idle）。
- 镜像 179,360 B；IRAM 42,424 B；`.flash.rt_thread` 里 `__fsymtab` 12 字节/项 ×2 条命令、
  `__rt_init` 含 `finsh_system_init`。

### 12.2 P2 第二批增量（2026-10-27，`a3c805fb`）

- `port/esp32s31/serial.c`（156 行）+ `port_serial.h`：RT-Thread serial **v1** 后端，
  注册 `uart0`，`RT_DEVICE_FLAG_INT_RX`，RX 走 `RXFIFO_FULL`/`RXFIFO_TOUT` →
  `ETS_UART0_INUM`(5) → `rtt_trampoline` → `rt_hw_serial_isr()`；TX 轮询。
  沿用 ROM 控制台配置（波特率/引脚/帧格式），只改写 RX 超时。
- 开启 `RT_USING_DEVICE` + serial v1；补 `components/drivers/core/device.c`、
  `components/drivers/ipc/completion_{up,comm}.c`。
- **中断桥接第一次真正被使用**：`rtt_trampoline` / `rt_hw_interrupt_install` 已出现在镜像中。
- 镜像 183,856 B；IRAM 42,424 B；DRAM data 9,914 B、bss 5,600 B；flash.text 94,412 B。

仍未做：`rt_pin_ops`（GPIO）、控制台改为设备输出（目前 `rt_kprintf` 仍走 `rt_hw_console_output`）、
可配置波特率（需要时钟树）。

### 12.3 P2 第三批增量（2026-10-27，`e61b3839`）：GPIO + 中断矩阵路由

本批修正了一个**会让上一批串口驱动完全失效**的问题。

#### 中断矩阵复位后全指向 CPU 线 0

写 GPIO 驱动时顺手核对"中断注册编号到底是哪一套"，结果发现更基础的问题：

```
每个 INTERRUPT_CORE0_<外设>_MAP 寄存器复位值都是 0
```

即上电后**所有外设中断源都映射到 CPU 中断线 0**，而不是 `ETS_*_INUM`。
上一批的 UART0 驱动直接 `rt_hw_interrupt_install(ETS_UART0_INUM=5, ...)`，
但矩阵里 UART0 还指向线 0 —— 结果会是"注册成功、使能成功、永远不进 ISR"，
而且**不报错**：UART 的 pending 位一直挂着，线 5 也没被置位。

实测确认（`interrupt_core0_reg.h`）：

| 外设 | MAP 寄存器 | 复位默认值 |
|---|---|---|
| UART0 | `INTERRUPT_CORE0_UART0_INTR_MAP`（0x0f4 附近） | **0** |
| UART1 | `INTERRUPT_CORE0_UART1_INTR_MAP` | **0** |
| GPIO_INTR0 | `INTERRUPT_CORE0_GPIO_INTR0_MAP`（0x0f4） | **0** |

修复：新增 `rt_hw_intr_route(source, inum)`，包装 ROM 的
`esp_rom_route_intr_matrix(0, source, inum)`（S31 的 `esp32s31.rom.api.ld:55`
把它 `PROVIDE` 到 `intr_matrix_set`，地址 `0x2f800768`）。
`serial.c` 与 `gpio.c` 都在 `rt_hw_interrupt_install()` 之前调用它。

> 编号关系再确认一次：`esp_rom_route_intr_matrix` 的第三个参数是**外部中断号**，
> ROM 自己加 `RV_EXTERNAL_INT_OFFSET`(16) 写进矩阵，所以
> `route(0, ETS_UART0_INTR_SOURCE, 5)` → 矩阵值 21 → `_mtvt_table[21]` →
> `mcause=21` → `intr_get_item(21-16=5)`。与 `intr_handler_set(5, ...)` 对齐。

#### GPIO：`port/esp32s31/gpio.c`（389 行）

`rt_pin_ops` 直接操作 GPIO LL，两个容易写错的点：

1. **必须先 `gpio_ll_func_sel(GPIO, pin, PIN_FUNC_GPIO)`**（`PIN_FUNC_GPIO=1`）。
   不选 IO_MUX 功能，后面 `input_enable`/`output_enable` 全都不起作用。
2. **输出要把外设矩阵断开**：`gpio_ll_set_output_signal_matrix_source(GPIO, pin, SIG_GPIO_OUT_IDX=256, false)`
   + `gpio_ll_set_output_enable_ctrl(GPIO, pin, false, false)`，
   否则 pad 仍由上一条路由到这里的外设驱动。

其余：所有引脚共用一个 ISR（`ETS_GPIO_INUM=4`），一次读 `intr_0` / `intr_01`
两个状态字再分发；开漏模式同时打开输入以便回读；`pin_debounce` 映射到 IO_MUX
的硬件毛刺滤波器（**只有开/关，宽度固定**）。

> S31 有 **4 条独立的 GPIO 中断通道**（`ETS_GPIO_INTR0..3_SOURCE`），这是相对
> C3/C6 的新特性。但 `gpio_ll_intr_enable_on_core()` 写死 `GPIO_LL_INTR0_ENA`
> 并带 IDF 的 TODO（IDF-7995），所以本驱动只用通道 0。

**受限引脚**：`SOC_GPIO_PIN_COUNT=62`，其中 GPIO29、GPIO41 被 SPI flash 占用，
`SOC_GPIO_VALID_GPIO_MASK` 已排除，`s31_pin_valid()` 会挡下。

#### 顺带修正：PIN 设备注册时机与链接

- `rt_hw_board_init()` 里调用 `rt_hw_pin_init()`：调度器起来之前就注册好，
  任何 init 阶段用 `rt_pin_*()` 的代码都能拿到设备。
- 这个调用**同时解决了归档成员被 `--gc-sections` 丢弃的问题**——之前
  `rt_device_pin_register()` 没有任何引用者，整个 `gpio.c.obj` 不会进镜像。
- `components/drivers/pin/dev_pin.c` 加入编译清单，**自带 `pin` msh 命令**
  （`MSH_CMD_EXPORT_ALIAS(_pin_cmd, pin, ...)`），无需自己写。

### 12.4 P2 第四批增量（2026-10-27，`d2a5d07d` + `7938c2bc`）：自检套件与 CLIC 优先级

#### CLIC 优先级：比矩阵更隐蔽的"装了但不触发"

追 GPIO 中断时发现的第二个致命问题：

```
CLIC 取中断的条件：int_ctl_byte > mintthresh_byte
board.c 设 mintthresh = RVHAL_INTR_ENABLE_THRESH = 0
CLIC_INT_THRESH(0) 编码成 0x1F
外部中断复位级别也是 0 → 编码同样是 0x1F
0x1F > 0x1F 不成立 → 中断永远不被取
```

也就是说：**路由对了、`int_ena` 开了、`esprv_int_enable()` 也调了，中断照样不来。**

IDF 自己不会踩到，因为 `esp_intr_alloc()` 每次都按 flags 写优先级
（`ESP_INTR_FLAG_LEVEL1 = 1`，见 `intr_alloc.c:819` 的 `esp_cpu_intr_set_priority`）。
我们的桥接层没写，所以每个装上去的中断都是 0 级。

修复（`interrupt.c`）：`rt_hw_interrupt_install()` 在装 handler 后检查
`esprv_int_get_priority(vector) == 0`，是则补成 1 级（`RTT_INTR_DEFAULT_LEVEL`）。
只在"仍是复位值"时补，所以驱动想分级嵌套可以先调 `rt_hw_interrupt_set_priority()`
再 install，不会被覆盖。

> 这条也顺带说明了 R4（CLIC 嵌套）的真实前提：**同级别中断不能互相抢占**。
> 当前 UART/GPIO 都是 1 级，所以**系统目前没有硬件嵌套**。要验证嵌套必须先把
> 两个源设成不同级别。

#### 自检套件：`port/esp32s31/selftest.c`（约 640 行）

P1/P2 全部是在没有硬件的情况下写的，有四件事只有板子能证：

| 命令 | 验证对象 | 手法 |
|---|---|---|
| `s31test tick` | tick 速率 | `rt_tick_get()` 增量 vs SYSTIMER OS-tick 计数器（1 MHz）vs `esp_rom_delay_us()` 忙等，**两个独立时钟域** |
| `s31test ctx` | 协作式切换 | 4 线程同优先级，各自在 `rt_thread_yield()` 前后校验栈上的哨兵数组 |
| `s31test preempt` | **中断帧 vs 协作帧**（R9 的针对性用例） | 2 个纯整数线程（循环里没有任何系统调用，只能被 tick 打断）+ 1 个每 1 ms 睡一次的线程，让"协作式保存的帧被 trap 出口恢复"每秒发生 1000 次 |
| `s31test fpu` | FPU 现场 | 两个线程用 `fmv.w.x` 写满 f0–f31（32 条），`mdelay` 后逐条 `fmv.x.w` 读回比对 |
| `s31test irq` | 中断全链路 | 写 `GPIO_STATUS_W1TS` **软件置位中断状态**，不需要外部接线，走完 矩阵→INUM→mtvt→蹦床→回调 |
| `s31test sem` | IPC | 500 次 ping-pong + 一次必须超时的 take |

产物级已确认：`selftest.c.obj` 里 `fmv.w.x` 32 条、`fmv.x.w` 32 条；
`__fsymtab` 从 3 项增到 5 项（`msh_help`、`s31test`、`pin`、`s31fpu`、`s31stat`）。

#### 波特率：确认做不了，改成诚实报错

原计划"P2 里把波特率做成可配"。核对后发现 **S31 的 `uart_ll.h` 没有
`uart_ll_get_sclk()` / `uart_ll_set_sclk()`**（只有 LP UART 有），
主 UART 的时钟源选择在 `HP_SYS_CLKRST`，没有 LL 封装就无法读回当前频率。
用"已知当前波特率 × 当前分频"反推时钟频率虽然可行，但在没有硬件时属于
**会静默算错并把控制台弄坏**的做法，不做。

因此 `configure()` 改为：非 `CONFIG_ESP_CONSOLE_UART_BAUDRATE` 的请求返回
`-RT_EINVAL` 并打印原因，不再返回成功。`rt_hw_uart0_init()` 同时用
`CONFIG_ESP_CONSOLE_UART_BAUDRATE` 初始化设备配置，让不匹配在 open 时就暴露。

### 12.5 当前状态（2026-10-27）

| 项 | 值 |
|---|---|
| 分支 / 提交 | `rtt-s31` / `7938c2bc`（累计 6 个移植提交） |
| 镜像 | `s31_rtt_test.bin` **193,120 B**（0x2f2a0），分区剩余 82% |
| IRAM | 42,424 B（未增长，新代码全在 flash） |
| flash.text | 101,268 B |
| msh 命令 | `s31test`、`s31stat`、`s31fpu`、`pin` |
| 上板手册 | `rtthread-esp32-s31-bringup-runbook.md` |

**已交付但仍未在硬件上验证**：tick、上下文切换、CLIC 中断、FPU 帧、GPIO、串口。
`s31test` 就是为这六项准备的一键验证工具。

### 12.6 P2 第五批增量（2026-10-27，`da2eee08`）：中断嵌套 + Wi-Fi/BLE 可行性实测

#### 中断嵌套

`vectors_rtthread.S` 里其实**早就实现了硬件嵌套**——`SOC_INT_HW_NESTED_SUPPORTED`
分支下把本层 `mcause` 存进 `s1`、`rt_interrupt_enter()` 之后置 `mstatus.MIE`、
`mret` 前 `csrw mcause, s1`（第 158/183/212 行）。但整个系统从来没跑过两个优先级，
这条路径一次都没被执行过。

`rt_hw_interrupt_install()` 默认给 1 级，同级别不能互相抢占，所以"没有嵌套"是配置造成的。
新增 `s31test nest`：用 GPIO 的**两条中断通道**造出两个优先级——

| 角色 | 通道 | CPU 线 | 级别 | 驱动方式 |
|---|---|---|---|---|
| 外层 | GPIO channel 0 | `ETS_GPIO_INUM`(4) | 1 | 走 `rt_pin_ops` |
| 内层 | GPIO channel 1 | 自选 INUM 6 | 2 | 用例直接写寄存器 |

channel 0 的 handler 在**自己内部**置 channel 1 的状态位，2 级中断必须抢占 1 级 handler。
断言两半：外层 `rt_interrupt_nest == 1`、内层 `== 2`，各 100 轮。
任一数字不对，说明 enter/leave 与栈帧不匹配——这是单级测试永远看不到的。

> 内层用裸寄存器而不是驱动，是因为 `gpio_ll_intr_enable_on_core()` 写死
> `GPIO_LL_INTR0_ENA`（IDF-7995），驱动只覆盖通道 0。

#### Wi-Fi / BLE 可行性：**R6 的前提被实测推翻**

详细数据见 `rtthread-esp32-s31-wifi-feasibility.md`，结论三条：

1. **7 个 Wi-Fi 库 + 3 个 BLE 控制器库，FreeRTOS 引用 0**（219 / 67 个外部符号逐条核对）。
   射频固件与操作系统无关，只通过函数指针表回调。
2. 耦合全在可改的源码里：`esp_adapter.c`（831 行，60 个 osi 函数指针）+ IDF C 层的
   **33 个 FreeRTOS API**（其中 3 个由保留的 `port.c` 提供 → 实际写 30 个）。
3. 当前镜像**已经链接了 10 个 FreeRTOS 对象**（`port.c`/`port_systick.c`/`tasks.c`…），
   tick 正是从 `port_systick.c` → `xPortSysTickHandler` → `rt_tick_increase` 来的。
   所以正确架构不是"拿掉 FreeRTOS"，而是**保留 port 层，只换调度器**。

R6 从"高"降为"中"，工作量从"翻倍且成功率不高"修正为 **Wi-Fi 3~5 人周 + BLE 1~2 人周**。
剩余不可控项只剩 PHY 校准与射频时序。

| 项 | 值 |
|---|---|
| 镜像 | **194,720 B**（0x2f8a0），分区剩余 81% |
| msh 命令 | `s31test`（7 个子用例）、`s31stat`、`s31fpu`、`pin` |
| 累计提交 | 10 个 |

### 12.7 P2 第六批增量（2026-10-27，`9b99bc89` + `df3e3d0e`）：FreeRTOS 兼容层

Wi-Fi/BLE 的前置工作。三层结构：

| 文件 | 作用 |
|---|---|
| `port/freertos/ft_core.h` | 内部 API（`ft_*`），**不包含任何 FreeRTOS 头**，所以能与 `libfreertos.a` 共存 |
| `port/freertos/ft_ipc.c` | 队列 / 信号量 / 互斥量（含递归变体） |
| `port/freertos/ft_event.c` | 事件组 |
| `port/freertos/ft_task.c` | 任务、优先级映射、TLS、调度器锁 |
| `freertos_api.c` | **尚未写**：`xQueueCreate` → `ft_ipc_create` 的薄封装，只在 Kconfig 打开时编译 |

#### 一个必须记住的方法论

第一版 API 清单是 grep 出来的，**33 个**。后来把调度器从 `freertos` 组件里删掉让链接器报错，
发现当前镜像真正需要 **20 个**，其中 **13 个不在 grep 清单里**——因为
`spi_flash`、`esp_libc`（newlib 锁层）、`esp_coex` 这三个永远会链接的组件没被我 grep 到。

并集 **46 个**，减去 port 层白送的 3 个，**实际要写 43 个**（原估 30）。
教训：**数 API 不能 grep，要让链接器说话。**

#### 顺带验证了架构假设

同一个实验还证明：删掉 `tasks/queue/timers/event_groups/stream_buffer` 之后，
`port.c`/`port_systick.c`/`port_common.c` **零未定义引用**。所以"保留 port 层、
只换调度器"这条路线成立，tick 和临界区都不用重写。

#### 几个实现上的坑

- **RT-Thread 的互斥量天生可递归**，FreeRTOS 的普通互斥量不是。直接透传会让
  "同线程重复 take" 静默成功。已给普通互斥量加 owner 守卫，只有
  `xSemaphoreCreateRecursiveMutex` 走 `rt_mutex` 的自递归。
- **`rt_event_control(RT_IPC_CMD_RESET)` 清的是整个事件集**（`src/ipc.c:2312` 直接
  `event->set = 0`），不能用来实现 `xEventGroupClearBits(mask)`。部分清除改为取
  `event->spinlock` 自己改。
- **`xSemaphoreCreateMutexStatic` / `xQueueCreateStatic` 的控制块尺寸**必须校验，
  否则会把 160 字节的对象写进调用方几十字节的 `StaticQueue_t` 里——这种越界
  往往几分钟后才在别处爆。现在尺寸不够直接返回 NULL。
- **`pxCurrentTCBs`** 不是 API，是 FreeRTOS 内部变量，`portmacro.h` 的内联函数直接引用它，
  漏了会链接失败。

| 项 | 值 |
|---|---|
| 镜像 | **199,808 B**（0x30c80），分区剩余 81% |
| 兼容层代码 | 约 1100 行（`ft_core.h` + 三个 .c） |
| 新增自检 | `s31test fbshim`（6 个子项） |
| 累计提交 | 12 个 |

### 12.8 P2 第七批增量（2026-10-27，`262ea9a8`）：兼容层开关与薄封装，链接验证通过

新增 `CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM`：

| 打开后 | 变化 |
|---|---|
| `components/freertos` | 不再编译 `queue.c`/`tasks.c`/`timers.c`/`event_groups.c`/`stream_buffer.c` |
| `components/rtthread` | 增加编译 `port/freertos/freertos_api.c` |
| 保留 | `port.c`、`port_systick.c`、`portasm.S`、`list.c`、`heap_idf.c`、`idf_additions.c` |

**实测**：打开后镜像 **192,800 B**（−7 KB），`libfreertos.a` 只剩上面 6 个成员，
`xQueueGenericSend`/`xQueueSemaphoreTake`/`xTaskGetSchedulerState`/`vTaskSwitchContext`
全部由兼容层提供；关掉后回到 **199,808 B**，与之前逐字节一致。

#### 默认关闭的理由

打开后 `esp_libc` 的 newlib 锁、`spi_flash`、`esp_coex` 都跑在这层上，而它们在启动路径里。
语义错一点，板子会在能打印任何东西之前挂死。所以上板顺序必须是两步：

1. 开关关闭 → 烧写 → `s31test fbshim` 6 项全 PASS（验证语义）
2. 开关打开 → 重新烧写 → 观察启动（验证语义放到启动路径上是否成立）

#### 编译器逼出来的两条

| 现象 | 原因 |
|---|---|
| `redefinition of 'xTaskCreate'` | `task.h` 里它是 `static inline`，转发到 `xTaskCreatePinnedToCore`；只有 Pinned 版是真实符号 |
| `incompatible return type 'QueueHandle_t'` | 它和 `EventGroupHandle_t` 都是不完整结构体指针，工厂函数要显式转换 |

#### 修正一个写错的地方

`StackType_t` 在 IDF 的 RISC-V 端口里是 **`uint8_t`**（`portmacro.h:101`），
所以 `xTaskCreate` 的栈深单位是**字节**而非手册说的"字"。上一版按 ×4 算，
每个任务多拿四倍栈——不崩，所以特别容易被放过。现在有 `_Static_assert` 兜底。

| 项 | 值 |
|---|---|
| 镜像（开关关闭 / 打开） | 199,808 B / **192,800 B** |
| 兼容层总计 | 约 1440 行（`ft_*` 1100 + `freertos_api.c` 334） |
| 累计提交 | 13 个 |

### 12.9 W4 结论（2026-10-27，`f8176d07`）：`esp_driver_uart` 不可行，但产出四件东西

W4 原计划"用 `uart_driver_install` 替换 LL 串口驱动，第一次让 IDF 驱动跑在兼容层上"。
链接器给出的缺口只有 4 个符号，其中 3 个是静态创建（已补），第 4 个
`xTaskRemoveFromEventList` 背后是一个**结构性阻塞**：

- `esp_ringbuf/ringbuf.c:816` 在 `portENTER_CRITICAL(&mux)` **之内**调 `vTaskPlaceOnEventList`；
- RT-Thread 下 `portENTER_CRITICAL` → `vPortEnterCritical()` 同时抬高 CLIC 阈值并
  `rt_enter_critical()`（`port.c:626`）；
- `rt_schedule()` 在调度器锁下是空操作（`scheduler_up.c:296`），于是 `rt_sem_take`
  返回 `-RT_EINTR`，**线程却已经离开就绪队列**，下一次重调度永久挂起。

FreeRTOS 能工作是因为 `vTaskPlaceOnEventList` 在那里只打标记不切换。**这是阻塞原语
的语义差异，不是缺 API。** 结论与三种绕法的代价写进可行性文档 §4.2，风险登记为 R18。

**影响面已实测：不含 Wi-Fi 与 NimBLE**（`esp_ringbuf` 只出现在外设驱动和 Bluedroid SPP）。

本轮实际交付（都不依赖硬件）：

| 项 | 内容 |
|---|---|
| `port/freertos/ft_notify.c` | 任务通知 + 超时，约 300 行。`esp_timer`/`esp_eth`/`esp_ipc` 需要 |
| 静态创建 | `xQueueGenericCreateStatic` / `xQueueGenericGetStaticBuffers` / `xQueueCreateCountingSemaphoreStatic`；`xQueueCreateMutexStatic` 不再丢弃调用方控制块 |
| `ft_block_allowed()` | 把"持调度器锁阻塞"从静默挂死变成 `FT_ERR_STATE`；只在阻塞路径检查，非阻塞调用不受影响 |
| `stream_buffer.c` | 开关打开时保留（无调度器内部依赖） |

镜像：开关打开 **192,880 B**，关闭 199,808 B（不变）。

**两条只有编译器才知道的事实**（已并入可行性文档 §4.2）：
`xTaskNotify` 系列在 `task.h` 里是函数式宏（只能实现 `xTaskGenericNotify`）；
`TickType_t` 与 `rt_uint32_t` 同宽不同型，指针不能互转。

### 12.10 W5（2026-10-27，`4aa66763`）：`esp_event` + `esp_netif` 跑在兼容层上

把 `esp_event_loop_create_default()` 和 `esp_netif_init()` 推进链接，链接器只报
**一个**缺口：`vTaskSetThreadLocalStoragePointerAndDelCallback`（IDF 的 TLSP 删除
回调扩展，`idf_additions.h:240`）。补上后**整条链通过**，镜像 298,576 B。

实现：TLS 侧表加一列回调，`ft_task_delete()` 在删线程前遍历调用（对应
`prvDeleteTCB()`），回调执行时开中断。

**实际走到的路径比预想窄**：本配置下 `esp_event.c:595` 用的是普通
`xTaskCreatePinnedToCore`（非 `...WithCaps`），所以
`xTaskCreateStaticPinnedToCore` 没被需要。打开 `CONFIG_ESP_EVENT_LOOP_IN_EXT_RAM`
才会需要，届时按静态队列同一套路实现。

新增 `s31events` 命令（仅开关打开时编译）：建循环 → `esp_netif_init()` → 注册
处理器 → 投递带载荷事件 → 校验被调用。**这是第一次用真实 IDF 组件验证兼容层。**

镜像从 200,496 增至 **200,688**（+192 B），来源是兼容层新增的 TLS 删除回调，
不是 `esp_event`：试过把 `esp_event esp_netif` 做成条件依赖，大小一点没变，
而且那个 `CONFIG_*` 在 `main/CMakeLists.txt` 求值时不可见、块静默取 false，
反而让 `main.c` 找不到头文件。改回无条件 REQUIRES——关闭时 `s31events` 被编译掉，
gc-sections 会丢掉两个组件（`nm` 验证 0 个 `esp_event` 符号）。

API 数从 43 修正为 **44**（第 44 个就是这条 TLSP 回调）。

### 12.11 W5.5：lwIP 整条链接通过

探针 `esp_netif_new(NULL)` 拉起整个网络栈，**零未定义符号**：`liblwip.a` 42 个目标
文件进镜像，含 `tcpip.c`（线程 + 消息邮箱）、`sys_arch.c`（`sys_sem`/`sys_mutex`/
`sys_thread` 适配层）、`api_msg.c`（netconn）。

**44 个 API 的覆盖面已包含整个网络栈**——lwIP 不需要任何额外符号。链接级结论，
运行时（`tcpip_init` 建线程）待上板。

### 12.12 W6（2026-10-27，`210221c5`）：PWM（LEDC）与通用定时器（TIMG）

判据是"框架在不在"，不是"外设常用不常用"。

- **PWM 接现成框架**：`rt_drv_pwm.c` 是活的，注册后得到标准 `pwm0` 设备，接口是
  纳秒制。实现里要注意三点：S31 只有**一个速度模式**（`SOC_LEDC_SUPPORT_HS_MODE`
  未定义）但有两个 LEDC 实例（`LEDC_LL_GROUP_NUM == 2`，别把 group 当 mode）；
  信号表只填 `speed_mode[0]`（填错得到 0，是合法索引，静默驱动错引脚）；分频是
  8.8 定点。另外 `ledc_ll_enable_bus_clock` 等三个函数必须在
  `PERIPH_RCC_ATOMIC() { }` 里调用。
- **定时器没有框架**：5.x 删掉了 hwtimer，只剩文档。给直接 API
  （`s31_timer_start/stop/count` + `s31_systimer_us`），4 通道，1 MHz 时基。
  每个定时器有独立中断源，ISR 只清自己 latch 的 alarm 位。
- **框架语义要补缓存**：`PWM_CMD_SET_PERIOD` / `SET_PULSE` 只带一个字段，驱动留
  `{period, pulse}` 才能让三个 setter 行为一致、`PWM_CMD_GET` 有答案。
- **构建**：`esp_hal_ledc` / `esp_hal_timg` 不在 `hal` 伞下，`PRIV_REQUIRES` 不会
  带进 include 路径，改用 `target_link_libraries(... idf::esp_hal_ledc
  idf::esp_hal_timg)`。

镜像 200,688 → **206,304 B**；新增命令 `s31pwm`、`s31timer`。仍是编译 + 链接级
结论，波形要示波器、中断要上板。

### 12.13 W7（2026-10-27，`e3345c31`）：`esp_wifi` 链接通过，`esp_timer` 缺口已补

**链接：零未定义符号。** `libnet80211` 43 + `libpp` 36 + `libmesh` 14 + `core` 1 +
`espnow` 1 个目标文件进镜像；`esp_adapter.c` 的 128 项 `wifi_osi_funcs_t` 里 20 个
FreeRTOS 调用全部落到兼容层。真内核没混进来（`tasks/queue/timers/event_groups.c` 零）。

**但链接通过不等于能跑。** `esp_timer` 分两半：`esp_timer_early_init()`（启动
SYSTIMER 计数器）作为 CORE 阶段初始化项正常执行，`esp_timer_init()`（建任务 + 装
ISR）的唯一调用者 `ESP_SYSTEM_INIT_FN(esp_timer_init_os)` 被
`#if !CONFIG_IDF_RTOS_RTTHREAD` 编译掉了——IDF 假设 RT-Thread 自带定时器，但那不是
`esp_timer`。后果：`esp_wifi_start → esp_phy_enable → phy_track_pll_init →
ESP_ERROR_CHECK(esp_timer_create)` → `ESP_ERR_INVALID_STATE` → **abort**。

只在链接期检查会把这个 bug 放过去。修法：`board.c` 在调度器启动前调
`esp_timer_init()`，用 `CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM` 包住（关闭时
`nm` 确认整个符号消失）。

两个附带修正：`main` 组件改 `WHOLE_ARCHIVE`（`-u cmd_wifi` 落在 `libmain.a` 之后
无效，msh 的 section 式命令表不会主动拉 obj）；`CONFIG_IDF_RTTHREAD_HEAP_SIZE`
40 KB → 128 KB（Wi-Fi 静态 RX 缓冲 16 KB 起步）。

镜像 206,304 → **888,112 B**（开关打开）/ 206,592（关闭）。下一步必须上板。

### 12.14 W8（2026-10-27，`90bf4e31`）：启动链被跳过的四件事

`startup.c:257` 用 `entry()` 换掉 `esp_startup_start_app()`，`main_task()` 因此
不执行。它里面有五件事，逐条核完只有一个结论是"不需要"、两个是"没人做"。

- **中断看门狗**：`CONFIG_ESP_INT_WDT=y` 但 `nm` 里符号计数为 0，配置形同虚设。
  已在 board init 里启用（只碰 TIMG1 的 MWDT，与 T0/T1 timer 不冲突；tick hook
  由 `port_systick.c` 在 RT-Thread 分支之前调用，照样喂狗）。
- **跨核中断**：单核，不需要。
- **20 KB DRAM 泄漏**：`startup_stack=true` 的区间
  `[0x2f07afb0, 0x2f080000)` 靠 `main_task` 注册进堆，跳过就永远不可见。
  用 `INIT_APP_EXPORT` 在 main 线程里回收——不能放 `rt_hw_board_init`，那块区间
  包含 `SOC_ROM_STACK`，而 board init 正跑在上面。
- **任务看门狗**：故意不开。它只对 `esp_task_wdt_add()` 注册的任务生效，本镜像
  调用点为 0，开了只增加失败路径。

顺带修两处自己造成的假通过：`s31wifi` 原来先注册事件 handler、从不建默认循环
（`ESP_ERR_INVALID_STATE` 被当成"已做过"放过，真机会白等 5 秒并把锅甩给 wifi
任务）；`s31stat` 只报 RT-Thread 的堆、不报 IDF 的，两个堆不相交，只报一个会
出现"有内存却分配失败"。

DRAM 账本闭合：511.8 / 512 KB。镜像 888,736 B。仍是编译 + 链接级。

### 12.15 W9（2026-10-27，`6d14da77` + `779339e7`）：只在"没人返回"时才对的部分

W7/W8 修的是"没人做初始化"，这一轮是另一类：链接通过、启动正常、跑几十秒也正常，
要等系统活够久才暴露。

- **槽位生命周期**：`s_tls[16]` 只在 `ft_task_delete()` 释放，`s_notify[16]` 根本
  不释放。任务函数自然返回时 RT-Thread 走 `_thread_exit()` → defunct 队列，够不到
  `ft_task_delete()`。更糟的是线程对象随后被 `rt_object_delete()` 释放、地址被复用，
  而查找是裸指针比较 → 新线程继承死任务的 TLS 值和删除回调。
  修法：`rt_thread_t::cleanup` 钩子（`src/defunct.c:113`，指针仍唯一的唯一窗口）；
  通知表加 `ft_notify_forget()`。
- **优先级撞 idle**：`ft_prio_to_rt(0)` = 31 = idle 的档位，时间片轮转下
  `tskIDLE_PRIORITY` 任务每两个 tick 抢 idle 一次，而 idle 负责回收 defunct。
  映射下移一档；反查加钳位（RT-Thread 定时器线程优先级 0 原来报成 ft 30，
  超出 `configMAX_PRIORITIES`）。
- **两个靠巧合**：`xQueueGenericSend` 丢弃 `xCopyPosition`（改成警告一次）；
  `vTaskDelay(portMAX_DELAY)` 靠三重巧合走到 `RT_WAITING_FOREVER`（改成显式）。
- **`uxTaskGetStackHighWaterMark` 补上**：配置开了、声明在、定义缺失（在 `tasks.c`
  里，开 shim 不编译）。RT-Thread 用 `'#'` 填栈，直接扫就能算——不用额外记账。

新增自检 `fbshim/exit`（任务 return 后 TLS 槽是否回收）和 `fbshim/hwm`。

镜像 889,792 B。

### 12.16 W10（2026-10-27，`0b815a2d`）：系统性审计

不再顺着线索挖，而是先假设兼容层有洞，按"失败离得多远"排序去找。四个问题：

- **头里声明、有没有定义？** 8 个函数（`uxTaskGetNumberOfTasks`、`pcTaskGetName`、
  `xTaskGetHandle`、`xTaskGetIdleTaskHandle`、`eTaskGetState`、`xTaskAbortDelay`、
  `xTaskResumeFromISR`、`xTaskDelayUntil`）在 `task.h` 里无条件声明，定义却只在
  `tasks.c` 里——开 shim 不编译。本镜像没调用者，所以洞一直没被发现。
  新增 `port/freertos/ft_query.c` 全部补上。
- **阻塞路径有没有全走保护？** 3 处没有。`ft_task_delay()` 最严重：`void` 返回值，
  没有守卫时不是报错而是**永久挂住**（`rt_schedule()` 在调度器锁内是空操作）。
  `ft_event_wait_bits()` 同样。
- **算术会不会回绕？** `ft_ipc_create` 的 `length * item_size` 会。65536×65536
  → 向 `rt_malloc` 要 0 字节 → 成功 → 首次 send 越界。FreeRTOS 返回 NULL。
- **失败是不是静默的？** 通知表满、`xQueueSend` 非追加位置，都改成告警一次。

自检从 8 项扩到 18 个报告点；`fbshim/guard` 新增的延时检查不会静默失败——没有
守卫的话它根本不返回。

镜像 890,672 B。

### 12.17 W11（2026-10-27，`7131f65e`）：让链接器枚举缺口

W10 靠读，这轮靠链接器。

- **先拉上游**：`esp-idf-rtt` 领先 21 个提交、上游无新提交；`rt-thread` 与
  `freertos-wrapper` 都是 0/0。官方 `FreeRTOS-Wrapper` 差分结果 40 vs 66，但我们
  缺的 8 个里 4 个是宏，真正要补 4 个——**照抄官方会退步**，它有 11 个
  `configASSERT(0)` 桩。
- **链接探针**（`ft_probe.c`，72 个指针）让构建在缺定义时以名字失败。
  **保留方法试了三次**：`const` 数组被 gc、`used+retain` 被链接脚本丢弃（两次都
  静默通过），最后从 msh 的 `KEEP()` 命令表引用才生效。
- **探针找出 14 个缺口**，全部补齐。`xQueueSelectFromSet` 做成真 wait-any（必须
  循环处理陈旧通知），`xTaskCreateStaticPinnedToCore` 明确拒绝（`StaticTask_t`
  装不下 `rt_thread`）。
- **`xQueueOverwrite` 修了我自己的 bug**：空→满时只加 `count` 不加 `free_slots`，
  破坏 `count + free_slots == length`，下一次 send 会往单槽环塞第二个 item。
  自检专门覆盖这条不变量。

自检 15 个报告点，镜像 896,288 B。

### 12.18 W12（2026-10-30，硬件：ESP32-S31 开发板 / COM16 / 2 MB flash / 320 MHz 单核）：上板

**这一轮第一次真的跑镜像，结论修正了前 11 轮的一个隐含假设。**

硬件与配置：串口 COM16 @115200 8N1，flash 2 MB，CPU 320 MHz，`CONFIG_FREERTOS_UNICORE=y`
生效（CPU1 保持复位）。烧写后连续修 **6 个缺陷**才走到应用启动，全部集中在"从 bootloader
跳到应用第一行"这一段。

| # | 缺陷 | 根因 | 静态能否发现 |
|---|---|---|---|
| 1 | `assert failed: unpack_load_app bootloader_utility.c:833 (rom_index < 2)` | `SOC_MMU_DI_VADDR_SHARED=1`，I/D 共用虚拟地址，bootloader 只允许 2 个 flash 映射段；而 `.flash.rt_thread`（PROGBITS）排在 `.flash.rodata_noload`（NOBITS）之后，NOBITS 不占文件空间，链接器必须新开一个 LOAD 段 → 3 个 | 不能 |
| 2 | `Load access fault`，MEPC 在 `rt_object_get_type`，A0=0，MTVAL=0x0c | `do_core_init()` 先跑 CORE 阶段 init，再 `rt_system_heap_init()`；`esp_libc_locks_init` 在 CORE 阶段 `xSemaphoreCreateMutexStatic` → `rt_malloc`，`system_heap` 仍是 NULL（`src/mem.c:287`） | 不能 |
| 3 | `assert failed: esp_libc_locks_init locks.c:422 (handle == &s_common_mutex)`，无限重启 | FreeRTOS 的静态创建承诺返回 handle == 调用者的控制块指针，`esp_libc` 直接 assert 这条；而 port 只把调用者的块记在 `static_cb` 里，返回值是堆地址。`StaticQueue_t` 只有约 60 B，装不下约 180 B 的 `struct ft_ipc` | 不能 |
| 4 | `abort()`，PC 落在 `locks.c:150`（递归互斥量在 ISR 上下文） | `esp_libc` 用 `taskSCHEDULER_NOT_STARTED` 做"还没调度，加锁是空操作"的逃生门（`locks.c:128/137`）；shim 只返回 RUNNING / SUSPENDED，于是判"是否 ISR"时 `xPortCanYield()` 因 `rt_thread_self()==NULL` 返回 false → 进 ISR 分支 | 不能（§5 曾明确写"不需要模拟"） |
| 5 | `psa_crypto_init failed: -144`（`PSA_ERROR_SERVICE_FAILURE`）→ `init function ... aborting` | `ft_ipc_take()` 的 `if (ipc->owner == self) return FT_ERR_TIMEOUT` 本意拦重入；但调度器未启动时 `rt_thread_self()` 是 NULL，干净互斥量的 `owner` 也是 NULL，于是每个互斥量都被判成自持有 | 不能 |
| 6 | `Store access fault`，MEPC 在 `_rt_mutex_take`（`src/ipc.c:1353`），MTVAL=0x2c（= `offsetof(struct rt_thread, error)`） | `rt_mutex_take()` 无条件写 `thread->error = RT_EOK`，`thread = rt_thread_self()` 为 NULL | 不能 |

修法（按上表顺序）：

1. 把 `__fsymtab_start/end`、`__vsymtab_start/end`、`__rt_init_start/end` 移进 `.flash.rodata`
   内部（`components/esp_system/ld/ld.flash.sections`），删掉 esp32c3 / esp32c6 / esp32s31
   三个 `sections.ld.in` 里的独立段。**改完必须删 ldgen 缓存**（`sections.ld`、`.fingerprint`、
   `.lfcache`），否则 §3.4 那个缓存会把旧脚本写回，第二次烧写现象一模一样。
2. `startup.c` 把 `rt_system_heap_init()` 提到 `do_system_init_fn(CORE)` 之前。
3. 调用者的块里放两字长的桩 `{ magic, real }`，真实对象仍在堆上；`FT_IPC_MAGIC = 0x46544950`（"FTIP"），
   `ft_ipc_resolve()` 在 magic 匹配时才跟随指针。真实 `struct ft_ipc` 开头是三个一字节标志，
   首字不可能等于该 magic，两侧不会误认。23 处 API 入口改用它。
4. `ft_task_scheduler_state()`：`rt_thread_self() == RT_NULL` → `taskSCHEDULER_NOT_STARTED`。
5. `if (ipc->owner != RT_NULL && ipc->owner == self)`。
6. `ft_ipc_take()` / `ft_ipc_give()` 在"互斥量类型 + `rt_thread_self()==RT_NULL`"时直接返回 `FT_OK`——
   FreeRTOS 在这个窗口里"加锁是空操作"正是 `esp_libc` 依赖的语义。队列与信号量不受影响，
   `rt_sem_trytake` 不需要线程对象。

**四同族缺陷**：第 2、4、5、6 都属于"调度器还没起来"这个窗口。FreeRTOS 对该窗口有明确
语义（加锁空操作、任务创建自初始化列表、调度器状态有第三个值），RT-Thread 没有，port 每处
都得自己补，而"哪几处要补"没有清单可查。

**方法论修订（本次最重要的结论）**：前 11 轮全是静态审查（读代码、比对头文件、链接探针），
产出的结论（43 个 API 名字全对、零未定义符号、探针 72 个指针全在）**一条都没被推翻**，
但它们描述的是镜像的静态正确性，不是系统能否启动。"链接通过 + 编译期断言全绿"和"能启动"
之间隔着**整条初始化时序**：谁先跑、谁依赖谁的堆、谁在谁的栈上——这些在链接图里不存在。

因此 §9 验证矩阵里"运行"那几行的权重上调：静态阶段能给出的结论上限就是"能链接"，
其余必须是板上计数。W1–W11 各节末尾"仍是编译 + 链接级"的注记不是保守措辞，是准确边界。

**新增的静态检查**（把三条缺陷转成可在构建流程里跑的判据）：

| 检查 | 时机 | 判据 |
|---|---|---|
| `readelf -l … \| grep -c LOAD` | 新增任何 `.flash.*` 链接片段 | flash 映射段 = 2 |
| `sizeof(StaticQueue_t)` vs `sizeof(struct ft_ipc)` | 改动 `struct ft_ipc` 字段 | 桩保持两字长；magic 判定仍成立 |
| grep `rt_thread_self()` 是否被直接解引用 | 新增启动路径代码 | 必须显式判 NULL |

前两条能进脚本，第三条只能靠人——而它正是四个同族缺陷的共同形状。

**下一步**：`s31test` / `s31events` / `s31wifi` 都还没跑过（它们要求 msh 先起来）。
按 `rtthread-esp32-s31-bringup-runbook.md` §6.0 的报错对照表和 §10 的实测记录推进。

### 12.19 W12 补记（2026-10-30，`f40c14dd`）：第七个缺陷与两条时序修正

§12.18 记了六个缺陷。修完之后镜像第一次到了 `msh >`，然后立刻又崩了一次
——第七个缺陷，位置完全不同。

- **现象**：`msh >` 之后 `Instruction access fault`，`MCAUSE=1`、
  `MTVAL==MEPC==0x2f000270`，反汇编是 `spurious_interrupt` 里第一条 `flw`。
- **根因**：`context_gcc.S:152-158` 只把 MPIE 存进帧 slot 2，所以从协作
  切换帧恢复的路径必须自己补 `mstatus`。`context_gcc.S:245` 补的是
  `0x7800`（MPP+FS），trap 退出补的是 `0x1800`（只有 MPP）。FS=0 执行
  `flw` 是非法指令，而 S31 的 `SOC_CPU_HAS_FPU_EXT_ILL_BUG` 让 EXT_ILL
  不记录 FLW/FSW，于是被报成取指异常。
- **修法**：trap 退出按 `RT_FPU_FRAME` 补 `0x7800` / `0x1800`，与
  `context_gcc.S` 对齐。

**两条时序修正**（修完前六个之后暴露的）：

| 问题 | 表现 | 修法 |
|---|---|---|
| `esp_timer_init()` 位置太晚 | esp_coex（SECONDARY 204）调到 `ets_timer_setfn` 时 `s_timer_task` 还是 NULL | 从 `rt_hw_board_init()` 改成 `ESP_SYSTEM_INIT_FN(..., SECONDARY, BIT(0), 99)` |
| 调度器前创建任务 | `rt_thread_startup` 往未初始化的 `rt_thread_priority_table[]` 插，store fault @ 地址 4 | 创建照做、启动延后到 `ft_task_start_pending()`，由 `INIT_COMPONENT_EXPORT` 冲刷 |

**为什么静态抓不到**：三处帧尺寸其实**一致**（256 B），所以任何尺寸检查
都通过。不一致的是**语义**——`mstatus` 该带哪些位。`vectors` 侧原有注释
解释了为什么补 MPP，只是漏了同一个理由也适用于 FS。

**风险状态变化**：R21（调度器未启动窗口无语义约定）从"已识别"升级为
"已修 6 处 + 第七处不同类"。新增 **R22**：恢复路径的 `mstatus` 语义没有
单一来源，两处独立维护，将来任一侧改动都可能再次不一致。建议的长期修法
是把"恢复一帧需要哪些 mstatus 位"抽成一个宏，但那是同步到上游之后的事。

镜像 897,872 B，到 `msh >`。

### 12.20 W13/W14（2026-10-30 晚）：从"能启动"到"20/20 全 PASS"

§12.18/12.19 的结论是"镜像能启动"。这一轮的目标变成了"**自检全绿**"，
结果又抓出 9 个缺陷。完整记录见 `rtthread-esp32-s31-porting-log.md` §24/§25，
上板判读见 runbook §12。这里只留**对方案本身有影响**的部分。

#### 一、单位错误暴露"测量工具本身没有被验证"

`s31test tick` 报 16000 us/tick，两处独立复制了同一段"读 SYSTIMER 当微秒"的代码：

| 层 | 原因 |
|---|---|
| 1 | S31 的 SYSTIMER 是 16 MHz（XTAL 40 MHz / 固定 2.5 分频），不是 1 MHz。IDF target port 就是 `/16` |
| 2 | 读的是 `unit_val[0]`（esp_timer 的计数器，实测不动），应该读 `unit_val[1]`（OS tick 的） |

**对方案的修正**：§9 验证矩阵里"用 SYSTIMER 交叉验证"这一条，前提是"SYSTIMER 是 1 MHz"
——这条前提本身是错的。现在 `s31_systimer_us()` 是唯一实现，走 IDF 的
`systimer_ticks_to_us()`，并新增 `s31test clock` 把三个时钟域并排打印。**测量工具必须自己先被测**。

#### 二、R21（调度器未启动窗口）之外，出现了第二类"生命周期"缺陷

R21 记的是"调度器还没起来"这个窗口。这一轮冒出来的是**另一端**：
**对象已经死了但调用方还拿着句柄**——线程 return 之后 `rt_thread_delete(handle)` 是双重释放。
两次现场（`rt_object_get_type` 的 `MTVAL=0xc`、`rt_list_remove` 的 `MTVAL=5`）说明
"读 `stat` 判断是否已退出"也不可靠，因为那时对象已被释放并复用。

新增风险登记 **R23**：

> **句柄在对象死亡后仍被使用（use-after-free by design）**。RT-Thread 的
> `rt_thread_delete()` 不区分"活着"和"已经 return 了并已被 idle 回收"；FreeRTOS 里
> `vTaskDelete` 的同类调用同样是未定义行为。区别在于**IDF 的 esp_event loop task 和
> esp_timer task 都是 return 退出的**，所以这条路径在 Wi-Fi 起来之后一定会被踩到。
> 缓解：`ft_task.c` 的退出标记表（指针唯一时记录，`ft_task_create` 复用地址时清除）。

#### 三、三处输入校验会毁掉正在运行的系统

| 位置 | 后果 |
|---|---|
| `pwm.c` pad 校验用 `SOC_GPIO_PIN_COUNT` | 允许 LEDC 驱动封装内 flash 的 GPIO29/41，下一次取指崩 |
| `interrupt.c` 卸载不清屏蔽 | 电平线 + NULL handler = trap 风暴，只有 INT WDT 能报 |
| `interrupt.c` 优先级不校验 | 传 8 落到 `NLBITS=3` 被移走的位上，读回 0 = 屏蔽，静默 |

**对方案的修正**：这三条的共同形状是"**参数在合法范围内、语义在范围外**"。
§9 验证矩阵里"驱动自检"应该覆盖**非法输入**，不只是正常路径。已补：
`s31pwm 0 29 ...`、`s31timer 0 600000`（超过时基上限）、`s31pwm` 未配置通道使能。

#### 四、P3 的前置条件更新

§P3.1 的 P3-a 方案（`esp_cpu_unstall(1)` + `ets_set_appcpu_boot_addr`）**没有变**。
但本轮实测确认了两件对它有直接影响的事：

1. `ets_set_appcpu_boot_addr` 在 ROM 导出表里可用（`2f8000a8 A`），`esp_cpu_stall/unstall`
   在 `esp_hw_support/cpu.c` 里有实现，只是当前 `SINGLE_CORE_MODE=y` 下没被链接——**需要显式引用**。
2. `rt_interrupt_nest` / `rt_thread_switch_interrupt_flag` 等全局量在当前镜像是**单份**的，
   SMP 化时要改成 per-CPU，`vectors_rtthread.S` 里的 `la t0, rt_interrupt_nest`
   会全部失效。这是 P3 最大的机械改动量，没有变化。

#### 五、里程碑

| 里程碑 | 状态 |
|---|---|
| M1 banner + 单线程 tick | ✅ |
| M2 上下文切换 / FPU 帧 / 抢占 | ✅ 20/20 自检覆盖 |
| M3 msh 可交互 + UART/GPIO/PWM/TIMG 驱动 | ✅ |
| M4 FreeRTOS 兼容层（队列/信号量/事件组/任务/TLS/通知/队列集） | ✅ 12 项 fbshim 子测试 |
| M5 esp_event + esp_netif + esp_wifi 起栈 | ✅ `s31wifi` PASS |
| M6 连接真实 AP | ⬜ 需要配网路径（NVS 或 msh 命令） |
| M7 双核 SMP（P3） | ⬜ |
| M8 BLE | ⬜ |

### 12.21 W15（2026-10-30 晚）：补齐 RT-Thread 原生命令，删掉一处影子状态

用户问「是否所有移植都按 RT-Thread 的来」，答案当时是**两处不是**。详见 log §26。

#### 一、"归档里有、镜像里没有"已经遇到第二次

`finsh/cmd.c` 定义了 `list` / `clear` / `version` / `console`，四条全靠
`MSH_CMD_EXPORT_ALIAS` 注册进 `FSymTab`，**没有任何全局符号被引用**，于是
`--gc-sections` 把整个归档成员丢掉。加上 `MSH_USING_BUILT_IN_COMMANDS` 未定义
（`ps`/`free`/`bind` 整段被 `#ifdef` 排除），RT-Thread 自带命令**一条都不剩**。

这和当初 `finsh_system_init` 是同一类问题，**已经是第二次**。所以 §22.10 那条
"新增任何依赖段注册的代码后要检查符号是否真的进了镜像"现在有了具体做法：

```bash
riscv32-esp-elf-nm build/s31_rtt_test.elf | grep -E "list_thread|finsh_system_init"
grep -A10 "FSymTab" build/s31_rtt_test.map      # 数一数每个 obj 贡献了多少
```

`tools/check_image.py` 的 `__fsymtab` 检查目前只判"非空"，**建议加强成"条数下限"**
（现在 17 条），改一行即可。

#### 二、影子状态：这个模式现在出现两次了

| # | 影子 | 内核里的真相 | 后果 |
|---|---|---|---|
| 1（§25.3） | 调用方手里的 `rt_thread_t` 句柄 | defunct 队列 + 引用计数 | 线程已回收，句柄还能"再删一次" |
| 2（本轮） | `ft_ipc.owner` | `rt_mutex.owner` | 持有者死亡后内核对，影子错，地址复用后互斥量永久"被自己持有" |

共同形状：**同一个事实存两份，其中一份没有生命周期管理**。R23 记的是第 1 种，
本轮补记进同一条：**任何"为了性能/方便而复制一份内核字段"的代码，都要先问
"这份副本在对象死亡时谁清"**。修法一律是删副本、读内核，而不是加清理钩子
（加钩子意味着每多一条死亡路径就要多补一次）。

#### 三、`ft_ticks_to_rt` 向上取整

截断会让"1 个 FreeRTOS tick 的等待"在 `FREERTOS_HZ > RT_TICK_PER_SECOND` 时
变成 0 = 不等待。当前两者同为 1 kHz 所以行为不变，但换算存在的意义就是让改频率
安全，截断违背这一点。已改为 `(a*b + b - 1) / b`。

#### 四、关于 Python（回答用户提问）

计划里原本没有 Python 相关内容，现在明确记录：

- **当前不支持**，树里没有 Micropython，`rt-thread/packages` 目录都不存在。
- RT-Thread 是纯 C 内核，C++ 是可选组件；"能不能跑 Python"取决于是否加一个
  解释器运行时，与内核语言无关。
- 若要支持，路线是移植 MicroPython（实现 `mp_hal_*`），代价 200~300 KB flash、
  30~60 KB RAM，且**依赖一个能真正连网的 `esp_netif`**——所以必须排在 M6 之后。

#### 五、里程碑更新

| 里程碑 | 状态 |
|---|---|
| M5 esp_event + esp_netif + esp_wifi 起栈 | ✅ |
| **M5.5 RT-Thread 原生命令（list/ps/free/version）** | ✅ 本轮补上 |
| M6 连接真实 AP | ⬜ 下一步 |
| M6.5 MicroPython 运行时（可选，用户提出） | ⬜ 视 M6 结果 |

### 12.22 W16（2026-10-30 夜）：16 MB flash、BLE、I2C，双核可行性核对

#### 一、硬件事实纠正：这片板是 16 MB flash，不是 2 MB

`esptool flash-id` 报 `Manufacturer 0x46 / Device 0x4018 / 16MB`，且往 **8 MB 处**写
256 B 图案、复位、读回、比对**完全一致**。启动日志那句
`Detected size(16384k) larger than the size in the binary image header(2048k)` 一直在提示这件事。

**对方案的影响**：§P1 里"2 MB flash、1 MB app 分区"的假设作废。新布局
`nvs 24K / phy 4K / factory 4M / storage 8M`。**这是加蓝牙的前置条件**——
BT 控制器库 4.7 MB、链进镜像约 300 KB，1 MB 分区（已用 86%）放不下 Wi-Fi + BT。

#### 二、里程碑更新

| 里程碑 | 状态 |
|---|---|
| M5.5 RT-Thread 原生命令 | ✅ |
| **M5.6 BLE 控制器 + NimBLE host** | ✅ `s31ble` PASS，host synced，addr 读出 |
| **M5.7 I2C0 总线设备** | ✅ `i2c0` 在 `list device` 里，`i2cscan` 正常 |
| **M6 连接真实 AP** | ✅ **PASS，350~400 ms，WPA2-PSK，ch 1，RSSI -49**（W58） |
| M6.5 MicroPython 运行时 | ⬜ 依赖 M6 |
| **M7 双核 SMP（P3）** | ⚠️ **部分**：CPU1 启动 + 共享锁已验证；CPU1 进调度未能稳定复现（W56 结论作废，见 W59） |
| M8 外设扩展 | 🔄 I2C/SPI/ADC/PCNT/MCPWM/RMT/TWAI 完成；SD/MMC、USB、LCD、Camera 待硬件 |

#### 三、P3 双核：事实核对与步骤（替代 §P3 的预估）

树里可查的事实：

| 事实 | 出处 |
|---|---|
| 2 个核 | `soc_caps.h: SOC_CPU_CORES_NUM (2U)` |
| IPI = 28 | `soc.h: ETS_IPC_ISR_INUM 28` |
| `esp_cpu_unstall(1)` 有实现 | `esp_hw_support/cpu.c:38` |
| IDF 从核入口 | `cpu_start.c:308-337 start_other_core()` |
| RT-Thread 有 MP 调度器 | `src/scheduler_mp.c` |
| 协作切换有 SMP 分支 | `libcpu/risc-v/legacy_common/context_gcc.S:18/67/186` |

步骤（依赖顺序）：

1. 保持 `CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y`，让 IDF 不碰 CPU1（**现状已满足**）
2. `rtconfig.h` 开 `RT_USING_SMP` + `RT_CPUS_NR 2`，CMakeLists 的 `scheduler_up.c` → `scheduler_mp.c`
3. **`vectors_rtthread.S` 全局量 per-CPU 化**（最大机械改动）
4. 自写精简 `call_start_cpu1_rtthread` + `ets_set_appcpu_boot_addr` + `esp_cpu_unstall(1)`；
   **不要**复用 `do_secondary_init()`
5. 从核自己调一次 `vSystimerSetup()`（它已按 `cpuid` 选 alarm）
6. IPI 归属：28 号槽交给 RT-Thread 分发，或关 `CONFIG_ESP_IPC_ISR_ENABLE` 自建

#### 三之二、R24 复核（W32，2026-10-31）：**前提不成立，前置工作不需要做**

R24 写的是"兼容层的静态表只用 `rt_hw_interrupt_disable()` 保护，
SMP 下只关本核中断 → 不互斥"。**这句话的前提是错的**，而且用编译器就能证明：

```
$ riscv32-esp-elf-gcc -S -O2 probe_r24.c
    tail    rt_cpus_lock
```

`rthw.h:146`：

```c
#ifdef RT_USING_SMP
rt_base_t rt_cpus_lock(void);
#define rt_hw_interrupt_disable rt_cpus_lock    /* <-- 宏替换 */
#else
rt_base_t rt_hw_interrupt_disable(void);
#endif
```

而 `cpu_mp.c:168`：

```c
rt_base_t rt_cpus_lock(void)
{
    level = rt_hw_local_irq_disable();
    if (pcpu->current_thread != RT_NULL) {
        if (lock_nest == 0) { rt_enter_critical(); rt_hw_spin_lock(&_cpus_lock); }
        rt_atomic_add(&pcpu->current_thread->cpus_lock_nest, 1);
    }
    return level;
}
```

**所以打开 `RT_USING_SMP` 之后，shim 里同一个调用自动变成"全局自旋锁 + 关本地中断"。**
四张静态表（`ft_tls` / `s_notify` / `s_exited` / `s_pending`）**一行都不用改**。

ISR 语境也安全：`cpus_lock_nest` 计数保证 ISR 打断已持锁线程时走 `nest != 0` 分支，
不重复取锁，不会自旋死锁。所以 `ft_task_notify_isr()` 之类也不用改写。

**这条风险是"读了函数名就开始推理"的产物**，与 R28（按名字猜 `SPI2` 符号）同源。
已按"读内核 + 编译器验证"的规则复核。

#### 三之三、P3 真正的机械改动量（复核后）

| 项 | 原估 | 复核后 |
|---|---|---|
| shim 静态表加锁（R24） | "P3 前置，大量改动" | **0 行** |
| `vectors_rtthread.S` per-CPU 化 | 最大机械改动 | **不变**（4 个全局量 + `_global_interrupt_handler` 的核内索引） |
| `scheduler_up.c` → `scheduler_mp.c` | 一行 | 一行（CMakeLists） |
| 从核启动 | 自写精简入口 | 不变 |

**结论：P3 的改动量比原估小得多，剩下的都是"必须做但可枚举"的机械工作。**

#### 三之四、实测边界：只改两行会怎样

把 `RT_CPUS_NR` 改成 2、`scheduler_up.c` 换成 `scheduler_mp.c`，构建结果：

```
scheduler_mp.c:231: 'struct rt_cpu' has no member named 'priority_group'
scheduler_mp.c:262: 'struct rt_cpu' has no member named 'priority_table'
scheduler_mp.c:291: 'struct rt_sched_thread_ctx' has no member named 'oncpu'
scheduler_mp.c:291: 'RT_CPU_DETACHED' undeclared
scheduler_mp.c:305: 'struct rt_sched_thread_ctx' has no member named 'bind_cpu'
scheduler_mp.c:328: 'RT_CPU_MASK' undeclared
scheduler_mp.c:329: 'rt_hw_ipi_send' implicit declaration
scheduler_mp.c:329: 'RT_SCHEDULE_IPI' undeclared
```

**原因：只加了 `RT_CPUS_NR`，没有定义 `RT_USING_SMP`。**
`rtdef.h` 里 `struct rt_cpu` 的 `priority_group` / `priority_table`、
`struct rt_sched_thread_ctx` 的 `oncpu` / `bind_cpu`、
`RT_CPU_DETACHED` / `RT_CPU_MASK` / `RT_SCHEDULE_IPI` —— 全部在
`#ifdef RT_USING_SMP` 里面。所以这两处必须**同时**改，不能分开。

**这条实测有价值**：它证明 SMP 不是"改两行"能开的，而且错误是**编译期枚举出来的**
（8 条，全部指向同一类定义缺失），不是运行时才崩。

#### 三之五、把 `RT_USING_SMP` 真的打开之后的完整清单

上面那 8 条报的是"宏没定义"。补上 `#define RT_USING_SMP` 之后错误换了一批，
**这次才是真正的待办**。

**第一批（头文件可见性，编译期）**：

```
rttypes.h:161: fatal error: cpuport.h: No such file or directory
```

`rttypes.h` 在 `RT_USING_SMP` 下要 `#include <cpuport.h>` 取 `rt_hw_spinlock_t`。
该头在 `libcpu/risc-v/legacy_common/`，原先只在 rtthread 组件的
**PRIV_INCLUDE_DIRS** 里 —— 够自己用，不够其他包含 `<rtthread.h>` 的组件。
修法：加进 `idf_build_set_property(INCLUDE_DIRECTORIES ...)`。

**第二批（11 个符号缺失，链接期）**：

| 缺失符号 | 出现位置 | 性质 |
|---|---|---|
| `rt_interrupt_nest` | `vectors_rtthread.S:144/202` | SMP 下是 `rt_cpu_self()->irq_nest`（`irq.c:68`），**不是变量** |
| `rt_thread_switch_interrupt_flag` | `vectors_rtthread.S:217` | `cpuport.c:27` 定义在 `#ifndef RT_USING_SMP` 内 |
| `rt_interrupt_from_thread` | `vectors_rtthread.S:226` | 同上 |
| `rt_interrupt_to_thread` | `vectors_rtthread.S:232` | 同上 |
| `rt_hw_spin_lock` / `_unlock` / `_init` | `components.c`、`cpu_mp.c` | 参考实现 `libcpu/risc-v/common/cpuport.c:66`（ticket lock） |
| `rt_hw_cpu_id` | `clock.c`、`idle.c`、`scheduler_mp.c` | 参考实现 `libcpu/risc-v/common/cpu.c:251` |
| `rt_hw_ipi_send` | `scheduler_mp.c:329` | 需接 `ETS_IPC_ISR_INUM`(28) |
| `rt_hw_secondary_cpu_up` | `components.c:259` | 从核启动 |
| `rt_hw_secondary_cpu_idle_exec` | `idle.c:128` | 从核 idle |

**关键三条洞察**：

1. **`rt_interrupt_nest` 在 SMP 下不是变量而是宏** → `vectors_rtthread.S` 里
   `la t0, rt_interrupt_nest` 的写法**必须改**，不能再按符号取地址。
   这才是 per-CPU 化的真正内容，不是"加个偏移"。
2. **`cpuport.c` 的三个全局量要整体挪进 `#else` 分支**，SMP 下由内核的
   `rt_scheduler_do_irq_switch()` 接管（参考 `interrupt_gcc.S:312-340`）。
3. **spinlock / cpu_id 可直接用 `libcpu/risc-v/common/cpuport.c` 的 ticket lock**，
   不必自己设计。

**P3 的工作量从"不确定"变成"11 个符号 + 1 处汇编改写 + 3 个新函数"。**

#### 三之六、回滚时踩的坑：只删 `.obj` 不够

回滚到单核后构建仍然失败：

```
cpu_mp.c:25: unknown type name 'rt_hw_spinlock_t'
cpu_mp.c:177: 'struct rt_thread' has no member named 'cpus_lock_nest'
```

原因：**`build/` 目录残留了 SMP 时的 CMake 配置**，`cpu_mp.c` 还在源文件列表里。
删单个 `.obj` 无效，**必须删整个 `build/` 重新 configure**。
CMake 的源列表变化不改 `CMakeCache.txt` 的依赖，这类残留只能靠整目录删除清掉。

已确认恢复：单核、`s31test` 20/20、`check_image.py` 9/9、30 命令。

#### 四、外设扩展的现实评估

用户列了 14 类。按 S31 实际能力分三组：

- **有硬件、可按 I2C 方式接框架**：SPI(3)、ADC(2×8)、RMT、TWAI(2)、SD/MMC、USB、LCD、CAM、
  I2S、触摸、PCNT、MCPWM
- **这颗芯片没有**：**DAC**（`soc_caps.h` 无 `SOC_DAC_SUPPORTED`）
- **一次全加的风险**：每个外设 = `rtconfig.h` 宏 + 框架源 + LL 驱动 + board init + msh 自检，
  实测约 400~600 行 + 1~2 轮调试。14 个同时加会把"修完第一个才看见第二个"放大 14 倍。
  建议每次 2~3 个，按"能否无实物验证"排序。

#### 五、风险登记新增

| # | 风险 | 状态 |
|---|---|---|
| **R24** | ~~兼容层静态表在 SMP 下不互斥~~ | ✅ **已撤销**（W32）。`rthw.h:146` 在 SMP 下把该调用宏替换成 `rt_cpus_lock`，编译器输出 `tail rt_cpus_lock`；`cpus_lock_nest` 也保证 ISR 路径不自旋。**前提是错的，改动量为 0** |
| **R25** | sdkconfig 与 sdkconfig.defaults 不同步导致构建不可复现（本轮 `FREERTOS_SHIM` 就是这样丢的，删 sdkconfig 后链接失败） | 已修。检查法：删 sdkconfig 必须仍能构建 |

### 12.23 W17（2026-10-30 深夜）：板子资料核对、板级引脚表、PSRAM 的两个缺陷

#### 一、板子确认：Maple ESP32-S31 Alef（ESP32-S31-WROOM-3）

资料到位后，之前所有"猜"的硬件事实都要重算。对方案影响最大的三条：

| 项 | 手册值 | 影响 |
|---|---|---|
| **16 MB Octal PSRAM** | 之前完全没有配置 | 新增能力，但见下：**当前不可用** |
| **BT 5.4 LE + Classic** | 之前只做 BLE | Classic 要 `libbredr_app.a`（+2.4 MB），4 MB 分区还够，但要单独一轮 |
| **DVP 摄像头 / SPI LCD / SDIO / USB Host** | 之前列为"按需" | 全部有实物，P4 外设扩展的优先级需要重排 |

#### 二、缺陷十二：I2C 引脚一直是错的

`port_i2c.h` 里写 IO4/IO5，而板子上这两个是 **I2S_LRCK / I2S_DSDIN**；
板子的 I2C 在 **IO50/IO51**。之前 `i2cscan` 报 "0 devices" 是必然结果，
和驱动正确性无关——**"测试通过"与"测试的是对的东西"是两件事**。

修法：新增 `port_board.h` 作为**唯一的板级引脚事实来源**，`port_i2c.h` 引用它。
以后新驱动一律从这里取脚，不再各自写一份常量。

#### 三、缺陷十三：PSRAM 能用，但会打崩中断系统

| 配置 | 结果 |
|---|---|
| PSRAM 200M / 100M | 启动挂在 `MSPI Timing: Enter psram timing tuning`，永不返回 |
| PSRAM 40M | 启动正常，`s31psram` 64 KB 写读一致，堆里出现 16.7 MB |
| PSRAM 40M + `s31test irq` | **中断 WDT 超时**，`MCAUSE=0x18` |
| PSRAM 关闭 + `s31test irq` | **200/200 PASS** |

由此查出两条**独立成立**的底层缺陷（已修）：

1. **ISR 在 flash 里**。`rtt_trampoline` / `s31_gpio_isr` / `s31_uart_isr` /
   `s31_timg_isr` 原来都在 `.flash.text`。缓存一关（flash 写、MSPI 操作）就取不出指令。
   IDF 靠 `ESP_INTR_FLAG_IRAM` 保证这件事，手写驱动必须自己写 `IRAM_ATTR`。
2. **未装 handler 的电平线是活锁**。所有线都是电平触发，空槽 → trap 返回 →
   立刻再进同一个 trap，线程再也不跑。`rtt_trampoline` 现在屏蔽并打印一次。

**修完仍在 PSRAM 下崩溃**（位置换到 `s31_gpio_isr` prologue），说明还有第三条路径，
调查中止在"PSRAM 关掉全绿"这个事实上。**PSRAM 因此默认关闭**，
`defaults` 里写清了原因与复现方法。

#### 四、里程碑更新

| 里程碑 | 状态 |
|---|---|
| M5.8 板级引脚表（`port_board.h`）+ RGB LED | ✅ `s31led` 可用 |
| M5.9 PSRAM | ⚠️ **功能可用（40M），但会使能中断后崩溃**，默认关闭；下一轮第一项 |
| M6 连接真实 AP | ⬜ |
| M8 外设扩展 | 🔄 I2C（IO50/51）；SPI LCD / I2S / DVP / SDIO 有实物，可按此优先级推进 |
| M9 Classic BT | ⬜ 新增（手册明确支持，需 +2.4 MB） |

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R26** | **段注册型代码被 `--gc-sections` 丢掉**（`list` 消失一次、`s31psram`/`s31led` 消失一次） | 已修两处。规则：新文件必须有被引用的全局符号当锚，并在 `nm` + map 里验证 |
| **R27** | **PSRAM 启用后的第三条中断崩溃路径未查明** | **未修**，PSRAM 默认关闭。前置：彻底处理未映射中断线 |

### 12.24 W18（2026-10-31 凌晨）：中断屏蔽的两个真缺陷，PSRAM 根因定位

#### 一、缺陷十四（严重）：`rt_hw_interrupt_mask()/umask()` 从来就没生效

`esprv_int_disable()` 在 S31 上被 **ROM 的 PROVIDE 覆盖**：

```
PROVIDE (esprv_int_enable  = esprv_intc_int_enable)   0x2f800758  ROM
PROVIDE (esprv_int_disable = esprv_intc_int_disable)  0x2f80075c  ROM
```

ROM 的 `esprv_intc_int_*` 是**老 INTC 接口，不写 CLIC 的 INTIE 位**。
`components/riscv/interrupt_clic.c` 的 CLIC 版本在这颗芯片上没有生效。

实测：`s31intr -m 16` 打印 `masked line 16`，紧接着 dump，`ie` 仍是 1。

**影响面比 PSRAM 大**：`rt_hw_interrupt_mask/umask`、`rt_hw_interrupt_uninstall`
里的屏蔽、`rt_hw_interrupt_install` 里的"先屏蔽再换槽"，**全部是空操作**。

**为什么自检一直绿**：GPIO 驱动用自己按引脚的 `int_ena`，`irq`/`nest` 直接写
status 寄存器触发，绕开了线路屏蔽。**绿色的测试对没被测到的接口毫无发言权。**

修法：直接读改写 CLIC 的 IE 位，不再经过 `esprv_int_*`；新增
`rt_hw_interrupt_is_masked()`，因为原来用 `esprv_int_get_priority() != 0`
判断"是否使能"——那是**级别**不是**使能位**。

#### 二、缺陷十五：清扫会误伤中断看门狗

清扫生效后剩 24/25/27（T1_WDT / CACHEERR / ASSIST_DEBUG）。IDF 故意把它们
交给汇编 panic 路径（`int_wdt.c:181-184`），handler 表里本来为空。
**屏蔽 24 等于关掉唯一能报告挂死的机制**。已显式豁免。

#### 三、上一轮那个"守卫"是死代码

`_global_interrupt_handler()`（`riscv/interrupt.c:59-69`）在 handler 槽为 NULL 时
**直接返回，不调用 `rtt_trampoline`**。所以 W17 加的
`if (c->fn == RT_NULL) 屏蔽` 对"未安装的线"一行都执行不到。
能挡住活锁的只有**在别处清 IE 位**——也就是本轮的清扫。

#### 四、100M/200M 的"挂死"其实是我们的断言 abort

符号化 dump 证明：

```
mspi_timing_psram_tuning -> mspi_timing_enter_low_speed_early
  -> mspi_timing_config_set_psram_clock -> periph_rcc_exit
  -> vPortExitCritical -> prvAssertCacheEnabled
  -> esp_system_abort("RT-Thread scheduler entry while flash cache is disabled")
```

`prvAssertCacheEnabled()`（`portable/riscv/port.c:541`）是**移植加的不变量**，
而 MSPI 调时序时合法地在关 cache 下走临界区。这是**移植不变量与 IDF 既有代码
路径的冲突**，不是 IDF 的 bug。修法方向明确，列为 PSRAM 启用的第二项前置工作。

#### 五、PSRAM 状态与里程碑

| 项 | 状态 |
|---|---|
| PSRAM 40M 启动 + `s31psram` | ✅ 64 KB @ `0x50000a1c` 写读一致，池 16.7 MB |
| PSRAM 40M + `s31test irq` | ❌ 仍中断 WDT，新路径未定位 |
| PSRAM 关闭 + 全量自检 | ✅ **20/20** |

| 里程碑 | 状态 |
|---|---|
| M5.10 `s31intr` 中断线诊断 | ✅ |
| M5.11 线路屏蔽真正生效 | ✅（缺陷十四） |
| M5.12 PSRAM 可用 | ⚠️ 功能验证通过，与 irq 测试未共存，默认关闭 |
| M5.13 MSPI 调时序与移植断言冲突 | ⬜ 已定位，未修 |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R27** | PSRAM 启用后 `s31test irq` 崩溃 | **部分修复**：mask 根因已修（缺陷十四），仍剩一条未定位路径。PSRAM 默认关闭 |
| **R28** | `prvAssertCacheEnabled()` 与 MSPI 调时序冲突，100M/200M 无法启动 | **已定位未修**。PSRAM 如需高于 40M，必须先解决 |
| **R29** | 接口"看起来接上了、实际接的是别的东西"（I2C 引脚、ROM 覆盖 esprv_int_*） | 已发现两例。规则：任何设置类操作必须跟一个能读回状态的检查 |

### 12.25 W19（2026-10-31 凌晨）：PSRAM 崩溃范围缩小，R27 定性更新

#### 一、结论先行：R27 不是中断风暴

上一轮判断"PSRAM 启用后仍有第三条中断崩溃路径"。这一轮给 `s31intr` 加了
**每线进入计数器**，直接推翻了那个判断：

| 证据 | 观察 |
|---|---|
| `s31test irq` 崩溃时的 `MEPC` | `0x2f8036ce`，符号化查不到任何函数 |
| 崩溃后 `s31intr` 的 `cnt` | **line 4 = 0**，一次都没进过中断 |
| 非中断路径（tick/sem/fpu/wifi） | PSRAM 下**全部 PASS** |
| `s31psram` | PSRAM 下**PASS**（64 KB 写读一致，池 16.7 MB） |

**所以 `rtt_trampoline()` 根本没被调用。** 前几轮"符号化 MEPC 指向
`s31_gpio_isr`，所以是 GPIO ISR 风暴"的推理**是错的**——`MEPC` 只是 WDT
采样那刻最后执行过的地址，不是调用栈。

#### 二、真正锁定的范围

只有 `rt_pin_attach_irq()` + `rt_pin_irq_enable()` 这条路径在 PSRAM 下活锁，
与具体引脚无关（pin 0 和 pin 10 都挂）。屏蔽中断看门狗后，活锁改报
`RT-Thread scheduler entry while flash cache is disabled`，栈上同时出现
`mspi_timing_config_set_psram_clock`。

**下一轮**：把 `s31_gpio_irq_setup()` 的三步（route / install / umask）
拆开逐步打点，用 `cnt` 确认卡在哪一步。这比继续符号化 MEPC 有效。

#### 三、本轮修的三个自身缺陷

1. `s31intr` 漏了 CLIC 的 +16 偏移（dump 的是内部线却标外部编号）
2. `s31intr` 级别字段没右移（打印 31/63/255 而非 1..7）
3. `selftest.c` 的三个 ISR 回调还在 flash 里（`st_irq_cb` / `st_nest_inner_isr` /
   `st_nest_outer_cb`），与 W18 修的驱动 ISR 同类，已加 `IRAM_ATTR`

#### 四、里程碑

| 里程碑 | 状态 |
|---|---|
| M5.14 `s31intr` 计数器 + 真实中断源名 | ✅ |
| M5.15 自检 ISR 全部移入 IRAM | ✅ |
| M5.16 pin IRQ 路径在 PSRAM 下的活锁 | ⬜ **已锁定范围，未修** |

#### 五、风险登记更新

| # | 风险 | 状态 |
|---|---|---|
| **R27** | PSRAM 启用后 `s31test irq` 崩溃 | **改判**：不是中断风暴（`cnt=0` 证明）。锁定为 pin IRQ 初始化路径与 MSPI 时钟切换的交互。PSRAM 默认关闭 |
| **R30** | **配置改了但没生效**：`choice` 型 kconfig（PSRAM 速度）改 `sdkconfig` 无效，必须改 `defaults` + 删 `sdkconfig` + **读回确认** | 已记录。本轮三次编译成 200M 才发现 |
| **R31** | **把 MEPC 当调用栈**：WDT dump 的 MEPC 是采样点的最后执行地址，不是出错点，据此推理会得出反的结论 | 已记录。判据改为 `s31intr` 的 `cnt` |

### 12.26 W20（2026-10-31 凌晨）：R27 关闭 —— GPIO 抢了 tick 的中断线

#### 一、根因

**不是 PSRAM、不是中断风暴、不是缓存。** 是 GPIO 驱动硬编码 `ETS_GPIO_INUM == 4`，
而 line 4 在 PSRAM 开启时属于 **OS tick**：

```
vSystimerSetup()   freertos/port_systick.c:86
    esp_intr_alloc(ETS_SYSTIMER_TARGET0_INTR_SOURCE + cpuid, ...)
    -> 分配器挑第一条可用线；PSRAM 开时正好是 4

gpio.c（旧）抢占同一行：
    rt_hw_intr_route(ETS_GPIO_INTR0_SOURCE, 4)
    rt_hw_interrupt_install(4, s31_gpio_isr, ...)
    rt_hw_interrupt_umask(4)
```

后果：tick 的源被挤走、handler 被覆盖 → tick 停 → 调度器停 → 看门狗报超时。

**PSRAM 关时能工作纯属运气**：分配器那时给 tick 另一条线，line 4 恰好空着。
PSRAM 打开后 MSPI/PSRAM 先占一条线，所有分配后移一位，运气用完。

#### 二、怎么定位的

新写 `s31pinirq`，把 setup 拆成 route/install/umask/pin_ena 四步，**每步之后读回硬件**。
第一次在 PSRAM 下跑就暴露两件事：

```
start   : ie=1 handler=yes        <- 命令还没做事，线已使能且有 handler
install : handler=ye<崩溃>        <- rt_kprintf 本身被打断
```

再修掉 `s31_line_name()` 的 +16 偏移错误（矩阵里存的是 CLIC 线号 `inum+16`，
我之前直接比 `inum`，所以永远显示 `-`），中断源名字出来，答案一目了然：
**line 4 = `SYSTIMER_TARGET0`**。

#### 三、修法

线号向分配器要，不硬编码：

```c
esp_intr_alloc(ETS_GPIO_INTR0_SOURCE, ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL1,
               rtt_trampoline, RT_NULL, &handle);
s_gpio_intno = esp_intr_get_intno(handle);
rt_hw_interrupt_install(s_gpio_intno, s31_gpio_isr, RT_NULL, "gpio");
```

handler 仍经 `rtt_trampoline`，所以 `s_ctx[]`、`s31intr` 计数器全部照常。
启动打印 `gpio: irq on line 7 (allocated, source 61)`。

#### 四、结果：PSRAM 与中断首次共存

| 配置 | 修复前 | 修复后 |
|---|---|---|
| PSRAM 关 | 20/20 | 20/20 |
| **PSRAM 开 @40M** | `irq`/`nest` 崩 | **20/20** |
| PSRAM 开 + 256 KB 写读 | 崩 | **PASS** |
| `s31wifi` / `s31ble` | PASS | PASS |

`sdkconfig.defaults` 里 **PSRAM 正式开启**（不再是注释）。

#### 五、里程碑

| 里程碑 | 状态 |
|---|---|
| M5.17 PSRAM 与中断共存 | ✅ **达成** |
| M5.18 硬件资源编号不再硬编码 | ✅ gpio 改完；gptimer 本来就用分配器 |
| M5.19 PSRAM 高于 40M | ⬜ 阻塞于 R28 |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R27** | PSRAM 启用后 `s31test irq` 崩溃 | ✅ **已关闭**。根因是 GPIO 抢占 tick 线，非 PSRAM |
| **R32** | **硬编码硬件资源编号**：`ETS_GPIO_INUM=4` 在不同配置下对应不同外设（PSRAM 关=GPIO，PSRAM 开=tick） | 已修一处，规则已立：中断线/DMA 通道/GPIO 一律向分配器申请并打印 |
| **R28** | PSRAM 100M/200M 撞 `prvAssertCacheEnabled()` | **未修**，40M 是当前上限 |
| **R33** | **显示层的偏移错误会掩盖真相**：`s31_line_name()` 少了 +16，名字全显示 `-`，让人误判"没有源" | 已修。教训：诊断工具本身的正确性要先验证 |

### 12.27 W21（2026-10-31）：R28 关闭 —— PSRAM 上到 200 MHz

#### 一、R28 的实质：移植自加的断言误伤 IDF 既有路径

`prvAssertCacheEnabled()` 原则正确（RT-Thread 调度器在 flash，cache 关着切上下文必崩），
但**粒度错了**："cache 关着"不是违规，"进入调度"才是。

MSPI 调时序必须在关 cache 的窗口里进 IDF 临界区
（`mspi_timing_tuning.c:654`），退出时被无条件 abort。

#### 二、修法：把断言收窄到"真的会调度"

```c
/* vPortExitCritical() 最外层 */
if (rt_critical_level() == 0 && rt_thread_self() != RT_NULL) {
    PRV_REQUIRE_CACHE_FOR_SWITCH();
}
```

| 调用点 | 修前 | 修后 |
|---|---|---|
| `vPortExitCritical()` | 无条件 abort | 仅当真可能调度时检查 |
| `vPortYield()` | 检查 | **仍无条件** |
| `vPortYieldFromISR()` | 检查 | **仍无条件** |

危险路径（ISR 里调度、主动让出）仍然被盯住；放开的只是
"MSPI 调时序时退出一个本来就不该调度成功的临界区"。

#### 三、四档速度全部通过

| 速度 | 修前 | 修后 |
|---|---|---|
| 40 MHz | PASS | PASS |
| 100 MHz | abort | **PASS** |
| **200 MHz** | abort | **PASS（新默认）** |
| 250 MHz | abort | **PASS**（芯片上限） |

200M 下：`s31psram 4096` 4 MB 写读一致、`s31test` 20/20 连续 3 次、
`s31wifi`/`s31ble` PASS、`check_image.py` 9/9。

#### 四、方法论：不要靠日志判断调优是否生效

200M 下启动日志仍打 `MSPI Timing: Enter psram timing tuning`，
`Readlatency` 与 40M 相同（`0x04`）。**日志没变不等于调优没跑**——
调优目标是 DQS 相位/延迟线。判据只能是**功能**：图案写读 + 全量自检。

同样，"200M 比 40M 快多少"**没有测量**，所以文档不写速度数字，只写"可用"。

#### 五、里程碑

| 里程碑 | 状态 |
|---|---|
| M5.19 PSRAM 高于 40M | ✅ **达成**，200M 为默认，250M 也可用 |
| M6 连接真实 AP | ⬜ 下一步 |
| M8 外设扩展 | 🔄 I2C 完成；PSRAM 已可用于帧缓冲/摄像头缓冲 |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R28** | PSRAM 100M/200M 撞 `prvAssertCacheEnabled()` | ✅ **已关闭**。断言收窄为条件检查 |
| **R34** | **用日志判断功能是否生效**：200M 下 timing tuning 日志照打、Readlatency 不变，据此会误判"调优没跑" | 已记录。判据改为功能验证，不用日志 |
| **R35** | **写了没测的数字**：没有 PSRAM 带宽基准，"200M 比 40M 快多少"不可写 | 已记录。文档只写"可用"，不写未测的量化结论 |

### 12.28 W22（2026-10-31）：SPI 双总线完成

#### 一、结果

`list device` 里现在有 **4 类设备 + 2 个 SPI 总线**：

```
uart0        Character Device
spi1         SPI Bus          <- 新
spi0         SPI Bus          <- 新
i2c0         I2C Bus
pwm0         PWM Device
pin          Pin Device
```

`s31spiloop 0/1` 能完成传输（返回 8 字节）；`rx` 全零是因为板上 MOSI/MISO 没短接，
命令如实说明"expected unless jumpered"，**既不报 FAIL 也不假装 PASS**。

#### 二、五个"寄存器手册里没写"的坑

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | `SPI2`/`SPI3` 符号不存在 | `GPSPI2`/`GPSPI3`（`spi_struct.h:1404`） |
| 2 | 信号名是 flash 引脚名 | `SPI2_CK_PAD_OUT_IDX` / `_D_PAD_OUT_IDX`(MOSI) / `_Q_PAD_IN_IDX`(MISO) / `_CS_PAD_OUT_IDX` |
| 3 | `spi_ll_enable_bus_clock` 是宏 | 包在 `PERIPH_RCC_ATOMIC()` 里 |
| 4 | **只开 bus clock 不够** | 还要 `spi_ll_enable_clock()`；HS/mst 是独立门，否则 `trans_done` 永不来 |
| 5 | `rt_device_find("spi0")` 是总线 | 先 `rt_spi_bus_attach_device()` |

第 4 条最隐蔽：`usr=1` 看似启动了传输，但状态机没时钟，表现为
`spi0: transfer timeout`。

第 5 条表现为 `_rt_mutex_take` → `rt_object_get_type` 处 load access fault：
总线的 `parent` 是 `rt_device`，强转成 `rt_spi_device` 会踩到不存在的锁。

#### 三、设计取舍（已写进代码注释）

- **不用 DMA**：描述符环 + 独立分配器；轮询够用，且 `SOC_SPI_MAXIMUM_BUFFER_SIZE`(64)
  本来就限制单次硬件传输
- **不做多线模式**：需要 flash 风格的 command/addr/dummy 三阶段
- **引脚不占 LCD 排座**：IO3..IO7 与 I2S 共享，启动时抢走会破坏音频路径

#### 四、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.1 I2C | ✅ |
| **M8.2 SPI ×2** | ✅ |
| M8.3 ADC | ⬜ 下一批 |
| M8.4 RMT / PCNT / MCPWM | ⬜ 无标准框架，直接 API |

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R36** | **同一外设的符号名与功能名不一致**（GPSPI vs SPI、D_PAD 实为 MOSI、Q_PAD 实为 MISO），编译器提示会误导到 flash 引脚 | 已记录。查 `gpio_sig_map.h` 的 `_PAD_` 系列，不要按功能名猜 |
| **R37** | **时钟门不止一个**：`spi_ll_enable_bus_clock` 只开 sys/apb，master 状态机要另外开 hs/mst | 已记录。同类问题可能在其它外设上重演 |
| **R38** | **`rt_device_find` 返回总线不是设备**，强转会踩未初始化的锁 | 已记录。SPI/I2C 等总线型设备必须先 attach |

### 12.29 W23（2026-10-31）：ADC 双单元完成，两个"读回 0"的坑

#### 一、结果

`list device` 现在有 **5 类设备**（char / ADC / SPI / I2C / PWM / pin）：

```
adc2         ADC Device           0
adc1         ADC Device           0
spi1 / spi0  SPI Bus
i2c0         I2C Bus
pwm0         PWM Device
pin          Pin Device
```

`adc1` 8 通道全部可读，同通道重复测量稳定到 ±10 LSB（17 位满量程 131072）。

#### 二、两个坑，都是"我以为硬件会记住，它没有"

**坑一：`enabled()` 只记了软件变量，没编程 pattern 表。**

```c
enabled():  adc->channel = channel;          /* 只记书 */
convert():  if (adc->channel != channel)     /* 永远为假 */
                adc_oneshot_ll_set_channel(); /* 永远不执行 */
```

pattern 表保持复位值 `0x00f00000`（通道 15，无效），**每次转换返回 0**。

**坑二：pattern 表每次转换都要重编程。**

表里只有 1 项，转换一次指针走过表尾，下次读 0。实测"第一次 1771，第二三次 0"。
`adc_oneshot_ll_program_pattern()` 的 `patt_p_clear` 脉冲就是复位指针。

**抓法**：先试"加延时等 SAR 同步"——**没用**；然后写 `s31adcdbg` dump 寄存器，
一次就看到 `done=1, raw=1807` 但 `s31adc` 报 0，两者一对比即知是通道选择没编程。
**猜是没用的，dump 一次就分清了**——这和 W20 的教训一致。

#### 三、明确不做的两件事

**不换算毫伏，`get_vref()` 返回 -1。** S31 没有 `SOC_ADC_CALIBRATION_V1_SUPPORTED`
也没有 RTC 控制器，IDF 校准组件给不出系数。写"典型 Vref=1100mV"看起来权威，
实际每块板偏几个百分点，下游计算会继承错误**且毫无提示**。
**raw code 是测量值，猜出来的电压不是。**

**不做连续/DMA 模式。** oneshot 是框架模型，也是唯一能在这台设备上验证的模式。

#### 四、引脚冲突（必须记录）

`adc_channel.h` 的映射是硅片固定的：adc1 ch0..7 → GPIO42..49，adc2 ch0..7 → GPIO50..57。
而 **GPIO50/51 就是这块板的 I2C 引脚**。

**所以 adc2 与 i2c0 不能同时用。** `s31adc 2 0` 直接拒绝并说明原因，传 `force` 才读。

#### 五、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.2 SPI ×2 | ✅ |
| **M8.3 ADC ×2（8 通道）** | ✅ |
| M8.4 RMT / PCNT / MCPWM | ⬜ 无标准框架，直接 API |
| M8.5 TWAI / SD / USB / LCD / CAM | ⬜ 按"有无实物"排序 |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R39** | **"硬件会记住"的假设**：通道号只记在软件、pattern 指针每次前进。两个独立的"读回 0"都源于此 | 已记录。规则：任何"设置一次、用很多次"的寄存器，先确认它是不是真的会保持 |
| **R40** | **未校准的值不能当测量值**：ADC 无校准路径时，报 raw code 而非猜出来的电压 | 已记录。`get_vref()` 返回 -1 是**正确行为**，不是缺陷 |
| **R41** | **硅片固定引脚与板级功能冲突**：adc2 ch0/1 与 i2c0 共用 GPIO50/51 | 已记录。命令层拒绝并解释 |

### 12.30 W24（2026-10-31）：PCNT 完成

#### 一、结果

四路 unit 全部通过（边数 100 / 250 / 1000 / 50，计数与边数完全一致）：

```
s31pcnt: unit 0 on GPIO16, driving 100 rising edges
  expected 100, counted 100
s31pcnt: PASS
```

#### 二、"不接线验证"的思路

PCNT 数引脚边沿，而引脚输入通路看到的是"这个脚被驱动到什么电平"——
**包括被自己的输出驱动器驱动**。所以软件拉高拉低 N 次，让 PCNT 数同一个脚，
走通了 输出驱动 → 焊盘 → 输入矩阵 → 毛刺滤波 → 计数器 的完整通路。

这是这台设备上唯一可行的完整性验证。台架做法（MCPWM 接 PCNT）做不了，
如实记录而不是假装做到了。

#### 三、坑：100 个边沿只数到 1 个

第一版背靠背写两次电平寄存器。PCNT 在 **APB 80 MHz（12.5 ns）** 上采样，
而两次写在 320 MHz 上高电平只有几十纳秒——**采样器整个高电平期都没踩到**。

修法：每个电平保持 2 µs（≈160 个 APB 周期）。改完立刻对。

**这个坑的价值在于它看起来像驱动 bug**（计数没涨 → 以为配置错），
实际是**信号比采样率快**——物理约束。失败信息里直接写了这一条。

#### 四、S31 上 PCNT 的要点

| # | 要点 |
|---|---|
| 1 | 2 group × 4 unit × 2 信号通道 + 1 控制通道 |
| 2 | APB 是唯一合法时钟源，其它值 assert |
| 3 | 计数 **16 位有符号**，回绕是硬件行为 |
| 4 | 毛刺滤波阈值以 APB 周期计，默认关闭 |
| 5 | 信号索引**通道优先**（`PCNT0_SIG_CH<c>_IN<unit>`），与其它外设相反 |

#### 五、为什么不做成 `rt_device`

RT-Thread **没有脉冲计数器设备类**。硬套只有一个调用者的 `rt_device` 包装
只增加间接层不增加契约。纯 C API + msh 命令是这个外设诚实的形状。

#### 六、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.3 ADC ×2 | ✅ |
| **M8.4 PCNT ×4** | ✅ |
| M8.5 RMT | ⬜ 下一批 |
| M8.6 MCPWM | ⬜ 下一批 |

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R42** | **采样率 vs 信号速率**：人工/软件驱动的信号可能比外设采样快，表现为"计数不涨"而非报错 | 已记录。软件驱动边沿必须 ≥ 几个外设时钟周期；失败信息要写清这条 |
| **R43** | **没有标准设备类的外设硬套 `rt_device`**：只增加间接层，不增加契约 | 已记录。PCNT 用纯 C API，是决策而非遗漏 |

### 12.31 W25（2026-10-31）：MCPWM 完成，四个缺陷 + 三次"量具失效"

#### 一、结果

互补对 + 死区，全频段通过（100 Hz ~ 20 kHz）：

| 频率 | 占空比 | PCNT 测频 | mcycle 测占空 |
|---|---|---|---|
| 100 Hz | 250 | ~97 Hz | **249/1000** |
| 500 Hz | 500 | ~500 Hz | **492/1000** |
| 1000 Hz | 500 | ~980 Hz | **499/1000** |
| 2000/5000/20000 Hz | — | 全部正确 | 不测（超量程） |

两把尺子都从输出引脚本身读，无需示波器/跳线。

#### 二、四个缺陷（前两个是真缺陷）

| # | 缺陷 | 表现 | 根因 |
|---|---|---|---|
| 1 | `START_STOP_FULL` 是一次性命令 | **0 Hz** | 语义是"跑到峰值就停" |
| 2 | 启动早于预分频写入 | **700 Hz（要 100）** | 预分频被锁存时是旧值 |
| 3 | `timern_period` 位域读错 | compare 全错 | 字段在 `[23:8]` 不是 `[15:0]` |
| 4 | `high*1000` 溢出 uint32 | **82/1000（要 250）** | 100 Hz 时 6.4e9 > 2^32 |

第 4 条形状最典型：**500 Hz 能过、100 Hz 错**——某些输入刚好不溢出。

#### 三、三次"量具失效"，都没怪被测对象

| # | 现象 | 真因 | 修法 |
|---|---|---|---|
| 1 | 100 Hz 测出 80 Hz | 50 ms 只有 5 个边沿，量化误差 20% | 自适应窗口（≥20 周期） |
| 2 | 1000 Hz 测出半周期 | 单周期抖动 | 累加 8 个周期 |
| 3 | 50% 测出 333/1000 | 从下降沿起步，量的是 低+高+低 | 以上升沿为唯一锚点 |

**关键决策**：2 kHz 时轮询法测出 285/1000（实际 300），
**把占空比测量的上限定在 1 kHz，超过报"未测量"而不是 FAIL**。

理由写进注释：**报 FAIL 是因为量具失效而指控被测对象，等于怪错了组件。**

#### 四、如实标注未验证项

- 死区的实际纳秒宽度（只编程 + 回读寄存器，没示波器）
- 输出的模拟形状（上升时间、过冲）

两条都写在 `port_mcpwm.h` 头注释里。

#### 五、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.4 PCNT ×4 | ✅ |
| **M8.5 MCPWM 互补对 + 死区** | ✅ |
| M8.6 RMT | ⬜ 剩最后一块"无标准框架"的外设 |
| M8.7 TWAI / SD / USB / LCD / CAM | ⬜ 需要实物或更大的工作量 |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R44** | **一次性 vs 连续命令**：`*_START_STOP_*` 这类枚举名字相近，语义差一个"停"，用错表现为完全没输出 | 已记录。MCPWM 用 `START_NO_STOP` |
| **R45** | **启动顺序**：分频器/动作必须在启动前写完，否则被锁存为旧值（表现为整数倍错误） | 已记录。表现为"7 倍 = 预分频值"这类可识别的比例 |
| **R46** | **32 位中间量溢出**：`high * 1000` 这类"看起来很小"的表达式在大计数值下溢出 | 已记录。用 u64；症状是"某些输入对、某些错" |
| **R47** | **量具失效时误报 FAIL**：测量手段的分辨率上限被当成被测对象的失败 | 已记录。超出量程时报"未测量"，并在容差判定前设上限 |

### 12.32 W26（2026-10-31）：RMT —— 修复三项，两项未完成

#### 一、结论先行：**RMT 没有做完**

与前几轮不同，这一轮的成果是**部分完成 + 如实标注**，而不是"又一个外设 PASS"。

| 项 | 状态 |
|---|---|
| 符号存储写入 | ✅ 修好（两个坑） |
| 时钟 / 引脚路由 | ✅ |
| 能发出边沿 | ✅ |
| **帧长控制** | ❌ 20 个符号发出 707 个边沿 |
| **完成检测** | ❌ `trans_done` 原始位不置位 |

`s31rmt` 现在**不报 PASS**，它报事实并标注 `Treat RMT as WIP`。

#### 二、修好的三个缺陷

| # | 缺陷 | 表现 | 根因 |
|---|---|---|---|
| 1 | 符号存储默认掉电 | 写入读回全 0 | `HP_SYSTEM.sys_rmt_mem_lp_ctrl`，独立低功耗域 |
| 2 | APB 窗口默认映射 FIFO | `RMTMEM[...]` 写进 FIFO | `sys_conf.apb_fifo_mask=0`，需置 1 |
| 3 | `int_ena` 默认 0 | `trans_done` 永远读不到 | 需 `rmt_ll_enable_interrupt()` |

第 2 条的教训：**我以为已经加了 nonfifo，实际没有**——写文件时被后续编辑覆盖。
**`sys_conf` 读回 0x05000010（mask=0）才是证据**，"我记得我加了"不算数。

#### 三、顺带修的两个超时错误

**(a) `esp_rom_delay_us(1)` 当超时滴答 → 23.2 秒。** 20000 次循环，每次 ~1.16 ms。
用 `board.py` 外部计时（24.66 s 含启动）交叉确认，不是错觉。

**(b) mcycle 量 23 秒跨度。** 32 位、13.4 秒回绕；算出来的 23,243,043 µs
**看起来合理纯属巧合**。改用 `rt_tick_get()`。

#### 四、未解决项

1. **帧长控制**：`tx_lim` 是阈值不是结束条件；`mem_size` 决定走几个 48 符号块。
   需要研究 IDF `rmt_tx.c` 的收尾机制。
2. **`trans_done` 原始位不置位**（`ena=1`、`raw=0`）。怀疑 loop 模式下 done 被吞，
   或 `tx_conti_mode_chn` 仍为 1（加了显式清零但没验证读回）。

#### 五、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.5 MCPWM | ✅ |
| **M8.6 RMT** | ⚠️ **部分完成**：存储/时钟/输出可用，帧长与完成检测未对 |
| M8.7 TWAI / SD / USB / LCD / CAM | ⬜ |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R48** | **外设内部子模块有独立电源域**：RMT 符号存储默认掉电，开总线时钟不会带上它 | 已记录。症状是"写入读回全 0" |
| **R49** | **同一地址窗口有 FIFO / 直接寻址两种语义**：`apb_fifo_mask` 默认 FIFO，直接写 `RMTMEM` 不生效 | 已记录。验证方式是读回 mask 位，不是"我记得设了" |
| **R50** | **`esp_rom_delay_us(1)` 不能当细粒度超时滴答**：实测每次 ~1.16 ms，20000 次 = 23 秒 | 已记录。用 cycle 自旋 |
| **R51** | **mcycle 只有 32 位**：320 MHz 下 13.4 秒回绕，长跨度测量会给出"看起来合理"的错值 | 已记录。长时间用 `rt_tick_get()` |
| **R52** | **"做了什么"与"做成了什么"混淆**：部分完成的外设如果报 PASS，会让后续工作建立在错误假设上 | 已记录。`s31rmt` 明确报 WIP 而不是 PASS |

### 12.33 W27（2026-10-31）：RMT 攻坚进展 —— 三个新证据，仍未收口

#### 一、上一轮的核心判断被推翻

我以为"零时长符号是退化符号、要用波形填满块来消灭"，正相反：
**零时长符号是 RMT 唯一认识的结束标记**（IDF `rmt_tx.c:597-630`）。
填满整块 = 删掉停止标记 = 块永久重放。这解释了 707 个边沿。

#### 二、三个新证据

| # | 证据 | 意义 |
|---|---|---|
| 1 | 零时长符号 = stop pattern | 已按正确语义改写（count 个符号 + 1 个 EOF） |
| 2 | `symbol[0]` 读回正确、`symbol[20]` 读回垃圾 | EOF 写不进索引 N，但索引 0 能 |
| 3 | `int_raw=0x100` = `TX_THRES(0)` | `tx_lim` 是"该补数据了"，**不是结束条件** |

第 2 条最硬：`mem_size=1`（48 符号块），索引 20 在范围内却写不进去。
候选解释：APB 窗口流式（写指针自增）或阵列布局与假设不符。

#### 三、本轮自己引入的两个 bug

**(a) cycle 预算单位错。** 注释写 50 ms，代码 `CPU_MHZ * 50u` = **50 µs**，小 1000 倍。
任何超过 50 µs 的帧都被中途 `tx_stop`。

**(b) 边沿计数缺空载基线。** 上一轮 4 符号 626 边沿、20 符号 630 边沿——与帧长无关。
本轮加基线后：

```
idle 50 ms : 0 edges
one frame  : 13179 edges
```

**空载 0 说明引脚没噪声，那 13179 就是真信号**——块确实在重放。
**基线测量把"量具坏了"和"被测对象坏了"区分开了**，这是上一轮缺的一步。

#### 四、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.6 RMT | ⚠️ **仍未完成**。存储(索引0)/时钟/输出可用；EOF 与 DONE 未解 |

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R53** | **把"特殊值"当成"坏值"清除**：0 时长符号是协议里的停止标记，当成噪声消灭会删掉唯一的终止条件 | 已记录。遇到全 0/全 F 的值先查它是不是魔法值 |
| **R54** | **单位错误藏在注释与代码的落差里**：注释写 50 ms、代码 50 µs，编译器不会报，只有行为异常 | 已记录。带单位的量在变量名或注释里写清换算 |
| **R55** | **缺基线的测量会把噪声算成信号**：必须先测"不驱动时的读数" | 已记录。本轮靠它区分了"量具坏"与"对象坏" |
| **R56** | **索引 0 能写不代表整个窗口能写**：窗口可能是流式的 | 新发现，下一轮入口 |

### 12.34 W28（2026-10-31）：MCPWM 判定改为"报不确定度"

#### 一、触发

收尾回归时 `s31mcpwm 1000 500 10` **失败了一次**（`off by 30/1000`），
而前几轮一直 PASS。重复 4 次：`499 / 490 / 499 / 470`——**跨 30/1000**。

PWM 的 compare 寄存器固定不变，所以这是**量具抖动**，不是被测对象。

#### 二、抖动来源与两个不够的修法

`high += (b-a)` 与 `period += (c-a)` 各自累加 8 次寄存器采样抖动，不同步 → 比值飘。

| 修法 | 结果 |
|---|---|
| 每周期占空比取中位数 | 仍有 `off by 23` |
| 容差放宽到 20/1000 | 仍有 `off by 23` |
| **报不确定度 + 60/1000 阈值** | ✅ 4/4 稳定 |

现在输出：

```
delta 1/1000 (sampler spread is ~30/1000 at this frequency)
```

**delta 和噪声都打印**，读的人能判断这是信号还是噪声。

#### 三、R47 复发（换了一种形式）

W25 记的 R47 是"量具失效时误报 FAIL"，当时说的是**量程不够**。
这一轮它**以"容差比噪声紧"的形式复发**。

已补充 R47：**容差必须 ≥ 量具噪声，且噪声要打印出来。**

#### 四、过程记录：连续行级编辑不可靠

这一轮把 `mcpwm.c` 的声明块改坏了**三次**（重复声明 `high_sum`、`samples`），
都是逐行替换时索引算错，浪费三次编译。最后按"从 `rounds` 到下一个注释"整块重写才干净。

**教训：连续的行级编辑不如整块替换可靠。**

#### 五、状态

| 项 | 状态 |
|---|---|
| MCPWM | ✅ 稳定（4/4），带不确定度输出 |
| RMT | ⚠️ WIP（§12.33） |
| `s31test` / `check_image.py` | **20/20** / **9/9** |

### 12.35 W29（2026-10-31）：RMT 完成 —— 最后两个根因都是复位默认值

#### 一、结果

| 半周期 | 符号数 | 边沿 | 时长 | 结果 |
|---|---|---|---|---|
| 50 µs | 40 | 40 | 4 ms | PASS |
| 100 µs | 1 | 1 | 0 ms | PASS |
| 100 µs | 10 | 10 | 2 ms | PASS |
| 200 µs | 7 | 7 | 2 ms | PASS |
| 500 µs | 4 | 4 | 4 ms | PASS |
| 1000 µs | 2 | 2 | 4 ms | PASS |

**6/6 全过**，边沿数 == 符号数精确相等，48 符号内存逐个比对通过。

#### 二、根因四：读指针只在 init 回卷一次

`rmt_ll_tx_reset_pointer()` 只在初始化时调用一次 → 第二帧从上一帧停下的位置继续。
症状：**第一帧对、第二帧 0 边沿**。修法：每次发射前回卷。

#### 三、根因五：`carrier_en_chn` 复位值是 **1**

`chnconf0` bit 21（`carrier_en_chn`）**复位默认开启载波调制**。
载波在符号内部翻转，于是**总时长完全正确**、但每符号约 32 个边沿。

实测 640/20 = 32，315/10 = 31.5，稳定在 32 而时长一分不差 ——
**这是调制类功能的指纹**。

加 `rmt_ll_tx_enable_carrier_modulation(dev, ch, false)` 后边沿数精确等于符号数。

#### 四、五条通用教训

| # | 教训 |
|---|---|
| 1 | **复位默认值可以是功能而不是零**：`carrier_en=1` 意味着"没人要求的功能默认开着" |
| 2 | **指针类状态必须每次使用前复位**，不能只在 init 里复位一次 |
| 3 | **"总时长对、边沿数错"→ 调制功能；"总时长也错"→ 长度控制**。两类要分开查 |
| 4 | **前一轮认定的"未解决"可能有两个叠加根因**：修好帧长才看得见载波 |
| 5 | **诊断顺序**：先看总时长 → 再看边沿数 → 再看"第一帧 vs 第二帧" → 最后看内存写入 |

#### 五、顺带修好：长半周期需要分频

时长字段 15 位，25 ns tick 只到 819 µs。现在 `rt_hw_rmt_square()` 自动抬分频器
（1 ms 用 div=8）。`rt_hw_rmt_ws2812()` 固定 div=1 —— WS2812 要 25 ns 分辨率。

**这是有意的取舍**：分频牺牲分辨率，对 1 ms 方波无所谓，对 WS2812 不可接受。

#### 六、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.5 MCPWM | ✅ |
| **M8.6 RMT** | ✅ **完成**（6/6 参数，内存逐个比对） |
| M8.7 TWAI / SD / USB / LCD / CAM | ⬜ 剩五项 |

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R57** | **复位默认值可能是"功能开着"而不是"零"**：`carrier_en_chn` 默认 1，没人要求它，症状是"数量对但细节错 32 倍" | 已记录 |
| **R58** | **指针/状态只在 init 复位一次**：第二帧起行为不同，症状是"第一次对、之后错" | 已记录 |
| **R59** | **"总时长对、边沿数错"指向调制类功能**，与"总时长也错"属不同故障类 | 已记录，作为诊断分流依据 |
| **R60** | **一个"未解决项"可能有两个叠加根因**：修好第一个才看得见第二个 | 已记录。RMT 用了三轮 |

### 12.36 W30（2026-10-31）：TWAI/CAN 基础设施通了，帧不完成（WIP）

#### 一、已建立

| 项 | 证据 |
|---|---|
| 时钟 / 存储上电 / 引脚路由 | 寄存器读回与写入一致 |
| 控制器接受帧 | TX buffer 状态 `1`（Ready） |
| 控制器取走帧 | `1 → 2 → 6` |
| 帧失败 | 状态 `6` = `TXT_ERR` |

#### 二、排除掉的五个假设（全部实测）

| # | 假设 | 结论 |
|---|---|---|
| 1 | 只设 `stm` 能回环 | ❌ 必须同时设 `ilbp` |
| 2 | RX 脚可不拉 | ❌ 浮空看不到 11 个连续隐性位 |
| 3 | TX buffer 空 = 0 | ❌ 编码：`1=Ready 2=TX 4=OK 6=Failed 8=Empty`，**复位 8** |
| 4 | 三段之和 = quanta | ❌ 必须 = `quanta - 1`（同步段不可编程） |
| 5 | `afm` 默认可用 | ❌ `afm=1` 无过滤器 = 全部丢弃 |

第 3 条：`while (status != 0)` 永远等不到。
第 4 条：1 Mbit/s 实际跑 1.33 Mbit/s。

#### 三、未解决

```
125k/250k/500k: buf 6 -> 6 -> 6, tec=0
     1 Mbps  : buf 1 -> 6 -> 6, tec=160
```

**低三档 tec=0 也是失败，1 Mbps 才有错误累加 —— 两个不同的问题。**
低三档可能根本没上总线；1 Mbps 上了但位错误。
另外 1 Mbps 的 `brp = 2.5 → 2`，**实际 1.25 Mbit/s**，这一档本身不准。

#### 四、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.6 RMT | ✅ 完成 |
| **M8.7a TWAI/CAN** | ⚠️ **基础设施完成，帧不完成** |
| M8.7b SD/MMC / USB / LCD / CAM | ⬜ 剩四项 |

#### 五、过程教训

两次 `flash` 卡 600 秒 → 残留 python 进程占着 COM16。
**flash 卡住先查进程**，清理后 `esptool chip_id` 立即连通。

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R61** | **模式位有依赖关系**：`ilbp` 单独设没用，`stm` 单独设也没用，必须组合 | 已记录 |
| **R62** | **状态字段是编码不是标志**：`8` 才是 Empty，`0` 是"不存在" | 已记录，与 R57 同类 |
| **R63** | **不可编程的固定段容易被漏算**：CAN 的同步段、SPI 的建立时间 | 已记录。凡"总计 = 各部分之和"都要先问有没有隐藏常量项 |
| **R64** | **整数取整会改变物理量**：`brp = 2.5 → 2` 把 1 Mbit/s 变成 1.25 Mbit/s | 已记录 |
| **R65** | **过滤器默认行为可能丢弃一切**：`afm=1` + 无过滤器 = 全丢，症状同"没信号" | 已记录 |
| **R66** | **串口工具残留进程会占住端口**：flash 卡住先查进程，不要等超时 | 已记录 |

### 12.37 W31（2026-10-31）：TWAI/CAN 完成 —— 根因是 `ilbp` 不生效

#### 一、决定性对比

| 配置 | 结果 |
|---|---|
| 单脚 TX=RX=GPIO23 | **4/4 PASS** |
| 两脚 TX=23, RX=24 | `tec=160, rec=0`（全失败） |

**`ilbp` 读到 1 但不产生行为。** 单脚能通证明控制器、时钟、时序、
帧格式、TX buffer、RX FIFO 全部正确。

上一轮"低三档 tec=0 是另一个问题"的判断**是错的**——当时状态机等待没修，
失败发生在更早阶段，掩盖了真实原因。

#### 二、修好的四个缺陷

| # | 缺陷 | 症状 | 修法 |
|---|---|---|---|
| 1 | 没等 `FCSI` 状态切换 | `buf 1→6`，`tec=0` | 等 FCSI 置起（带超时） |
| 2 | `brp` 截断 | 1 Mbps 实际 1.25 Mbps | 8..25 quanta 搜精确解 |
| 3 | `set_operate_cmd` 无超时自旋 | **控制台死掉** | 直接写 `command.val` + cycle 预算 |
| 4 | RX 排空无上界 | 同上 | 最多 4 次 |

第 1 条修好后，四档 `tec` 从"只有 1Mbps 有"变成"四档全 160"——
**行为一致了才看得出真正原因**。

第 3、4 条是**自己引入的死锁**，两次误判成串口问题。

#### 三、自己踩的坑：`rwcnt` 不是字数

```c
uint32_t rwcnt: 5;   // Re-transmission counter
```

我按名字猜成"字数"，写入 `(len+3)/4`，**把全通过的测试改成全失败**。
已回退。**字段名有歧义时先读定义。**

#### 四、完整验证

`s31twai` 连跑三次 4/4 PASS。`s31twai 0`（两脚）明确报已知限制。

**模拟层完全没测**：无收发器、无差分对、无终端电阻、无真实仲裁、无错误帧。

#### 五、里程碑

| 里程碑 | 状态 |
|---|---|
| M8.6 RMT | ✅ |
| **M8.7a TWAI/CAN** | ✅ **完成**（控制器路径，4 码率 × 3 次） |
| M8.7b SD/MMC / USB / LCD / CAM | ⬜ 剩四项 |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R67** | **硬件回环位可能"读到 1 但不生效"**：`ilbp` 在 S31 上如此，替代方案是外部/单脚回环 | 已记录。回环不通时先试单脚 |
| **R68** | **SDK 的 LL 辅助函数可能无超时自旋**：`twaifd_ll_set_operate_cmd` 会死循环 | 已记录。凡 `while (reg & bit);` 都自己加预算 |
| **R69** | **字段名可能与语义不符**：`rwcnt` 是重传计数器不是字数 | 已记录。按名字猜会改错方向 |
| **R70** | **修好第一个缺陷后，现象会"变整齐"**：四档 tec 从分散变成一致，这时才是看真正原因的时候 | 已记录。现象变一致是进展不是退步 |

### 12.38 W32（2026-10-31）：P3 前置风险 R24 被证伪

#### 一、R24 说什么

> 兼容层静态表只用 `rt_hw_interrupt_disable()` 保护，SMP 下只关本核 → 不互斥。

plan 曾把它列为"P3 最大的机械改动量"。

#### 二、用编译器验证

```c
$ riscv32-esp-elf-gcc -S -O2 probe_r24.c
probe_smp:
    tail    rt_cpus_lock
```

`rthw.h:146`：
```c
#ifdef RT_USING_SMP
#define rt_hw_interrupt_disable rt_cpus_lock
#endif
```

`cpu_mp.c:168`：`rt_cpus_lock()` = 关本地中断 + `rt_enter_critical()` + `rt_hw_spin_lock(&_cpus_lock)`

**打开 SMP 后同一行代码自动互斥。** ISR 路径也安全：`cpus_lock_nest` 是嵌套计数，
`nest != 0` 时不重复取锁。

#### 三、结论与改动量

| 项 | 原估 | 实际 |
|---|---|---|
| shim 四张静态表加锁 | 大量改动 | **0 行** |
| `ft_task_notify_isr` 改写 | 可能需要 | **不需要** |

**R24 已撤销。**

#### 四、顺带探明 P3 边界

只改 `RT_CPUS_NR=2` + `scheduler_up.c→mp.c`，构建给出 **8 条编译错误**，
全部指向 `#ifdef RT_USING_SMP` 里的结构成员和宏（`priority_group`、`oncpu`、
`bind_cpu`、`RT_CPU_MASK`、`rt_hw_ipi_send` …）。

**这 8 条把"开 SMP 要补什么"从猜测变成清单**，且全部编译期枚举。
已回滚，单核 20/20。

#### 五、方法论

R24 是"读函数名就开始推理"的产物：`rt_hw_interrupt_disable` 听起来就是关中断。
**但那个名字是宏，含义由条件编译决定。**

与 R28（按名字猜 `SPI2`，实际叫 `GPSPI2`）同源。**两次都是名字给了错误的确定感。**

**新规则：涉及"某符号在某个配置下是什么"时，让编译器回答。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R24** | ~~兼容层静态表在 SMP 下不互斥~~ | ✅ **撤销**（W32） |
| **R71** | **按名字推断语义**：`rt_hw_interrupt_disable` 是宏不是函数；`rwcnt` 是重传计数不是字数；`SPI2` 不存在 | 已记录（合并 R28/R69）。**让编译器或头文件回答** |
| **R72** | **条件编译改变符号含义**：同一个调用在不同 `RT_USING_*` 下是不同函数 | 已记录。跨配置复用代码时必须重新验证 |

### 12.39 W33（2026-10-31）：P3 待办清单量化 —— 11 个符号

#### 一、补上 `RT_USING_SMP` 之后的真实错误

前面 8 条是"宏没定义"。补上定义后：

**第一批（1 条，编译期）**：`rttypes.h:161: cpuport.h: No such file or directory`
—— `rt_hw_spinlock_t` 所在的头只在 rtthread 组件的 `PRIV_INCLUDE_DIRS` 里，
`include <rtthread.h>` 的其他组件看不到。**这是"私有 vs 全局包含路径"的边界问题。**

**第二批（11 个符号，链接期）**：

| 符号 | 位置 | 性质 |
|---|---|---|
| `rt_interrupt_nest` | `vectors_rtthread.S:144/202` | SMP 下是 `rt_cpu_self()->irq_nest`，**不是变量** |
| `rt_thread_switch_interrupt_flag` | `vectors_rtthread.S:217` | `cpuport.c:27` 在 `#ifndef RT_USING_SMP` 内 |
| `rt_interrupt_from_thread` | `vectors_rtthread.S:226` | 同上 |
| `rt_interrupt_to_thread` | `vectors_rtthread.S:232` | 同上 |
| `rt_hw_spin_lock/_unlock/_init` | `components.c`、`cpu_mp.c` | 参考 `libcpu/risc-v/common/cpuport.c:66` |
| `rt_hw_cpu_id` | `clock.c`、`idle.c`、`scheduler_mp.c` | 参考 `cpu.c:251` |
| `rt_hw_ipi_send` | `scheduler_mp.c:329` | 接 `ETS_IPC_ISR_INUM`(28) |
| `rt_hw_secondary_cpu_up` | `components.c:259` | 从核启动 |
| `rt_hw_secondary_cpu_idle_exec` | `idle.c:128` | 从核 idle |

#### 二、三个洞察

**(a) `rt_interrupt_nest` 不是变量 → `la` 取地址的写法必须换。**
plan 原写"全局量 per-CPU 化"，方向对但**低估了性质**：不是加偏移，
是这些量在 SMP 下**换了存在形式**。

**(b) `cpuport.c` 三个全局量整体挪进 `#else`。** SMP 下内核走
`rt_scheduler_do_irq_switch()`（`scheduler_mp.c:1016`）。

**(c) spinlock / cpu_id 可直接抄** `libcpu/risc-v/common/cpuport.c` 的 ticket lock。

#### 三、回滚坑：只删 `.obj` 不够

`build/` 残留 SMP 的 CMake 源列表，删单个 `.obj` 无效，**必须删整个 `build/`**。
前面的改动都是宏或新增源文件（会触发重编译/re-configure），**删源文件不会**。

#### 四、工作量量化

| 项 | 之前 | 现在 |
|---|---|---|
| R24（shim） | 大量改动 | **0 行** |
| P3 总体 | 不确定 | **11 符号 + 1 处汇编改写 + 3 个新函数** |
| spinlock | 待设计 | 可复用 ticket lock |

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R73** | **私有包含路径够了自己不够别人**：往公共头加 `#include` 时，该头必须在全局包含路径下 | 已记录 |
| **R74** | **删源文件不会触发 CMake 重配置**：宏变化会，源列表缩减不会，必须删 `build/` | 已记录 |
| **R75** | **"per-CPU 化"不等于"加偏移"**：有些量在 SMP 下变成宏，取地址的写法直接失效 | 已记录 |

### 12.40 W34（2026-10-31）：P3 待办 11 → 4，ticket lock 的半字取反

#### 一、实现的 7 个符号

新增 `port/esp32s31/smp_prims.c`（6 个）+ `port/esp32s31/smp_ipi.c`（1 个），
全部包在 `#ifdef RT_USING_SMP` 内，单核构建是空 object。

| 函数 | 要点 |
|---|---|
| `rt_hw_cpu_id` | `csrr mhartid`（与 IDF `rv_utils.c` 一致） |
| `rt_hw_spin_lock_init/_lock/_unlock` | ticket lock |
| `rt_hw_secondary_cpu_up` | **空实现**（安全方向） |
| `rt_hw_secondary_cpu_idle_exec` | `wfi` |
| `rt_hw_ipi_send` | CLIC pending 位读改写 |

#### 二、抓到的真 bug：ticket 取错了半字

```c
ticket = tmp & 0xFFFF;      // 错
ticket = (tmp >> 16) & 0xFFFF;   // 对
```

`amoadd` 加 `1<<16` 推进 `next`，返回加之前的值 → 票号在**高**半字。
取低半字**永远是 0**，所有核拿同一张票，**全部同时进临界区**。

**而且无竞争时完全正常**，只在真并发时错——最难查的一类。

Python 复算确认：正确布局给 `[0,1,2]`，错误表达式给 `[0,0,0]`。

#### 三、第二个坑：`pause` 编不出来

需要 `Zihintpause` 扩展，工具链默认 `-march` 不含。上游 `opcode.h` 用宏包了
`.4byte 0x0100000f` 的原始编码，但 `legacy_common/cpuport.h` 没有这个宏。
直接写原始编码：同一指令，不支持的核当无操作。

#### 四、CLIC pending 必须读改写

`CLIC_INT_IP`(bit0)、`CLIC_INT_IE`(bit8)、`CLIC_INT_CTL`(bit31:24)、
`CLIC_INT_ATTR_TRIG`(bit18:17) **共用 `CLIC_INT_CTRL_REG(i)` 一个字**。
盲写 `= CLIC_INT_IP` 会连优先级和使能一起清掉。

#### 五、清单 11 → 4

| 状态 | 符号 |
|---|---|
| ✅ | cpu_id、spin_lock 三件套、secondary_cpu_up/idle_exec、ipi_send |
| ❌ 剩 4 | `rt_interrupt_nest`、`rt_thread_switch_interrupt_flag`、`rt_interrupt_from/_to_thread` |

**剩 4 个是同一件事**：`vectors_rtthread.S` 三处 `la` + `cpuport.c` 三个全局量挪进 `#else`。

#### 六、两次回滚踩的同一个坑

**第一次**：恢复 `CMakeLists.txt` 时漏掉
`idf_build_set_property(COMPILE_DEFINITIONS RT_USING_USER_MAIN APPEND)`。
构建能过，**运行时**：

```
ESP_ERROR_CHECK failed: esp_err_t 0x103
[FAIL] init table order
[PASS] __fsymtab non-empty  144 B = 12 commands   <- 从 30 掉到 12
```

`RT_USING_USER_MAIN` 没了，`.rti_fn` 顺序错乱，板级初始化没跑完。

**第二次**：源文件列表重复加 `smp_prims.c`、漏掉 `smp_ipi.c`。

**教训：`CMakeLists.txt` 这类"改一行影响全局"的文件，用 `Compare-Object` /
`difflib` 对基线逐行核对**，比自己读可靠。最后是打印出恰好 4 处预期差异才收手。

#### 七、观测到 TWAI 间歇失败（未定位）

回归时 `s31twai` 有一次报 `0/4`，随后连跑三次 `4/4`。
**如实记录：单脚回环存在约 1/5 的间歇失败，待查。**

#### 八、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R76** | **ticket lock 半字布局错误无竞争时不显现**：只在真并发时爆 | 已记录。必须用数值复算验证 `[0,1,2]` |
| **R77** | **`pause` 需要 Zihintpause**：老工具链编不出，需原始编码 | 已记录 |
| **R78** | **CLIC 一个寄存器字里挤了 IP/IE/优先级/触发**：必须读改写 | 已记录 |
| **R79** | **`CMakeLists.txt` 的漏行只在运行时暴露**：`RT_USING_USER_MAIN` 掉了会让命令数从 30 掉到 12 | 已记录。改完必须 `difflib` 对基线 |

### 12.41 W35（2026-10-31）：P3 链接期清零，SMP 能链接但不启动

#### 一、4 个剩余符号的解决

| 符号 | 修法 |
|---|---|
| `rt_interrupt_nest` | 新增 `rt_hw_irq_nest_addr()` 返回本核计数器地址；汇编改成 `call` + `lw` |
| `rt_thread_switch_interrupt_flag` | SMP 分支不再引用（走 `rt_scheduler_do_irq_switch`） |
| `rt_interrupt_from_thread` | 同上 |
| `rt_interrupt_to_thread` | 同上 |

**关键发现**：`vectors_rtthread.S` 原本看不到 `rtconfig.h`，但**上一轮把
`rtt_config_dir` 加进全局包含路径的修复顺带解决了**——加一行 `#include "rtconfig.h"`
即可，不用改 riscv 组件。

#### 二、exit 路径按参考实现分叉

参考 `interrupt_gcc.S:312-340` 的 SMP 分支：

```asm
    mv    sp, s0
    mv    a0, s0
    call  rt_scheduler_do_irq_switch
    tail  rt_hw_context_switch_exit
```

SMP 下**完全不碰**三个全局量。已按此改写。

#### 三、撞到的两个链接问题

**(a) `-u` 锚点缺失**：`nm` 确认符号在 `librtthread.a` 里，但 riscv 组件的汇编
**在归档之后**被扫描，那时没人引用了。加 `-u rt_hw_irq_nest_addr`。
同时把函数定义成**两个分支都编译**（单核返回全局量地址），让这条路径本身成立。

**(b) `cpu_up.c` 与 `cpu_mp.c` 重复定义**：两者是同一套接口的两个配置版本。
上游 `src/SConscript:26-29` 明确：
`RT_USING_SMP` 关 → 删 `cpu_mp.c`/`scheduler_mp.c`；开 → 删 `cpu_up.c`/`scheduler_up.c`。
**我们的 CMakeLists 两个都列了。**

#### 四、结果

```
Project build complete.      <- 无 undefined，无 multiple definition
Hash of data verified.
esptool chip-id: Connected   <- bootloader 正常
serial: 停在 esp_image 加载   <- 到不了 RT-Thread banner
```

**从"11 个符号缺失"推进到"链接干净、运行时死"。链接期问题清零。**

#### 五、下一轮入口（已看出可疑点）

参考实现的 `rt_scheduler_do_irq_switch(s0)` 传的是**切换中断栈之前**的 sp，
而我写的是 `mv a0, sp`（当前 sp）。**这一处很可能就是不启动的原因。**

其余候选：
1. `rt_hw_secondary_cpu_up` 是空实现，内核可能在等从核
2. 双核初始化顺序

#### 六、里程碑

| 里程碑 | 状态 |
|---|---|
| M7 双核 SMP (P3) | ⚠️ **链接通过，不启动**。11 个符号全解决 |
| 单核固件 | ✅ 20/20、9/9、30 命令、外设全 PASS |

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R80** | **归档扫描顺序**：符号在库里不代表会被拉进来，riscv 汇编在归档之后扫描 | 已记录。用 `-u` 或双分支定义 |
| **R81** | **同一接口的两个配置实现不能同时链接**：`cpu_up.c` vs `cpu_mp.c` | 已记录。照上游 SConscript 的规则选 |
| **R82** | **连续三轮栽在 CMakeLists 上**：删/加源文件后位置错 | 已记录。**改完必须 `difflib` 对基线** |
| **R83** | **链接通过 ≠ 能跑**：SMP 的运行时正确性还没验证一步 | 已记录 |

### 12.42 W36（2026-10-31）：SMP 失败范围收缩到 banner 之前，三个假设被排除

#### 一、两个受控实验

| 配置 | 结果 |
|---|---|
| `RT_USING_SMP` + `RT_CPUS_NR=2` | 链接通过，不启动 |
| `RT_USING_SMP` + `RT_CPUS_NR=1` | **同样不启动，现象完全相同** |

第二个实验的动机：`RT_CPUS_NR=2` 时内核以为有两个核，而
`rt_hw_secondary_cpu_up()` 是空实现——只有 CPU0 在跑，IPI 没有接收方。
构造"走全部 SMP 代码路径但只用一核"的中间态，可以把这个因素单独排除。

#### 二、排除的三个假设

| 假设 | 状态 |
|---|---|
| 从核没启动导致内核等待 | ❌ **排除**（`RT_CPUS_NR=1` 无从核，照样死） |
| IPI 发到不存在的核导致挂 | ❌ **排除** |
| `rt_hw_secondary_cpu_up` 空实现是死因 | ❌ **排除** |

**三个看似合理的原因一次排掉，这是本轮最有价值的产出。**

#### 三、失败范围：banner 之前

`rtthread_startup()` 顺序：

```c
rt_hw_spin_lock_init(&_cpus_lock);   // SMP only
rt_hw_local_irq_disable();
rt_hw_board_init();
rt_show_version();                   // <- banner，没到
```

**范围从"整个启动过程"缩到三个函数。**

#### 四、发现的隐患：核数不变量没人守

`esp_system/startup.c:23-31`：

```c
#if !CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE
#ifndef RT_USING_SMP
#error "RT-Thread requires RT_USING_SMP when IDF starts multiple cores."
#elif RT_CPUS_NR != SOC_CPU_CORES_NUM
#error "RT-Thread and IDF must use the same number of cores."
#endif
#endif
```

项目目前 `CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y`，**这组断言从不生效**。
`RT_CPUS_NR=2` 时 IDF 只启一核、RT-Thread 以为有两核——**静默不一致**。

#### 五、过程记录：串口无输出先查芯片

实验二后串口完全静默（连 bootloader 都不打）。`esptool chip-id` 仍能连上，
说明芯片没坏，是复位时序问题。**判断"固件死了"之前先确认芯片能应答。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R84** | **核数配置的不变量没有守卫**：`CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE` 与 `RT_CPUS_NR` 可以静默不一致 | 已记录 |
| **R85** | **串口静默≠固件死**：先 `esptool chip-id` 确认芯片应答 | 已记录 |
| **R86** | **"空实现 stub"可能掩盖真因**：`secondary_cpu_up` 空着时看起来最可疑，实验证明不是 | 已记录。**先做受控实验再改** |

### 12.43 W37（2026-10-31）：SMP 不启动根因确定 —— flash cache 窗口里的函数调用

#### 一、探针方法

`esp_rom_printf` 直接写 UART FIFO，不需锁/堆/调度器，是唯一能在最早点输出的手段。
单核构建 5 个探针全部打印，hart=0，方法本身验证通过。

#### 二、SMP 构建：一个探针都没打印，但有 panic dump

```
I (409) MSPI Timing: Enter psram timing tuning
Guru Meditation Error: Core 0 panic'ed (Instruction access fault)
MEPC   : 0x40013b1e        <- IROM (flash 映射段)
MCAUSE : 0x00000001        <- instruction access fault
```

#### 三、根因

```
$ nm -n | 找 0x40013b1e
fault 0x40013b1e falls in: 40013b1e T rt_enter_critical
```

**调用链**：

```
trap -> vectors_rtthread.S              [IRAM，能执行]
      -> rt_hw_irq_nest_addr()          [原 flash -> 已加 IRAM_ATTR]
        -> rt_cpu_self()/rt_hw_cpu_id() [flash]
          -> rt_enter_critical()        [flash] <- MEPC
```

**MSPI PSRAM timing tuning 期间 flash cache 关闭**，跳到 flash 取指令即 fault。

**单核版不崩是因为它 `la t0, rt_interrupt_nest` 直接读全局，零跳转。**

#### 四、为什么只加 `IRAM_ATTR` 不够

加了之后 MEPC 仍是 `0x40013b1e`（只从 `rt_hw_cpu_id` 变成 `rt_enter_critical`，
因为 RA 变了）。**`IRAM_ATTR` 只解决链条第一环**，逐环加不现实。

#### 五、正确修法（下一轮）

| 方案 | 做法 | 评价 |
|---|---|---|
| A. 汇编里算 `_cpus[n].irq_nest` 偏移 | 需要硬编码结构偏移 | 脆弱 |
| **B. `tp` 存 per-CPU 指针** | 启动时 `tp = rt_cpu_self()`，trap 里 `lw t0, off(tp)` | **推荐** |

**方案 B 更稳**：`tp` 是 RISC-V 约定的 thread pointer，RT-Thread 参考实现
（`cpu.c:251`）也读 `tp`。

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R87** | **trap 路径不能调用 flash 里的函数**：MSPI/PSRAM 调时序时 cache 是关的 | 已记录。IRAM 路径必须**全程无跳转**或全部 IRAM |
| **R88** | **`IRAM_ATTR` 只治标**：一条调用链要每环都在 IRAM 才有用 | 已记录。改用寄存器直读 |
| **R89** | **探针不能依赖配置相关符号**：探针里调 `rt_hw_cpu_id()` 导致单核构建链接失败 | 已记录 |

### 12.44 W38（2026-10-31）：修法方向对了一半，单核回滚

#### 一、做了什么

两处 `call rt_hw_irq_nest_addr` 改成 mhartid 索引的 IRAM 指针表：

```asm
    la    t1, rt_irq_nest_slot
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t1
    lw    t0, 0(t0)
```

#### 二、结果：修好一半，弄坏一半

| 版本 | 现象 |
|---|---|
| 函数调用版 | **挂在 `Enter psram timing tuning`**（cache 关闭窗口） |
| 表在 BSS + board_init 填 | **更早挂**（中断先于 board_init，slot=NULL） |
| 表静态初始化 | **通过 PSRAM tuning**，挂在 `Single core mode` 之后 |

**关键证据**：静态表版**通过了 `Enter psram timing tuning`** ——
上一版正是在这里 fault。所以"trap 路径不能调 flash"的诊断**是对的**。

但静态表版在 `Single core mode` 之后静默挂住，无 panic、无重启。

#### 三、未查清

`IRAM_ATTR` 用在带非零初值的数据上，ESP-IDF 的 IRAM 段对初始化数据支持不可靠。
但若 `slot[0]` 真是 0，trap 里 `lw 0(0)` 应有 access fault + panic，
**实际没有 panic**，所以更像别的原因（自旋？）。**未验证，如实记录。**

#### 四、决策：回滚单核，保留 SMP 分支

单核固件稳定跑了 20 轮，不能为 P3 冒险。汇编改回 `#ifdef` 分叉：

```asm
#ifdef RT_USING_SMP
    ... mhartid 索引表 ...
#else
    la    t0, rt_interrupt_nest
    lw    t0, 0(t0)
#endif
```

回滚后：**20/20、9/9、30 命令、外设全 PASS**。

#### 五、结论与下一步

**"修好一个现象"不等于"修对了"。** 两个版本各自修掉一半。

**真正结论：trap 路径必须自包含。** 表方案方向对，但"IRAM 里放带初值的数据"
这条路需要再验证。下一步方向：**在汇编入口自己写表**（`la` + `sw`，cache 还开着时），
不依赖 C 的初始化数据。

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R90** | **IRAM 段的初始化数据可能不可靠**：`IRAM_ATTR` + 非零初值 | 已记录，未验证 |
| **R91** | **修好一个现象可能引入另一个**：两个版本各修一半 | 已记录。**通过 PSRAM tuning 是新证据，说明诊断对了一半** |
| **R92** | **主固件不能为实验冒险**：单核稳定 20 轮，改动必须先能回滚 | 已记录。`#ifdef` 分叉是正确做法 |

### 12.45 W39（2026-10-31）：表路径修好，抓到两个自己的汇编 bug

#### 一、W38 的判断是错的

W38 判定"`IRAM_ATTR` 用在数据上不可靠"。**这个判断是错的**，
真正原因是两个我自己写的汇编错误。W38 的误判是**有害的**：
它让我把表挪进 IRAM 段，反而掩盖了真正的问题。

#### 二、bug 1：覆盖了 `t1`

```asm
    mv    t1, sp                 ; t1 = 本层 context 指针
    la    t1, rt_irq_nest_slot   ; ← 覆盖了唯一不能碰的寄存器
    STORE t1, 0(sp)              ; 存进去的是表地址
```

原始代码只用 `t0`，所以 `t1` 安全。修法：改用 `t2`。

#### 三、bug 2：少一次解引用

```asm
    lw    t0, 0(t0)        ; t0 = slot[n] = &nest  ← 是指针
    bnez  t0, nested       ; 指针永远非零 -> 永远走嵌套分支
```

**后果极隐蔽**：板子能启动、能打 banner、`msh >` 出现，
但 `bnez` 永远成立，外层永不切中断栈，`rt_interrupt_nest` 从不归零，
**命令跑不完**。

#### 四、三轮判断的对照

| 轮次 | 判断 | 实际 |
|---|---|---|
| W37 | flash cache 窗口里的函数调用 | ✅ **对** |
| W38 | `IRAM_ATTR` 用在数据上不可靠 | ❌ **错** |
| W39 | 少一次解引用 | ✅ **对** |

#### 五、最终修法

```asm
    la    t2, rt_irq_nest_slot
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t2
    lw    t0, 0(t0)         ; slot[n]
    lw    t0, 0(t0)         ; 值
```

表在普通 `.data`（`2f0187b8 D`），**不需要 `IRAM_ATTR`**。
DRAM 在 cache 关闭时仍可读，只有**指令**需要 IRAM。

**单核现在走表路径并验证通过** —— 这意味着 SMP 的这段汇编被每轮自检覆盖。

#### 六、SMP 失败点前进了一步

```
MEPC : 0x40013b26  -> rt_enter_critical
RA   : 0x2f010f92  -> vPortEnterCritical
```

调用链 `vPortEnterCritical → rt_enter_critical → rt_cpus_lock → rt_hw_spin_lock`，
而 `rtthread_startup()` 里 `rt_hw_spin_lock(&_cpus_lock)` 之后**不释放**，
靠 `rt_cpus_lock_status_restore` 恢复。**SMP 下第一个临界区就冲突 → 自旋死。**

**这是比 cache 窗口更深的问题：SMP 的锁状态恢复链没接上。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R93** | **拿"不能碰的寄存器"当临时寄存器**：`t1` 在这一段保存 context | 已记录。改汇编前必须列出活跃寄存器 |
| **R94** | **少一层间接寻址的症状极隐蔽**：能启动、能打 banner，只是命令跑不完 | 已记录。指针表必须有第二次 `lw` |
| **R95** | **错误诊断会导致错误修复**：W38 把 `t1` 的 bug 误判为 IRAM 数据问题 | 已记录。**诊断要和"改完是否真的好了"交叉验证** |

### 12.46 W40（2026-10-31）：SMP 首次打出 banner —— 根因是 linker fragment 漏了 `*_mp` 对象

#### 一、根因

同一函数在两个构建里落点不同：

```
单核:  rt_enter_critical = 0x2f016184   (RAM)
SMP :  rt_enter_critical = 0x40013b26   (IROM/flash)
```

`linker.lf` 只写了 `scheduler_up:*` / `cpu_up:*` 的 noflash 规则。
**`RT_USING_SMP` 时这些翻译单元根本不在链接里**，实现来自
`scheduler_mp.c` / `cpu_mp.c`，**没有任何 noflash 规则**。

IDF 关 flash cache 调 PSRAM 时序时，中断路径跳进 IROM → fault。

**这是代码放置问题，不是锁语义问题。前几轮往锁的方向找是错的。**

#### 二、修法

给 `scheduler_mp` / `cpu_mp` / `smp_prims` 补上同样的 noflash 规则：

```diff
+ scheduler_mp:rt_enter_critical (noflash)
+ scheduler_mp:rt_exit_critical (noflash)
+ scheduler_mp:rt_critical_level (noflash)
+ scheduler_mp:rt_soft_atomic_add (noflash)
+ cpu_mp:rt_cpu_self (noflash)
+ cpu_mp:rt_cpus_lock (noflash)
+ cpu_mp:rt_cpus_unlock (noflash)
+ scheduler_mp:rt_cpus_lock_status_restore (noflash)
+ scheduler_mp:rt_sched_post_ctx_switch (noflash)
+ smp_prims (noflash)
```

**逐符号写法 `smp_prims:符号名` 不生效**（生成的 `sections.ld` 查不到），
必须用**整个对象** `smp_prims (noflash)`。ldgen 的对象名匹配比预想严格。

#### 三、修复前后

| 阶段 | 现象 |
|---|---|
| 前 | Instruction access fault，MEPC 在 IROM，**banner 之前死** |
| 后 | **banner 正常打印，无 panic** |

`nm` 验证五个关键函数全部进 RAM（`2f0160a8` ~ `2f016336`）。

#### 四、新失败点

banner 之后 shell 不响应。**不是 panic，是卡住。**
范围从"banner 之前"缩到"banner 之后"。

#### 五、单核不受影响

`*_mp` 规则在单核构建里匹配不到任何东西。单核验证全过：
20/20、9/9、30 命令、外设全 PASS。

#### 六、五轮排查路线

| 轮次 | 判断 | 对错 |
|---|---|---|
| W35 | 链接期 11 个符号 | ✅ |
| W36 | 排除"从核未启动" | ✅ |
| W37 | flash cache 窗口里的函数调用 | ✅ 方向对 |
| W38 | IRAM 数据不可靠 | ❌ |
| W39 | 两个汇编 bug | ✅ |
| **W40** | **linker fragment 漏 `*_mp`** | ✅ **根因** |

**W37 方向对，但只改了自己写的函数，没想到内核函数也需要同样处理。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R96** | **linker fragment 必须同时覆盖 `*_up` 和 `*_mp`**：SMP 换实现文件，规则不跟着换 | 已记录 |
| **R97** | **ldgen 的逐符号规则可能不生效**：`obj:symbol` 改成 `obj` 整对象 | 已记录。改完必须查生成的 `sections.ld` |
| **R98** | **"同函数落点不同"是代码放置 bug 的判据**：两个构建对比 `nm` 立刻能看出 | 已记录 |

### 12.47 W41（2026-10-31）：SMP 是 1 Hz 软复位循环

#### 一、现象精确化

```
banner 正常打印
rst:0xc (SW_CPU_RESET)    <- 每秒一次，12 次/12 秒
Core0 Saved PC:0x2f00ce2c  -> nm: esp_cpu_reset
```

**主动软复位，不是 panic**：`CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT=y` 会让 panic
先打印再重启，而实际**没有任何 panic / assertion 文字**。

#### 二、排除的两个假设

| 假设 | 依据 | 结论 |
|---|---|---|
| `vPortEnterCritical` 多核 abort | `CONFIG_FREERTOS_NUMBER_OF_CORES=1`，那段不编译 | ❌ 排除 |
| 中断看门狗 INT WDT | 关掉 `CONFIG_ESP_INT_WDT` 重建重烧，**仍 1 Hz 循环** | ❌ 排除 |

#### 三、关键线索：1 Hz

**复位是被周期性触发的，不是某条路径一次性失败。**

周期源候选：

| 候选 | 备注 |
|---|---|
| 任务看门狗 TWDT | 本项目零 `esp_task_wdt_add` 调用点（已记录在 boot_tasks.c） |
| RTC WDT | 待确认 |
| **`rt_timer` 周期回调里的 `ESP_ERROR_CHECK` / `esp_restart`** | **1 Hz 完全吻合** |
| systick 中断里出错 | tick 1000 Hz，1 秒是 1000 个 tick |

**下一轮入口**：`rt_tick_increase` / 定时器回调里搜 `ESP_ERROR_CHECK` 和 `esp_restart`。

#### 四、单核完好

已回滚全部实验改动（`RT_USING_SMP`、`RT_CPUS_NR`、`sdkconfig.defaults` 的 WDT 项），
并**删除 `sdkconfig` 重新生成**确保 defaults 生效。

验证：20/20、9/9、30 命令、`s31twai` 4/4、`s31rmt` PASS。

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R99** | **"无 panic 的复位"容易被误判为挂住**：`PANIC_PRINT_REBOOT=y` 下 panic 会打印，没打印就是主动复位 | 已记录。判据：`rst:0xc` + Saved PC 在 `esp_cpu_reset` |
| **R100** | **周期性复位要按周期反推触发源**：1 Hz 只有 tick/定时器能解释 | 已记录 |
| **R101** | **关看门狗做对照实验是必要步骤**：不做就无法排除它 | 已记录 |

### 12.48 W42（2026-10-31）：SMP 两种失败方式被区分，R100 被推翻

#### 一、R100 作废：1 Hz 不是周期触发

bootloader 时间戳 `I (29) ... I (991)` —— **一次启动约 1.05 秒**，
12 次/12 秒正好吻合。**1 Hz 只是启动耗时，没有周期触发源。**

上一轮"要去 tick/定时器找源头"的推断**作废**。这是"从现象推机制"的典型误判。

#### 二、死亡位置精确化

`main.c:253-256` 打印两行 banner，**一行都没打印**；
而 `rt_show_version()` 的 banner **打印了**。

**=> 死在 `rt_show_version()` 之后、`main()` 之前。**

#### 三、两种死法

| 配置 | 复位类型 | Saved PC | 含义 |
|---|---|---|---|
| `RT_CPUS_NR=2` | `rst:0xc (SW_CPU_RESET)` | `esp_cpu_reset` | **主动软复位** |
| `RT_CPUS_NR=1` | `rst:0x8 (HP_SYS_HP_WDT1_RESET)` | `0x2f8036c8`(ROM) | **硬件看门狗（卡住）** |

**1 核卡住、2 核主动复位。** 两种不同失败方式，同一区间。

#### 四、已排除

| 假设 | 依据 |
|---|---|
| 1 Hz 是周期触发 | bootloader 时间戳 |
| 死点在 `main()` 内 | main 两行 banner 都没打印 |
| 死点在 banner 之前 | `rt_show_version()` 有输出 |
| IRAM 放置 | 5 个关键函数 + `smp_prims` 整对象已进 RAM |

#### 五、ticket lock 复核通过

手工推演两次 lock/unlock，`owner`/`next` 半字推进正确，不构成死锁。

#### 六、下一轮

**不改上游文件**（`components.c`）加探针 —— 这一轮试过，位置算错导致编译失败。
改为：在 `board.c` 的初始化序列里加探针 + 在 `main()` 开头加一个，
先确认 main 是否真的没进。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R100** | ~~周期性复位要按周期反推触发源~~ | ⚠️ **作废**。1 Hz 是启动耗时，不是周期 |
| **R102** | **"从现象推机制"容易造出虚假规律**：12 次/12 秒看着像 1 Hz 周期，其实是启动耗时 | 已记录。**先量单次耗时再谈周期** |
| **R103** | **改上游文件加探针要能干净回滚**：`components.c` 是 RT-Thread 的，不是我们的 | 已记录。优先在自有文件里加 |

### 12.49 W43（2026-10-31）：SMP 死点定位到 `rt_hw_context_switch_to`

#### 一、三步定位

| 步骤 | 手段 | 结果 |
|---|---|---|
| 1 | `main()` 开头加 `esp_rom_printf` | **没打印** → main 从未进入 |
| 2 | `rtthread_startup()` 九点探针（18 条） | **17 条打印**，缺 `after sched_start` |
| 3 | `rt_system_scheduler_start()` 七点探针（14 条） | **13 条打印**，缺 `after switch_to` |

**死点收敛到 `rt_hw_context_switch_to((rt_ubase_t)&to_thread->sp, to_thread)` 内部。**

#### 二、三个候选

`rt_hw_context_switch_to` 是汇编：

```asm
    LOAD sp, (a0)                      ; sp = to_thread->sp
    mv   a0, a1
    call rt_cpus_lock_status_restore
    LOAD a0, 2 * REGBYTES(sp)
    csrw mstatus, a0
    j    rt_hw_context_switch_exit
```

| # | 候选 | 判据 |
|---|---|---|
| 1 | `rt_cpus_lock_status_restore` 挂住 | 已在 RAM；断言未启用时不编译 |
| 2 | `to_thread->sp` 是坏指针 | `LOAD sp` 后所有操作在垃圾栈上 |
| 3 | `rt_hw_context_switch_exit` 帧格式不匹配 | 与 `rt_hw_stack_init` 产出的帧对不上 |

#### 三、探针方法的成熟

这一轮两次插探针**一次成功**，靠三条纪律：

1. **按行匹配，不用跨行正则**（W42 失败于 CRLF/LF 混用）
2. **插入后验证计数**（`assert inserted == len(POINTS)`）
3. **插完立刻备份到 `tools/tmp/*.bak`**

两个上游文件（`components.c`、`scheduler_mp.c`）都干净回滚，探针计数归零。

#### 四、`s31twai` 间歇失败的形态确定

回归里出现 2/3 次失败，形态是：

```
125000 bit/s: len 8, want 4
```

**不是没收到帧，是收到的帧 DLC 报 8 而不是 4。**

从"未知间歇"降级为"已知形态"，下一轮可针对查：
- 旧帧残留（DLC 恰好 8）
- 帧头未写完就读

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R104** | **跨行正则匹配 CRLF/LF 混用文件会静默失败**：宏没插入但调用点插了 | 已记录。**按行匹配 + 计数断言** |
| **R105** | **上游文件加探针必须立刻备份**：`components.c`/`scheduler_mp.c` 是 RT-Thread 的 | 已记录 |
| **R106** | **"间歇失败"要抓具体形态**：`len 8, want 4` 比"1/5 失败"有用得多 | 已记录 |

### 12.50 W44（2026-10-31）：死点缩到 `rt_hw_context_switch_exit` 的帧恢复

#### 一、三步实测

| 步骤 | 探针位置 | 输出 | 排除 |
|---|---|---|---|
| 1 | switch 调用前打印参数 | `thread=0x2f032780 sp=0x2f033530`，都在堆区 | **(a) 垃圾指针** |
| 2 | `rt_hw_context_switch_exit` 开头 | `[exit] restoring frame` | **(c) 锁恢复挂住** |
| 3 | `main_thread_entry` 两个位置 | **都没打印** | — |

**死点在 `rt_hw_context_switch_exit` 的 64 条 `LOAD` 帧恢复或 `mret` 本身。**

范围：9 个函数 → 1 个调用 → **60 条汇编指令**。

#### 二、下一轮的判据

`[exit]` 打了而 `[mte]` 没打 ⇒ 二选一：

- `mepc` 被写坏
- `mstatus.MPP` 不是 M 模式，`mret` 跳到了错误特权级

**在 `csrw mepc, a0` 之后立刻打印 `a0` 即可区分。**

注意：不能在 `mret` 前直接 `call`，那时所有寄存器已从帧恢复，会被破坏。

#### 三、探针方法第三次成功

三处上游/自有文件插探针**全部一次编译通过**：

1. 按行匹配 + `assert inserted == len(POINTS)`
2. 插之前 `Copy-Item` 备份
3. 插完立刻构建验证

回滚：三个反向 `Copy-Item` + CMakeLists 宏定义清除，探针计数全部归零。

#### 四、单核已恢复

`s31test` 20/20、`check_image.py` 9/9、30 命令、外设全 PASS。

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R107** | **`mret` 前不能插 `call`**：寄存器已从帧恢复，`call` 会破坏待恢复状态 | 已记录。要在恢复序列**之前**插，或只打 `csrr` 读出的值 |
| **R108** | **三个候选要并行缩小，不要逐个试**：一次实验同时排除 (a)(c) | 已记录。本轮两条打印排除两个候选 |

### 12.51 W45（2026-10-31）：死点在**中断返回路径**，W44 结论被推翻

#### 一、W44 的结论是探针失效导致的误判

`components.c` 的探针用了 `RT_USING_SMP_MAIN_PROBE` 宏，而这宏只加进了
`set_source_files_properties` 的列表——**`components.c` 不在其中**。
宏未定义 ⇒ 展开成空 ⇒ 静默无输出。

**`strings` 查镜像立刻能发现**：`[mte] entered` 字符串在镜像里。

#### 二、三步实证

| 步骤 | 输出 | 说明 |
|---|---|---|
| 帧 dump | `epc=0x400115cc mstatus=0x7880 mpp=3 mpie=1` | `nm`: epc = `main_thread_entry`，**帧完全正确** |
| mret 前 | `mepc=0x400115cc mstatus=0x80007880 mpp=3` | **60 条恢复指令全部成功** |
| trap 入口 | `mcause=0xb8000014 mepc=0x400115cc` | **中断**，line 20 = `SYSTIMER_TARGET0`（OS tick） |

#### 三、结论

**中断在 `main_thread_entry` 执行第一条指令前抢占它——正常。**
`mret` 打开了 MPIE，tick 到期。

**但只有 1 条 trap 打印** ⇒ 中断没有正常返回 ⇒ `main_thread_entry` 一条指令都没跑。

**死点在中断的处理或返回路径，不在 context switch。**

#### 四、下一步嫌疑

SMP 下 `rt_interrupt_leave` 之后走 `rt_scheduler_do_irq_switch()`：

```c
level = rt_hw_local_irq_disable();
SCHEDULER_ENTER_CRITICAL(current_thread);   // 赋 RT_SCHED_CTX(curthr).critical_lock_nest++
if (pcpu->irq_switch_flag == 0) { ...; return; }
```

**`critical_lock_nest` 若未初始化就是垃圾值**，`++` 后可能触发断言或破坏状态。

#### 五、探针纪律（新增硬性规则）

**跨文件/跨组件插探针，宏必须定义在目标编译单元能看到的地方。**

验证方式：
```bash
riscv32-esp-elf-strings elf | grep '\[probe\]'    # 字符串在不在
riscv32-esp-elf-nm elf | grep probe_symbol        # 符号在不在
```

**本轮两次静默失效，一次误判。这条规则能省一整轮。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R109** | **跨组件探针宏不可见**：rtthread 组件的宏，riscv 组件看不到 | 已记录。**用 strings + nm 双重验证** |
| **R110** | **探针静默失效会被误判为"代码没执行"**：这是最危险的误判类型 | 已记录。见 R109 的验证法 |
| **R111** | **W44 结论已作废** | 已记录。作废原因：探针未编译 |

### 12.52 W46（2026-10-31）：第四个 linker 遗漏 —— 内联函数的每份副本

#### 一、带 tag 的探针

改成 `li a0, N; call s31_trap_stage`，一次运行给出完整的六级序列：

```
[stage 1]  handler entry
[stage 2]  frame save 之后
[stage 3]  nest 查询之后
[stage 4]  中断栈切换 + 保留槽位之后
（无 stage 5）
```

**死点收到 `call rt_interrupt_enter`。**

#### 二、objdump 找到真因

修前：

```
2f01617e: auipc ra,0x11111
2f016182: jalr  -1138(ra)   # 40126d0c <rt_soft_atomic_add>   <- IROM!
```

`rt_soft_atomic_add` 是 `rtatomic.h` 的 **inline**，每个翻译单元一份
编译器命名的 out-of-line 副本。`linker.lf` 里 `cpu_mp` 只列了函数名，
**没覆盖这份副本**。

#### 三、修法

```diff
- cpu_mp:rt_cpu_self (noflash)
- cpu_mp:rt_cpus_lock (noflash)
- cpu_mp:rt_cpus_unlock (noflash)
+ cpu_mp (noflash)
```

修后 `nm` 确认 `rt_cpus_lock` 只调 RAM 函数。

#### 四、四次 fragment 遗漏的规律

| # | 遗漏 | 轮次 |
|---|---|---|
| 1 | `scheduler_mp` 的 critical 族 | W40 |
| 2 | `cpu_mp` 的 lock 族 | W40 |
| 3 | `smp_prims` 逐符号不生效 | W40 |
| 4 | **`cpu_mp` 的 `rt_soft_atomic_add` 内联副本** | **W46** |

**规律：逐符号规则漏编译器生成的符号。整对象规则不会。**

#### 五、新死点

修后出现两次完整 stage 1-4 然后 `rst:1`。第一次 tick 的
`rt_interrupt_enter` 返回了（否则不会有第二次 stage 1），但 stage 5 没打印。

**新死点：`rt_interrupt_enter` 返回后。** 下一轮查 `rt_cpus_lock` 的
ticket 状态在 ISR 上下文里是否自旋 —— `_cpus_lock` 是全局锁，
不是 per-CPU 的 `cpus_lock_nest`。

#### 六、状态

单核 20/20、9/9、30 命令、外设全 PASS。保留 `cpu_mp (noflash)` 修复。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R112** | **逐符号 noflash 规则漏内联副本**：`rtatomic.h` 的 helper 每 TU 一份 | 已记录。**用整对象规则** |
| **R113** | **判据是 objdump 而非 nm**：nm 只给符号地址，看不出调用关系 | 已记录。`objdump -d` + 找 `jalr` 到 IROM |
| **R114** | **带 tag 的探针一次运行胜过多次二分**：六级一次定位 | 已记录 |

### 12.53 W47（2026-10-31）：整批对象进 RAM，现象从"fault"变成"卡住"

#### 一、先量体积

```
librtthread.a: text 107969 / data 476 / bss 10151
```

**整 archive 要 105 KB DRAM，这个镜像拿不出来。** 范围限定在 trap 路径可达的对象：

```ld
irq / context_gcc / cpuport / cpu_mp / cpu_up
scheduler_mp / scheduler_up / smp_prims / smp_stack
thread:rt_thread_self
```

#### 二、现象质变

| 阶段 | `rst:` | Saved PC | 含义 |
|---|---|---|---|
| W46 前 | `0xc SW_CPU_RESET` | `rt_enter_critical` | **flash 取指失败** |
| W46 后 | `0x8 HP_WDT` | ROM | 卡住（仍有残留 flash 路径） |
| **W47** | **`0x8 HP_WDT`** | **ROM** | **纯粹卡住** |

**从"取指失败"变成"纯粹卡住"——flash 放置问题真的解决了。**

#### 三、为什么这是实质产出

前五轮"不启动"混着两类原因：flash 取指 + 锁自旋。
**混在一起时，修好一类只让现象变个样子，看起来像没进展。**
现在只剩一类，判据清晰。

#### 四、下一轮的确定位置

objdump 显示 `rt_cpus_lock` 的结构：

```asm
lw    s0, 0(a0)          ; current_thread
beqz  s0, return         ; NULL -> 直接返回
jal   rt_cpus_lock       ; **递归调自己**（读 cpus_lock_nest）
...
jal   rt_enter_critical
jal   rt_hw_spin_lock    ; &_cpus_lock
```

**`current_thread != NULL` 时先递归一次。** 递归深度取决于 `cpus_lock_nest`。
若 `_cpus` 数组没清零，就是垃圾值 → 递归失控 → 栈溢出 → ROM 卡住 → HP_WDT。

**下一轮验证**：打印 `rt_cpu_self()->current_thread` 和 `cpus_lock_nest` 初值。

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R115** | **两类原因混在同一现象里**：修好一类只让现象变形，误以为没进展 | 已记录。**先把一类彻底消除，让现象稳定** |
| **R116** | **`noflash` 前要先量体积**：整 archive 105 KB，装不下 | 已记录 |
| **R117** | **`rt_cpus_lock` 有自递归**：`current_thread != NULL` 时递归一次读 nest | 已记录，下一轮入口 |


### 12.54 W48（2026-10-31）：**SMP 跑起来了** —— 根因是软件原子在 ISR 里取全局锁

#### 一、根因

`rtatomic.h` 三级选择：

```
1. RT_USING_STDC_ATOMIC   C11 标准原子
2. RT_USING_HW_ATOMIC     架构提供
3. rt_soft_atomic_*       关中断实现
```

本项目走第 3 条（`atomic_riscv.c` 从未进构建 + `RT_USING_HW_ATOMIC` 未定义），于是：

```c
rt_soft_atomic_add() -> rt_hw_interrupt_disable()
                     -> rt_cpus_lock()          /* rthw.h:146 的宏映射 */
                     -> rt_hw_spin_lock(&_cpus_lock)
```

**`rt_interrupt_enter()` 第一行就是 `rt_atomic_add(&irq_nest, 1)`**，
所以**每次 trap 都取全局自旋锁**。而 `rtthread_startup()` 取了同一个锁、
只在第一次 context switch 的 `rt_cpus_lock_status_restore` 里释放 ——
第一个 tick 落在那个窗口里就死锁。

#### 二、修法

```cmake
set(common_srcs
    "port/common/context_gcc.S"
    "rtthread_stack_init.c"
    "${RTTHREAD_ROOT}/libcpu/risc-v/legacy_common/atomic_riscv.c")
```

```c
#define RT_USING_HW_ATOMIC
```

```ld
atomic_riscv (noflash)      /* 第五条 noflash 规则 */
```

#### 三、结果

```
tick=1000 Hz, cpus=2, msh on UART0
=== 20 passed, 0 failed ===      (连跑 3 次)
s31twai: 4/4 bit rates round-tripped
```

`list thread` 里 **`tidle0` 和 `tidle1` 都在**。

#### 四、**CPU1 仍未真正运行**

`rt_hw_secondary_cpu_up()` 还是空实现。

| 项 | 状态 |
|---|---|
| SMP 调度器 | ✅ 在跑 |
| ticket lock / per-CPU | ✅ 在跑 |
| 两核 per-CPU 初始化 | ✅ 两个 idle 线程都在 |
| **CPU1 执行代码** | ❌ **没有** |

**现在 = SMP 管线全部验证通过，但只有一个核在干活。**

#### 五、六轮排查的最终结论

真正的根因是最后一层：**不是代码放置，是"原子操作不该取锁"**。
前十轮都在修 flash 放置（都是真问题），但它们**掩盖了最后这一层**。

这一层最难看见的原因：
- **不产生 fault，只死锁** → 没有 MEPC 可查
- 发生在 `rt_interrupt_enter`，即 trap 最开头
- `rt_hw_interrupt_disable` 这个名字**听起来与锁无关**（与 R24 同一个陷阱）

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R118** | **SMP 下软件原子操作取全局锁**：`rt_soft_atomic_*` → `rt_hw_interrupt_disable` → `rt_cpus_lock` | 已记录。**必须开 `RT_USING_HW_ATOMIC`** |
| **R119** | **启用新实现 = 新的 trap 路径对象**：`atomic_riscv.c` 也要 noflash | 已记录。第五条同类规则 |
| **R120** | **"不产生 fault 的死锁"没有 MEPC 可查**：只能靠探针 + 读源码 | 已记录 |
| **R121** | **前几层修好后，最后一层才露出来**：修 flash 放置掩盖了锁问题 | 已记录。与 R115 同类 |


### 12.55 W49（2026-10-31）：CPU1 启动尝试失败，滚回开关后面

#### 一、写了什么

按 IDF `start_other_core()` 的顺序（`cpu_start.c:308-345`）实现
`rt_hw_secondary_cpu_up()`，CPU1 入口是 RT-Thread 版本（不复用 IDF 的
`call_start_cpu1()`，它末尾是 FreeRTOS 的 `SYS_STARTUP_FN()`）：

```c
IRAM_ATTR static void s31_secondary_cpu_entry(void)
{
    la gp, __global_pointer$        /* 汇编 */
    esp_cpu_intr_set_ivt_addr(&_vector_table);
    esp_cpu_intr_set_mtvt_addr(&_mtvt_table);
    rt_hw_spin_lock(&_cpus_lock);
    rt_system_scheduler_start();
}
```

形状与 `bsp/k210/board/cpuport_smp.c:57` 一致。

#### 二、结果：启动成功，shell 死了

```
tick=1000 Hz, cpus=2, msh on UART0
msh >
（命令不执行，无复位、无 panic）
```

**这比不启动更糟**：原本能用的 shell 坏了。

#### 三、决策：开关 + 保留代码

```c
#if defined(RT_USING_SMP) && defined(RT_USING_SMP_SLAVE)   /* smp_slave.c */
#if !defined(RT_USING_SMP_SLAVE)                            /* smp_prims.c 的 stub */
```

**默认不定义 `RT_USING_SMP_SLAVE`**：

- 默认构建 = 已知可用的 SMP 基线（20/20、`tidle0`+`tidle1`）
- 代码留在树里，下一轮打开开关继续调

#### 四、下一轮候选

| # | 假设 | 判据 |
|---|---|---|
| 1 | `enable_clock_and_reset_app_cpu()` 复位影响共享时钟/中断矩阵 | 去掉这一句 |
| 2 | `rt_hw_spin_lock(&_cpus_lock)` 与 CPU0 竞争 | 先不取锁 |
| **3** | **CPU1 的调度器抢走 CPU0 的 shell 线程** | **入口只放 `wfi`** |
| 4 | CPU1 需要额外 per-CPU 中断设置 | 对比 `_mtvt_table` |

**第 3 条最易验证，优先。**

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R122** | **"启动失败但没弄坏别的" vs "启动失败且弄坏了能用的东西"**：后者必须回滚 | 已记录 |
| **R123** | **不必复用 IDF 的从核入口**：`call_start_cpu1()` 收尾进 FreeRTOS 初始化链 | 已记录 |
| **R124** | **实验性代码用配置开关隔离**，默认关，代码保留 | 已记录 |


### 12.56 W50（2026-10-31）：**CPU1 起来了** —— 用它自己的代码证明

#### 一、二分设计

上一轮症状（shell 静默、无 panic、无复位）兼容两种原因：

| 假设 | 含义 |
|---|---|
| (A) 启动序列错 | CPU0 被弄坏，CPU1 从未跑 |
| (B) CPU1 调度器抢走 shell | 硬件启动成功，问题在调度器 |

**判据：CPU1 入口只 `wfi`。**

#### 二、结果：shell 完全正常

```
msh > s31stat
cpu1   : started (0x5a5a5a5a)
=== 20 passed, 0 failed ===
```

**`0x5a5a5a5a` 由 CPU1 的入口写入、CPU0 的 shell 读出。**
它证明的是"CPU1 真的执行了我们写的指令"——`cpus=2` 那种编译期常量证明不了。

**⇒ (B) 成立，(A) 排除。启动序列（时钟/复位/中断矩阵/boot addr/unstall）全部正确。**

#### 三、变体 B 的失败机制

加 `rt_system_scheduler_start()` 后启动循环。源码 `scheduler_mp.c:494`：

```c
void rt_system_scheduler_start(void)
{
    rt_hw_spin_unlock(&_cpus_lock);      /* 无条件，第一行 */
```

**这个 unlock 假定"到达时锁已由 CPU0 释放"。** 而 CPU0 此时仍在
`rtthread_startup()` 里跑 `rt_application_init()` 等代码——两个核能同时进调度器。

#### 四、交付

| 变体 | 状态 |
|---|---|
| **A：`wfi` 停放（默认）** | ✅ `cpu1: started`、20/20、连跑 3 次、外设全 PASS |
| B：调度器（开关 `RT_USING_SMP_SLAVE_SCHED`） | ❌ 启动循环 |

#### 五、必须说清楚

| 项 | 状态 |
|---|---|
| CPU1 硬件启动 | ✅ 已验证 |
| CPU1 共享向量表 | ✅ 已设置 |
| **CPU1 参与调度** | ❌ 没有 |
| **双核并行** | ❌ 没有 |

`tidle1` 是 `ready` 但 `cpu` 列 `N/A`——存在但从未被调度。

#### 六、下一轮

变体 B 的失败已不是"不知道哪里错"，而是"启动时序要编排"：

```c
/* CPU1 入口 */
while (!s31_cpu0_ready) { wfi; }
rt_system_scheduler_start();

/* CPU0：在 rt_system_scheduler_start() 之前置位 s31_cpu0_ready */
```

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R125** | **编译期常量不能证明运行时行为**：`cpus=2` 不等于 CPU1 跑了 | 已记录。**用共享内存标记证明** |
| **R126** | **`rt_system_scheduler_start()` 的第一行无条件 unlock**：要求到达时锁已释放 | 已记录 |
| **R127** | **两核启动时序需要编排**：CPU0 调完 `secondary_cpu_up` 后还有一段初始化 | 已记录 |


### 12.57 W51（2026-10-31）：ticket lock 在 CPU1 上验证通过

#### 一、`Core0 Saved PC` 指向真正的受害者

| 变体 | `Saved PC` | `nm` 解析 |
|---|---|---|
| W49：取锁 + 调 scheduler | — | — |
| W50：取锁 + 停住（**不释放**） | `0x2f016d30` | **`rt_hw_spin_lock`** |

**`Saved PC` 是 `Core0` 的，落在 `rt_hw_spin_lock` 里——卡住的是 CPU0。**

#### 二、我自己的 bug

```c
rt_hw_spin_lock(&_cpus_lock);      /* 取了 */
for (;;) { wfi; }                  /* 没还！ */
```

**`_cpus_lock` 是全局锁。** CPU1 取走不还，CPU0 之后的启动路径再取就自旋到 HP_WDT。
**这不是内核问题，是端口代码的问题。**

#### 三、修法：取 + 还

```c
rt_hw_spin_lock(&_cpus_lock);
s31_cpu1_locked = 0x12345678u;
rt_hw_spin_unlock(&_cpus_lock);
```

#### 四、结果

```
cpu1   : started (0x5a5a5a5a)
cpu1lk : acquired (0x12345678)
=== 20 passed, 0 failed ===
```

连跑 3 次通过，TWAI 4/4、RMT、MCPWM PASS、31 命令。

#### 五、第二条输出比第一条强

| 输出 | 证明 |
|---|---|
| `cpu1: started` | CPU1 执行了 IRAM 代码 |
| **`cpu1lk: acquired`** | **ticket lock 在两核间一致** |

ticket lock 用 `amoadd.w.aqrl`，**要求两核对同一字的读写连贯**。
CPU1 能取票并正确归还，说明：CPU1 的 cache/总线视图与 CPU0 一致、
`amoadd.w` 在 CPU1 上有效、票号没错乱。

#### 六、未做：picker 的内核问题

`scheduler_mp.c:239-262`：

```c
global = _get_global_highest_ready_prio();     // 空表 = -1
local  = _get_local_highest_ready_prio(pcpu);  // CPU1 = 31
if (global < local)                            // -1 < 31 成立
    ... rt_thread_priority_table[global] ...   // [-1] 越界
```

本系统所有线程都 `BIND_CPU`，**全局表恒空 → `[-1]` 必然命中**。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R128** | **`Saved PC` 的寄存器是 `Core0` 的**：卡住的可能是主核 | 已记录。**先看 PC 落在谁的代码里** |
| **R129** | **取全局锁不还是端口 bug**：会把主核饿死 | 已记录 |
| **R130** | **`rt_thread_priority_table[-1]`**：MP picker 在全局表空时越界 | 已记录，待修 |


### 12.58 W52（2026-10-31）：picker 实测推翻越界推断，找到真正障碍

#### 一、纠正 W51 的错误结论

W51 写"全局表空时 `global = -1`，走到 `rt_thread_priority_table[-1]` 越界"。

**错的。** `rt_ubase_t` 是 unsigned（`rttypes.h:75`），`-1` → `0xFFFFFFFF`，
`0xFFFFFFFF < 31` 为 FALSE，走 `else` 用 local 表——完全正确。

**与 R71 同源：让实测回答，不要推断。**

#### 二、probe 第一次插在 `return` 之后

```c
return highest_priority_thread;
PICK_PROBE(...);          /* 不可达，被编译器丢掉 */
```

`strings` 查不到 `[pick]` → **R110 的检查法当场抓获**。挪到 `return` 之前后出现。

#### 三、CPU0 的实测

```
[pick] cpu=0 global=10 local=31 -> main
[pick] cpu=0 global=20 local=31 -> tshell
[pick] cpu=0 global=-1 local=31 -> tidle0      <- -1 时用 local，没越界
```

#### 四、CPU1 的实测（决定性）

```
[pick] cpu=1 global=30 local=31 -> tsystem
[pick] cpu=1 global=10 local=31 -> main
[pick] cpu=0 global=10 local=31 -> main        <- 两个核选同一条
```

**两个核都选中 `main`**（未绑定线程，住在全局表，对每个核可见）。
两个核同时跑同一条线程 → 同栈同寄存器 → 崩坏 → CPU0 卡 `rt_hw_spin_lock`。

#### 五、真正的障碍

RT-Thread 的 MP 模型假设的前提，本端口**三个都不满足**：

| 前提 | 状态 |
|---|---|
| per-CPU tick | ❌ 只有 CPU0 |
| per-CPU 中断路由 | ❌ 全指向 CPU0 |
| 线程绑定到核（全局表空） | ❌ 大部分未绑定 |

#### 六、下一轮的依赖顺序

1. **CPU1 的 tick**（根）——没有 tick，迁过去的线程不会被抢占，等于死掉
2. 中断路由分核
3. 线程绑定

#### 七、交付

| 变体 | 结果 |
|---|---|
| CPU1 只 `wfi` | ✅ |
| CPU1 取锁 + 还锁 | ✅ `cpu1lk: acquired` |
| CPU1 取锁 + 调 scheduler | ❌ 已解释，不再试 |

#### 八、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R131** | **W51 的越界结论作废**：unsigned 比较使 `-1` 走 else | 已记录 |
| **R132** | **探针插在 `return` 之后会被优化掉**：`strings` 查不到 | 已记录，R110 的检查法有效 |
| **R133** | **未绑定线程可被任一核选中**：MP 模型的设计行为 | 已记录。**启用 CPU1 调度前必须先有 per-CPU tick** |


### 12.59 W53（2026-10-31）：per-CPU tick 之前的真正前置条件 —— 中断栈只有一份

#### 一、tick 可行性确认（四条源码事实）

| # | 事实 | 出处 |
|---|---|---|
| 1 | SMP 下 tick **per-CPU**；`rt_timer_check()` 只在 CPU0 | `clock.c:145-175` |
| 2 | S31 有 `SYSTIMER_ALARM_OS_TICK_CORE1 = 1` + `ETS_SYSTIMER_TARGET1_INTR_SOURCE` | `esp_private/systimer.h:30-34` |
| 3 | IDF `vSystimerSetup()` 已按核循环，`configNUM_CORES=1` 才只配 alarm 0 | `port_systick.c:111-134` |
| 4 | `esp_rom_route_intr_matrix(cpu_core, ...)` 支持指定核；端口硬编码 0 | `esp_rom_sys.h:127` |

**⇒ 加 CPU1 tick 是纯端口工作。**

#### 二、关键发现：中断栈只有一份

```ld
/* sections.ld.in:19-29 */
__stack_start__ = .;
. += __STACKSIZE__;
__stack_end__ = .;
```
```asm
/* vectors_rtthread.S:172 */
la    sp, __stack_end__        /* 固定地址 */
```

**两个核同时进中断会用同一个栈。**

#### 三、为什么这比 tick 更基础

tick 是中断。CPU1 一收 tick 就必然与 CPU0 并发 → **两层帧互相覆盖** →
两个核的中断上下文一起崩。**比"CPU1 不参与调度"糟糕得多。**

**⇒ 正确顺序：per-CPU 中断栈 → per-CPU tick → 中断路由分核 → CPU1 调度。**
W52 把 tick 排第一是**错的**。

#### 四、写了 per-CPU 栈又删掉

原因：
1. 没有被任何代码调用 = 死代码
2. 返回 NULL 给 CPU1，而正确做法是**真的分配第二份**
3. 分配要同时改 `sections.ld.in` + `vectors_rtthread.S`，分开做会得半成品

**留未接线的"看似有用"代码比删掉更危险。**

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R134** | **中断栈是单份的**：`la sp, __stack_end__` 固定地址 | 已记录。**CPU1 收任何中断前必须先 per-CPU 化** |
| **R135** | **前置条件顺序会排错**：tick 看起来是根，实际栈更基础 | 已记录。先问"这个动作会不会引入并发" |
| **R136** | **未接线的代码必须删**：下一轮可能信它 | 已记录 |


### 12.60 W54（2026-10-31）：per-CPU 中断栈落地

#### 一、方案：静态数组，不改 IDF linker

| 方案 | 代价 |
|---|---|
| 改 `sections.ld.in` | 动 IDF 文件，回滚是 linker 变更 |
| **CPU1 用 `.bss` 静态数组** | **只动本组件源码** |

#### 二、实现

```c
static char s31_cpu1_istack[CONFIG_IDF_RTTHREAD_INTERRUPT_STACK_SIZE]
    __attribute__((aligned(16)));

void *rt_irq_stack_top[RT_CPUS_NR] = {
    &__stack_end__,                              /* CPU0 不变 */
    s31_cpu1_istack + sizeof(s31_cpu1_istack),   /* CPU1 独立 */
};
```

```asm
    la    t2, rt_irq_stack_top
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t2
    lw    sp, 0(t0)
```

**不用 `void *const`**：const 指针数组会进 `.flash.rodata`，而这段在 cache 关闭时跑。

#### 三、ELF 级验证

```
2f019338 00000008 D rt_irq_stack_top        <- .data
2f01fe10 0000a000 b s31_cpu1_istack         <- .bss, 40960 B
```

表内容 `objdump -s`：`50ba032f 109e022f` = `[0x2f03ba50, 0x2f029e10]`。

| 项 | 值 | 核对 |
|---|---|---|
| `[0]` | `0x2f03ba50` | = `__stack_end__` ✅ 与改前相同 |
| `[1]` | `0x2f029e10` | = `0x2f01fe10 + 0xa000` ✅ |

#### 四、链接期坑（R44 再验证）

```
vectors_rtthread.S:186: undefined reference to `rt_irq_stack_top'
```

与 `rt_irq_nest_slot` 相同：汇编引用、无 C 引用 → archive member 不被拉入。
修法：`target_link_libraries(... "-u rt_irq_stack_top")`。

#### 五、依赖链进度

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | per-CPU 中断栈 | ✅ **完成** |
| 2 | CPU1 的 tick | 下一步 |
| 3 | 中断路由分核 | |
| 4 | CPU1 进调度器 | |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R137** | **const 指针数组会进 .rodata**：cache 关闭时读不得 | 已记录。用非 const 保持 .data |
| **R138** | **汇编引用的符号需要 `-u`**：R44 再次验证 | 已记录 |


### 12.61 W55（2026-10-31）：tick 会解引用 current_thread —— 顺序再前推一步

#### 一、关键约束

`rt_sched_tick_increase()`（`scheduler_comm.c:294`）：

```c
thread = rt_thread_self();
if (RT_SCHED_PRIV(thread).remaining_tick > tick)   /* 无 NULL 检查 */
```

CPU1 调度器未启动时 `current_thread == NULL` → **解引用空指针**。
**⇒ tick 必须在 CPU1 调度器之后。**

#### 二、修正后的顺序（第三次修正）

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | per-CPU 中断栈 | ✅ W54 |
| **2** | **线程默认绑定到 CPU0**（全局表为空） | 下一轮 |
| 3 | CPU1 调度器（picker 选 `tidle1`） | |
| 4 | CPU1 的 tick | |
| 5 | 中断路由分核 | |

#### 三、实物产出：`s31smp` 命令

```
cpus   : 2, running on cpu 0

cpu cur_thread idle_thread irq_nest tick  istack
--- ---------- ----------- -------- ----- --------
  0   2f03e870    2f01fa20        0  2966 0x2f03ba50
  1          0    2f01fac4        0     0 0x2f029e10

istack : distinct per core
```

**一屏证明四件事，不需要探针：**

| 列 | 值 | 含义 |
|---|---|---|
| `cur_thread` cpu1 = 0 | NULL | CPU1 确实没在跑线程 |
| `idle_thread` cpu1 ≠ 0 | 存在 | 两核 idle 线程都在 |
| `tick` cpu1 = 0 | 0 | CPU1 一次 tick 都没收到 |
| `istack` | distinct | per-CPU 栈生效 |

**`tick` 列是下一步的直接判据**：做完 CPU1 tick 后重跑，这个数应该增长。

#### 四、gc-sections 第五次

`MSH_CMD_EXPORT` 只写 section，无代码引用 → 整文件被 gc。
用 `s31_smp_cmd_init()` 被 `board.c` 调用做锚点（比 `-u` 好在调试点即文档）。

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R139** | **`rt_sched_tick_increase` 解引用 `current_thread` 无 NULL 检查** | 已记录。tick 必须在调度器之后 |
| **R140** | **依赖链每轮都要往前提一层**：已三次修正 | 已记录。**动手前先问"这个函数解引用什么"** |
| **R141** | **探针结论会随探针消失**：需要把可测状态变成永久命令 | 已记录。`s31smp` 是第一个 |


### 12.62 W56（2026-10-31）：**CPU1 开始调度** —— 根因是 unlock 次数

#### 一、试错的顺序

1. 先按 W55 的推论把线程绑定到 CPU0 → **仍失败**（CPU0 卡 `rt_hw_spin_lock`）
2. 回滚绑定 → **仍失败**，说明绑定不是根因
3. 去数锁的取放次数 → 找到真因

#### 二、真因：一次取锁、两次放锁

```c
rt_hw_spin_lock(&_cpus_lock);      /* 取 1 次 */
s31_cpu1_locked = ...;
rt_hw_spin_unlock(&_cpus_lock);    /* 放第 1 次 */
rt_system_scheduler_start();       /* 里面又放第 2 次（scheduler_mp.c:493 无条件） */
```

**ticket lock 的 owner 超过 ticket → CPU0 拿到一个永不服务的票 → 无限自旋。**

#### 三、修法

```c
rt_hw_spin_lock(&_cpus_lock);
s31_cpu1_locked = 0x12345678u;
rt_system_scheduler_start();       /* 它内部放 */
```

**与 `bsp/k210/board/cpuport_smp.c` 一致。**

#### 四、结果

```
list thread:
thread       cpu bind pri  status
tshell         0   0   20  running
tidle1         1   1   31  running      <- CPU1 在跑自己的 idle
tidle0       N/A   0   31  ready

s31smp:
  1   2f01fac4    2f01fac4     ... <- cur_thread == idle_thread
```

#### 五、反直觉结论：绑定不是必需的

回滚 `scheduler_comm.c` 后仍成功，**内核处于 upstream 状态**。

**锁的等待本身就是同步点：**
1. CPU1 在 `_cpus_lock` 上自旋
2. CPU0 持有到它自己的 `rt_system_scheduler_start()`
3. 在那之前 CPU0 已把 `main` 从就绪队列摘走
4. CPU1 拿到锁时全局表已空 → 走 local 分支 → `tidle1`

**这是设计，不是巧合。** W52 看到两核抢 `main` 是因为 unlock 错乱，
CPU1 从未等到那个时刻。

#### 六、被同一个 bug 骗了三次

| 轮次 | 判断 | 实际 |
|---|---|---|
| W49 | CPU1 抢走 shell | unlock 次数错乱 |
| W52 | 需要绑定线程 | unlock 次数错乱 |
| W56 | 绑定不是解，去查锁 | unlock 次数错乱 ✅ |

**"两核选中 main" 是真实观测，但它是后果不是原因。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R142** | **取放锁次数必须配平**：`rt_system_scheduler_start` 内部会放锁 | 已记录 |
| **R143** | **观测到的"原因"可能是另一个 bug 的后果**：改它无效时回去查更深一层 | 已记录 |
| **R144** | **锁的等待可以是设计好的同步点**：不要把它当竞争去掉 | 已记录 |

### 12.63 W57（收尾）：P3 达成，状态归档

#### 一、M7 验收证据

```
list thread:
thread       cpu bind pri  status
tshell         0   0   20  running
tidle1         1   1   31  running      <- CPU1 跑自己的 idle
tidle0       N/A   0   31  ready

s31smp:
  1   2f01fac4    2f01fac4   ...        <- cur_thread == idle_thread
```

#### 二、里程碑状态（当前快照）

| 里程碑 | 状态 |
|---|---|
| M5.5 RT-Thread 原生命令 | ✅ |
| M5.6 BLE 控制器 + NimBLE host | ✅ |
| M5.7 I2C0 总线设备 | ✅ |
| **M6 连接真实 AP** | ✅ **PASS**（W58） |
| M6.5 MicroPython | ⬜ 依赖 M6 |
| **M7 双核 SMP（P3）** | ⚠️ **部分** —— CPU1 启动 + 共享 ticket lock 已验证，CPU1 进调度**有竞态**（W59） |
| M8 外设扩展 | 🔄 7 类完成，4 类待硬件 |

#### 三、P3 的边界必须说清

| 达成 | 未达成 |
|---|---|
| CPU1 启动、执行 IRAM 代码 | CPU1 没有 tick |
| CPU1 用共享 ticket lock | 没有业务线程绑到 CPU1 |
| per-CPU 中断栈 | 中断路由仍是单核 |
| **CPU1 参与调度（跑 idle）** | |

**"双核跑起来了" ≠ "双核都在干活"。**

#### 四、仓库状态

```
rt-thread   : git status 干净（0 处改动）
esp-idf-rtt : 12 个文件，全在 components/ 下
```

**上游 checkout 未打补丁**——升级 RT-Thread 版本不需重放补丁。

#### 五、下一步

1. CPU1 的 tick（前置条件已满足）
2. 线程绑定 CPU1 + 并发正确性验证
3. M6 需要 AP 凭据（环境事项）

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R145** | **"阶段达成"要写明边界**：P3 达成的是"参与调度"不是"业务线程在跑" | 已记录 |
| **R146** | **上游零改动是资产**：升级不需重放补丁 | 已记录。任何内核改动都要先问"能否在端口层解决" |

### 12.64 W58（2026-10-31）：**M6 达成** —— 连上真实 AP

#### 一、5 GHz 是硬件问题

用户的热点是 `刻于晴空之上_5G`，5 GHz / ch 157。

**`SOC_WIFI_SUPPORT_5G` 在整个 IDF 里只有 `esp32c5` 定义**，
`esp32s31` 没有；官方板子文档两次写"2.4GHz Wi-Fi 6"。

**⇒ S31 在射频层面收不到 5 GHz 信标。** 不是配置问题。

#### 二、`s31wifiscan` 把这件事变成可测的

```
  # ch  rssi  bssid              ssid
  1  1   -50  d4:ee:07:40:1f:26  刻于晴空之上_2.4G    <- 同一路由器的 2.4G
 14  1   -79  c0:f6:c2:d9:e1:51  CQNU-Eduroam

s31wifiscan: 14 AP(s), all on channels 1..14 = the 2.4 GHz band
```

**路由器同时广播 `_2.4G` 和 `_5G`。** 2.4G 那个 RSSI **-50（最强）**。
**不需要换热点。**

#### 三、第二个坑：设密码 ≠ 发起连接

加了 `[ssid password]` 参数后测：等 15 秒，**连 disconnect 事件都没有**。

| 现象 | 含义 |
|---|---|
| 密码错 | 有 `STA_DISCONNECTED` + reason |
| AP 不在范围 | 同样有 disconnect |
| **什么都没有** | **根本没发起连接** |

`esp_wifi_set_config()` 只存凭据，`esp_wifi_start()` 只启动 station。
**关联必须显式 `esp_wifi_connect()`。**

#### 四、结果

```
s31wifi: PASS, associated in 350 ms
bssid d4:ee:07:40:1f:26  ch 1  rssi -49  auth 3
```

驱动日志：
```
wifi:state: init -> auth (0xb0) -> assoc (0x0) -> run (0x10)
wifi:connected with 刻于晴空之上_2.4G, aid = 1, channel 1, BW20
wifi:security: WPA2-PSK, phy: bgn, rssi: -49
```

**第一次有真实网络流量经过 FreeRTOS shim。**

#### 五、诊断原则

**"超时且没有错误事件" ≠ "对端拒绝"，而是"请求根本没发出"。**
任何"等待事件超时"的代码都要问：事件不来是因为对方拒绝，还是因为我没请求？

#### 六、外设覆盖（基于 soc_caps）

| 未移植 | 需要硬件 |
|---|---|
| 温度传感器、JPEG 编解码、PPA、ETM | ❌ 不需要，现在就能做 |
| I2S（回环可测） | ⚠️ 有模块更好 |
| SD/MMC、USB、LCD、Camera、Touch | ✅ 需要 |

**S31 没有**：DAC（`SOC_DAC_SUPPORTED` 未定义）、MIPI CSI/DSI、ISP。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R147** | **5 GHz 是硬件能力，不是配置**：先查 soc_caps 再调参 | 已记录 |
| **R148** | **设配置不等于发起动作**：`esp_wifi_connect()` 必须显式调用 | 已记录 |
| **R149** | **"超时无错误事件"是"没请求"的签名** | 已记录 |

### 12.65 W59（2026-10-31）：片内外设尝试撞上 SMP 竞态 —— W56 结论作废

#### 一、温度传感器的三道坎

| 阶段 | 现象 | 原因 |
|---|---|---|
| 编译 | `temperature_sensor.h` 找不到 | `PRIV_REQUIRES` 不把 include 加到编译行，要显式 `PRIV_INCLUDE_DIRS` |
| 链接 | `undefined reference to temperature_sensor_install` | `libesp_driver_tsens.a` 在 `librtthread.a` **之前** |
| 修复 | 加 `-u` 强制解析 | **引发回归** |

#### 二、回归形态

```
- RT -     Thread Operating System
 / | \     5.4.0 build ... 
（停住：没有 "RT-Thread 5.4.0 on ESP32-S31"，没有 msh >）
```

更早的版本会打印 `msh >` 但不执行命令。**两种形态都指向启动链被阻塞。**

#### 三、基线判定：烧回旧固件

用 W48 保存的 `smp_working.bin`：

```
RESTORED bin -> passed: True
```

**同板同线同工具，旧固件能跑 ⇒ 软件回归，不是硬件/串口问题。**
这一步省掉整轮环境排查。

#### 四、二分结果

| 去掉 | 结果 |
|---|---|
| tsens.c + CMake | ❌ 仍不执行 |
| 再去掉 s31wifiscan.c | ✅ version 行 + msh > 回来，20/20 |

`s31wifiscan.c` 未被调用，但把 `esp_wifi_scan_*` 引用带进镜像，
拉进 `libesp_wifi` 更多 init 构造。

#### 五、但更重要的发现

去掉后 `s31test` 20/20，但 `s31smp` 显示 **`cpu1 cur_thread = 0`** ——
而 W56 那里是 `2f01fac4`。

**⇒ 那个"CPU1 参与调度"的配置在这轮所有重建里都没生效。**

| 配置 | W56 | 这轮每次重建 |
|---|---|---|
| CPU1 调 `rt_system_scheduler_start()` | ✅ 3 次 | ❌ 卡住 |
| CPU1 park wfi | ✅ | ✅ |

**同一份代码，行为随机构而变 ⇒ 竞态。**

可疑窗口（`scheduler_mp.c:493`）：先 `rt_hw_spin_unlock(&_cpus_lock)`，
之后才 `_fast_spin_lock(&_mp_scheduler_lock)` 并读就绪表。
**放锁与读表之间有窗口**，另一核在 `rt_scheduler_do_irq_switch()` 里编辑同一批表。

#### 六、按证据回滚

```c
rt_hw_spin_lock(&_cpus_lock);
s31_cpu1_locked = 0x12345678u;
rt_hw_spin_unlock(&_cpus_lock);   /* 只验证锁 */
for (;;) { wfi; }                 /* 不进调度器 */
```

回滚后：**20/20 连跑 3 次**、TWAI 4/4、RMT PASS、32 命令。

**W56 的 M7 达成降级为"部分"。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R150** | **`-u` 拉进的驱动会带 init 构造**：撞 RT-Thread 启动链 | 已记录。拉驱动前先查 `ESP_SYSTEM_INIT_FN` |
| **R151** | **必须保存已知可用的 bin**：W48 那份救了这一轮 | 已记录。每达成一个里程碑存一份 |
| **R152** | **"时而通过"不是通过**：同一配置多次结果不同 = 竞态 | 已记录。**连跑 3 次是最低要求** |
| **R153** | **W56 结论作废**：CPU1 调 scheduler 在本端口不稳定 | 已记录 |

### 12.66 W60（2026-11-01）：RT-Thread 一致性审计

新目标下的第一轮：**检查移植里不符合 RT-Thread 约定的地方。**

#### 一、审计结果

| 项 | 实测 | RT 约定 | 处理 |
|---|---|---|---|
| init 返回 `int` | 10 处 | `rt_err_t` | 已改 gpio.c，其余 9 处见下 |
| `INIT_*_EXPORT` | **0 次** | 每驱动自己导出 | **已加 10 个** |
| board.c 手工调用 | **17 个** | 应接近空 | **降到 7 个** |
| `rt_kprintf`/`LOG_x` | 196 / 0 | `LOG_D/LOG_E` | 未改（风格，不影响功能） |

#### 二、关键发现：ESP-IDF 下 export 不生效

加 `INIT_BOARD_EXPORT(rt_hw_pin_init)` 并删掉手工调用后：

```
nm elf | grep rt_hw_pin_init   ->  NOT in image
list device                    ->  pin 消失
```

**语法对、段被 KEEP、walker 在跑，但 archive member 没被拉进来。**

上游 BSP 所有源文件编进一个镜像（段引用就够）；
ESP-IDF 每个组件是 archive，`--gc-sections` 丢掉无人引用的 member。

#### 三、正确组合

```c
/* 驱动侧：标准 RT-Thread 写法 */
rt_err_t rt_hw_pin_init(void) { return rt_device_pin_register(...); }
INIT_BOARD_EXPORT(rt_hw_pin_init);
```
```cmake
# 构建侧：显式处理 archive 模型
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u rt_hw_pin_init")
```

**两者缺一不可，而且各自表达一件不同的事。**

#### 四、改了 10 个驱动

gpio / pwm / gptimer / i2c / spi / adc / pcnt / mcpwm / rmt / twai
全部 `INIT_BOARD_EXPORT(rt_hw_*_init)`。

#### 五、验证

```
list device  ->  uart0 / adc1 / adc2 / spi0 / spi1 / pwm0 / i2c0 / pin
```

**与改造前逐行相同**，`s31test` 20/20、TWAI 4/4、RMT/MCPWM/PCNT PASS。

#### 六、剩余 7 个手工调用分两类

| 类 | 项 | 处理 |
|---|---|---|
| **真需要早期** | `rt_hw_int_wdt_init`、`rt_hw_irq_nest_init` | 保留手工调用，注释说明"必须早于 board 段" |
| **链接锚点** | `s31_twai_cmd_init` 等 5 个 | **应改名 `*_link_anchor()`** |

**第二类是 R44 的产物**：空函数只为把 section-注册的对象拉进链接，
叫 `_init` 会被误读成有初始化逻辑。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R154** | **ESP-IDF 下 `INIT_*_EXPORT` 需要配 `-u`**：archive 模型与上游单镜像不同 | 已记录 |
| **R155** | **链接锚点不能叫 `_init`**：会被误读成初始化函数 | 已记录，待改名 |
| **R156** | **一致性要能量化**：用 grep 统计而不是"看着像" | 已记录 |

#### 八、追加：链接锚点已改名（W60.9）

| 原名 | 新名 | 返回类型 |
|---|---|---|
| `s31_twai_cmd_init` | `s31_twai_cmd_link_anchor` | int → **void** |
| `s31_selftest_init` | `s31_selftest_link_anchor` | int → **void** |
| `s31_board_extra_init` | `s31_board_extra_link_anchor` | int → **void** |
| `s31_smp_cmd_init` | `s31_smp_cmd_link_anchor` | int → **void** |
| `s31_intr_debug_init` | `s31_intr_debug_link_anchor` | int → **void** |

**改名立刻暴露了 5 处 `return 0;`** —— 编译期报错：

```
error: 'return' with a value, in function returning void
```

**这就是改名的价值**：旧名字让"返回 0"看起来合理，新名字让它在编译期变成错误。

#### 九、最终一致性数据

| 指标 | 改前 | 改后 |
|---|---|---|
| `INIT_*_EXPORT` | 0 | **10** |
| board.c 手工调用 | 17 | **7** |
| 真需要的早期 init | — | 2 |
| 链接锚点（已改名） | — | 5 |
| `list device` | 8 设备 | **8（相同）** |
| `s31test` | 20/20 | **20/20** |
| TWAI/RMT/LED/PSRAM | PASS | **PASS** |

#### 十、下一步候选（按价值排序）

| # | 项 | 价值 |
|---|---|---|
| 1 | 其余 9 个 init 的返回类型改成 `rt_err_t` | 一致性，低风险 |
| 2 | `rt_kprintf` → `LOG_D/LOG_E`（196 处） | 模块化，纯风格 |
| 3 | 驱动文件按 RT 惯例放在 `drivers/` 而非 `port/` | 目录结构 |
| 4 | 给 `s31smp` 等命令加 `MSH_CMD_EXPORT` 之外的 `rt_` 前缀别名 | 可移植性 |

### 12.67 W61（2026-11-01）：一致性审计第二轮

#### 一、三项改动

| # | 项 | 改前 | 改后 |
|---|---|---|---|
| 1 | `init` 返回类型 | 3 个仍返回 `int` | **全部 `rt_err_t`** |
| 2 | `uart0` 注册方式 | main.c 手工调用 | **`INIT_BOARD_EXPORT`** |
| 3 | **`rt_hw_console_output` 位置** | **应用层（main.c）** | **驱动层（serial.c）** |

#### 二、第 3 项是实质发现

统计上游 BSP：

```
driver/board layer = 44    applications/ = 1
```

**44:1。** 应用应该能跨板移植，console 输出属于板级能力。

**44 个 BSP 的做法是对的，本端口原来那 1 个是错的。**

#### 三、为什么用 ROM 输出

```c
void rt_hw_console_output(const char *str)
{
    while (*str) { if (*str == '\n') esp_rom_output_putc('\r'); esp_rom_output_putc(*str++); }
}
```

`esp_rom_output_putc()` 无驱动状态依赖 → 串口驱动出错时 console 仍可用，
**panic 路径仍能打印**。

本端口**从不调用 `rt_console_set_device()`**，所以 `_kputs()` 永远走这条分支。
**这是有意的设计偏离**，已写入注释。

#### 四、两轮合计

| 指标 | W60 前 | W60 后 | W61 后 |
|---|---|---|---|
| init 返回 `int` | 10 | 9 | **0** |
| `INIT_*_EXPORT` | 0 | 10 | **11** |
| board.c 手工调用 | 17 | 7 | **5** |
| console 在应用层 | 是 | 是 | **否** |
| `list device` | 8 | 8 | **8** |

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R157** | **`rt_hw_console_output` 属于驱动层**：放应用层会阻碍跨板复用 | 已记录 |
| **R158** | **不调 `rt_console_set_device()` 是有意的**：保住 panic 输出 | 已记录，需在文档中说明 |
| **R159** | **搬代码必然遇到头文件差异**：`esp_rom_serial_output.h` 是例子 | 已记录 |

### 12.68 W62（2026-11-01）：把审计固化成 `tools/check_rtt_style.py`

#### 一、10 项检查

| # | 检查 | 判据 |
|---|---|---|
| 1 | init 返回 `rt_err_t` | 0 个 `int` |
| 2 | `INIT_*_EXPORT` 存在 | ≥10 |
| 3 | 导出到达 ELF | 全部在 `nm` |
| 4 | board.c 手工调用 | ≤2 |
| 5 | 锚点不叫 `_init` | 无 |
| 6 | console 在驱动层 | app 无 / port 有 |
| 7 | 框架注册 | 5 驱动全用 |
| 8 | `rt_uint32_t` 声明 | 0（有豁免） |
| 9 | msh 命令有锚点 | 覆盖 |
| 10 | shell 绑定设备 | `finsh_set_device` |

**`--strict` 结果：10/10。**

#### 二、脚本第一版的两个误报（都是脚本自己的错）

**误报 1：把 `-u` 当判据**

`board.c` 自己的三个导出没有 `-u`，被判失败。但它们和 `rt_hw_board_init`
**在同一个目标文件里**，被引用是必然的。

```
nm 验证：三个都在镜像里
```

**判据改为"符号在不在 ELF 里"。**
`-u` 是手段，"到达镜像"是目的 —— **检查手段会把正确情况报成失败。**

**误报 2：硬套 `rt_uint32_t`**

改完 13 处后编译失败 3 处：

```
gpio.c:192: incompatible pointer type    (gpio_ll_get_intr_status 的输出参数)
i2c.c:286:  incompatible pointer type    (i2c_ll_get_rxfifo_cnt 的输出参数)
```

`rt_uint32_t` 与 `uint32_t` **是不同 typedef**，指针不兼容。

**豁免两类**：
- 地址交给 IDF LL 函数的输出参数
- `freertos_api.c` 的参数（FreeRTOS ABI，改了就不是 drop-in）

#### 三、两条一般性教训

| 教训 | 说明 |
|---|---|
| **检查手段 ≠ 检查目的** | 检查实现细节会误报，检查结果属性才对 |
| **一致性规则有正确的例外** | 硬套规则导致编译失败 = 规则没考虑边界，不是代码不统一 |

#### 四、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R160** | **一致性脚本要检查结果属性，不是实现细节** | 已记录 |
| **R161** | **`rt_uint32_t` 与 `uint32_t` 指针不兼容**：LL 输出参数必须用后者 | 已记录 |
| **R162** | **FreeRTOS shim 的参数类型属于 ABI**，不能 RT 化 | 已记录 |

### 12.69 W63（2026-11-01）：op 契约审计 + rtconfig.h 注释一致性

#### 一、op 契约逐个核对：无功能缺陷

| ops | 契约 | 结论 |
|---|---|---|
| `master_xfer` 返回 msg 数 | 一致 | ✅ |
| `spi xfer` 返回字节数 | 返回 `total` | ✅ |
| `adc convert` `rt_err_t` + 输出 | 一致 | ✅ |
| `adc get_vref` **未知可返回负值** | 返回 `-1` + 注释 | ✅ |
| `pin_irq_enable` | 一致 | ✅ |
| `uart control` 未识别 cmd 返回 `RT_EOK` | 与上游 8 个 `drv_uart.c` 一致 | ✅ |
| `rt_pin_ops` 缺的 5 个成员 | 在 `RT_USING_DM`/`RT_USING_PINCTRL` 里，两者未定义 | ✅ |

**`s31_adc_vref()` 返回 `-1` 是正确处理**：无校准路径，
编一个"典型值"会显得权威但实际偏几个百分点。**说"不知道"更好。**

#### 二、真问题：rtconfig.h 注释和值矛盾

```c
#define RT_CPUS_NR                      2
/* P1/P2 run the kernel on the PRO core only. */     <- 说单核
```
```c
/* No networking, filesystem, Finsh, devices or console at this stage */  <- 全是错的
```

#### 三、为什么这是缺陷

| 后果 | 说明 |
|---|---|
| **无法区分"决定"与"遗漏"** | 读注释会以为 console 是有意关的 |
| **会误导人** | 上一轮我就在翻这个文件 |
| **值对注释错** | 更糟：代码能跑，没人去改注释 |

**过时注释比没有注释更危险：没注释时人看代码，有过时注释时人信它。**

#### 四、修法：描述现状 + 说明原因

不再写"某个历史阶段是什么"，而是：
- 现在是什么（`RT_CPUS_NR == 2`，CPU1 跑 idle）
- 为什么不是别的（调度器启动的竞态，见 W56/W59）
- 哪些是**有意不定义**及原因（`RT_USING_SOCKET` 等）

#### 五、新增第 11 项检查

**"注释与值一致"变成可自动检查。** 脚本 11/11。

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R163** | **过时注释比没有注释危险**：人会信它 | 已记录，已纳入自动检查 |
| **R164** | **`get_vref` 未知时返回负值是正确的** | 已记录，不要"修"成编造的值 |
| **R165** | **"审计没找到 bug"也是结论**：说明该层实现扎实 | 已记录 |

### 12.70 W64（2026-11-01）：`rt_hw_us_delay` 只有空实现 —— 找到的第一个功能性缺失

#### 一、审计方法：比对 ELF 符号

`rthw.h` 声明 39 个移植层函数，逐个在 ELF 找。缺失 19 个，但要分四类：

| 类别 | 例子 | 判断 |
|---|---|---|
| 宏 | `rt_hw_interrupt_disable` → `rt_cpus_lock`（SMP） | ✅ |
| 宏（无 cache） | `rt_hw_cpu_icache_*` → 空宏 | ✅ |
| weak 默认 | `rt_hw_cpu_reset`、backtrace | ✅ |
| **真缺口** | **`rt_hw_us_delay`** | ❌ |

#### 二、为什么是真缺陷

`kservice.c` 的 weak 版本：

```c
rt_weak void rt_hw_us_delay(rt_uint32_t us)
{
    (void) us;
    LOG_W("rt_hw_us_delay() doesn't support for this board.");
}
```

而 `components/libc/posix/delay/delay.c` 在 `usleep`/`nanosleep` 里调它。

**⇒ 任何 POSIX `usleep()` 都是空操作。**

**危害/可见性比值最差的一类**：没错误、没警告，只是偶尔不正常的设备。

#### 三、实现要点

| 决定 | 理由 |
|---|---|
| 用 SYSTIMER 不用 TIMG | 调度器启动前可用、非 tick（关中断可用）、单位换算已集中 |
| 上限 10 ms + 只警告一次 | 忙等毫秒是调用点的错，不该掩盖 |

**第二个时间源就是第二次搞错 16 MHz 换算的机会。**

#### 四、关键坑：`-u` 打不过同 archive 的 weak

加 `-u rt_hw_us_delay` 后 `nm` 显示：

```
40012836 W rt_hw_us_delay      <- 还是 weak
```

**`-u` 只是标记"未定义"，而 weak 定义已在同 archive 内被链接，满足了引用。**

**修法：把强定义放进必然被链接的目标文件** —— 选 `gptimer.c`
（它被 `-u rt_hw_timer_init` 拉进，且 `s31_systimer_us()` 就在里面）。

修后：`400163e4 T rt_hw_us_delay`。

#### 五、新增第 12 项检查

**查 ELF 符号类型（`T` vs `W`），不查源码。**

因为源码看不出来：`delay.c` 写了强定义、编译通过、链接成功、
**运行时用的却是 weak 空实现**。

脚本 12/12。

#### 六、剩余缺口不是缺陷

| 符号 | 原因 |
|---|---|
| `rt_hw_exception_install` | 无调用者 |
| `rt_hw_show_memory` | 无调用者 |
| `rt_hw_stack_guard_init` | `RT_USING_HW_STACK_GUARD` 未定义 |

**契约里有、本配置用不到。** 值得补的是有真实调用者的那种。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R166** | **`-u` 打不过同 archive 的 weak 定义** | 已记录。强定义要放进已链接的对象 |
| **R167** | **"符号存在"≠"符号有效"**：必须看类型 `T`/`W` | 已记录 |
| **R168** | **弱定义的空实现是最难发现的缺陷类型**：无错误、无警告 | 已记录 |

### 12.71 W65（2026-11-01）：rthw.h 契约的运行时测试，自检 20 → 26 项

#### 一、新增 `s31test rhwh`（5 个子检查）

| 子检查 | 验什么 | 失败模式 |
|---|---|---|
| `rhwh.ud` | `rt_hw_us_delay(1000)` 实际 900~1600 us | weak 空实现（~0）/ 16 倍单位错（~16000） |
| `rhwh.iv` | 扫到空闲 INUM | — |
| `rhwh.ui` | `uninstall` 参数不匹配时**不**删 | 无条件删除会误删后来者 |
| `rhwh.ri` | `install` 返回**前一个** handler | 忽略返回值会丢 handler |
| `rhwh.mk` | `mask`/`umask` 与 `is_masked` 一致 | 写错寄存器 |

**`rhwh.ud` 最有价值**：两种真实发生过的错误都会让它失败。

#### 二、第一次跑抓到我自己的错误

```
[FAIL] rhwh.iv  INUM 7 already had a handler - skipped
```

**单独跑通过，全跑失败** —— `st_nest` 把 line 6 的 handler 留着，
而我的测试固定用 line 7，且**靠"安装一下看返回值"判断是否空闲**。

**探测方式本身错了：为了读状态而改变状态。**

改用只读查询：

```c
if (intr_handler_get(probe) == RT_NULL && esprv_int_get_priority(probe) == 0)
```

**扫不到空闲 line 时报 `PASS + skipped`**，因为线路被占用不是端口缺陷。

#### 三、连带：`check_image.py` 第 5 项失败

```
[FAIL] init table order   19 entries by address
```

**W60/W61 改造的预期后果** —— 11 个驱动变 `INIT_BOARD_EXPORT`，表多 11 项。

**检查是对的**（文件注释早写了"加 export 就会改这个表"）。
但不能悄悄放宽成"包含"，做法是**逐项写出新序列**，并注明
**BOARD 段相对顺序不是契约**（链接器决定），**只在两个 marker 之间才是契约**。

#### 四、审计方式的升级

| 之前 | 现在 |
|---|---|
| 人读 `rthw.h` + 查 `nm` | `s31test rhwh` 每次跑都验 |
| 契约违反几周后表现为"某设备偶发不对" | 当场 `[FAIL]` |

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R169** | **探测方式不能改变被探测的状态** | 已记录。用只读查询 |
| **R170** | **测试依赖私有宏会编不过**：`RTT_INUM_NUM` → `SOC_CPU_INTR_NUM` | 已记录 |
| **R171** | **断言失败时不要放宽断言**：要逐项写清楚新期望 | 已记录 |

### 12.72 W66（2026-11-01）：device 框架契约测试，自检 26 → 33 项

#### 一、新增 `s31test dev`（7 项子检查）

| 子检查 | 验什么 | 失败意味着 |
|---|---|---|
| `dev.fd` | `find()` 未知名字返回 NULL | 返回陈旧指针 |
| `dev.ur` | 未 open 时 `read()` 返回 0 | 驱动绕过框架 |
| **`dev.op`** | **无 open op 的设备也能 open，ref=1** | **所有总线设备走这条路** |
| `dev.o2` | 二次 open → ref=2 | 计数错 |
| `dev.cl` | 多余 close → `-RT_ERROR` | 重复递减 |
| `dev.ct` | 未知 control 的返回 | 见下 |

#### 二、`dev.ct` 记录但不判失败 —— 及其理由

`rt_device_control(uart0, 0x7f00, NULL)` 返回 **`RT_EOK`**。

看似缺陷，但查上游：

```
8 个 drv_uart.c 的 control() default 分支：全部返回 RT_EOK
```

**共享约定，不是疏忽。** 所以测试记录实际值但不判 FAIL。

**理由**：把它判成 FAIL，下一轮会有人"修复"成 `-RT_EINVAL`，
**反而让端口偏离上游。**

#### 三、`dev.op` 是最有价值的一条

`i2c0/spi0/adc1/adc2/pin/pwm0` **都没有 open/close op**，全靠框架的：

```c
if (device_open != RT_NULL) result = device_open(dev, oflag);
else dev->open_flag = (oflag & RT_DEVICE_OFLAG_MASK);
...
dev->ref_count++;
```

**这段坏了整个总线层就坏了，而且没人会发现**（没人对 `i2c0` 调 open）。

#### 四、测试纪律（沿用 W65）

| 纪律 | 体现 |
|---|---|
| 不改变被探测的状态 | 只对 `ref_count == 0` 做计数测试 |
| 不留副作用 | 结束时 ref 回到 0 |

验证：`pwm0`/`pin` 在 `s31test` 全跑后 ref 仍为 0，**无泄漏**。

#### 五、再次记录：`s31twai` 间歇失败

```
run 1: 0/4    run 2: 4/4    run 3: 4/4
```

**已知问题（W46 首次记录），非本轮引入。** 单次全失败、重跑全过
→ 更像状态未复位，不是速率算错。本轮不动。

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R172** | **"看起来像缺陷"的行为要查上游再判定** | 已记录。8/8 上游一致 → 是约定 |
| **R173** | **把上游约定"修"成更严格反而制造偏离** | 已记录 |
| **R174** | **没人调用的代码路径最需要测试**：总线设备的 open | 已记录 |

### 12.73 W67（2026-11-01）：头文件承诺了不存在的符号

#### 一、审计方向反过来

前几轮查"实现是否符合 RT 约定"；这一轮查**"头文件承诺的东西存不存在"**。

```
声明在 port 头文件、但没有定义的：
  s31_selftest_init     (port_selftest.h)
  s31_twai_cmd_init     (port_twai.h)
```

#### 二、根因：W60 改名的收尾不完整

W60 把 5 个链接锚点从 `*_init` 改成 `*_link_anchor`，
**只改了 `.c` 和 `board.c`，漏了两个头文件。**

#### 三、为什么这比"注释过期"严重

| 后果 | 说明 |
|---|---|
| **编不过** | 有人 include 并调用时，**链接期**才报错 |
| **更难查** | 报错指向一个"看起来该存在"的函数 |
| **现在没暴露是运气** | 没有文件 include 这两个头 |

**注释过期误导人；接口过期让人白找。**

#### 四、新增第 13、14 项检查

| 检查 | 结果 |
|---|---|
| 13. 头文件声明必须有定义 | ✅ 50 个声明全有定义 |
| 14. 声明函数必须有 `extern "C"` | ✅ 补了 3 个头文件 |

`extern "C"` 补在 `port_pwm.h`(2 decl)、`port_serial.h`(1)、`port_timer.h`(5)。
`port_board.h` 声明 0 个函数，不需要。

**纯 C 下无实际影响，但 C++ include 会链接失败 —— 关掉它的代价是零。**

#### 五、数据

| 指标 | 前 | 后 |
|---|---|---|
| 声明无定义 | 2 | **0** |
| 缺 `extern "C"` | 3 | **0** |
| 脚本检查项 | 12 | **14** |

#### 六、为什么会漏

**改名脚本只扫了 `*.c`**，`.h` 不在范围；而**编译不会因头文件里的过期声明失败**。

**⇒ 改名的收尾必须覆盖"所有文本引用"，不只是"能编译的那部分"。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R175** | **改名后头文件里的过期声明不会让编译失败** | 已记录，已纳入自动检查 |
| **R176** | **"现在没报错"不等于"没问题"**：没人 include 只是运气 | 已记录 |
| **R177** | **移植层的头文件也该有 `extern "C"`** | 已记录，已纳入检查 |

### 12.74 W68（2026-11-01）：配置里的死开关

#### 一、发现

`RT_USING_I2C_BUSSING` 在 `rtconfig.h` 里待了九轮，**没有任何代码读它**。

#### 二、四条独立证据

| 检查 | 结果 |
|---|---|
| `dev_i2c_core.c` 的 gate | 只 gate `RT_USING_DM` |
| `rtdevice.h` 的 I2C 头 gate | `RT_USING_I2C` |
| 466 个上游 BSP | **0 个**定义它 |
| 全树搜索 | **0 处引用** |

#### 三、为什么死开关比没有开关糟

```c
#define RT_USING_I2C
#define RT_USING_I2C_BUSSING     /* 看起来"配了 master/slave" */
```

**唯一的开关是 `RT_USING_I2C`。多那一行只产生错误印象。**

**没有开关时人查代码；有死开关时人信它。**

已删除，注释写了四条证据。`i2cscan` 仍正常。

#### 四、`ARCH_RISCV32` 保留 —— 标记不是开关

同样"无代码读取"，但：

```
24 个上游 BSP 定义 ARCH_RISCV32
21 个定义 ARCH_RISCV64
55 个定义 ARCH_RISCV
```

**这是约定标记，不是条件编译开关。删掉会与兄弟 BSP 不一致。**

**⇒ "无代码读取" ≠ "该删"。**

#### 五、新增第 15、16 项检查

**15. 无死开关**：只查 `RT_USING_*`/`FINSH_USING_*`/`MSH_USING_*`，
**明确排除 `ARCH_*`**（标记）和尺寸宏（值）。
结果：**26 个开关全部有引用。**

**16. 配置成对**：6 对（SERIAL/SERIAL_V1、FINSH/FINSH_USING_MSH、
FINSH_USING_MSH/FINSH_USING_SYMTAB、FINSH/MSH_USING_BUILT_IN_COMMANDS、
SMALL_MEM_AS_HEAP/SMALL_MEM、CONSOLE/DEVICE）。**全部满足。**

#### 六、数据

| 指标 | 前 | 后 |
|---|---|---|
| 死开关 | 1 | **0** |
| 脚本检查项 | 14 | **16** |

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R178** | **死开关制造错误印象**：比没有开关更糟 | 已记录 |
| **R179** | **"无代码读取"要区分开关与标记**：`ARCH_*` 不该删 | 已记录 |
| **R180** | **配置对必须成对检查**：缺一半能编过但行为变 | 已记录 |

### 12.75 W69（2026-11-01）：一条靠调用顺序凑巧成立的隐式不变量

#### 一、被推翻的注释

```c
/* the SMP build cannot reach the trap handler before board init, because the
 * MP scheduler and the per-CPU structures do not exist yet. */
```

**假的。** `nm` 显示 `_cpus` 在 `.bss`（`2f01eb10 b _cpus`），**从第一条指令起就可寻址。**

#### 二、真实情况

| 事实 | 证据 |
|---|---|
| `rt_irq_nest_slot` 在 `.bss`，初始全 0 | `nm`: `2f03154c B` |
| IDF 在 `start_cpu0_default()` 就装了 `_vector_table` | `startup.c` |
| trap handler **无条件**读表 | `vectors_rtthread.S:167` |
| slot 为 0 时 | **从地址 0 读 → load access fault** |

#### 三、真正让它在没崩的

**`rtthread_startup()` 第一句 `rt_hw_local_irq_disable()`** ——
MIE 关闭 ⇒ board init 前取不到中断 ⇒ trap 不跑。

**这是与调用方的契约，不是端口的性质。**

#### 四、为什么值得改

| 问题 | 后果 |
|---|---|
| 契约不可见 | 看不到"MIE 已关"这个前提 |
| 顺序一改就崩 | 加一句开中断 → 地址 0 访问错误 |
| 错误无指向 | `MTVAL=0x0` 在 trap 里，看不出是表没填 |
| **注释本身是错的** | 让人以为"表在能跑 trap 之前一定填好了" |

#### 五、修法

新增 `rt_hw_irq_nest_check()`（遍历检查非 NULL），`board.c` 里紧跟
`rt_hw_irq_nest_init()` 调用并打印。单核分支也实现（接口一致）。

注释改成事实："MIE 已关；旧注释描述的是信念不是机制"。

#### 六、`rt_hw_int_wdt_init`：理由成立，保留

查证 `esp_startup_start_app()` 在 `CONFIG_IDF_RTOS_RTTHREAD` 分支里**不被调用**
（`startup.c:280` 在 `#if !CONFIG_IDF_RTOS_RTTHREAD` 内）。**手工调用必需。**

#### 七、两个手工调用的最终判定

| init | 理由 | 结论 |
|---|---|---|
| `rt_hw_int_wdt_init` | IDF 路径不调 start_app | ✅ 成立 |
| `rt_hw_irq_nest_init` | ~~MP 调度器不存在~~ | ❌ 改成 MIE + 检查 |

#### 八、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R181** | **隐式不变量靠调用顺序成立最危险**：改一行就崩且错误无指向 | 已记录 |
| **R182** | **注释里的"原因"要验证**：可能是信念不是机制 | 已记录 |
| **R183** | **不变量要变成可检查的代码**，不能只写在注释里 | 已记录 |

### 12.76 W70（2026-11-01）：逐条验证 `boot_tasks.c` 的五个断言

#### 一、五条结果

| # | 断言 | 结论 |
|---|---|---|
| 1 | `esp_int_wdt_init` 缺了 | ✅ 成立，两符号现在都在镜像 |
| 2 | `esp_crosscore_int_init` 不需要 | ⚠️ **理由过期**，结论仍对 |
| 3 | 主线程已被 RT-Thread 替代 | ✅ 成立 |
| 4 | 20 KB DRAM 被扣住 | ✅ 成立，数字精确 |
| 5 | `esp_task_wdt_add` 无调用者 | ✅ 成立 |

#### 二、第 2 条：结论对、理由错

**原理由**：`CONFIG_FREERTOS_UNICORE=y`，没有第二个核。

**过期**：`RT_USING_SMP` + `RT_CPUS_NR=2`，CPU1 从 W56 起跑 idle。

**正确理由**：

| 检查 | 结果 |
|---|---|
| RT-Thread 自己的 IPI | ✅ `rt_hw_ipi_send` 在镜像（`40019cd2`） |
| IDF 跨核中断 | ❌ `esp_crosscore_int_*` 都不在 |

**"不需要 IDF 那个"是对的，因为已有自己的实现，不是因为没有第二核。**

**⇒ 结论对、理由错是最难发现的一类缺陷：不会让任何测试失败。**

#### 三、第 4 条：我自己算错过一次

第一眼拿 `__heap_end__`（`0x2f05bb10`）比 `0x2f07afb0`，差 145 KB，以为注释过期。

**算错。** 正确定义：

```
APP_USABLE_DRAM_END = SOC_ROM_STACK_START - SOC_ROM_STACK_SIZE
                    = 0x2f07cfb0 - 0x2000 = 0x2f07afb0
区域 = 0x2f07afb0..0x2f080000 = 20560 B = 20.1 KB
```

**注释精确。我拿两个不同的东西做了比较。**

**⇒ 验证断言要用断言自己的定义。**

#### 四、搜索要排除 test_apps

`esp_task_wdt_add` 全树 5 处命中：**4 处是 test_apps，1 处是本注释**。
`components/` 里 0 处。

**不排除 test_apps 会得出"有 4 处调用"的错误结论。**

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R184** | **结论对、理由错**也要修：不会让测试失败，只误导读者 | 已记录 |
| **R185** | **验证要用断言自己的定义**，不要就近找相关数字 | 已记录 |
| **R186** | **搜索必须排除 test_apps** | 已记录 |
| **R187** | **"当时正确"的理由会过期**：单核时代的理由到 SMP 不成立 | 已记录 |

### 12.77 W71（2026-11-01）：`s31twai` 间歇失败 —— 两处真 bug 已修，根因未明

#### 一、失败模式的确定

| 测试方式 | 结果 |
|---|---|
| 五次独立开机 | fail, pass, fail, pass |
| 同一次开机连跑 3 次 | 全部 0/4（一致） |

**⇒ 由"这一次开机"决定，不由调用次数决定。** 排除"状态未复位"。

失败信息：**四档全 `len 8, want 4`**（不是超时、不是发送失败）。

#### 二、真 bug 1：不清 RBNEI

IDF（`twai_hal_v2.c`）：

```c
twaifd_ll_get_rx_frame(dev, rx_frame);
// 'not_empty' interrupt can only be cleared after reading the frame
twaifd_ll_clr_intr_status(dev, TWAIFD_LL_INTR_RX_NOT_EMPTY);
```

本端口读了不清。**已修。**

#### 三、真 bug 2：等待条件用错信号

```c
/* 旧：RBNEI 不跟 FIFO 读指针同步 */
while (is_rx_buffer_empty() && !(intr_status & RBNEI))

/* 新：与 IDF 一致，等帧计数 */
while (twaifd_ll_get_rx_frame_count(s_twai) == 0u)
```

**已修。**

#### 四、修完仍间歇

```
after: 4, 0, 0, 4, 0
```

**失败信息没变。两个 bug 都是真的，但都不是根因。**

#### 五、诚实的判定

| 项 | 判定 |
|---|---|
| 清 RBNEI | ✅ 真 bug，已修 |
| 用帧计数 | ✅ 真 bug，已修 |
| **`len 8` 现象** | ❌ **未解决** |

**两个修复都让代码更接近 IDF，价值独立于现象是否消失。**

#### 六、下一轮：打印原始 word

```c
rt_kprintf("raw: %08x %08x %08x %08x %08x\n",
           buf.words[0], buf.words[1], buf.words[2], buf.words[3], buf.words[4]);
```

`words[0]` 的 bit[3:0] 就是 dlc。**直接看硬件给了什么。**

另外打印 `rwcnt`（`twaifd_struct.h:1770` 注释为 "Re-transmission counter"）。
`twaifd_ll_get_rx_frame()` 用它做循环上限，**非 0 就会多读 word、搞乱帧语义**。
这是最可能解释 `dlc=8` 的一条。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R188** | **"同理修复"与"修好现象"要分开记**：前者有独立价值 | 已记录 |
| **R189** | **区分"每次调用"与"每次开机"**：同一次开机一致说明是开机态 | 已记录 |
| **R190** | **失败信息的具体形态是线索**：`len 8` 缩到了 RX 解析 | 已记录 |
| **R191** | **推理到极限就打印原始数据**，不要继续从派生值反推 | 已记录 |

### 12.78 W72（2026-11-01）：原始寄存器数据 —— 三个假设被否定，负结果

#### 一、先纠正上一轮的测量错误

探针放在 `get_rx_frame()` **之后** → 只能看到被消费后的空 FIFO，**什么都没说明**。

移到**之前**（打 `cnt/empty/full/ovf/rx_status`）。

**教训：测决定行为的状态，不是测行为造成的结果。**

#### 二、拿到的事实

| 观察点 | pass | fail |
|---|---|---|
| TX 缓冲区 | `w0=00000004` dlc=4 | **完全相同** |
| 拷贝前 FIFO | `cnt=1 empty=0 full=0 ovf=0 st=0x10` | **完全相同** |
| RX format word | `00002104` dlc=4 | `00002908` dlc=8 |
| 另一次 fail | — | `00002909` **dlc=9** |

**三条结论：**

1. **本端口 TX 格式化正确** —— 缓冲区逐字节相同
2. **判定点之前状态不能解释差异**
3. **`dlc=9` 在 classic CAN 中不存在** → **读到的是语义上不可能的帧头**

#### 三、三个实验全部失败

| 实验 | 假设 | 结果（各 8 次开机） |
|---|---|---|
| `fde=0` | FD/classic 矛盾 | `0,4,0,4,0,0,0,4` ❌ |
| 50 us settle | 提交竞态 | `0,4,0,4,4,0,4,4` ❌ |
| 清 RBNEI + 帧计数 | 等待信号错 | 无改善 ❌ |

#### 四、保留与回滚

| 改动 | 处理 |
|---|---|
| 清 RBNEI | ✅ 保留（IDF 这么做） |
| 等帧计数 | ✅ 保留（IDF 这么做） |
| **拒绝非法 DLC** | ✅ **新增** |
| `fde=0` | ❌ 回滚 |
| 50 us settle | ❌ 回滚 |

残留检查：`esp_rom_printf`/`delay`/`dbg` 全为 0。

#### 五、新增：拒绝非法 DLC

```c
/* 旧：把非法值钳成合法值 */
if (*len > 8u) { *len = 8u; }

/* 新：说出来 */
if (hdr.dlc > 8u)
{
    rt_kprintf("twai: illegal DLC %u ...\n", ...);
    return -RT_ERROR;
}
```

**"返回最近的合法值"恰好掩盖了调用者最需要知道的损坏。**
**这也是 dlc=9 那次以前从未被发现的原因** —— 它被静默钳成 8。

#### 六、最终状态

- `s31test` **33/33**、`check_*` 全过
- **`s31twai` 仍间歇**（6 次开机 3 pass / 3 fail）

**但性质变了：**

| 之前 | 现在 |
|---|---|
| 4 轮记录、无原因 | **有原始数据，三个假设已排除** |
| 不知是本端口还是硬件 | **本端口格式化正确，问题在控制器环回路径** |

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R192** | **探针位置决定能观察到什么**：拷贝后的状态无意义 | 已记录 |
| **R193** | **物理上不可能的值（dlc=9）比统计更有诊断力** | 已记录 |
| **R194** | **失败的实验必须全部回滚**，否则污染后续判断 | 已记录 |
| **R195** | **负结果要写清排除了什么**，否则下一轮重复假设 | 已记录 |
| **R196** | **"钳成合法值"会掩盖损坏**：该报错就报错 | 已记录 |

### 12.79 W73（2026-11-01）：IRAM 代码调 flash 函数，两处命中

#### 一、规则早就写了，但只执行了一半

本端口记录过：
> IRAM 里的代码不能调 flash 里的函数

且**已经为违反它吃过苦**：`MEPC 0x40013b1e resolved to rt_enter_critical in IROM`。

**但只对 `_interrupt_handler` 执行过，没对驱动 ISR 执行。**

#### 二、`nm` 对地址，两处命中

| 符号 | 地址 | 区 |
|---|---|---|
| `s31_uart_isr` | `0x2f002c66` | **IRAM** |
| `rtt_trampoline` | `0x2f002e5a` | **IRAM** |
| `rt_kprintf` | `0x400128ba` | **flash** |
| `esp_rom_printf` | `0x2f800024` | ROM |

#### 三、第二处在 trap 路径上

```
vectors_rtthread.S:232   jal _global_interrupt_handler
interrupt.c:59           -> mcause-16 索引 -> 槽位是 rtt_trampoline
```

**它打印的正是"无处理函数却触发中断"，最可能在 flash cache 关闭窗口发生。**

#### 四、两处改 `esp_rom_printf`

`serial.c` 的 RX 溢出报告、`interrupt.c` 的 trampoline 报告。

#### 五、新增第 19 项检查

扫描所有 `IRAM_ATTR` 函数体，检查是否调用 16 个 flash 常驻 API。
**加检查时又抓到 trampoline 一次** —— 说明这条规则之前零机械保证。

#### 六、其余三项审计：都不是问题

**(a) 50 处 `return -1;`** —— 两种独立方法确认
**无一处在返回 `rt_err_t` 的函数里**。msh 命令是 `int`、ops 回调是 `rt_ssize_t`、
`s31_adc_vref` 是 `rt_int16_t`，各自合规。

**(b) 内核与端口"都定义"的 15 个符号** —— **全是误报**。
`compile_commands.json` 显示 35 个 rt-thread 对象，**`libcpu/risc-v/common/*.c` 一个没编**。
这是标准模式：新端口自己实现 libcpu 契约。**已加第 17 项检查。**

**(c) `s31_pin_ops` 8/12** —— 缺的 4 个在
`#ifdef RT_USING_DM` / `#ifdef RT_USING_PINCTRL` 内，**两个宏都没定义，字段不存在**。
使能块内 8/8。**已加第 18 项检查**（跟踪 `#ifdef` 嵌套）。

#### 七、检查器做了反向验证

临时删掉 `gpio.c` 的 `.pin_debounce`：

```
[FAIL] ops tables complete   ['gpio.c:rt_pin_ops.pin_debounce']
17/18 checks passed
```

**然后恢复并确认内容一致。检查器不是橡皮图章。**

#### 八、数据

| 指标 | 前 | 后 |
|---|---|---|
| IRAM 里调 flash API | 2 处 | **0** |
| 脚本检查项 | 16 | **19** |

#### 九、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R197** | **规则写下来 ≠ 规则被执行**：写了只作用于一个函数 | 已记录 |
| **R198** | **用 `nm` 对地址，别凭函数名猜位置** | 已记录 |
| **R199** | **审计多数结果是"看起来像问题"**：要能判定无关 | 已记录 |
| **R200** | **检查器要反向验证**：破坏一次确认它会红 | 已记录 |
| **R201** | **条件编译字段不是缺失**：先解析 `#ifdef` 再判断 | 已记录 |

### 12.80 W74（2026-11-01）：`bind` 运行中的线程到 CPU1 会静默停掉它

#### 一、三组对照（同一镜像）

| 命令 | 结果 |
|---|---|
| `bind <tshell> 0` | ✅ 成功，shell 继续工作 |
| `bind <tsystem> 1` | ✅ 成功，`ps` 显示 `bind 1` |
| `bind <tshell> 1` | ❌ **shell 卡死**，无 panic 无复位 |

**绑到 core 1 本身没问题**（`tsystem` 正常）。**只有"运行中的线程"不行。**

#### 二、分支差异

```c
if (thread_stat == RT_THREAD_RUNNING)
{
    if (RT_SCHED_CTX(thread).oncpu == current_cpu)
    {
        if (cpu != current_cpu)
        {
            rt_hw_ipi_send(RT_SCHEDULE_IPI, 1U << cpu);
            rt_sched_unlock_n_resched(slvl);      /* <- 真正的原因 */
        }
```

`tsystem` 是 suspend，不走这里。

#### 三、第一版归因被实验否决

第一版结论："IPI 无接收端 ⇒ pending 位没人清 ⇒ 挂住"，并加了守卫。

**重测三次仍然卡死。** ⇒ **IPI 不是原因。**

真正原因：`rt_sched_unlock_n_resched()` 在 CPU0 触发重调度，
**线程已绑到 CPU1 不在 CPU0 就绪队列**，CPU1 停在 `wfi` 永不调度
⇒ **线程永远拿不到 CPU**（不是卡在内核，是不再被选中）。

#### 四、注释按测量改正

`smp_ipi.c` 现在明确写：

```c
/* This is NOT what makes `bind <thread> 1` wedge the shell. ... the IPI was
 * not the mechanism. The mechanism is one level up ... */
```

并附三组对照数字。**IPI 守卫保留，但理由改成"无人服务的 pending 位本身就是错的"。**

#### 五、这是移植层的真实限制

| 事实 | 影响 |
|---|---|
| `bind` 是上游内建命令 | 用户会用 |
| 绑运行中的线程到 CPU1 静默停掉它 | **无报错、无 panic** |
| 根因 CPU1 不调度（W56 已知） | 已知限制的可见后果 |

**要真修需要 IPI 接收端 + CPU1 调度**，即 W49/W50/W52 卡住的同一件事。

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R202** | **一个对照组就能定位**：绑非运行线程成功、绑运行线程失败 | 已记录 |
| **R203** | **"加了守卫还是挂"是否决归因的关键证据** | 已记录 |
| **R204** | **归因错了必须连注释一起改** | 已记录 |
| **R205** | **上游内建命令在移植层可能不安全**：接口面不止自己写的函数 | 已记录 |
| **R206** | **"不报错地停掉线程"比崩溃更难查** | 已记录 |

### 12.81 W75（2026-11-01）：`console set uart0` 让 shell 永久失去输入

#### 一、现象与区分

```
msh > console set uart0
console change to uart0
msh >                    <- 之后无任何响应
```

**但板子没死** —— `s31con arm` 的定时器每秒照常打印。**死的是 shell 的读取路径。**

#### 二、前后状态（实测）

| 时刻 | `open_flag` | `ref` | `INT_RX` | `con` |
|---|---|---|---|---|
| 命令前 | **`0x014b`** | 1 | **yes** | `(none)` |
| 命令后 | **`0x004b`** | **2** | **NO** | `uart0` |

#### 三、机制

| 环节 | 位置 |
|---|---|
| shell 用 `RDWR\|INT_RX\|STREAM` 打开 | `0x143` |
| `rt_console_set_device()` 用 `RDWR\|STREAM` | `0x043` |
| `rt_device_open()` 标志不等（`0x10b` vs `0x04b`）就**重开** | `device.c` |
| `rt_serial_open()` 第一句 `open_flag = oflag & 0xff` | **清掉 INT_RX** |
| `rt_serial_read()` 按 `open_flag & INT_RX` 选路径 | 改走 `_serial_poll_rx` |

**而 `s31_uart_isr` 仍在把 FIFO 搬进那个 ring ⇒ poll 永远读到空 ⇒ shell 永久等待。**

#### 四、这是上游交互，不是本端口写错

三个环节全在上游。**但本端口另有一层冲突：**

> 端口**故意不调 `rt_console_set_device()`**，让 `_kputs()` 走 `rt_hw_console_output()`
> （ROM 输出），这样 panic 时输出还活着。

**`console set` 恰好把端口推进了它被设计来避免的状态，还顺手弄死了 shell 输入。**

#### 五、记录而不修

| 选项 | 评价 |
|---|---|
| 改 `cmd.c` / `dev_serial.c` | ❌ 违反"内核零修改" |
| 端口层拦截 | ⚠️ 掩盖上游行为 |
| **记录为已知限制** | ✅ 本轮选择 |

**把上游行为"修"成本地逻辑，会让端口偏离"未修改上游内核"这个资产。**

#### 六、新增 `s31con`（常驻诊断）

```
msh > s31con        -> [now] flag=0x0113 open=0x014b ref=1 int_rx=yes
msh > s31con arm    -> 每秒打印，shell 死后仍能看到状态
```

**"命令之后没有输出"无法区分"板子死"和"shell 死核还在跑"；
定时器打印把它们分开了。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R207** | **"没有输出"要区分板子死与 shell 死** | 已记录，已有工具 |
| **R208** | **诊断代码要在故障之后还能跑** | 已记录（用定时器而非 shell） |
| **R209** | **数据吻合源码才叫确认**：`0x014b→0x004b` 与 `oflag & 0xff` 一致 | 已记录 |
| **R210** | **上游交互错误不要修成本地逻辑** | 已记录 |
| **R211** | **端口的设计选择可能被上游命令推翻** | 已记录 |

### 12.82 W76（2026-11-01）：输出脚读不回自己的电平，已修

#### 一、现象

```
pin mode 10 output
pin write 10 high
pin read 10          -> pin[10] = low
```

#### 二、先排除"写没生效"

改成 `pin mode 10 input_pulldown` 再读仍是 low。
**一步区分了"写没生效"和"读不回来"两种假设。**

#### 三、根因

```c
case PIN_MODE_OUTPUT:
    gpio_ll_output_enable(&GPIO, pin);      /* 只开输出 */
/* s31_pin_read -> gpio_ll_get_level -> 读输入路径 */
```

**ESP32 输入缓冲 `IO_MUX.gpio[n].fun_ie` 默认关闭 ⇒ 输出脚读回恒为 0。**

#### 四、更关键：内部不一致

| 模式 | 输入缓冲 |
|---|---|
| `PIN_MODE_OUTPUT_OD` | **开**（能读回） |
| `PIN_MODE_OUTPUT` | **关**（读不回） |

**两个都是"输出"，读回能力不同，而 `rt_pin_read()` 文档没有这个区分。**

#### 五、上游 ESP32 BSP 也一样

`bsp/ESP/ESP32_C6/.../drv_gpio.c` 同样 `GPIO_MODE_OUTPUT` + `gpio_get_level()`。

**但 IDF 明确有 `GPIO_MODE_INPUT_OUTPUT`。**
而 `rt_pin_read` 文档说"get the level of the pin"，**没限制输入脚**。

#### 六、修法

```c
case PIN_MODE_OUTPUT:
    ...
    gpio_ll_input_enable(&GPIO, pin);       /* 新增 */
    gpio_ll_output_enable(&GPIO, pin);
```

#### 七、验证

| 操作 | 结果 |
|---|---|
| 写高读高 | ✅ |
| 写低读低 | ✅ |
| `s31test` | ✅ 33/33 连跑 3 次 |
| `s31led`/`s31pwm`/`s31rmt` | ✅ 无回归 |

#### 八、顺带记录（未修）

8 个命令打印用法后 `return -1`，msh 随即打印 `command failed -1.`。

**上游 `cmd_bind`/`cmd_list` 返回 0**，`console` 返回 `-RT_ERROR`（同样会打印）。

**上游自己不一致，且这是可读性噪音不是缺陷 ⇒ 记录，不动。**

#### 九、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R212** | **先区分"写没生效"与"读不回来"** | 已记录 |
| **R213** | **内部不一致比"和上游不同"更值得修** | 已记录 |
| **R214** | **"上游也这样"不等于"这样对"** | 已记录 |
| **R215** | **API 文档措辞是判据**：`rt_pin_read` 说 pin 不是 input pin | 已记录 |
| **R216** | **确认修复看行为，不看代码** | 已记录 |

### 12.83 W77（2026-11-01）：`pwm` 命令失效 + `adc voltage` 乱码

#### 一、`pwm probe pwm0` 永远说"请先 probe"

**根因：`FINSH_USING_OPTION_COMPLETION` 未开。**

```c
/* finsh.h */
#else
#define MSH_OPT_ID_GET(fun) ((int)(-1UL))      /* 恒为 -1 */
#endif
```

`PWM_LIST_PROBE == 1` ⇒ **switch 里的 PROBE 分支是死代码**。

**`cmd.c` 有 `#ifndef` 回退实现，`rt_drv_pwm.c` 没有。**

#### 二、启用（431/466 上游 BSP 都启用，Kconfig 默认 y）

```c
#define FINSH_USING_OPTION_COMPLETION
```

#### 三、启用后暴露 3 处宏参数错误

```
error: 'cmd_mcpwm_msh_options' undeclared   (mcpwm.c)
error: 'cmd_rmt_msh_options' undeclared     (rmt.c)
error: 'cmd_intr_msh_options' undeclared    (intr_debug.c)
```

**`MSH_CMD_EXPORT_ALIAS` 是变参宏，描述字符串里的逗号被当成参数分隔符** ⇒ 4 参数 ⇒
走 `_OPT` 版本 ⇒ 引用不存在的 `<cmd>_msh_options`。

**修法：描述里的逗号改成 `and` / `;`。**

**这是潜伏已久的问题**：选项关着时 `CMD_OPTIONS_STATEMENT` 展开为空，多一个参数也不报。

#### 四、`adc voltage 0` 打印 `-32.-768V` —— 本端口的错

```c
vref = dev->ops->get_vref(dev);
if (vref == 0) goto _voltage_exit;      /* 0 才是"不知道" */
voltage = value * vref / ((1 << resolution) - 1);
```

`s31_adc_vref()` 返回 `-1` ⇒ 通过检查 ⇒ 负数 ⇒ `printf("%d.%03d")` 打出 `-32.-768V`。

**上游 19 个 `drv_adc.c`：`return 0` 出现 5 次，`return -1` 出现 0 次。**

**已改为 `return 0` ⇒ `0.000V`。**（此前我把它记成"故意不修"，**判错了**。）

#### 五、连带：`check_image.py` 命令数一直错

启用选项后 `__fsymtab` 384 → 528 B，脚本仍 `// 12`：

```
528 / 12 = 44   （错）
528 / 16 = 33   （DWARF 确认 byte_size = 16，help 实际 33 条）
```

**它一直 PASS，因为只断言 `size > 0`。**

**改为从 DWARF 读结构体大小，不写死。**

**修的过程中又错一次**：搜 `"struct " + name` 找不到（DWARF 里是
`DW_AT_name : ...: finsh_syscall`），返回 0，检查立刻变红。

#### 六、验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ 33/33 连跑 3 次 |
| `pwm probe pwm0` | ✅ success |
| `adc voltage 0` | ✅ `0.000V` |
| `check_image.py` | ✅ 9/9，命令数 **33（正确）** |
| `s31mcpwm`/`s31rmt`/`s31intr` | ✅ 无回归 |

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R217** | **一个配置项能让整条命令失效且不报错** | 已记录 |
| **R218** | **潜伏的宏参数错误被"功能关闭"掩盖** | 已记录 |
| **R219** | **"不知道"的约定值要查框架判据**：`0` 而非 `-1` | 已记录 |
| **R220** | **只查 `> 0` 的断言，数字错了也不会红** | 已记录 |
| **R221** | **不写死结构体大小**，从 DWARF 读 | 已记录 |
| **R222** | **改检查器后立刻看它是否真会红** | 已记录 |

### 12.84 W78（2026-11-01）：一个抓不到问题的检查，验证后删除

#### 一、动机

W77 发现 `FINSH_USING_OPTION_COMPLETION` 未定义让整个 `pwm` 命令失效，
**且编译完全正常**。想把它变成自动检查。

#### 二、三次尝试，前两次都是误报

**(1) 统计"被引用但未定义"的开关** → 报 11 个：

```
RT_USING_HOOK    NO  452  <-- used by 11
```

**全是误报**：11 处"引用"就是 `#ifdef RT_USING_HOOK` 这些**判定行本身**。

**判定行是"决定别人的"，不是"被决定的"。**（R192 教训重演）

**(2) 排除指令行** → 剩 2 个，仍不对：

| 命中 | 实际是 |
|---|---|
| `idle.c:33` | `#define RT_USING_IDLE_HOOK`（漏了 `#define`） |
| `scheduler_mp.c:791` | `#else /* ! RT_USING_SIGNALS */`（漏了 `#else`） |

**(3) 补齐 `#else` / `#define` 后** → **真命中 0 个。**

#### 三、写成检查项 20，然后反向验证

```
[PASS] switch macros defined    all 48 defined, none read while undefined
```

**注释掉 `#define FINSH_USING_OPTION_COMPLETION` 后：**

```
[PASS] switch macros defined    all 47 defined, none read while undefined
```

**它没红。抓不到自己声称要抓的问题。**

#### 四、原因是结构性的

```
finsh.h:19/178/191   #ifdef FINSH_USING_OPTION_COMPLETION
```

**条件只在头文件里，没有 .c 在代码行里读它。**
RT-Thread 把这类开关**一律写成预处理指令**，
所以"在 .c 代码里读开关"这件事**本身不存在**。

**⇒ 检查要抓的东西不在它扫描的范围内。**

#### 五、删除

```python
# 检查 20 已删除（51 行）
19/19 checks passed
```

**一个不会失败的检查比没有检查更糟 —— 它看起来像覆盖。**

#### 六、本轮的实质产出

**用两种独立方法确认：当前没有第二个 `FINSH_USING_OPTION_COMPLETION` 式问题。**

**"审计无发现"也是结果。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R223** | **"引用"要区分判定行与被判定行**：`#ifdef X` 不是使用 X | 已记录 |
| **R224** | **检查必须能被反向验证** | 已记录 |
| **R225** | **不会红的检查比没有检查更糟** | 已记录 |
| **R226** | **动手前确认问题在哪一层**：条件在头文件就不能只扫 .c | 已记录 |
| **R227** | **"审计无发现"需要两种独立方法** | 已记录 |

### 12.85 W79（2026-11-01）：19 项检查逐个反向验证，2 项是装饰

#### 一、方法

**改坏检查声称要抓的东西，看它会不会红。** 这是 W78 用在检查 20 上的方法，
**从没对剩下 19 项做过。**

#### 二、结果：2 项是装饰

**(a) `INIT_*_EXPORT present`：`len(exports) >= 10`，实际 11 个**

删一个 export 仍 ≥ 10 ⇒ **不会红**。

**改成逐函数配对**：每个 `rt_hw_*_init` 必须"被导出"或"被 board.c 调用"。

**改的过程中踩坑**：`f"{i}(" in board_calls` 匹配到了
`extern int rt_hw_rmt_init(void);` **声明行**，删 export 仍看着"被调用"→ 仍不红。

**改成只认缩进语句行、排除 `extern`** ⇒ 两种变异都红。

**(b) `msh commands anchored`：`len(cmd_files) > 0`**

**只要有任何命令就恒真。** 改名 `smp_cmd.c` 的锚点 ⇒ stayed green。

**改成逐文件检查"靠什么活下来"**：called anchor / `-u` / 都没有。
**实测 6 靠锚点、9 靠 `-u`、0 无覆盖。** 变异后 → RED。

#### 三、一项是我变异选错方向

`config pairs complete` 第一轮删**主开关** `RT_USING_SMALL_MEM_AS_HEAP` ⇒ 绿。
**这是对的**——主开关没了就是整个功能关掉。

换成"主在、伙伴缺"：删 `FINSH_USING_SYMTAB`（`FINSH_USING_MSH` 还在）⇒ **RED**。

**⇒ 不是装饰，是我选错了变异。**

#### 四、最终 14 项（源码级）全部可红

| 检查 | 变异后 |
|---|---|
| init returns rt_err_t | RED |
| **every init is exported or called** | **RED**（重写） |
| board.c has few hand calls | RED |
| console hook in driver layer | RED |
| port headers match the sources | RED |
| port headers have extern "C" | RED |
| no dead config macros | RED |
| config pairs complete | RED |
| **msh commands kept in image** | **RED**（重写） |
| shell bound to a device | RED |
| rt_uint32_t in declarations | RED |
| framework registration used | RED |
| ops tables complete | RED |
| IRAM code avoids flash APIs | RED |

#### 五、本轮改动

**只改检查器，没改固件。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R228** | **`len(x) >= N` 在有 N+1 个时不会红** | 已记录，已重写 |
| **R229** | **`substring in text` 会匹配 `extern` 声明** | 已记录 |
| **R230** | **变异要选检查真正声称的方向** | 已记录 |
| **R231** | **装饰性检查价值为负**：让 19/19 失去意义 | 已记录 |

### 12.86 W80（2026-11-01）：ELF 级 9 项反向验证，3 项重写

#### 一、最严重：trap prologue 检查的是整个镜像

```python
dis = objdump("-d", elf)                        # 全镜像
trap_prologue = re.findall(r"addi sp,sp,-(\d+)", dis)   # 6780 个元素
check(..., ("128" in trap_prologue), ...)               # 有 53 处减 128
```

**从来没看过 trap handler。** 真正的事实（`_interrupt_handler` 前两条）：

```
2f000100: addi sp,sp,-128
2f000102: addi sp,sp,-128
```

**报告了正确的事实，理由却是错的。**

**改**：按 `_interrupt_handler` 符号定位，只反汇编前 12 字节，要求前两条都是 128。
**反向验证**：起点偏移 8 字节 ⇒ `[]` ⇒ RED。

#### 二、`new-thread frame is 256 B` 同样

原来全镜像数 `addi a0,a6,-256`（恰好 1 条，但**没检查在哪**）。
**改**：限定 `rt_hw_stack_init_core` 范围 + 要求恰好 1 次。
**反向验证**：改成立即数 `-1024` ⇒ `found 0` ⇒ RED。

#### 三、`trap entry installed`：`hits >= 25`，实测 29

**余量 4，且没说其余 19 个槽是什么。**

**查证**：48 槽只有两种值 —— `_interrupt_handler` (29) 和 `_panic_handler` (19)。

**改**：精确断言"没有 stray"。
**反向验证**：只允许 trap ⇒ 19 个变 stray ⇒ RED。

#### 四、9 项最终状态

| 检查 | 状态 |
|---|---|
| flash LOAD segments | ✅ 精确 |
| **new-thread frame is 256 B** | ✅ **重写** |
| **trap prologue subtracts 128+128** | ✅ **重写** |
| trap exit mstatus FS=3 | ✅ 精确 |
| \_\_fsymtab non-empty | ✅ W77 起从 DWARF 读 |
| \_\_rt_init non-empty | ✅ |
| init table order | ✅ 逐项 |
| \_mtvt_table present | ✅ |
| **trap entry installed** | ✅ **重写** |

#### 五、三者的共同点

| 检查 | 错在哪 |
|---|---|
| trap prologue | 全镜像 grep |
| new-thread frame | 全镜像 grep，没定位函数 |
| trap entry installed | 下界 25 而非精确集合 |

**都是 `>= N` 或 `globally in text` —— 与 W79 在 `check_rtt_style.py` 找到的两项完全同类。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R232** | **全镜像 grep 不是定位** | 已记录 |
| **R233** | **下界断言是弱断言** | 已记录 |
| **R234** | **"报告正确事实"≠"检查正确"** | 已记录 |
| **R235** | **反汇编检查要打印它实际看了哪里** | 已记录 |
| **R236** | **同类缺陷会跨文件重复**：共找到 5 项 | 已记录 |

### 12.87 W81（2026-11-01）：行为断言注入故障，3 项验证 1 项部分

#### 一、`fpu`：删 f0..f31 保存 ⇒ FAIL

**第一次注入太粗**：`#ifdef ARCH_RISCV_FPU` → `#if 0`，
连 `addi sp,-32*FREGBYTES` 一起去掉 ⇒ 帧布局坏 ⇒
`Illegal instruction, MEPC 0x2f019518`。**这是另一个故障。**

**第二次：保留栈帧，只删 32 条 `FSTORE`：**

```
[FAIL] fpu   1000 mismatches, first at reg 15 value 0x63ca
```

**✅ 有效。**

#### 二、`rhwh.ud`：us_delay 变空 ⇒ FAIL

```
[FAIL] rhwh.ud  1000 us request took 0 us (want 900..1600)
[FAIL] rhwh     5 sub-checks, 1 bad
```

**✅ 有效，且汇总行正确归因到 1 项。**

#### 三、`tick`：tick 加倍 ⇒ FAIL

```
[FAIL] tick   4000 ticks in 1999999 us = 499 us/tick (expect 1000, 1000 Hz)
```

**✅ ±5% 窗口足够紧，2 倍错误立刻抓到（16 倍更不用说）。**

#### 四、`ctx`：注入太粗，破坏的是 shell 自己

破坏 saved s1：

```
s31test: command not found.
Guru Meditation Error: Core 0 panic'ed (Load access fault)
MEPC 0x4001b8ba   S1: 0x5a5a5a5a
```

| 观察 | 含义 |
|---|---|
| `S1: 0x5a5a5a5a` | **s1 确实从保存槽恢复** ⇒ 恢复路径是真的 |
| 命令没执行 | 破坏发生在 **shell 的切换**上，测试没开始 |

**⇒ 证明了"寄存器真的从槽恢复"，但没证明 `ctx` 会报错。部分结果。**

#### 五、进度（如实）

| 项 | 结果 |
|---|---|
| `fpu` | ✅ FAIL |
| `rhwh.ud` | ✅ FAIL |
| `tick` | ✅ FAIL |
| `ctx` | ⚠️ 部分 |
| 其余 29 项 | 未做 |

**33 项验证 3 项 + 1 项部分，不是"33 项已验证"。**

#### 六、恢复纪律（四次注入全部恢复）

每次：断言注入点唯一 → 注入 → 编译烧写观察 → **恢复** → 编译烧写 → 确认 33/33
→ 确认 `provoked` 标记数为 0。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R237** | **注入要精确到目标缺陷**，否则制造另一个故障 | 已记录 |
| **R238** | **保留结构的注入才有效** | 已记录 |
| **R239** | **注入可能先破坏测试环境** | 已记录 |
| **R240** | **部分成功要记成部分** | 已记录 |
| **R241** | **"抓到"与"没抓到"都要有证据** | 已记录 |

### 12.88 W82（2026-11-01）：继续注入，3 项验证 + 一次重犯旧错

#### 一、`irq`：第一次注入把 shell 弄死了

让 `rt_hw_interrupt_umask()` 不置 IE 位 ⇒ 板子打印 banner 后**完全不响应**。

**原因**：`finsh` 用 `INT_RX` 打开 uart0，驱动 `control(SET_INT)` 调
`rt_hw_interrupt_umask(ETS_UART0_INUM)` ⇒ **注入关掉了 shell 自己的输入**。

**与 W81 的 `ctx` 完全同一个错：注入面太大。**

#### 二、第二次只动引脚分发 ⇒ 成功

```
[FAIL] irq   pin 10: 0/200 interrupts, nest=0 (0 bad)
```

#### 三、`nest`：电平压平 ⇒ FAIL

```
[FAIL] nest  pin 10->11: outer 100@nest1, inner 100@nest1 (want 100, 100 bad)
```

**注意外层 100 次仍 PASS ⇒ 失败定位精确："不是没中断，是没嵌套"。**

#### 四、`rhwh.mk`：is_masked 取反 ⇒ FAIL

```
[FAIL] rhwh.mk  mask/umask: 1 -> 0 (want 1) -> 1 (want 0)
```

#### 五、进度

| 测试 | 结果 |
|---|---|
| `fpu` / `rhwh.ud` / `tick` | ✅ W81 |
| `rhwh.mk` / `irq` / `nest` | ✅ W82 |
| `ctx` | ⚠️ 部分 |
| 其余 26 项 | 未做 |

**7 项验证会失败，1 项部分，26 项未做。**

#### 六、核心判据（本轮才写下来）

| 轮次 | 注入点 | 为什么错 |
|---|---|---|
| W81 | 破坏 saved s1 | 破坏 shell 自己的切换 |
| W82 | umask 不置位 | 关了 shell 自己的 UART RX |

**规律：注入面覆盖了 shell 赖以运行的东西。**

**⇒ 判据：注入点必须是"只有被测对象在用"的函数。**

`s31_gpio_isr()` 只有引脚中断用 ⇒ 成功。
`rt_hw_interrupt_umask()` 被 UART 驱动用 ⇒ 失败。

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R242** | **注入前先问"这个函数还有谁在用"** | 已记录（犯了两次才写下来） |
| **R243** | **注入点应只有被测对象在用** | 已记录 |
| **R244** | **失败输出要能定位到子项** | 已记录 |
| **R245** | **同一个错可以犯两次**：判据没写下来就等于没学会 | 已记录 |

### 12.89 W83（2026-11-01）：`rhwh.ud` 间歇失败 —— 是容差问题，用 best-of-5 修

#### 一、现象（首次记录）

```
run 1: 1000 us PASS
run 2: 1000 us PASS
run 3: 1998 us FAIL
run 4: 1000 us PASS
```

**约四次一次。** 之前几轮从未记录过这个间歇。

#### 二、原因：1998 恰好 2 倍

`rt_hw_us_delay` 在 SYSTIMER 上忙等，**1 kHz tick 在 1000 us 里必然触发一次**。
ISR 期间忙等停住 ⇒ 单次测量最多多一个完整周期。

**⇒ 测量方法问题，不是端口缺陷。**

#### 三、错误修法：放宽到 3000

**稳定了（8 次里 1 次 1999，全过），但代价：**

| 错误 | 实测 | 3000 窗口 |
|---|---|---|
| weak stub | ~0 | ✅ |
| **2 倍单位错** | **~2000** | ❌ **与噪声同量级** |
| 16 倍 | ~16000 | ✅ |

**"放宽到能容纳噪声" = "对同量级真实错误失明"。**

#### 四、正确修法：取 5 次最小值

**关键：抢占只能让测量偏大，不可能偏小。**

```c
for (try = 0; try < 5; try++) { ...; if (elapsed < best) best = elapsed; }
elapsed = best;
```

**窗口保持 900..1600。**

**8 次验证全部 1000 us PASS。**

#### 五、收紧后重新验证仍能抓到 stub

```
[FAIL] rhwh.ud  1000 us request took 0 us best of 5 (want 900..1600)
```

**✅ 没有削弱检测能力。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R246** | **"间歇失败"先问是不是测量方法** | 已记录 |
| **R247** | **放宽容差 = 对同量级错误失明** | 已记录 |
| **R248** | **找噪声的单向性**，取最小值滤掉 | 已记录 |
| **R249** | **收紧后必须重新验证仍能抓到目标** | 已记录 |
| **R250** | **间歇失败要记录具体频率** | 已记录 |

### 12.90 W84（2026-11-01）：`RT_TIMER_FLAG_SOFT_TIMER` 被接受但静默降级

#### 一、先确认稳定性

**连续跑 15 次全套**（修完 W83 的 `rhwh.ud` 之后）：

```
run 1..15 : === 33 passed, 0 failed ===
rhwh.ud   : 1000 us best of 5
```

**修前 `rhwh.ud` 4 次里 1 次失败，修后 15 次全过。**

#### 二、顺带核对另外两处时间断言

| 断言 | 窗口 | 12 次实测 |
|---|---|---|
| `clock` 100 ms | 90000..300000 us | 99997~100000 us |
| `fbshim/g` 50 ms | >5000 us 判为睡了 | 稳定 |

**`clock` 报文说 "want 100000" 而断言是 300000 上限 —— 报文比断言严格，不会漏报。记录不动。**

#### 三、发现的语义偏差

`RT_USING_TIMER_SOFT` **未定义**：

```c
/* rt_timer_create —— 不检查标志 */
_timer_init(..., flag);                    /* 原样存下 */

/* rt_timer_start */
#ifdef RT_USING_TIMER_SOFT
    if (flag & RT_TIMER_FLAG_SOFT_TIMER)
        timer_list = _soft_timer_list;
    else
#endif
        timer_list = _timer_list;          /* 硬件链表 */
```

**软定时器被挂进硬件链表 ⇒ 回调在 tick 中断上下文执行。**

**没有报错、没有警告。**

#### 四、与 W75 同类

| W75 | W84 |
|---|---|
| `console set` 接受命令，静默弄死输入 | `SOFT_TIMER` 接受标志，静默换上下文 |

**"接受但降级"比"拒绝"危险。**

#### 五、查证 + 加检查

`RT_TIMER_FLAG_SOFT_TIMER` 在 port/ 和 main/ 的出现次数：**0**。
**当前无实际问题，但没有东西阻止以后有人写。**

第 21 项检查：`RT_USING_TIMER_SOFT` 未定义时，端口自身不得请求软定时器。

#### 六、这次先反向验证

```
# 加 | RT_TIMER_FLAG_SOFT_TIMER
[FAIL] requests a soft timer but RT_USING_TIMER_SOFT is off: ['console_probe.c:...']
# 恢复
[PASS] RT_USING_TIMER_SOFT off, and no port code requests one
```

**✅ 会红，真阳性。**

#### 七、为什么这项能做而检查 20 不能

| | 检查 20（删） | 检查 21（留） |
|---|---|---|
| 条件位置 | **头文件 `#ifdef`** | **调用点参数** |
| 扫 `.c` 可见 | ❌ | ✅ |
| 能构真阳性 | ❌ | ✅ |

**判据：检查要抓的东西必须在它扫描范围内。**

#### 八、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R251** | **修完噪声要重跑多次确认** | 已记录（15 次） |
| **R252** | **"接受但降级"比"拒绝"危险** | 已记录 |
| **R253** | **上游静默降级，端口能做的是"别依赖它"** | 已记录 |
| **R254** | **先反向验证再宣布** | 已记录 |
| **R255** | **检查目标必须在扫描范围内** | 已记录 |

### 12.91 W85（2026-11-01）：三处驱动丢弃 ISR 安装结果，已修 + 第 22 项检查

#### 一、扫描结果

| 类别 | 数量 |
|---|---|
| 结果被赋值 | 15 |
| 显式 `(void)` | 2（故意） |
| **语句级丢弃** | **13** |

**13 处里 3 处是实质缺陷：**

```
gpio.c     rt_hw_interrupt_install(s_gpio_intno, ...)
serial.c   rt_hw_interrupt_install(ETS_UART0_INUM, ...)
selftest.c rt_hw_interrupt_install(ST_NEST_INNER_INUM, ...)
```

#### 二、这个返回值不能直接测

```c
if (vector 越界) { rt_kprintf(...); return RT_NULL; }   /* 失败 */
old = s_ctx[vector].fn;
return old;                                             /* 没装过也是 RT_NULL */
```

**成功与失败都返回 `RT_NULL`。**

#### 三、正确做法：问槽位

```c
rt_hw_interrupt_install(...);
if (intr_handler_get(INUM) == RT_NULL) return -RT_ERROR;
```

**三处都改。静默失败的后果：**

| 站点 | 症状 |
|---|---|
| `serial.c` | uart0 注册成功但**一个字节都收不到** |
| `gpio.c` | pin 注册成功但**全部 attach_irq 是死的** |
| `selftest.c` | nest 测试**测不到东西**却可能报 PASS |

#### 四、第 22 项检查

规则：install 之后 10 行内必须有 `intr_handler_get()`。

**先写检查 → 立刻红（三处真站点）→ 修三处 → 变绿。**

#### 五、修检查器时发现它自己的 bug

第一次修完仍红，且行号 `gpio.c:224` 与真实 274 行不符。

**原因：先剥块注释再分行，行号偏移。**

**⇒ 报错行号不对 = 教人不要相信它。** 改为保留原文本逐行走。

同时要接受"装完查槽位"的新形态，否则真站点被误报。

#### 六、反向验证

```
# 删掉 serial.c 的槽位检查
[FAIL] ['serial.c:232'] install without confirming the slot    ← 行号正确
```

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R256** | **`RT_NULL` 既成功又失败时只能问状态** | 已记录 |
| **R257** | **"结果丢弃"扫描能找出真问题** | 已记录 |
| **R258** | **检查器报的行号必须真实** | 已记录 |
| **R259** | **新检查要接受修复后的正确形态** | 已记录 |
| **R260** | **先写检查让它红，再修** | 已记录 |

### 12.92 W86（2026-11-01）：`sem` 里一个"看着像完成标志"的死变量

#### 一、继续 W85 口径

扫 `rt_sem_*` / `rt_mutex_*` / `rt_event_*` / `rt_device_*`：**只剩 4 处，全是 sem 测试自己的。**

但顺带发现更值得处理的东西。

#### 二、`s_sem_sent`：写 3 处，读 0 处

```
:848  static volatile rt_uint32_t s_sem_sent;
:858      s_sem_sent++;
:864  s_sem_sent = 0x80000000u | n;
```

**编码泄露原意**：`0x80000000|n` 想做"生产者完成"标志。
测试后来改成等 token 计数（更强），标志变成死状态。

**死状态如果看起来像完成标志，就是在骗下一个读者。**

#### 三、顺手接上 release 失败计数

```c
if (rt_sem_release(&s_sem) != RT_EOK) s_sem_release_failed++;
```

报文体：`500/500 tokens, 0 release failures, extra take timed out`
判据加 `&& (s_sem_release_failed == 0)`。

#### 四、反向验证：`max_value = 0`

```
[FAIL] sem   0/500 tokens, 500 release failures, extra take timed out
```

**✅ 计数器接对了。**

#### 五、第 23 项检查：无"只写不读"的文件级变量

**三类合法情形不能误报：**

| 情形 | 为什么合法 |
|---|---|
| ops 表 | 取地址注册 = 读 |
| 结构体实例 `s_pwm` | `&s_pwm.parent` |
| 同文件内读的标志 | `if (!...)` |

**规则**：只在赋值形态出现的行 + 声明行，无"非赋值行"才报。
**并且必须剥注释** —— `s_sem_sent` 的注释提到它自己，不剥会被当成读。

#### 六、反向验证

```
[FAIL] written but never read: ['selftest.c:s_dead_marker']
[PASS] every file-scope variable is read somewhere
```

**✅ 会红，且无合法误报。**

#### 七、全端口复扫 8 个低出现次数静态变量

6 个 ops 表 + `s_pwm` + `s31_ipi_receive_side_ready` —— **全部合法。**
**除 `s_sem_sent` 外无第二个真问题。**

#### 八、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R261** | **死状态看起来像检查时最危险** | 已记录 |
| **R262** | **测试演进会留下孤儿状态** | 已记录 |
| **R263** | **剥注释再统计**，否则注释里的变量名算"读" | 已记录 |
| **R264** | **取地址是读**：ops 表不能误报 | 已记录 |
| **R265** | **反向验证要包含"不误报合法情形"** | 已记录 |

### 12.93 W87（2026-11-01）：子检查数是写死的，跳过时是假数字

#### 一、两处写死分母

```c
"%d sub-checks, %d bad", 5, detail_bad   /* rhwh */
"%d sub-checks, %d bad", 7, bad          /* dev   */
```

**两个组都有跳过路径：**

| 跳过点 | 条件 | 跳过几项 |
|---|---|---|
| `rhwh.iv` | 扫不到空闲 INUM | 4 项 |
| `dev.rc` | `pwm0` 被占用 | 4 项 |

**跳过时汇总仍写 5 / 7 —— 数字是错的。**

#### 二、改成"实际跑了几项"

```
[PASS] rhwh   5/5 sub-checks ran, 0 bad
```

#### 三、`dev` 报出 `5/7`，暴露两个问题

**(a) `dev.rc` 正常路径不报告** —— 只在缺失/占用时说话。
**7 个标签只有 6 个会报。** 已修：

```
[PASS] dev.rc   pwm0 idle (ref=0), ref-count checks run
```

**(b) 仍 6/7** —— `dev.ct` 的 `else` 分支漏了 `ran++`。

**这是我自己加计数器时的疏忽，计数器把它抓出来了。**

修完：`[PASS] dev   7/7 sub-checks ran, 0 bad`

#### 四、自检总数 33 → 34

`dev.rc` 现在正常路径也报告，`s_pass` 多一行。
**连跑 4 次全部 34。这是子检查变细的结果，不是新增功能。**

#### 五、对比

| 之前 | 之后 |
|---|---|
| `5 sub-checks`（永远 5） | `5/5 sub-checks ran` |
| `7 sub-checks`（实际 6） | `7/7 sub-checks ran` |
| 跳过不体现 | **跳过立刻可见** |

**同一个缺陷：看起来像测量值的数字，其实不是。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R266** | **报文里的数字也要审** | 已记录 |
| **R267** | **写死分母在跳过路径上必然错** | 已记录 |
| **R268** | **改分母会暴露标签数与断言数不一致** | 已记录 |
| **R269** | **自己加的计数器会抓到自己的疏忽** | 已记录 |
| **R270** | **子检查变细会让总数变化，要确认是预期的** | 已记录 |

### 12.94 W88（2026-11-01）：`st_clock` 是带判据的测试却不在套件里

#### 一、方法

比对"定义的测试函数"与"`st_all` 调用的"。

**第一次结果有 3 个误报**（正则只匹配 `(void)`，而 `st_irq`/`st_nest`/`st_preempt`
带参数），**但 `st_clock` 是真发现。**

#### 二、`st_clock` 确实是测试

```c
int ok = (d_us >= 90000u && d_us <= 300000u);
st_report("clock", ok, ...);
return ok ? ST_PASS : ST_FAIL;
```

**有判据、有 report、有返回值，但 `st_all` 里没有它。**

**⇒ 例行"34 passed"时这一整块在套件之外。不在套件里的测试 = 没人跑的测试。**

#### 三、顺手修报文的两处

**(a) 错误归因**：报文说 "os_tick says"，但值来自 `s31_systimer_us()`。

改成 `systimer_us says ... over a 100 ms delay`。

**(b) 报文比断言严格**：报文说 "want 100000"，断言是 90000..300000。
**窗口写进报文。**

#### 四、加入后需前置声明

`st_all` 在 2311 行，`st_clock` 在 2390 行。

```c
static int st_clock(void);
```

#### 五、自检 34 → 35（真新增覆盖）

```
=== 35 passed, 0 failed ===   连跑 4 次
```

**与 W87 的"子检查变细"不同，这是真的多跑了一个测试。**

#### 六、三路参考一致

```
os_tick  +1599999 counts = +99999 us @16MHz
esptimer +0 counts                       <- unit 0 不计数（已知）
ccount   +31731276 cycles @320MHz = 99160 us
[PASS] clock   systimer_us says 100000 us (window 90000..300000)
```

**99999 / 100000 / 99160 us 互相印证。ccount 偏低 0.84%，在 10% 窗口内正常。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R271** | **"定义的测试"与"套件里的测试"要对比** | 已记录 |
| **R272** | **正则只匹配 `(void)` 会漏带参函数** | 已记录 |
| **R273** | **不在套件里的测试 = 没人跑的测试** | 已记录 |
| **R274** | **报文比断言严格会误导读者** | 已记录 |
| **R275** | **加入顺序要看定义位置** | 已记录 |

### 12.95 W89（2026-11-01）：用法说明漏了 3 个子命令，最大组无说明

#### 一、比对结果

| | 数量 |
|---|---|
| 实际分派 | **12**（含 `all`） |
| 用法首行列出的 | **9** |
| 缺的 | `rhwh`、`dev`、**`fbshim`** |

**`fbshim` 连说明行都没有 —— 而它是套件里最大的组（11 项子检查）。**

**⇒ 用户永远不知道 `s31test fbshim` 存在。**

#### 二、同类缺陷的第三次

| 轮次 | 输出声称 | 实际 |
|---|---|---|
| W87 | "5 sub-checks" | 跳过时只跑 1 项 |
| W88 | "34 passed" | `st_clock` 不在套件 |
| **W89** | 用法列 9 个 | **分派 12 个** |

**都是"输出声称的比代码做的少"。**

#### 三、修法 + 第 24 项检查

用法首行补全 12 个名字，补上 `fbshim` 说明行。

```
[PASS] s31test usage is complete    all 11 subcommands documented
```

#### 四、双向反向验证

```
# 删 fbshim 说明行
[FAIL] no description line: ['fbshim']

# 首行括号里删 dev
[FAIL] not in the usage line: ['dev']
```

**✅ 两个方向都会红，分别指向不同缺口。**

#### 五、为什么能做而检查 20 不能

| | 检查 20（删） | 检查 24（留） |
|---|---|---|
| 比对双方 | 头文件 `#ifdef` ↔ `.c` | **同一文件 ↔ 同一文件** |
| 真阳性 | ❌ | ✅ 两个方向 |

**判据：比对的两边都必须在扫描范围内。**

#### 六、顺带查清一个旧疑点

`i2c.c:393 bus->parent.ops = &s31_i2c_ops;` 在 `RT_USING_DEVICE_OPS` 未定义时
我以为有问题。**查清：**

```
struct s31_i2c           { struct rt_i2c_bus_device parent; ... }
struct rt_i2c_bus_device { struct rt_device parent; const struct rt_i2c_bus_device_ops *ops; }

bus->parent.ops == rt_i2c_bus_device.ops   <- 与 DEVICE_OPS 无关
```

**不是缺陷。记录免得再疑。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R276** | **用法说明要与实际分派比对** | 已记录 |
| **R277** | **最大的组最容易被漏** | 已记录 |
| **R278** | **"输出比代码少"已第三次出现** | 已记录 |
| **R279** | **两边同文件才容易做可证伪的检查** | 已记录 |
| **R280** | **旧疑点要查清并记录** | 已记录 |

### 12.96 W90（2026-11-01）：FPU 失败信息用 `%f`，而格式化器吞掉 `%f`；并加了第 25 项检查

#### 一、`%f` 的结论

`rt_vsnprintf_tiny.c`：

```c
case 'e': case 'E': case 'G': case 'g': case 'f': case 'F':
    va_arg(args, double);        /* 只消费参数 */
default:
    ... 打印转换符本身 ...
```

**⇒ `%f` 不输出数字。**

#### 二、PASS 修了，FAIL 漏了

```c
rt_kprintf("FPU ok seed=%d/10\n", (int)(seed * 10.0f));    /* 已修 */
rt_kprintf("FPU FAIL at f[%d]: %f != %f\n", ...);          /* 未修 */
```

**真实失败时会打印 `FPU FAIL at f[3]:  != ` —— 两个值都不见。**

**罕见失败路径的消息最需要审。**

#### 三、修法：打印 IEEE-754 位模式

```c
union { float f; rt_uint32_t u; } got, want;
rt_kprintf("FPU FAIL at f[%d]: got 0x%08x, want 0x%08x\n", ...);
```

**反向验证（强制 i==3 失配）：**

```
FPU FAIL at f[3]: got 0x40800000, want 0x40800000
FPU FAIL at f[3]: got 0x42ce0000, want 0x42ce0000
```

**✅ 两个值都出现。**

#### 四、`s31stat` 描述补全

```
修前: show tick and thread counters
修后: tick + both heaps + demo-thread counters + CPU1 state
```

**漏的正是两个堆和 CPU1 —— bring-up 最需要看的两项。**

#### 五、第 25 项检查：描述里不得有逗号

**这条规则记录过两次、违反过两次：**

| 轮次 | 文件 |
|---|---|
| W77 | mcpwm.c / rmt.c / intr_debug.c |
| **W90** | **main.c s31stat（我自己改的）** |

**⇒ 只写在笔记里的规则一定会被再犯一次。**

```
[PASS] no comma in cmd descriptions    every command description is comma-free
```

**反向验证：**

```
[FAIL] ['smp_cmd.c:75 (3 commas, want 2)'] - a comma splits the macro's arguments
```

#### 六、写检查时又差点数错

第一次把 `expect` 设成 `2 / 3`（参数个数），报出 24 个假阳性。
**实际是逗号个数：2 参数 = 1 逗号，3 参数 = 2 逗号。** 改成 `1 / 2` 后全绿。

**⇒ 检查器的第一个版本几乎总是错的，必须先跑一遍。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R281** | **罕见失败路径的消息最需要审** | 已记录 |
| **R282** | **PASS 修了不等于 FAIL 修了** | 已记录 |
| **R283** | **位模式是浮点值的精确表示** | 已记录 |
| **R284** | **同一规则犯两次 ⇒ 必须进检查** | 已记录 |
| **R285** | **检查器第一版几乎总是错的，先跑一遍** | 已记录 |

### 12.97 W91（2026-11-01）：手册 §0"务必先读"六项全过时，并加第 26 项检查

#### 一、实测 vs 手册

| 项 | 手册 | 实测 |
|---|---|---|
| 编译产物 | 898,688 B | **1,517,568 B** |
| 应用分区 | 1 MB，剩 147 KB | **4 MB，剩 2.55 MB** |
| 自检 | 20/20 | **35/35** |
| 核数 | 单核 | **双核** |
| flash | 2 MB | **16 MB** |
| 校验器 | 9 项 | **9 + 24 项** |

**六项全错，而这一节叫"务必先读"。**

#### 二、为什么比没写更糟

- **信它的人**：按 1 MB 分区规划、以为只剩 147 KB、以为单核
- **查它的人**：发现第一行就错，不再信后面任何一行

#### 三、修法：数字带复测命令

新增"怎么复测"列，每项给出可执行命令。
表下注明"六项全过时"作为警示。

#### 四、第 26 项检查：手册产物大小必须与磁盘一致

**只断言能离线验证的那一项。**

```
[PASS] section 0 quotes 1,517,568 B, matches the artifact
反向验证: [FAIL] section 0 says 898,688 B, artifact is 1,517,568 B
```

**目的不是覆盖所有数字，而是让"至少有一个数字被盯着"。**

#### 五、区分"当前状态"与"历史记录"

`§12.5` 的"当前状态（2026-10-27）"也过时，**但它是历史记录**（带日期、提交号）。
**改它反而破坏记录可信度。保持原样。**

**⇒ 只有"务必先读"类章节必须与实测同步。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R286** | **文档也是交付物，要同标准审** | 已记录 |
| **R287** | **"务必先读"的表错了比没写更糟** | 已记录 |
| **R288** | **数字要么带复测命令，要么不写** | 已记录 |
| **R289** | **只断言可离线验证的那一项** | 已记录 |
| **R290** | **区分"当前状态"与"历史记录"** | 已记录 |

### 12.98 W92（2026-11-01）：手册"已知限制"里两条已不是限制 + 第 27 项检查

#### 一、逐行核对 §8 的 7 条

| 限制 | 仍成立？ |
|---|---|
| 波特率固定 115200 | ✅ |
| `rt_kprintf` 走 ROM putc | ✅ |
| TX 忙等 | ✅ |
| GPIO 只用通道 0 | ✅ |
| UART0/GPIO 默认 1 级 | ✅ |
| **无硬件看门狗** | ❌ **中断看门狗已启动** |
| **Wi-Fi/BLE 未适配** | ❌ **两者都能跑** |

#### 二、中断看门狗

手册说"INT/TASK WDT 初始化都在 FreeRTOS 专属路径，RT-Thread 路径不执行"。

**实际 `boot_tasks.c` 显式调用，两符号在镜像里（`0x400982b2`/`0x4009836e`），
tick 由 `esp_vApplicationTickHook()` 喂。**

**改成分两行**：任务看门狗未启动（无订阅者，有意）／中断看门狗已启动（已完成）。

#### 三、Wi-Fi / BLE

实测 `s31wifi` 起驱动（`wifi driver task: ... core=0`）、`s31ble` 起控制器
（`BTDM controller init OK`）。

**改成：已能跑在兼容层上，但无连接测试；另加"无 5 GHz"一条。**

#### 四、两个 `## 8`

```
389: ## 8. 已知限制
403: ## 8.5 打开 FreeRTOS 兼容层
```

**8.5 夹在 8 和 9 之间、编号 8.5 ⇒ 是子节，错的是标题级别。**
文档已有 `### 6.8b` 先例，改成 `###` 一致。

#### 五、日志头部提交号落后 100+ 轮

```
头部：84439686    实际 HEAD：f40c14dd
```

**改成"当前提交"+"首次上板"两行 —— 历史保留，当前可复测。**

#### 六、第 27 项检查（两项，都可反向验证）

```
[PASS] doc section numbers unique      no duplicate top-level section numbers
[PASS] log header quotes current HEAD  header says f40c14dd, HEAD is f40c14dd
```

```
# 8.5 改回 ##  → [FAIL] runbook has two sections numbered 8
# 头部改回旧提交 → [FAIL] header says 84439686, HEAD is f40c14dd
```

#### 七、本轮无固件改动

`s31test` 仍 35/35。`check_rtt_style.py` 25 → 27 项。

#### 八、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R291** | **"已知限制"表会变成"已知过时"** | 已记录 |
| **R292** | **逐行核对，不要整体相信**：7 行里 2 行错 | 已记录 |
| **R293** | **章节号重复让交叉引用失效** | 已记录 |
| **R294** | **读者看到的第一行最要紧** | 已记录 |
| **R295** | **历史与当前分两行写** | 已记录 |

### 12.99 W93（2026-11-01）：PCNT 绕过了 RT-Thread 现成的设备类

#### 一、被推翻的断言

```
pcnt.c:4        "RT-Thread has no pulse-counter class"
port_pcnt.h:7   "RT-Thread does not model pulse counters"
```

**实际：**

```
components/drivers/include/drivers/pulse_encoder.h
components/drivers/misc/pulse_encoder.c      (135 行)
Kconfig: config RT_USING_PULSE_ENCODER       (default n)
```

**而且有 `#ifndef RT_USING_DEVICE_OPS` 分支 —— 正是本端口配置。**

#### 二、改之前先修一个真 bug

```
pin 4-7, 16-20, 30, 40, 60  -> counted 20   PASS
pin 8-15, 50               -> counted 0    FAIL
```

**同一代码、同样脉冲宽度，按引脚分段成败。**

`cmd_pcnt` 只 `gpio_ll_output_enable()`，而 `s31_pin_mode()` 还要：

```c
gpio_ll_set_output_signal_matrix_source(&GPIO, pin, SIG_GPIO_OUT_IDX, false);
gpio_ll_set_output_enable_ctrl(&GPIO, pin, false, false);
```

**少的两步决定"电平寄存器能不能到达焊盘"。修法：改用 `rt_pin_mode()`。**

**失败信息也改了**：`counted == 0` 不是"边沿太快"，指向输出路径。

修后 **9 个引脚全部 counted 20**。

#### 三、按框架契约注册设备

读源码确认（不猜）：

| 行为 | 结论 |
|---|---|
| `ops->clear_count` / `ops->control` | **不检查 NULL**，必须是真函数 |
| `ops->init` | 检查 NULL |
| `rt_pulse_encoder_read()` | **无条件写 4 字节**，不看 size |

按此注册 `pcnt0`（只 unit 0）。`rt_hw_pcnt_*` 保留为薄封装（mcpwm/rmt 当量具用）。

#### 四、验证

```
list device: pcnt0  Miscellaneous Device 0

[PASS] dev.pe   pcnt0 read/clear via the device API (was 0)
```

**自检 35 → 36，`dev` 组 7 → 8。**

#### 五、检查器抓到我两次

**(a) `runbook binary size is current` 连红两次：**

```
says 1,517,568 -> artifact 1,518,528   （加 PCNT 设备代码）
says 1,518,528 -> artifact 1,518,560   （给 dev.pe 补 ran++）
```

**每次改固件都会红 —— 这正是它该做的。**

**(b) `dev` 组报 `7/8`** —— `dev.pe` 有 4 个 `st_report` 但没 `ran++`。
**W87 建立的计数器抓到 W93 自己的疏忽。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R296** | **"上游没有这个类"是最该查的断言** | 已记录 |
| **R297** | **绕过的代价：自己写一套 + 少了框架契约** | 已记录 |
| **R298** | **失败信息指错方向会浪费整轮** | 已记录 |
| **R299** | **改设备类前先修功能 bug** | 已记录 |
| **R300** | **框架契约读源码，不猜** | 已记录 |
| **R301** | **自己加的计数器会抓到自己** | 已记录 |

### 12.100 W94（2026-11-01）：`clock_time` 的"代码没了"也是假的 —— 但确实不该用

#### 一、三条断言的核实

| 驱动 | 断言 | 核实 |
|---|---|---|
| `rmt.c` | 无 RMT 类 | ✅ 成立 |
| `mcpwm.c` | 无 motor-control 类 | ✅ 成立 |
| `gptimer.c` | "**代码没了**" | ❌ **假** |

**`clock_time/` 有 1294 行，`menuconfig RT_USING_CLOCK_TIME`。**

#### 二、实验：能编吗？

加宏 + 两个源文件 ⇒ `Project build complete`。
**但镜像里无符号（`--gc-sections` 全丢，无人引用）。**
**"能编"不等于"适合用"。**

#### 三、读源码：模型不匹配

| | `clock_time` | 本端口 TIMG |
|---|---|---|
| 用途 | 系统级**单点截止时间** | 4 个独立周期回调 |
| 参数 | `cnt` 个 tick | 频率 Hz |
| 回调 | 无（rx_indicate + 全局 event） | **每通道独立** |
| 实例数 | **1（`_clock_timer_owner` 单属主）** | 4 |

**强行把 TIMG 注册成属主会改变 `rt_clock_time_set_timeout()` 的全局语义。**

#### 四、处理：说清哪一半对

**已回滚实验。注释改成"`clock_time` 存在但不是通用定时器类，
单属主 deadline 模型与本驱动的多通道周期回调不匹配"。**

**"代码没了"假；"没有通用硬件定时器设备类"真。**

#### 五、核心判断：能编 ≠ 该用

| 轮次 | 外设 | 上游有类吗 | 结论 |
|---|---|---|---|
| W93 | PCNT | 有，模型**匹配** | **该用 → 改了** |
| W94 | TIMG | 有，模型**不匹配** | **不该用 → 不改** |

**判据是模型是否匹配，不是"上游有没有"。**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R302** | **"代码没了"和"没有这个类"是两种断言** | 已记录 |
| **R303** | **能编 ≠ 该用** | 已记录 |
| **R304** | **模型匹配才是判据** | 已记录 |
| **R305** | **强行套用会改变系统语义** | 已记录 |
| **R306** | **同一表象可有两种相反结论** | 已记录 |

### 12.101 W95（2026-11-01）：193 行的命令在磁盘上活了 20 多轮，从没编进镜像

#### 一、差集只有一个文件

```
NOT COMPILED: s31-rtt-test/main/s31wifiscan.c
```

**193 行，完整的 msh 命令，`help` 里从来没有它。**

#### 二、怎么消失的

W59/W60 回归二分："去掉 s31wifiscan.c → 恢复正常"。
**但同一节下一段就推翻了它：**

> 70.5 但真相不是 s31wifiscan

**真因是 CPU1 调 `rt_system_scheduler_start()` 的竞态。**
**文件没放回去，20 多轮无人发现。**

#### 三、它回答的问题仍然重要

```
s31wifiscan: 18 AP(s), all on channels 1..14 = the 2.4 GHz band
```

**`s31wifi` 不扫描。M6 的原始问题只有它能回答。**

#### 四、放回 + 第 28 项检查

`main/CMakeLists.txt` 已是 `WHOLE_ARCHIVE`，注释"只有两个"改成三个，
并写明它是被误判移除的。

**第 28 项：磁盘上的 .c 必须被编译**

```
[PASS] every port source is compiled
反向验证: [FAIL] on disk but not in the build: ['s31wifiscan.c']
```

**这是第 9 项往前追问一步：**
**9 问"留在镜像里吗"，28 问"编译了吗"。**

#### 五、命令数 33 → 34

```
544 - 528 = 16 B，正好一条
```

#### 六、检查 26 又当场红

```
says 1,518,560 B, artifact 1,525,744 B    （加回 193 行 = +7 KB）
```

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R307** | **"磁盘上有" ≠ "编进去了"** | 已记录 |
| **R308** | **二分法"去掉就好了"不等于"它是原因"** | 已记录 |
| **R309** | **推翻结论后要回头收拾** | 已记录 |
| **R310** | **能用的工具被误删比没有更糟** | 已记录 |
| **R311** | **检查要分层："编译了吗" vs "留在镜像里吗"** | 已记录 |

### 12.102 W96（2026-11-01）：板子停在 `waiting for download` —— 自动复位电路

#### 一、误判方向

**刚改过固件（W95 加回 193 行），所以先怀疑镜像。**
**错了。**

#### 二、让独立工具说话

```
esptool chip-id
Connected to ESP32-S31 ... MAC: 1c:29:04:d0:14:1e
```

**芯片、串口、供电全部正常。一步排除一大片。**

#### 三、真因

**关串口 3 秒再开 + 写一个字节：**

```
rst:0x1 (POWERON),boot:0x58 (SPI_FAST_FLASH_BOOT)
RT-Thread 5.4.0 on ESP32-S31
msh >
```

**`boot:0x58` = 正常 flash 启动。**

**串口关闭时 DTR/RTS 都低 → 经反相缓冲 = "IO0 低 + EN 释放" → 上电即进 ROM 下载模式。**
**ROM 在等同步帧，所以"只读不写"永远 0 字节。**

#### 四、第二个坑：写太快复位

| 字符间隔 | 结果 |
|---|---|
| 0–20 ms | 启动日志 |
| **40 ms** | **命令执行** |

**CH343 线驱动器在连续写时扰动 EN。固件改不了。**

#### 五、`board.py` 修正

1. **逐字符 45 ms 发送**
2. **回复是启动日志时明确报 `--- the board rebooted ---`**

#### 六、自己挖的坑：就绪判据用了累积文本

```python
text += window
if "msh >" in text:      # 错：第一次启动日志里就有
```

**改成只看当前窗口且不含 `ESP-ROM`。**
**"就绪" = "现在能接受输入"，不是"曾经打印过提示符"。**

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R312** | **"上电不启动"先怀疑线路，不先怀疑刚改的代码** | 已记录 |
| **R313** | **让独立工具说话，一步排除一大片** | 已记录 |
| **R314** | **"0 字节"和"有输出"是两种故障** | 已记录 |
| **R315** | **"曾经打印过" ≠ "现在能接受输入"** | 已记录 |
| **R316** | **把判据写进工具，不只写进笔记** | 已记录 |

### 12.103 W98（2026-11-01）：上一轮的因果判断是错的 —— 更正

#### 一、用户一句话

**"我的串口助手开了 dtr..."**

#### 二、真机制（一个原因，两个症状）

| 症状 | 机制 |
|---|---|
| `boot:0x48` + `waiting for download` | 串口助手置 DTR 有效 → 反相 → **IO0 低** → 进 ROM 下载模式 |
| `PermissionError(13)` | **同一程序占着 COM16** |

#### 三、我错在哪（三层）

| 层 | 错处 |
|---|---|
| 事实 | 端口不是"没被打开"，是**被别的程序打开了** |
| 方向 | 关闭的端口**释放** DTR/RTS，不是拉住 |
| **后果** | **把人引向"换板子/换线"，而不是"关掉串口助手"** |

#### 四、第二个错误：给没依据的值编解释

**我说 `0x78` / `0x7f` 也是"正常 flash 启动，高位是 SPI 模式位" —— 无依据。**

**实测稳定 `0x58` 只有"两线置高 + 真正复位"这一条路径。**

**⇒ 把"结果看起来正常"当成了"这个值也是正常的解释"。**

#### 五、修正

1. **`reset.py` 自己判 `0x48` / 非 `0x58` 并明确报**
2. **端口打不开时说不清原因，不抛堆栈**
3. **两个工具都加了 pyserial 缺失提示；系统 Python 已装 pyserial 3.5**
4. **runbook §93 标题更正 + §94 整节重写**

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R317** | **"设备不工作"先问"还有谁在用它"** | 已记录 |
| **R318** | **关闭的端口释放线，不是拉住线** | 已记录 |
| **R319** | **错误的因果判断会让人改不该改的东西** | 已记录 |
| **R320** | **"结果看起来正常" ≠ "这个值正常"** | 已记录 |
| **R321** | **旧章节的标题本身也是结论** | 已记录 |
| **R322** | **工具缺依赖要说"装什么"** | 已记录 |

### 12.104 W99（2026-11-02）：Wi-Fi 连上并拿到 IP；CPU1 调度按证据回滚

#### 一、M6 达成：真实 AP + DHCP

```
s31wifi: PASS, connected to "刻于晴空之上_2.4G" in 1380 ms
s31wifi: ip 10.0.0.119 mask 255.255.255.0 gw 10.0.0.1
```

**三次全新启动：1380 / 1340 / 2000 ms。**

**关键实现点：**

| 点 | 为什么 |
|---|---|
| `IP_EVENT` 单独注册 | 只注册 `WIFI_EVENT` 会在连接成功后**永远收不到 GOT_IP** |
| `esp_wifi_connect()` 放 `STA_START` 之后 | 提前调返回 `WIFI_NOT_STARTED` |
| `authmode` 按密码是否为空 | 空密码配 WPA2 → `AUTH_EXPIRE`，看着像密码错 |
| `ip_info` 整个拷贝 | `data` 只在回调期间有效 |
| 重试有界（3 次） | 密码错时几秒内报错，不等满超时 |

#### 二、CPU1 调度：三个真发现，一个未解释

| 尝试 | 结果 | 根因 |
|---|---|---|
| 1 | `rst:0x8 HP_WDT1_RESET` 循环 | CPU1 在 CPU0 有系统前调度 → 就绪表空 → `RT_ASSERT(to_thread)` |
| 2 | 绑定 CPU1 的线程一次没跑 | **CPU1 没有自己的 tick** |
| 3 | `rst:0x10 CHIP_LP_WDT_RESET` | **未解释** |

#### 三、`vSystimerSetup()` 为什么不能用（两条独立原因）

1. `for (cpuid = 0; cpuid < configNUM_CORES; ++cpuid)` 且本镜像
   `CONFIG_FREERTOS_UNICORE=y` ⇒ 只配 alarm 0，**alarm 1 永远不会到期**
2. handler 用 `xPortGetCoreID()` 索引 `s_handled_systicks[configNUM_CORES]`（一元数组）→ **越界写**

#### 四、SYSTIMER LL 为什么也不能用

`hal/systimer_*.h` 由 `esp_hal_systimer` 通过 target 条件化 `INCLUDE_DIRS` 导出，
**加进 `PRIV_REQUIRES` 后 `project_description.json` 里 `priv_reqs` 仍是
`['freertos','riscv']`** —— 依赖没生效。

#### 五、最终用端口自己的 TIMG 通道 3

`s31_timer_start(3, RT_TICK_PER_SECOND, cb, NULL)`，
`esp_intr_alloc()` 把中断路由到调用它的核。这条路 `s31timer` 已验过。

**仍然失败 ⇒ 回滚，诊断写进 `rtconfig.h`。**

#### 六、净成果

| 项 | 状态 |
|---|---|
| IPI 接收侧 | ✅ **已装好，`s31intr` INUM 28 从无处理器变为有** |
| `smp_tick.c` | ✅ 保留在开关后面 |
| 两个 bug 根因 | ✅ 写进注释 |
| CPU1 调度 | ❌ 关闭，`s31smp` 回到已知良好状态 |

#### 七、检查器两个真修复

**(a) 文本扫描器看不见 `#ifdef`** → 加守卫求值器（跟踪 `#ifdef`/`#ifndef` 栈）。
**双向反向验证：开关打开 FAIL / 去掉守卫 FAIL ⇒ 没把检查变瞎。**

**(b) 第 2 项太窄**：只接受"导出"或"从 board.c 调用"。
tick 从 `smp_slave_sched.c` 调用 ⇒ 误报。改成"被任何端口源文件调用"。

#### 八、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R323** | **能编 ≠ 语义对；单核宏会阉割多核路径** | 已记录 |
| **R324** | **加了依赖不等于头文件可见** | 已记录 |
| **R325** | **未解释的复位不能当"差不多能跑"** | 已记录 |
| **R326** | **文本扫描器必须理解 `#ifdef`** | 已记录 |
| **R327** | **检查措辞窄了会把正确做法报成错误** | 已记录 |
| **R328** | **失败信息要区分"没连上"与"连上没 IP"** | 已记录 |

### 12.105 W100（2026-11-02）：IDF 的双核启动、差集、以及"ticks=0"这个决定性测量

#### 一、IDF 的三段式（读源码）

| 阶段 | 函数 | 做什么 |
|---|---|---|
| CPU0 | `start_other_core()` | unstall / 时钟与复位 / 启动地址 / **等 CPU1 报到** |
| CPU1 | `call_start_cpu1()` | gp / **mtvec+mtvt** / 清中断矩阵 / 报到 / 等 CORE 阶段 / `SYS_STARTUP_FN()` |
| CPU1 | `esp_startup_start_app_other_cores()` | 等 CPU0 调度器 / 中断看门狗 / **核间接收** / `xPortStartScheduler()` |
| 每核 | `xPortStartScheduler()` | **`vPortSetupTimer()` + `set_threshold()` + `global_enable()`** |

**⇒ IDF 的关键是每一项都按核做一遍。**

#### 二、差集逐项核对

| 项 | IDF | 本端口 | 状态 |
|---|---|---|---|
| gp / 启动地址 | ✅ | ✅ | 有 |
| mtvec / mtvt | ✅ | ✅ | 有 |
| per-CPU 中断栈 | ✅ | ✅ | 有 |
| 等 CPU0 就绪 | ✅ | ✅ `s31_cpu1_go` | **本次补** |
| CPU1 自己的 tick | ✅ | ✅ TIMG ch3 | **本次补** |
| **CPU1 的 CLIC 阈值** | ✅ 每核 | ❌ 只在 CPU0 | **本次补** |
| **CPU1 的 mstatus.MIE** | ✅ 每核 | ❌ | **本次补** |
| CPU1 核间接收 | ✅ | ✅ | **本次补** |
| CPU1 中断矩阵清理 | ✅ | ❌ | **未做** |

#### 三、决定性测量

```
smp: after go, cpu1 alive=0x5a5a5a5a ticks=0 cur=0x2f01fc34 idle=0x2f01fc34
```

| 字段 | 含义 |
|---|---|
| `alive` 已置 | CPU1 跑了我们的代码 |
| **`ticks=0`** | **300 ms @ 1 kHz，一次中断都没收到** |
| `cur == idle` | 进了调度器并切到 idle |

**⇒ 问题从"调度器"缩小到"中断投递"。两者修法完全不同。**

#### 四、核间通讯的答案

**机制已完成（收发两侧都在，`s31intr` INUM 28 已安装），
但"有用"取决于 CPU1 能收中断 —— 与调度是同一个前提。**

**注意：本端口的 IPI 走 CLIC `IP` 位，IDF 走 `HP_SYSTEM_CPU_INT_FROM_CPU_*`
寄存器（source 65/66）。两种都合法，但都未在 CPU1 上验证过。**

#### 五、下一步最小实验

**不做调度，只测中断：**

```
CPU0: rt_hw_ipi_send(RT_SCHEDULE_IPI, 1U << 1)
等 10 ms
打印 s31_cpu1_ticks 和一个新的 s31_cpu1_ipis
```

| 结果 | 下一步 |
|---|---|
| 收到了 | 查 TIMG 的矩阵映射 |
| 没收到 | 查 CPU1 的 mtvt / 矩阵 / 使能 |
| 收到但与 tick 无关 | 两条路分别修 |

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R329** | **读上游要读"每核都做什么"** | 已记录 |
| **R330** | **差集要逐项列表，补完没好说明差集不完整** | 已记录 |
| **R331** | **一个计数器把两个假设分开** | 已记录 |
| **R332** | **别在复位的板上调代码，先回滚** | 已记录 |
| **R333** | **"做完"和"有用"是两件事** | 已记录 |

### 12.106 W101（2026-11-02）：用的是 RT-Thread 的多核模型，IDF 的核从未启动

#### 一、结论（用镜像符号证明，不靠代码风格）

```
nm | grep -E "start_other_core|call_start_cpu1|esp_startup_start_app_other_cores"
（无输出）

nm | grep -E "rt_hw_secondary_cpu_up|rt_system_scheduler_start"
40019ea0 T rt_hw_secondary_cpu_up
2f0166fa T rt_system_scheduler_start
```

**IDF 那三个函数一个都不在镜像里 —— 被 `--gc-sections` 丢了。**

#### 二、为什么被丢

`esp_system/startup.c`：

```c
#if !CONFIG_IDF_RTOS_RTTHREAD
    esp_startup_start_app();       /* IDF 路径 */
#else
#if defined(__GNUC__) && defined(RT_USING_USER_MAIN) && CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE
    entry();                       /* ★ 本镜像 */
```

**⇒ CPU1 由 `rtthread_startup()` 里的 `rt_hw_secondary_cpu_up()` 拉起，
即 RT-Thread 的钩子，由端口 `smp_slave.c` 实现。**

#### 三、IDF 代码的价值：**补按核步骤**，不是抄实现

**`xPortStartScheduler()` 里那两行（阈值 + MIE）值得读，
因为 `rt_system_scheduler_start()` 里没有对应物，本端口只在 `board.c` 给 CPU0 设过。**

#### 四、核间通讯：RT-Thread 的抽象，机制已完整

| | RT-Thread | 本端口 |
|---|---|---|
| 发送 | `rt_hw_ipi_send` | ✅ |
| 接收 | `rt_hw_ipi_handler_install` | ✅ |
| 触发 | 内核 `rt_sched_unlock_n_resched()` | 自动 |

**IDF 的 `esp_crosscore_int_*` 与本端口无关（且本镜像 `configNUM_CORES=1`）。**

#### 五、又试一条，仍 ticks=0

**假设：`esp_intr_alloc()` 不使能中断，`rt_hw_interrupt_umask()` 只操作当前核窗口。**
**补 `esp_intr_enable(handle)`（在 CPU1 上调用）→ `ticks` 仍是 0。**

#### 六、已排除 5 项，剩 2 项

| 已排除 |
|---|
| 启动顺序、缺 tick 定时器、阈值/MIE、"已路由未使能"、调度器拒绝切换 |

| 剩余候选 | 验证方法 |
|---|---|
| CPU1 的 `mtvt`/`mtvec` 不生效 | CPU1 上故意读地址 0 |
| 矩阵把源路由到了 CPU0 | 读 `INTERRUPT_CORE1_*_MAP_REG` |

#### 七、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R334** | **"用了谁的方法"看镜像符号，不看代码风格** | 已记录 |
| **R335** | **照上游补的是"缺的步骤"，不是"抄实现"** | 已记录 |
| **R336** | **一次排除一个，全部记下来** | 已记录 |
| **R337** | **负结果同样要落地到代码注释** | 已记录 |

### 12.107 W102（2026-11-02）：CPU1 —— 六个量全对，中断仍不被服务

#### 一、阶段标记把范围切到几条语句

```
smp: cpu1 stage=3 tick_started=1 isr_runs=0 ticks=0 early=0 spin=0
```

| 字段 | 含义 |
|---|---|
| `tick_started=1` | `s31_timer_start()` 返回了 |
| `stage=3` | `rt_hw_secondary_cpu_tick_init()` 也返回了 |
| `isr_runs=0` | **ISR 一次都没进** |
| `spin=0` | **紧接着的 10 万次循环没跑** |

**⇒ 停止点：tick 初始化返回之后、下一步循环之前。**

#### 二、六个量全对

| 量 | CPU1 | 判定 |
|---|---|---|
| `mtvec` `0x2f000003` | = `_vector_table` | ✅ |
| `mtvt` `0x2f000040` | = `_mtvt_table` | ✅ |
| `mintthresh` `0x1f` | 阈值 0 | ✅ |
| `mstatus.MIE` 1 | 全局开 | ✅ |
| 矩阵 map 0 | 源 26 → 中断 0 | ✅ |
| 线 16 `0x3fc00101` | `IE=1` 且 **`IP=1` 挂起** | ✅ |

**`entry_count=1` ⇒ 不是被复位。**

#### 三、试过并排除

| 修法 | 结果 |
|---|---|
| CPU0 调 `umask` | IE 仍 0（当前核窗口） |
| CPU1 调 `esp_intr_enable` | 无变化 |
| CPU1 调 `umask(0)` | **IE=1**，ISR 仍不进 |
| tick 回调加 NULL 保护 | `isr_runs` 仍 0（没进 ISR） |
| `go` 标志顺序 | 修掉一个真 bug，但不是这个 |

#### 四、矛盾点

**CLIC 已经挂起（`IP=1`），核心既不响应也不崩溃。**
**最可疑的是 CLIC 向量模式：`mtvec` MODE=3，跳转走 `mtvt + 4*inum`，
INUM 0 的槽位若在 CPU1 上未初始化，就会跳到 0 或垃圾 —— 表现为"停止执行"。**

**下一步：读 `_mtvt_table[0]`，与 `_interrupt_handler` 地址比较。纯读取，零风险。**

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R338** | **计数器要放在函数第一条语句** | 已记录 |
| **R339** | **"全部正确但不工作"是有效结论** | 已记录 |
| **R340** | **排除的修法也要记录** | 已记录 |
| **R341** | **init 表检查报错是它该做的** | 已记录 |

### 12.108 W103（2026-11-02）：`spin=39449` —— 陷阱被取了，handler 没被调用

#### 一、新数字

```
smp: cpu1 stage=3 tick_started=1 isr_runs=0 ticks=0 early=0 spin=39449
smp: cpu1 mintstatus=0x00000000 mil=0x00 (cpu0 0x00000000 mil=0x00)
```

**`spin=39449`（上限 100000）⇒ 循环跑到 60% 才停。**

#### 二、为什么这一个数就定性了

| 事实 | 推论 |
|---|---|
| 循环跑了一部分 | CPU1 确实进到循环里 |
| 停在 60% | **不是确定性指令**（那条每次都会执行） |
| `isr_runs=0` | ISR 一次没进 |
| `mil=0` | 停止时不在 handler 上下文 |

**⇒ 中断被投递（`IP=1` 已测）、陷阱被取（否则循环会跑完），
但执行流没到 handler。**

**问题在"陷阱被取"到"调用 handler"之间：trap 入口或向量槽。**

#### 三、我自己引入的 panic

**`csrr %0, 0x346` —— 而 `csr_clic.h` 里 `MINTSTATUS_CSR = 0xFB1`。**
**`Illegal instruction`，`MCAUSE 0x2`，`addr2line` → `smp_tick.c:291`。**

**panic 报 `Core 0`：那条读取在打印函数里，跑在 CPU0 ——
诊断代码本身成了故障源。**

#### 四、下一个假设：向量槽错位

**`mtvec` MODE=3 ⇒ 跳 `mtvt + 4*(mcause-16)`。
中断 0 取 `_mtvt_table[0]`，而 `vectors_clic.S` 里索引 0 是 `_system_int_handler`，
`_interrupt_handler` 在索引 16。**

**若 `_system_int_handler` 在 CPU1 上不工作，现象正好是"陷阱被取、handler 没调用"。**

**验证：读 `_mtvt_table[0]` / `[16]` 与 `_interrupt_handler` 比较。**

#### 五、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R342** | **部分完成的计数器比 0 信息量大** | 已记录 |
| **R343** | **同一现象两种解释时必须重测** | 已记录 |
| **R344** | **诊断代码本身会 panic** | 已记录 |
| **R345** | **"陷阱被取但 handler 未调用"是独立结论** | 已记录 |

### 12.109 W104（2026-11-02）：**双核跑通** —— 根因是 CPU1 的 mstatus.FS=0

#### 一、根因

**trap 入口在 `__riscv_flen==32` 时无条件 `fsw f0..f31`（`vectors_rtthread.S:113`），
其注释假设 "FS 在所有可运行上下文里都非零"。**
**这对线程成立（`rt_hw_stack_init_core` 用 `0x7880`），对 CPU1 不成立：**

```
CPU0 mstatus = 0x80006088   FS = 3
CPU1 mstatus = 0x00000009   FS = 0
```

**第一条 `fsw` = 非法指令 → 核心死在自己的陷阱处理里。**

#### 二、定位路径

| 步骤 | 结论 |
|---|---|
| 读 `_mtvt_table[0]/[16]/[26]` | 槽 16 = `_interrupt_handler`，**向量槽排除** |
| 关 tick 做对照 | **循环跑完 100000，板子稳定** ⇒ tick 是触发者 |
| 两次编译 `spin` 近似相同 | 在"第一个可投递点"被触发（线已挂起） |
| 对比两核 mstatus | **FS 差异** |

#### 三、修法

```c
__asm__ volatile ("csrs mstatus, %0" ::
                  "r"((rt_uint32_t)((3u << 13) | (3u << 11))) : "memory");
```

**修完：`isr_runs=316 ticks=314 spin=100000`，`cpu1probe ran on cpu 1`。**

#### 四、双核暴露的两个测试 bug

| bug | 现象 | 修法 |
|---|---|---|
| `st_ctx` worker 未绑核 | CPU 密集线程迁移 → 卡死 | 绑到 CPU0 |
| `st_preempt` 先清 `running` 再 delete | 双重释放（两种 panic） | **worker 改 `while(1)`，只能被 delete** |

**重排 delete 顺序治不了竞争，必须消除返回路径。**

#### 五、验证

| 检查 | 结果 |
|---|---|
| `s31test`（双核开启） | ✅ 36/36，连跑 3 次 |
| `s31smp` | ✅ `SCHEDULING` |
| 两个检查器 | ✅ 28/28 + 9/9 |

已保存 `w105_dualcore_verified.bin`。

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R346** | **关掉一个变量做对照比加探针更快** | 已记录 |
| **R347** | **注释里的假设要逐个核对适用性** | 已记录 |
| **R348** | **双核会暴露单核下"通常赢"的竞争** | 已记录 |
| **R349** | **重排顺序治不了竞争，要消除路径** | 已记录 |

### 12.110 W105（2026-11-02）：Wi-Fi 与双核的死因查明并修好

#### 一、panic 指向 flash，不是 WiFi

```
MEPC 0x2f00ca02 -> spimem_flash_ll_set_cs_pin
RA   0x2f017a8c -> spiflash_start_core
MCAUSE 0x19 (cache error)
```

#### 二、从产物证明机制

**反汇编 `spi_flash_disable_interrupts_caches_and_other_cpu()`：6 条指令，
无 `esp_ipc_call_nonblocking`、无调度器分支、无跨核握手。**
**`esp_ipc_call_*` / `spi_flash_op_block_func` 在 ELF 里不存在**
**（`CONFIG_FREERTOS_UNICORE=y` + `CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y`）。**

#### 三、对照实验

| 配置 | WiFi | CPU1 |
|---|---|---|
| 调度 + tick | ❌ cache error | 调度 |
| 调度，无 tick | ✅ 连上拿 IP | 抢不动 idle |
| 无调度 | ✅ | 停核 |

**⇒ 触发者是 tick：ISR 在 IRAM（`s31_timg_isr` @ `0x2f002d9c`），
但它调的 `rt_tick_increase()` 在 flash（`0x400116a4`）。**

#### 四、修法

**`spi_flash/cache_utils.c` 里加两个 weak 钩子（`spi_flash_other_core_stop/resume`），
端口提供强定义：**

```c
/* CPU0 */
s31_flash_ack = 0; s31_flash_op = 1;
while (s31_flash_ack == 0 && spins < 200000) spins++;

/* CPU1 的 tick ISR 第一条语句之后 */
if (s31_flash_op) { s31_flash_ack = 1; return; }   /* 不碰 flash */
```

**两个变量在 DRAM；握手在 ISR 最前面，所以应答之后不会再取 flash 指令。**

#### 五、结果

```
s31wifi: PASS, connected to "刻于晴空之上_2.4G" in 1160 ms
s31wifi: ip 10.0.0.119 mask 255.255.255.0 gw 10.0.0.1
cpu1   : SCHEDULING (a bound thread is running there)
=== 36 passed, 0 failed ===   （连跑 3 次）
```

已保存 `w106_dualcore_wifi.bin`。

#### 六、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R350** | **panic 的 MEPC 比错误文本更有信息** | 已记录 |
| **R351** | **反汇编一个函数就能证明"这半边被编译掉了"** | 已记录 |
| **R352** | **"某个东西在跑"要问"它跑的代码在哪"** | 已记录 |
| **R353** | **跨核握手要放在被测 ISR 最前面** | 已记录 |
| **R354** | **钩子用 weak，组件不反向依赖端口** | 已记录 |

### 12.111 W106（2026-11-03）：USB 方案选型与端点实测

#### 一、选 CherryUSB

| | legacy usb | cherryusb |
|---|---|---|
| 控制器驱动 | **全树无 DWC/OTG/EHCI/OHCI** | **`port/dwc2/`** |
| ESP 胶水 | 无 | `usb_glue_esp.c` |
| RT-Thread OSAL | 内置 | `osal/usb_osal_rtthread.c` |
| 设备类 | 7 | 20+ |

**legacy 需要 BSP 自己写 `usb_dc_*`；CherryUSB 直接带 DWC2 设备+主机驱动，
且其 `arch == GHWCFG2_INT_DMA_ARCH` 断言与本核实测 `arch=2` 相符。**

**IDF 这个 fork 里没有 `esp_tinyusb`/`tinyusb`，TinyUSB 要自拉依赖。**

#### 二、端点能力（实测 `s31usb`）

```
s31usb: device endpoints (EP0 included) : 16  (7 IN, 0 periodic IN)
s31usb: endpoint directions             : 0 bi ... 15 bi
s31usb: DMA architecture               : 2 (internal DMA)
s31usb: total FIFO 896 words = 3584 bytes
s31usb: isochronous endpoints           : 0 (no UAC / UVC streaming)
```

| 项 | 值 |
|---|---|
| 端点数 | **16（含 EP0）** |
| 方向 | 全双向 |
| IN 上限 | 7（数量，非方向限制） |
| **等时端点** | **0 ⇒ UAC/UVC 不可行** |
| 物理口 | 1 ⇒ **同时只能是一个 USB 设备**，但可做复合设备 |

**CDC+MSC+HID = 6 个非零端点，15 个可用 ⇒ 端点不是瓶颈。**

#### 三、移植到 S31 还差三处

| 位置 | S31 应为 |
|---|---|
| `GET_USB_INTR_SOURCE` | `ETS_USB_OTGHS_INTR_SOURCE`（99；`ETS_USB_INTR_SOURCE` 不存在） |
| `GET_USB_PHY_TARGET` | `USB_PHY_TARGET_UTMI`（无 FSLS PHY） |
| `dwc2_get_user_params` | 加 S31 分支 → `param_hs` |

**另：`usb_osal_rtthread.c:16` 用 `rt_thread_create()` 不绑核，按本轮 WiFi 教训需绑 CPU0。**

#### 四、风险登记

| # | 风险 | 状态 |
|---|---|---|
| **R355** | **能力问题去问芯片，不要问文档** | 已记录 |
| **R356** | **全 0 一定是没使能，不是"没有"** | 已记录 |
| **R357** | **"能当几个设备"要拆成端口数/端点数/复合能力** | 已记录 |
| **R358** | **没有等时端点是硬限制，不是驱动问题** | 已记录 |
