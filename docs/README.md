# 文档索引

按「你想知道什么」组织。

---

## 我想先把它跑起来

→ 根目录的 **`README.md`**，或者照「快速开始」五步走。

---

## 我想知道它做了什么、怎么做的

| 文档 | 内容 | 适合谁 |
|---|---|---|
| **`PORTING.md`** | 端口结构、启动流程、中断、时钟、内存、FreeRTOS 兼容层、`--gc-sections` 的坑 | 要读/改端口代码的人 |
| **`PERIPHERALS.md`** | 每个外设的实现状态、RT-Thread 接口、验证命令、**未移植清单** | 要知道「能不能用」的人 |
| **`SMP.md`** | 双核怎么跑通的、三个必须设置的 CSR、CLIC `SHV`、flash 跨核握手 | 要用/改双核的人 |
| **`USB.md`** | USB 方案选型、端点能力（读 GHWCFG 实测）、移植步骤 | 要做 USB 的人 |
| **`ROADMAP.md`** | **尚未移植的资源清单与优先级**、已知 bug、方法学 | 要接手的人 |

---

## 我遇到问题了

→ **`TROUBLESHOOTING.md`**

按现象组织：构建失败、烧录失败、运行时失败、panic 怎么读、检查器报错。

**panic 那节值得先看**——`MCAUSE` 速查表 + 「用 `addr2line` 解 MEPC」
是最省时间的一条路。

---

## 我想看完整历史

| 文档 | 内容 |
|---|---|
| **`porting-log.md`** | 116 节开发日志，**包括所有失败尝试** |
| **`bringup-runbook.md`** | 上板操作手册，按阶段记录怎么验证 |
| **`porting-plan.md`** | 计划、里程碑、风险登记（R1–R358） |

**开发日志刻意保留了「失败」。** 记录哪些做法行不通比记录哪些行得通更有用——
它能防止下一个人重走同一条死路。

---

## 几段最值得读的

### `SMP.md` §4：`mstatus.FS = 0`

双核调试里最难找的一个 bug。陷阱入口无条件 `fsw`，而 CPU1 从复位进入时
`FS=0`，第一条 `fsw` 就是非法指令。

**现象**是「陷阱被取了、handler 没进、核心静默、`mintstatus.MIL=0`」——
六个量全部正确，就是不动。

### `SMP.md` §6：flash 跨核握手

反汇编 `spi_flash_disable_interrupts_caches_and_other_cpu()` 只有 6 条指令，
没有跨核握手——因为 `CONFIG_FREERTOS_UNICORE=y` 把 IDF 的多核那半编译掉了。

**教训**：`MCAUSE` + `addr2line` 指向 `spimem_flash_ll_set_cs_pin`，
说明问题在 flash，不是 Wi-Fi。

### `PORTING.md`：`--gc-sections`

源文件编译了，`.obj` 在，但符号不在 ELF 里。
因为 ESP-IDF 每个组件编成归档，`--gc-sections` 丢掉没人引用的成员。

**这条踩过三次**：`rt_hw_pin_init`、`s31_cpu1_report`、`cmd_usb`。

### `TROUBLESHOOTING.md`：panic 怎么读

`MEPC` 比错误文本信息量大得多。而且要注意 **`Core 0` vs `Core 1`**——
有一次诊断代码本身在 CPU0 上 panic，而当时在查 CPU1 的问题。

---

## 文档维护约定

- `porting-log.md` 每轮开发追加一节
- `bringup-runbook.md` 每验证一个新能力就更新
- `porting-plan.md` 的风险登记持续追加

**`tools/check_rtt_style.py` 会检查文档时效性**：章节号不能重复、
日志头部引的 commit 必须是当前的、文档里写的二进制大小必须和实际一致。

**文档过期会被检查器拦下来**——这是有意设计的，
因为「文档说 A、代码做 B」比没有文档更糟。
