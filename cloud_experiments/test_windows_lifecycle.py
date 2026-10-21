"""Windows-only process ownership checks for the managed research supervisor."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from cloud_experiments import research


@unittest.skipUnless(os.name == "nt", "Windows process-tree boundary")
class WindowsManagedRunControls(unittest.TestCase):
    def test_timeout_reaps_descendants_with_the_supervised_run(self):
        with tempfile.TemporaryDirectory(prefix="athena-windows-process-tree-") as folder:
            root = Path(folder)
            marker = root / "descendant.pid"
            descendant = (
                "import sys,time;from pathlib import Path;"
                "Path(sys.argv[1]).write_text(str(__import__('os').getpid()));time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(30)"
            )
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            descendant_pid = None
            try:
                with self.assertRaisesRegex(TimeoutError, "timed out"):
                    run.run([sys.executable, "-B", "-c", parent, descendant, str(marker)], timeout=1)
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "Child must reach the actual process-tree boundary")
                descendant_pid = int(marker.read_text())

                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                open_process = kernel32.OpenProcess
                open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                open_process.restype = wintypes.HANDLE
                get_exit_code = kernel32.GetExitCodeProcess
                get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                get_exit_code.restype = wintypes.BOOL
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = [wintypes.HANDLE]
                close_handle.restype = wintypes.BOOL

                def alive(pid):
                    handle = open_process(0x1000, False, pid)
                    if not handle:
                        return False
                    code = wintypes.DWORD()
                    try:
                        return bool(get_exit_code(handle, ctypes.byref(code)) and code.value == 259)
                    finally:
                        close_handle(handle)

                deadline = time.monotonic() + 3
                while alive(descendant_pid) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(alive(descendant_pid), "Windows supervisor left a descendant running")
            finally:
                if descendant_pid is not None:
                    subprocess.run(["taskkill", "/PID", str(descendant_pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                run.sample_stream.close()

    def test_normal_exit_reaps_descendants_with_the_supervised_run(self):
        with tempfile.TemporaryDirectory(prefix="athena-windows-process-exit-") as folder:
            root = Path(folder)
            marker = root / "descendant.pid"
            descendant = (
                "import sys,time;from pathlib import Path;"
                "Path(sys.argv[1]).write_text(str(__import__('os').getpid()));time.sleep(30)"
            )
            parent = (
                "import subprocess,sys,time;"
                "subprocess.Popen([sys.executable,'-B','-c',sys.argv[1],sys.argv[2]],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(.3)"
            )
            run = research.ResearchRun(root / "run", "control", "a" * 64, {})
            descendant_pid = None
            try:
                self.assertEqual(
                    run.run([sys.executable, "-B", "-c", parent, descendant, str(marker)], timeout=2),
                    b"",
                )
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists(), "Child must reach the leader-exit boundary")
                descendant_pid = int(marker.read_text())
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    probe = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {descendant_pid}", "/FO", "CSV", "/NH"],
                        capture_output=True, text=True, check=False,
                    )
                    if f'"{descendant_pid}"' not in probe.stdout:
                        break
                    time.sleep(.05)
                else:
                    self.fail("Windows supervisor left a descendant after leader exit")
            finally:
                if descendant_pid is not None:
                    subprocess.run(["taskkill", "/PID", str(descendant_pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                run.sample_stream.close()


if __name__ == "__main__":
    unittest.main()
