# 排障手册（TROUBLESHOOTING.md）

这份文档按"现象"组织。找到你的现象，按对应条目排查。

---

## 一、构建相关

### `ERROR: ESP-IDF not found`

```
ERROR: ESP-IDF not found at <path>
```

`build.ps1` 默认找 `..\esp-idf`（压缩包自带的那个）。
如果你把 `esp-idf/` 移走了：

```powershell
.\tools\build.ps1 -IdfPath D:\somewhere\esp-idf build
```

或设环境变量 `IDF_PATH`。

### `No ESP-IDF Python virtualenv under ...`

```
ERROR: No ESP-IDF Python virtualenv under C:\Users\<you>\.espressif\python_env
```

工具链没装。执行：

```powershell
cd <本包目录>\esp-idf
.\install.ps1 esp32s31
```

**注意**：不要执行 `export.ps1`。`build.ps1` 自己会设环境，
而且 `export.ps1` 要求 PowerShell 允许运行未签名脚本。

### `The CMAKE_C_COMPILER: riscv32-esp-elf-gcc is not a full path`

工具链目录没进 PATH。

**原因**：`idf_tools.py export` 会在"某个工具没装"时整体失败
（例如 xtensa 工具链，S31 用不到），于是它本该打印的 PATH 也没了。

`build.ps1` 会回退到直接搜 `riscv32-esp-elf-gcc.exe`。如果还是失败，
手动确认：

```powershell
Get-ChildItem "$env:USERPROFILE\.espressif\tools\riscv32-esp-elf" -Recurse -Filter riscv32-esp-elf-gcc.exe
```

找到后：

```powershell
$env:PATH = "<那个目录>;" + $env:PATH
.\tools\build.ps1 build
```

### `fatal: not a git repository`

**无害。** IDF 的 CMake 会尝试读 git 版本号来生成版本字符串，
压缩包没有 `.git` 所以报这一句。构建继续。

### `enabled ccache in build but ccache program not found`

**无害。** 只是没用上 ccache，构建会慢一些。

要装：

```powershell
cd <本包目录>\esp-idf
.\install.ps1 esp32s31
```

---

## 二、烧录相关

### `waiting for download` / 一直停在 `boot:0x48`

**`0x48` 是下载模式**，说明板子进了 bootloader 而不是跑应用。

**最常见原因：串口助手占着 COM 口并且拉了 DTR。**

DTR 接的是复位/BOOT 选择电路，被拉低就强制进下载模式。
排查：

1. 关掉所有串口助手（MobaXterm、SSCOM、VSCode 串口插件……）
2. 确认没有别的 python 进程

```powershell
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
```

3. 重新烧录

**这不是"端口被占用"的错误**——端口能打开，只是 DTR 状态不对。

### 端口被占用

```powershell
# 找出占用 COM7 的进程
Get-CimInstance Win32_SerialPort | Where-Object { $_.DeviceID -eq 'COM7' }
```

或者直接杀掉所有 python：

```powershell
Get-Process python | Stop-Process -Force
```

### 烧完不启动，反复复位

看复位原因（`rst:0xN`）：

| 值 | 含义 | 常见原因 |
|---|---|---|
| `0x1` | POWERON | 正常 |
| `0x8` | `HP_SYS_HP_WDT1_RESET` | 看门狗。CPU1 在 CPU0 调度器就绪前就开始调度 |
| `0xc` | `SW_CPU_RESET` | 软件复位，通常是 panic 后重启 |
| `0x10` | `CHIP_LP_WDT_RESET` | 低功耗看门狗 |
| `0x1a` | `CPU_LOCKUP_RESET` | 核心锁死，通常是 cache 相关 |
| `0x10`/`0x1a` 交替 | | 见 `SMP.md` §6 flash 握手 |

---

## 三、运行时

### `s31test` 有几项失败

**先看是哪几项。**

| 失败项 | 大概率原因 |
|---|---|
| `irq` / `nest` | **GPIO10 / GPIO11 没接线**（这不是移植问题） |
| `twai` | 没接 CAN 收发器 |
| `spiloop` | MOSI / MISO 没短接 |
| `psram` | 板子没有 PSRAM，或 `CONFIG_SPIRAM` 配置不对 |
| `tick` / `clock` | 真问题，时钟源没跑对 |
| `ctx` / `preempt` | 真问题，调度或上下文切换 |
| `sem` / `rhwh` / `dev` / `fbshim` | 真问题 |

**跑单个子项定位：**

```
msh > s31test tick
msh > s31test irq
```

### `s31test irq` 失败

**它需要你在 GPIO10 上产生边沿。** 两种做法：

1. 把 GPIO10 接到 GND，然后手动碰 3V3
2. 把 GPIO10 和 GPIO11 短接（`nest` 需要这个）

悬空的话读数是随机的，测试会失败并打印接线提示。

### `s31smp` 报 `entered the scheduler but ran nothing`

CPU1 进了调度器但抢不动空闲线程。

**原因**：CPU1 没有 tick。检查 `rtconfig.h` 里 `RT_SMP_CPU1_NO_TICK` 是否被定义。

**如果没定义还是这样**，看引导日志里：

```
smp: cpu1 stage=4 tick_started=1 isr_runs=N ticks=M
```

`isr_runs=0` 说明中断没进 handler，参照 `SMP.md` §4/§5 检查
`mstatus.FS` 和 CLIC 的 `SHV` 位。

### `s31wifi` 一跑就复位

**已修**，但如果你的构建是旧版本，看 `SMP.md` §6。

现在的版本 `s31wifi 你的SSID 你的密码` 应该在 1-2 秒内打印 IP。

### Wi-Fi 连不上

```
s31wifi: FAIL, ... 
```

| 现象 | 原因 |
|---|---|
| 扫描能看到但连不上 | 密码错，或只支持 2.4 GHz（S31 没有 5 GHz） |
| 扫描什么都看不到 | 天线没接，或 AP 是 5 GHz |
| `WIFI_AUTH` 失败 | 密码超过 63 字符，或 AP 用了 WPA3-only |

**先扫一下确认射频正常：**

```
msh > s31wifiscan
```

### `pwm get 0` 打印 `Duty cycle : 2147483647%`

**上游 bug，无害。** 除零导致。功能正常，只是显示不对。
记录在 `porting-log.md`，未修。

### `bind <运行中的线程> 1` 之后线程卡死

**已知问题，未修。**

把一个正在运行的线程 bind 到另一个核，会让它卡住。
变通做法：在创建时绑核，而不是运行时绑：

```c
rt_thread_control(tid, RT_THREAD_CTRL_BIND_CPU, (void *)0);  /* 启动前 */
rt_thread_startup(tid);
```

### `console set uart0` 之后 shell 输入没了

**已知问题，未修。** 这个命令会改控制台配置，改完 shell 收不到输入。

**恢复**：复位板子。不要在不清楚后果时执行这个命令。

### `s31twai` 第一次通过，第二次失败

**已知问题，原因未查明。**

同一会话里重复跑会失败（0/4），冷启动后第一次正常（4/4）。

**变通**：复位后再跑。

---

## 四、panic 怎么读

panic 输出里最重要的不是错误文本，是 **`MEPC`**。

```
Guru Meditation Error: Core  0 panic'ed (Load access fault)
MEPC    : 0x40019e70  RA : 0x40019e5c
MCAUSE  : 0x00000005  MTVAL : 0x108110b0
```

用 `addr2line` 解出位置：

```powershell
$od = Get-ChildItem "$env:USERPROFILE\.espressif\tools\riscv32-esp-elf" -Recurse -Filter "riscv32-esp-elf-addr2line.exe" | Select-Object -First 1
& $od.FullName -e project\build\s31_rtt_test.elf -f -C 0x40019e70
```

### `MCAUSE` 速查

| 值 | 含义 |
|---|---|
| 1 | 指令访问错误 |
| 2 | 非法指令 |
| 5 | **读访问错误**（常见：空指针、已释放内存） |
| 7 | **写访问错误**（常见：向只读区写、已释放内存） |
| 0x19 | **cache 错误**（flash cache 关闭时取指） |

### 常见组合

| MEPC 位置 | 含义 |
|---|---|
| `rt_hw_spin_lock` | 内核锁了一个已释放的对象 |
| `rt_object_get_type` ← `rt_smem_free` | 双重释放 |
| `spimem_flash_ll_*` + `MCAUSE 0x19` | flash cache 关闭时访问 flash |
| `_panic_handler` 自己 | 中断配错（见 `SMP.md` §5 SHV） |

**`Core 0` vs `Core 1` 也要看。**
如果 panic 报 `Core 0` 但你怀疑是 CPU1 的问题，
可能是**诊断代码本身**跑在 CPU0 上出的错。

> **踩过的坑**：读 `mintstatus` 时把 CSR 号写成 `0x346`（正确是 `0xFB1`），
> 结果 CPU0 上一条 `csrr` 触发非法指令，panic 报 `Core 0`，
> 而我当时在查 CPU1 的问题。**诊断代码本身会 panic。**

---

## 五、检查器

### `check_image.py` 失败

```powershell
python tools\check_image.py
```

9 项检查，每项都对应一个"能编译但跑不对"的历史问题：

| 检查 | 失败意味着 |
|---|---|
| flash-mapped LOAD 段数 | 链接脚本变了 |
| 三个上下文帧大小一致 | 栈帧构造和陷阱入口对不上 |
| 陷阱出口 `mstatus` 带 FS=3 | 第一次 `flw` 会非法指令 |
| `__fsymtab` / `__rt_init` 非空 | 表被 gc-sections 丢了 → 没有 msh |
| init 表顺序 | `.rti_fn` 层级排序错 |
| 向量表可达 | mtvt 指向不对 |

### `check_rtt_style.py` 失败

```powershell
python tools\check_rtt_style.py --strict
```

28 项规范检查。常见失败：

| 检查 | 含义 |
|---|---|
| `every port source is compiled` | 有 `.c` 文件没加进 CMakeLists |
| `runbook binary size is current` | 文档里的二进制大小过期 |
| `no comma in cmd descriptions` | `MSH_CMD_EXPORT_ALIAS` 的描述里有逗号（会编译失败） |
| `doc section numbers unique` | 文档章节号重复 |

**`--strict` 会把 warning 也当失败。**

---

## 六、还是不行

按这个顺序收集信息：

```powershell
# 1. 完整启动日志（不是滚动的，是一次性的）
python tools\reset.py COM7 8

# 2. 运行时状态
python tools\board.py COM7 "s31stat"
python tools\board.py COM7 "s31smp"
python tools\board.py COM7 "s31usb"

# 3. 全量自检
python tools\board.py COM7 "s31test"

# 4. 镜像检查
python tools\check_image.py
python tools\check_rtt_style.py --strict
```

**`tools/reset.py` 比串口助手好用**：它拉 DTR 复位然后抓完整日志，
不会因为滚动丢失最关键的前 200 行。

**日志里这几行最有价值：**

```
rst:0x...                    ← 复位原因
smp: cpu1 alive=... entries=...   ← CPU1 是否真的跑了
smp: cpu1 stage=... isr_runs=...  ← CPU1 的中断是否进得去
Guru Meditation ...          ← 有 panic 的话
```

---

## 七、向别人求助时提供什么

最少这些：

1. **复位原因**（`rst:0x...`）和**完整的启动日志**
2. `s31smp` 和 `s31stat` 的输出
3. `s31test` 的完整输出（哪几项失败）
4. 板子型号（是不是 WROOM-3，有没有 PSRAM）
5. 你改过哪些配置

**不要只发一句"跑不起来"** —— 上面这些信息在日志里都有，
`tools/reset.py` 一次就能抓全。
