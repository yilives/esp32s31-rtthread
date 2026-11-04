"""
RT-Thread port conformance checker.

Two rounds of the audit were done with ad-hoc greps, which means the findings would
have been lost the moment the shell history was. This script is the same checks,
written down, so they can be re-run after every change and a regression shows up as
a number going the wrong way instead of as a surprise six rounds later.

Each check states what RT-Thread expects and why, because "the number went up" is
not actionable without that.

Usage:
    python tools/check_rtt_style.py            # report
    python tools/check_rtt_style.py --strict   # exit 1 on any FAIL

Exit codes: 0 = all checks OK, 1 = a check failed (only with --strict).
"""
import argparse
import pathlib
import re
import sys

# Paths are derived from this file, so the checkout works wherever it is cloned.
#
# This repository's layout:
#
#     <root>/tools/check_rtt_style.py     <- this file
#     <root>/component/                   the RT-Thread component
#     <root>/project/main/                the application
#
# The same script also runs inside a fully assembled tree, where the component has
# been copied into esp-idf/components/rtthread/. Both are handled: component/ wins
# if it exists, because that is the authoritative copy in this repository.
ROOT = pathlib.Path(__file__).resolve().parent.parent

if (ROOT / "component" / "CMakeLists.txt").is_file():
    PORT = ROOT / "component" / "port"
    CMAKE = ROOT / "component" / "CMakeLists.txt"
    IDF = None
else:
    # Assembled layout (an unpacked release, or the authoring workspace).
    if not (ROOT / "esp-idf").is_dir():
        for cand in pathlib.Path(__file__).resolve().parents:
            if (cand / "esp-idf-rtt").is_dir():
                ROOT = cand
                break
    IDF = ROOT / ("esp-idf-rtt" if (ROOT / "esp-idf-rtt").is_dir() else "esp-idf")
    PORT = IDF / "components/rtthread/port"
    CMAKE = IDF / "components/rtthread/CMakeLists.txt"

BOARD = PORT / "common/board.c"
APP = ROOT / ("s31-rtt-test" if (ROOT / "s31-rtt-test").is_dir() else "project") / "main"
RTCONFIG = PORT / "esp32s31/rtconfig.h"

# Where the build output lives. Depending on the layout it is under project/build,
# s31-rtt-test/build, or next to whichever project directory exists.
def _build_dir():
    for cand in (ROOT / "project/build", ROOT / "s31-rtt-test/build"):
        if cand.is_dir():
            return cand
    return ROOT / "project/build"


BUILD = _build_dir()

# Documents live in docs/ in this repository and at the root in the authoring tree.
def _doc(name):
    for cand in (ROOT / "docs" / name, ROOT / ("rtthread-esp32-s31-" + name)):
        if cand.is_file():
            return cand
    return ROOT / "docs" / name



def _toolchain_roots():
    """Where to look for riscv32-esp-elf-* binaries.

    IDF installs tools under IDF_TOOLS_PATH (default ~/.espressif). Honouring the
    environment variable means this works for any user, and falling back to the
    standard location means it works with no setup at all.
    """
    import os
    base = os.environ.get("IDF_TOOLS_PATH") or (pathlib.Path.home() / ".espressif")
    return pathlib.Path(base) / "tools" / "riscv32-esp-elf"


results = []


def check(name, ok, detail):
    results.append((name, ok, detail))


def read(p):
    return p.read_text(encoding="utf-8", errors="replace")


def defined_macros():
    """Macros rtconfig.h actually defines, for the guard evaluator below."""
    cfg = PORT / "esp32s31" / "rtconfig.h"
    if not cfg.exists():
        return set()
    return set(re.findall(r"^\s*#define\s+(\w+)", read(cfg), re.M))


def disabled_regions(text, defined):
    """Line numbers that sit inside an ``#ifdef``/``#ifndef`` whose macro is not defined.

    A text scanner cannot see conditional compilation, so a grep for a symbol also finds
    the ones behind a switch that is off - and then reports them as missing from the
    image, which is true and useless. This tracks the guard stack and returns the lines
    that are not part of the build.

    Only ``#ifdef``/``#ifndef`` are evaluated. ``#if`` expressions are not: guessing at
    those from text is how a check starts lying, and this port uses ``#ifdef`` for feature
    switches.
    """
    out = set()
    stack = []

    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("#ifdef "):
            stack.append(s.split()[1] not in defined)
        elif s.startswith("#ifndef "):
            stack.append(s.split()[1] in defined)
        elif s.startswith("#if "):
            stack.append(False)
        elif s.startswith("#else"):
            if stack:
                stack[-1] = not stack[-1]
        elif s.startswith("#endif"):
            if stack:
                stack.pop()
        if any(stack):
            out.add(i)
    return out


def main():
    strict = "--strict" in sys.argv
    port_c = sorted(PORT.rglob("*.c"))

    # 1. Init functions return rt_err_t.
    #    Upstream: every rt_hw_xxx_init / xxx_init returns rt_err_t and RT_EOK.
    bad = []
    for f in port_c:
        t = read(f)
        for m in re.finditer(r"^int ((?:rt_hw_|s31_)\w*_init)\(void\)", t, re.M):
            bad.append(f"{f.name}:{m.group(1)}")
    check("init returns rt_err_t", not bad, f"{len(bad)} int-returning: {bad}")

    # 2. Every driver init is exported through the init table.
    #
    #    This used to be `len(exports) >= 10`. There are 11, so removing one kept it
    #    green - reverse-verified, and therefore decoration. The assertion that
    #    actually holds the property is the pairing: an init function that exists but
    #    is never exported is a driver that silently does not initialise.
    # Feature switches decide what is compiled at all; skip what a disabled guard covers.
    _defined = defined_macros()
    _disabled = {f: disabled_regions(read(f), _defined) for f in port_c}

    def _in_disabled(f, pos, text):
        return text[:pos].count("\n") + 1 in _disabled[f]

    exports = {}
    for f in port_c:
        t = read(f)
        for m in re.finditer(r"^INIT_(BOARD|DEVICE|COMPONENT|APP)_EXPORT\((\w+)\)", t, re.M):
            if _in_disabled(f, m.start(), t):
                continue
            exports[m.group(2)] = m.group(1)

    inits = set()
    for f in port_c:
        t = read(f)
        for m in re.finditer(r"^rt_err_t ((?:rt_hw_)\w*_init)\(void\)", t, re.M):
            if _in_disabled(f, m.start(), t):
                continue
            inits.add(m.group(1))

    #    A hand call from board.c is a legitimate alternative, and the two that use it
    #    are well documented (the interrupt watchdog, because the IDF path that starts
    #    it is skipped; the nest table, because it must exist before any trap). Those
    #    two are guarded by a separate assertion below, so here they are accepted.
    #    Careful: board.c also *declares* these in `extern int rt_hw_x_init(void);`
    #    lines, so a plain substring test finds a declaration and calls it a call -
    #    which is how this assertion stayed green when an export was removed. A real
    #    call is an indented statement, not an extern declaration.
    board_calls = read(PORT / "common" / "board.c") if (PORT / "common" / "board.c").exists() else ""
    called = set()
    for line in board_calls.splitlines():
        s = line.strip()
        if s.startswith("extern "):
            continue
        m = re.match(r"(\w+)\s*\(", s)
        if m:
            called.add(m.group(1))
    # A call from any other port source counts too. The CPU1 tick is started by
    # smp_slave_sched.c rather than from board.c, and exporting it would move it into the
    # board-init band, which is the wrong stage - so "exported or called from board.c"
    # states less than the property it is trying to check.
    for i in list(inits):
        if i in exports or i in called:
            continue
        for f in port_c:
            if f.name == "board.c":
                continue
            body = read(f)
            for line in body.splitlines():
                s = line.strip()
                if s.startswith(("extern ", "*", "/*", "//")):
                    continue
                if re.match(rf"\b{i}\s*\(", s):
                    called.add(i)
                    break
            if i in called:
                break

    orphaned = sorted(i for i in inits if i not in exports and i not in called)
    check("every init is exported or called", not orphaned,
          f"neither exported nor called from board.c: {orphaned}" if orphaned
          else f"{len(inits)} init functions, each exported "
               f"({len(exports)} export(s)) or called from board.c")

    # 3. Every exported init must actually reach the image.
    #    ESP-IDF compiles each component to an archive and --gc-sections removes
    #    members nothing references; a .rti_fn entry alone is not a reference in a
    #    linker that has not reached that member yet. The fix is `-u`, but the
    #    property to verify is the outcome, not the flag: if the symbol is in the
    #    ELF, the board band will call it.
    #
    #    (An earlier version of this check demanded a -u per export and reported
    #    board.c's own three exports as failures. They are in the same object as
    #    rt_hw_board_init, which is obviously referenced, so they arrive without
    #    one. Checking the ELF says what is true; checking the flag said what is
    #    usually true.)
    elf = BUILD / "s31_rtt_test.elf"
    if elf.exists():
        nm = None
        for cand in _toolchain_roots().rglob(
                "riscv32-esp-elf-nm.exe"):
            nm = cand
            break
        if nm is not None:
            import subprocess
            syms = subprocess.run([str(nm), "-C", str(elf)], capture_output=True,
                                  text=True, errors="replace").stdout
            absent = [e for e in exports if f" {e}" not in syms]
            check("exports reach the image", not absent,
                  f"absent from ELF: {absent}" if absent else
                  f"all {len(exports)} present")
        else:
            check("exports reach the image", True, "nm not found, skipped")
    else:
        check("exports reach the image", True, "no ELF yet, skipped")

    # 4. board.c should not hand-call driver inits any more.
    #    Upstream: rt_hw_board_init is clock/heap/pin only; drivers register
    #    themselves and rt_components_board_init() runs them.
    board = read(BOARD)
    hand = re.findall(r"^\s+((?:rt_hw_|s31_)\w+)\(\);", board, re.M)
    driver_hand = [x for x in hand if "link_anchor" not in x]
    check("board.c has few hand calls", len(driver_hand) <= 2,
          f"{len(driver_hand)} hand calls: {driver_hand}")

    # 5. Link anchors must not be called *_init - the name implies setup logic.
    chains = []
    for f in port_c:
        t = read(f)
        if re.search(r"^\s+int \w+_init\(void\)\s*\{\s*return 0;\s*\}", t, re.M):
            chains.append(f.name)
    check("anchors not named _init", not chains, f"suspicious: {chains}")

    # 6. rt_hw_console_output belongs to the driver layer, not the application.
    in_app = [f.name for f in APP.glob("*.c") if "void rt_hw_console_output" in read(f)]
    in_port = [f.name for f in PORT.rglob("*.c") if "void rt_hw_console_output" in read(f)]
    check("console hook in driver layer", not in_app and in_port,
          f"app={in_app} port={in_port}")

    # 7. Drivers use the RT-Thread device framework, not raw registration.
    #    i2c/spi/pin/serial/adc must go through their framework helpers.
    framework = {
        "i2c.c": "rt_i2c_bus_device_register",
        "spi.c": "rt_spi_bus_register",
        "gpio.c": "rt_device_pin_register",
        "serial.c": "rt_hw_serial_register",
        "adc.c": "rt_hw_adc_register",
    }
    wrong = []
    for name, fn in framework.items():
        p = PORT / "esp32s31" / name
        if p.exists() and fn not in read(p):
            wrong.append(f"{name} lacks {fn}")
    check("framework registration used", not wrong, f"{wrong}" if wrong else "all five OK")

    # 8. No bare uint32_t for locals that stay local - RT uses rt_uint32_t.
    #
    #    Two exceptions are correct and must not be "fixed":
    #      - parameters of the FreeRTOS shim (freertos_api.c) must match the
    #        FreeRTOS signature exactly or the shim stops being a drop-in;
    #      - a local whose address is passed to an IDF LL function has to be the
    #        LL's own uint32_t, because rt_uint32_t is a different typedef and
    #        the pointer types would not agree (measured: -Wincompatible-pointer-types).
    bare = []
    for f in port_c:
        if f.name == "freertos_api.c":
            continue
        t = read(f)
        for m in re.finditer(r"^\s+uint32_t (\w+)[,;\[]", t, re.M):
            bare.append(f"{f.name}:{m.group(1)}")
    ll_output = {"lo", "hi", "cnt"}   # address taken by gpio_ll / i2c_ll
    bare = [b for b in bare if b.split(":")[1] not in ll_output]
    check("rt_uint32_t in declarations", not bare, f"{bare}" if bare else "none left")

    # 9. Every msh command object must be kept in the image.
    #
    #    MSH_CMD_EXPORT only writes into the KEEP()'d FSymTab section. --gc-sections
    #    cannot see a data-section reference, so a command file with no referenced
    #    global is dropped whole and its commands silently cease to exist. That has
    #    happened four times here (psram, led, rmt, twai, smp_cmd).
    #
    #    There are exactly two mechanisms that keep such a file:
    #      (a) a `*_link_anchor()` that board.c calls, or
    #      (b) `-u <global symbol>` in the rtthread CMakeLists.
    #
    #    The old assertion was `len(cmd_files) > 0`, which cannot fail while any
    #    command exists - reverse-verified: renaming an anchor left it green. This
    #    version asks the question that matters per file.
    board_src = read(PORT / "common" / "board.c") if (PORT / "common" / "board.c").exists() else ""
    cmake_src = read(CMAKE)
    u_symbols = set(re.findall(r"-u (\w+)", cmake_src))

    unkept = []
    kept_by_anchor = kept_by_u = 0
    for f in port_c:
        src = read(f)
        if "MSH_CMD_EXPORT" not in src:
            continue
        anchors = re.findall(r"void (s31_\w+_link_anchor)\(void\)", src)
        has_anchor = any((a + "(") in board_src for a in anchors)
        # Any non-static global this file defines that a -u names.
        globals_here = set(re.findall(
            r"^(?:rt_err_t|void|int|rt_uint\w+|rt_base_t|rt_bool_t)\s+"
            r"(\w+)\s*\(", src, re.M))
        has_u = bool(globals_here & u_symbols)
        if has_anchor:
            kept_by_anchor += 1
        elif has_u:
            kept_by_u += 1
        else:
            unkept.append(f.name)

    check("msh commands kept in image", not unkept,
          f"no anchor and no -u: {unkept}" if unkept
          else f"{kept_by_anchor} by called anchor, {kept_by_u} by -u, 0 unkept")

    # 10. FinSH is bound to a real device.
    app_main = read(APP / "main.c")
    check("shell bound to a device", 'finsh_set_device("uart0")' in app_main,
          "finsh_set_device(\"uart0\")" if 'finsh_set_device("uart0")' in app_main
          else "not found")

    # 11. rtconfig.h comments must not contradict its values.
    #     A stale comment is worse than none: it makes a deliberate choice look
    #     like an oversight, and the next reader cannot tell which to believe.
    rtcfg = read(RTCONFIG)
    contradictions = []
    if "RT_CPUS_NR                      2" in rtcfg and \
       "run the kernel on the PRO core only" in rtcfg:
        contradictions.append("says single-core but RT_CPUS_NR is 2")
    if "#define RT_USING_DEVICE" in rtcfg and \
       "No networking, filesystem, Finsh, devices or console" in rtcfg:
        contradictions.append("says no devices/console but both are enabled")
    if "#define RT_USING_FINSH" in rtcfg and \
       "No networking, filesystem, Finsh, devices or console" in rtcfg:
        contradictions.append("says no Finsh but RT_USING_FINSH is set")
    check("rtconfig.h comments consistent", not contradictions,
          f"{contradictions}" if contradictions else "no stale claims found")

    # 12. Port-owned rthw.h hooks must be STRONG, not the weak stub.
    #     kservice.c defines rt_weak defaults for rt_hw_us_delay, rt_hw_cpu_reset,
    #     rt_hw_cpu_shutdown, the backtrace pair and similar. A weak definition
    #     inside the same archive satisfies the reference, so a `-u` for a strong one
    #     does nothing - measured: rt_hw_us_delay stayed `W` after adding the flag and
    #     only became `T` once the implementation moved into an object that was
    #     already linked. The check therefore looks at the ELF, not at the sources.
    hooks = ["rt_hw_us_delay", "rt_hw_interrupt_mask", "rt_hw_interrupt_umask",
             "rt_hw_interrupt_install", "rt_hw_interrupt_uninstall",
             "rt_hw_console_output", "rt_cpus_lock", "rt_cpus_unlock",
             "rt_hw_local_irq_disable", "rt_hw_local_irq_enable"]
    if elf.exists() and nm is not None:
        weak = []
        for line in syms.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[2] in hooks and parts[1] in ("W", "V", "w", "v"):
                weak.append(parts[2])
        check("rthw hooks are strong", not weak,
              f"weak: {weak}" if weak else f"all {len(hooks)} strong")
    else:
        check("rthw hooks are strong", True, "no ELF, skipped")

    # 13. Port headers must not promise symbols that do not exist.
    #     This found two after the W60 rename: port_selftest.h still declared
    #     s31_selftest_init() and port_twai.h still declared s31_twai_cmd_init(),
    #     neither of which existed any more. A stale prototype does not fail the
    #     build (nothing includes those headers yet), it fails the *reader* - and if
    #     a caller ever includes one, it links into a symbol that is not there.
    port32 = PORT / "esp32s31"
    declared = {}
    for h in port32.glob("port_*.h"):
        ht = read(h)
        for m in re.finditer(
                r"^\s*(?:rt_err_t|rt_ssize_t|rt_uint\w+|rt_int\w+|void|int|rt_base_t|"
                r"rt_bool_t|rt_uint64_t)\s+\**((?:rt_hw_|s31_)\w+)\s*\(", ht, re.M):
            declared[m.group(1)] = h.name

    defined = set()
    for f in port32.glob("*.c"):
        ct = read(f)
        for m in re.finditer(
                r"^(?:rt_err_t|rt_ssize_t|rt_uint\w+|rt_int\w+|void|int|rt_base_t|"
                r"rt_bool_t|rt_uint64_t|static\s+\w[\w \*]*?)\s+\**"
                r"((?:rt_hw_|s31_)\w+)\s*\(", ct, re.M):
            defined.add(m.group(1))

    stale = sorted(set(declared) - defined)
    check("port headers match the sources", not stale,
          f"declared but not defined: {[(s, declared[s]) for s in stale]}"
          if stale else f"all {len(declared)} declarations have a definition")

    # 14. A port header that declares functions must wrap them in extern "C".
    #     RT-Thread's own headers all do. Without it a C++ translation unit gets
    #     mangled names and cannot link against the C definitions - a trap that
    #     costs nothing to close and a link error to discover.
    unguarded = []
    for h in port32.glob("port_*.h"):
        ht = read(h)
        has_fn = re.search(
            r"^\s*(?:rt_err_t|rt_ssize_t|rt_uint\w+|rt_int\w+|void|int|rt_base_t|"
            r"rt_bool_t|rt_uint64_t)\s+\**\w+\s*\(", ht, re.M) is not None
        if has_fn and 'extern "C"' not in ht:
            unguarded.append(h.name)
    check('port headers have extern "C"', not unguarded,
          f"missing guard: {unguarded}" if unguarded else "all guarded")

    # 15. Every feature macro in rtconfig.h must be read by something.
    #     RT_USING_I2C_BUSSING lived in this file for nine rounds while nothing
    #     consulted it (dev_i2c_core.c gates on RT_USING_DM, rtdevice.h gates on
    #     RT_USING_I2C, and none of the 466 upstream BSP configs define it). That is
    #     worse than having no switch: it reads as "master/slave support is
    #     configured" when the only thing that decides is RT_USING_I2C.
    #
    #     Only RT_USING_* / FINSH_USING_* / MSH_USING_* are checked - size and name
    #     macros are values, not switches, and ARCH_* are convention markers that
    #     upstream BSPs carry without any code reading them.
    RTROOT = ROOT / "rt-thread"
    rtcfg = read(RTCONFIG)
    switches = sorted(set(re.findall(r"^#define\s+((?:RT_USING|FINSH_USING|MSH_USING)\w*)",
                                     rtcfg, re.M)))
    # Names defined by the IDF port itself rather than by upstream RT-Thread.
    port_owned = {"RT_USING_SMP_SLAVE", "RT_USING_SMP_SLAVE_LOCK",
                  "RT_USING_HW_ATOMIC", "RT_USING_SMP"}
    if not RTROOT.is_dir():
        # This repository does not ship RT-Thread: it is cloned by
        # tools/fetch-upstream.* and is gitignored. Without it the corpus is only
        # the port's own sources, so every switch upstream reads would be reported
        # dead - a false failure, not a real one. Skip and say so.
        check("no dead config macros",
              True,
              "rt-thread/ not present (run tools/fetch-upstream.ps1), skipped")
    else:
        corpus = []
        for f in list(RTROOT.rglob("*.c")) + list(RTROOT.rglob("*.h")):
            if "bsp" in str(f).lower():
                continue
            corpus.append(read(f))
        for f in (PORT).rglob("*.c"):
            corpus.append(read(f))
        bulk = "\n".join(corpus)
        dead = [s for s in switches if s not in port_owned and s not in bulk]
        check("no dead config macros", not dead,
              f"never read by any code: {dead}" if dead else
              f"all {len(switches)} switches referenced")

    # 16. Macros where setting one without its partner silently changes behaviour.
    pairs = [
        ("RT_USING_SERIAL", "RT_USING_SERIAL_V1 or _V2",
         ["RT_USING_SERIAL_V1", "RT_USING_SERIAL_V2"]),
        ("RT_USING_FINSH", "FINSH_USING_MSH", ["FINSH_USING_MSH"]),
        ("FINSH_USING_MSH", "FINSH_USING_SYMTAB", ["FINSH_USING_SYMTAB"]),
        ("RT_USING_FINSH", "MSH_USING_BUILT_IN_COMMANDS",
         ["MSH_USING_BUILT_IN_COMMANDS"]),
        ("RT_USING_SMALL_MEM_AS_HEAP", "RT_USING_SMALL_MEM", ["RT_USING_SMALL_MEM"]),
        ("RT_USING_CONSOLE", "RT_USING_DEVICE",
         ["RT_USING_DEVICE", "RT_USING_DEVICE_OPS"]),
    ]
    broken = []
    for primary, need_desc, needs in pairs:
        if f"#define {primary}" in rtcfg or f"#define {primary} " in rtcfg:
            if not any(f"#define {n}" in rtcfg or f"#define {n} " in rtcfg
                       for n in needs):
                broken.append(f"{primary} needs {need_desc}")
    check("config pairs complete", not broken,
          f"{broken}" if broken else f"all {len(pairs)} pairs satisfied")

    # 17. The generic RISC-V port must not be compiled alongside this one.
    #
    #     This port supplies the whole libcpu contract itself (spin locks, cpu id,
    #     console output, us delay, interrupt mask/umask/install). The generic files
    #     that normally provide those are libcpu/risc-v/common/*.c, and building both
    #     would give duplicate definitions resolved by link order - a build that links
    #     cleanly and behaves differently depending on which object came first.
    #
    #     Only legacy_common/atomic_riscv.c is expected from libcpu. Checked against
    #     compile_commands.json rather than the CMakeLists text, because that is the
    #     compiler's own answer to what was built.
    import json

    generic = ["/libcpu/risc-v/common/cpuport.c", "/libcpu/risc-v/common/cpu.c",
               "/libcpu/risc-v/common/console.c", "/libcpu/risc-v/common/riscv_timer.c",
               "/libcpu/risc-v/common/interrupt.c"]
    ccdb = BUILD / "compile_commands.json"
    if ccdb.exists():
        entries = json.loads(ccdb.read_text(encoding="utf-8"))
        built = [e["file"].replace("\\", "/") for e in entries]
        both = [g for g in generic if any(b.endswith(g) for b in built)]
        check("generic riscv port not built", not both,
              f"also compiled: {both}" if both else
              "only legacy_common/atomic_riscv.c comes from libcpu")
    else:
        check("generic riscv port not built", True, "no compile_commands.json, skipped")

    # 18. Ops tables must fill every field that exists in THIS configuration.
    #
    #     An unset ops field is a NULL function pointer, and RT-Thread's frameworks
    #     generally do not NULL-check the required ones. But "unset" only counts when
    #     the field is compiled in: s31_pin_ops looked like 8/12 until the four
    #     RT_USING_DM / RT_USING_PINCTRL fields were excluded, and both options are
    #     off here.
    #
    #     The check therefore resolves the struct definition and skips fields that sit
    #     inside an #ifdef whose macro is not defined in rtconfig.h.
    def _ops_fields(struct_name):
        pat = re.compile(r"struct\s+" + re.escape(struct_name) + r"\s*\{")
        for h in (RTROOT / "components").rglob("*.h"):
            try:
                ht = read(h)
            except Exception:
                continue
            m = pat.search(ht)
            if not m:
                continue
            # Walk the body, tracking #ifdef nesting so conditional fields can be
            # attributed to their guard.
            depth = 0
            i = m.end() - 1
            body = []
            guards = []
            for raw in ht[i:].splitlines()[1:]:
                s = raw.strip()
                if s.startswith("#ifdef ") or s.startswith("#if defined("):
                    guards.append(re.findall(r"\w+", s.split("(", 1)[-1])[0]
                                  if "defined(" in s else s.split()[1])
                elif s.startswith("#endif"):
                    if guards:
                        guards.pop()
                elif s.startswith("};"):
                    break
                fm = re.match(r"\s*[\w\s\*]+?\(\*(\w+)\)\s*\(", raw)
                if fm:
                    body.append((fm.group(1), list(guards)))
            return body
        return []

    incomplete = []
    for f in port32.glob("*.c"):
        ct = read(f)
        for m in re.finditer(r"(?:static\s+)?const\s+struct\s+(rt_\w+_ops)\s+"
                             r"(\w+)\s*=\s*\{([^}]*)\}", ct, re.S):
            stype, body = m.group(1), m.group(3)
            assigned = set(re.findall(r"\.(\w+)\s*=", body))
            for fname, guards in _ops_fields(stype):
                if fname in assigned:
                    continue
                # Skip when any enclosing guard macro is not enabled here.
                if any(f"#define {g}" not in rtcfg for g in guards):
                    continue
                incomplete.append(f"{f.name}:{stype}.{fname}")
    check("ops tables complete", not incomplete,
          f"unassigned in this config: {incomplete}" if incomplete
          else "every enabled ops field is assigned")

    # 19. Code in IRAM must not call a flash-resident RT-Thread API.
    #
    #     IDF clears the flash cache for SPI-flash writes and for MSPI/PSRAM timing
    #     tuning. Anything running in that window has to be RAM-resident all the way
    #     down. The port applies this to the trap handler and had not applied it to
    #     the driver ISRs: s31_uart_isr (0x2f002c66, IRAM) called rt_kprintf
    #     (0x400128ba, flash), which faults if an overrun is reported in that window.
    flash_apis = ("rt_kprintf", "rt_malloc", "rt_free", "rt_realloc", "rt_calloc",
                  "rt_sem_take", "rt_mutex_take", "rt_thread_mdelay", "rt_thread_delay",
                  "rt_tick_get", "rt_snprintf", "rt_vsnprintf", "rt_device_find",
                  "rt_device_open", "rt_sem_release", "rt_mutex_release")
    iram_calls = []
    for f in port32.glob("*.c"):
        ct = read(f)
        plain = re.sub(r"/\*.*?\*/", "", ct, flags=re.S)
        plain = re.sub(r"//[^\n]*", "", plain)
        for m in re.finditer(r"\bIRAM_ATTR\b([^;{]*?)\b(\w+)\s*\(", plain):
            name = m.group(2)
            depth = 0
            j = plain.index("{", m.end() - 1) if "{" in plain[m.end():m.end() + 400] else -1
            if j < 0:
                continue
            k = j
            while k < len(plain):
                if plain[k] == "{":
                    depth += 1
                elif plain[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            body = plain[j:k]
            for api in flash_apis:
                if re.search(r"\b" + api + r"\s*\(", body):
                    iram_calls.append(f"{f.name}:{name} calls {api}")
    check("IRAM code avoids flash APIs", not iram_calls,
          f"{iram_calls}" if iram_calls else "no flash-resident calls from IRAM code")

    # 21. No soft timers while RT_USING_TIMER_SOFT is off.
    #
    #     timer.c accepts RT_TIMER_FLAG_SOFT_TIMER unconditionally, but the branch in
    #     rt_timer_start() that puts such a timer on the software list is inside
    #     #ifdef RT_USING_TIMER_SOFT. With the feature off - as here - the timer is
    #     silently placed on the hardware list, so its callback runs in tick-interrupt
    #     context instead of a timer thread. Accepted flag, downgraded behaviour, no
    #     diagnostic.
    #
    #     That is upstream and not fixable from the port. What the port can assert is
    #     that none of its own code asks for a soft timer, because such a request
    #     would not do what it says.
    if "#define RT_USING_TIMER_SOFT" not in rtcfg:
        soft_uses = []
        app_c = list(APP.glob("*.c"))
        for f in list(port_c) + app_c:
            src = read(f)
            for lineno, line in enumerate(src.splitlines(), 1):
                s = line.strip()
                if s.startswith("*") or s.startswith("/*") or s.startswith("//"):
                    continue
                if "RT_TIMER_FLAG_SOFT_TIMER" in s:
                    soft_uses.append(f"{f.name}:{lineno}")
        check("no soft timers without the feature", not soft_uses,
              f"requests a soft timer but RT_USING_TIMER_SOFT is off: {soft_uses}"
              if soft_uses
              else "RT_USING_TIMER_SOFT off, and no port code requests one")
    else:
        check("no soft timers without the feature", True,
              "RT_USING_TIMER_SOFT is on, check not applicable")

    # 22. A driver must not discard the result of installing its own ISR.
    #
    #     rt_hw_interrupt_install() returns RT_NULL both for "installed, there was no
    #     previous handler" (the normal case) and for "vector rejected", where it also
    #     prints a diagnostic. So an ignored result cannot distinguish success from
    #     failure - and a rejected install leaves a driver that initialises cleanly and
    #     never receives anything.
    #
    #     The correct pattern is not to test the return value but to confirm the slot
    #     took the handler, or to validate the vector first. This check flags the
    #     discarded-result form so each site has to say which it did.
    #     Line numbers come from the ORIGINAL text: stripping comments first shifts
    #     them, and a checker that reports the wrong line teaches the reader to
    #     distrust it. Comment lines are skipped while walking instead.
    #
    #     The accepted pattern is "install, then confirm the slot", because the return
    #     value cannot distinguish "no previous handler" from "vector rejected". A site
    #     that installs and never asks the slot is the defect.
    bad_install = []
    for f in port_c:
        src = read(f)
        lines = src.splitlines()
        in_comment = False
        for idx, line in enumerate(lines):
            s = line.strip()
            if in_comment:
                if "*/" in s:
                    in_comment = False
                continue
            if s.startswith("/*"):
                if "*/" not in s:
                    in_comment = True
                continue
            if s.startswith("*") or s.startswith("//"):
                continue
            if not (s.startswith("rt_hw_interrupt_install(")
                    or s.startswith("rt_interrupt_install(")):
                continue
            # Look ahead a few lines for the slot check that must follow.
            window = "\n".join(lines[idx:idx + 10])
            if "intr_handler_get(" not in window:
                bad_install.append(f"{f.name}:{idx + 1}")
    check("ISR install result is used", not bad_install,
          f"{bad_install} install without confirming the slot took the handler"
          if bad_install else "every ISR install confirms the slot")

    # 23. No file-scope variable that is written but never read.
    #
    #     `s_sem_sent` was `static volatile`, incremented per release, set to
    #     0x80000000|n on exit, and never read - a completion flag that nothing checked.
    #     Dead state that looks like a check is worse than no state: the next reader
    #     assumes somebody consults it.
    #
    #     Address-taken uses count as reads (ops tables are registered by pointer), and
    #     comments are stripped first so a mention in prose is not mistaken for one.
    write_only = []
    for f in list(port_c) + list(APP.glob("*.c")):
        src = read(f)
        body = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        body = re.sub(r"//[^\n]*", "", body)
        for m in re.finditer(
                r"^static\s+(?:volatile\s+)?(?:const\s+)?[\w ]*?\b(\w+)\s*(?:=|;)",
                body, re.M):
            name = m.group(1)
            if name in ("int", "void", "char", "unsigned", "const", "volatile"):
                continue
            lines = [ln for ln in body.splitlines()
                     if re.search(rf"\b{re.escape(name)}\b", ln)]
            if len(lines) < 2:
                continue
            reads = [ln for ln in lines
                     if not re.search(
                         rf"^\s*(?:static\s+[\w ]*?)?\b{re.escape(name)}\b\s*"
                         rf"(?:=|\+\+|--|\+=|-=|\|=|&=)", ln)]
            # A declaration line is not a read either.
            reads = [ln for ln in reads
                     if not re.match(r"^\s*static\b", ln)]
            if not reads:
                write_only.append(f"{f.name}:{name}")
    check("no write-only statics", not write_only,
          f"written but never read: {write_only}" if write_only
          else "every file-scope variable is read somewhere")

    # 24. Every s31test subcommand the dispatcher accepts must be in the usage text.
    #
    #     Measured once: 12 subcommands dispatched, 9 named on the usage line, and
    #     `fbshim` - the largest group in the suite - had no description line at all.
    #     A usage text that under-reports is the same defect as a hardcoded sub-check
    #     count: the output claims less than the code and the reader cannot tell.
    st = PORT / "esp32s31/selftest.c"
    if st.exists():
        src = read(st)
        dispatched = set(re.findall(r'rt_strcmp\(argv\[1\], "(\w+)"\)', src))
        dispatched.discard("all")          # `all` is the default, always documented
        usage_line = set()
        for m in re.finditer(r'usage: s31test \[([^\]]+)\]', src):
            usage_line |= set(m.group(1).split("|"))
        undocumented = sorted(dispatched - usage_line)
        described = set(re.findall(r's31test (\w+)\s', src))
        undescribed = sorted(dispatched - described)
        check("s31test usage is complete", not undocumented and not undescribed,
              f"dispatched but not in the usage line: {undocumented}; "
              f"no description line: {undescribed}"
              if (undocumented or undescribed)
              else f"all {len(dispatched)} subcommands documented")
    else:
        check("s31test usage is complete", True, "selftest.c not found, skipped")

    # 25. No comma inside an MSH_CMD_EXPORT description.
    #
    #     MSH_CMD_EXPORT / MSH_CMD_EXPORT_ALIAS are variadic. A comma in the
    #     description becomes an argument separator, turning the 3-argument form into
    #     the 4-argument form, which references `<command>_msh_options` - a symbol that
    #     does not exist. With FINSH_USING_OPTION_COMPLETION off the extra argument is
    #     discarded instead, so the same mistake is silent on other configurations.
    #
    #     This rule has been written down twice and broken twice (W77: mcpwm, rmt,
    #     intr_debug; W90: s31stat). A rule that lives only in a note gets broken again.
    comma_desc = []
    for f in list(port_c) + list(APP.glob("*.c")):
        src = read(f)
        lines = src.splitlines()
        in_comment = False
        for idx, line in enumerate(lines):
            s = line.strip()
            if in_comment:
                if "*/" in s:
                    in_comment = False
                continue
            if s.startswith("/*"):
                if "*/" not in s:
                    in_comment = True
                continue
            if s.startswith("*") or s.startswith("//"):
                continue
            if "MSH_CMD_EXPORT" not in s:
                continue
            # Gather the whole macro invocation (it may span lines).
            whole = s
            j = idx
            while not whole.rstrip().endswith(");") and j + 1 < len(lines):
                j += 1
                whole += " " + lines[j].strip()
            # The alias form has 3 arguments; the plain form has 2 (plus the optional
            # 4th "opt" marker). Count commas at nesting depth 0.
            depth = 0
            in_str = False
            esc = False
            commas = 0
            for ch in whole[whole.index("(") + 1:]:
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        if depth == 0:
                            break
                        depth -= 1
                    elif ch == "," and depth == 0:
                        commas += 1
            # MSH_CMD_EXPORT(cmd, desc)                -> 2 args, 1 comma
            # MSH_CMD_EXPORT_ALIAS(cmd, alias, desc)   -> 3 args, 2 commas
            expect = 1 if "MSH_CMD_EXPORT(" in whole else 2
            if commas != expect:
                comma_desc.append(f"{f.name}:{idx + 1} ({commas} commas, want {expect})")
    check("no comma in cmd descriptions", not comma_desc,
          f"{comma_desc} - a comma splits the macro's arguments"
          if comma_desc else "every command description is comma-free")

    # 26. The runbook's status table must quote the real binary size.
    #
    #     §0 is the "read this first" section and it was wrong in six of six rows:
    #     898,688 B vs 1,517,568 B measured, 1 MB partition vs 4 MB, 20 self tests vs 35,
    #     single core vs dual, 2 MB flash vs 16 MB.
    #
    #     Only the artefact size can be verified offline, so only that is asserted. The
    #     point is not to cover every number - it is that at least one number in that
    #     table is checked, so a stale table cannot pass silently again.
    import re as _re

    bin_path = BUILD / "s31_rtt_test.bin"
    runbook = _doc("bringup-runbook.md")
    if bin_path.exists() and runbook.exists():
        actual = bin_path.stat().st_size
        rb = read(runbook)
        m = _re.search(r"\|\s*编译产物\s*\|[^|]*?\*\*([\d,]+)\s*B\*\*", rb)
        claimed = int(m.group(1).replace(",", "")) if m else None
        check("runbook binary size is current", claimed == actual,
              f"section 0 says {claimed:,} B, artifact is {actual:,} B" if claimed is not None
              and claimed != actual else
              f"section 0 quotes {actual:,} B, matches the artifact" if claimed else
              "no size claim found in section 0")
    else:
        check("runbook binary size is current", True, "binary or runbook missing, skipped")

    # 27. Document structure and cross-document consistency.
    #
    #     (a) Section numbers must be unique. The runbook had two "## 8" because 8.5 was
    #         written at heading level 2 - it sat between 8 and 9 and was numbered 8.5,
    #         so the level was the bug. Duplicates make every "§8" reference ambiguous.
    #     (b) The log's header must quote the current HEAD. It advertised 84439686 while
    #         the branch was at f40c14dd - the same staleness as the status table, one
    #         document over.
    #
    #     Only offline-checkable claims are asserted; the self-test count needs the board.
    import subprocess as _sub

    docs = {
        "runbook": _doc("bringup-runbook.md"),
        "log": _doc("porting-log.md"),
        "plan": _doc("porting-plan.md"),
    }
    dupes = []
    for label, path in docs.items():
        if not path.exists():
            continue
        body = read(path)
        nums = re.findall(r"^## (\d+)\.", body, re.M)
        seen = set()
        for n in nums:
            if n in seen:
                dupes.append(f"{label} has two sections numbered {n}")
            seen.add(n)
    check("doc section numbers unique", not dupes,
          f"{dupes}" if dupes else "no duplicate top-level section numbers")

    log_path = docs["log"]
    if log_path.exists():
        body = read(log_path)
        m = re.search(r"当前提交 `([0-9a-f]{7,})`", body)
        try:
            head = _sub.run(["git", "-C", str(IDF),
                             "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True, errors="replace",
                            timeout=20).stdout.strip()
        except Exception:
            head = ""
        check("log header quotes current HEAD", not head or (m and m.group(1) == head),
              f"header says {m.group(1) if m else 'nothing'}, HEAD is {head}"
              if head else "cannot read HEAD, skipped")
    else:
        check("log header quotes current HEAD", True, "log missing, skipped")

    # 28. Every .c in the port and app must be compiled.
    #
    #     main/s31wifiscan.c - a 193-line msh command - sat on disk outside the build
    #     for 20+ rounds. It was pulled during the W59/W60 regression bisection because
    #     removing it appeared to fix a hang; the next log section exonerates it (the
    #     real cause was CPU1 calling rt_system_scheduler_start()), but nobody put the
    #     file back and nothing noticed it was gone from the image.
    #
    #     This is check 9's question asked one step earlier: 9 asks "is this file kept
    #     in the image", this asks "is it compiled at all". Only the port and the app
    #     are checked - rt-thread has hundreds of sources and only some are meant to
    #     build.
    ccdb2 = BUILD / "compile_commands.json"
    if ccdb2.exists():
        import json as _json2

        compiled = {pathlib.Path(e["file"]).resolve()
                    for e in _json2.loads(ccdb2.read_text(encoding="utf-8"))}
        orphans = []
        for folder in (port32, PORT / "common", APP):
            if not folder.exists():
                continue
            for f in sorted(folder.glob("*.c")):
                if f.resolve() not in compiled:
                    orphans.append(f.name)
        check("every port source is compiled", not orphans,
              f"on disk but not in the build: {orphans}" if orphans
              else "every .c in port/ and main/ is in the build")
    else:
        check("every port source is compiled", True, "no compile_commands.json, skipped")


    # Report.
    width = max(len(n) for n, _, _ in results)
    failed = 0
    print()
    print("RT-Thread port conformance")
    print("=" * (width + 40))
    for name, ok, detail in results:
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<{width}}  {detail}")
    print("=" * (width + 40))
    print(f"{len(results) - failed}/{len(results)} checks passed")
    print()

    if strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
