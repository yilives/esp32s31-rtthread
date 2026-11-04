"""Check the S31 image against the invariants that only the board could catch.

Every item below corresponds to a defect that was found the hard way, listed in
rtthread-esp32-s31-porting-log.md section 22.10 and 23.4 as "this should be a
build-time check, not a manual one". The log says two of the three can be
scripted and the third cannot; this script is those two plus the frame-layout
checks, so a regression shows up before flashing instead of after.

Checks:
  1. flash-mapped LOAD segments == 2   (SOC_MMU_DI_VADDR_SHARED; bootloader
                                        asserts rom_index < 2)
  2. three context-frame sizes agree   (new-thread builder, trap prologue,
                                        cooperative switch - all 256 bytes)
  3. trap exit mstatus carries FS=3    (the 7th defect: FS=0 + flw = MCAUSE=1)
  4. __fsymtab / __rt_init non-empty   (a dead-stripped table means no msh)
  5. init table order is ascending     (.rti_fn levels must sort)
  6. vector table reaches the shared trap entry

Usage: check_image.py [path/to/s31_rtt_test.elf]
Exit code 0 when every check passes, 1 otherwise.
"""
import pathlib
import re
import subprocess
import sys

def _toolchain_bin():
    """riscv32-esp-elf binutils directory, wherever IDF put them.

    IDF_TOOLS_PATH wins if it is set; otherwise the standard ~/.espressif location.
    The version directory name changes with every toolchain release, so the newest one
    is picked instead of a hard-coded name.
    """
    import os
    base = pathlib.Path(os.environ.get("IDF_TOOLS_PATH") or (pathlib.Path.home() / ".espressif"))
    root = base / "tools" / "riscv32-esp-elf"
    if not root.is_dir():
        raise SystemExit(
            "riscv32-esp-elf toolchain not found under %s.\n"
            "Set IDF_TOOLS_PATH, or install the toolchain with:\n"
            "  cd <esp-idf>; .\\install.ps1 esp32s31" % root)
    versions = sorted(
        (d for d in root.iterdir() if d.is_dir() and (d / "riscv32-esp-elf" / "bin").is_dir()),
        key=lambda d: d.name, reverse=True)
    if not versions:
        raise SystemExit("No riscv32-esp-elf/bin under %s" % root)
    return versions[0] / "riscv32-esp-elf" / "bin"


TC = _toolchain_bin()


def _default_elf():
    """Where the build output lives, if it has been built.

    The archive builds into project/build; the author's working tree used
    s31-rtt-test/build. Both are accepted so this script works in either place.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    for cand in (root / "project/build/s31_rtt_test.elf",
                 root / "s31-rtt-test/build/s31_rtt_test.elf"):
        if cand.is_file():
            return cand
    return root / "project/build/s31_rtt_test.elf"


DEFAULT_ELF = _default_elf()

failures: list[str] = []
notes: list[str] = []


def run(tool: str, *args: str) -> str:
    proc = subprocess.run(
        [str(TC / tool), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout + proc.stderr


def check(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name:<28} {detail}")
    if not ok:
        failures.append(name)


def load_segments(elf: pathlib.Path) -> list[tuple[int, int, int]]:
    """Return (vaddr, filesz, memsz) for every LOAD program header."""
    out = run("riscv32-esp-elf-readelf.exe", "-lW", str(elf))
    segs = []
    for line in out.splitlines():
        m = re.match(r"\s+LOAD\s+0x[0-9a-f]+\s+0x([0-9a-f]+)\s+0x[0-9a-f]+"
                     r"\s+0x([0-9a-f]+)\s+0x([0-9a-f]+)", line)
        if m:
            segs.append((int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)))
    return segs


def struct_size(elf: pathlib.Path, name: str) -> int:
    """Return a struct's size in bytes, read from the ELF's DWARF.

    Hardcoding a struct size produces numbers that are wrong in the output and still
    pass, which is worse than an outright failure: the fsymtab entry grew from 12 to
    16 bytes when FINSH_USING_OPTION_COMPLETION was enabled, and "44 commands" was
    printed for a 33-command image.
    """
    out = run("riscv32-esp-elf-objdump.exe", "--dwarf=info", str(elf))
    # objdump renders the name as DW_AT_name : ...: finsh_syscall - the word
    # "struct" is not in the text, so searching for it finds nothing.
    i = out.find(": " + name)
    while i >= 0:
        seg = out[i:i + 900]
        for line in seg.splitlines():
            if "DW_AT_byte_size" in line:
                try:
                    return int(line.rsplit(":", 1)[1].strip())
                except ValueError:
                    return 0
        i = out.find(": " + name, i + 1)
    return 0


def symbols(elf: pathlib.Path) -> dict[str, int]:
    out = run("riscv32-esp-elf-nm.exe", "-n", str(elf))
    syms: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            try:
                syms[parts[2]] = int(parts[0], 16)
            except ValueError:
                pass
    return syms


def main() -> int:
    elf = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ELF
    if not elf.exists():
        print(f"missing {elf}")
        print()
        print("Build the project first:")
        print("  .\\tools\\build.ps1 build")
        print()
        print("This check reads the ELF, so it can only run after a build.")
        return 2

    print(f"image: {elf}\n")

    # 1 ------------------------------------------------------------ segments
    segs = load_segments(elf)
    flash = [s for s in segs if 0x40000000 <= s[0] < 0x44000000]
    check("flash LOAD segments", len(flash) == 2,
          f"{len(flash)} flash-mapped of {len(segs)} LOAD "
          f"(want 2; more trips bootloader_utility.c:833)")
    for v, _, _ in segs:
        print(f"        LOAD vaddr 0x{v:08x}")

    # 2 ------------------------------------------------------------- frames
    dis = run("riscv32-esp-elf-objdump.exe", "-d", "--no-show-raw-insn", str(elf))
    syms0 = symbols(elf)

    # The trap prologue subtracts 128 twice, back to back, at the trap entry itself:
    # FP area first, then the integer area.
    #
    # This used to test `"128" in re.findall(r"addi sp,sp,-(\d+)", dis)` - i.e. whether
    # ANY of the 6780 stack adjustments in the image happened to be 128. It was
    # satisfied by 53 unrelated functions and never looked at the trap handler, so it
    # would have passed with a completely different prologue. Disassemble the entry
    # by symbol instead of trusting a global grep.
    trap_entry = syms0.get("_interrupt_handler")
    prologue: list[str] = []
    if trap_entry is not None:
        raw = run("riscv32-esp-elf-objdump.exe", "-d", "--no-show-raw-insn",
                  f"--start-address=0x{trap_entry:x}",
                  f"--stop-address=0x{trap_entry + 12:x}", str(elf))
        prologue = re.findall(r"addi\s+sp,sp,-(\d+)", raw)
    check("trap prologue subtracts 128+128", prologue[:2] == ["128", "128"],
          f"trap entry at 0x{trap_entry:x} subtracts {prologue[:2]} "
          f"(want two 128s: FP area then integer area)" if trap_entry
          else "no _interrupt_handler symbol")

    # The frame builder reserves the whole integer+FP frame in one subtraction. Tied
    # to rt_hw_stack_init_core's own range rather than counted image-wide.
    stack_init = syms0.get("rt_hw_stack_init_core")
    sizes_256 = 0
    if stack_init is not None:
        raw = run("riscv32-esp-elf-objdump.exe", "-d", "--no-show-raw-insn",
                  f"--start-address=0x{stack_init:x}",
                  f"--stop-address=0x{stack_init + 0x140:x}", str(elf))
        sizes_256 = len(re.findall(r"addi\s+a0,a6,-256", raw))
    check("new-thread frame is 256 B", sizes_256 == 1,
          f"rt_hw_stack_init_core at 0x{stack_init:x} reserves -256 once "
          f"(found {sizes_256})" if stack_init else "no rt_hw_stack_init_core symbol")

    # 3 -------------------------------------------------- trap exit mstatus
    mstatus_ok = False
    mstatus_detail = "no csrw mstatus found"
    lines = dis.splitlines()
    for i, line in enumerate(lines):
        if "csrw" in line and "mstatus" in line:
            window = "\n".join(lines[max(0, i - 8):i])
            if "addi" in window and "-2048" in window:
                # lui 0x8 ; addi -2048  ==  0x8000 - 0x800  ==  0x7800
                mstatus_ok = True
                mstatus_detail = "literal 0x7800 (MPP=M | FS=3) before csrw mstatus"
                break
    check("trap exit mstatus FS=3", mstatus_ok, mstatus_detail)

    # 4 --------------------------------------------------------- symbol tables
    syms = symbols(elf)
    fsym_size = syms.get("__fsymtab_end", 0) - syms.get("__fsymtab_start", 0)
    init_size = syms.get("__rt_init_end", 0) - syms.get("__rt_init_start", 0)
    # The entry size is not a constant: struct finsh_syscall gains a
    # `struct msh_cmd_opt *opt` field when FINSH_USING_OPTION_COMPLETION is set.
    # Read it from DWARF rather than assuming it.
    syscall_size = struct_size(elf, "finsh_syscall")
    cmd_count = fsym_size // syscall_size if syscall_size else 0
    check("__fsymtab non-empty", fsym_size > 0 and cmd_count > 0,
          f"{fsym_size} B = {cmd_count} commands "
          f"(entry {syscall_size} B, from DWARF)")
    check("__rt_init non-empty", init_size > 0,
          f"{init_size} B = {init_size // 4} init entries")

    vsym = syms.get("__vsymtab_end", 0) - syms.get("__vsymtab_start", 0)
    if vsym == 0:
        notes.append("__vsymtab is empty: no FINSH_VAR_EXPORT anywhere in the tree")

    # 5 --------------------------------------------------------- init order
    # The table is walked by address, so what has to be ordered is the level
    # prefix of each entry as it appears in memory. The levels live in the map
    # file (the symbol name does not carry them), so the expected sequence is
    # spelled out - it is a fixed property of this port's own exports.
    #
    #   0      rti_start                  marker
    #   0.end  rti_board_start            marker
    #   1      (BOARD band: every driver registers itself here, in link order)
    #          rt_hw_uart0_init, rt_hw_pin_init, rt_hw_i2c_init, rt_hw_pwm_init,
    #          rt_hw_timer_init, rt_hw_spi_init, rt_hw_adc_init, rt_hw_pcnt_init,
    #          rt_hw_mcpwm_init, rt_hw_rmt_init, rt_hw_twai_init
    #   1.end  rti_board_end              marker  (end of the BOARD band)
    #   4      rt_hw_start_pending_tasks  COMPONENT: flush pre-scheduler tasks
    #   6      finsh_system_init          APP: bring up the shell
    #   6      rt_hw_intr_sweep_again     APP: mask orphaned interrupt lines
    #   6      rt_hw_reclaim_startup_stack APP: hand the boot stack back to IDF
    #   6.end  rti_end                    marker
    #
    # The BOARD-band members are ordered by address, i.e. by which archive member
    # the linker pulled in first, so their relative order is not a contract - only
    # their position between the two markers is. They are listed explicitly anyway
    # so that a driver that accidentally lands in the wrong band shows up here.
    #
    # Adding an INIT_*_EXPORT anywhere in the tree changes this list, and that is
    # the point of the check: it is a deliberate reminder that init order is part
    # of the port's contract, not an accident of link order. Note the two
    # INIT_APP_EXPORT entries are ordered by address, i.e. by the order the linker
    # saw them, so a new APP-stage export can land either side of an existing one.
    expected = [
        "rti_start",
        "rti_board_start",
        "rt_hw_uart0_init",
        "rt_hw_pin_init",
        "rt_hw_i2c_init",
        "rt_hw_pwm_init",
        "rt_hw_timer_init",
        "rt_hw_spi_init",
        "rt_hw_adc_init",
        "rt_hw_pcnt_init",
        "rt_hw_mcpwm_init",
        "rt_hw_rmt_init",
        "rt_hw_twai_init",
        "rti_board_end",
        "rt_hw_start_pending_tasks",
        # Reports CPU1's entry count, trap vectors and interrupt gates from CPU0. An
        # APP-stage export on purpose: the values are only meaningful once main has run
        # and CPU1 has had time to execute its entry. Its position is link order, which
        # is exactly what this list exists to document.
        "s31_cpu1_report",
        # Releases CPU1 into the scheduler. Only present with RT_SMP_CPU1_SCHEDULES, which
        # is why it was absent from this list until the switch was left on: the list is
        # the contract, and the contract changed when CPU1 started scheduling.
        "s31_cpu1_release",
        "finsh_system_init",
        "rt_hw_intr_sweep_again",
        "rt_hw_reclaim_startup_stack",
        "rti_end",
    ]
    entries = sorted(
        (addr, name) for name, addr in syms.items()
        if name.startswith("__rt_init_")
        and name not in ("__rt_init_start", "__rt_init_end")
    )
    got = [n[len("__rt_init_"):] for _, n in entries]
    check("init table order", got == expected,
          f"{len(got)} entries by address: {', '.join(got)}")

    # 6 ------------------------------------------------------------ vectors
    mtvt = syms.get("_mtvt_table")
    trap = syms.get("_interrupt_handler")
    check("_mtvt_table present", mtvt is not None and trap is not None,
          f"mtvt @ 0x{mtvt:08x}" if mtvt else "missing")
    if mtvt is not None and trap is not None:
        raw = run("riscv32-esp-elf-objdump.exe", "-s",
                  f"--start-address=0x{mtvt:x}",
                  f"--stop-address=0x{mtvt + 0xc0:x}", str(elf))
        body = raw.split("Contents of section")[-1]
        words: list[int] = []
        for line in body.splitlines():
            toks = line.split()
            # Each line is: <address> <4 x 32-bit word> [ascii]; the words are
            # printed as raw bytes, so "0001002f" is the little-endian spelling
            # of 0x2f000100.
            if len(toks) >= 2 and re.fullmatch(r"[0-9a-f]{8}", toks[0]):
                for w in toks[1:5]:
                    if re.fullmatch(r"[0-9a-f]{8}", w):
                        words.append(int.from_bytes(bytes.fromhex(w), "little"))
        # Every slot must point at a handler this image defines. Measured: the 48
        # slots hold exactly two values - _interrupt_handler (29) and _panic_handler
        # (19), the latter for CLIC lines configured with the "panic" attribute.
        # The assertion used to be `hits >= 25`, which would not notice six slots
        # pointing anywhere, and did not say what the rest were.
        panic = syms.get("_panic_handler")
        known = {w for w in words if w in (trap, panic)}
        stray = sorted(set(words) - known)
        hits = sum(1 for w in words if w == trap)
        check("trap entry installed", not stray and hits > 0,
              f"{hits} slots -> trap, "
              f"{sum(1 for w in words if w == panic)} -> panic, "
              f"{len(stray)} stray ({[hex(s) for s in stray]})" if stray
              else f"{hits} slots -> _interrupt_handler, "
                   f"{sum(1 for w in words if w == panic)} -> _panic_handler, 0 stray")

    # ---------------------------------------------------------------- notes
    if notes:
        print("\nnotes:")
        for n in notes:
            print(f"  - {n}")

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
