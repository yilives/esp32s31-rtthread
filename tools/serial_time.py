"""Timestamp every line the board emits, to tell a slow tick from a fast clock.

Usage: serial_time.py <port> <command>

The s31test tick check compares rt_tick_get() against a busy wait on CPU cycles
plus a counter read, and once reported 15999 us/tick instead of 1000. Those two
come from different places: the tick is incremented by the SYSTIMER alarm ISR,
and the elapsed time came from the SYSTIMER counter. If both were slow by the
same factor the ratio would look correct - so a 16x discrepancy means one of
them is wrong relative to real time.

The host clock is the only reference that is not derived from either. Printing
arrival times for each line shows whether the busy wait inside the test took 2 s
(as written) or 32 s (16x slow), which decides the question.

RESOLVED (2026-10-30): the 16x was a unit error in the port, not a clock
problem. The S31 SYSTIMER counter runs at 16 MHz (XTAL 40 MHz / fixed 2.5
divider), so a raw register read is 62.5 ns per count; both st_now_us() and
s31_systimer_us() used the raw value as microseconds. IDF's own target port
converts with `ticks / 16` (components/esp_hw_support/port/esp32s31/systimer.c)
and the port now does the same. This script is kept because the next "the tick
rate looks wrong" report needs a host-clock reference to settle it, and because
the resolution is exactly what it could not have told you on its own.
"""
import sys
import time

import serial

PROMPT = "msh >"


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: serial_time.py <port> <command>")
        return 2

    port, command = sys.argv[1], sys.argv[2]

    with serial.Serial(port, 115200, timeout=0.1, write_timeout=2.0) as sp:
        # Both True is the running state on this board; anything else holds
        # the chip in reset (inverting buffers on EN/IO0).
        sp.dtr = True
        sp.rts = True

        # Wait for a prompt so the command is not typed into the boot log.
        buf = bytearray()
        deadline = time.time() + 25.0
        while time.time() < deadline:
            chunk = sp.read(2048)
            if not chunk:
                continue
            buf += chunk
            if PROMPT.encode() in bytes(buf):
                break
        else:
            print("error: no prompt")
            return 1

        sp.reset_input_buffer()
        t_start = time.time()
        sp.write(command.encode() + b"\r\n")
        sp.flush()

        pending = ""
        idle_since = time.time()
        deadline = time.time() + 60.0

        while time.time() < deadline:
            chunk = sp.read(1024)
            now = time.time()

            if chunk:
                idle_since = now
                pending += chunk.decode("utf-8", "replace")

                # Emit whatever complete lines have accumulated.
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    line = line.replace("\r", "").strip()
                    if line:
                        print(f"  +{now - t_start:7.3f}s  {line}")
            elif now - idle_since > 6.0 and pending == "":
                # Quiet for a while after having seen output: assume done.
                break

        print("total wall clock: %.3f s" % (time.time() - t_start))

    return 0


if __name__ == "__main__":
    sys.exit(main())
