#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from novel_downloader import NovelDownloader


HOST = "127.0.0.1"
PORT = int(os.environ.get("NOVEL_DOWNLOADER_PORT", "8765"))
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = BASE_DIR.parent / "下载的小说"
INDEX_FILE = BASE_DIR / "index.html"
downloader = NovelDownloader(output_dir=DEFAULT_OUTPUT)


class AppHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "name": "novel_downloader"})
            return

        if self.path in ("/", "/index.html"):
            body = INDEX_FILE.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._send_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_HEAD(self) -> None:
        if self.path in ("/", "/index.html", "/api/health"):
            self.send_response(HTTPStatus.OK)
            self._send_cors_headers()
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
            if self.path == "/api/inspect":
                result = downloader.inspect(payload["url"])
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
                return

            if self.path == "/api/chapter-preview":
                result = downloader.preview_chapter(payload["url"])
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
                return

            if self.path == "/api/book-catalog":
                result = downloader.build_tree(payload["url"])
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
                return

            if self.path == "/api/download":
                output_dir = Path(payload.get("output") or str(DEFAULT_OUTPUT))
                if not output_dir.is_absolute():
                    output_dir = BASE_DIR.parent / output_dir
                book_workers = int(payload.get("book_workers") or 2)
                book_workers = min(max(book_workers, 1), 4)
                local_downloader = NovelDownloader(output_dir=output_dir, book_workers=book_workers)
                result_path = local_downloader.download(payload["url"])
                scan_root = result_path if result_path.is_dir() else result_path.parent
                chapter_count = len(list(scan_root.rglob("chapters/*.txt"))) if scan_root.exists() else 0
                image_count = 0
                if scan_root.exists():
                    image_count = sum(
                        1
                        for path in scan_root.rglob("*")
                        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}
                    )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "result": {
                            "merged_path": str(result_path),
                            "chapter_count": chapter_count,
                            "image_count": image_count,
                        },
                    },
                )
                return

            if self.path == "/api/open-output":
                output_dir = Path(payload.get("output") or str(DEFAULT_OUTPUT))
                if not output_dir.is_absolute():
                    output_dir = BASE_DIR.parent / output_dir
                output_dir.mkdir(parents=True, exist_ok=True)
                import subprocess

                subprocess.Popen(["open", str(output_dir)])
                self._send_json(HTTPStatus.OK, {"ok": True, "result": {"path": str(output_dir)}})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "接口不存在"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> None:
    port = find_available_port(PORT)
    url = f"http://{HOST}:{port}"

    def open_browser() -> None:
        webbrowser.open(url)

    threading.Timer(1.0, open_browser).start()
    print(f"网页小说下载器已启动：{url}")
    print("关闭这个窗口即可停止服务。")
    server = ThreadingHTTPServer((HOST, port), AppHandler)
    server.serve_forever()


def find_available_port(preferred_port: int) -> int:
    for port in range(preferred_port, preferred_port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((HOST, port)) != 0:
                return port
    raise RuntimeError("没有找到可用端口，请先关闭其他下载器窗口后再试。")


if __name__ == "__main__":
    main()
