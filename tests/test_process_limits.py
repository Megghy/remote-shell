import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from remote_shell_mcp.config import RuntimeConfig
from remote_shell_mcp.connection_pool import ConnectionPool
from remote_shell_mcp.models import RemoteProfile
from remote_shell_mcp.profile_store import ProfileStore


class ProcessTests(unittest.TestCase):
    def test_endpoint_slot_is_released_when_owner_process_is_killed(self):
        script = """
import sys
from pathlib import Path
import remote_shell_mcp.connection_pool as module
from remote_shell_mcp.config import RuntimeConfig
from remote_shell_mcp.models import RemoteProfile
module.DATA_DIR = Path(sys.argv[1])
pool = module.ConnectionPool(RuntimeConfig(max_endpoint_connections=1), {})
lease = pool._endpoint_lease(RemoteProfile('a', 'a', 'u', 'winrm', 5985))
print('READY', flush=True)
sys.stdin.readline()
lease.release()
pool.close_all()
"""
        with tempfile.TemporaryDirectory() as directory:
            child = subprocess.Popen(
                [sys.executable, "-c", script, directory],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline().strip(), "READY")
                with patch(
                    "remote_shell_mcp.connection_pool.DATA_DIR", Path(directory)
                ):
                    pool = ConnectionPool(
                        RuntimeConfig(
                            max_endpoint_connections=1, connection_wait_seconds=0.05
                        ),
                        {},
                    )
                    try:
                        profile = RemoteProfile("b", "a", "u", "winrm", 5985)
                        with self.assertRaises(ConnectionError):
                            pool._endpoint_lease(profile)
                        child.kill()
                        child.communicate(timeout=5)
                        lease = pool._endpoint_lease(profile)
                        lease.release()
                    finally:
                        pool.close_all()
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_concurrent_process_profile_writes_do_not_lose_hosts(self):
        script = """
import sys
from pathlib import Path
from remote_shell_mcp.profile_store import ProfileStore
from remote_shell_mcp.models import RemoteProfile
store = ProfileStore(Path(sys.argv[1]))
for i in range(15):
    host = sys.argv[2] + '-' + str(i)
    store.save(RemoteProfile(host, host, 'u', 'winrm', 5985))
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            children = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(path), str(i)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for i in range(4)
            ]
            try:
                for child in children:
                    stdout, stderr = child.communicate(timeout=15)
                    self.assertEqual(child.returncode, 0, stdout + stderr)
                store = ProfileStore(path)
                for i in range(4):
                    for j in range(15):
                        self.assertEqual(store.get(f"{i}-{j}").host, f"{i}-{j}")
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
