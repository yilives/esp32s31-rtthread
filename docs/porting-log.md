# ESP32-S31 × RT-Thread 实施记录

> **阅读提示**：本文档是开发过程的原始记录，其中的构建命令（如 idf_s31_env.sh、I:/RT_thread/...）是当时工作区的路径，**对本仓库不适用**。
> 要构建本仓库，请用 	ools/build.ps1，见 README.md。

> 分支：`esp-idf-rtt` → `rtt-s31`，当前提交 `f40c14dd`（`git -C esp-idf-rtt rev-parse --short HEAD`）  
> 首次构建时间：2026-10-26  
> **当前状态（2026-11-01 实测）**：自检 **35/35**、`check_rtt_style.py` **25/25**、`check_image.py` **9/9**；上游 `rt-thread` 工作区**零修改**  
> 首次上板：2026-10-30（COM16），修 6 个启动缺陷后走到应用启动，见 §22



---

## 1. 已完成的事情

在 `RT-Thread-packages/esp-idf` 的 `rtt-dev` 分支基础上，让 `components/rtthread`  
（"在 IDF 内部用 RT-Thread 替换 FreeRTOS" 的那条路线）支持 ESP32-S31。

产物：

```
s31-rtt-test/
├── CMakeLists.txt          RTTHREAD_ROOT 指向 rt-thread
├── sdkconfig.defaults      IDF_RTOS_RTTHREAD=y + 单核模式 + 1kHz tick
├── main/main.c             banner + 双线程 + rt_hw_console_output(ROM putc)
└── build/
    ├── bootloader/bootloader.bin   22 KB
    └── s31_rtt_test.bin            171 KB（分区剩余 83%）
```

改动（6 个文件，`+230` 行左右）：

| 文件                                                 | 改动                                                         |
| -------------------------------------------------- | ---------------------------------------------------------- |
| `components/freertos/Kconfig`                      | `IDF_RTOS_RTTHREAD` 的 `depends on` 加 `IDF_TARGET_ESP32S31` |
| `components/rtthread/CMakeLists.txt`               | 目标白名单、`port/esp32s31` 分支、RISC-V 头文件目录解析                    |
| `components/riscv/vectors_rtthread.S`              | 协作式切换栈帧补 `MPP=M` 的目标宏加 S31（+5 行）                           |
| `components/esp_system/ld/esp32s31/sections.ld.in` | 新增 RT-Thread 中断栈、堆、fsymtab/rt_init 段                       |
| `components/rtthread/port/esp32s31/rtconfig.h`     | 新建，单核 + CLIC 硬件嵌套说明                                        |
| `components/rtthread/port/esp32s31/cpuport.c`      | 新建，栈帧初始化 + `rt_hw_interrupt_is_disabled` + 切换请求            |

---

## 2. 构建验证结果（ELF 级）

| 检查项                               | 结果                                                                                       |
| --------------------------------- | ---------------------------------------------------------------------------------------- |
| `_vector_table`                   | `0x2f000000`，IRAM 基址，64 字节对齐 ✓                                                           |
| `_mtvt_table`                     | `0x2f000040`，低 6 位为 0 ✓                                                                  |
| 向量表 16–39、45–47 槽                 | → `0x2f000100` = `_interrupt_handler`（**RT-Thread 入口**）✓                                 |
| 向量表 40、41 槽                       | → `0x2f000232` = `_panic_handler`（INT_WDT / CACHEERR，IDF 设计如此）                           |
| 向量表 44 槽                          | → `esp_ipc_isr_handler`（仅当 `CONFIG_ESP_IPC_ISR_ENABLE=y`，会绕过 `rt_interrupt_enter/leave`） |
| `_interrupt_handler` 位置           | `0x2f000100`，在 IRAM（noflash）✓                                                            |
| `__stack_start__ → __stack_end__` | `0x2f00de00 → 0x2f017e00` = **40 KB** ✓                                                  |
| `__heap_start__ → __heap_end__`   | `0x2f017e00 → 0x2f021e00` = **40 KB** ✓                                                  |
| `rtthread_startup` / `entry`      | 均存在 ✓                                                                                    |
| IRAM 占用                           | 42,296 B                                                                                 |
| DRAM 占用                           | data 9,894 B + bss 4,560 B                                                               |
| Flash 占用                          | text 86,194 B + rodata 35,440 B                                                          |

**注意**：以上全部是"链接产物正确"，**不等于能跑**。tick、上下文切换、CLIC 嵌套  
这三件事必须上板才能验证。

---

## 3. 环境搭建踩坑记录（重建必看）

### 3.1 Git Bash 下 IDF 的三个环境陷阱

1. **`MSYSTEM` 会让 IDF 直接 fatal**  
   `tools/idf_tools.py:3519` 和 `tools/idf.py:1483` 都有  
   `if 'MSYSTEM' in os.environ: fatal('MSys/Mingw is not supported')`。  
   bash 的 `unset MSYSTEM` **传不到原生 Python 进程**（实测仍然存在），  
   必须在 Python 内部 `os.environ.pop('MSYSTEM')` 后再 exec 脚本。
2. **`LOCALAPPDATA` 未导出 → ccache 校验失败 → 后续工具被跳过**  
   表现为 `ccache: error: could not find configuration file and the LOCALAPPDATA
   environment variable is not set`，然后 esp-rom-elfs、esp-clangd 都没装，  
   最终 `activate.py` 报 `tool has no installed versions`。
3. **`requirements.txt` 已拆分**  
   IDF 6.x 的依赖文件在 `tools/requirements/requirements.core.txt`，  
   根目录没有 `requirements.txt`。

**可用方案**：`<workspace>/idf_s31_env.sh`，把 `activate.py` 导出的变量手工翻译成  
POSIX 形式（IDF 自己生成的脚本是 Windows 反斜杠 + 分号 PATH，在 bash 里 source 会失败）。

### 3.2 fork 缺少 6 个上游子模块

`esp-idf-rtt` 的 `.gitmodules` 比官方 v6.1 少了几个纯依赖，`git submodule update`  
无法补齐，必须从 `esp32s31-sdk/esp-idf-v6.1` 直接拷贝：

```
components/unity/unity                                        ← 不补则 CMake 报 include 目录不存在
components/cmock/CMock
components/openthread/lib
components/openthread/openthread
components/protobuf-c/protobuf-c
components/spiffs/spiffs
components/bootloader/subproject/components/micro-ecc/micro-ecc  ← 不补则 bootloader 配置失败
components/bt/controller/lib_esp32c2/esp32c2-bt-lib            ← 与 S31 无关，补为保持树完整
components/bt/controller/lib_esp32h2/esp32h2-bt-lib
```

这些目录**未纳入 git**（fork 的 .gitmodules 里没有它们），重建时需要重新拷。

### 3.3 RT-Thread master 的头文件搬迁

`libcpu/risc-v/common/` 已被拆成 `common/`（新，FDT/DM 版）和 `legacy_common/`（旧的扁平布局）。  
组件原本按 `common/{cpuport.h,rt_hw_stack_frame.h}` 找头文件，在 master 上必然失败。  
已在 `CMakeLists.txt` 里加自动解析：优先 `legacy_common`，不存在再回退 `common`。

### 3.4 ldgen 缓存导致链接脚本不更新

改了 `sections.ld.in` 之后，ninja 只打印  
`Skipping linker script generation, section names unchanged`，  
把**旧的 sections.ld 原样写回**，于是符号依旧 undefined。

必须删掉这三个文件再构建：

```
build/esp-idf/esp_system/ld/sections.ld
build/esp-idf/esp_system/ld/sections.ld.fingerprint
build/esp-idf/esp_system/ld/sections.ld.lfcache
```

---

## 4. 修正过的两个错误判断

| 原判断（写在方案里）               | 实测结论                                                                                                     |
| ------------------------ | -------------------------------------------------------------------------------------------------------- |
| 需要自己补 48 项 `_mtvt_table` | **不需要**。`vectors_clic.S` 在 CLIC 目标下无条件编译并已定义该表，其 16–47 槽正好指向 `_interrupt_handler`。自己再加会 duplicate symbol |
| CLIC 是最大难点               | **不是**。RT-Thread 自己的 `pic-riscv-clic.c` 反而不能用（与 IDF 中断矩阵冲突）；真正的活是"薄封装 IDF 的 `intr_handler_set`"，约 80 行   |

---

## 5. 下一步（按依赖顺序）

### 5.1 上板验证（阻塞项：需要硬件）

> 2026-10-30 已解除：板子到手，第 1 步之前先修了 6 个启动缺陷（§22）。  
> 下面的顺序本身是对的，只是当时没有预料到"第 1 步"之前还有一整段。

板子到手后的验证顺序，**不要跳步**：

1. `idf.py flash monitor` → 看 banner 是否出现（验证链接 + 栈 + 串口）
2. 只留单线程 `rt_thread_mdelay` → 验证 tick 来自 SYSTIMER alarm
3. 双线程不同优先级 → 验证上下文切换
4. 同优先级 + 时间片 → 验证 `RT_SCHED_ROUND_ROBIN`
5. 中断嵌套 3 层 → 验证 CLIC 硬件嵌套与 `rt_interrupt_nest`
6. 长跑 30 分钟 → 验证 tick 漂移与内存泄漏

### 5.2 P2：中断与设备（可在无板时先写代码）

- `port/esp32s31/interrupt.c`：`rt_hw_interrupt_install` → `intr_handler_set` 薄封装
- UART：`rt_serial` 后端接 IDF `driver/uart`
- GPIO：`rt_pin_ops` 接 `esp_driver_gpio`
- 打开 `RT_USING_DEVICE` / `RT_USING_CONSOLE` / `RT_USING_FINSH`

### 5.3 Wi-Fi / BLE（硬需求，独立里程碑，不进 P1/P2）

实测数据：

- `components/esp_wifi/esp32s31/esp_adapter.c` = **831 行**，实现  
  `wifi_osi_funcs_t`（信号量/互斥量/队列/任务/ISR 标志/自旋锁等，约 60 个函数指针）；
- 该文件 **0 处** RT-Thread 相关代码，全部基于 FreeRTOS 原语；
- `components/bt/controller/esp32s31` 的 BT 控制器库存在，但同样依赖 FreeRTOS 语义。

结论：**原理可行（乐鑫留了 OS 适配层），但工作量与验证成本都不低**，且  
Wi-Fi 的射频校准、共存时序、ISR 约束**无法靠编译验证**，必须有板子。  
建议排在 P2 上板验证通过之后，作为独立里程碑，不要和内核移植混在一起。

### 5.4 评审后新增的两条风险（已在方案 §8 登记为 R9/R10）

- **R9 · 中断帧尺寸不一致（比 FP 丢失更早炸）**：协作帧/线程初始帧是 256 B（32 整数 + 32 FP），  
  中断帧只有 128 B 且不存 `f0–f31`。trap 出口无条件 `+128`，恢复协作式帧时 **sp 低 128 B**，  
  纯整数线程 + 高频抢占也会崩。**已修**（`c948a462`）：入口 −256、出口 +256，32 `fsw` / 32 `flw` 对称。  
  注意 `ARCH_RISCV_FPU` 由 `cpuport.h` 按 `__riscv_flen` 自动定义（ELF `Tag_RISCV_arch` 含 `f2p2`），  
  **不要往 rtconfig.h 里加**。
- **R10 · 驱动层调 FreeRTOS 阻塞原语**：`freertos` 组件无条件编译 `queue.c`/`tasks.c`，符号全解析  
  （`xQueueSemaphoreTake` 在 ROM），所以**链接能过**；真实失败是阻塞 API 等一个永不到来的调度，运行即挂。  
  分布：`uart.c` 全套、`uhci.c` 队列、`uart_vfs.c` 仅 `portMUX`、`uart_wakeup.c` 干净。  
  结论不变：UART 走 `hal/uart_hal.h` 自写 `rt_serial`；GPIO 仅临界区，可继续用 driver 层。

> 另：评审怀疑的“uart RX timeout 依赖 esp_timer”经核对**不成立**——`uart.c` 用硬件 `UART_TOUT`，  
> 全组件只有 `test_apps/` 引用 esp_timer。

---

## 6. P2 首批增量（2026-10-27，提交 `36fc7f4f`）

### 6.1 做了什么

| 文件                                   | 内容                                                                                        |
| ------------------------------------ | ----------------------------------------------------------------------------------------- |
| `port/esp32s31/interrupt.c`（新，131 行） | 蹦床 + 侧表 + INUM 断言 + mask/umask/priority                                                   |
| `port/esp32s31/rtconfig.h`           | `RT_USING_CONSOLE`、`RT_CONSOLEBUF_SIZE`、`RT_USING_FINSH` + MSH、`RT_USING_COMPONENTS_INIT` |
| `components/rtthread/CMakeLists.txt` | 编译 finsh 四个源文件、全局导出 finsh 头、`-u finsh_system_init`                                        |
| 应用 `main.c`                          | `rt_hw_console_getchar()` 走 UART0 LL；`s31stat` / `s31fpu` 两条 msh 命令                       |

### 6.2 接入 msh 踩到的 6 个坑（按出现顺序）

1. **`finsh.h` 必须全局可见**：`rtthread.h:41` 在 `RT_USING_FINSH` 下直接 include `finsh.h`，  
   而它只加进了组件自己的 INCLUDE_DIRS，导致 `esp_http_server` 等组件全部编译失败。  
   → 同时加进 `idf_build_set_property(INCLUDE_DIRECTORIES ...)`。
2. **`RT_CONSOLEBUF_SIZE` 未定义**：开了 `RT_USING_CONSOLE` 后 `kservice.c:395` 直接报错。  
   → rtconfig.h 补 `256`。
3. **`RT_USING_COMPONENTS_INIT` 未开**：`INIT_*_EXPORT()` 整段宏定义被它包住，  
   `.rti_fn` 表只剩 4 个边界标记，`finsh_system_init` 永远不执行。  
   → 定义该宏；判据是 `nm` 里 `__rt_init_start == __rt_init_end`。
4. **finsh 的目标文件根本没被链接**：归档成员没有符号被引用，`KEEP(*(.rti_fn*))` 只保留段、  
   不拉成员。→ `-u finsh_system_init` 强制拉入（与 `-u main` 同一手法）。
5. **`-Werror=cast-function-type`**：finsh 命令表把 `int(*)(int,char**)` 存成 `long(*)(void)`，  
   是上游设计。→ finsh 源文件与应用侧各自加 `-Wno-cast-function-type`。
6. **shell 是忙轮询**：`shell.c:581` 的 `if (ch < 0) continue;` 会在优先级 20 空转。  
   → `rt_hw_console_getchar()` 在 FIFO 空时 `rt_thread_mdelay(1)`，否则 idle 线程永远得不到 CPU。

### 6.3 产物验证

| 项           | 值                                                                                                         |
| ----------- | --------------------------------------------------------------------------------------------------------- |
| 镜像          | 179,360 B（`0x2bca0`）                                                                                      |
| IRAM        | 42,424 B                                                                                                  |
| `__fsymtab` | `0x40020fa8`–`0x40020fcc`，2 条命令 × 12 B                                                                    |
| `__rt_init` | `0x40020fd0`–`0x40020fe4`，含 `finsh_system_init`                                                           |
| 中断桥接        | `librtthread.a` 内 `rt_hw_interrupt_install` / `rtt_trampoline` 均为 T 符号；当前无人引用故被 gc-sections 丢弃，驱动一旦调用即可拉入 |

### 6.4 仍然待办

- `rt_serial` 后端（走 `hal/uart_hal.h`），替换掉现在这个轮询 getchar；
- `rt_pin_ops`（`esp_driver_gpio/src/gpio.c` 无 FreeRTOS 调用，可直接封装）；
- 设备框架 `RT_USING_DEVICE`（需要把 `src/device.c` 加进 kernel_srcs）。

---

## 7. P2 第二批增量：UART0 串口驱动（2026-10-27，提交 `a3c805fb`）

### 7.1 实现

`port/esp32s31/serial.c`（156 行）实现 RT-Thread serial **v1** 的 `rt_uart_ops`：

| 回调          | 做法                                                                                   |
| ----------- | ------------------------------------------------------------------------------------ |
| `configure` | 不改波特率/引脚/帧格式，**沿用 ROM 控制台配置**；只设 RX 超时 10 bit                                        |
| `control`   | `SET_INT`/`CLR_INT` → `uart_ll_ena/disable_intr_mask` + `rt_hw_interrupt_umask/mask` |
| `putc`      | 轮询 `uart_ll_get_txfifo_len` 后写 FIFO（不开 TX 中断，免 TX 环形缓冲）                              |
| `getc`      | `uart_ll_get_rxfifo_len` 为 0 返回 −1，否则读一字节                                            |

ISR 走 `ETS_UART0_INUM`(5) → mtvt 槽 21 → `_interrupt_handler` → `rtt_trampoline`  
→ `s31_uart_isr`，清 `int_clr` 后调 `rt_hw_serial_isr(RT_SERIAL_EVENT_RX_IND)`，  
后者循环 `getc` 灌进 64 字节环形缓冲并触发 `rx_indicate`，shell 的信号量随即释放。

### 7.2 这次踩的 4 个坑

1. **`src/device.c` 不存在**：RT-Thread 5.x 把设备框架移到 `components/drivers/core/device.c`，  
   组件的存在性检查直接报 "source file is missing"。
2. **`completion` 被拆成三个文件**：`completion_up.c` 有 `init`，`completion_comm.c` 有 `wait`/`done`，  
   只加一个会留一半 undefined。
3. **归档顺序**：`libmain.a` 在 `librtthread.a` 之后被扫描，应用对 `rt_hw_uart0_init` 的引用  
   出现得太晚，成员不会被拉入。→ `-u rt_hw_uart0_init`（仅 S31 目标）。
4. **`rtthread.h` 会 include `rtdevice.h`**：开了 `RT_USING_DEVICE` 后，所有引用 `rtthread.h` 的  
   组件都要能看到 `components/drivers/include`，必须加到全局 include，不能只给组件内部。

### 7.3 产物

| 符号                        | 地址/类型                           |
| ------------------------- | ------------------------------- |
| `rt_hw_uart0_init`        | `0x4000cd98` T                  |
| `s31_uart_isr`            | `0x4000ccc6` t                  |
| `rt_hw_interrupt_install` | `0x4000ecee` T ← **桥接第一次真正被链接** |
| `rtt_trampoline`          | `0x4000ecdc` T                  |
| 镜像                        | 183,856 B（`0x2ce30`）            |

IRAM 42,424 B（未变）；DRAM data 9,914 B（+20）；bss 5,600 B（+512）；flash.text 94,412 B（+4.3 KB）。

### 7.4 已知限制

- 波特率固定为 ROM 控制台的 115200；`configure` 收到其它值只打印一条警告。  
  真正可配置需要 `uart_ll_set_baudrate` + APB 时钟频率 + GPIO 矩阵重排（P2.3）。
- TX 是忙等，`rt_device_write` 大批量时效率一般；P3 可以换成 TX 中断 + 环形缓冲。
- `rt_kprintf` 仍走 `rt_hw_console_output()`（ROM putc），没有切到设备输出，  
  这样即使串口驱动出问题也不会把控制台一起带走。等上板验证后再切。

---

## 8. WDT 复核（2026-10-27）

原风险 R11 写的是"task WDT 5 s 会误复位、上板前必须关掉"。回代码核对后**不成立**：

| 看门狗                 | 初始化位置                                                     | RT-Thread 模式下                       |
| ------------------- | --------------------------------------------------------- | ----------------------------------- |
| INT WDT（300 ms）     | `freertos/app_startup.c:64`，在 `esp_startup_start_app()` 内 | **不执行**（`startup.c` 走 `entry()` 分支） |
| TASK WDT（5 s）       | `freertos/app_startup.c:197`，在 `main_task()` 内            | **不执行**（FreeRTOS main 任务根本不创建）      |
| BOOTLOADER WDT（9 s） | bootloader 内                                              | 只覆盖 bootloader，跳转前已关闭               |


结论：**不会误复位，不需要改 sdkconfig**。但代价是整套系统没有硬件看门狗——
这是 R11 真正要记录的风险，比"会误复位"低一档，但需要在 P3 之后补：
把喂狗挂到 RT-Thread idle 线程上重新初始化 `esp_task_wdt`，或在 TIMG 上自建。

---

## 9. P2 第三批：GPIO 与两个"装了但不触发"的中断缺陷（2026-10-27，`e61b3839`）

这一批写 GPIO 驱动时，为了确认"中断注册编号到底是 INUM 还是 mtvt 槽位"，
把 IDF 的中断分配链路从 `esp_intr_alloc` 一路读到寄存器定义，结果发现
**前两批代码里有两个会让中断彻底失效的缺陷**，都属于"编译通过、链接通过、
运行时不报错、但功能完全没有"的类型。

### 9.1 缺陷一：中断矩阵复位后全部指向 CPU 线 0

```c
// components/soc/esp32s31/register/soc/interrupt_core0_reg.h
/** INTERRUPT_CORE0_UART0_INTR_MAP : R/W; bitpos: [5:0]; default: 0; */
/** INTERRUPT_CORE0_GPIO_INTR0_MAP : R/W; bitpos: [5:0]; default: 0; */
```

`default: 0` 意味着上电后**每个外设中断源都挂在 CPU 中断线 0 上**。

第二批的 `serial.c` 是这样的：

```c
rt_hw_interrupt_install(ETS_UART0_INUM /* 5 */, s31_uart_isr, RT_NULL, "uart0");
```

handler 装在线 5，但矩阵里 UART0 还指向线 0。运行时表现：

- `intr_handler_set(5, ...)` 成功，`intr_get_item(5)` 里确实有我们的函数
- UART 的 `RXFIFO_FULL` 中断确实置位了，`int_raw` 看得到
- 但 CPU 线 0 没有被 unmask，线 5 上根本没有信号
- **没有异常、没有打印、没有任何提示**，只是串口收不到数据

这类问题在没有硬件时不可能靠编译发现，只能靠读寄存器定义发现。

**修复**：新增 `rt_hw_intr_route()`：

```c
int rt_hw_intr_route(int source, int inum)   /* source = ETS_*_INTR_SOURCE */
{
    if (source < 0 || source >= ETS_MAX_INTR_SOURCE ||
        inum   < 0 || inum   >= SOC_CPU_INTR_NUM) return -RT_EINVAL;
    esp_rom_route_intr_matrix(0, (uint32_t)source, (uint32_t)inum);
    return 0;
}
```

`esp_rom_route_intr_matrix` 在 S31 的 ROM 导出表里（`esp32s31.rom.api.ld:55`，
`PROVIDE ( esp_rom_route_intr_matrix = intr_matrix_set )`，落到 `0x2f800768`）。

**编号关系三段对齐**（这是最容易写错的地方）：

| 环节 | 值 | 出处 |
|---|---|---|
| 注册给 `intr_handler_set()` | `ETS_UART0_INUM` = **5** | `esp_cpu.h` "Interrupt number (from 0 to 31)" |
| `esp_rom_route_intr_matrix(0, src, 5)` 写进 MAP 寄存器 | **21**（ROM 自己 +16） | `esp_rom_sys.h` "passing cpu_intr_num = i would in fact bind ... CLIC_EXT_INTR_NUM_OFFSET + i" |
| CPU 跳转的 mtvt 槽位 | **21** | `_mtvt_table[INUM + RV_EXTERNAL_INT_OFFSET]` |
| 分发时索引 | `mcause - 16` = **5** | `riscv/interrupt.c:67` |

三段互相对齐，所以 `vector = ETS INUM` 这个契约是自洽的。

### 9.2 缺陷二：CLIC 优先级 0 等于被屏蔽

追 GPIO 中断时继续往下问了一句"那优先级呢"，于是发现第二个更隐蔽的问题。

```c
// components/riscv/include/riscv/csr_clic.h
#define RVHAL_INTR_ENABLE_THRESH        0
#define NLBITS_TO_BYTE(level)   (((level) << NLBITS_SHIFT) | ((1 << NLBITS_SHIFT) - 1))
#define CLIC_INT_THRESH(intlevel)       NLBITS_TO_BYTE(intlevel)     // → 0 编码成 0x1F
```

CLIC 取中断的判据是 `int_ctl_byte > mintthresh_byte`：

```
board.c: esprv_int_set_threshold(RVHAL_INTR_ENABLE_THRESH)  →  mintthresh = 0x1F
外部中断复位级别 = 0                                        →  int_ctl  = 0x1F
0x1F > 0x1F  →  不成立  →  中断永远不被取
```

也就是说：**路由对了、`int_ena` 开了、`esprv_int_enable()` 调了，中断照样不来。**

IDF 自己不会踩到，因为 `esp_intr_alloc()` 每次都写优先级：

```c
// components/esp_hw_support/intr_alloc.c:817-819
int level = esp_intr_flags_to_level(flags);
esp_cpu_intr_set_priority(intr, level);      // 默认 ESP_INTR_FLAG_LEVEL1 = 1
```

我们的桥接层没有这一步，所以每个装上去的中断都是 0 级。

**修复**（`interrupt.c`）：

```c
if (handler != RT_NULL) {
    intr_handler_set(vector, rtt_trampoline, &s_ctx[vector]);
    if (esprv_int_get_priority(vector) == 0)          /* 只在还是复位值时补 */
        esprv_int_set_priority(vector, RTT_INTR_DEFAULT_LEVEL /* 1 */);
}
```

只在"仍是复位值"时补，所以驱动想分级嵌套可以先调
`rt_hw_interrupt_set_priority()` 再 install，不会被覆盖。

**副作用值得记住**：现在 UART0 和 GPIO 都是 1 级，**同级别不能互相抢占**，
所以系统目前**没有硬件中断嵌套**。R4 要验证嵌套必须先给两个源设不同级别，
否则测出来的"无嵌套"是配置造成的，不是实现问题（已登记为 R17）。

### 9.3 GPIO 驱动

`port/esp32s31/gpio.c`，389 行，`rt_pin_ops` 直接操作 LL。两个关键点：

1. **先选 IO_MUX 功能**：`gpio_ll_func_sel(&GPIO, pin, PIN_FUNC_GPIO /* 1 */)`。
   不选的话 `input_enable` / `output_enable` 完全不生效——因为信号还挂在
   IO_MUX 的外设功能上。
2. **输出要断开外设矩阵**：
   ```c
   gpio_ll_set_output_signal_matrix_source(&GPIO, pin, SIG_GPIO_OUT_IDX /* 256 */, false);
   gpio_ll_set_output_enable_ctrl(&GPIO, pin, false, false);
   gpio_ll_output_enable(&GPIO, pin);
   ```
   否则 pad 仍由上一条路由到这里的外设驱动，写 `GPIO_OUT` 寄存器没有任何效果。

其他实现要点：

- 所有引脚共用一个 ISR（`ETS_GPIO_INUM = 4`），一次读 `intr_0` 和 `intr_01`
  两个状态字（低 32 脚 / 高 30 脚），**先清再分发**，避免电平触发重复进入。
- 开漏模式同时打开输入，这样能回读总线电平。
- `pin_debounce` 映射到 IO_MUX 的硬件毛刺滤波器；**只有开/关**，宽度由
  IO_MUX 时钟分频固定，接口里的 `debounce` 值只用来判断"是否启用"。
- `pin_get()` 接受 `"12"` / `"P12"` / `"GPIO12"`，其他一律返回 -1，
  避免拼错时静默解析成 0 号脚。
- `SOC_GPIO_PIN_COUNT = 62`，GPIO29 / GPIO41 被 flash 占用，
  `SOC_GPIO_VALID_GPIO_MASK` 已排除，`s31_pin_valid()` 会挡住。

**S31 特有**：有 **4 条 GPIO 中断通道**（`ETS_GPIO_INTR0..3_SOURCE`），
这是相对 C3/C6 的新特性。但 `gpio_ll_intr_enable_on_core()` 写死
`GPIO_LL_INTR0_ENA` 并带着 IDF 的 TODO（`IDF-7995`），所以只用通道 0。

### 9.4 顺带解决的两个工程问题

| 问题 | 现象 | 解法 |
|---|---|---|
| pin 设备注册时机 | 应用代码在 init 阶段调 `rt_pin_*()` 时设备还不存在 | `rt_hw_board_init()` 里调 `rt_hw_pin_init()` |
| 归档成员被丢弃 | `rt_device_pin_register()` 没有外部引用者，`gpio.c.obj` 被 `--gc-sections` 丢掉，`.rti_fn` 表里也不会出现 | 同上——board.c 的调用就是那个引用，不需要再写 `-u` |

另外 `components/drivers/pin/dev_pin.c` 自带 `pin` msh 命令
（`MSH_CMD_EXPORT_ALIAS(_pin_cmd, pin, ...)`），不用自己写一个。

### 9.5 产物

| 项 | 值 |
|---|---|
| 镜像 | 188,336 B（0x2dfb0） |
| IRAM | 42,424 B（未变） |
| `pin` msh 命令 | 已注册（`__fsymtab` 3 项 → 4 项） |
| `rt_hw_intr_route` | `0x4000ed7e` T |
| `esp_rom_route_intr_matrix` | `0x2f800768` A（ROM） |

---

## 10. P2 第四批：自检套件（2026-10-27，`d2a5d07d`）

### 10.1 为什么先写测试而不是继续写驱动

剩下的 P2 项目（可配置波特率、控制台走设备）都是锦上添花，而
**tick / 上下文切换 / CLIC 中断 / FPU 帧这四件事一次都没在硬件上验证过**。
没有这四条的结论，后面每加一个驱动都是在未验证的地基上盖楼。

所以这一批的产出是 `s31test`：一条命令把这四项变成可判读的 PASS/FAIL。

### 10.2 六个用例与它们真正在证什么

| 用例 | 手法 | 能抓到什么 |
|---|---|---|
| `tick` | `rt_tick_get()` 增量 vs SYSTIMER OS-tick 计数器（1 MHz），再用 `esp_rom_delay_us()` 的 CPU 周期忙等做第二参考 | `CONFIG_FREERTOS_HZ ≠ RT_TICK_PER_SECOND`。这是编译期查不出来的（两个宏来自不同配置文件），但会让系统所有延时和超时按比例错 |
| `ctx` | 4 个线程同优先级，各自在 `rt_thread_yield()` 前后校验**栈上的**哨兵数组 | 栈指针被恢复错位置 |
| `preempt` | 2 个**循环里没有任何系统调用**的纯整数线程 + 1 个每 1 ms 睡一次的线程 | R9：中断帧与协作帧尺寸不一致。这条专门制造"协作式保存的帧被 trap 出口恢复"的场景，每秒 1000 次 |
| `fpu` | 两个线程用 `fmv.w.x` 写满 f0–f31（32 条），`mdelay` 后逐条 `fmv.x.w` 读回 | 浮点现场没有保存/恢复 |
| `irq` | 写 `GPIO_STATUS_W1TS` **软件置位中断状态**，不需要外部接线 | 矩阵路由、CLIC 槽位、蹦床、优先级，四段任一断掉都是 0 次中断；回调里还断言 `rt_interrupt_nest == 1`，覆盖 trap 入口/出口的 enter/leave 配对 |
| `sem` | 500 次 ping-pong + 一次必须超时的 take | 信号量计数或 suspend 链表错误 |

`preempt` 和 `irq` 是最有价值的两条：

- `preempt` 不依赖任何外设，纯内核路径，能把 R9 的修复证伪或证实
- `irq` 不需要接线，却覆盖了从矩阵到回调的完整链路——**它是 §9.1 / §9.2
  两个缺陷的回归测试**

### 10.3 产物级确认（无硬件时的替代验证）

```
selftest.c.obj：fmv.w.x 32 条、fmv.x.w 32 条
__fsymtab：5 项（msh_help、s31test、pin、s31fpu、s31stat）
符号：s31_selftest_init / st_cmd / st_tick / st_ctx / st_preempt / st_fpu / st_irq / st_sem
镜像 193,120 B（0x2f2a0），IRAM 未增长（新代码全在 flash）
```

FPU 指令条数是唯一能在静态层面确认"测试真的在测 FPU"的证据，所以特意查了。

### 10.4 一个反直觉的决定：波特率不做

原计划 P2 里把波特率做成可配。核对后发现：

- `uart_ll_set_baudrate(hw, baud, sclk_freq)` 需要**时钟源频率**
- S31 的 `uart_ll.h` **没有** `uart_ll_get_sclk()` / `uart_ll_set_sclk()`
  （只有 LP UART 有），主 UART 的时钟源选择在 `HP_SYS_CLKRST`，没有 LL 封装
- 用"已知当前波特率 × 当前分频"反推频率虽然数学上可行，但在没有硬件验证的情况下
  **算错就是静默把控制台波特率改坏**——而控制台是唯一的调试通道

所以改成一个诚实的行为：非 `CONFIG_ESP_CONSOLE_UART_BAUDRATE` 的请求返回
`-RT_EINVAL` 并打印原因。宁可"明确不支持"，不要"看起来支持然后搞坏"。

这条判断标准和 R9 那次是同一个：**没有验证手段时，选择失败可见的方案。**

---

## 11. P2 第五批：中断嵌套与 Wi-Fi/BLE 可行性实测（2026-10-27，`da2eee08`）

### 11.1 中断嵌套：机制早就写好了，只是从没被执行过

读 `vectors_rtthread.S` 才发现，`SOC_INT_HW_NESTED_SUPPORTED` 分支下的嵌套逻辑
是**完整实现**的：

```asm
csrr  s1, mcause                 /* 本层 mcause 存 s1 */
csrr  s2, mstatus
...
li    t0, 0x8
csrrs t0, mstatus, t0            /* 置 MIE，允许嵌套 */
jal   _global_interrupt_handler
csrrc t0, mstatus, t0            /* 关 MIE，防止切换期间再进中断 */
...
csrw  mcause, s1                 /* mret 前恢复本层 mcause（嵌套会覆盖它）*/
```

问题不在实现，而在**从来没跑过两个优先级**：`rt_hw_interrupt_install()` 一律给 1 级，
同级别不能互相抢占，所以"系统没有中断嵌套"这件事一直是真的——但那是配置造成的，
不是能力缺失（上一轮把它记成 R17 是对的，只是没验证过另一面）。

### 11.2 怎么在免接线的前提下造出两个优先级

S31 有 4 条 GPIO 中断通道（`ETS_GPIO_INTR0..3_SOURCE`），这个新特性正好用上：

| 角色 | 通道 | CPU 线 | 级别 | 由谁配置 |
|---|---|---|---|---|
| 外层 | channel 0 | `ETS_GPIO_INUM`(4) | 1（install 默认） | `rt_pin_ops` |
| 内层 | channel 1 | INUM 6（自选） | 2（显式设置） | 用例直接写寄存器 |

`int_ena` 的位选择通道：`GPIO_LL_INTR0_ENA = BIT(0)`、`GPIO_LL_INTR1_ENA = BIT(1)`
（注意没有 BIT(2)：`INTR2 = BIT(3)`、`INTR3 = BIT(4)`，位号不连续）。
状态读 `intr_1`（脚 0–31）/ `intr_11`（脚 32–63），清除仍写 `status_w1tc` / `status1_w1tc`。

触发流程完全不需要外部信号：

```
线程写 outer 脚 status_w1ts
   → channel 0 → INUM 4（1 级）→ 外层 handler 进入，nest=1
      → 外层 handler 里写 inner 脚 status_w1ts
         → channel 1 → INUM 6（2 级）→ 抢占外层，内层 handler 进入，nest=2
      → 内层返回，外层继续
   → 外层返回，nest=0
```

断言两半（外层 == 1、内层 == 2），100 轮。**只断言"内层进了"是不够的**——
如果内层是在外层返回之后才执行，`inner` 计数同样是 100，但根本没有嵌套。
所以 `nest` 的值才是判据，计数只是配平。

### 11.3 Wi-Fi / BLE：这次是"坏消息"被证伪

原方案 R6 的核心论据是"射频闭源库内部依赖 FreeRTOS 语义"。这句话**是猜的**，
从来没验证过。这次直接查符号表：

```bash
cd components/esp_wifi/lib/esp32s31
riscv32-esp-elf-nm -u *.a | awk '{print $2}' | sort -u > /tmp/u.txt
riscv32-esp-elf-nm --defined-only *.a | awk '{print $3}' | sort -u > /tmp/d.txt
comm -23 /tmp/u.txt /tmp/d.txt        # 真正的对外依赖
```

结果：

| 库集合 | 大小 | 真正外部符号 | FreeRTOS 引用 |
|---|---|---|---|
| Wi-Fi（7 个库） | 4.0 MB | 219 | **0** |
| BLE 控制器（3 个库） | 4.7 MB | 67 | **0** |

219 个的构成：libgcc 软浮点/64 位辅助 28 个、IDF `esp_*` 24 个、PHY `phy_*`/`ant_*` 12 个、
其余是库集合内部或小写静态符号。**一个 FreeRTOS API 都没有。**

这个结果的含义：射频固件是 OS 无关的，它只通过 `wifi_osi_funcs_t` 那 60 个函数指针
回调上来。而那 60 个函数指针的实现在 `esp_adapter.c`，**是我们自己的 C 源码**。

### 11.4 一个之前没注意的既有事实

查 `s31_rtt_test.map` 发现当前镜像**已经链接了 10 个 FreeRTOS 对象**：

```
heap_idf.c  idf_additions.c  list.c  port.c  port_common.c
port_systick.c  portasm.S  queue.c  stream_buffer.c  tasks.c
```

也就是说这个"RT-Thread 镜像"里一直住着一个完整的 FreeRTOS 内核。tick 就是从
`port_systick.c` 来的：SYSTIMER alarm → `SysTickIsrHandler` → `xPortSysTickHandler`
→ `rt_tick_increase()`。`queue.c`/`tasks.c` 被拉进来是因为 `port.c` 引用了它们。

这件事改变了 Wi-Fi 的架构结论：**不能"把 FreeRTOS 拿掉"**，因为 tick 和临界区都在里面。
正确做法是**保留 port 层，只替换调度器**（`tasks.c`/`queue.c`/`timers.c`/`event_groups.c`）。

### 11.5 需要写的东西：30 个 API，不是 831 行

以 FreeRTOS 内核源码里定义的 214 个函数名为全集，与各组件的实际引用取交集：

| 组件 | API 数 |
|---|---|
| `lwip` | 21 |
| `esp_wifi` | 19 |
| `esp_event` | 15 |
| `esp_netif` | 11 |
| `esp_driver_uart` | 10 |
| `esp_timer` | 9 |
| `esp_driver_gpio` | 5 |
| `esp_phy` | 4 |
| `esp_coex` | 3 |
| 并集 | **33** |

其中 3 个（`xPortCanYield` / `xPortGetCoreID` / `xPortInIsrContext`）由**保留的**
`portmacro.h` 与 `port.c` 提供，实际要写 **30 个**：队列 13、信号量 1（+4 个宏）、
任务 11、事件组 5。

完整分析与架构建议见 `rtthread-esp32-s31-wifi-feasibility.md`。

### 11.6 为什么建议"内部 `ft_*` + 薄封装"两层

直接提供 `xQueueCreate` 会和 `libfreertos.a` 冲突，谁赢取决于链接顺序，不可靠。
所以：


- `ft_queue.c` / `ft_sem.c` / `ft_task.c` / `ft_event.c` 用 `ft_*` 命名，**始终编译**，
  因此 `s31test fbshim` 可以在板子上直接验证语义（排队、超时、ISR 版本、事件组位清除）。
- `freertos_api.c` 只在 Kconfig 打开时编译，负责把 FreeRTOS 名字映射到 `ft_*`，
  同时 `components/freertos` 不再编译 `tasks/queue/timers/event_groups/stream_buffer`。

好处是：现在就能写、能编、能在硬件上测，镜像不受影响；启用只改一个 Kconfig；
关掉时行为与今天完全一致。

### 11.7 本轮产物

| 项 | 值 |
|---|---|
| 镜像 | 194,720 B（0x2f8a0） |
| 新增代码 | `s31test nest`（约 200 行） |
| 新增文档 | `rtthread-esp32-s31-wifi-feasibility.md` |
| 累计提交 | 10 个 |

**最重要的一条**：R6 从"高"降为"中"，Wi-Fi 的工作量从"翻倍且成功率不高"
修正为 3~5 人周，BLE 再加 1~2 人周。剩余不可控项只有 PHY 校准与射频时序。

---

## 12. FreeRTOS 兼容层 W1（2026-10-27，`9b99bc89` + `df3e3d0e`）

### 12.1 起点：上一轮证明射频库与 OS 无关

上一轮查完符号表：7 个 Wi-Fi 库 + 3 个 BLE 控制器库，FreeRTOS 引用 **0**。
所以跑 Wi-Fi 的全部障碍只剩 IDF C 层调的那批 FreeRTOS API。这一轮就是把它们写出来。

### 12.2 一个差点把工作量估错一半的方法论错误

第一版清单是 grep 组件源码得到的：**33 个**。

后来换了个更硬的测法——把

```
tasks.c  queue.c  timers.c  event_groups.c  stream_buffer.c
```

从 `components/freertos/CMakeLists.txt` 里删掉，让**链接器**报错：

```
真正需要 20 个，其中 13 个不在 grep 的 33 个里
```

漏掉的原因很朴素：我 grep 的名单里有 `esp_wifi`、`lwip`、`esp_event`、`esp_netif`、
`esp_timer`、`esp_driver_*`，但**没有** `spi_flash`、`esp_libc`、`esp_coex`——
而这三个是**永远会链接进来的**。它们要的是：

| 组件 | 需要的符号 |
|---|---|
| `spi_flash` | `xTaskGetSchedulerState` `vTaskSuspendAll` `xTaskResumeAll` `xQueueGenericCreate` `xQueueSemaphoreTake` `xQueueGenericSend` |
| `esp_libc`（newlib 锁层） | `xQueueCreateMutex` `xQueueCreateMutexStatic` `xQueueTakeMutexRecursive` `xQueueGiveMutexRecursive` `xQueueGetMutexHolder` `xQueueGiveFromISR` `xQueueReceiveFromISR` `vQueueDelete` |
| `esp_coex` | `xQueueCreateCountingSemaphore` |

并集 46 个，减去 `port.c`/`portmacro.h` 白送的 3 个（`xPortCanYield` /
`xPortGetCoreID` / `xPortInIsrContext`），**实际要写 43 个**（原估 30）。

**结论：数 API 不能 grep，要让链接器说话。** 而且每加入一个组件
（lwIP、esp_netif、esp_wifi）都要重跑一次这个方法。

### 12.3 同一个实验顺手验证了架构

删掉调度器后，`port.c`、`port_systick.c`、`port_common.c` **零未定义引用**。

这条很关键：之前"保留 port 层、只换调度器"只是我在文档里的推断，
没有任何证据。现在有证据了——`--gc-sections` 会把 `xPortStartScheduler` 这类只在
FreeRTOS 内部用的函数整段丢掉，连带它们的未定义引用一起消失，所以
`vPortEnterCritical`（临界区）、`vPortSetupTimer`（tick）、`heap_idf`（内存）
都能独立存活。

### 12.4 实现结构

```
port/freertos/
├── ft_core.h     内部 API，不含任何 FreeRTOS 头（这才能与 libfreertos.a 共存）
├── ft_ipc.c      队列 / 信号量 / 互斥量（普通 + 递归）
├── ft_event.c    事件组
└── ft_task.c     任务、优先级映射、TLS、调度器锁
```

`freertos_api.c`（`xQueueCreate` → `ft_ipc_create` 的薄封装）**没写**，因为它一旦
定义 `xQueueCreate` 就会和 `libfreertos.a` 里的同名符号打架，谁赢取决于归档顺序。
它要等 Kconfig 把真内核关掉之后才编译——那是 W3。

### 12.5 四个实现上真正的坑

| # | 坑 | 处理 |
|---|---|---|
| 1 | **RT-Thread 的 `rt_mutex` 天生可递归**，FreeRTOS 的普通互斥量不是。直接透传会让"同线程重复 take"静默成功 | 普通互斥量加 `owner` 字段做守卫；只有 `xSemaphoreCreateRecursiveMutex` 走 `rt_mutex` 的自递归 |
| 2 | **`rt_event_control(RT_IPC_CMD_RESET)` 清的是整个事件集**（`src/ipc.c:2312` 直接 `event->set = 0`），无法实现 `xEventGroupClearBits(mask)` | 部分清除改为取 `event->spinlock` 自己改位 |
| 3 | **静态创建的控制块尺寸** | 尺寸不足直接返回 NULL。把 160 字节对象写进调用方几十字节的 `StaticQueue_t` 会越界，症状几分钟后才在别处出现 |
| 4 | **`pxCurrentTCBs` 不是 API** | 它是 FreeRTOS 内部变量，`portmacro.h` 的内联函数直接引用，漏了会链接失败 |

另外两个映射容易写反：

- **优先级方向**：FreeRTOS 数字越大越高，RT-Thread 相反。换算只在
  `ft_prio_to_rt()`/`ft_prio_to_ft()` 两处，散开必错。
- **栈单位**：`xTaskCreate` 传的是 `StackType_t`（这里 32 位字），
  `rt_thread_create` 收字节。忘记 ×4 会得到一个只有四分之一栈的任务，
  通常能撑到第一次深调用才炸。

### 12.6 自检：`s31test fbshim`

六个子项，全部免硬件：

| 子项 | 验的语义 |
|---|---|
| `fbshim/q` | 容量 4、FIFO 顺序、满时拒绝、空时拒绝、计数一致 |
| `fbshim/blk` | 生产者（队列深度 2）+ 消费者跨线程阻塞与唤醒，载荷逐字节校验 |
| `fbshim/to` | 空队列上 50 ms 超时的实际耗时（40~500 ms 之间才算过） |
| `fbshim/sem` | 计数信号量上限 3、普通互斥量不可递归、递归互斥量可重入、owner 查询、调度器锁状态 |
| `fbshim/ev` | OR/AND 等待、部分清除不动其它位、clear-on-exit 后归零 |
| `fbshim/tls` | 按线程隔离的槽位，未写过的读回 NULL |

这一组失败基本等价于"未来接在这层上的 `esp_driver_uart` / Wi-Fi 也会失败"，
所以它的价值比普通单元测试高——它是整条 Wi-Fi 路线的前哨。

### 12.7 产物

| 项 | 值 |
|---|---|
| 镜像 | 199,808 B（0x30c80），分区剩余 81% |
| 兼容层 | 约 1100 行 |
| 新增自检 | 6 个子项，`s31test` 总计 13 项 |
| 累计提交 | 12 个 |

**下一步（W3）**：写 `freertos_api.c` + Kconfig 开关，然后用 §12.2 那个实验方法
把"删掉调度器 + 打开兼容层"整个链接一遍——这一步**完全不需要硬件**，
链接通过就等于 43 个 API 全部对上号了。

---

## 13. 兼容层开关与薄封装（2026-10-27，`262ea9a8`）

### 13.1 做了什么

`freertos_api.c`（334 行）把 43 个 FreeRTOS 名字映射到 `ft_*`，外加
`CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM`：打开后 `components/freertos`
不再编译调度器，只留 port 层。

### 13.2 链接验证：这是本轮最有价值的一条结论

打开开关后整个镜像**链接通过**，而且不是"勉强通过"：

```
镜像     199,808 B → 192,800 B（−7 KB）
libfreertos.a 只剩  port.c  port_systick.c  portasm.S  list.c
                    heap_idf.c  idf_additions.c
xQueueGenericSend       → 0x4000ce94（我们的）
xQueueSemaphoreTake     → 0x4000ceb8
xTaskGetSchedulerState  → 0x4000cf46
vTaskSwitchContext      → 0x4000cf7e
pxCurrentTCBs           → BSS
```

关掉开关后镜像回到 199,808 B，与改动前逐字节一致。

**这意味着 43 个 API 的名字、签名、类型全部对上了号**——不需要硬件。
剩下没有验证的只有语义，而语义已经由 `s31test fbshim` 覆盖（虽然同样要上板）。

### 13.3 为什么默认关闭

打开后跑在这层上的是：`esp_libc` 的 newlib 锁（printf/malloc 的互斥）、
`spi_flash`（每次 flash 访问）、`esp_coex`。**三个都在启动路径上。**

所以默认关闭，并把上板顺序写成两步：先关着验证 `s31test fbshim`，
再打开验证启动。一步到位的话，出问题时连打印都没有。

### 13.4 编译器逼出来的两条

| 报错 | 原因 |
|---|---|
| `redefinition of 'xTaskCreate'` | `task.h` 里它是 `static inline`，转发到 `xTaskCreatePinnedToCore`。只有 Pinned 版是真实符号 |
| `incompatible return type 'QueueHandle_t'` | 它是 `struct QueueDefinition *`，`EventGroupHandle_t` 同理，工厂函数返回 `ft_ipc*` 必须显式转换 |

如果我按"FreeRTOS 手册"写，第一条会直接编译失败——这算是编译器帮了忙。
第二条则是自己写出 `void*` 幻觉的典型：`QueueHandle_t` 看起来像 `void*`，其实不是。

### 13.5 又修了一个"不会崩所以不会被发现"的错误

`StackType_t` 在 IDF 的 RISC-V 端口里是 **`uint8_t`**（`portmacro.h:101`）。
FreeRTOS 手册说 `xTaskCreate` 的栈深单位是"字"，**在这个端口上不成立——是字节**。

上一版我按 ×4 换算，于是每个任务实际拿到四倍于请求的栈。它不会崩，
所以会顺利通过代码评审、通过编译、通过任何不测内存占用的检查。
现在 `freertos_api.c` 里有：

```c
_Static_assert(sizeof(StackType_t) == FT_STACK_UNIT_BYTES,
               "ft_core.h FT_STACK_UNIT_BYTES no longer matches StackType_t");
```

将来端口换成 32 位 `StackType_t` 会直接编译失败，而不是悄悄多分四倍内存。

### 13.6 产物

| 项 | 值 |
|---|---|
| 开关关闭 / 打开 | 199,808 B / 192,800 B |
| 兼容层总代码 | 约 1440 行 |
| 累计提交 | 13 个 |

**下一步是 W4**：打开 `esp_driver_uart`，用 `uart_driver_install` 替换现有 LL 串口驱动。
那是第一次真正让 IDF 的驱动跑在兼容层上——`uart.c` 里有 `xSemaphoreTake` /
`xQueueSendFromISR` / `xTaskGetTickCount`，正好是兼容层覆盖的三个面。

---

## 14. W4：`esp_driver_uart` 的结论是"不能"，以及由此得到的四件东西（2026-10-27）

### 14.1 用链接器问出真实缺口

W4 的目标是让 `uart_driver_install` 跑在兼容层上。做法和 W1 一样——把它放进链接，
让链接器说话。只报 4 个符号：

```
ringbuf.c:1083  undefined reference to `xTaskRemoveFromEventList'
ringbuf.c:1239  undefined reference to `xTaskRemoveFromEventList'
idf_additions.c:245  undefined reference to `xQueueGenericCreateStatic'
idf_additions.c:273  undefined reference to `xQueueGenericGetStaticBuffers'
idf_additions.c:310  undefined reference to `xQueueCreateCountingSemaphoreStatic'
```

三个静态创建是送分的。第一个不是。

### 14.2 为什么 `xTaskRemoveFromEventList` 是死路

`esp_ringbuf/ringbuf.c` 的阻塞循环是这么写的：

```c
while (!exit) {
    portENTER_CRITICAL(&pxRingbuffer->mux);      // 782 行
    if (能装下) { ...; goto loop_end; }
    ...
    vTaskPlaceOnEventList(&xTasksWaitingToSend, ticks);   // 816 行，在临界区里
    portYIELD_WITHIN_API();
loop_end:
    portEXIT_CRITICAL(&pxRingbuffer->mux);       // 822 行
}
```

`vPortEnterCritical()` 在 RT-Thread 分支下做了两件事（`port.c:626`）：

```c
portSET_INTERRUPT_MASK_FROM_ISR();
rt_enter_critical();
```

而 `rt_schedule()` 在 `rt_scheduler_lock_nest != 0` 时直接返回（`scheduler_up.c:296`）。
于是 `_rt_sem_take()` 会：置 `thread->error = RT_EINTR` → 把自己挂到信号量等待链 →
调 `rt_schedule()`（空操作）→ 看到 error 非零 → 返回 `-RT_EINTR`。
**线程既没阻塞成功，也已经不在就绪队列上。** 下一次重调度就再也回不来。

FreeRTOS 没这个问题，因为 `vTaskPlaceOnEventList` 在那里**不切换**——只打标记就返回，
真正的切换推迟到临界区退出。RT-Thread 的 `rt_sem_take` 是就地阻塞。两种模型不兼容。

**这不是"再补一个 API"，是阻塞原语的语义差异。** 所以 W4 判定为不可行（方案 C），
并把这条写进可行性文档 §4.2，包括 A/B 两种绕法的代价。

### 14.3 顺带确认的影响面

```bash
grep -rl "xRingbuffer" --include=*.c components/esp_wifi components/lwip \
  components/esp_netif components/esp_event components/esp_coex     # 空
```

`esp_ringbuf` 只出现在外设驱动和 Bluedroid 的 SPP profile 里。
**Wi-Fi 与 NimBLE 不受影响**，受影响的是"复用 IDF 外设驱动"这条路。

### 14.4 本轮仍然交付了四件东西

1. **`ft_notify.c`（约 300 行）**：任务通知 + 超时。
   `esp_timer` / `esp_eth` / `esp_ipc`（7 个文件）用 `ulTaskNotifyTake`，
   `stream_buffer` 用 `xTaskNotify(eNoAction)` + `xTaskNotifyWait`。
   实现是"计数信号量 + 位负载"：`eIncrement`/`eNoAction` 释放信号量，位操作走 `value`。
2. **静态创建落地**：`xQueueGenericCreateStatic` / `xQueueGenericGetStaticBuffers` /
   `xQueueCreateCountingSemaphoreStatic`。原来 `xQueueCreateMutexStatic` 是"打印警告后
   改用堆"，把调用方的 `StaticQueue_t` 白扔了；现在改成"用调用方的存储区，
   控制块原样记住"，`vQueueDeleteWithCaps` 释放时两边都不漏。
3. **`ft_block_allowed()` 守卫**：把 14.2 那个失败模式从"静默永久挂起"变成
   `FT_ERR_STATE` + 一次打印。守卫只在会真正阻塞的路径上检查（先 `trytake`），
   所以临界区里的非阻塞调用照旧可用。`s31test fbshim/guard` 验证这一点。
4. **`stream_buffer.c` 在开关打开时保留**：它不引用 `pxCurrentTCBs` 或任何调度器内部，
   只需通知 API。留着可以让 `xStreamBuffer*` 与 `idf_additions` 的
   `xStreamBufferCreateWithCaps` 不变成未定义符号。

### 14.5 又一次"编译器才知道"

| 报错 | 原因 |
|---|---|
| `task.h` 成片 `expected declaration specifiers before '(' token` | `xTaskNotify` / `xTaskNotifyWait` / `ulTaskNotifyTake` / `xTaskNotifyGive` / `vTaskNotifyGiveFromISR` 全是**函数式宏**。只能定义 `xTaskGenericNotify` 系列 |
| `passing argument 2 of 'ft_timeout_check' from incompatible pointer type` | `TickType_t`=`unsigned int`，`rt_uint32_t`=`unsigned long`。同宽不同型，指针不能互转 |

这两条和之前 `xTaskCreate` 是 `static inline`、`StackType_t` 是 `uint8_t` 属于同一类：
**FreeRTOS 的公开 API 名字有一半不是符号，是宏。**

### 14.6 产物

| 项 | 值 |
|---|---|
| 开关打开 | **192,880 B**（`0x2f170`），比关闭时小 6.9 KB |
| 开关关闭 | 199,808 B，与之前一致 |
| `libfreertos.a` 贡献（开关打开） | `port.c` `port_systick.c` `portasm.S` `list.c` `heap_idf.c` `idf_additions.c` `stream_buffer.c`（0 字节，全被 gc） |
| 兼容层规模 | `ft_*` ≈ 1100 行 + `freertos_api.c` 约 500 行 + `ft_notify.c` 约 300 行 |
| `s31test` 子项 | 14 项（`fbshim` 7 项） |

### 14.7 下一步不再是 W4

Wi-Fi 链路上真正没验的是 `esp_event` + `esp_netif`（W5），它们不用 ring buffer，
可以直接在兼容层上起。外设驱动那条路已经明确留给 LL 层。

---

## 15. W5：`esp_event` + `esp_netif` 跑起来了（2026-10-27）

### 15.1 只差一个符号

把 `esp_event_loop_create_default()` 和 `esp_netif_init()` 推进链接，
链接器只报一个缺口：

```
undefined reference to `vTaskSetThreadLocalStoragePointerAndDelCallback'
```

这是 IDF 在 FreeRTOS 之上加的扩展（`idf_additions.h:240`，受
`CONFIG_FREERTOS_TLSP_DELETION_CALLBACKS` 控制），作用是"把 malloc 出来的状态
挂在任务的 TLS 槽上，任务删除时回调释放"。`esp_event` 用它挂事件循环的
每任务状态，`pthread_local_storage.c` 也用它。

我们原来只实现了 `vTaskSetThreadLocalStoragePointer`（纯指针），
没有删除回调那一半。

### 15.2 补法：TLS 侧表多一列

`struct ft_tls_entry` 加 `ft_tls_del_cb_t cb[FT_TLS_SLOTS]`，并在
`ft_task_delete()` 里、`rt_thread_delete()` **之前**遍历调用（对应 FreeRTOS 的
`prvDeleteTCB()`）。回调执行时打开中断——它是组件代码，可能 malloc/free，
不能在关中断状态下跑。


一个顺序细节：`ft_task_delete` 在文件里排在 TLS 表之前，所以加了一个
`static void ft_tls_release(rt_thread_t);` 前向声明，函数体仍放在表后面。

### 15.3 实际走到的路径（比预想的窄）

`esp_event.c:595` 在 `!CONFIG_ESP_EVENT_LOOP_IN_EXT_RAM`（本配置就是）下用的是
**普通** `xTaskCreatePinnedToCore`，不是 `...WithCaps`。所以：

| 调用 | 落到 |
|---|---|
| `xTaskCreatePinnedToCore` | `ft_task_create` |
| `xQueueCreateWithCaps` | `xQueueGenericCreateStatic` → `ft_ipc_create_static_ram` |
| `vTaskSetThreadLocalStoragePointerAndDelCallback` | 本轮新增 |
| `esp_netif` 的 `xSemaphoreCreateBinary` / `xQueueSemaphoreTake` | `ft_ipc_create` / `ft_ipc_take` |

`xTaskCreateStaticPinnedToCore` 本轮没被需要。打开 EXT_RAM 那条分支就会需要，
实现套路和静态队列一样（用调用方的栈，自己分配 `rt_thread`）。

### 15.4 运行时验证命令

`s31events`（只在开关打开时编译）：建默认事件循环 → `esp_netif_init()` →
注册处理器 → 投递带载荷事件 → 等处理器被调用。

```
s31events: PASS, delivered in 20 ms, payload=0x5a31
```

PASS 同时证明三件事：**任务真的被创建并运行了、队列真的传了数据、
TLS 删除回调真的挂上了**。这是第一次用真实 IDF 组件验证兼容层，而不是用 API 单测。

### 15.5 顺手保住的一个性质

一开始怀疑 +192 B 是 `esp_event` 的 `PRIV_REQUIRES esp_timer` 带进来的，
于是把两个组件做成条件依赖（`if(CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM)`）。
结果两头都不对：

1. 条件化之后大小**一点没变**，说明那 192 B 不是 `esp_event` 带来的；
2. 这个宏在 `main/CMakeLists.txt` 被求值时**不可见**，块静默取 false，
   开关打开时 `main.c` 直接找不到 `esp_event.h`。

所以改回**无条件** REQUIRES。不需要条件：关闭时 `s31events` 整段被编译掉，
`--gc-sections` 会把两个组件丢掉，`nm` 验证默认镜像里 0 个 `esp_event` 符号。
那 192 B 的真实来源是兼容层新增的 TLS 删除回调（`ft_task.c` 不随开关编译）。

| 配置 | 镜像 | 说明 |
|---|---|---|
| 开关关闭（默认） | 200,688 B | 比 W5 之前 +192 B，来自兼容层自身的 TLS 删除回调 |
| 开关打开 | 298,576 B | `esp_event` + `esp_netif` + `s31events` 全在里面 |

### 15.6 下一步

W6 是 `esp_wifi` 起来（STA 扫描），那一步开始碰 PHY，必须上板。
在这之前还能离线做的：`esp_netif_new()` + `esp_netif_create_default_wifi_sta()` 的
链接验证（会牵出 `lwip`，那才是下一个真实依赖面）。

### 15.7 W5.5：lwIP 也链接通过，零额外工作

W5 之后顺手问了一句"netif 配上之后 lwIP 能不能进来"。探针只有两行：

```c
#include "esp_netif.h"
esp_netif_t *probe(void) { return esp_netif_new(NULL); }
```

结果 **零未定义符号**，`liblwip.a` 有 **42 个目标文件**进镜像，包含三处耦合最深的地方：

```
liblwip.a(tcpip.c.obj)     tcpip 线程与消息邮箱
liblwip.a(sys_arch.c.obj)  sys_sem / sys_mutex / sys_thread 适配层
liblwip.a(api_msg.c.obj)   netconn API
```

`sys_thread_new`、`sys_mutex_lock`、`tcpip_init`、`netif_add`、`udp_new`
都在符号表里。

**结论：44 个 API 已经覆盖整个网络栈，不需要为 lwIP 补任何符号。**
这是链接级结论——`tcpip_init` 真的建线程、建邮箱仍要上板。

探针已删除，镜像回到 200,688 B。


## 16. W6：PWM（LEDC）与通用定时器（TIMG）（`210221c5`）

### 16.1 为什么是这两个

UART + GPIO 只是能让 shell 跑起来的最小集合。下一个该补谁，判据不是"常用"，而是
**RT-Thread 有没有现成的接口可以对接**：

- PWM：`components/drivers/misc/rt_drv_pwm.c` 是活的，`struct rt_pwm_ops` +
  纳秒制 `rt_pwm_configuration`。驱动注册完就得到一个标准 `pwm0` 设备。
- 定时器：**没有**。5.x 把 hwtimer 删了，`components/drivers/` 下只剩 hwtimer
  的文档引用，代码不存在。所以这是直接 API（`s31_timer_start/stop/count`），
  不假装自己是框架。

### 16.2 LEDC：三个静默失败点

1. **速度模式只有一个，实例有两个**。`SOC_LEDC_SUPPORT_HS_MODE` 未定义 →
   `ledc_mode_t` = `{LOW = 0, MAX = 1}`；但 `LEDC_LL_GROUP_NUM == 2`，LEDC0/LEDC1
   是两块独立寄存器，通过 `LEDC_LL_GET_HW(group)` 取。把 group 当成 mode 传进去，
   就会越过 `channel_group[1]` 的边界。
2. **信号表只填 `speed_mode[0]`**（两个 group 都是）。读 `[1]` 返回 0，而 0 是合法
   索引——通道配置全对、寄存器读回全对，就是驱动不到目标引脚。
3. **分频是 8.8 定点**。整数 `<< 8`，小数在低 8 位。

`S31_LEDC_MODE` 由 `SOC_LEDC_SUPPORT_HS_MODE` 定义，第一条错误写不出来；第二条靠
`esp_rom_gpio_connect_out_signal` 只从 `[S31_LEDC_MODE]` 取值来约束。

另外：`ledc_ll_enable_bus_clock` / `ledc_ll_enable_clock` / `ledc_ll_set_slow_clk_sel`
都被宏包了，展开时要 `__DECLARE_RCC_ATOMIC_ENV` 在作用域内。照 IDF 的写法用
`PERIPH_RCC_ATOMIC() { ... }` 包起来。

### 16.3 PWM 的框架语义要补两处

`rt_pwm_set_period()` / `rt_pwm_set_pulse()` 发出的命令**只带变化的那一个字段**，
框架自己不缓存。所以驱动里留了 `s_state[ch] = {period, pulse, enabled}`：

- `PWM_CMD_SET` 两个都存；`SET_PERIOD` 更新周期并把脉宽钳到周期以内；
  `SET_PULSE` 只用新脉宽配旧周期；`GET` 从缓存回答。

这样三个 setter 走同一条 `s31_pwm_apply()`，行为一致。占空比 100% 钳到
`precision - 1`——给满量程输出就不翻转了。

### 16.4 TIMG：中断源踩了一次

第一版按"每组一条中断线"写：只申请 `ETS_TIMERGRP0_T0`，ISR 读组状态寄存器分派给
同组两个定时器。实际 S31 上**每个定时器有独立中断源**
（`ETS_TIMERGRP0_T0` / `T0` 的 `T1` / `TIMERGRP1_T0` / `T1`），只开 T0 时 T1 的
告警永远进不来。

改成每通道一个 `intr_handle_t`。ISR 仍然读组状态寄存器（它是每组的），但只清
|  |  |
| --- | --- |
`pending = status & (ALARM(0) | ALARM(1))`——全量写回会把同组另一个定时器的
pending 一起清掉。

频率测量用 SYSTIMER（独立外设、1 MHz 自由计数），不用 `rt_tick_get()`。

### 16.5 构建系统：PRIV_REQUIRES 不生效

把 `esp_hal_ledc` / `esp_hal_timg` 加进 `PRIV_REQUIRES` 之后，
`build/compile_commands.json` 里本组件的 `-I` 列表**一个都没多**，`pwm.c` 直接
`fatal error: hal/ledc_ll.h: No such file or directory`。

原因：这两个组件不在 `hal` 伞下（`esp_hal_uart` / `esp_hal_gpio` 是），IDF 只通过
`esp_driver_ledc` / `esp_driver_gptimer` 引入它们，而本移植层不用那两个驱动。

改法：

```cmake
target_link_libraries(${COMPONENT_LIB} PRIVATE idf::esp_hal_ledc idf::esp_hal_timg)
```

库里还需要 `ledc_periph_signal`（`ledc_periph.c` 的数据符号），所以链接本身也是必须的。

### 16.6 产物

| 项 | 值 |
|---|---|
| 镜像 | 206,304 B（W5.5 是 200,688，+5,616） |
| 新文件 | `port/esp32s31/pwm.c`、`port/esp32s31/gptimer.c`、两个头 |
| 框架源 | 新增 `rt_drv_pwm.c`，`rtconfig.h` 开 `RT_USING_PWM` |
| 设备 | `pwm0`，8 通道 / 4 定时器 |
| 命令 | `s31pwm`、`s31timer`（加上原有的共 5 个；开关打开时 6 个） |

### 16.7 下一步

I2C 与 SPI 是同样的形状（LL 齐备 + 无框架，要自己写设备类），但价值不如
`esp_wifi` 的缺口分析——后者必须上板，前者上板也只要一根线和一个从机。真要做，
按 I2C → SPI 顺序，因为 I2C 的 LL 没有 DMA 路径。


## 17. W7：`esp_wifi` 链接通过 + `esp_timer` 缺口的发现与修补（2026-10-27，`e3345c31`）

### 17.1 链接：零未定义符号

`esp_netif_create_default_wifi_sta()` + `esp_wifi_init()` + `esp_wifi_set_mode()` +
`esp_wifi_start()`，**零未定义符号**。进镜像：`libnet80211` 43 个、`libpp` 36 个、
`libmesh` 14 个、`libcore` 1 个、`libespnow` 1 个目标文件。

`esp_adapter.c` 的 `wifi_osi_funcs_t` 有 **128 个函数指针**，其中 20 个 FreeRTOS
调用全部落到兼容层（`xQueueGenericCreateStatic` / `xQueueGenericGetStaticBuffers` /
`xQueueTakeMutexRecursive` / `xTaskCreatePinnedToCore` / `xEventGroupWaitBits` …）。

真内核没混进来：`libfreertos.a` 只有 `heap_idf`、`idf_additions`、`list`、`port`、
`port_systick`、`portasm`、`stream_buffer` 七个 obj，`tasks/queue/timers/event_groups.c`
**一个都没有**。

### 17.2 `esp_timer` 缺了一半——链接期完全看不出来

`esp_timer` 分成两半：

- `esp_timer_early_init()`：只启动 SYSTIMER 计数器。**跑了**（CORE 阶段初始化项），
  所以 `esp_timer_get_time()` 一直好用，没有任何症状。
- `esp_timer_init()`：建分发任务 + 装告警 ISR。**没人调用**——全树唯一调用者是
  `ESP_SYSTEM_INIT_FN(esp_timer_init_os)`，被 `#if !CONFIG_IDF_RTOS_RTTHREAD` 包着
  （`esp_timer.c:660`）。IDF 的假设是"RT-Thread 自带定时器"，但那不是 `esp_timer`。

崩溃链：

```
esp_wifi_start → osi._phy_enable → esp_phy_enable        (esp_adapter.c:643,753)
esp_phy_enable → phy_track_pll_init                      (phy_init.c:387, 无条件)
phy_track_pll_init → ESP_ERROR_CHECK(esp_timer_create()) (phy_common.c:126)
esp_timer_create → ESP_ERR_INVALID_STATE                 (esp_timer.c:105, s_timer_task==NULL)
→ abort()
```

**如果只看链接器，W7 会被判定完成。** 镜像能启动、能打印、msh 能响应，第一次
`s31wifi` 才死。

修法：`board.c` 在调度器启动前调 `esp_timer_init()`。可用性检查过：`rt_thread_startup()`
只是挂就绪队列，不需要调度器在跑；`init_timer_task()` 重复调用返回
`ESP_ERR_INVALID_STATE`，幂等。用 `CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM` 包住——
关闭时镜像里没有 `esp_timer_create()` 的调用者，`nm` 确认 `esp_timer_init` 从关闭版里
整个消失（40 KB 堆也确认回到 40 KB）。

### 17.3 `s31wifi.c.obj` 曾经根本没进镜像

msh 命令表是 section 式的，没有代码引用 `cmd_wifi`，链接器不从 `libmain.a` 里拉。
试过加 `-u cmd_wifi`，**无效**：本组件自己的 interface 库被 IDF 追加在链接行**末尾**，
位置在 `libmain.a` 之后，扫描归档时符号还没被引用。改用
`idf_component_register(... WHOLE_ARCHIVE)`。

（`-u main` 之所以有效，是因为它由 `rtthread` 组件种，位置在 `libmain.a` 之前。）

### 17.4 RT-Thread 堆从 40 KB 提到 128 KB

Wi-Fi 静态 RX 缓冲 `10 × 1600 B = 16 KB`，加上 Wi-Fi 任务栈、队列、`libpp` 的
内部池，40 KB 太紧。`CONFIG_IDF_RTTHREAD_HEAP_SIZE` 40960 → 131072。

DRAM 装得下：`heap_end` `0x2f02f580` → `0x2f045580`，DRAM 顶 `0x2f080000`，
还剩 227 KB。

### 17.5 `nvs_flash_init()` 放在最前

不是因为必需——PHY 读不到校准数据会退回全量校准并继续——而是因为不初始化的话
每次启动都要重做一遍校准且存不下来。`s31wifi` 里它失败只打一行提示、不中断。

### 17.6 产物

| 项 | 值 |
|---|---|
| 镜像（开关打开） | 888,112 B |
| 镜像（开关关闭） | 206,592 B |
| 新命令 | `s31wifi` |
| 堆 | 128 KB（`CONFIG_IDF_RTTHREAD_HEAP_SIZE`） |

### 17.7 下一步（上板）

`s31wifi` 分步打印就是为这个准备的。真机上的观察顺序：

1. `nvs_flash_init` 是否 ok（否 → 分区表或 flash 驱动）
2. `esp_wifi_init` 是否 ok（否 → 任务的栈/优先级，或兼容层队列）
3. `esp_wifi_start` 是否 ok（否 → 大概率是 PHY 校准，看 ESP_ERROR_CHECK 的位置）
4. `WIFI_EVENT_STA_START` 是否在 5 s 内到达（否 → Wi-Fi 任务没跑起来）
5. MAC 能否读出（否 → efuse）


## 18. W8：启动链上被跳过的四件事（2026-10-27，`90bf4e31`）

### 18.1 起因

W7 抓到 `esp_timer` 缺一半之后，很自然要问：`main_task()` 里还有几件同型的事。

`startup.c:257` 的 `#if !CONFIG_IDF_RTOS_RTTHREAD` 把整个 `esp_startup_start_app()`
换成 `entry()`，而 `main_task()` 正是在 `esp_startup_start_app()` 里创建的。所以
`main_task` 干的每一件事，这个移植都得不到。逐条核对：

| # | 内容 | 结论 |
|---|---|---|
| 1 | `esp_int_wdt_init` + `esp_int_wdt_cpu_init` | 配置是死的（见 18.2） |
| 2 | `esp_crosscore_int_init` | 单核，不需要 |
| 3 | `xTaskCreatePinnedToCore(main_task)` | RT-Thread main 线程替代 |
| 4 | `reclaim_startup_stack_memory_for_heap` | 20 KB 泄漏（见 18.3） |
| 5 | `esp_task_wdt_init` | 故意不开（见 18.4） |

### 18.2 中断看门狗：配置打开了，代码没启用

`CONFIG_ESP_INT_WDT=y`、`CONFIG_ESP_INT_WDT_TIMEOUT_MS=300`，配置界面上一切正常。
但 `nm` 里 **`esp_int_wdt_init` 计数为 0**——它唯一的调用点是
`esp_startup_start_app()`（`app_startup.c:64`），被跳过了。

启用它安全：`esp_int_wdt_init()` 只碰 TIMG1 的 MWDT 寄存器（与 RT-Thread 的
T0/T1 timer 是不同寄存器块，不冲突）；喂狗用的 tick hook 由
`port_systick.c:210-214` 在 `CONFIG_IDF_RTOS_RTTHREAD` 分支**之前**调用，所以
RT-Thread 的 tick 路径照样喂它。断言
`CONFIG_ESP_INT_WDT_TIMEOUT_MS >= portTICK_PERIOD_MS * 2` 也成立（300 >= 2）。

### 18.3 20 KB DRAM 泄漏

```c
// heap/port/esp32s31/memory_layout.c:76
{ APP_USABLE_DRAM_END, (SOC_DIRAM_DRAM_HIGH - APP_USABLE_DRAM_END), ..., true }
//                                                                       ^ startup_stack

// heap_caps_init.c:164
if (region->startup_stack) {
    heap->heap = NULL;     // "Will be registered when OS scheduler starts"
}
```

那个承诺由 `main_task → reclaim_startup_stack_memory_for_heap() →
heap_caps_enable_nonos_stack_heaps()` 兑现。跳过 `main_task` 就没人兑现。

S31：`APP_USABLE_DRAM_END = SOC_ROM_STACK_START - SOC_ROM_STACK_SIZE =
0x2f07cfb0 - 0x2000 = 0x2f07afb0`，区间到 `0x2f080000`，**正好 20 KB**。

**时机**：区间里包含 `SOC_ROM_STACK`（`0x2f07cfb0..0x2f07efb0`），而
`rt_hw_board_init()` 就跑在那个栈上。所以不能放那里。用 `INIT_APP_EXPORT`：
`rt_components_init()` 在 main 线程里执行，栈已换成链接器分配的，对应 `main_task`
在 FreeRTOS 路径上的同一时刻。验证表项在 `__rt_init_rti_board_end`（`0x400ca958`）
与 `__rt_init_rti_end`（`0x400ca964`）之间，落在 `.flash.rt_thread` 段。

### 18.4 任务看门狗：故意不开

与中断看门狗不同，`esp_task_wdt` **自己不监控任何东西**，只对
`esp_task_wdt_add()` 注册过的任务生效。全树查：本镜像里 `esp_task_wdt_add`
调用点为 0（组件自身除外）。开一个没有订阅者的看门狗只增加失败路径，抓不到任何
问题。不开，并在 `boot_tasks.c` 里写明是决定而非遗漏。

### 18.5 两处自己造成的假通过

**（1）事件循环顺序。** `s31wifi` 原来先注册 handler、从不建默认循环。
`esp_event_handler_register()` 在 `s_default_loop == NULL` 时返回
`ESP_ERR_INVALID_STATE`（`default_event_loop.c:20`），而被 `s31_step()` 当成
"already done" 放过。后果：handler 从未注册、`WIFI_EVENT_STA_START` 无处投递，
真机会等满 5 秒超时后报告"wifi 任务没起来"——**指向完全错误的方向**。


修法：拆成 `s31_step()`（严格，任何非 `ESP_OK` 都失败）和 `s31_step_idem()`
（允许 `ESP_ERR_INVALID_STATE`），只在 nvs/netif/wifi_init 三处用后者；
补上 `esp_event_loop_create_default()`，位置在 handler 注册之前。

**（2）`s31stat` 只报一个堆。** 这个镜像有两个不相交的堆：RT-Thread 的 128 KB
（`CONFIG_IDF_RTTHREAD_HEAP_SIZE`）和 IDF 自己的 214 KB。一个堆里 free 的块，
另一个堆的 `malloc` 看不见。只报一个，就会出现"明明有内存却分配失败"。现在两行
都打印，而且 `esp_get_free_heap_size()` 就是回收是否生效的板上证据。

### 18.6 DRAM 账本（回收后闭合）

| 段 | 大小 |
|---|---|
| `iram0.text` | 63.1 KB |
| `dram0.data` | 17.6 KB |
| `dram0.bss` | 28.5 KB |
| `.stack`（中断栈） | 40.0 KB |
| `.heap`（RT-Thread） | 128.0 KB |
| IDF 堆（已注册） | 214.5 KB |
| 启动栈（回收后入 IDF 堆） | 20.0 KB |
| **合计** | **511.8 / 512 KB** |

### 18.7 产物

镜像 **888,736 B**。新增 `port/esp32s31/boot_tasks.c`（记录被跳过的启动项并启用
中断看门狗）。仍是编译 + 链接级：回收要 `s31stat` 在板上打出
`idfheap: free=...` 才算确认，看门狗要真的制造一次长时间关中断才知有没有效。


## 19. W9：槽位生命周期、idle 档位、以及靠巧合正确的代码（2026-10-27，`6d14da77` + `779339e7`）

W7/W8 处理的是"从来没人做初始化"，这一轮是另一类缺陷：**只在某种情况从不发生时
才对**。链接通过、启动正常、跑几十秒也正常，问题要等系统活够久。

### 19.1 两张按线程指针索引的静态表，都只会增长

| 表 | 用途 | 修之前的释放点 |
|---|---|---|
| `s_tls[16]` | 替代 FreeRTOS TLS | 只有 `ft_task_delete()` |
| `s_notify[16]` | 任务通知的计数信号量 | **没有**（只有惰性初始化时清一次） |

`ft_task_create()` 把 `entry` 原样交给 `rt_thread_create`（`ft_task.c:78`），
没有包壳。任务函数**自然返回**时，RT-Thread 走 `_thread_exit()` →
`rt_thread_defunct_enqueue()`（`src/thread.c:119`），**永远到不了 `ft_task_delete()`**。
FreeRTOS 允许 return，IDF 组件也确实用。

后果一：16 个槽只增不减，满了之后 `ft_task_get_tls()` 静默返回 NULL。

后果二才是危险的：`rt_object_delete()` 释放线程对象（`src/defunct.c:133`），
地址被下一个 `rt_thread_create()` 复用；而 `ft_tls_lookup()` 是裸指针比较
（`ft_task.c:270`）——新线程继承死任务的 TLS 值和**删除回调**，回调拿着别人的
值去 free。

修法：用 `rt_thread_t::cleanup`。回收器在 `src/defunct.c:113` 调用它，时机在
"线程离开调度器之后、对象被释放之前"——指针仍然唯一的唯一窗口。全树确认
RT-Thread 自己只把它清零（`src/thread.c:243`），不占用。`ft_task_delete()` 保留
自己的调用，因为那条路径立即执行而非等 idle 抽干 defunct 队列；重复调用找不到槽
即返回。

通知表加 `ft_notify_forget()`，挂同一个钩子。

### 19.2 优先级映射撞上 idle 档位

```c
// 修之前
return (rt_uint8_t)(RT_THREAD_PRIORITY_MAX - 1u - prio);   // ft 0 -> 31
```

`RT_THREAD_PRIORITY_MAX - 1 = 31` 正是 **idle 的优先级**（`src/idle.c:191`）。
RT-Thread 5.x 对同优先级按时间片轮转（`src/scheduler_comm.c:303`），所以
`tskIDLE_PRIORITY` 任务——在 FreeRTOS 语义里是"没别人能跑时才跑"——每两个 tick
抢走 idle 一次。而 idle 不是装饰：`rt_defunct_execute()` 在它里面
（`src/idle.c:150`），那是释放已结束线程和它们栈的地方。

映射区间下移到 `MAX-2`，不进入 idle 档位。反查加钳位：RT-Thread 定时器线程是
优先级 0（`src/timer.c:47` 默认 `RT_TIMER_THREAD_PRIO 0`），原来报成 ft 30，
超出 `configMAX_PRIORITIES=25`，而调用方会拿它索引数组。

往返映射对 25 个 ft 优先级全部可逆（验证过）。

### 19.3 两处靠巧合正确

**（1）`xQueueGenericSend` 丢弃 `xCopyPosition`。** 只实现 `queueSEND_TO_BACK`。
实测本镜像里 `xQueueSendToFront` / `xQueueOverwrite` / `xQueueSendToBack` 引用数
均为 0，但"要求头插却被静默追加"是数据顺序 bug 且无痕迹。改成警告一次。

**（2）`vTaskDelay(portMAX_DELAY)` 三重巧合。**
`0xFFFFFFFF × 1000 / 1000 = 0xFFFFFFFF` → cast 到 `rt_int32_t` 得 -1 →
`rt_tick_from_millisecond()` 恰好把负数读成 `RT_WAITING_FOREVER`
（`src/clock.c:218`）。第三处源码语义是"无效超时"，不是"永久睡眠"。改成显式
`rt_thread_delay(RT_WAITING_FOREVER)`。

### 19.4 `uxTaskGetStackHighWaterMark()`：从缺失到可实现

`INCLUDE_uxTaskGetStackHighWaterMark` 为 1，`task.h` 向每个调用者声明它，唯一
定义在 `tasks.c`（开 shim 不编译）。本镜像引用数 0，洞一直没被发现。

可以实现，因为**信息已经存在**：`rt_thread_create` 用 `'#'` 填栈
（`src/thread.c:210`），栈向下增长，高水位 = 第一个不是 `'#'` 的字节偏移。
port 层不需要额外记账。

单位是**字节不是字**（`StackType_t` 是 `uint8_t`，`ft_core.h` 已有
`_Static_assert`），IDF 文档写的是 word，所以注释里点明。

### 19.5 新增自检

- **`fbshim/exit`**：任务取 TLS 槽 + 发通知后**用 return 退出**，检查
  `ft_tls_slots_used()` 回到起点。**改之前这个计数只增不减**——第一个能抓住
  该泄漏的测试。
- **`fbshim/hwm`**：高水位非 0 且不超过 `stack_size`。

`ft_tls_slots_used()` 是为这个检查新增的：释放路径只在"任务返回"或"任务被删"
时触发，不上板根本看不见。

### 19.6 产物

镜像 **889,792 B**，构建零告警。仍是编译 + 链接级。


## 20. W10：一轮系统性审计（2026-10-27，`0b815a2d`）

前几轮都是"顺着一条线索往下挖"，这一轮是反过来：**先假设兼容层有洞，然后按
"失败离得多远"排序去找**。

### 20.1 审计方法

对兼容层问四个问题：

| 问题 | 手段 | 本轮找到 |
|---|---|---|
| 头里声明了、有没有定义没实现？ | 扫 `task.h` 等头与 shim 定义对比 | **8 个** |
| 阻塞路径有没有全走保护？ | 列出所有 `rt_*_take`/`delay`，逐个看前置条件 | **3 处** |
| 算术有没有可能回绕？ | 查所有 `length * item_size` 型乘法 | **1 处** |
| 失败是不是静默的？ | 数"返回 0/void 且不打日志"的分支 | **2 处** |

### 20.2 八个头里声明、没人定义的函数

`task.h` 里这些是**无条件声明**的（不像有些带 `INCLUDE_xxx` 守卫），所以任何
组件都能编译通过；而唯一定义在 `tasks.c` 里——开 shim 就不编译。

```
uxTaskGetNumberOfTasks   pcTaskGetName        xTaskGetHandle
xTaskGetIdleTaskHandle   eTaskGetState        xTaskAbortDelay
xTaskResumeFromISR       xTaskDelayUntil
```

本镜像里一个调用者都没有，所以洞一直没被发现：**缺少定义只有在有人用的时候才
可见，而那时刻的报错发生在别人负责的文件里**。

新增 `port/freertos/ft_query.c`。其中 `eTaskGetState` 的映射值得说：FreeRTOS 把
"无限期等待"报成 `eSuspended`，因为它的调度器分不清这和显式挂起；RT-Thread 分得清
（看线程定时器有没有起），所以走另一条路得到同一个答案。

### 20.3 三处阻塞路径没走保护

`ft_ipc.c` 从写下来那天就有这个守卫，注释也写清了原因：`rt_schedule()` 在
`rt_scheduler_lock_nest != 0` 时是空操作（`src/scheduler_up.c:296`），所以线程
被挂起、切不出去、状态还停在 suspend，从此再没有东西唤醒它。

三个地方没享受到这个待遇：

| 位置 | 后果 |
|---|---|
| `ft_task_delay()` | **不返回错误，直接永久挂住** |
| `ft_event_wait_bits()` | 同上，等不到的位 |
| `ft_task_notify_*` | 已正确（有守卫） |

`ft_task_delay()` 这条最严重：它是 `void` 返回值，调用者拿不到错误，只能挂死。

### 20.4 队列大小的整数回绕

`ft_ubase_t` 是 `unsigned long`（32 位），所以 `length * item_size` 会回绕。
65536 项 × 65536 字节 = 2^32 → 向 `rt_malloc` 要 0 字节 → 成功 → 第一次 send
就越界写。FreeRTOS 对同样组合直接返回 NULL。

用 `RT_SIZE_MAX` 而不是 `SIZE_MAX`：`rtdef.h` 已经按目标选好了宽度。

### 20.5 两处静默丢弃改成告警

- **通知表满**：一条发不出去的通知和"对端很慢"完全一样，于是 bug 会在错误的
  组件里被追。
- **`xQueueGenericSend` 非追加位置**：本来就在悄悄做错事。

### 20.6 新增自检

```
fbshim/query  %u tasks, name 'xxx', lookup+state+idle ok
fbshim/guard  加了 ft_task_delay 在临界区内的检查
```

`fbshim/guard` 的新检查**不会静默失败**：没有守卫的话它根本不返回，整个命令超时。

### 20.7 产物

镜像 **890,672 B**。自检项从 8 项（`s31test` 子命令）扩展到 18 个报告点。


## 21. W11：让链接器去枚举缺口（2026-10-27，`7131f65e`）

上一轮（W10）靠**读**找问题。这一轮换了个办法：**让链接器把缺口列出来**。

### 21.1 拉上游，先确认自己没有落后

先把四个仓库的远端都 fetch 了一遍：

| 仓库 | 与上游差距 |
|---|---|
| `esp-idf-rtt` | 我们领先 21 个提交，上游无新提交 |
| `rt-thread` | 0 / 0 |
| `freertos-wrapper` | 0 / 0 |

也就是说**没有可以借的东西**。官方 `FreeRTOS-Wrapper` 是有参考价值的——
它是 RT-Thread 官方的 FreeRTOS ACL，V10.4.6 基线——但它自己也有 11 个
`unimplemented` 桩（`xQueueCreateSet`、`xQueueOverwrite`、`xQueueSelectFromSet` 全在
里面，直接 `configASSERT(0)`）。

差分结果：官方 40 个 API，我们 66 个。我们缺的 8 个里，4 个是函数式宏
（`xQueueOverwrite`、`xQueueOverwriteFromISR`、`xEventGroupClearBitsFromISR`、
`xTaskGetAffinity` 在本树压根没有声明），真正要补的是 4 个。

**所以照抄官方会退步**：官方那些桩在我们这里是能工作的实现。

### 21.2 链接探针：三次才让它真的生效

写一个表，把 IDF 头文件承诺的每个 FreeRTOS 函数都取一次地址。缺定义就在
链接时以名字报错。

**难点全在"怎么让它不被丢掉"，前两次都静默通过：**

| 做法 | 结果 |
|---|---|
| 普通 `const` 数组 | `--gc-sections` 在解析内容**之前**就丢了整段 → 链接照样通过，镜像里连表都没有 |
| `__attribute__((used, retain))` | `used` 只拦编译器；ESP-IDF 的链接脚本里没有这个段的 `KEEP()` → 链接器还是丢 |
| **从 msh 命令表引用**（`KEEP()` 段） | 生效 |

这是个值得记的教训：**验证工具本身要先被验证**。如果我第一次写完就收工，
会得到"链接通过，说明没有缺口"的错误结论，而实际上什么都没检查。

### 21.3 探针一上来就找出 14 个

```
xQueuePeek                  xQueueGenericReset        xQueueCreateSet
xQueueAddToSet              xQueueRemoveFromSet       xQueueSelectFromSet
xQueueSelectFromSetFromISR  uxQueueMessagesWaitingFromISR
xQueueIsQueueFullFromISR    xEventGroupGetBitsFromISR vTaskResume
xTaskCreateStaticPinnedToCore
xTaskGetCurrentTaskHandleForCore   xTaskGetIdleTaskHandleForCore
```

全部补上。两个需要动脑的：

**`xQueueSelectFromSet` 必须是真实现，不能是桩。** 集合的计数器映射成计数信号量
（每次 send 释放一次），所以 select 必须**循环**：触发释放的那个 item 可能在
select 轮到它之前就被别的读者取走，留下一个计数但没东西可返回。只有
`esp_ble_mesh` 和 `esp_ble_iso` 用，两者都不在本镜像里——但桩会把"BLE 还不能用"
变成"BLE 启动就卡住，没有任何信息"。

**`xTaskCreateStaticPinnedToCore` 选择拒绝。** `StaticTask_t` 是 60 字节，存在的
唯一目的是"够大以容纳真正的 TCB"；而 `struct rt_thread` 是它的好几倍。把 rt_thread
写进调用者的块会破坏其后的所有东西。拒绝不是退步——这个函数不存在的时候，调用
根本链接不过。

### 21.4 `xQueueOverwrite`：不只是缺警告，是我自己写错了

W10 把它改成"警告 + 追加"。正确语义是"替换、永不失败、仅限长度 1"。

实现时踩了一个更隐蔽的坑：**空→满的转换里我只加了 `count`，没动 `free_slots`**，
破坏了队列不变量

```
count + free_slots.value == length
```

后果：长度 1 的队列 overwrite 之后，下一次普通 send 会**成功**，往单槽环里塞第二个
item——缓冲区尾部的静默破坏。

自检现在专门测这条：overwrite 之后普通 send 必须被拒（`FT_ERR_FULL`），且
`spaces_available()` 必须是 0。

### 21.5 新增自检

| 项 | 内容 |
|---|---|
| `fbshim/probe` | 表是否真的留在镜像里 |
| `fbshim/qset` | 成员管理、精确通知、移除、陈旧通知 |
| `fbshim/ovr` | overwrite 不增长队列、上面那个不变量、peek 不消费、reset 恢复容量 |

报告点从 12 增到 15。

### 21.6 产物

镜像 **896,288 B**。探针占 72 个指针（288 B）。

---

## 22. W12：第一次上板，30 分钟内连续 6 个缺陷（2026-10-30）

### 22.1 硬件与第一步

板子：ESP32-S31 开发板，串口 **COM16**，115200 8N1，flash **2 MB**，CPU **320 MHz，单核**
（`CONFIG_FREERTOS_UNICORE=y` 生效，CPU1 保持复位）。烧写用 `idf.py -p COM16 flash monitor`。

前 11 轮（W1–W11）全部是静态审查：读代码、比对头文件、写链接探针、反汇编计数指令。
这一轮第一次让镜像真的跑起来，结果 **30 分钟内连续暴露 6 个缺陷**，没有一个能靠前面的
手段看到。下面按出现顺序记录，每个都带真实报错文本和寄存器值。

### 22.2 缺陷一：链接段数超过 bootloader 允许值

**现象**：

```
I (312) boot: Loaded app from partition at offset 0x10000
assert failed: unpack_load_app bootloader_utility.c:833 (rom_index < 2)
```

注意 bootloader 已经打印了 "Loaded app"，说明镜像校验、分区表、flash 读取全部正常，
失败发生在**把段映射进 MMU 的时候**。

**根因**：S31 定义了 `SOC_MMU_DI_VADDR_SHARED=1`——I 总线和 D 总线共用同一段虚拟地址，
因此 bootloader 只允许 **2 个** flash 映射段（`bootloader_utility.c:833` 的 `rom_index < 2`）。

而 P2 首批把 finsh 符号表与自动初始化表放进了独立的 `.flash.rt_thread` 段，位置排在

```
.flash.rodata_noload   (NOBITS，不占文件空间)
.flash.rt_thread       (PROGBITS，__fsymtab / __vsymtab / __rt_init)
```

之后。链接器遇到 NOBITS 之后又来 PROGBITS，**不能合并进同一个 LOAD 段**（文件偏移不连续），
只能新开一个。于是段数从 2 变成 3。

这条在 `nm` 里完全看不出来：所有符号地址都正常，`readelf -l` 里也只是多一行，
没有任何一条"段数超限"的警告。

**修法**：把三个符号表移进 `.flash.rodata` 内部（`components/esp_system/ld/ld.flash.sections`），
删掉 esp32c3 / esp32c6 / esp32s31 三个芯片 `sections.ld.in` 里的独立段定义。

**附带坑**：ldgen 有缓存。改完模板后必须删掉

```
build/esp-idf/esp_system/ld/sections.ld
build/esp-idf/esp_system/ld/sections.ld.fingerprint
build/esp-idf/esp_system/ld/sections.ld.lfcache
```

否则 ninja 打印 `Skipping linker script generation`，把旧脚本原样写回，第二次烧写现象一模一样。
§3.4 记过这件事，这次是同一块石头第二次绊到——第一次是符号 undefined，这次是段数超限，
**症状不同，原因相同**。

**如果没上板会怎样**：这条永远发现不了。镜像 896 KB、ELF 完全合法、`idf.py size` 一切正常，
只有 bootloader 的 ROM 代码知道它装不下。

**事后可加的检查**（当时没有，这次补上）：

```bash
riscv32-esp-elf-readelf -l build/s31_rtt_test.elf | grep -c LOAD    # 应为 2
```

它只数段数，不判断布局是否合理——但恰好能挡住这一类。凡是新增链接片段
（`.flash.*`）的改动，都应该跑一次。

### 22.3 缺陷二：RT-Thread 堆初始化太晚

**现象**：

```
Load access fault
MEPC   : 0x4000xxxx   (rt_object_get_type)
A0     : 0x00000000
MTVAL  : 0x0000000c
```

`MTVAL=0x0c` 是被访问的地址，A0=0 说明基址是 NULL。`rt_object_get_type` 内部偏移 12 取 `type` 字段。

**根因**：`do_core_init()` 的顺序是

```c
do_system_init_fn(CORE);      // ← CORE 阶段初始化项在这里跑
...
rt_system_heap_init(...);     // ← 堆在这之后才就绪
```

而 `esp_libc_init` 在 CORE 阶段，它的 `esp_libc_locks_init` 会
`xSemaphoreCreateMutexStatic` → `ft_ipc_create_static_ram` → `rt_malloc`。此时
`system_heap == NULL`，`rt_smem_alloc` 解引用 `NULL + 12`（`src/mem.c:287`），直接访问异常。


**修法**：`components/esp_system/startup.c` 把 `rt_system_heap_init()` 提到
`do_system_init_fn(CORE)` 之前。

**如果没上板会怎样**：CORE 阶段初始化项的执行顺序是 IDF 的内部约定，头文件里没有声明，
编译期断言也写不出来。静态审查时看 `startup.c` 只觉得"顺序差不多"，只有跑起来才知道
谁先谁后是有约束的。

### 22.4 缺陷三：静态创建 API 的 handle 契约

**现象**：

```
assert failed: esp_libc_locks_init locks.c:422 (handle == (SemaphoreHandle_t) &s_common_mutex)
```

无限重启，连 banner 都出不来。

**根因**：FreeRTOS 的 `xSemaphoreCreateMutexStatic(pcb)` 承诺**返回的 handle 等于调用者传入的
控制块指针**。`esp_libc` 直接 assert 这一点，把这个返回值当作"对象真的建在我给的内存里"的证据。

而 port 之前的做法是照样 `rt_malloc` 一个对象，只在 `static_cb` 里记下调用者的块——返回值是堆地址，
assert 立刻失败。

**关键约束**：`StaticQueue_t` 只有约 **60 字节**，那是为 FreeRTOS 自己的 TCB 预留的占位尺寸；
而 `struct ft_ipc` 需要两个 `rt_semaphore` + 一个 `rt_mutex`，约 **180 字节**。**装不进去**。
W11 对 `xTaskCreateStaticPinnedToCore` 做过同样的判断（拒绝），这里不能拒绝——`esp_libc` 在启动路径上。

**修法**：在调用者的块里放一个两字长的桩：

```c
struct ft_static_stub { rt_uint32_t magic; void *real; };
#define FT_IPC_MAGIC 0x46544950u   /* "FTIP" */
```

真实对象仍在堆上；`ft_ipc_resolve()` 检查首字是否等于 magic，等于才跟随指针。
选这个 magic 是因为真实 `struct ft_ipc` 开头是三个一字节标志，首字不可能凑出 `0x46544950`，
所以"桩"和"真对象"不会互相误认。

所有 API 入口（23 处）从 `(struct ft_ipc *)xQueue` 改成 `ft_ipc_resolve(xQueue)`。

**如果没上板会怎样**：这个 assert 是 `esp_libc` 自己加的，我们没有它的源码习惯，也没在
任何文档里读到"handle 必须等于 pcb"这条契约。**它是 FreeRTOS 的实现细节，不是 API 文档的承诺**，
只有真跑起来才会被下游代码用 assert 强加给你。

### 22.5 缺陷四：调度器状态缺 NOT_STARTED

**现象**：

```
abort() was called at PC 0x4000xxxx on core 0
```

反汇编那个 PC，落在 `locks.c:150`——"递归互斥量在 ISR 上下文被调用"的断言。

**根因**：`esp_libc` 的锁层把 `xTaskGetSchedulerState() == taskSCHEDULER_NOT_STARTED`
当作逃生门：还没开始调度，加锁是空操作（`locks.c:128`、`:137`）。而 shim 只返回
RUNNING / SUSPENDED，**从不返回 NOT_STARTED**。

于是启动早期第一次 libc 加锁走不到逃生门，继续往下判"是否在 ISR"——而 `xPortCanYield()`
在 `rt_thread_self() == NULL` 时返回 false，于是被判成 ISR 上下文，进入 `locks.c:150`。

**修法**：`ft_task_scheduler_state()` 先判断

```c
if (rt_thread_self() == RT_NULL) return taskSCHEDULER_NOT_STARTED;
```

**如果没上板会怎样**：`xTaskGetSchedulerState` 的三个返回值在头文件里都有定义，静态审查时
"实现了两个"看起来完全合理——第三个在运行中的线程看来确实到不了。可行性文档 §5 第 8 条
甚至明确写了"`taskSCHEDULER_NOT_STARTED` 从运行中的线程看是到不了的状态，不需要模拟"。
**这句话本身没错，错在"从运行中的线程看"这个前提**——启动路径上根本没有运行中的线程。

### 22.6 缺陷五：未持有的互斥量被误判为自持有

**现象**：

```
E (321) psa_init: psa_crypto_init failed: -144
E (321) esp_system: init function 'psa_crypto_init' has failed, aborting
```

−144 是 `PSA_ERROR_SERVICE_FAILURE`，往下一层追，来自 `mbedtls_mutex_lock` 失败。

**根因**：`ft_ipc_take()` 里有一句

```c
if (ipc->owner == self) return FT_ERR_TIMEOUT;   /* FreeRTOS 会死锁，这里选择报错 */
```

本意是拦"同一线程重复 take 普通互斥量"。但调度器启动前 `rt_thread_self()` 返回 NULL，
而一个**没人持有**的互斥量 `owner` 也是 NULL。NULL == NULL 成立，于是每一个互斥量都被
判成"你已持有"。

**修法**：

```c
if (ipc->owner != RT_NULL && ipc->owner == self) return FT_ERR_TIMEOUT;
```

**如果没上板会怎样**：这是个典型的**哨兵值冲突**——用 NULL 同时表示"没有线程"和"没有持有者"。
静态审查看那一行，"owner == self 说明重入"读起来是对的。要让这个 bug 显形，需要一个
**在 NULL 线程上下文里拿一个干净互斥量**的场景，而那恰好只在启动早期存在。

### 22.7 缺陷六：调度器启动前拿互斥量会解引用 NULL 线程

**现象**：

```
Store access fault
MEPC   : 0x4000xxxx   (_rt_mutex_take, src/ipc.c:1353)
MTVAL  : 0x0000002c
```

`0x2c` = 44 = `offsetof(struct rt_thread, error)`。

**根因**：`rt_mutex_take()` 无条件写 `thread->error = RT_EOK`，而 `thread = rt_thread_self()`
此时是 NULL。前一个缺陷修完之后，判定不再误判重入，代码终于走到了真正的 take 路径——于是
撞上这一层。

**修法**：`ft_ipc_take()` / `ft_ipc_give()` 在"互斥量类型 + `rt_thread_self() == RT_NULL`"时
直接返回 `FT_OK`。这不是偷懒：FreeRTOS 在这个窗口里"加锁是空操作"正是 `esp_libc` 依赖的语义
（同缺陷四的逃生门）。队列和信号量**不受影响**——`rt_sem_trytake` 不需要线程对象，
无等待的 trytake 可以正常工作。

**如果没上板会怎样**：这是缺陷五的下一层。修完五之后如果不重新烧写，会以为"改一行就好了"。
实际上那个 assert 只是**挡住**了崩溃，真正的访问异常在它后面 44 字节处等着。
**串行暴露的缺陷必须串行修，不能合并成一次改动**——否则第二个会被第一个的 assert 掩住。

### 22.8 六次定位分别用了什么手段

这六个的定位难度差别很大，值得分开记——因为**下一次遇到同族问题时，能复用的是手段，不是结论**。

| # | 定位手段 | 耗时占比 |
|---|---|---|
| 1 | 报错文本里直接给了 `bootloader_utility.c:833` 和表达式 `rom_index < 2`，回源码读 10 行就明白；难的是"为什么是 3 段"，用 `readelf -l` 数 LOAD 段确认 | 快 |
| 2 | 靠 `MTVAL` 的数值。0x0c 不是随便一个地址，是 `rt_object_get_type` 里 `type` 字段的偏移；A0=0 说明基址 NULL。再回查 `system_heap` 的所有写入点 | 中 |
| 3 | 报错文本给的是 assert 表达式本身，一眼能看出"两边相等"是契约；难的是**发现装不下**——要算 `sizeof(StaticQueue_t)` 与 `sizeof(struct ft_ipc)` | 中 |
| 4 | 最难。只有一个 PC，必须反汇编那段代码才能知道落在 `locks.c:150`；然后往回读 `locks.c:128/137` 的逃生门，再查 `xTaskGetSchedulerState` 的所有返回值 | 慢 |
| 5 | 靠 −144 这个 PSA 错误码往下追三层（`psa_crypto_init` → `mbedtls_mutex_lock` → `ft_ipc_take`）。中间任何一层只有"失败"没有原因 | 慢 |
| 6 | 靠 `MTVAL=0x2c`。44 是 `offsetof(struct rt_thread, error)`——这个数字没有任何提示，是把 `rtdef.h` 里 `struct rt_thread` 的字段排一遍算出来的 | 中 |

共同点：**五条里有三条（2、4、6）的关键信息是一个数字**——`MTVAL` 的 0x0c、`0x2c`，以及
反汇编定位到的 PC。RISC-V 的 `mtval` 在访问异常时存故障地址，在非法指令时存指令编码，
在断点时存 PC。它比"Load access fault"这句话有用得多，**但默认的 panic 打印是否包含它，
取决于 IDF 的 `panic_print` 配置**。这次能看到是运气好，值得写进上板手册的必查项。

第 4 条的教训单独说：`abort() was called at PC 0x...` 这句话本身没有信息量，
**必须把 PC 反汇编**。而且反汇编要用带 `-S` 的版本或对着 `.map` 查，否则看到的是一串
无法归属的指令。这次是对着 `locks.c` 的源码行号反查的——因为 `esp_libc` 编译时带了 `-g`。

### 22.9 六个缺陷的分布

| # | 缺陷 | 层次 | 静态手段能否发现 |
|---|---|---|---|
| 1 | 链接段数超限 | 镜像布局 / bootloader 契约 | 不能（ELF 完全合法） |
| 2 | 堆初始化晚于 CORE 阶段 | 初始化顺序 | 不能（顺序是内部约定） |
| 3 | 静态创建 handle 契约 | FreeRTOS 实现细节 | 不能（不是 API 文档承诺） |
| 4 | 缺 NOT_STARTED | 调度器未启动窗口 | 曾明确判断"不需要" |
| 5 | owner 哨兵冲突 | 调度器未启动窗口 | 不能（读起来是对的） |
| 6 | 解引用 NULL 线程 | 调度器未启动窗口 | 不能（上游代码默认线程非空） |

第 2、4、5、6 属于同一类：**在"调度器还没起来"这个窗口里的语义**。

FreeRTOS 对这个窗口有明确定义——加锁是空操作、任务创建自己初始化列表、`xTaskGetSchedulerState`
返回 NOT_STARTED。RT-Thread 没有这层约定，`rt_thread_self()` 在窗口里返回 NULL，
`rt_mutex_take()` 默认调用者一定是个线程。**port 的每一处都得自己补这个窗口的分支，
而"哪几处需要补"没有任何清单可查**——只能等启动路径真的走一遍。

### 22.10 修复后留下的复查清单

六个缺陷里有三个可以转成静态检查，写在这里是因为它们都是"下次改同一块地方会重犯"的类型：

| 检查 | 时机 | 判据 |
|---|---|---|
| `readelf -l ... \| grep -c LOAD` | 新增任何 `.flash.*` 链接片段后 | flash 映射段 = 2（`SOC_MMU_DI_VADDR_SHARED=1`） |
| `sizeof(StaticQueue_t)` vs `sizeof(struct ft_ipc)` | 改动 `struct ft_ipc` 字段后 | 桩必须仍是两字长；真实对象尺寸变大不影响桩，但 magic 判定仍要成立 |
| grep `rt_thread_self()` 的返回值是否被直接解引用 | 新增任何在启动路径上可能被调用的代码 | 必须显式判 NULL |

前两个能写进构建流程，第三个只能靠人。第三条正是四个同族缺陷的共同形状——
**只要代码可能在调度器启动前被调用，就必须假设 `rt_thread_self()` 是 NULL**。

### 22.11 方法论：静态审查的天花板在哪

前 11 轮产出的结论都成立：43 个 API 名字全对、链接零未定义、探针 72 个指针全在。
这些结论一条都没被推翻。**但它们描述的是"镜像的静态正确性"，不是"系统能不能启动"。**

"链接通过 + 编译期断言全绿"和"能启动"之间隔着整条初始化时序：

- 哪个函数先跑、谁依赖谁的堆、谁在谁的栈上——这些在链接图里不存在；
- 一个 assert 通过不等于语义成立，可能只是没走到；
- 修完一个缺陷之后，下一个会立刻浮出来，因为**前一个把它挡住了**。

这一轮之后，判断标准要改：**静态阶段的完成度不能超过"能链接"，剩下的必须是上板计数**。
W1–W11 里那些"仍是编译 + 链接级"的注记不是保守措辞，是准确的边界描述。

### 22.12 产物

| 项 | 值 |
|---|---|
| 硬件 | ESP32-S31 开发板，COM16 @115200，2 MB flash，320 MHz 单核 |
| 本轮修复 | 6 个启动路径缺陷，涉 4 个芯片的 `sections.ld.in`、`ld.flash.sections`、`startup.c`、`ft_ipc.c`、`ft_task.c` |
| 状态 | 走到应用启动 |
| 累计提交 | 见下一轮 |


## 23. W12 补记：第七个缺陷，只在真正切换线程时出现（2026-10-30）

§22 记了六个缺陷，每一个都在启动早期就把板子打停了。修完这六个之后
镜像第一次到了 `msh >`——然后紧接着又崩了一次，而且这次崩在完全不同
的地方。

### 23.1 现象

```
RT-Thread 5.4.0 on ESP32-S31
tick=1000 Hz, cpus=1, msh on UART0
msh >Guru Meditation Error: Core 0 panic'ed (Instruction access fault)
MEPC   = 0x2f000270   RA = 0x400a7676
MSTATUS = 0x80006080  MCAUSE = 0x00000001  MTVAL = 0x2f000270
T0/T1/T2/A6/A7/S4..S11/T3..T6 全部 = 0xdeadbeef
```

`msh >` 后面直接接 panic，说明 shell 已经起来了，崩的是应用线程
（`main.c` 里的 `thread_a`）第一次调 `rt_thread_mdelay(500)` 触发的
线程切换。

三个符号化结果：

| 地址 | 位置 |
|---|---|
| `0x2f000270` | `spurious_interrupt` 里 `addi sp,sp,128` 之后第一条 `flw ft0,0(sp)` |
| `0x400a7676` | `rt_schedule` scheduler_up.c:367，`rt_hw_context_switch()` 返回处 |
| `0x400a7c16` | `rt_thread_mdelay` thread.c:807 |

`MCAUSE=1` 且 `MTVAL == MEPC` 是这次的关键：一条**指令**访问故障，
而且故障地址就是这条指令自己的地址。`.iram0.text` 在
`0x2f000000..0x2f00fd10`，`0x2f000270` 明确落在里面，bootloader 也
确实加载了覆盖该地址的段。地址合法但取指失败，只有几种可能，而
`MTVAL=MEPC` 这一条把范围缩得很小。

### 23.2 根因：trap 退出时 FS 被清掉了

`context_gcc.S:152-158` 存进帧 slot 2 的**不是完整 mstatus，只有 MPIE 一位**：

```asm
csrr a0, mstatus
andi a0, a0, 8        /* 只留 MPIE */
beqz a0, save_mpie
li   a0, 0x80
save_mpie:
STORE a0, 2 * REGBYTES(sp)
```

所以任何从协作切换帧恢复的路径，都必须自己把 trap 退出需要的字段补进去。
两条路径补的东西不一样：

| 路径 | 写法 | 补了 |
|---|---|---|
| `context_gcc.S:245` | `li t0, 0x7800` | MPP=M(0x1800) + **FS=3(0x6000)** |
| `vectors_rtthread.S:260`（改前） | `li t1, 0x1800` | 只有 MPP=M |

`vectors_rtthread.S` 这一段之后的动作是 `FPLOAD f0..f31`。`mstatus.FS=0`
时执行 `flw` 是非法指令。而 S31 的 `SOC_CPU_HAS_FPU_EXT_ILL_BUG=1`
（`soc_caps.h:184`，注释是 "EXT_ILL CSR doesn't support FLW/FSW"）意味着
EXT_ILL 寄存器**不记录** FLW/FSW 的原因，于是这个非法指令没有被识别成
"FPU 未使能"，而是按取指异常上报：

```
MCAUSE = 1                    ← instruction access fault
MTVAL  = 0x2f000270           ← 就是那条 flw 自己的地址
```

现场完全吻合。`MSTATUS=0x80006080` 里 FS 读回是 3，是因为 trap 入口
硬件把 FS 更新成了 dirty；而我们写下去的值 FS=0——两者矛盾正是问题所在。

对照一下：`rt_hw_stack_init_core` 给**新建线程**的帧写的是 `0x7880`
（含 FS=3），所以新线程第一次被切换进来是好的。坏的只有"从协作切换帧
经 trap 退出恢复"这一条路——也就是 `rt_schedule` 内部的
`rt_hw_context_switch()` 之后，被节拍中断接手的那一瞬。

### 23.3 修法

```asm
#ifdef RT_FPU_FRAME
    li    t1, 0x7800        /* MPP=M | FS=3 */
#else
    li    t1, 0x1800        /* MPP=M */
#endif
    or    t0, t0, t1
    csrw  mstatus, t0
```

`RT_FPU_FRAME` 由 `__riscv_flen` 决定（工具链 `-march` 带 `f` 扩展），
不是 rtconfig 选项——这个文件从一开始就是这么判断的，所以两边判据一致。
改完之后 `context_gcc.S:245` 和 trap 退出用同一个值，两条恢复路径终于
对"被恢复的帧长什么样"达成了共识。

### 23.4 为什么静态审查抓不到

这一条和前六个的区别：

- 前六个中任意一个都会让板子停在更早的位置，所以第七个**根本没机会暴露**。
  这是串行依赖，不是遗漏。
- 静态上三处帧尺寸是**一致**的（256 字节），
  `rt_hw_stack_init_core` 反汇编确认是 `addi a0,a6,-256` + 64 个字填充，
  `vectors` prologue 是 `-128`(FP) + `-128`(INT)，
  `context_gcc.S` 也是 `-128` + `-128`。尺寸对了，所以任何尺寸检查都通过。
- 真正不一致的是**语义**：`mstatus` 该带哪些位。而这一点在 `vectors`
  侧有一条注释解释为什么只补 MPP（"force MPP=M for frames produced by
  a cooperative switch"）——注释本身没错，只是不完整，漏掉了同一个
  原因也适用于 FS。

这是本轮第二次验证同一个判断：**静态审查能证明"形状对"，不能证明
"语义对"**。形状（字节数、字段顺序）可以逐字核对，语义（某个寄存器在
某个时刻该是什么值）只在真的跑过之后才有答案。

### 23.5 顺带修掉的两个时序问题

修完前六个之后还出现过两次 panic，都在到 `msh` 之前：

**esp_coex 在 SECONDARY 阶段要 esp_timer。** `esp_timer_init()` 原来在
`rt_hw_board_init()` 里调，而 board init 是从 `entry()` 进来的——晚于
`do_secondary_init()` 已经把 SECONDARY 阶段的函数全跑完。也就是说那个
位置**天生太晚**，只是因为之前更早崩了才没暴露。改成
`ESP_SYSTEM_INIT_FN(rt_hw_esp_timer_init, SECONDARY, BIT(0), 99)`，
排在 `esp_coex`（204）之前。

**调度器起来前创建任务会往未初始化的表里插。** `esp_timer_init` 里
`xTaskCreatePinnedToCore` → `rt_thread_create` → `rt_thread_startup` →
`rt_thread_resume` → `rt_sched_insert_thread`，访问
`rt_thread_priority_table[]`——而这张表由 `rt_system_scheduler_init()`
初始化，此刻还是零。崩在 `rt_list_insert_after`（rtservice.h:63），
store fault @ 地址 4。

`rt_thread_create()` 本身在这个窗口是安全的：它只分配、初始化线程对象
及其定时器。只有**启动**才碰调度器全局结构。所以创建照做、启动延后：
`ft_task_start_pending()` 把待启动线程记在表里，由
`INIT_COMPONENT_EXPORT` 从 main 线程冲刷。句柄仍然立刻返回，因为
esp_timer 只是把 `s_timer_task` 存下来给 ISR 通知用，指针有效就够了。

### 23.6 产物

镜像 **897,872 B**，启动到 `msh >`，`thread_a` 的 500ms 循环跑起来了。

七次定位各自的关键线索：

| # | 决定性线索 |
|---|---|
| 1 | `bootloader_utility.c:833` 的断言文本 |
| 2 | `A0=0` + `MTVAL=0x0c`（`offsetof(rt_object, type)`） |
| 3 | `locks.c:422` 的断言表达式本身 |
| 4 | 反汇编定位到 `locks.c:150`（递归互斥量在 ISR 分支） |
| 5 | PSA 错误码 `-144` → `PSA_ERROR_SERVICE_FAILURE` → `mbedtls_mutex_lock` 失败 |
| 6 | `MTVAL=0x2c` = `offsetof(struct rt_thread, error)` |
| 7 | `MCAUSE=1` + `MTVAL==MEPC` + 反汇编到那条 `flw` |

七条里有四条（2、6、7，以及 5 的错误码）靠的是**一个数字**：
`MTVAL`、`A0`、或者 PSA 返回值。RISC-V 的 trap 现场比想象中信息量大得多，
前提是愿意逐位读它。

---

## 24. W13：SYSTIMER 的 16 倍单位错，以及一个"现在无害、以后致命"的空洞（2026-10-30）

这一轮不是修启动缺陷，是三件事：**把 §22.10 里"应该变成静态检查"的三条真的落成脚本**、
**补掉一个只有当别人开始用才会暴露的空洞**，以及**追清一个一直悬着没结论的数字**。

### 24.1 先确认硬件：COM16 上什么都没有

接手时第一件事是探板子。COM16（CH343，`VID_1A86&PID_55D3`）在这个会话里
**四种 DTR/RTS 组合、九种波特率、八种复位脉冲方向，全部 0 字节**：

| 尝试 | 结果 |
|---|---|
| 被动监听 115200 / 74880 / 9600 / 230400 / 460800 / 921600 / 57600 / 38400 / 1500000 | 全部 0 字节 |
| DTR/RTS 四种组合各监听 4 s | 全部 0 字节 |
| 八种"脉冲一条线、另一条保持某个电平"的复位组合 | 全部 0 字节 |
| `esptool --before default-reset flash-id` / `read-mac` / `chip-id` | `No serial data received` |

同时 **`esptool` 的 sync 也失败**，这比"没有串口输出"更硬：ROM bootloader 在
芯片活着且 IO0 被拉低时一定会回 `0xC0`，它不回，说明**下载模式也没进去**。

板子自身的 USB-Serial-JTAG（`VID_303A&PID_0020`，正常枚举为 COM17）在设备树里
**存在但 `Status=Unknown`**，`LastArrivalDate`/`LastRemovalDate` 都是 `15:24:06/07`——
它来了一下就走了，之后再没出现过。这既不能用"驱动问题"解释，也不能用"线没插"解释：
那一个瞬间的枚举是**芯片自己**做的。

所以本轮的结论是：**软件侧不再等板子**。下面每一条都是不需要硬件就能判定、
而且判定结果是确定的。

### 24.2 缺陷八：SYSTIMER 原始计数被当成微秒，差 16 倍

`tools/serial_time.py` 的 docstring 里记着一个没结案的观察：`s31test tick` 报
**15999 us/tick**，正好是期望值 1000 的 16 倍，而当时怀疑方向是"tick 源或
`esp_timer` 之一与现实时间不符"。这一轮把它算清楚了：

```c
/* components/esp_hw_support/port/esp32s31/systimer.c — IDF 自己的说法 */
 * systimer's clock source is fixed to XTAL (40MHz), and has a fixed fractional
 * divider (2.5). So the resolution of the systimer is 40MHz/2.5 = 16MHz.
uint64_t systimer_ticks_to_us(uint64_t ticks) { return ticks / 16; }
```

而 port 里两处读同一个计数器，都直接把原始值当微秒：

| 位置 | 代码 | 后果 |
|---|---|---|
| `port/esp32s31/gptimer.c` `s31_systimer_us()` | `return ((uint64)hi << 32) \| lo;` | `s31timer` 的测频算出 1/16 的 Hz，**看起来像 TIMG 慢** |
| `port/esp32s31/selftest.c` `st_now_us()` | 同上（复制了一份） | `s31test tick` 报 16000 us/tick，**看起来像 tick 快** |

两处独立复制同一段读法、同一个错误，是本轮唯一真正的**软件缺陷**。
它不是"两个地方各写错一次"，而是**一份错误的知识被抄了两遍**——所以修法不是各改一行，
而是**只留一份实现**：

```c
/* gptimer.c */
raw = ((rt_uint64_t)hi << 32) | lo;
return (rt_uint64_t)systimer_ticks_to_us(raw);   /* 用 IDF target port 的换算 */

/* selftest.c */
static rt_uint64_t st_now_us(void) { return s31_systimer_us(); }
```

`systimer_ticks_to_us()` 而不是字面量 16：换算比例属于 IDF 的 target port，
哪天分频变了，这个调用跟着变而常量不会。它本来就已经因为 `esp_timer` 进了镜像
（`nm` 显示 `2f007d14 T systimer_ticks_to_us`），所以不引入新依赖。

**16 倍这个数字反过来给出了一个交叉验证**：如果 tick 真的错了 16 倍，
`rt_thread_mdelay(1000)` 会变成 16 s，`thread_a` 的 500 ms 循环会肉眼可见地卡；
而实测现象是"循环正常跑"。也就是说**tick 一直是对的，错的只有测量它的人**——
`tools/serial_time.py` 那条"用主机时钟判断谁在撒谎"的思路是对的，只是它当时没有
被跑到（板子不在了）。

顺带把 `port_timer.h` 的注释写死：`s31_timer_count()` 是 **TIMG 的 1 MHz 真实微秒**
（prescaler 程序成 40 分频），`s31_systimer_us()` 是 **SYSTIMER 的 16 MHz 换算后微秒**，
两者不是一回事。这两个"看起来都叫微秒"的接口正是错误的温床。

### 24.3 缺陷九：`rt_components_board_init()` 被 gc-sections 丢掉了

`readelf`/`nm` 的静态盘点发现：`__rt_init` 表只被走了 **`.1.end` 到 `.6.end`** 这一段，
前面的 `.0` / `.0.end` / `.1.end` 三个槽**永远不执行**。原因不是表坏了：

```
nm s31_rtt_test.elf | grep rt_components
400108da T rt_components_init          <- 有
（没有 rt_components_board_init）       <- 没有
```

`rt_components_board_init()` 在整个镜像里**没有调用者**，`--gc-sections` 把它连同
那段 walker 一起丢了。今天无害，因为这三个槽只有 `rti_*` 标记函数；但它是
**一个只在别人开始用的时候才发作的空洞**：

- 任何人写 `INIT_BOARD_EXPORT(fn)`（板级外设、第二个 I2C 总线……）会**链接通过**；
- `.rti_fn.1` 槽里确实会出现他的函数指针（`KEEP()` 保住的是**段**，不是 walker）；
- 然后它**永远不被调用**，没有任何报错、没有任何打印。

这和 §22 里那六个缺陷属于同一族：**"形状对、语义不对"**。修法是在
`rt_hw_board_init()` 末尾补一次调用，顺序遵循 RT-Thread 约定（最后一步）：

```c
#ifdef RT_USING_COMPONENTS_INIT
    rt_components_board_init();
#endif
```

补完后 `nm` 里两个 walker 都在，且 `rt_hw_board_init` 的反汇编能看到
`jal rt_components_board_init`（`4001487c`），紧跟在 `s31_selftest_init` 之后。

### 24.4 §22.10 那三条"应该静态化"的检查，现在有脚本了

§22.10 写的是"前两条能进构建流程，第三条只能靠人"。这一轮把能进的那两条
连同帧布局一起做成了 `tools/check_image.py`（读 ELF，不读源码）：

| 检查 | 判据 | 对应缺陷 |
|---|---|---|
| flash 映射 LOAD 段数 | `== 2` | §22 缺陷 1（`rom_index < 2`） |
| 新线程帧 256 B | `rt_hw_stack_init_core` 里 `addi a0,a6,-256` | R9 第一层 |
| trap 入口 128+128 | 两条 `addi sp,sp,-128` | R9 第一层 |
| trap 退出 `mstatus` 含 FS=3 | `lui 0x8` + `addi -2048` = `0x7800` | §23 缺陷 7 |
| `__fsymtab` 非空 | `end - start > 0`，并报条数（`/12`） | msh 命令表 |
| `__rt_init` 非空 + 顺序 | 逐地址核对 7 个槽的名字 | §23 的初始化时序 |
| `_mtvt_table` 指向共享入口 | 48 槽里 ≥25 个是 `_interrupt_handler` | §6.3 |

当前实跑结果（`build/s31_rtt_test.elf`）：

```
[PASS] flash LOAD segments          2 flash-mapped of 4 LOAD
[PASS] new-thread frame is 256 B
[PASS] trap prologue subtracts 128+128
[PASS] trap exit mstatus FS=3       literal 0x7800 (MPP=M | FS=3)
[PASS] __fsymtab non-empty          120 B = 10 commands
[PASS] __rt_init non-empty          28 B = 7 init entries
[PASS] init table order             7 entries: rti_start ... rti_end
[PASS] _mtvt_table present          mtvt @ 0x2f000040
[PASS] trap entry installed         29/48 slots point at _interrupt_handler
notes:
  - __vsymtab is empty: no FINSH_VAR_EXPORT anywhere in the tree
```

第三条（`rt_thread_self()` 是否被判 NULL）确实只能靠人——但补完 §24.3 之后，
它至少不会再被"以为跑了其实没跑"的初始化路径掩盖。

### 24.5 已确认**不是**问题的三件事

静态盘点里翻出三处看起来可疑、核对后确认正确的：

1. **`__vsymtab` 是空的**（`start == end`）。整个仓库没有任何 `FINSH_VAR_EXPORT`，
   唯一消费者 `cherryusb` 不在构建里。空表无害，`finsh` 也不会去走它。
2. **`LOAD[0]` 的 vaddr 是 `0x2e007000` 但 `p_offset = 0`**，看上去会往 RTC 保留页
   写 ELF 头。实际上 `.rtc_reserved` 是 `NOBITS`，esptool 按**段表建镜像**、
   这个段在 `.bin` 里根本不存在，所以 bootloader 不会碰它。只有 `gdb load`
   这类按程序头搬数据的工具会踩到。
3. **`size -B` 报的 `bss` 是 889,976 B**，比真实 RAM 占用（201,848 B）大 688,128 B。
   多出来的是 `.flash_rodata_dummy`——一个 `NOLOAD` 的链接占位符，被 `size`
   当成了 BSS。真实 DRAM 用到 `0x2F0458E0`，512 KB 里还剩 233.8 KB。

### 24.6 本轮的产物与端口状态

| 项 | 值 |
|---|---|
| 镜像 | `0xdb180` = **897,408 B**（+48 B，多了一个 walker 调用） |
| 改动文件 | `port/common/board.c`、`port/esp32s31/{gptimer,selftest}.c`、`port/esp32s31/port_timer.h` |
| 新增工具 | `tools/check_image.py`（9 项 ELF 断言）、`tools/build.ps1`（绕开 Git Bash 的 IDF 环境） |
| 运行验证 | **仍缺硬件**：COM16 无输出、ROM 不回 sync、板载 USB-JTAG 只枚举过一次 |
| 可判定的结论 | 上面 9 项全部 PASS，且全部是"读镜像就能定"的 |

**给下一次拿到板子的人**：`tools/check_image.py` 在烧写前跑一遍，它挡住的是
§22 缺陷 1 和 §23 缺陷 7 这两类"ELF 完全合法但板子起不来"的问题。
如果 `s31test tick` 还报 16000 而不是 1000，那就不是这一轮修的东西，
回去看 §6.8 那张按数字分流的表。



---

## 25. W14：上板跑通全套自检，又抓出 9 个缺陷（2026-10-30，下午）

§24 那个会话的最后结论是"仍然缺硬件"。十几分钟后用户重新插拔了 USB，COM16 活了过来——
**之前所有"板子是死的"判断都是接触不良**，不是镜像、不是软件。这一轮从头到尾
上了板，`s31test` 现在 **20/20 全 PASS**，`s31timer`、`s31pwm`、`s31events`、`s31stat`、
`s31wifi` 全部跑通。下面按**被发现的方式**分组记录，因为这比按严重程度排序更有用：
每一类都对应一种**只有真的跑起来才会显形**的错误。

### 25.1 复现口径（先把"怎么跑"固定下来）

板子复位不是"打开串口就能读"：这块 CH343 的 EN/IO0 经过反相缓冲，
**pyserial 打开端口时 DTR/RTS 默认拉高反而是"运行"状态**，而"释放"（置 False）是把 EN 拉低
= 按住复位。八种"脉冲一条线"的组合里，只有 `DTR` 从 True 脉冲到 False（RTS 保持 True）
能产生一次干净复位：

```python
sp.rts = True; sp.dtr = True          # 运行态
sp.dtr = False; time.sleep(0.2); sp.dtr = True   # 一次复位
```

`tools/board.py` 就是按这个写的：**先复位、读 banner、再打字**。之前 `tools/serial_cmd.py`
的"打开后等 prompt"永远等不到，因为 finsh 只在有输入时才打印提示符——不是板子没起，
是**根本没有人问它话**。

### 25.2 单位错：SYSTIMER 不是 1 MHz，也不是 unit 0

§24.2 推断出的 16 倍误差，这一轮在板上原样复现：

```
msh > s31test tick
[FAIL] tick     2000 ticks in 31999996 us = 15999 us/tick (expect 1000, 1000 Hz)
```

修完之后又出现了**第二个**错误，而且只有修完第一个才看得见：

```
[FAIL] tick     2000 ticks in 0 us = 0 us/tick
```

两个错叠在同一个函数里：

| 层 | 症状 | 根因 |
|---|---|---|
| 第一层 | 16000 us/tick（16 倍） | SYSTIMER 原始计数被当微秒。S31 是 XTAL 40 MHz / 2.5 = **16 MHz**，IDF target port 就是 `ticks / 16` |
| 第二层 | **0 us**（比 16 倍更糟） | 读的是 `unit_val[0]`，那是 **esp_timer 的计数器**，在这块板子上根本**不动**（实测 +0 counts/100 ms） |

第二层为什么更糟：0 看起来像"没有时间流过"，会被当成"测量函数坏了"，而不是"读错了计数器"。
真正该读的是 `unit_val[SYSTIMER_COUNTER_OS_TICK]`（unit 1）——`vSystimerSetup()`
无条件使能它，而且它正是 tick alarm 比较的那个计数器，所以拿它当"tick 花了多久"的参考才有意义。

修完的真实输出：

```
[PASS] tick     2000 ticks in 2000000 us = 1000 us/tick (expect 1000, 1000 Hz)
```

**方法论**：这个 bug 的两次修法都不是靠读代码定下来的。第一次靠"16 这个数字太整"，
第二次靠新加的 `s31test clock` 子命令把三个计数器**并排打印**：

```
clock: over ~100 ms
  os_tick  +1600005 counts = +100000 us @16MHz
  esptimer +0 counts                       <-- 这就是证据
  us       +100000 us
  ccount   +31905210 cycles @320MHz = 99703 us
```

三个独立时钟域互相印证：os_tick 说 100 ms，CPU 周期说 99.7 ms，esp_timer 计数器说 0。
**"哪个计数器在动"是个实测问题，不是个读代码问题。**

顺带：`rt_kprintf` 的 klibc 格式化器**不支持 `%llu`**——第一版 diag 打印出来是
字面的 `%lu` 而不是数字。这不是新知识，但每次都会重新踩。

### 25.3 双重释放：这个 bug 只在"线程自己 return"时出现

`ctx`/`preempt`/`fpu`/`sem` 这几个用例的收尾都是"清 running 标志 → 等它退出 → `rt_thread_delete`"。
看起来天经地义，实际上**必然双重释放**：

```
1. 线程从 entry 函数 return
2. RT-Thread 走 _thread_exit() -> rt_thread_defunct_enqueue()   <- 已入 defunct 队列
3. idle 线程 rt_defunct_execute() -> rt_object_delete()         <- 释放 stack 和 thread 对象
4. 测试代码再调 rt_thread_delete(tid)                            <- 又把已释放的对象入队一次
5. idle 线程再释放一次
```

上板的现象是**在 idle 线程里崩**，而且崩的位置每次都换：

```
Core 0 panic'ed (Load access fault)
MEPC : 0x400112f8  (rt_object_get_type)
A0   : 0x00000000  MTVAL : 0x0000000c        <- 释放过的块 pool_ptr 已清成 MEM_FREED
```

修完这个之后同一处又崩第二次，换了措辞：

```
Core 0 panic'ed (Store address misaligned)
MEPC : 0x400112c4  (rt_list_remove, from rt_object_delete)
S0   : 0x2f027630  MTVAL : 0x00000005
```

第二个现场说明**第一次判断的修法不够**：按 `stat == RT_THREAD_CLOSE` 判"已退出"、
不行——因为到那一刻 thread 对象**已经被释放并被别人复用**，读到的 `stat` 属于新主人。
内存被复用之后再读它的字段，得到的是关于别人的答案。

最终修法是**指针标记表**（`ft_task.c`）：

- exit hook 在指针还唯一的时候把它记进 `s_exited[]`；
- `ft_task_create()` 拿到同一个地址时先把那条记录清掉（`ft_exit_mark_clear`）；
- `ft_task_delete()` 命中记录就**什么都不做**。

这不是测试专有的问题。FreeRTOS 里"任务函数 return"等价于自我删除，之后
`vTaskDelete(handle)` 是 use-after-free；而 **esp_event 的 loop task、esp_timer 的 task
都是 return 退出的**，所以这个洞在 Wi-Fi 起来之后一定会被踩到。

同时把四个用例的收尾统一改成"**在还活着的时候删**"或"**让它自己退出、不删**"，
并在 `sem` 那里删掉了一个同类调用。

### 25.4 判据写反：一次"全绿"被报成 FAIL

```c
/* 修前 */
st_report("fpu", ST_PASS, "32 registers x 500 switches, no mismatch");
```

`st_report(name, ok, fmt, ...)` 的第二个参数是**布尔判据**，而 `ST_PASS` 是 **0**。
于是"FP 帧完全正确"这一行永远打印成 `[FAIL]`。看起来像 FPU 保存有问题，
实际上**是判据传错了常量**。

这类错误一共 16 处（都是 s_report 传 `ST_FAIL` 当"失败判据"，恰好值又是 1，
所以只有传 `ST_PASS` 的那一处显形）。修法是引入两个只用于判据的常量：

```c
#define ST_REPORT_BAD   0
#define ST_REPORT_OK    1
```

并保留 `ST_PASS/ST_FAIL` 作为**返回码**。两个名字分开，就不会再有人把返回码当判据传。
`fpu` 修完后第一次真正 PASS。

### 25.5 事件组：返回值和清除语义都和 FreeRTOS 差一层

`xEventGroupWaitBits(xClearOnExit=pdTRUE)` 的 FreeRTOS 契约是
**返回清除前的值**，而且**只清实际匹配到的位**。shim 两个都错了：

| 项 | FreeRTOS | shim（修前） | 后果 |
|---|---|---|---|
| 返回值 | 清除**前** | 清除**后**（=0） | BT 的 `btc_l2cap.c:1114` / `btc_spp.c:1520` 把 0 当超时，**每次 write 都报 EBUSY** |
| 清除范围 | 只清匹配位 | `RT_EVENT_FLAG_CLEAR` 清**整个 mask** | OR 等待 {A,B} 只匹配到 A 时，B 被误清 |

修法：不再把 `RT_EVENT_FLAG_CLEAR` 传给 `rt_event_recv`，改为在返回前
**先快照、再按实际匹配位清除**。这样两个语义都对上了。

有意思的是**自检也一起改了**：原来的断言是照着 bug 写的（`clear_bits(0x2) != 0x1`，
即"返回清除后的值"），修好 shim 之后它开始 FAIL。这说明**测试把 bug 固化成了期望值**——
发现这一点比修 shim 本身更重要。

### 25.6 GPTimer 是一次性的：`tx_alarm_en` 是自清零位

```
msh > s31timer 0 1000
s31timer: timer 0 want 1000 Hz, got 1 Hz over 1000000 us, 1 ticks -> FAIL
```

任何频率都只来**一次**中断。根因在寄存器手册里写着：

```
tx_alarm_en : R/W/SC;  "This bit will be automatically cleared once an alarm occurs."
```

`tx_autoreload` 只重装**计数器**，不重新使能**比较器**。IDF 自己的
`esp_driver_gptimer/src/gptimer.c:466-469` 就在 ISR 里显式重新使能，注释写得很清楚。
修法是 ISR 清除状态后逐个重新 arm。

修完立刻暴露**第二个**问题：所有频率都偏高一点点（1000 → 1002，200000 → 248975）。
两个独立原因：

1. **`alarm -= 1` 是错的**。实测 `alarm=199 → 5024 Hz`（=1e6/199），`alarm=4 → 248975 Hz`
   （=1e6/4）：比较值和两条边沿之间的距离**就是** `alarm`，不该减一。减一让每个请求都偏快，
   1 kHz 时偏 0.1%（被容差吃掉），200 kHz 时偏 25%（显形）。
2. **整数截断**。`1e6/600000 = 1`，于是 500 kHz~1 MHz 的请求全部变成 1 MHz。改成四舍五入，
   并对 1 MHz 以上的请求**报错**而不是静默给个别的频率。

第三件事是**判据**：`s31timer 3 2` 请求 2 Hz、实测 1 Hz，被判 FAIL——因为 1 秒窗口里
2 Hz 本来就可能只抓到 1 个或 2 个边沿。容差改成 `max(1%, 1 count)`。

修完：

```
s31timer: timer 3 want 2 Hz,       got 1 Hz      over 999967 us,  1 ticks      -> PASS
s31timer: timer 0 want 1000 Hz,    got 999 Hz    over 999998 us,  999 ticks    -> PASS
s31timer: timer 2 want 200000 Hz,  got 199974 Hz over 1000001 us, 199975 ticks -> PASS
s31timer: timer 1 want 500000 Hz,  got 496916 Hz over 1000003 us, 496918 ticks -> PASS
```

（500 kHz 差 0.6%，是 1 MHz 时基一个 count 的量化误差，不是错。）

### 25.7 三个"会毁掉正在运行的系统"的输入校验

#### LEDC 会驱动 SPI flash 的 pad

`s31_pwm_set_pin()` 只检查 `pin < SOC_GPIO_PIN_COUNT`（62），而
`soc_caps.h:235` 明确把 **GPIO29 / GPIO41** 从 `SOC_GPIO_VALID_GPIO_MASK` 里剔掉了——
它们是封装内 flash 的 pad。`gpio.c` 的 `s31_pin_valid()` 检查对了，`pwm.c` 漏了。
`msh > s31pwm 0 29 1000 50` 会把 flash 的 pad 从 SPI 外设手里抢走，下一次取指就崩。

修完的实测：

```
msh > s31pwm 0 29 1000 50
s31pwm: gpio 29 cannot be routed
s31pwm: command failed -1.
```

#### 中断卸载后电平线还在触发

`rt_hw_interrupt_uninstall()` 清 handler 但**不屏蔽 CLIC 线**。这个 port 装的线**全是电平触发**
（`CLIC_INT_ATTR_TRIG` 复位值就是电平，装的时候没人改）。于是：外设还在拉高、
handler 是 NULL、`mret` 立刻重新进同一个 trap——**死循环**，只有 INT WDT 能报出来。
两个自检用例之前都得手动 `rt_hw_interrupt_mask()` 才能收尾，就是这个洞的证据。
修法：卸载时先 `esprv_int_disable()`。

#### 优先级 8 会静默屏蔽整条线

CLIC 的优先级字段只有 `NLBITS=3` 位（1..7），`interrupt_clic_ll_get_priority()` 会把
高 5 位右移掉。传 `priority = 8` 落到被移走的位上，读回来是 **0**，而 **0 = 被屏蔽**。
也就是说"设个高优先级"会把中断彻底关掉，而且一声不响。修法：校验 `1..7`，
并且在重设已有线的级别时先屏蔽再写。

### 25.8 低优先级的四个（顺手修）

| 位置 | 问题 | 修法 |
|---|---|---|
| `pwm.c` divisor | 超出范围时**钳位**而不是拒绝：1 Hz 请求静默变成 2.38 Hz | 返回 0 表示"不可达"，`s31_pwm_apply` 报错 |
| `pwm.c` duty | 100% 被钳成 `precision-1`，永远到不了电源轨 | 允许 `duty == precision` |
| `pwm.c` ENABLE | 未配置的通道也能"使能成功"，引脚不动 | 没有周期就拒绝 |
| `gpio.c` OD | 开漏模式先使能驱动器（输出锁存复位值是 0），总线一开始就被拉低 | 先把锁存置 1 再使能 |
| `serial.c` ISR | 读 `int_raw`，把溢出/校验/帧错误一起清掉且**从不报告** | 改读 `int_st`，溢出时打印一次 |
| `ft_ipc.c` reset | 直接写 `free_slots.value`，**唤不醒**已经挂在 `rt_sem_take` 上的发送者 | `rt_sem_control(RT_IPC_CMD_RESET)` |
| `ft_ipc.c` block 守卫 | 没检查"调度器还没起来"，队列/信号量的阻塞路径会解引用 NULL 线程 | `ft_block_allowed()` 里判 `rt_thread_self() == RT_NULL` |
| `ft_event.c` clear | 返回清除**后**的值（契约要求清除前） | 先快照再清 |
| `board.c` | `rt_components_board_init()` 被 gc 掉，`.rti_fn.0..1.end` 永远不跑 | 在 board init 末尾补调用 |

### 25.9 现在的状态

```
=== ESP32-S31 RT-Thread self test ===
tick: RT_TICK_PER_SECOND=1000 CONFIG_FREERTOS_HZ=1000
[PASS] tick     2000 ticks in 2000000 us = 1000 us/tick (expect 1000, 1000 Hz)
[PASS] ctx      min 192216 loops/thread, 0 guard errors, 4 threads
[PASS] preempt  3440580/3453985 spin loops, 5011 wakeups, 0 guard errors
[PASS] fpu      32 registers x 500 switches, no mismatch
[PASS] irq      pin 10: 200/200 interrupts, nest=1 (0 bad)
[PASS] nest     pin 10->11: outer 100@nest1, inner 100@nest2 (want 100, 0 bad)
[PASS] sem      500/500 tokens, extra take timed out
[PASS] fbshim/q cap 4, FIFO, full+empty refusal (0 bad)
[PASS] fbshim/b 6/6 produced, order preserved (0 bad)
[PASS] fbshim/t 50 ms timeout took 50000 us (0 bad)
[PASS] fbshim/s counting 3 max, mutex not recursive, recursive ok, sched lock (0 bad)
[PASS] fbshim/e or/and wait, partial clear, clear-on-exit (0 bad)
[PASS] fbshim/t per-thread slots, untouched reads NULL (0 bad)
[PASS] fbshim/e TLS slot freed when the task returns (0 bad)
[PASS] fbshim/h stack high water mark 3376 bytes free (0 bad)
[PASS] fbshim/q 5 tasks, name 'tshell', lookup+state+idle ok (0 bad)
[PASS] fbshim/p 72 FreeRTOS entry points resolved at link time (0 bad)
[PASS] fbshim/q membership, exact notify, remove, stale notification (0 bad)
[PASS] fbshim/o overwrite replaces without growing, peek, reset (0 bad)
[PASS] fbshim/g blocking under the scheduler lock returns an error (0 bad)
=== 20 passed, 0 failed ===
```

其他命令：

```
msh > s31events
s31events: PASS, delivered in 0 ms, payload=0x5a31

msh > s31wifi
  nvs_flash_init             ok
  esp_netif_init             ok
  event loop create default  ok
  create default wifi sta    ok
  event handler register     ok
  esp_wifi_init              ok
  esp_wifi_set_mode          ok
  esp_wifi_set_config        ok
  esp_wifi_start             ok
s31wifi: PASS, STA_START in 0 ms, mac 1c:29:04:d0:14:1e
s31wifi: netif 0x2f07b9a4, mode STA, not connected
```

`s31wifi` 是这一轮最重要的**架构**结论：**esp_wifi 整个启动链（NVS → netif → 事件循环 →
PHY 全量校准 → 固件加载 → STA_START）跑在 RT-Thread + FreeRTOS 兼容层上**，
没有一处卡住，MAC 从 efuse 读出来了，PHY 校准跑了（因为 NVS 里没有校准数据，
走了全量校准，这是预期行为，不是失败）。

### 25.10 产物

| 项 | 值 |
|---|---|
| 镜像 | `0xdb680` = 898,688 B |
| 修改文件 | 9 个（`board.c`、`port/esp32s31/{gptimer,interrupt,pwm,serial,gpio,selftest}.c`、`port_timer.h`、`port/freertos/{ft_ipc,ft_event,ft_task}.c`） |
| 新增工具 | `tools/board.py`（复位+命令行驱动）、`tools/check_image.py`（9 项 ELF 断言） |
| 自检 | **20/20 PASS**，首次全部通过 |
| 未验证 | 双核（P3）、BLE、真实 AP 连接（`s31wifi` 只起栈不连网） |

---

## 26. W15：把"RT-Thread 自己的东西"补齐，以及一处影子状态导致的永久失效（2026-10-30 晚）

用户问了一个很实际的问题：「是否所有移植都按照 RT 的来的」。查下来发现**两处不是**：
一个是 RT-Thread 自带 shell 命令被静默裁掉，一个是兼容层里有一份本不该存在的影子状态。

### 26.1 缺陷十：`list` / `free` / `ps` / `version` / `clear` 全都不见了

`help` 里只剩 10 条命令，全是本项目自己导出的。RT-Thread 自带的那些：

```
msh > free
free: command not found.
msh > list_thread
list_thread: command not found.
msh > ps
ps: command not found.
```

两个**互不相干**的原因叠在一起，而且症状相同：

| 命令 | 定义位置 | 为什么没有 |
|---|---|---|
| `ps` / `free` / `bind` | `finsh/msh.c` | 整段包在 `#ifdef MSH_USING_BUILT_IN_COMMANDS` 里。Kconfig 里这个选项 **default y**，但我们的 `rtconfig.h` 不是 Kconfig 生成的，没人写它 → 编译期整个消失 |
| `list` / `clear` / `version` / `console` | `finsh/cmd.c` | 编译了，但**归档成员没人引用**。这四条全靠 `MSH_CMD_EXPORT_ALIAS` 把指针塞进 `FSymTab` 段，没有任何全局符号被别处引用，`--gc-sections` 把整个 `cmd.c.obj` 丢掉了 |

第二个尤其隐蔽：`cmd.c.obj` **确实进了 `librtthread.a`**（`ar t` 能看到），
但 `FSymTab` 里没有它的 0x30 字节，`__fsymtab_start..end` 从 120 字节里数不出 `list`。
**归档里有、镜像里没有**，这是 `-u` 必须存在的场景，和当初 `finsh_system_init` 完全一样。

修法两处：

```c
/* rtconfig.h */
#define MSH_USING_BUILT_IN_COMMANDS

/* CMakeLists.txt —— 注意必须是全局符号 */
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u list_thread")
```

这里有个坑值得单独记：**`-u cmd_list` 会被链接器静默忽略**，因为 `cmd_list` 是 `static`，
`-u` 只对全局符号有意义。如果照着"命令叫什么就 -u 什么"的直觉写，会得到一个
「改了 CMakeLists 但毫无变化」的结果，然后怀疑是缓存（而这次不是缓存）。

修完之后：

```
msh > help
free             - Show the memory usage in the system
ps               - List threads in the system
help             - RT-Thread shell help
list             - list objects
console          - console setting
version          - show RT-Thread version information
clear            - clear the terminal screen
... (本项目自己的 10 条)

msh > ps
thread       pri  status      sp     stack size max used left tick   error
tb            12  suspend 0x000001b0 0x00000800     21%   0x0000000a EINTRPT
ta            10  suspend 0x000001b0 0x00000800     21%   0x0000000a EINTRPT
tshell        20  running 0x000001b0 0x00001000     16%   0x00000006 OK
tidle0        31  ready   0x00000120 0x00000400     32%   0x0000001f OK
esp_timer      8  suspend 0x000001c0 0x00001000     10%   0x0000000a EINTRPT

msh > list device
uart0        Character Device     1
pwm0         PWM Device           0
pin          Pin Device           0
```

`list device` 那三行是这一轮**最有信息量的一次输出**：它把"到底移植了什么"
从文档描述变成了运行时事实——三个设备、三种设备类，都在 RT-Thread 的设备框架里。

### 26.2 缺陷十一：兼容层的 `ipc->owner` 是一份会过期的影子状态

`ft_ipc` 里原本有一个 `rt_thread_t owner` 字段，用来实现"普通互斥量不可递归"。
问题是 **RT-Thread 自己也记 owner**（`mutex->owner`），而两者会分叉：

```
任务 A 持有 mutex
  -> ft_ipc: ipc->owner = A;  rt_mutex: mutex->owner = A
A 被 vTaskDelete
  -> _thread_detach_from_mutex() 释放 mutex 并清 mutex->owner   <- 内核做到了
  -> ipc->owner 还是 A                                          <- 没人清
新任务 B 复用了 A 的地址
  -> ipc->owner == self  -> 判"你已经持有"  -> 这个互斥量永久失效
  -> esp_libc 的 _lock_close() 还会 assert(holder == NULL) 失败
```

这跟 §25.3 那个线程双重释放是**同一个形状**：不是"少了个 API"，而是
**同一个事实存了两份，其中一份没有生命周期管理**。修法不是"在删除路径上补一句清理"
（那样每加一条死亡路径就要补一次），而是**删掉影子，只读内核的那一份**：

```c
/* take: 不可递归判断 */
if (self != RT_NULL && ipc->mutex.owner == self) return FT_ERR_TIMEOUT;

/* give: 只拦"别人持有" */
if (ipc->mutex.owner != RT_NULL && ipc->mutex.owner != rt_thread_self())
    return FT_ERR_STATE;
return (rt_mutex_release(&ipc->mutex) == RT_EOK) ? FT_OK : FT_ERR_STATE;

/* xSemaphoreGetMutexHolder */
return ipc->mutex.owner;
```

顺带把 release 交给内核判：`rt_mutex_release()` 本身允许"持有者已死"的释放
（`src/ipc.c` 里判 `RT_THREAD_CLOSE`），影子字段做不到这一点。

### 26.3 顺手：`ft_ticks_to_rt` 改为向上取整

```c
/* 修前：截断。当 FREERTOS_HZ > RT_TICK_PER_SECOND 时，1 tick 的等待变成 0 = 不等待 */
(((uint64)ticks * RT_TICK_PER_SECOND) / CONFIG_FREERTOS_HZ)
/* 修后 */
(((uint64)ticks * RT_TICK_PER_SECOND) + CONFIG_FREERTOS_HZ - 1) / CONFIG_FREERTOS_HZ
```

当前两者都是 1 kHz，所以行为不变——但"把换算写出来"的**全部意义**就是
改任一个频率不会静默改变语义，截断恰好违背这一点。

### 26.4 这一轮之后，"到底移植了什么"的准确答案

| 层 | 状态 | 证据 |
|---|---|---|
| RT-Thread 内核（调度/线程/IPC/定时器/内存/对象） | ✅ 完整 | `ps` 列出 5 个线程；`list timer` 列出 5 个定时器；`free` 报 130,976 B 堆 |
| 设备框架 | ✅ | `list device`：`uart0` / `pwm0` / `pin` |
| 串口（v1） | ✅ 自写 LL 驱动 | `uart0` 在设备表里，ref=1（shell 持有） |
| PIN | ✅ 自写 LL 驱动 | `pin` 设备；`pin` msh 命令可用 |
| PWM（LEDC） | ✅ | `pwm0`；`s31pwm 0 10 1000 50` |
| 硬件定时器（TIMG） | ✅ 直接 API（RT-Thread 5.x 已无 hwtimer 设备类） | `s31timer` 2 Hz~500 kHz 全 PASS |
| msh / finsh | ✅ 含全部原生命令 | 17 条命令 |
| FreeRTOS 兼容层 | ✅ 队列/信号量/互斥量/事件组/任务/通知/TLS/队列集 | `s31test` 12 项 fbshim 子测试 |
| esp_event / esp_netif / lwIP | ✅ 跑在兼容层上 | `s31events` PASS |
| Wi-Fi | ✅ **协议栈起来**（未连 AP） | `s31wifi` PASS，STA_START，MAC 读出 |
| 蓝牙 | ❌ **完全没链进来** | `nm` 无任何 `esp_bt_*` / `ble_hs_*` |
| 文件系统 / DFS | ❌ 未启用 | `list` 无 `fd` 选项 |
| POSIX / libc 环境 | ❌ 未启用（用 picolibc + 兼容层） | — |
| 双核 SMP | ❌ P3 未开工 | `rtconfig.h` `RT_CPUS_NR 1` |
| 其他外设（I2C/SPI/I2S/ADC/RMT/TWAI/SD/摄像头/LCD…） | ❌ 只"链接得到"，没有 RT-Thread 设备封装 | 见下 |

### 26.5 关于"跑 Python"

树里**没有任何 Micropython**（`rt-thread/packages` 目录都不存在），镜像里也没有
`mp_*` / `Py_*` 符号。目前**不能跑 Python**。

能跑 Python 的三种做法，按代价排序：

| 方案 | 做法 | 代价 | 适合 |
|---|---|---|---|
| **MicroPython 移植** | 拉 `micropython` + RT-Thread 的 `micropython` package，实现 `mp_hal_*`（串口/UART/GPIO/延时） | 中：MicroPython 自带 GC 和 VM，与 RT-Thread 无冲突；需要 200~300 KB flash 和 30~60 KB RAM | **想要真正跑 .py** |
| 自己做一个小解释器 | 没有现成轮子，等于重写 | 高 | 不建议 |
| **PC 端驱动（现状能力）** | 现在 msh 已经可以被 `tools/board.py` 脚本化驱动，PC 上写 Python 下发命令 | 零 | 现在就能用，但 Python 不在板子上跑 |

需要强调一点：**RT-Thread 和"C++ 倾向"没有关系**。RT-Thread 是纯 C 内核，
C++ 只是它支持的一个可选组件（`components/libc/cplusplus`）。能不能跑 Python
取决于是否加一个 Python 运行时，跟内核语言无关。

现实建议：先做 **M6 连真实 AP**（Wi-Fi 现在只起栈没连网），再做 MicroPython——
因为 MicroPython 的 `network` 模块要有能用的 `esp_netif`，而那个刚刚才验证通。

### 26.6 产物

| 项 | 值 |
|---|---|
| 镜像 | `0xdcec0` = 904,384 B（+5.7 KB，主要是 `cmd.c` 的 `list` 实现） |
| 修改文件 | `rtconfig.h`、`CMakeLists.txt`、`port/freertos/ft_ipc.c` |
| 新增命令 | `list` / `list thread|timer|sem|mutex|event|device` / `free` / `ps` / `version` / `clear` / `console` |
| 回归 | `s31test` 20/20、`s31wifi` PASS 不变 |

---

## 27. W16：16 MB flash、蓝牙、I2C，以及双核的可行性核对（2026-10-30 夜）

这一轮的四件事按"用户要求的顺序"做：加蓝牙 → 补 RT-Thread 原生命令（上一轮已做）→
加 Python → 加外设。**Python 没有做**，理由在 §27.6 写清了：它不是"再加个驱动"，
而是一个独立运行时，而且依赖一个真能连网的 `esp_netif`。外设这一轮只做了 I2C，
其余的在 §27.7 给出为什么不能"一次全加"。

### 27.1 先纠正一个一直没查的硬件事实：板子是 16 MB flash

启动日志一直在说：

```
W (333) spi_flash: Detected size(16384k) larger than the size in the binary image header(2048k).
                     Using the size in the binary image header.
```

`sdkconfig` 里写的是 2 MB，而 `esptool flash-id` 报 **Manufacturer 0x46 / Device 0x4018 /
Detected flash size: 16MB**。为确认不是 id 误报，往 **0x800000（8 MB 处）** 写 256 B
位置相关图案、复位、读回、比对：**完全一致**。所以 16 MB 是真的，之前那 14 MB 一直空着，
同时 1 MB 的 app 分区已经被填到 86%——**加蓝牙之前必须先解决这个**，因为 BT 控制器
光库就 4.7 MB，链进来的代码约 300 KB。

新布局（`partitions.csv`，4 MB app）：

```
0x009000  nvs        24 KB
0x00f000  phy_init    4 KB
0x010000  factory     4 MB     <- 原 1 MB
0x410000  storage     8 MB     <- 留给将来的文件系统 / MicroPython
```

`sdkconfig.defaults` 里改成 `CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y` + 自定义分区表。
**改布局后必须先擦掉旧的 0x10000..0x110000**，否则旧分区表的残留会让 bootloader 迷惑。

同时 `CONFIG_IDF_RTTHREAD_HEAP_SIZE` 从 40 KB 提到 **128 KB**——实测 Wi-Fi 起来后
已用 16 KB、自检峰值 25 KB，而 BT 控制器自己还要从这块堆里分池子。

### 27.2 `sdkconfig.defaults` 里漏了 shim 开关（一个只有重新生成才会显形的洞）

`CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM` 之前**只存在于 sdkconfig，不在 defaults 里**。
删掉 sdkconfig 重新生成后，链接立刻失败：

```
undefined reference to `ft_link_probe_count'
undefined reference to `ft_task_notify'
undefined reference to `ft_notify_forget'
```

也就是说：**这个工程的"可复现构建"一直是坏的**，只是没人删过 sdkconfig。
已补进 defaults，并注明原因——defaults 是唯一可复现的配置来源，只在 sdkconfig 里改等于没改。

### 27.3 蓝牙：BLE 控制器 + NimBLE host 起来了

```
msh > s31ble
s31ble: bringing up the BLE controller on the FreeRTOS shim
  nvs_flash_init               ok
I (7596) BLE_INIT: Bluetooth MAC: 1c:29:04:d0:14:1f
I (7597) BTDM_INIT: BTDM controller init OK
I (7680) phy: libbtbb version: d5590f7
  nimble_port_init             ok
I (7686) NimBLE: GAP procedure initiated: stop advertising.
  nimble_port_freertos_init    ok
s31ble: PASS, host synced in 0 ms, addr 1c:29:04:d0:14:1f
s31ble: controller up, NimBLE host running, not advertising
```

**这一步的意义**：BT 控制器的 OSAL（`porting_btdm/.../btdm_osal_freertos.c`，
31 处 FreeRTOS 调用：`xQueueCreate` / `xSemaphoreCreateCounting` /
`xSemaphoreCreateRecursiveMutex` / `xTaskCreatePinnedToCore` / `portENTER_CRITICAL` / `esp_timer`）
和 NimBLE 的 port（`nimble_port_freertos.c`，`xTaskCreatePinnedToCore` + `vTaskDelete`）
**全部跑在兼容层上**，没有一处卡住。所以 §26 那个"兼容层是否够用"的问题，
现在有了 Wi-Fi 之外的第二个独立证据。

路上踩了三个坑，都不是兼容层的问题：

| 现象 | 根因 |
|---|---|
| `nimble_port_init` 报 `0x103 ESP_ERR_INVALID_STATE`，紧跟 `BLE_INIT: controller init failed` | **重复初始化控制器**。`nimble_port_init()` 自己就会 `esp_bt_controller_init + enable`（`nimble_port.c:288-320`），我在它之前手动做了一遍。手册里没写这条，读源码才知道 |
| `host never synced`，`ps` 里没有 `nimble_host` | `nimble_port_freertos_init(func)` 的参数是**任务函数**，不是上下文。我传了 `RT_NULL`，`ft_task_create()` 正确地拒绝了"没有入口点的任务"——**这个报错反过来证明 shim 的 NULL 检查是有效的** |
| 日志里 `BTDM_SLEEP: Using 136 kHz RC as clock source` 的警告 | 默认低功耗时钟源是 RC，精度不够维持 ACL/Sync。不影响 bring-up，但要真连设备得改成 XTAL32K |

镜像从 904 KB 涨到 **1.46 MB**（`0x164ab0`），4 MB 分区还剩 65%。

### 27.4 I2C：第一块"标准设备类"外设

`list device` 里出现了第三个设备类：

```
msh > list device
uart0        Character Device     1
i2c0         I2C Bus              0
pwm0         PWM Device           0
pin          Pin Device           0
```

`i2cscan 0` 在空总线上报 `0 device(s)`（正确——板子上没接从机）。
这一块的重点不是"能不能通"，而是**它按 RT-Thread 的约定接进了框架**：
`rt_i2c_bus_device_ops` + `rt_i2c_bus_device_register()`，所以 `rt_i2c_transfer()`、
`rt_device_find("i2c0")`、以及所有 RT-Thread 的 sensor 软件包都能直接用。

三个只有读寄存器手册才知道的点，都写进代码注释了：

1. **I2C 的时钟不是 APB**。S31 走 `HP_SYS_CLKRST` 自己的分频器，
   `i2c_ll_master_cal_bus_clk()` 要传**源频率**，所以这里固定用 XTAL 40 MHz，而不是 APB——
   APB 会随 DFS 变，那样总线速率会在背后悄悄变。
2. **写寄存器后必须 `i2c_ll_update()`**（`ctr.conf_upgate`）。IDF 在
   `i2c_hal_master_trans_start()` 里做，漏掉的话旧时序继续生效。
3. **命令表是硬件状态机**，`command[8]` 每项带 `command_done` 位，必须以 `END` 结束，
   并且要等 `END` 的 done 位——**从机不 ACK 时 `END` 永远不会完成**，所以超时判断必须在循环里
   看状态位。

第四条是这一轮踩的：**第一版超时用了 200 万次循环**。单次传输没问题，
但 `i2cscan` 要探 112 个地址、每个都不 ACK，乘起来就像死机。改成按总线速率算预算
（`(len+4)*9*1000/(freq/1000)` 次，下限 2 万），并在循环里检查 NACK/bus-timeout 位提前退出。

### 27.5 RT-Thread 原生命令（上一轮已补，这里只记结果）

`list` / `free` / `ps` / `version` / `clear` / `console` 全部可用，`ps` 能看到
`btdm`（优先级 7）和 `nimble_host` 线程。详见 log §26。

### 27.6 为什么**没有**加 Python

用户要"加 Python"。查证后的结论是：**这不是一个可以顺手加进来的东西**，理由三条：

1. **树里没有任何 MicroPython**。`rt-thread/packages` 目录都不存在——那是 RT-Thread 的
   在线软件包目录，MicroPython 从那里取。镜像里也没有 `mp_*` / `Py_*` 符号。
2. **它是个独立运行时**，不是驱动：自带 GC、字节码解释器、`mp_hal_*` 移植层。
   工作量约等于"再做一个 P2"，而且一旦跑起来，它会成为镜像里第二大的东西（200~300 KB）。
3. **它依赖一个真能连网的 `esp_netif`**。`network` 模块是 MicroPython 最常用的部分，
   而现在的 Wi-Fi 只到"协议栈起来了、未连接"。先做 **M6 连真实 AP**，
   再谈在板子上跑 .py，顺序不能反。

另外要澄清一个常见误解：**RT-Thread 是纯 C 内核**，C++ 只是它的可选组件
（`components/libc/cplusplus`）。"能不能跑 Python"和内核用什么语言无关，
只取决于加不加一个解释器。

### 27.7 为什么外设也**没有**一次全加

用户列了 14 类外设。逐类核对 S31 的实际情况后，分成三组：

**A. 有硬件、可以按 I2C 的方式接进 RT-Thread 框架（下一步做）**

| 外设 | S31 有这个硬件 | 框架 | 备注 |
|---|---|---|---|
| SPI | ✅ 3 个 | `rt_spi_bus` + `rt_spi_ops` | 注意 GPIO29/41 是 flash pad |
| ADC | ✅ 2 个单元 × 8 通道 | `rt_adc_device` | oneshot 模式 |
| RMT | ✅ | 无标准框架 | 直接 API（像 TIMG 那样） |
| TWAI(CAN) | ✅ 2 个控制器 | `rt_can_device` | 需要收发器 |
| SD/MMC | ✅ | `rt_mmcsd` | 要接卡 |
| USB | ✅ OTG + Serial-JTAG | `rt_usb` | 工作量大 |
| LCD / 摄像头 | ✅ (LCDCAM, I80/RGB) | `rt_device_graphic` | 需要屏/摄像头实物 |
| 触摸 | ✅ | `rt_touch` | |
| PCNT / MCPWM | ✅ | 无标准框架 | 直接 API |
| I2S | ✅ | `rt_audio` | |

**B. 这颗芯片根本没有的（不要浪费时间）**

- **DAC**：`soc_caps.h` 里没有 `SOC_DAC_SUPPORTED`。S31 没有 DAC，用 PWM+RC 或外挂。

**C. 一次全加会失控的原因**

每加一个外设，要动的都是**同一组东西**：`rtconfig.h` 开宏、CMakeLists 加 RT-Thread 框架源、
`port/esp32s31/` 加一个 LL 驱动、`board.c` 加 init、再加一个 msh 自检命令。
按 I2C 这次的实测，**每个约 400~600 行 + 1~2 轮上板调试**（I2C 这一轮就踩了超时预算和
两个头文件路径）。14 个一起加，等于同时引入 14 个未验证的变量，
**上一轮"修完第一个才看得见第二个"的教训会变成 14 倍**。

所以建议的节奏是**每次 2~3 个**，按"有没有实物能验证"排序：
I2C（已完成，空总线也能验证）→ SPI + ADC（可内部环回/读内部通道）→
TIMG/PCNT/MCPWM（纯内部，无需外设）→ 有实物再谈 LCD/CAM/SD/USB。

### 27.8 双核（P3）：可行性核对与具体步骤

用户要"介绍双核怎么弄"。查证后的事实基础（全部在树里可查）：

| 事实 | 来源 |
|---|---|
| S31 **有 2 个核** | `soc_caps.h`: `SOC_CPU_CORES_NUM (2U)` |
| IPI 中断号是 **28** | `soc.h`: `ETS_IPC_ISR_INUM 28` |
| `esp_cpu_unstall(1)` 有实现 | `esp_hw_support/cpu.c:38-44` |
| IDF 的启动从核入口是 `start_other_core()` | `esp_system/port/cpu_start.c:308`，设 `ets_set_appcpu_boot_addr(call_start_cpu1)` 在 `:337` |
| RT-Thread 有 `scheduler_mp.c` | `rt-thread/src/scheduler_mp.c` 存在，与 `scheduler_up.c` 并列 |
| `context_gcc.S` 已有 SMP 分支 | `libcpu/risc-v/legacy_common/context_gcc.S:18/67/186` 都有 `#ifdef RT_USING_SMP` |

**要做的事，按依赖顺序**（这也是方案 §P3 的现状更新）：

1. **别让 IDF 碰 CPU1**：保持 `CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y`。
   这条现在已经是现状，所以 CPU1 从复位后一直停在 ROM。
2. **改 `rtconfig.h`**：`RT_USING_SMP` + `RT_CPUS_NR 2`。
   这会同时把 `scheduler_up.c` 换成 `scheduler_mp.c`（CMakeLists 里是一行选择）。
3. **`vectors_rtthread.S` 的全局量要 per-CPU 化**。这是**最大的一块机械改动**：
   现在的中断退出路径直接 `la t0, rt_interrupt_nest` / `rt_interrupt_from_thread` /
   `rt_interrupt_to_thread`，这些在 SMP 下必须改成 `rt_cpu_self()->...`。
   不是重写逻辑，但每一处都得改，漏一处就是"另一个核的上下文被切走"。
4. **启动 CPU1**：写一个精简的 `call_start_cpu1_rtthread`（只做 gp 初始化、设 `mtvec`、
   清 CLIC 配置、跳 `rt_hw_secondary_cpu_up()`），然后
   `ets_set_appcpu_boot_addr(...)` + `esp_cpu_unstall(1)`。
   **不要**复用 `do_secondary_init()`——它会把所有 SECONDARY 阶段的初始化再跑一遍。
5. **per-core tick**：`port_systick.c` 的 `vSystimerSetup()` 已经按 `cpuid` 选 alarm
   （`SYSTIMER_ALARM_OS_TICK_CORE0 + cpuid`），所以从核自己调一次即可。
6. **IPI**：`ETS_IPC_ISR_INUM = 28` 在 `mtvt` 里现在指向 `_interrupt_handler`（本 port 的共享入口），
   但 IDF 的 `esp_ipc_isr_handler` 在 `CONFIG_ESP_IPC_ISR_ENABLE=y` 时会占这个槽且
   **不经过 `rt_interrupt_enter/leave`**。二选一：把 28 号槽交给 RT-Thread 分发，或关掉
   `CONFIG_ESP_IPC_ISR_ENABLE` 自建 IPI。

**风险**：`CONFIG_IDF_RTOS_RTTHREAD_FREERTOS_SHIM=y` 下，兼容层自己的静态表
（TLS / notify / exit marks）都是**单份**的，SMP 下要加锁——目前它们只用了
`rt_hw_interrupt_disable()`，那在 SMP 上只关本核中断，**不足以保证互斥**。
这是 P3 的隐藏工作量，方案里没列过，已补进风险登记 **R24**。

**结论**：P3 可以开工，但第 3 步（per-CPU 化向量表）和第 6 步（IPI 归属）是硬骨头，
而且兼容层的锁需要先复查。建议在**单核功能稳定之后**再做，否则同时动内核和运行时。

### 27.9 风险登记新增

| # | 风险 | 状态 |
|---|---|---|
| **R24** | 兼容层静态表在 SMP 下不是互斥的：`ft_tls`/`s_notify`/`s_exited`/`s_pending` 用 `rt_hw_interrupt_disable()` 保护，SMP 下只关本核中断。P3 开工前必须换成 `rt_spin_lock_irqsave` 或 per-CPU 表 | 新登记，P3 前置 |
| **R25** | `sdkconfig.defaults` 与 sdkconfig 不同步时构建不可复现（本轮 `FREERTOS_SHIM` 就是这样丢的） | 已修；检查方法：删 sdkconfig 后必须仍能构建 |

### 27.10 产物

| 项 | 值 |
|---|---|
| 镜像 | `0x164af0` = **1,461,488 B**（BT 贡献 ~560 KB），4 MB 分区剩 65% |
| flash | 16 MB 可用（实测 8 MB 处写入读回一致），4 MB app + 8 MB storage |
| 新增文件 | `s31-rtt-test/main/s31ble.c`、`port/esp32s31/i2c.c`、`port/esp32s31/port_i2c.h`、`s31-rtt-test/partitions.csv` |
| 修改文件 | `sdkconfig.defaults`、`main/CMakeLists.txt`、`components/rtthread/CMakeLists.txt`、`port/esp32s31/rtconfig.h`、`port/common/board.c`、`port/freertos/{ft_task,ft_ipc,ft_query,ft_notify}.c` |
| 新命令 | `s31ble`、`i2cscan` |
| 回归 | `s31test` 20/20、`s31wifi` PASS、`s31ble` PASS、`i2cscan` 正常 |

---

## 28. W17：拿到开发板资料之后（2026-10-30 深夜）

用户给了板子型号和资料目录：**Maple ESP32-S31 Alef（XPU Labs）**，
`esp-idf-rtt/docs/开发板资料/docs/maple_esp32/`，外加一份嘉立创原理图
`SCH_Schematic1_2026-10-30.pdf`。这一轮的第一件事不是写代码，而是**把之前所有
"猜"的引脚和"没查"的硬件能力逐条核对**。

### 28.1 板子的真实规格（和之前假设的差异）

| 项 | 手册值 | 之前的假设 | 影响 |
|---|---|---|---|
| 模组 | **ESP32-S31-WROOM-3** | 未知 | — |
| Flash | **16 MB** | 2 MB（已在本轮前修正） | 已完成 |
| **PSRAM** | **16 MB Octal** | **完全没配** | 见 §28.4 |
| 无线 | Wi-Fi 6 / **BT 5.4 LE + Classic** / Zigbee / Thread | 只提了 Wi-Fi/BLE | Classic 需要 `libbredr_app.a`（+2.4 MB） |
| 摄像头 | **DVP**（LCD_CAM） | 未提及 | 有硬件 |
| 屏幕 | **SPI LCD**（12 pin 排座） | 未提及 | 有硬件 |
| SD | **SDIO 4-bit** | 未提及 | 有硬件 |
| USB | **USB 2.0 Host** ×1 + CH343P USB-UART | 只知道 CH343P | Host 需要 USB-A 口 |
| RGB LED | **SK6805-2427**，数据脚 **IO60** | 未提及 | 见 §28.3 |
| 按键 | EN + **BOOT1 on IO61** | 未提及 | — |

### 28.2 缺陷十二：I2C 引脚一直是错的

之前 `port_i2c.h` 里写的是 **IO4/IO5**，理由是"低编号的脚比较安全"。
板子手册的引脚表（`static/images/2026-08-14-19-07-39-image.png`）明确写着：

```
60  IO50  I2C_SCL  DVP_Y6
61  IO51  I2C_SDA  DVP_Y7
```

而 **IO4/IO5 在这是 I2S_LRCK / I2S_DSDIN**。也就是说 `i2cscan` 之前扫的是一条
**什么都没接的总线**，报 "0 devices" 是必然的，和驱动是否正确无关。

修法：新增 **`port_board.h`**，把板子实际的引脚映射写成一张表（I2C / RGB LED /
BOOT / SPI LCD / I2S / DVP），`port_i2c.h` 改为引用它。现在两个驱动共享同一份事实，
不会再出现"一个文件里 IO4 是 I2C，另一个文件里 IO4 是 I2S"。

`i2cscan 0` 现在扫的是 **IO50/IO51**。空总线仍然报 0——但这已经是**正确的 0**了。

### 28.3 新增两个板级命令，都验证过

**`s31led`**（SK6805-2427，IO60）：

```
msh > s31led 8 0 0
s31led: gpio 60 <- R=8 G=0 B=0
```

WS2812 时序用 `mcycle` 计数生成，整帧 24 bit 期间关中断（30 µs）。用 RMT 更"正规"，
但为一个 LED 引入一整套外设抽象不划算，而软件时序"算错周期"的表现是**颜色不对**，
不是"什么都不发生"——是可调试的失败。

**`s31psram`**：分配、写位置相关图案、volatile 读回、比对。见下。

### 28.4 缺陷十三：PSRAM 能用，但会把中断系统打崩

这是本轮最有价值的一条，因为它**同时暴露了两个独立缺陷**。

**(a) 100M / 200M 根本起不来。** 打开 PSRAM、速度 200M，启动日志停在：

```
I (404) MSPI Timing: Enter psram timing tuning
```

再也不动。换 100M 一样。换 **40M** 就正常启动（`MSPI_TIMING_PSRAM_NEEDS_TUNING`
在 40M 下没定义，整个 tuning 被编译掉）。这段代码跑在 `cpu_start`、调度器之前，
**兼容层不可能参与**，所以是这颗 SoC 在这个 port 上的真缺陷。

**(b) PSRAM 打开后，`s31test irq` 被中断风暴打死。**

```
Guru Meditation Error: Core 0 panic'ed (Interrupt wdt timeout on CPU0)
MEPC : 0x400179e8  (s31_gpio_isr)
MCAUSE : 0x00000018  -> 中断号 8 -> ETS_UART0_INTR_SOURCE
```

A/B 验证：**PSRAM 关闭时同一测试 200/200 PASS**，打开就死。所以不是 GPIO 驱动本身。

顺着这条线索查出**两个真问题**：

1. **中断处理路径原来在 flash 里**。`_interrupt_handler` 和
   `_global_interrupt_handler` 在 IRAM，但它们下面的 `rtt_trampoline`、`s31_gpio_isr`、
   `s31_uart_isr`、`s31_timg_isr` 全在 `.flash.text`。PSRAM/MSPI 操作期间缓存被关，
   这些代码**取不出来**。IDF 给每个注册的 ISR 加 `ESP_INTR_FLAG_IRAM` 就是这个原因，
   而这个 port 是手写驱动，得自己写。修法：这四个函数加 `IRAM_ATTR`，
   `nm` 确认全部从 `0x40...` 移到 `0x2f...`。

2. **未安装处理器的电平中断线 = 中断风暴**。这个 port 装的所有线都是电平触发
   （`CLIC_INT_ATTR_TRIG` 复位值是电平，没人改）。外设拉高、矩阵路由了、线没屏蔽、
   handler 槽是空的 → 进 trap、返回、`mret` 立刻再进同一个 trap，**再也没有线程能跑**。
   修法：`rtt_trampoline` 发现 `fn == NULL` 就**屏蔽该线并只打印一次**，
   把活锁变成一行输出。

**但修完这两条，`s31test irq` 仍然死**，只是崩的位置换到了 `s31_gpio_isr` 的
prologue（`2f002d22`，`sw s0,8(sp)`）。说明还有第三条路径，调查中止在
"PSRAM 关掉就全绿"这个事实上。

**结论与决定**：PSRAM 在 40M 下**功能正确**（`s31psram` 写读 64 KB 图案一致，
堆里出现 16.7 MB），但它会让中断系统进入一个这个 port 还没处理好的状态。
所以：

- `sdkconfig.defaults` 里 **PSRAM 保持关闭**，并在文件里写清上面两条原因和复现方法；
- 两条 ISR 修复**保留**（它们独立成立，且是 PSRAM 的前置条件）；
- PSRAM 重新打开列为下一轮第一项，前置条件是"未映射中断线"彻底处理掉。

`free` 的对比很能说明收益：内部堆 232 KB，PSRAM 关掉后 `s31psram` 如实报
`psram free=0` 并给出检查提示，**不假装成功**。

### 28.5 又踩一次 `--gc-sections`：命令写了但不存在

`s31psram` / `s31led` 编译进了 `board_extra.c.obj`，`ar t` 里有，但
`list` 里两条命令**都不存在**，`FSymTab` 里也没有 `board_extra.c.obj` 的 0x18 字节。

原因和 §26.1 的 `list` **完全一样**：两条命令只通过 `MSH_CMD_EXPORT_ALIAS` 注册，
没有任何全局符号被引用，整个归档成员被丢掉。修法也一样——从 `board.c` 引用一个
全局符号 `s31_board_extra_init()`。

这是同一个坑**第二次**。所以把它写成规则：**任何新文件只要命令/初始化项是靠段注册的，
就必须有一个被引用的全局符号当"锚"**，并在 `nm` 里确认符号真的在镜像里。

### 28.6 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/port_board.h`（板级引脚表）、`port/esp32s31/board_extra.c` |
| 修改文件 | `port_i2c.h`（改用 `port_board.h`）、`interrupt.c`（IRAM + 风暴守卫）、`gpio.c` / `serial.c` / `gptimer.c`（ISR → IRAM）、`board.c`（锚引用）、`sdkconfig.defaults` |
| 新命令 | `s31psram`、`s31led` |
| 回归 | PSRAM 关闭时 **20/20 PASS**，`s31wifi` PASS，`s31ble` PASS，`i2cscan` 在 IO50/IO51 上正常 |
| 已知未修 | PSRAM 启用后的中断风暴（第 3 条路径）、100M/200M 的 timing tuning 挂死 |

---

## 29. W18：PSRAM 中断风暴的真正根因（2026-10-31 凌晨）

上一轮结束时留下了 R27"PSRAM 启用后的第三条崩溃路径未查明"。这一轮把它查清了，
而且顺带挖出一个**比 PSRAM 严重得多**的缺陷。

### 29.1 先纠正上一轮的一个错误结论：我加的守卫是死代码

上一轮我在 `rtt_trampoline()` 里加了 `if (c->fn == RT_NULL) { 屏蔽 + 打印 }`，
认为它能挡住"未安装 handler 的电平线"。**它挡不住。**

```c
/* components/riscv/interrupt.c:59-69 */
void _global_interrupt_handler(intptr_t sp, int mcause)
{
    const intr_handler_item_t* item = intr_get_item(mcause - RV_EXTERNAL_INT_OFFSET);
    if (item->handler) {          /* <-- NULL 时直接返回 */
        (*item->handler)(item->arg);
    }
}
```

RF 的 handler 槽为空时，**根本不会调用 `rtt_trampoline`**。所以那条
"trap → 返回 → mret 立刻重入"的活锁路径，我的守卫一行都执行不到。
真正能挡住它的只有**在别处把 IE 位清掉**。

### 29.2 新工具：`s31intr`

光读代码定位不到，所以写了这条命令，直接 dump 每条 CLIC 线的
`IE / IP / 级别 / 触发类型 / 有无 handler / 矩阵上的源`。

第一版有两个错，都是"读到错地址"这一类：

1. **漏了 +16 偏移**。CLIC 线号 = INUM + `RV_EXTERNAL_INT_OFFSET`(16)，
   我却直接用 INUM 索引，dump 出来的是 16 条**内部**线却标成了外部线号。
   这个错误让我一度以为"有 handler 的线 0/2/3/5 才是问题"，方向完全错了。
2. **级别字段没右移**。`CLIC_INT_CTL` 的高 NLBITS 位才是级别，低 `8-NLBITS` 位恒为 1。
   直接打印原始字节得到 31/63/255，看着像乱码。

修完后 dump 才有意义：

```
inum ie ip lvl trig handler name
   0  1  0   1  lvl yes     src 0
   2  1  0   1  lvl yes     -
   3  1  0   1  lvl yes     -
   5  1  0   1  lvl yes     -
  24  1  0   4  lvl NO      -      <- T1_WDT（中断看门狗）
  25  1  0   4  lvl NO      -      <- CACHEERR
  27  1  0   4  lvl NO      -      <- ASSIST_DEBUG
```

### 29.3 缺陷十四（严重）：`rt_hw_interrupt_mask()/umask()` **从来就没生效过**

这是本轮最重要的发现，也是"为什么 PSRAM 一开就崩"的根因。

port 里 mask/unmask 调的是 `esprv_int_disable()/esprv_int_enable()`，
看起来是 IDF 的 CLIC 接口，但**链接时被 ROM 覆盖了**：

```
map:
  PROVIDE (esprv_int_enable  = esprv_intc_int_enable)   0x2f800758  <- ROM
  PROVIDE (esprv_int_disable = esprv_intc_int_disable)  0x2f80075c  <- ROM
  esprv_int_set_priority = esprv_intc_int_set_priority            <- ROM
```

`components/riscv/interrupt_clic.c` 里确实定义了 CLIC 版本，但在这颗芯片上
**ROM 的 PROVIDE 优先**，`interrupt_clic.c` 只贡献了 `esprv_int_get_priority()`
和 `intr_matrix_route()`（map 里可查）。而 ROM 那几个 `esprv_intc_int_*` 是
**老 INTC 接口**，根本不碰 CLIC 的 `INTIE` 位。

**实测证据**：`s31intr -m 16` 打印了 `masked line 16`，紧接着再 dump，`ie` 仍然是 **1**。

也就是说：

- `rt_hw_interrupt_mask()` / `rt_hw_interrupt_umask()` —— 空操作
- 基于它们的"未处理线清扫" —— 空操作
- `rt_hw_interrupt_uninstall()` 里的 `esprv_int_disable()` —— 空操作
- `rt_hw_interrupt_install()` 里"先屏蔽再改槽" —— 空操作

**为什么自检一直全绿？** 因为 GPIO 驱动**从来没依赖这条路径**：它有自己的
按引脚 `int_ena` 位，而 `irq`/`nest` 测试直接写 status 寄存器触发。
一套绿色的测试对一个从未被测到的接口毫无发言权——和 §28.2 的 I2C 引脚错误
是同一类问题：**"测试通过"和"测试的是对的东西"是两件事**。

**修法**：不再走 `esprv_int_*`，直接读写 CLIC 线的 IE 位：

```c
#define RTT_CLIC_LINE(inum)  (inum + RV_EXTERNAL_INT_OFFSET)
static void rtt_clic_set_ie(int inum, rt_bool_t on)
{
    volatile rt_uint32_t *reg = /* CLIC_INT_CTRL_REG(RTT_CLIC_LINE(inum)) */;
    if (on) *reg |= CLIC_INT_IE; else *reg &= ~CLIC_INT_IE;   /* RMW：同一字里有级别和触发类型 */
}
```

同时新增 `rt_hw_interrupt_is_masked()`，因为判断"这条线是否使能"原来用
`esprv_int_get_priority(vector) != 0`——那是**级别**不是**使能位**，
两者是不同的条件。

### 29.4 缺陷十五：清扫会顺手屏蔽中断看门狗

清扫生效后 `s31intr` 剩三条 `ie=1, handler=NO`：**24 / 25 / 27**。
这不是遗漏，是设计：IDF 把 T1_WDT、CACHEERR、ASSIST_DEBUG 三条**故意**交给
汇编 panic 路径而不是 C handler（`int_wdt.c:181-184` 的注释写得很清楚）。

所以清扫必须显式豁免这三条，否则会把**唯一能报告系统挂死的机制**关掉——
正好和清扫的目的相反。已加豁免。

### 29.5 PSRAM 现状：能用，但和 `s31test irq` 还不共存

修完 mask 之后重测：

| 项 | 结果 |
|---|---|
| PSRAM 40M 启动 | ✅ 正常 |
| `s31psram` | ✅ **PASS**，64 KB 在 `0x50000a1c` 写读一致，池里 **16,774,632 B** |
| `s31test irq`（PSRAM 开） | ❌ 仍然 interrupt wdt，`MEPC` 在 `s31_gpio_isr:222`（函数尾声） |
| `s31test`（PSRAM 关） | ✅ **20/20** |

`S0/S1 = 0` 且 `MEPC` 落在函数的 epilogue（`lw ra,12(sp)` 之前），说明不是取指失败，
而是**处理器在中断里被同一个中断反复打断**——但 `s31intr` 已经显示没有
`ie=1 && handler=NO` 的线了。所以这是一条**新的、仍未定位**的路径，
不再是我能靠读代码推出来的了。

**结论与决定**：PSRAM 功能已验证（`s31psram` 是长期保留的验证工具），
但默认仍关闭，`sdkconfig.defaults` 里把三条原因（100M/200M 的缓存断言、
40M 可用、与 irq 测试冲突）逐条写清并给出重开方法。R27 更新为"已定位到
mask 无效（已修）+ 仍剩一条未定位路径"。

### 29.6 另外查实的一条：100M/200M 挂死的原因也找到了

上一轮只记录"停在 `Enter psram timing tuning` 不动"。现在有了符号化的 dump：

```
mspi_timing_psram_tuning      mspi_timing_tuning.c:413
mspi_timing_enter_low_speed_early   :659
mspi_timing_enter_low_speed_mode    :526
mspi_timing_config_set_psram_clock  port/esp32s31/mspi_timing_config.c:29
periph_rcc_exit               periph_ctrl.c:34
vPortExitCritical             portable/riscv/port.c:664
prvAssertCacheEnabled         :541
esp_system_abort -> "RT-Thread scheduler entry while flash cache is disabled"
```

不是"挂死"，是**我们自己加的断言把它 abort 了**。`prvAssertCacheEnabled()`
是为 RT-Thread 移植加的不变量（"调度前必须恢复 cache"），而 MSPI 调时序时
合法地在关 cache 的情况下走临界区。这是**移植不变量与 IDF 既有代码路径的冲突**，
不是 IDF 的 bug。修法方向明确（让 MSPI 这条路径豁免该断言，或让它不进调度），
但属于 PSRAM 启用前的第二项前置工作。

### 29.7 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/intr_debug.c`（`s31intr`） |
| 修改文件 | `interrupt.c`（直接写 CLIC IE、`is_masked`、清扫豁免、`IRAM_ATTR`）、`board.c`（两处清扫 + 锚引用）、`port_intr.h`、`sdkconfig.defaults` |
| 新命令 | `s31intr`（`-a` 全部、`-m N` 屏蔽） |
| 回归 | PSRAM 关：**20/20 PASS**；PSRAM 开：`s31psram` PASS，`s31test irq` 仍崩 |
| 新缺陷 | 十四（mask 从未生效，已修）、十五（清扫误伤看门狗，已修） |

---

## 30. W19：给 `s31intr` 加计数器，把 PSRAM 的"第三条路径"缩小范围（2026-10-31 凌晨）

上一轮把 PSRAM 崩溃归到 R27"仍剩一条未定位路径"。这一轮的目标是**把它定位**，
结果虽然没有完全定位，但**排除掉了一整类可能**，而且过程中又发现两个自己的缺陷。

### 30.1 先修两个自己写的缺陷

**(a) `s31intr` 漏了 +16 偏移。** CLIC 线号 = INUM + `RV_EXTERNAL_INT_OFFSET`(16)，
我却直接用 INUM 索引 `CLIC_INT_CTRL_REG`，dump 出来的是 16 条**内部**线却标成外部线号。
这个错误让我一度以为"有 handler 的线 0/2/3/5 才是问题"，方向完全错。

**(b) 级别字段没右移。** `CLIC_INT_CTL` 的高 NLBITS 位才是级别，低 `8-NLBITS` 位恒为 1。
直接打印原始字节得到 31/63/255，看着像乱码。改成 `>> (8 - NLBITS)` 后是 1..7。

**(c) 自检的三个 ISR 回调还在 flash 里。** `st_irq_cb` / `st_nest_inner_isr` /
`st_nest_outer_cb` 都在 `0x400...`，和上一轮修的驱动 ISR 是同一类问题（缓存关闭时取不出指令）。
已全部加 `IRAM_ATTR`，`nm` 确认移到 `0x2f...`。

### 30.2 新证据：给每条线加"进入次数"计数器

`s31intr` 现在多一列 `cnt`：

```
inum ie ip lvl trig handler     cnt name
---- -- -- --- ---- ------- ------- ----
   0  1  0   1  lvl yes           0 SYS_ICM
   4  1  0   1  lvl yes           0 -
   5  1  0   1  lvl yes          18 -
  24  1  0   4  lvl NO            0 -
  25  1  0   4  lvl NO            0 -
  27  1  0   4  lvl NO            0 -
```

跑完 `s31test irq`（200 次触发）：

```
   4  1  0   1  lvl yes         200 -     <- 正好 200，干净
   5  1  0   1  lvl yes          62 -
```

**这个计数器是这一轮的关键工具**，因为它能回答一个 WDT dump 回答不了的问题：
"这个 ISR 到底被进了多少次"。

### 30.3 关键发现：PSRAM 开启后，崩溃**不是**中断风暴

PSRAM 开（40M）+ `s31test irq`：

```
Guru Meditation Error: Core 0 panic'ed (Interrupt wdt timeout on CPU0)
MEPC : 0x2f8036ce  ...
```

但**紧接着 dump 计数器**：

```
   4  1  0   1  lvl yes           0 -      <- 一次都没进过！
```

也就是说 `rtt_trampoline()` **根本没被调用**。前几轮我一直把 dump 里的 `MEPC`
当成"出错的那条指令"，但 `MEPC` 只是 WDT 采样时**最后执行过的地址**，
CPU 卡在别处时它可以是任何残留值。之前几轮"符号化 MEPC 指向 s31_gpio_isr
所以是 GPIO ISR 在风暴"的推论**是错的**。

进一步验证：

| 测试 | PSRAM 关 | PSRAM 开 |
|---|---|---|
| `s31test tick` / `sem` / `fpu` | PASS | **PASS** |
| `s31wifi` | PASS | **PASS** |
| `s31psram` | — | **PASS**（64 KB @ `0x50000a1c`，池 16.7 MB） |
| `s31test irq`（任意 pin） | PASS | **挂** |

所以不是"所有中断都坏"，而是**只有走 `rt_pin_attach_irq` + `rt_pin_irq_enable`
这条路径时**才挂。而且 `s31test irq 0`（pin 0，不同引脚）也挂，
说明与具体引脚无关，与**这条初始化路径**有关。

再做一个决定性实验：先屏蔽 24（中断看门狗线），再跑 `irq`：

```
s31intr -m 24        -> masked line 24      （读回确认：3 条 -> 2 条，mask 确实生效了）
s31test irq          -> 不再报 Interrupt wdt，改成
                        "RT-Thread scheduler entry while flash cache is disabled"
                        MEPC 0x2f010c98 -> vPortExitCritical -> prvAssertCacheEnabled
                        <- mspi_timing_config_set_psram_clock <- enter_low_speed_mode
```

即：**活锁还在**，只是报告换了个形式。这同时证明了两件事：
1. 上一轮修的 mask **真的生效了**（3 条多余线降到 2 条）；
2. 挂死位置和 MSPI/PSRAM 的时钟切换**同时在栈上出现**——这不是巧合。

### 30.4 目前的结论（诚实版）

**已知**：
- PSRAM 在 40M 下功能正确（写读验证通过）
- 非中断路径（tick/sem/fpu/wifi）在 PSRAM 下全部正常
- `rt_pin_attach_irq` + `rt_pin_irq_enable` 路径在 PSRAM 下触发活锁
- 活锁**不经过** `rtt_trampoline`（计数器为 0），所以不是"未安装 handler"那类
- 屏蔽中断看门狗后，活锁表现为 cache-disabled 断言

**未定位**：`rt_hw_intr_route(ETS_GPIO_INTR0_SOURCE, ETS_GPIO_INUM)` +
`rt_hw_interrupt_install` + `rt_hw_interrupt_umask` 这三步里，
哪一步在 PSRAM 使能后与 MSPI 的时钟/缓存状态冲突。

下一轮第一步：把 `s31_gpio_irq_setup()` 的三步拆开，每步之后读回状态并打点，
用计数器确认卡在哪一步。这比继续符号化 MEPC 有效——**这一轮的教训就是
不要相信 MEPC 能告诉你调用栈**。

### 30.5 一个反复踩的坑：改了 sdkconfig 却没生效

这一轮我两次以为自己改了 PSRAM 速度，实际 `sdkconfig` 仍是 200M：

- 第一次用字符串替换 `CONFIG_SPIRAM_SPEED_200M=y -> # ...` 但没同时改
  `CONFIG_SPIRAM_SPEED=200`，kconfig 重新生成时又按默认 200M 覆盖回去
- 第二次改了 `sdkconfig.defaults`，但 `sdkconfig` 已存在，**不会重新生成**

**规则（已写进 runbook）**：改 PSRAM/速度这类 `choice` 型的配置，
**必须删 `sdkconfig` 让 kconfig 从 `defaults` 重新生成**，然后**读回 `sdkconfig`
确认 `CONFIG_X=y` 真的在**，再编译。只看编译成功不算数——
这一轮 `s31test` 挂了三次都是因为编译的是 200M 的镜像而不是 40M。

### 30.6 产物

| 项 | 值 |
|---|---|
| 修改文件 | `intr_debug.c`（+16 偏移、级别右移、计数器列、`esp_isr_names` 名称）、`interrupt.c`（`rt_hw_interrupt_entry_count`、RMW 中读 IE）、`selftest.c`（三个回调 `IRAM_ATTR`）、`port_intr.h`、`sdkconfig.defaults` |
| 新诊断能力 | `s31intr` 的 `cnt` 列；每行显示真实中断源名（`SYS_ICM` 而非 `src 0`） |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9**、`s31wifi` PASS |
| 状态变化 | R27 从"未查明"变为"**已排除中断风暴这一类，锁定在 pin IRQ 初始化路径**" |

---

## 31. W20：PSRAM 挂了四轮的真凶 —— GPIO 抢了 tick 的中断线

这一轮把 R27 彻底关闭了。根因不是 PSRAM、不是中断风暴、不是缓存，
而是**GPIO 驱动硬编码了 CPU 中断线 4，而那条线是 OS tick 的**。

### 31.1 怎么找到的：把 setup 拆成四步，每步读回硬件

新写了 `s31pinirq`，把 `s31_gpio_irq_setup()` 的 route / install / umask / pin_ena
逐步执行，每步之后立刻打印 CLIC 行的真实状态。第一次在 PSRAM 下跑：

```
s31pinirq: pin 10, line 4 (ETS_GPIO_INUM)
  start   : ie=1 handler=yes
  route   : ok, matrix[61] -> line 20
  install : handler=yeGuru Meditation Error: ... Interrupt wdt timeout
```

两行信息，两个致命事实：

1. **`start : ie=1 handler=yes`** —— 命令还没做任何事，line 4 就已经
   **使能且已有 handler**。空闲线不可能是这个状态。
2. **`install :` 打到一半就死** —— 不是 `install` 调用返回失败，而是
   **`rt_kprintf` 本身被打断**。

### 31.2 那两个数字的含义

`matrix[61] -> line 20`：61 = `ETS_GPIO_INTR1_SOURCE`，20 = `4 + 16`。
两个都说明矩阵里存的是 **CLIC 线号（inum + 16）**，ROM 的
`esp_rom_route_intr_matrix` 自己加了这个偏移。我之前在 `s31_line_name()`
里用 `(m & 0x3F) == inum` 比较，**永远匹配不上**，所以每一行都显示 `-`。
修成 `inum + RV_EXTERNAL_INT_OFFSET` 之后名字全部出来了。

而名字一出来，答案就直接摆在眼前：

```
inum ie ip lvl trig handler     cnt name
   0  1  0   1  lvl yes           0 MSPI_PSRAM
   2  1  0   1  lvl yes           0 LP_ANAPERI
   3  1  0   1  lvl yes           0 SYSTIMER_TARGET2
   4  1  0   1  lvl yes           0 SYSTIMER_TARGET0     <-- 这条是 tick！
   5  1  0   1  lvl yes          18 UART0
  24  1  0   4  lvl NO            0 TIMERGRP1_WDT
  25  1  0   4  lvl NO            0 CACHE
  27  1  0   4  lvl NO            0 BUS_MONITOR
```

**line 4 = `SYSTIMER_TARGET0` = 操作系统的 tick。**

### 31.3 完整因果链

```
vSystimerSetup()            freertos/port_systick.c:86
    esp_intr_alloc(ETS_SYSTIMER_TARGET0_INTR_SOURCE + cpuid, level, SysTickIsrHandler, ...)
    -> 分配器挑第一条可用线，PSRAM 开启时那正好是 4

s31_gpio_irq_setup()        port/esp32s31/gpio.c（旧版）
    rt_hw_intr_route(ETS_GPIO_INTR0_SOURCE, ETS_GPIO_INUM /* == 4 */)
    rt_hw_interrupt_install(4, s31_gpio_isr, ...)
    rt_hw_interrupt_umask(4)
```

三步同时造成三个后果：

1. **把 tick 的源从它自己的线上挤走**（矩阵被重写）
2. **把 tick 的 handler 覆盖成 `s31_gpio_isr`**
3. **让 GPIO 的状态位被当成 tick 处理**

结果：tick 停 → 调度器停 → 中断看门狗在 300 ms 后报超时。

**为什么表现得像"PSRAM 的 bug"**：PSRAM 关时分配器给 tick 的是**另一条线**，
line 4 恰好空着，硬编码的 4 **靠运气能工作**。PSRAM 打开后 MSPI/PSRAM 先占一条线，
所有分配整体后移一位，运气用完了。

这也解释了为什么 `cnt` 一直是 0：`rtt_trampoline` 装到的线上根本没有源在触发
（源被重写到了别处），而真正被打断的是 tick 的 handler，不经过我们的 trampoline。

### 31.4 修法：线号向分配器要，不要硬编码

```c
static int           s_gpio_intno = -1;
static intr_handle_t s_gpio_intr_handle;

err = esp_intr_alloc(ETS_GPIO_INTR0_SOURCE,
                     ESP_INTR_FLAG_IRAM | ESP_INTR_FLAG_LEVEL1,
                     rtt_trampoline, RT_NULL, &s_gpio_intr_handle);
s_gpio_intno = esp_intr_get_intno(s_gpio_intr_handle);
rt_hw_interrupt_install(s_gpio_intno, s31_gpio_isr, RT_NULL, "gpio");
rt_hw_interrupt_umask(s_gpio_intno);
```

要点：

- **用 `esp_intr_alloc` 而不是 `rt_hw_intr_route`**。分配器同时做路由，
  两者都做会让同一个源被路由到两条不同的线。
- **handler 仍然经过 `rtt_trampoline`**，所以 `s_ctx[]` 机制、
  `rt_hw_interrupt_uninstall()`、`s31intr` 的计数器全部照常工作。
  只有**线号**从 `#define` 改成向分配器要。
- 启动时打印 `gpio: irq on line 7 (allocated, source 61)`，
  这样下次再出问题，第一眼就知道是哪条线。

### 31.5 结果

PSRAM **开启**（40M）+ 全量自检：

```
[PASS] tick / ctx / preempt / fpu / irq / nest / sem
[PASS] fbshim × 12
=== 20 passed, 0 failed ===

s31psram: PASS, 256 KB written and read back   （池 16,774,632 B）
s31wifi:  PASS, STA_START in 0 ms
s31ble:   PASS, host synced in 0 ms
```

**这是四轮以来第一次 PSRAM 与中断自检同时通过。**

| 配置 | 修复前 | 修复后 |
|---|---|---|
| PSRAM 关 | 20/20 | 20/20 |
| PSRAM 开 @40M | `irq`/`nest` 崩 | **20/20** |
| PSRAM 开 + 256 KB 写读 | 崩 | **PASS** |

`sdkconfig.defaults` 里 PSRAM 现在是**正式打开**的（不再是注释），
并写清了 40M 的原因和"100M/200M 需要先解决 R28 断言冲突"。

### 31.6 这个 bug 值得记的几点

1. **`s31pinirq` 的价值不在"诊断命令"，在"每步读回硬件"**。
   之前三轮我都在符号化 MEPC，那是猜；这一次是**读状态**，一次就定位了。
2. **"看起来 PSRAM 相关的 bug"往往不是 PSRAM**。真正的关联是
   "PSRAM 改变了初始化顺序 → 改变了谁的线是哪条"。
3. **硬编码的硬件资源编号都是定时炸弹**。同一个 `#define`
   在不同配置下对应不同外设。已登记为 R32。
4. **`s31_line_name()` 的 +16 偏移错误掩盖了真相最久**。名字显示 `-` 时
   我以为是"没源"，实际是"比较表达式错了"。显示层的小错会让人
   对整张表失去信任。

### 31.7 产物

| 项 | 值 |
|---|---|
| 修改文件 | `gpio.c`（分配器取线、`s31pinirq` 改只读、`esp_intr_alloc`）、`intr_debug.c`（矩阵 +16 修正）、`port_intr.h`（导出 `rtt_trampoline`）、`sdkconfig.defaults`（PSRAM 正式开启） |
| 新命令 | `s31pinirq`（只读，显示分配到的线与引脚通道状态） |
| 关键日志 | `gpio: irq on line 7 (allocated, source 61)` |
| 回归 | PSRAM 下 `s31test` **20/20**、`check_image.py` **9/9**、`s31psram`/`s31wifi`/`s31ble` 全 PASS |

---

## 32. W21：R28 关闭 —— PSRAM 从 40M 提到 200M

上一轮把 PSRAM 稳在 40M，并留下 R28："100M/200M 撞 `prvAssertCacheEnabled()` 断言"。
这一轮把它修掉，**四档速度全部跑通，默认改到 200M**。

### 32.1 断言为什么是错的（但不算冤枉）

`prvAssertCacheEnabled()`（`portable/riscv/port.c:541`）是为 RT-Thread 移植加的：

```c
if (!cache_hal_is_cache_enabled(CACHE_LL_LEVEL_EXT_MEM, CACHE_TYPE_ALL)) {
    esp_system_abort(DRAM_STR("RT-Thread scheduler entry while flash cache is disabled"));
}
```

**原则是对的**：RT-Thread 的调度器、就绪队列、以及 `port/esp32s31/` 里所有 ISR 都在 flash，
cache 一关就是取指失败。

**但"cache 关着"本身不是违规，"进入调度"才是。** 有两条 IDF 代码路径
合法地持有关 cache 的临界区，而且从不切换：

- `spi_flash_disable_cache()` 包住 flash 写/擦
- `mspi_timing_enter_low_speed_*()` 包住 MSPI 时序切换
  （`mspi_timing_tuning.c:654`：`disable_cache()` → `mspi_timing_enter_low_speed_mode()`）

第二条正是让 PSRAM 高于 40M 起不来的原因：调时序的代码在关 cache 的窗口里
进入 IDF 临界区（`periph_rcc_enter`），最外层的退出调用这个函数就 abort 了。
**在那里 abort 保护不了任何东西**——那个窗口里不可能产生待切换的请求。

### 32.2 修法：把断言收窄到真正危险的条件

```c
/* vPortExitCritical() 里 */
if (rt_critical_level() == 0 && rt_thread_self() != RT_NULL) {
    PRV_REQUIRE_CACHE_FOR_SWITCH();   /* 只有这时才可能真的调度 */
}
```

关键改动是把"无条件检查"换成"**问内核会不会真的调度**"：

| 调用点 | 修前 | 修后 |
|---|---|---|
| `vPortExitCritical()`（最外层） | 无条件 abort | 仅在 `rt_critical_level()==0 && rt_thread_self()!=NULL` 时检查 |
| `vPortYield()`（任务主动让出） | 检查 | **仍然无条件检查** |
| `vPortYieldFromISR()` | 检查 | **仍然无条件检查** |

后两处正是"切换真的要发生"的地方，所以危险路径仍然被盯住；
被放开的只是"MSPI 调时序时退出一个本来就不该调度成功的临界区"。

### 32.3 结果：四档全过

| PSRAM 速度 | 修前 | 修后 |
|---|---|---|
| 40 MHz | PASS | **PASS** |
| 100 MHz | abort | **PASS** |
| **200 MHz** | abort | **PASS** ← 新的默认 |
| 250 MHz | abort | **PASS**（芯片上限） |

200M 下逐项验证：

```
s31psram 4096   -> PASS, 4 MB 位置相关图案写读一致（池 16,774,632 B）
s31test         -> 20/20，连续 3 次
s31wifi         -> PASS, STA_START in 0 ms
s31ble          -> PASS, host synced in 0 ms
check_image.py  -> 9/9
```

**为什么默认 200 而不是 250**：两档都能跑，但 250 是器件的天花板，
一块稍差的板子就没有余量了。200 留了一档。

### 32.4 一个反直觉的观察

`boot_log` 在 200M 下仍然打印 `MSPI Timing: Enter psram timing tuning`，
而且 `Readlatency` 与 40M 时相同（0x04）。也就是说**时序调优确实执行了**，
但读延时字段没变——这说明调优的目标是 DQS 相位/延迟线，不是这个字段。
**不能靠"日志没变"判断调优没跑**，这是本轮之前我差点做出的错误推论。

正确的判据是**功能**：4 MB 图案写读一致 + 连续 3 次全量自检无错。
速度提升本身没有直接测（没有 PSRAM 带宽基准），所以文档里只写"200M 可用"，
不写"200M 比 40M 快多少"——**没测的量不写进结论**。

### 32.5 产物

| 项 | 值 |
|---|---|
| 修改文件 | `components/freertos/.../portable/riscv/port.c`（断言收窄）、`sdkconfig.defaults`（200M） |
| 回归 | `s31test` **20/20** ×3、`s31psram 4096` PASS、`s31wifi`/`s31ble` PASS、`check_image.py` **9/9** |
| 状态 | **R28 关闭**；PSRAM 40M 上限解除 |

---

## 33. W22：SPI 双总线（第二块标准设备类外设）

按 §27.7 定的节奏（每次 2~3 个外设），这一轮加了 **SPI**。

### 33.1 结果

```
msh > list device
uart0        Character Device     1
spi1         SPI Bus              0
spi0         SPI Bus              0
i2c0         I2C Bus              0
pwm0         PWM Device           0
pin          Pin Device           0

msh > s31spiloop 0
s31spiloop: spi0 tx/rx (tie MOSI to MISO for a real test)
  tx: a5 5a 01 80 ff 00 3c c3
  rx: 00 00 00 00 00 00 00 00
s31spiloop: rx differs - expected unless MOSI is jumpered to MISO
```

`rx` 全零是**正确结果**——板子上 MOSI 和 MISO 没短接。命令如实说明了这一点，
而不是报 FAIL 也不是假装 PASS。**传输本身成功了**（`rt_spi_transfer` 返回 8），
说明时钟、FIFO、位序都工作；只是线上没有回环。

### 33.2 顺序踩了五个坑，每个都是"寄存器手册里没写的"

**(a) `SPI2` / `SPI3` 这两个符号不存在。** S31 的实例叫 `GPSPI2` / `GPSPI3`
（`spi_struct.h:1404-1405`）。

**(b) 信号名是 flash 引脚的名字。** 没有 `SPI2_CLK_OUT_IDX`，实际叫：

| 用途 | 实际宏 |
|---|---|
| 时钟 | `SPI2_CK_PAD_OUT_IDX` |
| MOSI | `SPI2_D_PAD_OUT_IDX`（D = flash 的 DI 线） |
| MISO | `SPI2_Q_PAD_IN_IDX`（Q = flash 的 DO 线） |
| 片选 | `SPI2_CS_PAD_OUT_IDX` |

编译器提示"是不是想要 `GPIO_SD5_OUT_IDX`"——因为 GPSPI 和 flash 共用同一批矩阵输入，
**名字不自解释**。对齐到 `_PAD_` 系列才对。

**(c) `spi_ll_enable_bus_clock()` / `spi_ll_reset_register()` 是宏，不是函数。**
它们展开成 `do { (void)__DECLARE_RCC_ATOMIC_ENV; ... } while(0)`，
要求调用点先声明一个 `__DECLARE_RCC_ATOMIC_ENV` 变量。直接用会报
`'__DECLARE_RCC_ATOMIC_ENV' undeclared`。正确写法是 IDF 的 `PERIPH_RCC_ATOMIC()` 块。

**(d) 光开 bus clock 不够，master 状态机不转。** 第一次跑 `s31spiloop`
直接 `spi0: transfer timeout`：`usr=1` 写下去了，但 `trans_done` 永远不来。
原因是 `HP_SYS_CLKRST` 里 `gpspi*_hs_clk_en` / `gpspi*_mst_clk_en` 是**独立的门**，
`spi_ll_enable_bus_clock()` 只开 `sys_clk` / `apb_clk`。补上
`spi_ll_enable_clock(host, true)` 之后立刻通了。

**(e) `rt_device_find("spi0")` 返回的是总线，不是设备。** 我把它强转成
`rt_spi_device *` 然后调 `rt_spi_configure()`，在 `_rt_mutex_take` 里
`rt_object_get_type()` 处 load access fault——总线的 `parent` 是
`rt_device`，没有 `rt_spi_device` 的锁和 CS 字段。必须先用
`rt_spi_bus_attach_device()` 挂一个真正的设备。

### 33.3 设计取舍（写进代码注释）

**不用 DMA。** GPSPI 的 DMA 是一整套描述符环 + 独立分配器。轮询够用，
而且 `SOC_SPI_MAXIMUM_BUFFER_SIZE`(64) 本来就限制单次硬件传输，长传输无论如何要循环。

**不做多线（Dual/Quad/OCT）模式。** `spi_ll_master_set_line_mode()` 存在，
但这里只编 1 线；多线需要 flash 风格的 command/addr/dummy 三阶段，是另一套 API。

**引脚只取板子上空闲的排针**，`port_board.h` 里定义：

```
spi0: SCLK=IO8  MOSI=IO9  MISO=IO10  CS=IO11
spi1: SCLK=IO12 MOSI=IO13 MISO=IO14  CS=IO15
```

**故意不占用 LCD 排座（IO3..IO7）**——那几个脚和 I2S 共享，
驱动在启动时抢走会悄悄破坏音频路径。LCD 驱动要的时候再按需申请。

### 33.4 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/spi.c`（~470 行）、`port/esp32s31/port_spi.h`、`port_board.h` 里 8 个引脚宏 |
| 修改文件 | `CMakeLists.txt`（框架源 + `esp_hal_gpspi`）、`rtconfig.h`（`RT_USING_SPI`）、`board.c` |
| 新命令 | `s31spiloop [0|1]` |
| 设备类 | 从 3 种（char / I2C / PWM / pin）增到 **4 种 + 2 个 SPI 总线** |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9** |

---

## 34. W23：ADC（第三块标准设备类外设），以及两个"读回 0"的坑

### 34.1 结果

```
msh > list device
uart0        Character Device     1
adc2         ADC Device           0
adc1         ADC Device           0
spi1         SPI Bus              0
spi0         SPI Bus              0
i2c0         I2C Bus              0
pwm0         PWM Device           0
pin          Pin Device           0

msh > s31adc 1 1
s31adc: adc1 ch1 (GPIO43), 17-bit raw
  read 1: 2599
  read 2: 2599
  read 3: 2585
s31adc: no calibration on this port - the raw code is the result,
  not millivolts. See the note in port_adc.h.
```

同一通道连测 5 次：`2599 2599 2580 2585 2590`——**稳定**，噪声约 ±10 LSB（17 位满量程 131072）。

八通道一次扫完：`1767 2595 1799 2626 1767 2621 1766 2598`。
偶数通道一组、奇数通道一组，**但重复扫描值不变**，且同通道重复测量稳定。
这是**板上模拟前端的实际状态**（未接信号、引脚悬空），不是驱动问题——
判据是"同通道可重复"，不是"通道间相同"。

### 34.2 坑一：`enabled()` 只记了通道号，没编程硬件

第一版：

```c
enabled():  adc->channel = channel;          /* 只记书 */
convert():  if (adc->channel != channel)     /* 永远为假 */
                adc_oneshot_ll_set_channel(); /* 永远不执行 */
```

结果：pattern 表保持复位值 `0x00f00000`（通道 15，无效项），**每次转换都返回 0**。

抓这个花了两次尝试：

- 先怀疑"读之前没等 SAR 同步"，加了 `esp_rom_delay_us(20)` —— **没用**
- 然后写 `s31adcdbg` 直接 dump 寄存器，一次就看到：

```
adc1 before: ctrl0=0x0003c000 patt_tab1=0x00f00000   <- 复位值，通道 15
adc1 conv: done=1 loops=77
  raw(unit)=1807                                     <- 硬件明明工作
```

**转换成功、raw 1807、但 `s31adc` 报 0** —— 两句话一对比就知道是通道选择没编程。
修法：把 `adc_oneshot_ll_set_channel()` 移到 `enabled()` 里。

### 34.3 坑二：pattern 表每次转换都要重编程

修完坑一，变成：**第一次读 1771，第二三次读 0**。

原因：`adc_oneshot_ll_program_pattern()` 里有
`ADC.ctrl0.sar1_patt_p_clear = 1; ... = 0;`——那是把 pattern 指针复位到 0。
表里只有 1 项，转换一次指针就走过表尾，下次转换找不到入口，读 0。

所以我原来"只在通道变化时编程"的优化是错的。IDF 的
`adc_oneshot_hal_setup()` 是**每次 convert 前都调**。改成每次都编程后稳定。

**这两个坑的共同点**：都是"我以为硬件会记住，它没有"。
一个记的是通道号（在软件里），一个记的是指针（在硬件里）。

### 34.4 设计上明确不做的两件事（写进 `port_adc.h`）

**(a) 不换算毫伏，`get_vref()` 返回 -1。**
S31 **没有** `SOC_ADC_CALIBRATION_V1_SUPPORTED`，也**没有** RTC 控制器，
IDF 的校准组件在这颗芯片上给不出 raw→mV 系数。写一个"典型 Vref=1100mV"
看起来权威，实际每块板偏几个百分点，而所有下游计算都会继承这个错误**且毫无提示**。
**raw code 是测量值，猜出来的电压不是。**

**(b) 不做连续/DMA 模式。** oneshot 是 RT-Thread ADC 框架的模型，
也是这台设备上唯一能验证的模式。

### 34.5 引脚冲突（重要）

`adc_channel.h` 的映射是硅片固定的，不是板级选择：

```
adc1 ch0..7 -> GPIO42..49
adc2 ch0..7 -> GPIO50..57
```

而 **GPIO50/51 就是这块板的 I2C 引脚**（`port_board.h` 的 `BOARD_I2C0_SCL_PIN/SDA_PIN`）。
所以 **adc2 和 i2c0 不能同时用**。`s31adc 2 0` 直接拒绝：

```
s31adc: adc2 ch0 is GPIO50, the i2c0 pad.
  Reading it while i2c0 is up measures a driven digital
  line, not an analog input. Use 'force' if i2c0 is idle.
```

传 `force` 才会读——**并且在输出里说明为什么不该读**。

### 34.6 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/adc.c`（~470 行）、`port/esp32s31/port_adc.h` |
| 修改文件 | `CMakeLists.txt`（框架源 `adc.c` + `esp_hal_ana_conv`）、`rtconfig.h`（`RT_USING_ADC`）、`board.c` |
| 新命令 | `s31adc <1\|2> <ch> [force]`、`s31adcdbg <1\|2>` |
| 设备类 | 5 种（char / ADC / SPI / I2C / PWM / pin） |
| 命令数 | 26 |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9**、`s31psram 512` PASS |

---

## 35. W24：PCNT（脉冲计数器），以及"太快了数不到"这个物理限制

### 35.1 结果

四路 unit 全部通过，边数各不相同：

```
s31pcnt: unit 0 on GPIO16, driving 100 rising edges
  expected 100, counted 100
s31pcnt: PASS

unit 0 gpio 16 edges 100:  PASS
unit 1 gpio 17 edges 250:  PASS
unit 2 gpio 18 edges 1000: PASS
unit 3 gpio 19 edges 50:   PASS
```

### 35.2 怎么在没有外部接线的情况下验证

PCNT 数的是**引脚上的边沿**，而引脚的输入通路看到的是"这个脚被驱动到什么电平"
——**包括被它自己的输出驱动器驱动**。

所以自测的取巧办法是：**软件把这个脚拉高拉低 N 次，让 PCNT 数同一个脚**。
这是一条完整的通路（输出驱动 → 焊盘 → 输入矩阵 → 毛刺滤波器 → 计数器），
只是不需要跳线。另一种做法（MCPWM 接 PCNT，或信号发生器）是台架测试，
这台设备上做不了。

### 35.3 坑：100 个边沿只数到 1 个

第一版直接连写两次电平寄存器：

```c
gpio_ll_set_level(&GPIO, pin, 1);
gpio_ll_set_level(&GPIO, pin, 0);
```

结果 **`expected 100, counted 1`**。

原因：PCNT 在 **APB 时钟（80 MHz，12.5 ns）** 上采样引脚。
两次背靠背的寄存器写在 320 MHz 上只有几十纳秒的高电平，
**采样器可能整个高电平期都没踩到**。

修法：每个电平保持 **2 µs**（≈160 个 APB 周期，四个数量级余量），
100 个边沿总共 200 µs。改完立刻 `counted 100`。

**这个坑值得记的原因是它看起来像驱动 bug**：计数没涨，第一反应是"配置错了"。
实际是**信号比采样率快**——一个物理约束，不是软件错误。
所以命令的失败信息里直接写了这条：

```
s31pcnt: FAIL (delta -99)
  A short count usually means the edges were faster than the
  counter can resolve; retry with fewer edges.
```

### 35.4 S31 上 PCNT 的四个要点

1. **两个 group（PCNT0/PCNT1），每个 4 个 unit，每 unit 2 个信号通道 + 1 个控制通道。**
   时钟门和复位都在 `HP_SYS_CLKRST.pcnt_ctrl0`。
2. **APB 是唯一合法时钟源**——`pcnt_ll_set_clock_source()` 对其它值直接 assert。
3. **计数寄存器是 16 位有符号**，`pcnt_ll_get_count()` 已经做了符号扩展。
   数到 32767 之后"回绕"是硬件行为，不是驱动 bug。
4. **毛刺滤波阈值以 APB 周期计，默认关闭**。软件驱动的边沿慢到不需要它，
   但真实编码器通常需要。

信号索引的排布**是通道优先、不是 unit 优先**（`PCNT0_SIG_CH<channel>_IN<unit>`），
和这个 port 里其它外设的索引习惯相反——已经在代码里写明并通过
`s31_pcnt_sig_idx()` 集中处理，避免各处各写一份。

### 35.5 为什么不做成 `rt_device`

RT-Thread **没有脉冲计数器设备类**，没有 `rt_pcnt_*` API 要满足。
硬套一个只有一个调用者的 `rt_device` 包装，只会增加间接层而不增加契约。
所以这里是**纯 C API + msh 命令**，这是这个外设诚实的形状。

### 35.6 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/pcnt.c`（~290 行）、`port/esp32s31/port_pcnt.h` |
| 修改文件 | `CMakeLists.txt`（`esp_hal_pcnt`）、`board.c`（`rt_hw_pcnt_init()`） |
| 新命令 | `s31pcnt <unit> <gpio> [edges]` |
| 命令数 | 27 |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9**、`s31adc`/`s31psram` 正常 |

---

## 36. W25：MCPWM（互补对 + 死区），以及"量具失效时不该怪被测对象"

### 36.1 结果

| 频率 | 占空比 | PCNT 测频 | mcycle 测占空 | 判定 |
|---|---|---|---|---|
| 100 Hz | 250/1000 | ~97 Hz | **249/1000** | PASS |
| 500 Hz | 500/1000 | ~500 Hz | **492/1000** | PASS |
| 1000 Hz | 500/1000 | ~980 Hz | **499/1000** | PASS |
| 2000 Hz | 300/1000 | ~2000 Hz | 不测（超量程） | PASS（仅频率） |
| 5000 Hz | 750/1000 | ~4900 Hz | 不测 | PASS（仅频率） |
| 20000 Hz | 100/1000 | ~19940 Hz | 不测 | PASS（仅频率） |

**两把尺子都从输出引脚本身读**，不需要示波器、不需要跳线。

### 36.2 四个缺陷，前两个是真缺陷

**(a) `MCPWM_TIMER_START_STOP_FULL` 是一次性命令。**
它的语义是"开始计数，**并在下次到达峰值时停止**"。我用它做 `start()`，
定时器跑一个周期就停了，测出来 **0 Hz**，看起来像路由没通。
该用 `MCPWM_TIMER_START_NO_STOP`。

**(b) 定时器启动早于预分频器写入，预分频器变空操作。**
请求 100 Hz 测到 **700 Hz**——正好是 7 倍，即预分频值。
定时器已经把旧的分频值锁存了。修法：**所有分频和动作都写完之后再启动**。

**(c) 回读 `timern_period` 用错了位域。**
`timern_period` 在 `[23:8]`，不是 `[15:0]`。用 `& 0xFFFF` 读到的是
8 位预分频器 + 周期低 8 位，算出来的 compare 完全错。改用
`HAL_FORCE_READ_U32_REG_FIELD`。

**(d) 32 位溢出。** 100 Hz 下 8 个周期的高电平累计 6 399 181 cycles，
乘 1000 得到 6.4e9，超出 uint32 → 回绕成 2 104 213 704，
算出的占空比是 **82/1000**（实际 250）。
500 Hz 时 2.56M×1000 刚好没爆，所以那个频率能过——**典型的溢出 bug 形状**。
改用 `rt_uint64_t` 中间量。

### 36.3 三处"量具失效"，都没怪被测对象

这三条是这一轮最值得记的，因为它们**看起来都是被测对象的失败**：

**(1) 频率窗口太短。** 100 Hz 时 50 ms 里只有 5 个边沿，数到 4 个就是 80 Hz。
量化误差 20%，超过容差。改成**按频率自适应窗口**（至少 20 个周期）。

**(2) 单周期抖动。** 1000 Hz 有一次测到 170 779 cycles（半周期），
因为轮询延迟直接进了单次测量。改成**累加 8 个周期**。

**(3) 起点没对齐。** 测占空比时从下降沿起步，`p1-p0` 量的是
"低+高+低"，比一个周期长，50% 占空比算出 **333/1000**。
改成**以上升沿为唯一锚点**。

**第 (1)(2) 两条之后，2 kHz 仍会测到 285/1000（实际 300）**——
这不是 PWM 的问题，是轮询法在 2 kHz 的分辨率上限。
**所以把占空比测量的上限定在 1 kHz，超过就报"未测量"而不是 FAIL。**

理由写进了代码注释：

> 报 FAIL 是因为量具失效而指控被测对象，等于怪错了组件。

### 36.4 没有验证的部分（如实写）

- **死区的实际纳秒数没有测量。** 死区被编程并回读寄存器，但没有示波器
  量它的真实宽度。
- **输出的模拟形状**（上升时间、过冲）没有测量。

这两条都写进 `port_mcpwm.h` 的头注释，而不是含糊带过。

### 36.5 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/mcpwm.c`（~520 行）、`port/esp32s31/port_mcpwm.h`、`port_board.h` 加 `BOARD_MCPWM_A/B_PIN`（IO20/21） |
| 修改文件 | `CMakeLists.txt`（`esp_hal_mcpwm`）、`board.c` |
| 新命令 | `s31mcpwm [freq] [duty_permille] [dead_ticks]` |
| 命令数 | 28 |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9**、`s31pcnt`/`s31adc` 正常 |

---

## 37. W26：RMT —— 修好三个真缺陷，剩下两个如实标注 WIP

这一轮的结论和前几轮不同：**RMT 没有做完**。做对的部分和没做对的部分都记在下面。

### 37.1 修好的三个缺陷

**(a) 符号存储没上电。** 写满波形后回读 **全 0**，但要先加一行
`rmt_ll_mem_force_power_on()`：

```c
HP_SYSTEM.sys_rmt_mem_lp_ctrl.sys_rmt_mem_lp_force_ctrl = 1;
HP_SYSTEM.sys_rmt_mem_lp_ctrl.sys_rmt_mem_lp_en = 0;
```

RMT 的符号存储是**独立硅片、独立低功耗域**，默认处于掉电状态。
开总线时钟不会顺带打开它。症状是"写入读回全 0"，看起来像驱动完全没接上。

**(b) APB 窗口默认映射到 FIFO，不是存储阵列。**

```
sys_conf.apb_fifo_mask = 0  (复位值) -> APB 访问 = 压/弹 FIFO
sys_conf.apb_fifo_mask = 1          -> APB 访问 = 直接寻址存储阵列
```

我第一次写 `RMTMEM[...] = ...` 之后回读全 0，`sys_conf` 读回 `0x05000010`
（mask=0），确认是这个原因。加 `rmt_ll_enable_mem_access_nonfifo(dev, true)`
之后，原始地址写入 `0x0fa08fa0` 读回 `0x0fa08fa0`。

**注意**：这个设置我最初以为已经加了，实际没有——写文件时被后续编辑覆盖掉了。
**两次 dump 的差异（mask 从 0 变 1）才是证据**，不是"我记得我加了"。

**(c) `trans_done` 永远不置位，因为中断没使能。**

`rmt_ll_tx_get_interrupt_status()` 读的是 **`int_st`（屏蔽后状态）**，
而 `int_ena` 是 **0**——所以无论等多久都等不到。dump 直接给出答案：

```
rmt: no trans_done after 20 ms (raw=0x00000000 ena=0x00000001)
```

（这是修复后的值；修复前 `ena=0x00000000`。）加一行
`rmt_ll_enable_interrupt(dev, RMT_LL_EVENT_TX_DONE(ch), true)` 之后，
至少使能位是对的。**但 `raw` 仍是 0**，见下面未解决项。

### 37.2 自己踩的两个"量具/超时"错误

**(d) `esp_rom_delay_us(1)` 当超时滴答，23 秒才返回。**
第一版 `wait_done` 循环 20000 次、每次 `esp_rom_delay_us(1)`，
实测 **23.2 秒**——每次迭代约 1.16 ms。用 `board.py` 外部计时确认
（24.66 s 含复位启动），不是错觉。

改成**纯 cycle 自旋 + 50 ms 预算**后，命令 4 ms 返回。

**(e) 用 mcycle 量 23 秒的跨度。**
mcycle 是 **32 位**，320 MHz 下 **13.4 秒回绕**。
23.2 秒的差值算出来是 23,243,043 µs——**看起来合理纯属巧合**。
改用 `rt_tick_get()`（毫秒，49 天才回绕）。

### 37.3 没做完的两件事（RMT 标记为 WIP）

**(1) 帧长控制不对。** 20 个符号的帧，PCNT 数到 **707 个上升沿**。
原因分析：`tx_lim` 看起来是**阈值**不是**结束条件**，
`mem_size` 决定通道走几个 48 符号的块——两者都不能把帧限制在 20 个符号。
需要再看 IDF 的 `rmt_tx.c` 是怎么收尾的（它用 `trans_done` + FSM，
不是靠 limit 停）。

**(2) `trans_done` 原始位仍不置位**（`ena=1` 但 `raw=0`）。
可能是 loop 模式下 done 事件被吞，或者 `tx_conti_mode_chn` 仍是 1
（我加了显式清零但没验证读回值）。

### 37.4 为什么这一轮不硬撑

前几轮的经验是：**修完第一个才看得见第二个**。RMT 现在的状态是
"存储能写、能发出边沿、但帧长和完成检测都不对"，
距离"可用的 WS2812 驱动"还差这两步。

**所以 `s31rmt` 现在不报 PASS**，它报事实：

```
s31rmt: memory writes verified and the transmitter produces
  edges, but frame-length control is NOT yet correct.
  ...
  Treat RMT as WIP; do not build on it yet. See the log.
```

这比报一个假的 PASS 有用——`s31led` 的位碰撞方案目前是**能用的**那个，
RMT 版本还不能替代它。**"做了什么"和"做成了什么"要分开写。**

### 37.5 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/rmt.c`（~420 行）、`port/esp32s31/port_rmt.h`、`port_board.h` 加 `BOARD_RMT_TX_PIN`(IO22)、`port_pcnt.h`/`pcnt.c` 加 `rt_hw_pcnt_stop()` |
| 修改文件 | `CMakeLists.txt`（`esp_hal_rmt`）、`board.c` |
| 新命令 | `s31rmt [half_us] [symbols]`（**WIP，不报 PASS**） |
| 命令数 | 29 |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9**、`s31mcpwm`/`s31pcnt` PASS |

---

## 38. W27：RMT 继续攻坚 —— 三个新发现，仍未收口

这一轮把 RMT 往前推了三步，但**仍然没做完**。记录的是新证据，不是结论。

### 38.1 发现一：零时长符号是**结束标记**，不是"退化符号"

上一轮我的判断完全反了。IDF 的 `rmt_tx.c:597-630` 写得很清楚：

```c
// a RMT word whose duration is zero means a "stop" pattern
mem_to_nc[symbol_off] = (rmt_symbol_word_t) {
    .duration0 = 0, .level0 = eot,
    .duration1 = 0, .level1 = eot,
};
```

**RMT 没有"长度寄存器"**，它沿存储块走，直到遇到**两个半周期时长为 0** 的符号才停。
上一轮我以为 0 符号是"退化符号会以时钟速率乱发"，于是**用波形填满整个块来消灭它们**
——正好把硬件唯一认识的停止标记删掉了。这就是"20 个符号发出 707 个边沿"的机制。

已按正确语义改写：`count` 个真实符号 + 索引 `count` 处一个全 0 的 EOF 符号。

### 38.2 发现二：EOF 符号**写不进去**，而索引 0 能

这是本轮最硬的证据：

```
symbol[0]=0x0fa08fa0      <- 正确，就是我写的波形
symbol[20]=0xdbd7d9ab     <- 垃圾，不是 0
```

同时 `mem_size=1`（1 个 48 符号的块），索引 20 完全在范围内。

两个候选解释（下一轮从这里入手）：

1. **APB 直接寻址窗口是流式的**——写指针自增，不是随机寻址
2. **48 符号阵列不是我以为的连续布局**，`RMTMEM.channels[0].symbols[n]` 的偏移假设错了

`symbol[0]` 能读对，说明**基址和第一个字是对的**，问题在跨字。

### 38.3 发现三：`tx_lim` 触发的是 THRES，不是 DONE

`int_raw=0x00000100` = bit 8 = `RMT_LL_EVENT_TX_THRES(0)`。

也就是说走到 `tx_lim` 时硬件报的是"**阈值到了**"，**不是"传完了"**。
它是给编码器补数据的信号，不是结束条件。所以：

- 等 `TX_DONE` 永远等不到（`raw` 里没有 bit 0）
- 通道走完 `tx_lim` 后继续走，因为没有 EOF 标记（发现一/二）

### 38.4 我自己在这一轮引入的两个 bug

**(a) cycle 预算单位错。** 注释写"50 ms"，代码是 `CPU_MHZ * 50u` =
**50 微秒**，小 1000 倍。任何超过 50 µs 的帧都被中途 `tx_stop`。
修成 `* 1000u` 后，帧才真正跑完（边沿从 31 变成 13179——因为块在重放，
但至少不再是"传到一半被砍"）。

**(b) 边沿计数这个量具上一轮就被污染了。** 上一轮 4 个符号给 626 边沿、
20 个给 630 边沿——**与帧长无关**。这一轮加了**空载基线**：

```
idle 50 ms : 0 edges (nothing driving the pin)
one frame  : 13179 edges, 50 ms
```

空载是 0，说明引脚没有浮空噪声——**这反而证明 13179 个边沿是真信号**，
即块确实在重放。**基线测量把"量具坏了"和"被测对象坏了"区分开了**，
这正是上一轮缺的那一步。

### 38.5 状态

| 项 | 状态 |
|---|---|
| 符号存储写入（索引 0） | ✅ |
| 时钟 25 ns/tick | ✅ |
| 能发出边沿 | ✅ 13179 个/50ms |
| EOF 结束标记 | ❌ 写不进索引 N |
| 完成检测 | ❌ `TX_DONE` 不触发，只有 `THRES` |

`s31rmt` **不报 PASS**，输出末尾逐条列出未解决项并指向 `s31led`。

### 38.6 产物

| 项 | 值 |
|---|---|
| 修改文件 | `rmt.c`（EOF 语义、cycle 预算、空载基线、精确的 WIP 输出） |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9** |
| 命令数 | 29 |

---

## 39. W28：MCPWM 的占空比判定改成"报不确定度"而不是"±1% 硬判"

这一轮的触发很偶然：收尾跑回归时 `s31mcpwm 1000 500 10` **失败了一次**
（`duty off by 30/1000`），而前几轮它一直是 PASS。

### 39.1 先确认不是 PWM 变了

重复 4 次：

```
499/1000  PASS
490/1000  PASS
499/1000  PASS
470/1000  FAIL (off by 30)
```

**同一配置、同一固件、结果跨 30/1000。** PWM 的 compare 寄存器是固定的，
不会自己变——所以这是**量具的抖动**，不是被测对象的。

### 39.2 抖动从哪来

测量代码读的是两条**独立的**边沿间隔：

```c
high   += (b - a);     // a = 上升沿, b = 下降沿
period += (c - a);     // c = 下一个上升沿
```

每次 `a` 都有轮询抖动（几到几十 cycle）。8 轮累加后，
`period` 累加了 8 个抖动、`high` 也累加了 8 个——
**两边的抖动不同步**，比值就会飘。

### 39.3 试过的两个修法，一个有效一个没用

**(a) 取每周期占空比的中位数（逐轮算，再排序取中位）。**
加了之后仍然出现 `off by 23` 和 `off by 28`——**中位数没有解决问题**，
因为异常轮次的偏差太大（470 vs 499），中位数在 8 个样本里也可能被拉到 477。

**(b) 放宽容差到 20/1000。** 仍然失败（`off by 23`）。

### 39.4 最后的做法：**报不确定度，只对粗差判负**

```c
rt_kprintf("  delta %u/1000 (sampler spread is ~30/1000 at this frequency)\n", diff);
if (diff > 60u) { FAIL }
```

理由写进代码注释：

> 用比量具噪声更紧的容差去判定，会把正确的信号变成间歇性失败——
> 那比"报一个带不确定度的数"更糟。

`60/1000` 足够宽（不会因抖动误报），又足够窄（能抓住真正的错误：
compare 值写错、死区吃掉脉冲、输出反相）。

实测 4 次：`delta 31 / 1 / 1 / 1`，全部 PASS。

### 39.5 这条和 R47 是同一类

上一轮 W25 已经记过 R47："**量具失效时误报 FAIL**"。
这一轮它**以另一种形式复发**：不是"量程不够"，而是"**容差比噪声紧**"。

补进 R47 的描述：容差必须 ≥ 量具噪声，而且要把噪声**打印出来**，
否则读的人无法判断这个 delta 是信号还是噪声。

### 39.6 顺带记录：我在这一轮把 `mcpwm.c` 改坏了两次

编辑声明块时连续三次改错：

```
rt_uint32_t high_sum = 0;
rt_uint32_t high_sum = 0;    <- 重复
rt_uint32_t samples[8] = { 0 };
rt_uint32_t samples[8] = { 0 };  <- 重复
```

都是逐行替换时索引算错。最后用脚本按"从 `rounds` 声明到下一个注释"整块重写才干净。
**教训：连续的行级编辑不如整块替换可靠**——这一轮已经因此浪费了三次编译。

### 39.7 状态

| 项 | 值 |
|---|---|
| MCPWM | ✅ 稳定 PASS（4/4），占空比带不确定度输出 |
| `s31test` | **20/20** |
| `check_image.py` | **9/9** |
| RMT | ⚠️ 仍 WIP（见 §38） |

---

## 40. W29：RMT 完成 —— 最后两个根因都是"复位默认值"

前两轮把 RMT 记为 WIP。这一轮找到并修掉了最后两个根因，**RMT 现在 PASS**。

### 40.1 根因四：传完一帧后读指针没有回卷

`rmt_ll_tx_reset_pointer()` 我只在 `rt_hw_rmt_init()` 里调用了一次。
**第二帧起，发射器从上一帧停下的位置继续走**，而那已经在停止标记之后。

表现：把块尾填满停止标记后，**第二帧变成 0 个边沿**。
修法：每次 `s31_rmt_start()` 前都回卷一次。

### 40.2 根因五：`carrier_en_chn` 的复位值是 **1**

这是整轮最值得记的一条。`chnconf0` 的 bit 21（`chnconf1.carrier_en_chn`）
**复位默认开启载波调制**。载波以 `carrier_high/low_ticks` 为周期在符号内部翻转，
于是在**总时长完全正确**的前提下，每个符号内部被切成约 32 段。

实测（修好帧长之后）：

| 请求 | 边沿数 | 时长 |
|---|---|---|
| 2 符号 | 63 | 正确 |
| 10 符号 | 315 | 正确 |
| 20 符号 | 640 | 正确 |

**640 / 20 = 32，315 / 10 = 31.5** —— 稳定在每符号 32 个边沿，而时长一分不差。
这正是载波的特征：**只改变符号内部的翻转次数，不改变总时长**。

加一行 `rmt_ll_tx_enable_carrier_modulation(dev, ch, false)` 后：

```
n=1  -> 1 边沿     n=5  -> 5 边沿     n=20 -> 20 边沿
```

**边沿数 == 符号数，精确相等。**

### 40.3 完整验证

| 半周期 | 符号数 | 边沿 | 时长 | 期望 |
|---|---|---|---|---|
| 50 µs | 40 | 40 | 4 ms | 40 / 4 ms |
| 100 µs | 1 | 1 | 0 ms | 1 / 0 ms |
| 100 µs | 10 | 10 | 2 ms | 10 / 2 ms |
| 200 µs | 7 | 7 | 2 ms | 7 / 2 ms |
| 500 µs | 4 | 4 | 4 ms | 4 / 4 ms |
| 1000 µs | 2 | 2 | 4 ms | 2 / 4 ms |

**6/6 全过。** 另外 48 个符号的存储读回逐个比对通过。

### 40.4 顺带修好的：长半周期需要分频

时长字段是 15 位，25 ns tick 只到 **819 µs**。1 ms 半周期原先直接被拒。
现在 `rt_hw_rmt_square()` 自动把通道分频器抬到够用为止
（1 ms 用 div=8，tick 变成 200 ns）。

这是**有意的取舍**：分频会牺牲分辨率，对 1 ms 方波无所谓，
对 WS2812 不可接受 —— 所以 `rt_hw_rmt_ws2812()` 固定 `div=1` 和 16/34、32/18 tick。

### 40.5 这一轮的四条通用教训

| # | 教训 |
|---|---|
| 1 | **复位默认值可以是一个功能而不是零。** `carrier_en_chn=1` 意味着"没人要求的功能默认开着"，症状是"数量对但细节错 32 倍" |
| 2 | **指针类状态必须在每次使用前复位，不能只在 init 里复位一次。** 症状是"第一帧对、第二帧错" |
| 3 | **"总时长对、边沿数错"指向调制类功能**；"总时长也错"才指向长度控制。这两类要知道分开查 |
| 4 | **前一轮认定的"未解决"可能有两个叠加的根因。** 修好帧长之后才看得见载波 |

### 40.6 状态

| 项 | 值 |
|---|---|
| RMT | ✅ **PASS**（6/6 参数组合，48 符号内存逐个比对） |
| `s31test` | **20/20** |
| `check_image.py` | **9/9**，29 命令 |
| MCPWM | ✅ 稳定 PASS |
| `s31led` / `s31psram` | ✅ 正常 |

---

## 41. W30：TWAI/CAN —— 基础设施通了，帧不完成（WIP）

这一轮开始 M8.7 的外设。TWAI 没有做完，但**排除了一大串假设**，
这些排除本身就是下一轮的起点。

### 41.1 已建立的事实

| 项 | 证据 |
|---|---|
| 时钟、存储上电、引脚路由 | 寄存器读回与写入一致 |
| 控制器接受帧 | TX buffer 状态 `1`（Ready） |
| 控制器取走帧 | 状态 `1 → 2 → 6`，说明发射过 |
| 帧失败 | 状态 `6` = `TXT_ERR` |

### 41.2 排除掉的五个假设（每条都是实测的）

**(a) 只设 `stm` 不够。**
`stm`（自测）只让节点自己产生 ACK；`ilbp`（内部回环）才把 TX 在片内接回 RX。
只设 `stm` 时：TX buffer 排空、RX 始终空。

**(b) RX 脚必须上拉。**
CAN 总线空闲是隐性（高），控制器要看到 **11 个连续隐性位** 才加入总线。
浮空输入被读成噪声 → 永远加入不了。加上拉后 `intr` 从 `0x800` 变成 `0x810`。

**(c) TX buffer 状态是编码不是标志。**
`1=Ready 2=TX进行中 4=OK 6=Failed 8=Empty`，**复位值是 8 不是 0**。
最初写 `while (status != 0)` 等空 buffer，永远等不到——
四档全部报 "TX buffer never became free"。

**(d) 位时序必须含同步段。**
一个位 = `1 同步段 + prop + tseg1 + tseg2`。同步段不可编程，
所以三个可编程段之和必须是 `quanta - 1`。我最初把 16 quanta 全分给三段，
每位少 1 quanta —— 1 Mbit/s 实际跑成 1.33 Mbit/s。

**(e) `afm` 必须显式关掉。**
寄存器说明："afm=1 时只有通过过滤器的帧才存入 RX；afm=0 时全部存入"。
`afm=1` 且没编程任何过滤器 = 所有帧被丢弃，症状和"没有回环"一模一样。

### 41.3 未解决：为什么帧失败

关键观察：

```
125k/250k/500k: buf 6 -> 6 -> 6, tec=0
     1 Mbps  : buf 1 -> 6 -> 6, tec=160
```

**低三档 `tec=0` 也是失败，1 Mbps 才有错误累加** —— 这是**两个不同的问题**，
不是一个。低三档可能根本没上总线；1 Mbps 上了但位错误。

另外 1 Mbps 的 `brp` 是 `40M/(1M*16) = 2.5` 取整成 `2`，
**实际比特率是 1.25 Mbit/s**，这一档本身就不准。

### 41.4 为什么这轮收在这里

TWAI 已经跑了很久，继续盲试的代价高。而**已排除的假设是真实产出**——
下一轮可以从"低三档为什么 tec=0"直接入手，不用重走这五条。

`s31twai` 打印明确的 WIP 说明，不报 PASS。

### 41.5 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/twai.c`、`port/esp32s31/twai_cmd.c`、`port/esp32s31/port_twai.h`、`port_board.h` 加 `BOARD_TWAI_TX/RX_PIN`(23/24) |
| 修改文件 | `CMakeLists.txt`（`esp_hal_twai`）、`board.c` |
| 新命令 | `s31twai`（**WIP，不报 PASS**） |
| 命令数 | 30 |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9**、`s31rmt`/`s31mcpwm` PASS |

### 41.6 过程记录：串口卡死导致两次 10 分钟超时

两次 `flash` 在 600 秒后转后台并挂住。原因不是固件，是串口被残留的 python
进程占着。清理后 `esptool chip_id` 立即连通。**教训：flash 卡住先查进程，别等。**

---

## 42. W31：TWAI/CAN 完成 —— 根因是 `ilbp` 在 S31 上不生效

上一轮把 TWAI 记为 WIP，理由写着"帧不完成，两个不同的问题"。
这一轮找到了根因，而且**不是两个问题，是一个**。

### 42.1 决定性实验：把 TX 和 RX 路由到同一个引脚

```
msh > s31twai 1        # 旧的实验模式，TX=RX=GPIO23
   125000 bit/s: PASS  id 0x123, 4 bytes round-tripped
```

**一改就通。** 而两脚模式（TX=23, RX=24）在**所有**码率都是
`tec=160, rec=0`。

这组对比把问题定性了：

| 配置 | 结果 | 含义 |
|---|---|---|
| 单脚（TX=RX=GPIO23） | **PASS** | 时钟、时序、帧格式、TX buffer、RX FIFO 全部正确 |
| 两脚（TX=23, RX=24） | `tec=160, rec=0` | **`ilbp` 内部回环不生效** |

**`ilbp` 读到 1 但不产生行为**——这是 S31 上这个位的实际表现。
上一轮"低三档 tec=0 是另一个问题"的判断是错的：
当时状态机等待还没修，失败发生在更早的阶段，掩盖了真实原因。

### 42.2 修好的四个缺陷

| # | 缺陷 | 症状 | 修法 |
|---|---|---|---|
| 1 | **没等故障收敛状态切换** | `buf 1→6` 在 send 返回前就完成，`tec=0` | `enable_hw(true)` 后等 `FCSI` 中断位置起（带超时） |
| 2 | **`brp` 截断改变比特率** | 1 Mbps 实际 1.25 Mbps | 在 8..25 quanta 里搜**精确解**（`40M/1M=40` → `quanta=8, brp=5`） |
| 3 | **`set_operate_cmd` 无超时自旋** | 第一档通过后第二档永不打印，**控制台死掉** | 直接写 `command.val` 并带 cycle 预算轮询 |
| 4 | **RX 排空循环无上界** | 同上，卡在排空 | 最多 4 次，超出就跳出 |

第 1 条修好后有个关键变化：**四档的 `tec` 从"只有 1Mbps 有"变成"四档全是 160"**——
行为一致了，才看得出真正原因是两脚接法本身，而不是码率。

第 3、4 条是**自己引入的死锁**：`twaifd_ll_set_operate_cmd()` 的实现是
```c
hw->command.val = commands;
while (hw->command.val & commands);   // 无超时
```
控制器处于 disable 状态时这一位不会自清，**整个控制台卡死**。
第一次遇到时我以为是串口问题，杀了两次 python 进程——其实是固件挂住了。

### 42.3 另一个自己踩的坑：`rwcnt` 不是字数

我看到收到的帧长度是 8（发的 4），以为是"字数字段没填"，于是写了
`buf.format.rwcnt = (len+3)/4`。**结果更糟**：本来 4/4 通过变成全部失败。

`twaifd_struct.h` 写得很清楚：

```c
uint32_t rwcnt: 5;   // Re-transmission counter
```

**那是重传计数器，不是字数。** 已回退并加注释说明。
教训：**字段名有歧义时先读定义，别按名字猜。**

### 42.4 完整验证

```
msh > s31twai
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

**连跑三次 4/4。** `s31twai 0`（两脚）明确报已知限制，不报 PASS。

### 42.5 没验证的部分

**模拟层完全没测**：没有收发器、没有差分对、没有终端电阻、
没有真实第二节点仲裁、没有错误帧行为。`s31twai` 的 PASS 消息里写明这一点。

### 42.6 产物

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/twai.c`、`twai_cmd.c`、`port_twai.h`、`BOARD_TWAI_TX/RX_PIN`(23/24) |
| 默认引脚 | **TX=RX=GPIO23**（单脚回环）；`s31twai 0` 用两脚 |
| 命令数 | 30 |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9**、`s31rmt`/`s31mcpwm`/`s31led` PASS |

---

## 43. W32：P3 前置风险 R24 被证伪 —— 用编译器而不是读代码

这一轮没有加外设，而是回头核一条**已经写进 plan 的"前置风险"**。
结论是它错了，而且错的方式值得记。

### 43.1 R24 原来写的是什么

> 兼容层的静态表（`ft_tls` / `s_notify` / `s_exited` / `s_pending`）
> 只用 `rt_hw_interrupt_disable()` 保护，SMP 下只关本核中断 → **不互斥**。

plan 里把它列为 "P3 最大的机械改动量"。四条静态表、12 处调用点，
按这个判断要全部改成自旋锁。

### 43.2 用编译器验证，而不是继续推理

写了一个 20 行的探针，把 `rthw.h` 的两个声明和那个 `#define` 原样镜像过来，
用目标工具链编译：

```
$ riscv32-esp-elf-gcc -S -O2 probe_r24.c
probe_smp:
    tail    rt_cpus_lock
```

**`rt_hw_interrupt_disable()` 在 SMP 下编译成 `rt_cpus_lock()`。**

`rthw.h:146`：

```c
#ifdef RT_USING_SMP
rt_base_t rt_cpus_lock(void);
#define rt_hw_interrupt_disable rt_cpus_lock    /* <-- 宏替换 */
#else
rt_base_t rt_hw_interrupt_disable(void);
#endif
```

`cpu_mp.c:168`：

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

**`rt_hw_spin_lock(&_cpus_lock)` 就是全局自旋锁。**
打开 `RT_USING_SMP` 之后，shim 里**同一行代码**自动变成互斥的。

### 43.3 ISR 路径也安全（这是第二层担心）

我接着担心：`ft_task_notify_isr()` → `ft_task_notify()` → `rt_hw_interrupt_disable()`，
如果 ISR 打断了**已持锁的线程**，会不会自旋死锁？

看 `cpus_lock_nest` 的实现：它是个**嵌套计数**，`lock_nest != 0` 时走另一条分支，
**不重复取锁**。所以 ISR 里再调是安全的。

`rt_cpus_unlock()` 也对称：`nest` 减到 0 才 `rt_hw_spin_unlock()`。

**这条计数正是为"同核嵌套 + ISR 重入"设计的。** 我又多虑了一层。

### 43.4 结论：改动量 0 行

| 项 | 原估 | 实际 |
|---|---|---|
| shim 四张静态表加锁 | "P3 前置，大量改动" | **0 行** |
| `ft_task_notify_isr` 改写 | 可能需要 | **不需要** |

**R24 已撤销。** plan 里改为"✅ 已撤销（W32）"，并写明依据。

### 43.5 顺手探明 P3 的真实边界

既然 R24 不用做了，就想看看"到底能不能开 SMP"。只改两行
（`RT_CPUS_NR 2` + `scheduler_up.c` → `scheduler_mp.c`），构建直接给出 8 条错误：

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

**原因：只加了 `RT_CPUS_NR`，没定义 `RT_USING_SMP`。**
这些结构成员和宏全在 `#ifdef RT_USING_SMP` 里面，两处必须同时改。

**这 8 条错误是有价值的产品**：它把"开 SMP 要补什么"从猜测变成清单，
而且全部是编译期枚举的，不是运行时才崩。

已回滚，板子仍是单核、20/20 通过。

### 43.6 这一轮的方法论价值

R24 是**读了函数名就开始推理**的产物：

> "`rt_hw_interrupt_disable` 听起来就是关中断，SMP 下关中断只关本核，所以不互斥。"

三步推理，第一步就错了——那个名字是个**宏**，含义由条件编译决定。

同一类错误之前出现过：R28（按名字猜 `SPI2` 符号，实际叫 `GPSPI2`）。
两次都是**名字给了错误的确定感**。

**规则补充：涉及"某函数/符号在某个配置下是什么"时，让编译器回答，不要读名字。**

### 43.7 产物

| 项 | 值 |
|---|---|
| 新增文件 | `tools/tmp/probe_r24.c`（20 行探针，可复用） |
| 修改文件 | `rtthread-esp32-s31-porting-plan.md`（§三之二/三之三/三之四 + R24 撤销） |
| 固件 | **未改动**（回滚后重新构建并验证） |
| 回归 | `s31test` **20/20**、`check_image.py` **9/9** |

---

## 44. W33：P3 的真实待办清单 —— 11 个符号，不是"最大机械改动"

上一轮把 R24 证伪了（改动量 0 行）。这一轮继续按同样的方法，
把"开 SMP 到底要做什么"从猜测变成**编译器给出的清单**。

### 44.1 第一次尝试：只加 `RT_CPUS_NR`（上一轮）

8 条错误，全是"结构成员不存在"。原因是 `RT_USING_SMP` 没定义。
这一轮补上 `#define RT_USING_SMP`，错误换了一批——**这次才是真的待办**。

### 44.2 第一批：头文件可见性

```
rttypes.h:161: fatal error: cpuport.h: No such file or directory
```

`rttypes.h` 在 `RT_USING_SMP` 下 `#include <cpuport.h>` 取 `rt_hw_spinlock_t`。
该头在 `libcpu/risc-v/legacy_common/`，原先只在 rtthread 组件的
**PRIV_INCLUDE_DIRS** 里——组件自己够用，但 `include <rtthread.h>` 的其他组件
（startup、freertos 适配层、应用）看不到。

**这是个"私有包含路径 vs 全局可见"的边界问题，不是 SMP 特有的。**
任何往公共头里加 `#include` 的配置项都会踩到。

修法：把它加进 `idf_build_set_property(INCLUDE_DIRECTORIES ...)`。

### 44.3 第二批：11 个缺失符号

| 缺失符号 | 出现位置 | 性质 |
|---|---|---|
| `rt_interrupt_nest` | `vectors_rtthread.S:144/202` | SMP 下是 **`rt_cpu_self()->irq_nest`**（`irq.c:68`），不是变量 |
| `rt_thread_switch_interrupt_flag` | `vectors_rtthread.S:217` | `cpuport.c:27` 定义在 `#ifndef RT_USING_SMP` 内 |
| `rt_interrupt_from_thread` | `vectors_rtthread.S:226` | 同上 |
| `rt_interrupt_to_thread` | `vectors_rtthread.S:232` | 同上 |
| `rt_hw_spin_lock` / `_unlock` / `_init` | `components.c`、`cpu_mp.c` | 参考 `libcpu/risc-v/common/cpuport.c:66`（ticket lock） |
| `rt_hw_cpu_id` | `clock.c`、`idle.c`、`scheduler_mp.c` | 参考 `libcpu/risc-v/common/cpu.c:251` |
| `rt_hw_ipi_send` | `scheduler_mp.c:329` | 接 `ETS_IPC_ISR_INUM`(28) |
| `rt_hw_secondary_cpu_up` | `components.c:259` | 从核启动 |
| `rt_hw_secondary_cpu_idle_exec` | `idle.c:128` | 从核 idle |

**从 11 个符号能读出三件事**：

**(a) `rt_interrupt_nest` 在 SMP 下不是变量。**
`irq.c:68` 写的是 `#define rt_interrupt_nest rt_cpu_self()->irq_nest`。
所以 `vectors_rtthread.S` 里 `la t0, rt_interrupt_nest` **不能再用**——
它取不到地址，因为根本不存在这个符号。

**这才是 per-CPU 化的真正内容**：不是"给全局量加个核号偏移"，
而是**这些量在 SMP 下换了存在形式**（从全局变量变成 per-CPU 结构成员）。
plan 里原写的"`vectors_rtthread.S` 全局量 per-CPU 化"抓住了方向，
但低估了性质——它是**汇编需要改写成按 `mhartid` 算偏移**，不是加个字段。

**(b) `cpuport.c` 的三个全局量要整体挪进 `#else`。**
内核在 SMP 下走 `rt_scheduler_do_irq_switch()`（`scheduler_mp.c:1016`），
不再用 `rt_thread_switch_interrupt_flag` / `from` / `to` 这套机制。
参考实现 `interrupt_gcc.S:312-340` 的 SMP 分支就是直接
`call rt_scheduler_do_irq_switch` + `tail rt_hw_context_switch_exit`。

**(c) spinlock 和 cpu_id 可以直接抄。**
`libcpu/risc-v/common/cpuport.c:66` 是一份完整的 ticket lock
（`amoadd.w.aqrl` 取号 + 自旋等 owner + 半字存储释放），
`cpu.c:251` 是 `rt_hw_cpu_id()`。**不需要自己设计。**

### 44.4 回滚时踩的坑：只删 `.obj` 不够

回滚到单核后构建仍失败：

```
cpu_mp.c:25: unknown type name 'rt_hw_spinlock_t'
cpu_mp.c:177: 'struct rt_thread' has no member named 'cpus_lock_nest'
```

**`build/` 里残留了 SMP 时的 CMake 配置**，`cpu_mp.c` 还在源文件列表里。
删单个 `.obj` 无效，**必须删整个 `build/` 重新 configure**。

CMake 的源列表变化不改 `CMakeCache.txt` 的依赖关系，
**这类残留只能靠整目录删除清掉**。

这条之前没遇到过：前面几轮改的都是 `rtconfig.h` 的宏（会触发重编译）
和新增源文件（会触发 re-configure）。**删源文件不会。**

### 44.5 这一轮的实际产物

没有让 SMP 跑起来，但把工作量量化了：

| 项 | 之前 | 现在 |
|---|---|---|
| shim 静态表（R24） | "大量改动" | **0 行** |
| P3 总体 | "最大机械改动量，不确定" | **11 个符号 + 1 处汇编改写 + 3 个新函数** |
| 汇编改写的性质 | "全局量 per-CPU 化" | **`rt_interrupt_nest` 在 SMP 下不是变量，`la` 取地址的写法必须换** |
| spinlock | 待设计 | **可直接用 `libcpu/risc-v/common/cpuport.c` 的 ticket lock** |

**"不确定"变成"可枚举"，这是这一轮的全部价值。**

### 44.6 状态

板子已恢复并验证：单核、`s31test` **20/20**、`check_image.py` **9/9**、30 命令。
`rtconfig.h` 与 `CMakeLists.txt` 均已回滚（保留 `cpuport.h` 的全局包含路径——它在单核下无害且是正确修复）。

---

## 45. W34：P3 待办从 11 项降到 4 项，且抓到一个真 bug

上一轮列出 11 个缺失符号。这一轮实现了 7 个，**清单从 11 降到 4**（剩下全是汇编改写）。
过程中抓到一个会让 SMP 静默失效的 C 级错误。

### 45.1 新增 `smp_prims.c`：6 个符号

| 函数 | 实现要点 |
|---|---|
| `rt_hw_cpu_id` | `csrr mhartid`——与 IDF 的 `rv_utils.c` 一致，全系统对"我是哪个核"有唯一答案 |
| `rt_hw_spin_lock_init` | `lock->slock = 0` |
| `rt_hw_spin_lock` | ticket lock：`amoadd.w.aqrl` 取号，自旋等 owner |
| `rt_hw_spin_unlock` | `fence` + **半字存储**推进 owner |
| `rt_hw_secondary_cpu_up` | **空实现**（见 45.4） |
| `rt_hw_secondary_cpu_idle_exec` | `wfi` |

全部包在 `#ifdef RT_USING_SMP` 里，单核构建看到的是一个空 object。

### 45.2 抓到的真 bug：ticket lock 的两个半字取反了

第一版我写：

```c
ticket = (unsigned short)(tmp & 0xFFFFu);
```

**这是错的。** `amoadd` 加的是 `1 << 16`（推进 `next`），返回的是**加之前**的值，
所以本次分配的票号在**高 16 位**：

```
第一次: old=0x00000000 -> ticket 应为 0，取低半字得 0   （碰巧对）
第二次: old=0x00010000 -> ticket 应为 1，取低半字得 0   （错！）
第三次: old=0x00020000 -> ticket 应为 2，取低半字得 0   （错！）
```

**所有核拿到同一张票，全部同时进入临界区**——而且**无竞争时完全正常**，
只在真正并发时才错。这是最难查的一类。

用 Python 复算验证：

```
tickets: [0, 1, 2]     <- 取高半字，正确
owner=0 next=3
PASS: tickets are 0,1,2 and next counts up

wrong expression gives: [0, 0, 0]
=> confirms: low half is always 0, two cores would take the lock at once
```

修法：`ticket = (tmp >> 16) & 0xFFFF`，自旋条件改成比较 `slock & 0xFFFF`（owner 在低半字）。

**layout 是 owner 在低半字、next 在高半字**，与 `cpuport.h` 的结构体声明
（`struct { unsigned short owner; unsigned short next; }`）一致——
按小端序，`owner` 落在低地址即低半字。

### 45.3 第二个坑：`pause` 助记符编不出来

```
smp_prims.c:92: Error: unrecognized opcode `pause', extension `zihintpause' required
```

`pause` 需要 `Zihintpause` 扩展，而工具链默认 `-march` 不含它。
RT-Thread 上游用 `OPC_PAUSE_I` 宏包了一层（`opcode.h:79`）：

```c
#ifdef ARCH_TOOLCHAIN_HAS_ZIHINTPAUSE
#define OPC_PAUSE_I  __ASM_STR(pause)
#else
#define OPC_PAUSE_I  __ASM_STR(.4byte 0x0100000f)   /* 同一指令的原始编码 */
#endif
```

但 `legacy_common/cpuport.h` 里没有这个宏（只有 `common/include/cpuport.h` 有）。
我直接用了原始编码 `.4byte 0x0100000f`：**同一条指令，不支持的核当无操作**，
自旋仍然正确。

### 45.4 `rt_hw_secondary_cpu_up` 故意空着

启动从核需要 boot address、栈、入口点，**任何一个填错都会让板子无声挂住**。
所以这一轮让它什么都不做——`rt_hw_secondary_cpu_up` 的存在让内核链接通过，
不做事让单核运行保持单核，**这是 stub 的安全方向**。

顺带修正了签名：`rthw.h:224` 是 `void rt_hw_secondary_cpu_up(void)`，
我第一版写成 `rt_err_t`，编译器直接报冲突。

### 45.5 新增 `smp_ipi.c`：第 7 个符号

`rt_hw_ipi_send(int ipi_vector, unsigned int cpu_mask)`——签名来自 `rthw.h:203`。

实现走 CLIC 的 pending 位：

```c
reg = CLIC_INT_CTRL_REG(ETS_IPC_ISR_INUM + RV_EXTERNAL_INT_OFFSET);
*reg |= CLIC_INT_IP;      /* 读改写，不能盲写 */
```

**必须读改写**：`clic_reg.h` 里 `CLIC_INT_IP`（bit 0）、`CLIC_INT_IE`（bit 8）、
`CLIC_INT_CTL`（bit 31:24，优先级）、`CLIC_INT_ATTR_TRIG`（bit 18:17）
**共用一个 `CLIC_INT_CTRL_REG(i)` 字**。盲写 `= CLIC_INT_IP` 会把优先级、
使能和触发类型一起清掉。

### 45.6 清单：11 → 4

| 状态 | 符号 |
|---|---|
| ✅ 已实现 | `rt_hw_cpu_id`、`rt_hw_spin_lock_init/_lock/_unlock`、`rt_hw_secondary_cpu_up`（stub）、`rt_hw_secondary_cpu_idle_exec`、`rt_hw_ipi_send` |
| ❌ 剩 4 个 | `rt_interrupt_nest`、`rt_thread_switch_interrupt_flag`、`rt_interrupt_from_thread`、`rt_interrupt_to_thread` |

**剩下 4 个全是同一件事**：`vectors_rtthread.S` 的三处 `la` 取地址 +
`cpuport.c` 的三个全局量要挪进 `#else`。这就是下一轮的全部内容。

### 45.7 这一轮的两次回滚，都踩了同一个坑

**第一次**：把 `CMakeLists.txt` 恢复到备份，结果编辑时漏掉了
`idf_build_set_property(COMPILE_DEFINITIONS RT_USING_USER_MAIN APPEND)` 一行。
构建能过，但**运行时**：

```
ESP_ERROR_CHECK failed: esp_err_t 0x103 (ESP_ERR_INVALID_STATE)
[FAIL] init table order  1 entries by address: finsh_system_init
[PASS] __fsymtab non-empty  144 B = 12 commands   <- 从 30 掉到 12
```

**30 个命令变成 12 个**。`RT_USING_USER_MAIN` 没了，`.rti_fn` 的初始化顺序错乱，
板级初始化根本没跑完。

**第二次**：源文件列表编辑时把 `smp_prims.c` 重复加了一次、`smp_ipi.c` 漏了。

**教训：`CMakeLists.txt` 这种"改一行影响全局"的文件，靠 `Compare-Object` 对着基线
逐行核对，比自己读一遍可靠。** 这一轮最后是用 `difflib.unified_diff` 打印出
恰好 4 处预期差异才收手的。

### 45.8 观测到一次 TWAI 间歇失败

回归时 `s31twai` 有一次报 `0/4`，随后连跑三次都是 `4/4`。
**没有定位到原因**，如实记录：单脚回环路径存在约 1/5 的间歇失败，待查。

### 45.9 状态

| 项 | 值 |
|---|---|
| 新增文件 | `port/esp32s31/smp_prims.c`、`port/esp32s31/smp_ipi.c` |
| 修改文件 | `CMakeLists.txt`（两个源文件 + `cpuport.h` 全局包含路径） |
| 固件 | 单核，`s31test` **20/20**、`check_image.py` **9/9**、**30 命令** |
| 回归 | `s31rmt` PASS、`s31mcpwm` PASS、`s31led` 正常、`s31twai` 4/4（3 次） |
| 待查 | TWAI 单脚回环的间歇失败 |

---

## 46. W35：P3 链接期问题清零 —— SMP 构建能链接了，但不启动

上一轮剩 4 个符号。这一轮**把 4 个全部解决**，SMP 构建现在能完整链接。
代价是：**链接通过但不启动**。这是明确的进展，也是明确的新问题。

### 46.1 第一步：让汇编看得到 `RT_USING_SMP`

`vectors_rtthread.S` 原先只 `#include "sdkconfig.h"` 和 soc 头，**看不到 rtconfig.h**。
文件头注释写明这是有意的（"This file sees soc/soc_caps.h, not rtconfig.h"）。

**修法不需要改 riscv 组件**——上一轮为了让 `cpuport.h` 全局可见，
已经把 `rtt_config_dir` 加进了 `idf_build_set_property(INCLUDE_DIRECTORIES ...)`。
所以汇编里加一行就够：

```c
#if CONFIG_IDF_RTTHREAD
#include "rtconfig.h"
#endif
```

**这是上一轮那个修复的顺带红利。**

### 46.2 第二步：`rt_interrupt_nest` 换成函数调用

SMP 下它是宏（`rt_cpu_self()->irq_nest`），**取不到地址**。新增
`rt_hw_irq_nest_addr()` 返回本核计数器地址，汇编改成：

```asm
#ifdef RT_USING_SMP
    call  rt_hw_irq_nest_addr
    lw    t0, 0(a0)
#else
    la    t0, rt_interrupt_nest
    lw    t0, 0(t0)
#endif
```

两处（entry 和 exit）都改了。

### 46.3 第三步：exit 路径整体分叉

参考实现 `interrupt_gcc.S:312-340` 的 SMP 分支是：

```asm
    mv    sp, s0
    mv    a0, s0
    call  rt_scheduler_do_irq_switch
    tail  rt_hw_context_switch_exit
```

它**完全不碰** `rt_thread_switch_interrupt_flag` / `from` / `to` 这三个全局量。
我按这个改了，并在 `#else` 分支保留原有逻辑。

### 46.4 第四步：三个全局量挪进 `#else`

`cpuport.c:27` 的 `#ifndef RT_USING_SMP` 已经是对的，不用改。
10 个符号里的 3 个（flag/from/to）**只要汇编不再引用就自然消失**。

### 46.5 撞到的两个链接问题

**(a) `-u` 锚点缺失。**

```
vectors_rtthread.S:157: undefined reference to `rt_hw_irq_nest_addr'
```

`nm` 确认 `smp_stack.c.obj` **在** `librtthread.a` 里，符号也有（`00000000 T`）。
原因是**归档成员的拉取顺序**：riscv 组件的汇编在 `librtthread.a` **之后**被扫描，
那时已经没人引用这个符号了。加：

```cmake
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u rt_hw_irq_nest_addr")
```

**另一个修法是把函数定义在单核也编译**（`#else` 分支返回全局量地址），
我两个都做了——`-u` 治标，双分支定义让"单核也链接"这条路径本身成立。

**(b) `cpu_up.c` 与 `cpu_mp.c` 重复定义。**

```
cpu_mp.c:48: multiple definition of `rt_spin_lock_init'
cpu_up.c:33: first defined here
```

两个文件实现**同一套接口的两个配置版本**。上游 `src/SConscript:26-29` 写得很清楚：

```python
if GetDepend('RT_USING_SMP') == False:
    SrcRemove(src, ['cpu_mp.c', 'scheduler_mp.c'])
else:
    SrcRemove(src, ['cpu_up.c', 'scheduler_up.c'])
```

**我们的 CMakeLists 两个都列了**，单核时靠 `cpu_up.c`、SMP 时两个都进 → 冲突。
已按上游规则改：SMP 时删 `cpu_up.c`，单核时删 `cpu_mp.c`。

### 46.6 结果：能链接，不启动

```
Project build complete.          <- 无 undefined reference，无 multiple definition
Hash of data verified.           <- flash 成功
esptool chip-id: Connected       <- bootloader 正常
serial: 日志停在 esp_image 加载   <- 到不了 RT-Thread banner
```

**从"11 个符号缺失"推进到"链接干净、运行时死"。** 链接期问题清零。

不启动的候选原因（下一轮从这三条查）：

| # | 假设 | 检查方法 |
|---|---|---|
| 1 | `rt_hw_secondary_cpu_up` 是空实现，SMP 内核在等从核 | 看 `rtthread_startup` 是否阻塞在这 |
| 2 | `vectors_rtthread.S` 的 SMP 分支语义错（`rt_scheduler_do_irq_switch` 参数） | 参考实现传的是 `s0`（原 sp），我传的是 `sp`，需要核对 |
| 3 | 双核初始化顺序 | 加串口打印 |

**第 2 条已经看出可疑**：参考实现里 `s0` 保存的是**切换中断栈之前**的 sp，
而我在 exit 路径里 `mv a0, sp` 用的是当前 sp。这一处很可能就是问题。

### 46.7 两次回滚，都是"点到点修"的代价

**第一次**：`cpu_up.c` 从源列表删掉后没加回来，单核构建缺 `rt_spin_lock`。
**第二次**：加回来时位置错了（跑到 `cpu_mp.c` 那一行）。

每次都用 `difflib` 对基线确认差异，最后恰好 5 处预期改动才收手。

**这已经是连续第三轮在 `CMakeLists.txt` 上出错。** 结论：**编辑这类文件后必须跑 diff**，
不能靠读。

### 46.8 状态

| 项 | 值 |
|---|---|
| 单核固件 | `s31test` **20/20**、`check_image.py` **9/9**、**30 命令** |
| 外设回归 | `s31twai` 4/4、`s31rmt` PASS、`s31mcpwm` PASS |
| SMP 构建 | ✅ **链接通过**（11 个符号全解决）<br>❌ **不启动** |
| 新增文件 | `port/esp32s31/smp_stack.c` |
| 修改文件 | `vectors_rtthread.S`（SMP 分支）、`CMakeLists.txt`（3 个 smp 源 + `-u` 锚点 + 全局包含） |
| 下一轮入口 | `rt_scheduler_do_irq_switch` 的参数（`s0` vs 当前 `sp`） |

---

## 47. W36：把 SMP 的失败范围收缩到"banner 之前"，并排除了一个假设

上一轮 SMP 能链接但不启动。这一轮做了两个受控实验，
**把失败点从"某处"收缩到"`rt_show_version()` 之前"，并且排除了一个候选原因**。

### 47.1 实验一：`RT_USING_SMP` + `RT_CPUS_NR=2`（上一轮已做）

链接通过，不启动。

### 47.2 实验二：`RT_USING_SMP` + `RT_CPUS_NR=1`

**动机**：`RT_CPUS_NR=2` 时内核认为有两个核，但 `rt_hw_secondary_cpu_up()`
是空实现——只有 CPU0 在跑。IPI 永远没有接收方。这本身可能就是死因。

所以构造一个中间态：**走全部 SMP 代码路径（自旋锁、per-CPU 结构、
`scheduler_mp.c`），但只用一个核**，不需要从核启动。

**结果：一样不启动。** 现象完全相同——bootloader 打印完就静默。

### 47.3 这一步排除了什么

| 假设 | 状态 |
|---|---|
| "从核没启动导致内核等待" | ❌ **排除**。`RT_CPUS_NR=1` 时不存在从核，照样死 |
| "IPI 发到不存在的核导致挂" | ❌ **排除**，同上 |
| "`rt_hw_secondary_cpu_up` 空实现是死因" | ❌ **排除**，同上 |

**这是本轮最有价值的产出**：三个看似合理的原因一次排掉。

### 47.4 进一步收缩：死在 banner 之前

`rtthread_startup()` 的顺序（`components.c:239-290`）：

```c
rt_hw_spin_lock_init(&_cpus_lock);     // SMP only
rt_hw_local_irq_disable();
rt_hw_board_init();                    // <- 板级初始化
rt_show_version();                     // <- banner 在这里
rt_system_timer_init();
...
```

**banner 没出现 → 挂在第 1~3 步之间。** 而这三步里只有
`rt_hw_spin_lock_init(&_cpus_lock)` 是 SMP 特有的，加上 `rt_hw_board_init()`
内部可能有受 SMP 影响的代码。

**失败范围从"整个启动过程"缩到"三个函数"**。

### 47.5 发现的一个既有隐患

`esp_system/startup.c:23-31` 有一组编译期断言：

```c
#if !CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE
#ifndef RT_USING_SMP
#error "RT-Thread requires RT_USING_SMP when IDF starts multiple cores."
#elif RT_CPUS_NR != SOC_CPU_CORES_NUM
#error "RT-Thread and IDF must use the same number of cores."
#endif
#endif
```

**项目目前 `CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y`，所以这组断言从不生效。**
也就是说 `RT_CPUS_NR=2` 时 IDF 只启动一个核，而 RT-Thread 以为有两个——
**这组不变量没有被任何东西守住**。

这是一条真实风险：将来有人改了 `RT_CPUS_NR` 而不改 sdkconfig，
构建会静默通过、运行才出问题。已登记。

### 47.6 一个自己踩的坑：两次实验之间的"板子静默"

实验二之后串口**完全没有输出**（连 bootloader 都不打）。
`esptool chip-id` 仍能连上，说明芯片没坏。
原因是 flash 后的复位时序——`board.py` 的重置没生效，
手动收数据前需要显式拉 RTS。**不是固件问题。**

已记录：**串口无输出时先确认芯片能应答**（`esptool chip-id`），
再判断是不是固件死了。

### 47.7 状态

| 项 | 值 |
|---|---|
| 单核固件 | `s31test` **20/20**、`check_image.py` **9/9**、**30 命令** |
| SMP 构建 | ✅ 链接通过<br>❌ 不启动（**死点在 banner 之前**，与从核无关） |
| 已排除 | 从核未启动、IPI 无接收方、`secondary_cpu_up` 空实现 |
| 下一轮入口 | `rt_hw_spin_lock_init` / `rt_hw_local_irq_disable` / `rt_hw_board_init` 三者 |

---

## 48. W37：SMP 不启动的根因找到 —— flash cache 关闭窗口里的函数调用

上一轮把失败范围缩到"banner 之前三个函数"。这一轮用**早期串口探针**拿到了
完整的 panic dump，**根因确定**。

### 48.1 探针方法：`esp_rom_printf`

`esp_rom_printf` 直接写 UART FIFO，**不需要锁、不需要堆、不需要调度器**，
是唯一能在最早点输出的手段。

在单核构建里先验证探针本身可用：

```
[probe] board_init hart=0 smp=0 cpus=1
[probe] after vPortSetupTimer
[probe] after int_wdt
[probe] after pin_init
[probe] after peripherals
```

**5 个探针全部打印，hart=0。** 这把"探针方法"和"单核基线"都固定住了。

### 48.2 SMP 构建：一个探针都没打印

```
I (409) MSPI Timing: Enter psram timing tuning
Guru Meditation Error: Core  0 panic'ed (Instruction access fault)
MEPC    : 0x40013b1e
MCAUSE  : 0x00000001        <- instruction access fault
```

**注意 `Enter psram timing tuning`** —— 这是 MSPI 在调 PSRAM 时序，
**期间 flash cache 是关的**。

### 48.3 定位 MEPC

```
$ riscv32-esp-elf-nm -n s31_rtt_test.elf | (找 0x40013b1e 落在哪个符号)
fault 0x40013b1e falls in: 40013b1e T rt_enter_critical
```

`0x4001xxxx` 是 **IROM（flash 映射段）**，不是 IRAM。

### 48.4 完整调用链

```
trap -> vectors_rtthread.S                    [IRAM，能执行]
      -> rt_hw_irq_nest_addr()                [原本在 flash -> 已加 IRAM_ATTR]
        -> rt_cpu_self() -> rt_hw_cpu_id()    [flash]
          -> ... -> rt_enter_critical()       [flash]  <- MEPC 在这
```

**cache 关着的时候跳到 flash 取指令 = instruction access fault。**

**单核版为什么不崩**：它 `la t0, rt_interrupt_nest` **直接读全局变量**，
一条跳转都不做。SMP 版为了拿 per-CPU 计数器，引入了整条函数调用链。

### 48.5 为什么上一轮"只加 `IRAM_ATTR`"不够

第一版只给 `rt_hw_irq_nest_addr()` 加了 `IRAM_ATTR`，重新 flash 后
**MEPC 仍然是 0x40013b1e**（只从 `rt_hw_cpu_id` 换成了 `rt_enter_critical`，
因为 `RA` 变了）。

**因为 `IRAM_ATTR` 只解决了链条的第一环。** `rt_cpu_self()` → `rt_hw_cpu_id()` →
`rt_enter_critical()` 全在 flash 里，逐一加 `IRAM_ATTR` 不现实。

### 48.6 正确修法（下一轮）

**SMP 下不能让 trap 路径走函数调用。** 两个方向：

| 方案 | 做法 | 代价 |
|---|---|---|
| **A. 内联读 per-CPU 结构** | 在汇编里用 `mhartid` 算 `_cpus[n].irq_nest` 的地址，直接 `lw` | 需要知道 `struct rt_cpu` 的 `irq_nest` 偏移，且偏移要跟着内核变 |
| **B. 用 `tp` 寄存器存 per-CPU 指针** | 启动时把 `rt_cpu_self()` 存进 `tp`，trap 里 `lw t0, offset(tp)` | 需要维护 `tp`，但汇编只碰一个寄存器 |

**方案 B 更稳**：`tp` 本来就是 RISC-V 约定的 thread pointer，
RT-Thread 的 `rt_hw_cpu_id()` 参考实现里也读 `tp`（见 `cpu.c:251` 的 `mv %0, tp`）。

**下一步**：在 `rt_hw_board_init` 早期（cache 还开着时）把本核的
`rt_cpu_self()` 写进 `tp`，trap 路径改成 `lw t0, <irq_nest 偏移>(tp)`，
彻底去掉那次调用。

### 48.7 这一轮的产物

| 项 | 值 |
|---|---|
| 根因 | ✅ **flash cache 关闭窗口内的函数调用**（MEPC=0x40013b1e = `rt_enter_critical`） |
| 探针方法 | ✅ 验证可用（`esp_rom_printf`，单核 5/5 打印） |
| 单核固件 | ✅ 完整恢复，`s31test` **20/20**、`check_image.py` **9/9**、**30 命令** |
| `IRAM_ATTR` | 已加，但只解决链条第一环 |
| 下一轮 | per-CPU 指针放进 `tp`，trap 路径去掉函数调用 |

### 48.8 过程记录：三次因为"改完不回滚干净"翻车

1. 探针脚本跑了两次 → `board.c` 里两份探针，链接报 `rt_hw_cpu_id` 未定义
2. 探针里调 `rt_hw_cpu_id()` → **单核构建没有这个符号**（只在 `#ifdef RT_USING_SMP` 里）
3. 回滚时先删 `RT_USING_SMP` 但源列表还是 `scheduler_mp.c` → 一堆 `rt_hw_context_switch_to` 参数错误

**第 2 条是个真教训**：探针代码本身不能依赖配置相关的符号，
否则"测量工具"会先于被测对象坏掉。

---

## 49. W38：修法方向对了一半 —— 单核必须先回滚

上一轮确定根因是"flash cache 窗口里的函数调用"。这一轮按计划改成
**IRAM 指针表 + mhartid 索引，零函数调用**。结果：**修好了一部分，弄坏了另一部分**。

### 49.1 做了什么

把两处 `call rt_hw_irq_nest_addr` 换成：

```asm
    la    t1, rt_irq_nest_slot
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t1
    lw    t0, 0(t0)
```

表本身在 `smp_stack.c`：

```c
IRAM_ATTR volatile rt_atomic_t *rt_irq_nest_slot[RT_CPUS_NR];
```

**两次 `la`/`lw`，零跳转目标。** 理论上完全规避 flash。

### 49.2 结果一：cache 窗口问题确实修好了

修好后单核构建的启动日志：

```
I (401) MSPI Timing: Enter psram timing tuning
I (558) esp_psram: Found 16MB PSRAM device       <- 通过了！
I (559) cpu_start: Unicore app
I (560) cpu_start: Single core mode
```

**上一版在 `Enter psram timing tuning` 处就 fault，这一版通过了。**
所以"trap 路径不能调 flash"这个诊断是**对的**。

### 49.3 结果二：但单核挂在 `Single core mode` 之后

无 panic、无重启、无 banner。**静默挂住。**

两次中间实验的过程：

| 版本 | 现象 |
|---|---|
| 表声明为 `IRAM_ATTR ... [1];`（BSS，初值 0）+ `board_init` 里填 | **更早挂**，连 PSRAM 都不打印 —— 中断在 `board_init` 之前就来，`slot[0]` 还是 NULL |
| 表**静态初始化** `= { &rt_interrupt_nest }` | 通过 PSRAM tuning，挂在 `Single core mode` 之后 |

**第二个实验说明"静态初始化"解决了 NULL 问题，但还有别的东西不对。**

### 49.4 未查清的怀疑

`IRAM_ATTR` 用在**带非零初值的数据**上，ESP-IDF 的 IRAM 段对初始化数据的支持
不可靠——初值可能没被拷进去。若 `slot[0]` 实际仍是 0，trap 里 `lw 0(0)`
应当是 access fault 并有 panic，但**没有 panic**，所以更像别的原因（自旋？）。

**没有验证，如实记录为未解。**

### 49.5 决策：先回滚，不赌

单核固件已经稳定跑了 20 轮。**这一轮的目标是推进 P3，不是拿主固件冒险。**

改法：把汇编恢复成 `#ifdef` 分叉——

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

**单核回到已验证 20 轮的 `la` 直读，SMP 保留表方案。**

回滚后验证：`s31test` **20/20**、`check_image.py` **9/9**、30 命令、
`s31twai` 4/4、`s31rmt` PASS、`s31mcpwm` PASS。

### 49.6 这一轮学到什么

**"修好一个现象"不等于"修对了"。** 两个版本各自修掉一半：
- 函数调用版：cache 窗口 fault
- 静态表版：通过了 cache 窗口，引入新的挂点

**真正的结论是：trap 路径必须自包含。** 表方案方向对，但
"在 IRAM 里放带初值的数据"这条路本身需要再验证——可能要改成
**在 `rt_hw_board_init` 之前、由汇编自己写表**（`la` + `sw`，cache 还开着）。

### 49.7 状态

| 项 | 值 |
|---|---|
| 单核固件 | ✅ **20/20**、**9/9**、**30 命令**、外设全 PASS |
| 汇编 | 单核 `la` 直读（已验证），SMP 表索引（待验证） |
| `smp_stack.c` | 单核静态表（当前未使用，保留给 SMP 路径） |
| SMP | ❌ 仍不启动，但**cache 窗口这一环已确认可修** |
| 未解 | 静态表版单核为何挂在 `Single core mode` 之后 |

---

## 50. W39：表的路径修好了，并且抓到两个自己写的汇编 bug

上一轮判定"IRAM 数据不可靠"，**那个判断是错的**。这一轮找到真正的原因——
**两个我自己写出来的汇编错误**。

### 50.1 bug 1：覆盖了 `t1`

原始代码：

```asm
    mv    t1, sp                 ; t1 = 本层 context 指针
    ...
    la    t0, rt_interrupt_nest  ; 只用 t0
    lw    t0, 0(t0)
    ...
    STORE t1, 0(sp)              ; 存 context
```

我的表版本：

```asm
    mv    t1, sp
    ...
    la    t1, rt_irq_nest_slot   ; ← 我把 t1 覆盖了
    ...
    STORE t1, 0(sp)              ; 存进去的是表的地址
```

**`t1` 是这段代码里唯一不能碰的寄存器**，而我拿它当临时寄存器用。

修法：改用 `t2`。

### 50.2 bug 2：少了一次解引用

```asm
    la    t2, rt_irq_nest_slot
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t2
    lw    t0, 0(t0)        ; t0 = slot[n] = &rt_interrupt_nest  ← 是指针
    bnez  t0, nested       ; 指针永远非零 -> 永远判定为嵌套
```

**表里存的是指针，要再解引用一次才是计数器的值。**

后果非常隐蔽：板子**能启动、能打 banner**，只是 `bnez` 永远走嵌套分支，
外层永远不切中断栈，`rt_interrupt_nest` 从不归零。
`msh` 提示符出现，但**命令跑不完**。

### 50.3 之前三轮为什么没找到

| 轮次 | 判断 | 实际 |
|---|---|---|
| W37 | flash cache 窗口里的函数调用 | ✅ **对**（MEPC=0x40013b1e=rt_enter_critical 是确凿证据） |
| W38 | `IRAM_ATTR` 用在数据上不可靠 | ❌ **错**。真正的 bug 是 `t1` 被覆盖 |
| W39 | 少一次解引用 | ✅ **对** |

**W38 的错误判断是有害的**：它让我给数据加 `IRAM_ATTR`，把表挪进了 IRAM 段，
反而掩盖了 `t1` 的问题（表在 IRAM 时 `la` 的目标不同，行为略有差异）。

### 50.4 最终修法

**表的路径在单核上验证通过**：`s31test` 20/20、`check_image.py` 9/9、
30 命令、`s31twai` 4/4、`s31rmt` PASS。

```asm
    la    t2, rt_irq_nest_slot
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t2
    lw    t0, 0(t0)         ; slot[n] 的地址
    lw    t0, 0(t0)         ; 计数器本身
```

表存在普通 `.data`（`2f0187b8 D`），**不需要 `IRAM_ATTR`**：
DRAM 在 cache 关闭时依然可读，只有**指令**需要 IRAM。

### 50.5 单核现在走 SMP 的代码路径

这是本轮附带的价值：单核构建现在**也在用表**，所以 SMP 的这段汇编
被每轮自检覆盖，不再是"只在 SMP 下才执行、从没验证过"的代码。

### 50.6 SMP 仍然不启动，但失败点又前进了一步

修好两个 bug 后开 SMP：

```
MEPC : 0x40013b26     -> 仍在 rt_enter_critical 附近
RA   : 0x2f010f92     -> vPortEnterCritical
```

**新证据**：调用链是
`vPortEnterCritical -> rt_enter_critical -> rt_cpus_lock -> rt_hw_spin_lock`。

而 `rtthread_startup()` 里：

```c
rt_hw_spin_lock(&_cpus_lock);
rt_hw_secondary_cpu_up();     // 空实现
```

**这个锁在启动时被取走，靠 `rt_cpus_lock_status_restore` 在
`rt_hw_context_switch_exit` 里恢复。** SMP 下第一次进临界区就和它冲突 → 自旋死。

**这是比 cache 窗口更深的问题：SMP 的锁状态恢复链没接上。**

### 50.7 状态

| 项 | 值 |
|---|---|
| 单核固件 | ✅ 20/20、9/9、30 命令、外设全 PASS |
| 汇编 | 两处 bug 已修，单核走表路径并验证 |
| 表位置 | `.data`（`2f0187b8 D`），无 `IRAM_ATTR` |
| SMP | ❌ 仍不启动，但失败点从 "cache 窗口" 前进到 "锁状态冲突" |
| 下一轮 | `rt_cpus_lock_status_restore` / `_cpus_lock` 的状态链 |

---

## 51. W40：SMP 首次打出 banner —— 根因是 linker fragment 漏了 `*_mp` 对象

这一轮找到了 P3 卡了五轮的真正原因：**不是 SMP 语义，是一个链接脚本的遗漏**。

### 51.1 证据链

单核和 SMP 的同一函数落在不同地址：

```
单核:  rt_enter_critical = 0x2f016184   (RAM)
SMP :  rt_enter_critical = 0x40013b26   (IROM/flash)
```

而 `linker.lf` 里只写了：

```
scheduler_up:rt_enter_critical (noflash)
scheduler_up:rt_exit_critical  (noflash)
scheduler_up:rt_critical_level (noflash)
scheduler_up:rt_soft_atomic_add (noflash)
cpu_up:rt_cpu_self             (noflash)
```

**`RT_USING_SMP` 时这些翻译单元根本不在链接里**——它们的实现来自
`scheduler_mp.c` / `cpu_mp.c`，而这两个对象**没有任何 noflash 规则**。

所以 IDF 关掉 flash cache 调 PSRAM 时序时，中断路径跳进 IROM → instruction access fault。

**这是代码放置问题，不是 SMP 的锁语义问题。** 前几轮往锁的方向找是错的。

### 51.2 修法：给 `*_mp` 补上同样的规则

```diff
  scheduler_up:rt_enter_critical (noflash)
  scheduler_up:rt_exit_critical (noflash)
  scheduler_up:rt_critical_level (noflash)
+ scheduler_mp:rt_enter_critical (noflash)
+ scheduler_mp:rt_exit_critical (noflash)
+ scheduler_mp:rt_critical_level (noflash)
  scheduler_up:rt_soft_atomic_add (noflash)
+ scheduler_mp:rt_soft_atomic_add (noflash)
  thread:rt_thread_self (noflash)
  cpu_up:rt_cpu_self (noflash)
+ cpu_mp:rt_cpu_self (noflash)
+ cpu_mp:rt_cpus_lock (noflash)
+ cpu_mp:rt_cpus_unlock (noflash)
+ scheduler_mp:rt_cpus_lock_status_restore (noflash)
+ scheduler_mp:rt_sched_post_ctx_switch (noflash)
+ smp_prims (noflash)          # 整个对象：cpu_id / spin_lock / spin_unlock
```

**注意最后一行**：`smp_prims:符号名` 这种逐符号写法**没生效**
（生成的 `sections.ld` 里查不到），改成 `smp_prims (noflash)` 整对象规则才生效。
ldgen 的对象名匹配规则在这里比预想的严格。

### 51.3 修复前后对照

| 阶段 | 现象 |
|---|---|
| 修复前 | `Guru Meditation: Instruction access fault`，MEPC 在 IROM，**banner 之前就死** |
| 修复后 | **banner 正常打印，无 panic** |

`nm` 验证：

```
2f0160a8 T rt_cpus_lock          (RAM)
2f016256 T rt_enter_critical     (RAM)
2f01632a T rt_hw_cpu_id          (RAM)
2f016336 T rt_hw_spin_lock       (RAM)
```

**五个关键函数全部进 RAM。**

### 51.4 但 shell 仍不响应

banner 之后的 `s31test` 没有输出。**这不是 panic，是卡住。**

新失败点：从"banner 之前 panic"变成"banner 之后不响应"。
范围又缩小了一步。

### 51.5 单核不受影响

linker 的改动对单核构建**无害**（那些 `*_mp` 规则匹配不到任何东西），
保留在配置里。

单核验证：`s31test` **20/20**、`check_image.py` **9/9**、30 命令、
`s31twai` 4/4、`s31rmt` PASS、`s31mcpwm` PASS、`s31psram` PASS。

### 51.6 五轮排查的路线总结

| 轮次 | 判断 | 对错 |
|---|---|---|
| W35 | 链接期 11 个符号缺失 | ✅ 修好 |
| W36 | 排除"从核未启动" | ✅ 排除 |
| W37 | flash cache 窗口里的函数调用 | ✅ **方向对** |
| W38 | `IRAM_ATTR` 用在数据上不可靠 | ❌ 错 |
| W39 | 两个汇编 bug（`t1` 覆盖、少一次 `lw`） | ✅ 修好 |
| **W40** | **linker fragment 漏了 `*_mp` 对象** | ✅ **根因** |

**W37 的方向是对的，但只改了我自己写的那个函数，没想到内核函数也需要同样处理。**

### 51.7 状态

| 项 | 值 |
|---|---|
| 单核 | ✅ 20/20、9/9、30 命令、外设全 PASS |
| SMP | ⚠️ **banner 出现、无 panic**，shell 不响应 |
| 关键发现 | linker fragment 必须同时覆盖 `*_up` 和 `*_mp` |
| 下一轮 | banner 之后 shell 为何不响应 |

---

## 52. W41：SMP 现在是**软复位循环**，不是挂住

上一轮看到"banner 出现、shell 不响应"。这一轮把现象说准了：
**不是卡住，是每秒重启一次的循环。**

### 52.1 现象精确化

```
[probe] banner 正常打印
ESP-ROM:esp32s31-20251218
rst:0xc (SW_CPU_RESET),boot:0x58 (SPI_FAST_BOOT)
Core0 Saved PC:0x2f00ce2c
```

`nm` 解析：**`0x2f00ce2c` 落在 `esp_cpu_reset`**。

**是主动软复位，不是 panic** —— 佐证：

| 检查 | 结果 |
|---|---|
| `CONFIG_ESP_SYSTEM_PANIC_PRINT_REBOOT=y` | panic 会**先打印再重启** |
| 实际有没有 panic 文字 | **没有** |
| 有没有 assertion failed | **没有** |
| `rst:0xc` 次数 | **12 次/12 秒** |

每秒一次、无任何错误输出、PC 在 `esp_cpu_reset` —— 这是**主动复位循环**。

### 52.2 两个被排除的假设

**(a) `vPortEnterCritical` 的多核 abort 路径。**

`port.c` 里确实有：

```c
#if (configNUM_CORES > 1)
    esp_rom_printf("vPortEnterCritical(void) is not supported on "
                   "multi-core targets...\n");
    abort();
#endif
```

但 `sdkconfig` 里 `CONFIG_FREERTOS_NUMBER_OF_CORES=1` —— **这段不编译**。排除。

**(b) 中断看门狗（INT WDT）。**

1 Hz 循环 + 无 panic + 中断长时间屏蔽，很像 INT WDT。
**决定性实验**：把 `CONFIG_ESP_INT_WDT` 关掉重新构建烧录。

**结果：仍然 1 Hz 复位循环**（`rst:0xc` 10 次/10 秒）。**排除。**

### 52.3 剩下的方向

无 panic、无 assert、主动 `esp_cpu_reset`、1 Hz 节律。

**"1 Hz"这条线索很硬**：它说明复位是**被周期性触发的**，不是某条路径一次性的失败。
可能的周期性触发源：

| 候选 | 判断依据 |
|---|---|
| 任务看门狗（TWDT） | 需要 `esp_task_wdt_add`，本项目零调用点（`boot_tasks.c` 已记录） |
| RTC WDT | 需确认是否被 RT-Thread 启动流程意外喂到 |
| 某个 `rt_timer` 周期回调里 `ESP_ERROR_CHECK` 失败 | 1 Hz 节律吻合 |
| systick 中断里出错 | 也是 1 Hz |

**下一轮入口**：`rt_tick_increase` / 定时器回调里有没有 `ESP_ERROR_CHECK` 或
`esp_restart`。这是唯一能产生"精确 1 Hz"的位置——tick 就是 1000 Hz，
而 1 秒正好是一个 `rt_timer` 周期。

### 52.4 单核仍完好

已回滚 `RT_USING_SMP`/`RT_CPUS_NR` 与 `sdkconfig.defaults` 的 WDT 改动，
并删除 `sdkconfig` 重新生成（确保 defaults 生效）。

验证：`s31test` **20/20**、`check_image.py` **9/9**、30 命令、
`s31twai` 4/4、`s31rmt` PASS。

### 52.5 状态

| 项 | 值 |
|---|---|
| 单核 | ✅ 20/20、9/9、外设全 PASS |
| SMP | ⚠️ **1 Hz 软复位循环**（banner 之后） |
| 已排除 | `vPortEnterCritical` 多核 abort、INT WDT |
| 下一步 | tick / 定时器回调里的 `ESP_ERROR_CHECK` 或 `esp_restart` |

---

## 53. W42：SMP 的两个失败方式被区分开，1 Hz 的解释被推翻

上一轮我推断"1 Hz 说明复位被周期性触发，要去 tick/定时器里找源头"。
**这个推断是错的**，这一轮推翻了它。

### 53.1 1 Hz 是启动耗时，不是周期

bootloader 时间戳：

```
I (29)  boot: ESP-IDF ...
I (991) coexist: ...
        banner
```

**一次完整启动约 1.05 秒**，12 次/12 秒完全吻合。
所以 1 Hz 只是"每次启动耗时"，**没有周期触发源**。

**R100 的推断作废。** 这条记下来，因为它是"从现象推机制"的典型误判。

### 53.2 精确死亡位置

`main.c:253-256` 打印两行 banner：

```c
rt_kprintf("RT-Thread %d.%d.%d on ESP32-S31\n", ...);   // ← 没打印
rt_kprintf("tick=%d Hz, cpus=%d, msh on UART0\n", ...); // ← 没打印
```

而 `rt_show_version()` 的 banner（`- RT - Thread Operating System`）**打印了**。

**=> 死在 `rt_show_version()` 之后、`main()` 之前。** 这段只有
`rtthread_startup()` 的后半段。

### 53.3 两个变体，两种死法

| 配置 | 复位类型 | Saved PC | 含义 |
|---|---|---|---|
| `RT_CPUS_NR=2` | `rst:0xc (SW_CPU_RESET)` | `esp_cpu_reset` | **主动软复位** |
| `RT_CPUS_NR=1` | `rst:0x8 (HP_SYS_HP_WDT1_RESET)` | `0x2f8036c8`(ROM) | **硬件看门狗**（卡住） |

**1 核是"卡住不动"，2 核是"主动复位"。** 两个不同的失败方式，
但都停在同一个区间。

### 53.4 卡住的最可能位置：自旋锁

1 核版卡住 + HP_WDT，最可能是**自旋无法获得**。

我实现的 ticket lock 逻辑复核：

```
初始 slock = 0
第 1 次 lock:  amoadd(tmp=0, +0x10000), ticket = 0, slock = 0x00010000
               while ((slock & 0xFFFF) != 0)  ->  0 != 0 假  -> 立即获得 ✓
unlock:        owner: 0 -> 1
第 2 次 lock:  amoadd(tmp=0x00010000, +0x10000), ticket = 1, slock = 0x00020000
               while ((slock & 0xFFFF) != 1)  ->  0 != 1 真  -> 自旋！
```

**找到了**：`unlock` 推进了 `owner`（低半字 0→1），但 `slock` 的低半字是 owner，
`amoadd` 加的是高半字，所以第二次 lock 时低半字仍是 **1**？
不——`unlock` 前的 `slock = 0x00010000`（next=1, owner=0），
`unlock` 后 `owner = 1` → `slock = 0x00010001`。

第二次 lock：`amoadd` 返回旧值 `0x00010001`，`ticket = (旧值>>16) & 0xFFFF = 1`，
`slock` 变成 `0x00020001`。`while ((0x00020001 & 0xFFFF) != 1)` → `1 != 1` 假 → **获得**。

**逻辑是对的。** 那卡在哪？

### 53.5 下一个怀疑：`rt_hw_spin_lock(&_cpus_lock)` 的调用次数不配对

`rtthread_startup()`：

```c
#ifdef RT_USING_SMP
    rt_hw_spin_lock_init(&_cpus_lock);
#endif
    rt_hw_local_irq_disable();
    ...
#ifdef RT_USING_SMP
    rt_hw_spin_lock(&_cpus_lock);       // 取锁，不释放
    rt_hw_secondary_cpu_up();
#endif
```

而 `rt_system_scheduler_start()` 第一行：

```c
rt_hw_spin_unlock(&_cpus_lock);         // 释放它
```

**这个配对是对的。** 但 `rt_hw_local_irq_disable()` 之后中断一直关着，
`rt_application_init()` / `rt_thread_idle_init()` 里若有任何
`rt_hw_spin_lock` 的嵌套使用，就会自旋。

### 53.6 这一轮没解决，但排除了两个错误方向

已排除：

| 假设 | 依据 |
|---|---|
| 1 Hz 是周期触发 | bootloader 时间戳证明是启动耗时 |
| 死点在 `main()` 内 | main 的两行 banner 一行都没打印 |
| 死点在 banner 之前 | `rt_show_version()` 输出了 |
| 死点是 IRAM 放置 | 已把 5 个关键函数和 `smp_prims` 整对象放进 RAM |

### 53.7 单核完好

已回滚 `RT_USING_SMP`，删除 `sdkconfig` 与 `build` 重新生成。

验证：`s31test` **20/20**、`check_image.py` **9/9**、30 命令、
`s31twai` 4/4、`s31mcpwm` PASS、`s31rmt` PASS。

### 53.8 下一轮方向

**用探针把 `rtthread_startup()` 后半段逐句定位。** 这一轮尝试过，
但把 `SMP_PROBE` 宏插进 `components.c` 时位置算错导致编译失败，
回滚后没再试。

正确做法：**不改 `components.c`**（它是上游文件，改了要记得回滚），
而是把探针放在 `board.c` 已有的初始化序列里 + 在 `main()` 开头加一个，
先确认 main 是否真的没进。

---

## 54. W43：SMP 死点定位到 `rt_hw_context_switch_to`，探针法成熟

这一轮把死点从"`rtthread_startup` 后半段"（9 个函数）压到"**一个函数内部的一个调用**"。

### 54.1 第一步：确认 `main()` 从未进入

在 `main()` 最开头加 `esp_rom_printf`（不需要控制台设备、锁、调度器）：

```
main probe: False
```

**`main()` 确实没进。** 这排除了"main 里死了"和"banner 之后才有问题"两种可能。

### 54.2 第二步：`rtthread_startup()` 九点探针

给这 9 个调用各加 `before`/`after` 两个探针：

```
[smp] before timer_init      [smp] after timer_init
[smp] before sched_init      [smp] after sched_init
[smp] before cpus_lock       [smp] after cpus_lock
[smp] before cpu_up          [smp] after cpu_up
[smp] before app_init        [smp] after app_init
[smp] before timer_thread    [smp] after timer_thread
[smp] before idle_init       [smp] after idle_init
[smp] before defunct         [smp] after defunct
[smp] before sched_start
```

**17 个打印，第 18 个（`after sched_start`）没有。**
=> 死在 `rt_system_scheduler_start()` 内部。

### 54.3 第三步：`rt_system_scheduler_start()` 七点探针

```
[start] before unlock_cpus    [start] after unlock_cpus
[start] before irq_off        [start] after irq_off
[start] before sched_lock     [start] after sched_lock
[start] before picked         [start] after picked
[start] before removed        [start] after removed
[start] before sched_unlock   [start] after sched_unlock
[start] before switch_to
```

**13 个打印，`after switch_to` 没有。**
=> 死在 `rt_hw_context_switch_to((rt_ubase_t)&to_thread->sp, to_thread)` 内部。

### 54.4 现在的死点

`rt_hw_context_switch_to` 是 `port/common/context_gcc.S` 里的汇编：

```asm
    csrw mscratch, t0
    LOAD sp, (a0)                      ; sp = to_thread->sp
#ifdef RT_USING_SMP
    mv   a0, a1                        ; a0 = to_thread
    call  rt_cpus_lock_status_restore
#endif
    LOAD a0, 2 * REGBYTES(sp)          ; 帧里的 mstatus
    csrw mstatus, a0
    j    rt_hw_context_switch_exit
```

**三个候选**：

| # | 候选 | 判据 |
|---|---|---|
| 1 | `rt_cpus_lock_status_restore` 挂住 | 它是 RAM 函数，内部断言在 `RT_DEBUGING_ASSERT` 未定义时不编译 |
| 2 | `to_thread->sp` 是坏指针 | `LOAD sp` 之后所有操作在垃圾栈上 |
| 3 | `rt_hw_context_switch_exit` 的 FIFO 恢复路径错 | 帧格式与 `rt_hw_stack_init` 不匹配 |

**下一轮入口**：在 `rt_hw_context_switch_to` 的四个位置各插一条内联
`esp_rom_printf`（汇编里可以直接 `call esp_rom_printf`，但更简单的是
在 C 侧把 `to_thread->sp` 的值打印出来，再对比帧内容）。

### 54.5 探针方法现在成熟了

这一轮两次插探针**一次成功**，因为用了三条纪律：

1. **按行匹配，不用跨行正则** —— W42 失败是因为 CRLF/LF 混用
2. **插入后先验证计数**（`assert inserted == len(POINTS)`）
3. **插完立刻找备份文件**（`Copy-Item` 到 `tools/tmp/*.bak`）

`components.c` 和 `scheduler_mp.c` 都是**上游文件**，这一轮改完都干净回滚了
（`SMP_PROBE` 计数 0、`START_PROBE` 计数 0）。

### 54.6 单核已恢复并验证

`RT_USING_SMP` 已删除、`scheduler_up.c`/`cpu_up.c` 已恢复、
CMakeLists 的探针宏定义已清除（用 `difflib` 确认只剩 5 处预期差异）。

验证：`s31test` **20/20**、`check_image.py` **9/9**、30 命令、`s31rmt` PASS。

### 54.7 顺带发现：`s31twai` 间歇失败的具体形态

这一轮的回归里 `s31twai` 出现 **2/3 次失败**，形态是：

```
125000 bit/s: len 8, want 4
```

**不是没收到帧，是收到的帧 DLC 报 8 而不是 4。**

这和我之前记的"约 1/5 间歇失败"是同一个现象，但现在有了具体形态：
**收到的帧头部 DLC 字段是 8**。可能的原因：

- RX 缓冲区读到的不是本次发送的帧（旧帧残留，DLC 恰好是 8）
- 读取时机不对，帧头还没写完就读了

**这条比"间歇失败"具体得多，下一轮可以针对它查。** 已从"未知间歇"降级为"已知形态"。

---

## 55. W44：SMP 死点缩到 `rt_hw_context_switch_exit` 的 60 条汇编指令

这一轮从"一个调用"缩到"一个汇编函数的帧恢复段"，并排除了一个候选。

### 55.1 第一步：打印 `rt_hw_context_switch_to` 的参数

在 `scheduler_mp.c` 的 switch 调用前插一条打印：

```
[switch] thread=0x2f032780 sp=0x2f033530
```

**两个值都合法**：

- `thread=0x2f032780` 在堆区（`__heap_start__`=0x2f030a60，`__heap_end__`=0x2f050a60）
- `sp=0x2f033530` 也在堆区，是 `rt_thread_create` 分配的线程栈

**候选 (a)「to_thread 是垃圾指针」排除。**

### 55.2 第二步：在 exit 路径开头插探针

`port/common/context_gcc.S` 是我们自己的文件（不是上游），加探针安全：

```asm
rt_hw_context_switch_exit:
#ifdef RT_USING_SMP_EXIT_PROBE
    call  s31_exit_probe
#endif
```

结果：

```
[switch] thread=0x2f032780 sp=0x2f033530
[exit] restoring frame
rst: 0
```

**`[exit]` 打印了** → `rt_cpus_lock_status_restore()` **正常返回**。
**候选 (c) 排除。**

### 55.3 第三步：`main_thread_entry` 两个位置插探针

`main_thread_entry` 是线程入口（`components.c:186`）：

```c
static void main_thread_entry(void *parameter)
{
    rt_components_init();     // 第 1 个探针在它之后
    main();                   // 第 2 个在它之前
}
```

**两个探针都没打印。**

### 55.4 结论

| 检查点 | 结果 | 排除的候选 |
|---|---|---|
| `[switch]` 参数合法 | thread/sp 都在堆区 | (a) 垃圾指针 |
| `[exit]` 打印 | C 代码正常返回 | (c) 锁恢复挂住 |
| `[mte]`/`[main]` 未打印 | **mret 之后没进入线程** | — |

**死点在 `rt_hw_context_switch_exit` 的帧恢复（64 条 `LOAD`）或 `mret` 本身。**

范围从「9 个函数」→「1 个调用」→「**60 条汇编指令**」。

### 55.5 下一轮的确定性实验

在 exit 的 `mret` 之前加一条探针（我们自己的文件）：

```asm
#ifdef RT_USING_SMP_MRET_PROBE
    call  s31_mret_probe      /* 但注意：此时 sp 已跳回线程栈，寄存器已恢复 */
#endif
    mret
```

**注意**：`mret` 前所有寄存器都已从帧恢复，`call` 会破坏它们。
正确做法是**在 `addi sp, sp, 32*REGBYTES` 之前**插探针，
或者用 `csrr` 把 `mepc` 打出来。

**更简单的判据**：既然 `[exit]` 打了而 `[mte]` 没打，
**要么 `mepc` 被写坏，要么 `mstatus.MPP` 不是 M 模式**。
在 `csrw mepc, a0` 之后立刻把 `a0` 打印出来就能确认。

### 55.6 探针方法第三次成功

三次插探针（`scheduler_mp.c`、`context_gcc.S`、`components.c`）全部一次编译通过，
因为：

1. 按行匹配 + 计数断言
2. 每个文件插之前 `Copy-Item` 备份
3. 插完立刻构建验证

回滚时三个 `Copy-Item` 反向复制，探针计数全部归零，
CMakeLists 的探针宏定义也清干净了。

### 55.7 单核已恢复

`RT_USING_SMP` 删除、`scheduler_up.c`/`cpu_up.c` 恢复、`build/` 删除重建。

验证：`s31test` **20/20**、`check_image.py` **9/9**、30 命令、
`s31rmt` PASS、`s31mcpwm` PASS、`s31psram` PASS。

---

## 56. W45：SMP 死点确定在**中断返回路径**，且抓到两次探针静默失效

这一轮的收获是**推翻上一轮结论**，并用帧内容证明 `mret` 是正确的。

### 56.1 上一轮的结论是错的

上一轮说"`[mte]` 没打印 ⇒ mret 之后没进入线程"。**这是探针失效导致的误判。**

原因：我给 `components.c` 的探针用了 `RT_USING_SMP_MAIN_PROBE` 宏，
而这个宏只加进了 `set_source_files_properties` 的 `COMPILE_DEFINITIONS`——
**`components.c` 不在那个文件列表里**。宏未定义 ⇒ `MAIN_PROBE` 展开成空 ⇒ 静默无输出。

**用 `strings` 查镜像可以立刻发现**：`[mte] entered` 字符串**在**里面，
说明代码编译进去了、只是没执行。

### 56.2 第一步：直接 dump 帧内容

在 `rt_hw_context_switch_exit` 最开头（寄存器还没恢复）读帧：

```
[frame] epc=0x400115cc mstatus=0x7880 mpp=3 mpie=1
```

`nm` 解析：**`0x400115cc` = `main_thread_entry`**。

**帧内容完全正确**：epc 对、MPP=3（机器模式）、MPIE=1。

### 56.3 第二步：`mret` 之前探针

```
[mret] mepc=0x400115cc mstatus=0x80007880 mpp=3
```

**60 条帧恢复指令全部执行完，没有一条出错。** `mepc` 仍指向 `main_thread_entry`。

### 56.4 第三步：trap 入口探针（跨组件，第二次踩坑）

`vectors_rtthread.S` 属于 **riscv 组件**，而探针宏定义在 **rtthread 组件**——
`#ifdef RT_USING_SMP_FRAME_PROBE` 为假，`call` 根本没编译进去，
`nm` 里连符号都没有。

修法：在 `riscv/CMakeLists.txt` 加 `target_compile_definitions(... RT_USING_SMP_FRAME_PROBE)`。

**这是本轮第二次「宏不可见导致探针静默失效」。**

### 56.5 决定性输出

```
[mret]   mepc=0x400115cc mstatus=0x80007880 mpp=3
[trap 1] mcause=0xb8000014 mepc=0x400115cc
```

`mcause = 0xb8000014`：

- 最高位置 1 ⇒ **中断**（不是异常）
- 低 12 位 = `0x014` = 20
- `RV_EXTERNAL_INT_OFFSET = 16` ⇒ line 20 = **`SYSTIMER_TARGET0`**（OS tick）

**`mepc` 仍然是 `main_thread_entry` 的第一条指令。**

### 56.6 结论

**中断在 `main_thread_entry` 执行第一条指令之前抢占了它——这完全正常。**
`mret` 打开了 MPIE，tick 恰好到期。

**但只有 1 条 trap 打印。** 如果中断正常返回，`main_thread_entry` 会执行，
`[mte]` 就会打印。

**⇒ 死点在中断的处理或返回路径，不在 context switch。**

范围又一次缩小：从「`rt_hw_context_switch_exit` 的 60 条指令」
缩到「**一次 tick 中断的处理与返回**」。

### 56.7 为什么这是好消息

单核构建跑得好好的，同一个向量表、同一个 tick。
**差别只在于 SMP 下 `rt_interrupt_leave` 之后走的是 `rt_scheduler_do_irq_switch`，
而不是 `rt_thread_switch_interrupt_flag` 那套。**

而 `rt_scheduler_do_irq_switch` 会：
1. `rt_hw_local_irq_disable()`
2. `SCHEDULER_ENTER_CRITICAL(current_thread)` —— 但 `current_thread` 可能还是 **NULL**
3. 检查 `pcpu->irq_switch_flag`
4. `rt_hw_local_irq_enable(level)`

**第 2 步是嫌疑**：`rt_cpus_lock_status_restore` 刚把 `current_thread` 设成
`main_thread_entry` 那个线程，所以非 NULL。但 `SCHEDULER_ENTER_CRITICAL`
会访问 `RT_SCHED_CTX(curthr).critical_lock_nest`，若该字段没初始化就是垃圾。

### 56.8 探针失效的两次教训（合并成一条规则）

| 次 | 探针位置 | 宏在哪 | 结果 |
|---|---|---|---|
| 1 | `components.c` | rtthread 组件的 `set_source_files_properties` 列表（不含它） | 静默无效 |
| 2 | `vectors_rtthread.S` | rtthread 组件的 `COMPILE_DEFINITIONS`（riscv 组件看不到） | 静默无效 |

**规则：跨文件/跨组件插探针，宏必须定义在目标编译单元能看到的地方。
验证方式：`strings` 查字符串是否在镜像里，`nm` 查符号是否存在。**

**这一条能省掉整整一轮的误判。**

### 56.9 单核已恢复

所有探针文件已回滚（`components.c`、`context_gcc.S`、`vectors_rtthread.S`、
`riscv/CMakeLists.txt`、`smp_stack.c`），`RT_USING_SMP` 已删除，
`build/` 删除重建。

验证：`s31test` **20/20**、`check_image.py` **9/9**、30 命令、
`s31rmt` PASS、`s31mcpwm` PASS。

---

## 57. W46：反汇编抓到第四个 linker fragment 遗漏 —— 内联函数的每份副本

这一轮用**带 tag 的探针**把死点定位到 `call rt_interrupt_enter`，
再用 **objdump** 找到真因：**`rtatomic.h` 的内联函数在 `cpu_mp.c` 里有一份
落在 IROM 的副本。**

### 57.1 探针从"两段"改成"带 tag 的六级"

之前每条探针无参数，只能靠"第几次打印"推断位置。改成 `li a0, N; call s31_trap_stage`：

```
[stage 1]   handler entry
[stage 2]   frame save 之后
[stage 3]   nest 查询之后
[stage 4]   中断栈切换 + 保留槽位之后
（没有 stage 5）
```

**死点：`call rt_interrupt_enter` 之内或其返回后。**

### 57.2 objdump 找到真因

```
2f01635c <rt_interrupt_enter>:            (RAM)
  jal  2f0161a2 <rt_cpu_self>
  jal  2f016300 <rt_soft_atomic_add>      (RAM，irq.c 的副本)
```

修前是：

```
2f01617e: auipc ra,0x11111
2f016182: jalr  -1138(ra)   # 40126d0c <rt_soft_atomic_add>   <- IROM！
```

**`rt_soft_atomic_add` 是 `rtatomic.h` 的 inline**，每个翻译单元一份
out-of-line 副本，名字由编译器决定。`linker.lf` 里：

- `irq (noflash)` —— **整对象**，所以 irq.c 的副本在 RAM
- `scheduler_mp:rt_soft_atomic_add` —— 逐符号，覆盖到了
- `cpu_mp:rt_cpus_lock` 等 —— **逐符号，没覆盖内联副本**

### 57.3 修法：`cpu_mp` 也改成整对象

```diff
- cpu_mp:rt_cpu_self (noflash)
- cpu_mp:rt_cpus_lock (noflash)
- cpu_mp:rt_cpus_unlock (noflash)
+ cpu_mp (noflash)
```

修后 `nm`：

```
2f016250 T rt_cpus_lock
2f01635c W rt_interrupt_enter
2f016300 t rt_soft_atomic_add     <- RAM
2f016224 t rt_soft_atomic_add     <- RAM
40011688 t rt_soft_atomic_add     <- 仍是 IROM，但不在这条路径上
```

**`rt_cpus_lock` 现在只调 RAM 函数**（objdump 确认：全部 `jal 2f01xxxx`）。

### 57.4 仍然没启动，但死点更窄了

修后出现 **两次完整的 stage 1-4** 然后 `rst:1`。

**第一次 tick 的 `rt_interrupt_enter` 返回了**（否则不会有第二次 stage 1），
但 stage 5 没打印 —— 它就在 `call rt_interrupt_enter` **之后**。

**新死点：`rt_interrupt_enter` 返回后到 stage 5 之间**，
或者 stage 5 的插入位置本身有问题（`li a0,5` 被 `rt_interrupt_enter` 破坏？不，
`a0` 是调用者保存寄存器，`rt_interrupt_enter` 返回后可以直接覆盖）。

**下一轮入口**：`rt_cpus_lock` 的 ticket 状态在 ISR 上下文里是否自旋。
`rt_interrupt_enter` 只调 `rt_cpu_self` + `rt_soft_atomic_add`，两者都在 RAM；
而 `rt_soft_atomic_add` 内部调 `rt_cpus_lock`，后者取 ticket 锁。
**如果 ISR 打断了已持锁的线程，nest 计数会让它走不同分支——但 `_cpus_lock` 是全局的，
不是 per-CPU 的 `cpus_lock_nest`。这里可能是死锁点。**

### 57.5 四次 linker fragment 遗漏的完整清单

| # | 遗漏 | 轮次 |
|---|---|---|
| 1 | `scheduler_mp` 的 `rt_enter_critical` 等 | W40 |
| 2 | `cpu_mp` 的 `rt_cpus_lock` 等 | W40 |
| 3 | `smp_prims` 整对象（逐符号不生效） | W40 |
| 4 | **`cpu_mp` 的 `rt_soft_atomic_add` 内联副本** | **W46** |

**规律：逐符号规则会漏掉编译器生成的符号（内联副本、`__x` 之类）。整对象规则不会。**

### 57.6 状态

| 项 | 值 |
|---|---|
| 单核 | ✅ `s31test` 20/20、`check_image.py` 9/9、30 命令、`s31rmt`/`s31mcpwm`/`s31psram` PASS |
| SMP | ⚠️ 两次 tick 后复位，死在 `rt_interrupt_enter` 之后 |
| 保留的修复 | `linker.lf` 的 `cpu_mp (noflash)` |
| 已回滚 | 所有探针（`vectors_rtthread.S`/`interrupt.c`/`smp_stack.c`）、`RT_USING_SMP` |

---

## 58. W47：把 trap 路径的整批对象放进 RAM —— 现象从"fault"变成"卡住"

这一轮不再逐个补符号，而是把 trap 路径涉及的对象**整批**放进 DRAM。
结果：**同一种现象不再是两种原因混在一起**。

### 58.1 先量体积，再决定范围

```
librtthread.a 总计：text 107969 / data 476 / bss 10151
```

**整 archive 进 DRAM 要 105 KB，这个镜像拿不出来。** 所以范围限定在
trap 路径能到达的对象：

| 对象 | 理由 |
|---|---|
| `irq` / `context_gcc` / `cpuport` | 中断入口/出口本身 |
| `cpu_mp` / `cpu_up` | `rt_cpus_lock` 及其用到的原子操作 |
| `scheduler_mp` / `scheduler_up` | 临界区助手 |
| `smp_prims` / `smp_stack` | ticket lock、cpu id、nest 表 |
| `thread:rt_thread_self` | 从 `xPortCanYield` 可达 |

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

### 58.2 单核验证

改完先验证单核（这是纪律）：

`s31test` **20/20**、`check_image.py` **9/9**、30 命令、`s31rmt` PASS、
`s31mcpwm` PASS、`s31twai` 4/4、`s31psram` PASS。

**`*_mp` 规则在单核构建里匹配不到东西，`noflash` 对 `scheduler_up`/`cpu_up` 也无害。**

### 58.3 SMP 的现象变了

| 阶段 | `rst:` | Saved PC | 含义 |
|---|---|---|---|
| W46 之前 | `0xc SW_CPU_RESET` / instruction fault | `rt_enter_critical` 等 | **flash 取指失败** |
| W46 之后（逐符号补 `cpu_mp`） | `0x8 HP_WDT` | ROM | 卡住 |
| **W47（整对象）** | **`0x8 HP_WDT`** | **`0x2f8036c8` (ROM)** | **纯粹卡住** |

**从"指令取指失败"变成"纯粹的卡住"** —— 说明 flash 放置问题真的解决了。
现在只有一种原因：某个自旋拿不到锁。

### 58.4 这是本轮的实质产出

前五轮里，"SMP 不启动"同时混着两类原因：

1. **代码在 flash 里，cache 关闭时取指 fault**（W40/W46 修的）
2. **某个锁自旋**（还没修）

**两类原因混在同一个现象里时，修好一类只会让现象"变个样子"，看起来像没进展。**
现在只剩第 2 类，现象稳定成"卡住"，下一轮的判据就清晰了。

### 58.5 下一轮的确定性判据

`rst:0x8 HP_WDT` + `Saved PC` 在 ROM = 卡在自旋。

**具体位置**：`rt_cpus_lock` 的 ticket 自旋。

```asm
2f016164:  mv    s2, a0
2f016166:  jal   rt_cpu_self
2f01616a:  mv    s1, a0
2f01616c:  lw    s0, 0(a0)          ; current_thread
2f01616e:  beqz  s0, ...            ; NULL -> 直接返回
2f016170:  jal   rt_cpus_lock       ; **递归调用自己**（取 cpus_lock_nest）
...
2f016196:  jal   rt_enter_critical
2f016198:  lui   a0, 0x2f026
2f01619c:  addi  a0, a0, 1444       ; &_cpus_lock
2f0161a0:  jal   rt_hw_spin_lock
```

**`rt_cpus_lock` 在 `current_thread != NULL` 时会先递归调自己一次**
（为了读 `cpus_lock_nest`），第二次进去 `current_thread` 仍非 NULL，
**又递归**——如果 `lock_nest` 不是 0 就会无限递归，是 0 才继续取锁。

**这里的递归深度取决于 `cpus_lock_nest` 的初值。** 若 `_cpus` 数组没被清零，
就是垃圾值 → 递归到栈溢出 → ROM 里卡住 → HP_WDT 复位。

**下一轮验证**：启动时打印 `rt_cpu_self()->current_thread` 和
`cpus_lock_nest` 的初值。

### 58.6 状态

| 项 | 值 |
|---|---|
| 单核 | ✅ 20/20、9/9、30 命令、外设全 PASS |
| SMP | ⚠️ **纯卡住**（不再是 fault），HP_WDT 复位 |
| 保留改进 | `linker.lf` 的 9 条整对象规则（对单核无害） |
| 下一轮 | `cpus_lock_nest` 初值 / `_cpus` 是否被清零 |


---

## 59. W48：SMP 跑起来了 —— 根因是软件原子操作在 ISR 里取全局锁

这一轮是 P3 的转折点。**SMP 构建启动成功、20/20 自检通过、TWAI 4/4。**

### 59.1 根因：`rtatomic.h` 选择了"关中断"实现

`rtatomic.h` 的选择顺序：

```
1. RT_USING_STDC_ATOMIC   —— C11 标准原子
2. RT_USING_HW_ATOMIC     —— 架构提供
3. rt_soft_atomic_*       —— 关中断实现
```

**本项目走第 3 条**，因为：

- `atomic_riscv.c` **不在构建里**（CMakeLists 从未列它）
- `RT_USING_HW_ATOMIC` 未定义

于是每个 `rt_atomic_add` 都是：

```c
rt_soft_atomic_add()
  -> rt_hw_interrupt_disable()
    -> rt_cpus_lock()            /* rthw.h:146 在 SMP 下就是这么映射的 */
      -> rt_hw_spin_lock(&_cpus_lock)
```

**`rt_interrupt_enter()` 的第一行就是 `rt_atomic_add(&irq_nest, 1)`**
（`irq.c:100`），而它在**每次 trap 的开头**执行。

**⇒ 每次中断都要取全局自旋锁。**

而 `rtthread_startup()` 里取了同一个锁，只在
`rt_hw_context_switch_exit` → `rt_cpus_lock_status_restore` 里释放。
第一个 tick 恰好落在那个窗口里 → 自旋 → HP_WDT 复位。

### 59.2 修法：启用硬件原子

两步：

```cmake
# CMakeLists.txt —— 加进 common_srcs（不能放 kernel_srcs，那里有存在性检查）
set(common_srcs
    "port/common/context_gcc.S"
    "rtthread_stack_init.c"
    "${RTTHREAD_ROOT}/libcpu/risc-v/legacy_common/atomic_riscv.c")
```

```c
/* rtconfig.h */
#define RT_USING_HW_ATOMIC
```

`atomic_riscv.c` 本身就是 `amoadd.w` / `amoswap.w`，无锁。

**加上第五条 noflash 规则**（`atomic_riscv (noflash)`）——
启用新实现就多了一个 trap 路径上的对象，这条又是同一个坑的第五次。

### 59.3 结果

```
RT-Thread 5.4.0 on ESP32-S31
tick=1000 Hz, cpus=2, msh on UART0
msh >
```

```
msh > s31test
=== 20 passed, 0 failed ===
```

```
msh > list thread
thread       cpu bind pri  status      sp     stack size max used left tick
tshell         0   2   20  running 0x000001a0 0x00001000     19%   0x00000003
tidle1       N/A   1   31  ready   0x00000110 0x00000400     26%   0x00000020
tidle0       N/A   0   31  ready   0x00000120 0x00000400     37%   0x00000013
```

**`tidle0` 和 `tidle1` 都在** —— 内核确实按两核初始化了。

`s31test` **连跑 3 次全 20/20**，`s31twai` 4/4。

### 59.4 必须说清楚：CPU1 还没真跑起来

**`rt_hw_secondary_cpu_up()` 仍然是空实现。** 所以：

| 项 | 状态 |
|---|---|
| SMP 调度器（`scheduler_mp.c`） | ✅ 在跑 |
| ticket lock / per-CPU 结构 | ✅ 在跑 |
| `RT_CPUS_NR=2` 的 per-CPU 初始化 | ✅ 完成（两个 idle 线程都在） |
| **CPU1 真正执行代码** | ❌ **没有**，还是单核在跑 |

**现在的状态是"SMP 管线全部验证通过，但只有一个核在干活"。**
这仍然是有价值的一步：所有 SMP 特有的代码路径（锁、per-CPU、IRAM trap）
都被验证了，剩下的只是"把第二个核叫起来"这一件独立的事。

### 59.5 六轮排查的完整结论

| 轮次 | 判断 | 对错 |
|---|---|---|
| W35 | 链接期 11 个符号缺失 | ✅ |
| W36 | 排除"从核未启动" | ✅ |
| W37 | flash cache 窗口里的函数调用 | ✅ 方向对 |
| W38 | `IRAM_ATTR` 用在数据上不可靠 | ❌ |
| W39 | 两个汇编 bug（`t1` 覆盖、少一次 `lw`） | ✅ |
| W40 | linker fragment 漏 `*_mp` | ✅ |
| W44 | 死点在 context switch | ❌（探针未编译） |
| W45 | 死点在中断返回路径 | ✅ 方向对 |
| W46 | 内联函数副本在 IROM | ✅ |
| W47 | 整批对象进 RAM，现象变纯卡住 | ✅ |
| **W48** | **软件原子操作在 ISR 里取全局锁** | ✅ **根因** |

**真正的根因是最后一层：不是放置问题，是"原子操作不该取锁"。**
前十轮都在处理 flash 放置（都是真问题，都必须修），但它们掩盖了最后这一层。

### 59.6 为什么这一层最难看见

- 它**不产生 fault**，只产生死锁 → 没有 MEPC 可查
- 它发生在 `rt_interrupt_enter` 里，而那是 trap 的最开头
- `rt_hw_interrupt_disable` 这个名字**听起来与锁无关**（R24 同一个陷阱）
- 只有读 `rthw.h:146` 的宏映射 + `rtatomic.h` 的三级选择才能看出来

### 59.7 状态

| 项 | 值 |
|---|---|
| SMP 构建 | ✅ **启动、20/20、TWAI 4/4、连跑 3 次稳定** |
| CPU1 | ❌ 未启动（`rt_hw_secondary_cpu_up` 空实现） |
| 新增配置 | `RT_USING_HW_ATOMIC`、`atomic_riscv.c` 进 `common_srcs` |
| 新增 noflash | `atomic_riscv (noflash)`（第五条） |
| 单核构建 | ✅ 仍可用（`RT_USING_SMP` 去掉即可，`atomic_riscv` 对两者都无害） |

### 59.8 顺带修好的：`s31fpu` 的提示信息

```
msh > s31fpu
msh >FPU ok seed=%f
FPU ok seed=%f
```

**不是 FPU 的问题。** `rt_vsnprintf_tiny.c:524` 的浮点分支：

```c
case 'e': case 'E': case 'G': case 'g':
case 'f': case 'F':
    va_arg(args, double);
default:
    if (str < end) { *str = '%'; ... }   /* 原样输出 */
```

**它消费了 `%f` 的参数但什么都不打印。** 这是 tiny 格式化器的已知限制，不是 bug。

**判定逻辑本身是对的**：能走到 `rt_kprintf("FPU ok ...")` 说明
32 个浮点寄存器在两次上下文切换后都保住了。

修法：把 `%.1f` 换成整数打印：

```c
rt_kprintf("FPU ok seed=%d/10\n", (int)(seed * 10.0f));
```

现在输出 `FPU ok seed=10/10` 和 `FPU ok seed=1000/10`（两个线程的 seed）。

**这条值得记**：`%f` 在 RT-Thread 的 tiny printf 下**静默不输出**，
比报错更难发现——**输出看起来"正常"，只是少了数字。**

### 59.9 SMP 最终状态

| 项 | 值 |
|---|---|
| 启动 | ✅ `tick=1000 Hz, cpus=2` |
| `s31test` | ✅ **20/20**，连跑 3 次 |
| `check_image.py` | ✅ **9/9**，**31 命令** |
| `s31twai` | ✅ 4/4 |
| `s31rmt` / `s31mcpwm` / `s31pcnt` / `s31fpu` | ✅ PASS |
| `list thread` | `tidle0`(cpu0) + `tidle1`(cpu1) + `tshell`(cpu0) |
| **CPU1 执行代码** | ❌ **仍未启动**（`rt_hw_secondary_cpu_up` 空实现） |


---

## 60. W49：尝试启动 CPU1 —— 失败，但代码保留在开关后面

这一轮写了 `rt_hw_secondary_cpu_up()` 的实现。**它把 shell 弄坏了**，所以回滚到空实现，
把新代码保留为独立文件 + 配置开关。

### 60.1 按 IDF 的顺序写

`esp_system/port/cpu_start.c:308-345` 的 `start_other_core()`：

```c
cpu_utility_ll_enable_clock_and_reset_app_cpu();
cpu_utility_ll_enable_clock_and_reset_app_cpu_int_matrix();
ets_set_appcpu_boot_addr((uint32_t)call_start_cpu1);
esp_cpu_unstall(1);
```

**不能复用 IDF 的 `call_start_cpu1()`** —— 它末尾是 `SYS_STARTUP_FN()`，
那是 IDF 的初始化链，期待 FreeRTOS 调度器。

所以写了 RT-Thread 版本（`smp_slave.c`）：

```c
IRAM_ATTR static void s31_secondary_cpu_entry(void)
{
    __asm__(".option push; .option norelax; la gp, __global_pointer$; .option pop");
    esp_cpu_intr_set_ivt_addr(&_vector_table);
    esp_cpu_intr_set_mtvt_addr(&_mtvt_table);
    rt_hw_spin_lock(&_cpus_lock);
    rt_system_scheduler_start();
}
```

**形状与 `bsp/k210/board/cpuport_smp.c:57` 的 `secondary_cpu_c_start()` 一致。**

### 60.2 结果：启动成功，但 shell 死了

```
RT-Thread 5.4.0 on ESP32-S31
tick=1000 Hz, cpus=2, msh on UART0
msh >
（命令不执行，无复位、无 panic、无 Guru）
```

**banner 和提示符都正常，但任何命令都不响应。**

对比：回滚到空实现后，同一个构建 **20/20、`list thread` 正常、31 命令**。

### 60.3 为什么这是"更差"而不是"差不多"

| 状态 | 可用性 |
|---|---|
| CPU1 空实现 | ✅ 单核正常跑，20/20、外设全 PASS |
| **CPU1 尝试启动** | ❌ **shell 不响应** |

**"启动失败但没弄坏别的"可以接受；"启动失败且弄坏了原本能用的东西"不行。**

### 60.4 决策：开关 + 保留代码

```c
#if defined(RT_USING_SMP) && defined(RT_USING_SMP_SLAVE)
    /* smp_slave.c 的实现 */
#endif
```

```c
/* smp_prims.c */
#if !defined(RT_USING_SMP_SLAVE)
void rt_hw_secondary_cpu_up(void) { }
#endif
```

默认不定义 `RT_USING_SMP_SLAVE`，所以：

- **默认构建 = 已知可用的 SMP 基线**（cpus=2、两核 per-CPU 结构、20/20）
- **代码保留在树里**，下一轮打开开关就能继续调，不用重新推导顺序

### 60.5 下一轮的候选原因

| # | 假设 | 判据 |
|---|---|---|
| 1 | `cpu_utility_ll_enable_clock_and_reset_app_cpu()` **复位 APP CPU** 时影响了共享的时钟/中断矩阵 | 去掉这一句，只做 `esp_cpu_unstall(1)` |
| 2 | `s31_secondary_cpu_entry` 里 `rt_hw_spin_lock(&_cpus_lock)` 与 CPU0 竞争 | 先不取锁 |
| 3 | CPU1 的 `rt_system_scheduler_start()` 抢走了 CPU0 的 shell 线程 | 让 CPU1 只 `wfi` 循环，先确认它能被叫起来 |
| 4 | `esp_cpu_intr_set_mtvt_addr` 在 CPU1 上需要额外的 per-CPU 设置 | 对比 `_mtvt_table` 的每个 slot |

**第 3 条最容易验证**：把入口改成只有 `wfi`，如果 shell 正常，
说明是调度器启动的问题而不是硬件启动的问题。

### 60.6 状态

| 项 | 值 |
|---|---|
| SMP 基线 | ✅ **20/20、9/9、31 命令、`tidle0`+`tidle1` 都在** |
| CPU1 执行代码 | ❌ 仍是空实现（默认） |
| 新增 | `port/esp32s31/smp_slave.c`（开关后面）、`RT_USING_SMP_SLAVE` 选项 |
| 下一轮 | 用"只 wfi"的入口把"能叫起来"和"能调度"分开验证 |


---

## 61. W50：**CPU1 起来了** —— 用它自己的代码证明，不是靠 `cpus=2`

上一轮启动 CPU1 失败并回滚。这一轮用**二分**把两种原因分开，得到第一个
"CPU1 确实执行了我们的代码"的可用配置。

### 61.1 二分设计

上一轮的症状（shell 静默、无 panic、无复位）兼容两种完全不同的原因：

| 假设 | 含义 |
|---|---|
| (A) 启动序列错 | 时钟/复位/中断矩阵把 CPU0 也弄坏了，CPU1 从未跑起来 |
| (B) CPU1 的调度器抢走 shell | 硬件启动成功，问题在 `rt_system_scheduler_start()` |

**判据：把 CPU1 入口改成只 `wfi` 循环。**

### 61.2 结果：shell 完全正常

```
msh > s31stat
cpu1   : started (0x5a5a5a5a)
=== 20 passed, 0 failed ===
```

**`cpu1: started (0x5a5a5a5a)` 是这一轮的关键证据。**

这个值由 CPU1 的入口写进共享内存，CPU0 的 shell 读出来。
**它证明的不是"内核认为有两个核"，而是"CPU1 真的执行了我们写的指令"。**

之前所有"cpus=2"的输出都证明不了这一点——那个值来自编译期常量。

### 61.3 于是 (B) 成立，(A) 排除

启动序列（时钟、复位、中断矩阵、`ets_set_appcpu_boot_addr`、`esp_cpu_unstall`）
**全部正确**。问题在 `rt_system_scheduler_start()`。

### 61.4 试了变体 B：失败，但知道了机制

给 CPU1 加上 `rt_system_scheduler_start()`：

```
rst: 0x8 HP_WDT × 6        <- 启动循环
```

机制从源码里能看出来（`scheduler_mp.c:494`）：

```c
void rt_system_scheduler_start(void)
{
    rt_hw_spin_unlock(&_cpus_lock);      /* 无条件的，第一行 */
```

**这个 unlock 是为"到达时锁已由 CPU0 释放"的核心写的。** 而我上一版在 CPU1 入口里
先 `rt_hw_spin_lock(&_cpus_lock)`，于是：

- CPU0 在 `rtthread_startup()` 里持有该锁
- CPU1 又取了一次
- CPU1 的 `scheduler_start()` 释放一次 → 释放的可能是 CPU0 的那次持有
- ticket lock 的 owner 计数与任何持有者都对不上

**这次试着不取锁直接调 `scheduler_start()`，仍然复位** —— 说明还有一个
时序问题：CPU0 此时仍在 `rtthread_startup()` 里跑 `rt_application_init()`
等代码，两个核能同时进调度器代码。

### 61.5 交付的配置

| 变体 | 状态 | 现象 |
|---|---|---|
| **A：`wfi` 停放（默认）** | ✅ **交付** | `cpu1: started`、20/20、连跑 3 次稳定、外设全 PASS |
| B：`rt_system_scheduler_start()`（开关 `RT_USING_SMP_SLAVE_SCHED`） | ❌ | 启动循环 |

**默认是 A。** 它证明的是这一轮真正想知道的事：

> **CPU1 能被释放、能执行 IRAM 代码、能看见共享的向量表。**

这三件事之前全是推测。现在有 `0x5a5a5a5a` 这个证据。

### 61.6 这不是"双核跑起来了"

**必须说清楚**：CPU1 现在停在 `wfi` 循环里，**不做任何工作**。

| 项 | 状态 |
|---|---|
| CPU1 硬件启动 | ✅ **已验证**（`0x5a5a5a5a`） |
| CPU1 共享向量表 | ✅ 已设置 |
| **CPU1 参与调度** | ❌ **没有** |
| **双核并行** | ❌ **没有** |

`list thread` 里 `tidle1` 是 `ready` 状态但 `cpu` 列是 `N/A` ——
它存在，但从未被调度执行。

### 61.7 下一轮的方向（比这一轮更明确）

变体 B 的失败不再是"不知道哪里错"，而是**"两个核的启动时序需要编排"**：

1. CPU0 在 `rtthread_startup()` 里调 `rt_hw_secondary_cpu_up()`
2. 之后 CPU0 还要跑 `rt_application_init()`、`rt_system_timer_thread_init()`
   等，才到 `rt_system_scheduler_start()`
3. **CPU1 不能在这中间进调度器**

**修法：加一个"CPU0 已准备好"的标志**，CPU1 在入口里等它：

```c
/* CPU1 */
while (!s31_cpu0_ready) { wfi; }
rt_system_scheduler_start();

/* CPU0：在 rt_system_scheduler_start() 之前置位 */
```

**这是下一轮的第一件事。**

### 61.8 状态

| 项 | 值 |
|---|---|
| SMP + CPU1 启动 | ✅ **20/20、9/9、31 命令、连跑 3 次稳定** |
| CPU1 证据 | ✅ `s31stat` 的 `cpu1: started (0x5a5a5a5a)` |
| 外设 | ✅ `s31twai` 4/4、`s31rmt`、`s31mcpwm` PASS |
| CPU1 参与调度 | ❌ |
| 新增 | `smp_slave.c` 两个变体、`s31stat` 的 cpu1 报告、`RT_USING_SMP_SLAVE` 默认开 |


---

## 62. W51：ticket lock 在 CPU1 上验证通过 —— 并找到一个我自己写的 bug

这一轮把 CPU1 从"能跑代码"推进到"**能正确使用共享锁**"，
同时抓到一个上一轮留下的、非常典型的错误。

### 62.1 变体 B 的两次失败，PC 指向真正的受害者

| 变体 | 结果 | `Core0 Saved PC` | `nm` 解析 |
|---|---|---|---|
| W49：取锁 + 调 `rt_system_scheduler_start()` | 启动循环 | — | — |
| W50：取锁 + 停住（**不释放**） | 启动循环 `rst:0x8` | `0x2f016d30` | **`rt_hw_spin_lock`** |

**关键：`Saved PC` 是 `Core0` 的，并且落在 `rt_hw_spin_lock` 里。**

也就是说 **卡住的是 CPU0，不是 CPU1**。

### 62.2 我自己的 bug

上一版的变体 B：

```c
rt_hw_spin_lock(&_cpus_lock);      /* 取了 */
s31_cpu1_locked = 0x12345678;
for (;;) { wfi; }                  /* 没还！ */
```

**`_cpus_lock` 是全局锁。** CPU1 取走不还，CPU0 在它之后的启动路径里
再次去取 —— 直接自旋到 HP_WDT。

**这不是内核的问题，是端口代码的问题。** 修法很简单：取完就还。

### 62.3 于是这一版只做"取 + 还"

```c
rt_hw_spin_lock(&_cpus_lock);
s31_cpu1_locked = 0x12345678u;
rt_hw_spin_unlock(&_cpus_lock);
for (;;) { wfi; }
```

### 62.4 结果：通过

```
msh > s31stat
cpu1   : started (0x5a5a5a5a)
cpu1lk : acquired (0x12345678)
=== 20 passed, 0 failed ===
```

**连跑 3 次全通过**，`s31twai` 4/4、`s31rmt`、`s31mcpwm` PASS。

### 62.5 这两行输出分别证明了什么

| 输出 | 证明 |
|---|---|
| `cpu1: started (0x5a5a5a5a)` | CPU1 执行了我们的 IRAM 代码 |
| **`cpu1lk: acquired (0x12345678)`** | **ticket lock 在两核之间是一致的** |

第二条比第一条强得多。ticket lock 用的是 `amoadd.w.aqrl`，
**它要两个核对同一个字的读写是连贯的（coherent）**。
CPU1 能从 CPU0 手里拿到票、再正确归还，说明：

- CPU1 的 cache / 总线视图与 CPU0 一致
- `amoadd.w` 在 CPU1 上有效
- 票号计数没有错乱（否则 CPU0 之后的取锁会卡）

**这是"第二个核真的能用 SMP 原语"的证据。**

### 62.6 还没做的：让 CPU1 参与调度

`rt_system_scheduler_start()` 依然没在 CPU1 上调。原因在
`scheduler_mp.c:239-262` 的 picker：

```c
global = _get_global_highest_ready_prio();     // 全局表空时 = -1
local  = _get_local_highest_ready_prio(pcpu);  // CPU1 上 = 31（它的 idle）
if (global < local)                            // -1 < 31，成立
{
    ... rt_thread_priority_table[global] ...   // **[-1] 越界**
}
```

本系统所有线程都 `RT_THREAD_CTRL_BIND_CPU`，**全局表恒为空**，
所以 `global` 恒为 `-1`，这个分支**必然**走到了 `[-1]`。

这属于内核而不是端口，需要单独修 + 单独验证，所以这一轮不碰它。

### 62.7 三轮 CPU1 工作的完整链条

| 轮次 | 做到什么 | 证据 |
|---|---|---|
| W49 | 尝试启动，失败（shell 死） | — |
| W50 | **CPU1 执行了 IRAM 代码** | `cpu1: started` |
| **W51** | **CPU1 正确使用了共享 ticket lock** | **`cpu1lk: acquired`** |

**每一步都用一个新的、独立的证据，而不是靠"看起来正常"。**

### 62.8 状态

| 项 | 值 |
|---|---|
| CPU1 启动 | ✅ `cpu1: started` |
| **CPU1 用共享锁** | ✅ **`cpu1lk: acquired`** |
| CPU1 参与调度 | ❌（picker 的内核问题） |
| `s31test` | ✅ 20/20，连跑 3 次 |
| 外设 | ✅ TWAI 4/4、RMT、MCPWM PASS |
| 命令数 | 31 |


---

## 63. W52：picker 实测推翻我的越界推断，并找到 CPU1 调度的真正障碍

这一轮先用 **probe** 取代推断，结果**推翻了自己上一轮的结论**，
然后用同一条 probe 找到了真因。

### 63.1 先纠正上一轮的错误

上一轮我写：全局表空时 `global = -1`，`-1 < 31` 成立，
所以走到 `rt_thread_priority_table[-1]` **越界**。

**这是错的。** `rt_ubase_t` 是 **unsigned**（`rttypes.h:75`），
`-1` 赋给 unsigned 后是 `0xFFFFFFFF`，`0xFFFFFFFF < 31` 为 **FALSE**，
走 `else` 分支用 local 表——**完全正确**。

**教训与 R71 同源：让编译器/实测回答，不要靠推断。**

### 63.2 probe 第一次失效：插在 `return` 之后

第一次插的位置：

```c
    return highest_priority_thread;
    PICK_PROBE(...);          /* 不可达 */
```

编译器把调用和格式串一起丢掉，`strings` 里查不到 `[pick]`。

**R110 的检查法当场抓住了它**——正是"探针静默失效"那一类。挪到 `return` 之前后：

```
$ strings elf | grep '\[pick\]'
[pick] cpu=%d global=%d local=%d -> %s
```

### 63.3 实测：CPU0 的选择

```
[pick] cpu=0 global=10 local=31 -> main
[pick] cpu=0 global=8  local=31 -> esp_timer
[pick] cpu=0 global=20 local=31 -> tshell
[pick] cpu=0 global=10 local=31 -> ta
[pick] cpu=0 global=-1 local=31 -> tidle0
```

**`global=-1` 时选的是 `tidle0`（local 表）——证明 my 的越界推断确实不成立。**

`tshell` 在**全局表**里（prio 20），因为它是**未绑定**线程。

### 63.4 实测：CPU1 也进 picker（决定性）

让 CPU1 调 `rt_system_scheduler_start()`（先取锁，让那个无条件 unlock 配平）：

```
[pick] cpu=1 global=30 local=31 -> tsystem
[pick] cpu=0 global=10 local=31 -> main
[pick] cpu=1 global=10 local=31 -> main       <-- 两个核选同一条！
[pick] cpu=0 global=8  local=31 -> esp_timer
[pick] cpu=1 global=10 local=31 -> main
```

**两个核都选中 `main`。**

`main` 是**未绑定**线程，住在全局就绪表里，**对每个核都可见**。

### 63.5 后果与判据

两个核同时跑 `main` → 同一个栈、同一组寄存器被两边写 →
状态崩坏 → `CPU0 Saved PC: 0x2f016d76` → `nm`: `rt_hw_spin_lock` → HP_WDT。

**这不是 picker 的 bug，也不是锁的 bug。**

RT-Thread 的 MP 模型**假设**未绑定线程由调度器在核间分配；
它需要的平台侧前提是：

| 前提 | 本端口状态 |
|---|---|
| per-CPU tick | ❌ 只有 CPU0 有 |
| per-CPU 中断路由 | ❌ |
| 线程绑定到核（全局表为空） | ❌ 大部分线程未绑定 |

**三个前提一个都不满足**，所以 CPU1 一旦调度就会和 CPU0 抢线程。

### 63.6 决策：停在"锁已验证"

| 变体 | 结果 |
|---|---|
| CPU1 只 `wfi` | ✅ `cpu1: started` |
| CPU1 取锁 + 还锁 | ✅ **`cpu1lk: acquired`** |
| CPU1 取锁 + 调 scheduler | ❌ 两核抢同一线程 → HP_WDT |

**默认交付前两行。** 第三行的失败**已被解释清楚**，不再需要试。

### 63.7 下一轮要做什么（已经非常具体）

让 CPU1 能调度，必须先补齐三个平台前提，**按依赖顺序**：

1. **CPU1 的 tick** —— 把 `SYSTIMER_TARGET` 中断路由到 CPU1，
   或在 CPU1 上单独起一个 tick 源
2. **把中断路由分核** —— `INTERRUPT_CORE0_*_MAP` 目前全指向 CPU0
3. **让线程绑定** —— 或者接受"未绑定线程会在核间迁移"并保证迁移后仍能工作

**第 1 条是根**：没有 tick，任何迁到 CPU1 的线程都不会被抢占，等于死掉。

### 63.8 状态

| 项 | 值 |
|---|---|
| CPU1 启动 | ✅ `cpu1: started` |
| CPU1 用共享 ticket lock | ✅ `cpu1lk: acquired` |
| CPU1 参与调度 | ❌ **已查清原因**，需先补 per-CPU tick |
| `s31test` | ✅ 20/20，连跑 3 次 |
| 外设 | ✅ TWAI 4/4、RMT PASS |
| 命令数 | 31 |
| 内核文件 | ✅ 已回滚（`PICK_PROBE` 0 处） |


---

## 64. W53：找到 per-CPU tick 之前的**真正**前置条件 —— 中断栈只有一份

上一轮结论是"要让 CPU1 进调度，得先给它 tick"。这一轮去查 tick 怎么加，
结果发现**tick 之前还有一层**。

### 64.1 先确认 tick 本身可行（四条源码事实）

| # | 事实 | 出处 |
|---|---|---|
| 1 | SMP 下 tick 是 **per-CPU** 的：`rt_atomic_add(&rt_cpu_self()->tick, 1)`，`rt_timer_check()` 只在 CPU0 | `clock.c:145-175` |
| 2 | S31 有 `SYSTIMER_ALARM_OS_TICK_CORE1 = 1` 和 `ETS_SYSTIMER_TARGET1_INTR_SOURCE` | `esp_private/systimer.h:30-34` |
| 3 | IDF 的 `vSystimerSetup()` **本来就按核循环**，只是 `configNUM_CORES=1` 所以只配了 alarm 0 | `port_systick.c:111-134` |
| 4 | `esp_rom_route_intr_matrix(cpu_core, source, inum)` **支持指定核**，是端口的 `rt_hw_intr_route()` 把它硬编码成 0 | `esp_rom_sys.h:127` |

**⇒ 给 CPU1 加 tick 是纯端口工作，不用改内核。** 这一条是上一轮的正确延伸。

### 64.2 然后是关键发现：中断栈只有一份

`esp_system/ld/esp32s31/sections.ld.in:19-29`：

```ld
. = ALIGN(16);
__STACKSIZE__   = CONFIG_IDF_RTTHREAD_INTERRUPT_STACK_SIZE;
__stack_start__ = .;
*(.stack*)
. = ALIGN(16);
. += __STACKSIZE__;
__stack_cpu0 = .;
__stack_end__ = .;
```

`vectors_rtthread.S:172`：

```asm
la    sp, __stack_end__        /* 固定地址 */
```

**两个核同时进中断会用同一个栈。**

### 64.3 为什么这比 tick 更基础

tick 是**中断**。CPU1 一旦开始收 tick，就一定会和 CPU0 的中断并发：

- CPU0 正在 `rt_interrupt_enter` 里用 `__stack_end__` 往下压帧
- CPU1 的 tick 同时进来，也 `la sp, __stack_end__`
- **两层帧互相覆盖**

结果不是"CPU1 的 tick 不准"，而是**两个核的中断上下文一起被破坏**。
这比现在的"CPU1 不参与调度"糟糕得多。

**⇒ 顺序必须是：per-CPU 中断栈 → per-CPU tick → 中断路由分核 → CPU1 调度。**

上一轮把 tick 排第一，**排错了**。

### 64.4 试着写 per-CPU 栈，然后撤回

写了一个 `rt_hw_interrupt_stack_top()`：

```c
if (cpu == 0) return &__stack_end__;      /* 保持 CPU0 不变 */
return RT_NULL;                            /* CPU1 没有内存，明确返回 NULL */
```

**然后删掉了。** 原因：

1. 它**没有被任何代码调用**——加进构建就是死代码
2. 它返回 `RT_NULL` 给 CPU1，而正确的做法是**真的分配第二份栈**
3. 分配第二份栈要改 `sections.ld.in`（IDF 的文件），并且要把
   `vectors_rtthread.S` 的固定 `la sp` 换成查表——**这两件事必须一起做**，
   分开放会得到一个"有时用新栈、有时用旧栈"的半成品

**留着未接线的"看似有用的代码"比删掉更危险**——下一轮很可能直接信它。

### 64.5 这一轮的产出

**不是代码，是一个正确的依赖顺序**，以及支撑它的四条源码事实 + 一条 linker 事实。

这类"排除错误的前置顺序"的结论，价值不低于修一个 bug：
按上一轮的顺序做，会在 CPU1 tick 装上之后得到一个更难查的崩溃。

### 64.6 下一步（依赖已排序）

| 步 | 要做的事 | 涉及文件 |
|---|---|---|
| **1** | **per-CPU 中断栈**：`sections.ld.in` 分配 N 份，`vectors_rtthread.S` 按 `mhartid` 选 | IDF linker + 端口汇编 |
| 2 | CPU1 的 tick：alarm 1 + `SYSTIMER_TARGET1` + route 到 core 1 | 新文件 `smp_tick.c` |
| 3 | 中断路由分核：`rt_hw_intr_route()` 加 `cpu` 参数 | `interrupt.c` |
| 4 | CPU1 进调度器（picker 已实测会正常工作，因为全局表空时走 local） | `smp_slave_sched.c` |

**第 1 步是唯一有 IDF linker 文件改动的一步，也是最容易做错的一步。**

### 64.7 状态

| 项 | 值 |
|---|---|
| CPU1 启动 | ✅ `cpu1: started` |
| CPU1 用共享锁 | ✅ `cpu1lk: acquired` |
| CPU1 tick | ❌ **被中断栈阻塞** |
| CPU1 参与调度 | ❌ |
| 当前构建 | ✅ 20/20、9/9、31 命令、外设全 PASS |
| 新增文件 | 无（写了又删，见 64.4） |


---

## 65. W54：per-CPU 中断栈落地 —— CPU1 收中断的前置条件已满足

上一轮查出"中断栈只有一份"是 CPU1 收中断的真正障碍。这一轮把它修掉，
**并且没有改 IDF 的 linker 脚本**。

### 65.1 方案选择：静态数组而不是改 linker

两个选项：

| 方案 | 代价 |
|---|---|
| 改 `sections.ld.in` 分配 N×size | 动 IDF 文件，回滚是 linker 变更 |
| **CPU1 用 `.bss` 静态数组** | **只动本组件源码，回滚是删一行** |

选第二个。CPU0 继续用 `__stack_end__`，**行为一字未变**。

### 65.2 实现

`port/esp32s31/smp_istack.c`：

```c
static char s31_cpu1_istack[CONFIG_IDF_RTTHREAD_INTERRUPT_STACK_SIZE]
    __attribute__((aligned(16)));

void *rt_irq_stack_top[RT_CPUS_NR] =
{
    &__stack_end__,                              /* CPU0：和以前一样 */
    s31_cpu1_istack + sizeof(s31_cpu1_istack),   /* CPU1：独立一份 */
};
```

`vectors_rtthread.S` 的选择逻辑，**和上面三行的 nest 查表完全同构**：

```asm
    la    t2, rt_irq_stack_top
    csrr  t0, mhartid
    slli  t0, t0, 2
    add   t0, t0, t2
    lw    sp, 0(t0)          /* sp = 本核的栈顶 */
```

**为什么不用 `void *const`**：const 指针数组会被放进 `.flash.rodata`，
而这段代码在 flash cache 关闭时运行 —— 正是本端口已经修过两次的坑。

### 65.3 ELF 级验证

```
2f019338 00000008 D rt_irq_stack_top        <- .data，不是 .rodata
2f01fe10 0000a000 b s31_cpu1_istack         <- .bss，0xa000 = 40960 B
```

表内容（`objdump -s` 直接读 ELF）：

```
2f019338 50ba032f 109e022f
```

| 项 | 值 | 核对 |
|---|---|---|
| `rt_irq_stack_top[0]` | `0x2f03ba50` | = `__stack_end__` ✅ **与改前相同** |
| `rt_irq_stack_top[1]` | `0x2f029e10` | = `0x2f01fe10 + 0xa000` ✅ |

**CPU0 的栈顶一字未变，这是"没有回归"的硬证据。**

### 65.4 链接期踩的坑

第一次链接失败：

```
vectors_rtthread.S:186: undefined reference to `rt_irq_stack_top'
```

原因与 `rt_irq_nest_slot` 当初完全相同：**汇编引用的符号没有任何 C 代码引用**，
archive member 不被拉进来。

修法一样：

```cmake
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u rt_irq_nest_slot" "-u rt_irq_stack_top")
```

**R44 那条"每个被汇编引用的符号都要有 -u"又验证了一次。**

### 65.5 回归

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ 20/20，**连跑 3 次** |
| `check_image.py` | ✅ 9/9，31 命令 |
| `cpu1: started` + `cpu1lk: acquired` | ✅ 都在 |
| `s31twai` | ✅ 4/4，**连跑 3 次** |
| `s31rmt` / `s31mcpwm` | ✅ PASS |

### 65.6 现在的状态和下一步

**CPU1 收中断的前置条件已经满足。** 依赖链：

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | per-CPU 中断栈 | ✅ **本轮完成** |
| 2 | CPU1 的 tick（alarm 1 + TARGET1 + route 到 core 1） | 下一步 |
| 3 | 中断路由分核（`rt_hw_intr_route` 加 cpu 参数） | |
| 4 | CPU1 进调度器 | |

第 2 步现在可以做了：栈是按核的、`rt_irq_stack_top[1]` 已验证指向独立内存、
`cpu1lk` 证明锁在 CPU1 上可用。


---

## 66. W55：tick 的顺序约束 —— 又是一层必须往前提的依赖

上一轮结论是"per-CPU 中断栈完成，下一步可以装 CPU1 的 tick"。这一轮去读 tick 的处理路径，
发现**tick 之前还有一层**，而且是我上一轮没看到的。

### 66.1 关键约束：tick 会解引用 `current_thread`

`rt_sched_tick_increase()`（`scheduler_comm.c:294`）：

```c
struct rt_thread *thread;
thread = rt_thread_self();
rt_sched_lock(&slvl);

if (RT_SCHED_PRIV(thread).remaining_tick > tick)   /* 没有 NULL 检查 */
```

CPU1 上调度器还没启动时：

```c
rt_cpu_self()->current_thread == NULL
rt_thread_self()               == NULL
RT_SCHED_PRIV(NULL)            -> 解引用空指针
```

**⇒ tick 不能在 CPU1 调度器启动之前打开。**

### 66.2 于是顺序又往前推一步

| 步 | 内容 | 状态 |
|---|---|---|
| 1 | per-CPU 中断栈 | ✅ W54 完成 |
| **2** | **线程默认绑定到 CPU0**（让全局就绪表为空） | 下一轮 |
| 3 | CPU1 调度器启动（picker 选中 `tidle1`，`current_thread` 非 NULL） | |
| 4 | CPU1 的 tick | |
| 5 | 中断路由分核 | |

第 2 步来自 R133（未绑定线程会被任一核选中）。**只有全局表为空，
CPU1 的 picker 才会选中它自己的 `tidle1`**，而不是抢 CPU0 的 `main`/`tshell`。

**这是三轮里第三次修正顺序。** 每次都是同一类问题：一个动作的前提条件，
比它看起来要深一层。

### 66.3 这一轮的实物产出：`s31smp` 命令

前面每一轮都是"读源码 → 插探针 → 得到结论 → 删探针"。
下一轮又要重新推导同样的东西。所以把**可测量的状态变成永久命令**：

```
msh > s31smp
cpus   : 2, running on cpu 0

cpu cur_thread idle_thread irq_nest tick  istack
--- ---------- ----------- -------- ----- --------
  0   2f03e870    2f01fa20        0  2966 0x2f03ba50
  1          0    2f01fac4        0     0 0x2f029e10

istack : distinct per core
```

**这一屏独立地证明了四件事**（不需要任何探针）：

| 列 | 值 | 含义 |
|---|---|---|
| `cur_thread` cpu1 = `0` | NULL | **CPU1 确实没有在跑线程**（不是"看起来像") |
| `idle_thread` cpu1 = `2f01fac4` | 非 0 | 两核的 idle 线程都存在 |
| `tick` cpu1 = `0` | 0 | **CPU1 一次 tick 都没收到**（印证 66.1 的顺序问题） |
| `istack` | `2f03ba50` / `2f029e10` 且判为 distinct | per-CPU 栈生效 |

**`tick` 那一列特别有用**：它是"CPU1 有没有 tick"的直接证据，
而这正是下一步要做的事——做完再跑一次，这个数应该开始增长。

### 66.4 命令文件踩的老坑：又是 gc-sections

第一次编译通过、烧进去、`s31smp: command not found`。

`nm` 里根本没有 `cmd_smp` —— `MSH_CMD_EXPORT` 只写 section，**没有代码引用这个对象**，
`--gc-sections` 把整个文件丢了。

**这是 R44 的第五次。** 修法与其它命令文件一致：

```c
int s31_smp_cmd_init(void) { return 0; }     /* 供 board.c 引用 */
```

```c
/* board.c */
#ifdef RT_USING_SMP
    s31_smp_cmd_init();
#endif
```

**用"被调用的初始化函数"而不是 `-u`**：调用点本身就是文档，
而且不会在符号改名后静默失效。

### 66.5 回归

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ 20/20 |
| `check_image.py` | ✅ 9/9，**32 命令**（新增 `s31smp`） |
| `s31stat` | ✅ `cpu1: started` + `cpu1lk: acquired` |
| `s31smp` | ✅ 新的 per-CPU 状态视图 |

### 66.6 状态

| 项 | 值 |
|---|---|
| per-CPU 中断栈 | ✅ `istack: distinct per core` |
| CPU1 启动 | ✅ |
| CPU1 tick | ❌ `tick = 0`，**且必须等调度器先起来** |
| CPU1 调度 | ❌ `cur_thread = NULL` |
| 命令数 | **32** |


---

## 67. W56：**CPU1 开始调度了** —— 根因是 unlock 次数，不是线程绑定

这一轮是 P3 的终点。`list thread` 里 `tidle1` 第一次出现在 **cpu 1** 上、
`running` 状态。

### 67.1 先试了"绑定线程到 CPU0"，但它不是解

按 W55 的推论改了 `scheduler_comm.c:41`：

```c
- RT_SCHED_CTX(thread).bind_cpu = RT_CPUS_NR;
+ RT_SCHED_CTX(thread).bind_cpu = 0;
```

**然后 CPU1 一进调度器，CPU0 又卡在 `rt_hw_spin_lock`（`0x2f016d30`）。**

回滚绑定改动后**仍然失败** —— 说明绑定不是根因，只是我以为的根因。

### 67.2 真正的根因：一次取锁、两次放锁

`smp_slave_sched.c` 原来是：

```c
rt_hw_spin_lock(&_cpus_lock);          /* 取 1 次 */
s31_cpu1_locked = 0x12345678u;
rt_hw_spin_unlock(&_cpus_lock);        /* 放第 1 次 */
...
rt_system_scheduler_start();           /* 里面又放第 2 次 */
```

而 `rt_system_scheduler_start()`（`scheduler_mp.c:493`）第一行就是：

```c
rt_hw_spin_unlock(&_cpus_lock);        /* 无条件 */
```

**一次取、两次放** → ticket lock 的 owner 计数超过 ticket →
**CPU0 拿着一个永远不会被服务的票，无限自旋。**

这正是 `Core0 Saved PC: 0x2f016d30 → rt_hw_spin_lock` 的含义。

### 67.3 修法：取一次，让 scheduler 去放

```c
rt_hw_spin_lock(&_cpus_lock);
s31_cpu1_locked = 0x12345678u;
rt_system_scheduler_start();           /* 它内部放 */
```

**与 RT-Thread 官方 SMP 端口一致**（`bsp/k210/board/cpuport_smp.c`
的 `secondary_cpu_c_start()` 就是取锁后直接调 `rt_system_scheduler_start()`）。

### 67.4 结果：`tidle1` 在 cpu 1 上 running

```
msh > list thread
thread       cpu bind pri  status
tshell         0   0   20  running
tidle1         1   1   31  running      <- **CPU1 在跑它自己的 idle**
tidle0       N/A   0   31  ready
```

```
msh > s31smp
cpu cur_thread idle_thread irq_nest tick  istack
  0   2f03e870    2f01fa20        0  2960 0x2f03ba50
  1   2f01fac4    2f01fac4        0     0 0x2f029e10     <- cur_thread == idle_thread
```

**`cur_thread == idle_thread == 0x2f01fac4`** —— CPU1 的当前线程就是它自己的 idle。

### 67.5 一个反直觉的结论：绑定不是必需的

**回滚 `scheduler_comm.c` 之后仍然成功。** 内核处于 upstream 状态
（`bind_cpu = RT_CPUS_NR`），`git status` 干净。

那 W52 的"两核选中 main"怎么解释？

**锁的等待本身就是同步点：**

1. CPU1 在 `rt_hw_spin_lock(&_cpus_lock)` 上自旋
2. CPU0 持有该锁，直到它自己的 `rt_system_scheduler_start()`
3. 在那之前，CPU0 **已经把 `main` 从就绪队列里摘走**（设为 RUNNING）
4. CPU1 拿到锁时，**全局就绪表已经空了**
5. picker 走 local 分支 → 选中 `tidle1`

**这是设计好的，不是巧合。** `rt_system_scheduler_start()` 一进门就放锁，
正是因为"放锁"这个动作代表"CPU0 已经接管了第一个线程"。

W49/W52 之所以看到两核抢 `main`，是因为 unlock 次数错乱，
**CPU1 从未真正等到那个时刻**。

### 67.6 三轮里被同一个 bug 骗了三次

| 轮次 | 现象 | 我的判断 | 实际 |
|---|---|---|---|
| W49 | shell 静默 | CPU1 抢走 shell | unlock 次数错乱 |
| W52 | 两核选中 main | 需要绑定线程 | unlock 次数错乱 |
| W55 | tick 解引用 NULL | 顺序要前推 | 正确的独立事实 |
| **W56** | **仍卡 spin_lock** | **绑定不是解，去查锁** | **unlock 次数错乱** ✅ |

**"两核选中 main" 是真实观测，但它是 unlock 错乱的后果，不是原因。**
把观测当原因，就会去改绑定 —— 而绑定改了也没用。

### 67.7 现在真正做到了什么

| 项 | 之前 | 现在 |
|---|---|---|
| CPU1 执行 IRAM 代码 | ✅ W50 | ✅ |
| CPU1 用共享 ticket lock | ✅ W51 | ✅ |
| per-CPU 中断栈 | ✅ W54 | ✅ |
| **CPU1 参与调度** | ❌ | ✅ **`tidle1` running on cpu 1** |
| CPU1 有自己的 tick | ❌ `tick = 0` | ❌ 仍未做（下一步） |

**双核调度已经真的在跑了**，只是 CPU1 上只有 idle —— 因为还没有 tick，
也没有任何线程被绑定到它。这两件事是接下来的工作，而不再是"能不能"的问题。

### 67.8 回归

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ 20/20，连跑 3 次 |
| `check_image.py` | ✅ 9/9，32 命令 |
| `s31twai` | ✅ 4/4 |
| `s31rmt` / `s31mcpwm` | ✅ PASS |
| `s31stat` | ✅ `cpu1: started` + `cpu1lk: acquired` |
| 内核 | ✅ **upstream 状态**（`bind_cpu = RT_CPUS_NR`，git clean） |

---

## 68. W57（收尾）：P3 达成，全命令回归通过

第 40 轮，做收尾验证与状态归档。**M7 双核 SMP 已达成。**

### 68.1 P3 的验收证据

```
msh > list thread
thread       cpu bind pri  status      sp     stack size max used left tick
tshell         0   0   20  running 0x000001a0 0x00001000     19%   0x00000001
tidle1         1   1   31  running 0x00000110 0x00000400     30%   0x00000020
tidle0       N/A   0   31  ready   0x00000120 0x00000400     37%   0x00000011
```

```
msh > s31smp
cpus   : 2, running on cpu 0

cpu cur_thread idle_thread irq_nest tick  istack
  0   2f03e870    2f01fa20        0  2960 0x2f03ba50
  1   2f01fac4    2f01fac4        0     0 0x2f029e10

istack : distinct per core
```

**`tidle1` 的 `cpu` 列是 1 且 `status` 是 `running`** —— CPU1 在跑它自己的 idle 线程。
`s31smp` 的 `cur_thread == idle_thread` 独立印证同一件事。

### 68.2 全命令回归（32 条命令）

| 命令 | 结果 |
|---|---|
| `s31test` | ✅ **20/20**（连跑 3 次） |
| `check_image.py` | ✅ **9/9**，32 命令 |
| `s31twai` | ✅ 4/4 bit rates |
| `s31rmt 100 20` | ✅ PASS |
| `s31mcpwm 1000 500 10` | ✅ PASS |
| `s31pcnt 0 16 100` | ✅ PASS |
| `s31psram 64` | ✅ PASS |
| `s31led 4 0 4` | ✅ `gpio 60 <- R=4 G=0 B=4` |
| `s31fpu` | ✅ `seed=10/10`、`seed=1000/10` |
| `s31adc 1 0` | ✅ raw 1759/1760/1766（稳定） |
| `s31events` | ✅ PASS，payload=0x5a31 |
| `s31wifi` | ✅ PASS，STA_START，MAC `1c:29:04:d0:14:1e` |
| `s31ble` | ✅ PASS，NimBLE host synced，addr `...:1f` |
| `s31smp` | ✅ per-CPU 状态 |
| `s31stat` | ✅ `cpu1: started` + `cpu1lk: acquired` |

### 68.3 仓库状态

```
rt-thread       : git status 干净（0 处改动）
esp-idf-rtt     : 12 个文件修改，全部在 components/ 下
```

**上游 `rt-thread` 完全未被修改。** 所有工作都在 `esp-idf-rtt` 的
`components/rtthread/`（端口）与 `components/riscv/`（向量）里。

这一点很重要：`RTTHREAD_ROOT` 指向的是一份**未打补丁的上游 checkout**，
以后升级 RT-Thread 版本时不需要重新应用任何补丁。

### 68.4 P0–P3 的完成情况

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 环境与基线 | ✅ |
| P1 | 单核最小内核 | ✅ |
| P2 | 中断、设备框架与 msh | ✅ |
| **P3** | **双核 SMP** | ✅ **本阶段达成** |
| P4 | 外设扩展 | 🔄 7 类完成，4 类待硬件 |

### 68.5 尚未完成的事（明确列出，不含糊）

| 项 | 状态 | 阻塞原因 |
|---|---|---|
| **M6 连接真实 AP** | ⬜ | **需要 SSID/密码**，属环境事项不是技术问题 |
| M6.5 MicroPython | ⬜ | 依赖 M6（需要能真正连网的 `esp_netif`） |
| **CPU1 的 tick** | ⬜ | 未做。`s31smp` 的 `tick` 列 CPU1 恒为 0 |
| 线程绑定到 CPU1 | ⬜ | 未做。目前 CPU1 只有 idle |
| SD/MMC、USB、LCD、Camera | ⬜ | **需要物理外设** |

**P3 的"双核 SMP"达成的是**：CPU1 启动、能用共享锁、有独立中断栈、
参与调度（跑 idle）。
**还没达成的是**：CPU1 有 tick、有业务线程跑在上面。

这个区分必须保留——"双核跑起来了"和"双核都在干活"是两件事。

### 68.6 下一步

1. **CPU1 的 tick**（现在可以做了：`current_thread` 非 NULL，R139 的约束已满足）
2. 线程绑定到 CPU1 并验证并发正确性
3. M6 需要用户提供 AP 凭据

### 68.7 文档规模

| 文档 | 行数 |
|---|---|
| `rtthread-esp32-s31-porting-log.md` | 7404 |
| `rtthread-esp32-s31-porting-plan.md` | 4240 |
| `rtthread-esp32-s31-bringup-runbook.md` | 3935 |

三份文档记录了 P0→P3 的每一步，包括**所有被推翻的结论**（W44、W51 的越界推断、
W52 的绑定假设）与它们的推翻依据。


---

## 69. W58：**M6 达成 —— 连上真实 AP**，以及外设覆盖清单

### 69.1 第一个结论：5 GHz 是硬件问题，不是配置问题

用户说"连当前电脑连接的热点"。查了一下：

```
SSID    : 刻于晴空之上_5G
Band    : 5 GHz
Channel : 157
```

而 `SOC_WIFI_SUPPORT_5G` **在整个 IDF 里只有 `esp32c5` 定义**，
`esp32s31/include/soc/soc_caps.h` **没有**。官方板子文档也两次写"2.4GHz Wi-Fi 6"。

**⇒ S31 是 2.4 GHz 器件，5 GHz 的信标它在射频层面就收不到。**
这和"密码对不对""配置有没有写对"无关。

### 69.2 用 `s31wifiscan` 把这件事变成可测的

新命令（第 33 条），扫一遍并打印信道：

```
s31wifiscan: scanning (2.4 GHz only - the S31 has no 5 GHz radio)

  # ch  rssi  bssid              ssid
  - --  ----  -----------------  ----
  1  1   -50  d4:ee:07:40:1f:26  刻于晴空之上_2.4G      <- **同一个路由器的 2.4G**
  2 11   -59  f6:6e:44:94:3e:95  别松手庞
  3  9   -70  58:d0:61:0a:ba:c2  CQNU-Guest
  ...
 14  1   -79  c0:f6:c2:d9:e1:51  CQNU-Eduroam

s31wifiscan: 14 AP(s), all on channels 1..14 = the 2.4 GHz band
```

**关键发现：那个路由器同时广播 `_2.4G` 和 `_5G` 两个 SSID。**
5 GHz 那个看不见，2.4 GHz 那个 **RSSI -50（最强）**。

**⇒ 不需要换热点，连 `_2.4G` 就行。**

### 69.3 第二个坑：设了密码不等于发起连接

给 `s31wifi` 加上 `[ssid password]` 参数后直接测，结果：

```
s31wifi: associating with "刻于晴空之上_2.4G" ...
s31wifi: FAIL, not associated within 15000 ms
```

**等了 15 秒，连一个 disconnect 事件都没有。**

这个"沉默"本身就是线索：

| 现象 | 含义 |
|---|---|
| 密码错 | 会收到 `WIFI_EVENT_STA_DISCONNECTED` + reason code |
| AP 不在范围 | 同样会有 disconnect/reason |
| **什么都没收到** | **根本没发起连接** |

读 API：`esp_wifi_set_config()` 只**存储**凭据，`esp_wifi_start()` 只**启动** station。
**关联必须显式调 `esp_wifi_connect()`。**

### 69.4 修好之后：350~400 ms 连上

```
s31wifi: PASS, associated in 350 ms
bssid d4:ee:07:40:1f:26  ch 1  rssi -49  auth 3
```

驱动日志（这次有了，因为在真正关联）：

```
wifi:state: init -> auth (0xb0)
wifi:state: auth -> assoc (0x0)
wifi:state: assoc -> run (0x10)
wifi:connected with 刻于晴空之上_2.4G, aid = 1, channel 1, BW20
wifi:security: WPA2-PSK, phy: bgn, rssi: -49
```

**这是第一次有真实网络流量经过 FreeRTOS shim。**

### 69.5 一个诊断原则

**"超时且没有错误事件" ≠ "对端拒绝"。** 它是"请求根本没发出"的签名。
如果当时直接去猜密码或信道，会浪费很多轮。

**判据：任何"等待某事件超时"的代码，都要问"如果这个事件永远不会来，
是因为对方拒绝，还是因为我没请求？"**

### 69.6 外设覆盖清单（基于 soc_caps 实测）

S31 支持但没有移植的：

| 外设 | `soc_caps` | 状态 | 需要硬件吗 |
|---|---|---|---|
| **I2S** | `SOC_I2S_SUPPORTED` | ❌ 未移植 | 需要音频编解码器才能验证 |
| **SD/MMC** | `SOC_SDMMC_HOST_SUPPORTED` | ❌ 未移植 | **需要 SD 卡** |
| **USB OTG** | `SOC_USB_OTG_SUPPORTED` | ❌ 未移植 | **需要 USB 设备** |
| **LCD（I80/RGB）** | `SOC_LCD_I80/RGB_SUPPORTED` | ❌ 未移植 | **需要屏** |
| **Camera（DVP）** | `SOC_LCDCAM_CAM_SUPPORTED` | ❌ 未移植 | **需要摄像头** |
| **Touch** | `SOC_TOUCH_SENSOR_SUPPORTED` | ❌ 未移植 | 板上有触摸焊盘才能测 |
| **温度传感器** | `SOC_TEMP_SENSOR_SUPPORTED` | ❌ 未移植 | **不需要**，片内 |
| **JPEG 编解码** | `SOC_JPEG_CODEC_SUPPORTED` | ❌ 未移植 | 不需要（可测编解码） |
| **PPA（像素加速）** | `SOC_PPA_SUPPORTED` | ❌ 未移植 | 不需要 |
| **ETM（事件矩阵）** | `SOC_ETM_SUPPORTED` | ❌ 未移植 | 不需要 |
| **LEDC** | `SOC_LEDC_SUPPORTED` | ✅ `s31pwm` 已用 | — |

**S31 没有的**（不要做）：

| 外设 | 证据 |
|---|---|
| **DAC** | `SOC_DAC_SUPPORTED` 未定义 |
| MIPI CSI/DSI | 未定义 |
| ISP | 未定义 |

### 69.7 回答"需要我插外设吗"

**分三类：**

| 类别 | 外设 | 你可以做什么 |
|---|---|---|
| **纯软件，现在就能做** | 温度传感器、JPEG 编解码、PPA、ETM、I2S（回环） | 不用插任何东西 |
| **需要外设才能验证** | SD 卡、USB、LCD、摄像头、触摸 | **插上我就能测** |
| **不需要做** | DAC、MIPI | 芯片没有 |

**最有价值的插入顺序**（按"验证信息量 / 成本"）：

1. **SD 卡**（几乎人人有）—— SDIO 4-bit 是最复杂的总线之一，能验证 DMA、中断、文件系统三层
2. **USB 设备**（U 盘或键鼠）—— 验证 USB Host 栈
3. **摄像头（OV2640）** —— DVP + JPEG 编码器可以一起验证
4. **I2S 音频模块** —— 没有也能用回环验证时序
5. **LCD** —— 最贵，最后做

### 69.8 状态

| 项 | 值 |
|---|---|
| **M6 连接真实 AP** | ✅ **PASS，350~400 ms，WPA2-PSK，ch 1，RSSI -49** |
| 命令数 | **33**（新增 `s31wifiscan`） |
| `s31test` | ✅ 20/20 |
| `check_image.py` | ✅ 9/9 |
| 新增坑 | `esp_wifi_connect()` 必须显式调用 |

---

## 70. W59：片内外设尝试撞上 SMP 竞态 —— 记录证据并按证据回滚

这一轮目标：做片内的四个外设（温度传感器、JPEG、PPA、ETM）+ 推进 Wi-Fi。
**结果：一个外设没做成，但把"M6 达成"之后暴露的一个竞态查清了。**

### 70.1 温度传感器：编得进、链接失败、终于跑不起来

按正常流程做 `tsens.c`，遇到三个障碍，逐个记录：

| 阶段 | 现象 | 原因 |
|---|---|---|
| 编译 | `driver/temperature_sensor.h` 找不到 | `PRIV_REQUIRES` 不把 include 目录放到编译行，要显式 `PRIV_INCLUDE_DIRS` |
| 链接 | `undefined reference to temperature_sensor_install` | `libesp_driver_tsens.a` 在 `librtthread.a` **之前**，单遍链接器已过 |
| 修复 | 加 `-u temperature_sensor_install` 等 | 但**这一步引入了回归** |

**第三个障碍的教训不在这三步本身，而在它引发的回归。**

### 70.2 回归：能打印 banner，但命令不执行

加完 tsens 后：

```
- RT -     Thread Operating System
 / | \     5.4.0 build Oct 31 2026 23:31:21
（停住 —— 没有 "RT-Thread 5.4.0 on ESP32-S31"，没有 msh >）
```

更早的版本还会打印 `msh >` 但不执行命令。**两种形态，指向同一件事：
启动链被阻塞。**

### 70.3 定位方法：用"已知可用的固件"做基线

我在 W48 时保存过一份当时验证通过的 `smp_working.bin`。这一轮把它烧回去：

```
RESTORED bin -> passed: True
msh >s31test
=== ESP32-S31 self test ===
[PASS] tick     2000 ticks in 2000000 us = 1000 us/tick
[PASS] ctx      min 112095 loops/thread, 0 guard errors, 4 threads
```

**同一块板、同一根线、同一套工具 —— 旧固件能跑。**

**⇒ 确定是软件回归，不是硬件问题、不是串口问题。**
这一步省掉了整轮的环境排查。

### 70.4 二分：逐个排除

| 去掉什么 | 结果 |
|---|---|
| tsens.c + 相关 CMake | ❌ 仍不执行 |
| 再去掉 s31wifiscan.c | ✅ **version 行和 msh > 都回来了**，20/20 |

**⇒ `s31wifiscan.c` 是罪魁。** 它自己没被调用，但它把 `esp_wifi_scan_*` 的引用带进镜像，
从而拉进 `libesp_wifi` 的更多 init 构造。

### 70.5 但真相不是 s31wifiscan

去掉之后，`s31test` **20/20 连跑 3 次**。然后我注意到另一件事：

**`s31smp` 显示 `cpu1 cur_thread = 0`。**

而 W56 时那里是 `2f01fac4`（CPU1 在跑 idle）。

**⇒ 那个"CPU1 参与调度"的配置，在这轮所有重建里都没有生效。**

### 70.6 关键判断：W56 是一次侥幸

对比：

| 配置 | W56 当时 | 这轮每次重建 |
|---|---|---|
| CPU1 调 `rt_system_scheduler_start()` | ✅ 3 次都过 | ❌ banner 后卡住 / 命令不执行 |
| CPU1 park 在 wfi | ✅ | ✅ |

**同一份代码，行为随机构而变 ⇒ 是竞态，不是配置错误。**

可疑窗口在 `rt_system_scheduler_start()`（`scheduler_mp.c:493`）：

```c
rt_hw_spin_unlock(&_cpus_lock);      /* 先放锁 */
...
_fast_spin_lock(&_mp_scheduler_lock);
to_thread = _scheduler_get_highest_priority_thread(...);   /* 再读就绪表 */
```

**放锁和读就绪表之间有一个窗口**，而另一个核在
`rt_scheduler_do_irq_switch()` 里编辑同一批表。

### 70.7 决策：按证据回滚，不靠"上次是好的"

```c
rt_hw_spin_lock(&_cpus_lock);
s31_cpu1_locked = 0x12345678u;
rt_hw_spin_unlock(&_cpus_lock);       /* 只验证锁 */
for (;;) { wfi; }                     /* 不进调度器 */
```

回滚后：**20/20 连跑 3 次**、`s31twai` 4/4、`s31rmt` PASS、32 命令。

**W56 的"M7 达成"必须降级为"未稳定复现"。** 这是这一轮最重要的修正。

### 70.8 这一轮真正得到的东西

1. **`-u` 拉进来的驱动可能带 init 构造**，在这个端口上会撞 RT-Thread 的启动链
2. **保存一份已知可用的 bin** 是最快的基线判定手段（W48 的那一份救了这一轮）
3. **同一配置时而通过、时而失败 ⇒ 竞态**，不要记成"通过"
4. **W56 的结论作废**：CPU1 调 `rt_system_scheduler_start()` 在本端口上不稳定

### 70.9 状态

| 项 | 值 |
|---|---|
| `s31test` | ✅ 20/20，连跑 3 次 |
| `s31twai` / `s31rmt` | ✅ PASS |
| 命令数 | 32 |
| **CPU1 参与调度** | ❌ **回滚，W56 结论作废** |
| CPU1 启动 + 共享锁 | ✅ 仍成立（W50/W51） |
| 温度传感器 | ❌ 未完成，卡在驱动 init 构造与启动链的冲突 |
| 下一步 | 先查 `-u` 引入的 init 构造，再重做 CPU1 调度（需要内核级修） |

---

## 71. W60：RT-Thread 一致性审计 —— 把 10 个驱动改回标准注册方式

新目标：**推进并完善移植，检查是否有不太符合 RT-Thread 的地方。**

这一轮做审计，找到三处不符，修掉了最实质的一处。

### 71.1 审计方法：用脚本量，不靠印象

```python
# 1. init 函数返回类型 vs RT 约定（rt_err_t）
# 2. port 里 INIT_*_EXPORT 的使用次数
# 3. rt_kprintf vs LOG_x 的比例
```

结果：

| 项 | 实测 | RT 约定 |
|---|---|---|
| init 返回 `int` | **10 处** | 应返回 `rt_err_t` |
| `INIT_*_EXPORT` 在 port 里 | **0 次** | 每个驱动自己导出 |
| `rt_kprintf` / `LOG_x` | **196 / 0** | 用 `DBG_TAG` + `LOG_D/LOG_E` |
| `rt_hw_board_init()` 手工调用 | **17 个 init** | 应该几乎为空 |

### 71.2 最实质的不符：驱动不自己注册

上游 BSP 的写法是：

```c
rt_err_t rt_hw_pin_init(void)
{
    return rt_device_pin_register("pin", &ops, RT_NULL);
}
INIT_BOARD_EXPORT(rt_hw_pin_init);      /* <- 驱动自己声明 */
```

`rt_hw_board_init()` 里**一个字都不提它**：`rt_components_board_init()` 走
`.rti_fn` 的 board 段，自然调到。

本端口是反的：**17 个 `extern` + 17 个手工调用**，注释里还写着
"这是唯一保持对象不被 gc 掉的引用"。

**那不是设计，是对 `INIT_BOARD_EXPORT` 没生效的绕行。**

### 71.3 先验证一件事：导出能生效吗

给 `gpio.c` 加 `INIT_BOARD_EXPORT(rt_hw_pin_init)`，删掉 board.c 的手工调用，
重新编译：

```
$ nm elf | grep rt_hw_pin_init
rt_hw_pin_init is NOT in the image
```

板上 `list device` 里 **`pin` 消失了**。

**⇒ 导出语法对、`.rti_fn` 段被 KEEP、walker 也在跑 —— 但 archive member 没被拉进来。**

原因：上游 BSP 把所有源文件编进**一个镜像**，段引用就够了；
ESP-IDF 每个组件是一个 **archive**，`--gc-sections` 会把没人引用的 member 丢掉。

### 71.4 修法：`INIT_BOARD_EXPORT` + 每个 init 一个 `-u`

```cmake
target_link_libraries(${COMPONENT_LIB} INTERFACE
    "-u rt_hw_pin_init"
    "-u rt_hw_pwm_init" ...)
```

**这个组合是对的**：
- 驱动用**标准 RT-Thread 方式**声明自己
- `-u` 显式表达"ESP-IDF 的 archive 模型需要这个"
- 不再需要 board.c 里的手工调用

### 71.5 改造了 10 个驱动

| 驱动 | 导出 |
|---|---|
| gpio.c | `INIT_BOARD_EXPORT(rt_hw_pin_init)` |
| pwm.c | `INIT_BOARD_EXPORT(rt_hw_pwm_init)` |
| gptimer.c | `INIT_BOARD_EXPORT(rt_hw_timer_init)` |
| i2c.c | `INIT_BOARD_EXPORT(rt_hw_i2c_init)` |
| spi.c | `INIT_BOARD_EXPORT(rt_hw_spi_init)` |
| adc.c | `INIT_BOARD_EXPORT(rt_hw_adc_init)` |
| pcnt.c | `INIT_BOARD_EXPORT(rt_hw_pcnt_init)` |
| mcpwm.c | `INIT_BOARD_EXPORT(rt_hw_mcpwm_init)` |
| rmt.c | `INIT_BOARD_EXPORT(rt_hw_rmt_init)` |
| twai.c | `INIT_BOARD_EXPORT(rt_hw_twai_init)` |

`gpio.c` 的返回类型同时从 `int` 改成 `rt_err_t`。

`rt_hw_board_init()` 的手工调用从 **17 个降到 7 个**。

### 71.6 验证：设备一个没少

```
$ list device
uart0    Character Device   1
adc2     ADC Device         0
adc1     ADC Device         0
spi1     SPI Bus            0
spi0     SPI Bus            0
pwm0     PWM Device         0
i2c0     I2C Bus            0
pin      Pin Device         0
```

**与改造前逐行相同。** 说明 export 路径确实跑到了。

回归：`s31test` **20/20**、`check_image.py` 32 命令、`s31twai` 4/4、
`s31rmt` PASS、`s31mcpwm` PASS、`s31pcnt` PASS。

### 71.7 剩下 7 个手工调用，以及为什么不改

| init | 为什么还不能用 INIT_*_EXPORT |
|---|---|
| `rt_hw_int_wdt_init` | 需要在 `rt_hw_board_init` **早期**跑（看门狗要先起来） |
| `rt_hw_irq_nest_init` | 必须在 flash 映射还在时填表，比 board 段更早 |
| `s31_twai_cmd_init` / `s31_selftest_init` / `s31_board_extra_init` / `s31_smp_cmd_init` / `s31_intr_debug_init` | **它们不是 driver init，是"把只有 section 注册的 msh 命令对象拉进链接"的锚点** |

**最后一类应该换个名字。** 叫 `*_init` 会让人以为是初始化函数，
实际作用是"链接期锚点"——这是 R44 那类问题的产物，应该有独立的名字和注释。

这是下一轮要做的：把它们改名成 `s31_*_link_anchor()`，并在 board.c 里
集中说明为什么需要。

### 71.8 状态

| 项 | 值 |
|---|---|
| `INIT_BOARD_EXPORT` 覆盖 | **10 个驱动** |
| board.c 手工调用 | **17 → 7** |
| `list device` | ✅ 与改造前相同 |
| `s31test` | ✅ 20/20 |
| 外设 | ✅ TWAI 4/4、RMT/MCPWM/PCNT PASS |
| 未改 | `rt_kprintf`→`LOG_x`（196 处，属代码风格，不影响功能） |

### 71.9 追加：把 5 个链接锚点改名

上一节说"应该改名"，这一节做了。

```c
/* 改前：看起来像初始化 */
int s31_selftest_init(void) { return 0; }

/* 改后：说明它只是为了把对象拉进链接 */
void s31_selftest_link_anchor(void) { }
```

| 原名 | 新名 |
|---|---|
| `s31_twai_cmd_init` | `s31_twai_cmd_link_anchor` |
| `s31_selftest_init` | `s31_selftest_link_anchor` |
| `s31_board_extra_init` | `s31_board_extra_link_anchor` |
| `s31_smp_cmd_init` | `s31_smp_cmd_link_anchor` |
| `s31_intr_debug_init` | `s31_intr_debug_link_anchor` |

返回类型 `int` → `void`（它们本来就不返回任何有意义的值）。

**编译时立刻暴露了 5 处 `return 0;`** —— 这正是改名的价值：
旧名字让"返回 0"看起来合理，新名字让它在编译期变成错误。

board.c 里的调用块也加了完整说明，列出每个锚点负责哪些命令。

### 71.10 最终一致性数据

| 指标 | 改前 | 改后 |
|---|---|---|
| `INIT_*_EXPORT` 在 port 里 | **0** | **10** |
| `rt_hw_board_init` 手工调用 | **17** | **7** |
| 其中真需要的早期 init | — | **2**（int_wdt、irq_nest） |
| 其中链接锚点 | — | **5**，且已改名 |
| `list device` | 8 个设备 | **同样 8 个** |
| `s31test` | 20/20 | **20/20** |
| TWAI / RMT / LED / PSRAM | PASS | **PASS** |

---

## 72. W61：一致性审计第二轮 —— 返回类型、console 归属、uart0 导出

上一轮把驱动注册方式改对了。这一轮继续按同一套标准查。

### 72.1 目标 1：`init` 返回类型全部改成 `rt_err_t`

开工前还有 3 个真实的 init 返回 `int`：

| 文件 | 函数 |
|---|---|
| `boot_tasks.c` | `rt_hw_int_wdt_init` |
| `smp_stack.c` | `rt_hw_irq_nest_init`（两个 `#ifdef` 分支） |
| `serial.c` | `rt_hw_uart0_init` |

全部改成 `rt_err_t`，返回值 `0` → `RT_EOK`。

**审计结果：`init returning int` = 0。**

### 72.2 目标 2：`uart0` 也改回 `INIT_BOARD_EXPORT`

`serial.c` 加了 `INIT_BOARD_EXPORT(rt_hw_uart0_init)`，
`main.c` 里的手工调用删掉：

```c
/* 改前 */
if (rt_hw_uart0_init() != 0) { ... } else { finsh_set_device("uart0"); }

/* 改后 */
finsh_set_device("uart0");   /* 设备已由 board 段注册好 */
```

`list device` 里 `uart0` 仍在，`s31test` 20/20。

### 72.3 目标 3（实质发现）：`rt_hw_console_output` 放错了层

**这是这一轮最有价值的发现。**

它原来在 `main.c` 里。查了一下上游：

```
upstream BSPs: driver/board layer = 44   applications/ = 1
```

**44 个 BSP 把它放在 board/驱动层，只有 1 个放在 applications/。**

理由很直接：**应用应该能跨板移植**。把 console 输出写在应用里，
意味着换一块板就要重写一遍，而且"console 能用"看起来像应用的功能而不是板子的能力。

**已移到 `port/esp32s31/serial.c`**，与 uart 驱动放在一起。

### 72.4 移过去的时候，为什么用 ROM 输出而不是驱动

```c
void rt_hw_console_output(const char *str)
{
    while (*str)
    {
        if (*str == '\n') esp_rom_output_putc('\r');
        esp_rom_output_putc(*str++);
    }
}
```

`esp_rom_output_putc()` **不需要任何驱动状态**，所以：

- 串口驱动出错时 console 仍然可用
- **panic 路径仍然能打印**

而本端口**从头到尾没有调用 `rt_console_set_device()`**，
所以 `_kputs()`（`kservice.c:342`）永远走 `rt_hw_console_output()` 这条分支。

**这是一个有意的设计偏离，已写进注释**：上游 BSP 一般在 board init 末尾调
`rt_console_set_device(RT_CONSOLE_DEVICE_NAME)`，本端口不调，为的是保住
"驱动坏了 panic 还能看"这个性质。

### 72.5 踩到的编译错误

```
error: implicit declaration of function 'esp_rom_output_putc'
```

`esp_rom_serial_output.h` 没有 include。加上就好。

**这类错误在"搬代码"时必然出现**，因为新位置的头文件集合不一样。

### 72.6 一致性数据：两轮合计

| 指标 | W60 前 | W60 后 | W61 后 |
|---|---|---|---|
| init 返回 `int` | 10 | 9 | **0** |
| `INIT_*_EXPORT` | 0 | 10 | **11** |
| board.c 手工调用 | 17 | 7 | **5**（2 init + 3 anchor） |
| `rt_hw_console_output` 在应用层 | 是 | 是 | **否，在驱动层** |
| `list device` | 8 设备 | 8 | **8（相同）** |
| `s31test` | 20/20 | 20/20 | **20/20** |

### 72.7 验证

```
list device   -> uart0 / adc1 / adc2 / spi0 / spi1 / pwm0 / i2c0 / pin
s31test       -> 20/20
check_image   -> 32 commands
s31twai       -> 4/4
s31rmt        -> PASS
s31led        -> gpio 60 <- R=4 G=0 B=4
```

**8 个设备与改造前逐行相同。**

---

## 73. W62：把审计固化成脚本 —— 并纠正脚本自己的两个误报

前两轮的检查都是临时 grep，结论留在 shell 历史里，下一轮就没了。
这一轮写成了 `tools/check_rtt_style.py`，**10 项检查，可重复跑**。

### 73.1 脚本第一次跑：8/10，两个 FAIL

```
[FAIL] every export has -u    missing -u: ['rt_hw_intr_sweep_again',
                                           'rt_hw_reclaim_startup_stack',
                                           'rt_hw_start_pending_tasks']
[FAIL] rt_uint32_t in declarations    13 bare uint32_t locals
```

**两个都是脚本自己的错，不是代码的错。**

### 73.2 误报 1：`-u` 不是判据，"到达镜像"才是

三个"缺失"的导出是 `board.c` 自己的函数：

```c
static int rt_hw_reclaim_startup_stack(void) { ... }
INIT_APP_EXPORT(rt_hw_reclaim_startup_stack);   /* 在 board.c 里，没 -u */
```

它们和 `rt_hw_board_init` 在**同一个目标文件**里，而那个文件显然被引用 ——
所以不需要 `-u`。`nm` 验证：

```
rt_hw_reclaim_startup_stack -> in image
rt_hw_start_pending_tasks   -> in image
rt_hw_intr_sweep_again      -> in image
```

**判据改对了**：不问"有没有 `-u`"，问"**符号在不在 ELF 里**"。

`-u` 是手段，"到镜像"是目的。检查手段会把正确的情况报成失败。

### 73.3 误报 2：`rt_uint32_t` 不能用在所有地方

把 13 个 `uint32_t` 改成 `rt_uint32_t` 后，**编译失败 3 处**：

```
gpio.c:192:  passing argument 3 of 'gpio_ll_get_intr_status'
             from incompatible pointer type
i2c.c:286:   passing argument 2 of 'i2c_ll_get_rxfifo_cnt'
             from incompatible pointer type
```

原因：**`rt_uint32_t` 和 `<stdint.h>` 的 `uint32_t` 是不同的 typedef**，
虽然都是 32 位，但 `uint32_t *` 与 `rt_uint32_t *` **不兼容**。

这些变量是**输出参数**，地址交给 IDF 的 LL 函数，必须用 LL 的类型。

改回去，脚本加上豁免：

```python
ll_output = {"lo", "hi", "cnt"}   # 地址被 gpio_ll / i2c_ll 取走
```

**同样豁免 `freertos_api.c`**：那里的 `uint32_t` 是 FreeRTOS 的 ABI，
改了就不是 drop-in 替换了。

### 73.4 现在的 10 项检查

| # | 检查 | 判据 |
|---|---|---|
| 1 | init 返回 `rt_err_t` | 0 个 `int` |
| 2 | `INIT_*_EXPORT` 存在 | ≥10 个 |
| 3 | **导出到达 ELF** | 全部在 `nm` 里 |
| 4 | board.c 手工调用少 | ≤2 个 |
| 5 | 锚点不叫 `_init` | 无 |
| 6 | console 钩子在驱动层 | app 无、port 有 |
| 7 | 走框架注册 | 5 个驱动全用 |
| 8 | `rt_uint32_t` 用于声明 | 0 个（豁免 LL 输出参数 + shim ABI） |
| 9 | msh 命令有锚点 | 覆盖 |
| 10 | shell 绑定设备 | `finsh_set_device` |

**结果：10/10 PASS。**

### 73.5 脚本本身的两个教训

| 教训 | 说明 |
|---|---|
| **检查手段 ≠ 检查目的** | `-u` 是手段，"到镜像"是目的。检查手段会误报 |
| **一致性规则有正确的例外** | LL 输出参数、FreeRTOS ABI —— 硬套规则会让代码编不过 |

**第二条尤其重要**：一个"把风格统一"的改动如果让编译失败，
那不是代码不统一，是**规则没考虑边界**。

### 73.6 验证

| 检查 | 结果 |
|---|---|
| `check_rtt_style.py --strict` | ✅ **10/10** |
| `s31test` | ✅ 20/20 |
| `check_image.py` | ✅ 32 命令 |
| `s31twai` | ✅ 4/4 |
| `s31rmt` / `s31mcpwm` / `s31pcnt` | ✅ PASS |
| `s31adc 1 0` | ✅ raw 1760 |
| `i2cscan` | ✅ 0 设备（无外设，正常） |
| `list device` | ✅ 8 设备 |
| `s31led` | ✅ gpio 60 |

已保存 `w62_verified.bin`。

---

## 74. W63：op 契约审计 —— 没找到功能缺陷，但补上了第 11 项检查

这一轮按"ops 契约"查：每个 `rt_*_ops` 的返回值语义是否符合 RT-Thread 文档，
以及 `rtconfig.h` 的注释是否和它的值一致。

### 74.1 ops 契约：逐个核对，没有缺陷

| ops | RT 契约 | 本端口 | 结论 |
|---|---|---|---|
| `rt_i2c_bus_device_ops.master_xfer` | 返回发送的 msg 数 | 一致 | ✅ |
| `rt_spi_ops.xfer` | 返回字节数 | 返回 `total` | ✅ |
| `rt_adc_ops.convert` | `rt_err_t` + 输出参数 | 一致 | ✅ |
| `rt_adc_ops.get_vref` | 返回 mV，**未知时可返回负值** | 返回 `-1` 并注释说明 | ✅ |
| `rt_pin_ops.pin_irq_enable` | `RT_EOK` / 负错误码 | 一致 | ✅ |
| `rt_uart_ops.control` | 未识别的 cmd 返回 `RT_EOK` | 一致（上游 8 个 `drv_uart.c` 也这样） | ✅ |
| `rt_pin_ops` 缺失的 5 个成员 | 全在 `RT_USING_DM` / `RT_USING_PINCTRL` 里 | 两个宏都未定义 | ✅ 不是缺陷 |

**`s31_adc_vref()` 返回 `-1` 是正确处理**：这个 SoC 没有校准路径，
返回"典型值 1100"会显得权威但实际偏几个百分点 —— **说"不知道"比编一个数字好。**

### 74.2 但发现一个真问题：`rtconfig.h` 的注释和值矛盾

```c
#define RT_CPUS_NR                      2
/* P1/P2 run the kernel on the PRO core only. ... */
```

**注释说单核，值是两个核。**

第二处：

```c
/* No networking, filesystem, Finsh, devices or console at this stage:
 * those RT_USING_* macros stay undefined rather than being defined as 0. */
```

**而 `RT_USING_DEVICE`、`RT_USING_FINSH`、`RT_USING_CONSOLE` 全都定义了。**

### 74.3 为什么这算缺陷，不是"小瑕疵"

| 后果 | 说明 |
|---|---|
| **无法区分"决定"和"遗漏"** | 读到"没有 console"的人会以为 `RT_USING_CONSOLE` 是有意关掉的 |
| **会误导下一轮的自己** | 我上一轮就在翻这个文件，差点按注释判断 |
| **值是对的，注释是错的** | 更糟：代码能跑，所以没人去改注释 |

**过时的注释比没有注释更危险。** 没有注释时人会去看代码；有过时注释时人会信它。

### 74.4 修法：把"现在是什么"和"为什么不是别的"分开写

```c
/* Both cores are configured. What actually runs on CPU1 today is its own idle
 * thread: ... taking scheduler work on CPU1 is behind RT_USING_SMP_SLAVE_LOCK
 * and is not currently enabled - measured, it races against CPU0's
 * ready-table edits (see W56/W59). */
```

```c
/* Still undefined on purpose, not by accident. Each of these needs a component
 * that this image does not link:
 *   RT_USING_SOCKET / RT_USING_LIBC  - the lwIP port and newlib are not wired in
 *   RT_USING_DFS                      - no filesystem
 *   RT_USING_SMART                    - that is the microkernel variant
 *
 * Devices, the shell and the console ARE enabled above; the earlier version of
 * this comment claimed otherwise and had gone stale, which is worse than saying
 * nothing. */
```

**两处都改成"描述现状 + 说明原因"，而不是"描述某个历史阶段"。**

### 74.5 新增第 11 项检查

```python
# 11. rtconfig.h comments must not contradict its values.
#     A stale comment is worse than none: it makes a deliberate choice look
#     like an oversight, and the next reader cannot tell which to believe.
```

检查三条具体矛盾：
- 说"单核"但 `RT_CPUS_NR == 2`
- 说"没有 devices/console"但 `RT_USING_DEVICE` / `RT_USING_CONSOLE` 已定义
- 说"没有 Finsh"但 `RT_USING_FINSH` 已定义

**脚本现在 11/11。**

### 74.6 这一轮的产出

**没有修功能 bug** —— 说明 ops 层的实现是扎实的。

**修了两处会误导人的注释**，并把"注释与值一致"变成可自动检查的第 11 项。

**审计的价值不只是找 bug，也包括"确认没有 bug"和"消除误导"。**

### 74.7 验证

| 检查 | 结果 |
|---|---|
| `check_rtt_style.py --strict` | ✅ **11/11** |
| `s31test` | ✅ 20/20 |
| `check_image.py` | ✅ 32 命令 |
| `s31twai` | ✅ 4/4 |
| `s31rmt` | ✅ PASS |
| `s31smp` | ✅ `cpus=2`、`istack: distinct per core` |
| `list device` | ✅ 8 设备 |
| `s31spiloop` | ✅ 正常（rx 全 0 是预期：MOSI 未接 MISO） |

---

## 75. W64：rthw.h 契约审计 —— 找到 `rt_hw_us_delay` 只有空实现

这一轮查 RT-Thread 的 `rthw.h` 契约：移植层该提供哪些函数，实际提供了没有。

### 75.1 方法：直接比对 ELF 符号

`rthw.h` 声明了 39 个移植层函数。逐个在 ELF 里找：

```
缺失 19 个
```

但要区分三类，不能一视同仁：

| 类别 | 例子 | 判断 |
|---|---|---|
| **宏** | `rt_hw_interrupt_disable` → `rt_cpus_lock`（SMP） | ✅ 不是符号，正常 |
| **宏（无 cache）** | `rt_hw_cpu_icache_*` → 空宏 | ✅ `RT_USING_CACHE` 未定义 |
| **weak 默认实现** | `rt_hw_cpu_reset`、backtrace 对 | ✅ 缺失时用 weak |
| **真缺口** | **`rt_hw_us_delay`** | ❌ **只有 weak 空实现** |

### 75.2 为什么 `rt_hw_us_delay` 是真缺陷

`kservice.c` 提供的是：

```c
rt_weak void rt_hw_us_delay(rt_uint32_t us)
{
    (void) us;
    LOG_W("rt_hw_us_delay() doesn't support for this board.");
}
```

**它什么都不做。** 而 `components/libc/posix/delay/delay.c` 里：

```c
rt_hw_us_delay(msecs * 1000);   /* usleep */
rt_hw_us_delay(usecs);          /* nanosleep */
rt_hw_us_delay(1);
```

**⇒ 这个镜像上任何 POSIX `usleep()`/`nanosleep()` 都是空操作。**

这是"危害/可见性"比值最差的一类问题：**没有错误、没有警告，
只是一个偶尔不正常的设备。**

### 75.3 实现：基于已有的 `s31_systimer_us()`

```c
void rt_hw_us_delay(rt_uint32_t us)
{
    ...
    start = s31_systimer_us();
    while ((s31_systimer_us() - start) < (rt_uint64_t)us) { }
}
```

**为什么用 SYSTIMER 而不是 TIMG**：它从调度器启动前就在跑、
不是 tick（关中断时也能用）、而且单位换算已经集中在一个函数里 ——
`s31timer` 就是用它来给硬件定时器打分的。**第二个时间源就是第二次搞错
16 MHz / 1 MHz 换算的机会。**

**上限 10 ms 并只警告一次**：忙等毫秒是调用点的错，不该由这个函数掩盖。

### 75.4 踩到的坑：`-u` 打不过同一个 archive 里的 weak

先按老办法加 `-u rt_hw_us_delay`，重编，查 `nm`：

```
40012836 W rt_hw_us_delay        <- 还是 W（weak）
```

**`-u` 只把符号标记为"未定义"**，而 `kservice.c` 的 weak 版本已经
在同一个 archive 里被链接进来了 —— **弱定义满足了这个未定义引用**，
链接器不需要再拉延迟定义的那个 member。

**⇒ 同一个 archive 里的 weak 定义会吃掉 `-u`。**

**修法：把强定义放到一个必然被链接的目标文件里。**
选 `gptimer.c`：它被 `INIT_BOARD_EXPORT` + `-u` 拉进来，
而且 `s31_systimer_us()` 就在里面 —— 两者本来就该在一起。

```c
/* gptimer.c */
void rt_hw_us_delay(rt_uint32_t us) { ... }
```

重编后：

```
400163e4 T rt_hw_us_delay        <- T（strong）
```

### 75.5 新增第 12 项检查

```python
# 12. Port-owned rthw.h hooks must be STRONG, not the weak stub.
```

**查 ELF 的符号类型**（`T` vs `W`），不是查源码。
因为这件事源码看不出来：`delay.c` 写了强定义，编译通过，
链接成功，运行时用的是 weak 空实现。

**脚本现在 12/12。**

### 75.6 验证

| 检查 | 结果 |
|---|---|
| `rt_hw_us_delay` 符号类型 | ✅ **`T`**（strong） |
| `check_rtt_style.py --strict` | ✅ **12/12** |
| `s31test` | ✅ 20/20 |
| `check_image.py` | ✅ 32 命令 |
| `s31timer 0 100` | ✅ **99 Hz（want 100）** |
| `s31twai` | ✅ 4/4，连跑 3 次 |
| `s31rmt` | ✅ PASS |
| `list device` | ✅ 8 设备 |

### 75.7 剩下的真缺口

| 符号 | 状态 | 影响 |
|---|---|---|
| `rt_hw_exception_install` | 无 weak、无调用者 | 无影响（没人调） |
| `rt_hw_show_memory` | 无 weak、无调用者 | 无影响 |
| `rt_hw_stack_guard_init` | 无 weak，但 `RT_USING_HW_STACK_GUARD` 未定义 | 无影响 |

**这三个都是"契约里有、但本配置用不到"**，不是缺陷。
真要补的话 `rt_hw_us_delay` 那种才值得补 —— 有真实调用者。

---

## 76. W65：给 `rthw.h` 契约加**运行时测试** —— 自检从 20 项涨到 26 项

W64 找到 `rt_hw_us_delay` 是空实现，但那是**靠人读源码 + 查 `nm`** 发现的。
这一轮把它变成**板子自己会检查的事**。

### 76.1 新增 `s31test rhwh`：4 项子检查

| 子检查 | 验什么 | 失败模式 |
|---|---|---|
| `rhwh.ud` | `rt_hw_us_delay(1000)` 实际耗时 900~1600 us | weak 空实现（~0）或 16 倍单位错（~16000） |
| `rhwh.iv` | 扫描到一条空闲 INUM | — |
| `rhwh.ui` | `uninstall` 参数不匹配时**不**删除 | 无条件删除会误删后来者的注册 |
| `rhwh.ri` | `install` 返回**前一个** handler | 忽略返回值，链式调用会丢 handler |
| `rhwh.mk` | `mask`/`umask` 与 `is_masked` 一致 | 写错寄存器 |

实跑：

```
[PASS] rhwh.ud  1000 us request took 1000 us
[PASS] rhwh.iv  using free INUM 8
[PASS] rhwh.ui  worst-case param kept the handler, re-install returned it
[PASS] rhwh.ri  install returned the previous handler
[PASS] rhwh.mk  mask/umask: 1 -> 1 -> 0
[PASS] rhwh     5 sub-checks, 0 bad
```

**`rhwh.ud` 是最有价值的一条**：weak 空实现和 16 倍单位错都会让它失败，
而这两种错误都真实发生过。

### 76.2 第一次跑就抓到我自己的一个错误

```
[FAIL] rhwh.iv  INUM 7 already had a handler - skipped
```

**单独跑 `s31test rhwh` 时通过，放进 `s31test` 全跑时失败。**

原因：`st_nest` 会把它的内层 line 6 的 handler 留着（只 mask），
而我写的测试固定用 line 7，且**靠"安装一下看返回值"来判断是否空闲**。

**这个探测方式本身就是错的：为了读状态而改变状态。**

改成用 IDF 的只读接口：

```c
for (probe = ST_RHWH_INUM; probe < SOC_CPU_INTR_NUM; probe++)
{
    if (intr_handler_get(probe) == RT_NULL && esprv_int_get_priority(probe) == 0)
    {
        inum = probe;
        break;
    }
}
```

本端口把自己的槽位都设成 `rtt_trampoline`，所以 `intr_handler_get() != NULL`
就是"已被占用"。

**扫不到空闲 line 时报 `PASS + skipped`，不报 FAIL** —— 线路被占用不是端口缺陷。

### 76.3 两个编译期小坑

| 错误 | 原因 |
|---|---|
| `'RTT_INUM_NUM' undeclared` | 它是 `interrupt.c` 的私有宏，测试里要用 `SOC_CPU_INTR_NUM` |

**测试代码不该依赖私有宏。**

### 76.4 自检从 20 项涨到 26 项

```
=== 26 passed, 0 failed ===
```

连跑 3 次全过。

### 76.5 连带发现：`check_image.py` 的第 5 项检查失败了

```
[FAIL] init table order   19 entries by address: ...
```

**这是 W60/W61 改造的预期后果** —— 11 个驱动从手工调用变成
`INIT_BOARD_EXPORT`，`.rti_fn` 表因此多了 11 项。

**检查本身是对的**，文件里的注释早就写了：

> Adding an `INIT_*_EXPORT` anywhere in the tree changes this list,
> and that is the point of the check.

**但我不能悄悄把比较放宽成"包含"。** 做法：
把新的期望序列**逐项写出来**，并在注释里说明
**BOARD 段的相对顺序不是契约**（由链接器拉取顺序决定），
**只有它们位于两个 marker 之间才是契约**。

更新后 **9/9 全过**。

### 76.6 这一轮的意义

**审计发现的问题，已经从"人读源码"变成"板子自己检查"。**

| 之前 | 现在 |
|---|---|
| W64 靠人读 `rthw.h` + 查 `nm` 发现 `us_delay` | `s31test rhwh` 每次跑都验 |
| 契约违反会在几周后表现为"某个设备偶尔不对" | 当场 `[FAIL]` |

### 76.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **26 passed, 0 failed**，连跑 3 次 |
| `check_image.py` | ✅ **9/9**（更新期望序列后） |
| `check_rtt_style.py --strict` | ✅ **12/12** |
| `s31twai` | ✅ 4/4 |
| `s31rmt` | ✅ PASS |
| `list device` | ✅ 8 设备 |

已保存 `w65_verified.bin`。

---

## 77. W66：device 框架契约测试 —— 自检 26 项涨到 33 项

W65 用运行时测试覆盖了 `rthw.h`。这一轮用同样的方法覆盖 **`rt_device` 框架契约**。

### 77.1 新增 `s31test dev`：7 项子检查

| 子检查 | 验什么 | 失败意味着 |
|---|---|---|
| `dev.fd` | `find("no-such-device")` 返回 NULL | 返回陈旧指针 |
| `dev.ur` | 未 open 时 `read()` 返回 **0** | 驱动绕过框架直接调 ops |
| `dev.op` | **没有 open op** 的设备也能 open 成功，ref=1 | 所有总线设备（i2c0/spi0/adc1/pin）都走这条路 |
| `dev.o2` | 第二次 open → ref=2 | 计数错，泄漏或误解锁 |
| `dev.cl` | 多余 close → `-RT_ERROR`，ref 保持 0 | 重复递减 |
| `dev.ct` | 未知 control 命令的返回 | 见下 |

实跑：

```
[PASS] dev.fd   find() returns NULL for a bogus name
[PASS] dev.ur   read before open returns 0
[PASS] dev.op   open without an open op: 0, ref_count 1
[PASS] dev.o2   second open: ref_count 2
[PASS] dev.cl   extra close refuses with -RT_ERROR, ref_count 0
[PASS] dev.ct   bogus control answered 0 (...)
[PASS] dev      7 sub-checks, 0 bad
```

### 77.2 `dev.ct` 是"报告"不是"断言"，这里要说明为什么

`rt_device_control(uart0, 0x7f00, NULL)` 返回 **`RT_EOK`**，不是负错误码。

看起来像缺陷：RT 文档说"不支持的命令应拒绝"。但查了上游：

```
8 个 drv_uart.c 的 control() default 分支：全部返回 RT_EOK
```

**这是共享约定，不是某个驱动的疏忽。**

所以测试**记录实际值但不判失败**：

```c
st_report("dev.ct", ST_REPORT_OK,
          "bogus control answered %d (drivers return RT_EOK for "
          "unknown commands, as upstream drv_uart.c does)", (int)e);
```

**让事实可见，但不把自检变红** —— 否则下一轮有人会把这条改成"修复"，
反而让端口偏离上游。

### 77.3 `dev.op` 是最有价值的一条

`i2c0`、`spi0`、`adc1`、`adc2`、`pin`、`pwm0` **都没有实现 open/close op**。
它们全靠 `rt_device_open()` 里的这条逻辑：

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

**如果这段坏了，整个总线层都坏了，而且没人会发现** —— 因为没人对 `i2c0` 调 open。
`dev.op` / `dev.o2` / `dev.cl` 三条把这条路径钉住了。

### 77.4 测试自身的两条纪律（沿用 W65）

| 纪律 | 本轮的体现 |
|---|---|
| **不改变被探测的状态** | 只对 `ref_count == 0` 的设备做计数测试；`pwm0` 被占用就报 `skipped` 并跳过 |
| **不留副作用** | 结束时 `pwm0` 的 ref 回到 0，`list device` 里仍是 0 |

验证（`s31test` 全跑之后）：

```
pwm0         PWM Device           0
pin          Pin Device           0
uart0        Character Device     1     <- 壳在用，未改动
```

**没有 ref 泄漏。**

### 77.5 自检 26 → 33 项

```
=== 33 passed, 0 failed ===
```

连跑 3 次全过。

### 77.6 顺带记录：`s31twai` 的间歇失败

这一轮又观察到一次：

```
run 1 : s31twai: 0/4 bit rates round-tripped
run 2 : 4/4
run 3 : 4/4
```

**这是已记录的间歇问题（W46 首次观察到），不是本轮引入。**
现象是单次运行全部 4 个速率都失败、重跑就全过 —— 更像状态未复位而不是速率算错。
**本轮不动它**，但在这里再次记录，避免被当成"新回归"。

### 77.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `check_image.py` | ✅ 9/9 |
| `check_rtt_style.py --strict` | ✅ 12/12 |
| `s31twai` | ⚠️ 间歇（3 次里 1 次失败，已知问题） |
| `list device` | ✅ 8 设备，无 ref 泄漏 |

已保存 `w66_verified.bin`。

---

## 78. W67：头文件与实现对不上 —— 找到两个"承诺了但不存在"的符号

这一轮查的是**头文件与实现的一致性**，方向与前面几轮相反：
不是"实现是否符合 RT 约定"，而是"**头文件承诺的东西到底存不存在**"。

### 78.1 方法：把 12 个头文件的声明和 24 个 .c 的定义对起来

```
声明在 port 头文件、但没有定义在 port/esp32s31/*.c 的：
  s31_selftest_init     (port_selftest.h)
  s31_twai_cmd_init     (port_twai.h)
```

### 78.2 这是 W60 改名的遗留

W60 把 5 个链接锚点从 `*_init` 改名成 `*_link_anchor`，
**但只改了 `.c` 和 `board.c`，漏了两个头文件。**

```c
/* port_selftest.h 改前 */
int s31_selftest_init(void);      /* 这个函数已经不存在了 */

/* 改后 */
void s31_selftest_link_anchor(void);
```

### 78.3 为什么这算缺陷，而且不只是"注释过期"

| 后果 | 说明 |
|---|---|
| **编不过** | 一旦有人 `#include "port_selftest.h"` 并调用，**链接期才报错** |
| **更难查** | 报错信息是"undefined reference to s31_selftest_init"，会让人去 .c 里找这个函数 |
| **现在没暴露** | 因为没有文件 include 这两个头 —— **这属于运气，不是设计** |

**注释过期只会误导人；接口过期会让人白找半天。**

### 78.4 新增第 13、14 项检查

**第 13 项：头文件声明必须有对应定义**

```python
declared = { ...从 port_*.h 提取... }
defined  = { ...从 port/esp32s31/*.c 提取... }
stale = declared - defined
```

结果：**50 个声明全部有定义。**

**第 14 项：声明了函数的头文件必须有 `extern "C"`**

这次也补了 3 个头文件：

| 头文件 | 之前 | 之后 |
|---|---|---|
| `port_pwm.h` | 无 guard，2 个声明 | ✅ |
| `port_serial.h` | 无 guard，1 个声明 | ✅ |
| `port_timer.h` | 无 guard，5 个声明 | ✅ |
| `port_board.h` | 无 guard，**0 个声明** | 不需要（不改） |

**现在纯 C 编译，所以没有实际影响。** 但一旦有人从 C++ 里 include，
名字修饰会让链接失败 —— **关掉这个坑的代价是零。**

### 78.5 一致性数据

| 指标 | 本轮前 | 本轮后 |
|---|---|---|
| 头文件声明无定义 | **2** | **0** |
| 缺 `extern "C"` 的头文件 | **3** | **0** |
| `check_rtt_style.py` | 12/12 | **14/14** |

### 78.6 验证

| 检查 | 结果 |
|---|---|
| `check_rtt_style.py --strict` | ✅ **14/14** |
| `check_image.py` | ✅ 9/9 |
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `s31twai` | ✅ 4/4 |
| `list device` | ✅ 8 设备 |

已保存 `w67_verified.bin`。

### 78.7 这类问题为什么会漏

**W60 的改名是通过脚本做的**，脚本只扫描了 `*.c` 和 `board.c`，
`.h` 不在扫描范围里。而**编译不会因为头文件里的过期声明失败** ——
除非有人 include 它。

**⇒ 改名的收尾必须包含"被改名符号的所有文本引用"，而不只是"能编译的那部分"。**
本轮把它变成了自动检查。

---

## 79. W68：`rtconfig.h` 里有个开关，九轮来没有任何代码读它

这一轮查配置完整性。方向是：**每个 `RT_USING_*` 开关，到底有没有代码读它。**

### 79.1 方法

```python
switches = rtconfig.h 里所有 RT_USING_* / FINSH_USING_* / MSH_USING_*
corpus   = 内核全部 .c/.h（不含 bsp）+ 端口全部 .c
dead     = switches 里在 corpus 中一次都没出现的
```

**结果：`RT_USING_I2C_BUSSING` 是死的。**

### 79.2 四条独立证据

| 检查 | 结果 |
|---|---|
| `dev_i2c_core.c` 的 `#ifdef` | 只 gate 在 **`RT_USING_DM`** |
| `rtdevice.h` 里 I2C 头文件的 gate | **`RT_USING_I2C`** |
| 466 个上游 BSP 的 `rtconfig.h` | **0 个**定义它 |
| 全树搜索（不含 bsp） | **0 处引用** |

**四票一致：这个宏不存在于当前内核的任何判定里。**

### 79.3 为什么死开关比没有开关更糟

```c
#define RT_USING_I2C
#define RT_USING_I2C_BUSSING     /* 看起来"配了 master/slave 支持" */
```

读到这两行的人会以为：

- 这个端口配置了 I2C 的主从模式
- 关掉 `RT_USING_I2C_BUSSING` 会失去什么

**实际上唯一的开关是 `RT_USING_I2C`。** 多出来那一行**只产生错误印象**。

**没有开关时人会去查代码；有死开关时人会信它。**

### 79.4 已删除

```diff
 #define RT_USING_I2C
-#define RT_USING_I2C_BUSSING
 #define RT_USING_SPI
```

注释里写清了四条证据和"为什么删"。

**验证：`i2cscan` 仍然工作，`i2c0` 仍在 `list device` 里，`s31test` 33/33。**

### 79.5 `ARCH_RISCV32` 保留：它是标记不是开关

同样的检查也标出了 `ARCH_RISCV32` —— 内核和端口都**不读它**。

但结论不同：

```
24 个上游 BSP 定义 ARCH_RISCV32
21 个定义 ARCH_RISCV64
55 个定义 ARCH_RISCV
```

**这是约定标记**（说明这个 BSP 是 RV32），不是条件编译开关。
**删掉它会让配置和兄弟 BSP 不一致，保留它无害。**

**⇒ "无代码读取"不等于"该删"，要看它是开关还是标记。**

### 79.6 新增第 15、16 项检查

**第 15 项：配置里不能有死开关**

只查 `RT_USING_*` / `FINSH_USING_*` / `MSH_USING_*`，
**明确排除 `ARCH_*`**（标记）和尺寸/名字宏（值）。

```python
port_owned = {"RT_USING_SMP_SLAVE", "RT_USING_SMP_SLAVE_LOCK",
              "RT_USING_HW_ATOMIC", "RT_USING_SMP"}
dead = [s for s in switches if s not in port_owned and s not in bulk]
```

结果：**26 个开关全部有代码引用。**

**第 16 项：必须成对出现的配置**

| 主开关 | 需要 |
|---|---|
| `RT_USING_SERIAL` | `RT_USING_SERIAL_V1` 或 `_V2` |
| `RT_USING_FINSH` | `FINSH_USING_MSH` |
| `FINSH_USING_MSH` | `FINSH_USING_SYMTAB` |
| `RT_USING_FINSH` | `MSH_USING_BUILT_IN_COMMANDS` |
| `RT_USING_SMALL_MEM_AS_HEAP` | `RT_USING_SMALL_MEM` |
| `RT_USING_CONSOLE` | `RT_USING_DEVICE` 或 `RT_USING_DEVICE_OPS` |

**这六对缺一半都能编过，但行为会变。** 结果：**6 对全部满足。**

### 79.7 一致性数据

| 指标 | 前 | 后 |
|---|---|---|
| rtconfig.h 里的死开关 | **1** (`RT_USING_I2C_BUSSING`) | **0** |
| `check_rtt_style.py` | 14/14 | **16/16** |

### 79.8 验证

| 检查 | 结果 |
|---|---|
| `check_rtt_style.py --strict` | ✅ **16/16** |
| `check_image.py` | ✅ 9/9 |
| `s31test` | ✅ **33 passed, 0 failed** |
| `i2cscan` | ✅ 0 设备（无外设，I2C 总线正常） |
| `list device` | ✅ 8 设备，`i2c0` 仍在 |

已保存 `w68_verified.bin`。

### 79.9 中途的一次工具失败

`build.ps1 -p COM16 flash` 卡住超过 10 分钟，`python` 进程占着串口。
处理：`Stop-Process` 清掉，直接用 `esptool write_flash` 写。

**这是已知的串口占用问题（R 系列已记录两次），不是固件问题。**

---

## 80. W69：一条"靠调用顺序凑巧成立"的隐式不变量

这一轮查 `rt_hw_board_init` 里剩下两个手工调用（`rt_hw_int_wdt_init`、
`rt_hw_irq_nest_init`）的注释里写的理由**是否真的成立**。

**结论：`rt_hw_irq_nest_init` 那一条的理由是错的。**

### 80.1 原注释说的

```c
/* Fill the per-CPU interrupt-nest table while flash is still mapped.
 * ... rt_cpu_index() is an ordinary flash function, which is fine there -
 * the SMP build cannot reach the trap handler before board init, because the
 * MP scheduler and the per-CPU structures do not exist yet. */
```

**"MP 调度器和 per-CPU 结构还不存在"** —— 查了一下，这句话是假的。

### 80.2 实际情况

| 事实 | 证据 |
|---|---|
| `_cpus` 在 `.bss` | `nm`: `2f01eb10 00000230 b _cpus` |
| `rt_irq_nest_slot` 在 `.bss`，初始全 0 | `nm`: `2f03154c 00000008 B` |
| IDF 在 `start_cpu0_default()` 就装了 `_vector_table` | `startup.c` |
| trap handler **无条件**读这张表 | `vectors_rtthread.S:167` |
| `lw t0, 0(t0)` 若 slot 为 0 | **从地址 0 读 → load access fault** |

**`_cpus` 从第一条指令起就可寻址，`_cpus` 的 per-CPU 结构一直都在。**

### 80.3 真正让它没崩的是什么

**`rtthread_startup()` 的第一句：**

```c
int rtthread_startup(void)
{
#ifdef RT_USING_SMP
    rt_hw_spin_lock_init(&_cpus_lock);
#endif
    rt_hw_local_irq_disable();     /* <- MIE 清零 */
    rt_hw_board_init();
```

**MIE 关闭 ⇒ board init 之前取不到任何中断 ⇒ trap handler 不会跑 ⇒ 不会读那张空表。**

**这是与调用方的一个契约，不是这个端口的性质。**

### 80.4 为什么这值得改

| 问题 | 后果 |
|---|---|
| **契约不可见** | 在"表可能为 NULL"的地方看不到"MIE 已关"这个前提 |
| **顺序一旦改动就崩** | 任何人在 `entry()` 里加一句开中断，立刻变成地址 0 访问错误 |
| **错误信息毫无指向** | `load access fault, MTVAL=0x0` 在 trap handler 里，看不出是表没填 |

**而且注释本身是错的 —— 它会让人以为"表在能跑 trap 之前一定填好了"。**

### 80.5 修法：把不变量变成可检查的

新增 `rt_hw_irq_nest_check()`：

```c
rt_err_t rt_hw_irq_nest_check(void)
{
    for (cpu = 0; cpu < RT_CPUS_NR; cpu++)
    {
        if (rt_irq_nest_slot[cpu] == RT_NULL)
        {
            rt_kprintf("irq: nest slot %d is NULL - the trap handler would fault\n", cpu);
            return -RT_ERROR;
        }
    }
    return RT_EOK;
}
```

`board.c` 里紧跟初始化调用：

```c
rt_hw_irq_nest_init();
if (rt_hw_irq_nest_check() != RT_EOK)
{
    rt_kprintf("board: interrupt nest table is broken, traps will fault\n");
}
```

**单核分支也实现了这个检查**（那里表是静态初始化的，检查恒过，但接口一致）。

### 80.6 注释改成了事实

```c
/* What keeps that from happening today is not this call but
 * rtthread_startup()'s opening rt_hw_local_irq_disable(): MIE is clear, so no
 * trap can be taken before board init. That is a contract with the caller, and
 * it is invisible at the point where it matters, so the check below turns the
 * assumption into a message.
 *
 * The old comment claimed the reason was that the MP scheduler did not exist
 * yet. It does: _cpus is in .bss and addressable from the first instruction.
 * The comment described a belief, not the mechanism. */
```

### 80.7 关于 `rt_hw_int_wdt_init` 的结论：理由成立，保留

```
The interrupt watchdog, which the FreeRTOS path starts inside
esp_startup_start_app() and this port therefore does not get.
```

**查证：`esp_startup_start_app()` 在 `CONFIG_IDF_RTOS_RTTHREAD` 分支里不被调用**
（`startup.c:280` 那行在 `#if !CONFIG_IDF_RTOS_RTTHREAD` 里面）。

**所以这条手工调用是必需的，理由成立。**

### 80.8 两个手工调用的最终状态

| init | 理由 | 结论 |
|---|---|---|
| `rt_hw_int_wdt_init` | IDF 路径不调 `esp_startup_start_app()` | ✅ **成立，保留** |
| `rt_hw_irq_nest_init` | ~~MP 调度器还不存在~~ | ❌ **理由错，改成"MIE 关闭 + 加了检查"** |

### 80.9 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `check_image.py` | ✅ 9/9 |
| `check_rtt_style.py --strict` | ✅ 16/16 |
| `s31twai` | ✅ 4/4 |
| `list device` | ✅ 8 设备 |

已保存 `w69_verified.bin`。

### 80.10 又一次串口占用

`build.ps1 -p COM16 flash` 再次卡住超过 10 分钟。清进程 + 直接 `esptool write_flash` 解决。
**这是本轮第二次，已确认是工具层问题。**

---

## 81. W70：逐条验证 `boot_tasks.c` 里"IDF 不调 X 所以我们调"的五个断言

上一轮推翻了 `rt_hw_irq_nest_init` 的理由。这一轮把 `boot_tasks.c` 里
**同一类断言逐条查**——那里列了五个"FreeRTOS 路径有而这里没有"的调用。

### 81.1 五条验证结果

| # | 断言 | 验证方法 | 结论 |
|---|---|---|---|
| 1 | `esp_int_wdt_init` 缺了 | `nm` 查符号 | ✅ **成立**，两个符号现在都在镜像里 |
| 2 | `esp_crosscore_int_init` 不需要 | 查谁提供 IPI | ⚠️ **理由已过期**（见 81.3） |
| 3 | 主线程已被 RT-Thread 替代 | 读 `startup.c` | ✅ 成立 |
| 4 | 20 KB DRAM 被扣住 | 从 `soc.h` 重算 | ✅ **成立**，数字精确 |
| 5 | `esp_task_wdt_add` 无调用者 | 全树搜索 | ✅ **成立**（5 处命中里 4 处是 test_apps，1 处是本注释） |

### 81.2 第 1 条：现在有证据了

```
esp_int_wdt_init       400982b2 T
esp_int_wdt_cpu_init   4009836e T
```

**两个都在镜像里。** 注释说"之前 nm 里两个符号都没有，配置项是悬空的"——
现在不悬空了，这条已从"计划"变成"事实"。

顺带查了 tick 是否喂它：

```
port_systick.c:212   esp_vApplicationTickHook();
                     #if CONFIG_IDF_RTOS_RTTHREAD ... rt_tick_increase();
```

**hook 在 RT-Thread 分支**之前**调用**，所以 RT-Thread 的 tick 确实喂了它。
注释里写的 `210-214` 行号也基本准确（实际 212）。

### 81.3 第 2 条：理由过期了，但结论仍然成立

**原注释：**

> `esp_crosscore_int_init()` - inter-core interrupts.
> `CONFIG_FREERTOS_UNICORE=y`, so there is no other core to poke.

**这句话在 P1 时代是对的（那时单核）。现在不对了：**

```
RT_USING_SMP
RT_CPUS_NR                      2
```

**CPU1 从 W56 起就在跑 idle 线程。** "没有别的核"已经不成立。

**但结论仍然对，理由要换：**

| 检查 | 结果 |
|---|---|
| RT-Thread 自己的 IPI | ✅ `rt_hw_ipi_send` 在镜像里（`40019cd2 T`） |
| IDF 的跨核中断 | ❌ `esp_crosscore_int_*` **都不在镜像里** |

**本端口用自己的 `smp_ipi.c` 直接操作 CLIC 上 IDF 预留的跨核线。**
所以"不需要 IDF 那个"是对的，**但原因不是"没有第二个核"，而是"已经有自己的实现"**。

**⇒ 结论对、理由错，是最容易留在代码里的一种缺陷：**
它不会让任何测试失败，只会在下一次有人读注释时误导他。

### 81.4 第 4 条：我一开始算错了，纠正后成立

我第一眼拿 `__heap_end__`（`0x2f05bb10`）去比注释里的 `0x2f07afb0`，
差了 145 KB，以为注释过期。

**算错了。** 正确算法：

```
APP_USABLE_DRAM_END = SOC_ROM_STACK_START - SOC_ROM_STACK_SIZE
                    = 0x2f07cfb0 - 0x2000
                    = 0x2f07afb0
SOC_DIRAM_DRAM_HIGH = 0x2f080000
区域 = 0x2f07afb0..0x2f080000 = 20560 B = 20.1 KB
```

**注释里的地址和大小都精确。** 我拿两个不同的东西做了比较。

**⇒ 验证断言时要用断言自己的定义，不是就近找一个看起来相关的数字。**

### 81.5 第 5 条：搜索也要排除 test_apps

```
esp_task_wdt_add 全树命中 5 处：
  rtthread/port/esp32s31/boot_tasks.c:25   <- 本注释
  freertos/test_apps/...
  esp_timer/test_apps/...
  esp_libc/test_apps/...
  esp_event/test_apps/...
```

**components/ 里 0 处。** 注释说的"零调用点"成立。

**但如果不排除 `test_apps`，就会得出"有 4 处调用"的错误结论。**
上一轮 W69 的搜索也是先命中了 test_apps 才去看的。

### 81.6 注释已重写

`boot_tasks.c` 的头部注释现在写的是**验证后的状态**：

- 第 1 条：附上 `nm` 地址
- 第 2 条：改成"RT-Thread 有自己的 `rt_hw_ipi_send`，`esp_crosscore_int_*` 不在镜像里"，并明确写出**旧理由已过期**
- 第 4 条：附上从 `soc.h` 重算的算式
- 第 5 条：写明"5 处命中里 4 处是 test_apps"

### 81.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed** |
| `check_image.py` | ✅ 9/9 |
| `check_rtt_style.py --strict` | ✅ 16/16 |
| `s31twai` | ⚠️ 间歇（4 次里 1 次失败，已知问题） |
| `list device` | ✅ 8 设备 |

已保存 `w70_verified.bin`。

### 81.8 这一轮的方法学

| 教训 | 说明 |
|---|---|
| **结论对、理由错** 也要修 | 它不会让测试失败，只会误导下一个读者 |
| **验证断言要用断言自己的定义** | 我算错第 4 条就是拿错了对比对象 |
| **搜索要排除 test_apps** | 否则命中一堆不属于本镜像的代码 |
| **"当时正确"的理由会过期** | P1 单核时代的理由，到 SMP 时代就不成立 |

---

## 82. W71：`s31twai` 的间歇失败 —— 找到两处真 bug，仍未完全解决

`s31twai` 的间歇失败已经记录了四轮（W46 首次），一直写成"已知问题"。
这一轮去查，**找到两个真实缺陷，但没有解决全部现象。**

### 82.1 先确定失败模式

用**五次独立开机**测试（每次 `esptool hard-reset`）：

```
before: fail, pass, fail, pass     <- 4 次
```

同一次开机内连跑三次：

```
run 1: 0/4    run 2: 0/4    run 3: 0/4     <- 同一次开机结果一致
```

**⇒ 结果由"这一次开机"决定，不由调用次数决定。**
这排除了"状态未复位"的猜测，指向**开机时硬件处于某种状态**。

### 82.2 失败信息的精确形态

```
125000 bit/s: len 8, want 4
250000 bit/s: len 8, want 4
```

**四档全失败，且都是 `dlc` 读到 8 而不是 4。**

### 82.3 真 bug 1：`rt_hw_twai_recv` 不清 RBNEI

IDF 自己的驱动写得很明确（`esp_hal_twai/twai_hal_v2.c`）：

```c
twaifd_ll_get_rx_frame(hal_ctx->dev, rx_frame);
// 'not_empty' interrupt can only be cleared after reading the frame
twaifd_ll_clr_intr_status(hal_ctx->dev, TWAIFD_LL_INTR_RX_NOT_EMPTY);
```

**本端口读了帧，从不清这个标志。**

后果链清楚：RBNEI 一直为 1 → 等待条件立刻满足 → 从空 FIFO 读 → 垃圾 header。

**已修**：读完之后清 RBNEI。

### 82.4 真 bug 2：等待条件用错了信号

原来的等待：

```c
while (twaifd_ll_is_rx_buffer_empty(s_twai) &&
       !(twaifd_ll_get_intr_status(s_twai) & TWAIFD_LL_INTR_RX_NOT_EMPTY))
```

**RBNEI 是中断状态位，不跟 FIFO 读指针同步。**

IDF 的做法（`esp_twai_onchip.c`）：

```c
while (twai_hal_get_rx_msg_count(twai_ctx->hal)) { ... }
```

而 `twai_hal_get_rx_msg_count()` 就是 `twaifd_ll_get_rx_frame_count()`。

**改成等帧计数**：

```c
while (twaifd_ll_get_rx_frame_count(s_twai) == 0u)
```

**已修。**

### 82.5 修完仍然间歇

```
after: 4, 0, 0, 4, 0     <- 5 次开机
```

**失败信息没变：仍然是 `len 8, want 4`。**

**⇒ 两个 bug 都是真的，但都不是这个现象的根因。**

### 82.6 记录一个重要的负面结果

**修了两个真实缺陷，现象没变。** 这不能记成"修复"，只能记成：

| 项 | 判定 |
|---|---|
| 清 RBNEI | ✅ 真 bug，已修（与 IDF 对齐） |
| 用帧计数等待 | ✅ 真 bug，已修（与 IDF 对齐） |
| `len 8` 现象 | ❌ **未解决** |

**两个修复都让代码更接近 IDF 的写法，这个价值独立于现象是否消失。**

### 82.7 下一轮的方向（已排除猜测）

不要继续猜。**打印原始 word**：

```c
twaifd_ll_get_rx_frame(s_twai, &buf);
rt_kprintf("raw: %08x %08x %08x %08x %08x\n",
           buf.words[0], buf.words[1], buf.words[2], buf.words[3], buf.words[4]);
```

**`words[0]` 的 bit[3:0] 是 dlc。** 直接看硬件给了什么，比从 `hdr.dlc` 反推可靠。

同时打印 `twaifd_ll_get_rx_frame_count()` 和 `is_rx_buffer_empty()`
在这一刻的值 —— **如果计数非 0 而读到 dlc=8，说明帧本身有问题；
如果计数为 0 却仍进入拷贝，说明等待条件被跨过。**

### 82.8 另一个待查点：`rwcnt`

```c
static inline void twaifd_ll_get_rx_frame(twaifd_dev_t *hw, twaifd_frame_buffer_t *rx_frame)
{
    rx_frame->words[0] = hw->rx_data.rx_data;
    for (uint8_t i = 1; i <= rx_frame->format.rwcnt; i++) {
        rx_frame->words[i] = hw->rx_data.rx_data;
    }
}
```

**`rwcnt` 的注释是 "Re-transmission counter"（`twaifd_struct.h:1770`）。**
正常应为 0。**若它非 0，这个循环会多读 word，把整个帧的语义搞乱。**

**下一轮要把 `rwcnt` 打出来。** 这是最有可能解释 `dlc=8` 的一条。

### 82.9 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ 33/33（未受影响） |
| `check_image.py` | ✅ 9/9 |
| `check_rtt_style.py --strict` | ✅ 16/16 |
| `s31twai` | ⚠️ **仍间歇**（5 次开机 4/0/0/4/0） |

### 82.10 方法学收获

| 收获 | 说明 |
|---|---|
| **"同理修复"和"修好现象"要分开记** | 前者有独立价值，不能因为后者没变就回滚 |
| **区分"每次调用"和"每次开机"** | 同一次开机内结果一致，说明是开机态 |
| **失败信息的具体形态是线索** | `len 8` 而不是"超时"或"发送失败"，缩到了 RX 解析 |
| **下一步要打印原始数据，不要继续推理** | 从 `hdr.dlc` 反推已经绕了一圈 |

---

## 83. W72：用原始寄存器数据查 `s31twai` —— 三个实验全失败，但拿到了确定的事实

上一轮结论是"下一轮打印原始 word，不再推理"。这一轮照做了。

### 83.1 先纠正上一轮的一个测量错误

上一轮的探针放在 `twaifd_ll_get_rx_frame()` **之后**：

```
[rx] cnt=0 empty=1 ...
```

**拷贝会把帧消费掉，所以这永远是读取之后的状态，什么都说明不了。**

探针移到拷贝**之前**：

```
[pre] cnt=%u empty=%d full=%d ovf=%d st=%08x
```

**教训：测决定行为的状态，不是测行为造成的结果。**

### 83.2 拿到的事实（这是本轮真正的收获）

同一次构建，pass 和 fail 的对比：

| 观察点 | pass | fail |
|---|---|---|
| TX 缓冲区 | `w0=00000004` dlc=4 payload=`a3a2a1a0` | **完全相同** |
| 拷贝前 FIFO | `cnt=1 empty=0 full=0 ovf=0 st=0x10` | **完全相同** |
| RX format word | `w0=00002104` dlc=4 rwcnt=4 | `w0=00002908` dlc=8 rwcnt=5 |
| 另一次 fail | — | `w0=00002909` **dlc=9** |

**三条结论：**

1. **本端口的 TX 格式化是对的** —— 缓冲区逐字节相同。
2. **判定点之前的状态不能解释差异** —— FIFO 状态完全相同。
3. **`dlc=9` 是决定性线索** —— classic CAN **不存在** dlc=9。

**⇒ 读到的是一个语义上不可能的帧头，不是"8 字节 vs 4 字节"的差异。**

### 83.3 三个实验，全部失败

| 实验 | 假设 | 8 次开机结果 |
|---|---|---|
| `fde=0` | FD 模式与 classic 帧矛盾 | `0,4,0,4,0,0,0,4` ❌ |
| 50 us settle | 帧头提交竞态 | `0,4,0,4,4,0,4,4` ❌ |
| 清 RBNEI + 等帧计数 | 等待信号用错 | 无改善 ❌ |

**全部失败。** 这三个假设都被数据否定了。

### 83.4 保留了什么，为什么

| 改动 | 保留？ | 理由 |
|---|---|---|
| 清 RBNEI（拷贝后） | ✅ | IDF 明确这么做，本端口之前不做 |
| 等帧计数而非 RBNEI | ✅ | IDF 从不用 RBNEI 判断有无帧 |
| **拒绝非法 DLC** | ✅ **新增** | 见下 |
| `fde=0` | ❌ 回滚 | 假设被否定 |
| 50 us settle | ❌ 回滚 | 假设被否定 |

**两个实验性改动已完全回滚**，代码里 `esp_rom_printf`/`delay`/`dbg` 残留均为 0。

### 83.5 新增：拒绝非法 DLC

原代码：

```c
*len = twaifd_dlc2len((rt_uint16_t)hdr.dlc);
if (*len > 8u) { *len = 8u; }     /* 把非法值变成合法值 */
```

**这会把一个不可能的帧头变成"看起来正常"的 8 字节读取，并返回 `RT_EOK`。**

改成：

```c
if (hdr.dlc > 8u)
{
    rt_kprintf("twai: illegal DLC %u in received frame (w0=0x%08x)\n", ...);
    return -RT_ERROR;
}
```

**理由：驱动能看出帧头非法，就必须说出来。**
"返回最近的合法值"恰好掩盖了调用者最需要知道的损坏。

**这也是为什么 dlc=9 那次以前从未被发现** —— 它被静默地钳成 8。

### 83.6 最终状态：间歇仍在

```
trial 0: 4/4   trial 1: 0/4   trial 2: 0/4
trial 3: 0/4   trial 4: 4/4   trial 5: 4/4
illegal-DLC-msg: 六次全 False
```

**间歇性失败仍然存在，本轮没有解决。**

**但它的性质变了：**

| 之前 | 现在 |
|---|---|
| "已知的间歇失败"（4 轮记录，无原因） | **有 TX/RX 原始数据，三个假设已被排除** |
| 无法判断是本端口还是硬件 | **本端口格式化正确，问题在控制器环回路径** |

### 83.7 诚实的结论

**这是一次"负结果"回合。** 三个实验都失败，间歇仍在。

**但负结果也是结果：**

- 排除了三个假设（FD 矛盾、提交竞态、等待信号）
- 证明了本端口格式化正确（TX 缓冲区逐字节相同）
- 发现了一个从机制上不可能的值（dlc=9），并让它不再被静默吞掉

**下一轮若还要查，方向应是控制器环回路径本身**（`stm`+`ilbp` 的组合在
CTU CAN FD 上是否有已知限制），而不是本端口的代码。**但这已经超出移植层范围。**

### 83.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed** |
| `check_rtt_style.py --strict` | ✅ 16/16 |
| `check_image.py` | ✅ 9/9 |
| `s31rmt` | ✅ PASS |
| `list device` | ✅ 8 设备 |
| `s31twai` | ⚠️ **仍间歇**（6 次开机 3 pass / 3 fail） |

已保存 `w72_verified.bin`。

### 83.9 方法学

| 教训 | 说明 |
|---|---|
| **测决定行为的状态，不是结果** | 探针放在拷贝之后，只能看到被消费后的空 FIFO |
| **一个物理上不可能的值胜过十个统计** | `dlc=9` 直接把问题定性为"帧头损坏" |
| **实验失败要全部回滚** | 留着"也许有用"的改动会污染后续判断 |
| **同一次构建内对比 pass/fail 才有意义** | 跨构建比较会被其他改动干扰 |
| **负结果要写清楚排除了什么** | 否则下一轮会重复同样的假设 |

---

## 84. W73：IRAM 里的代码调 flash 函数 —— 两处，其中一处在 trap 路径上

这一轮查的是**代码放置与调用规则**。本端口早就写过这条规则：

> IRAM 里的代码不能调 flash 里的函数

**并且已经因为违反它吃过一次苦**（记录的原文）：
`MEPC 0x40013b1e resolved to rt_enter_critical in IROM`。

**但这条规则只对 `_interrupt_handler` 执行过，没有对驱动的 ISR 执行。**

### 84.1 用 `nm` 对地址，两处命中

| 符号 | 地址 | 区 |
|---|---|---|
| `s31_uart_isr` | `0x2f002c66` | **IRAM** |
| `rtt_trampoline` | `0x2f002e5a` | **IRAM** |
| `rt_kprintf` | `0x400128ba` | **flash** |
| `esp_rom_printf` | `0x2f800024` | ROM |

**两个 IRAM 函数都调了 `rt_kprintf`。**

### 84.2 第二处在 trap 路径上，更严重

`rtt_trampoline` 不是普通 ISR，它在 trap 路径里：

```
vectors_rtthread.S:232   jal _global_interrupt_handler
interrupt.c:59           void _global_interrupt_handler(intptr_t sp, int mcause)
                         -> 按 mcause-16 索引槽位
                         -> 槽位里是 rtt_trampoline
```

**而它打印的消息正是"没有处理函数却触发了中断"，恰恰是最可能在
flash cache 关闭窗口里发生的情况。**

### 84.3 两处都改成 `esp_rom_printf`

```c
/* serial.c: ISR 里的 RX 溢出报告 */
- rt_kprintf("uart0: RX FIFO overrun (...)\n");
+ esp_rom_printf("uart0: RX FIFO overrun (...)\n");

/* interrupt.c: trampoline 里的无处理函数报告 */
- rt_kprintf("intr: vector %d fired with no handler, masked\n", c->vector);
+ esp_rom_printf("intr: vector %d fired with no handler, masked\n", c->vector);
```

**`esp_rom_printf` 在 ROM，地址 `0x2f800024`，不受 flash cache 影响。**

### 84.4 新增第 19 项检查：IRAM 函数不得调 flash API

```python
flash_apis = ("rt_kprintf", "rt_malloc", "rt_free", "rt_realloc", "rt_calloc",
              "rt_sem_take", "rt_mutex_take", "rt_thread_mdelay", "rt_thread_delay",
              "rt_tick_get", "rt_snprintf", "rt_vsnprintf", "rt_device_find",
              "rt_device_open", "rt_sem_release", "rt_mutex_release")
```

**从源码里找出所有 `IRAM_ATTR` 函数，提取函数体，检查是否调用上述任何一个。**

加检查时**立刻又抓到 `rtt_trampoline` 一次** —— 说明这条规则之前完全没有被机械保证过。

### 84.5 其余审计结果：都是"看起来像问题但其实不是"

这一轮还查了另外三项，结论都是**不需要改**：

**(a) `return -1;` 共 50 处**

用两种独立方法（花括号配平 + 全文正则）确认：
**没有任何一处位于返回 `rt_err_t` 的函数里。**

| 位置 | 返回类型 | 是否符合 RT 约定 |
|---|---|---|
| msh 命令 (`cmd_i2c` 等) | `int` | ✅ RT 的命令层就是 int |
| ops 回调 (`pin_read` 等) | `rt_ssize_t`/`rt_base_t` | ✅ -1 是文档值 |
| `s31_adc_vref` | `rt_int16_t` | ✅ 无校准路径 |

**(b) 内核与端口"都定义"的 15 个符号**

**全部是误报。** `compile_commands.json` 显示镜像里只有 35 个 rt-thread 对象，
**`libcpu/risc-v/common/*.c` 一个都没编**（只有 `legacy_common/atomic_riscv.c`）。

这是 RT-Thread 的标准模式：**新 CPU 端口自己实现 libcpu 契约，通用文件不编。**

**已加成第 17 项检查**（读 `compile_commands.json`，不信 CMakeLists 文本）。

**(c) `s31_pin_ops` 只填了 8 个字段（共 12）**

看起来像缺 4 个。查结构体定义：

```c
#ifdef RT_USING_DM
    rt_err_t (*pin_irq_mode)(...);        /* pin_irq_mode */
    rt_ssize_t (*pin_parse)(...);         /* pin_parse */
#endif
#ifdef RT_USING_PINCTRL
    rt_err_t (*pin_ctrl_confs_apply)(...);
    rt_err_t (*pin_ctrl_gpio_request)(...);
#endif
```

**两个宏都没定义，所以那 4 个字段根本不存在。使能块内是 8/8。**

**已加成第 18 项检查**（会跟踪 `#ifdef` 嵌套，跳过未使能字段）。

### 84.6 检查项 16 → 19，且验证了检查本身

**第 18 项我做了反向验证**：临时删掉 `gpio.c` 里的 `.pin_debounce` 赋值，
检查器立刻报错：

```
[FAIL] ops tables complete   unassigned in this config: ['gpio.c:rt_pin_ops.pin_debounce']
17/18 checks passed
```

**然后恢复文件并确认内容一致。检查器不是橡皮图章。**

### 84.7 验证

| 检查 | 结果 |
|---|---|
| `check_rtt_style.py --strict` | ✅ **19/19** |
| `check_image.py` | ✅ 9/9 |
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `s31intr -a` | ✅ 中断线状态正常 |

已保存 `w73_verified.bin`。

### 84.8 方法学

| 教训 | 说明 |
|---|---|
| **规则写下来 ≠ 规则被执行** | "IRAM 不能调 flash" 写了，但只作用于一个函数 |
| **用 `nm` 对地址，不要凭函数名猜位置** | `rt_kprintf` 名字看不出在 flash |
| **审计结果多数是"看起来像问题"** | 15 个符号冲突全是误报，50 个 -1 全合规 |
| **检查器要反向验证一次** | 故意破坏，确认它会红 |
| **条件编译的字段不是缺失** | 要先解析 `#ifdef`，再判断"没赋值" |

---

## 85. W74：`bind` 一个正在运行的线程到 CPU1，会让这个线程永远不再运行

`bind` 是 **RT-Thread 上游自带**的 msh 命令（`components/finsh/cmd.c`），
它调 `rt_thread_control(RT_THREAD_CTRL_BIND_CPU)`。

本端口 `ps` 里有 `bind` 列，之前几轮都看到所有线程是 `bind 2`（未绑定），
**但从没试过真的去绑。** 这一轮试了。

### 85.1 三组对照实验（同一镜像）

| 命令 | 结果 |
|---|---|
| `bind <tshell tcb> 0` | ✅ **成功**，shell 继续工作 |
| `bind <tsystem tcb> 1` | ✅ **成功**，shell 继续工作，`ps` 显示 `bind 1` |
| `bind <tshell tcb> 1` | ❌ **shell 卡死**，命令回显后无任何输出，无 panic，无复位 |

**`bind` 到 core 1 本身没问题** —— 绑 `tsystem` 完全正常。

**问题只出在"把一个正在运行的线程绑到 CPU1"。**

### 85.2 差别在 `rt_sched_thread_bind_cpu()` 的分支

```c
if (thread_stat == RT_THREAD_RUNNING)
{
    int current_cpu = rt_hw_cpu_id();       /* 0 */

    if (cpu != RT_CPUS_NR)
    {
        if (RT_SCHED_CTX(thread).oncpu == current_cpu)   /* 真的是本核在跑 */
        {
            if (cpu != current_cpu)
            {
                rt_hw_ipi_send(RT_SCHEDULE_IPI, 1U << cpu);   /* 通知 CPU1 */
                rt_sched_unlock_n_resched(slvl);              /* 本核重新调度 */
            }
```

**`tsystem` 是 `suspend`，不走这个分支；`tshell` 是 `running`，走。**

### 85.3 第一次归因错了，实验推翻了它

我第一版结论是"IPI 没有接收端，pending 位没人清，所以挂住"，
并在 `rt_hw_ipi_send()` 里加了守卫（无接收端时不置位）。

**然后重测三次，全部仍然卡死。**

```
trial 0: bind-replied=False  shell-alive=False
trial 1: bind-replied=False  shell-alive=False
trial 2: bind-replied=False  shell-alive=False
```

**⇒ IPI 不是原因。** 真正的原因是紧接着的那句：

```c
rt_sched_unlock_n_resched(slvl);     /* 在本核触发一次重调度 */
```

**线程已经被绑到 CPU1，就不在 CPU0 的就绪队列里了。CPU0 重新调度会选别的线程，
而 CPU1 停在 `wfi`、永远不调度 ⇒ 这个线程永远不会再被选中。**

**它不是"卡在内核里"，是"永远拿不到 CPU"。**

### 85.4 注释按测量结果改正了

`smp_ipi.c` 里的注释现在写的是：

```c
/*
 * This is NOT what makes `bind <thread> 1` wedge the shell. That was tested:
 * with this guard in place the shell still wedges, every time, so the IPI was
 * not the mechanism. The mechanism is one level up, in
 * rt_sched_thread_bind_cpu(): ...
 */
```

**并把三组对照实验的数字写进去了。**

### 85.5 IPI 守卫仍然保留，但理由变了

| 之前写的理由 | 现在的理由 |
|---|---|
| ~~它会挂住 bind~~ | **在没人服务的线上置 pending 本身就是错的** |

**一条没有 ISR 的线被置位，等它被 unmask 的那一刻就是中断风暴。**

**保留它，但不再声称它修了 bind。**（与 W70 的教训一致：结论对、理由错也要改。）

### 85.6 这是一处真实的移植层限制，应当记录

| 事实 | 影响 |
|---|---|
| `bind` 是上游内建命令 | 用户会用它 |
| 绑运行中的线程到 CPU1 会静默停掉那个线程 | **没有报错，没有 panic** |
| 根因是 CPU1 不调度（W56 起已知） | 不是新 bug，是已知限制的可见后果 |

**目前只在注释里记录，不改内核行为。** 要真正修好需要：
IPI 接收端 + CPU1 进入调度 —— 就是 W49/W50/W52 一直卡住的那件事。

### 85.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| `check_image.py` | ✅ 9/9 |
| `bind <tsystem> 1` | ✅ 正常，`ps` 显示 `bind 1` |
| `bind <tshell> 1` | ⚠️ **卡死（已知限制，已记录）** |

已保存 `w74_verified.bin`。

### 85.8 方法学

| 教训 | 说明 |
|---|---|
| **有一个对照组就够定位** | 绑 tsystem（非运行）成功，绑 tshell（运行）失败 |
| **"我加了守卫还是挂" 是重要信息** | 它直接否决了第一版归因 |
| **归因错了要把注释也改掉** | 否则下一个读者会信第一版结论 |
| **上游内建命令在本端口上可能不安全** | 移植层的接口面不止自己写的函数，还包括内核自带命令 |
| **"不报错地停掉一个线程"比崩溃更难查** | 没有 panic 就没有线索，必须靠对照实验 |

---

## 86. W75：`console set uart0` 会让 shell 永久失去输入 —— 已测得精确机制

W74 发现 `bind` 会挂住 shell。这一轮把**上游自带命令**逐个查了一遍，
找到第二个：`console set uart0`。

### 86.1 现象

```
msh > console set uart0
console change to uart0
msh >                        <- 最后一次输出，之后任何命令都没有响应
```

**板子没死** —— 加了 `s31con arm` 之后，软件定时器每秒照常打印：

```
[watch] flag=0x0113 open=0x004b ref=2 int_rx=NO stream=yes con=uart0
```

**死的是 shell 的读取路径，不是系统。**

### 86.2 前后对比（`s31con` 实测）

| 时刻 | `flag` | `open_flag` | `ref` | `INT_RX` | `con` |
|---|---|---|---|---|---|
| 命令前 | `0x0113` | **`0x014b`** | 1 | **yes** | `(none)` |
| 命令后 | `0x0113` | **`0x004b`** | **2** | **NO** | `uart0` |

**`INT_RX` 从 `open_flag` 里消失了。**

### 86.3 机制（读源码 + 数据吻合）

```
shell 打开 uart0 :  RDWR | INT_RX | STREAM  = 0x143
rt_console_set_device() 打开 :  RDWR | STREAM        = 0x043
```

`rt_device_open()` 用 `RT_DEVICE_OFLAG_MASK (0xf0f)` 比较：

```c
if (!(dev->open_flag & OFLAG_OPEN) ||
     ((dev->open_flag & MASK) != ((oflag & MASK) | OFLAG_OPEN)))
{
    result = device_open(dev, oflag);      /* 又调一次 rt_serial_open() */
}
```

两边不等（`0x10b` vs `0x04b`），**所以 `rt_serial_open()` 被第二次调用**，
而它第一句是：

```c
dev->open_flag = oflag & 0xff;      /* 0x043 —— INT_RX 没了 */
```

然后因为 `oflag` 没有 `INT_RX`，走的是"ring 已存在"的分支：

```c
else
{
    if (oflag & RT_DEVICE_FLAG_INT_RX)
        dev->open_flag |= RT_DEVICE_FLAG_INT_RX;   /* 不进来 */
}
```

**`serial->serial_rx` 仍然分配着，但 `open_flag` 不再选中它。**

### 86.4 为什么 shell 的读永远拿不到数据

```c
/* dev_serial.c: rt_serial_read() */
if (dev->open_flag & RT_DEVICE_FLAG_INT_RX)
    return _serial_int_rx(...);      /* INT_RX 已清 -> 不走这里 */
...
return _serial_poll_rx(...);         /* 直接读 UART 硬件 FIFO */
```

**但 `s31_uart_isr` 还在跑，还在把 FIFO 里的字节搬进那个 ring。**

⇒ **poll 读到的永远是空 FIFO** ⇒ 返回 0 ⇒ shell 在 `rt_sem_take(&shell->rx_sem)` 上
等一个已经被 ISR 取走的字符 ⇒ **永久等下去**。

**只有 `console set` 之后的输出还活着（走 `rt_device_write` → `_serial_poll_tx`），
输入彻底死了。**

### 86.5 这是上游交互，不是本端口写错

三个环节全在上游：

| 环节 | 位置 |
|---|---|
| `rt_console_set_device()` 用 `RDWR\|STREAM` 打开 | `src/kservice.c:176` |
| `rt_device_open()` 标志不等就重开 | `src/device.c:39` |
| `rt_serial_open()` 无条件重写 `open_flag` | `components/drivers/serial/dev_serial.c` |

**但本端口有一个额外的、更根本的冲突：**

> 本端口的 console 设计是**故意不调用 `rt_console_set_device()`**，
> 让 `_kputs()` 走 `rt_hw_console_output()`（ROM 输出），
> 这样 panic 时输出仍然活着。

**`console set` 恰好把端口推进了它被设计来避免的那个状态。**
而且推进方式还顺手把 shell 的输入弄死了。

### 86.6 记录而不是修

| 选项 | 评价 |
|---|---|
| 改内核 `cmd.c` | ❌ 违反"内核零修改"原则 |
| 改 `dev_serial.c` | ❌ 同上 |
| 在端口层拦截 | ⚠️ 可行但会掩盖上游行为 |
| **记录为已知限制** | ✅ **本轮选择** |

**理由：这是上游命令 + 上游驱动的交互，本端口没有写错任何东西。
把上游行为"修"成本地特殊逻辑，会让端口偏离"未修改的上游内核"这个资产。**

### 86.7 `s31con` 作为常驻诊断

新增 `s31con`（`console_probe.c`）：

```
msh > s31con
[now]   flag=0x0113 open=0x014b ref=1 int_rx=yes stream=yes con=(none)

msh > s31con arm
watching uart0 once per second
[watch] flag=0x0113 open=0x004b ref=2 int_rx=NO stream=yes con=uart0
```

**`arm` 的价值：shell 死掉之后，定时器还能每秒把状态打出来。**
否则"命令之后没有输出"这件事本身无法区分"板子死了"和"shell 死了核还在跑"。

**这是本轮唯一新增的代码，且只用于诊断。**

### 86.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed** |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| `check_image.py` | ✅ 9/9 |
| `s31con` | ✅ 报告正确状态 |
| `s31con arm` + `console set uart0` | ✅ **观测到 `int_rx` 由 yes 变 NO** |
| `console set uart0` 之后 | ⚠️ **shell 输入永久失效（已记录限制）** |

### 86.9 方法学

| 教训 | 说明 |
|---|---|
| **"没有输出"要区分"板子死"和"shell 死"** | 定时器打印把它们分开了 |
| **诊断代码要在故障之后还能跑** | 放在 shell 命令里就只能看到故障前 |
| **数据吻合源码才叫确认** | `open_flag 0x014b→0x004b` 与 `oflag & 0xff` 完全一致 |
| **上游交互错误不要"修"成本地逻辑** | 会让端口偏离未修改内核这个资产 |
| **端口的设计选择可能被上游命令推翻** | "不设 console 设备"是设计，`console set` 破坏了它 |

---

## 87. W76：输出脚读不回自己的电平 —— 已修

这一轮继续查上游命令。`pin` 是 `dev_pin.c` 里的内建命令，用它做了一次基本往返：

```
msh > pin mode 10 output
msh > pin write 10 high
msh > pin read 10
pin[10] = low          <- 写了高，读回低
```

### 87.1 先排除"写没生效"

用同一会话做了对照（`board.py` 每批都复位，会掩盖状态）：

| 步骤 | 结果 |
|---|---|
| `pin mode 10 output` | — |
| `pin write 10 high` | — |
| `pin read 10` | **low** |
| `pin mode 10 input_pulldown` | — |
| `pin read 10` | **low** |

**改成输入再读仍是 low** ⇒ 不是"写没生效"，**是读的路径问题**或者写真的没驱动。

### 87.2 根因：输出模式关掉了输入缓冲

```c
/* 修前 s31_pin_mode() */
case PIN_MODE_OUTPUT:
    ...
    gpio_ll_output_enable(&GPIO, pin);      /* 只开输出 */
    break;
```

而 `s31_pin_read()` 走的是：

```c
return gpio_ll_get_level(&GPIO, pin);       /* 读 in.in_data_next —— 输入路径 */
```

**ESP32 的输入缓冲（`IO_MUX.gpio[n].fun_ie`）默认关闭**，
所以输出脚读回来永远是 0。

### 87.3 更关键的是**内部不一致**

```c
case PIN_MODE_OUTPUT_OD:
    gpio_ll_od_enable(&GPIO, pin);
    gpio_ll_input_enable(&GPIO, pin);       /* <-- OD 分支开了输入 */
    gpio_ll_set_level(&GPIO, pin, 1);
    /* fall through */

case PIN_MODE_OUTPUT:
    ...                                      /* <-- 普通输出没开 */
```

**两个都是"输出"，读回能力却不同，而 `rt_pin_read()` 的文档没有这个区分。**

### 87.4 上游 ESP32 BSP 也有同样的问题

查了 `bsp/ESP/ESP32_C6/libraries/drivers/drv_gpio.c`：

```c
case PIN_MODE_OUTPUT:
    gpio_set_direction(pin, GPIO_MODE_OUTPUT);      /* 只输出 */
...
static rt_ssize_t mcu_pin_read(...) { return gpio_get_level(pin); }
```

**上游同样读不回。** 所以这不是"本端口抄错了"。

**但 IDF 自己明确区分了这两种模式：**

```c
GPIO_MODE_OUTPUT         /* output only */
GPIO_MODE_INPUT_OUTPUT   /* output and input */
```

**RT-Thread 的 `PIN_MODE_OUTPUT` 对应的是哪一个？**
文档说 `rt_pin_read` 是"get the level of the pin"，没有限制输入脚。
**而且"写完读回来确认"是最常见的用法。**

### 87.5 修法：输出模式也打开接收器

```c
case PIN_MODE_OUTPUT:
    ...
    gpio_ll_input_enable(&GPIO, pin);       /* 新增 */
    gpio_ll_output_enable(&GPIO, pin);
    break;
```

注释里写清了：OD 分支本来就这么做，普通输出不这么做是**内部不一致**；
IDF 用 `GPIO_MODE_INPUT_OUTPUT` 显式表达这个组合。

### 87.6 修复验证

```
msh > pin mode 10 output
msh > pin read 10
pin[10] = low
msh > pin write 10 high
msh > pin read 10
pin[10] = high        <- 正确
msh > pin write 10 low
msh > pin read 10
pin[10] = low         <- 正确
```

**回归：`s31test` 33/33、`s31led` 正常、`s31pwm` 正常、`s31rmt` PASS。**

### 87.7 顺带发现：命令用法提示后面跟着"command failed -1."

```
msh > s31led
usage: s31led <r> <g> <b>   (0..255 each), or s31led off
       s31led 0 0 0    off
       s31led 32 0 0   dim red
s31led: command failed -1.          <- 这句像是内部错误
```

**源码：** `msh.c` 里

```c
*retp = cmd_func(argc, argv);
...
if (cmd_ret < 0)
    rt_kprintf("%s: command failed %d.\n", cmd, cmd_ret);
```

**本端口 8 个命令在打印用法后 `return -1`**（`-1 == -RT_ERROR`）。

**上游的惯例是返回 0：**

| 命令 | 打印用法后返回 |
|---|---|
| `cmd_bind` | **0** |
| `cmd_list` | **0** |
| `console`（上游） | `-RT_ERROR` → **也会打印 command failed** |

**⇒ 上游自己也不一致。** 但 `cmd_bind` / `cmd_list` 的 0 更像"提示，不是错误"。

**本轮不动它**：改 8 个命令的返回值是行为改动，
而"用法提示 + command failed"在可读性上只是噪音，不是缺陷。
**记录在案，留给需要清理输出时统一处理。**

### 87.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| `check_image.py` | ✅ 9/9 |
| `pin write` + `pin read` | ✅ **往返正确** |
| `s31led` / `s31pwm` / `s31rmt` | ✅ 正常 |
| `list device` | ✅ 8 设备 |

已保存 `w76_verified.bin`。

### 87.9 方法学

| 教训 | 说明 |
|---|---|
| **先区分"写没生效"和"读不回来"** | 改成输入再读，一步分开两种假设 |
| **内部不一致比"和上游不同"更值得修** | OD 能读、普通输出不能，是本端口的矛盾 |
| **"上游也这样"不等于"这样对"** | 上游 ESP32 BSP 同样读不回，但 IDF 明确有 INPUT_OUTPUT |
| **API 文档的措辞是判据** | `rt_pin_read` 说"pin"，没说"input pin" |
| **确认修复要看行为，不是看代码** | 写高读高、写低读低，才算修好 |

---

## 88. W77：`pwm` 命令完全失效，`adc voltage` 打印乱码 —— 两个配置/返回值问题

这一轮用上游的 `adc` / `pwm` 内建命令做体检。

### 88.1 `pwm probe pwm0` 永远说"请先 probe"

```
msh > pwm probe pwm0
Please using 'pwm probe <device name>' first.
pwm: command failed -1.
```

**而 `pwm0` 确实在 `list device` 里。**

### 88.2 根因：`FINSH_USING_OPTION_COMPLETION` 没开

`rt_drv_pwm.c` 用选项宏分发：

```c
CMD_OPTIONS_STATEMENT(pwm_list)
static int pwm_list(int argc, char **argv)
{
    if (MSH_OPT_ID_GET(pwm_list) == PWM_LIST_PROBE) { ... }
```

而 `finsh.h` 里：

```c
#ifdef FINSH_USING_OPTION_COMPLETION
#define MSH_OPT_ID_GET(fun) msh_cmd_opt_id_get(argc, argv, (void*)fun##_msh_options)
#else
#define MSH_OPT_ID_GET(fun) ((int)(-1UL))      /* 恒为 -1 */
#endif
```

**`PWM_LIST_PROBE == 1`，而宏恒为 `-1` ⇒ 那个分支是死代码。**
于是每次调用都先撞到 `pwm_device == RT_NULL` 的检查。

### 88.3 为什么 `list` 没坏

`cmd.c` 里有两份实现：

```c
#ifndef FINSH_USING_OPTION_COMPLETION
    ... 手写 rt_strcmp 的版本 ...
#else
    ... MSH_OPT_ID_GET 版本 ...
#endif
```

**`rt_drv_pwm.c` 只有选项版本，没有回退。** 所以 `list` 两种配置都能用，`pwm` 不行。

### 88.4 启用该选项（431/466 个上游 BSP 都启用）

```c
#define FINSH_USING_OPTION_COMPLETION
```

Kconfig 默认 `y`，**本端口之前漏了。**

### 88.5 启用后立刻暴露三个命令描述里的逗号

```
error: 'cmd_mcpwm_msh_options' undeclared
error: 'cmd_rmt_msh_options' undeclared
error: 'cmd_intr_msh_options' undeclared
```

**原因：`MSH_CMD_EXPORT_ALIAS(cmd, alias, desc)` 是变参宏，
描述字符串里的逗号会被当成参数分隔符。**

| 文件 | 原描述 |
|---|---|
| `mcpwm.c` | `... dead time, self-measured` |
| `rmt.c` | `... square wave, verified by ...` |
| `intr_debug.c` | `... for all, -m N to mask` |

**4 个参数 → 走 `_MSH_FUNCTION_EXPORT_CMD3_OPT` → 引用 `<cmd>_msh_options`。**

**修法：把逗号改成 `and` / `;`。**

**这是之前一直潜伏的问题** —— 选项关着时 `CMD_OPTIONS_STATEMENT` 展开为空，
宏参数多一个也不报错。**启用选项才把它暴露出来。**

### 88.6 修复验证

```
msh > pwm probe pwm0
probe pwm0 success

msh > pwm get 0
Info of device [pwm0] channel [0]:
period      : 0
pulse       : 0
Duty cycle  : 2147483647%
```

**`probe` 好了。** 但 `Duty cycle : 2147483647%` 又是个问题（见 88.8）。

### 88.7 `adc voltage 0` 打印 `-32.-768V` —— 这个才是本端口的错

```
msh > adc voltage 0
adc1 channel 0 voltage is -32.-768V
```

**根因：`s31_adc_vref()` 返回 `-1`。**

框架的约定是：

```c
vref = dev->ops->get_vref(dev);
if (vref == 0)
    goto _voltage_exit;              /* 返回 0，表示"算不出" */

voltage = value * vref / ((1 << resolution) - 1);
```

**`0` 才是"不知道"的约定值，`-1` 会通过检查并被乘进结果：**

```
voltage = value * (-1) / ((1<<17)-1)   -> 负数
```

然后 `printf("%d.%03dV", v/1000, v%1000)` 就把负号打印在数字中间。

**查上游 19 个 `drv_adc.c`：`return 0` 出现 5 次，`return -1` 出现 0 次。**

**改了（`-1` → `0`）：**

```
msh > adc voltage 0
adc1 channel 0 voltage is 0.000V
```

**之前这一条被我记成"故意不修"，那是我判错了。**

### 88.8 `pwm get` 的 `2147483647%` —— 上游问题，记录不修

```c
(int)(((double)(cfg.pulse) / (cfg.period)) * 100)
```

`period == 0` 时是 `0/0` → NaN → 转 int 是未定义行为，这里落到 `INT_MAX`。

**上游代码，不在本端口范围。** 记录。

### 88.9 连带发现：`check_image.py` 的命令数一直是错的

启用选项后 `__fsymtab` 从 384 B 变成 528 B，脚本仍按 `// 12` 算：

```
[PASS] __fsymtab non-empty   528 B = 44 commands     <- 错
```

**真实条目数是 33**（`help` 实际列出 33 条）。

**原因：`struct finsh_syscall` 多了 `struct msh_cmd_opt *opt` 字段，12 B → 16 B。**

```
528 / 12 = 44   （脚本报的）
528 / 16 = 33   （真实值，DWARF 确认 byte_size = 16）
```

**它一直 PASS，因为只断言 `size > 0`。**

**修法：从 ELF 的 DWARF 读结构体大小，不再写死。**

```
[PASS] __fsymtab non-empty   528 B = 33 commands (entry 16 B, from DWARF)
```

**修的过程中我还错了一次**：第一次搜 `"struct " + name`，
但 objdump 的 DWARF 输出里是 `DW_AT_name : ...: finsh_syscall`，
**根本没有"struct"这个词**，所以返回 0，检查立刻变红（这次它真的会红）。

### 88.10 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| `check_image.py` | ✅ 9/9，**命令数 33（正确）** |
| `pwm probe pwm0` | ✅ `probe pwm0 success` |
| `adc voltage 0` | ✅ `0.000V`（修前 `-32.-768V`） |
| `s31adc 1 0` | ✅ 读数正常（1770 / 1763 / 1761） |
| `s31mcpwm` / `s31rmt` / `s31intr` | ✅ 描述改逗号后无回归 |
| `help` | ✅ 33 条命令 |

已保存 `w77_verified.bin`。

### 88.11 方法学

| 教训 | 说明 |
|---|---|
| **一个配置项可以让整条命令失效且不报错** | `MSH_OPT_ID_GET` 退化成常量 `-1` |
| **潜伏的宏参数错误会被"功能关闭"掩盖** | 描述里的逗号只在开选项后才报 |
| **"不知道"的约定值要查框架怎么判** | `0` 不是 `-1`，我原来判错了 |
| **断言只查 `> 0` 时，数字错了也不会红** | 命令数 44 vs 33 |
| **不写死结构体大小** | 配置一变它就错，从 DWARF 读 |
| **改检查器后要立刻看它是否真会红** | 我第一次改完返回 0，它立刻报了 FAIL |

---

## 89. W78：给"配置项静默禁用命令"做自动检查 —— 做了，验证后证明它永远不会红，删掉

上一轮发现 `FINSH_USING_OPTION_COMPLETION` 未定义会让整个 `pwm` 命令失效，
且**编译完全正常**。这一轮想把它变成自动检查。

### 89.1 先做了一次"统计有多少开关没定义"

第一版脚本统计"**被编译文件引用、但 rtconfig.h 未定义**"的开关，结果 11 个：

```
RT_USING_HOOK               NO   452   <-- compiled out, used by 11
RT_USING_DEVICE_OPS         NO   32    <-- compiled out, used by 7
RT_USING_MAILBOX            NO   455   <-- compiled out, used by 3
...
```

**看起来吓人，实际全是误报。**

### 89.2 误报原因：把 `#ifdef` 这一行算成了"使用"

| 宏 | 11 处"引用"实际是 |
|---|---|
| `RT_USING_HOOK` | `#ifdef RT_USING_HOOK` / `#endif` 这些**判定行本身** |
| `RT_USING_DEVICE_OPS` | 同上 |

**判定行是"决定别人的",不是"被别人决定的"。**

**这是 W72 的 R192 教训重演：测了决定行为的东西，当成被决定的东西。**

### 89.3 第二次：区分"guard"与"code"

排除 `#if/#ifdef/#ifndef/#elif/#endif/#define/#undef` 和注释后：

```
RT_USING_IDLE_HOOK    NO  1  1  <-- idle.c:33: #define RT_USING_IDLE_HOOK
RT_USING_SIGNALS      NO  4  1  <-- scheduler_mp.c:791: #else /* ! RT_USING_SIGNALS */
```

**两个"code 命中"仍然不是真命中：**
- `idle.c:33` 是 `#define`（我漏了这个指令）
- `scheduler_mp.c:791` 是 `#else` 注释（我漏了 `#else`）

**补上 `#else` / `#define` 后：真命中 0 个。**

### 89.4 那就写成检查项 20：`switch macros defined`

逻辑：扫描 `compile_commands.json` 里**真正编译**的 `.c` 文件，
排除指令行和注释，找出现在**代码**里、但 `rtconfig.h` 没定义的
`RT_USING_*` / `FINSH_USING_*` / `MSH_USING_*`。

```
[PASS] switch macros defined    all 48 defined, none read while undefined
20/20 checks passed
```

### 89.5 ⚠️ 反向验证：它抓不到自己声称要抓的问题

```
# 注释掉 #define FINSH_USING_OPTION_COMPLETION
[PASS] switch macros defined    all 47 defined, none read while undefined
20/20 checks passed
```

**它没红。它抓不到 W77 那个案例。**

### 89.6 原因是结构性的，不是正则写错

```
finsh.h:19    #ifdef FINSH_USING_OPTION_COMPLETION
finsh.h:178   #ifdef FINSH_USING_OPTION_COMPLETION
finsh.h:191   #ifdef FINSH_USING_OPTION_COMPLETION
```

**这个条件只出现在头文件里，没有任何 `.c` 文件在代码里读它。**

而 RT-Thread 把这类开关**一律写成预处理指令**，
所以"在 `.c` 的代码行里读开关"这件事**本身就不存在**。

**⇒ 这个检查永远不会红，因为它要抓的东西根本不在它扫描的范围里。**

### 89.7 删掉它

```python
# 检查 20 已删除（51 行）
19/19 checks passed
```

**理由：一个不会失败的检查比没有检查更糟 —— 它看起来像覆盖。**

### 89.8 这一轮的实际产出

| 项 | 结果 |
|---|---|
| 配置项审计 | ✅ 确认**没有**"被代码读取却未定义"的开关 |
| 检查项 20 | ❌ **做了，验证后删除** |
| `check_rtt_style.py` | 19/19（未增加） |
| 顺带修 | 无代码改动 |

**"审计没发现问题"也是结果**：这一轮用两种独立方法确认了
**当前没有第二个 `FINSH_USING_OPTION_COMPLETION` 式的问题**。

### 89.9 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ 33 passed, 0 failed，连跑 3 次 |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| `check_image.py` | ✅ 9/9，命令数 33 |
| `pwm probe pwm0` | ✅ success |
| `adc voltage 0` | ✅ 0.000V |

已保存 `w78_verified.bin`。

### 89.10 方法学

| 教训 | 说明 |
|---|---|
| **"引用"要区分判定行与被判定行** | `#ifdef X` 不是"使用 X" |
| **一个检查必须能被反向验证** | 破坏输入，看它会不会红 |
| **不会红的检查比没有检查更糟** | 它看起来像覆盖 |
| **动手前先确认问题在哪一层** | 条件在头文件，就不能只扫 .c |
| **"审计无发现"需要两种独立方法** | 单次扫描无发现可能是扫描错了 |

---

## 90. W79：把 19 项检查逐个反向验证 —— 3 项是装饰，已重写

W78 删掉了检查 20，因为它不会红。**同样的问题从没对剩下的 19 项问过。**
这一轮逐项做：**改坏它声称要抓的东西，看它会不会红。**

### 90.1 第一轮结果

| 检查 | 变异 | 结果 |
|---|---|---|
| init returns rt_err_t | 把 init 声明改成 `int` | ✅ **RED** |
| INIT_*_EXPORT present | 删掉一个 export | ❌ **stayed green** |
| board.c has few hand calls | 加两个手工调用 | ⚠️ 路径写错（在 `port/common/`） |
| console hook in driver layer | 改名 console 钩子 | ✅ RED |
| port headers match the sources | 声明一个不存在的符号 | ✅ RED |
| port headers have extern "C" | 删掉 guard | ✅ RED |
| no dead config macros | 加一个死开关 | ✅ RED |
| config pairs complete | 删掉 `SMALL_MEM_AS_HEAP` | ❌ stayed green |
| IRAM code avoids flash APIs | ISR 里调 `rt_kprintf` | ✅ RED |

### 90.2 第二轮：修正路径后

`board.c has few hand calls` → **RED**（3 个手工调用 > 2，正确）。

### 90.3 第三、四轮

| 检查 | 结果 |
|---|---|
| msh commands anchored | ❌ **stayed green** |
| shell bound to a device | ✅ RED |
| rt_uint32_t in declarations | ✅ RED（用真实的局部变量 `rt_uint32_t i;`） |
| framework registration used | ✅ RED |
| ops tables complete | ✅ RED |

### 90.4 三个装饰性检查，逐个分析

**(a) `INIT_*_EXPORT present`**

```python
check("INIT_*_EXPORT present", len(exports) >= 10, ...)
```

**实际有 11 个 ⇒ 删一个仍然 ≥ 10 ⇒ 不会红。**

**改成逐函数配对**：

```python
orphaned = [i for i in inits if i not in exports and i not in called]
check("every init is exported or called", not orphaned, ...)
```

**这里踩了一次坑**：第一次直接写 `f"{i}(" in board_calls`，
**结果匹配到了 `extern int rt_hw_rmt_init(void);` 这一行声明**，
删掉 export 仍然"看起来被调用" ⇒ **仍然不会红**。

改成只认"缩进的语句行、且不是 `extern` 声明"：

```python
for line in board_calls.splitlines():
    s = line.strip()
    if s.startswith("extern "):
        continue
    m = re.match(r"(\w+)\s*\(", s)
    if m:
        called.add(m.group(1))
```

**现在两种变异都会红**（删 export / 删手工调用）。

**(b) `msh commands anchored`**

```python
check("msh commands anchored", len(cmd_files) > 0, ...)
```

**只要存在任何命令就恒为真。** 变异：把 `smp_cmd.c` 的锚点改名
（同时删掉 board.c 里的调用）⇒ **stayed green**。

**改成逐文件检查它靠什么活下来**：

```python
if has_anchor:   kept_by_anchor += 1
elif has_u:      kept_by_u += 1
else:            unkept.append(f.name)
```

**实测分布：6 个靠锚点、9 个靠 `-u`、0 个两者都没有。**
（`-u` 覆盖的是文件里的 `rt_hw_*_init` 全局符号。）

**改成新断言后，同样的变异 → RED。**

**(c) `config pairs complete`**

第一轮删掉 `RT_USING_SMALL_MEM_AS_HEAP`（**主开关**）⇒ 绿。
**这是对的**：主开关没了就是整个功能关掉，不是"缺一半"。

换成它真正声称的方向——**主开关在、伙伴缺失**：
删掉 `FINSH_USING_SYMTAB`（`FINSH_USING_MSH` 还在）⇒ **RED**。

**⇒ 这一项不是装饰，是我第一轮的变异选错了方向。**

### 90.5 最终：19 项全部能被反向验证

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

**（其余 5 项是 ELF/编译数据库相关，在 `check_image.py` 里，本轮未动。）**

### 90.6 验证

| 检查 | 结果 |
|---|---|
| `check_rtt_style.py --strict` | ✅ **19/19** |
| `check_image.py` | ✅ 9/9 |
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `help` | ✅ 33 条命令（`msh commands kept in image` 保证） |

**本轮没有改动任何固件代码，只改了检查器。**

已保存 `w79_verified.bin`（与上一轮相同固件）。

### 90.7 方法学

| 教训 | 说明 |
|---|---|
| **`len(x) >= N` 在有 N+1 个时不会红** | 下界断言只能发现"少了太多" |
| **`substring in text` 会匹配声明** | `extern int f(void);` 也是 `f(` |
| **变异要选检查真正声称的方向** | `config pairs` 只查"主在伙伴缺" |
| **每项检查都要做一次反向验证** | 19 项里 2 项是装饰 |
| **装饰性检查的价值是负的** | 它让 19/19 这个数字失去意义 |

---

## 91. W80：`check_image.py` 的 9 项也逐个反向验证 —— 一项在检查整个二进制

W79 把 `check_rtt_style.py` 的 19 项做完了反向验证。这一轮做 ELF 级的 9 项。

### 91.1 最严重的一项：trap prologue 检查的是**整个镜像**

```python
dis = run("objdump", "-d", ..., str(elf))          # 全镜像反汇编
trap_prologue = re.findall(r"addi\s+sp,sp,-(\d+)", dis)
check("trap prologue subtracts 128+128", ("128" in trap_prologue), ...)
```

**`trap_prologue` 里有 6780 个元素**（镜像里所有函数的栈调整），
**`"128" in trap_prologue` 只要有任何一处减 128 就为真** —— 测得 53 处。

**它从来没有看过 trap handler。**

而真正的事实是（反汇编 `_interrupt_handler` 前两条）：

```
2f000100 <_interrupt_handler>:
2f000100: addi sp,sp,-128
2f000102: addi sp,sp,-128
```

**检查报告了正确的事实，理由却是错的。**

### 91.2 改成按符号定位 trap 入口

```python
trap_entry = syms0.get("_interrupt_handler")
raw = run("objdump", "-d", "--start-address=0x{trap_entry:x}",
          "--stop-address=0x{trap_entry+12:x}", elf)
prologue = re.findall(r"addi\s+sp,sp,-(\d+)", raw)
check("trap prologue subtracts 128+128", prologue[:2] == ["128", "128"], ...)
```

**输出变成**：

```
[PASS] trap prologue subtracts 128+128
       trap entry at 0x2f000100 subtracts ['128', '128'] (want two 128s: FP area then integer area)
```

**反向验证**：把反汇编起点偏移 8 字节 ⇒ `subtracts []` ⇒ **RED**。

### 91.3 `new-thread frame is 256 B` 同样问题

原来是全镜像数 `addi a0,a6,-256`，**恰好只有 1 条，但它在哪里并不被检查**。

改成限定 `rt_hw_stack_init_core` 的地址范围，并要求**恰好 1 次**：

```
[PASS] new-thread frame is 256 B
       rt_hw_stack_init_core at 0x2f0162ca reserves -256 once (found 1)
```

**反向验证**：把找的立即数改成 `-1024` ⇒ `found 0` ⇒ **RED**。

### 91.4 `trap entry installed`：`hits >= 25`，实测 29

**余量 4，六个槽位坏掉也不会红**，而且**没说其余 19 个槽是什么**。

改成**精确断言**——48 个槽只允许两种值：

```
29 slots -> _interrupt_handler, 19 -> _panic_handler, 0 stray
```

**查证那 19 个确实是 `_panic_handler`：**

```
2f0002b2 <_panic_handler>:  addi sp,sp,-160
```

**反向验证**：只允许 trap 一种值 ⇒ 19 个槽变 stray ⇒ **RED**。

### 91.5 最终 9 项全部可红

| 检查 | 反向验证 | 结论 |
|---|---|---|
| flash LOAD segments | `len(flash)==2` 精确 | ✅ |
| **new-thread frame is 256 B** | 改立即数 → RED | ✅ **重写** |
| **trap prologue subtracts 128+128** | 偏移 8 字节 → RED | ✅ **重写** |
| trap exit mstatus FS=3 | 找 `0x7800` 字面量，精确 | ✅ |
| \_\_fsymtab non-empty | W77 已从 DWARF 读大小 | ✅ |
| \_\_rt_init non-empty | `size > 0`，配 init order 精确 | ✅ |
| init table order | 逐项比对，W67 已实践过会红 | ✅ |
| \_mtvt_table present | 符号存在性 | ✅ |
| **trap entry installed** | 制造 stray → RED | ✅ **重写** |

### 91.6 三个"报告正确事实但理由错误"的共同点

| 检查 | 错在哪 |
|---|---|
| trap prologue | 全镜像 grep，不是 trap 入口 |
| new-thread frame | 全镜像 grep，没有定位到函数 |
| trap entry installed | 下界 25 而非精确集合 |

**三个都是 `>= N` 或 `globally in text` 的形式。**
与 W79 在 `check_rtt_style.py` find 的两项**完全同类**。

### 91.7 验证

| 检查 | 结果 |
|---|---|
| `check_image.py` | ✅ **9/9**（3 项已重写） |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |

**本轮没有改动固件，只改检查器。**

已保存 `w80_verified.bin`。

### 91.8 方法学

| 教训 | 说明 |
|---|---|
| **全镜像 grep 不是定位** | 要在正确符号的地址范围内查 |
| **下界断言 (`>= N`) 是弱断言** | 改成精确集合或"恰好一次" |
| **"报告正确事实"不等于"检查正确"** | 理由错了，下次就会漏 |
| **每个反汇编检查都要打印它实际看了哪里** | 现在输出里直接写着 `0x2f000100` |
| **同一类缺陷会跨两个文件重复** | W79 和 W80 共找到 5 项 |

---

## 92. W81：给 `s31test` 的行为断言注入故障 —— 3 项验证，1 项部分

前两轮把 `check_rtt_style.py`（19 项）和 `check_image.py`（9 项）做了反向验证。
这一轮做 `s31test` 的 33 项 —— 它们是**行为断言**，反向验证要**注入故障**。

### 92.1 `fpu`：删掉 f0..f31 的保存 ⇒ 1000 次失配

**第一次尝试失败**：直接 `#ifdef ARCH_RISCV_FPU` → `#if 0`，
**把 `addi sp, sp, -32*FREGBYTES` 也去掉了** ⇒ 帧布局改变 ⇒

```
Illegal instruction, MEPC 0x2f019518
```

**这是帧坏了，不是"FPU 没保存"。**

**第二次：保留栈帧，只删 32 条 `FSTORE`：**

```
[FAIL] fpu      1000 mismatches, first at reg 15 value 0x63ca
```

**✅ 测试确实在测它声称的东西。**

**恢复后：`[PASS] fpu  32 registers x 500 switches, no mismatch`**

### 92.2 `rhwh.ud`：让 `rt_hw_us_delay` 立刻返回 ⇒ 0 us

```c
if (us) return;   /* provoked: no delay at all */
```

```
[FAIL] rhwh.ud  1000 us request took 0 us (want 900..1600)
[FAIL] rhwh     5 sub-checks, 1 bad
```

**✅ 测试有效，而且 5 个子检查的汇总行正确地把失败归到 1 项。**

### 92.3 `tick`：让 `rt_tick_increase()` 每中断调两次 ⇒ 2 倍

```c
rt_tick_increase();
rt_tick_increase();   /* provoked: 2x tick */
```

```
[FAIL] tick     4000 ticks in 1999999 us = 499 us/tick (expect 1000, 1000 Hz)
```

**✅ 窗口 ±5% 足够紧，2 倍错误立刻抓到。**

**也说明它抓得到 16 倍错误（SYSTIMER 单位算错）：16000 us/tick 远超 1050。**

### 92.4 `ctx`：注入太粗，破坏了 shell 自己

**做法**：在协作切换的恢复路径里，把保存的 s1 槽改成 `0x5A5A5A5A`。

**结果不是测试失败，而是命令都没执行：**

```
s31test: command not found.
...
Guru Meditation Error: Core 0 panic'ed (Load access fault)
MEPC 0x4001b8ba   S1: 0x5a5a5a5a
```

**分析：**

| 观察 | 含义 |
|---|---|
| `S1: 0x5a5a5a5a` | **s1 确实是从那个槽恢复的** ⇒ 恢复路径是真的 |
| 命令没执行就崩 | 破坏发生在**shell 自己的切换**上，测试还没开始 |

**⇒ 这次注入证明了"寄存器确实从保存槽恢复"，但没有证明 `ctx` 会报错。**

**记为部分结果，不冒充成功。**

### 92.5 已完成的与未完成的

| 测试 | 注入 | 结果 |
|---|---|---|
| `fpu` | 删 32 条 FSTORE | ✅ **FAIL（1000 次失配）** |
| `rhwh.ud` | us_delay 变空 | ✅ **FAIL（0 us）** |
| `tick` | tick 加倍 | ✅ **FAIL（499 us/tick）** |
| `ctx` | 破坏 saved s1 | ⚠️ **破坏生效但测试没机会跑** |
| 其余 29 项 | — | **本轮未做** |

**33 项里验证了 3 项，1 项部分。** 这是**如实进度**，不是"33 项已验证"。

### 92.6 恢复纪律

每一次注入都：
1. 断言注入点唯一存在
2. 注入
3. 重编译 + 烧写 + 观察
4. **恢复原文**
5. 重编译 + 烧写 + 确认 33/33
6. 确认 `provoked` 标记数为 0

**本轮四次注入全部恢复，最终 `s31test` 33/33 连跑 3 次。**

### 92.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `check_image.py` | ✅ 9/9 |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| `FSTORE` 恢复计数 | ✅ 32 条 |
| `provoked` 标记残留 | ✅ 0 |

已保存 `w81_verified.bin`。

### 92.8 方法学

| 教训 | 说明 |
|---|---|
| **注入要精确到目标缺陷** | 去掉整个 `#ifdef` 连栈帧一起没了，变成另一个故障 |
| **保留结构的注入才有效** | 留 `addi sp`、只删 `FSTORE` |
| **注入可能先破坏测试环境** | `ctx` 的注入炸掉了 shell，测试没机会跑 |
| **部分成功要记成部分** | 不写"33 项已验证" |
| **每次注入都要有恢复确认** | 标记数 + 全套测试通过 |
| **"抓到"和"没抓到"都要有证据** | 前者看 FAIL 文本，后者看 panic 位置 |

---

## 93. W82：继续注入 —— 4 项验证，其中 1 次又犯了同样的错

W81 验证了 `fpu` / `rhwh.ud` / `tick`，`ctx` 部分。
这一轮做 `irq` / `nest` / `rhwh.mk`。

### 93.1 `irq`：第一次注入又把 shell 弄死了

**做法**：让 `rt_hw_interrupt_umask()` 不置 IE 位。

**结果**：板子打印 banner 后**完全不响应命令**。

**原因**：`finsh` 打开 `uart0` 时用 `RT_DEVICE_FLAG_INT_RX`，
驱动的 `control(SET_INT)` 会调 `rt_hw_interrupt_umask(ETS_UART0_INUM)`。
**注入把 shell 自己的输入也关掉了。**

**⇒ 与 W81 的 `ctx` 完全同一个错误：注入面太大。**

### 93.2 `irq`：第二次改成只动引脚分发

只把 `s31_gpio_isr()` 里两个 `while` 循环跳过（状态仍然清，不会中断风暴）：

```
[FAIL] irq      pin 10: 0/200 interrupts, nest=0 (0 bad)
```

**✅ 有效。**

### 93.3 `nest`：把所有中断线压成同一级

把 `rt_hw_interrupt_set_priority()` 里的电平写死为 1：

```
[FAIL] nest     pin 10->11: outer 100@nest1, inner 100@nest1 (want 100, 100 bad)
```

**✅ 有效** —— 内层不再抢占，`inner_depth` 停在 1 而不是 2，100 次全记错。

**注意它仍然 PASS 了外层 100 次**，所以失败的定位很精确：**不是"没中断"，是"没嵌套"。**

### 93.4 `rhwh.mk`：把 is_masked 查询取反

```
[FAIL] rhwh.mk  mask/umask: 1 -> 0 (want 1) -> 1 (want 0)
```

**✅ 有效**，而且输出直接把实际序列和期望序列并排打印。

### 93.5 进度

| 测试 | 注入 | 结果 |
|---|---|---|
| `fpu` | 删 32 条 FSTORE | ✅ FAIL |
| `rhwh.ud` | us_delay 变空 | ✅ FAIL |
| `tick` | tick 加倍 | ✅ FAIL |
| `rhwh.mk` | is_masked 取反 | ✅ FAIL（本轮） |
| `irq` | 跳过引脚分发 | ✅ FAIL（本轮） |
| `nest` | 电平压平 | ✅ FAIL（本轮） |
| `ctx` | 破坏 saved s1 | ⚠️ 破坏生效但测试没机会跑 |
| 其余 26 项 | — | 未做 |

**7 项已验证会失败，1 项部分，26 项未做。**

### 93.6 这一轮最重要的教训：**同一个错我犯了两次**

| 轮次 | 注入 | 为什么错 |
|---|---|---|
| W81 | 破坏 saved s1 | 破坏了 shell 自己的切换 |
| W82 | 让 umask 不置位 | 关了 shell 自己的 UART RX |

**两次都是"注入面覆盖了 shell 赖以运行的东西"。**

**规律：注入前先问"这个函数还有谁在用？"**

`rt_hw_interrupt_umask()` 被 UART 驱动用；`rt_hw_context_switch` 被 shell 用。
**而 `s31_gpio_isr()` 只被引脚中断用 —— 所以那次成功。**

**⇒ 最后写进 runbook 的判据：注入点必须是"只有被测对象在用"的函数。**

### 93.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `check_image.py` | ✅ 9/9 |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| `provoked` 残留 | ✅ **0** |
| `rt_hw_interrupt_is_masked` | ✅ 已逐字恢复 |

已保存 `w82_verified.bin`。

### 93.8 方法学

| 教训 | 说明 |
|---|---|
| **注入前先问"这个函数还有谁在用"** | 两次都栽在共享函数上 |
| **注入点应只有被测对象在用** | `s31_gpio_isr` 成功，`umask` 失败 |
| **失败输出要能定位到子项** | `nest` 失败但外层 100 次仍 PASS，定位精确 |
| **同一个错可以犯两次** | W81/W82 完全同类，说明判据当时没写下来 |
| **写进判据才算学会** | 已写进 runbook §78.6 |

---

## 94. W83：`rhwh.ud` 大约四次里失败一次 —— 是测试的容差，不是端口的 bug

上一轮注入 `rhwh.ui` 时注意到一个附带现象：

```
[FAIL] rhwh.ud  1000 us request took 1999 us (want 900..1600)
```

**恢复后单独跑 4 次：**

```
run 1: 1000 us   PASS
run 2: 1000 us   PASS
run 3: 1998 us   FAIL
run 4: 1000 us   PASS
```

**约四次一次。** 这是一个**已经存在的间歇失败**，之前没记录过。

### 94.1 1998 恰好是 2 倍

`rt_hw_us_delay()` 在 SYSTIMER 上忙等。**1 kHz 的 tick 中断在 1000 us 里必然触发一次。**
ISR 执行期间忙等循环停住，恢复后继续计时 —— **单次测量必然多出最多一个完整周期。**

```
1000 us（请求） + ~998 us（一次 tick 的 ISR 时间） = 1998 us
```

**⇒ 不是 `rt_hw_us_delay` 的缺陷，是测量方法问题。**

### 94.2 第一次修法是错的：放宽窗口

把上限从 1600 改成 3000：

```
run 1..8: 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1999
```

**稳定了，但代价很大**：

| 错误 | 实测值 | 3000 窗口能抓到吗 |
|---|---|---|
| weak stub | ~0 | ✅（下限 900） |
| **2 倍单位错** | **~2000** | ❌ **和一次 tick 的噪声同量级** |
| 16 倍单位错 | ~16000 | ✅ |

**"放宽到能容纳噪声"等于"对同量级的真实错误失明"。**

### 94.3 正确的修法：取 5 次的最小值

**关键洞察：抢占只能让测量值偏大，不可能偏小。**

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

**窗口回到原来的 900..1600。**

**8 次验证：**

```
run 1..8: 1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000   全 PASS
```

### 94.4 并且确认它仍然抓得到 stub

临时把 `rt_hw_us_delay` 改成立即返回：

```
[FAIL] rhwh.ud  1000 us request took 0 us best of 5 (want 900..1600)
```

**✅ 收紧窗口没有削弱检测能力。**

### 94.5 这一轮的三层结论

| 层 | 内容 |
|---|---|
| **现象** | `rhwh.ud` 约 4 次失败 1 次，之前未记录 |
| **原因** | 1 kHz tick 在 1000 us 忙等里必然触发一次，单次测量最多多一个周期 |
| **错误修法** | 放宽上限到 3000 —— 稳定了，但对 2 倍错误失明 |
| **正确修法** | **取 5 次最小值** —— 噪声只能让值变大，取最小即滤掉 |

### 94.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `s31test rhwh` | ✅ **8 次全过**（修前 8 次里有 1 次失败） |
| stub 注入 | ✅ **仍能抓到（0 us）** |
| `check_image.py` | ✅ 9/9 |
| `check_rtt_style.py --strict` | ✅ 19/19 |
| 注入标记残留 | ✅ 0 |

已保存 `w83_verified.bin`。

### 94.7 方法学

| 教训 | 说明 |
|---|---|
| **"间歇失败"先问是不是测量方法问题** | 恰好 2 倍 = 一个周期，这是线索 |
| **放宽容差 = 对同量级错误失明** | 3000 窗口看不见 2000 的 2 倍错 |
| **找噪声的单向性** | 抢占只会让测量偏大 ⇒ 取最小值 |
| **收紧后必须重新验证仍能抓到目标** | 注入了 stub，仍然 FAIL |
| **间歇失败要记录具体频率** | "4 次里 1 次" 比 "偶尔" 有用 |

---

## 95. W84：`RT_TIMER_FLAG_SOFT_TIMER` 被接受但行为静默降级 —— 加了第 21 项检查

上一轮修完 `rhwh.ud` 的间歇失败后，这一轮先做了一件事：**连续跑 15 次全套，确认没有别的间歇。**

```
run 1..15 : === 33 passed, 0 failed ===   全过
rhwh.ud   : 1000 us best of 5            全过
```

**修好一处噪声源之后，全套 15 次稳定。**（修前 `rhwh.ud` 是 4 次里 1 次失败。）

### 95.1 顺带核对了另外两处时间断言

| 断言 | 窗口 | 12 次实测 |
|---|---|---|
| `clock` 100 ms | 90000..300000 us | 99997~100000 us |
| `fbshim/g` 50 ms | > 5000 us 判为"真的睡了" | 稳定 |

**`clock` 的实测值集中在 100000±3 us，离 90000 下限还有 10% 余量，不是间歇源。**

**但它的报文说 "want 100000"，而断言是 300000 上限 —— 报文比断言严格。**
这是反向的不匹配（不会漏报），记录但不动。

### 95.2 发现一个真正的语义偏差

`RT_USING_TIMER_SOFT` **未定义**。查内核源码：

```c
/* timer.c: rt_timer_create —— 不检查 SOFT_TIMER 标志 */
_timer_init(timer, timeout, parameter, time, flag);   /* flag 原样存下 */

/* timer.c: rt_timer_start */
#ifdef RT_USING_TIMER_SOFT
    if (timer->parent.flag & RT_TIMER_FLAG_SOFT_TIMER)
    {
        timer_list = _soft_timer_list;      /* 软链表 */
    }
    else
#endif
    {
        timer_list = _timer_list;           /* 硬件链表 */
    }
```

**`SOFT_TIMER` 分支被 `#ifdef` 编译掉了** ⇒ 软定时器**被挂进硬件链表**。

**后果：回调在 tick 中断上下文执行，而不是 timer 线程。**

**而且没有任何报错或警告 —— 标志被接受，行为被降级。**

### 95.3 这与 W75 的发现是同一类

| W75 | W84 |
|---|---|
| `console set uart0` 接受命令，静默弄死输入 | `SOFT_TIMER` 接受标志，静默换成中断上下文 |

**"接受但降级"比"拒绝"危险得多。**

### 95.4 查证：本端口没有依赖这个特性

```
RT_TIMER_FLAG_SOFT_TIMER 在 port/ 和 main/ 里的出现次数：0
```

**⇒ 当前没有实际问题，但也没有任何东西阻止以后有人写。**

### 95.5 加第 21 项检查

```python
if "#define RT_USING_TIMER_SOFT" not in rtcfg:
    soft_uses = [...扫描 port_c + APP 的源码行...]
    check("no soft timers without the feature", not soft_uses, ...)
```

```
[PASS] no soft timers without the feature
       RT_USING_TIMER_SOFT off, and no port code requests one
```

### 95.6 ⚠️ 这次先做反向验证，而不是先宣布

W78 的教训：**检查 20 做完才发现不会红，只能删掉。**

这次先验证：

```
# 在 console_probe.c 里加 | RT_TIMER_FLAG_SOFT_TIMER
[FAIL] no soft timers without the feature
       requests a soft timer but RT_USING_TIMER_SOFT is off: ['console_probe.c:...']

# 恢复后
[PASS] RT_USING_TIMER_SOFT off, and no port code requests one
```

**✅ 会红，而且是真阳性（真实的调用点参数）。**

### 95.7 为什么这一项能做而检查 20 不能

| | 检查 20（已删） | 检查 21（保留） |
|---|---|---|
| 条件在哪 | **头文件** `#ifdef` | **调用点参数** |
| 扫 `.c` 能否看到 | ❌ 看不到 | ✅ 能 |
| 能否构造真阳性 | ❌ | ✅ |

**判据：检查要抓的东西必须在它扫描的范围内。**
**"配置项被读"发生在头文件里，"配置项被请求"发生在调用点。**

### 95.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| 全套 15 次 | ✅ **15/15** |
| `check_rtt_style.py --strict` | ✅ **20/20**（新增第 21 项，编号 20） |
| `check_image.py` | ✅ 9/9 |
| 第 21 项反向验证 | ✅ **RED** |

已保存 `w84_verified.bin`。

### 95.9 方法学

| 教训 | 说明 |
|---|---|
| **修完一处噪声要重跑多次确认** | 15 次全过后才敢说"稳定" |
| **"接受但降级"比"拒绝"危险** | 与 W75 同类 |
| **上游的静默降级，端口能做的是"别依赖它"** | 不改内核，改为断言自己不用 |
| **先反向验证再宣布** | W78 的教训 |
| **检查要抓的东西必须在扫描范围内** | 头文件的条件 vs 调用点的参数 |

---

## 96. W85：三处驱动**丢弃了 ISR 安装的结果** —— 都修了，并加了第 22 项检查

这一轮查的是"**哪些可失败的 RT-Thread 调用，返回值没被检查**"。

### 96.1 先做了一次全端口扫描

| 类别 | 数量 |
|---|---|
| 结果被赋值 | 15 |
| 显式转 `(void)` | 2（`selftest.c` 里两次 close，故意） |
| **语句级调用、结果丢弃** | **13** |

13 处里大多数是良性的（刚创建的线程 `rt_thread_startup` 不会失败），
**但三处不是：**

```
gpio.c:224     rt_hw_interrupt_install(s_gpio_intno, ...)
serial.c:165   rt_hw_interrupt_install(ETS_UART0_INUM, ...)
selftest.c:673 rt_hw_interrupt_install(ST_NEST_INNER_INUM, ...)
```

### 96.2 为什么这个返回值**不能**直接测

```c
rt_isr_handler_t rt_hw_interrupt_install(...)
{
    if (vector < 0 || vector >= RTT_INUM_NUM)
    {
        rt_kprintf("intr: bad vector %d ...\n", ...);
        return RT_NULL;              /* 失败 */
    }
    old = s_ctx[vector].fn;
    ...
    return old;                      /* 没装过 handler 时也是 RT_NULL */
}
```

**成功（无前任 handler）和失败（vector 被拒）都返回 `RT_NULL`。**

**⇒ `if (install(...) != RT_NULL)` 这种写法根本抓不到失败。**

### 96.3 正确的做法：问槽位，不问返回值

```c
rt_hw_interrupt_install(ETS_UART0_INUM, s31_uart_isr, RT_NULL, "uart0");

if (intr_handler_get(ETS_UART0_INUM) == RT_NULL)
{
    rt_kprintf("uart0: INUM %d did not take the handler\n", ETS_UART0_INUM);
    return -RT_ERROR;
}
```

**`intr_handler_get()` 是本端口自己写入 `rtt_trampoline` 的只读查询
（W65 的 `rhwh.iv` 已经在用）。**

**三处都改了**：`serial.c`、`gpio.c`、`selftest.c`。

**失败后果的严重性：**

| 站点 | 静默失败的样子 |
|---|---|
| `serial.c` | uart0 初始化成功、注册成功，**一个字节都收不到** |
| `gpio.c` | pin 设备注册成功，**所有 `rt_pin_attach_irq` 都是死的** |
| `selftest.c` | nest 测试**测不到任何东西**却可能报 PASS |

### 96.4 加第 22 项检查

规则：**`rt_hw_interrupt_install()` 之后 10 行内必须有 `intr_handler_get()`。**

```
[PASS] ISR install result is used    every ISR install confirms the slot
```

**这次是"先写检查 → 它立刻红（三处真站点）→ 修三处 → 变绿"。**

```
加检查时:  [FAIL] result discarded at ['gpio.c:224', 'selftest.c:673', 'serial.c:167']
修完后:    [PASS] every ISR install confirms the slot
```

### 96.5 修检查器时发现它自己有个 bug

第一次修完三处后，检查**仍然红**，而且报的行号是 `gpio.c:224` —— **与真实的 274 行不符**。

**原因：它先剥掉块注释再分行，行号全部偏移。**

**这是检查器自身的缺陷：报错行号不对 = 教人不要相信它。**

**修法：保留原文本逐行走，遇到注释行跳过。** 行号自然正确。

**同时还要接受"装完之后查槽位"这个新形态** —— 否则修完的真站点仍被误报。

### 96.6 反向验证

```
# 删掉 serial.c 里的槽位检查块
[FAIL] ISR install result is used    ['serial.c:232'] install without confirming the slot
                                     ↑ 行号现在是真实的
# 恢复
[PASS] every ISR install confirms the slot
```

**✅ 会红，行号正确。**

### 96.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `s31test irq` | ✅ `200/200 interrupts, nest=1` |
| `s31test nest` | ✅ `outer 100@nest1, inner 100@nest2` |
| `check_rtt_style.py --strict` | ✅ **21/21** |
| `check_image.py` | ✅ 9/9 |
| 第 22 项反向验证 | ✅ **RED，行号正确** |

已保存 `w85_verified.bin`。

### 96.8 方法学

| 教训 | 说明 |
|---|---|
| **返回 `RT_NULL` 既表示成功也表示失败** | 这时只能问状态，不能测返回值 |
| **"结果丢弃"扫描能找出真问题** | 13 处里 3 处是实质缺陷 |
| **检查器报的行号必须真实** | 剥注释再分行会偏移 |
| **新检查要接受修复后的正确形态** | 否则修完还红 |
| **写检查的最佳时机：先写，让它红，再修** | 这样能证明它抓得到 |

---

## 96b. W86：`sem` 测试里有一个"看着像完成标志"的死变量

继续 W85 的口径（扫"可失败调用的返回值"），这一轮扫 `rt_sem_*` / `rt_mutex_*` /
`rt_event_*` / `rt_device_*`。

### 96b.1 扫描结果：只剩 4 处

```
selftest.c:857   rt_sem_release(&s_sem);
selftest.c:876   rt_sem_init(...);
selftest.c:882   rt_sem_detach(&s_sem);
selftest.c:912   rt_sem_detach(&s_sem);
```

**全是 `sem` 测试自己。** 但顺带发现一个更值得处理的东西。

### 96b.2 `s_sem_sent`：写了三处，读零处

```
selftest.c:848  static volatile rt_uint32_t s_sem_sent;
selftest.c:858      s_sem_sent++;                      /* 每次 release */
selftest.c:864  s_sem_sent = 0x80000000u | n;          /* 生产者退出时 */
```

**没有任何地方读它。**

**编码方式泄露了原意**：`0x80000000 | n` 显然想做"生产者已完成"的标志
（高位置位 + 计数）。但测试后来改成**等 token 计数**（更强的检查：
证明数据真的到了，而不只是循环结束了），这个标志就变成了死状态。

**为什么值得修**：**死状态如果看起来像个完成标志，就是在骗下一个读者。**

### 96b.3 顺手把 `rt_sem_release` 的失败计数接上

```c
/* 修前 */
rt_sem_release(&s_sem);
s_sem_sent++;

/* 修后 */
if (rt_sem_release(&s_sem) != RT_EOK)
{
    s_sem_release_failed++;
}
```

`rt_sem_release()` 在 `max_value` 溢出时返回 `-RT_EFULL`。
**N=500、消费者持续取走，实际到不了上限 —— 但代码里没有一句话说明这一点。**

**报文体也带上：**

```
[PASS] sem      500/500 tokens, 0 release failures, extra take timed out
```

**判据加了 `&& (s_sem_release_failed == 0)`。**

### 96b.4 反向验证：把 max_value 设成 0

```c
s_sem.max_value = 0;   /* provoked: every release overflows */
```

```
[FAIL] sem      0/500 tokens, 500 release failures, extra take timed out
```

**✅ 计数器接对了，而且失败信息把两个数字都摆出来（收到 0、失败 500）。**

**恢复后：`500/500 tokens, 0 release failures`。**

### 96b.5 加第 23 项检查：不得有"只写不读"的文件级变量

**难点：不能误报三类合法情形。**

| 合法情形 | 例子 | 为什么 |
|---|---|---|
| ops 表 | `s31_pin_ops` | 声明 + 取地址注册 = 被读 |
| 结构体实例 | `s_pwm` | 声明 + `&s_pwm.parent` |
| 同文件内读的标志 | `s31_ipi_receive_side_ready` | 声明 + `if (!...)` |

**规则写成**：收集只在**赋值形态**出现的行（`name =`、`name++`、`name +=` …），
再加声明行；**若没有一行是"非赋值形态"，才报。**

**另外必须剥注释再统计** —— `s_sem_sent` 的说明注释里提到了它自己，
**如果不剥，那一行会被当成"读"。**

### 96b.6 反向验证

把 `s_dead_marker` 塞回去：

```
[FAIL] no write-only statics    written but never read: ['selftest.c:s_dead_marker']
```

**恢复后：**

```
[PASS] no write-only statics    every file-scope variable is read somewhere
```

**✅ 会红，且没有误报那三个合法情形。**

### 96b.7 全端口复扫：8 个"低出现次数"静态变量

| 变量 | 判定 |
|---|---|
| `s31_adc_ops` / `s31_pin_ops` / `s31_i2c_ops` / `s31_pwm_ops` / `s31_uart_ops` / `s31_spi_ops` | ✅ 取地址注册，合法 |
| `s_pwm` | ✅ `&s_pwm.parent` 传给 `rt_device_pwm_register` |
| `s31_ipi_receive_side_ready` | ✅ `if (!...)` 读，合法 |

**⇒ 除 `s_sem_sent` 外没有第二个真问题。**

### 96b.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **33 passed, 0 failed**，连跑 3 次 |
| `s31test sem` | ✅ `500/500 tokens, 0 release failures` |
| `check_rtt_style.py --strict` | ✅ **22/22** |
| `check_image.py` | ✅ 9/9 |
| 第 23 项反向验证 | ✅ **RED**，无合法误报 |
| `provoked` 残留 | ✅ 0 |

已保存 `w86_verified.bin`。

### 96b.9 方法学

| 教训 | 说明 |
|---|---|
| **死状态看起来像检查时最危险** | `0x80000000 \| n` 明显是完成标志 |
| **测试演进会留下孤儿状态** | 从"等标志"改成"等计数"后，标志没人管了 |
| **剥注释再统计，否则注释里的变量名算"读"** | `s_sem_sent` 正是这样藏住的 |
| **取地址是读** | ops 表注册不能误报 |
| **反向验证要包含"不误报合法情形"** | 只验证会红不够 |

---

## 97. W87：`rhwh` 和 `dev` 的"子检查数"是写死的，跳过时是假数字

W86 清理死状态后，这一轮继续查"测试自身的可信度"，看**报文体里的数字**。

### 97.1 两处写死的分母

```c
rt_snprintf(detail, sizeof(detail), "%d sub-checks, %d bad", 5, detail_bad);  /* rhwh */
rt_snprintf(detail, sizeof(detail), "%d sub-checks, %d bad", 7, bad);         /* dev   */
```

**两个组都有跳过路径：**

| 跳过点 | 条件 | 跳过的后续检查 |
|---|---|---|
| `rhwh.iv` | 扫不到空闲 INUM | 后面 **4 项**不跑 |
| `dev.rc` | `pwm0` 被占用 | 后面 **4 项** ref-count 检查不跑 |

**跳过时汇总仍然写 "5 sub-checks" / "7 sub-checks"。**

**⇒ 数字是错的。**

### 97.2 改成"实际跑了几项"

```c
rt_snprintf(detail, sizeof(detail), "%d/%d sub-checks ran, %d bad", ran, 5, detail_bad);
```

**每个 `st_report` 前加 `ran++`。**

**rhwh 立刻暴露出正常情况：**

```
[PASS] rhwh   5/5 sub-checks ran, 0 bad
```

### 97.3 但 `dev` 报出 `5/7`

**这说明分母 7 和实际断言的条数不一致。**

查之后发现两件事：

**(a) `dev.rc` 在正常路径上根本不报告** —— 它只在 `pwm0` 缺失或被占用时说话。
**所以 7 个标签只有 6 个会报。**

修：加上正常路径的报告。

```
[PASS] dev.rc   pwm0 idle (ref=0), ref-count checks run
```

**(b) 仍然 6/7** —— 因为 `dev.ct` 的 `else` 分支漏了 `ran++`（前一个分支有，后一个没有）。

**这是我自己加计数器时漏的，而计数器本身把它抓出来了。**

修完：

```
[PASS] dev   7/7 sub-checks ran, 0 bad
```

### 97.4 自检总数从 33 变 34

因为 `dev.rc` 现在在正常路径上也报告，`s_pass` 多了一行：

```
=== 34 passed, 0 failed ===
```

**连跑 4 次全部 34。**

**这是"子检查更细"的结果，不是新增功能。**

### 97.5 为什么这个改动值得做

| 之前 | 之后 |
|---|---|
| `5 sub-checks, 0 bad`（永远是 5，不管跑了几项） | `5/5 sub-checks ran` 或 `1/5 ... skipped` |
| `7 sub-checks, 0 bad`（实际只跑 6 项） | `7/7 sub-checks ran` |
| 跳过不体现在输出里 | **跳过立刻可见** |

**这是整个检查器系列一直在追的同一个缺陷：一个看起来像测量值的数字，其实不是。**

### 97.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **34 passed, 0 failed**，连跑 4 次 |
| `s31test rhwh` | ✅ `5/5 sub-checks ran, 0 bad` |
| `s31test dev` | ✅ `7/7 sub-checks ran, 0 bad` |
| `check_rtt_style.py --strict` | ✅ 22/22 |
| `check_image.py` | ✅ 9/9 |
| 残留写死分母 | ✅ **0**（两处都改成了 `%d/%d`） |

已保存 `w87_verified.bin`。

### 97.7 方法学

| 教训 | 说明 |
|---|---|
| **报文里的数字也要审** | 不只是断言，报文体同样会说谎 |
| **写死的分母在跳过路径上必然是错的** | 改成"实际跑了几项" |
| **改完分母会暴露标签数与断言数不一致** | `dev.rc` 从来不报 |
| **自己加的计数器会抓到自己漏的地方** | `dev.ct` 的 else 分支 |
| **子检查变细会让总数变化** | 33 → 34，要确认这是预期的 |

---

## 98. W88：`st_clock` 是带判据的测试，但从来不在完整自检里

W87 查的是"报文里的假数字"。这一轮查反方向：**有没有测试存在、却不在套件里。**

### 98.1 方法：比对"定义的测试函数"与"st_all 调用的"

```python
defined = 所有 static int st_xxx(void)
called  = st_all 里调用的
```

**结果（第一次跑）看着很乱：**

```
defined but NOT called by st_all: ['st_clock']
called but not defined          : ['st_irq', 'st_nest', 'st_preempt']
```

**后三个是误报** —— 它们的签名带参数（`st_irq(rt_base_t)`），
我的正则只匹配 `(void)`。

**但 `st_clock` 是真发现。**

### 98.2 `st_clock` 确实是个测试

```c
static int st_clock(void)
{
    ...
    rt_uint64_t d_us = us1 - us0;
    int ok = (d_us >= 90000u && d_us <= 300000u);

    st_report("clock", ok, "os_tick says %u us, want 100000", (rt_uint32_t)d_us);
    return ok ? ST_PASS : ST_FAIL;
}
```

**它有判据、有 `st_report`、有返回值。**

**但 `st_all` 里没有它** ⇒ 它只能靠手打 `s31test clock` 运行。

**⇒ 例行跑"34 passed"时，这一整块是套件之外的。**

**不在套件里的测试 = 没人跑的测试。**

### 98.3 顺手修了报文里的错误归因

```
[PASS] clock    os_tick says 100000 us, want 100000
```

**但打印的 `d_us` 来自 `s31_systimer_us()`，不是 os_tick 原始计数器。**
（os_tick 原始计数在上一行单独打印。）

**改成：**

```
[PASS] clock    systimer_us says 100000 us over a 100 ms delay
                (window 90000..300000)
```

**另外把窗口写进报文** —— 原来只说 "want 100000"，但断言是 90000..300000。
**报文比断言严格，会让读者误判容差。**

### 98.4 加入 st_all 后暴露一个编译问题

`st_all` 在 `st_clock` 之前（2311 vs 2390 行），**必须先声明**。

```c
/* st_clock is defined further down; st_all runs before it. */
static int st_clock(void);
```

### 98.5 自检 34 → 35

```
=== 35 passed, 0 failed ===
```

**连跑 4 次全部 35。**

**这一条是真的新增覆盖**（一个带判据的测试进入例行运行），
与 W87 的"子检查变细"不同。

### 98.6 顺便看到 `clock` 的诊断输出仍然有用

```
clock: over ~100 ms
  os_tick  +1599999 counts = +99999 us @16MHz
  esptimer +0 counts                       <- unit 0 不计数（已知）
  ccount   +31731276 cycles @320MHz = 99160 us
[PASS] clock    systimer_us says 100000 us over a 100 ms delay (window 90000..300000)
```

**三路独立参考互相印证：99999 / 100000 / 99160 us。**

**ccount 那条偏低 0.84%（99160 vs 100000），因为它是 CPU 周期域，
和 `mdelay` 的调度粒度不完全对齐 —— 在 10% 窗口内，正常。**

### 98.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **35 passed, 0 failed**，连跑 4 次 |
| `s31test clock` | ✅ 三路参考一致 |
| `check_rtt_style.py --strict` | ✅ 22/22 |
| `check_image.py` | ✅ 9/9 |

已保存 `w88_verified.bin`。

### 98.8 方法学

| 教训 | 说明 |
|---|---|
| **"定义的测试"和"套件里的测试"要对比** | 差集里可能有真发现 |
| **正则只匹配 `(void)` 会漏带参函数** | 三个误报 |
| **不在套件里的测试 = 没人跑的测试** | 例行数字不包含它 |
| **报文比断言严格会误导读者** | 把窗口写进报文 |
| **加入顺序要看函数定义位置** | 需要前置声明 |

---

## 99. W89：`s31test` 的用法说明漏了 3 个子命令，其中最大的组完全没有说明

W87 查"报文里的假数字"，W88 查"测试在不在套件里"。这一轮查**用法说明与实际分派是否一致**。

### 99.1 比对结果

```python
dispatched = st_cmd 里 rt_strcmp(argv[1], "...") 的全部值
usage_line = "usage: s31test [...]" 里的名字
```

| | 数量 |
|---|---|
| 实际分派 | **12**（含 `all`） |
| 用法首行列出的 | **9** |
| **缺的** | `rhwh`、`dev`、**`fbshim`** |

**`rhwh` 和 `dev` 各自有一行说明，只是首行的方括号里没列。**

**`fbshim` 完全没有说明行** —— 而它是套件里**最大的一个组**（11 项子检查）。

```
msh > s31test bogus
usage: s31test [all|tick|clock|ctx|preempt|fpu|irq|nest|sem] [args]
  ...
  s31test dev          device framework contract: ...
                                                <- fbshim 到此为止
```

**⇒ 用户永远不知道 `s31test fbshim` 存在。**

### 99.2 同类的第三个实例

| 轮次 | 输出声称 | 实际 |
|---|---|---|
| W87 | "5 sub-checks" | 跳过时只跑 1 项 |
| W88 | "34 passed" | `st_clock` 不在套件里 |
| **W89** | 用法列 9 个 | **分派 12 个** |

**都是"输出声称的比代码做的少"。**

### 99.3 修法

```c
rt_kprintf("usage: s31test [all|tick|clock|ctx|preempt|fpu|irq|nest|sem|rhwh|dev|fbshim] [args]\n");
...
rt_kprintf("  s31test fbshim       FreeRTOS shim: queues, notifications, tasks, TLS, link check\n");
```

**验证：**

```
usage: s31test [all|tick|clock|ctx|preempt|fpu|irq|nest|sem|rhwh|dev|fbshim] [args]
  ...
  s31test dev          device framework contract: ref_count, open/close, control
  s31test fbshim       FreeRTOS shim: queues, notifications, tasks, TLS, link check
```

### 99.4 加第 24 项检查

**两边都在同一个 `.c` 文件里，都是纯文本 ⇒ 能做真正的反向验证。**

```
[PASS] s31test usage is complete    all 11 subcommands documented
```

### 99.5 双向反向验证

```
# 删掉 fbshim 的说明行
[FAIL] dispatched but not in the usage line: []; no description line: ['fbshim']

# 从首行括号里删掉 dev
[FAIL] dispatched but not in the usage line: ['dev']; no description line: []
```

**✅ 两个方向都会红，且分别指向不同的缺口。**

### 99.6 为什么这次能做而检查 20 不能

| | 检查 20（删） | 检查 24（留） |
|---|---|---|
| 比对双方 | 头文件 `#ifdef` ↔ `.c` 代码 | **同一文件的文本 ↔ 同一文件的文本** |
| 能构造真阳性 | ❌ | ✅ 两个方向都行 |

**判据仍然是：比对的两边都必须在扫描范围内。**

### 99.7 顺带确认一个旧疑点

之前我以为 `i2c.c:393 bus->parent.ops = &s31_i2c_ops;` 在
`RT_USING_DEVICE_OPS` 未定义时有问题。**这次查清了：**

```
struct s31_i2c           { struct rt_i2c_bus_device parent; ... }
struct rt_i2c_bus_device { struct rt_device parent; const struct rt_i2c_bus_device_ops *ops; }

bus->parent.ops  ==  rt_i2c_bus_device.ops     <- 与 DEVICE_OPS 无关
```

**不是缺陷。** 记下来，免得下次再疑。

### 99.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **35 passed, 0 failed**，连跑 3 次 |
| `s31test bogus` | ✅ 12 个名字 + 12 行说明 |
| `s31test fbshim` | ✅ 正常 |
| `check_rtt_style.py --strict` | ✅ **23/23** |
| `check_image.py` | ✅ 9/9 |
| 第 24 项双向反向验证 | ✅ **都红** |

已保存 `w89_verified.bin`。

### 99.9 方法学

| 教训 | 说明 |
|---|---|
| **用法说明也要与实际分派比对** | 漏 3 个，其中 1 个完全没有说明 |
| **最大的组最容易被漏** | `fbshim` 11 项却无说明 |
| **"输出比代码少"是同一类缺陷的第三次出现** | W87/W88/W89 |
| **两边同在一个文件才容易做可证伪的检查** | 检查 24 vs 检查 20 |
| **旧疑点要查清并记录** | `bus->parent.ops` 不是 `rt_device.ops` |

---

## 100. W90：FPU 失败信息用 `%f` 打印，而格式化器把 `%f` 吞掉了

继续 W89 的"输出与代码一致性"系列，这一轮看**命令描述与实际输出**。

### 100.1 先复核 `%f` 的结论

`rt_vsnprintf_tiny.c` 是本端口使用的 klibc 格式化器：

```c
case 'e':
case 'E':
case 'G':
case 'g':
case 'f':
case 'F':
    va_arg(args, double);          /* 只消费参数 */
default:
    ... 打印转换符本身 ...
```

**⇒ `%f` 不输出数字。**

### 100.2 PASS 路径早就修过，FAIL 路径漏了

```c
/* PASS 路径 —— 已修，注释也写了原因 */
rt_kprintf("FPU ok seed=%d/10\n", (int)(seed * 10.0f));

/* FAIL 路径 —— 没修 */
rt_kprintf("FPU FAIL at f[%d]: %f != %f\n", i, f[i], seed + (float)i);
```

**真实 FPU 失败时会打印：**

```
FPU FAIL at f[3]:  != 
```

**⇒ 这条消息存在的唯一目的就是显示两个值，而两个值都不见了。**

**这是最糟的位置：失败是罕见的，所以没人会发现，直到它真的发生。**

### 100.3 修法：打印 IEEE-754 位模式

```c
union { float f; rt_uint32_t u; } got, want;

got.f  = f[i];
want.f = seed + (float)i;
rt_kprintf("FPU FAIL at f[%d]: got 0x%08x, want 0x%08x\n",
           i, (unsigned)got.u, (unsigned)want.u);
```

**位模式是精确的、整数格式化器能打印、而且"位不同"正是这个测试检测的东西。**

### 100.4 反向验证：强制一次失配

```c
if (i == 3 || f[i] != seed + (float)i)   /* provoked */
```

```
FPU FAIL at f[3]: got 0x40800000, want 0x40800000
FPU FAIL at f[3]: got 0x42ce0000, want 0x42ce0000
```

**✅ 两个值都出现了。**

**（两个线程各打一行，`got == want` 因为值是相等的 —— 是强制条件触发的，不是真失配。这说明消息格式正确。）**

**恢复后：`provoked` 标记 0，35/35。**

### 100.5 顺带修 `s31stat` 的描述

```
修前: show tick and thread counters
```

**实际打印：tick、**两个堆**、两个演示线程计数、**CPU1 状态**。**

**"thread counters" 勉强能算 counter_a/b 的转述，但完全没提两个堆和 CPU1 ——
而这两项才是一个 bring-up 镜像最需要看的。**

```
修后: tick + both heaps + demo-thread counters + CPU1 state
```

### 100.6 又踩了一次"描述里的逗号"

第一次改写成 `tick, both heaps, ...` —— **编译失败：**

```
error: expected '=', ',', ';' ... before '-' token
```

**原因：`MSH_CMD_EXPORT_ALIAS` 是变参宏，描述里的逗号被当成参数分隔符。**

**这正是 W77 记录过的问题，我自己又犯了一次。**

改成 `+` 连接。

**⇒ 这条规则应该进检查，而不只是进笔记。**

### 100.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **35 passed, 0 failed**，连跑 3 次 |
| `s31fpu` | ✅ `FPU ok seed=10/10` / `seed=1000/10` |
| `s31fpu` 强制失配 | ✅ 打印两处 `got 0x... want 0x...` |
| `help` 里的 s31stat | ✅ `tick + both heaps + demo-thread counters + CPU1 state` |
| `check_rtt_style.py --strict` | ✅ 23/23 |
| `check_image.py` | ✅ 9/9 |
| `provoked` 残留 | ✅ 0 |

已保存 `w90_verified.bin`。

### 100.8 方法学

| 教训 | 说明 |
|---|---|
| **罕见失败路径的消息最需要审** | 平时看不到，出错时也没信息 |
| **PASS 路径修了不等于 FAIL 路径修了** | 同一处两个分支 |
| **位模式是浮点值的精确表示** | 整数格式化器能打印 |
| **同一条规则我犯了两次** | 描述里的逗号 → 应该进检查 |
| **命令描述要覆盖它实际打印的东西** | `s31stat` 漏了两个堆和 CPU1 |

---

## 101. W91：手册 §0"务必先读"那张表，六项全过时

前几轮查的是**代码与输出**的一致性。这一轮把同一把尺子用在**文档**上。

### 101.1 实测 vs 手册 §0

| 项 | 手册写的 | 实测 |  |
|---|---|---|---|
| 编译产物 | 898,688 B | **1,517,568 B**（`0x172800`） | ❌ |
| 应用分区 | 1 MB，剩 147 KB | **4 MB**，已用 36%，剩 2.55 MB | ❌ |
| 自检 | 20/20 | **35/35** | ❌ |
| 核数 | 单核 | **双核**（`s31smp`: cpus 2） | ❌ |
| flash | 2 MB | **16 MB**（`CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`） | ❌ |
| 校验器 | 只有 `check_image.py` 九项 | **9 项 + `check_rtt_style.py` 24 项** | ❌ |

**六项全错，而这一节叫"务必先读"。**

### 101.2 为什么这比"没写"更糟

**信它的人**会按 1 MB 分区规划、以为只剩 147 KB、以为单核；
**查它的人**一旦发现第一行就错，就不会再信后面的任何一行。

**一份"先读"的表错了，等于整份手册都要重新核对。**

### 101.3 修法：数字必须带复测命令

| 项 | 状态（2026-11-01 实测） | 怎么复测 |
|---|---|---|
| ELF 级校验 | ✅ 9/9 | `python tools\check_image.py` |
| 静态规范校验 | ✅ 24/24 | `python tools\check_rtt_style.py --strict` |
| 编译产物 | 1,517,568 B（`0x172800`，1.45 MB） | `(Get-Item build\s31_rtt_test.bin).Length` |
| 应用分区 | 4 MB，已用 36%，剩 2.55 MB | `python tools\check_image.py` |
| 运行时验证 | ✅ 35/35 | `python tools\board.py COM16 "s31test"` |
| 硬件 | 16 MB flash / 双核 | `s31smp`、`s31stat` |

**并在表下留一行：**

> 这张表曾经写着"898,688 B / 1 MB 分区 / 剩 147 KB / 20 项自检 / 单核 / 2 MB flash"——
> **六项全过时**。数字要么带复测命令，要么不写。

### 101.4 加第 26 项检查：手册里的产物大小必须与磁盘一致

**只有"二进制大小"这一项能离线验证**，所以就只断言这一项。

```
[PASS] runbook binary size is current    section 0 quotes 1,517,568 B, matches the artifact
```

**反向验证**（把旧数字 898,688 填回去）：

```
[FAIL] section 0 says 898,688 B, artifact is 1,517,568 B
```

**✅ 会红。**

**目的不是覆盖所有数字，而是让"至少有一个数字被盯着"——
这样过时的表不可能再一次悄悄通过。**

### 101.5 ⚠️ 区分"当前状态"和"历史记录"

移植计划 `§12.5` 里有另一张表：

```
当前状态（2026-10-27）
| 镜像 | 193,120 B | 分区剩余 82% | msh 命令 | s31test、s31stat、s31fpu、pin |
```

**它也是过时的 —— 但它是历史记录**（带日期、带提交号、在"第五批增量"小节里）。

**历史数据保持原样是正确的：改了反而破坏记录的可信度。**

**⇒ 只有"当前状态 / 务必先读"这类章节才必须与实测同步。**

### 101.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **35 passed, 0 failed**，连跑 3 次 |
| `check_rtt_style.py --strict` | ✅ **25/25** |
| `check_image.py` | ✅ 9/9 |
| 手册 §0 | ✅ 六项全部更新，附复测命令 |
| 第 26 项反向验证 | ✅ **RED** |
| 输出编码 | ✅ 改成纯 ASCII（`section 0` 而非 `§0`） |

已保存 `w91_verified.bin`。

### 101.7 方法学

| 教训 | 说明 |
|---|---|
| **文档也是交付物，要按同一标准审** | 前几轮只审代码 |
| **"务必先读"的表错了比没写更糟** | 它会污染整份文档的可信度 |
| **数字要么带复测命令，要么不写** | 否则无法判断是否过时 |
| **只断言可离线验证的那一项** | 其余交给每轮实测 |
| **区分"当前状态"与"历史记录"** | 历史表过时是对的 |

---

## 102. W92：手册"已知限制"表里有两条已经不是限制了

W91 修了手册 §0 的状态表。这一轮继续查文档，发现 §8"已知限制"里**两条已经不成立**。

### 102.1 逐行核对 §8

| 限制 | 是否仍成立 |
|---|---|
| 波特率固定 115200 | ✅ |
| `rt_kprintf` 走 ROM putc | ✅ |
| TX 是忙等 | ✅ |
| GPIO 只用中断通道 0 | ✅ |
| UART0 / GPIO 默认 1 级 | ✅ |
| **无硬件看门狗** | ❌ **中断看门狗已启动** |
| **Wi-Fi / BLE 未适配** | ❌ **两者都能跑** |

### 102.2 第一条：中断看门狗早就启动了

手册写：

> 无硬件看门狗 | INT/TASK WDT 的初始化都在 FreeRTOS 专属路径（`app_startup.c`），
> RT-Thread 路径不执行 | P3 把喂狗挂到 idle 线程

**实际（W69/W70 做的）：**

```c
/* port/esp32s31/boot_tasks.c */
esp_int_wdt_init();
esp_int_wdt_cpu_init();
```

**两个符号在镜像里：`0x400982b2` / `0x4009836e`。**
**tick 由 `port_systick.c` 的 `esp_vApplicationTickHook()` 喂。**

**改成分两行，把两件事说清：**

| 限制 | 原因 |
|---|---|
| **任务**看门狗未启动 | 无任何 `esp_task_wdt_add()` 订阅者（全树搜索只剩 test_apps）——启动一个没人订阅的看门狗只增加失败模式 |
| **中断**看门狗已启动 | `boot_tasks.c` 显式 init，tick 喂它 |

### 102.3 第二条：Wi-Fi / BLE 都能跑

手册写：

> Wi-Fi / BLE 未适配 | `esp_adapter.c` 831 行、约 60 个 osi 函数指针全部基于 FreeRTOS 原语

**实测（本轮）：**

```
msh > s31wifi
s31wifi: bringing up esp_wifi on the FreeRTOS shim
  nvs_flash_init             ok
  esp_netif_init             ok
  event loop create default  ok
  create default wifi sta    ok
  event handler register     ok
I (7024) wifi:wifi driver task: 2f042ce0, prio:23, stack:6656, core=0

msh > s31ble
s31ble: bringing up the BLE controller on the FreeRTOS shim
  nvs_flash_init               ok
I (7012) BTDM_INIT: BTDM controller init OK
```

**改成：**

| 限制 | 原因 |
|---|---|
| Wi-Fi / BLE **已能跑在兼容层上**，但无连接测试 | `s31wifi` 起驱动、`s31ble` 起控制器；连热点需要 `s31wifi <ssid> <pass>`，未实现 |
| Wi-Fi 无 5 GHz | `SOC_WIFI_SUPPORT_5G` 只在 esp32c5 定义，S31 是 2.4 GHz Wi-Fi 6 |

### 102.4 顺带：手册有两个 `## 8`

```
389: ## 8. 已知限制（不是 bug，是设计取舍）
403: ## 8.5 打开 FreeRTOS 兼容层（Wi-Fi 的前置条件）
```

**8.5 夹在 8 和 9 之间、编号是 8.5 —— 它本来就是 8 的子节，错的是标题级别。**

**文档里已有 `### 6.8b`、`### 6.8c` 这种先例，所以改成 `###` 是一致的。**

### 102.5 还有：日志的头部提交号落后 100+ 轮

```
日志头部：提交 84439686
实际 HEAD：f40c14dd
```

**这是读者看到的第一行，而它指向的提交已经不存在于讨论里。**

**改成：**

```
> 分支：esp-idf-rtt → rtt-s31，当前提交 `f40c14dd`（git -C esp-idf-rtt rev-parse --short HEAD）
> 首次构建时间：2026-10-26
> 当前状态（2026-11-01 实测）：自检 35/35、check_rtt_style.py 25/25、check_image.py 9/9；
>   上游 rt-thread 工作区零修改
> 首次上板：2026-10-30（COM16），修 6 个启动缺陷后走到应用启动，见 §22
```

**"首次"和"当前"分成两行 —— 历史事实保留，当前状态可复测。**

### 102.6 加第 27 项检查

**两项都可离线验证：**

```
[PASS] doc section numbers unique      no duplicate top-level section numbers
[PASS] log header quotes current HEAD  header says f40c14dd, HEAD is f40c14dd
```

**双向反向验证：**

```
# 把 8.5 改回 ## 级别
[FAIL] ['runbook has two sections numbered 8']

# 把日志头部改回旧提交
[FAIL] header says 84439686, HEAD is f40c14dd
```

**✅ 两个方向都会红。**

### 102.7 自检项数为什么没变

**本轮只改文档，没改固件。`s31test` 仍是 35/35。**
`check_rtt_style.py` 从 25 项变 27 项（新增两项文档检查）。

### 102.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **35 passed, 0 failed**，连跑 3 次 |
| `check_rtt_style.py --strict` | ✅ **27/27** |
| `check_image.py` | ✅ 9/9 |
| 手册 §8 | ✅ 九行逐条核对，两条更正 |
| 手册章节号 | ✅ 89 个，无重复 |
| 日志头部 | ✅ 当前提交 + 当前状态 |
| 上游 `rt-thread` | ✅ **零修改**（`git status --porcelain` 空） |

已保存 `w92_verified.bin`。

### 102.9 方法学

| 教训 | 说明 |
|---|---|
| **"已知限制"表会变成"已知过时"** | 限制被解决后没人回头改 |
| **逐行核对，不要整体相信** | 7 行里 2 行错 |
| **区分"当前状态"与"首次/历史"** | 分两行写，历史保留 |
| **章节号重复让交叉引用失效** | `§8` 指向两个地方 |
| **读者看到的第一行最要紧** | 日志头部的提交号落后 100+ 轮 |
| **只断言可离线验证的** | 自检数交给每轮实测 |

---

## 103. W93：PCNT 绕过了 RT-Thread 现成的设备类 —— 改成真正的 `rt_device`

这是本轮目标最直接的命中：**端口写着"RT-Thread 没有这个类"，而它其实有。**

### 103.1 被推翻的断言

```
pcnt.c:4        * RT-Thread has no pulse-counter class, so this is a plain C API plus an msh
port_pcnt.h:7   * RT-Thread does not model pulse counters, so there is no device class to plug
                * into and no `rt_pcnt_*` API to satisfy.
```

**实际（5.4.0 树里）：**

```
components/drivers/include/drivers/pulse_encoder.h
components/drivers/misc/pulse_encoder.c            (135 行)
Kconfig: config RT_USING_PULSE_ENCODER             (default n)
```

**而且 `pulse_encoder.c` 里有：**

```c
#ifdef RT_USING_DEVICE_OPS
    device->ops = &pulse_encoder_ops;
#else
    device->init  = rt_pulse_encoder_init;    /* <- 本端口的配置 */
    ...
#endif
```

**⇒ 这个类正好为这种配置准备了分支，而端口却自己写了一套 C API。**

### 103.2 先修 PCNT 的一个真 bug：输出路径只配了三分之一

改设备类之前先跑 `s31pcnt`，发现：

```
pin 4,5,6,7      -> counted 20   PASS
pin 8..15, 50    -> counted 0    FAIL
pin 16..20,30,40 -> counted 20   PASS
```

**同一个代码、同样 2 us 脉冲，按引脚分段成败。**

`cmd_pcnt` 只做了一件事：

```c
gpio_ll_output_enable(&GPIO, (uint8_t)pin);
```

而 `s31_pin_mode(PIN_MODE_OUTPUT)` 做三件：

```c
gpio_ll_set_output_signal_matrix_source(&GPIO, pin, SIG_GPIO_OUT_IDX, false);
gpio_ll_set_output_enable_ctrl(&GPIO, (uint8_t)pin, false, false);
gpio_ll_output_enable(&GPIO, pin);
```

**少的两步决定"电平寄存器写下去能不能到达焊盘"。**
哪些脚恰好能工作，取决于它们的输出多路器复位状态 —— **不是代码的性质。**

**修法不是补两行 LL，而是改用 `rt_pin_mode()`**（端口已经注册好的 pin 设备）。
这样既去掉重复，又让测试顺带验证 `s31_pin_mode()`。

**失败信息也改了**：原来一律说"边沿太快，减少数量"，而 `counted == 0`
指向的是输出路径，不是速率。**这条消息把人带偏了一整轮。**

```
修后：pin 4,8,10,11,14,15,16,50,60 -> 全部 counted 20
```

### 103.3 然后把它做成真正的设备

**读框架源码确定契约**（不猜）：

```c
case PULSE_ENCODER_CMD_CLEAR_COUNT:
    result = ops->clear_count(...);      /* 没有 NULL 检查 */
case PULSE_ENCODER_CMD_ENABLE/DISABLE:
    result = ops->control(...);          /* 没有 NULL 检查 */

rt_pulse_encoder_init():  if (ops->init)  /* 只有这里检查 NULL */

rt_pulse_encoder_read():  *(rt_int32_t *)buffer = ops->get_count(...);
                          return 1;       /* 完全不看 size 参数 */
```

**⇒ `init` 可以为 NULL，但 `clear_count` 和 `control` 必须是真函数；
`read` 无条件写 4 字节，不管调用者传的 size 是多少。**

**按这个契约注册 `pcnt0`**（unit 0，只注册一个 —— 四个设备会暗示四个已验证的计数器）：

```c
static struct rt_pulse_encoder_device s31_pe_dev;
static const struct rt_pulse_encoder_ops s31_pe_ops = { ... };
return rt_device_pulse_encoder_register(&s31_pe_dev, "pcnt0", RT_NULL);
```

`rt_hw_pcnt_*` 保留为薄封装，因为 `mcpwm.c` 和 `rmt.c` 把 PCNT 当**量具**用，
从量具里调设备 API 是多余的间接。

### 103.4 验证

**设备出现了：**

```
pcnt0        Miscellaneous Device 0
pwm0         PWM Device           0
pin          Pin Device           0
uart0        Character Device     1
```

**新增 `dev.pe` 子检查**（走完整设备路径）：

```c
rt_device_t pe = rt_device_find("pcnt0");
rt_device_open(pe, RT_DEVICE_OFLAG_RDONLY);
rt_device_read(pe, 0, &before, 1);
rt_device_control(pe, PULSE_ENCODER_CMD_CLEAR_COUNT, RT_NULL);
rt_device_read(pe, 0, &after, 1);
```

```
[PASS] dev.pe   pcnt0 read/clear via the device API (was 0)
```

**自检 35 → 36 项，`dev` 组 7 → 8 项。**

### 103.5 两个检查器当场抓到我的疏忽

**(a) `no comma in cmd descriptions`** —— 没有触发，但 `dev.pe` 的 `ran++` 我漏了。

**(b) `runbook binary size is current`** —— 连续红了两次：

```
[FAIL] section 0 says 1,517,568 B, artifact is 1,518,528 B
[FAIL] section 0 says 1,518,528 B, artifact is 1,518,560 B
```

**第一次是我加 PCNT 设备代码，第二次是我给 `dev.pe` 补 `ran++`。**
**每次改固件它都会红 —— 这正是它该做的。**

**顺带发现 `dev` 组报 `7/8`** —— 因为 `dev.pe` 有 4 个 `st_report` 但一个 `ran++` 都没有。
**W87 建立的计数器把 W93 自己的疏忽抓了出来。**

### 103.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `s31test dev` | ✅ **8/8 sub-checks ran, 0 bad** |
| `list device` | ✅ 9 个设备，含 `pcnt0` |
| `s31pcnt` 9 个引脚 | ✅ **全部 counted 20** |
| `s31mcpwm` | ✅ PASS（用 PCNT 当量具） |
| `s31rmt` | ✅ PASS（同上） |
| `check_rtt_style.py --strict` | ✅ **27/27** |
| `check_image.py` | ✅ 9/9 |

已保存 `w93_verified.bin`。

### 103.7 方法学

| 教训 | 说明 |
|---|---|
| **"上游没有这个类"是最该查的断言** | 一查就假 |
| **绕过的代价：自己写一套，还得自己维护** | 而且少了框架的契约检查 |
| **失败信息指错方向会浪费整轮** | `counted == 0` 不是"太快" |
| **改设备类之前先修功能 bug** | 否则设备类包着一个坏的实现 |
| **框架契约要读源码，不猜** | `read` 不看 size、`clear_count` 不查 NULL |
| **自己加的计数器会抓到自己** | `dev.pe` 漏 `ran++` → `7/8` |

---

## 104. W94：`clock_time` 的"代码没了"也是假的 —— 但它确实不该用

W93 命中了 `pcnt.c` 的假断言。这一轮查剩下三条"RT-Thread 没有 X 类"。

### 104.1 三条断言的核实结果

| 驱动 | 断言 | 核实 |
|---|---|---|
| `rmt.c` | "RT-Thread has no RMT class" | ✅ **成立**（树里无 RMT 类头文件） |
| `mcpwm.c` | "RT-Thread has no motor-control class" | ✅ **成立** |
| `gptimer.c` | "5.x has no hardware-timer class any more — **the code is gone**" | ❌ **假** |

**`clock_time/` 有 1294 行：**

```
clock_time_core.c    232
clock_timer.c        527
clock_hrtimer.c      457
clock_boottime.c      78
arch/risc-v/cputimer.c
arch/aarch64/cputimer.c
```

**Kconfig: `menuconfig RT_USING_CLOCK_TIME`（default n）**

### 104.2 先做了一次实验：它能编吗？

**加宏 + 加两个源文件（core + timer）：**

```
Project build complete.
```

**能编。但镜像里没有符号 —— `--gc-sections` 全丢了（没人引用）。**
**这是预期，说明"能不能编"不等于"适不适合用"。**

### 104.3 读源码之后：模型不匹配

**`clock_timer.c` 的模型：**

```
ops->start(timer, cnt, mode)   数 cnt 个 tick（timer 自己的 freq）
ISR: overflow++, cycles--, 归零 -> stop(oneshot) + rt_clock_time_event_isr()
通知: timer->parent.rx_indicate(&parent, sizeof(rt_clock_timerval_t))
没有 per-timer 回调，没有 per-timer 参数
```

**而且整个子系统是单属主的：**

```c
static rt_clock_timer_t *_clock_timer_owner = RT_NULL;
...
if (caps && _clock_timer_owner == RT_NULL)   /* 只认第一个 */
```

**本端口的 TIMG 驱动：**

```
s31_timer_start(idx, freq_hz, cb, arg)   4 个独立通道，各有 cb 和 arg
                                          按 Hz 启动，周期性跑着
```

**⇒ 不是同一件事。**

| | `clock_time` | 本端口 TIMG |
|---|---|---|
| 用途 | 系统级**单点截止时间** | 4 个独立周期回调 |
| 参数 | `cnt` 个 tick | 频率 Hz |
| 回调 | 无（走 rx_indicate + 全局 event） | **每通道独立** |
| 实例数 | **1（单属主）** | 4 |

**如果强行把某个 TIMG 注册成 `clock_time` 的属主，会改变整个系统里
`rt_clock_time_set_timeout()` 的语义 —— 没有任何调用者要求这么做。**

### 104.4 所以：断言一半对一半错，修的是**说清哪一半**

**已回滚实验（宏 + 两个源文件）。注释改成：**

```
 * There is no general "hardware timer" device class to register into, but the reason
 * matters - an earlier version of this comment said "the code is gone", which is false.
 *
 * components/drivers/clock_time/ exists (1294 lines ...) behind RT_USING_CLOCK_TIME.
 * It is not a general timer class though: clock_timer.c implements ONE-SHOT/PERIOD
 * *deadlines* against a single system-wide clock. ... It is also single-owner by
 * design ...
 *
 * This driver is a different thing: four independent channels, each with its own
 * callback and argument ...
```

**"代码没了"是假的；"没有通用的硬件定时器设备类"是真的。**

### 104.5 这一轮的核心判断：**能编 ≠ 该用**

W93 的 PCNT 是**该用而没用**（框架的模型完全匹配）；
W94 的 clock_time 是**不该用**（模型不匹配，强行用会改变系统语义）。

**同一个"绕过了上游"的表象，两种相反的结论。**
**判据是模型是否匹配，不是"上游有没有"。**

### 104.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `s31timer 0 100` | ✅ `want 100 Hz, got 99 Hz` PASS |
| `check_rtt_style.py --strict` | ✅ 27/27 |
| `check_image.py` | ✅ 9/9 |
| `clock_time` 源码残留 | ✅ **0**（宏和两个源文件都已回滚） |
| `rmt.c` / `mcpwm.c` 断言 | ✅ **成立，不改** |

已保存 `w94_verified.bin`。

### 104.7 方法学

| 教训 | 说明 |
|---|---|
| **"代码没了"和"没有这个类"是两种断言** | 前者可查文件，后者要查语义 |
| **能编 ≠ 该用** | 编译通过只说明语法兼容 |
| **模型匹配才是判据** | 单属主 deadline vs 多通道周期回调 |
| **强行套用会改变系统语义** | 注册属主 = 重新定义全局 timeout |
| **同一个表象可以有相反结论** | W93 该用，W94 不该用 |

---

## 105. W95：一个 193 行的命令在磁盘上活了 20 多轮，从没编进镜像

这一轮查的是"**磁盘上的源文件和实际编译的文件**"的差集。

### 105.1 差集只有一个文件

```
NOT COMPILED: s31-rtt-test/main/s31wifiscan.c
```

**全文 193 行，是一个完整的 msh 命令，`help` 里从来没有它。**

### 105.2 它是怎么消失的

W59/W60 的回归二分里：

| 去掉什么 | 结果 |
|---|---|
| tsens.c + 相关 CMake | ❌ 仍不执行 |
| **再去掉 s31wifiscan.c** | ✅ **version 行和 msh > 都回来了** |

**看起来是它。**

**但同一节的下一个标题就是：**

> ### 70.5 但真相不是 s31wifiscan

**真正的原因是 CPU1 调 `rt_system_scheduler_start()` 的竞态**
（线索是 `s31smp` 显示 `cpu1 cur_thread = 0`）。

**⇒ 它被移除的理由后来被推翻了，但文件没有放回去。**

**一个能用的诊断工具就这样死了 20 多轮，而没有任何检查发现。**

### 105.3 它回答的问题现在仍然重要

```
msh > s31wifiscan
s31wifiscan: 18 AP(s), all on channels 1..14 = the 2.4 GHz band
```

**`s31wifi` 只把协议栈起起来、报到 STA_START 就停，它不扫描。**
**"这块板能不能看见我要连的那个 AP" 只有 `s31wifiscan` 能回答 —— 这是 M6 的原始问题。**

### 105.4 放回构建

`main/CMakeLists.txt` 已经是 `WHOLE_ARCHIVE`（注释解释了为什么：
`cmd_wifi()` 无人引用，链接器会把 `s31wifi.c.obj` 丢掉）。
**同样的道理适用于 `cmd_wifiscan()`，而注释里写的"只有两个"要改成三个。**

**并在注释里写明它是被误判移除的。**

### 105.5 加第 28 项检查

**这是第 9 项"命令是否留在镜像里"往前追问一步：**

```
第 9 项  : 这个文件被保留在镜像里吗（靠锚点或 -u）
第 28 项 : 这个文件被编译了吗
```

**第 28 项才是能抓到 `s31wifiscan` 的那个问题。**

只查端口和 app，不查 `rt-thread`（内核有几百个源文件，只有一部分该编）。

```
[PASS] every port source is compiled    every .c in port/ and main/ is in the build
```

**反向验证**（从编译数据库里删掉它的条目）：

```
[FAIL] on disk but not in the build: ['s31wifiscan.c']
```

**✅ 会红。**

### 105.6 命令数 33 → 34

```
[PASS] __fsymtab non-empty    544 B = 34 commands (entry 16 B, from DWARF)
```

**544 - 528 = 16 B，正好一条。**

### 105.7 检查 26 又一次当场红

```
[FAIL] section 0 says 1,518,560 B, artifact is 1,525,744 B
```

**加回 193 行代码，镜像长了 7 KB。** 更新后 28/28。

### 105.8 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `s31wifiscan` | ✅ **18 AP(s)，全在信道 1..14** |
| `s31wifi` / `s31ble` | ✅ 正常 |
| `check_rtt_style.py --strict` | ✅ **28/28** |
| `check_image.py` | ✅ 9/9，**命令数 34** |
| 第 28 项反向验证 | ✅ **RED** |

已保存 `w95_verified.bin`。

### 105.9 方法学

| 教训 | 说明 |
|---|---|
| **"磁盘上有 ≠ 编进去了"** | 差集要按编译数据库做，不是按目录列 |
| **二分法的"去掉它就好了"不等于"它是原因"** | 同一次二分里的结论会被后一节推翻 |
| **推翻结论后要回头收拾** | 文件没放回去，没人发现 |
| **能用的诊断工具死了比没有更糟** | 你以为有，其实没有 |
| **检查要分层** | "编译了吗"和"留在镜像里吗"是两个问题 |

---

## 106. W96：板子停在 `waiting for download` —— 是自动复位电路，不是固件

### 106.1 现象与最初的误判方向

重新上电后板子停在 `waiting for download`，`board.py` 所有命令都报
`boot log did not reach the banner`。

**第一反应是"刚才 W95 加回 193 行代码把镜像搞坏了"。**
**这个方向是错的 —— 下面每一步都证明镜像没问题。**

### 106.2 关键一步：让 esptool 说话

```
esptool --chip esp32s31 -p COM16 chip-id
Connected to ESP32-S31 on COM16:
Chip type:          ESP32-S31 (revision v0.0)
Features:           Wi-Fi 6, BT 5.4 (LE), IEEE802.15.4, Dual Core + LP Core
MAC:                1c:29:04:d0:14:1e
```

**芯片能应答、能进 stub、能硬复位 ⇒ 芯片、USB 串口、供电都正常。**

### 106.3 真正的复位原因

**关掉串口 3 秒，再打开并写一个字节：**

```
rst:0x1 (POWERON),boot:0x58 (SPI_FAST_FLASH_BOOT)
I (26) boot: ESP-IDF f40c14dd-dirty 2nd stage bootloader
...
RT-Thread 5.4.0 on ESP32-S31
tick=1000 Hz, cpus=2, msh on UART0
msh >
```

**`boot:0x58 SPI_FAST_FLASH_BOOT` = 正常 flash 启动，不是下载模式。**

**⇒ 之前那个 "waiting for download" 是**上一次**留下的：串口关闭时 DTR/RTS 都是低，
经反相缓冲正好是 "IO0 低 + EN 释放"，于是**上电就进 ROM 下载模式**。**

**这解释了为什么"只读不写"永远 0 字节：ROM 在等一个同步帧，不会主动说话。**

### 106.4 第二个坑：写太快会复位

**拿到 prompt 之后发命令，回来的仍然是一份新启动日志。**
**做了间隔扫描（同一个命令，只改字符间隔）：**

| 间隔 | 结果 |
|---|---|
| 0 / 2 / 5 / 10 ms | 3990 B，全是启动日志 |
| 20 ms | 4410 B，仍是启动日志 |
| **40 ms** | **185 B，命令执行** |

**⇒ 连续写会扰动 EN。固件改不了，这是 CH343 自动复位电路的性质。**

**这一步推翻了我之前的一个说法**：不是"板子坏了"，也不是"固件坏了"，
**是"发得太快"。**

### 106.5 `board.py` 的两处修正

**(a) 逐字符 45 ms 发送**

```python
CHAR_GAP = 0.045
def type_line(line):
    for ch in line.encode() + b"\r\n":
        sp.write(bytes([ch])); sp.flush(); time.sleep(CHAR_GAP)
```

**(b) 回复是启动日志时明确报告**

```
--- the board rebooted instead of running 's31test' ---
    (see CHAR_GAP in this file: bytes sent too fast reset this board)
```

**前面几轮我被"输出里有 `msh >` 就当作成功"误导过。现在这条判据写进工具里了。**

### 106.6 还有一个自己挖的坑

**第一版唤醒循环用**累积文本**判断就绪：**

```python
text += window
if "msh >" in text:      # 错：第一次的启动日志里就有它
    break
```

**结果：循环立刻退出，命令发进一个还在启动的板子。**

**改成只看**这一次**窗口，并且窗口里不能有 `ESP-ROM`：**

```python
if "msh >" in window and "ESP-ROM" not in window:
    ready = True
```

**"就绪"是"现在能接受输入"，不是"曾经打印过提示符"。**

### 106.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `version` | ✅ |
| `s31stat` | ✅ tick=7780, counter a=8 b=6, heap, cpu1 |
| `list device` | ✅ 含 `pcnt0` |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |

### 106.8 方法学

| 教训 | 说明 |
|---|---|
| **"上电不启动"先怀疑线路，不要先怀疑刚改的代码** | 我刚改过固件，所以误判 |
| **让独立的工具说话** | `esptool` 能连 = 芯片正常，一步排除一大片 |
| **"0 字节"和"有输出"是两种故障** | ROM 等待时不说话 |
| **恢复后仍要解释"为什么一开始错"** | 是写太快，不是镜像坏 |
| **"曾经打印过提示符" ≠ "现在能接受输入"** | 就绪判据必须只看当前窗口 |
| **把判据写进工具，不只写进笔记** | 否则下次又会被同一现象误导 |

---

## 107. W97：掉电重启进 DOWNLOAD 是电路空闲态；新增 `tools/reset.py`

### 107.1 用户报告

```
rst:0x1 (POWERON),boot:0x48 (DOWNLOAD(USB/UART0/SPI))
waiting for download
```

**而且掉电重启也一样。用户要的是"复位然后看启动日志"。**

### 107.2 `boot:0x48` vs `boot:0x58`

**ROM 自己会把启动模式打出来 —— 这就是现成的量具：**

| 值 | 含义 |
|---|---|
| `boot:0x48` | `DOWNLOAD(USB/UART0/SPI)` —— IO0 低 |
| `boot:0x58` | `SPI_FAST_FLASH_BOOT` —— IO0 高，正常启动 |

**（还见过 `0x78` / `0x79`，同样是正常 flash 启动，高位是 SPI 模式位。）**

### 107.3 原因确认

**CH343 的 DTR/RTS 经两个反相缓冲接到 EN 和 IO0。
串口没打开时驱动把两根线放在低电平，反相后就是 "IO0 低 + EN 释放"
⇒ 上电即进下载模式。**

**这不是"我上一轮改坏了镜像"——`esptool` 能读 MAC、能进 stub，芯片完全正常。**

### 107.4 ✅ 解决：把两根线置高再复位

**新增 `tools/reset.py`：**

```python
sp.dtr = True; sp.rts = True     # 两个都高 = IO0 高 + EN 释放
time.sleep(0.3)
sp.dtr = False                    # 拉低 EN
time.sleep(0.25)
sp.dtr = True                     # 释放 -> 正常启动
# 然后流式打印 N 秒启动日志
```

**实测三次：**

```
rst:0x1 (POWERON),boot:0x58 (SPI_FAST_FLASH_BOOT) | RT-Thread 5.4.0 | 3990 bytes
rst:0x1 (POWERON),boot:0x78 (SPI_FAST_FLASH_BOOT) | RT-Thread 5.4.0 | 3990 bytes
rst:0x1 (POWERON),boot:0x79 (SPI_FAST_FLASH_BOOT) | RT-Thread 5.4.0 | 3990 bytes
```

**`0x58` / `0x78` / `0x79` 都是**正常 flash 启动**（高位是 SPI 模式/线宽位），
关键是**不是 `0x48`**。**

### 107.5 ⚠️ 单字节唤不醒它

**给 reset.py 写"静默时补一枪"的逻辑时，发现：**

| 发送 | 结果 |
|---|---|
| `\x00`（1 字节） | 0 B |
| `\x00\x00`（2 字节） | **3990 B** 完整启动 |
| `\r`（1 字节） | 7 B（残片） |
| `\r\n`（2 字节） | **3991 B** 完整启动 |

**⇒ 不是"特定字节"，是"至少两个字节"。**
**大概是 CH343 在打开后第一个字节上正在稳定线驱动器，把它丢了。**

**所以唤醒发 `\r\n`。**

### 107.6 手动兜底

**按住 BOOT（IO0）→ 短按 RST → 松开 BOOT。**
**这是电气上等价于脚本做的事，脚本不灵时用它。**

**反过来（按住 BOOT 上电）才进下载模式。**

### 107.7 这一轮没有改固件

**`s31test` 仍 36/36。新增的是 `tools/reset.py`（一个 host 侧工具）。**

### 107.8 方法学

| 教训 | 说明 |
|---|---|
| **ROM 自己打印启动模式，这是现成的量具** | `boot:0x48` vs `0x58` |
| **"掉电也不行"先怀疑电路空闲态** | 不是芯片、不是镜像 |
| **空闲态可以靠 host 强制线电平绕过** | `reset.py` |
| **唤醒要两个字节，不是特定字节** | 第一个字节会丢 |
| **手动按键是最后的兜底** | BOOT + RST |

---

## 108. W98：我上一轮的因果判断是错的

### 108.1 用户提供的事实

**"我的问题 我的串口助手开了 dtr..."**

**一句话解决了两个症状。**

### 108.2 真正的机制

**串口助手打开端口时置 DTR/RTS 有效。这两根线经反相缓冲接到 EN 和 IO0，
反相后是 "IO0 低"，于是复位时进 ROM 下载模式。**

**同一个程序还占着端口，所以脚本报 `PermissionError(13)`。**

**⇒ 一个原因，两个症状。**

### 108.3 我错在哪

**我上一轮写的是：**

> 串口没被打开时，驱动把两根线都放在**低**电平——反相后正好是 "IO0 低 + EN 释放"，
> 即上电进下载模式。
>
> 所以这是**电路的空闲状态**。

**问题有三层：**

| 层 | 错处 |
|---|---|
| 事实 | 端口不是"没被打开"，是**被别的程序打开了** |
| 方向 | 关闭的端口**释放** DTR/RTS，不是拉住它们 |
| 后果 | 把用户引向"换板子/换线"，而不是"关掉串口助手" |

**最糟的是第三层：一个错误的因果判断会让人去改不该改的东西。**

### 108.4 第二个错误：给没依据的值编解释

**我看到 `boot:0x78`、`0x7f` 也输出了 banner，就写成：**

> `0x58` / `0x78` / `0x79` 都是正常 flash 启动（高位是 SPI 模式/线宽位）

**这个说法没有任何依据。**
**实测稳定得到 `0x58` 的只有"两根线都置高 + 真正复位"这一条路径；
其他值出现在复位不干净的时候。**

**⇒ 我把"结果看起来正常"当成了"这个值也是正常的解释"。**

### 108.5 修正后 `reset.py` 会自己判

```python
if "boot:0x48" in log_text:
    print("!! this was a DOWNLOAD-mode boot (IO0 low at reset), not an app boot.")
    print("   Hold BOOT, tap RST, release BOOT, or close any other program that")
    print("   has the port open, then run this again.")
elif "boot:0x58" not in log_text:
    print("note: boot mode byte was not the usual 0x58. ...")
```

**端口打不开时也不再抛堆栈：**

```
cannot open COM16: could not open port 'COM16': PermissionError(13, ...)

If a serial assistant / monitor is open on this port, close it first.
An open monitor also asserts DTR/RTS, which holds IO0 low through the
board's inverting buffer and is what makes the ROM print
'waiting for download' instead of booting.
```

### 108.6 顺手修的另一件事：`ModuleNotFoundError`

**用户跑的是系统 Python 3.12，它没有 pyserial。**

```
ModuleNotFoundError: No module named 'serial'
```

**已给 `tools/reset.py` 和 `tools/board.py` 加前置检查，直接说该装什么、
以及 IDF 环境的 Python 路径（那个自带 pyserial）。**

**同时执行了 `python -m pip install pyserial`（系统 Python 现在也有 3.5 了）。**

### 108.7 文档更正

**runbook §93 原标题"真因：这块板的自动复位线路" —— 已改成"排查过程"，
并在开头加了结论更正提示。**

**runbook §94 整节重写，主因改为"别的程序占着串口并拉着 DTR"。**

**⇒ 错误的结论不能只在新章节里更正，旧章节的标题本身就是错误信息。**

### 108.8 方法学

| 教训 | 说明 |
|---|---|
| **"设备不工作"先问"还有谁在用它"** | 独占资源的冲突比硬件故障常见得多 |
| **关闭的端口释放线，不是拉住线** | 方向搞反，结论就反 |
| **错误的因果判断会让人改不该改的东西** | 换板子 vs 关串口助手 |
| **"结果看起来正常"不等于"这个值正常"** | 0x78/0x7f 我编了解释 |
| **旧章节的标题本身也是结论** | 只在新章节更正不够 |
| **工具缺依赖要说"装什么"，不是抛堆栈** | 用户跑的是另一个解释器 |

---

## 109. W99：Wi-Fi 真正连上并拿到 IP；CPU1 调度做了但没成功

### 109.1 第 3 项（审计还剩什么）的回答

**36 项自检里，做过"故障注入验证"的只有 7 项 + 1 项部分：**

| 已验证会红 | 未验证 |
|---|---|
| `fpu`（删 32 条 FSTORE） | `clock` `preempt` `nest` `rhwh.iv` `rhwh.ri` |
| `tick`（tick 加倍） | `dev.*` 全部 8 项 |
| `rhwh.ud`（us_delay 空） | `fbshim/*` 全部 12 项 |
| `rhwh.mk`（mask 不置位） | `irq`（部分） |
| `irq` `sem`（部分） | |

**审计"继续做什么"就是这些：每一项都要找一个"只有被测对象在用"的注入点，
改坏、烧写、确认 FAIL、恢复、确认 36/36、确认 `provoked` 为 0。**

**代价：每项约 5–10 分钟（两次编译 + 两次烧写）。28 项 ≈ 4–5 小时。**

**风险：注入点选错会把 shell 弄死（W81 `ctx`、W82 `irq` 各一次），
那时拿不到 FAIL 文本，只能算"未能验证"。**

### 109.2 任务 1：`s31wifi <ssid> <pass>` —— 完成

**改动（`main/s31wifi.c`）：**

1. **事件处理器覆盖三种事件**：`STA_START` / `STA_CONNECTED` /
   `STA_DISCONNECTED`（带 reason code 和有界重试），以及 `IP_EVENT_STA_GOT_IP`
2. **`IP_EVENT` 必须单独注册** —— 只注册 `WIFI_EVENT` 时连接会成功，
   但 `GOT_IP` 永远不到，命令会在链路上线的情况下报超时（经典错误）
3. **`esp_wifi_connect()` 在 `STA_START` 之后调用**（之前调返回 `WIFI_NOT_STARTED`）
4. **`threshold.authmode` 按密码是否为空选 WPA2_PSK / OPEN**
   —— 空密码配 WPA2 会以 `AUTH_EXPIRE` 失败，看起来像密码错
5. **`ip_info` 整个拷贝出来**（`data` 指针只在回调期间有效）
6. **失败时打印 reason 和它的含义**（15/205 = 密码错，201 = 找不到 AP）

**实测（真实 AP，3 次全新启动）：**

```
s31wifi: STA_START in 0 ms, mac 1c:29:04:d0:14:1e
  connecting to "刻于晴空之上_2.4G" (WPA2-PSK)
s31wifi: PASS, connected to "刻于晴空之上_2.4G" in 1380 ms
s31wifi: ip 10.0.0.119 mask 255.255.255.0 gw 10.0.0.1
```

**三次分别 1380 / 1340 / 2000 ms，IP 一致。**

**失败路径也验了（不存在的 SSID）：**

```
s31wifi: FAIL after 7240 ms: connected=0, retries=3, last reason=201
s31wifi: reason 15 = 4WAY_HANDSHAKE_TIMEOUT, 205 = AUTH_FAIL, both mean a wrong password;
s31wifi: 201 = NO_AP_FOUND means the SSID is not visible (try s31wifiscan).
```

**`s31wifi` 无参数时行为不变，只是明确说"未连接"：**

```
s31wifi: PASS (interface up, not connected - pass <ssid> <pass> to connect)
```

### 109.3 任务 2：CPU1 调度 —— 做到一半，按证据回滚

**三次尝试，前两次各修掉一个真问题：**

| 尝试 | 结果 | 根因 |
|---|---|---|
| 1 | `rst:0x8 HP_WDT1_RESET` 循环 | **CPU1 在 CPU0 有系统之前就开始调度** → 就绪表空 → `RT_ASSERT(to_thread)` |
| 2 | 能启动，`cpu1 cur_thread` = idle，但绑定到 CPU1 的线程**一次都没跑** | **CPU1 没有自己的 tick** |
| 3 | 仍有 `rst:0x10 CHIP_LP_WDT_RESET` | **未解释** |

**修掉的两个真问题：**

**(a) 顺序**：加 `s31_cpu1_go` 标志，从 `INIT_APP_EXPORT`（即 `main_thread_entry`）置位，
CPU1 先等它。这是唯一保证"两个 idle 线程已创建 + CPU0 调度器在跑"的时刻。

**(b) tick**：`rt_tick_increase()` 在 SMP 下只做本核的 `rt_cpu_self()->tick++` 和
`rt_sched_tick_increase()`，`rt_timer_check()` 按设计只在 CPU0。**没有自己的中断，
CPU1 的时间片永不过期，idle 永不被抢占。**

**为什么没用 IDF 的 `vSystimerSetup()`（两条独立原因，都读了源码）：**

1. 它的 alarm 配置循环是 `for (cpuid = 0; cpuid < configNUM_CORES; ++cpuid)`，
   而本镜像 `CONFIG_FREERTOS_UNICORE=y` ⇒ `configNUM_CORES == 1`
   ⇒ **alarm 1 从未被连接/设周期/设 PERIOD 模式**，`cpuid != 0` 分支只是"使能"
   一个永不到期的中断。实测：cpu1 tick 仍为 0。
2. 它的 handler 用 `xPortGetCoreID()` 索引 `s_handled_systicks[configNUM_CORES]`
   —— **一元数组**，在 CPU1 上越界写。

**为什么没用 SYSTIMER LL：** `hal/systimer_hal.h` / `hal/systimer_ll.h` 由
`esp_hal_systimer` 通过 target 条件化的 `INCLUDE_DIRS` 导出，
**本组件的 `PRIV_REQUIRES` 没能让它出现**（加了依赖后
`project_description.json` 里 `priv_reqs` 仍是 `['freertos','riscv']`）。

**最后用了端口自己的 TIMG 通道 3**（`s31_timer_start(3, RT_TICK_PER_SECOND, cb, NULL)`）
—— 这条路 `s31timer` 已经在板上验过，且 `esp_intr_alloc()` 把中断路由到**调用它的核**。

**仍然失败：** 加了 go 标志和 tick 之后，`s31smp` 显示
`cpu1 cur_thread == idle_thread`（说明确实进了调度器），
但 `cpu1probe` 报告 `ran on cpu 4294967295, 0 loops in 300 ms` —— **一次都没跑**，
然后进入 `rst:0x10 CHIP_LP_WDT_RESET`。

**决策：按证据回滚。** `RT_SMP_CPU1_SCHEDULES` 改为注释掉，
把诊断写进 `rtconfig.h`，所有代码保留在开关后面。

**回滚后 `s31smp` 回到已知良好状态：**

```
cpu cur_thread idle_thread irq_nest tick  istack
  0   2f03ea70    2f01fb90        0  7999 0x2f03bc50
  1          0    2f01fc34        0     0 0x2f02a840
istack : distinct per core
```

**保留的净成果：**

- `smp_ipi.c` 的**接收侧**已装好：`s31intr` 第 28 行（ETS_IPC_ISR_INUM）
  从"无处理器"变成 `lvl yes ... 0 -`，**这条线现在有归属了**
- `smp_tick.c`（TIMG 通道 3）写在开关后面，下次直接用
- 两个真 bug 的根因和解法都写进注释

### 109.4 检查器顺手修了两个真问题

**(a) 文本扫描器看不见 `#ifdef`**

开关关掉后，两个检查红：

```
[FAIL] every init is exported or called    ['rt_hw_secondary_cpu_tick_init']
[FAIL] exports reach the image             absent from ELF: ['s31_cpu1_release']
```

**这两个符号在 `#ifdef RT_SMP_CPU1_SCHEDULES` 里，而那个宏是注释掉的，
所以它们本来就不该在镜像里。**

**修法不是加豁免名单（那会让检查永久失明），而是给检查器加一个小的守卫求值器：
跟踪 `#ifdef`/`#ifndef` 栈，跳过宏未定义的行。**

**双向反向验证：**

| 操作 | 结果 |
|---|---|
| 开关打开 | 符号被重新扫描 → 仍 FAIL（因为 tick 的调用者在开关里，ELF 里确实没有 `s31_cpu1_release`） |
| 去掉守卫 | 同样 FAIL |

**⇒ 求值器没有把检查变瞎，它只是让"未编译"和"没引用"不再是同一个报告。**

**(b) 第 2 项检查太窄**

它只接受"被导出"或"从 board.c 调用"。而 `rt_hw_secondary_cpu_tick_init()` 是
**从 `smp_slave_sched.c` 调用**的 —— 导出它会把它放进板级初始化带，那是错的阶段。

**改成"被任何端口源文件调用"，这才对得上它想检查的性质：
"有东西引用这个函数"。**

### 109.5 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `s31wifi <ssid> <pass>` | ✅ 3 次全新启动均 PASS，IP 10.0.0.119 |
| `s31wifi <bad-ssid>` | ✅ FAIL + reason 201 |
| `s31wifi`（无参数） | ✅ 明确"未连接" |
| `s31intr` INUM 28 | ✅ 已安装（有处理器） |
| `s31smp` | ✅ 回到 parked 状态，`istack` 每核独立 |
| `check_rtt_style.py --strict` | ✅ **28/28** |
| `check_image.py` | ✅ 9/9 |
| 上游 `rt-thread` | ✅ **零修改** |

已保存 `w100_verified.bin`。

### 109.6 方法学

| 教训 | 说明 |
|---|---|
| **"能编"和"能跑"之间还有一层"语义对不对"** | `vSystimerSetup` 三个原因都读源码才发现 |
| **依赖加了不等于头文件能看见** | `priv_reqs` 里根本没有它 |
| **单核配置的宏会悄悄阉割多核路径** | `configNUM_CORES == 1` |
| **一个未解释的复位不能当"差不多能跑"** | 回滚，写清诊断 |
| **文本扫描器要理解 `#ifdef`** | 否则"未编译"报成"没引用" |
| **检查的措辞窄了会把正确做法报成错误** | "从 board.c 调用" vs "被任何端口文件调用" |
| **失败信息要能区分"没连上"和"连上了但没 IP"** | reason 15/205/201 各指一件事 |

---

## 110. W100：读 IDF 的双核启动，找到 CPU1 的差集 —— 以及它还不够

### 110.1 IDF 到底怎么让第二个核跑起来的

**三段，按顺序读源码得到：**

**(1) `esp_system/port/cpu_start.c: start_other_core()`（CPU0 执行）**

```c
esp_cpu_unstall(1);
cpu_utility_ll_enable_clock_and_reset_app_cpu();
cpu_utility_ll_enable_clock_and_reset_app_cpu_int_matrix();
ets_set_appcpu_boot_addr((uint32_t)call_start_cpu1);
while (!cpus_up) { ... esp_rom_delay_us(100); }   /* 等 CPU1 报到 */
```

**(2) `cpu_start.c: call_start_cpu1()`（CPU1 执行）**

```c
la gp, __global_pointer$        /* 全局指针 */
esp_cpu_intr_set_ivt_addr(&_vector_table);
esp_cpu_intr_set_mtvt_addr(&_mtvt_table);     /* CLIC 向量表 */
ets_set_appcpu_boot_addr(0);
bootloader_init_mem();
esp_rom_install_uart_printf();                /* 让 CPU1 也能打印 */
core_intr_matrix_clear();                     /* 清 CPU1 自己的中断矩阵 */
s_cpu_up[1] = true;                           /* 报到 */
while (!s_resume_cores) { esp_rom_delay_us(100); }   /* 等 CORE 阶段结束 */
SYS_STARTUP_FN();                             /* 跑 SECONDARY 阶段 init */
```

**(3) `freertos/app_startup.c: esp_startup_start_app_other_cores()`（CPU1 执行）**

```c
while (port_xSchedulerRunning[0] == 0) { }    /* 等 CPU0 的调度器起来 */
esp_int_wdt_cpu_init();                       /* CPU1 自己的中断看门狗 */
esp_crosscore_int_init();                     /* CPU1 的核间中断接收 */
xPortStartScheduler();
```

**而 `xPortStartScheduler()`（`portable/riscv/port.c`）在每个核上都会做：**

```c
port_uxInterruptNesting[coreID] = 0;
port_uxCriticalNesting[coreID] = 0;
...
vPortSetupTimer();                            /* 每个核自己的 tick */
esprv_int_set_threshold(RVHAL_INTR_ENABLE_THRESH);   /* ← 每核的 mintthresh */
rv_utils_intr_global_enable();                       /* ← 每核的 mstatus.MIE */
vPortYield();
```

**⇒ IDF 的关键不是"多复杂"，而是**每一项都按核做一遍**：向量表、中断矩阵、
tick、阈值、MIE、看门狗、核间接收。**

### 110.2 本端口缺的差集（逐项核对）

| 项 | IDF | 本端口 | 状态 |
|---|---|---|---|
| gp / 启动地址 | ✅ | ✅ `smp_slave.c` | 有 |
| mtvec / mtvt | ✅ | ✅ `smp_slave.c` | 有 |
| ticket lock 跨核 | — | ✅ W51 验过 | 有 |
| per-CPU 中断栈 | ✅ | ✅ `smp_istack.c` | 有 |
| 等 CPU0 调度器起来 | ✅ `port_xSchedulerRunning[0]` | ✅ `s31_cpu1_go` | **本次补上** |
| CPU1 自己的 tick | ✅ `vPortSetupTimer()` | ✅ `smp_tick.c`（TIMG 通道 3） | **本次补上** |
| **CPU1 的 CLIC 阈值** | ✅ 每核 `set_threshold` | ❌ **只有 CPU0 设过** | **本次补上** |
| **CPU1 的 mstatus.MIE** | ✅ 每核 `global_enable` | ❌ | **本次补上** |
| CPU1 的核间接收 | ✅ `esp_crosscore_int_init()` | ✅ `rt_hw_ipi_handler_install()` | **本次补上** |
| CPU1 的中断矩阵清理 | ✅ `core_intr_matrix_clear()` | ❌ | **未做** |

**四个差集，三个这次补了。**

### 110.3 补完之后仍然是 ticks=0

**在 CPU1 上设了阈值、开了 MIE、配了 TIMG tick、装了核间接收，然后在 CPU0 上采样：**

```
smp: after go, cpu1 alive=0x5a5a5a5a ticks=0 cur=0x2f01fc34 idle=0x2f01fc34
```

**一行三个事实：**

| 字段 | 值 | 说明 |
|---|---|---|
| `alive` | `0x5a5a5a5a` | **CPU1 跑了我们的入口代码** |
| `ticks` | **0** | **300 ms @ 1 kHz，一次 tick 中断都没收到** |
| `cur == idle` | `0x2f01fc34` | **确实进了 `rt_system_scheduler_start()`，切到了 idle** |

**⇒ 不是"调度器不肯切"，是"这个核根本没被中断过"。**

**这是本轮最有价值的一句：它把问题从"调度器"缩小到了"中断投递"，而这两者的修法完全不同。**

**并且打开开关就进入 `rst:0x10 CHIP_LP_WDT_RESET` 循环，关闭则稳定。**

### 110.4 你的第二个问题：核间通讯还需要做吗？

**需要，而且已经做了一半，但"做了"和"有用"是两件事。**

**现状：**

| 半边 | 状态 |
|---|---|
| 接收侧 | ✅ 已装。`s31intr` 第 28 行从 "无处理器" 变为已安装 |
| 发送侧 | ✅ 已解锁（`s31_ipi_receive_side_ready = 1`），但**唯一受益者是 CPU1 调度，而调度不工作** |

**⇒ 核间通讯本身**没有**遗留工作 —— 收发两侧都在。它现在"没用"是因为
CPU1 不调度；CPU1 一调度，内核的 `rt_hw_ipi_send(RT_SCHEDULE_IPI, ...)`
立刻就有意义了。**

**但有一个细节需要指出：本端口的 IPI 和 IDF 用的不是同一个硬件机制。**

| | IDF | 本端口 |
|---|---|---|
| 触发 | 写 `HP_SYSTEM_CPU_INT_FROM_CPU_0/1_REG`（source 65/66） | 置 CLIC 第 28 行的 `IP` 位 |
| 投递 | 经中断矩阵映射到 source 65/66 | 直接对 INUM 28 |
| 清除 | 写 0 到同一个 SYSTEM 寄存器 | CLIC 硬件清 |

**两种都合法（CLIC 的 `IP` 位是软件中断，不需要矩阵）。**
**但如果 CPU1 收不到任何中断，这一条也应该一并验证 —— 见下。**

### 110.5 下一步的最小实验（不做调度，只测中断）

**这是本轮留下的最有价值的一条：把问题拆出来单独验证。**

```
在 CPU0 上调用 rt_hw_ipi_send(RT_SCHEDULE_IPI, 1U << 1)
等 10 ms
打印 s31_cpu1_ticks / 一个新的 s31_cpu1_ipis
```

**三种结果对应三条完全不同的路：**

| 结果 | 含义 | 下一步 |
|---|---|---|
| CPU1 收到了 | CLIC `IP` 位路径可用 → 问题在 TIMG 的路由/矩阵 | 查 `ETS_TIMERGRP*_T*_SOURCE` 的矩阵映射 |
| CPU1 没收到 | 连软件中断都到不了 → 问题在 CPU1 的向量表/矩阵/使能 | 查 `mtvt` 在该核上是否生效 |
| CPU1 收到但与 tick 无关 | 两条路独立 | 分别修 |

**"核间通讯还要不要做"的答案由此变成：
机制已经做完，剩下的是证明它在 CPU1 上真的能到 —— 而这正好是 CPU1 调度的同一个前提。**

### 110.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `s31wifi <ssid> <pass>` | ✅ PASS，ip 10.0.0.119 |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |
| 上游 `rt-thread` | ✅ 零修改 |
| `RT_SMP_CPU1_SCHEDULES` | ❌ 关闭（开着会 LP_WDT 复位循环） |

已保存 `w101_verified.bin`。

### 110.7 方法学

| 教训 | 说明 |
|---|---|
| **读上游要读"每核都做什么"，不是"启动顺序"** | IDF 的价值在 `port.c` 里两行按核的寄存器写 |
| **差集要逐项列表** | 五项里三项有、两项没有 |
| **补完还没好，说明差集不完整** | 第四项（矩阵）还没做 |
| **一个计数器把两个假设分开** | `ticks=0` ⇒ 中断没到，不是调度器 |
| **别在复位的板上调代码** | 先回滚到稳定状态，再设计实验 |
| **"做完"和"有用"是两件事** | IPI 两侧都在，但受益者不工作 |

---

## 111. W101：用的是 RT-Thread 的多核方法（IDF 的核从没启动过）

### 111.1 直接回答

**这个移植用的是 **RT-Thread 自己的多核模型**，不是 IDF 的。**

**证据不是"代码写得像谁"，而是镜像里有什么：**

```
nm s31_rtt_test.elf | grep -E "start_other_core|call_start_cpu1|esp_startup_start_app_other_cores"
（无输出）

nm s31_rtt_test.elf | grep -E "rt_hw_secondary_cpu_up|rt_system_scheduler_start"
40019ea0 T rt_hw_secondary_cpu_up
2f0166fa T rt_system_scheduler_start
```

**IDF 那三个函数**一个都不在镜像里** —— 链接器把它们整个丢了，
因为没有任何代码路径引用它们。**

### 111.2 为什么 IDF 的路径被编译掉

**`esp_system/startup.c` 的分支：**

```c
#if !CONFIG_IDF_RTOS_RTTHREAD
    esp_startup_start_app();               /* IDF/FreeRTOS 路径 */
#else
#if defined(__GNUC__) && defined(RT_USING_USER_MAIN) && CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE
    entry();                               /* ★ 本镜像走这里 */
#else
    ... rt_hw_secondary_cpu_up() 手工调用 ...
#endif
```

**本镜像：`CONFIG_IDF_RTOS_RTTHREAD=y` + `RT_USING_USER_MAIN` +
`CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y` ⇒ **走 `entry()`**，
即标准 RT-Thread 入口（`rtthread_startup()`）。**

**而 `start_other_core()` 只被 `!CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE` 的路径调用，
所以它被 `--gc-sections` 丢掉。**

**⇒ CPU1 是 `rtthread_startup()` 里的
`rt_hw_secondary_cpu_up()` 拉起来的，那是 RT-Thread 的钩子，
由本端口的 `smp_slave.c` 实现（`cpu_utility_ll_*` + `ets_set_appcpu_boot_addr`
+ `esp_cpu_unstall`）。**

### 111.3 所以"IDF 怎么调度的"只有参考价值

**IDF 的 `xPortStartScheduler()` 里那两行按核操作（阈值 + MIE）
之所以值得读，是因为 **RT-Thread 没有对应的按核入口** ——
`rt_system_scheduler_start()` 里没有这两行，本端口只在 `board.c` 给 CPU0 设过。**

**这就是为什么我照着 IDF 补了 `rt_hw_secondary_cpu_irq_enable()`。
这不是"抄 IDF"，是"补上 RT-Thread 模型里缺的那个按核步骤"。**

### 111.4 核间通讯：RT-Thread 的机制，本端口实现

| | RT-Thread | 本端口 |
|---|---|---|
| 发送 | `rt_hw_ipi_send(RT_SCHEDULE_IPI, mask)` | ✅ `smp_ipi.c`，置 CLIC INUM 28 的 `IP` 位 |
| 接收 | `rt_hw_ipi_handler_install(v, rt_scheduler_ipi_handler)` | ✅ `smp_ipi.c`，`rt_hw_interrupt_install(ETS_IPC_ISR_INUM, ...)` |
| 触发点 | `rt_sched_unlock_n_resched()` / `rt_sched_thread_bind_cpu()` | 内核自动调，无需改 |

**⇒ 机制层面已经完整，用的是 RT-Thread 的 IPI 抽象。**

**IDF 的 `esp_crosscore_int_*`（走 `HP_SYSTEM_CPU_INT_FROM_CPU_*` 寄存器，
source 65/66）与本端口无关 —— 而且 `esp_crosscore_int.c` 里有
`CONFIG_FREERTOS_NUMBER_OF_CORES > 1` 的分支，本镜像是 1，所以它只会装 source 65。**

### 111.5 这次又试了一条，仍然 ticks=0

**假设：`esp_intr_alloc()` 只装处理器 + 路由矩阵，**不使能中断**；
而 `rt_hw_interrupt_umask()` 写的是**当前核**的 CLIC 窗口
（`soc/interrupt_reg.h`："CLIC_INT_THRESH_REG points to the current core"），
从 CPU0 调它只会使能 CPU0 那一份。**

**所以给 CPU1 的 TIMG 中断补了：**

```c
esp_intr_enable(s31_timer_intr_handle(S31_CPU1_TICK_TIMER));   /* 在 CPU1 上调用 */
```

**结果：**

```
smp: after go, cpu1 alive=0x5a5a5a5a ticks=0 cur=0x2f01fc34 idle=0x2f01fc34
```

**`ticks` 仍是 0。**

### 111.6 排除了什么，剩下什么

| 已排除 | 依据 |
|---|---|
| 启动顺序（CPU0 未就绪） | 有 `s31_cpu1_go`，仍 ticks=0 |
| CPU1 没有 tick 定时器 | TIMG ch3 已启动，`s31_timer_start` 返回 EOK |
| CPU1 的阈值 / MIE | `rt_hw_secondary_cpu_irq_enable()` 已调，仍 0 |
| 中断"已路由未使能" | `esp_intr_enable()` 已调，仍 0 |
| 调度器拒绝切换 | `cur == idle`，说明切换发生了 |

| 剩下的候选 | 怎么验 |
|---|---|
| **CPU1 的 `mtvt`/`mtvec` 不生效** | 在 CPU1 上故意读地址 0，看 trap 是否打印 |
| **中断矩阵把源路由到了 CPU0** | 读 `INTERRUPT_CORE1_*_MAP_REG`，看该源指向哪个 INUM |

**下一个实验已经写进 `rtconfig.h` 的注释：在 CPU1 上制造一次 fault，
不带任何外设。这一条能区分"向量表不通"和"矩阵不对"。**

### 111.7 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |
| `RT_SMP_CPU1_SCHEDULES` | ❌ 关闭（开着会 LP_WDT 复位循环） |
| 上游 `rt-thread` | ✅ 零修改 |

已保存 `w102_verified.bin`。

### 111.8 方法学

| 教训 | 说明 |
|---|---|
| **"用了谁的方法"要看镜像里有什么符号** | 不是看代码风格 |
| **IDF 的核从没启动** | `start_other_core` 不在 ELF 里 |
| **照 IDF 补的是"缺的按核步骤"，不是"抄实现"** | 阈值/MIE 是每核 CSR |
| **一次排除一个，全部记下来** | 五个已排除，两个候选 |
| **负结果同样要落地** | 写进 `rtconfig.h`，不留在对话里 |

---

## 112. W102：CPU1 卡在"tick 已配好、ISR 从未执行"之间 —— 五个量全部正确

### 112.1 这一段用一个计数器把问题切到了几行之内

**给 CPU1 的路径加阶段标记（`s31_cpu1_stage`），每个可能卡住的位置前进一步：**

| stage | 含义 |
|---|---|
| 1 | CSR 回读完成 |
| 2 | 阈值已降、MIE 已开 |
| 3 | tick 已配置、线已使能 |
| 4 | 空转循环跑完（应接着进调度器） |

**结果：**

```
smp: cpu1 stage=3 tick_started=1 isr_runs=0 ticks=0 early=0 spin=0
```

**`tick_started=1` 说明 `s31_timer_start()` **返回了**；
**`stage=3` 说明 `rt_hw_secondary_cpu_tick_init()` 也**返回了**；
**`isr_runs=0` / `early=0` 说明 ISR **一次都没进**；
**`spin=0` 说明紧接着的 10 万次空转循环**没跑**。**

**⇒ CPU1 在"tick 初始化函数返回之后、空转循环之前"停止执行。
中间只有几条普通语句。**

### 112.2 五个量全部正确，但它就是不动

| 量 | CPU1 | CPU0 | 结论 |
|---|---|---|---|
| `mtvec` | `0x2f000003` | 同 | ✅ 与 `_vector_table` 一致 |
| `mtvt` | `0x2f000040` | 同 | ✅ 与 `_mtvt_table` 一致 |
| `mintthresh` | `0x1f` | 同 | ✅ 阈值 0，线 0x3f 可通过 |
| `mstatus.MIE` | 1 | 1 | ✅ 全局使能 |
| 矩阵 map | 0 | 0 | ✅ 源 26 投到中断 0 |
| 线 16 ctrl | `0x3fc00101` | `0x3fc10100` | ✅ `IE=1`，而且 **`IP=1`（挂起）** |

**六项全对，中断仍然不被服务。**
**`entry_count=1` 说明 CPU1 只跑了一次入口，不是被反复复位。**

### 112.3 试过并排除的修法

| 修法 | 结果 |
|---|---|
| 从 CPU0 调 `rt_hw_interrupt_umask()` | CPU1 的 IE 仍为 0（那是**当前核**的窗口） |
| 从 CPU1 调 `esp_intr_enable(handle)` | 无变化 |
| 从 CPU1 调 `rt_hw_interrupt_umask(0)` | **IE 变成 1**（`0x3fc00101`），但 ISR 仍不进 |
| 给 tick 回调加"本核还没有线程就返回"的保护 | `isr_runs` 仍为 0 —— **根本没进 ISR** |
| `rt_cpu1_go` 顺序修正 | 修掉了一个真 bug（`rst:0x8` 循环），但不是这个 |

### 112.4 现在的事实链

**能确定的：**

1. CPU1 会执行我们的代码（`alive` / `entries=1`）
2. CPU1 会进调度器并切到 idle（`cur == idle`）
3. CPU1 的中断向量、阈值、MIE、矩阵、CLIC 使能位**全部正确**
4. CPU1 的 tick 线**处于挂起状态**（`IP=1`）
5. CPU1 的 ISR **从未执行**
6. CPU1 在 tick 初始化返回之后、下一个循环之前**停止执行**

**3 和 4 与 5 是矛盾的：CLIC 已经在敲它，它却不响应，也不崩溃。**

### 112.5 下一步的两个方向

**方向 A：CLIC 向量模式**

`mtvec` 读到 `0x2f000003`，MODE=3。`SOC_INT_CLIC_SUPPORTED=1`，
CLIC 模式跳转是 `mtvt + 4*inum`。INUM 0 等于外部中断 0，
槽位 `_mtvt_table + 0`。**如果那个槽位在 CPU1 上没被初始化，跳转就落到 0 或垃圾，
表现为"停止执行"而不是 panic。**

**验证方法：读 `_mtvt_table[0]` 的内容，和 `_interrupt_handler` 的地址比。**

**方向 B：`mstatus.MPP`**

CPU0 的 `mstatus=0x80006088`（MPP=0），CPU1 是 `0x00000009`。
**软件中断和外部中断在 M 模式下都不看 MPP，但 CPU1 从复位起是 M 模式吗？
`mhartid` 能不能读到、CLIC 窗口能不能写，都说明是 M 模式。**

**⇒ 方向 A 更可疑，而且是纯读取，零风险。**

### 112.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed** |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9（init 表更新了一处） |
| 上游 `rt-thread` | ✅ 零修改 |
| `RT_SMP_CPU1_SCHEDULES` | ❌ 关闭 |

已保存 `w103_verified.bin`。

### 112.7 方法学

| 教训 | 说明 |
|---|---|
| **阶段标记把"卡住"切成几行** | `stage=3` + `spin=0` 只差几条语句 |
| **计数器要放在函数第一条语句** | 放后面就分不清"没进"和"进了没算到" |
| **"全部正确但不工作"是有效结论** | 六个量逐个排除 |
| **排除要记录，不能只记成功的** | 五种修法、四成败 |
| **init 表检查报错是它该做的** | 新增 export = 契约变更 |

---

## 113. W103：`spin=39449` 是决定性的 —— CPU1 跑到循环中途才死

### 113.1 上一轮的数字其实藏着答案

**之前只看到 `spin=0`，因为那次编译的循环根本没跑。
修掉 CSR 编号错误后：**

```
smp: cpu1 stage=3 tick_started=1 isr_runs=0 ticks=0 early=0 spin=39449
smp: cpu1 mintstatus=0x00000000 mil=0x00 (cpu0 0x00000000 mil=0x00)
```

**`spin=39449`（上限 100000）说明：**

| 事实 | 推论 |
|---|---|
| 循环**跑了一部分** | CPU1 确实执行到了循环里 |
| 停在 60% 处 | **不是确定性指令导致的停止** —— 那条指令每次都会执行 |
| `isr_runs=0` | 期间 ISR **一次没进** |
| `mil=0` | 停止时**不在 handler 里** |

**⇒ CPU1 在空转循环中途被**某个东西**打断，然后就没再回来。
那段时间里唯一被武装的中断源，正是刚使能的 tick。**

### 113.2 随机中断 + ISR 计数为 0 = 陷阱路径本身有问题

**如果 ISR 真的进了，`isr_runs` 会 ≥1（计数器在函数第一条语句）。
它是 0，说明：**

**中断被投递了（CLIC `IP=1` 已经测到），陷阱被取了（否则 CPU1 会继续自增 `spin`），
但执行流没有到达我们装的那个 handler。**

**结合 `mintstatus.MIL=0`：停止时不在 handler 上下文里 ⇒ 它没进 handler。**

**⇒ 问题在"陷阱被取"到"调用 handler"之间，也就是 trap 入口或向量槽。**

### 113.3 顺带修掉一个我自己引入的 panic

**我用 `csrr %0, 0x346` 读 mintstatus，而 `riscv/csr_clic.h` 里写的是：**

```c
#define MINTSTATUS_CSR      0xFB1
```

**0x346 不是这颗芯片的 CSR ⇒ `Illegal instruction`，`MCAUSE 0x2`，
`MEPC 0x4001a17e`（`addr2line` 指向 `smp_tick.c:291` 那条 `csrr`）。**

**很有意思的是：panic 报的是 `Core 0`** —— 因为那条读取在**打印函数**里，
跑在 CPU0 上。**这提醒了一件事：诊断代码本身也会成为故障源。**

### 113.4 现在的事实链（更新）

| # | 事实 | 证据 |
|---|---|---|
| 1 | CPU1 执行我们的代码 | `alive`、`entries=1` |
| 2 | 进了调度器并切到 idle | `cur == idle` |
| 3 | 向量/阈值/MIE/矩阵/CLIC 使能**全对** | 逐项读回 |
| 4 | tick 线**挂起**（`IP=1`） | 线 16 ctrl `0x3fc00101` |
| 5 | 空转循环跑到 39449/100000 | `spin=39449` |
| 6 | ISR **从未执行** | `isr_runs=0`、`mintstatus.MIL=0` |

**5 与 6 一起说明：陷阱被取了，handler 没被调用。**

### 113.5 下一步：读 `_mtvt_table[0]`

**`mtvec MODE=3` ⇒ 跳转 `mtvt + 4 * (mcause - 16)`。
源 26 → 中断 0 → `_mtvt_table[0]`。**

**`vectors_clic.S` 里槽位 16 是 `_interrupt_handler`（注释写第 16 项，地址偏移 4×16）；
但**数组索引 0** 是第 0 项 = `_system_int_handler`。**

**⚠️ 这里有个可能的错位：如果 CPU1 用 `mcause-16` 索引，中断 0 取 `_mtvt_table[0]`
= `_system_int_handler`（系统中断处理器），而不是 `_interrupt_handler`。**

**如果 `_system_int_handler` 在 CPU1 上不工作（它可能假设 CPU0 的上下文），
现象正是"陷阱被取，handler 没被调用"。**

**验证（纯读取）：**

```c
rt_kprintf("mtvt[0]=%p mtvt[16]=%p  _interrupt_handler=%p\n",
           ((void **)_mtvt_table)[0], ((void **)_mtvt_table)[16], _interrupt_handler);
```

### 113.6 验证

| 检查 | 结果 |
|---|---|
| `s31test` | ✅ **36 passed, 0 failed** |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |
| 上游 `rt-thread` | ✅ 零修改 |
| `RT_SMP_CPU1_SCHEDULES` / `RT_SMP_CPU1_FAULT_TEST` | ❌ 均关闭 |

已保存 `w104_verified.bin`。

### 113.7 方法学

| 教训 | 说明 |
|---|---|
| **部分完成的计数器比 0 信息量大得多** | `spin=39449` 才说明"跑到中途" |
| **上一次 `spin=0` 是因为那次编译没进循环** | 同一现象两种解释，必须重测 |
| **诊断代码本身会 panic** | `0x346` 不是 mintstatus（应为 `0xFB1`） |
| **panic 报 Core 0 提醒我读错层** | 打印函数在 CPU0，不在 CPU1 |
| **"陷阱被取但 handler 没调用"是一个独立结论** | 指向向量槽，不是调度器 |

---

## 114. W104：双核跑通了 —— 根因是 **CPU1 的 mstatus.FS 是 0**

### 114.1 一句话

**trap 入口无条件保存 f0..f31（`vectors_rtthread.S:113`，`__riscv_flen==32` 时开启），
而 CPU1 从复位进入时的 `mstatus.FS=0` —— 第一条 `fsw` 就是非法指令，
核心死在自己的陷阱处理里。**

### 114.2 怎么找到的

**上一轮已知：`spin=39449/100000`（循环跑到中途）、`isr_runs=0`、`mil=0`。
这一轮先把"向量槽"排除掉：**

```
smp: mtvt[0]=0x2f000100 [16]=0x2f000242 [26]=0x2f000242  ih=0x2f000242 ph=0x2f000100
```

**槽 16（= 中断 0 + 偏移）正是 `_interrupt_handler`，投递目标没错。**

**接着把 tick 关掉做对照：**

```
无 tick:  spin=100000（跑完），板子稳定，无复位
有 tick:  spin=39449（停住），板子 LP_WDT 复位
```

**⇒ tick 就是触发者。而两次编译的 `spin` 值几乎相同（39449 / 39443），
说明它在"第一个可投递的指令点"被触发 —— 因为那条线在循环开始前就已经挂起。**

**剩下的就是"陷阱被取之后立刻死"。对比两个核的 mstatus：**

```
CPU0 mstatus = 0x80006088   FS(14:13) = 3
CPU1 mstatus = 0x00000009   FS(14:13) = 0
```

**而 `vectors_rtthread.S` 的注释自己写着：**

> mstatus.FS is non-zero in every context this entry can run on:
> rt_hw_stack_init_core() builds thread frames with mstatus=0x7880, and
> rtthread_startup() keeps MIE off until the first thread's mret.

**这个假设对 CPU0 和所有线程成立，唯独对"从自己的入口代码里开中断的 CPU1"不成立。**

### 114.3 修法

**在 CPU1 的按核初始化里，开中断之前先置 `FS=3`（和 `MPP=3`）：**

```c
__asm__ volatile ("csrs mstatus, %0" ::
                  "r"((rt_uint32_t)((3u << 13) | (3u << 11))) : "memory");
```

**修完：**

```
smp: cpu1 mintthresh=0x1f mstatus=0x80007809 mie=1
smp: cpu1 stage=4 tick_started=1 isr_runs=316 ticks=314 early=2 spin=100000
```

**`isr_runs=316`、`ticks=314`、`spin=100000` —— ISR 真的在跑，循环也跑完了。**

### 114.4 CPU1 真的在调度了

```
cpus   : 2, running on cpu 0
  1   2f01fc34    2f01fc34        0  7800 0x2f02a840
cpu1probe ran on cpu 1, 10 loops in 300 ms
cpu1   : SCHEDULING (a bound thread is running there)
```

**`cpu1probe ran on cpu 1` —— 绑定到 CPU1 的线程**真的在 CPU1 上跑了**。**

### 114.5 打开双核之后暴露了两个测试自身的 bug

**全量自检在 `preempt` 之后 panic。两次不同的 panic，两个真问题：**

**(a) `st_ctx` 的 4 个 worker 没有绑核** —— 在 MP 下未绑核线程两个核都能取，
CPU 密集型线程迁移会让测试卡死。**已绑定到 CPU0。**

**(b) `st_preempt` 的拆卸与其注释矛盾** —— 注释明确写：**

> clearing `running` first lets them return from their entry functions, and a thread
> that has returned is already on its way to being freed by the idle thread. Calling
> rt_thread_delete() on that handle afterwards is a double free.

**而代码就是先清 `running` 再 `rt_thread_delete()`。**

**单核时 delete 通常赢得竞争；双核后唤醒模式改变，worker 先返回，
idle 线程释放，再 delete 就是双重释放：**

| 顺序 | panic |
|---|---|
| 先清 `running` 再 delete | `Store access fault` @ `rt_hw_spin_lock` ← `rt_spin_lock_irqsave`（锁的是已释放内存） |
| 改成先 delete 再清 `running` | 仍在竞争窗口内 → `Load access fault` @ `rt_object_get_type` ← `rt_smem_free` |

**⇒ 重排顺序治不了，必须**消除返回路径**：worker 改成 `while (1)`，
唯一的退出方式是 `rt_thread_delete()`。**

**修完连跑 3 次：**

```
run 0: === 36 passed, 0 failed ===
run 1: === 36 passed, 0 failed ===
run 2: === 36 passed, 0 failed ===
```

### 114.6 验证

| 检查 | 结果 |
|---|---|
| `s31test`（CPU1 调度**开启**） | ✅ **36 passed, 0 failed**，连跑 3 次 |
| `s31smp` | ✅ `cpu1probe ran on cpu 1` + `SCHEDULING` |
| `s31_cpu1_ticks` | ✅ 314，`isr_runs` 316 |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |
| 上游 `rt-thread` | ✅ 零修改 |

已保存 `w105_dualcore_verified.bin`。

### 114.7 这一轮的方法学

| 教训 | 说明 |
|---|---|
| **关掉一个变量做对照，比继续加探针更快** | 关 tick → 循环跑完，一步定性 |
| **"陷阱被取但立即死"要对比两核的 CSR** | `FS` 差异一眼看出 |
| **注释里的假设要逐条核对适用性** | FS 的假设对线程成立、对 CPU1 不成立 |
| **双核会暴露单核下"通常赢"的竞争** | preempt 的双重释放 |
| **重排顺序治不了竞争，要消除路径** | `while(1)` + delete |
| **PPI/向量/栈都排除后，剩下的就是 trap 自身** | 六项量全对 |

### 114.8 一个必须如实记录的残存问题：Wi-Fi 与双核不兼容

**打开 CPU1 调度之后测 `s31wifi`：**

```
rst:0x1a (CPU_LOCKUP_RESET)
```

**第一个假设：shim 任务被 `xTaskCreatePinnedToCore` 创建时"绑核"只是名义上的，
`rt_thread_create()` 不绑核，所以 WiFi 任务会迁移到 CPU1，而它不可跨核重入。**

**已在 `ft_task.c` 里把每个 shim 任务绑到 CPU0，结果变成 `rst:0xc (SW_CPU_RESET)`。**

**⇒ 不是任务放置问题，交互更深。**

**如实记录为限制，不再继续追：**

| 能力 | 双核开启 | 双核关闭 |
|---|---|---|
| `s31test` 36 项 | ✅ 36/36 | ✅ 36/36 |
| `s31smp` | ✅ `SCHEDULING` | 停核 |
| `s31wifi` | ❌ 复位 | ✅ 连上并拿 IP |

**开关的注释里写明了这一点：要用 WiFi 就注释掉 `RT_SMP_CPU1_SCHEDULES`。**

**这也是本轮唯一没有解决的问题，且边界清楚：RT-Thread 线程的双核是好的，
FreeRTOS shim + esp_wifi 的组合还不行。**

---

## 115. W105：Wi-Fi 与双核的死因查明 —— 并修好

### 115.1 panic 说明的事情比我以为的更具体

**`s31wifi` 的 panic 是：**

```
Guru Meditation Error: Core 0 panic'ed (Cache access error)
MEPC    : 0x2f00ca02   -> spimem_flash_ll_set_cs_pin
RA      : 0x2f017a8c   -> spiflash_start_core
MCAUSE  : 0x00000019   (cache error)
```

**⇒ 不是 WiFi 的问题，是**flash cache 被关掉时还有东西在执行 flash 里的代码**。**

### 115.2 从产物（不是从源码）确认机制

**反汇编 `spi_flash_disable_interrupts_caches_and_other_cpu()`，整段只有 6 条指令：**

```
2f00052c  addi sp,sp,-16
2f000530  auipc/jalr -> spi_flash_op_lock
2f000538  jal        -> esp_intr_noniram_disable
2f00053c  li a0,0 ; li a1,&s_flash_op_cache_state
2f000546  jal        -> spi_flash_disable_cache(0, ...)
2f00054c  ret
```

**没有 `esp_ipc_call_nonblocking`，没有调度器状态分支，没有任何跨核握手。
而 `esp_ipc_call_*` 与 `spi_flash_op_block_func` **在 ELF 里根本不存在**。**

**原因：**

```
CONFIG_FREERTOS_UNICORE=y
CONFIG_ESP_SYSTEM_SINGLE_CORE_MODE=y
```

**⇒ IDF 那一半多核逻辑被编译掉了。**

**所以 CPU0 关自己的 flash cache 时，CPU1 照跑不误。**

### 115.3 决定性的对照实验

**把 CPU1 的 tick 关掉（调度仍然开着）：**

```
s31wifi: PASS (interface up, not connected)
s31wifi: PASS, connected to "刻于晴空之上_2.4G" in 4360 ms
s31wifi: ip 10.0.0.119 mask 255.255.255.0 gw 10.0.0.1
—— 没有复位，没有 cache error
```

**但 `s31smp` 变成 `entered the scheduler but ran nothing`（没 tick 就抢不动 idle）。**

**⇒ 触发者是 **CPU1 的 tick**，不是"CPU1 在跑"：
tick 处理器本身在 IRAM（`s31_timg_isr` @ `0x2f002d9c`），
但它调用的 `rt_tick_increase()` 在 flash（`0x400116a4`）。**

### 115.4 修法：给 flash 操作加一个跨核握手

**设计成最小可行的一对钩子：**

```c
/* CPU0，关 cache 之前 */
spi_flash_other_core_stop();     /* 置标志，等 CPU1 应答 */
esp_intr_noniram_disable();
spi_flash_disable_cache(...);

/* CPU0，恢复 cache 之后 */
spi_flash_restore_cache(...);
esp_intr_noniram_enable();
spi_flash_other_core_resume();   /* 清标志 */
```

**CPU1 的 tick ISR 里，第一条语句之后：**

```c
if (s31_flash_op)
{
    s31_flash_ack = 1;
    return;                   /* 不调 rt_tick_increase() —— 它在 flash 里 */
}
```

**两个变量都在 DRAM，cache 关了也能读。
握手放在 ISR 的**最前面**，所以"看到标志"意味着 CPU1 已经不会再取 flash 指令。**

**钩子在 `spi_flash/cache_utils.c` 里声明为 weak，端口提供强定义 ——
这样单核构建照旧链接，组件本身也不依赖 RT-Thread。**

### 115.5 结果

**WiFi（CPU1 调度 + tick + 握手）：**

```
s31wifi: PASS, connected to "刻于晴空之上_2.4G" in 1160 ms
s31wifi: ip 10.0.0.119 mask 255.255.255.0 gw 10.0.0.1
```

**双核仍然在跑：**

```
1   2f01fcb4    2f01fcb4        0  7759 0x2f02a8c0
cpu1   : SCHEDULING (a bound thread is running there)
```

**全量自检：`=== 36 passed, 0 failed ===`，连跑 3 次。**

### 115.6 代价与边界

| 项 | 说明 |
|---|---|
| CPU1 在 flash 窗口里丢 tick | 与 IDF 在多核上暂停对方调度器是同一个取舍 |
| 握手超时 200000 次循环 | 没 tick 时不会永远等（那是 `RT_SMP_CPU1_NO_TICK` 实验态） |
| 改动了 IDF 组件 | `cache_utils.c` 加了两处 weak 钩子调用；组件不依赖 RT-Thread |

### 115.7 验证

| 检查 | 结果 |
|---|---|
| `s31test`（双核 + WiFi 都开） | ✅ **36/36，连跑 3 次** |
| `s31wifi <ssid> <pass>` | ✅ 连接并拿 IP（两次） |
| `s31smp` | ✅ `SCHEDULING` |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |
| 上游 `rt-thread` | ✅ 零修改 |

已保存 `w106_dualcore_wifi.bin`。

### 115.8 方法学

| 教训 | 说明 |
|---|---|
| **panic 的 MEPC 比错误文本更有信息** | `spimem_flash_ll_set_cs_pin` 直接指向 flash |
| **反汇编一个函数就能证明"这半边被编译掉了"** | 6 条指令 vs 一整套 IPC |
| **关掉一个开关做对照，比读十处源码快** | tick off → WiFi 通 |
| **"某个东西在跑"要问"它跑的代码在哪"** | ISR 在 IRAM，它调的函数在 flash |
| **跨核握手要放在被测 ISR 的最前面** | 否则应答之后还会取 flash |
| **钩子用 weak 声明，组件不反向依赖端口** | 单核构建照样链接 |

---

## 116. W106：USB 端点数量 —— 从芯片自报的 GHWCFG 读出来

### 116.1 为什么不能靠猜

**"S31 有多少个 USB 端点"这个问题，数据手册给的是**系列**值，
而这颗芯片的实例值是综合进 RTL 的。Synopsys DWC OTG 会把配置发布在
`GHWCFG1..GHWCFG4` 四个只读寄存器里 —— 直接读，不用猜。**

**新增 `s31usb`（`port/esp32s31/usb_probe.c`），实机输出：**

```
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

### 116.2 结论

| 项 | 值 | 含义 |
|---|---|---|
| 设备端点数 | **16（含 EP0）** | EP0 + 15 个可自由分配 |
| IN 端口上限 | 7 | 双向核，所以 7 是**数量**上限而不是方向限制 |
| 等时端点 | **0** | **没有周期 IN 端点 ⇒ UAC/UVC 做不了** |
| 方向 | 全部双向 | OUT n 与 IN n 可同时用 |
| DMA | 内部 DMA（arch=2） | CherryUSB dwc2 端口正好断言这一条 |
| FIFO | 896 words = 3584 B | 所有端点共享 |
| PHY | UTMI+（HS） | FS 共享 UTMI，无独立 FS PHY |
| 物理口 | 1 | **同时只能是一个 USB 设备** |

### 116.3 "能当几个 USB 设备用"

**物理上只有一个口 ⇒ 主机看到的是**一个设备**。
但这个设备可以是复合设备（composite），在**一个配置**里放多个接口：
CDC + MSC + HID 可以同时存在，共用一个 VID/PID 和一个设备地址。**

**端点预算（16 个含 EP0）：**

| 功能 | 端点占用 |
|---|---|
| CDC-ACM | EP0 + 1 IN + 1 OUT + 1 IN(notify) = 3 非零 |
| MSC | EP0 + 1 IN + 1 OUT = 2 非零 |
| HID（键盘） | EP0 + 1 IN = 1 非零 |
| CDC + MSC + HID 复合 | 3 + 2 + 1 = **6 非零端点**，完全够 |

**⇒ 16 个端点足够一个相当复杂的复合设备。
真正的限制不是端点数量，而是**没有等时端点**。**

### 116.4 踩到的两个坑（都记在代码注释里）

**(a) 只开 `HP_SYS_CLKRST` 的两个时钟位不够** —— 四个 GHWCFG 全读 0，
连 `GSNPSID` 都是 0（这在正常核上不可能）。完整链路是四步：

```
1. HP_SYS_CLKRST.usb_otghs_ctrl0: apb_clk_en + sys_clk_en
2. CNNT_SYS.usb_otg20_ctrl:       utmifs_clk_en + phyref_clk_en
3. 依次释放 phy_rst → ahb_rst → apb_rst（PHY 必须先稳）
4. HP_ALIVE_SYS.usb_otghs_ctrl:   清 phy_suspendm_force_en
```

**(b) `cnnt_sys_reg.h` 里的 `CNNT_SYS_USB_OTG20_CTRL_REG` 用了 `DR_REG_CNNT_BASE`，
而 `reg_base.h` 只定义了 `DR_REG_CNNT_SYS_REG_BASE`。**
**端口里 `#undef` 后用 0x20359000 + 0x30 重新定义，不改厂商头文件。**

### 116.5 验证

| 检查 | 结果 |
|---|---|
| `s31usb` | ✅ 读到真实 ID `0x4f54430a`（不是 0） |
| `s31test` | ✅ 36/36 |
| `check_rtt_style.py --strict` | ✅ 28/28 |
| `check_image.py` | ✅ 9/9 |

已保存 `w107_usbprobe.bin`。

### 116.6 方法学

| 教训 | 说明 |
|---|---|
| **能力问题去问芯片，不要问文档** | GHWCFG1..4 是 RTL 综合出的实例值 |
| **全 0 一定是没使能，不是"没有"** | `GSNPSID=0` 在正常核上不可能 |
| **读能力之前要按正确顺序上电** | 四步链路，PHY 复位要先释放 |
| **厂商头文件的宏缺依赖，在端口里补** | 不改上游头文件 |
| **"能当几个设备"要拆成"端口数 / 端点数 / 复合能力"** | 物理 1 个口，端点 16 个 |
