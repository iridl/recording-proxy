import argparse
import contextlib
import functools
import hashlib
import http.server
import multiprocessing
from multiprocessing.sharedctypes import Synchronized
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse
import urllib.request


@contextlib.contextmanager
def recording_proxy(
        dirname: str,
        is_record: bool,
        prefixes: Sequence[str] = (),
        verbose: bool = False
):
    backchannel = multiprocessing.Value('i', 0)
    proxy_proc = multiprocessing.Process(
        target=run_proxy,
        args=[dirname, is_record, 0, prefixes, verbose, backchannel],
    )
    proxy_proc.start()

    while (port := backchannel.value) == 0:
        time.sleep(.01)

    # Even though the proxy only handles http for now, intercept
    # both http and https so that tests that accidentally use https
    # don't silently go around the proxy.
    previous_http_proxy = os.environ.get("http_proxy")
    os.environ["http_proxy"] = f"http://127.0.0.1:{port}"
    previous_https_proxy = os.environ.get("https_proxy")
    os.environ["https_proxy"] = f"http://127.0.0.1:{port}"

    try:
        yield
    finally:
        proxy_proc.terminate()
        proxy_proc.join()

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
            # These first three just get passed through to super
            request: Any,
            client_address: Any,
            server: Any,
            dirname: str,
            is_record: bool,
            prefixes: Iterable[str],
            verbose: bool,
    ):
        self.dirname = Path(dirname)
        self.is_record = is_record
        self.prefixes = prefixes
        self.verbose = verbose
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
            def sanitize(s: str):
                return re.sub(r'[^a-zA-Z0-9.-]', '_', s)
            subdir = sanitize(parsed_url.netloc)
            slug = sanitize(parsed_url.path[:100])
            file_path = self.dirname / subdir / f"{slug}-{fingerprint}"

            if self.is_record:
                self._fetch_and_record(url, file_path)
            elif file_path.exists():
                self._serve_from_disk(file_path, url)
            else:
                self.send_error(404, f"No recorded response found for: {url}")
        else:
            self._pass_through(url)

    def log_message(self, format: str, *args):
        if self.verbose:
            super().log_message(format, *args)

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


def run_proxy(
        dirname: str,
        is_record: bool,
        requested_port: int,
        prefixes: Iterable[str],
        verbose: bool,
        backchannel: "Synchronized[int] | None" = None
):
    server = http.server.HTTPServer(
        ('127.0.0.1', requested_port),
        functools.partial(
            RecordingProxyHandler,
            dirname=dirname,
            is_record=is_record,
            prefixes=prefixes,
            verbose=verbose,
        )
    )
    allocated_port = server.server_address[1]
    if verbose:
        mode = 'recording' if is_record else 'playback'
        print(f"Recording proxy: {mode} mode on port {allocated_port} with fixture dir {dirname}")
    if backchannel is not None:
        backchannel.value = allocated_port
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Recording HTTP proxy")
    parser.add_argument("--record", action="store_true",
                        help="Make real network requests and save the responses.")
    parser.add_argument("--port", type=int, default=0, help="Proxy port (default: pick a random one)")
    parser.add_argument("--dir", default="responses", help="Directory to store responses")
    parser.add_argument("--prefix", action='append')
    parser.add_argument("--verbose", action='store_true')
    args = parser.parse_args()
    run_proxy(args.dir, args.record, args.port, args.prefix, args.verbose)

