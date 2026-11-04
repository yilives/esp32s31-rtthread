"""Run finsh commands without assuming the prompt arrives by itself.

Two lessons from this board, both encoded here:

  * The shell prints its prompt only in response to input. A passive listener
    waits forever - the previous helper did exactly that, and its failure looked
    like a dead board.
  * The reset lines are inverted and pyserial asserts DTR/RTS on open, so a
    reset has to be requested explicitly rather than avoided. This pulses
    reset, reads the banner, then types.

Usage: board.py <port> <command> [more commands...]
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

PROMPT = b"msh >"
CMD_MARKERS = ("msh >",)   # any command output arrives after a prompt


def read_some(sp: serial.Serial, seconds: float) -> bytes:
    out = bytearray()
    deadline = time.time() + seconds
    while time.time() < deadline:
        chunk = sp.read(4096)
        if chunk:
            out += chunk
    return bytes(out)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: board.py <port> <command> [more...]")
        return 2

    port = sys.argv[1]
    commands = sys.argv[2:]

    with serial.Serial(port, 115200, timeout=0.05, write_timeout=2.0) as sp:
        sp.rts = True
        sp.dtr = True
        time.sleep(0.2)

        # Wake the board, then wait for the prompt.
        #
        # This board's EN and IO0 reach the CH343 through inverting buffers, so the
        # levels a closed port leaves behind hold the chip: left alone it prints nothing.
        # Writing bytes is what lets it start. The ROM log afterwards says
        # `rst:0x1 (POWERON), boot:0x58 (SPI_FAST_FLASH_BOOT)` - a normal flash boot, so
        # the image is fine and this is purely the auto-reset wiring.
        #
        # The boot takes about a second, and characters sent during it are dropped. That
        # is why the command has to wait for `msh >` instead of following the wake byte
        # immediately: otherwise the run prints a boot log and no test results.
        text = ""
        ready = False
        for attempt in range(6):
            sp.write(b"\r\n")
            sp.flush()
            window = read_some(sp, 8.0).decode("utf-8", "replace")
            text += window
            if "msh >" in window:
                ready = True
                break

        if not ready and "RT-Thread" not in text and "msh >" not in text:
            printable = "".join(
                c if 32 <= ord(c) < 127 or c in "\r\n\t" else "." for c in text
            )
            print("--- no banner after 12 wake attempts ---")
            print(printable[-1500:])
            return 1

        # Drain whatever the wake left behind, so the first command's output is clean.
        read_some(sp, 0.5)
        sp.reset_input_buffer()

        # Typing speed matters, and not for the usual reason.
        #
        # Measured with a 6-point sweep on this board: sending "s31stat\r\n" as one
        # burst makes the chip reset - the capture comes back as a fresh boot log
        # (3990 B, `rst:0x1 (POWERON)`) with no command output. The same bytes spaced
        # 40 ms apart execute normally (185 B of real output). 20 ms still fails, so the
        # threshold is between 20 and 40 ms.
        #
        # This is the auto-reset wiring: the CH343's line drivers disturb EN while the
        # UART streams, and no firmware change can fix it. So type like a person.
        CHAR_GAP = 0.045

        def type_line(line: str) -> None:
            for ch in line.encode() + b"\r\n":
                sp.write(bytes([ch]))
                sp.flush()
                time.sleep(CHAR_GAP)

        for cmd in commands:
            print("=" * 12, cmd)
            type_line(cmd)

            buf = bytearray()
            deadline = time.time() + 240.0
            now = time.time()
            while time.time() < deadline:
                chunk = sp.read(4096)
                if chunk:
                    buf += chunk
                    now = time.time()
                elif time.time() - now > 20.0 and buf:
                    break
            text = buf.decode("utf-8", "replace")
            if "ESP-ROM" in text and CMD_MARKERS[0] not in text:
                print("--- the board rebooted instead of running '%s' ---" % cmd)
                print("    (see CHAR_GAP in this file: bytes sent too fast reset this "
                      "board)")
                print(text.replace("\r", "")[-800:])
                continue
            print(text.replace("\r", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
