"""Talk to the s31 board over its console port.

Usage:
    serial_run.py <port> <command> [timeout]

Sends the command, reads until the shell prints its next prompt or the timeout
expires, and prints everything that arrived. Kept free of any protocol
assumptions beyond "print prompt then wait for input", because that is all finsh
promises.

Design notes:
  * The prompt finsh prints is "msh >" - but it is also echoed back as the line
    is typed, so waiting for the *second* occurrence is what marks the end of a
    command. A fixed sleep would be simpler and less reliable.
  * Reads are buffered: a slow command streams output over many seconds, and
    dropping the reader for a moment must not lose bytes in the middle.
"""
import sys
import time

import serial

PROMPT = b"msh >"


def run(port: str, command: str, timeout: float = 30.0, baud: int = 115200) -> int:
    with serial.Serial(port, baud, timeout=0.2, write_timeout=2.0) as sp:
        sp.reset_input_buffer()

        # Wake the shell. A bare newline makes finsh print the prompt again.
        sp.write(b"\r\n")
        time.sleep(0.4)
        sp.reset_input_buffer()

        sp.write(command.encode() + b"\r\n")
        sp.flush()

        buf = bytearray()
        deadline = time.time() + timeout
        seen_prompt = 0

        while time.time() < deadline:
            chunk = sp.read(1024)
            if not chunk:
                continue
            buf += chunk

            text = bytes(buf).decode("utf-8", "replace")
            printable = "".join(
                c if (32 <= ord(c) < 127) or c in "\r\n\t" else "." for c in text
            )
            sys.stdout.write(printable)
            sys.stdout.flush()
            buf.clear()

            # The echoed command contains no prompt, so the first prompt after
            # the echo is the one that means "done".
            seen_prompt = printable.count("msh >")
            if seen_prompt >= 2:
                break

        sys.stdout.write("\n")
        return 0 if seen_prompt >= 2 else 1


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: serial_run.py <port> <command> [timeout]")
        sys.exit(2)
    tmo = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    sys.exit(run(sys.argv[1], sys.argv[2], tmo))
