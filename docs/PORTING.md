# 移植说明（PORTING.md）

这份文档解释 **ESP32-S31 上的 RT-Thread 端口是怎么做的**，以及**为什么这么做**。
它面向要修改、扩展或审查这个端口的人。

如果你只是想把它跑起来，看 `README.md` 就够了。

---

## 1. 整体结构

```
ESP-IDF v6.1
│
├── components/rtthread/           ← 替换 FreeRTOS 内核的组件
│   ├── CMakeLists.txt             把 RT-Thread 源码和端口源码编进来
│   ├── linker.lf                  链接片段（中断嵌套表、栈顶表等）
│   ├── port/
│   │   ├── common/                与目标无关的 glue
│   │   │   ├── board.c            rt_hw_board_init
│   │   │   ├── context_gcc.S      协作式上下文切换
│   │   │   └── ...
│   │   ├── esp32s31/              ★ S31 专属：全部原创代码
│   │   ├── freertos/              FreeRTOS API 兼容层
│   │   └── esp32c3, esp32c6/      其他目标
│   └── rtthread_stack_init.c      栈帧构造
│
└── rt-thread/                     ← 未修改的上游 5.4.0
    ├── src/                       kernel
    ├── include/
    ├── components/                finsh, drivers/core, drivers/serial, ...
    └── libcpu/risc-v/             RISC-V 通用原子操作
```

**关键设计选择：上游零修改。**

`rt-thread/` 与官方 5.4.0 逐字节相同（`git status` 干净）。
所有 S31 相关的东西都在 `components/rtthread/port/` 里。
这意味着升级 RT-Thread 只需要替换 `rt-thread/` 目录。

---

## 2. 启动流程

ESP-IDF 的启动流程被接管在 `esp_system/startup.c`：

```c
#if defined(__GNUC__) && defined(RT_USING_USER_MAIN) && CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE
    entry();                    // ← RT-Thread 的入口
#else
    ... FreeRTOS 路径 ...
#endif
```

`entry()` 是 RT-Thread 的 `components.c`，它依次调用：

```
rtthread_startup()
  ├── rt_hw_local_irq_disable()
  ├── rt_hw_board_init()
  │     ├── 时钟、堆、控制台
  │     ├── INIT_BOARD_EXPORT 阶段的驱动
  │     └── rt_system_heap_init(...)
  ├── rt_show_version()
  ├── rt_system_timer_init()
  ├── rt_system_scheduler_init()
  ├── rt_application_init()        → main_thread_entry → main()
  ├── rt_system_signal_init()
  └── rt_system_scheduler_start()  永不返回
```

### 为什么要开 `CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y`

这看起来和"双核"矛盾，其实不是。

这两个宏描述的是 **FreeRTOS 是否在多个核上调度**：

```
CONFIG_FREERTOS_UNICORE=y
CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y
```

它们让 IDF 相信"只有一个核在跑调度器"，于是 IDF 不调用 `start_other_core()`，
把第二个核留给 RT-Thread 自己启动。

**实测证据**（`nm` 查镜像）：

```
start_other_core                 不存在
call_start_cpu1                  不存在
esp_startup_start_app_other_cores 不存在
rt_hw_secondary_cpu_up           存在  ← 我们自己的
```

如果把它们设成 `n`，`startup.c` 会走另一条 RT-Thread 分支（`entry()` 不被调用），
**整个启动流程就不一样了**。所以这两个宏必须保持 `y`。

同时 `app_startup.c` 有一个编译期检查：`FREERTOS_UNICORE` 和
`SINGLE_CORE_MODE` 不一致会 `#error`（AMP 不支持）。两者必须相同。

---

## 3. 中断

### CLIC

S31 用的是 **CLIC**（Core-Local Interrupt Controller），不是 PLIC。

| 概念 | 值 |
|---|---|
| 中断线号 | `INUM + 16`（`RV_EXTERNAL_INT_OFFSET`） |
| 控制寄存器 | `CLIC_INT_CTRL_REG(i) = DR_REG_CLIC_CTRL_BASE + i*4` |
| 另一位核的窗口 | `+ DUALCORE_CLIC_CTRL_OFF (0x10000)` |
| 控制字 | bit0 `IP`（挂起）、bit8 `IE`（使能）、bit16 `SHV`（向量）、bit31:24 优先级 |
| 阈值 | `mintthresh`（CSR `0x347`），**每核独立** |
| 向量表 | `mtvt`（CSR `0x307`） |

### 三个必须知道的事实

**(a) `CLIC_INT_CTRL_REG` 指的是"当前核"的窗口。**

从 CPU0 调 `rt_hw_interrupt_umask()`，写的是 CPU0 的窗口。
要让 CPU1 的某条线使能，**必须在 CPU1 上调用**，或者显式加 `DUALCORE_CLIC_CTRL_OFF`。

**(b) `mintthresh` 是每核的。**

CPU0 的阈值改了不影响 CPU1。IDF 在每个核的 `xPortStartScheduler()` 里都设一次，
这个端口也必须如此。

**(c) CLIC 有硬件嵌套（`SOC_INT_HW_NESTED_SUPPORTED=1`）。**

同一优先级不能互相抢占。默认所有线都是 1 级，所以默认没有嵌套。
要嵌套，在 `rt_hw_interrupt_install()` **之前**调 `rt_hw_interrupt_set_priority()`。

### 陷阱入口

`components/riscv/vectors_rtthread.S` 提供 `_interrupt_handler`：

```asm
_interrupt_handler:
#ifdef RT_FPU_FRAME
    addi sp, sp, -32 * FPREG_BYTES      ; 先留 f0..f31 的位置
#endif
    addi sp, sp, -32 * REGBYTES         ; 再留 32 个整数寄存器
    STORE x1,  1*REGBYTES(sp)
    ...
```

**`RT_FPU_FRAME` 的开启条件是工具链的 `__riscv_flen`。**
S31 的 `-march` 带 `f2p2`，所以 `__riscv_flen == 32`，
`RT_FPU_FRAME` **总是开启**，陷阱入口**无条件保存 f0..f31**。

> **这是双核调试中最难找的一个坑。**
> `fsw` 在 `mstatus.FS == 0` 时是非法指令。
> 线程帧由 `rt_hw_stack_init_core()` 用 `mstatus=0x7880` 构造，所以线程里 FS 总是 3。
> 但 **CPU1 从复位进入自己的入口代码时 FS 是 0**——
> 它在第一条 `fsw` 上就死在自己陷阱处理里，表现是"陷阱被取、handler 没被调用、核心静默"。
> 修法见 §5。

---

## 4. 时钟与 tick

`rt_tick_increase()` 由 SYSTIMER 的 alarm 驱动：

```
SYSTIMER unit 1 → 1 kHz alarm → s31_timg_isr / systimer ISR → rt_tick_increase()
```

**为什么用 unit 1 而不是 unit 0：**
unit 0 是 `esp_timer` 拥有的，实测这块板子上它在 `esp_timer_early_init()` 之后
仍然读到 0（见 `s31test clock` 的注释）。unit 1 是 `vSystimerSetup()` 使能的那一个。

`RT_TICK_PER_SECOND` 必须等于 `CONFIG_FREERTOS_HZ`（都是 1000）。
很多 IDF 组件按 `CONFIG_FREERTOS_HZ` 换算毫秒，两者不一致会导致超时算错。

---

## 5. SMP 双核

这是整个移植里最复杂的部分。完整踩坑记录见 `SMP.md`，这里讲结构。

### 文件分工

| 文件 | 职责 |
|---|---|
| `smp_slave.c` | CPU1 入口、启动、寄存器基线 |
| `smp_slave_sched.c` | CPU1 进入调度器前的准备 |
| `smp_tick.c` | CPU1 的 tick、FPU/阈值/MIE 使能、flash 握手 |
| `smp_ipi.c` | 核间中断（`rt_hw_ipi_send` / `rt_scheduler_ipi_handler`） |
| `smp_prims.c` | 自旋锁（ticket lock，`amoadd.aqrl`） |
| `smp_istack.c` | 每核中断栈 |
| `smp_stack.c` | 中断嵌套表（`rt_irq_nest_slot`，按 `mhartid` 索引） |
| `smp_cmd.c` | `s31smp` 命令 |

### CPU1 的启动顺序（每一步都不能换）

```c
rt_hw_secondary_cpu_up()
  ├── 装 IPI handler          ← 必须最先，否则 CPU1 一启动就收不到核间中断
  ├── 时钟 / 复位释放
  ├── ets_set_appcpu_boot_addr(cpu1_entry)
  └── esp_cpu_unstall(1)
```

CPU1 入口里：

```c
cpu1_entry()
  ├── 设置 gp
  ├── esp_cpu_intr_set_ivt_addr(&_vector_table)
  ├── esp_cpu_intr_set_mtvt_addr(&_mtvt_table)
  ├── 等 s31_cpu1_go（由 INIT_APP_EXPORT 设置，此时 CPU0 调度器已在跑）
  ├── rt_hw_secondary_cpu_irq_enable()
  │     ├── csrs mstatus, (3<<13)|(3<<11)   ← FS=3, MPP=3  ★ 必须先做
  │     ├── esprv_int_set_threshold(...)
  │     └── csrsi mstatus, 8                ← MIE
  ├── rt_hw_secondary_cpu_tick_init()
  │     ├── s31_timer_start(ch3, 1000, cb, NULL)
  │     ├── rt_hw_interrupt_umask(0)
  │     └── 置 SHV（向量模式）
  ├── 从 mintthresh/mstatus/mintstatus 读回值存全局变量
  └── rt_hw_spin_lock(&_cpus_lock); rt_system_scheduler_start();
```

**为什么 `s31_cpu1_go` 要等到 `INIT_APP_EXPORT`：**

`rt_system_scheduler_start()` 在就绪表为空时会 `RT_ASSERT(to_thread)` 失败。
CPU1 被 `rtthread_startup()` 释放的时间点比空闲线程创建早，
所以必须等 CPU0 的调度器把两个空闲线程都建好之后才放行。

**实测**：不放行会得到 `rst:0x8 HP_SYS_WDT1_RESET` 循环。

### 三个必须设置的 CSR

| CSR | 值 | 不设会怎样 |
|---|---|---|
| `mstatus.FS` | 3 | 第一条 `fsw` 非法指令，核心死 |
| `mintthresh` | 0 | 所有中断被屏蔽，CPU1 永远不被抢占 |
| `mstatus.MIE` | 1 | 中断不投递 |

### CLIC 的 `SHV` 位（向量模式）

```
CPU0 line16 = 0x3fc10100   SHV(bit16)=1
CPU1 line16 = 0x3fc00101   SHV(bit16)=0   ← 默认没开
```

`SHV=0` 时硬件跳 `mtvec` 而不是 `mtvt + 4*id`，而 `_vector_table` 只有一条
`j _panic_handler`。**结果是中断进了 panic，不返回。**

所以 CPU1 的每条线在使能后必须显式置 `SHV`。

### flash 操作的跨核握手

**问题**：`spi_flash_disable_interrupts_caches_and_other_cpu()` 在这个构建里
只关了自己的 cache（反汇编确认：6 条指令，无 IPC），
因为 `CONFIG_FREERTOS_UNICORE=y` 把 IDF 的多核那半编译掉了。

于是 CPU0 关 cache 时 CPU1 还在跑，CPU1 的 tick handler 调 `rt_tick_increase()`
（在 flash 里）→ `MCAUSE 0x19` cache error。

**修法**：`spi_flash/cache_utils.c` 加两个 weak 钩子，端口提供强定义。

```c
/* CPU0，关 cache 前 */
spi_flash_other_core_stop();      /* 置标志，等 CPU1 应答 */
esp_intr_noniram_disable();
spi_flash_disable_cache(...);

/* CPU1 的 tick ISR，第一条语句之后 */
if (s31_flash_op) { s31_flash_ack = 1; return; }   /* 不碰 flash */
```

两个变量在 DRAM（cache 关了也能读），握手在 ISR 最前面
（应答之后 CPU1 不会再取 flash 指令）。

钩子用 `weak` 声明，组件不反向依赖 RT-Thread，单核构建照旧链接。

---

## 6. 内存

| 池 | 来源 | 大小 |
|---|---|---|
| RT-Thread 堆 | `CONFIG_IDF_RTTHREAD_HEAP_SIZE`，从内部 DRAM 切 | 128 KB |
| 中断栈 | `CONFIG_IDF_RTTHREAD_INTERRUPT_STACK_SIZE` | 40 KB（每核一份） |
| PSRAM | `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)` | 16 MB |

**为什么 PSRAM 用 `CAPS_ALLOC` 而不是 `USE_MALLOC`：**
RT-Thread 的堆是一块独立区域，`malloc()` 返回 PSRAM 会给出
RT-Thread 分配器看不见的指针。PSRAM 必须显式通过 `heap_caps_malloc` 拿。

---

## 7. FreeRTOS API 兼容层

`port/freertos/` 提供 IDF 组件需要的 FreeRTOS API，
让 `esp_timer` / `esp_event` / `esp_netif` / `lwip` / `esp_wifi` / `bt` 能在 RT-Thread 上跑。

| 文件 | 内容 |
|---|---|
| `ft_task.c` | 任务、调度器状态、TLS、退出钩子 |
| `ft_ipc.c` | 队列、信号量、互斥量、事件组 |
| `ft_notify.c` | 任务通知 |
| `ft_event.c` | `esp_event` 需要的部分 |
| `ft_query.c` | 查询类 API |

**设计约束：**

- **每个 shim 任务都绑到 CPU0。** `xTaskCreatePinnedToCore` 的"pin"在 RT-Thread 里
  原本不起作用（`rt_thread_create` 不绑核），IDF 组件不可跨核重入，
  所以 `ft_task.c` 显式 `rt_thread_control(..., RT_THREAD_CTRL_BIND_CPU, 0)`。
- **`xTaskGetSchedulerState()` 的 `NOT_STARTED` 不是装饰。**
  `esp_libc` 的锁层用它作为"还没开始加锁"的逃生口；如果永远不返回
  `NOT_STARTED`，CORE 阶段的第一把锁会走到 ISR 分支然后 abort。
- **`vTaskSuspendAll()` 用自己的计数器**，不用 `rt_critical_level()`。
  两者是不同的嵌套：`vPortEnterCritical()` 也会 `rt_enter_critical()`，
  用后者会把所有加临界区的驱动误报为 SUSPENDED。

---

## 8. 构建系统的一个坑

ESP-IDF 每个组件编成一个**归档**（`.a`），链接时 `--gc-sections`
会丢掉没有任何引用的成员。

上游 RT-Thread 习惯是"一个镜像编所有源文件"，所以 `INIT_*_EXPORT` 和
`MSH_CMD_EXPORT` 只靠 section 就够。在这里不够。

**表现**：源文件编译了，`.obj` 在，但符号不在 ELF 里。

**修法**：在 `CMakeLists.txt` 里给每个"没人引用"的符号加 `-u`：

```cmake
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u rt_hw_pin_init")
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u cmd_usb")
```

`-u` 把未定义引用种在归档被读之前，强制拉取那个成员。

**这条踩过三次**：`rt_hw_pin_init`、`s31_cpu1_report`、`cmd_usb`。

---

## 9. 验证方式

这个端口不靠"看起来对"来判断。每个结论都有对应的检查：

| 检查 | 工具 | 作用 |
|---|---|---|
| 36 项运行时自检 | `s31test` | 内核 + 外设，上板跑 |
| 9 项镜像检查 | `tools/check_image.py` | 链接产物：段数、栈帧大小、FS=3、表非空、初始化顺序 |
| 28 项代码规范 | `tools/check_rtt_style.py` | 编译完整性、死代码、文档时效、符号一致性 |

**`check_image.py` 的意义**：它能抓到"能编译但跑不对"的问题。
例如陷阱出口的 `mstatus` 里 `FS=0` 会导致第一次 `flw` 就 `MCAUSE=1` ——
这种问题编译期完全看不出来。

**每个检查器都必须能失败。** 加一条检查时要先故意破坏对应条件，
确认它真的报错，再恢复。做不到失败的检查器比没有更糟。

---

## 10. 如果你要移植到别的 S31 板子

改动通常只有这些：

1. `project/sdkconfig.defaults` —— flash 大小、PSRAM 模式、控制台 UART
2. `project/partitions.csv` —— 如果 flash 不是 16 MB
3. `port/esp32s31/board_extra.c` —— 板载 LED 的引脚和型号
4. `port/esp32s31/serial.c` —— 如果控制台不是 UART0

内核和端口本身与板子无关。

---

## 11. 如果你要移植到别的 ESP 芯片

这个组件已经支持 C3 / C6 / S31。加新目标的步骤：

1. 建 `port/<target>/`，至少要有 `rtconfig.h`、`cpuport.c`、`interrupt.c`
2. 在 `CMakeLists.txt` 的 `target_srcs` 分支里加进去
3. 确认 `-march` 是否带 FPU（决定 `RT_FPU_FRAME`），以及 `mtvec` 是 CLIC 还是普通模式
4. 单核目标可以完全跳过 `smp_*.c`

---

## 12. 一句话总结

**上游零修改 + 端口集中在 `port/esp32s31/` + 每个结论都有可复现的检查。**
