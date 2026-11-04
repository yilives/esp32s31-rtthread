# 双核（SMP）说明（SMP.md）

ESP32-S31 有**两个 RISC-V 核**。这个移植让**两个核都参与 RT-Thread 调度**。

```
msh > s31smp
cpus   : 2, running on cpu 0
cpu cur_thread idle_thread irq_nest tick  istack
  0   2f03eb40    2f01fc10        0  7974 0x2f03bd20
  1   2f01fcb4    2f01fcb4        0  7759 0x2f02a8c0
istack : distinct per core
cpu1   : SCHEDULING (a bound thread is running there)
```

两个核各有**独立的 tick、中断栈、中断阈值**，并且都在跑线程。

---

## 1. 用的是谁的多核模型

**RT-Thread 的，不是 IDF 的。**

证据是镜像里的符号：

```
$ nm s31_rtt_test.elf | grep -E "start_other_core|call_start_cpu1|secondary_cpu"
start_other_core                  不存在
call_start_cpu1                   不存在
esp_startup_start_app_other_cores 不存在
rt_hw_secondary_cpu_up            存在   ← 我们的
rt_system_scheduler_start         存在
```

IDF 的第二核启动代码被 `CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y` 编译掉了，
`--gc-sections` 又把剩下的丢掉。第二个核完全由 `port/esp32s31/smp_*.c` 启动。

**IDF 的代码只被用作参考**——参考它每个核在 `xPortStartScheduler()` 里做哪些
寄存器操作（阈值、MIE），这是 RT-Thread 自己没有、但对 CLIC 必需的部分。

---

## 2. 文件分工

| 文件 | 职责 | 关键点 |
|---|---|---|
| `smp_slave.c` | CPU1 入口、启动时序、寄存器基线 | IPI 必须先装 |
| `smp_slave_sched.c` | CPU1 进调度器前的准备 | 等 `s31_cpu1_go` |
| `smp_tick.c` | CPU1 的 tick、FS/MPP/阈值/MIE、flash 握手 | 最复杂的文件 |
| `smp_ipi.c` | 核间中断 | 收发两端 |
| `smp_prims.c` | 自旋锁（ticket lock） | `amoadd.aqrl` |
| `smp_istack.c` | 每核中断栈 | 按 `mhartid` 选栈顶 |
| `smp_stack.c` | 中断嵌套表 | 链接期常量 |
| `smp_cmd.c` | `s31smp` 命令 | 诊断入口 |

---

## 3. 启动顺序为什么不能换

```
rt_hw_secondary_cpu_up()
  1. rt_hw_ipi_handler_install()     ← 最先
  2. 时钟 / 复位释放
  3. ets_set_appcpu_boot_addr()
  4. esp_cpu_unstall(1)
```

**第 1 步必须最先**：CPU1 一启动就可能收到核间中断（例如 CPU0 的调度器要它
开始工作），没有 handler 就会进 `_panic_handler`。

CPU1 入口：

```
cpu1_entry()
  1. 设置 gp
  2. 装向量表（ivt + mtvt）
  3. 等 s31_cpu1_go
  4. rt_hw_secondary_cpu_irq_enable()   ← FS=3 必须先于任何陷阱
  5. rt_hw_secondary_cpu_tick_init()
  6. 读回 CSR 存全局变量（供 CPU0 打印）
  7. rt_hw_spin_lock(&_cpus_lock)
  8. rt_system_scheduler_start()
```

**第 3 步为什么等**：`rt_system_scheduler_start()` 在就绪表为空时
`RT_ASSERT(to_thread)` 失败。CPU1 被 `rtthread_startup()` 释放的时间点比空闲线程
创建早，所以必须等 CPU0 把两个空闲线程都建好（`INIT_APP_EXPORT` 阶段）。
不等会得到 `rst:0x8 HP_SYS_WDT1_RESET` 循环。

---

## 4. 三个必须设置的 CSR

### `mstatus.FS = 3`（FPU 状态）★ 最难找的坑

陷阱入口在 `__riscv_flen == 32` 时**无条件保存 f0..f31**：

```asm
#if defined(__riscv_flen) && (__riscv_flen == 32)
    FPSTORE f0, (32 * REGBYTES + 0 * FPREG_BYTES)(sp)
```

它的注释写着：

> mstatus.FS is non-zero in every context this entry can run on:
> rt_hw_stack_init_core() builds thread frames with mstatus=0x7880

**这个假设对 CPU0 和所有线程成立，对 CPU1 不成立：**

```
CPU0 mstatus = 0x80006088   FS(14:13) = 3
CPU1 mstatus = 0x00000009   FS(14:13) = 0
```

`fsw` 在 `FS=0` 时是非法指令。CPU1 在第一条 `fsw` 上死在自己的陷阱处理里。

**表现**：陷阱被取（代码停止前进），但 handler 一次都没进（`isr_runs=0`），
而且 `mintstatus.MIL=0`（没进 handler 上下文）。

**修法**：

```c
__asm__ volatile ("csrs mstatus, %0" ::
                  "r"((rt_uint32_t)((3u << 13) | (3u << 11))) : "memory");
```

用 `FS=3`（dirty）而不是 1：handler 是要**保存**寄存器，不是初始化。

### `mintthresh = 0`

**每核独立**。CPU0 设了不影响 CPU1。不设的话所有中断被屏蔽，
CPU1 的 tick 配好了也投递不到，绑定的线程永远不会被抢占。

### `mstatus.MIE = 1`

全局中断使能。

---

## 5. CLIC 的 `SHV` 位（向量模式）

CLIC 控制字 bit16 是 `SHV`：**是否硬件向量中断**。

```
CPU0 line16 = 0x3fc10100   SHV=1
CPU1 line16 = 0x3fc00101   SHV=0   ← 默认
```

`SHV=0` 时硬件跳 `mtvec` 而不是 `mtvt + 4*id`。
而 `_vector_table` 只有一条指令：

```asm
_vector_table:
    j _panic_handler
```

**结果：中断进了 panic，不返回。**

所以每条线在 CPU1 上使能后必须显式置 `SHV`：

```c
volatile rt_uint32_t *ctrl =
    (volatile rt_uint32_t *)(DR_REG_CLIC_CTRL_BASE + 16 * 4);
*ctrl |= CLIC_INT_ATTR_SHV;
```

`CLIC_INT_CTRL_REG` 是**当前核**的窗口，所以在 CPU1 上执行就写 CPU1。

---

## 6. flash 操作的跨核握手

### 问题

`spi_flash_disable_interrupts_caches_and_other_cpu()` 在这个构建里的反汇编是
**6 条指令**：

```
2f00052c  addi sp,sp,-16
2f000530  auipc/jalr -> spi_flash_op_lock
2f000538  jal        -> esp_intr_noniram_disable
2f00053c  li a0,0 ; li a1,&s_flash_op_cache_state
2f000546  jal        -> spi_flash_disable_cache(0, ...)
2f00054c  ret
```

**没有 `esp_ipc_call_nonblocking`，没有跨核握手。**
而且 `esp_ipc_call_*` 和 `spi_flash_op_block_func` 在整个 ELF 里都不存在。

原因：

```
CONFIG_FREERTOS_UNICORE=y
CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y
```

IDF 认为"只有一个核在跑调度器"，把多核那半编译掉了。

**后果**：CPU0 关 flash cache 时 CPU1 还在跑。CPU1 的 tick handler 调
`rt_tick_increase()`（在 flash 里，地址 `0x400116a4`）→ `MCAUSE 0x19` cache error。

**表现**：`s31wifi` 一跑就 `rst:0x1a CPU_LOCKUP_RESET`（或绑定 shim 任务后
变 `SW_CPU_RESET`）。

### 修法

`components/spi_flash/cache_utils.c` 加两个 weak 钩子：

```c
void IRAM_ATTR spi_flash_other_core_stop(void);
void IRAM_ATTR spi_flash_other_core_resume(void);
```

端口提供强定义（`smp_tick.c`）：

```c
void IRAM_ATTR spi_flash_other_core_stop(void)
{
    s31_flash_ack = 0;
    s31_flash_op = 1;
    while (s31_flash_ack == 0 && spins < 200000u) spins++;
}
```

CPU1 的 tick ISR，**第一条语句之后**：

```c
if (s31_flash_op) { s31_flash_ack = 1; return; }
```

三个设计要点：

1. **两个变量在 DRAM**，cache 关了也能读。
2. **握手在 ISR 最前面**，所以"看到标志"意味着 CPU1 已经不会再取 flash 指令。
3. **钩子用 weak**，组件不反向依赖 RT-Thread，单核构建照旧链接。

### 对照实验（证明触发者是 tick，不是"CPU1 在跑"）

| 配置 | Wi-Fi | CPU1 |
|---|---|---|
| 调度 + tick | ❌ cache error | 调度 |
| 调度，无 tick | ✅ 连上拿 IP | 抢不动 idle |
| 无调度 | ✅ | 停核 |

`RT_SMP_CPU1_NO_TICK` 开关就是为这个实验留的，现在是一个有用的诊断工具：
**它能一次区分"CPU1 跑代码导致的问题"和"CPU1 的 tick 导致的问题"。**

---

## 7. 核间中断（IPI）

**机制两端都完整：**

- 发送：`rt_hw_ipi_send()` → poke CLIC 的 `IP` 位（INUM 28）
- 接收：`rt_hw_ipi_handler_install()` → `rt_scheduler_ipi_handler()`

**和 IDF 的区别**：IDF 写 `HP_SYSTEM_CPU_INT_FROM_CPU_0/1_REG`（源 65/66），
这个移植戳 CLIC 的 `IP` 位。两者都合法，本移植的已在 CPU1 上验证。

---

## 8. 自旋锁

`smp_prims.c` 用 ticket lock：

```c
amoadd.w.aqrl %0, (1<<16), (%1)    /* next++，返回旧值 */
ticket = (tmp >> 16) & 0xFFFF
while ((slock & 0xFFFF) != ticket) pause
```

`owner` 在低半字，`next` 在高半字。

**踩过的坑**：把两半弄反会得到一个"无竞争时正常、有竞争时死锁或双持有"的锁。
注释里记录了这一点。

`pause` 用的是裸编码 `0x0100000f`——工具链默认 `-march` 不含 Zihintpause，
汇编器不认这个名字。

---

## 9. 中断栈

**一个共享栈是不够的。** 链接器只给一个中断栈（`__stack_end__`），
两个核同时在中断里会互相覆盖（栈向下生长）。

`smp_istack.c` 给 CPU1 一个 `.bss` 里的静态数组，然后用按 `mhartid` 索引的表
选栈顶：

```c
extern void *rt_irq_stack_top[2];   /* .data，链接期常量 */
```

**为什么不是 `void *const`**：const 数组会被放进 `.flash.rodata`，
而陷阱处理读它时 flash cache 可能关着——这正是这个移植修过两次的问题。

---

## 10. 验证双核

```
msh > s31smp
```

关键判据是这一行：

```
cpu1   : SCHEDULING (a bound thread is running there)
```

不是 `entered the scheduler but ran nothing`。

`cpu1probe ran on cpu 1, N loops in 300 ms` —— **CPU 编号必须是 1**，
这是"绑到 CPU1 的线程真的在 CPU1 上跑"的直接证据。

---

## 11. 如果你不想用双核

注释掉 `port/esp32s31/rtconfig.h` 里的：

```c
#define RT_SMP_CPU1_SCHEDULES
```

CPU1 会停在 `wfi` 循环里不参与调度。

**关掉之后：**
- `s31smp` 报 `parked`
- `s31test` 仍然 36/36
- 功耗略低

**开着也不影响的东西**：Wi-Fi（已修好握手）、BLE、所有外设。

**唯一需要留意**：自己写的线程如果是 CPU 密集型且不绑核，可能在两个核之间迁移。
需要固定就用 `rt_thread_control(tid, RT_THREAD_CTRL_BIND_CPU, (void *)0)`。

> **`s31test ctx` 曾经因为这个卡死。** 它的 4 个 worker 线程创建后没绑核，
> 在 MP 下可以在两个核之间迁移，导致测试挂住。
> 现在所有 worker 都绑到 CPU0——**这是测试自身的 bug，不是内核的**。

---

## 12. 一个测试自身的双重释放（双核才暴露）

`s31test preempt` 的注释写着：

> clearing `running` first lets them return from their entry functions, and a thread
> that has returned is already on its way to being freed by the idle thread. Calling
> rt_thread_delete() on that handle afterwards is a double free.

**而代码就是先清 `running` 再 `rt_thread_delete()`。**

单核时 delete 通常赢得竞争。双核后唤醒模式改变，worker 先返回，
idle 线程释放，再 delete 就是双重释放：

| 顺序 | panic |
|---|---|
| 先清 `running` 再 delete | `Store access fault` @ `rt_hw_spin_lock` |
| 先 delete 再清 `running` | `Load access fault` @ `rt_object_get_type` |

**重排顺序治不了**，因为竞争窗口还在。修法是**消除返回路径**：
worker 改成 `while (1)`，唯一的退出方式是 `rt_thread_delete()`。

---

## 13. 排障顺序（下次遇到类似问题）

1. **看 tick 计数**（`isr_runs`）—— 分"没被中断"与"中断了但没进 handler"
2. **关掉一个变量做对照** —— 比继续加探针快得多
3. **读两核的同一组 CSR 做 diff** —— `mtvec`/`mtvt`/`mintthresh`/`mstatus`/`mintstatus`
4. **读向量槽** —— `_mtvt_table[0]` 与 `_interrupt_handler` 比
5. **计数器要放在函数第一条语句** —— 放后面就分不清"没进"和"进了没算到"

**最有用的一个习惯**：panic 的 `MEPC` 用 `addr2line` 解出来，
比错误文本本身信息量大得多。
`Cache access error` + `MEPC -> spimem_flash_ll_set_cs_pin` 直接指向 flash，
而不是"Wi-Fi 有问题"。
