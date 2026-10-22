#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pcldl 的验收测试：Range 正常 / 断点续传 / 多源容错 / 不支持 Range 降级 / 限速。

全部在本地回环上跑，不依赖外网。
    python tests/test_pcldl.py
"""

import hashlib
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pcldl  # noqa: E402

pcldl.setup_console()          # Windows 控制台默认 cp1252，不先切 UTF-8 打印中文会崩

SCRIPT = os.path.join(ROOT, "pcldl.py")
WORK = tempfile.mkdtemp(prefix="pcldl-test-")


# --------------------------------------------------------------- 测试服务端
class Counter:
    def __init__(self):
        self.bytes = 0
        self.reqs = 0
        self.lock = threading.Lock()

    def add(self, n):
        with self.lock:
            self.bytes += n

    def hit(self):
        with self.lock:
            self.reqs += 1


def make_handler(data, rate_per_conn=0, always_fail=False, support_range=True):
    st = Counter()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            st.hit()
            if always_fail:
                body = b"nope"
                self.send_response(500)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            total = len(data)
            rng = self.headers.get("Range")
            start, end, partial = 0, total - 1, False
            if support_range and rng and rng.startswith("bytes="):
                s, _, e = rng[6:].partition("-")
                if s:
                    start = int(s)
                if e:
                    end = int(e)
                end = min(end, total - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True

            body = data[start:end + 1]
            self.send_response(206 if partial else 200)
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Accept-Ranges", "bytes" if support_range else "none")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"v1"')
            self.end_headers()

            step = 64 * 1024
            delay = (step / rate_per_conn) if rate_per_conn else 0
            try:
                for i in range(0, len(body), step):
                    chunk = body[i:i + step]
                    self.wfile.write(chunk)
                    st.add(len(chunk))
                    if delay:
                        time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *a):
            pass

    return H, st


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def clean(out):
    for p in (out, out + ".part", os.path.join(WORK, ".pcl_tmp")):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)


def case(title, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {title}  {detail}")
    return ok


# --------------------------------------------------------------- A 正常下载
def t_normal():
    size = 24 * 1024 * 1024
    data = os.urandom(size)
    H, st = make_handler(data, rate_per_conn=8 * 1024 * 1024)
    srv = serve(H)
    out = os.path.join(WORK, "a.bin")
    clean(out)

    nf = pcldl.NetFile([f"http://127.0.0.1:{srv.server_address[1]}/a.bin"], out,
                       max_threads=8, quiet=True)
    t0 = time.monotonic()
    nf.run()
    el = time.monotonic() - t0
    srv.shutdown()

    ok = sha256(out) == hashlib.sha256(data).hexdigest() and len(nf.segments) >= 2
    r = case("正常下载（动态分片）", ok,
             f"{el:.2f}s  {size/el/1048576:.1f}MB/s  分片={len(nf.segments)}"
             f"  请求={st.reqs}  重试={nf.retry_total}")
    clean(out)
    return r


# --------------------------------------------------------------- B 断点续传
def t_resume():
    size = 32 * 1024 * 1024
    data = os.urandom(size)
    H, st = make_handler(data, rate_per_conn=4 * 1024 * 1024)
    srv = serve(H)
    url = f"http://127.0.0.1:{srv.server_address[1]}/r.bin"
    out = os.path.join(WORK, "b.bin")
    clean(out)

    p = subprocess.Popen([sys.executable, SCRIPT, url, "-o", out, "-x", "6", "-q"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=WORK)
    deadline = time.time() + 40
    while time.time() < deadline:
        if st.bytes > size * 0.45 or p.poll() is not None:
            break
        time.sleep(0.05)
    p.kill()
    p.wait()

    first = st.bytes
    state_ok = os.path.exists(os.path.join(WORK, ".pcl_tmp", os.path.basename(out) + ".json"))

    rc = subprocess.call([sys.executable, SCRIPT, url, "-o", out, "-x", "6", "-q"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=WORK)
    srv.shutdown()
    total = st.bytes
    ok = (rc == 0
          and sha256(out) == hashlib.sha256(data).hexdigest()
          and total < size * 1.9)
    r = case("中断后续传", ok,
             f"首次 {first/1048576:.1f}MB + 续传共 {total/1048576:.1f}MB"
             f"（文件 {size/1048576:.0f}MB）  state={state_ok}")
    clean(out)
    return r


# --------------------------------------------------------------- C 多源容错
def t_failover():
    size = 8 * 1024 * 1024
    data = os.urandom(size)
    Hb, _ = make_handler(data, always_fail=True)
    Hg, _ = make_handler(data)
    srvb, srvg = serve(Hb), serve(Hg)
    out = os.path.join(WORK, "c.bin")
    clean(out)

    nf = pcldl.NetFile([f"http://127.0.0.1:{srvb.server_address[1]}/c.bin",
                        f"http://127.0.0.1:{srvg.server_address[1]}/c.bin"],
                       out, max_threads=4, quiet=True)
    nf.run()
    srvb.shutdown()
    srvg.shutdown()
    ok = sha256(out) == hashlib.sha256(data).hexdigest()
    r = case("多源容错（首源 500）", ok, f"重试={nf.retry_total}")
    clean(out)
    return r


# --------------------------------------------------------------- D 降级
def t_plain():
    size = 6 * 1024 * 1024
    data = os.urandom(size)
    H, _ = make_handler(data, support_range=False)
    srv = serve(H)
    out = os.path.join(WORK, "d.bin")
    clean(out)
    nf = pcldl.NetFile([f"http://127.0.0.1:{srv.server_address[1]}/d.bin"], out, quiet=True)
    nf.run()
    srv.shutdown()
    ok = sha256(out) == hashlib.sha256(data).hexdigest() and nf.plain
    r = case("不支持 Range 降级单流", ok, f"plain={nf.plain}")
    clean(out)
    return r


# --------------------------------------------------------------- E 限速
def t_limit():
    size = 8 * 1024 * 1024
    data = os.urandom(size)
    H, _ = make_handler(data)
    srv = serve(H)
    out = os.path.join(WORK, "e.bin")
    clean(out)
    nf = pcldl.NetFile([f"http://127.0.0.1:{srv.server_address[1]}/e.bin"], out,
                       limit=4 * 1024 * 1024, max_threads=4, quiet=True)
    t0 = time.monotonic()
    nf.run()
    el = time.monotonic() - t0
    srv.shutdown()
    speed = size / el / 1048576
    ok = sha256(out) == hashlib.sha256(data).hexdigest() and speed < 5.2
    r = case("令牌桶限速 4MB/s", ok, f"实测 {speed:.2f}MB/s  用时 {el:.2f}s")
    clean(out)
    return r


def main():
    try:
        results = [t_plain(), t_failover(), t_limit(), t_normal(), t_resume()]
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print("\n" + ("ALL PASS" if all(results) else "SOME FAILED"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
