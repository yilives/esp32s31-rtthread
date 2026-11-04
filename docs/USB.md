# USB 移植说明（USB.md）

ESP32-S31 有 **一个 USB OTG 2.0 高速控制器**（Synopsys DWC OTG）。
这份文档回答三个问题并给出移植步骤：

1. 用哪个 USB 栈？
2. 端点有多少、能当几个 USB 设备用？
3. 要移植还需要做哪些事？

---

## 1. 芯片能力：读出来的，不是查来的

`msh > s31usb` 直接读 Synopsys DWC 的 `GHWCFG1..GHWCFG4` 寄存器
（RTL 综合时写死的只读寄存器，是**实例值**而不是系列值）：

```
s31usb: Synopsys ID 0x4f54430a (DWC OTG rev 79.54)
s31usb: GHWCFG1=00000000 GHWCFG2=239ffed2 GHWCFG3=038046e8 GHWCFG4=de11aa30
s31usb: device endpoints (EP0 included) : 16  (7 IN, 0 periodic IN)
s31usb: endpoint directions             : 0 bi, 1 bi, 2 bi, ... 15 bi
s31usb: DMA architecture                : 2 (internal DMA)
s31usb: HS PHY UTMI+ and ULPI, FS PHY shared with UTMI+
s31usb: total FIFO 896 words = 3584 bytes on this core
s31usb: interrupt sources               : OTGHS=99, multi-proc=100, serial-jtag=2
s31usb: one physical port; a composite device can use at most 16 endpoint(s)
s31usb: isochronous endpoints           : 0 (no UAC / UVC streaming)
```

### 逐项解释

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

---

## 2. 能当几个 USB 设备用

**物理口只有一个**（`SOC_USB_OTG_PERIPH_NUM=1`）⇒
主机看到的**永远是一个设备地址**。

但**一个配置里可以放多个接口**，这就是复合设备（composite）。
端点预算（16 个含 EP0，即 15 个可用）：

| 功能 | 非零端点占用 |
|---|---|
| CDC-ACM（虚拟串口） | 3（bulk IN + bulk OUT + interrupt IN） |
| MSC（U 盘） | 2（bulk IN + bulk OUT） |
| HID（键盘/鼠标） | 1（interrupt IN） |
| MIDI | 2 |
| 自定义 vendor | 2 |

**举例：CDC + MSC + HID = 6 个非零端点，占 15 个里的 6 个。**

**⇒ 端点数量不是瓶颈，可以做一个相当复杂的复合设备。**

### 真正的瓶颈：没有等时端点

| 想做 | 可行性 |
|---|---|
| CDC / MSC / HID / MIDI / DFU / vendor | ✅ |
| USB 网卡（ECM / RNDIS / NCM） | ✅（bulk） |
| **UAC 音频（麦克风/扬声器）** | ❌ |
| **UVC 摄像头** | ❌ |

**这是 RTL 综合时就没放，不是驱动问题。**

---

## 3. 用哪个栈：CherryUSB

RT-Thread 5.4.0 里有两套 USB 栈：

| | `components/legacy/usb/` | `components/drivers/usb/cherryusb/` |
|---|---|---|
| 状态 | 旧栈（legacy） | 当前推荐 |
| 设备类 | `cdc_vcom` `mstorage` `hid` `ecm` `rndis` `winusb` `audio_*`（7 类） | `cdc` `msc` `hid` `midi` `adb` `audio` `video` `dfu` `vendor` 等 20+ |
| 主机类 | `hub` + core | `hub` `serial` `msc` 等 |
| **控制器驱动** | **全树搜不到 DWC/OTG/EHCI/OHCI** | **`port/dwc2/usb_dc_dwc2.c` + `usb_hc_dwc2.c`** |
| ESP 平台胶水 | 无 | **`port/dwc2/usb_glue_esp.c`** |
| RT-Thread OSAL | 内置 | **`osal/usb_osal_rtthread.c`** |

**关键一条**：legacy 的 `usbdevice/` 只有 `core/` 和 `class/`，
需要 BSP 自己实现 `usb_dc_*` 控制器驱动。
**CherryUSB 直接带 DWC2 设备与主机驱动。**

**而且它的断言与本核实测相符：**

```c
USB_ASSERT_MSG(g_dwc2_udc[busid].hw_params.arch == GHWCFG2_INT_DMA_ARCH,
               "This dwc2 version does not support ...");
```

实测 `GHWCFG2.ARCH = 2 = internal DMA` ✓

### 为什么不用 TinyUSB

**这个 IDF fork 里没有 `esp_tinyusb` / `tinyusb` 组件。**
`components/` 下的 USB 相关目录只有：

```
esp_driver_usb_serial_jtag     USB Serial/JTAG（不是 OTG）
esp_hal_usb                    HAL 层
esp_usb_cdc_rom_console        ROM console
```

用 TinyUSB 需要自己拉依赖并写全部 glue；CherryUSB 已经在树里，
只差三处平台适配。

---

## 4. 移植步骤：还差三处

CherryUSB 的 `port/dwc2/usb_glue_esp.c` **不认识 S31**（只判 S2/S3/P4）。
需要改：

### (a) 中断源

```c
/* 现在（S31 上 ETS_USB_INTR_SOURCE 根本不存在） */
#define GET_USB_INTR_SOURCE(reg_base)   ETS_USB_INTR_SOURCE

/* S31 应为 */
#define GET_USB_INTR_SOURCE(reg_base)   ETS_USB_OTGHS_INTR_SOURCE   /* = 99 */
```

### (b) PHY 类型

```c
/* 现在 */
#define GET_USB_PHY_TARGET(reg_base)    USB_PHY_TARGET_INT

/* S31 无 FSLS PHY（SOC_USB_FSLS_PHY_NUM=0），应改为 */
#define GET_USB_PHY_TARGET(reg_base)    USB_PHY_TARGET_UTMI
```

### (c) 用户参数

```c
void dwc2_get_user_params(uint32_t reg_base, struct dwc2_user_params *params)
{
#if CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3
    memcpy(params, &param_fs, ...);
#elif CONFIG_IDF_TARGET_ESP32P4
    ...
+#elif CONFIG_IDF_TARGET_ESP32S31
+   memcpy(params, &param_hs, sizeof(struct dwc2_user_params));
#endif
}
```

`param_hs` 里的 FIFO 划分是按 896 words 总量算的，与本核实测一致。

### (d) 线程绑核

`rt-thread/components/drivers/usb/cherryusb/osal/usb_osal_rtthread.c:16`：

```c
htask = rt_thread_create(name, entry, args, stack_size, prio, 10);
```

**没有绑核。** 按 WiFi 的教训，USB 线程在双核开启时也要绑到 CPU0：

```c
#ifdef RT_USING_SMP
    rt_thread_control(htask, RT_THREAD_CTRL_BIND_CPU, (void *)0);
#endif
```

---

## 5. 时钟与复位：一个必须按顺序做的四步链

**先只开两个时钟位，读出来全是 0**——包括 `GSNPSID`。
`GSNPSID` 是 Synopsys ID，正常核上不可能是 0，
所以全 0 只说明**外设没使能**，不是"没有这个外设"。

完整链路（参考 `esp_hal_usb/esp32s31/include/hal/usb_utmi_ll.h`）：

```c
/* 1. 时钟 */
REG_SET_BIT(HP_SYS_CLKRST_USB_OTGHS_CTRL0_REG,
            HP_SYS_CLKRST_REG_USB_OTGHS_APB_CLK_EN);
REG_SET_BIT(HP_SYS_CLKRST_USB_OTGHS_CTRL0_REG,
            HP_SYS_CLKRST_REG_USB_OTGHS_SYS_CLK_EN);
REG_SET_BIT(CNNT_SYS_USB_OTG20_CTRL_REG, CNNT_SYS_USB_OTG20_UTMIFS_CLK_EN);
REG_SET_BIT(CNNT_SYS_USB_OTG20_CTRL_REG, CNNT_SYS_USB_OTG20_PHYREF_CLK_EN);

/* 2. 复位释放 —— PHY 先稳，再放总线和寄存器 */
REG_CLR_BIT(CNNT_SYS_USB_OTG20_CTRL_REG, CNNT_SYS_USB_OTG20_PHY_RST_EN);
REG_CLR_BIT(CNNT_SYS_USB_OTG20_CTRL_REG, CNNT_SYS_USB_OTG20_AHB_RST_EN);
REG_CLR_BIT(CNNT_SYS_USB_OTG20_CTRL_REG, CNNT_SYS_USB_OTG20_APB_RST_EN);

/* 3. 让 DWC 自己控制 PHY suspend/resume */
REG_CLR_BIT(HP_ALIVE_SYS_USB_OTGHS_CTRL_REG,
            HP_ALIVE_SYS_REG_USB_OTGHS_PHY_SUSPENDM_FORCE_EN);
```

`usb_new_phy()`（在 `esp_hw_support/usb_phy/usb_phy.c`，已编译进镜像）
内部会走 `usb_utmi_hal_init()`，把这些做全，所以用它就不用手写。

### 一个厂商头文件的坑

`cnnt_sys_reg.h` 里：

```c
#define CNNT_SYS_USB_OTG20_CTRL_REG (DR_REG_CNNT_BASE + 0x30)
```

但 `reg_base.h` 只定义了 `DR_REG_CNNT_SYS_REG_BASE`（0x20359000），
**没有 `DR_REG_CNNT_BASE`**。

`port/esp32s31/usb_probe.c` 里这样处理（不改厂商头文件）：

```c
#undef CNNT_SYS_USB_OTG20_CTRL_REG
#define CNNT_SYS_USB_OTG20_CTRL_REG (DR_REG_CNNT_SYS_REG_BASE + 0x30)
```

---

## 6. 一个物理口，两套控制器

S31 同时有：

| 控制器 | 用途 | 中断源 |
|---|---|---|
| **USB OTG**（DWC） | 通用 USB 设备/主机 | 99（+100 多核端点） |
| **USB Serial/JTAG** | 调试串口 + JTAG | 2 |

两者**共用 PHY 引脚**，不能同时用。切换通过
`LP_SYSTEM_REG_SW_HW_USB_PHY_SEL` / `LP_SYSTEM_REG_SW_USB_PHY_SEL`。

**做 OTG 时要注意**：如果 eFuse 里的 `DIS_USB_JTAG` 没烧，
USB Serial/JTAG 可能抢着驱动引脚。

---

## 7. 验证方式

**不用等 USB 栈移植完，现在就能验证芯片能力：**

```
msh > s31usb
```

输出里的 `GSNPSID` 必须是 `0x4f54430a` 这样的真实值。
**如果全是 0，说明时钟链没做对**，而不是芯片没这个外设。

**移植完 USB 栈之后怎么验：**

1. 插到 PC，`dmesg` / 设备管理器应该出现一个新设备
2. 如果做的是 CDC：PC 上会多一个串口，双向收发
3. 如果做的是 MSC：PC 上会多一个磁盘
4. `msh > s31usb` 的 `DIEPCTL0` / `DOEPCTL0`（加 `-d` 参数）应该非零

---

## 8. 建议的移植顺序

1. **CDC-ACM 虚拟串口** —— 最简单，2 个非零端点，验证整条链路
2. **MSC** —— 复用 bulk 通路，加 SCSI 命令处理
3. **复合设备（CDC + MSC）** —— 验证多接口描述符
4. HID / MIDI 按需

**不要一上来就做 UAC/UVC** —— 没有等时端点，做了也跑不了。
