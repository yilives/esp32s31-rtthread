# ESP32-S31 + RT-Thread 上板手册

> **阅读提示**：本文档是开发过程的原始记录，其中的构建命令（如 idf_s31_env.sh、I:/RT_thread/...）是当时工作区的路径，**对本仓库不适用**。
> 要构建本仓库，请用 	ools/build.ps1，见 README.md。

> 目标读者：拿到板子后第一次上电的人。
> 本文只包含**可执行的操作**和**可判读的现象**，设计原理见
> `rtthread-esp32-s31-porting-plan.md`，踩坑过程见 `rtthread-esp32-s31-porting-log.md`。

---

## 0. 当前状态（务必先读）

| 项 | 状态（2026-11-01 实测） | 怎么复测 |
|---|---|---|
| 编译 | ✅ 通过 | `powershell -File tools\build.ps1 build` |
| 链接 | ✅ 通过 | 同上 |
| ELF 级校验 | ✅ **`check_image.py` 9/9** | `python tools\check_image.py` |
| 静态规范校验 | ✅ **`check_rtt_style.py` 24/24** | `python tools\check_rtt_style.py --strict` |
| 编译产物 | **1,533,824 B**（`0x176780`，1.46 MB） | `(Get-Item build\s31_rtt_test.bin).Length` |
| 应用分区 | **4 MB**（`partitions.csv` factory 槽），**已用 36.6%，剩 2.55 MB** | `python tools\check_image.py` |
| **运行时验证** | ✅ **全套自检 35/35 PASS** | `python tools\board.py COM16 "s31test"` |
| 硬件 | ESP32-S31 / COM16 / **16 MB flash** / 16 MB Octal PSRAM / 320 MHz / **双核** | `s31smp`、`s31stat` |

> 这张表曾经写着"898,688 B / 1 MB 分区 / 剩 147 KB / 20 项自检 / 单核 / 2 MB flash"——
> **六项全过时**。数字要么带复测命令，要么不写。

上板结论分两段：

1. **能启动**：首次烧写连续修 6 个启动路径缺陷才走到应用（§10），加第 7 个 FPU 帧缺陷（§11）。
   这七个全部位于"bootloader 跳转到应用第一行"这一段，没有一个能靠编译、链接或 ELF 检查发现。
2. **能用**：修完之后跑自检，又抓出 9 个缺陷（§12）——单位错、双重释放、判据写反、
   事件组语义、TIMG 自清零位、三处危险的输入校验。**第一个缺陷修完后才能看见第二个**，
   这是本轮最值得记住的经验。

所以：**"编译通过 + 链接通过 + ELF 检查通过"不是"能启动"，"能启动"也不是"能跑"**。
本文档 §6 的判读路径按实测重排过优先级，§10–§12 记录每个缺陷的真实报错。

### 0.1 怎么让板子说话（这一条每次都会踩）

打开串口**不等于**能看到输出：finsh 只在收到输入时才打印提示符，而这颗 CH343 的 EN/IO0
经过反相缓冲，**DTR/RTS 拉高是"运行"，置 False 是"按住复位"**。所以要么手动回车 + 复位一次，
要么直接用脚本：

```powershell
# 复位、读 banner、再执行命令（推荐）
python tools\board.py COM16 "s31test"

# 只读一次启动日志
python tools\board.py COM16
```

`tools/board.py` 里的复位序列就是唯一能产生干净复位的那一种：

```python
sp.rts = True; sp.dtr = True                     # 运行态
sp.dtr = False; sleep(0.2); sp.dtr = True        # 一次复位
```

---

## 1. 环境

一次性配置（已经做好，跳过即可）：

```bash
source <workspace>/idf_s31_env.sh
```

这个脚本做了四件在 Git Bash 下必须做的事，缺一个 `idf.py` 就起不来：

1. 清除 `MSYSTEM`（IDF 检测到 MSys/Mingw 会直接 fatal，而 bash 的 `unset` 传不到原生进程）
2. 补 `LOCALAPPDATA`（缺了会导致 ccache 版本校验失败，连锁跳过后续工具）
3. 把 IDF 托管的 Python 环境、`riscv32-esp-elf-gcc` 15.2.0、cmake 4.0.3、ninja 1.12.1 放进 `PATH`
4. 设置 `IDF_PATH` 与 `ESP_IDF_VERSION`

验证：

```bash
riscv32-esp-elf-gcc --version | head -1   # 期望 esp-15.2.0_20251204
idf.py --version                          # 期望 v6.1
```

---

## 2. 构建

```bash
source <workspace>/idf_s31_env.sh
cd project
idf.py -DIDF_TARGET=esp32s31 build
```

预期结尾：

```
s31_rtt_test.bin binary size 0x2f2a0 bytes. Smallest app partition is 0x100000 bytes. 0xd0d60 bytes (82%) free.
Project build complete.
```

> **如果链接脚本类改动没生效**，先删 ldgen 缓存再构建。ldgen 会按 fingerprint 直接写回旧结果，
> 表现为"改了 `sections.ld.in` 但错误一模一样"：
> ```bash
> rm -f build/esp-idf/esp_system/ld/sections.ld build/esp-idf/esp_system/ld/sections.ld.fingerprint build/esp-idf/esp_system/ld/sections.ld.lfcache
> ```

---

## 3. 烧写与监视

```bash
source <workspace>/idf_s31_env.sh
cd project
idf.py -p COM<N> flash monitor
```

- Windows 下端口形如 `COM5`；若板子用的是 USB-Serial-JTAG，`idf.py` 能直接识别。
- 退出 monitor：`Ctrl + ]`。
- 烧写失败先确认 `CONFIG_ESPTOOLPY_FLASHSIZE` 与实物一致（当前 sdkconfig 为默认值）。

---

## 4. 首次上电的预期输出

正常情况下应该在 1 秒内看到（115200 8N1）：

```
RT-Thread 5.4.0 on ESP32-S31
tick=1000 Hz, cpus=1, msh on UART0

 \ | /
- RT -     Thread Operating System
 / | \     5.4.0 build ...
 2006 - 2024 Copyright by RT-Thread team
msh >
```

判读要点：

| 现象 | 说明 |
|---|---|
| 只有前两行，没有 banner | `rt_components_board_init()` 之后的 finsh 初始化没跑到 → 见 §6.3 |
| 有 banner，没有 `msh >` | shell 线程起来了但设备没绑定 → 见 §6.4 |
| 有 `msh >` 但不回显按键 | UART0 RX 中断链路断 → 见 §6.4 |
| 打印到一半复位 | 见 §6.7 |
| 完全没有输出 | 见 §6.1 / §6.2 |

---

## 5. 自检序列

**这是本次移植最重要的验证手段。** 一条命令跑全部：

```
msh > s31test
```

也可以单独跑，逐项定位：

| 命令 | 验证什么 | 通过标准 |
|---|---|---|
| `s31test tick` | tick 速率（SYSTIMER 与 CPU 周期两个独立时钟源交叉验证） | `1000 us/tick`（±5%） |
| `s31test ctx` | 4 线程协作式切换，栈哨兵 | `0 guard errors`，每线程 loops > 0 |
| `s31test preempt 5` | **纯整数线程 + 1 kHz 强制抢占**（R9 的针对性用例） | `0 guard errors` |
| `s31test fpu` | f0–f31 跨线程切换 | `32 registers x 500 switches, no mismatch` |
| `s31test irq 10` | GPIO 中断全链路（软件置位，免接线）+ ISR 内的 `rt_interrupt_nest` | `200/200`，`nest=1`，`0 bad` |
| `s31test nest 10` | **中断嵌套**：GPIO 通道 0（1 级）handler 内部触发通道 1（2 级） | 外层 `nest=1`、内层 `nest=2`，100 轮 |
| `s31test sem` | 信号量乒乓 + 超时语义 | `500/500 tokens` |
| `s31test fbshim` | FreeRTOS 兼容层（队列/信号量/互斥量/事件组/TLS/调度器锁/阻塞守卫） | 7 行全 PASS |
| `s31events` | 真实组件：`esp_event` 默认循环 + `esp_netif_init()` + 投递事件（仅开关打开时存在） | `s31events: PASS, delivered in N ms, payload=0x5a31` |
| `s31timer 0 1000` | TIMG 通道 0，1 kHz 周期中断，SYSTIMER 独立测频 1 秒 | `got 1000 Hz ... PASS`（±1%） |
| `s31pwm 0 <gpio> 1000 50` | LEDC 通道 0 输出 1 kHz / 50%，走标准 `rt_pwm_set` + `rt_pwm_enable` | 打印 `timer 0 (period 1000000 ns, pulse 500000 ns)`，示波器可见方波 |
| `s31wifi` | `esp_wifi` 在兼容层上的完整启动链（NVS → netif → init → mode → config → start），每步打印 `esp_err_t` | 六行 `ok` + `s31wifi: PASS, STA_START in N ms, mac xx:xx:xx:xx:xx:xx` |

预期结果：

```
=== ESP32-S31 RT-Thread self test ===
tick: RT_TICK_PER_SECOND=1000 CONFIG_FREERTOS_HZ=1000
[PASS] tick     2000 ticks in 2000143 us = 1000 us/tick (expect 1000, 1000 Hz)
[PASS] ctx      min 3412 loops/thread, 0 guard errors, 4 threads
s31test: preempting for 5 s at 1000 Hz ...
[PASS] preempt  812341/809122 spin loops, 4998 wakeups, 0 guard errors
[PASS] fpu      32 registers x 500 switches, no mismatch
[PASS] irq      pin 10: 200/200 interrupts, nest=1 (0 bad)
[PASS] nest     pin 10->11: outer 100@nest1, inner 100@nest2 (want 100, 0 bad)
[PASS] sem      500/500 tokens, extra take timed out
[PASS] fbshim/q cap 4, FIFO, full+empty refusal (0 bad)
[PASS] fbshim/blk 6/6 produced, order preserved (0 bad)
[PASS] fbshim/to 50 ms timeout took 50123 us (0 bad)
[PASS] fbshim/sem counting 3 max, mutex not recursive, recursive ok, sched lock (0 bad)
[PASS] fbshim/ev or/and wait, partial clear, clear-on-exit (0 bad)
[PASS] fbshim/tls per-thread slots, untouched reads NULL (0 bad)
[PASS] fbshim/guard blocking under the scheduler lock returns an error (0 bad)
=== 14 passed, 0 failed ===
```

**全 PASS 意味着 P1 和 P2 的所有运行时假设同时成立**——这是 P1/P2 可以判定完成的唯一依据。

---

## 6. 失败时的判读路径

### 6.0 先按报错文本对号（2026-10-30 实测顺序）

第一次上板的 6 个失败点全部在这一段，**按出现顺序排**。遇到原始报错时先查这张表，
比从头排除快得多。

| 串口上的文本 | 位置 | 见 |
|---|---|---|
| `Loaded app from partition` 之后紧跟 `assert failed: unpack_load_app bootloader_utility.c:833 (rom_index < 2)` | bootloader，MMU 映射 | §10.1 |
| `Load access fault`，MEPC 在 `rt_object_get_type`，A0=0，MTVAL=0x0c | CORE 阶段初始化，堆未就绪 | §10.2 |
| `assert failed: esp_libc_locks_init locks.c:422 (handle == (SemaphoreHandle_t) &s_common_mutex)`，无限重启 | newlib 锁初始化 | §10.3 |
| `abort() was called at PC ... on core 0`，反汇编落在 `locks.c:150` | libc 加锁走了 ISR 分支 | §10.4 |
| `psa_crypto_init failed: -144` 后跟 `init function 'psa_crypto_init' has failed, aborting` | mbedtls 互斥量 take 被误判重入 | §10.5 |
| `Store access fault`，MEPC 在 `_rt_mutex_take`（`src/ipc.c:1353`），MTVAL=0x2c | `rt_thread_self()` 为 NULL | §10.6 |

前 4 条都发生在**打印 banner 之前**，所以"完全没有 banner"未必是硬件问题；先看有没有
`assert failed` 或 `fault` 字样——它们是启动路径在自己报告位置。

### 6.1 完全没有输出

按顺序排除，每一步都能独立确认：

1. **确认 ROM 能打印。** 临时烧一个 stock IDF 的 `hello_world`（S31 是 preview target，`idf.py set-target esp32s31` 可能提示）。ROM 都不打印说明是硬件/接线/波特率，与移植无关。
2. **确认不是等太久。** `main()` 之前的路径：ROM → bootloader → `startup.c` → `entry()`。bootloader 自身有 9 秒 WDT，卡住会复位并打印 `rst:0x...`。
3. **确认镜像真的写进去了。** `idf.py flash` 结尾应有 `Hash of data verified.`。

### 6.2 输出是乱码

波特率不匹配。当前镜像继承 ROM 的 `CONFIG_ESP_CONSOLE_UART_BAUDRATE`（默认 115200），
驱动层**不支持改波特率**（S31 的 `uart_ll.h` 没有 `uart_ll_get_sclk/set_sclk`，见 §8）。改 `sdkconfig` 的 console 波特率然后重新构建，不要在运行时改。

### 6.3 有 banner 但 msh 没起来

finsh 通过 `.rti_fn` 表自启动，三个环节任一断掉都会静默失败：

```bash
# 1) 表里有没有 finsh_system_init
riscv32-esp-elf-nm build/s31_rtt_test.elf | grep -E "(__rt_init_start|__rt_init_end)"
#    start == end  → 表是空的
# 2) finsh 代码有没有被链接进来
riscv32-esp-elf-nm build/s31_rtt_test.elf | grep finsh_system_init
#    空 → 归档成员没被拉入（需要 -u finsh_system_init）
# 3) RT_USING_COMPONENTS_INIT 有没有定义
grep COMPONENTS_INIT esp-idf-rtt/components/rtthread/port/esp32s31/rtconfig.h
```

这三条在 `esp-idf-rtt/components/rtthread/CMakeLists.txt` 里都有注释说明为什么必须这么写。

### 6.4 有 `msh >` 但按键无反应

输入走的是中断接收，链路有五段，从后往前查：

```
UART0 RXFIFO → RXFIFO_FULL/TOUT 中断 → 矩阵映射 UART0→INUM 5 → CLIC 槽位 21
             → _interrupt_handler → rtt_trampoline → s31_uart_isr → 环形缓冲 → 释放 shell 信号量
```

最快的判断：`s31test irq` 如果 PASS，说明**矩阵映射、CLIC 槽位、蹦床、优先级**这四段都是通的，
问题只可能在 UART 自己的 `RXFIFO_FULL/TOUT` 使能或 `uart_ll_set_rx_tout` 上。

如果 `s31test irq` 也 FAIL（0 次中断），见 §6.5。

### 6.5 `s31test irq` 报告 0 次中断

按可能性从高到低：

| 可能原因 | 如何确认 | 修复位置 |
|---|---|---|
| **CLIC 优先级仍是 0** | 反汇编 `rt_hw_interrupt_install` 应看到 `esprv_int_get_priority` / `esprv_int_set_priority` | `interrupt.c` 的 `RTT_INTR_DEFAULT_LEVEL` |
| **矩阵没路由** | 反汇编应看到 `esp_rom_route_intr_matrix`；运行时读 `INTERRUPT_CORE0_GPIO_INTR0_MAP`（偏移 0xf4）应为 4+16=20 | `rt_hw_intr_route()` |
| 中断阈值不为 0 | `mintthresh` 应等于 `CLIC_INT_THRESH(0)` = 0x1F | `board.c` 的 `esprv_int_set_threshold` |
| mtvt 表未装载 | GDB 读 `mtvt` CSR，应等于 `_mtvt_table` 地址（`0x2f000040`） | `cpu_start.c` |

`s31test irq <pin>` 用写 `GPIO_STATUS_W1TS` 的方式人为置位中断状态，**不需要外部接线**，
所以它失败一定是内核侧链路问题，不是硬件问题。

### 6.6 `s31test preempt` 报 guard errors

栈指针偏移。这条用例专门打 `vectors_rtthread.S` 的中断帧布局：

- 协作式帧（`context_gcc.S`、`rt_hw_stack_frame_t`）= **256 B**（128 B 整数 + 128 B 浮点）
- 中断帧（`vectors_rtthread.S`）= **必须也是 256 B**

两者不一致时，trap 出口的 `addi sp, sp, N` 会让恢复后的 sp 偏 ±128 B，
**纯整数线程、不出现任何 float 也会崩**。确认方法：

```bash
O=build/esp-idf/rtthread/CMakeFiles/__idf_rtthread.dir/../../../../components/riscv/CMakeFiles/__idf_riscv.dir/vectors_rtthread.S.obj
riscv32-esp-elf-objdump -d "$O" | grep -E "addi\s+sp,sp,-?(128|256)"
# 期望：入口两条 addi sp,sp,-128（先 FP 区再整数区），出口两条 addi sp,sp,+128
```

### 6.7 运行一段时间后复位

先看复位原因：

```
msh > s31stat
```

复位后串口第一行会打印 `rst:0x...`：

| `rst:` 码 | 含义 | 处理 |
|---|---|---|
| `0x3` / `0x7` | 软件/硬件看门狗 | **不应该发生**：RT-Thread 路径上不初始化 INT/TASK WDT（见方案 R11）。若出现，说明走了 FreeRTOS 分支，检查 `startup.c` 的分支条件 |
| `0xc` | 任务看门狗 | 同上 |
| `0x8` / `0x9` | 掉电/欠压 | 硬件供电 |
| `0x4` | 异常（非法指令/访问） | 用 `idf.py monitor` 的 backtrace，或 GDB 挂上去看 `mcause` |

### 6.8 `s31test tick` 报错

**先看数字是不是 16000 上下。** 这两个原因完全不同：

| 报的数字 | 原因 | 查什么 |
|---|---|---|
| `16000 us/tick`（≈16 倍） | **已修**：SYSTIMER 的原始计数被当成微秒用了。S31 的 SYSTIMER 是 XTAL 40 MHz 经固定 2.5 分频 = **16 MHz**，一个 raw count 是 62.5 ns；IDF 自己的 target port 就是 `ticks / 16`（`components/esp_hw_support/port/esp32s31/systimer.c`） | 若是旧镜像（`f40c14dd` 及更早）就会有这个 16 倍。修法：`s31_systimer_us()` 和 `st_now_us()` 都走 `systimer_ticks_to_us()`，见 log §24 |
| 其他比例（比如 100 或 0.9） | `RT_TICK_PER_SECOND` 与 `CONFIG_FREERTOS_HZ` 不一致。tick 由 IDF 的 SYSTIMER alarm 驱动，`xPortSysTickHandler()` 每个 alarm 调一次 `rt_tick_increase()`，所以两者必须相等 | 下面两条 grep |

```bash
grep -E "RT_TICK_PER_SECOND" esp-idf-rtt/components/rtthread/port/esp32s31/rtconfig.h
grep -E "CONFIG_FREERTOS_HZ" s31-rtt-test/sdkconfig
```

`selftest.c` 里有编译期检查，正常情况下不一致会直接构建失败。所以**编译能过、tick 还是错**，
基本就只可能是单位问题（16 倍），不是频率配置问题。

### 6.8b `s31test nest` 报 nest 不是 2

先分清是"根本没嵌套"还是"嵌套了但计数不对"：

| 现象 | 含义 |
|---|---|
| `inner 0@nest0` | 内层一次都没进 → 通道 1 的路由/优先级/`int_ena` 有问题 |
| `inner 100@nest1` | 内层进了，但**没有抢占**外层（是外层返回后才执行的）→ 优先级没生效 |
| `inner 100@nest2` 但 `outer` 不是 1 | `rt_interrupt_enter/leave` 与外层帧不匹配 |
| `INUM 6 already in use` | 有人先占了 CPU 线 6，换一个（用例里改 `ST_NEST_INNER_INUM`） |

第二条最需要警惕：它说明 CLIC 没有按 `INTERRUPT_PRIO_REG` 做抢占。检查
`board.c` 的 `esprv_int_set_threshold()` 是否被别处改过（阈值必须小于内层级别）。

### 6.8c `s31test fbshim` 失败

这一组测的是 FreeRTOS 兼容层，**和 `esp_driver_uart` / 未来的 Wi-Fi 是同一套代码**，
所以它失败基本等价于"这些组件也会失败"。

| 失败项 | 含义 |
|---|---|
| `fbshim/q` | 队列容量或 FIFO 顺序错 → 环形缓冲的 head/tail/count 不一致 |
| `fbshim/blk` | 跨线程阻塞或唤醒没生效 → 两个信号量（avail/space）配对错了 |
| `fbshim/to` | 超时时间明显偏离 50 ms → `ft_ticks_to_rt()` 的换算或 tick 频率不对 |
| `fbshim/sem` | 计数上限、互斥量递归性、调度器锁三者之一不对 |
| `fbshim/ev` | 事件组的 AND/OR、部分清除、clear-on-exit 语义不对 |
| `fbshim/tls` | 线程局部存储侧表没有正确按线程隔离 |

### 6.9 `s31test fpu` 报 mismatch

浮点现场没保存。注意**不要**去 `rtconfig.h` 里加 `ARCH_RISCV_FPU_S/_D`——
`cpuport.h` 已经根据 `-march` 里的 `__riscv_flen` 自动定义了，重复定义反而冲突。真正要查的是：

```bash
# 1) 工具链确实在编 FP（arch tag 应含 f2p2）
riscv32-esp-elf-readelf -A build/esp-idf/rtthread/CMakeFiles/__idf_rtthread.dir/../../../components/riscv/CMakeFiles/__idf_riscv.dir/vectors_rtthread.S.obj | grep -i arch
# 2) 中断帧里有 32 条 fsw 和 32 条 flw
riscv32-esp-elf-objdump -d vectors_rtthread.S.obj | grep -c fsw   # 期望 32
riscv32-esp-elf-objdump -d vectors_rtthread.S.obj | grep -c flw   # 期望 32
```

---

## 7. 需要收集回来的信息

出问题时不描述现象，直接给这四样：

```bash
# 1) 完整启动日志
idf.py -p COM<N> monitor > boot.log

# 2) 自检输出
#    msh > s31test > 从 monitor 里复制全文

# 3) ELF 关键符号
riscv32-esp-elf-nm build/s31_rtt_test.elf | grep -E "(_vector_table|_mtvt_table|_interrupt_handler|__rt_init_start|__rt_init_end)" > syms.txt

# 4) 复位原因
#    串口第一行 rst:0x...
```

---

## 8. 已知限制（不是 bug，是设计取舍）

| 限制 | 原因 | 何时处理 |
|---|---|---|
| 波特率固定 115200，改不了 | S31 的 `uart_ll.h` 缺 `uart_ll_get_sclk/set_sclk`，无法读取当前时钟源频率 | 需要直接操作 `HP_SYS_CLKRST` 寄存器，P3 之后 |
| `rt_kprintf` 走 ROM putc，不走设备 | 有意为之：驱动出问题时控制台仍然可用，否则失去唯一调试通道 | 稳定后可选 |
| TX 是忙等 | 没有 TX 环形缓冲，shell 一次只写几个字节 | 需要高速日志时再加 |
| GPIO 只用中断通道 0 | `gpio_ll_intr_enable_on_core()` 硬编码 `GPIO_LL_INTR0_ENA`，IDF 自己留了 TODO（IDF-7995） | 需要 >32 个独立中断源时 |
| UART0 / GPIO 默认都是 1 级 | 同级别不能互相抢占，所以**默认配置下没有硬件嵌套** | 机制本身可用（`s31test nest` 验证）；要嵌套就在 `rt_hw_interrupt_install()` **之前**调 `rt_hw_interrupt_set_priority()` |
| **任务**看门狗未启动 | `esp_task_wdt_init()` 在 FreeRTOS 专属路径里，而本镜像没有任何 `esp_task_wdt_add()` 订阅者（全树搜索确认，只剩 test_apps）——启动一个没人订阅的看门狗只会增加失败模式 | 需要时再开 |
| **中断**看门狗已启动 | `port/esp32s31/boot_tasks.c` 显式调 `esp_int_wdt_init()` + `esp_int_wdt_cpu_init()`；两个符号在镜像里 （`0x400982b2` / `0x4009836e`），tick 由 `port_systick.c` 的 `esp_vApplicationTickHook()` 喂 | 已完成 |
| Wi-Fi / BLE 已能跑在兼容层上，但**没有连接测试** | `s31wifi` 起驱动、`s31ble` 起控制器，都在 FreeRTOS 兼容层上；连热点需要 `s31wifi <ssid> <pass>`，未实现 | 需要联网时 |
| Wi-Fi 无 5 GHz | `SOC_WIFI_SUPPORT_5G` 只在 esp32c5 定义，S31 是 2.4 GHz Wi-Fi 6 | 硬件限制 |

---

### 8.5 打开 FreeRTOS 兼容层（Wi-Fi 的前置条件）

开关：`CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM`，默认 **关闭**。

打开后 `components/freertos` 不再编译 `queue.c`/`tasks.c`/`timers.c`/
`event_groups.c`/`stream_buffer.c`，API 由 `port/freertos/freertos_api.c` 提供
（已离线验证：链接通过，镜像从 199,808 B 降到 192,800 B）。

**必须分两步做，不要一次到位。** 打开后 `esp_libc` 的 newlib 锁、`spi_flash`、
`esp_coex` 全都跑在兼容层上，而它们在启动路径里——语义错一点，板子会在能打印
任何东西之前挂死。

### 第 1 步：保持关闭，验证语义

```bash
idf.py -p COM<N> flash monitor
msh > s31test fbshim
```

六行全 PASS 才继续。任何一行 FAIL 都说明兼容层有问题，此时修还来得及。

### 第 2 步：打开开关，验证启动路径

```bash
cd project
echo "CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM=y" >> sdkconfig
source <workspace>/idf_s31_env.sh
idf.py -DIDF_TARGET=esp32s31 build
idf.py -p COM<N> flash monitor
```

判断标准：

| 现象 | 结论 |
|---|---|
| 正常打出 banner 和 `msh >` | 启动路径（newlib 锁、spi_flash）在兼容层上成立 |
| 卡在启动、无输出 | 高度怀疑 `xQueueCreateMutexStatic`：我们的实现会退化成堆分配，newlib 锁初始化可能因此失败。**这条 2026-10-30 已被实测证实并修复**——见 §10.3（handle 契约）、§10.4（缺 NOT_STARTED）、§10.5（owner 哨兵）、§10.6（NULL 线程）。若再现，先按这四条查 |
| 能启动但 `s31test` 里某项挂住 | 该子项对应的语义在真实并发下不成立 |

再跑一遍 `s31test`，13 项应当仍然全 PASS。

### 回退

```bash
cd project
sed -i '/CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM/d' sdkconfig
idf.py -DIDF_TARGET=esp32s31 build
```

---

## 9. 下一步（按依赖顺序）

1. **上板跑通 §4 / §5** —— 2026-10-30 已完成"能启动"这一半（§10），
   tick / 上下文切换 / 嵌套 / FPU 的自检结果仍需逐项记录
2. 打开 `CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM`，按 §8.5 的两步法验证
3. 打开 `esp_driver_uart`（`uart_driver_install`），用它替换现有的 LL 串口驱动
4. 中断嵌套：用 `rt_hw_interrupt_set_priority()` 把 UART 设 2、GPIO 设 1，验证 `rt_interrupt_nest`
5. 双核 SMP（P3）：`RT_CPUS_NR=2` + `start_other_core()` + IPI
6. Wi-Fi/BLE：`esp_event` → `esp_netif` → `esp_wifi`，再逐函数替换 `esp_adapter.c`


### `s31wifi` 的预期输出与失败定位

```
msh > s31wifi
s31wifi: bringing up esp_wifi on the FreeRTOS shim
  nvs_flash_init             ok
  esp_netif_init             ok
  create default wifi sta    ok
  event handler register     ok
  esp_wifi_init              ok
  esp_wifi_set_mode          ok
  esp_wifi_set_config        ok
  esp_wifi_start             ok
s31wifi: PASS, STA_START in N ms, mac 30:30:f9:xx:xx:xx
s31wifi: netif 0x... , mode STA, not connected
```

失败时打印的是具体那一步的 `esp_err_t` 和名字，按下表定位：

| 卡在哪一步 | 大概率原因 |
|---|---|
| `nvs_flash_init` | nvs 分区缺失或 flash 驱动。**不致命**，命令会继续（PHY 退回全量校准） |
| `esp_netif_init` | lwIP 线程或邮箱没起来 |
| `create default wifi sta` | `esp_netif` 的队列分配失败（堆不够） |
| `event handler register` | 默认事件循环没建（`esp_event_loop_create_default()` 在这里是隐式的） |
| `esp_wifi_init` | 兼容层的队列/信号量/任务创建，或堆不足 |
| `esp_wifi_start` | **PHY 校准**。若在这里 abort，先看是否 `board: esp_timer_init failed` |
| 全部 ok 但 5 s 内没 STA_START | Wi-Fi 任务创建了但没跑 —— 调度优先级或栈 |

**注意**：本命令不连接 AP，`wifi_config_t` 全零是有意的（这个镜像没有配网路径）。
验证的是"栈起来了"，不是"连上了"。连接测试要另加 SSID/密码，属于下一阶段。

### `s31stat` 现在报两个堆

```
msh > s31stat
tick   : 123456
counter: a=82 b=82
heap   : total=131072 used=... max=...     <- RT-Thread 的 128 KB
idfheap: free=...                          <- IDF 自己的 ~234 KB
```

两个堆**不相交**：`rt_malloc` 出来的块释放后回到第一个堆，`malloc`（IDF 侧）
看不见；反之亦然。只报一个就会出现"明明有内存却分配失败"。

`idfheap: free` 还是一个诊断信号：**回收启动栈之前，那 20 KB 不属于任何堆**。
如果这个数字明显偏小（比如比 200 KB 小很多），先怀疑回收没跑起来——查
`rt_hw_reclaim_startup_stack` 是否在 `.rti_fn.6` 段里、是否被 `rt_components_init()`
执行到。

### 槽位与栈水位：两个只有跑久了才看得出的检查

```
msh > s31test fbshim
...
fbshim/exit  PASS  TLS slot freed when the task returns (0 bad)
fbshim/hwm   PASS  stack high water mark 1984 bytes free (0 bad)
```

**`fbshim/exit`** 建一个任务，让它取 TLS 槽、发一个通知，然后用 `return` 退出
（不调 `vTaskDelete`），再检查占用槽数回到起点。这条路径修之前是漏的：任务函数
返回时 RT-Thread 走 defunct 队列，够不到 `ft_task_delete()`，而线程对象地址会被
下一个任务复用——新任务会继承死任务的 TLS 值和删除回调。

如果这一项 FAIL，说明 `rt_thread_t::cleanup` 钩子没生效。查两处：`ft_task_create`
里有没有设 `thread->cleanup`，以及 `rt_defunct_execute()` 有没有真的把线程从
defunct 队列取出来（idle 线程得能跑——见下面"优先级"一节）。

**`fbshim/hwm`** 是栈高水位，单位是**字节**（`StackType_t` 是 `uint8_t`）。它扫
RT-Thread 填的 `'#'` 模式（`src/thread.c:210`），数出底部还有多少没被踩过。
0 或者超过 `stack_size` 都算 FAIL——前者意味着整个栈用完了或扫描坏了，后者意味着
扫描越界。

Wi-Fi 起来之后这个数字值得盯：`esp_wifi_init` 的任务栈是 3584 字节，如果只剩
几个字节，说明 IDF 给的默认值在这个 port 上不够。

### 构建时就会失败：链接探针

`ft_probe.c` 里是一张表，把 IDF 头文件承诺的 72 个 FreeRTOS 函数各取一次地址。
**缺任何一个，`idf.py build` 会直接以符号名失败**，不会等到运行时。

这张表靠 `s31test` 命令里的一个引用保活（msh 的命令表在 `KEEP()` 段里）。别删那个
引用：没有它，`--gc-sections` 会在解析内容前丢掉整段，构建照样通过但什么也没检查
——这个坑我踩了两次，`const` 数组和 `used+retain` 都不行。

如果构建突然报某个 `xQueue*` 未定义，说明真缺实现，不是误报。

### `s31test fbshim` 现在有 15 个报告点

一次跑完能看到兼容层每个面的状态：

```
fbshim/q      队列容量、FIFO、满/空拒绝
fbshim/blk    跨任务双向阻塞
fbshim/to     超时精度（50 ms 允许 40~500 ms）
fbshim/sem    计数信号量、非递归 mutex
fbshim/ev     事件组 OR/AND、部分清除、clear-on-exit
fbshim/tls    每线程槽位、未写读过 NULL
fbshim/exit   任务 return 后槽位回收      <- 只在跑久了才看得出
fbshim/hwm    栈高水位（字节）
fbshim/query  任务数、按名查找、状态、idle 句柄
fbshim/guard  调度器锁内阻塞被拒绝，不是挂住
fbshim/probe  链接探针的表真的在镜像里
fbshim/qset   队列集：成员、精确通知、移除、陈旧通知
fbshim/ovr    overwrite 不增长队列、不变量、peek 不消费、reset 恢复容量
```

`fbshim/guard` 里加了 `ft_task_delay()` 的检查：在临界区内调 50 tick 和
`portMAX_DELAY`，两者都必须立刻返回。**这一项不会静默失败**——如果守卫丢了，
命令根本走不到打印那一行，整个 `s31test` 会卡死在这里。所以"卡死"本身就是
这个测试的失败信号。

### 优先级：映射后不进入 idle 档位

FreeRTOS 的 `tskIDLE_PRIORITY`（0）映射到 RT-Thread 的 `MAX-2`，不是 `MAX-1`。
后者是 idle 自己的档位，时间片轮转下会跟 idle 抢 CPU——而 idle 是回收已结束线程的
地方。如果板上出现"线程创建了但 `list_thread` 里看不到已结束的线程被清掉"、
或者内存只涨不降，先查这里。

### 中断看门狗

`CONFIG_ESP_INT_WDT_TIMEOUT_MS=300`。启用后，如果关中断超过 300 ms（比如在临界区
里死循环、或者在 Flash 操作期间缓存被关而中断处理函数在 Flash 里），会直接
panic 并打印位置，而不是静默卡死。这是 bring-up 阶段最想要的失败模式：
**能报告的位置，比"卡住了"有用得多**。

上板时如果想确认它真的在工作，可以在 msh 里制造一次：进入 `s31test nest 100`
这类长时间关中断的路径，或者临时写个死循环测试。能看到 panic 输出就说明生效。

---

## 10. 上板实测记录（2026-10-30）

### 10.0 环境

| 项 | 值 |
|---|---|
| 板子 | ESP32-S31 开发板 |
| 串口 | COM16，115200 8N1 |
| flash | 2 MB |
| CPU | 320 MHz，单核（`CONFIG_FREERTOS_UNICORE=y` 生效，CPU1 保持复位） |

烧写命令 `idf.py -p COM16 flash monitor`。第一次烧写后走到应用启动之前，串行暴露了 6 个缺陷。
**它们是串行的**：修掉前一个，后一个才浮出来，因为前一个的 assert 把后一个挡住了。
所以下面按出现顺序记录，不要合并成一次改动。

### 10.1 段数超过 bootloader 上限

```
I (312) boot: Loaded app from partition at offset 0x10000
assert failed: unpack_load_app bootloader_utility.c:833 (rom_index < 2)
```

"Loaded app" 已经打印，说明分区表、镜像校验、flash 读取都正常；失败在**把段映射进 MMU** 这一步。

S31 的 `SOC_MMU_DI_VADDR_SHARED=1`——I/D 总线共用虚拟地址，bootloader 只给 **2 个** flash
映射段。而 `.flash.rt_thread`（`__fsymtab`/`__vsymtab`/`__rt_init`，PROGBITS）排在
`.flash.rodata_noload`（NOBITS）之后：NOBITS 不占文件空间，后面再接 PROGBITS 时文件偏移不连续，
链接器只能新开一个 LOAD 段，于是 2 变 3。

**ELF 检查看不出来**：符号地址全对，`idf.py size` 正常，唯一线索是 `readelf -l` 多一行段。
修法是把三个符号表并入 `.flash.rodata` 内部，并删掉 esp32c3 / esp32c6 / esp32s31 的独立段定义。

改完**必须删 ldgen 缓存**，否则现象一模一样（§2 那个提示）：

```bash
rm -f build/esp-idf/esp_system/ld/sections.ld{,.fingerprint,.lfcache}
```

改完的自检命令：

```bash
riscv32-esp-elf-readelf -l build/s31_rtt_test.elf | grep -c LOAD    # flash 映射段应为 2
```

### 10.2 堆初始化晚于 CORE 阶段

```
Load access fault
MEPC   : 0x4000xxxx   (rt_object_get_type)
A0     : 0x00000000
MTVAL  : 0x0000000c
```

`MTVAL=0x0c` 是被访问地址，A0=0 说明基址 NULL，偏移 12 正是 `rt_object_get_type` 取 `type` 字段的位置。

`do_core_init()` 先跑 CORE 阶段初始化项、之后才 `rt_system_heap_init()`；而 `esp_libc_init`
在 CORE 阶段就 `xSemaphoreCreateMutexStatic` → `rt_malloc`，此时 `system_heap == NULL`
（`src/mem.c:287`）。修法：把 `rt_system_heap_init()` 提到 `do_system_init_fn(CORE)` 之前。

### 10.3 静态创建 API 的 handle 契约

```
assert failed: esp_libc_locks_init locks.c:422 (handle == (SemaphoreHandle_t) &s_common_mutex)
```

无限重启。FreeRTOS 的 `xSemaphoreCreateMutexStatic(pcb)` 承诺**返回 handle == 调用者传入的
控制块指针**，`esp_libc` 用 assert 把这条实现细节当契约用。

尺寸上装不进去：`StaticQueue_t` 约 60 字节（为 FreeRTOS 自己的 TCB 预留），
`struct ft_ipc` 约 180 字节（两个 `rt_semaphore` + 一个 `rt_mutex`）。硬塞会越界写 100 多字节。

修法是在调用者的块里放两字长的桩 `{ magic, real }`，真实对象留在堆上；
`FT_IPC_MAGIC = 0x46544950`（"FTIP"），`ft_ipc_resolve()` 只在 magic 匹配时跟随指针。
真实 `struct ft_ipc` 开头是三个一字节标志，首字凑不出这个值，两侧不会误认。

**这一条对"离线验证"的意义**：链接阶段完全正常，`fbshim` 也测不到——它测的是 API 语义，
而 assert 来自下游调用方对**实现细节**的依赖。

### 10.4 调度器状态缺 NOT_STARTED

```
abort() was called at PC 0x4000xxxx on core 0
```

反汇编该 PC，落在 `locks.c:150`（递归互斥量在 ISR 上下文）。

`esp_libc` 把 `xTaskGetSchedulerState() == taskSCHEDULER_NOT_STARTED` 当"还没开始调度，
加锁是空操作"的逃生门（`locks.c:128` / `:137`）。shim 只返回 RUNNING / SUSPENDED，
于是第一次 libc 加锁走不到逃生门，继续判"是否在 ISR"——而 `xPortCanYield()` 在
`rt_thread_self()==NULL` 时返回 false，被判成 ISR 上下文。

修法：`ft_task_scheduler_state()` 里 `rt_thread_self() == RT_NULL` → `taskSCHEDULER_NOT_STARTED`。

### 10.5 owner 哨兵冲突

```
E (321) psa_init: psa_crypto_init failed: -144
E (321) esp_system: init function 'psa_crypto_init' has failed, aborting
```

−144 = `PSA_ERROR_SERVICE_FAILURE`，来自 `mbedtls_mutex_lock` 失败。

`ft_ipc_take()` 的 `if (ipc->owner == self) return FT_ERR_TIMEOUT` 本意是拦"同线程重复 take
普通互斥量"。但调度器未启动时 `rt_thread_self()` 返回 NULL，而**干净互斥量的 `owner` 也是 NULL**，
NULL == NULL 成立——每个互斥量都被判成"你已持有"。

修法：`if (ipc->owner != RT_NULL && ipc->owner == self)`。

这是哨兵值冲突：NULL 同时表示"没有线程"和"没有持有者"。读代码时那一行看起来是对的。

### 10.6 解引用 NULL 线程

```
Store access fault
MEPC   : 0x4000xxxx   (_rt_mutex_take, src/ipc.c:1353)
MTVAL  : 0x0000002c
```

`0x2c` = 44 = `offsetof(struct rt_thread, error)`。`rt_mutex_take()` 无条件写
`thread->error = RT_EOK`，而 `thread = rt_thread_self()` 是 NULL。

修法：`ft_ipc_take()` / `ft_ipc_give()` 在"互斥量类型 + `rt_thread_self()==RT_NULL`"时直接返回
`FT_OK`。这就是 FreeRTOS 在该窗口的"加锁是空操作"语义。队列和信号量不受影响——
`rt_sem_trytake` 不需要线程对象。

### 10.7 六个缺陷的共同点

第 2、4、5、6（堆顺序、NOT_STARTED、owner 判定、NULL 线程）是**同一族**：
都在"调度器还没起来"这个窗口里。FreeRTOS 对它有明确定义（加锁空操作、任务创建自己初始化列表、
调度器状态有第三个值），RT-Thread 没有，port 每一处都得自己补——而"哪几处要补"没有清单。

第 1 条是镜像布局对 bootloader 契约的依赖，第 3 条是下游对 FreeRTOS 实现细节的依赖。
四类缺陷没有一类落在 §5 的自检覆盖范围内，因为 `s31test` 要等 msh 起来才能跑。

### 10.8 对判读顺序的影响

上板前认为"完全不打印 → 查 §6.1 硬件"是最可能的路径；实际 6 个缺陷里有 4 个发生在
banner 之前，其中 5 个**自己打印了可读的报错**。所以现在的顺序是：

1. 先看有没有 `assert failed` / `fault` / `abort` 字样，按 §6.0 对号；
2. 确实是空白才走 §6.1（换 stock IDF 验证 ROM 与接线）；
3. 能出 banner 之后再跑 §5 自检。

## 11. 第七个缺陷：线程切换时的 FPU 帧（2026-10-30）

前六个缺陷修完之后，板子第一次打印到 `msh >`，然后立刻 panic：

```
RT-Thread 5.4.0 on ESP32-S31
tick=1000 Hz, cpus=1, msh on UART0
msh >Guru Meditation Error: Core 0 panic'ed (Instruction access fault)
MEPC = 0x2f000270  MCAUSE = 0x00000001  MTVAL = 0x2f000270
```

### 判读顺序

1. **`MCAUSE=1` 且 `MTVAL == MEPC`** —— 这是取指故障，而且故障地址就是
   该指令自己的地址。先确认这个地址所在的段确实被加载了（看启动日志里
   `esp_image: segment N: vaddr=...` 那一行），如果加载了，就不是缺段。
2. **反汇编那个地址**。`0x2f000270` 是 `spurious_interrupt` 里
   `addi sp,sp,128` 之后的第一条 `flw ft0,0(sp)`。
3. **看 `MSTATUS` 的 FS 位（bit 13:14）**。FS=0 时执行任何 FP 指令都是
   非法指令。
4. **注意 `SOC_CPU_HAS_FPU_EXT_ILL_BUG`**。这颗芯片的 EXT_ILL 寄存器
   不记录 FLW/FSW，所以"FP 指令遇到 FS=0"不会被报成非法指令，而是被
   报成 `MCAUSE=1` 且 `MTVAL` 等于该 `flw` 的地址。**这是最容易误判的
   一处**——看起来像取指问题，其实是 FPU 状态问题。

### 检查清单

| 检查 | 期望 |
|---|---|
| `vectors_rtthread.S` trap 退出的 `li t1, ...` | FPU 构建下必须是 `0x7800`（MPP+FS），不是 `0x1800` |
| `context_gcc.S` 恢复路径 | `ARCH_RISCV_FPU` 下是 `0x7800` |
| 帧尺寸三处一致 | `rt_hw_stack_init_core` 的 `-256`、prologue 的 `-128`+`-128`、`context_gcc.S` 的 `-128`+`-128` |

三处尺寸一致**不代表**没问题：尺寸一致只保证不越界，`mstatus` 里该带
哪些位是另一回事。这是这一轮最值得记住的一条。

### 修完之后

镜像 897,872 B，`msh >` 可用，`thread_a` 的 500ms 循环正常。

## 12. 上板自检阶段抓出的 9 个缺陷（2026-10-30，§10/§11 之后）

§10 的六个 + §11 的一个，全部是"能不能启动"。修完之后**第一次跑完整自检**，又抓出九个。
按**现象**索引，遇到同类问题直接对照：

| # | 串口上的现象 | 真实原因 | 修在哪 |
|---|---|---|---|
| 8 | `s31test tick` 报 `15999 us/tick`（16 倍） | SYSTIMER 原始计数被当微秒，S31 是 16 MHz 不是 1 MHz | `gptimer.c` `s31_systimer_us()` |
| 8b | 修完上一条后变成 `0 us/tick` | 读的是 `unit_val[0]`（esp_timer 的计数器，实测不动），该读 unit 1 | 同上 |
| 9 | `Guru Meditation ... Load access fault`，MEPC 在 `rt_object_get_type`，`A0=0`、`MTVAL=0xc`，**在 idle 线程里** | 线程已从 entry 返回并被 idle 释放，测试又调了一次 `rt_thread_delete` = 双重释放 | `ft_task.c` 退出标记表 |
| 9b | 修完上一条后变成 `Store address misaligned`，MEPC 在 `rt_list_remove` | 按 `stat == CLOSE` 判"已退出"不行：那时对象已释放并被复用，读到的是新主人的字段 | 同上，改成指针标记 |
| 10 | `[FAIL] fpu` 但打印的正是成功文案 | `st_report(name, ST_PASS, ...)`：判据参数要布尔，`ST_PASS` 是 0 | `selftest.c` 引入 `ST_REPORT_OK/BAD` |
| 11 | `[FAIL] fbshim/ev` | 事件组返回清除**后**的值、且清了整个 mask；FreeRTOS 要清除**前**、只清匹配位 | `ft_event.c` |
| 12 | `s31timer` 任意频率都只有 1 次中断 | `tx_alarm_en` 是 **R/W/SC 自清零位**，`tx_autoreload` 只重装计数器 | `gptimer.c` ISR 里重新 arm |
| 12b | 修完上一条后频率系统性偏高（1000→1002、200000→248975） | (a) `alarm -= 1` 是错的；(b) 整数截断让 >500 kHz 都变成 1 MHz | `gptimer.c` 四舍五入 + 越界报错 |
| 13 | `s31pwm 0 29 ...` 之后整机崩 | LEDC 的 pad 校验用了 `SOC_GPIO_PIN_COUNT`，没排掉 flash 的 GPIO29/41 | `pwm.c` 用 `SOC_GPIO_VALID_GPIO_MASK` |

### 12.1 判读要点：三个"数字很小/很整"的线索

这一轮再次验证 §10.7 的结论——**关键信息往往是一个数字**：

| 数字 | 含义 |
|---|---|
| `15999 us/tick` | 16 倍。S31 的 SYSTIMER 是 16 MHz（XTAL 40 MHz / 2.5），IDF 自己就是 `ticks / 16` |
| `0 us/tick` | 计数器根本没动。`unit_val[0]` 归 esp_timer，不在跑；`unit_val[1]` 才是 tick 用的那个 |
| `MTVAL=0xc` + `A0=0` | NULL 基址 + 偏移 12 = `offsetof(rt_object, type)`。要么堆没初始化，要么对象已被释放 |

`tools/board.py` 里新增的 `s31test clock` 子命令就是为这类问题准备的：它把
**三个独立时钟域并排打印**（os_tick / esptimer / CPU 周期），哪个不动一眼就看出来：

```
clock: over ~100 ms
  os_tick  +1600005 counts = +100000 us @16MHz
  esptimer +0 counts                       <-- 不动
  us       +100000 us
  ccount   +31905210 cycles @320MHz = 99703 us
```

### 12.2 `s31timer` 现在的正确输出

```
s31timer: timer 3 want 2 Hz,       got 1 Hz      over 999967 us,  1 ticks      -> PASS
s31timer: timer 0 want 1000 Hz,    got 999 Hz    over 999998 us,  999 ticks    -> PASS
s31timer: timer 2 want 200000 Hz,  got 199974 Hz over 1000001 us, 199975 ticks -> PASS
s31timer: timer 1 want 500000 Hz,  got 496916 Hz over 1000003 us, 496918 ticks -> PASS
```

容差是 `max(1%, 1 count)`：1 秒窗口里 2 Hz 本来就可能只抓到 1 或 2 个边沿，用严格百分比
会把正确的定时器判成坏。500 kHz 那一行差 0.6%，是 1 MHz 时基的量化误差。

### 12.3 完整自检现在长这样

```
=== 20 passed, 0 failed ===
```

（逐行输出见 porting-log §25.9。`s31test` 一项跑全部，单项仍然可以单独跑。）

### 12.4 `s31wifi` 的意义

```
s31wifi: PASS, STA_START in 0 ms, mac 1c:29:04:d0:14:1e
```

这是**架构级**的结论：NVS → netif → 事件循环 → `esp_wifi_init` → PHY 全量校准 →
固件加载 → STA_START 整条链跑在 RT-Thread + FreeRTOS 兼容层上，一处没卡。
PHY 走全量校准是因为 NVS 里没有校准数据（`failed to load RF calibration data (0x1102)`），
这是**预期行为**，不是失败；跑过一次之后会存回 NVS，下次就快了。

未验证的仍有三项：双核（P3）、BLE、真实 AP 连接（本命令只起栈不连网）。

## 13. RT-Thread 原生命令与兼容层影子状态（2026-10-30 晚）

### 13.1 如果 `list` / `free` / `ps` / `version` 报 `command not found`

两个独立原因，症状一样：

| 命令 | 原因 | 修法 |
|---|---|---|
| `ps` `free` `bind` | `msh.c` 里整段在 `#ifdef MSH_USING_BUILT_IN_COMMANDS` 内，而 `rtconfig.h` 不是 Kconfig 生成的，没人写这个宏 | `rtconfig.h` 加 `#define MSH_USING_BUILT_IN_COMMANDS` |
| `list` `clear` `version` `console` | `cmd.c` 编译了，但整个归档成员没人引用 → `--gc-sections` 丢掉 | CMakeLists 加 `target_link_libraries(... INTERFACE "-u list_thread")` |

**注意 `-u` 后面必须是全局符号。** `cmd_list` 是 `static`，写 `-u cmd_list`
链接器会**静默忽略**，改了 CMakeLists 却毫无变化——很容易误判成 ldgen 缓存。

验证方法（不用上板）：

```bash
riscv32-esp-elf-nm build/s31_rtt_test.elf | grep list_thread    # 期望 T list_thread
grep -A8 "FSymTab" build/s31_rtt_test.map                        # 期望看到 cmd.c.obj 的 0x30
```

### 13.2 正确的输出长什么样

```
msh > list device
device               type         ref count
------------ -------------------- ----------
uart0        Character Device     1
pwm0         PWM Device           0
pin          Pin Device           0

msh > ps
thread       pri  status      sp     stack size max used left tick   error
tb            12  suspend 0x000001b0 0x00000800     21%   0x0000000a EINTRPT
ta            10  suspend 0x000001b0 0x00000800     21%   0x0000000a EINTRPT
tshell        20  running 0x000001b0 0x00001000     16%   0x00000006 OK
tidle0        31  ready   0x00000120 0x00000400     32%   0x0000001f OK
esp_timer      8  suspend 0x000001c0 0x00001000     10%   0x0000000a EINTRPT

msh > free
total    : 130976
used     : 16784
maximum  : 20560
available: 114192
```

`list device` 是"到底移植了什么"最直接的回答：三个设备、三个 RT-Thread 设备类。

### 13.3 互斥量"已经持有"但没人持有

如果 `xSemaphoreTake` 对一个明明空闲的互斥量报超时、`xSemaphoreGetMutexHolder`
返回一个已经死掉的线程，说明兼容层里**存了一份影子 owner**，而 RT-Thread
在持有者死亡时会自己释放并清 `mutex->owner`，那份影子没人清，地址复用之后
就永久指向"自己"。已修：`ft_ipc` 只读内核的 `mutex->owner`，不再维护副本。

### 13.4 能不能跑 Python

**当前不能**：树里没有 Micropython，镜像里没有 `mp_*` / `Py_*` 符号。
要跑 .py 需要移植 MicroPython（实现 `mp_hal_*` 那一层，200~300 KB flash）。
这与 RT-Thread 用 C 写没有关系——C++ 只是它的可选组件，加 Python 运行时是另一件事。

顺序建议：先让 Wi-Fi 连上真实 AP（现在是"起栈成功、未连接"），
因为 MicroPython 的 `network` 模块要有能用的 `esp_netif`。

## 14. 16 MB flash、BLE、I2C（2026-10-30 夜）

### 14.1 先把 flash 大小改对

这片板是 **16 MB**，不是 2 MB：

```powershell
python -m esptool --port COM16 --before default-reset flash-id
# Manufacturer: 46 / Device: 4018 / Detected flash size: 16MB
```

启动日志里那句 `Detected size(16384k) larger than ... (2048k)` 一直在提示。
`tools/tmp/flash_probe.py` 会往 8 MB 处写图案、复位、读回、比对，用来证明上半区不是镜像别名。

新布局（`s31-rtt-test/partitions.csv`）：

```
0x009000  nvs        24 KB
0x00f000  phy_init    4 KB
0x010000  factory     4 MB
0x410000  storage     8 MB    <- 空着，留给文件系统 / MicroPython
```

**换布局必须擦掉旧区域**，否则旧分区表残留会让 bootloader 迷惑：

```powershell
python -m esptool --port COM16 erase-region 0x10000 0x110000
```

### 14.2 `s31ble`

```
msh > s31ble
  nvs_flash_init               ok
  nimble_port_init             ok
  nimble_port_freertos_init    ok
s31ble: PASS, host synced in 0 ms, addr 1c:29:04:d0:14:1f
```

判读：

| 现象 | 含义 |
|---|---|
| `nimble_port_init FAILED 0x103` | 控制器**已被手动初始化过**。`nimble_port_init()` 自己会 init+enable，不要再手动做 |
| `host never synced` + `ps` 里没有 `nimble_host` | `nimble_port_freertos_init()` 的参数是**任务函数**，传 NULL 建不出任务 |
| `BTDM_SLEEP: Using 136 kHz RC` 警告 | 低功耗时钟源默认是 RC，精度不够维持 ACL/Sync。真连设备前要改 XTAL32K |

蓝牙起来后 `ps` 会多两个线程：`btdm`（优先级 7）和 `nimble_host`。

### 14.3 `i2cscan`

```
msh > list device
uart0        Character Device     1
i2c0         I2C Bus              0
pwm0         PWM Device           0
pin          Pin Device           0

msh > i2cscan 0
i2cscan: probing i2c0 (100000 Hz)
i2cscan: 0 device(s) on i2c0
```

空总线报 0 是**正确结果**，不是失败。接一个从机（比如 0x38 的 AHT10）再跑一次，
应该看到 `0x38 ACK`。

引脚是 `port_i2c.h` 里的 `S31_I2C0_SCL_PIN=4` / `S31_I2C0_SDA_PIN=5`，
要改就改那两个宏。驱动会拒绝 GPIO29/41（flash pad）。

**如果 `i2cscan` 像卡住**：检查超时预算。第一版用了 200 万次循环，
空总线上 112 个地址全不 ACK，乘起来就是假死。现在按总线速率算预算并在
NACK/bus-timeout 位提前退出。

### 14.4 构建可复现性：删掉 sdkconfig 必须仍能构建

这一轮踩到过：`CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM` 只写在 `sdkconfig`、没进
`sdkconfig.defaults`，删掉 sdkconfig 重新生成后链接失败（`ft_task_notify` 未定义）。
**任何新增的 CONFIG 都必须同时进 defaults**，否则"我这儿能编"就不可复现。

检查法：

```powershell
Remove-Item project\sdkconfig
powershell -ExecutionPolicy Bypass -File tools\build.ps1 build
```

### 14.5 加外设的正确节奏

每加一个外设要动同一组五样东西：`rtconfig.h` 宏、CMakeLists 框架源、LL 驱动、
`board.c` init、msh 自检。I2C 这一次约 500 行 + 3 轮调试（超时预算 + 两个头文件路径）。
**一次加 2~3 个，不要一次加 14 个**——上一轮"修完第一个才看得见第二个"的教训会按倍数放大。

S31 **没有 DAC**（`soc_caps.h` 里没有 `SOC_DAC_SUPPORTED`），别为它浪费时间。

## 15. 板子资料核对与 PSRAM（2026-10-30 深夜）

板子型号：**Maple ESP32-S31 Alef（XPU Labs）**，模组 **ESP32-S31-WROOM-3**。
资料在 `esp-idf-rtt/docs/开发板资料/docs/maple_esp32/`，原理图
`esp-idf-rtt/SCH_Schematic1_2026-10-30.pdf`。

### 15.1 引脚以 `port_board.h` 为准

之前 I2C 用的是 IO4/IO5，**是错的**——那两个脚在这块板子上是 I2S_LRCK / I2S_DSDIN。
板子手册的引脚表写得很清楚：

```
IO50  I2C_SCL    IO51  I2C_SDA        <- 音频/视频排座上的 I2C
IO60  RGB LED (SK6805-2427 DIN)
IO61  BOOT1 按键（按下为 0）
IO3/4/5/6/7  LCD_DC / LCD_CS / LCD_SCK / LCD_MOSI / LCD_RST
             （同时是 I2S_SCLK / LRCK / DSDIN / SDOUT / PA_CTRL，二者不能同时用）
IO0/IO1      CAM_I2C_SDA / CAM_I2C_SCL（摄像头自己的 I2C）
```

这些常量集中在 **`components/rtthread/port/esp32s31/port_board.h`**。要改引脚就改那里，
不要再往各个驱动里各写一份。

### 15.2 `s31led`

```
msh > s31led 8 0 0
s31led: gpio 60 <- R=8 G=0 B=0
```

### 15.3 `s31psram`：现在会诚实报 FAIL

```
msh > s31psram
s31psram: internal heap free=232576, psram free=0
s31psram: FAIL, no PSRAM in the heap at all
  check CONFIG_SPIRAM, and the boot log for
  'oct_psram: ... density' and 'MSPI Timing'
```

这是**正确行为**：当前 `sdkconfig.defaults` 里 PSRAM 是关的，原因见 §15.4。

### 15.4 为什么 PSRAM 暂时关着

板子是 **16 MB Octal PSRAM**，但它现在还不能用：

| 速度 | 结果 |
|---|---|
| 200 MHz | 启动停在 `MSPI Timing: Enter psram timing tuning`，**永不返回** |
| 100 MHz | 同上 |
| **40 MHz** | **能启动**，`s31psram` 写读 64 KB 图案一致，堆里出现 16.7 MB |
| 40 MHz + 跑 `s31test irq` | **中断 WDT 超时**（`MCAUSE=0x18` → 中断 8） |

40 MHz 能用但会打崩中断，所以默认关着。要重新试：

1. 打开 `sdkconfig.defaults` 里注释掉的 6 行 `CONFIG_SPIRAM*`（保持 `SPEED_40M`）
2. **不要**用 100M/200M，会挂死在 timing tuning
3. 烧写后先跑 `s31psram`，再跑 `s31test`，看 `irq` 是否还崩

### 15.5 两条已修的底层问题（即使 PSRAM 关着也成立）

**ISR 必须在 IRAM。** `_interrupt_handler` 在 IRAM，但它下面的
`rtt_trampoline` / `s31_gpio_isr` / `s31_uart_isr` / `s31_timg_isr` 原来在
`.flash.text`。flash 写/擦或 MSPI 操作期间缓存关闭，这些代码取不出来。
已全部加 `IRAM_ATTR`。可用 `nm` 自检：

```powershell
riscv32-esp-elf-nm build\s31_rtt_test.elf | Select-String "s31_gpio_isr|rtt_trampoline"
# 地址应以 2f 开头（IRAM），不是 40（flash）
```

**未装 handler 的电平线会被屏蔽。** `rtt_trampoline` 现在发现 `fn == NULL` 就屏蔽该线
并打印一次 `intr: vector N fired with no handler, masked`。没有这条，
一条空槽电平线会让整个系统活锁（只有 300 ms 中断看门狗能报出来）。

### 15.6 新文件加命令的规则（踩了两次）

`s31psram` / `s31led` 编译进了 `librtthread.a`，`ar t` 看得到，但 `help` 里没有，
`FSymTab` 里也没有——因为两条命令只靠 `MSH_CMD_EXPORT_ALIAS` 注册，**没有任何全局符号
被引用**，`--gc-sections` 把整个成员丢了。这和之前 `list` 消失是同一个坑。

**规则：新文件只要靠段注册（msh 命令、`INIT_*_EXPORT`），就必须有一个被引用的全局符号
当锚**，然后验证：

```powershell
riscv32-esp-elf-nm build\s31_rtt_test.elf | Select-String "s31_board_extra_init"
Select-String -Path build\s31_rtt_test.map -Pattern "FSymTab"   # 应看到该 obj 的字节数
```

## 16. `s31intr` 与中断屏蔽的两个真缺陷（2026-10-31 凌晨）

### 16.1 `s31intr`

```
msh > s31intr
inum ie ip lvl trig handler name
---- -- -- --- ---- ------- ----
   0  1  0   1  lvl yes     src 0
   2  1  0   1  lvl yes     -
   3  1  0   1  lvl yes     -
   5  1  0   1  lvl yes     -
  24  1  0   4  lvl NO      -      <- T1_WDT，故意豁免
  25  1  0   4  lvl NO      -      <- CACHEERR，故意豁免
  27  1  0   4  lvl NO      -      <- ASSIST_DEBUG，故意豁免

3 line(s) are ENABLED with NO handler.
```

- `s31intr -a` 连全零的线也打印
- `s31intr -m <inum>` 屏蔽一条线

**判读**：只要出现 `ie=1 handler=NO` 且**不是 24/25/27**，那条电平线就是活锁源头
（trap 返回 → `mret` 立刻重入 → 线程再也不跑 → 只有中断看门狗报告）。
24/25/27 是 IDF 故意交给汇编 panic 路径的，屏蔽它们会关掉看门狗本身。

### 16.2 缺陷：`rt_hw_interrupt_mask()` 曾经完全无效

`esprv_int_disable()` 在 S31 上被链接到 **ROM**：

```
PROVIDE (esprv_int_disable = esprv_intc_int_disable)   0x2f80075c  <- ROM
PROVIDE (esprv_int_enable  = esprv_intc_int_enable)    0x2f800758  <- ROM
```

ROM 那几个是**老 INTC 接口，不写 CLIC 的 INTIE 位**。
`components/riscv/interrupt_clic.c` 的 CLIC 版本在这颗芯片上被 PROVIDE 覆盖了。

**怎么发现的**：`s31intr -m 16` 打印 `masked line 16`，紧接着 dump，`ie` 还是 1。
**修法**：直接读改写 CLIC 的 IE 位（`interrupt.c` 的 `rtt_clic_set_ie()`），
不再经过任何 `esprv_int_*`。

**为什么自检一直是绿的**：GPIO 驱动有自己的按引脚 `int_ena`，`irq`/`nest` 测试
直接写 status 寄存器触发，完全绕开线路屏蔽。**绿色的测试对没被测到的接口毫无发言权。**

### 16.3 缺陷：清扫会误伤中断看门狗

清扫生效后剩下的 24/25/27 必须**显式豁免**。IDF 把这三条交给汇编 panic 路径
（`esp_system/int_wdt.c:181-184` 有注释），handler 表里本来就是空的。
屏蔽 24 等于把唯一能报告挂死的机制关掉。已在 `rt_hw_intr_mask_unhandled()` 里豁免。

### 16.4 PSRAM 的现状与重开方法

| 配置 | 结果 |
|---|---|
| 100M / 200M | **abort**：`RT-Thread scheduler entry while flash cache is disabled` |
| 40M | 启动正常，`s31psram` **PASS**（64 KB @ `0x50000a1c`，池 16.7 MB） |
| 40M + `s31test irq` | **中断 WDT**，MEPC 在 `s31_gpio_isr` 尾声 |

100M/200M 的 abort 不是挂死，是**我们自己加的断言**：`portable/riscv/port.c:541`
的 `prvAssertCacheEnabled()` 是 RT-Thread 移植的不变量，而 MSPI 调时序时
合法地在关 cache 下走临界区。方向已明确，属于 PSRAM 的前置工作。

重开 PSRAM（`sdkconfig.defaults` 里注掉的 7 行，保持 `SPEED_40M`）：

```powershell
# 取消注释 CONFIG_SPIRAM* 那几行，然后
Remove-Item project\sdkconfig
powershell -ExecutionPolicy Bypass -File tools\build.ps1 build
powershell -ExecutionPolicy Bypass -File tools\build.ps1 -p COM16 flash
```

烧完先 `s31psram`，再 `s31test`。**不要用 100M/200M。**

### 16.5 教训

这一轮两个缺陷都属于同一类：**接口看起来接上了，实际接的是别的东西**。

- I2C 引脚（§15.1）：接的是 I2S 的脚，扫了一条空总线
- 线路屏蔽（§16.2）：调的是 ROM 的老 INTC 函数，CLIC 的 IE 位没动

共同点：**没有验证"我改的东西真的改了"**。所以现在的规则是——
任何"设置"类操作，跟一个能读回状态的检查：

| 操作 | 读回检查 |
|---|---|
| 屏蔽/使能线 | `s31intr` 看 `ie` 列 |
| 配置引脚 | 示波器或 `s31pin` 读回 |
| I2C 总线 | `i2cscan` 至少要能报出正确的总线状态 |
| 段注册的命令 | `nm` + map 里的 FSymTab 字节数 |

## 17. `s31intr` 的计数器，与"不要相信 MEPC"（2026-10-31）

### 17.1 `s31intr` 现在带计数

```
msh > s31intr
inum ie ip lvl trig handler     cnt name
---- -- -- --- ---- ------- ------- ----
   0  1  0   1  lvl yes           0 SYS_ICM
   4  1  0   1  lvl yes           0 -
   5  1  0   1  lvl yes          18 -
  24  1  0   4  lvl NO            0 -
  25  1  0   4  lvl NO            0 -
  27  1  0   4  lvl NO            0 -
```

- `cnt` = 这条线的 handler **被调用过多少次**（只在 `rtt_trampoline` 里自增）
- `name` = 该线上的真实中断源（`esp_isr_names[]`），不再是 `src N`

**`cnt` 是判断"中断风暴"的唯一可靠依据**：一条线如果 `cnt` 在飞快增长，
就是风暴；如果 `cnt` 是 0 而系统挂了，**说明根本没进中断**。

### 17.2 教训：MEPC 不能当调用栈用

前几轮我多次做这样的推理：

> WDT dump 里 `MEPC = 0x400179e8`，符号化是 `s31_gpio_isr`，所以是 GPIO ISR 在风暴。

**这个推理是错的。** `MEPC` 只是 WDT 采样那一刻 CPU 最后执行过的地址。
如果 CPU 卡在别的地方（比如某个循环里），`MEPC` 可以是任何残留值。

正确的做法：**看 `s31intr` 的 `cnt`**。这一轮 PSRAM 下 `irq` 崩溃时：

```
MEPC : 0x2f8036ce  （符号化什么都查不到）
cnt  : line 4 = 0   （一次都没进过中断）
```

两条证据合起来才说明：**它不是中断风暴，中断根本没发生**。

### 17.3 已查清与未查清

| 项 | PSRAM 关 | PSRAM 开 @40M |
|---|---|---|
| `s31test tick/sem/fpu` | PASS | **PASS** |
| `s31wifi` | PASS | **PASS** |
| `s31psram` | — | **PASS**（64 KB，池 16.7 MB） |
| `s31test irq`（任意 pin） | PASS | **活锁** |
| `s31intr` 读状态 | 正常 | **正常** |

所以：**PSRAM 本身没问题，非中断路径也没问题**，
只有 `rt_pin_attach_irq` + `rt_pin_irq_enable` 这条路径在 PSRAM 下活锁。

屏蔽中断看门狗（`s31intr -m 24`）后，活锁改报
`RT-Thread scheduler entry while flash cache is disabled`，
栈上同时出现 `mspi_timing_config_set_psram_clock` —— 这条线索指向
**pin IRQ 初始化与 MSPI 时钟切换的交互**，下一轮从这里拆。

### 17.4 又一个"改了没生效"：sdkconfig 的 choice 型配置

这一轮我有三次以为自己在测 40M，实际编译的是 200M：

1. 字符串替换 `CONFIG_SPIRAM_SPEED_200M=y` 但没改 `CONFIG_SPIRAM_SPEED=200`，
   kconfig 重新生成时按默认值覆盖回去
2. 改了 `sdkconfig.defaults`，但 `sdkconfig` 已存在，**不会重新生成**

**规则**：改 PSRAM 速度这类 `choice` 配置——

```powershell
# 1. 先改 sdkconfig.defaults
# 2. 删掉 sdkconfig 让它重新生成
Remove-Item project\sdkconfig
powershell -ExecutionPolicy Bypass -File tools\build.ps1 build
# 3. 读回确认，不要只看"编译成功"
Select-String -Path project\sdkconfig -Pattern "CONFIG_SPIRAM_SPEED"
```

这和第 16.5 节是同一类：**设置了就要读回验证**。只不过这次验证的是
"kconfig 真的接受了我写的值"，而不是"寄存器真的被我改了"。

## 18. PSRAM 与中断终于共存（2026-10-31 凌晨）

### 18.1 现在的默认配置

`sdkconfig.defaults` 里 **PSRAM 正式开启**（40 MHz，CAPS_ALLOC）：

```
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y
CONFIG_SPIRAM_SPEED_40M=y
CONFIG_SPIRAM_USE_CAPS_ALLOC=y
CONFIG_SPIRAM_IGNORE_NOTFOUND=y
```

查看：

```
msh > s31psram
s31psram: internal heap free=226120, psram free=16774632
s31psram: allocated 262144 bytes at 0x50000a1c (psram)
s31psram: PASS, 256 KB written and read back
```

`s31psram` 不带参数测 64 KB，`s31psram 256` 测 256 KB。

### 18.2 之前那个"PSRAM 一开就崩"到底是什么

**不是 PSRAM，是 GPIO 驱动抢了 OS tick 的中断线。**

```
vSystimerSetup()   freertos/port_systick.c:86
    esp_intr_alloc(ETS_SYSTIMER_TARGET0_INTR_SOURCE + cpuid, ...)
    -> 分配器挑第一条可用线；PSRAM 开时正好是 line 4

gpio.c（旧）
    rt_hw_intr_route(ETS_GPIO_INTR0_SOURCE, ETS_GPIO_INUM /*==4*/)
    rt_hw_interrupt_install(4, s31_gpio_isr, ...)
```

GPIO 驱动把 tick 的源挤走、handler 覆盖掉，tick 停 → 调度器停 → 看门狗报。

**PSRAM 关时能工作纯属运气**：那时分配器给 tick 的是另一条线，line 4 空着。

修法：线号向 `esp_intr_alloc` 要，不硬编码。启动日志会打：

```
gpio: irq on line 7 (allocated, source 61)
```

### 18.3 `s31pinirq`：确认 GPIO 用哪条线

```
msh > s31pinirq 10
s31pinirq: source 61 -> line 7 (from esp_intr_alloc)
  line ie=1 handler=yes entries=200
  pin  10: int_ena=0 status=0
```

只读命令。`entries` 是这条线被进入的次数——跑过 200 次 `irq` 测试后应该是 200，
如果是 0 说明中断根本没进（这条判据帮我找到了真凶）。

### 18.4 一条以后必须遵守的规则：不要硬编码硬件资源编号

同一个 `#define ETS_GPIO_INUM = 4`，在不同配置下对应不同外设：

| 配置 | line 4 是谁 |
|---|---|
| PSRAM 关 | GPIO（运气） |
| **PSRAM 开** | **SYSTIMER_TARGET0（OS tick）** |

**规则**：CPU 中断线、DMA 通道、GPIO 编号这类"谁先要谁拿"的资源，
一律向分配器申请（`esp_intr_alloc` / `esp_dma_chan_alloc` 等），
拿到手后**打印出来**，不要在驱动里写死。

`gptimer.c` 已经是这么做的（`esp_intr_alloc`），`serial.c` 仍用固定线
（UART0 的 5 号线是芯片固定映射，可以接受），`gpio.c` 这一轮改掉了。

### 18.5 自检现在是什么状态

PSRAM 开启下的完整结果：

```
[PASS] tick / ctx / preempt / fpu / irq / nest / sem
[PASS] fbshim × 12
=== 20 passed, 0 failed ===

s31psram: PASS, 256 KB
s31wifi:  PASS, mac 1c:29:04:d0:14:1e
s31ble:   PASS, addr 1c:29:04:d0:14:1f
check_image.py: 9/9
```

这是第一次 PSRAM 和中断测试同时通过。

### 18.6 仍然存在的问题

| 问题 | 状态 |
|---|---|
| PSRAM 100M / 200M 无法启动 | **未修**（R28）：MSPI 调时序在关 cache 的情况下走临界区，撞上移植加的 `prvAssertCacheEnabled()` 断言 |
| 40M 的带宽上限 | 约 5 MB/s。LCD 帧缓冲够用，高帧率视频不够 |

## 19. PSRAM 现在跑 200 MHz（2026-10-31）

### 19.1 四档速度全部可用

| 速度 | 结果 | 备注 |
|---|---|---|
| 40 MHz | PASS | MSPI 时序调优被编译掉 |
| 100 MHz | PASS | |
| **200 MHz** | **PASS（默认）** | 留一档余量 |
| 250 MHz | PASS | 器件上限 |

改速度的方法（**必须重建 sdkconfig**）：

```powershell
# 1. 改 sdkconfig.defaults 里的 CONFIG_SPIRAM_SPEED_200M 那一段
# 2. 删掉 sdkconfig，让 kconfig 重新生成
Remove-Item project\sdkconfig
powershell -ExecutionPolicy Bypass -File tools\build.ps1 build
# 3. 读回确认，别只看"编译成功"
Select-String project\sdkconfig -Pattern "CONFIG_SPIRAM_SPEED_"
# 4. 烧写 + 验证
powershell -ExecutionPolicy Bypass -File tools\build.ps1 -p COM16 flash
```

### 19.2 验证 PSRAM 的正确方法

```
msh > s31psram 4096
s31psram: internal heap free=221112, psram free=16774632
s31psram: allocated 4194304 bytes at 0x50000a1c (psram)
s31psram: PASS, 4096 KB written and read back
```

**不要靠启动日志判断**。200M 下 `boot_log` 仍然打
`MSPI Timing: Enter psram timing tuning`，而且 `Readlatency` 字段与 40M 相同
（都是 `0x04`）——调优的目标是 DQS 相位/延迟线，不是这个字段。
**日志没变不代表调优没跑**；判据只能是功能：图案写读一致 + 全量自检。

### 19.3 之前为什么 100M/200M 起不来

不是 PSRAM 的问题，是**移植自己加的断言误伤**：

```c
/* freertos/.../portable/riscv/port.c */
if (!cache_hal_is_cache_enabled(...)) esp_system_abort(
    "RT-Thread scheduler entry while flash cache is disabled");
```

MSPI 调时序时必须关 cache 进临界区（`mspi_timing_tuning.c:654` 的
`disable_cache()` → `mspi_timing_enter_low_speed_mode()`），退出临界区时
这条断言就 abort 了。

**修法**：断言改成"问内核会不会真的调度"——

```c
if (rt_critical_level() == 0 && rt_thread_self() != RT_NULL) {
    /* 只有这时才可能真的切换 */
}
```

`vPortYield()` 和 `vPortYieldFromISR()` 两处**仍然无条件检查**，
因为那才是切换真的要发生的地方。

### 19.4 当前建议

- LCD 帧缓冲、摄像头缓冲用 `heap_caps_malloc(len, MALLOC_CAP_SPIRAM)`
- 不要把 RT-Thread 的堆搬进 PSRAM（`IDF_RTTHREAD_HEAP_SIZE` 仍留在内部 DRAM）：
  内核对象在 PSRAM 上会在 cache 关闭期间不可访问
- 需要中断里访问的缓冲**不要**放 PSRAM

## 20. SPI 双总线（2026-10-31）

### 20.1 设备注册情况

```
msh > list device
uart0        Character Device     1
spi1         SPI Bus              0
spi0         SPI Bus              0
i2c0         I2C Bus              0
pwm0         PWM Device           0
pin          Pin Device           0
```

`spi0` = GPSPI2，`spi1` = GPSPI3。SPI0/SPI1 是 flash 控制器，不动。

引脚（`port_board.h`）：

| 总线 | SCLK | MOSI | MISO | CS |
|---|---|---|---|---|
| spi0 | IO8 | IO9 | IO10 | IO11 |
| spi1 | IO12 | IO13 | IO14 | IO15 |

**故意不用 LCD 排座（IO3..IO7）**——那几个脚和 I2S 共享，
启动时就抢走会悄悄破坏音频。LCD 驱动按需申请。

### 20.2 回环自测

```
msh > s31spiloop 0
s31spiloop: spi0 tx/rx (tie MOSI to MISO for a real test)
  tx: a5 5a 01 80 ff 00 3c c3
  rx: 00 00 00 00 00 00 00 00
s31spiloop: rx differs - expected unless MOSI is jumpered to MISO
```

**`rx` 全零是正常的**（板上 MOSI/MISO 没短接）。用杜邦线把 IO9 和 IO10 短接
再跑，应该看到 `s31spiloop: PASS, loopback identical`。

`rx` 全零但仍返回 8 字节，说明**传输本身成功**——时钟、FIFO、位序都在工作。

### 20.3 写 SPI 驱动必须知道的五件事（全部踩过）

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | `SPI2`/`SPI3` 符号不存在 | 用 `GPSPI2`/`GPSPI3` |
| 2 | 信号名不是 CLK/MOSI/MISO | `SPI2_CK_PAD_OUT_IDX` / `SPI2_D_PAD_OUT_IDX` / `SPI2_Q_PAD_IN_IDX` / `SPI2_CS_PAD_OUT_IDX` |
| 3 | `spi_ll_enable_bus_clock` 是宏，要 `__DECLARE_RCC_ATOMIC_ENV` | 包在 `PERIPH_RCC_ATOMIC() { ... }` 里 |
| 4 | **只开 bus clock 不够，`trans_done` 永不来** | 还要 `spi_ll_enable_clock(host, true)`（HS/mst 是另一个门） |
| 5 | `rt_device_find("spi0")` 是**总线**不是设备 | 先 `rt_spi_bus_attach_device()` 挂一个 `rt_spi_device` |

第 4 条最隐蔽：`usr=1` 写下去了，看起来传输开始了，但状态机没时钟，
`trans_done` 永远不来，表现为 `spi0: transfer timeout`。

### 20.4 用 SPI 设备的标准写法

```c
static struct rt_spi_device my_dev;

rt_spi_bus_attach_device(&my_dev, "flash", "spi0", RT_NULL);

struct rt_spi_configuration cfg = {
    .mode = RT_SPI_MODE_0 | RT_SPI_MSB,
    .data_width = 8,
    .max_hz = 1000000,
};
rt_spi_configure(&my_dev, &cfg);

/* 写命令 + 读数据 */
rt_spi_send_then_recv(&my_dev, cmd, 1, buf, len);
```

### 20.5 尚未实现

| 项 | 原因 |
|---|---|
| DMA | GPSPI DMA 是描述符环 + 独立分配器，轮询够用 |
| Dual/Quad/OCT 线模式 | 需要 flash 风格的 command/addr/dummy 三阶段，是另一套 API |
| 从机模式 | 未验证 |

## 21. ADC（2026-10-31）

### 21.1 设备与通道映射

```
msh > list device
adc2         ADC Device           0
adc1         ADC Device           0
```

映射是**硅片固定的**（`soc/adc_channel.h`），不是板级选择：

| 单元 | 通道 | GPIO |
|---|---|---|
| adc1 | 0..7 | **42..49** |
| adc2 | 0..7 | **50..57** |

**⚠️ GPIO50/51 就是这块板的 I2C 引脚。** 所以 **adc2 与 i2c0 不能同时用**。
`s31adc 2 0` 会拒绝，除非加 `force`。

### 21.2 读一个通道

```
msh > s31adc 1 1
s31adc: adc1 ch1 (GPIO43), 17-bit raw
  read 1: 2599
  read 2: 2599
  read 3: 2585
s31adc: no calibration on this port - the raw code is the result,
  not millivolts. See the note in port_adc.h.
```

连读 3 次是为了让"参考还没稳"这件事可见，而不是藏在一个平均值里。
实测同通道重复测量稳定到 ±10 LSB。

### 21.3 **没有毫伏换算**，这是故意的

`get_vref()` 返回 **-1（未知）**。原因：

- S31 **没有** `SOC_ADC_CALIBRATION_V1_SUPPORTED`
- S31 **没有** RTC 控制器

IDF 的校准组件在这颗芯片上给不出 raw→mV 系数。写一个"典型 Vref=1100mV"
看起来权威，实际每块板偏几个百分点，而所有下游计算都会继承这个错误**且毫无提示**。

**raw code 是测量值，猜出来的电压不是。** 需要校准电压，得先读出芯片
eFuse/模拟通路的校准数据——那是真活，不是一个默认值。

### 21.4 调试命令

```
msh > s31adcdbg 1
adc1 before: int_raw=0x00000000 ctrl0=0x0003c000 ctrl1=0x0003c000
  ctrl2=0x0000a000 ctrl_date=0x82508220 ref=0xe0000000
  sar1_data_status=0x00000000 sar2=0x00000000
  patt_tab1 sar1=0x00f00000 sar2=0x00f00000
adc1 conv: done=1 loops=77
  int_raw=0x80000000 int_clr=0x00000000
  sar1_data_status=0x0000070f sar2=0x00000000
  raw(unit)=1807
  ctrl0=0x0c800008 ctrl1=0x0003c000
```

**这个命令是本轮的关键工具**。`s31adc` 返回 0 时，有三种完全不同的可能：
转换没启动 / 启动了但我读的寄存器不对 / 引脚真的 0V。
猜是没用的，dump 一次就分清了——`done=1, raw=1807` 说明硬件正常，
问题只在通道选择。

### 21.5 写 ADC 驱动必须知道的两件事（都踩过）

**1. `rt_adc_enable()` 里要编程通道，不能只记软件变量。**

```c
/* 错：enabled 只记 bookkeeping，convert 里判断"通道变了才编程" */
enabled():  adc->channel = channel;
convert():  if (adc->channel != channel) { 编程; }   /* 永远为假 */

/* 对：enabled 就把 pattern 编程下去 */
```

**2. pattern 表每次转换都要重编程。**

表里只有 1 项，转换一次指针就走过表尾，下次读 0。
`adc_oneshot_ll_program_pattern()` 里的 `patt_p_clear` 脉冲就是复位指针。
IDF 的 `adc_oneshot_hal_setup()` 是每次 convert 前都调。

**这两个坑的共同点：都是"我以为硬件会记住，它没有"。**
一个记的是通道号（在软件里），一个记的是指针（在硬件里）。

### 21.6 通道读数的解读

八通道扫一遍（未接信号）：

```
ch0 1767   ch1 2599   ch2 1799   ch3 2626
ch4 1767   ch5 2621   ch6 1766   ch7 2598
```

偶数组约 1770，奇数组约 2600。**重复扫描值不变，同通道重复测量稳定**，
所以这是**板上模拟前端的实际状态**（引脚悬空），不是驱动问题。

判据是"**同通道可重复**"，不是"通道间相同"——后者会让人去修一个不存在的 bug。

## 22. PCNT 脉冲计数器（2026-10-31）

### 22.1 用法

```
msh > s31pcnt 0 16 100
s31pcnt: unit 0 on GPIO16, driving 100 rising edges
  expected 100, counted 100
s31pcnt: PASS
```

参数：`<unit 0..3> <gpio> [边数，默认 100]`。

四路 unit 都验证过：

| unit | gpio | 边数 | 结果 |
|---|---|---|---|
| 0 | 16 | 100 | PASS |
| 1 | 17 | 250 | PASS |
| 2 | 18 | 1000 | PASS |
| 3 | 19 | 50 | PASS |

### 22.2 这条命令怎么"不接线的"

PCNT 数的是**引脚上的边沿**。而引脚的输入通路看到的是"这个脚被驱动到什么电平"，
**包括被它自己的输出驱动器驱动**。

所以自测的取巧办法是：**软件把这个脚拉高拉低 N 次，让 PCNT 数同一个脚**。
这是一条完整通路（输出驱动 → 焊盘 → 输入矩阵 → 毛刺滤波 → 计数器），
只是不需要跳线。

### 22.3 如果计数不对

命令会直接给出两个最可能的原因：

```
s31pcnt: FAIL (delta -99)
  A short count usually means the edges were faster than the
  counter can resolve; retry with fewer edges.
```

**这个提示是踩坑踩出来的**：第一版连写两次电平寄存器，100 个边沿只数到 **1** 个。
因为 PCNT 在 APB 时钟（80 MHz，12.5 ns）上采样，而两次背靠背的寄存器写在 320 MHz 上
只有几十纳秒的高电平——**采样器整个高电平期都没踩到**。

修法是每个电平保持 2 µs。**所以自己写驱动时，脉宽必须 ≥ 几个 APB 周期。**

**关键认知：这种失败看起来像驱动 bug（计数没涨），实际是信号比采样率快——
物理约束，不是软件错误。**

### 22.4 S31 上 PCNT 的四个要点

| # | 要点 |
|---|---|
| 1 | 两个 group（PCNT0/PCNT1），每 group 4 unit，每 unit 2 信号通道 + 1 控制通道 |
| 2 | **APB 是唯一合法时钟源**，其它值直接 assert |
| 3 | 计数寄存器是 **16 位有符号**，`pcnt_ll_get_count()` 已做符号扩展；32767 之后回绕是硬件行为 |
| 4 | 毛刺滤波阈值以 **APB 周期**计，默认关闭；真实编码器通常需要 |

**信号索引是通道优先，不是 unit 优先**：
`PCNT0_SIG_CH<channel>_IN<unit>`——和这个 port 里其它外设的索引习惯相反。

### 22.5 为什么没有 `rt_device`

RT-Thread **没有脉冲计数器设备类**，没有 `rt_pcnt_*` API。
硬套一个只有一个调用者的 `rt_device` 包装只会增加间接层而不增加契约。
所以这里是**纯 C API + msh 命令**。

API 在 `port_pcnt.h`：

```c
rt_err_t rt_hw_pcnt_init(void);
rt_err_t rt_hw_pcnt_setup(int unit, rt_base_t pin);   /* 数上升沿 */
int      rt_hw_pcnt_get(int unit);
void     rt_hw_pcnt_clear(int unit);
```

## 23. MCPWM 互补 PWM（2026-10-31）

### 23.1 用法

```
msh > s31mcpwm 1000 500 10
s31mcpwm: 1000 Hz, duty 500/1000, dead 10 ticks
  A=GPIO20  B=GPIO21 (complementary)
  edges in 50 ms: 49 -> ~980 Hz (want 1000)
  measuring duty on GPIO20 ...
  high 1277774 cycles of 2558426 -> duty 499/1000 (want 500)
s31mcpwm: PASS (frequency and duty both measured on the pad)
```

参数：`[频率 50..100000] [占空比 0..1000] [死区 tick]`。

实测覆盖：

| 频率 | 占空比 | PCNT 测频 | mcycle 测占空 | 判定 |
|---|---|---|---|---|
| 100 Hz | 250 | ~97 Hz | **249/1000** | PASS |
| 500 Hz | 500 | ~500 Hz | **492/1000** | PASS |
| 1000 Hz | 500 | ~980 Hz | **499/1000** | PASS |
| 2000 Hz | 300 | ~2000 Hz | 不测 | PASS（仅频率） |
| 5000 Hz | 750 | ~4900 Hz | 不测 | PASS（仅频率） |
| 20000 Hz | 100 | ~19940 Hz | 不测 | PASS（仅频率） |

### 23.2 两把尺子

| 量什么 | 怎么量 | 适用上限 |
|---|---|---|
| 频率 | PCNT 数输出脚上的上升沿 | 全频段 |
| 占空比 | mcycle 轮询同一个脚，量高电平 / 周期 | **1 kHz** |

**都从输出引脚本身读**，不需要示波器、不需要跳线——A 脚同时是 MCPWM 输出和 GPIO 输入。

**超过 1 kHz 时占空比报"未测量"，不报 FAIL。**
理由：轮询法在 2 kHz 已经会测出 285/1000（实际 300），
**那是量具失效，不是 PWM 错**。怪错组件比不测更糟。

### 23.3 四个踩过的坑

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | `MCPWM_TIMER_START_STOP_FULL` **是一次性命令**（跑到峰值就停） | 用 `MCPWM_TIMER_START_NO_STOP` |
| 2 | 定时器启动早于预分频器写入 → 预分频变空操作（100 Hz 测出 700 Hz = 7×） | **所有分频和动作写完再启动** |
| 3 | `timern_period` 在 `[23:8]` 不是 `[15:0]` | 用 `HAL_FORCE_READ_U32_REG_FIELD`，别用 `& 0xFFFF` |
| 4 | `high_cycles * 1000` 在 100 Hz 下溢出 uint32（6.4e9） | 用 `rt_uint64_t` 中间量 |

第 4 条的表现最有欺骗性：**500 Hz 能过、100 Hz 算出 82/1000**（实际 250）——
典型的溢出 bug 形状（某些输入刚好不溢出）。

### 23.4 测量代码自身也踩了三个坑

这三条都属于"量具失效但看起来像被测对象失败"：

| # | 现象 | 原因 | 修法 |
|---|---|---|---|
| 1 | 100 Hz 测出 80 Hz | 50 ms 窗口只有 5 个边沿，量化误差 20% | **自适应窗口**（≥20 个周期） |
| 2 | 1000 Hz 一度测出 170779 cycles | 单周期抖动 | **累加 8 个周期** |
| 3 | 50% 占空比测出 333/1000 | 从下降沿起步，量的是 低+高+低 | **以上升沿为唯一锚点** |

### 23.5 没有验证的部分

- **死区的实际纳秒宽度**：只编程并回读寄存器，没用示波器量
- **输出的模拟形状**（上升时间、过冲）

这两条在 `port_mcpwm.h` 的头注释里明确写了"未测量"。

### 23.6 引脚

`port_board.h`：

```c
#define BOARD_MCPWM_A_PIN       20
#define BOARD_MCPWM_B_PIN       21
```

IO20/21 是这块板上的空闲脚：不是 I2C（50/51）、RGB（60）、BOOT1（61）、
flash（29/41），也不是 LCD/I2S 排座（3..7）。

## 24. RMT（WIP，2026-10-31）

### 24.1 当前状态：**未完成**

```
msh > s31rmt 100 20
s31rmt: 20 symbols x 100 us per half on GPIO22
  memory : symbol[0]=0x0fa08fa0 (both halves 4000 ticks = 100000 ns)
  rmt: no trans_done after 20 ms (raw=0x00000000 ena=0x00000001)
  TX call took 4 ms
  rising edges counted: 707
  ...

s31rmt: memory writes verified and the transmitter produces
  edges, but frame-length control is NOT yet correct.
  Treat RMT as WIP; do not build on it yet. See the log.
```

**能用的**：符号存储写入、时钟、引脚路由、能发出边沿。
**不能用的**：帧长控制（20 个符号发出 707 个边沿）、完成检测。

**WS2812 目前请继续用 `s31led`**（位碰撞版），它是能用的那个。

### 24.2 已经踩平的三件事（写驱动必看）

| # | 坑 | 正确做法 |
|---|---|---|
| 1 | **符号存储默认掉电**，写入读回全 0 | `rmt_ll_mem_force_power_on()` —— 独立硅片、独立低功耗域，开总线时钟不会带上它 |
| 2 | **APB 窗口默认映射 FIFO**，`RMTMEM[...]` 写进 FIFO | `rmt_ll_enable_mem_access_nonfifo(dev, true)`；验证 `sys_conf.apb_fifo_mask` 变成 1 |
| 3 | **`int_st` 是屏蔽后状态，`int_ena` 默认 0** | `rmt_ll_enable_interrupt(dev, RMT_LL_EVENT_TX_DONE(ch), true)` |

第 2 条尤其值得注意：`sys_conf` 读回 `0x05000010`（mask=0）是唯一可靠的证据，
"我记得我加了"不算数。

### 24.3 顺带修的两个超时错误

**(a) `esp_rom_delay_us(1)` 不能当超时滴答。**
20000 次循环 = **23.2 秒**（每次 ~1.16 ms）。改成纯 cycle 自旋后 4 ms 返回。

**(b) mcycle 不能量 13 秒以上的跨度。**
32 位，320 MHz 下 13.4 秒回绕。23 秒的差值算出来"看起来合理"纯属巧合。
**量长时间用 `rt_tick_get()`**（毫秒，49 天才回绕）。

### 24.4 未解决项（下一轮从这里继续）

1. **帧长控制**：`tx_lim` 是阈值不是结束条件，`mem_size` 决定走几个 48 符号块。
   需要研究 IDF `rmt_tx.c` 的收尾机制（它用 `trans_done` + FSM）。
2. **`trans_done` 原始位不置位**（`ena=1`、`raw=0`）。
   怀疑 loop 模式下 done 被吞，或 `tx_conti_mode_chn` 仍为 1。

### 24.5 引脚

```c
#define BOARD_RMT_TX_PIN        22
```

**故意不用 IO60**（板载 SK6805）——`s31led` 占着那个脚，
而方波测试的全部意义是"可测量"，LED 的数据线不是。

## 24b. RMT 攻坚进展（2026-10-31，仍未完成）

### 24b.1 三个新证据

**(1) 零时长符号是结束标记。** IDF `rmt_tx.c:597-630`：

```c
// a RMT word whose duration is zero means a "stop" pattern
```

RMT **没有长度寄存器**，它沿块走直到遇到双半周期为 0 的符号。
上一轮"填满整块消灭 0 符号"的做法**正好删掉了唯一的停止标记**。

**(2) EOF 符号写不进索引 N，但索引 0 能。**

```
symbol[0]=0x0fa08fa0      <- 正确
symbol[20]=0xdbd7d9ab     <- 垃圾
```

`mem_size=1`（48 符号块），索引 20 在范围内。候选解释：
APB 窗口是流式的（写指针自增），或阵列不是假设的连续布局。

**(3) `tx_lim` 触发的是 THRES 不是 DONE。**

`int_raw=0x00000100` = bit 8 = `RMT_LL_EVENT_TX_THRES(0)`。
它是"该补数据了"的信号，**不是结束条件**。

### 24b.2 这一轮自己引入的两个 bug

| # | bug | 表现 | 修法 |
|---|---|---|---|
| 1 | cycle 预算单位错（`CPU_MHZ*50` = 50 µs 而非 50 ms） | 任何 >50µs 的帧被中途砍断，20 符号只数到 31 边沿 | `* 1000u` |
| 2 | 边沿计数没有空载基线 | 4 符号给 626、20 符号给 630，与帧长无关 | **先测空载** |

**第 2 条是方法上的关键**：加了基线之后

```
idle 50 ms : 0 edges (nothing driving the pin)
one frame  : 13179 edges, 50 ms
```

空载 0 说明引脚没噪声 —— 那 13179 就是**真信号**，即块确实在重放。
**基线把"量具坏了"和"被测对象坏了"区分开了。**

### 24b.3 现在能信什么

| 能力 | 状态 |
|---|---|
| 符号存储写入（索引 0） | ✅ 可验证 |
| 时钟 25 ns/tick | ✅ 寄存器可读 |
| 能发出边沿 | ✅ 13179/50ms |
| EOF 结束标记 | ❌ |
| 完成检测 | ❌ |

**WS2812 继续用 `s31led`**（位碰撞版）。`s31rmt` 输出末尾会明确写这行。

## 25. MCPWM 占空比现在报"不确定度"（2026-10-31）

### 25.1 为什么要改

`s31mcpwm 1000 500 10` 曾经**间歇性失败**。同一固件、同一配置，重复 4 次：

```
499/1000  PASS
490/1000  PASS
499/1000  PASS
470/1000  FAIL (off by 30)
```

PWM 的 compare 寄存器是固定的，不会自己变 —— 所以这是**量具抖动**。

### 25.2 现在的输出

```
msh > s31mcpwm 1000 500 10
  high 1277394 cycles of 2558031 -> duty 499/1000 (want 500)
  delta 1/1000 (sampler spread is ~30/1000 at this frequency)
s31mcpwm: PASS (frequency and duty both measured on the pad)
```

**`delta` 和 `sampler spread` 都打印出来**，读的人能判断这个偏差是信号还是噪声。

判定阈值是 **60/1000**——比噪声宽（不误报），又能抓住真错误
（compare 写错、死区吃掉脉冲、输出反相）。

### 25.3 抖动从哪来

```c
high   += (b - a);     // a = 上升沿, b = 下降沿
period += (c - a);     // c = 下一个上升沿
```

每次 `a` 都有轮询抖动，8 轮累加后两边累加的抖动不同步，比值就飘。

**试过但不够的两个修法**：取每周期中位数（仍有 `off by 23`）、
放宽容差到 20/1000（仍有 `off by 23`）。
最终是"报不确定度 + 60/1000 阈值"才稳定。

### 25.4 通则（已进风险登记 R47）

**判定阈值必须 ≥ 量具噪声，而且噪声要打印出来。**

用比噪声更紧的容差去判定，会把正确信号变成间歇性失败——
那比"报一个带不确定度的数"更糟。

## 26. RMT 完成（2026-10-31）

### 26.1 用法

```
msh > s31rmt 100 20
s31rmt: 20 symbols x 100 us per half on GPIO22
  memory: 48 symbols, write/read verified
  symbol[0]=0x0fa08fa0  EOF[20]=0x00000000
  chnconf0=0x00510140 conti=0 mem_size=1 tx_lim=20
  idle 50 ms : 0 edges (nothing driving the pin)
  one frame  : 20 edges, 4 ms, expected 20 edges and 4 ms
  idle 0, tx 20 edges in 4 ms (want 20 edges, 4 ms)
s31rmt: PASS (memory, frame length, edges and duration all measured on the pad)
```

参数：`[半周期 1..1000 µs] [符号数 1..47]`。

### 26.2 实测覆盖

| 半周期 | 符号数 | 边沿 | 时长 | 结果 |
|---|---|---|---|---|
| 50 µs | 40 | 40 | 4 ms | PASS |
| 100 µs | 1 | 1 | 0 ms | PASS |
| 100 µs | 10 | 10 | 2 ms | PASS |
| 200 µs | 7 | 7 | 2 ms | PASS |
| 500 µs | 4 | 4 | 4 ms | PASS |
| 1000 µs | 2 | 2 | 4 ms | PASS |

**边沿数 == 符号数，精确相等。** 另加 48 个符号的内存逐个比对。

### 26.3 五个根因（写 RMT 驱动必看）

| # | 根因 | 症状 | 修法 |
|---|---|---|---|
| 1 | **符号存储默认掉电** | 写入读回全 0 | `rmt_ll_mem_force_power_on()` |
| 2 | **APB 窗口默认映射 FIFO** | `RMTMEM[...]` 写进 FIFO | `rmt_ll_enable_mem_access_nonfifo()` |
| 3 | **`int_ena` 默认 0** | `trans_done` 读不到 | `rmt_ll_enable_interrupt()` |
| 4 | **读指针只在 init 回卷一次** | 第一帧对、第二帧 0 边沿 | **每次发射前 `rmt_ll_tx_reset_pointer()`** |
| 5 | **`carrier_en_chn` 复位值是 1** | 时长全对但每符号 32 个边沿 | `rmt_ll_tx_enable_carrier_modulation(dev, ch, false)` |

**第 5 条最隐蔽**：没人要求载波，它默认开着。
症状是"总时长正确、边沿数 32 倍"——**这个组合就是调制类功能的指纹**。

### 26.4 诊断顺序（下次遇到类似问题）

1. **总时长对不对？** 不对 → 长度控制问题（EOF 标记、mem_size、tx_lim）
2. **总时长对、边沿数不对？** → 调制类功能（载波、DMA 重载）
3. **第一帧对、第二帧不对？** → 指针/状态没复位
4. **写入读回全 0？** → 电源域或窗口语义

### 26.5 长半周期需要分频

时长字段 15 位，25 ns tick 只到 **819 µs**。
`rt_hw_rmt_square()` 自动抬分频器（1 ms 用 div=8）。
**`rt_hw_rmt_ws2812()` 固定 div=1**——WS2812 要 25 ns 分辨率，不能分频。

### 26.6 与 `s31led` 的关系

现在 RMT 是**参考量具**：40 MHz XTAL + 15 位硬件计数器，
不受 cache 和中断影响。`s31led` 的位碰撞版本靠数 CPU cycle，
可以用 RMT 的结果去校准它。

**`s31rmt` 现在报 PASS**，不再是 WIP。

## 27. TWAI/CAN（WIP，2026-10-31）

### 27.1 当前状态：**未完成**

```
msh > s31twai
s31twai: internal loopback + self test (stm, ilbp, tstm)
  pads: TX=GPIO23 RX=GPIO24
   125000 bit/s: no frame. buf state 6 -> 6 -> 6, tec=0 rec=0 intr=0x00000810
   250000 bit/s: no frame. buf state 6 -> 6 -> 6, tec=0 rec=0 intr=0x00000810
   500000 bit/s: no frame. buf state 6 -> 6 -> 6, tec=0 rec=0 intr=0x00000810
  1000000 bit/s: no frame. buf state 1 -> 6 -> 6, tec=160 rec=0 intr=0x00000854
s31twai: 0/4 bit rates round-tripped
```

**能用的**：时钟、存储上电、引脚路由、TX buffer 填充与 arm、状态机可观测。
**不能用的**：帧在总线上完成。

### 27.2 已排除的五个假设（每条都实测过）

| # | 假设 | 结论 |
|---|---|---|
| 1 | 只设 `stm` 就能回环 | ❌ 必须同时设 `ilbp`（片内 TX→RX） |
| 2 | RX 脚可以不拉 | ❌ CAN 空闲是隐性高，浮空则永远看不到 11 个连续隐性位 |
| 3 | TX buffer 空 = 状态 0 | ❌ 是编码：`1=Ready 2=TX 4=OK 6=Failed 8=Empty`，**复位值 8** |
| 4 | 三个可编程段之和 = quanta | ❌ 必须 = `quanta - 1`，同步段不可编程 |
| 5 | `afm` 默认就行 | ❌ `afm=1` 且无过滤器 = 全部丢弃，症状同"没回环" |

第 3 条最坑：`while (status != 0)` 永远等不到，四档全报 "TX buffer never became free"。

第 4 条影响比特率：16 quanta 里漏掉同步段 = 每位少 1 quanta，
1 Mbit/s 实际跑 1.33 Mbit/s。

### 27.3 下一轮入口

**关键观察：低三档 `tec=0` 也是失败，1 Mbps 才有 `tec=160`** ——
这是**两个不同的问题**。

| 档位 | 现象 | 推测 |
|---|---|---|
| 125k/250k/500k | `buf 6 -> 6 -> 6`, `tec=0` | 帧可能根本没上总线 |
| 1 Mbps | `buf 1 -> 6 -> 6`, `tec=160` | 上了总线，位错误 |

另外 1 Mbps 的 `brp = 40M/(1M*16) = 2.5` 取整成 `2`，
**实际 1.25 Mbit/s** —— 这一档本身就不准。先修 `brp` 取整，再看低三档。

### 27.4 引脚

```c
#define BOARD_TWAI_TX_PIN       23
#define BOARD_TWAI_RX_PIN       24
```

RX 脚**必须有上拉**（代码里已加）。

### 27.5 过程教训

两次 `flash` 卡住 600 秒，原因是残留的 python 进程占着 COM16。
**flash 卡住先查进程**（`Get-Process python | Stop-Process -Force`），别干等。

## 28. TWAI/CAN 完成（2026-10-31）

### 28.1 用法

```
msh > s31twai          # 单脚回环（默认，能过）
msh > s31twai 0        # 两脚，需要外部连线或收发器
```

```
s31twai: internal loopback + self test (stm, ilbp, tstm)
  pads: TX=GPIO23 RX=GPIO23  (same pad, wire loop)
  mode=0x02270314  bmm=0 stm=1 ilbp=1 ena=0 tstm=1
   125000 bit/s: PASS  id 0x123, 4 bytes round-tripped
   250000 bit/s: PASS  id 0x123, 4 bytes round-tripped
   500000 bit/s: PASS  id 0x123, 4 bytes round-tripped
  1000000 bit/s: PASS  id 0x123, 4 bytes round-tripped
s31twai: 4/4 bit rates round-tripped
s31twai: PASS (controller path verified; no transceiver is fitted,
  so the analog layer is untested)
```

### 28.2 关键发现：`ilbp` 在 S31 上不生效

| 配置 | 结果 |
|---|---|
| 单脚 TX=RX=GPIO23 | **4/4 PASS** |
| 两脚 TX=23, RX=24 | `tec=160, rec=0`（全部失败） |

`ilbp`（内部回环）位**读到 1 但不产生行为**。
单脚能通证明控制器、时钟、时序、帧格式、TX buffer、RX FIFO 全部正确。

**要用真实 CAN 总线，必须外接收发器**（TX/RX 分两根线），
那时 `s31twai 0` 的配置才是对的，且需要收发器把总线拉成隐性。

### 28.3 四个踩过的坑

| # | 坑 | 症状 | 修法 |
|---|---|---|---|
| 1 | 没等 `FCSI` 状态切换 | `buf 1→6`，`tec=0` | `enable_hw(true)` 后等 FCSI 置起（带超时） |
| 2 | `brp` 截断 | 1 Mbps 实际 1.25 Mbps | 在 8..25 quanta 搜**精确解** |
| 3 | `set_operate_cmd` 无超时自旋 | **控制台死掉** | 直接写 `command.val` + cycle 预算 |
| 4 | RX 排空循环无上界 | 同上 | 最多 4 次 |

**第 3 条最容易误判**：`twaifd_ll_set_operate_cmd()` 内部是
`while (hw->command.val & commands);`，控制器 disable 时 bit 不自清 → 死循环。
第一次遇到时我以为是串口问题，杀了两次 python 进程。

### 28.4 `rwcnt` 不是字数

`twaifd_frame_buffer_t.format.rwcnt` 的注释是 **"Re-transmission counter"**。
我看到收到长度 8 以为是字数字段，写了 `rwcnt = (len+3)/4`，
**结果把全通过的测试改成全失败**。已回退。

**字段名有歧义时先读定义。**

### 28.5 未验证

**模拟层完全没测**：无收发器、无差分对、无终端电阻、无真实仲裁、无错误帧。
PASS 消息里写明了。

### 28.6 引脚

```c
#define BOARD_TWAI_TX_PIN       23
#define BOARD_TWAI_RX_PIN       24
```

**默认 `s31twai` 把 RX 也路由到 GPIO23**（单脚回环）。
RX 脚有上拉（真实总线需要隐性高）。

## 29. 复核"前置风险"：R24 是怎么错的（2026-10-31）

### 29.1 结论先行

plan 里长期挂着一条 **R24**："兼容层静态表在 SMP 下不互斥，
用 `rt_hw_interrupt_disable()` 只关本核"——列为 P3 最大机械改动。

**它是错的，改动量 0 行。**

### 29.2 怎么验证的（20 行探针）

```c
/* tools/tmp/probe_r24.c，镜像 rthw.h 的两个声明和那个 #define */
typedef long rt_base_t;
rt_base_t rt_cpus_lock(void);
#define rt_hw_interrupt_disable rt_cpus_lock

int shim_like_critical_section(int v)
{
    rt_base_t level = rt_hw_interrupt_disable();
    ...
}
```

```
$ riscv32-esp-elf-gcc -S -O2 probe_r24.c
probe_smp:
    tail    rt_cpus_lock
```

**编译器说它调用的是 `rt_cpus_lock`。**

### 29.3 为什么

`rthw.h:146`：

```c
#ifdef RT_USING_SMP
#define rt_hw_interrupt_disable rt_cpus_lock
#endif
```

`cpu_mp.c:168` 的 `rt_cpus_lock()` 做三件事：
关本地中断、`rt_enter_critical()`、**`rt_hw_spin_lock(&_cpus_lock)`**。

**所以 `RT_USING_SMP` 一开，shim 里同一行代码自动变成全局互斥。**

ISR 路径也安全：`cpus_lock_nest` 是嵌套计数，ISR 打断持锁线程时 `nest != 0`，
不重复取锁。

### 29.4 错误是怎么产生的

> "`rt_hw_interrupt_disable` 听起来就是关中断；SMP 下关中断只关本核；所以不互斥。"

三步推理，**第一步就错了**——那个名字是宏，含义由条件编译决定。

**同一类错误在项目里出现过三次**：
| 风险 | 按名字猜的 | 实际 |
|---|---|---|
| R24 | `rt_hw_interrupt_disable` = 关本核中断 | 宏，SMP 下 = 全局自旋锁 |
| R28 | `SPI2` 符号 | 叫 `GPSPI2` |
| R69 | `rwcnt` = 字数 | 是**重传计数器** |

### 29.5 规则

**涉及"某符号在某个配置下是什么"时，让编译器回答。**

做起来很便宜：写个 20 行探针，`gcc -S`，看 `tail` 到哪。
比读半小时源码 + 写进文档 + 后续所有工作都基于错误前提便宜得多。

### 29.6 顺带：P3 的真实边界

只改 `RT_CPUS_NR=2` + 换 `scheduler_mp.c`，构建给出 **8 条编译错误**：

```
'struct rt_cpu' has no member named 'priority_group'
'struct rt_cpu' has no member named 'priority_table'
'struct rt_sched_thread_ctx' has no member named 'oncpu'
'RT_CPU_DETACHED' undeclared
'struct rt_sched_thread_ctx' has no member named 'bind_cpu'
'RT_CPU_MASK' undeclared
'rt_hw_ipi_send' implicit declaration
'RT_SCHEDULE_IPI' undeclared
```

原因：**只加 `RT_CPUS_NR` 没定义 `RT_USING_SMP`**，这些成员全在 `#ifdef` 里。

**这 8 条就是"开 SMP 的待办清单"**，全部编译期枚举。已回滚，单核 20/20。

## 30. 开 SMP 的真实清单（2026-10-31 实测）

### 30.1 别只看"宏没定义"

只改 `RT_CPUS_NR=2` + 换 `scheduler_mp.c`，报 8 条"结构成员不存在"——
那是 `RT_USING_SMP` 没定义。补上定义后错误换一批，**那才是真的待办**。

### 30.2 第一批：头文件可见性

```
rttypes.h:161: fatal error: cpuport.h: No such file or directory
```

`rttypes.h` 在 SMP 下 `#include <cpuport.h>` 取 `rt_hw_spinlock_t`。
该头原本只在 rtthread 组件的 **PRIV_INCLUDE_DIRS** 里。

**修法**：加进全局：
```cmake
idf_build_set_property(INCLUDE_DIRECTORIES
    "...;${rtt_config_dir};${rtt_riscv_port_dir}" APPEND)
```

**这是一类通用问题**：往公共头里加 `#include` 时，被包含的头必须在**全局**路径下。

### 30.3 第二批：11 个缺失符号

| 符号 | 位置 | 怎么补 |
|---|---|---|
| `rt_interrupt_nest` | `vectors_rtthread.S` | **SMP 下是宏**（`rt_cpu_self()->irq_nest`），汇编不能用 `la` 取地址 |
| `rt_thread_switch_interrupt_flag` | `vectors_rtthread.S` | `cpuport.c` 的三个全局量挪进 `#else` |
| `rt_interrupt_from_thread` | `vectors_rtthread.S` | 同上 |
| `rt_interrupt_to_thread` | `vectors_rtthread.S` | 同上 |
| `rt_hw_spin_lock/_unlock/_init` | `components.c` | 抄 `libcpu/risc-v/common/cpuport.c:66` |
| `rt_hw_cpu_id` | `clock.c`、`idle.c` | 抄 `cpu.c:251` |
| `rt_hw_ipi_send` | `scheduler_mp.c` | 接 `ETS_IPC_ISR_INUM`(28) |
| `rt_hw_secondary_cpu_up` | `components.c` | 写 |
| `rt_hw_secondary_cpu_idle_exec` | `idle.c` | 写 |

### 30.4 最容易低估的一条

**`rt_interrupt_nest` 在 SMP 下不是变量。**

```c
/* irq.c:68 */
#define rt_interrupt_nest rt_cpu_self()->irq_nest
```

所以 `vectors_rtthread.S:144` 的 `la t0, rt_interrupt_nest` **编译不出符号**。
"per-CPU 化"不是"给全局量加核号偏移"，而是**这些量换了存在形式**——
汇编得改成按 `mhartid` 算 per-CPU 结构偏移。

### 30.5 spinlock 不用自己设计

`libcpu/risc-v/common/cpuport.c:66` 是完整的 ticket lock：

```c
amoadd.w.aqrl  取号 -> 自旋等 owner -> 半字存储释放
```

直接搬过来即可，含 `rt_hw_barrier()` 的内存序处理。

### 30.6 回滚：删整个 `build/`

改了源文件列表之后回滚，**只删 `.obj` 不够**：

```
cpu_mp.c:25: unknown type name 'rt_hw_spinlock_t'
```

`build/` 里残留 SMP 时的 CMake 源列表。**必须 `Remove-Item build -Recurse` 重新 configure。**

宏变化会触发重编译，**删源文件不会触发 re-configure**——这是本轮新踩的。

## 31. SMP 硬件原语（2026-10-31）

### 31.1 已实现

`smp_prims.c`（6 个）+ `smp_ipi.c`（1 个），全部包在 `#ifdef RT_USING_SMP` 里。

| 函数 | 实现 |
|---|---|
| `rt_hw_cpu_id` | `csrr mhartid` |
| `rt_hw_spin_lock_init` | `slock = 0` |
| `rt_hw_spin_lock` | ticket lock，`amoadd.w.aqrl` |
| `rt_hw_spin_unlock` | `fence` + **半字存储** |
| `rt_hw_secondary_cpu_up` | **空**（安全方向） |
| `rt_hw_secondary_cpu_idle_exec` | `wfi` |
| `rt_hw_ipi_send` | CLIC pending 读改写 |

### 31.2 ticket lock 的半字布局（关键）

```c
/* 一个 32 位字里：owner 在低半字，next 在高半字 */
__asm__ volatile ("amoadd.w.aqrl %0, %2, (%1)"
                  : "=&r"(tmp) : "r"(&lock->slock), "r"(1UL << 16) : "memory");

ticket = (tmp >> 16) & 0xFFFF;          /* 对：票号在高半字 */
while ((lock->slock & 0xFFFF) != ticket) pause;   /* owner 在低半字 */
```

**写错的表现**：取低半字永远是 0，所有核拿同一张票，**全部同时进临界区**——
而且**无竞争时完全正常**，只在真并发时爆。

**验证法**（不用板子）：

```python
word = 0; tickets = []
for _ in range(3):
    old = word; word += 1 << 16
    tickets.append((old >> 16) & 0xFFFF)
# 必须得到 [0, 1, 2]
```

### 31.3 `pause` 的坑

```
Error: unrecognized opcode `pause', extension `zihintpause' required
```

用原始编码代替助记符：

```c
__asm__ volatile (".4byte 0x0100000f");
```

同一条指令。不支持的核当无操作，自旋仍正确。

### 31.4 CLIC pending 必须读改写

```c
reg = CLIC_INT_CTRL_REG(ETS_IPC_ISR_INUM + RV_EXTERNAL_INT_OFFSET);
*reg |= CLIC_INT_IP;      /* 不能 *=* */
```

一个字里挤了：`IP`(bit0)、`IE`(bit8)、`CTL` 优先级(bit31:24)、`TRIG`(bit18:17)。
盲写会连优先级和使能一起清掉。

### 31.5 `CMakeLists.txt` 的改法

这类文件改一行影响全局，**用 diff 对基线核对**：

```python
import difflib
print("\n".join(difflib.unified_diff(base.splitlines(), now.splitlines(), n=0)))
```

本轮漏掉 `idf_build_set_property(COMPILE_DEFINITIONS RT_USING_USER_MAIN APPEND)`
一行，**构建能过但运行时**：

```
[FAIL] init table order
[PASS] __fsymtab non-empty  144 B = 12 commands   <- 从 30 掉到 12
```

`RT_USING_USER_MAIN` 掉了 → `.rti_fn` 顺序错 → 板级初始化没跑完。

### 31.6 剩余待办（4 项，全在汇编层）

| 符号 | 位置 |
|---|---|
| `rt_interrupt_nest` | `vectors_rtthread.S:144/202` —— SMP 下是宏，`la` 用不了 |
| `rt_thread_switch_interrupt_flag` | `vectors_rtthread.S:217` |
| `rt_interrupt_from_thread` | `vectors_rtthread.S:226` |
| `rt_interrupt_to_thread` | `vectors_rtthread.S:232` |

后三个的修法：`cpuport.c` 的三个全局量挪进 `#else`，SMP 下由
`rt_scheduler_do_irq_switch()` 接管。

## 32. SMP 链接期清零（2026-10-31）

### 32.1 现状

**SMP 构建能完整链接，但不启动。**

```
Project build complete.      <- 无 undefined，无 multiple definition
Hash of data verified.
esptool chip-id: Connected   <- bootloader 正常
serial: 停在 esp_image 加载   <- 到不了 RT-Thread banner
```

单核固件不受影响：`s31test` 20/20、`check_image.py` 9/9、30 命令、外设全 PASS。

### 32.2 让汇编看到 rtconfig.h

`vectors_rtthread.S` 原本只 include `sdkconfig.h` 和 soc 头。
**不用改 riscv 组件**——rtthread 组件已把 `rtt_config_dir` 加进全局包含路径：

```c
#if CONFIG_IDF_RTTHREAD
#include "rtconfig.h"
#endif
```

### 32.3 SMP 下的 exit 路径

```asm
#ifdef RT_USING_SMP
    call  rt_hw_irq_nest_addr
    lw    t0, 0(a0)
#else
    la    t0, rt_interrupt_nest
    lw    t0, 0(t0)
#endif
    bnez  t0, spurious_interrupt

#ifdef RT_USING_SMP
    mv    a0, s0              # <- 注意：s0，不是当前 sp
    call  rt_scheduler_do_irq_switch
    tail  rt_hw_context_switch_exit
#else
    ... 原有 flag/from/to 逻辑 ...
#endif
```

**`a0` 必须是 `s0`（切换中断栈之前的 sp），不是当前 sp。**
参考 `interrupt_gcc.S:312-340`。写错会不启动。

### 32.4 两个链接坑

**(a) 归档扫描顺序**

```
vectors_rtthread.S:157: undefined reference to `rt_hw_irq_nest_addr'
```

`nm` 显示符号在 `librtthread.a` 里，但 **riscv 组件的汇编在归档之后扫描**。
两种修法（都做了）：

```cmake
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u rt_hw_irq_nest_addr")
```
+ 函数在**两个分支都定义**（单核返回全局量地址）。

**(b) `cpu_up.c` vs `cpu_mp.c`**

```
cpu_mp.c:48: multiple definition of `rt_spin_lock_init'
cpu_up.c:33: first defined here
```

两者是同一套接口的两个配置版本。上游 `src/SConscript:26-29`：
- `RT_USING_SMP` 关 → 删 `cpu_mp.c` + `scheduler_mp.c`
- `RT_USING_SMP` 开 → 删 `cpu_up.c` + `scheduler_up.c`

**我们的 CMakeLists 两个都列了。**

### 32.5 `CMakeLists.txt` 的纪律

**连续三轮栽在这个文件上**（漏行导致命令数 30→12、重复加源文件、位置错）。

改完必须：

```python
import difflib
print('\n'.join(difflib.unified_diff(base.splitlines(), now.splitlines(), n=0)))
```

确认**只有预期的那几处差异**才收手。

### 32.6 下一轮入口

`rt_scheduler_do_irq_switch` 的参数已看出可疑（见 32.3）。
其余候选：`rt_hw_secondary_cpu_up` 是空实现、双核初始化顺序。

## 33. SMP 排障：受控实验法（2026-10-31）

### 33.1 现状

| 配置 | 结果 |
|---|---|
| `RT_USING_SMP` + `RT_CPUS_NR=2` | 链接通过，不启动 |
| `RT_USING_SMP` + `RT_CPUS_NR=1` | **同样不启动** |

单核固件不受影响：20/20、9/9、30 命令、外设全 PASS。

### 33.2 用中间态排除因素

`RT_CPUS_NR=2` 时有三个可疑因素同时存在：
1. 从核没启动（`rt_hw_secondary_cpu_up` 空实现）
2. IPI 发到不存在的核
3. SMP 代码路径本身有问题

**构造 `RT_USING_SMP` + `RT_CPUS_NR=1`**：走全部 SMP 代码路径
（自旋锁、per-CPU 结构、`scheduler_mp.c`），但只有一个核。

**结果一样不启动** → 前两个因素**全部排除**，问题在第三项。

**这是排障方法的要点：把多个可疑因素拆成可以单独控制实验的变量。**

### 33.3 失败点：banner 之前

```c
rt_hw_spin_lock_init(&_cpus_lock);   // SMP only
rt_hw_local_irq_disable();
rt_hw_board_init();
rt_show_version();                   // <- banner，没到
```

**范围缩到三个函数。**

### 33.4 一个隐患：核数不变量没人守

`esp_system/startup.c:23-31` 有编译期断言要求
`RT_CPUS_NR == SOC_CPU_CORES_NUM`，但**只在
`!CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE` 时生效**。

项目现在是单核模式 → 断言不生效 → `RT_CPUS_NR=2` 静默通过。

**改 `RT_CPUS_NR` 之前必须先看 sdkconfig 的 `SINGLE_CORE_MODE`。**

### 33.5 串口静默时的判断顺序

```
1. esptool chip-id        能连上 -> 芯片好，是固件或复位问题
2. 检查 RTS 复位序列       board.py 的重置可能没生效
3. 再判断固件是否死
```

实验二之后串口完全没输出（连 bootloader 都不打），但 `chip-id` 能连——
是复位时序问题，不是固件死。

### 33.6 下一轮

三个候选：`rt_hw_spin_lock_init`、`rt_hw_local_irq_disable`、`rt_hw_board_init`。
建议顺序：先看 `rt_hw_board_init` 里有没有依赖 `RT_USING_SMP` 的代码路径
（它调用链最长），再回头查自旋锁初始化。

## 34. SMP 不启动的根因：flash cache 窗口里的函数调用（2026-10-31）

### 34.1 症状

```
I (409) MSPI Timing: Enter psram timing tuning
Guru Meditation Error: Core  0 panic'ed (Instruction access fault)
MEPC   : 0x40013b1e
MCAUSE : 0x00000001
```

### 34.2 定位

```bash
riscv32-esp-elf-nm -n s31_rtt_test.elf | grep '^4001'
# 找到 0x40013b1e 落在哪个符号
```

结果：`0x40013b1e T rt_enter_critical`（**IROM，flash 映射段**）。

### 34.3 根因

```
trap -> vectors_rtthread.S              [IRAM，能执行]
      -> rt_hw_irq_nest_addr()          [原 flash -> 已加 IRAM_ATTR]
        -> rt_cpu_self()/rt_hw_cpu_id() [flash]
          -> rt_enter_critical()        [flash] <- MEPC
```

**MSPI PSRAM timing tuning 期间 flash cache 是关的。** 此时跳到 flash
取指令 → instruction access fault。

**单核版不崩，是因为它 `la t0, rt_interrupt_nest` 直接读全局，零跳转。**
SMP 版为了拿 per-CPU 计数器引入了整条函数调用链。

### 34.4 为什么 `IRAM_ATTR` 不够

加完之后 MEPC **仍然是** `0x40013b1e`（只是从 `rt_hw_cpu_id` 换成
`rt_enter_critical`，因为 RA 变了）。

**一条调用链要每环都在 IRAM 才有用**，逐环加属性不现实。

### 34.5 正确修法

**trap 路径必须零函数调用。** 用 `tp` 寄存器：

```asm
# 启动时（cache 开着）：
#   tp = rt_cpu_self()
# trap 里：
    lw    t0, IRQ_NEST_OFF(tp)     # 一次 load，不跳转
```

`tp` 是 RISC-V 约定的 thread pointer，RT-Thread 参考实现
（`libcpu/risc-v/common/cpu.c:251`）也读 `tp`。

**需要知道 `struct rt_cpu` 里 `irq_nest` 的偏移** —— 可以用一个
`_Static_assert` + 导出常量（`offsetof`）让汇编引用，避免硬编码。

### 34.6 探针方法（可复用）

```c
{
    extern int esp_rom_printf(const char *fmt, ...);
    esp_rom_printf("[probe] after X\n");
}
```

**`esp_rom_printf` 不需锁、不需堆、不需调度器**，是唯一能在最早点输出的手段。

**注意**：探针本身不能调配置相关的符号。我在探针里调了
`rt_hw_cpu_id()`，而它只在 `#ifdef RT_USING_SMP` 里定义 →
**单核构建链接失败**。测量工具先于被测对象坏掉。

### 34.7 回滚纪律

本轮三次因为回滚不干净翻车：
1. 探针脚本跑两次 → board.c 里两份探针
2. 探针调 `rt_hw_cpu_id()` → 单核没这个符号
3. 删了 `RT_USING_SMP` 但源列表还是 `scheduler_mp.c` → 一堆参数错误

**`rtconfig.h` 和 `CMakeLists.txt` 是联动的，必须一起改、一起回滚。**

## 35. SMP 修法第二轮：方向对了一半（2026-10-31）

### 35.1 三个版本的对照

| 版本 | 现象 | 结论 |
|---|---|---|
| 函数调用 `rt_hw_irq_nest_addr()` | 挂在 `Enter psram timing tuning` | cache 关闭窗口 fault |
| 表在 BSS + `board_init` 填 | **更早挂**（连 PSRAM 都不打印） | 中断先于 board_init，`slot=NULL` |
| 表静态初始化 | **通过 PSRAM tuning**，挂在 `Single core mode` 之后 | 解决了 NULL，还有别的问题 |

### 35.2 关键证据

静态表版**通过了 `Enter psram timing tuning`**：

```
I (401) MSPI Timing: Enter psram timing tuning
I (558) esp_psram: Found 16MB PSRAM device       <- 上一版在这里 fault
I (559) cpu_start: Unicore app
I (560) cpu_start: Single core mode
```

**"trap 路径不能调 flash"这个诊断是对的** —— 换成零跳转的 `la`/`lw` 就过了。

### 35.3 但还有新的挂点

`Single core mode` 之后静默挂住，无 panic、无重启、无 banner。

**怀疑（未验证）**：`IRAM_ATTR` 用在带非零初值的数据上，
ESP-IDF 的 IRAM 段可能不保证初始化数据被正确放置。
若 `slot[0]` 实际是 0，应触发 access fault + panic，但**没有 panic**，
所以更像自旋或别的原因。

### 35.4 当前做法：`#ifdef` 分叉

```asm
#ifdef RT_USING_SMP
    la    t1, rt_irq_nest_slot
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t1
    lw    t0, 0(t0)
#else
    la    t0, rt_interrupt_nest
    lw    t0, 0(t0)
#endif
```

**单核用已验证 20 轮的直读，SMP 用表。** 回滚后全部检查通过。

### 35.5 下一步方向

表方案不能依赖 C 的初始化数据。**在汇编入口自己写表**：

```asm
# 在 rt_hw_board_init 之前执行、cache 还开着时：
    la    t0, rt_interrupt_nest
    la    t1, rt_irq_nest_slot
    sw    t0, 0(t1)
```

这样表的初值由指令保证，不经过 `.data` 段。

### 35.6 教训

**"修好一个现象"不等于"修对了"。** 两个版本各修掉一半问题：
- 函数调用版修不掉 cache 窗口
- 静态表版修掉了 cache 窗口，引入新挂点

**主固件不能为实验冒险。** 任何改动先做成 `#ifdef` 分叉，
保证默认配置不受影响——这一条以后固定执行。

## 36. trap 路径改写：两个汇编 bug（2026-10-31）

### 36.1 最终正确的写法

```asm
    la    t2, rt_irq_nest_slot     /* 必须用 t2，不能用 t1 */
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t2
    lw    t0, 0(t0)                /* slot[n]，是个指针 */
    lw    t0, 0(t0)                /* 再解引用才是计数值 */
```

**两条铁律**：

1. **`t1` 在这一段保存本层 context 指针，绝对不能碰。**
   原始代码只用 `t0`，加寄存器时必须先看前面有没有 `mv t1, sp`。
2. **表里存的是指针，要 `lw` 两次。**

### 36.2 两个 bug 的症状对照

| bug | 症状 |
|---|---|
| 覆盖 `t1` | 挂在 `Single core mode` 之后，无 panic |
| 少一次 `lw` | **能启动、能打 banner、`msh >` 出现，但命令跑不完** |

**第二个 bug 最危险**：所有"看起来正常"的表象都在，只有行为不对。
如果不是跑 `s31test` 而是只看 banner，会以为成功了。

### 36.3 表放在 `.data` 就够了

```
$ nm -S s31_rtt_test.elf | grep rt_irq_nest_slot
2f0187b8 00000004 D rt_irq_nest_slot
```

**DRAM 在 flash cache 关闭时依然可读。只有指令取指需要 IRAM。**
给数据加 `IRAM_ATTR` 不但没必要，还会把表挪进初始化 IRAM 段，行为更难预测。

### 36.4 单核也走表路径

现在单核和 SMP 用同一段汇编。好处：**SMP 的这段代码被每轮自检覆盖**，
不再是"只在 SMP 下才执行、从没验证过"的代码。

### 36.5 SMP 的新失败点

修好两个 bug 后：

```
MEPC : 0x40013b26  -> rt_enter_critical
RA   : 0x2f010f92  -> vPortEnterCritical
```

调用链 `vPortEnterCritical → rt_enter_critical → rt_cpus_lock → rt_hw_spin_lock`。

`rtthread_startup()` 里 `rt_hw_spin_lock(&_cpus_lock)` 之后**不释放**，
靠 `rt_cpus_lock_status_restore` 在 `rt_hw_context_switch_exit` 里恢复。
**SMP 下第一个临界区就冲突 → 自旋死。**

下一轮从这里继续。

## 37. SMP 首次打出 banner：linker fragment 的 `*_mp` 遗漏（2026-10-31）

### 37.1 判据：同函数在两个构建里落点不同

```bash
# 单核
riscv32-esp-elf-nm -C s31_rtt_test.elf | grep rt_enter_critical
# 2f016184 T rt_enter_critical      <- RAM

# SMP
riscv32-esp-elf-nm -C s31_rtt_test.elf | grep rt_enter_critical
# 40013b26 T rt_enter_critical      <- IROM (flash)
```

**同一个函数，两个构建落在不同段** —— 这就是代码放置 bug 的指纹。

### 37.2 为什么

`linker.lf` 只覆盖了单核对象：

```
scheduler_up:rt_enter_critical (noflash)
cpu_up:rt_cpu_self             (noflash)
```

`RT_USING_SMP` 时这些翻译单元**不在链接里**，实现来自
`scheduler_mp.c` / `cpu_mp.c`，**没有 noflash 规则** → 落进 IROM。

IDF 关 flash cache 调 PSRAM 时序时，中断路径跳进 IROM → instruction access fault。

### 37.3 需要补的规则

```
scheduler_mp:rt_enter_critical (noflash)
scheduler_mp:rt_exit_critical (noflash)
scheduler_mp:rt_critical_level (noflash)
scheduler_mp:rt_soft_atomic_add (noflash)
scheduler_mp:rt_cpus_lock_status_restore (noflash)
scheduler_mp:rt_sched_post_ctx_switch (noflash)
cpu_mp:rt_cpu_self (noflash)
cpu_mp:rt_cpus_lock (noflash)
cpu_mp:rt_cpus_unlock (noflash)
smp_prims (noflash)
```

### 37.4 坑：逐符号规则可能不生效

`smp_prims:rt_hw_cpu_id (noflash)` **生成的 `sections.ld` 里查不到**，
符号仍留在 IROM。

改成**整个对象**：

```
smp_prims (noflash)
```

才生效。**改完必须验证**：

```bash
grep smp_prims build/esp-idf/esp_system/ld/sections.ld
nm -C build/s31_rtt_test.elf | grep rt_hw_spin_lock
```

### 37.5 修复后

```
banner: True | guru: 0
```

**banner 正常打印，无 panic。** 五个关键函数全部进 RAM：

```
2f0160a8 T rt_cpus_lock
2f016256 T rt_enter_critical
2f01632a T rt_hw_cpu_id
2f016336 T rt_hw_spin_lock
```

### 37.6 新失败点

shell 不响应。**不是 panic，是卡住。** 范围从"banner 之前"缩到"banner 之后"。

### 37.7 对单核无害

`*_mp` 规则在单核构建里匹配不到任何东西。已验证单核全过。
**可以安全地留在配置里。**

## 38. SMP 是 1 Hz 软复位循环（2026-10-31）

### 38.1 判据：软复位 vs panic vs 挂住

| 现象 | 判据 |
|---|---|
| **软复位** | `rst:0xc (SW_CPU_RESET)`，`Core0 Saved PC` 在 `esp_cpu_reset` |
| panic | 有 `Guru Meditation Error` 或 `assertion failed` 文字 |
| 挂住 | 无任何输出，也无 `rst:` 行 |

本项目 `CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT=y` —— **panic 一定会先打印**。
所以"有 `rst:0xc` 但没有任何错误文字" = **主动调用 `esp_restart`/`esp_cpu_reset`**。

### 38.2 当前现象

```
banner 打印正常
rst:0xc (SW_CPU_RESET)     12 次 / 12 秒  <- 精确 1 Hz
Core0 Saved PC:0x2f00ce2c  -> nm: esp_cpu_reset
无 panic、无 assertion、无 backtrace
```

### 38.3 已排除

| 假设 | 排除依据 |
|---|---|
| `vPortEnterCritical` 多核 abort | `CONFIG_FREERTOS_NUMBER_OF_CORES=1`，代码不编译 |
| 中断看门狗 INT WDT | 关掉 `CONFIG_ESP_INT_WDT` 重建重烧，**仍 1 Hz** |

### 38.4 1 Hz 这条线索

**周期性复位要去周期源里找，不要在启动路径里找。**

| 候选 | 备注 |
|---|---|
| 任务看门狗 TWDT | 本项目零 `esp_task_wdt_add` 调用点 |
| RTC WDT | 待确认 |
| `rt_timer` 周期回调里的 `ESP_ERROR_CHECK` / `esp_restart` | **1 Hz 吻合** |
| systick 中断出错 | tick 1000 Hz，1 秒 = 1000 tick |

### 38.5 对照实验的价值

关掉 INT WDT 重建重烧，是**唯一**能排除它的方法。
这一轮花了一次完整构建+烧录，换来"排除一个假设"——**值得**。

**不做对照实验就只能猜**，而 W37/W38 已经证明猜错的代价更高。

### 38.6 回滚纪律

`CONFIG_ESP_INT_WDT` 改在 `sdkconfig.defaults`，但 `sdkconfig` 已存在时**不会重新生成**。
必须：

```powershell
Remove-Item sdkconfig
Remove-Item build -Recurse
```

否则改动静默不生效。

## 39. 区分 SMP 的两种失败方式（2026-10-31）

### 39.1 先量一次启动耗时

```
I (29)  boot: ESP-IDF ...
I (991) coexist: ...
        banner
```

**一次启动约 1.05 秒。** 所以"12 次复位/12 秒"不是 1 Hz 周期，
**是每次启动耗时**。

**教训：看到规律先量单次耗时，再谈周期。**

### 39.2 复位类型速查

| `rst:` 值 | 含义 | 典型原因 |
|---|---|---|
| `0xc SW_CPU_RESET` | 软件复位 | `esp_restart` / `esp_cpu_reset` |
| `0x8 HP_SYS_HP_WDT1_RESET` | 硬件看门狗 | **卡住不动**，WDT 到点踢 |
| `0x3 SW_SYS_RST` | 系统复位 | panic 后 reboot |

**`SW_CPU_RESET` 和 `HP_WDT_RESET` 是两种不同的死法**，要分开查。

### 39.3 当前两个变体

| 配置 | 复位 | Saved PC | 含义 |
|---|---|---|---|
| `RT_CPUS_NR=2` | `0xc SW_CPU_RESET` | `esp_cpu_reset` | 主动复位 |
| `RT_CPUS_NR=1` | `0x8 HP_SYS_HP_WDT1_RESET` | `0x2f8036c8`(ROM) | 卡住 |

### 39.4 死亡位置：`rt_show_version()` 之后、`main()` 之前

判据：`main.c:253` 的第一行 banner 没打印，而 `rt_show_version()` 的有打印。

```c
/* main.c */
rt_kprintf("RT-Thread %d.%d.%d on ESP32-S31\n", ...);    // ← 没出现
```

**这一段只有 `rtthread_startup()` 的后半段：**
```
rt_system_timer_init()
rt_system_scheduler_init()
rt_hw_spin_lock(&_cpus_lock)
rt_hw_secondary_cpu_up()
rt_application_init()
rt_system_timer_thread_init()
rt_thread_idle_init()
rt_thread_defunct_init()
rt_system_scheduler_start()
```

### 39.5 ticket lock 复核结论

手工推演两次 lock/unlock，`owner` 在低半字、`next` 在高半字，
`amoadd` 加 `1<<16` 推进 next，`unlock` 半字存储推进 owner。
**逻辑正确，不构成死锁。**

### 39.6 探针纪律

**不要在 `components.c` 这类上游文件里加探针。**
这一轮试过：宏插入位置算错 → 编译失败 → 回滚浪费一轮。

**优先在自有文件里加**：
- `board.c`（我们维护）
- `main.c`（项目自己的）
- `smp_probe.c` 这类新文件

## 40. 探针定位法（2026-10-31）

### 40.1 三步收敛

| 步骤 | 手段 | 输出 |
|---|---|---|
| 1 | `main()` 第一行 `esp_rom_printf` | 确认 `main()` 是否进入 |
| 2 | 每个可疑调用前后各一条 | `before X` / `after X` |
| 3 | 收敛到函数内部，再插 | 同上 |

**实测效果**：`rtthread_startup()` 的 9 个调用 → 17/18 打印；
`rt_system_scheduler_start()` 的 7 个点 → 13/14 打印。
**死点从 9 个函数收敛到 1 个调用。**

### 40.2 三条纪律（缺一必翻车）

**(a) 按行匹配，不用跨行正则。**

W42 失败原因：文件是 **CRLF**，正则里写的是 `\n`，
结果**宏定义没插进去、调用点插进去了** → `implicit declaration of SMP_PROBE`。

```python
# 对
for line in lines:
    if line.strip() == "rt_system_timer_init();":

# 错（CRLF 下不匹配，且难察觉）
re.sub(r"    rt_system_timer_init\(\);\n", ...)
```

**(b) 插入后先验证计数再写文件。**

```python
assert inserted == len(CALLS), f"{inserted} of {len(CALLS)}"
```

**(c) 改上游文件前先备份。**

```powershell
Copy-Item "rt-thread\src\components.c" "tools\tmp\components.c.bak"
```

回滚就是反向 `Copy-Item`，比手工删探针可靠。

### 40.3 输出手段的选择

| 手段 | 可用时机 | 限制 |
|---|---|---|
| `rt_kprintf` | 调度器启动后 | 需要控制台 |
| **`esp_rom_printf`** | **进程最早点** | 无锁、无堆、无调度器、无控制台设备 |
| `s31intr` | 运行时 | 需要 msh |

**`esp_rom_printf` 是唯一能在 `main()` 之前输出的手段。**

### 40.4 当前死点

```
rt_hw_context_switch_to((rt_ubase_t)&to_thread->sp, to_thread)
```

三个候选：`to_thread->sp` 坏指针 / `rt_cpus_lock_status_restore` 挂住 /
帧格式与 `rt_hw_stack_init` 不匹配。

## 41. SMP 死点缩到汇编帧恢复（2026-10-31）

### 41.1 三条打印，两个候选排除

```
[switch] thread=0x2f032780 sp=0x2f033530
[exit] restoring frame
（无 [mte] / 无 [main]）
```

| 打印 | 含义 | 排除 |
|---|---|---|
| `[switch]` | thread 和 sp 都在堆区（`0x2f030a60`..`0x2f050a60`） | to_thread 垃圾指针 |
| `[exit]` | `rt_cpus_lock_status_restore()` 正常返回 | 锁恢复挂住 |
| 无 `[mte]` | **mret 之后没到线程入口** | — |

**死点：`rt_hw_context_switch_exit` 的帧恢复或 `mret`。**

### 41.2 下一轮怎么查

`[exit]` 打了而 `[mte]` 没打，只剩两种可能：

| 可能 | 判据 |
|---|---|
| `mepc` 被写坏 | 在 `csrw mepc, a0` 之后打印 `a0` |
| `mstatus.MPP` 不是 M | 打印 `mstatus` |

**⚠️ 不要在 `mret` 前直接 `call`** —— 此时 32 个整数 + 32 个浮点寄存器
**已从帧恢复**，`call` 会把它们全破坏。

正确做法：

```asm
    LOAD a0, 0 * REGBYTES(sp)
    csrw mepc, a0
#ifdef RT_USING_SMP_MEPC_PROBE
    /* a0 此时还可以用（马上会被 LOAD x1 覆盖），
     * 但 call 会破坏 x1/x3..x31，只能在这里比较安全的做法
     * 是先把整个帧恢复到一半，再在 sp 跳走之前打 */
#endif
```

**更稳妥**：在**进入 exit 时**（寄存器还没恢复）就把 `frame->epc` 和
`frame->mstatus` 读出来打印。

```asm
rt_hw_context_switch_exit:
#ifdef RT_USING_SMP_EXIT_PROBE
    mv   s11, sp              /* 保存 sp，call 会改变调用者保存寄存器 */
    call s31_exit_probe       /* 内部读 0(s11) 和 2*4(s11) 并打印 */
    mv   sp, s11              /* 恢复 */
#endif
```

**注意 `s11` 也会被 `mret` 前的 `LOAD x27, 27*REGBYTES(sp)` 覆盖，但那不影响。**

### 41.3 探针纪律（第三次验证）

| 纪律 | 本轮验证 |
|---|---|
| 按行匹配不用跨行正则 | 三次插入一次成功 |
| `assert inserted == len(POINTS)` | 第二次尝试抓到了 1/2 不匹配 |
| 插之前备份 | 三个文件干净回滚 |
| 插完立刻构建 | 第一次缺 `esp_attr.h` 立刻发现 |

## 42. 探针必须先验证"是否编译进去"（2026-10-31）

### 42.1 两次静默失效，一次误判

| 次 | 探针位置 | 宏位置 | 现象 | 真因 |
|---|---|---|---|---|
| 1 | `components.c` | rtthread 组件的 `set_source_files_properties` 列表（**不含它**） | 无输出 | 宏未定义，展开成空 |
| 2 | `vectors_rtthread.S`（riscv 组件） | rtthread 组件的 `COMPILE_DEFINITIONS` | 无输出 | riscv 组件看不到 |

**第 1 次直接导致 W44 的误判**："mret 之后没进线程"——实际上探针根本没编译。

### 42.2 验证法（必做）

```bash
# 1. 字符串在不在镜像里
riscv32-esp-elf-strings build/s31_rtt_test.elf | grep '\[probe\]'
# 有 -> 编译进去了，运行时没执行
# 无 -> 宏没生效，探针被 #ifdef 剪掉了

# 2. 符号在不在
riscv32-esp-elf-nm build/s31_rtt_test.elf | grep probe_symbol
```

**先做这两步，再解读"没打印"。**

### 42.3 跨组件方案

探针在 A 组件，宏在 B 组件 → 不会生效。两种解法：

**(a) 给包含探针的组件补定义**

```cmake
# components/riscv/CMakeLists.txt
target_compile_definitions(${COMPONENT_LIB} PRIVATE RT_USING_SMP_FRAME_PROBE)
```

**(b) 用目标组件本来就有的宏**

`RT_USING_SMP` 来自 `rtconfig.h`，而 `rtconfig.h` 是**全局可见**的
（`idf_build_set_property(INCLUDE_DIRECTORIES ...)`），所以用它最可靠。

### 42.4 SMP 的真实死点（本轮实证）

```
[frame] epc=0x400115cc mstatus=0x7880 mpp=3 mpie=1     <- 帧正确
[mret]  mepc=0x400115cc mstatus=0x80007880 mpp=3       <- 60 条恢复全成功
[trap 1] mcause=0xb8000014 mepc=0x400115cc             <- OS tick 抢在第一条指令前
```

`0x400115cc` = `main_thread_entry`。**中断抢占正常，但没有返回。**

**⇒ 死点在中断的处理/返回路径，不在 context switch。**

SMP 下这段走 `rt_scheduler_do_irq_switch()`。

## 43. noflash 规则：用整对象，不要逐符号（2026-10-31）

### 43.1 四次遗漏，同一个原因

| # | 遗漏的东西 | 症状 |
|---|---|---|
| 1 | `scheduler_mp` 的 critical 族 | trap 里进 flash → fault |
| 2 | `cpu_mp` 的 lock 族 | 同上 |
| 3 | `smp_prims` 逐符号写不生效 | 规则被忽略 |
| 4 | **`cpu_mp` 的 `rt_soft_atomic_add` 内联副本** | `rt_cpus_lock` 调 IROM |

### 43.2 为什么逐符号不够

`rtatomic.h` 里的 helper 是 `static inline`，每个翻译单元生成一份
**编译器命名的** out-of-line 副本。`linker.lf` 里写
`cpu_mp:rt_cpus_lock (noflash)` 覆盖不到 `cpu_mp` 里的那份
`rt_soft_atomic_add`。

**改成 `cpu_mp (noflash)` 才覆盖整个对象的所有 section。**

### 43.3 正确的写法

```
[mapping:rtthread]
archive: librtthread.a
entries:
    if IDF_RTOS_RTTHREAD = y:
        irq (noflash)              # 整对象
        context_gcc (noflash)      # 整对象
        cpu_mp (noflash)           # 整对象  <- 不要逐符号
        smp_prims (noflash)        # 整对象  <- 不要逐符号
        scheduler_mp (noflash)     # 整对象  <- 最省事
```

**`scheduler_mp` 直接整对象最省事**：它的每个函数都在调度路径上。

### 43.4 验证方法：objdump，不是 nm

`nm` 只给符号地址，看不出调用关系。要看**调用目标**：

```bash
riscv32-esp-elf-objdump -d --start-address=0x2f016250 \
    --stop-address=0x2f01629c s31_rtt_test.elf | grep -E 'jal|jalr|auipc'
```

判据：
- `jal 2f01xxxx` → RAM，安全
- `auipc` + `jalr` 到 `40xxxxxx` → **IROM，定时炸弹**

**这个判据是本轮唯一可行的诊断手段。**

### 43.5 带 tag 的探针

```asm
li    a0, 4
call  s31_trap_stage
```

```c
IRAM_ATTR void s31_trap_stage(int tag)
{
    static unsigned char seen[8];
    if (tag < 0 || tag >= 8 || seen[tag] >= 2) return;
    seen[tag]++;
    esp_rom_printf("[stage %d]\n", tag);
}
```

**一次运行给出完整序列**，比每轮只加一个探针快得多。
`seen[tag]` 分级限流，避免 1 kHz tick 淹没串口。

## 44. noflash 的作用域要按体积定（2026-10-31）

### 44.1 先量再定

```bash
riscv32-esp-elf-size -t librtthread.a | tail -1
# 107969  476  10151  118596  1cf44  (TOTALS)
```

**105 KB text。整 archive 进 DRAM 装不下。**

### 44.2 实际用的规则

```ld
irq (noflash)
context_gcc (noflash)
cpuport (noflash)
cpu_mp (noflash)
cpu_up (noflash)
scheduler_mp (noflash)
scheduler_up (noflash)
smp_prims (noflash)
smp_stack (noflash)
thread:rt_thread_self (noflash)
```

**都是整对象**（`thread` 那条例外，它太大而只需要一个函数）。

### 44.3 现象的两种形态要分清

| `rst:` | Saved PC | 含义 | 该查什么 |
|---|---|---|---|
| `0xc SW_CPU_RESET` | `esp_cpu_reset` | 主动复位 | abort / assert / ESP_ERROR_CHECK |
| **instruction access fault** | IROM 地址 | **flash 取指** | 代码放置（noflash 规则） |
| **`0x8 HP_WDT`** | ROM 地址 | **卡住** | 自旋 / 死循环 |
| `0x8 HP_WDT` | 自己的代码 | 卡在自己的循环 | 该函数 |

**"fault" 和 "卡住" 是两个不同的排查方向。**

### 44.4 两类原因混在一起时

前五轮"SMP 不启动"同时有 flash 取指和锁自旋两个原因。
**修好 flash 那一类之后，现象从 fault 变成卡住** ——
如果不区分形态，会以为"改了没效果"。

**先把一类彻底消除（用整对象规则一次覆盖），让现象稳定成单一形态，再查第二类。**

### 44.5 下一轮入口

`rt_cpus_lock` 的自递归结构：

```asm
lw    s0, 0(a0)          ; current_thread
beqz  s0, return         ; NULL -> 直接返回
jal   rt_cpus_lock       ; 递归读 cpus_lock_nest
...
jal   rt_hw_spin_lock    ; &_cpus_lock
```

**递归深度取决于 `cpus_lock_nest`。** 若 `_cpus` 未清零 → 垃圾值 → 递归失控。


## 45. SMP 启动成功（2026-10-31）

### 45.1 症状与根因

症状：banner 打印后 1 秒复位，`rst:0x8 HP_WDT`，Saved PC 在 ROM，**无 panic**。

根因：**软件原子操作在 ISR 里取全局自旋锁。**

```
rtatomic.h 三级选择：
  1. RT_USING_STDC_ATOMIC   C11 标准原子
  2. RT_USING_HW_ATOMIC     架构提供        <- 本项目缺这个
  3. rt_soft_atomic_*       关中断实现      <- 落到了这里

rt_soft_atomic_add() -> rt_hw_interrupt_disable()
                     -> rt_cpus_lock()        <- rthw.h:146 的宏，SMP 下就是全局锁
                     -> rt_hw_spin_lock(&_cpus_lock)
```

`rt_interrupt_enter()` 第一行就是 `rt_atomic_add(&irq_nest, 1)` —— **每次 trap 都取锁。**

### 45.2 修法（两步 + 一条 noflash）

```cmake
# components/rtthread/CMakeLists.txt —— 放 common_srcs，不能放 kernel_srcs
set(common_srcs
    "port/common/context_gcc.S"
    "rtthread_stack_init.c"
    "${RTTHREAD_ROOT}/libcpu/risc-v/legacy_common/atomic_riscv.c")
```

```c
/* rtconfig.h */
#define RT_USING_HW_ATOMIC
```

```ld
/* linker.lf */
atomic_riscv (noflash)
```

**`kernel_srcs` 会被拿去做"文件必须存在"的检查（且按 `RTTHREAD_ROOT/src` 拼路径），
`atomic_riscv.c` 在 libcpu 下，放进去会 configure 失败。**

### 45.3 结果

```
tick=1000 Hz, cpus=2, msh on UART0
msh > s31test
=== 20 passed, 0 failed ===
```

`list thread`：

```
thread       cpu bind pri  status
tshell         0   2   20  running
tidle1       N/A   1   31  ready
tidle0       N/A   0   31  ready
```

**两个 idle 线程都在** —— 内核按两核初始化了。

### 45.4 ⚠️ CPU1 还没真正运行

**`rt_hw_secondary_cpu_up()` 仍是空实现。**

这一轮验证的是 **SMP 管线**：调度器、ticket lock、per-CPU 结构、IRAM trap 路径。
**CPU1 一条指令都没执行。**

**别把"cpus=2 且能跑"误当成"双核并行"。** 看 `list thread` 的 `cpu` 列：
所有线程都在 `cpu 0` 或 `N/A`，**没有线程在 cpu 1 上跑**。

### 45.5 为什么这个根因最难找

| 特点 | 后果 |
|---|---|
| **不产生 fault，只死锁** | 没有 MEPC 可查 |
| 发生在 `rt_interrupt_enter`，trap 最开头 | 探针要打在第一条指令 |
| `rt_hw_interrupt_disable` 名字与锁无关 | **和 R24 同一个陷阱** |

**只有读 `rthw.h:146` 的宏映射 + `rtatomic.h` 的三级选择才能看出来。**

### 45.6 排查顺序的教训

前十轮都在修 flash 放置问题（`noflash` 规则，五条），**它们都是真问题**，
但**每一层修好之后，下一层才露出来**：

```
链接缺符号 -> 汇编 bug -> flash 放置 -> 内联副本 -> 整批放置 -> 软件原子取锁
```

**"修好一个现象"不等于"修对了"** —— 现象会变形，但根因可能还在下一层。

### 45.7 顺带发现：`%f` 在 tiny printf 下静默不输出

```
msh > s31fpu
FPU ok seed=%f        <- 字面量，没有数字
```

`rt_vsnprintf_tiny.c:524`：

```c
case 'e': case 'E': case 'G': case 'g':
case 'f': case 'F':
    va_arg(args, double);
default:
    *str = '%'; ...          /* 原样输出 */
```

**消费了参数但不打印。** 比报错更难发现——输出"看起来正常"，只是少了数字。

**本项目所有浮点结果都用整数打印**（`(int)(x * 10.0f)` 这种）。

### 45.8 最终验证清单（SMP 构建）

| 检查 | 结果 |
|---|---|
| `tick=1000 Hz, cpus=2` | ✅ |
| `s31test` | ✅ 20/20（连跑 3 次） |
| `check_image.py` | ✅ 9/9，31 命令 |
| `s31twai` | ✅ 4/4 |
| `s31rmt` | ✅ PASS |
| `s31mcpwm` | ✅ PASS |
| `s31pcnt` | ✅ PASS |
| `s31fpu` | ✅ seed 保住了 |
| `list thread` | `tidle0`(cpu0) + `tidle1`(cpu1) |

**但 CPU1 仍未执行任何代码。** 见 45.4。


## 46. CPU1 启动：尝试与回滚（2026-10-31）

### 46.1 当前状态

| 项 | 状态 |
|---|---|
| SMP 基线（`RT_USING_SMP`，不定义 `RT_USING_SMP_SLAVE`） | ✅ 20/20、`tidle0`+`tidle1` 都在 |
| CPU1 执行代码 | ❌ 空实现（默认） |
| `RT_USING_SMP_SLAVE` 开关 | 代码在，**默认关** |

### 46.2 打开开关会发生什么

```
tick=1000 Hz, cpus=2, msh on UART0
msh >
（命令不执行，无复位、无 panic）
```

**banner 和提示符正常，但 shell 死了。**

### 46.3 为什么回滚

**"启动失败但没弄坏别的"可以接受；"启动失败且弄坏了原本能用的东西"不行。**

回滚后同一份代码 **20/20、31 命令、两核 per-CPU 结构都在**。

### 46.4 下一轮的验证顺序

**先确认"能被叫起来"，再确认"能调度"。**

把 `s31_secondary_cpu_entry` 改成只剩 `while (1) { wfi; }`：

- **shell 正常** → 硬件启动成功，问题在 `rt_system_scheduler_start()`
- **shell 仍死** → 问题在启动序列（时钟/复位/中断矩阵）

**这个二分把两种完全不同的原因一次分开。**

### 46.5 不要复用 IDF 的 `call_start_cpu1()`

它末尾是 `SYS_STARTUP_FN()` —— IDF 的初始化链，期待 FreeRTOS 调度器。
RT-Thread 需要自己的入口，形状参考
`rt-thread/bsp/k210/board/cpuport_smp.c:57`。


## 47. CPU1 启动成功：用共享内存标记证明（2026-10-31）

### 47.1 怎么证明 CPU1 真的跑了

**不要相信 banner 里的 `cpus=2`** —— 那是编译期常量。

让 CPU1 的入口写一个共享变量，CPU0 的 shell 读它：

```c
/* CPU1 入口 */
s31_cpu1_alive = 0x5A5A5A5A;
for (;;) { wfi; }

/* s31stat */
rt_kprintf("cpu1   : %s (0x%08x)\n",
           s31_cpu1_alive ? "started" : "not started", s31_cpu1_alive);
```

```
msh > s31stat
cpu1   : started (0x5a5a5a5a)
```

**这才是"CPU1 执行了我们的代码"的证据。**

### 47.2 二分法：`wfi` 停放 vs 进调度器

| CPU1 入口 | 结果 |
|---|---|
| 只 `wfi` | ✅ shell 正常、20/20、连跑 3 次稳定 |
| `rt_system_scheduler_start()` | ❌ 启动循环 `rst:0x8` |

**这个二分把"启动序列错"和"调度器抢占"一次分开。**

结果：**启动序列是对的**，问题在调度器。

### 47.3 变体 B 的失败机制

`scheduler_mp.c:494`：

```c
void rt_system_scheduler_start(void)
{
    rt_hw_spin_unlock(&_cpus_lock);      /* 无条件，第一行 */
```

**它假定到达时锁已由 CPU0 释放。** 而 CPU0 此时还在
`rtthread_startup()` 里跑 `rt_application_init()`——
**两个核可以同时进调度器代码**。

### 47.4 下一轮：编排启动时序

```c
/* CPU1 入口 */
la gp, __global_pointer$
set mtvec / mtvt
s31_cpu1_alive = 0x5A5A5A5A
while (!s31_cpu0_ready) { wfi; }      /* 等 CPU0 准备就绪 */
rt_system_scheduler_start();

/* CPU0：在 rt_system_scheduler_start() 之前 */
s31_cpu0_ready = 1;
```

**关键是"CPU0 已准备好"这个标志**，而不是去猜锁的状态。

### 47.5 当前交付配置

```c
/* rtconfig.h */
#define RT_USING_SMP
#define RT_CPUS_NR                      2
#define RT_USING_SMP_SLAVE              /* 启动 CPU1，停在 wfi */
```

**CPU1 起来了、能执行 IRAM 代码、共享向量表已设置，但不参与调度。**


## 48. CPU1 用共享锁：取 + 还（2026-10-31）

### 48.1 先看 `Saved PC` 落在谁的代码里

```
rst:0x8 (HP_SYS_HP_WDT1_RESET)
Core0 Saved PC:0x2f016d30
```

```bash
riscv32-esp-elf-nm -C --defined-only -n elf | awk '$1 > "2f016d30"' | head -1
# 2f016d1e T rt_hw_spin_lock
```

**`Saved PC` 是 `Core0` 的，落在 `rt_hw_spin_lock` 里——卡住的是 CPU0。**

### 48.2 上一版的 bug

```c
rt_hw_spin_lock(&_cpus_lock);      /* 取了 */
for (;;) { wfi; }                  /* 没还！ */
```

**`_cpus_lock` 是全局的。** CPU1 取走不还，CPU0 之后就自旋到 HP_WDT。

### 48.3 正确写法

```c
rt_hw_spin_lock(&_cpus_lock);
s31_cpu1_locked = 0x12345678u;
rt_hw_spin_unlock(&_cpus_lock);     /* 还回去 */
for (;;) { wfi; }
```

### 48.4 验证

```
msh > s31stat
cpu1   : started (0x5a5a5a5a)
cpu1lk : acquired (0x12345678)
=== 20 passed, 0 failed ===
```

**`cpu1lk: acquired` 比 `cpu1: started` 强**：ticket lock 用 `amoadd.w.aqrl`，
要求两核对同一字的读写连贯。CPU1 能取票并归还，说明 cache/总线视图一致、
`amoadd.w` 有效、票号没乱。

### 48.5 还没做：picker 的越界

`scheduler_mp.c:239`：

```c
global = _get_global_highest_ready_prio();     // 空表 = -1
local  = _get_local_highest_ready_prio(pcpu);  // CPU1 = 31
if (global < local)                            // -1 < 31
    rt_thread_priority_table[global]           // [-1] 越界
```

**所有线程都 BIND_CPU 时全局表恒空，这个分支必然命中。**

修它属于内核改动，需要单独验证。


## 49. 启用 CPU1 调度前必须先有 per-CPU tick（2026-10-31）

### 49.1 症状

CPU1 调 `rt_system_scheduler_start()` 后：

```
rst:0x8 (HP_SYS_HP_WDT1_RESET)
Core0 Saved PC:0x2f016d76      -> nm: rt_hw_spin_lock
```

### 49.2 真因：两个核抢同一条线程

在 picker 里插探针（`scheduler_mp.c` 的 `_scheduler_get_highest_priority_thread`，
**必须插在 `return` 之前**，否则被优化掉）：

```
[pick] cpu=0 global=10 local=31 -> main
[pick] cpu=1 global=10 local=31 -> main
```

**两个核都选中 `main`。** 它是未绑定线程，住在全局就绪表，**对每个核可见**。

两个核同时跑它 → 同栈同寄存器 → 崩坏 → CPU0 卡在 `rt_hw_spin_lock`。

### 49.3 这不是 picker 的 bug

`global < local` 的比较是**无符号**的（`rt_ubase_t`），
全局表空时 `-1` → `0xFFFFFFFF` → 比较为假 → 走 local 表，**正确**。

**SMP 的设计就是让未绑定线程在核间迁移。** 问题在于本端口缺少这套设计假设的前提。

### 49.4 三个缺失的前提

| 前提 | 状态 | 后果 |
|---|---|---|
| per-CPU tick | ❌ 只有 CPU0 有 | 迁到 CPU1 的线程不被抢占，等于死掉 |
| per-CPU 中断路由 | ❌ 全指向 CPU0 | 迁过去的线程收不到外设事件 |
| 线程绑定到核 | ❌ 大部分未绑定 | 全局表非空，任何核都能抢 |

### 49.5 正确的推进顺序

1. **先给 CPU1 一个 tick**（`SYSTIMER_TARGET` 路由到 CPU1，或独立 tick 源）
2. 再把需要的外设中断路由到 CPU1
3. 最后才让 CPU1 进调度器

**顺序不能颠倒**：没有 tick 的核上的线程是死的，抢过去只会让系统看起来"卡住"。

### 49.6 当前交付配置

```c
/* rtconfig.h */
#define RT_USING_SMP
#define RT_CPUS_NR                      2
#define RT_USING_SMP_SLAVE              /* 启动 CPU1 */
#define RT_USING_SMP_SLAVE_LOCK         /* CPU1 取锁 + 还锁，验证 ticket lock */
```

```
msh > s31stat
cpu1   : started (0x5a5a5a5a)
cpu1lk : acquired (0x12345678)
=== 20 passed, 0 failed ===
```

**CPU1 起来了、能用共享锁，但不参与调度。这是当前硬件的正确状态。**


## 50. CPU1 调度的依赖顺序（2026-10-31 修正）

### 50.1 修正 W52 的顺序

W52 写"先给 CPU1 一个 tick"。**排错了**，tick 之前还有一层。

### 50.2 正确的顺序

| 步 | 内容 | 涉及文件 |
|---|---|---|
| **1** | **per-CPU 中断栈** | IDF `sections.ld.in` + `vectors_rtthread.S` |
| 2 | CPU1 的 tick（alarm 1 + TARGET1 + route 到 core 1） | 新文件 `smp_tick.c` |
| 3 | 中断路由分核（`rt_hw_intr_route` 加 cpu 参数） | `interrupt.c` |
| 4 | CPU1 进调度器 | `smp_slave_sched.c` |

### 50.3 为什么栈必须在 tick 之前

tick 是中断。现在的中断栈：

```ld
__stack_start__ = .;
. += __STACKSIZE__;
__stack_end__ = .;
```
```asm
la    sp, __stack_end__        /* 固定地址，两个核相同 */
```

**CPU0 正在压帧时 CPU1 的 tick 进来，会压到同一片内存。**

结果不是"tick 不准"，而是**两个核的中断上下文一起被破坏**——
比现在"CPU1 不参与调度"更糟。

### 50.4 检查清单：加任何 CPU1 中断前问三句

1. 这个中断会在两个核上**并发**吗？
2. 它用的**栈**是 per-CPU 的吗？
3. 它访问的**全局状态**有锁吗？

**三个都答"是"才能装。**

### 50.5 不要留未接线的代码

这一轮写了 `rt_hw_interrupt_stack_top()` 然后删了：

- 没被调用 = 死代码
- 返回 NULL 给 CPU1 是错的（应该真分配第二份）
- 分配要同时改 linker 和汇编，分开做会得半成品

**未接线的"看似有用"代码会被下一轮当成已完成的工作。**


## 51. per-CPU 中断栈（2026-10-31）

### 51.1 为什么必须做

`vectors_rtthread.S` 原来是：

```asm
la    sp, __stack_end__        /* 固定地址 */
```

两个核同时进中断 → 从同一个地址往下压帧 → 互相覆盖。

### 51.2 实现（不改 IDF linker）

```c
/* port/esp32s31/smp_istack.c */
static char s31_cpu1_istack[CONFIG_IDF_RTTHREAD_INTERRUPT_STACK_SIZE]
    __attribute__((aligned(16)));

void *rt_irq_stack_top[RT_CPUS_NR] = {
    &__stack_end__,                              /* CPU0：不变 */
    s31_cpu1_istack + sizeof(s31_cpu1_istack),   /* CPU1：独立 */
};
```

```asm
/* vectors_rtthread.S */
    la    t2, rt_irq_stack_top
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t2
    lw    sp, 0(t0)
```

### 51.3 两个必须注意的点

**(a) 不要用 `void *const`**

const 指针数组会被放进 `.flash.rodata`，而这段代码在 flash cache 关闭时执行。
**非 const 才留在 `.data`。**

**(b) 必须加 `-u`**

```cmake
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-u rt_irq_nest_slot" "-u rt_irq_stack_top")
```

汇编引用、无 C 引用 → archive member 不被拉入 → `undefined reference`。

### 51.4 验证方法：直接读 ELF

```bash
riscv32-esp-elf-nm -C -S elf | grep -E 'rt_irq_stack_top|s31_cpu1_istack'
# 2f019338 00000008 D rt_irq_stack_top      <- D = .data（不是 R = .rodata）
# 2f01fe10 0000a000 b s31_cpu1_istack       <- b = .bss

riscv32-esp-elf-objdump -s --start-address=0x2f019338 --stop-address=0x2f019340 elf
# 2f019338 50ba032f 109e022f   -> [0x2f03ba50, 0x2f029e10]
```

**`[0] == __stack_end__` 是"CPU0 没有回归"的硬证据。**

### 51.5 回归

`s31test` 20/20 连跑 3 次、`check_image.py` 9/9、31 命令、
`s31twai` 4/4 连跑 3 次、`s31rmt`/`s31mcpwm` PASS。


## 52. 新增 `s31smp`：per-CPU 状态一屏可见（2026-10-31）

### 52.1 为什么需要它

前面每一轮都是"读源码 → 插探针 → 得结论 → 删探针"，
下一轮又要重新推导。把**可测状态变成永久命令**。

### 52.2 用法

```
msh > s31smp
cpus   : 2, running on cpu 0

cpu cur_thread idle_thread irq_nest tick  istack
--- ---------- ----------- -------- ----- --------
  0   2f03e870    2f01fa20        0  2966 0x2f03ba50
  1          0    2f01fac4        0     0 0x2f029e10

istack : distinct per core
```

### 52.3 怎么读

| 列 | 判据 |
|---|---|
| `cur_thread` = 0 | 该核没有在跑线程 |
| `idle_thread` ≠ 0 | 该核的 idle 线程已创建 |
| `tick` | **该核收到的 tick 数**——做完 CPU1 tick 后应开始增长 |
| `istack` | 两核必须不同，最后一行会直接判 SHARED |

### 52.4 ⚠️ tick 的顺序约束

`rt_sched_tick_increase()`（`scheduler_comm.c:294`）：

```c
thread = rt_thread_self();
if (RT_SCHED_PRIV(thread).remaining_tick > tick)   /* 无 NULL 检查 */
```

**CPU1 调度器未启动时 `current_thread == NULL` → 崩。**

**⇒ 顺序：线程绑定到 CPU0 → CPU1 调度器 → CPU1 tick → 路由分核。**

### 52.5 加 msh 命令的老坑（第五次）

`MSH_CMD_EXPORT` 只写 section，**没有代码引用 → `--gc-sections` 丢整个文件**。

```c
/* 命令文件 */
int s31_smp_cmd_init(void) { return 0; }

/* board.c */
#ifdef RT_USING_SMP
    s31_smp_cmd_init();
#endif
```

**验证方法**（必做）：
```bash
riscv32-esp-elf-nm elf | grep cmd_smp      # 符号在不在
```


## 53. CPU1 开始调度（2026-10-31）

### 53.1 结论

```
msh > list thread
thread       cpu bind pri  status
tshell         0   0   20  running
tidle1         1   1   31  running      <- CPU1 在跑自己的 idle
tidle0       N/A   0   31  ready
```

**双核调度真的在跑了。**

### 53.2 根因：一次取锁、两次放锁

```c
/* 错的 */
rt_hw_spin_lock(&_cpus_lock);      /* 取 */
...
rt_hw_spin_unlock(&_cpus_lock);    /* 放 1 */
rt_system_scheduler_start();       /* 内部放 2 —— scheduler_mp.c:493 无条件 */

/* 对的 */
rt_hw_spin_lock(&_cpus_lock);      /* 取 */
...
rt_system_scheduler_start();       /* 内部放 —— 配平 */
```

**一次取、两次放 → ticket lock 的 owner 超过 ticket → CPU0 无限自旋。**

判据：
```
rst:0x8 (HP_SYS_HP_WDT1_RESET)
Core0 Saved PC:0x2f016d30     -> nm: rt_hw_spin_lock
```

### 53.3 反直觉：线程绑定不是必需的

内核保持 upstream 的 `bind_cpu = RT_CPUS_NR`（未绑定）也能跑。

**因为锁的等待本身就是同步点：** CPU0 持有 `_cpus_lock` 直到它自己的
`rt_system_scheduler_start()`，而在那之前它已经把第一个线程从就绪队列摘走。
CPU1 等到锁时全局表已空，自然走 local 分支拿 `tidle1`。

**`rt_system_scheduler_start()` 一进门就放锁，是在表达"第一个线程已被接管"。**

### 53.4 教训：观测到的"原因"可能是后果

| 轮次 | 我看到 | 我以为 | 实际 |
|---|---|---|---|
| W49 | shell 静默 | CPU1 抢走 shell | unlock 错乱 |
| W52 | 两核选中 main | 要绑定线程 | unlock 错乱 |
| W56 | 仍卡 spin_lock | 绑定不是解，去查锁 | unlock 错乱 ✅ |

**改了没用，说明它不是原因。回去查更深一层。**

### 53.5 下一步

CPU1 现在只跑 idle，因为：

| 缺什么 | 现状 |
|---|---|
| CPU1 的 tick | `s31smp` 的 `tick` 列为 0 |
| 线程绑定到 CPU1 | 没有线程被绑到 cpu 1 |

tick 现在可以做了 —— CPU1 已有 `current_thread`（`rt_sched_tick_increase`
不会解引用 NULL 了，见 R139）。

## 54. P3 完成状态与交接（2026-10-31）

### 54.1 验收命令

```
msh > list thread        # tidle1 的 cpu=1、status=running
msh > s31smp             # cur_thread == idle_thread（cpu 1）
msh > s31test            # 20/20
msh > s31stat            # cpu1: started / cpu1lk: acquired
```

### 54.2 达成的与未达成的

| 达成 | 未达成 |
|---|---|
| CPU1 启动并执行 IRAM 代码 | CPU1 没有 tick（`s31smp` 的 `tick` 列恒 0） |
| CPU1 正确使用 ticket lock | 没有业务线程绑到 CPU1 |
| per-CPU 中断栈 | 中断路由仍是单核（全指向 CPU0） |
| **CPU1 参与调度（跑 idle）** | |

**"双核跑起来了" ≠ "双核都在干活"。这个区分必须保留。**

### 54.3 下一步的正确顺序

| 步 | 内容 | 前置条件 |
|---|---|---|
| 1 | **CPU1 的 tick** | ✅ 已满足（`current_thread` 非 NULL，R139） |
| 2 | 线程绑定 CPU1 | 需要 tick 先能用 |
| 3 | 中断路由分核 | 需要 per-CPU tick |
| 4 | M6 连 AP | **需要用户提供 SSID/密码** |

### 54.4 仓库状态

```
rt-thread   : git status 干净（0 处改动）
esp-idf-rtt : 12 个文件，全在 components/ 下
```

**上游 `rt-thread` 未打任何补丁。** 升级 RT-Thread 版本不需要重新应用补丁。

### 54.5 全命令清单（32 条）

```
free  ps  list  help
s31pinirq  s31pwm  s31timer  s31test  s31led  s31psram  s31intr
s31spiloop  s31adcdbg  s31adc  s31pcnt  s31mcpwm  s31rmt  s31twai
s31events  s31fpu  s31stat  s31wifi  s31ble  s31smp
（加上 RT-Thread 自带命令）
```

### 54.6 交接要点

**如果接手这个项目，先做这三件事：**

1. `board.py COM16 "s31test"` —— 20/20 才算基线正常
2. `board.py COM16 "s31smp"` —— 看 per-CPU 状态
3. 读 `plan` 的 §12.6x 序列 —— 那里是**所有被推翻的结论**及其依据

**不要相信任何没有 ELF 级或运行时证据的说法**，包括本文档里的。
每一条重要结论后面都标了它是怎么验证的。

## 55. M6：连接真实 AP（2026-10-31）

### 55.1 先查频段能力，再调参数

```bash
grep SOC_WIFI_SUPPORT_5G components/soc/*/include/soc/soc_caps.h
# 只有 esp32c5
grep SOC_WIFI_SUPPORT_5G components/soc/esp32s31/include/soc/soc_caps.h
# 没有 -> S31 是 2.4 GHz 器件
```

**5 GHz 信标在射频层面收不到，和密码无关。**

### 55.2 `s31wifiscan`：把"看不见"变成可测

```
msh > s31wifiscan
  # ch  rssi  bssid              ssid
  1  1   -50  d4:ee:07:40:1f:26  刻于晴空之上_2.4G
 14  1   -79  c0:f6:c2:d9:e1:51  CQNU-Eduroam

s31wifiscan: 14 AP(s), all on channels 1..14 = the 2.4 GHz band
```

**信道 1..14 = 2.4 GHz；36..177 = 5 GHz。列表里只有低信道，
就证明无线电按 caps 工作。**

### 55.3 连接

```
msh > s31wifi 刻于晴空之上_2.4G keqing1120
  ...
  esp_wifi_connect           ok
  wifi:state: init -> auth -> assoc -> run
s31wifi: PASS, associated in 350 ms
  bssid d4:ee:07:40:1f:26  ch 1  rssi -49  auth 3
```

### 55.4 ⚠️ 坑：设配置 ≠ 发起连接

```c
esp_wifi_set_config(...);   /* 只存凭据 */
esp_wifi_start();           /* 只启动 station */
esp_wifi_connect();         /* ← 关联必须显式调用 */
```

**漏掉第三行：等 15 秒，一个事件都没有（连 disconnect 都没有）。**

判据：

| 现象 | 含义 |
|---|---|
| 有 `STA_DISCONNECTED` + reason | 真的试过，被拒绝 |
| **什么都没有** | **根本没请求** |

**"超时且无错误事件" 是 "请求没发出" 的签名，不是 "对端拒绝"。**

### 55.5 怎么用

```
s31wifi                     # 只启动栈（原行为，PASS 表示栈 OK）
s31wifi <ssid> <password>   # 启动并关联
s31wifiscan                 # 扫 2.4 GHz AP
```

## 56. 用已知可用的 bin 做基线判定（2026-10-31）

### 56.1 场景

改了一堆东西之后，板子能打印 banner 但命令不执行。**此时最耗时的错误方向是查硬件。**

### 56.2 正确顺序

```powershell
# 1. 烧回上次验证通过的 bin
python -m esptool --chip esp32s31 -p COM16 -b 921600 write_flash 0x10000 tools\tmp\smp_working.bin

# 2. 测
board.py COM16 "s31test"
```

- **旧固件能跑** → 软件回归，二分源码
- **旧固件也不行** → 环境问题（串口/进程/电源）

**这一步几分钟，能省掉整轮的环境排查。**

### 56.3 所以：每达成一个里程碑就存一份 bin

```powershell
Copy-Item build\s31_rtt_test.bin tools\tmp\<milestone>.bin
```

**W48 存的那一份在这一轮直接救了场。**

### 56.4 二分时要 clean build

```powershell
Remove-Item build -Recurse -Force
```

**删源文件不触发 CMake 重新配置**（R22），只 `build.ps1 build` 会保留旧的编译单元。

### 56.5 ⚠️ "时而通过"不是通过

| 观察 | 结论 |
|---|---|
| 同一配置连跑 3 次都过 | 暂时接受 |
| 同一配置有时过有时不过 | **竞态，不能记成通过** |
| 改了别的东西之后不再通过 | **原结论作废** |

**W56 的 CPU1 调度就是这样：当时 3 次都过，之后的每次重建都不行。**

### 56.6 W59 的具体教训

| # | 教训 |
|---|---|
| 1 | `-u` 拉进来的驱动可能带 `ESP_SYSTEM_INIT_FN` 构造，撞 RT-Thread 启动链 |
| 2 | `PRIV_REQUIRES` 不把 include 目录放到编译行，要显式 `PRIV_INCLUDE_DIRS` |
| 3 | 库在链接行上的**顺序**决定单遍链接器能否解析：`libesp_driver_*` 在 `librtthread.a` 之前 |
| 4 | 一个**没被调用**的文件也能改变行为（它带进来的符号会拉进更多 init 构造） |

## 57. RT-Thread 一致性检查清单（2026-11-01）

### 57.1 三条可自动化的检查

```bash
# 1. init 函数返回类型：RT 约定是 rt_err_t，不是 int
grep -rn "^int .*_init(void)" port/

# 2. 驱动是否自己导出：RT 约定每个驱动 INIT_BOARD_EXPORT
grep -rn "INIT_BOARD_EXPORT\|INIT_DEVICE_EXPORT" port/

# 3. rt_hw_board_init 里还剩多少手工调用
grep -c "^    \(rt_hw_\|s31_\).*_init();" port/common/board.c
```

### 57.2 判定标准

| 指标 | 符合 RT 约定 | 说明 |
|---|---|---|
| init 返回类型 | `rt_err_t` | `int` 也能跑，但不符约定 |
| 驱动注册方式 | `INIT_*_EXPORT` | 手工调用 = 绕行 |
| `rt_hw_board_init` 长度 | 短 | 只做 clock/heap/console |
| 日志 | `LOG_D/LOG_E` | `rt_kprintf` 可以但不符合模块化约定 |

### 57.3 ⚠️ ESP-IDF 下 `INIT_*_EXPORT` 的陷阱

**导出语法对、`.rti_fn` 段被 KEEP、walker 在跑 —— 但 archive member 不会自动拉进来。**

```bash
# 加完 INIT_BOARD_EXPORT 后必须验证
riscv32-esp-elf-nm elf | grep rt_hw_xxx_init
# 没有 -> 对象被 gc 掉了
```

修法：**每个导出的 init 配一个 `-u`**

```cmake
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u rt_hw_pin_init")
```

**这个组合才是对的**：
- 驱动用标准 RT-Thread 方式声明
- `-u` 显式处理 ESP-IDF 的 archive 模型

### 57.4 命名规范：链接锚点不要叫 `_init`

```c
/* 误导：看起来是初始化函数 */
int s31_selftest_init(void) { return 0; }

/* 清楚：说明它只是为了把对象拉进链接 */
void s31_selftest_link_anchor(void) { }
```

**`*_init` 会被误读成"这里有初始化逻辑"。** 一个空函数叫 `_init`，
下一个人一定会花时间找它到底初始化了什么。

## 58. 一致性审计清单（第二轮，2026-11-01）

### 58.1 五条可自动化的检查

```bash
# 1. init 返回类型（RT 约定 rt_err_t）
grep -rn "^int .*_init(void)" port/

# 2. 驱动是否自己导出
grep -rn "INIT_BOARD_EXPORT\|INIT_DEVICE_EXPORT" port/ | wc -l

# 3. board.c 手工调用数
grep -c "^    \(rt_hw_\|s31_\).*();" port/common/board.c

# 4. console 钩子在不在应用层（应该在驱动层）
grep -rn "rt_hw_console_output" applications/ 2>/dev/null   # 应该为空

# 5. 导出与 -u 是否一一对应
#    exported but no -u -> 链接时会被丢
#    -u but no export    -> 冗余
```

### 58.2 判定：`rt_hw_console_output` 该放哪

**上游统计：**

```
driver/board layer = 44
applications/      = 1
```

**放驱动层。** 应用要能跨板移植；console 是板级能力。

### 58.3 ⚠️ 本端口的有意偏离：不调 `rt_console_set_device()`

上游 BSP 通常在 board init 末尾：

```c
rt_console_set_device(RT_CONSOLE_DEVICE_NAME);
```

**本端口不调。** 这样 `_kputs()`（`kservice.c:342`）永远走
`rt_hw_console_output()` 这条分支，于是：

| 好处 | 说明 |
|---|---|
| 串口驱动出错时 console 仍可用 | ROM 输出不依赖驱动状态 |
| **panic 路径能打印** | 这是最关键的 |

**偏离是有意的，必须写进注释**，否则下一个人会"顺手修好它"。

### 58.4 搬代码时必然踩的坑

```
error: implicit declaration of function 'esp_rom_output_putc'
```

**换位置 = 换头文件集合。** 把函数从 main.c 搬到 serial.c 后，
`esp_rom_serial_output.h` 就不在包含链里了。

### 58.5 当前一致性状态

| 指标 | 值 |
|---|---|
| init 返回 `int` | **0** |
| `INIT_*_EXPORT` | **11** |
| board.c 手工调用 | **5**（2 个真 init + 3 个链接锚点） |
| console 钩子位置 | **驱动层 serial.c** |
| `list device` | 8 设备，与改造前相同 |

## 59. `tools/check_rtt_style.py`（2026-11-01）

### 59.1 用法

```powershell
python tools\check_rtt_style.py            # 报告，总是退出 0
python tools\check_rtt_style.py --strict   # 有 FAIL 时退出 1
```

**每次改动后跑一次**，把"风格漂移"变成数字。

### 59.2 10 项检查

```
RT-Thread port conformance
====================================================================
[PASS] init returns rt_err_t         0 int-returning: []
[PASS] INIT_*_EXPORT present         14 exports
[PASS] exports reach the image       all 14 present
[PASS] board.c has few hand calls    2 hand calls
[PASS] anchors not named _init       suspicious: []
[PASS] console hook in driver layer  app=[] port=['serial.c']
[PASS] framework registration used   all five OK
[PASS] rt_uint32_t in declarations   none left
[PASS] msh commands anchored         18 files, 5 anchors
[PASS] shell bound to a device       finsh_set_device("uart0")
====================================================================
10/10 checks passed
```

### 59.3 ⚠️ 脚本第一版的两个误报

**(a) 把 `-u` 当判据**

`board.c` 自己的三个导出没有 `-u`，被报失败。但它们和 `rt_hw_board_init`
在同一个目标文件里，被引用是必然的。

**判据改成"符号在不在 ELF 里"（`nm`）。**

> **检查手段 ≠ 检查目的。** `-u` 是手段，"到镜像"是目的。

**(b) 硬套 `rt_uint32_t`**

13 处改完后编译失败：

```
error: passing argument 3 of 'gpio_ll_get_intr_status'
       from incompatible pointer type
```

**`rt_uint32_t` 和 `uint32_t` 是不同 typedef，指针不兼容。**

必须豁免：
- 地址交给 IDF LL 函数的**输出参数**
- **`freertos_api.c` 的参数**（FreeRTOS ABI）

### 59.4 一般性结论

| 结论 | 说明 |
|---|---|
| **检查结果属性，不检查实现细节** | 否则把正确情况报成失败 |
| **一致性规则要有例外清单** | 硬套规则导致编译失败 = 规则不完整 |

### 59.5 建议的用法

**放进每次改动的收尾流程：**

```powershell
python tools\check_rtt_style.py --strict
python tools\check_image.py
board.py COM16 "s31test"
```

三条都过，再保存 bin。

## 60. op 契约核对清单（2026-11-01）

### 60.1 五个 driver 的 op 契约逐条核对

| ops | 契约要点 | 本端口 |
|---|---|---|
| `rt_i2c_bus_device_ops` | `master_xfer` 返回**发送的 msg 数** | ✅ |
| `rt_spi_ops` | `xfer` 返回**字节数** | ✅ |
| `rt_adc_ops` | `convert` 用 `rt_err_t` + 输出参数 | ✅ |
| `rt_adc_ops.get_vref` | **未知时返回负值是正确的** | ✅ 返回 `-1` |
| `rt_pin_ops` | 缺的 5 个成员在 `RT_USING_DM`/`RT_USING_PINCTRL` 里 | ✅ 两个宏未定义 |
| `rt_uart_ops.control` | 未识别 cmd 返回 `RT_EOK` | ✅ 与上游 8 个 `drv_uart.c` 一致 |

### 60.2 ⚠️ 不要"修"的两处

**(a) `s31_adc_vref()` 返回 `-1`**

这个 SoC 没有校准路径。**返回"典型值 1100"会显得权威但实际偏几个百分点。**
`-1` 是 `rt_adc_ops.get_vref` 允许的"未知"表示。

**(b) `uart control` 未识别 cmd 返回 `RT_EOK`**

有人会觉得应该返回 `-RT_EINVAL`。查了上游 8 个 `drv_uart.c`：
**全部是 `RT_EOK`。** 改了反而不符。

### 60.3 第 11 项检查：注释与值一致

```python
# 11. rtconfig.h comments must not contradict its values.
```

检查三条具体矛盾：
- 说"单核"但 `RT_CPUS_NR == 2`
- 说"没有 devices/console"但都已定义
- 说"没有 Finsh"但 `RT_USING_FINSH` 已定义

**过时注释比没有注释更危险**：没有注释时人会去看代码，有过时注释时人会信它。

### 60.4 写配置注释的正确方式

**不要写"某个历史阶段是什么"**，写：

```c
/* 现在是什么 */
#define RT_CPUS_NR                      2

/* 为什么不是别的 */
/* CPU1 现在只跑 idle；让它接调度工作的代码在 RT_USING_SMP_SLAVE_LOCK 后面，
 * 当前未启用 —— 实测会和 CPU0 的就绪表编辑竞争（W56/W59）。 */

/* 哪些是有意不定义 */
/* RT_USING_SOCKET / RT_USING_LIBC - lwIP 和 newlib 没接进来
 * RT_USING_DFS                    - 没有文件系统 */
```

**三段式：值 → 现状 → 原因与例外。**

## 61. rthw.h 契约核对（2026-11-01）

### 61.1 方法：比对 ELF 符号，不看源码

```bash
riscv32-esp-elf-nm -C build/s31_rtt_test.elf > syms.txt
# rthw.h 的 39 个移植层函数逐个查
```

**缺失 19 个，分四类：**

| 类别 | 判断 |
|---|---|
| 宏（`rt_hw_interrupt_disable` → `rt_cpus_lock`） | ✅ |
| 空宏（`RT_USING_CACHE` 未定义时的 icache/dcache） | ✅ |
| weak 默认（`rt_hw_cpu_reset`、backtrace） | ✅ |
| **真缺口**（有调用者但只有 weak 空实现） | ❌ |

### 61.2 找到的真缺口：`rt_hw_us_delay`

```c
/* kservice.c */
rt_weak void rt_hw_us_delay(rt_uint32_t us)
{
    (void) us;   /* 什么都不做 */
    LOG_W("rt_hw_us_delay() doesn't support for this board.");
}
```

**`components/libc/posix/delay/delay.c` 的 `usleep`/`nanosleep` 调它。**

**⇒ 所有 POSIX 睡眠都是空操作。**

### 61.3 ⚠️ `-u` 打不过同 archive 的 weak 定义

```bash
# 加了 -u rt_hw_us_delay 之后
nm elf | grep rt_hw_us_delay
40012836 W rt_hw_us_delay        # 还是 W
```

**`-u` 只是标记"未定义"，weak 定义已在同 archive 内被链接，满足了引用。**

**修法：把强定义放进必然被链接的目标文件。**

本次选 `gptimer.c`（被 `-u rt_hw_timer_init` 拉进，且 `s31_systimer_us()` 在同一个文件）。

修后：`400163e4 T rt_hw_us_delay`。

### 61.4 第 12 项检查：符号必须是 `T` 不是 `W`

```python
# 12. Port-owned rthw.h hooks must be STRONG, not the weak stub.
hooks = ["rt_hw_us_delay", "rt_hw_interrupt_mask", ..., "rt_hw_console_output"]
weak = [sym for sym in hooks if nm_type(sym) in ("W","V","w","v")]
```

**必须查 ELF，不能查源码**：
源码写了强定义、编译通过、链接成功，
**运行时却是 weak 空实现** —— 三条全部看不出来。

### 61.5 检查清单：移植层该提供的 rthw.h 函数

| 函数 | 本端口 |
|---|---|
| `rt_hw_interrupt_mask/umask/install/uninstall` | ✅ 强定义 |
| `rt_cpus_lock/unlock` | ✅ |
| `rt_hw_local_irq_disable/enable` | ✅ |
| `rt_hw_console_output` | ✅ serial.c |
| `rt_hw_us_delay` | ✅ **W64 补上** |
| `rt_hw_context_switch*` | ✅ 汇编 |
| `rt_hw_spin_lock/unlock/init` | ✅ |
| `rt_hw_ipi_send` | ✅ |
| `rt_hw_secondary_cpu_up/idle_exec` | ✅ |
| `rt_hw_interrupt_init` | ⬜ 无调用者，可省 |
| `rt_hw_exception_install` / `rt_hw_show_memory` | ⬜ 无调用者 |
| `rt_hw_stack_guard_init` | ⬜ `RT_USING_HW_STACK_GUARD` 未定义 |

## 62. `s31test rhwh`：rthw.h 契约的运行时测试（2026-11-01）

### 62.1 用法

```
msh > s31test rhwh
[PASS] rhwh.ud  1000 us request took 1000 us
[PASS] rhwh.iv  using free INUM 8
[PASS] rhwh.ui  worst-case param kept the handler, re-install returned it
[PASS] rhwh.ri  install returned the previous handler
[PASS] rhwh.mk  mask/umask: 1 -> 1 -> 0
[PASS] rhwh     5 sub-checks, 0 bad
```

**`s31test` 全跑时也包含它，总数 26 项。**

### 62.2 五项各验什么

| 子检查 | 失败意味着 |
|---|---|
| `rhwh.ud` | `rt_hw_us_delay` 是 weak 空实现，或 SYSTIMER 单位算错 16 倍 |
| `rhwh.iv` | 没有空闲中断线（报 PASS + skipped，不是缺陷） |
| `rhwh.ui` | `uninstall` 无条件删除，会误删后来者的注册 |
| `rhwh.ri` | `install` 没返回前一个 handler，链式调用会丢 |
| `rhwh.mk` | mask/umask 写错寄存器 |

### 62.3 ⚠️ 写这类测试的三条纪律

**(a) 探测不能改变被探测的状态**

第一版用"安装一下看返回值"判断 line 是否空闲 —— **为了读状态而改变状态**，
而且和 `st_nest` 抢 line 7。改用只读的 `intr_handler_get()`。

**(b) 资源被占用不是缺陷**

扫不到空闲 line 时报 `PASS + skipped`。**报 FAIL 会让自检在正常配置下变红。**

**(c) 测试不要用私有宏**

`RTT_INUM_NUM` 是 `interrupt.c` 的内部宏，测试里用 `SOC_CPU_INTR_NUM`。

### 62.4 连带：`check_image.py` 的 init 顺序检查

W60/W61 把 11 个驱动改成 `INIT_BOARD_EXPORT` 后：

```
[FAIL] init table order   19 entries by address
```

**这是检查按设计工作**（文件注释：加 export 就会改这个表）。

**处理原则：不放宽断言，逐项写清楚新期望。**

并且在注释里注明：
- **BOARD 段的相对顺序不是契约**（链接器拉取顺序决定）
- **只在两个 marker 之间才是契约**

更新后 9/9。

## 63. `s31test dev`：device 框架契约（2026-11-01）

### 63.1 用法

```
msh > s31test dev
[PASS] dev.fd   find() returns NULL for a bogus name
[PASS] dev.ur   read before open returns 0
[PASS] dev.op   open without an open op: 0, ref_count 1
[PASS] dev.o2   second open: ref_count 2
[PASS] dev.cl   extra close refuses with -RT_ERROR, ref_count 0
[PASS] dev.ct   bogus control answered 0 (...)
[PASS] dev      7 sub-checks, 0 bad
```

**`s31test` 全跑时包含它，总数 33 项。**

### 63.2 `dev.op` 为什么最重要

**`i2c0`、`spi0`、`adc1`、`adc2`、`pin`、`pwm0` 都没有 open/close op。**

它们依靠框架里的这段：

```c
if (device_open != RT_NULL)
    result = device_open(dev, oflag);
else
    dev->open_flag = (oflag & RT_DEVICE_OFLAG_MASK);   /* 直接记账 */

if (result == RT_EOK || result == -RT_ENOSYS)
{
    dev->open_flag |= RT_DEVICE_OFLAG_OPEN;
    dev->ref_count++;
}
```

**没人对 `i2c0` 调 `rt_device_open()`**，所以这段坏了也不会有人发现 ——
直到用户接上一个 I2C 传感器。

### 63.3 ⚠️ `dev.ct`：为什么记录而不判失败

```c
rt_device_control(uart0, 0x7f00, NULL);   /* 返回 RT_EOK，不是负错误码 */
```

**看起来违反 RT 文档。** 查上游：

```
8 个 drv_uart.c 的 control() default 分支：全部返回 RT_EOK
```

**这是共享约定。** 所以：

```c
st_report("dev.ct", ST_REPORT_OK,
          "bogus control answered %d (drivers return RT_EOK for unknown "
          "commands, as upstream drv_uart.c does)", (int)e);
```

**理由：如果判成 FAIL，下一轮会有人"修复"它，反而让端口偏离上游。**

### 63.4 判定原则

```
看到"违反文档"的行为
  -> 先查上游 8 个以上同类实现
  -> 一致  => 是约定，记录现状
  -> 不一致 => 是本端口的缺陷，修
```

**"修得更严格"不等于"更正确"。**

## 64. 头文件与实现的一致性（2026-11-01）

### 64.1 两个必须查的方向

前面几轮查的是**实现**是否符合 RT 约定。这一轮查**反方向**：

```
头文件承诺的符号  ->  到底存不存在？
```

```bash
# 从 port_*.h 提取声明，从 port/esp32s31/*.c 提取定义，取差集
# 本轮结果：2 个声明没有定义
```

### 64.2 W60 改名的遗留

`*_init` → `*_link_anchor` 的改名**只改了 .c 和 board.c，漏了头文件**：

```c
/* port_selftest.h 里还写着 */
int s31_selftest_init(void);      /* 函数已经不存在 */
```

### 64.3 为什么这比注释过期严重

| 问题 | 后果 |
|---|---|
| 注释过期 | 误导人 |
| **接口过期** | **编不过，而且报错指向一个"看起来该存在"的函数** |

**当前没暴露，只是因为没人 include 这两个头 —— 这是运气，不是设计。**

### 64.4 `extern "C"` 守卫

RT-Thread 自己的头文件全都包 `extern "C"`。本轮补了 3 个：

| 头文件 | 声明数 |
|---|---|
| `port_pwm.h` | 2 |
| `port_serial.h` | 1 |
| `port_timer.h` | 5 |

**纯 C 下无影响，C++ include 会因名字修饰链接失败。关掉它的代价是零。**

`port_board.h` 声明 0 个函数，不需要。

### 64.5 ⚠️ 改名的收尾清单

改名时容易只改"能编译的那部分"：

| 要改的 | 编译会提醒吗 |
|---|---|
| `*.c` 里的定义和调用 | ✅ 会 |
| `*.h` 里的声明 | ❌ **不会**（没人 include 就不报错） |
| 注释里的提法 | ❌ 不会 |
| 文档里的提法 | ❌ 不会 |

**所以改名脚本必须扫全部文本引用，并在收尾时用 grep 验证旧名字零出现。**

### 64.6 现在的检查项（14 项）

```
[PASS] init returns rt_err_t
[PASS] INIT_*_EXPORT present
[PASS] exports reach the image
[PASS] board.c has few hand calls
[PASS] anchors not named _init
[PASS] console hook in driver layer
[PASS] framework registration used
[PASS] rt_uint32_t in declarations
[PASS] msh commands anchored
[PASS] shell bound to a device
[PASS] rtconfig.h comments consistent
[PASS] rthw hooks are strong
[PASS] port headers match the sources      <- 本轮新增
[PASS] port headers have extern "C"        <- 本轮新增
```

## 65. `rtconfig.h` 完整性（2026-11-01）

### 65.1 两个方向

| 方向 | 问题 |
|---|---|
| **死开关** | 定义了但没代码读 —— 制造错误印象 |
| **缺一半** | 成对宏只设了一个 —— 能编过但行为变 |

### 65.2 死开关：`RT_USING_I2C_BUSSING`

**四条独立证据证明它没有任何作用：**

| 检查 | 结果 |
|---|---|
| `dev_i2c_core.c` gate | 只 gate `RT_USING_DM` |
| `rtdevice.h` I2C 头 gate | `RT_USING_I2C` |
| 466 个上游 BSP | 0 个定义它 |
| 全树搜索 | 0 处引用 |

**为什么比没有开关糟：**

```c
#define RT_USING_I2C
#define RT_USING_I2C_BUSSING     /* 读起来像"配了 master/slave" */
```

**没有开关时人查代码；有死开关时人信它。**

### 65.3 ⚠️ 但"无代码读取"不等于"该删"

`ARCH_RISCV32` 同样无代码读取，但：

```
24 个上游 BSP 定义 ARCH_RISCV32
21 个定义 ARCH_RISCV64
55 个定义 ARCH_RISCV
```

**这是约定标记（"这个 BSP 是 RV32"），不是条件编译开关。**
删掉会与兄弟 BSP 不一致。

**判定：**

| 类型 | 例子 | 处理 |
|---|---|---|
| 条件编译开关 | `RT_USING_I2C_BUSSING` | 死则删 |
| **约定标记** | `ARCH_RISCV32`、`SOC_ESP32_S31` | **保留** |
| 数值/名字宏 | `RT_NAME_MAX` | 保留（有代码读） |

### 65.4 成对宏（6 对）

| 主开关 | 需要 |
|---|---|
| `RT_USING_SERIAL` | `RT_USING_SERIAL_V1` 或 `_V2` |
| `RT_USING_FINSH` | `FINSH_USING_MSH` |
| `FINSH_USING_MSH` | `FINSH_USING_SYMTAB` |
| `RT_USING_FINSH` | `MSH_USING_BUILT_IN_COMMANDS` |
| `RT_USING_SMALL_MEM_AS_HEAP` | `RT_USING_SMALL_MEM` |
| `RT_USING_CONSOLE` | `RT_USING_DEVICE` 或 `RT_USING_DEVICE_OPS` |

**缺任何一半都能编过，但掉功能或掉命令。**

### 65.5 脚本检查项（16 项）

```
[PASS] init returns rt_err_t
[PASS] INIT_*_EXPORT present
[PASS] exports reach the image
[PASS] board.c has few hand calls
[PASS] anchors not named _init
[PASS] console hook in driver layer
[PASS] framework registration used
[PASS] rt_uint32_t in declarations
[PASS] msh commands anchored
[PASS] shell bound to a device
[PASS] rtconfig.h comments consistent
[PASS] rthw hooks are strong
[PASS] port headers match the sources
[PASS] port headers have extern "C"
[PASS] no dead config macros              <- 本轮新增
[PASS] config pairs complete              <- 本轮新增
```

## 66. 中断嵌套表的隐式不变量（2026-11-01）

### 66.1 问题

`rt_irq_nest_slot` 在 `.bss`，**初始全 0**：

```
2f03154c 00000008 B rt_irq_nest_slot
```

trap handler 无条件读它：

```asm
la   t2, rt_irq_nest_slot
lw   t0, 0(t0)      /* slot[n] = 0 */
lw   t0, 0(t0)      /* 从地址 0 读 -> load access fault */
```

### 66.2 原来靠什么没崩

**`rtthread_startup()` 的第一句：**

```c
rt_hw_local_irq_disable();     /* MIE = 0 */
rt_hw_board_init();            /* 到这里才填表 */
```

**MIE 关着 ⇒ 取不到中断 ⇒ trap 不跑 ⇒ 不读空表。**

**这是与调用方的契约，不是端口的性质。** 任何人在 `entry()` 里加一句开中断就崩，
而且崩在 trap handler 里报 `MTVAL=0x0`，完全看不出是这张表。

### 66.3 已加的检查

```c
rt_hw_irq_nest_init();
if (rt_hw_irq_nest_check() != RT_EOK)
{
    rt_kprintf("board: interrupt nest table is broken, traps will fault\n");
}
```

### 66.4 ⚠️ 判断"注释里的原因"是否成立

原注释说：

> the SMP build cannot reach the trap handler before board init, because the
> MP scheduler and the per-CPU structures do not exist yet

**验证：** `nm` 显示 `_cpus` 在 `.bss`（`2f01eb10 b _cpus`），**从第一条指令就可寻址**。

**注释描述的是信念，不是机制。**

### 66.5 两个手工 init 的判定

| init | 注释的理由 | 查证结果 |
|---|---|---|
| `rt_hw_int_wdt_init` | IDF 路径不调 `esp_startup_start_app()` | ✅ `startup.c:280` 在 `#if !CONFIG_IDF_RTOS_RTTHREAD` 内，**成立** |
| `rt_hw_irq_nest_init` | "MP 调度器还不存在" | ❌ **假的**，真因是 MIE 关闭 |

### 66.6 通用教训

**看到注释写"因为 X 所以 Y"，要验证 X。**

| 情况 | 处理 |
|---|---|
| X 成立，Y 由 X 保证 | 保留，并可考虑加断言 |
| X 不成立 | **注释是错的**，改成真实机制 |
| X 成立但 Y 只是"恰好" | **把 Y 变成可检查的**，别只写在注释里 |

## 67. 验证代码注释里的"因为 X 所以 Y"（2026-11-01）

### 67.1 五条断言的验证结果（`boot_tasks.c`）

| # | 断言 | 结论 |
|---|---|---|
| 1 | `esp_int_wdt_init` 缺了 | ✅ 两符号现在都在镜像（`400982b2` / `4009836e`） |
| 2 | `esp_crosscore_int_init` 不需要 | ⚠️ **理由过期**，结论仍对 |
| 3 | 主线程已被 RT-Thread 替代 | ✅ |
| 4 | 20 KB DRAM 被扣住 | ✅ 数字精确 |
| 5 | `esp_task_wdt_add` 无调用者 | ✅ |

### 67.2 ⚠️ 最危险的一类：结论对、理由错

**原文：**

> `esp_crosscore_int_init()` ... `CONFIG_FREERTOS_UNICORE=y`,
> so there is no other core to poke.

**P1 单核时代正确。现在 `RT_CPUS_NR=2`、CPU1 跑 idle，理由不成立。**

**但结论仍对**，因为：

| 检查 | 结果 |
|---|---|
| RT-Thread 自己的 IPI | ✅ `rt_hw_ipi_send`（`40019cd2 T`） |
| IDF 的跨核中断 | ❌ `esp_crosscore_int_*` 全不在 |

**正确理由：已有自己的实现，不是因为没有第二个核。**

**这一类缺陷不会让任何测试失败，只会在下一个人读注释时误导他。**

### 67.3 两个操作层面的教训

**(a) 验证要用断言自己的定义**

验证第 4 条时我拿 `__heap_end__`（RT-Thread 堆末端）去比注释里的地址，
差了 145 KB，以为注释过期。**算错。** 正确算法：

```
APP_USABLE_DRAM_END = SOC_ROM_STACK_START - SOC_ROM_STACK_SIZE
                    = 0x2f07cfb0 - 0x2000 = 0x2f07afb0
区域 = 0x2f07afb0 .. 0x2f080000 = 20560 B
```

**注释精确。我拿两个不同的东西做了比较。**

**(b) 搜索必须排除 `test_apps`**

`esp_task_wdt_add` 全树 5 处命中，**4 处是 test_apps**，`components/` 里 0 处。

**不排除就会得出"有 4 处调用"的错误结论。**

### 67.4 检查清单

验证一条"因为 X 所以 Y"：

1. **X 还能验证吗** —— 用 `nm` / 源码 / 重新计算，不要用记忆
2. **X 现在还成立吗** —— 配置可能变过（单核→SMP）
3. **Y 真的由 X 推出吗** —— 可能是别的原因
4. **搜索有没有排除 test_apps / bsp / examples**

| 情况 | 处理 |
|---|---|
| X 成立，Y 由 X 保证 | 保留，可加断言 |
| X 不成立 | 改注释 |
| **X 不成立但 Y 仍对** | **改注释，写明正确理由** |
| X 成立但只是"恰好" | 把 Y 变成可检查的代码 |

## 68. `s31twai` 间歇失败的诊断（2026-11-01，未完全解决）

### 68.1 先分清两种间歇

| 测试 | 结果 | 含义 |
|---|---|---|
| 同一次开机连跑 3 次 | 3 次一致 | 不是"调用次数"问题 |
| 五次独立开机 | fail/pass/fail/pass | **是"开机态"问题** |

**这一步就把范围砍掉一半。**

### 68.2 失败信息的具体形态

```
125000 bit/s: len 8, want 4
```

**不是超时、不是"TX buffer 不空"、不是"controller did not start"。**
是 RX 拿到的帧 `dlc = 8` 而发送的是 4 字节。

**信息形态直接指向 RX 解析，不是速率计算。**

### 68.3 两处真 bug（已修，与 IDF 对齐）

**(a) 不清 RBNEI**

```c
/* IDF twai_hal_v2.c */
twaifd_ll_get_rx_frame(dev, rx_frame);
twaifd_ll_clr_intr_status(dev, TWAIFD_LL_INTR_RX_NOT_EMPTY);
```

**(b) 等待条件用 RBNEI 而不是帧计数**

```c
/* 旧 */ while (is_rx_buffer_empty() && !(intr_status & RBNEI))
/* 新 */ while (twaifd_ll_get_rx_frame_count(s_twai) == 0u)
```

IDF 的 `esp_twai_onchip.c` 用 `while (twai_hal_get_rx_msg_count(hal))`，
而那个函数就是 `twaifd_ll_get_rx_frame_count()`。

### 68.4 ⚠️ 但现象没消失

```
修完：4, 0, 0, 4, 0
```

**两个 bug 都是真的，但都不是这个现象的根因。**

**这种情况必须诚实记录：**

| 项 | 记法 |
|---|---|
| 修了两处与 IDF 不一致的代码 | ✅ **修复** |
| 间歇失败消失 | ❌ **没有** |

**不能因为现象没变就把修复回滚** —— 它们让代码与 IDF 一致，这个价值独立。

### 68.5 下一步：打印原始数据，停止推理

```c
twaifd_ll_get_rx_frame(s_twai, &buf);
rt_kprintf("raw: %08x %08x %08x %08x %08x\n",
           buf.words[0], buf.words[1], buf.words[2], buf.words[3], buf.words[4]);
rt_kprintf("cnt=%u empty=%d rwcnt=%u\n",
           twaifd_ll_get_rx_frame_count(s_twai),
           twaifd_ll_is_rx_buffer_empty(s_twai),
           buf.format.rwcnt);
```

**`words[0]` 的 bit[3:0] 就是 dlc。**

判据：

| 观察 | 结论 |
|---|---|
| `cnt` 非 0 且 `dlc=8` | 帧本身有问题（TX 侧或硬件） |
| `cnt` 为 0 却进了拷贝 | 等待条件被跨过 |
| `rwcnt` 非 0 | `get_rx_frame` 的循环多读了 word |

**`rwcnt` 是最可疑的一条**：注释是 "Re-transmission counter"，
但 `twaifd_ll_get_rx_frame` 拿它当循环上限用。

### 68.6 方法学

| 教训 | 说明 |
|---|---|
| **先分清"每次调用"vs"每次开机"** | 一次测试就能砍掉一半范围 |
| **失败信息的形态就是线索** | `len 8` 排除了速率/启动/发送 |
| **修复与现象分离记录** | 否则会把真修复回滚掉 |
| **推理到极限就打印原始值** | 从 `hdr.dlc` 反推已绕一圈 |

## 69. `s31twai` 原始数据分析（2026-11-01，负结果）

### 69.1 探针位置：这一步上一轮做错了

**错误做法：** 在 `twaifd_ll_get_rx_frame()` **之后**打印

```
[rx] cnt=0 empty=1     <- 永远是读取后的状态
```

**拷贝会消费帧，所以这行什么都不说明。**

**正确做法：** 在拷贝**之前**打印

```
[pre] cnt=1 empty=0 full=0 ovf=0 st=0x10
```

### 69.2 pass / fail 的对比（同一次构建）

| 观察点 | pass | fail |
|---|---|---|
| TX 缓冲区 | `w0=00000004` dlc=4 payload=`a3a2a1a0` | **完全相同** |
| 拷贝前 FIFO | `cnt=1 empty=0 full=0 ovf=0 st=0x10` | **完全相同** |
| RX format word | `00002104` dlc=4 rwcnt=4 | `00002908` dlc=8 rwcnt=5 |
| 另一次 fail | — | `00002909` **dlc=9** |

### 69.3 关键判读

**(a) TX 缓冲区逐字节相同** → 本端口格式化没问题，问题不在发送侧

**(b) 拷贝前 FIFO 状态相同** → 判定点之前没有任何可观察的差异

**(c) `dlc=9`** → **classic CAN 不存在 dlc=9**

**第三点是决定性的：读到的不是"8 字节帧"，而是一个语义上不可能的帧头。**

### 69.4 被否定的三个假设

| 假设 | 实验 | 结果（8 次开机） |
|---|---|---|
| FD 模式与 classic 帧矛盾 | `fde=0` | `0,4,0,4,0,0,0,4` ❌ |
| 帧头提交竞态 | 加 50 us | `0,4,0,4,4,0,4,4` ❌ |
| 等待信号用错 | 帧计数替代 RBNEI | 无改善 ❌ |

**三个都失败。列在这里是为了让下一轮不要重复。**

### 69.5 ⚠️ "钳成合法值"掩盖了损坏

```c
/* 旧代码 */
*len = twaifd_dlc2len((rt_uint16_t)hdr.dlc);
if (*len > 8u) { *len = 8u; }        /* dlc=9 -> 报 8 字节，返回 RT_EOK */
```

**这就是 dlc=9 从未被发现的原因。**

改成：

```c
if (hdr.dlc > 8u)
{
    rt_kprintf("twai: illegal DLC %u in received frame (w0=0x%08x)\n", ...);
    return -RT_ERROR;
}
```

**原则：驱动能看出数据非法，就必须报错。
"返回最近的合法值"把损坏变成了"看起来正常"。**

### 69.6 当前状态

```
trial 0: 4/4   trial 1: 0/4   trial 2: 0/4
trial 3: 0/4   trial 4: 4/4   trial 5: 4/4
```

**间歇仍在。** 剩下的方向是控制器环回路径（`stm`+`ilbp`）本身的限制，
**超出移植层范围。**

## 70. IRAM 与 flash 的调用规则（2026-11-01）

### 70.1 规则本身

> **IRAM 里的代码不能调 flash 里的函数。**

原因：IDF 在写 SPI flash 和调 MSPI/PSRAM 时序时会关 flash cache。
那个窗口里执行 flash 地址 = 访问错误。

### 70.2 ⚠️ 这条规则最容易犯的错：只对一处执行

本端口早就吃过亏：

```
MEPC 0x40013b1e resolved to rt_enter_critical in IROM
```

修好 trap handler 后，**规则被当成"已解决"，但驱动 ISR 从来没检查过**。

### 70.3 怎么查：用 `nm` 对地址，不要看样子

```
$ nm s31_rtt_test.elf | grep -E "s31_uart_isr|rtt_trampoline|rt_kprintf|esp_rom_printf"
2f002c66 t s31_uart_isr        <- IRAM
2f002e5a T rtt_trampoline      <- IRAM
400128ba W rt_kprintf          <- flash
2f800024 A esp_rom_printf      <- ROM
```

**地址规则：**

| 地址段 | 含义 |
|---|---|
| `0x4000_xxxx` ~ `0x4040_xxxx` | **IROM / flash** |
| `0x2f00_xxxx` ~ `0x2f08_xxxx` | DRAM（IRAM 代码在这里） |
| `0x2f80_xxxx` | **ROM**（常驻，安全） |

### 70.4 两处命中

| 函数 | 区 | 调用的 | 后果 |
|---|---|---|---|
| `s31_uart_isr` | IRAM | `rt_kprintf` | 溢出报告时可能 fault |
| `rtt_trampoline` | IRAM | `rt_kprintf` | **在 trap 路径上** |

`rtt_trampoline` 的路径：

```
vectors_rtthread.S:232   jal _global_interrupt_handler
interrupt.c:59           -> mcause-16 索引槽位 -> rtt_trampoline
```

**它打印的正是"无处理函数却触发中断"——最可能发生在 flash cache 关闭窗口。**

### 70.5 修法

```c
/* 两处都改成 ROM 里的输出 */
esp_rom_printf("...");
```

### 70.6 自动检查（第 19 项）

扫描所有 `IRAM_ATTR` 函数体，检查是否调用：

```
rt_kprintf, rt_malloc, rt_free, rt_realloc, rt_calloc,
rt_sem_take, rt_mutex_take, rt_thread_mdelay, rt_thread_delay,
rt_tick_get, rt_snprintf, rt_vsnprintf, rt_device_find, rt_device_open,
rt_sem_release, rt_mutex_release
```

**安全替代：**

| 需求 | 用这个 |
|---|---|
| 打印 | `esp_rom_printf` |
| 延时 | `esp_rom_delay_us` |
| 读计数 | 直接读寄存器（`s31_twai_cycles()` 这类自己实现的） |
| 内存 | 不要在 IRAM 里动态分配 |

### 70.7 检查器要反向验证

**故意破坏一次，确认它会红：**

```powershell
# 临时删掉 gpio.c 里的 .pin_debounce
[FAIL] ops tables complete   ['gpio.c:rt_pin_ops.pin_debounce']
17/18 checks passed
```

**没做过反向验证的检查器不知道自己会不会失败。**

## 71. `bind`：上游内建命令在本端口上的已知限制（2026-11-01）

### 71.1 现象

```
msh > bind 0x2f03e930 1        # tshell 的 TCB
bind 0x2f03e930 1
                                <- 到此为止，无回复、无 panic、无复位
```

**之后再发任何命令都没有响应。** 复位后恢复。

### 71.2 三组对照（必须先做这个）

| 命令 | 结果 | 说明 |
|---|---|---|
| `bind <tshell> 0` | ✅ 成功 | 同核，不走 IPI 分支 |
| `bind <tsystem> 1` | ✅ 成功 | **线程是 suspend，不走运行中分支** |
| `bind <tshell> 1` | ❌ 卡死 | 运行中 + 跨核 |

**结论：绑到 CPU1 本身没问题，只有"运行中的线程"不行。**

### 71.3 机制

```c
/* scheduler_mp.c: rt_sched_thread_bind_cpu() */
if (thread_stat == RT_THREAD_RUNNING)
{
    if (RT_SCHED_CTX(thread).oncpu == current_cpu)
    {
        if (cpu != current_cpu)
        {
            rt_hw_ipi_send(RT_SCHEDULE_IPI, 1U << cpu);
            rt_sched_unlock_n_resched(slvl);     /* <- 在 CPU0 重调度 */
        }
```

**线程已绑到 CPU1，不在 CPU0 的就绪队列里。**
**CPU0 重调度会选别的线程；CPU1 停在 `wfi` 永不调度。**

**⇒ 这个线程永远拿不到 CPU。不是卡在内核里，是不再被选中。**

### 71.4 ⚠️ 别急着归因到 IPI

我第一版结论是"IPI 没有接收端"，加了守卫之后**重测三次仍然卡死**。

**"加了守卫还是挂" 就是否决这个归因的证据。**

**现在的注释写的是测量支持的说法**，不是第一版猜想。

### 71.5 操作建议

| 场景 | 做法 |
|---|---|
| 想验证 IPI / 绑核 | 绑**非运行**线程（`tsystem` 这种 suspend 的） |
| 想绑正在跑的线程到 CPU1 | **不要做**，会静默停掉它 |
| 需要 CPU1 干活的真需求 | 等 IPI 接收端 + CPU1 调度（W49/W50/W52 的同一件事） |

### 71.6 移植层的接口面比想象的大

**不只是自己写的那些 `rt_hw_*`。**
上游自带的内建命令（`bind`、`ps`、`list`、`free` …）也在这个端口上运行，
它们假设的平台能力（这里：IPI + 多核调度）如果没有，**命令会静默失效或挂住**。

**审计移植层时要把"内核自带的命令"也算进接口面。**

## 72. 上游内建命令的两个致命陷阱（2026-11-01）

### 72.1 已确认的两个

| 命令 | 后果 | 记录 |
|---|---|---|
| `bind <运行中线程> 1` | 那个线程**永远不再运行** | W74 |
| `console set uart0` | **shell 永久失去输入**（核还在跑） | W75 |

**两个都是 RT-Thread 上游自带的命令，不是本端口写的。**

### 72.2 `console set uart0` 的精确机制

**命令前后实测：**

| 时刻 | `open_flag` | `ref` | `INT_RX` | `con` |
|---|---|---|---|---|
| 前 | `0x014b` | 1 | **yes** | `(none)` |
| 后 | `0x004b` | 2 | **NO** | `uart0` |

**链条：**

```
shell 打开    : RDWR|INT_RX|STREAM = 0x143
console 重开  : RDWR|STREAM        = 0x043
rt_device_open 发现标志不等 -> 再调 rt_serial_open()
rt_serial_open 第一句: dev->open_flag = oflag & 0xff  -> INT_RX 被清
rt_serial_read 按 open_flag & INT_RX 选路径 -> 改走 poll
s31_uart_isr 仍把 FIFO 搬进 ring -> poll 永远读到空 -> shell 永久等待
```

### 72.3 为什么这个端口特别容易中招

**端口故意不调 `rt_console_set_device()`**，让 `_kputs()` 走
`rt_hw_console_output()`（ROM 输出），**这样 panic 时输出还活着**。

**`console set` 恰好把端口推进了它被设计来避免的状态。**

### 72.4 怎么诊断"没有输出"

**关键区分：板子死了 vs shell 死了。**

```
msh > s31con arm                 <- 先用软件定时器布一个探针
watching uart0 once per second

msh > console set uart0
console change to uart0
msh >[watch] flag=0x0113 open=0x004b ref=2 int_rx=NO stream=yes con=uart0
      [watch] ...                <- 定时器还在打，说明核还活着
```

**诊断代码要放在"故障之后还能跑"的地方**（定时器 / 另一个线程），
不能只放在 shell 命令里 —— 那样只能看到故障前的状态。

### 72.5 处理原则

| 情况 | 做法 |
|---|---|
| 上游命令 + 上游驱动交互出错 | **记录为已知限制**，不改内核 |
| 端口自己写错的 | 修 |
| 需要规避 | 在文档里写清"不要用这条命令"，而不是偷偷改行为 |

**理由：把上游行为改成"本地特殊逻辑"，会让端口偏离
"未修改的上游内核"这个核心资产。**

### 72.6 命令安全清单

| 命令 | 状态 |
|---|---|
| `help` `version` `list` `ps` `free` | ✅ 安全 |
| `s31*` | ✅ 安全（本端口自有） |
| `s31con` | ✅ 安全（诊断用） |
| `bind <运行中线程> <别的核>` | ❌ **停掉那个线程** |
| `console set uart0` | ❌ **shell 输入永久失效** |

## 73. `pin` 命令与 GPIO 读回（2026-11-01）

### 73.1 症状

```
msh > pin mode 10 output
msh > pin write 10 high
msh > pin read 10
pin[10] = low        <- 写了高读回低
```

### 73.2 一步区分两种假设

**把脚改成输入再读**：

```
pin mode 10 input_pulldown
pin read 10          -> 仍然 low
```

| 若读回 high | 则是"输出模式下输入缓冲被关" |
|---|---|
| **实际仍 low** | **两种情况都可能，需要看源码** |

**（本例实际是前者：输出模式关了输入缓冲。）**

### 73.3 根因

```c
case PIN_MODE_OUTPUT:
    gpio_ll_output_enable(&GPIO, pin);      /* 只开输出 */
```

而 `rt_pin_read()` → `gpio_ll_get_level()` → 读**输入**路径。

**ESP32 的 `IO_MUX.gpio[n].fun_ie` 默认 0 ⇒ 输出脚读回恒 0。**

### 73.4 为什么这算缺陷（而不只是"和上游不同"）

**本端口内部就不一致：**

| 模式 | 输入缓冲 | 能读回 |
|---|---|---|
| `PIN_MODE_OUTPUT_OD` | 开 | ✅ |
| `PIN_MODE_OUTPUT` | 关 | ❌ |

**两个都是"输出"。**

**判据是 API 文档：`rt_pin_read()` 说 "get the level of the pin"，
没有说 "of an input pin"。**

### 73.5 修复后

```
pin write 10 high -> pin read 10 -> high    ✅
pin write 10 low  -> pin read 10 -> low     ✅
```

### 73.6 ⚠️ 判断"上游也这样"要看 IDF 的模型

上游 `bsp/ESP/ESP32_C6` 的 `drv_gpio.c` 同样只 `GPIO_MODE_OUTPUT`。

**但 IDF 的枚举明确区分：**

```c
GPIO_MODE_OUTPUT         /* output only */
GPIO_MODE_INPUT_OUTPUT   /* output and input */
```

**RT-Thread 的 `PIN_MODE_OUTPUT` 应该映射到后者**，
因为 RT-Thread 的 API 假设读回可用。

### 73.7 顺带：用法提示后的 "command failed -1."

```
msh > s31led
usage: ...
s31led: command failed -1.
```

**源码：** `msh.c` 里 `if (cmd_ret < 0) rt_kprintf("%s: command failed %d.\n", ...)`。

**本端口 8 个命令用法分支 `return -1`（= `-RT_ERROR`）。**

| 上游命令 | 用法后返回 |
|---|---|
| `cmd_bind` | 0 |
| `cmd_list` | 0 |
| `console` | `-RT_ERROR`（也会打印） |

**上游自己不一致。这是可读性噪音，未修。**

## 74. `pwm` / `adc` 内建命令的两个问题（2026-11-01）

### 74.1 `pwm probe` 永远失败

```
msh > pwm probe pwm0
Please using 'pwm probe <device name>' first.
```

**而 `pwm0` 在 `list device` 里。**

**根因：`FINSH_USING_OPTION_COMPLETION` 未开。**

```c
/* finsh.h */
#else
#define MSH_OPT_ID_GET(fun) ((int)(-1UL))     /* 常量 -1 */
#endif
```

`PWM_LIST_PROBE = 1` ⇒ **分支永不匹配**。

**`cmd.c` 有 `#ifndef` 回退，`rt_drv_pwm.c` 没有 ⇒ `list` 能用，`pwm` 不能用。**

**修：`#define FINSH_USING_OPTION_COMPLETION`**（Kconfig 默认 y，431/466 BSP 启用）。

### 74.2 ⚠️ 启用后暴露 3 处宏参数错误

```
error: 'cmd_mcpwm_msh_options' undeclared
error: 'cmd_rmt_msh_options' undeclared
error: 'cmd_intr_msh_options' undeclared
```

**原因：`MSH_CMD_EXPORT_ALIAS(cmd, alias, desc)` 是变参宏，
描述字符串里的逗号会被计入参数个数。**

```
MSH_CMD_EXPORT_ALIAS(a, b, "x, y")     -> 4 个参数 -> 走 _OPT 版本
                                                            -> 引用 a_msh_options（不存在）
```

**修法：描述里不要放逗号。**

**这类错误在选项关闭时**完全看不到**（宏展开为空）。**

### 74.3 `adc voltage` 打印 `-32.-768V`

**根因：`get_vref()` 返回 `-1`，而框架的约定值是 `0`。**

```c
vref = dev->ops->get_vref(dev);
if (vref == 0) goto _voltage_exit;      /* 0 = "算不出" */
voltage = value * vref / ((1 << resolution) - 1);
```

**`-1` 通过检查被乘进结果** ⇒ 负数 ⇒ `printf("%d.%03dV")` 打出 `-32.-768V`。

**上游惯例：`return 0` 出现 5 次，`return -1` 出现 0 次。**

**修：改成 `return 0` ⇒ `0.000V`。**

### 74.4 检查器：不要写死结构体大小

```python
# 错：配置一变就错，而且仍然 PASS
f"{fsym_size} B = {fsym_size // 12} commands"

# 对：从 DWARF 读
syscall_size = struct_size(elf, "finsh_syscall")
```

**`struct finsh_syscall` 在开 `FINSH_USING_OPTION_COMPLETION` 后 12 B → 16 B。**

**528/12 = 44（错） vs 528/16 = 33（对，help 实际 33 条）。**

### 74.5 结论

| 问题 | 类型 | 处理 |
|---|---|---|
| `pwm` 命令失效 | **配置缺失** | 启用选项 |
| 3 处宏参数错误 | **潜伏的语法问题** | 描述去掉逗号 |
| `adc voltage` 乱码 | **返回值违反约定** | `-1` → `0` |
| 命令数 44 vs 33 | **检查器写死常量** | 改读 DWARF |
| `pwm get` 打印 `2147483647%` | 上游 `0/0` | 记录不修 |

## 75. 一个不会失败的检查，比没有检查更糟（2026-11-01）

### 75.1 案例

W77 发现未定义 `FINSH_USING_OPTION_COMPLETION` 会让 `pwm` 命令失效而**编译正常**。
想把这类问题做成自动检查，结果**做了又删掉**。

### 75.2 三次尝试

**(1) 统计"被引用但未定义"的宏** → 报 11 个，**全误报**

`#ifdef RT_USING_HOOK` 这一行**本身**被算成了"引用"。

**判定行是决定别人的，不是被决定的。**

**(2) 排除 `#if/#ifdef/#endif`** → 剩 2 个，仍误报

| 命中 | 漏排的指令 |
|---|---|
| `#define RT_USING_IDLE_HOOK` | `#define` |
| `#else /* ! RT_USING_SIGNALS */` | **`#else`** |

**(3) 补齐后** → 真命中 **0**。

### 75.3 写成检查，反向验证，删掉

```
[PASS] switch macros defined    all 48 defined
# 破坏配置后：
[PASS] switch macros defined    all 47 defined     <- 没红
```

**原因：条件在 `finsh.h` 里，没有 `.c` 在代码行里读它。
RT-Thread 把这类开关一律写成预处理指令。**

**⇒ 检查要抓的东西不在它扫描的范围内。**

### 75.4 原则

| 原则 | 说明 |
|---|---|
| **每个检查都要反向验证一次** | 破坏输入，确认它会红 |
| **不会红的检查立即删除** | 它看起来像覆盖，实际是装饰 |
| **先确认问题在哪一层** | 条件在头文件，就别只扫 `.c` |
| **"无发现"要用两种独立方法确认** | 单次无发现可能是扫描错了 |

### 75.5 反向验证的模板

```python
# 1. 备份
orig = CFG.read_text()
# 2. 破坏输入
CFG.write_text(orig.replace('#define X', '#undef X'))
# 3. 跑检查器
r = subprocess.run([sys.executable, CHECKER, '--strict'], capture_output=True, text=True)
# 4. 恢复并确认
CFG.write_text(orig)
assert CFG.read_text() == orig
# 5. 看它是否真的红了
```

**本项目的 `check_rtt_style.py` 里，第 18 项（ops tables）
做过这个验证并成功变红；第 20 项（switch macros）做完发现不会红，已删除。**

## 76. 反向验证检查器：19 项里 2 项是装饰（2026-11-01）

### 76.1 方法

**对每一项：改坏它声称要抓的东西，跑一遍，看它会不会红。**

```python
orig = path.read_text()
path.write_text(orig.replace(old, new, 1))
out = subprocess.run([sys.executable, CHECKER, '--strict'], capture_output=True, text=True)
path.write_text(orig)
assert path.read_text() == orig          # 恢复必须逐字节一致
red = any(l.startswith('[FAIL]') and name in l for l in out.stdout.splitlines())
```

### 76.2 发现的两种装饰形态

**(a) 下界断言**

```python
check("INIT_*_EXPORT present", len(exports) >= 10, ...)   # 实际 11 个
```

**删一个仍然 ≥ 10。** 下界只能发现"少了太多"。

**改成逐函数配对**：每个 `rt_hw_*_init` 必须被导出或被 board.c 调用。

**(b) `len(x) > 0`**

```python
check("msh commands anchored", len(cmd_files) > 0, ...)   # 恒真
```

**改成逐文件检查它靠什么活下来**（锚点 / `-u` / 都没有）。

### 76.3 ⚠️ 两个陷阱

**陷阱 1：`substring in text` 会匹配声明**

```python
# 错：extern int rt_hw_rmt_init(void); 也含 "rt_hw_rmt_init("
if f"{name}(" in board_src:

# 对：只认缩进的语句行，排除 extern
for line in board_src.splitlines():
    s = line.strip()
    if s.startswith("extern "):
        continue
    m = re.match(r"(\w+)\s*\(", s)
    if m:
        called.add(m.group(1))
```

**陷阱 2：变异要选检查真正声称的方向**

```
config pairs complete 声称的:  主开关在 + 伙伴缺失
我第一次的变异:               删掉主开关  -> 绿（正确！）
正确方向的变异:               删掉伙伴    -> 红
```

**"检查没红"有两种原因：检查坏了，或变异选错了。**

### 76.4 现状

**源码级 14 项全部可红**（见日志 §90.5）。
**ELF 级 5 项在 `check_image.py` 里，尚未逐项反向验证。**

### 76.5 原则

| 原则 | 说明 |
|---|---|
| **每个检查至少反向验证一次** | 否则不知道它会不会红 |
| **`>= N` 和 `> 0` 是弱断言** | 优先写成"逐个对象满足性质" |
| **字符串匹配要小心声明** | `extern` / 注释 / `#define` 都可能匹配 |
| **不变红时先怀疑变异选错** | 再怀疑检查坏了 |
| **装饰性检查的价值是负的** | 它给了一个假的覆盖数字 |

## 77. ELF 级检查的反向验证（2026-11-01）

### 77.1 最典型的错误：全镜像 grep 当成定位

```python
# 错：检查"trap 序言减 128 两次"
dis = objdump("-d", elf)                                 # 整个镜像
trap_prologue = re.findall(r"addi sp,sp,-(\d+)", dis)    # 6780 个元素
check(..., "128" in trap_prologue, ...)                  # 53 处满足
```

**它从来没看过 trap handler。** 任何人写出完全不同的序言，它仍然会绿。

```python
# 对：按符号定位，只反汇编那几条指令
trap_entry = syms["_interrupt_handler"]
raw = objdump("-d", f"--start-address=0x{trap_entry:x}",
              f"--stop-address=0x{trap_entry+12:x}", elf)
prologue = re.findall(r"addi\s+sp,sp,-(\d+)", raw)
check(..., prologue[:2] == ["128", "128"], ...)
```

**输出里要写出它看了哪个地址**，这样"看错地方"一眼可见：

```
[PASS] trap prologue subtracts 128+128
       trap entry at 0x2f000100 subtracts ['128', '128']
```

### 77.2 下界断言的余量问题

```python
hits = 29
check(..., hits >= 25, ...)     # 6 个槽坏掉也不会红
```

**改成精确集合**：

```python
panic = syms["_panic_handler"]
stray = sorted(set(words) - {trap, panic})
check(..., not stray and hits > 0,
      f"{hits} -> trap, {n_panic} -> panic, {len(stray)} stray")
```

```
[PASS] trap entry installed   29 slots -> _interrupt_handler, 19 -> _panic_handler, 0 stray
```

### 77.3 ELF 检查的反向验证手法

**不能改源码（要重编译），所以改检查器本身，让它"看错地方"：**

| 检查 | 造错方式 | 期望 |
|---|---|---|
| trap prologue | 反汇编起点偏移 8 字节 | `subtracts []` → RED |
| new-thread frame | 找 `-1024` 而不是 `-256` | `found 0` → RED |
| trap entry installed | 只允许 `_interrupt_handler` | 19 个 stray → RED |

```python
orig = CHECKER.read_text()
patched = orig.replace(needle, broken_needle, 1)
tmp.write_text(patched)
r = subprocess.run([sys.executable, str(tmp)], ...)
assert '[FAIL]' in r.stdout
tmp.unlink()
```

### 77.4 检查清单（针对 ELF/反汇编断言）

1. **它在哪个地址范围内查？** 全镜像 = 不是定位
2. **用的是 `>= N` 吗？** 换成精确集合或"恰好一次"
3. **输出里说了它看了哪里吗？**
4. **造一个"看错地方"的变体，它会红吗？**

### 77.5 项目现状

| 检查器 | 项数 | 全部已反向验证 |
|---|---|---|
| `check_rtt_style.py` | 19 | ✅（W79） |
| `check_image.py` | 9 | ✅（W80） |
| `s31test` | 33 | ❌ **未做**（需要注入故障） |

## 78. 给行为断言注入故障（2026-11-01）

### 78.1 与前两轮的区别

| 对象 | 反向验证手法 |
|---|---|
| `check_rtt_style.py`（19 项） | 改坏源码文件，跑检查器 |
| `check_image.py`（9 项） | 改坏检查器让它"看错地方" |
| **`s31test`（33 项）** | **注入故障，重编译烧写，看测试会不会报 FAIL** |

### 78.2 三个成功的注入

**(a) `fpu`：删掉 f0..f31 的 32 条 `FSTORE`**

```
[FAIL] fpu   1000 mismatches, first at reg 15 value 0x63ca
```

**⚠️ 第一次注入是错的**：把 `#ifdef ARCH_RISCV_FPU` 整个关掉，
连 `addi sp, sp, -32*FREGBYTES` 一起去掉 → 帧布局坏 →
`Illegal instruction, MEPC 0x2f019518`。

**那是"帧坏了"，不是"FPU 没保存"。注入要精确到目标缺陷。**

**(b) `rhwh.ud`：让 `rt_hw_us_delay` 立刻返回**

```
[FAIL] rhwh.ud  1000 us request took 0 us (want 900..1600)
[FAIL] rhwh     5 sub-checks, 1 bad
```

**(c) `tick`：`rt_tick_increase()` 每次中断调两次**

```
[FAIL] tick   4000 ticks in 1999999 us = 499 us/tick (expect 1000, 1000 Hz)
```

**也证明了它的 ±5% 窗口抓得到 16 倍错误（SYSTIMER 单位算错）。**

### 78.3 一个失败的注入（记成部分）

**做法**：破坏协作切换里保存的 s1。

**结果**：不是测试失败，而是**命令都没执行**：

```
s31test: command not found.
Guru Meditation Error: Core 0 panic'ed (Load access fault)
MEPC 0x4001b8ba   S1: 0x5a5a5a5a
```

| 观察 | 含义 |
|---|---|
| `S1: 0x5a5a5a5a` | **s1 确实从保存槽恢复** → 恢复路径是真的 |
| 命令没执行 | **破坏的是 shell 自己的切换**，测试没机会跑 |

**⇒ 证明了"寄存器真的从槽恢复"，没证明 `ctx` 会报错。**

**这类结果必须记成"部分"，不能算成功。**

### 78.4 注入的纪律

```
1. 断言注入点唯一存在（count == 1）
2. 注入，并在代码里留 "provoked" 标记
3. 编译 + 烧写 + 观察
4. 恢复原文
5. 编译 + 烧写 + 确认全套通过
6. 确认 "provoked" 标记数为 0
```

**没有第 4-6 步的注入是危险的：忘了恢复就以为自己在测别的东西。**

### 78.5 当前覆盖

| 测试 | 注入结果 |
|---|---|
| `fpu` | ✅ FAIL |
| `rhwh.ud` | ✅ FAIL |
| `tick` | ✅ FAIL |
| `ctx` | ⚠️ 部分 |
| 其余 29 项 | 未做 |

**"33 项通过"和"33 项被验证过会失败"是两件不同的事。**

## 79. 注入点的选择：只挑"只有被测对象在用"的函数（2026-11-01）

### 79.1 两次栽在同一个坑

| 轮次 | 注入 | 后果 |
|---|---|---|
| W81 | 破坏协作切换里保存的 s1 | **shell 自己崩了**，测试没机会跑 |
| W82 | `rt_hw_interrupt_umask()` 不置位 | **shell 的 UART RX 被关**，命令不响应 |

**两次都是同一个错误：注入面覆盖了 shell 赖以运行的东西。**

### 79.2 为什么

`rt_hw_interrupt_umask()` 不只引脚中断用：

```
finsh 打开 uart0 时用 RT_DEVICE_FLAG_INT_RX
  -> dev_serial.c 的 control(RT_DEVICE_CTRL_SET_INT)
  -> serial.c 的 s31_uart_control
  -> rt_hw_interrupt_umask(ETS_UART0_INUM)     <- 注入命中
```

**`rt_hw_context_switch` 同理：shell 自己每次读写都要切换。**

### 79.3 成功的那次为什么成

```c
/* 只动 s31_gpio_isr 里两个 while 循环 */
if (0) while (lo) { ... }
if (0) while (hi) { ... }
```

**这个函数只有 GPIO 引脚中断在用。**

```
[FAIL] irq   pin 10: 0/200 interrupts, nest=0 (0 bad)
```

### 79.4 判据

**注入前先问三个问题：**

| 问题 | 若答案是"是" |
|---|---|
| 这个函数还有别人在用吗？ | **换注入点** |
| shell / tick / 调度器路径会经过它吗？ | **换注入点** |
| 破坏它会让板子无法响应吗？ | **换注入点** |

**理想的注入点：只有被测对象在用，且破坏后系统仍能跑完并打印结果。**

### 79.5 三个失败输出都很精确定位

```
[FAIL] irq      pin 10: 0/200 interrupts, nest=0 (0 bad)
[FAIL] nest     outer 100@nest1, inner 100@nest1 (want 100, 100 bad)
[FAIL] rhwh.mk  mask/umask: 1 -> 0 (want 1) -> 1 (want 0)
```

**`nest` 那行尤其好：外层 100 次仍然 PASS，只有内层记错
⇒ 一眼看出"不是没中断，是没嵌套"。**

**注入成功的一半在于"失败信息能定位到子项"。**

### 79.6 当前覆盖率

| 测试 | 已验证会失败 |
|---|---|
| `fpu` `rhwh.ud` `tick` `rhwh.mk` `irq` `nest` | ✅ 6 项 |
| `ctx` | ⚠️ 部分 |
| 其余 26 项 | 未做 |

## 80. 时间测量的间歇失败：用 best-of-N 而不是放宽容差（2026-11-01）

### 80.1 现象

`rhwh.ud` 测 `rt_hw_us_delay(1000)`，窗口 900..1600 us。

**约四次里失败一次，读数是 1998 us —— 恰好 2 倍。**

### 80.2 原因

`rt_hw_us_delay` 在 SYSTIMER 上忙等。**1 kHz 的 tick 在 1000 us 里必然触发一次**，
ISR 期间忙等停住，单次测量就多出最多一个完整周期。

```
1000 us（请求） + ~998 us（一次 tick） = 1998 us
```

### 80.3 ⚠️ 错误修法：放宽窗口

把上限改成 3000 —— **稳定了，但**：

| 错误 | 实测 | 3000 窗口 |
|---|---|---|
| weak stub | ~0 | ✅ 抓到 |
| **2 倍单位错** | **~2000** | ❌ **看不见** |
| 16 倍单位错 | ~16000 | ✅ 抓到 |

**"放宽到能容纳噪声"等于"对同量级的真实错误失明"。**

### 80.4 正确修法：取 N 次的最小值

**关键洞察：抢占只能让测量偏大，不可能偏小。**

```c
rt_uint32_t best = 0xFFFFFFFFu;
for (try = 0; try < 5; try++)
{
    t0 = s31_systimer_us();
    rt_hw_us_delay(1000);
    t1 = s31_systimer_us();
    elapsed = (rt_uint32_t)(t1 - t0);
    if (elapsed < best) best = elapsed;
}
elapsed = best;
```

**窗口保持 900..1600。**

```
修前: 8 次里 1 次 1998 us FAIL
修后: 8 次全部 1000 us PASS
```

### 80.5 收紧后必须重新验证

**改完之后要再注入一次，确认还抓得到目标：**

```
[FAIL] rhwh.ud  1000 us request took 0 us best of 5 (want 900..1600)
```

**✅ 没有削弱检测能力。**

### 80.6 通用判据

| 情况 | 做法 |
|---|---|
| 噪声**单向**（只会让值变大） | **取最小值** |
| 噪声**双向** | 取中位数，或明确写出两侧余量 |
| 噪声与目标错误**同量级** | **不能靠放宽容差**，必须换测量方法 |
| 无法区分噪声与错误 | 改变测量条件（关中断、提高采样率） |

### 80.7 记录要求

**"偶尔失败"是没用的描述。** 要写：

```
8 次里 1 次失败，读数 1998 us（恰好 2 倍）
```

**频率 + 具体数值才能暴露规律。**

## 81. "接受但降级"是上游最常见的静默陷阱（2026-11-01）

### 81.1 两个实例

| 命令/标志 | 表面 | 实际 |
|---|---|---|
| `console set uart0` | 接受，打印"console change to uart0" | shell 的 RX 永久失效 |
| `RT_TIMER_FLAG_SOFT_TIMER` | 接受，`rt_timer_create` 返回句柄 | 回调在 tick 中断上下文执行 |

**共同点：不报错、不返回失败、行为却是错的。**

### 81.2 为什么危险

**"拒绝"会让人立刻知道走错路；"接受但降级"要到很晚才暴露，
而且暴露时症状离原因很远。**

### 81.3 端口能做什么

**不能改内核（内核零修改是资产）。能做的是：断言自己不用这些特性。**

```python
# 检查 21
if "#define RT_USING_TIMER_SOFT" not in rtcfg:
    soft_uses = [扫描 port 和 app 里的 RT_TIMER_FLAG_SOFT_TIMER]
    check("no soft timers without the feature", not soft_uses, ...)
```

```
[PASS] RT_USING_TIMER_SOFT off, and no port code requests one
```

### 81.4 ⚠️ 写这类检查前先问三个问题

**这是 W78 删掉检查 20 之后总结的：**

| 问题 | 若答"否" |
|---|---|
| 条件在 `.c` 的代码里吗？ | **检查看不到，不要写** |
| 能构造一个真阳性吗？ | 不能，就不要写 |
| 破坏后它会红吗？ | 不会，**立即删除** |

**检查 20** 的条件在 `finsh.h` 的 `#ifdef` 里 ⇒ 扫 `.c` 永远看不到 ⇒ **删掉**。
**检查 21** 的条件是 `rt_timer_create` 的**参数** ⇒ 能扫到 ⇒ **保留**。

**判别口诀：条件在头文件就不必写这条检查；条件在调用点才值得写。**

### 81.5 反向验证模板

```python
orig = path.read_text()
path.write_text(orig.replace(old, broken, 1))
out = run_checker()
path.write_text(orig)
assert path.read_text() == orig
assert '[FAIL]' in out, "check is decoration"
```

**W79/W80/W84 都用这个模板。W78 用它发现了装饰性检查。**

### 81.6 当前检查器覆盖

| 检查器 | 项数 | 全部反向验证 |
|---|---|---|
| `check_rtt_style.py` | 20 | ✅ |
| `check_image.py` | 9 | ✅ |
| `s31test` | 33 | 7 项 + 1 项部分 |

## 82. `rt_hw_interrupt_install()` 的返回值不可测（2026-11-01）

### 82.1 问题

```c
rt_isr_handler_t rt_hw_interrupt_install(int vector, ...)
{
    if (vector < 0 || vector >= RTT_INUM_NUM)
    {
        rt_kprintf("intr: bad vector %d ...\n", ...);
        return RT_NULL;                 /* 失败 */
    }
    old = s_ctx[vector].fn;
    ...
    return old;                         /* 没装过 handler 时也是 RT_NULL */
}
```

**成功（无前任）和失败（vector 被拒）都返回 `RT_NULL`。**

⇒ `if (install(...) != RT_NULL)` **永远抓不到失败**。

### 82.2 正确写法

```c
rt_hw_interrupt_install(INUM, isr, RT_NULL, "name");

/* 问槽位，不问返回值 */
if (intr_handler_get(INUM) == RT_NULL)
{
    rt_kprintf("INUM %d did not take the handler\n", INUM);
    return -RT_ERROR;
}
```

**`intr_handler_get()` 是只读查询** —— 本端口把自己拥有的槽位都设成
`rtt_trampoline`，所以非 NULL 就是"装上了"。

### 82.3 静默失败的后果

| 站点 | 症状 |
|---|---|
| `uart0` | 初始化成功、注册成功，**一个字节都收不到** |
| `pin` 设备 | 注册成功，**所有 `rt_pin_attach_irq` 都是死的** |
| `nest` 自检 | **测不到任何东西**却可能报 PASS |

**都比崩溃难查。**

### 82.4 自动检查（第 22 项）

规则：**`rt_hw_interrupt_install()` 之后 10 行内必须有 `intr_handler_get()`。**

```python
if not (s.startswith("rt_hw_interrupt_install(") or ...):
    continue
window = "\n".join(lines[idx:idx + 10])
if "intr_handler_get(" not in window:
    bad_install.append(f"{f.name}:{idx+1}")
```

### 82.5 ⚠️ 写这类检查时的两个坑

**(a) 行号必须来自原始文本**

```python
# 错：剥掉块注释再分行，行号全部偏移
plain = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
for lineno, line in enumerate(plain.splitlines(), 1):

# 对：保留原行，走的时候跳过注释
lines = src.splitlines()
in_comment = False
for idx, line in enumerate(lines):
    ...
```

**(b) 检查必须接受修复后的正确形态**

修完三处之后检查仍然红，因为它只认"结果被赋值"。
**要接受"装完查槽位"这个标准形态。**

### 82.6 扫描口径

本项目用三种口径扫"未检查的返回值"：

| 口径 | 含义 |
|---|---|
| 结果被赋值 | 后续可能被检查 |
| 显式 `(void)` | 故意忽略，可接受但要列出 |
| **语句级丢弃** | **可能漏检** |

13 处丢弃里 3 处是实质缺陷。**这个比例说明口径有效。**

## 83. 死状态：写了但没人读的变量（2026-11-01）

### 83.1 实例

```c
static volatile rt_uint32_t s_sem_sent;   /* :848 声明 */
        s_sem_sent++;                     /* :858 每次释放 */
    s_sem_sent = 0x80000000u | n;         /* :864 生产者退出 */
```

**没有任何地方读它。**

### 83.2 为什么危险

`0x80000000 | n` 这个编码**明显是"生产者已完成"的完成标志**
（高位置位 + 计数）。测试后来改成等 token 计数（更强的检查），
**标志就变成了孤儿 —— 但代码里看不出来它已经没用。**

**下一个读者会以为"有人在检查这个"。**

### 83.3 检测规则（第 23 项）

```
收集某个 static 变量的所有出现行
↓
排除声明行
↓
剩下的行里有"非赋值形态"吗？
  name =, name++, name +=, name |=    <- 赋值
  其它任何形态                          <- 读
↓
全是赋值 ⇒ 只写不读 ⇒ 报告
```

### 83.4 ⚠️ 三个必须避开的误报

| 情形 | 例子 | 真相 |
|---|---|---|
| ops 表 | `s31_pin_ops` | 取地址注册 = 读 |
| 结构体实例 | `s_pwm` | `&s_pwm.parent` = 读 |
| 同文件内读的标志 | `s31_ipi_receive_side_ready` | `if (!...)` = 读 |

**还有一条更隐蔽的：必须先剥注释。**
`s_sem_sent` 的说明注释里提到了它自己 —— **不剥注释，那一行会被当成"读"。**

### 83.5 反向验证要验证两件事

**(a) 会红：**

```
[FAIL] no write-only statics    written but never read: ['selftest.c:s_dead_marker']
```

**(b) 不误报合法情形：**

```
[PASS] no write-only statics    every file-scope variable is read somewhere
```

**只验证 (a) 不够 —— 一个频繁误报的检查会被关掉，等于没有。**

### 83.6 顺手修的另一半

**同一处还丢弃了 `rt_sem_release()` 的返回值**（可能返回 `-RT_EFULL`）。

```c
if (rt_sem_release(&s_sem) != RT_EOK) s_sem_release_failed++;
```

**报文体现：`500/500 tokens, 0 release failures, extra take timed out`**

**注入验证：`max_value = 0` ⇒ `0/500 tokens, 500 release failures`**
**两个数字并排摆出来，一眼看出是生产端还是消费端的问题。**

## 84. 报文体里的数字也会说谎（2026-11-01）

### 84.1 实例

```c
rt_snprintf(detail, sizeof(detail), "%d sub-checks, %d bad", 5, detail_bad);
```

**5 是写死的。** 但 `rhwh.iv` 扫不到空闲中断线时会 `return`，后面 4 项不跑。

**跳过时仍然打印 "5 sub-checks" —— 分母是假的。**

### 84.2 修法：报"实际跑了几项"

```c
int ran = 0;
...
ran++;
st_report("rhwh.ud", ...);
...
rt_snprintf(detail, sizeof(detail), "%d/%d sub-checks ran, %d bad", ran, 5, detail_bad);
```

```
[PASS] rhwh   5/5 sub-checks ran, 0 bad      <- 全部跑了
[PASS] rhwh   1/5 sub-checks ran, 0 bad      <- 跳过了 4 项，一眼可见
```

**跳过从"看不见"变成"写在输出里"。**

### 84.3 改完立刻暴露两个真问题

**(a) `dev.rc` 正常路径从来不报告**

它只在 `pwm0` 缺失或被占用时说话。**所以 7 个标签里只有 6 个会报**，
分母 7 从结构上就达不到。

**(b) 某个分支漏了 `ran++`**

`dev.ct` 的两个 `else if` 分支，一个加了计数器，一个没加。

**⇒ 这两个都是"计数器"自己发现的。**

### 84.4 ⚠️ 自检总数会变

修完 `dev.rc` 后：

```
=== 33 passed ===  ->  === 34 passed ===
```

**不是新增功能，是子检查变细（同一组多报一行）。**
**要确认这是预期的，不要以为"测试数变了就是出问题"。**

### 84.5 审核报文的清单

| 检查项 | 例子 |
|---|---|
| 数字是算出来的还是写死的？ | `5` vs `ran` |
| 跳过路径会打印什么？ | "skipped" 还是假装跑了 |
| 标签数与断言数一致吗？ | 7 个标签，6 个会报 |
| 每个分支都计数了吗？ | `dev.ct` 的 else 少了 |

**报文体和断言一样需要审计。**

## 85. 不在套件里的测试 = 没人跑的测试（2026-11-01）

### 85.1 发现方式

**比对"定义了哪些测试"和"`st_all` 调用了哪些"：**

```python
defined = re.findall(r"^static int (st_\w+)\(void\)", t, re.M)
called  = re.findall(r"\b(st_\w+)\(", st_all_body)
print(sorted(defined - called))
```

```
['st_clock']
```

### 85.2 为什么危险

```c
int ok = (d_us >= 90000u && d_us <= 300000u);
st_report("clock", ok, ...);
return ok ? ST_PASS : ST_FAIL;
```

**它有判据、有 report、有返回值 —— 是个正经测试。**

**但 `st_all` 里没有它 ⇒ 只能手打 `s31test clock` 才跑。**

**例行跑 `s31test` 报 "34 passed" 时，这一整块在套件之外。**

### 85.3 常见的"漏进套件"原因

| 原因 | 例子 |
|---|---|
| 原本是诊断命令，后来加了判据 | `st_clock` |
| 加了新测试但忘了改 `st_all` | —— |
| 条件编译的测试在另一套配置里 | —— |
| 手滑注释掉了 | —— |

**前两个都能靠"差集检查"发现。**

### 85.4 顺手修报文的两处

**(a) 错误归因**

```
报文: os_tick says 100000 us
实际: 值来自 s31_systimer_us()，os_tick 原始计数在上一行单独打印
```

改成 `systimer_us says ... over a 100 ms delay`。

**(b) 报文比断言严格**

```
报文: want 100000
断言: 90000..300000
```

**读者会以为容差是精确值。** 把窗口写进报文：

```
(window 90000..300000)
```

### 85.5 加入套件后

```
=== 34 passed ===  ->  === 35 passed ===
```

**与"子检查变细"不同，这是真的多跑了一个测试。**

**看数字变化时要知道是哪一种。**

### 85.6 三路时钟参考互相印证

```
os_tick  +1599999 counts = +99999 us @16MHz
esptimer +0 counts                       <- unit 0 不计数
ccount   +31731276 cycles @320MHz = 99160 us
[PASS] clock   systimer_us says 100000 us (window 90000..300000)
```

**99999 / 100000 / 99160——三个独立域，偏差 < 1%。**

**`ccount` 偏低 0.84% 是正常的：它是 CPU 周期域，和 `mdelay` 的调度粒度不完全对齐。**

## 86. "输出声称的比代码做的少"——同一类缺陷的三次出现（2026-11-01）

### 86.1 三个实例

| 轮次 | 输出 | 实际 |
|---|---|---|
| W87 | `5 sub-checks` | 跳过时只跑 1 项 |
| W88 | `34 passed` | `st_clock` 不在套件里 |
| W89 | 用法列 9 个子命令 | 分派 12 个 |

**共同点：读输出的人无法知道代码其实做了更多（或更少）。**

### 86.2 W89 的具体情况

```
usage: s31test [all|tick|clock|ctx|preempt|fpu|irq|nest|sem] [args]
```

**实际分派 12 个，缺 `rhwh`、`dev`、`fbshim`。**

**`rhwh` 和 `dev` 各自有说明行**（只是首行括号没列）；
**`fbshim` 连说明行都没有** —— 而它是**套件里最大的组，11 项子检查**。

**⇒ 用户永远不知道 `s31test fbshim` 存在。**

### 86.3 检查 24：可证伪

**两边都在同一个 `.c` 文件里：**

```python
dispatched = set(re.findall(r'rt_strcmp\(argv\[1\], "(\w+)"\)', src))
usage_line = 首行方括号里的名字
described  = re.findall(r's31test (\w+)\s', src)
```

```
[PASS] s31test usage is complete    all 11 subcommands documented
```

**双向反向验证：**

```
# 删 fbshim 说明行
[FAIL] no description line: ['fbshim']

# 首行括号里删 dev
[FAIL] not in the usage line: ['dev']
```

### 86.4 ⚠️ 为什么这次能做而检查 20 不能

| | 检查 20（已删） | 检查 24（保留） |
|---|---|---|
| 比对双方 | 头文件 `#ifdef` ↔ `.c` 代码 | **同一文件 ↔ 同一文件** |
| 能构造真阳性 | ❌ | ✅ 两个方向都行 |

**判据：比对的两边都必须在扫描范围内。**

### 86.5 审核"输出与代码一致性"的清单

| 检查 | 方法 |
|---|---|
| 报文里的数字是算的还是写死的？ | 找 `"%d ...", 5` 这种 |
| 分母与实际断言数一致吗？ | 数标签，数分支 |
| 存在的测试都在套件里吗？ | `defined - called` |
| 用法说明覆盖所有分派吗？ | `dispatched - usage_line` |
| 报文比断言严格还是宽松？ | 看 `want X` vs 实际窗口 |

### 86.6 顺带查清一个旧疑点

```c
bus->parent.ops = &s31_i2c_ops;    /* i2c.c:393，RT_USING_DEVICE_OPS 未定义 */
```

**看起来像访问不存在的字段，其实：**

```
struct s31_i2c           { struct rt_i2c_bus_device parent; ... }
struct rt_i2c_bus_device { struct rt_device parent; const struct rt_i2c_bus_device_ops *ops; }
```

**`bus->parent` 是 `rt_i2c_bus_device`，`.ops` 是它自己的 `rt_i2c_bus_device_ops` 字段，
与 `RT_USING_DEVICE_OPS` 无关。不是缺陷。**

## 87. `%f` 在这个镜像里不打印数字（2026-11-01）

### 87.1 事实

本端口用的是 `rt_vsnprintf_tiny.c`：

```c
case 'e': case 'E': case 'G': case 'g': case 'f': case 'F':
    va_arg(args, double);        /* 只消费参数，不输出 */
default:
    ... 打印转换符本身 ...
```

**⇒ `rt_kprintf("%f", x)` 输出空。**

### 87.2 最危险的位置：失败信息

```c
/* 修前 */
rt_kprintf("FPU FAIL at f[%d]: %f != %f\n", i, f[i], seed + (float)i);
```

**真实失败时打印：**

```
FPU FAIL at f[3]:  != 
```

**这条消息存在的唯一目的就是显示两个值，而两个值都不见了。**

**罕见失败路径平时看不到，出错时也没信息 —— 是最糟的组合。**

### 87.3 修法：位模式

```c
union { float f; rt_uint32_t u; } got, want;
got.f = f[i];
want.f = seed + (float)i;
rt_kprintf("FPU FAIL at f[%d]: got 0x%08x, want 0x%08x\n",
           i, (unsigned)got.u, (unsigned)want.u);
```

**位模式是精确的、整数格式化器能打印、而且"位不同"正是测试检测的东西。**

### 87.4 审查清单：哪里会用到 `%f`？

```powershell
Select-String -Path ...\*.c -Pattern '%[-+ #0-9.*]*[fFeEgG]' |
  Where-Object { $_.Line -match 'printf|kprintf' }
```

**当前只有一处（已修）。**
**这个条目应该定期复扫 —— 新加任何浮点打印都会静默失效。**

### 87.5 顺带：命令描述必须覆盖实际输出

```
修前: show tick and thread counters
实际: tick、两个堆、演示线程计数、CPU1 状态
修后: tick + both heaps + demo-thread counters + CPU1 state
```

**漏的正是两个堆和 CPU1 —— bring-up 最需要看的两项。**

### 87.6 检查 25：描述里不得有逗号

**这条规则在 W77 记录、W90 又被我自己违反。**

```c
MSH_CMD_EXPORT_ALIAS(cmd_stats, s31stat, tick, both heaps, ...)   /* 编译错误 */
```

**`MSH_CMD_EXPORT_ALIAS` 是变参宏。改成 `+` 连接。**

```
[PASS] no comma in cmd descriptions    every command description is comma-free
```

**"写进笔记"不够，必须写进检查。**

### 87.7 ⚠️ 写这个检查时我数错了

第一次把期望值设成"参数个数"（2 / 3），报出 **24 个假阳性**。

**正确的是"逗号个数"：2 参数 = 1 逗号，3 参数 = 2 逗号。**

**检查器的第一版几乎总是错的 —— 必须先跑一遍再相信它。**

## 88. 文档里的数字会过时（2026-11-01）

### 88.1 这次发现的

§0"当前状态（务必先读）"**六项全过时**：

| 项 | 曾写 | 实测 |
|---|---|---|
| 编译产物 | 898,688 B | 1,517,568 B |
| 分区 | 1 MB，剩 147 KB | 4 MB，剩 2.55 MB |
| 自检 | 20/20 | 35/35 |
| 核数 | 单核 | 双核 |
| flash | 2 MB | 16 MB |
| 校验 | 9 项 | 9 + 24 项 |

### 88.2 为什么这是严重问题

**"务必先读"的表错了，读者有两个坏选择：**

- **信它** → 按 1 MB 分区规划，以为只剩 147 KB
- **查它** → 发现第一行就错，不再信后面任何一行

**两种都让整份手册失效。**

### 88.3 规则：数字必须带复测命令

| 项 | 状态（实测日期） | 怎么复测 |
|---|---|---|
| 编译产物 | 1,517,568 B（**W91 当时的快照**，现在的值见 §0） | `(Get-Item build\s31_rtt_test.bin).Length` |
| 自检 | 35/35 | `python tools\board.py COM16 "s31test"` |
| 静态校验 | 24/24 | `python tools\check_rtt_style.py --strict` |

**没有复测命令的数字，下一轮就会变成谎话。**

### 88.4 自动检查（第 26 项）

**只断言能离线验证的那一项（产物大小）：**

```
[PASS] section 0 quotes 1,517,568 B, matches the artifact
```

**反向验证（把旧数字填回去）：**

```
[FAIL] section 0 says 898,688 B, artifact is 1,517,568 B
```

**覆盖不全没关系，关键是"至少有一个数字被盯着"，过时的表就不会悄悄通过。**

### 88.5 ⚠️ 历史记录不要改

`porting-plan.md §12.5` 的"当前状态（2026-10-27）"也过时了，**但它是历史记录**：

```
当前状态（2026-10-27）
| 分支 / 提交 | rtt-s31 / 7938c2bc |
| 镜像 | 193,120 B |
```

**带日期、带提交号、在"第 N 批增量"小节里 —— 这是当时的真实状态。**

**改它 = 篡改记录。**

**判据：**

| 章节类型 | 过时了怎么办 |
|---|---|
| "当前状态 / 务必先读" | **必须更新** |
| "第 N 批增量（日期）" | **保持原样** |
| "风险登记" | 追加，不删旧的 |

### 88.6 检查清单

| 问题 | 方法 |
|---|---|
| 哪个文档有"当前状态"章节？ | `grep "当前状态\|务必先读"` |
| 里面的数字能复测吗？ | 每个数字配一条命令 |
| 有自动检查吗？ | 至少一项离线可验证的 |
| 是历史记录还是当前状态？ | 看有没有日期 + 在什么小节里 |

## 89. "已知限制"表会变成"已知过时"（2026-11-01）

### 89.1 这次发现的

§8 的 7 行限制里，**2 行已经不成立**：

| 曾写 | 实际 |
|---|---|
| 无硬件看门狗 | **中断看门狗已启动**（`boot_tasks.c`，符号 `0x400982b2`/`0x4009836e`） |
| Wi-Fi / BLE 未适配 | **两者都能跑**（`s31wifi` / `s31ble` 实测） |

### 89.2 为什么这类表特别容易过时

**"限制"是当时做不到的事。做不到的事会被做掉，但没人回头删那一行。**

而"能力"表不会：新能力会被积极记录。

**⇒ 限制表需要定期复扫，能力表不需要。**

### 89.3 修法：拆成"还没做"和"已经做完"

```markdown
| **任务**看门狗未启动 | 无 esp_task_wdt_add() 订阅者，启动它只增加失败模式 | 需要时再开 |
| **中断**看门狗已启动 | boot_tasks.c 显式 init，tick 喂它 | 已完成 |
```

**把"已完成"也放进同一张表 —— 这样下次读的人马上知道这一项不用再操心。**

### 89.4 顺带修的两处结构问题

**(a) 两个 `## 8`**

```
389: ## 8. 已知限制
403: ## 8.5 打开 FreeRTOS 兼容层     <- 8.5 夹在 8 和 9 之间
```

**编号是 8.5 ⇒ 它是子节，错的是标题级别。改成 `###`。**
（文档已有 `### 6.8b` 先例。）

**章节号重复会让所有 `§8` 引用变得含糊。**

**(b) 日志头部提交号落后 100+ 轮**

```
头部：84439686      实际：f40c14dd
```

**这是读者看到的第一行。**

### 89.5 自动检查（第 27 项）

```
[PASS] doc section numbers unique      no duplicate top-level section numbers
[PASS] log header quotes current HEAD  header says f40c14dd, HEAD is f40c14dd
```

**反向验证：**

```
# 8.5 改回 ## 级别      -> [FAIL] runbook has two sections numbered 8
# 头部改回旧提交        -> [FAIL] header says 84439686, HEAD is f40c14dd
```

### 89.6 文档审查清单（累积）

| 检查 | 方法 |
|---|---|
| "当前状态"表的数字 | 每项配复测命令（第 26 项自动查产物大小） |
| "已知限制"表的每一行 | **逐行实测**，不复用旧结论 |
| 章节编号 | 唯一且递增（第 27 项） |
| 文档头部的提交/版本 | 与 `git rev-parse` 一致（第 27 项） |
| 历史记录 | **不要改**，加"当前状态"另写 |

### 89.7 一条判据

**"当时正确"的陈述，如果没有复测方法，就一定会变成错话。**

**所以每一条带数字或带"没有/不能"的陈述，都要能回答："现在怎么验证它？"**

## 90. 不要替 UP 主写框架：PCNT 与 `pulse_encoder`（2026-11-01）

### 90.1 被推翻的断言

```c
/* pcnt.c:4，改前 */
* RT-Thread has no pulse-counter class, so this is a plain C API plus an msh
```

**假的。5.4.0 里有：**

```
components/drivers/include/drivers/pulse_encoder.h
components/drivers/misc/pulse_encoder.c      (135 行)
Kconfig: config RT_USING_PULSE_ENCODER       (default n)
```

**而且它有 `#ifndef RT_USING_DEVICE_OPS` 分支 —— 正好是本端口的配置。**

### 90.2 ⚠️ 教训：`"上游没有 X"` 是最该核实的一类断言

**它会让整个实现走上一条不需要的路。** 而核实成本极低：

```powershell
Get-ChildItem rt-thread\components\drivers\include\drivers\*.h | Where-Object Name -match "pulse|encoder"
```

### 90.3 改之前先修功能 bug

`s31pcnt` 本身有问题：

```
pin 4-7, 16-20, 30, 40, 60  -> counted 20
pin 8-15, 50                -> counted 0
```

**按引脚分段成败，说明不是"边沿太快"。**

`cmd_pcnt` 只调了 `gpio_ll_output_enable()`，而一个输出脚需要三件事：

```c
gpio_ll_set_output_signal_matrix_source(&GPIO, pin, SIG_GPIO_OUT_IDX, false);
gpio_ll_set_output_enable_ctrl(&GPIO, (uint8_t)pin, false, false);
gpio_ll_output_enable(&GPIO, pin);
```

**少的两步决定"写电平寄存器能不能到达焊盘"。哪些脚能用只是复位状态的巧合。**

**修法：改用 `rt_pin_mode(pin, PIN_MODE_OUTPUT)`** —— 一次调用拿全三步，
顺带让测试也验证了 pin 驱动。

### 90.4 失败信息必须区分"零"和"少"

```c
/* 改前：两种情况都说这句 */
rt_kprintf("  A short count usually means the edges were faster than the\n");
rt_kprintf("  counter can resolve; retry with fewer edges.\n");

/* 改后 */
if (counted == 0)
    rt_kprintf("  Zero means no edge reached the counter: check the pad's\n"
               "  output path (mux source / output enable), not the edge rate.\n");
else
    rt_kprintf("  A short count usually means the edges were faster ...\n");
```

**`counted == 0` 和 "少几个" 是两类完全不同的故障。混在一起说，会把人带偏一整轮。**

### 90.5 框架契约要读源码

**`pulse_encoder.c` 的实际行为（不要猜）：**

| 行为 | 结论 |
|---|---|
| `ops->clear_count` / `ops->control` | **不检查 NULL** —— 必须是真函数 |
| `ops->init` | 检查 NULL —— 可以为 NULL |
| `rt_pulse_encoder_read()` | **无条件写 4 字节**，完全不看 `size` 参数 |

**最后一条最容易踩：**

```c
rt_int8_t small;
rt_device_read(pe, 0, &small, 1);   /* 写 4 字节到 1 字节的变量 -> 栈损坏 */
```

**调用者必须传 `rt_int32_t *`。**

### 90.6 设备化后的用法

```c
rt_device_t d = rt_device_find("pcnt0");
rt_int32_t count;

rt_device_open(d, RT_DEVICE_OFLAG_RDONLY);
rt_device_read(d, 0, &count, 1);                       /* 必须 32 位缓冲 */
rt_device_control(d, PULSE_ENCODER_CMD_CLEAR_COUNT, RT_NULL);
rt_device_close(d);
```

**`rt_hw_pcnt_*` 保留**，因为 `mcpwm.c` / `rmt.c` 把 PCNT 当量具用 ——
**从量具里调设备 API 是多余的间接。**

### 90.7 两个检查器抓到了我的疏忽

**(a) 产物大小连红两次**

```
says 1,517,568 -> artifact 1,518,528   （加 PCNT 代码）
says 1,518,528 -> artifact 1,518,560   （补 dev.pe 的 ran++）
```

**每次改固件都会红 —— 这正是第 26 项存在的意义。**

**(b) `dev` 组报 `7/8`**

`dev.pe` 有 4 个 `st_report` 却没加 `ran++`。
**W87 为"假分母"建的计数器，抓到了 W93 自己的遗漏。**

### 90.8 清单：把外设做成设备类之前

1. **查树里有没有现成的类** —— `ls components/drivers/include/drivers/`
2. **读那个类的 Kconfig 与 SConscript** —— 确认它在什么条件下编译
3. **读它的 ops 结构体** —— 哪些函数必须提供、有没有 NULL 检查
4. **读它的 read/control 实现** —— 有没有隐含假设（如"不看 size"）
5. **改之前先让功能本身跑对** —— 否则设备类包着一个坏实现
6. **加一条走设备 API 的子检查** —— 证明能通过框架拿到数

## 91. "上游有类"不等于"该用"（2026-11-01）

### 91.1 两种相反的结论

| 轮次 | 外设 | 上游有对应类吗 | 模型匹配吗 | 结论 |
|---|---|---|---|---|
| W93 | PCNT | ✅ `pulse_encoder` | ✅ 完全匹配 | **改用设备类** |
| W94 | TIMG | ✅ `clock_time` | ❌ 不匹配 | **保持私有 API** |

**同一个表象（"绕过了上游"），两个相反的正确做法。**

### 91.2 判据：模型匹配，不是"上游有没有"

**PCNT ↔ `pulse_encoder`：**

```
ops: init / get_count / clear_count / control
read: 写一个 rt_int32_t
control: ENABLE / DISABLE / CLEAR_COUNT / GET_TYPE
```

**本端口要的就是"读计数、清零、使能、停"。一一对应。**

**TIMG ↔ `clock_time`：**

| | `clock_time` | 本端口 TIMG |
|---|---|---|
| 用途 | 系统级**单点截止时间** | 4 个独立周期回调 |
| 参数 | `cnt` 个 tick | 频率 Hz |
| 回调 | 无（`rx_indicate` + 全局 event） | **每通道独立 cb + arg** |
| 实例数 | **1**（`_clock_timer_owner`） | 4 |

**⇒ 不是同一件事。强行套用会重新定义全局 `rt_clock_time_set_timeout()`。**

### 91.3 ⚠️ "能编"是最容易误判的信号

**实验：加 `RT_USING_CLOCK_TIME` + 两个源文件 ⇒ `Project build complete`。**

**但镜像里没有任何符号** —— `--gc-sections` 全丢了（没人引用）。

**编译通过只说明语法兼容，不说明语义匹配。**

### 91.4 断言要分清"文件在不在"和"类是不是这个"

```c
/* 假： */
RT-Thread 5.x has no hardware-timer device class any more - the code is gone.

/* 真： */
There is no general "hardware timer" device class to register into, but
components/drivers/clock_time/ exists (1294 lines) - it is a single-owner
deadline subsystem, not a general timer class.
```

**"代码没了"可查文件（`ls clock_time/`）；"没有这个类"要查语义（读 ops 结构体）。**

### 91.5 判断清单

**看到"上游没有 X"时：**

1. **`ls components/drivers/include/drivers/`** —— 文件在不在
2. **读那个类的 ops 结构体** —— 契约是什么
3. **读它的 read/control 实现** —— 有没有隐含假设
4. **问：我要做的事，这个类是不是就为它设计的？**
5. **如果匹配 → 改；如果不匹配 → 保留私有 API，并把理由写清**

**第 4 步是关键。前 3 步只回答"有没有"，第 4 步回答"该不该"。**

## 92. "磁盘上有"不等于"编进去了"（2026-11-01）

### 92.1 这次的发现

```
NOT COMPILED: main/s31wifiscan.c        (193 行)
```

**整个仓库里唯一一个这样的文件。**

### 92.2 它是被误判移除的

W59/W60 的二分：

| 去掉什么 | 结果 |
|---|---|
| tsens.c + CMake | ❌ 仍不执行 |
| **再去掉 s31wifiscan.c** | ✅ 恢复 |

**看起来是它 —— 但同一节下一段就推翻了：**

> 70.5 但真相不是 s31wifiscan

**真因是 CPU1 调 `rt_system_scheduler_start()` 的竞态。**

**⇒ 移除的理由被推翻，文件却没放回去。**

### 92.3 ⚠️ 二分法的两个陷阱

**(a) "去掉它就好了" ≠ "它是原因"**

去掉一个文件会改变**镜像布局**。布局变了，竞态的胜负就变了。
**所以它对竞态类缺陷有"疗效"，但不是在修它。**

**(b) 结论被推翻后，回头收拾的只有代码，没有构建**

**这条是通用流程缺口：二分时移除了 5 个文件，恢复时只恢复了 4 个。**

### 92.4 分层检查

| 问题 | 检查 |
|---|---|
| 源码被编译了吗 | **第 28 项**（查 `compile_commands.json`） |
| 编译产物留在镜像里吗 | 第 9 项（查锚点 / `-u`） |
| 命令出现在 shell 里吗 | `help` 计数（第 2 项附近的 fsymtab 检查） |

**三层都可能断，检查也要分三层。**

**`s31wifiscan` 断在第一层，而当时只做了第 9 项的检查（因为它是关于链接的）。**

### 92.5 检查 28 的做法

```python
compiled = {Path(e["file"]).resolve() for e in compile_commands.json}
for folder in (port32, port_common, app):
    for f in folder.glob("*.c"):
        if f.resolve() not in compiled:
            orphans.append(f.name)
```

**只查端口和 app，不查 `rt-thread`**（内核有几百个源文件，只有一部分该编）。

**反向验证：从数据库里删掉一条 → 立刻红。**

### 92.6 一条流程建议

**二分结束后，把"移除了什么"列成清单，逐项决定"恢复 / 不恢复 / 稍后恢复"，
而不是只恢复让测试变绿的那几个。**

**这次的代价：一个能用的 M6 诊断工具死了 20 多轮。**

## 93. 排查过程：`waiting for download`（结论见 §94，此处标题已更正）（2026-11-01）

### 93.0 ⚠️ 结论更正

**这一节最初把根因归给"这块板的自动复位线路"。那个结论是错的 ——
真正的原因是**别的程序占着串口并拉着 DTR**，见 §94。**

**下面保留的是当时**有效的事实**（esptool 能连、启动模式字节、字符间隔的影响），
以及那条最重要的方法学教训；**因果判断请直接看 §94。**

### 93.2 排查过程（每一步都是实测）

| 观察 | 结论 |
|---|---|
| `esptool` 能连上、能读 MAC | **芯片和 USB 串口都正常** |
| 关掉串口 3 秒再打开 + 写一个字节 | **完整启动日志 + banner + `msh >`** |
| 只打开串口、不写，读 3~15 秒 | **0 字节** |
| 启动日志第一行 | `rst:0x1 (POWERON), boot:0x58 (SPI_FAST_FLASH_BOOT)` → **正常 flash 启动，不是下载模式** |

**⇒ 镜像没问题、芯片没坏。板子被 DTR/RTS 的静态电平按住了。**
**CH343 的 EN/IO0 经过反相缓冲：串口关闭时两个输出都是低，反相后正好是 "IO0 低 + EN 释放"，
于是上电进 ROM 下载模式，等一个永远不会来的同步帧。**

### 93.3 第二个坑：写太快也会复位

**拿到 prompt 之后发命令，得到的仍然是一份新的启动日志。做了一次间隔扫描：**

| 字符间隔 | 结果 |
|---|---|
| 0 ms（一整串） | 3990 B，**全是启动日志** |
| 2 / 5 / 10 ms | 同上 |
| 20 ms | 4410 B，仍是启动日志 |
| **40 ms** | **185 B，命令正常执行** |

**⇒ 连续写会让 CH343 的线驱动器扰动 EN，芯片被复位。**
**固件改不了这个 —— 这是板子的自动复位电路。**

### 93.4 `tools/board.py` 的修法

```python
CHAR_GAP = 0.045          # 实测阈值在 20~40 ms 之间

def type_line(line):
    for ch in line.encode() + b"\r\n":
        sp.write(bytes([ch]))
        sp.flush()
        time.sleep(CHAR_GAP)
```

**并且：回复里出现 `ESP-ROM` 而没有任何命令输出时，明确报告**

```
--- the board rebooted instead of running 's31test' ---
    (see CHAR_GAP in this file: bytes sent too fast reset this board)
```

**而不是把它当成命令输出打印出来 —— 这正是我前面几轮被误导的地方。**

### 93.5 唤醒序列也要改

**旧序列：开串口 → 写一个 CR → 脉冲 DTR 复位 → 读 4 秒。**
**问题：写一个 CR 已经让板子启动了，再脉冲 DTR 又复位一次，命令落在第二次启动中间。**

**新序列：**

```
写一个 CR
读 8 秒，直到出现 msh >
（若 6 次都没有，才放弃）
然后按 45 ms/字符 逐字发送命令
```

**注意：判断"就绪"必须看**这一次**读到的窗口，不能看累积文本 ——
第一次的启动日志里已经含 `msh >`，用累积判断会让循环立刻退出，
命令发进一个还在启动的板子。**

### 93.6 顺带发现：`s31wifiscan` 的 `help` 描述

放回构建后确认：

```
s31wifiscan      - list visible 2.4 GHz APs
s31wifiscan: 18 AP(s), all on channels 1..14 = the 2.4 GHz band
```

### 93.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `version` | ✅ 正常输出 |
| `s31stat` | ✅ tick / counter / heap / cpu1 全部出现 |
| `list device` | ✅ 含 `pcnt0` |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |

### 93.8 给下次的清单

| 症状 | 先查什么 |
|---|---|
| 上电 `waiting for download` | **不是镜像问题**，先看串口线的静态电平 |
| 有 banner 但命令没输出 | **是不是写太快**，降到 45 ms/字符 |
| 命令输出是一份启动日志 | **板子被复位了**，不是命令失败 |
| `esptool` 连不上但芯片发热正常 | 关串口、等 3 秒、再开 |

## 94. `waiting for download` —— 主因是"别的程序占着串口并拉着 DTR"（2026-11-01）

### 94.1 症状

```
rst:0x1 (POWERON),boot:0x48 (DOWNLOAD(USB/UART0/SPI))
waiting for download
```

**`boot:0x48` = 复位时 IO0 是低电平，芯片进了 ROM 的下载模式。**

### 94.2 真因（此处更正过一次）

**一开始我写的是"串口关闭时驱动把 DTR/RTS 留在低电平"。这个解释是错的。**

**用户的实际情形：串口助手开着。**

**串口助手一打开端口就置 DTR/RTS 有效；这两根线经反相缓冲接到 EN 和 IO0，
反相后就是 "IO0 低"。于是每次复位/上电都进下载模式。**

**同一个原因还解释了另一件事 —— 脚本报：**

```
PermissionError(13, '拒绝访问。', None, 5)
```

**端口被占着。**

**⇒ 两个症状，一个原因：别的程序持有 COM16 并驱动着控制线。**

### 94.3 先做这一步

**关掉串口助手 / 任何占用 COM16 的程序（不只是关窗口，要真的释放端口）。**

**`reset.py` 会在打不开端口时直接说这件事：**

```
cannot open COM16: could not open port 'COM16': PermissionError(13, ...)

If a serial assistant / monitor is open on this port, close it first.
An open monitor also asserts DTR/RTS, which holds IO0 low through the
board's inverting buffer and is what makes the ROM print
'waiting for download' instead of booting.
```

**而不是抛一段 `ModuleNotFoundError` / `PermissionError` 堆栈。**

### 94.4 然后复位并看日志

```powershell
python tools\reset.py COM16          # 复位 + 打印 12 秒启动日志
python tools\reset.py COM16 8        # 只打印 8 秒
```

**实测：**

```
rst:0x1 (POWERON),boot:0x58 (SPI_FAST_FLASH_BOOT)
...
RT-Thread 5.4.0 on ESP32-S31
tick=1000 Hz, cpus=2, msh on UART0
msh >
--- 3990 bytes ---
```

**脚本做的事：打开端口 → 两根线都置高（IO0 高 = 运行，EN 释放）→ 脉冲 DTR 拉低 EN →
松开 → 流式打印启动日志。**

**这就是"按住 BOOT 再按 RST 松手"的电气等价。**

### 94.5 脚本现在会检查启动模式字节

**ROM 自己把启动模式打在日志第一行，所以这可以直接判：**

| 值 | 含义 |
|---|---|
| `0x48` | `DOWNLOAD(USB/UART0/SPI)` —— IO0 低 |
| `0x58` | `SPI_FAST_FLASH_BOOT` —— 正常 |

**`reset.py` 现在：**

* 见到 `0x48` → 明确报"这是下载模式，不是应用启动"，并提示关掉占用端口的程序 / 按 BOOT
* 不是 `0x58` → 提示重跑一次（复位可能不干净）

**⚠️ 更正：我一度说 `0x78` / `0x7f` 也是"正常 flash 启动，高位是 SPI 模式位"——
这个说法没有依据，已删除。稳定得到 `0x58` 的只有"两根线都置高 + 真正复位"这一条路径。**

### 94.6 单字节唤不醒它（仍然成立）

| 发送 | 结果 |
|---|---|
| `\x00`（1 字节） | 0 B |
| `\x00\x00`（2 字节） | **3990 B 完整启动** |
| `\r`（1 字节） | 7 B 残片 |
| `\r\n`（2 字节） | **3991 B 完整启动** |

**不是"某个特定字节"，是"至少两个字节"。**
**`reset.py` 的静默补救因此发的是 `\r\n`。**

### 94.7 手动兜底

1. **按住板上 BOOT（IO0）键**
2. **短按 RST，松开**
3. **再松开 BOOT**

**反过来（按住 BOOT 上电）才是进下载模式。**

### 94.8 想刷机时

```powershell
python -m esptool --chip esp32s31 -p COM16 write_flash 0x10000 build\s31_rtt_test.bin
```

**esptool 会自己拉线进下载模式，不用按键。**
**若报 `Wrong boot mode detected (0x78)`，重试即可 —— 它会再拉一次线。**
**同样地：先关掉占用端口的程序。**

### 94.9 小结

| 操作 | 结果 |
|---|---|
| 串口助手开着 + 裸掉电 | ❌ 占端口，且 DTR 拉低 IO0 → 下载模式 |
| 关掉串口助手 + 裸掉电 | ✅ 正常启动（无条件拉 DTR 时） |
| `tools\reset.py` | ✅ 强制正常启动 + 完整日志 |
| 手动 BOOT + RST | ✅ 正常启动 |

**结论：先关掉占用 COM16 的程序。剩下的用 `reset.py` 就够 —— 既保证正常启动，
又直接给你要看的启动日志。**

## 95. Wi-Fi 连上真实 AP（2026-11-02）

### 95.1 用法

```
msh > s31wifi                                   # 只起接口，不连接
msh > s31wifi <ssid>                            # 开放网络
msh > s31wifi <ssid> <password>                 # WPA2-PSK
```

### 95.2 成功输出

```
s31wifi: STA_START in 0 ms, mac 1c:29:04:d0:14:1e
  connecting to "刻于晴空之上_2.4G" (WPA2-PSK)
s31wifi: PASS, connected to "刻于晴空之上_2.4G" in 1380 ms
s31wifi: ip 10.0.0.119 mask 255.255.255.0 gw 10.0.0.1
```

**三次全新启动：1380 / 1340 / 2000 ms，IP 一致。**

### 95.3 失败时怎么读

```
s31wifi: FAIL after 7240 ms: connected=0, retries=3, last reason=201
s31wifi: reason 15 = 4WAY_HANDSHAKE_TIMEOUT, 205 = AUTH_FAIL, both mean a wrong password;
s31wifi: 201 = NO_AP_FOUND means the SSID is not visible (try s31wifiscan).
```

| reason | 含义 | 先查什么 |
|---|---|---|
| 15 | 4WAY_HANDSHAKE_TIMEOUT | **密码错** |
| 205 | AUTH_FAIL | **密码错** |
| 201 | NO_AP_FOUND | **SSID 不可见** → `s31wifiscan` |
| 2 | AUTH_EXPIRE | 可能是**空密码配了 WPA2** |

### 95.4 凭据不落盘

**SSID/密码只从命令行来，重启就忘。**
**本镜像没有配网路径，把密码偷偷写进 NVS 比不写更糟。**

### 95.5 参数上限

**SSID ≤ 32 字节，密码 ≤ 64 字节**（`wifi_config_t` 的字段宽度），超了直接报错不尝试。

### 95.6 相关命令

| 命令 | 作用 |
|---|---|
| `s31wifiscan` | 列出可见 AP（信道、RSSI、BSSID、SSID） |
| `s31wifi` | 起接口 |
| `s31wifi <ssid> <pass>` | 连接并等 IP |
| `s31ble` | 起 BLE 控制器 |

---

## 96. CPU1 调度：做了一半，按证据回滚（2026-11-02）

### 96.1 现在的状态

```
cpu cur_thread idle_thread irq_nest tick  istack
  0   2f03ea70    2f01fb90        0  7999 0x2f03bc50
  1          0    2f01fc34        0     0 0x2f02a840
istack : distinct per core
```

**CPU1 起来了、执行过 IRAM 代码、证明了 ticket lock 跨核可用，然后停在 `wfi`。**
**它不参与调度。**

### 96.2 试过什么，为什么没成

| 尝试 | 结果 |
|---|---|
| CPU1 直接调 `rt_system_scheduler_start()` | `rst:0x8 HP_WDT1_RESET` 循环 |
| 加上 go 标志（等 CPU0 有系统） | 能启动，但绑定到 CPU1 的线程**一次没跑** |
| 再给 CPU1 加自己的 tick | 仍 `rst:0x10 CHIP_LP_WDT_RESET` |

### 96.3 两个已查清的根因（留着给下次）

**(a) 顺序**：`rt_hw_secondary_cpu_up()` 在 `rtthread_startup()` 里被调用，
**早于 idle 线程创建和 CPU0 的 `rt_system_scheduler_start()`**。
CPU1 若此时调度，就绪表是空的，`RT_ASSERT(to_thread)` 触发。
**修法**：`s31_cpu1_go` 由 `INIT_APP_EXPORT` 置位（即 `main_thread_entry`）。

**(b) tick**：`rt_tick_increase()` 在 SMP 下只做本核
`rt_cpu_self()->tick++` 和 `rt_sched_tick_increase()`；
`rt_timer_check()` 按设计只在 CPU0。
**没有自己的中断，CPU1 的时间片永不过期。**

### 96.4 ⚠️ 两个"看起来能用"但其实不能用的方案

**(a) IDF 的 `vSystimerSetup()`**

```c
for (cpuid = 0; cpuid < configNUM_CORES; ++cpuid)   /* 本镜像 = 1 */
```

**`CONFIG_FREERTOS_UNICORE=y` ⇒ 只有 alarm 0 被配置，alarm 1 永不到期。**
**而且它的 handler 用 `xPortGetCoreID()` 索引 `s_handled_systicks[configNUM_CORES]`
—— 一元数组，CPU1 上越界写。**

**(b) 直接用 SYSTIMER LL**

`hal/systimer_*.h` 由 `esp_hal_systimer` 通过 target 条件化 `INCLUDE_DIRS` 导出。
**加进 `PRIV_REQUIRES` 之后 `project_description.json` 里 `priv_reqs` 仍是
`['freertos','riscv']` —— 依赖没生效，头文件仍然找不到。**

### 96.5 想继续的话从哪里开始

1. **先确认 CPU1 到底收不收中断**：`s31_cpu1_ticks`（`smp_tick.c` 里）统计
   CPU1 的 tick 回调次数，把它打印出来。**如果一直是 0，问题在中断路由，
   不在调度器。**
2. 若中断能到：查 `rt_hw_secondary_cpu_idle_exec()`（现在是裸 `wfi`）
   与 `pcpu->irq_switch_flag` 的交互。
3. 把 `RT_SMP_CPU1_SCHEDULES` 打开即可回到这次的代码，**不用重写。**

### 96.6 一条判断

**"能启动 + 有一个线程看起来在跑"不是"CPU1 在调度"。**
本次的关键证据是 `cpu1probe ran on cpu 4294967295, 0 loops in 300 ms`
—— **绑定到 CPU1 的线程一次都没执行。**
**没有这条，`cpu1 cur_thread == idle_thread` 很容易被当成成功。**

## 97. IDF 官方库怎么调度第二个核（2026-11-02）

### 97.1 三段式

**CPU0（`esp_system/port/cpu_start.c: start_other_core`）：**

```c
esp_cpu_unstall(1);
cpu_utility_ll_enable_clock_and_reset_app_cpu();
cpu_utility_ll_enable_clock_and_reset_app_cpu_int_matrix();
ets_set_appcpu_boot_addr((uint32_t)call_start_cpu1);
while (!cpus_up) { esp_rom_delay_us(100); }    /* 等 CPU1 报到 */
```

**CPU1（同文件 `call_start_cpu1`）：**

```c
la gp, __global_pointer$
esp_cpu_intr_set_ivt_addr(&_vector_table);
esp_cpu_intr_set_mtvt_addr(&_mtvt_table);
core_intr_matrix_clear();          /* 清 CPU1 自己的矩阵 */
s_cpu_up[1] = true;                /* 报到 */
while (!s_resume_cores) { esp_rom_delay_us(100); }
SYS_STARTUP_FN();                  /* SECONDARY 阶段 init */
```

**CPU1（`freertos/app_startup.c: esp_startup_start_app_other_cores`）：**

```c
while (port_xSchedulerRunning[0] == 0) { }   /* 等 CPU0 的调度器 */
esp_int_wdt_cpu_init();
esp_crosscore_int_init();                    /* 核间中断接收 */
xPortStartScheduler();
```

### 97.2 ⭐ 关键：`xPortStartScheduler` 里的两行按核操作

`components/freertos/FreeRTOS-Kernel/portable/riscv/port.c`：

```c
BaseType_t xPortStartScheduler(void)
{
    BaseType_t coreID = xPortGetCoreID();
    port_uxInterruptNesting[coreID] = 0;
    port_uxCriticalNesting[coreID] = 0;
    ...
    vPortSetupTimer();                                    /* 每核 tick */
    esprv_int_set_threshold(RVHAL_INTR_ENABLE_THRESH);    /* 每核 mintthresh */
    rv_utils_intr_global_enable();                        /* 每核 mstatus.MIE */
    vPortYield();
}
```

**⇒ IDF 不是"有复杂的顺序"，而是**每一项都按核做一遍**。
`mintthresh` 和 `mstatus` 都是**每核独立的 CSR**，CPU0 设过不等于 CPU1 设过。**

### 97.3 对照表

| 项 | IDF | 本端口 | |
|---|---|---|---|
| gp / 启动地址 | ✅ | ✅ | |
| mtvec / mtvt | ✅ | ✅ | |
| per-CPU 中断栈 | ✅ | ✅ | |
| 等 CPU0 就绪 | ✅ | ✅ `s31_cpu1_go` | 本次补 |
| CPU1 自己的 tick | ✅ | ✅ TIMG ch3 | 本次补 |
| **CPU1 的 CLIC 阈值** | ✅ 每核 | ❌ 只在 CPU0 设 | **本次补** |
| **CPU1 的 mstatus.MIE** | ✅ 每核 | ❌ | **本次补** |
| CPU1 核间接收 | ✅ | ✅ | 本次补 |
| CPU1 中断矩阵清理 | ✅ | ❌ | **未做** |

### 97.4 补完还是不行，但拿到了决定性的一个数

**在 CPU1 上设阈值、开 MIE、配 TIMG tick、装核间接收之后，从 CPU0 采样：**

```
smp: after go, cpu1 alive=0x5a5a5a5a ticks=0 cur=0x2f01fc34 idle=0x2f01fc34
```

| 字段 | 含义 |
|---|---|
| `alive` = 0x5a5a5a5a | **CPU1 跑了我们的入口代码** |
| `ticks` = **0** | **300 ms @ 1 kHz，一次中断都没收到** |
| `cur == idle` | **进了 `rt_system_scheduler_start()`，切到 idle** |

**⇒ 不是"调度器不肯切"，是"这个核根本没被中断过"。**
**这一条把工作从"改调度器"改到"查中断投递"，方向完全不同。**

### 97.5 核间通讯还需要做吗？

**机制不用再做，验证还没做。**

| 半边 | 状态 |
|---|---|
| 接收侧 | ✅ 已装（`s31intr` INUM 28 有处理器） |
| 发送侧 | ✅ 已解锁 |
| **在 CPU1 上能到吗** | ❓ **未验证** |

**⚠️ 而且本端口的 IPI 和 IDF 不是同一个硬件机制：**

| | IDF | 本端口 |
|---|---|---|
| 触发 | 写 `HP_SYSTEM_CPU_INT_FROM_CPU_0/1_REG`（source 65/66） | 置 CLIC 第 28 行 `IP` 位 |
| 矩阵 | 需要（source → INUM） | 不需要（软件中断直投） |

**两种都合法。但既然 CPU1 连 tick 都收不到，
应该先验证"任何中断能不能到 CPU1"，再谈是哪条路径。**

### 97.6 下一步的最小实验

**不做调度，只测中断：**

```c
/* CPU0 */
rt_hw_ipi_send(RT_SCHEDULE_IPI, 1U << 1);
rt_thread_mdelay(10);
rt_kprintf("cpu1 ticks=%u ipis=%u\n", s31_cpu1_ticks, s31_cpu1_ipis);
```

| 结果 | 含义 | 下一步 |
|---|---|---|
| 收到 | CLIC `IP` 位可用 | 查 TIMG 的矩阵映射 |
| 没收到 | 连软件中断都到不了 | 查 CPU1 的 `mtvt` / 矩阵 / 使能 |
| 收到但与 tick 无关 | 两条路独立 | 分别修 |

**判据：`s31_cpu1_ticks` 从 0 变成非 0，只在这一处发生。**

## 98. 用的是 RT-Thread 还是 IDF 的多核方法？（2026-11-02）

### 98.1 答案：RT-Thread 的

**不是看代码风格，而是看**镜像里有什么符号**：**

```powershell
nm s31_rtt_test.elf | Select-String "start_other_core|call_start_cpu1"
# 无输出

nm s31_rtt_test.elf | Select-String "rt_hw_secondary_cpu_up"
# 40019ea0 T rt_hw_secondary_cpu_up
```

**IDF 的 `start_other_core` / `call_start_cpu1` /
`esp_startup_start_app_other_cores` **一个都不在镜像里** ——
链接器把它们丢了，因为没人引用。**

### 98.2 为什么

`esp_system/startup.c` 的分支：

```c
#if !CONFIG_IDF_RTOS_RTTHREAD
    esp_startup_start_app();          /* IDF 的 */
#else
#if ... && CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE
    entry();                          /* ★ 本镜像走这里 */
```

**`entry()` 就是标准 RT-Thread 入口 `rtthread_startup()`。
CPU1 由其中的 `rt_hw_secondary_cpu_up()` 拉起 —— RT-Thread 的钩子。**

### 98.3 那 IDF 的代码还有什么用

**读它，是为了知道**RT-Thread 模型里缺了哪一步**，不是照抄实现。**

**具体：`xPortStartScheduler()` 在每个核上做**

```c
esprv_int_set_threshold(RVHAL_INTR_ENABLE_THRESH);   /* 每核 mintthresh */
rv_utils_intr_global_enable();                       /* 每核 mstatus.MIE */
```

**而 `rt_system_scheduler_start()` 里没有这两行，
本端口只在 `board.c` 给 CPU0 设过。**
**`mintthresh` 和 `mstatus` 都是**每核独立 CSR**，所以这是真缺的一步。**

### 98.4 核间通讯还需要做吗

**机制不用做了，验证还没做完。**

| | 用什么 | 状态 |
|---|---|---|
| 发送 | RT-Thread `rt_hw_ipi_send` | ✅ `smp_ipi.c` |
| 接收 | RT-Thread `rt_hw_ipi_handler_install` | ✅ 已装，`s31intr` INUM 28 有处理器 |
| 触发 | 内核 `rt_sched_unlock_n_resched()` | ✅ 自动 |
| **在 CPU1 上能到吗** | — | ❓ **未验证** |

**IDF 的 `esp_crosscore_int_*`（`HP_SYSTEM_CPU_INT_FROM_CPU_*`，source 65/66）
与本端口无关，而且本镜像 `configNUM_CORES=1`，它的多核分支根本不编译。**

### 98.5 CPU1 现在排到哪一步了

**已排除 5 个原因：**

| 已排除 | 怎么证的 |
|---|---|
| 启动顺序（CPU0 未就绪） | 加了 go 标志，仍 ticks=0 |
| 没有 tick 定时器 | TIMG ch3 已启动，返回 EOK |
| CPU1 的阈值 / MIE | `rt_hw_secondary_cpu_irq_enable()` 已调 |
| 中断"已路由未使能" | `esp_intr_enable()` 已调 |
| 调度器拒绝切换 | `cur == idle`，切换发生了 |

**剩下 2 个候选：**

| 候选 | 怎么验 |
|---|---|
| **CPU1 的 `mtvt`/`mtvec` 不生效** | **在 CPU1 上故意读地址 0，看 trap 打不打印** |
| **中断矩阵把源路由到了 CPU0** | 读 `INTERRUPT_CORE1_*_MAP_REG` |

**第一个实验不带任何外设，最干净 —— 先做它。**

### 98.6 一句话

**RT-Thread 负责"怎么调度"，IDF 只是在旁边提供"这台机器需要哪些按核的寄存器操作"
的参考。两者不是二选一，是分工。**

## 99. CPU1 排障手册（2026-11-02）

### 99.1 现在的精确状态

```
smp: cpu1 stage=3 tick_started=1 isr_runs=0 ticks=0 early=0 spin=0
```

**翻译：**

| 事实 | 证据 |
|---|---|
| CPU1 执行了我们的入口 | `alive=0x5a5a5a5a`, `entries=1` |
| 进了调度器并切到 idle | `cur == idle` |
| tick 初始化函数**返回了** | `tick_started=1`, `stage=3` |
| tick 的 ISR **一次没进** | `isr_runs=0`, `early=0` |
| 之后的空转循环**没跑** | `spin=0` |

**⇒ 停止点在"tick 初始化返回"与"下一步"之间的几条语句。**

### 99.2 六个量全都对

| 量 | CPU1 | 应等于 |
|---|---|---|
| `mtvec` | `0x2f000003` | `_vector_table` (0x2f000000) + MODE 3 |
| `mtvt` | `0x2f000040` | `_mtvt_table` |
| `mintthresh` | `0x1f` | 阈值 0 |
| `mstatus.MIE` | 1 | 1 |
| 矩阵 map（源 26） | 0 | 0 |
| 线 16 ctrl | `0x3fc00101` | `IE=1`，且 `IP=1`（**挂起**） |

**ⓘ 对比 CPU0：`mstatus=0x80006088`（FS=3, MPIE=1），CPU1 是 `0x00000009`。
FS/MPIE 都不影响中断投递。**

### 99.3 已排除的修法（别再试一遍）

| 修法 | 结果 | 原因 |
|---|---|---|
| 从 CPU0 调 `rt_hw_interrupt_umask()` | ❌ | 写的是**当前核**的 CLIC 窗口 |
| 从 CPU1 调 `esp_intr_enable(handle)` | ❌ | 无变化 |
| 从 CPU1 调 `rt_hw_interrupt_umask(0)` | ⚠️ **IE 变 1**，但 ISR 不进 | 前进了一步 |
| tick 回调加"没有线程就返回" | ❌ | ISR 根本没进 |
| `s31_cpu1_go` 顺序 | ✅ 修掉真 bug（`rst:0x8`） | 但不是这个 |

### 99.4 下一步：查 CLIC 向量槽

**`mtvec` MODE=3 ⇒ CLIC 向量中断，跳转目标是 `mtvt + 4 * (mcause - 16)`。
源 26 映射到中断 0，所以 CPU1 会跳 `_mtvt_table[0]`。**

**如果那个槽位在 CPU1 上没被正确写入，跳转就落到 0 或垃圾 ——
现象正是"停止执行"，不是 panic。**

**验证（纯读取，零风险）：**

```c
/* 在 CPU1 上，或从 CPU0 读同一张表 */
rt_kprintf("mtvt[0]=%p  _interrupt_handler=%p\n",
           ((void **)_mtvt_table)[0], _interrupt_handler);
```

**两者不相等 ⇒ 找到根因。**

### 99.5 怎么复现实验环境

```powershell
# 1. 打开开关
#    rtconfig.h: #define RT_SMP_CPU1_SCHEDULES
powershell -File tools\build.ps1 build
python -m esptool --chip esp32s31 -p COM16 -b 921600 write_flash 0x10000 build\s31_rtt_test.bin
python tools\reset.py COM16 6
```

**关掉开关即回到稳定状态（36/36，不复位）。**

### 99.6 诊断输出在哪

**`s31_cpu1_report()`（`smp_tick.c`，`INIT_APP_EXPORT`）无条件打印：**

```
smp: cpu1 alive=... entries=... mtvec=... mtvt=...
smp: timg src=26 map core0=0 core1=0
smp: line16 ie core0=1 core1=0 (ctrl ...)
```

**开关打开时，`s31_cpu1_release()` 会额外打印"after go"那一组。
⚠️ 它排在 `s31_cpu1_report` 之后（init 表顺序），所以前者看到的是 **go 之前**的状态。**

### 99.7 一条判断

**"六个量全对但不工作"不是失败，是**强约束**：
它把可能的根因从"调度器/中断/时钟"三大类缩到了"CLIC 向量槽"一个点。**
**下一步只需要一次读内存。**

## 100. CPU1：从"完全不动"到"陷阱被取但 handler 没调用"（2026-11-02）

### 100.1 决定性数字

```
smp: cpu1 stage=3 tick_started=1 isr_runs=0 ticks=0 early=0 spin=39449
smp: cpu1 mintstatus=0x00000000 mil=0x00 (cpu0 0x00000000 mil=0x00)
```

**`spin=39449` 上限是 100000 ⇒ 空转循环跑到 60% 才停。**

### 100.2 这一个数排除了什么

| 曾被怀疑 | 为什么排除 |
|---|---|
| CPU1 没在运行 | 循环跑了 39449 次 |
| 某条指令必挂 | 同样的指令重复了 39449 次 |
| 卡在 `esp_intr_alloc` / `umask` | `stage=3`、`tick_started=1` 都写在它们之后 |
| 被反复复位 | `entries=1` |

**⇒ 是被**一次异步事件**打断的。当时唯一武装的中断就是 tick。**

### 100.3 由此得到的结论

| # | 事实 | 证据 |
|---|---|---|
| 1 | 中断被投递 | CLIC `IP=1` |
| 2 | 陷阱被取 | 循环没跑完 |
| 3 | **handler 没被调用** | `isr_runs=0`（计数器在函数第一条语句） |
| 4 | 不在 handler 上下文 | `mintstatus.MIL=0` |

**⇒ 问题在"陷阱被取"与"调用 handler"之间：trap 入口或向量槽。**

### 100.4 ⚠️ 一个我自己造的故障，值得记下来

**我写 `csrr %0, 0x346` 想读 `mintstatus`，而 `riscv/csr_clic.h` 写的是：**

```c
#define MINTSTATUS_CSR      0xFB1
```

**结果：`Illegal instruction`，`MCAUSE 0x2`，**
**`addr2line` 指向 `smp_tick.c:291`（就是那条 `csrr`）。**

**panic 报 `Core 0` —— 因为这条读取在打印函数里，跑在 CPU0 上。
诊断代码本身成了故障源，还伪装成"CPU1 的问题"。**

**⇒ 加诊断代码时，先确认 CSR 编号来自头文件，不要手写。**

### 100.5 下一个假设：CLIC 向量槽错位

**`mtvec` MODE=3 ⇒ 硬件跳转 `mtvt + 4 * (mcause - 16)`。**

**源 26 映射到中断 0，所以取 `_mtvt_table[0]`。
而 `vectors_clic.S` 里：**

```asm
_mtvt_table:
    .word _system_int_handler       /* 0 */
    ...
    .word _interrupt_handler        /* 16 */
```

**⇒ 索引 0 是 `_system_int_handler`，不是 `_interrupt_handler`。**

**如果 `_system_int_handler` 在 CPU1 上不工作（它可能假设 CPU0 的上下文），
现象正是"陷阱被取、handler 没被调用"。**

### 100.6 下一次实验（纯读取，零风险）

```c
rt_kprintf("mtvt[0]=%p mtvt[16]=%p  _interrupt_handler=%p\n",
           ((void **)_mtvt_table)[0], ((void **)_mtvt_table)[16],
           _interrupt_handler);
```

**三种结果：**

| mtvt[0] | 含义 |
|---|---|
| = `_interrupt_handler` | 假设不成立，继续查 trap 入口 |
| = `_system_int_handler` | **找到错位**，改映射或改表 |
| 其他 / 0 | 表本身没初始化 |

### 100.7 复现方式

```powershell
# rtconfig.h 打开这两个：
#   #define RT_SMP_CPU1_SCHEDULES
#   （RT_SMP_CPU1_FAULT_TEST 保持注释）
powershell -File tools\build.ps1 build
python -m esptool --chip esp32s31 -p COM16 -b 921600 write_flash 0x10000 build\s31_rtt_test.bin
python tools\reset.py COM16 6
```

**稳定状态（开关关闭）：36/36 自检、28/28 规范检查、9/9 镜像检查，不复位。**

## 101. ✅ 双核跑通了（2026-11-02）

### 101.1 现在的状态

```
msh > s31smp
cpus   : 2, running on cpu 0
cpu cur_thread idle_thread irq_nest tick  istack
  0   2f03eab0    2f01fb90        0  8015 0x2f03bc90
  1   2f01fc34    2f01fc34        0  7800 0x2f02a840
istack : distinct per core
  cpu1probe ran on cpu 1, 10 loops in 300 ms
cpu1   : SCHEDULING (a bound thread is running there)
```

**两个核都在调度，各自有独立的 tick 和中断栈。**

### 101.2 根因（值得记住的一条）

**trap 入口在 FPU 构建下无条件保存 f0..f31：

```asm
#if defined(__riscv_flen) && (__riscv_flen == 32)
    FPSTORE f0, ...
```

它的注释写着"FS 在所有可运行上下文里都非零"，因为线程帧用 `mstatus=0x7880` 构造。
**但 CPU1 从复位进入自己的入口代码时 `mstatus.FS=0`，
第一条 `fsw` 就是非法指令，核心死在自己的陷阱处理里。**

**修法：CPU1 开中断前先置 `FS=3`。**

### 101.3 那三个数字是怎么定位到这里的

```
stage=4 tick_started=1 isr_runs=0 ticks=0 early=0 spin=39449
```

| 观察 | 排除 |
|---|---|
| `spin=39449`（上限 100000） | 不是"没在跑"，也不是"某条指令必挂" |
| `isr_runs=0` | ISR 一次没进 |
| `mil=0` | 停止时不在 handler 里 |

**再用"关掉 tick"做对照：**

```
无 tick:  spin=100000, 板子稳定
有 tick:  spin=39449, 板子复位
```

**⇒ tick 触发。再对比两核 `mstatus`，FS 差异暴露根因。**

### 101.4 双核打开后必须绑核

**未绑核线程在 MP 下两个核都能取。`s31test ctx` 的 4 个 CPU 密集 worker
不绑核会让测试卡死。** 已在 `selftest.c` 里加 `st_bind_cpu0()` 并逐个绑定。

### 101.5 一个测试自身的双重释放（双核才暴露）

`st_preempt` 的注释：

> clearing `running` first lets them return ... Calling rt_thread_delete() on that
> handle afterwards is a double free.

**而代码就是先清再删。单核时 delete 通常赢；双核后 worker 先返回，就双重释放。**

| 顺序 | panic |
|---|---|
| 先清 `running` 再 delete | `Store access fault` @ `rt_hw_spin_lock` |
| 先 delete 再清 `running` | `Load access fault` @ `rt_object_get_type` |

**⇒ 重排治不了，worker 必须**永不返回**（`while(1)`），唯一退出是 `rt_delete()`。**

### 101.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `s31smp` | ✅ `cpu1probe ran on cpu 1` |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |
| 上游 `rt-thread` | ✅ 零修改 |

### 101.7 开关现在的位置

```c
/* rtconfig.h */
#define RT_SMP_CPU1_SCHEDULES      /* CPU1 参与调度 */
/* #define RT_SMP_CPU1_NO_TICK */  /* 实验用：跳过 CPU1 tick */
/* #define RT_SMP_CPU1_FAULT_TEST */ /* 实验用：故意读地址 0 */
```

**三个开关都在，前两个实验开关默认关闭。**

### 101.8 排障顺序（下次遇到类似问题）

1. **tick 计数**（`isr_runs`）—— 分"没被中断"与"中断了但没进 handler"
2. **关掉一个变量做对照** —— 比继续加探针快得多
3. **读两核的同一组 CSR 做 diff** —— `mtvec/mtvt/mintthresh/mstatus/mintstatus`
4. **读向量槽** —— `_mtvt_table[0]` 与 `_interrupt_handler` 比
5. **检查函数第一条语句的计数器** —— 放后面就分不清"没进"和"进了没算到"

### 101.9 ⚠️ 已知限制：Wi-Fi 与双核暂不兼容

**打开 `RT_SMP_CPU1_SCHEDULES` 后：**

```
msh > s31wifi
rst:0x1a (CPU_LOCKUP_RESET)
```

**第一个假设是 shim 任务被迁移到 CPU1（`rt_thread_create()` 不绑核）。
把每个 shim 任务绑到 CPU0 之后变成 `rst:0xc (SW_CPU_RESET)` —— 没有解决。**

**⇒ 不是任务放置问题，交互更深。**

| 能力 | 双核开 | 双核关 |
|---|---|---|
| `s31test` | ✅ 36/36 | ✅ 36/36 |
| `s31smp` | ✅ `SCHEDULING` | 停核 |
| `s31wifi` | ❌ 复位 | ✅ 连上拿 IP |

**要 WiFi 就注释掉 `RT_SMP_CPU1_SCHEDULES`；要双核就接受 WiFi 不可用。
开关的注释里写了这一条。**

## 102. ✅ Wi-Fi 与双核现在能共存（2026-11-02）

### 102.1 结论

```
msh > s31smp
  1   2f01fcb4    2f01fcb4        0  7759 0x2f02a8c0
cpu1   : SCHEDULING (a bound thread is running there)

msh > s31wifi 刻于晴空之上_2.4G keqing1120
s31wifi: PASS, connected to "刻于晴空之上_2.4G" in 1160 ms
s31wifi: ip 10.0.0.119 mask 255.255.255.0 gw 10.0.0.1

msh > s31test
=== 36 passed, 0 failed ===
```

**双核、Wi-Fi、自检可以同时成立。**

### 102.2 死因：flash cache 关掉时 CPU1 还在取 flash 指令

**panic 的关键字段：**

```
MEPC 0x2f00ca02  -> spimem_flash_ll_set_cs_pin
RA   0x2f017a8c  -> spiflash_start_core
MCAUSE 0x19      -> cache error
```

### 102.3 为什么 IDF 没帮上忙

**反汇编 `spi_flash_disable_interrupts_caches_and_other_cpu()` 只有 6 条指令：
op_lock → noniram_disable → disable_cache → ret。
没有 IPC、没有跨核握手，而且 `esp_ipc_call_*` 在 ELF 里不存在。**

**因为：**

```
CONFIG_FREERTOS_UNICORE=y
CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y
```

**⇒ 那两个宏是"这台机器只有一个核在跑调度器"的断言，而 RT-Thread 有第二个核。
断言与事实不符时，IDF 那一半多核代码不会被编译进来。**

### 102.4 对照实验确定了触发者

| 配置 | WiFi | CPU1 |
|---|---|---|
| 调度 + tick | ❌ cache error | 调度 |
| 调度，无 tick | ✅ 连上拿 IP | 抢不动 idle |
| 无调度 | ✅ | 停核 |

**⇒ 是 tick，不是"CPU1 在跑"。处理器在 IRAM，它调的函数在 flash。**

### 102.5 修法（`spi_flash/cache_utils.c` + 端口）

**IDF 组件加两个 weak 钩子：**

```c
void IRAM_ATTR spi_flash_other_core_stop(void);   /* weak，单核构建用空实现 */
void IRAM_ATTR spi_flash_other_core_resume(void);
```

**端口提供强定义：**

```c
void IRAM_ATTR spi_flash_other_core_stop(void)
{
    s31_flash_ack = 0;
    s31_flash_op = 1;
    while (s31_flash_ack == 0 && spins < 200000u) spins++;
}
```

**CPU1 的 tick ISR 第一条语句之后：**

```c
if (s31_flash_op) { s31_flash_ack = 1; return; }   /* 不调 rt_tick_increase() */
```

**两个变量在 DRAM（cache 关了也能读），
握手在 ISR 最前面（应答之后 CPU1 不会再取 flash 指令）。**

### 102.6 代价

| 项 | 说明 |
|---|---|
| flash 窗口里 CPU1 丢 tick | 与 IDF 多核暂停对方调度器同一取舍 |
| 200000 次超时 | 无 tick 实验态下不会永远等 |
| 改了 IDF 组件 | 两处调用 + weak 声明，组件不反向依赖 RT-Thread |

### 102.7 三个开关现在的意义

```c
#define RT_SMP_CPU1_SCHEDULES       /* CPU1 参与调度 */
/* #define RT_SMP_CPU1_NO_TICK */   /* 实验：跳过 CPU1 tick */
/* #define RT_SMP_CPU1_FAULT_TEST */ /* 实验：故意读地址 0 */
```

**`RT_SMP_CPU1_NO_TICK` 现在是一个有用的诊断开关：
它能区分"CPU1 跑代码导致的问题"和"CPU1 的 tick 导致的问题"。**

### 102.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ 36/36，连跑 3 次 |
| `s31wifi` 连接 | ✅ 两次，ip 10.0.0.119 |
| `s31smp` | ✅ `SCHEDULING` |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |

## 103. USB：选哪个栈、有多少端点、能当几个设备用（2026-11-03）

### 103.1 结论先说

| 问题 | 答案 |
|---|---|
| 用 RT-Thread 的还是 CherryUSB？ | **CherryUSB**（RT-Thread 自带的那份），理由见 §103.2 |
| 端点有多少？ | **16 个（含 EP0）**，全部双向，7 个可作 IN，**0 个等时** |
| 能当几个 USB 设备？ | **物理上 1 个**；一个复合设备最多用 16 个端点 |
| 能做 UAC / UVC 吗？ | **不能** —— 周期 IN 端点数为 0 |

### 103.2 为什么是 CherryUSB，不是 legacy

**RT-Thread 5.4.0 里有两套 USB 栈：**

| | `components/legacy/usb/` | `components/drivers/usb/cherryusb/` |
|---|---|---|
| 状态 | 旧栈（legacy） | 当前推荐 |
| 设备类 | `cdc_vcom` `mstorage` `hid` `ecm` `rndis` `winusb` `audio_*` | `cdc` `msc` `hid` `midi` `adb` `audio` `video` `dfu` `vendor` 等 20+ |
| 主机类 | `hub` + core | `hub` `serial` `msc` 等 |
| **控制器驱动** | **没有** | **`port/dwc2/usb_dc_dwc2.c`、`usb_hc_dwc2.c`** |
| ESP 平台胶水 | 无 | **`port/dwc2/usb_glue_esp.c`** |
| RT-Thread OSAL | 内置 | **`osal/usb_osal_rtthread.c`** |

**关键一条：legacy 的 `usbdevice/` 只有 `core/` 和 `class/`，全树搜不到任何
DWC/OTG/EHCI/OHCI 控制器驱动 —— 它需要 BSP 自己实现一个 `usb_dc_*`。
CherryUSB 直接带 DWC2 设备与主机驱动。**

**而且 CherryUSB 的 dwc2 端口正好断言本核的能力：**

```c
USB_ASSERT_MSG(g_dwc2_udc[busid].hw_params.arch == GHWCFG2_INT_DMA_ARCH,
               "This dwc2 version does not support ...");
```

**实测本核 `GHWCFG2.ARCH = 2 = internal DMA`，这条断言成立。**

**⇒ 不需要引第三方 TinyUSB：IDF 这个 fork 里**没有** `esp_tinyusb`/`tinyusb`
组件（`components/` 下只有 `esp_driver_usb_serial_jtag`、`esp_hal_usb`、
`esp_usb_cdc_rom_console`），所以 TinyUSB 要自己拉依赖；CherryUSB 已经在树里。**

### 103.3 端点数量是**读出来的**，不是查来的

**新增 `s31usb` 命令（`port/esp32s31/usb_probe.c`），读 Synopsys DWC 的
`GHWCFG1..4` —— 这四个是 RTL 综合时写死的只读寄存器，是实例值而不是系列值：**

```
msh > s31usb
s31usb: Synopsys ID 0x4f54430a (DWC OTG rev 79.54)
s31usb: GHWCFG1=00000000 GHWCFG2=239ffed2 GHWCFG3=038046e8 GHWCFG4=de11aa30
s31usb: device endpoints (EP0 included) : 16  (7 IN, 0 periodic IN)
s31usb: endpoint directions             : 0 bi, 1 bi, ... 15 bi
s31usb: DMA architecture               : 2 (internal DMA)
s31usb: HS PHY UTMI+ and ULPI, FS PHY shared with UTMI+
s31usb: total FIFO 896 words = 3584 bytes on this core
s31usb: interrupt sources              : OTGHS=99, multi-proc=100, serial-jtag=2
s31usb: one physical port; a composite device can use at most 16 endpoint(s)
s31usb: isochronous endpoints           : 0 (no UAC / UVC streaming)
```

**逐项解释：**

| 字段 | 值 | 含义 |
|---|---|---|
| `GHWCFG2.NUMDEVEPS` | 15 | 设备端点号 0..15 ⇒ **16 个，含 EP0** |
| `GHWCFG4.INEPS` | 7 | 可作 IN 的**数量**上限（双向核，不是方向限制） |
| `GHWCFG4.NUMDEVPERIOEPS` | **0** | **没有周期 IN 端点 ⇒ 无等时传输** |
| `GHWCFG1.EPDIR` | 0 | 每一位 0 = 该端点号双向（OUT n 与 IN n 可同时用） |
| `GHWCFG3.DFIFODEPTH` | 896 | 所有端点共享 3584 B FIFO |
| `GHWCFG2.ARCH` | 2 | 内部 DMA |
| `GHWCFG2.HSPHYTYPE` | 3 | UTMI+ 与 ULPI |
| `GHWCFG2.FSPHYTYPE` | 2 | FS 共享 UTMI（`SOC_USB_FSLS_PHY_NUM=0`，无独立 FS PHY） |

### 103.4 "能当几个 USB 设备用"

**物理口只有一个（`SOC_USB_OTG_PERIPH_NUM=1`）⇒ 主机看到的**永远是一个设备地址**。

**但在**一个配置**里可以放多个接口，这就是"复合设备"。
端点预算（16 个含 EP0，即 15 个可用）：**

| 功能 | 非零端点占用 |
|---|---|
| CDC-ACM（虚拟串口） | 3（bulk IN + bulk OUT + interrupt IN） |
| MSC（U 盘） | 2（bulk IN + bulk OUT） |
| HID（键盘/鼠标） | 1（interrupt IN） |
| MIDI | 2 |
| 自定义 vendor | 2 |

**举例：CDC + MSC + HID = 6 个非零端点，占 15 个里的 6 个。
⇒ 端点数量不是瓶颈，可以做一个相当复杂的复合设备。**

**真正的瓶颈是**没有等时端点**：**

| 想做 | 可行性 |
|---|---|
| CDC / MSC / HID / MIDI / DFU / vendor | ✅ |
| USB 网卡（ECM/RNDIS/NCM） | ✅（bulk） |
| **UAC 音频（麦克风/扬声器）** | ❌ 需要等时端点 |
| **UVC 摄像头** | ❌ 需要等时端点 |

### 103.5 移植到 S31 还差什么

**CherryUSB 的 `port/dwc2/usb_glue_esp.c` **不认识 S31**（只判 S2/S3/P4）。
三处需要改：**

| 位置 | 现状 | S31 应该是 |
|---|---|---|
| `GET_USB_INTR_SOURCE` | `ETS_USB_INTR_SOURCE`（**S31 不存在这个宏**） | `ETS_USB_OTGHS_INTR_SOURCE`（= 99） |
| `GET_USB_PHY_TARGET` | `USB_PHY_TARGET_INT` | `USB_PHY_TARGET_UTMI`（S31 无 FSLS PHY） |
| `dwc2_get_user_params()` | 只 memcpy `param_fs`/`param_hs` | 加 `#elif CONFIG_IDF_TARGET_ESP32S31` → `param_hs` |

**另外 `usb_osal_rtthread.c:16` 用的是 `rt_thread_create()`，不绑核 ——
按本轮 WiFi 的教训，USB 线程在双核开启时也要绑到 CPU0。**

**`usb_new_phy()` 在镜像里（`esp_hw_support/usb_phy/usb_phy.c` 已编译），
它内部会走 `usb_utmi_hal_init()`，把 §103.7 的四步时钟/复位链路做全。**

### 103.6 两个实测踩到的坑

**(a) 只开两个时钟位读出来全是 0**

**先只置 `HP_SYS_CLKRST.{APB,SYS}_CLK_EN`，结果四个 GHWCFG 全 0，
连 `GSNPSID` 都是 0 —— 正常核上不可能。完整链路四步：**

```
1. HP_SYS_CLKRST.usb_otghs_ctrl0: apb_clk_en + sys_clk_en
2. CNNT_SYS.usb_otg20_ctrl:       utmifs_clk_en + phyref_clk_en
3. 依次释放 phy_rst → ahb_rst → apb_rst（PHY 必须先稳）
4. HP_ALIVE_SYS.usb_otghs_ctrl:   清 phy_suspendm_force_en
```

**(b) 厂商头文件缺宏依赖**

**`cnnt_sys_reg.h` 的 `CNNT_SYS_USB_OTG20_CTRL_REG` 用 `DR_REG_CNNT_BASE`，
而 `reg_base.h` 只定义 `DR_REG_CNNT_SYS_REG_BASE`（0x20359000）。
在端口里 `#undef` 后用 `0x20359000 + 0x30` 重新定义，不改厂商头文件。**

### 103.7 验证

| 检查 | 结果 |
|---|---|
| `s31usb` | ✅ 读到真实 `GSNPSID=0x4f54430a` |
| `s31test` | ✅ 36/36 |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |

已保存 `w107_usbprobe.bin`。
