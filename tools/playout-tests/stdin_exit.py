"""Starts the server the way PlayOut does (no console window, piped stdin/stdout/stderr) and checks:

1. idle CPU while the stdin pipe stays open;
2. idle CPU after stdin is closed without EXIT (the server must keep running);
3. a graceful stop: "EXIT\\n" on stdin, stdin closed, the process exits by itself.

Usage: python -I stdin_exit.py <folder with casparcg.exe> [<folder> ...]
Run run_all.ps1 once first: it writes casparcg.config next to this script.
"""
import ctypes
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "casparcg.config")
CREATE_NO_WINDOW = 0x08000000


def cpu_seconds(pid):
    """Total CPU time of a process, in seconds (Windows)."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    times = [ctypes.c_ulonglong() for _ in range(4)]
    ctypes.windll.kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times])
    ctypes.windll.kernel32.CloseHandle(handle)
    return (times[2].value + times[3].value) / 1e7


def idle_cpu(pid, seconds=5):
    start = cpu_seconds(pid)
    time.sleep(seconds)
    return (cpu_seconds(pid) - start) / seconds


def wait_port(port, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.25)
    return False


def start(exe_dir):
    proc = subprocess.Popen([os.path.join(exe_dir, "casparcg.exe"), CONFIG], cwd=exe_dir,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=CREATE_NO_WINDOW)
    for stream in (proc.stdout, proc.stderr):  # drain, as PlayOut does
        threading.Thread(target=lambda s=stream: [None for _ in s], daemon=True).start()
    if not wait_port(5260, 30):
        proc.kill()
        raise SystemExit(f"{exe_dir}: AMCP port did not open")
    time.sleep(3)
    return proc


def main():
    for exe_dir in sys.argv[1:]:
        print(f"== {exe_dir}")

        proc = start(exe_dir)
        print(f"  stdin open      : {idle_cpu(proc.pid):.2f} CPU-s/s")
        # On Windows the server reads stdin in _O_U16TEXT mode: a pipe must carry UTF-16LE.
        proc.stdin.write("EXIT\n".encode("utf-16-le"))
        proc.stdin.flush()
        proc.stdin.close()
        t0 = time.time()
        try:
            code = proc.wait(timeout=15)
            print(f"  EXIT + close    : exited by itself in {time.time() - t0:.1f} s, code {code}")
        except subprocess.TimeoutExpired:
            print("  EXIT + close    : still running after 15 s, killed")
            proc.kill()
        time.sleep(2)

        proc = start(exe_dir)
        proc.stdin.close()
        time.sleep(1)
        cpu = idle_cpu(proc.pid)
        alive = proc.poll() is None
        print(f"  stdin closed    : {cpu:.2f} CPU-s/s, still running: {alive}")
        proc.kill()
        proc.wait()
        time.sleep(2)


if __name__ == "__main__":
    main()
