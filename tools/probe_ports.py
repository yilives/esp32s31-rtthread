"""Probe a serial port, or list the ones this machine has.

    python tools/probe_ports.py            # list ports
    python tools/probe_ports.py COM7       # probe one (3 s listen)
    python tools/probe_ports.py COM7 921600

Probing opens the port separately per invocation because a port already claimed by a
terminal program blocks in open() with no timeout available - the only way out is a
watchdog thread, which is what the worker below is.

The listing mode exists because the docs used to say "run probe_ports.py to see your
ports" while the script only accepted a port name. Anyone following the docs got
"usage: probe_ports.py <PORT> [baud]" and no list.
"""
import sys
import threading
import time

import serial


def list_ports() -> None:
    """Print the serial ports this machine exposes, with a description if known."""
    from serial.tools import list_ports as lp

    ports = sorted(lp.comports(), key=lambda p: p.device)
    if not ports:
        print("no serial ports found")
        print()
        print("If a board is plugged in:")
        print("  - the USB-to-UART bridge driver may be missing (CH343 / CP210x / FTDI)")
        print("  - on Windows, check Device Manager -> Ports (COM & LPT)")
        return

    print(f"{len(ports)} port(s):")
    for p in ports:
        desc = p.description or ""
        hwid = p.hwid or ""
        print(f"  {p.device:<10} {desc}")
        if hwid:
            print(f"  {'':<10} {hwid}")


def probe(port: str, baud: int = 115200, listen: float = 3.0) -> None:
    result: dict[str, object] = {}

    def worker() -> None:
        try:
            sp = serial.Serial(port, baud, timeout=0.2, write_timeout=1.0)
            with sp:
                sp.reset_input_buffer()
                # Ctrl-C first: a shell prints its prompt, a bootloader says
                # nothing, and a Linux console echoes. All three are informative.
                sp.write(b"\r\n")
                sp.flush()
                deadline = time.time() + listen
                out = bytearray()
                while time.time() < deadline:
                    chunk = sp.read(512)
                    if chunk:
                        out += chunk
                result["data"] = bytes(out)
        except Exception as exc:                  # noqa: BLE001
            result["err"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=listen + 6.0)

    if t.is_alive():
        print(f"=== {port}: TIMEOUT (open blocked or read hung)")
        print("    A terminal program probably holds the port. Close it and retry.")
        return

    if "err" in result:
        print(f"=== {port}: {result['err']}")
        return

    data = result.get("data", b"")
    print(f"=== {port}: {len(data)} bytes")
    if data:
        text = data.decode("utf-8", "replace")
        printable = "".join(
            c if 32 <= ord(c) < 127 or c in "\r\n\t" else "." for c in text
        )
        print(printable[:2000])


if __name__ == "__main__":
    if len(sys.argv) < 2:
        list_ports()
        sys.exit(0)
    if sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
    probe(sys.argv[1], baud)
