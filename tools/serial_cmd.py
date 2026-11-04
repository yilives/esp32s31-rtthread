"""Run finsh commands over the s31 console.

Usage: serial_cmd.py <port> <cmd1> [cmd2 ...]

Line states on this board, which took a while to establish:

    DTR=True  RTS=True   -> chip runs, console live        (this is the one)
    DTR=False RTS=False  -> nothing
    DTR=True  RTS=False  -> nothing
    DTR=False RTS=True   -> nothing

The three dead states all hold the chip in reset. That is inverted from what
the signal names suggest, because this CH343 drives EN and IO0 through
inverting buffers. Every "board is unresponsive" reading earlier in the session
was this: pyserial asserts both lines when it opens the port, and any attempt to
"release" them by setting them False actually held EN low.

So do not touch DTR/RTS after opening. Just read.
"""
import sys
import time

import serial

PROMPT = "msh >"
BOOT_TIMEOUT = 25.0


def wait_for_prompt(sp: serial.Serial, timeout: float) -> bool:
    """Consume output until a prompt appears, then let the buffer settle."""
    buf = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = sp.read(2048)
        if not chunk:
            continue
        buf += chunk
        if PROMPT.encode() in bytes(buf):
            settle = time.time() + 0.25
            while time.time() < settle:
                if not sp.read(2048):
                    break
            return True
    return False


def run_command(sp: serial.Serial, command: str, timeout: float) -> str:
    sp.reset_input_buffer()
    sp.write(command.encode() + b"\r\n")
    sp.flush()

    text = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = sp.read(2048)
        if not chunk:
            continue
        text += chunk.decode("utf-8", "replace")
        # Two prompts: the one that closed the previous line, and the one this
        # command prints when it is done.
        if text.count(PROMPT) >= 2:
            break

    lines = text.replace("\r", "").split("\n")
    out = []
    for line in lines:
        line = line.replace(PROMPT, "").strip()
        if not line or line == command.strip():
            continue
        out.append(line)
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: serial_cmd.py <port> <cmd1> [cmd2 ...]")
        return 2

    port = sys.argv[1]
    commands = sys.argv[2:]

    with serial.Serial(port, 115200, timeout=0.1, write_timeout=2.0) as sp:
        # Both True is the running state on this board. Setting them False -
        # the intuitive "release" - holds the chip in reset.
        sp.dtr = True
        sp.rts = True

        if not wait_for_prompt(sp, BOOT_TIMEOUT):
            print("error: no prompt within %.0fs" % BOOT_TIMEOUT)
            print("       on this board DTR and RTS must both be True")
            return 1

        for cmd in commands:
            print("=" * 10, cmd)
            body = run_command(sp, cmd, 60.0)
            print(body if body else "(no output)")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
