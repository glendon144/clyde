"""Local process simulation, not a VM/chroot isolation test."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from .broker import Broker

def main():
    print('LOCAL SIMULATION: no VM or chroot containment demonstrated', flush=True)
    with tempfile.TemporaryDirectory(prefix='clyde-') as directory:
        with Broker(('127.0.0.1', 0), Path(__file__).parent.parent / 'clyde_fixtures/introduction.txt',
                    Path(directory) / 'audit.jsonl') as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run([sys.executable, '-m', 'clyde.controller', '--port',
                                         str(server.server_address[1])], timeout=8)
                print('Broker audit:\n' + (Path(directory) / 'audit.jsonl').read_text(), end='')
                return result.returncode
            finally:
                server.shutdown()
                thread.join(timeout=2)

if __name__ == '__main__':
    raise SystemExit(main())
