import argparse
import contextlib
import functools
import hashlib
import http.server
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.parse import urlparse
import urllib.request


HERE = Path(__file__).absolute().parent
DATA_DIR = HERE.parent / 'data' / 'responses'

@contextlib.contextmanager
def recording_proxy(is_record, prefixes=()):
    port = 8080
    cmd = [
        "python",
        HERE / "recording_proxy.py",
        "--port", str(port),
        "--dir", DATA_DIR
    ]
    if is_record:
        cmd += ["--record"]
    for p in prefixes:
        cmd += ["--prefix", p]
    proxy_proc = subprocess.Popen(cmd)

    # Wait for the proxy to start accepting connections
    for i in range(10):
        try:
            sock = None
            sock = socket.socket()
            sock.connect(('127.0.0.1', port))
            break
        except ConnectionRefusedError:
            pass
        finally:
            if sock is not None:
                sock.close()
        time.sleep(.1)
    else:
        raise Exception('Failed to connect to proxy')

    if proxy_proc.poll() is not None:
        raise Exception('recording proxy failed to start')

    # Even though the proxy only handles http for now, intercept
    # both http and https so that tests that accidentally use https
    # don't silently go around the proxy.
    previous_http_proxy = os.environ.get("http_proxy")
    os.environ["http_proxy"] = f"http://127.0.0.1:{port}"
    previous_https_proxy = os.environ.get("https_proxy")
    os.environ["https_proxy"] = f"http://127.0.0.1:{port}"

    yield

    proxy_proc.terminate()
    proxy_proc.wait()

    if previous_http_proxy is None:
        del os.environ["http_proxy"]
    else:
        os.environ["http_proxy"] = previous_http_proxy
    if previous_https_proxy is None:
        del os.environ["https_proxy"]
    else:
        os.environ["https_proxy"] = previous_https_proxy


class RecordingProxyHandler(http.server.BaseHTTPRequestHandler):
    def __init__(
            self,
            request,
            client_address,
            server,
            dir: str,
            is_record: bool,
            prefixes=(),
    ):
        self.dir = Path(dir)
        self.is_record = is_record
        self.prefixes = prefixes
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        url = self.path
        if self.prefixes:
            is_proxy = False
            for p in self.prefixes:
                if url.startswith(p):
                    is_proxy = True
                    break
        else:
            is_proxy = True

        if is_proxy:
            fingerprint = hashlib.md5(url.encode()).hexdigest()
            parsed_url = urlparse(url)
            def sanitize(s):
                return re.sub(r'[^a-zA-Z0-9.-]', '_', s)
            subdir = sanitize(parsed_url.netloc)
            slug = sanitize(parsed_url.path[:100])
            file_path = self.dir / subdir / f"{slug}-{fingerprint}"

            if self.is_record:
                self._fetch_and_record(url, file_path)
            elif file_path.exists():
                self._serve_from_disk(file_path, url)
            else:
                self.send_error(404, f"No recorded response found for: {url}")
        else:
            self._pass_through(url)

    def _serve_from_disk(self, path: Path, url: str) -> None:
        with path.open('rb') as f:
            content = f.read()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(content)

    def _pass_through(self, url: str) -> None:
        try:
            with urllib.request.urlopen(url) as response:
                content = response.read()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(502, f"Upstream Error: {e}")


    def _fetch_and_record(self, url: str, file_path: Path) -> None:
        try:
            with urllib.request.urlopen(url) as response:
                content = response.read()
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(content)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_error(502, f"Upstream Error: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recording HTTP proxy")
    parser.add_argument("--record", action="store_true",
                        help="Make real network requests and save the responses.")
    parser.add_argument("--port", type=int, default=8080, help="Proxy port (default: 8080)")
    parser.add_argument("--dir", default="responses", help="Directory to store responses")
    parser.add_argument("--prefix", action='append')
    args = parser.parse_args()
    server = http.server.HTTPServer(
        ('127.0.0.1', args.port),
        functools.partial(
            RecordingProxyHandler,
            dir=args.dir,
            is_record=args.record,
            prefixes=args.prefix,
        )
    )
    mode = 'recording' if args.record else 'playback'
    print(f"Recording proxy: {mode} mode on port {args.port}")
    server.serve_forever()
