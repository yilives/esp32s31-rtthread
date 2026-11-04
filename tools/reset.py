"""Reset the board and stream its boot log live.

This is the tool the user actually asked for: press reset, watch the boot log.

Why it also fixes "waiting for download"
----------------------------------------
Measured on this board (2026-11-01), with the ROM's own boot-mode byte as the readout:

    bare power cycle, host port CLOSED        boot:0x48 (DOWNLOAD(USB/UART0/SPI))
    port open, both lines HIGH, reset pulsed  boot:0x58 (SPI_FAST_FLASH_BOOT)

The CH343's DTR and RTS reach EN and IO0 through inverting buffers. When the port is
closed the driver leaves both deasserted, the inverter turns that into "IO0 low, EN
released", and the ROM therefore enters its UART download mode - which is what prints
`waiting for download` and stops.

So the board does not need a different image or a different cable: it needs a host holding
the two lines at their run levels while EN is pulsed. That is exactly what this script
does, and it is why a bare power cycle behaves differently from a reset done here.

Usage:
    python tools/reset.py [COM16] [seconds]
"""
import sys
import time

try:
    import serial
except ModuleNotFoundError:
    import sys

    sys.stderr.write(
        "pyserial is not installed for this interpreter:\n"
        "  %s\n"
        "\n"
        "Install it with:\n"
        "  %s -m pip install pyserial\n"
        "\n"
        "If you normally use the ESP-IDF environment, its Python already has it:\n"
        "  C:\\Users\\<you>\\.espressif\\python_env\\idf6.1_py3.13_env\\"
        "Scripts\\python.exe tools\\%s\n"
        % (sys.executable, sys.executable, __file__.rsplit("\\", 1)[-1])
    )
    raise SystemExit(2)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("usage: reset.py [PORT] [SECONDS]")
        print("       PORT defaults to COM16, SECONDS to 12")
        print()
        print("Use tools/probe_ports.py to list the ports on this machine.")
        return 0

    port = sys.argv[1] if len(sys.argv) > 1 else "COM16"
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0

    try:
        sp = serial.Serial(port, 115200, timeout=0.05, write_timeout=2.0)
    except serial.SerialException as exc:
        # "Access is denied" on Windows means another program owns the port. A serial
        # assistant left open is the usual cause, and it is also what puts the board in
        # download mode: the assistant asserts DTR/RTS, IO0 is pulled low through the
        # inverting buffer, and the ROM waits for a sync frame.
        print("cannot open %s: %s" % (port, exc))
        print()
        print("If a serial assistant / monitor is open on this port, close it first.")
        print("An open monitor also asserts DTR/RTS, which holds IO0 low through the")
        print("board's inverting buffer and is what makes the ROM print")
        print("'waiting for download' instead of booting.")
        return 2

    # Run state: IO0 high, EN released. Both high is the level that gives a flash boot.
    sp.dtr = True
    sp.rts = True
    time.sleep(0.3)

    # Reset: pull EN through the inverting buffer, then release it.
    sp.dtr = False
    time.sleep(0.25)
    sp.dtr = True

    deadline = time.time() + seconds
    wrote = 0
    log_text = ""
    sys.stdout.write("--- boot log ---\n")
    sys.stdout.flush()
    while time.time() < deadline:
        chunk = sp.read(4096)
        if chunk:
            wrote += len(chunk)
            log_text += chunk.decode("utf-8", "replace")
            sys.stdout.write(chunk.decode("utf-8", "replace").replace("\r", ""))
            sys.stdout.flush()
        elif wrote == 0 and time.time() > deadline - seconds + 2.0:
            # Nothing at all after two seconds: the ROM is waiting silently, so nudge it
            # once. Two bytes, not one - a single byte does not wake it (measured).
            sp.write(b"\r\n")
            sp.flush()

    sp.close()
    print()
    print("--- %d bytes ---" % wrote)

    # The ROM prints its own boot mode in the first line:
    #   0x48 = DOWNLOAD(USB/UART0/SPI), i.e. IO0 was low at reset
    #   0x58 = SPI flash boot, the normal case
    # Other values (0x78, 0x7f, ...) turned up when the reset was not clean. They still
    # boot, but they are not the value a good reset produces, so say so rather than
    # print the byte and move on - an earlier version of this comment waved them off as
    # "SPI mode bits", which was wrong.
    if "boot:0x48" in log_text:
        print()
        print("!! this was a DOWNLOAD-mode boot (IO0 low at reset), not an app boot.")
        print("   Hold BOOT, tap RST, release BOOT, or close any other program that")
        print("   has the port open, then run this again.")
    elif "boot:0x58" not in log_text:
        print()
        print("note: boot mode byte was not the usual 0x58. If the app did not start,")
        print("      run this again so the lines are driven for a full reset.")

    return 0 if wrote else 1


if __name__ == "__main__":
    sys.exit(main())
