#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcldl —— PCL2 内置下载引擎（ModNet.vb）的 Python 移植。

上游：https://github.com/Meloong-Git/PCL
      Plain Craft Launcher 2/Modules/Base/ModNet.vb（1933 行，VB.NET）

本文件按源码逐段移植，关键处标注了对应的 VB 行号，例如 `# ModNet.vb:862`。
命名也尽量对齐：NetSource / NetThread / NetFile / NetManagerClass。

与上游一致的机制
  * 首线程不发 Range，靠 Content-Length 拿文件大小（:917）
  * 段边界不存储，由链表下一节点的起点推导（:483-497），插入新段即自动收缩前一段
  * 切分位置 = 最大段末端 - 未完成量 × 0.4（:862）
  * 切分触发条件：全局速度 < 速度下限（初值 256KB/s，:319、:1747）
  * 速度下限只涨不跌：近 10 次采样均值 × 0.85（:1699）
  * 调度 20ms（:1727），限速配额 100ms 补一次（:1783）
  * 收到有效数据即清零失败计数（:1014）
  * 合并按 DownloadDone 逐段读（:1197），单段直接拷贝（:1187）
  * 合并失败重试 3 次，间隔 500ms×n（:1233）

本移植额外提供（上游没有）
  * 跨进程断点续传（上游每次 Start 都会清空 Download 缓存目录，:1809）
  * CLI、进度条、哈希校验、磁盘预检的跨平台实现

用法
  python pcldl.py https://example.com/big.iso
  python pcldl.py URL1 URL2 -o out.bin --limit 8M -x 16
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from enum import IntEnum

# ------------------------------------------------------------------ 常量
#: 调度线程心跳。ModNet.vb:1727  `Thread.Sleep(20)`
NET_TICK = 0.020
#: 限速配额补充周期。ModNet.vb:1783
LIMIT_TICK = 0.100
#: 速度下限初值。ModNet.vb:319  `NetTaskSpeedLimitLow = 256 * 1024L`
SPEED_LOW_INIT = 256 * 1024
#: 小于这个未完成量就不再切分。ModNet.vb:772
FILE_PIECE_LIMIT = 256 * 1024
#: 切分比例。ModNet.vb:862  `DownloadEnd - DownloadUndone * 0.4`
PIECE_CUT_RATIO = 0.4
#: 读缓冲。ModNet.vb:991
READ_BUFFER = 16384
#: 连接超时下限 / 上限。ModNet.vb:909
TIMEOUT_MIN_MS = 15000
TIMEOUT_MAX_MS = 30000
#: 慢速断开判据。ModNet.vb:1025
SLOW_GAP_MS = 5000
#: 超过这个大小才查磁盘。ModNet.vb:951
DISK_CHECK_MIN = 50 * 1024 * 1024
#: 临时文件目录名。ModNet.vb:984
TEMP_SUBDIR = "Download"
#: 禁止多线程的源。ModNet.vb:853
NO_SPLIT_HOSTS = ("pcl2-server", "meloong.com", "bmclapi", "github.com",
                  "optifine.net", "momot.rs")
#: 单源失败次数上限。ModNet.vb:1084  `NetTaskThreadLimit + 2`
FAIL_OVER_LIMIT = 2
#: 合并重试次数。ModNet.vb:1233
MERGE_RETRY = 3

VERSION = "2.0"
#: 未知大小时段的“末端”
BIG_SIZE = 1000 * 1000 * 1000 * 1000


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


# ------------------------------------------------------------------ 状态
class NetState(IntEnum):
    """ModNet.vb:373"""
    WaitingToCheck = -1
    WaitingToDownload = 0
    Connecting = 1
    Reading = 2
    Downloading = 3
    Merging = 4
    Finished = 5
    Canceled = 6


# ------------------------------------------------------------------ 异常
class RangeNotSupported(Exception):
    """ModNet.vb:1302"""


class HttpCodeError(Exception):
    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------ 工具
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def parse_size(text: str) -> int:
    t = str(text).strip().upper().replace(" ", "")
    m = re.fullmatch(r"([\d.]+)([KMG]?)", t)
    if not m:
        raise argparse.ArgumentTypeError(f"无法解析大小: {text}")
    mult = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[m.group(2)]
    return int(float(m.group(1)) * mult)


def setup_console() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            for h in (k.GetStdHandle(-11), k.GetStdHandle(-12)):
                mode = ctypes.c_uint32()
                if k.GetConsoleMode(h, ctypes.byref(mode)):
                    k.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass


class Term:
    ON = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    DIM = "\x1b[2m" if ON else ""
    RED = "\x1b[31m" if ON else ""
    GRN = "\x1b[32m" if ON else ""
    YEL = "\x1b[33m" if ON else ""
    CYA = "\x1b[36m" if ON else ""
    RST = "\x1b[0m" if ON else ""
    CLR = "\x1b[K" if ON else ""


# ------------------------------------------------------------------ NetSource
class NetSource:
    """ModNet.vb:356"""
    __slots__ = ("id", "url", "fail_count", "ex", "single_thread", "is_failed")

    def __init__(self, sid: int, url: str):
        self.id = sid
        self.url = url
        self.fail_count = 0
        self.ex: BaseException | None = None
        self.single_thread: NetThread | None = None
        self.is_failed = False

    @property
    def tag(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc or self.url

    def __repr__(self) -> str:
        return f"<Source {self.id} {self.tag} fail={self.fail_count}{' FAILED' if self.is_failed else ''}>"


# ------------------------------------------------------------------ NetThread
class NetThread:
    """ModNet.vb:428。段边界不存储，由链表中下一个节点推导（:483）"""
    __slots__ = ("uuid", "task", "th", "download_start", "download_done", "temp",
                 "state", "source", "init_time", "last_receive",
                 "_speed", "_speed_ms", "_speed_done", "no_write")

    def __init__(self, uuid: int, task: NetFile, start: int, source: NetSource):
        self.uuid = uuid
        self.task = task
        self.th: threading.Thread | None = None
        self.download_start = start
        self.download_done = 0
        self.temp: str | None = None
        self.state = NetState.WaitingToDownload
        self.source = source
        self.init_time = _now_ms()
        self.last_receive = -1
        self._speed = 0
        self._speed_ms = _now_ms()
        self._speed_done = 0
        self.no_write = False

    #: ModNet.vb:483
    def download_end(self) -> int:
        t = self.task
        with t.lock_chain:
            idx = t.threads.index(self)
            if idx + 1 < len(t.threads):
                return t.threads[idx + 1].download_start - 1
        if t.is_unknown_size or t.file_size < 0:
            return BIG_SIZE
        return t.file_size - 1

    #: ModNet.vb:501
    def undone(self) -> int:
        return self.download_end() - (self.download_start + self.download_done) + 1

    #: ModNet.vb:522  每 200ms 采一次速
    def speed(self) -> int:
        now = _now_ms()
        if now - self._speed_ms > 200:
            dt = now - self._speed_ms
            self._speed = (self.download_done - self._speed_done) * 1000 // max(dt, 1)
            self._speed_done = self.download_done
            self._speed_ms += dt
        return self._speed

    @property
    def is_ended(self) -> bool:
        return self.state in (NetState.Finished, NetState.Canceled)

    @property
    def is_first_thread(self) -> bool:
        """ModNet.vb:466  只有最开头那个线程才不带 Range"""
        return (self.download_start + self.download_done == 0
                and self.task.file_size == -2)


# ------------------------------------------------------------------ NetManager
class NetManager:
    """ModNet.vb:1639"""

    def __init__(self, limit_high: int = 0, thread_limit: int = 16):
        self.thread_limit = thread_limit
        self.speed_limit_low = SPEED_LOW_INIT       # :319
        self.speed_limit_high = limit_high if limit_high > 0 else -1
        #: -1 表示不限速（:347）。初始给 1/10 个配额，避免开头 1 秒的突发
        self.speed_limit_left = float(limit_high) / 10 if limit_high > 0 else -1.0
        self.thread_count = 0
        self.download_done = 0

        self.speed = 0.0
        self.peak = 0.0
        self._speed_last: list[float] = []          # :1683 最近 30 条
        self._speed_last_done = 0
        self._last_refresh = time.monotonic()

        self.files: list[NetFile] = []
        self._lock = threading.Lock()
        self._started = False
        self._stop = False

    # ---------------------------------------------------------- 启动
    def start(self) -> None:
        """ModNet.vb:1719"""
        with self._lock:
            if self._started:
                return
            self._started = True
        for did in (0, 1):
            threading.Thread(target=self._starter, args=(did,), daemon=True,
                             name=f"ThreadStarter{did}").start()
        threading.Thread(target=self._refresher, daemon=True, name="StatRefresher").start()

    def stop(self) -> None:
        self._stop = True

    # ---------------------------------------------------------- 线程启动器
    def _starter(self, did: int) -> None:
        """ModNet.vb:1723"""
        while not self._stop:
            time.sleep(NET_TICK)
            with self._lock:
                files = list(self.files)
            ongoing = [f for f in files
                       if f.uuid % 2 != did
                       and NetState.WaitingToDownload <= f.state < NetState.Merging]
            if not ongoing:
                continue
            if self.speed >= self.speed_limit_low:      # :1747 速度足够，不再新增
                continue
            for f in ongoing:
                if self.thread_count >= self.thread_limit:   # :1749
                    break
                prep = sum(1 for t in f.threads if t.state < NetState.Downloading)
                dl = sum(1 for t in f.threads if t.state == NetState.Downloading)
                if prep > dl:                                # :1762 准备中的比下载中的还多
                    continue
                new = f.try_begin_thread()
                if new is not None:
                    if "bmclapi" in new.source.url:
                        time.sleep(0.1)                      # :1764
                    break                                    # 一次只加一个，下个 tick 再看速度

    # ---------------------------------------------------------- 统计刷新
    def _refresher(self) -> None:
        """ModNet.vb:1773"""
        next_tick = time.monotonic()
        while not self._stop:
            if self.speed_limit_high > 0:                    # :1779
                self.speed_limit_left = min(
                    self.speed_limit_left + self.speed_limit_high / 10,
                    float(self.speed_limit_high))
            self.refresh_stat()
            next_tick += LIMIT_TICK
            dt = next_tick - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            else:
                next_tick = time.monotonic()

    def refresh_stat(self) -> None:
        """ModNet.vb:1676"""
        now = time.monotonic()
        dt = now - self._last_refresh
        if dt <= 0:
            return
        self._last_refresh = now

        inst = max(0.0, (self.download_done - self._speed_last_done) / dt)
        self._speed_last.insert(0, inst)                     # :1686
        del self._speed_last[30:]                            # :1687
        self._speed_last_done = self.download_done

        # 加权显示速度，越新权重越大（:1690）
        total = 0.0
        wsum = 0
        w = len(self._speed_last)
        for v in self._speed_last:
            total += v * w
            wsum += w
            w -= 1
        self.speed = total / wsum if wsum else 0.0
        self.peak = max(self.peak, self.speed)

        # 速度下限只涨不跌（:1699）
        if len(self._speed_last) >= 10:
            top = self._speed_last[:10]
            limit = sum(top) / len(top) * 0.85
            if limit > self.speed_limit_low:
                self.speed_limit_low = limit

    def register(self, f: NetFile) -> None:
        with self._lock:
            self.files.append(f)


# ================================================================== NetFile
class NetFile:
    """ModNet.vb:579"""

    def __init__(self, urls: list[str], out: str, manager: NetManager, *,
                 headers: dict[str, str] | None = None, insecure: bool = False,
                 quiet: bool = False, linear: bool = False,
                 no_resume: bool = False, hashes: dict[str, str] | None = None):
        urls = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
        self.sources = [NetSource(i, u) for i, u in enumerate(urls)]
        #: 只在“所有源逐个单线程重试”时用到（:598）
        self.sources_once: list[NetSource] = []
        self.first_thread_source = 0
        self.retried = False

        self.out = os.path.abspath(out)
        self.local_name = os.path.basename(self.out)
        self.uuid = random.randrange(1, 1 << 30)
        #: 稳定文件名键，保证跨进程续传能复用同一批 .tmp
        self.url_key = hashlib.sha1("|".join(urls).encode("utf-8")).hexdigest()[:12]
        self.manager = manager

        self.file_size = -2                 # -2 = 未探测（:466 的判定依赖它）
        self.is_unknown_size = False
        self.threads: list[NetThread] = []
        self.state = NetState.WaitingToCheck
        self.ex: list[BaseException] = []
        self.fail_count = 0
        self.download_done = 0
        self.connect_count = 0
        self.connect_time = 0
        self._uuid_seq = 0

        self.lock_state = threading.RLock()
        self.lock_chain = threading.RLock()
        self.lock_source = threading.RLock()
        self.lock_merge = threading.Lock()

        self.headers = {"User-Agent": "PCL2/pcldl", "Accept": "*/*",
                        "Accept-Encoding": "identity"}
        self.headers.update(headers or {})
        self.quiet = quiet
        self.linear = linear
        self.no_resume = no_resume
        self.hashes = {k: v.lower() for k, v in (hashes or {}).items() if v}
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.peak = 0.0
        self.retry_total = 0
        self.started = 0.0
        self.elapsed = 0.0
        self._last_print = 0.0
        self._plock = threading.Lock()
        self.show = not quiet

        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._ssl = ctx

        root = os.path.dirname(self.out) or "."
        self.tmpdir = os.path.join(root, ".pcl_tmp")
        self.tempdir = os.path.join(self.tmpdir, TEMP_SUBDIR)   # :984
        self.state_path = os.path.join(self.tmpdir, self.local_name + ".json")

    # ---------------------------------------------------------- 源管理
    def has_available_source(self, allow_once: bool = True) -> bool:
        """ModNet.vb:631"""
        with self.lock_source:
            if any(not s.is_failed for s in self.sources):
                return True
            if allow_once and self.sources_once:
                return True
        return False

    def get_source(self, sid: int = 0) -> NetSource | None:
        """ModNet.vb:607"""
        with self.lock_source:
            if sid >= len(self.sources) or sid < 0:
                sid = 0
            if self.has_available_source(False):
                cur = sid
                for _ in range(len(self.sources) + 1):
                    if not self.sources[cur].is_failed:
                        return self.sources[cur]
                    cur = (cur + 1) % len(self.sources)
                return None
            if self.sources_once:
                return self.sources_once[0]
            return None

    # ---------------------------------------------------------- 建线程
    def _next_uuid(self) -> int:
        with self.lock_state:
            self._uuid_seq += 1
            return self._uuid_seq

    def try_begin_thread(self) -> NetThread | None:
        """ModNet.vb:808"""
        try:
            start = 0
            src: NetSource | None = None
            with self.lock_source:
                if self.manager.thread_count >= self.manager.thread_limit:
                    return None
                if not self.has_available_source():
                    return None
                if self.state >= NetState.Merging or self.state == NetState.WaitingToCheck:
                    return None
                with self.lock_state:
                    if self.state < NetState.Connecting:
                        self.state = NetState.Connecting

                if not self.has_available_source(False):
                    # 只剩单线程可用点（:826）
                    only = self.sources_once[0] if self.sources_once else None
                    if only and only.single_thread and not only.single_thread.is_ended:
                        return None
                    self.reset()
                    self.state = NetState.Reading

            with self.lock_chain:
                # 线程全死了且一个字节都没拿到：丢掉旧段，换源从头来
                alive = any(t.th and t.th.is_alive() for t in self.threads)
                if self.threads and not alive and self.download_done == 0:
                    self.reset()
                if not self.threads:
                    start = 0
                    src = self.get_source(self.first_thread_source)   # :837
                    if src is None:
                        return None
                    self.first_thread_source = src.id + 1
                else:
                    resumed = False
                    for t in self.threads:                            # :842 失败点续接
                        if t.state == NetState.Canceled and t.undone() > 0:
                            start = t.download_start + t.download_done
                            src = self.get_source(t.source.id + 1)
                            resumed = True
                            break
                    if not resumed:
                        src = self.get_source()                       # :850
                        if src is None:
                            return None
                        low = src.url.lower()
                        if any(h in low for h in NO_SPLIT_HOSTS):     # :853
                            return None
                        if self.file_size > 0:                        # 已知道大小才谈切分
                            # 找剩余最多的一段（:857）
                            target = max(self.threads, key=lambda t: t.undone())
                            if target.undone() < FILE_PIECE_LIMIT:    # :861
                                return None
                            end = self._end_of(target)
                            start = end - int(target.undone() * PIECE_CUT_RATIO)  # :862
                        else:
                            return None

                if src is None:
                    return None
                if self.file_size >= 0 and not self.is_unknown_size and start > self.file_size:
                    return None
                if start < 0:
                    return None

                th = NetThread(self._next_uuid(), self, start, src)   # :869
                th.th = threading.Thread(target=self._thread_body, args=(th,), daemon=True,
                                         name=f"dl/{self.uuid}/{th.uuid}")
                # 插入链表：按起点排序，前一段的“末端”自动变成 start-1（:877）
                pos = len(self.threads)
                for i, t in enumerate(self.threads):
                    if t.download_start > start:
                        pos = i
                        break
                self.threads.insert(pos, th)
                if not self.has_available_source(False):
                    with self.lock_source:
                        if self.sources_once:
                            self.sources_once[0].single_thread = th

            self.manager.thread_count += 1                        # :887
            th.th.start()
            return th
        except BaseException as e:                                # noqa: BLE001
            self.fail(e)
            return None

    def _end_of(self, th: NetThread) -> int:
        i = self.threads.index(th)
        if i + 1 < len(self.threads):
            return self.threads[i + 1].download_start - 1
        return BIG_SIZE if self.is_unknown_size or self.file_size < 0 else self.file_size - 1

    # ---------------------------------------------------------- 线程体
    def _thread_body(self, th: NetThread) -> None:
        """ModNet.vb:902"""
        src = th.source
        #: 慢源超时会一路放大，但封顶 30s（:909）
        base = max(self.connect_average or 0, TIMEOUT_MIN_MS)
        timeout = min(base * (1 + src.fail_count), TIMEOUT_MAX_MS) / 1000.0

        fh = None
        resp = None
        temp_new = False
        data_count = 0
        th.state = NetState.Connecting
        try:
            if src in self.sources_once and src.single_thread is not th:
                raise _SourceBreak

            req = urllib.request.Request(src.url, headers=dict(self.headers), method="GET")
            #: 断点续传时要按 start+done 取，上游因为 Done 恒为 0 所以写的是 Start（:917）
            pos = th.download_start + th.download_done
            if not th.is_first_thread:
                req.add_header("Range", f"bytes={pos}-")
            try:
                resp = urllib.request.urlopen(req, timeout=timeout, context=self._ssl)
            except urllib.error.HTTPError as e:
                raise HttpCodeError(f"错误码 {e.code}，{src.url}", e.code) from e

            code = resp.status
            if not (200 <= code < 300):
                raise HttpCodeError(f"错误码 {code}，{src.url}", code)

            with self.lock_state:
                if self.state == NetState.Canceled:
                    raise _SourceBreak

            cl_raw = resp.headers.get("Content-Length")
            content_length = int(cl_raw) if cl_raw and cl_raw.isdigit() else -1

            # ---- 文件大小校验（:926 起）
            if content_length < 0:
                if self.file_size > 1:
                    if pos == 0:
                        pass                                    # :930 从别处已拿到大小
                    else:
                        raise RangeNotSupported("ContentLength 返回 -1，视作不支持分段")  # :933
                else:
                    self.file_size = -1                         # :936
                    self.is_unknown_size = True
            elif th.is_first_thread:
                self.file_size = content_length                 # :948
                self.is_unknown_size = False
                if content_length > DISK_CHECK_MIN:
                    self._check_disk(content_length)
            elif self.file_size < 0:
                raise Exception("非首线程运行时，尚未获取文件大小")   # :963
            elif pos > 0 and content_length >= self.file_size:
                raise RangeNotSupported(                        # :964
                    f"该下载源不支持分段下载：Range 起始于 {pos}，"
                    f"预期 {self.file_size - pos}，实得 {content_length}，"
                    f"总大小 {self.file_size}")
            elif self.file_size - pos != content_length:
                raise RangeNotSupported(                        # :970
                    f"分段大小不一致：预期 {self.file_size - pos}，"
                    f"实得 {content_length}")

            th.state = NetState.Reading
            with self.lock_state:
                if self.state < NetState.Reading:
                    self.state = NetState.Reading

            # ---- 缓存文件（:984）
            self._ensure_tempdir()
            th.temp = self._temp_path(th)
            if os.path.exists(th.temp):
                fh = open(th.temp, "r+b")
                fh.seek(th.download_done)
            else:
                fh = open(th.temp, "w+b")
                temp_new = True

            # ---- 读循环（:991）
            want = READ_BUFFER
            buf = resp.read(want)
            while (self.is_unknown_size or th.undone() > 0) \
                    and buf and not self.manager._stop \
                    and self.state < NetState.Merging \
                    and (not src.is_failed or src.single_thread is th):
                # 限速（:996）
                while self.manager.speed_limit_high > 0 and self.manager.speed_limit_left <= 0:
                    time.sleep(0.016)
                real = len(buf) if self.is_unknown_size else min(len(buf), th.undone())
                if self.manager.speed_limit_high > 0:
                    self.manager.speed_limit_left -= real       # :1000

                delta = _now_ms() - th.last_receive
                if delta > 100000000:
                    delta = 1
                if real > 0:
                    if th.download_done == 0:                   # :1005
                        th.state = NetState.Downloading
                        with self.lock_state:
                            if self.state < NetState.Downloading:
                                self.state = NetState.Downloading
                        with self.lock_state:
                            self.connect_count += 1
                            self.connect_time += _now_ms() - th.init_time
                    src.fail_count = 0                          # :1014 有数据就清零
                    self.fail_count = 0
                    self.manager.download_done += real
                    self.download_done += real
                    th.download_done += real
                    fh.write(buf[:real])
                    if th.undone() == 0 and not self.is_unknown_size:
                        break                                   # :1023
                    # 速度过慢（:1025）
                    if th.last_receive > 0 and delta > SLOW_GAP_MS and delta > real \
                            and src.single_thread is None:
                        raise TimeoutError(
                            f"由于速度过慢断开链接，下载 {real} B，消耗 {delta} ms")
                    th.last_receive = _now_ms()
                elif th.last_receive > 0 and delta > timeout * 1000:
                    raise TimeoutError("操作超时，无数据")        # :1032
                buf = resp.read(want)

            # ---- 收束（:1037）
            if self.state == NetState.Canceled:
                th.state = NetState.Canceled
            elif src.is_failed and src.single_thread is not th:
                th.state = NetState.Canceled
            elif th.download_done == 0:
                # 一个字节都没拿到就结束：当作该源失败，否则会在同一位置无限重开线程
                th.state = NetState.Canceled
                self.source_fail(th, RuntimeError("无返回数据"), False)
            elif th.undone() > 0 and not self.is_unknown_size:
                th.state = NetState.Canceled
            else:
                th.state = NetState.Finished

        except _SourceBreak:
            th.state = NetState.Canceled
        except BaseException as e:                              # noqa: BLE001
            th.state = NetState.Canceled
            src.ex = e
            self.source_fail(th, e, False)
        finally:
            for obj in (resp, fh):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:
                    pass
            self.manager.thread_count -= 1                      # :1064
            if temp_new and fh and th.download_done == 0:
                try:
                    os.remove(th.temp)
                except OSError:
                    pass
            # 触发合并（:1066）
            total_done = sum(t.download_done for t in list(self.threads))
            finished_ok = (not self.is_unknown_size) or th.state == NetState.Finished
            if finished_ok and ((self.file_size >= 0 and total_done >= self.file_size)
                                or (self.file_size == -1 and total_done > 0)) \
                    and self.state < NetState.Merging and th.state != NetState.Canceled:
                self.merge(th)

    # ---------------------------------------------------------- 失败处理
    def source_fail(self, th: NetThread, ex: BaseException, is_merge: bool) -> None:
        """ModNet.vb:1070"""
        with self.lock_source:
            th.source.fail_count += 1
            self.fail_count += 1
        th.state = NetState.Canceled
        th.source.ex = ex

        msg = str(ex)
        code = ex.code if isinstance(ex, HttpCodeError) else 0
        range_bad = isinstance(ex, RangeNotSupported) or code == 416   # :1079
        should_disable = (
            is_merge or range_bad
            or code in (404, 502) or "(404)" in msg or "(502)" in msg
            or "未能解析" in msg or "无返回数据" in msg or "空间不足" in msg
            or ((code in (403, 429) or "(403)" in msg or "(429)" in msg)
                and "bmclapi" not in th.source.url)
            or (th.source.fail_count >= max(5, min(self.manager.thread_limit, 30))
                and self.download_done < 1)
            or th.source.fail_count > self.manager.thread_limit + FAIL_OVER_LIMIT
        )
        if not should_disable:                                  # :1085
            return

        with self.lock_source:
            if not th.source.is_failed or th.source.single_thread is th:
                th.source.is_failed = True
                if th.source in self.sources_once:
                    self.sources_once.remove(th.source)
            else:
                return

        if "空间不足" in msg:                                    # :1100
            self.fail(ex)
            return
        if self.has_available_source() and not is_merge:         # :1103
            return
        if not self.retried:                                     # :1105
            self.retry_total += 1
            self.retried = True
            with self.lock_source:
                self.sources_once.clear()
                for s in self.sources:
                    self.sources_once.append(s)
                    s.is_failed = True
            self.reset()
            with self.lock_state:
                self.state = NetState.WaitingToDownload
            return
        if self.has_available_source() and is_merge:             # :1121
            self.reset()
            with self.lock_state:
                self.state = NetState.WaitingToDownload
            return
        self.fail(ex)                                            # :1127

    def fail(self, ex: BaseException) -> None:
        """ModNet.vb:1251"""
        with self.lock_state:
            if self.state >= NetState.Finished:
                return
            self.ex.append(ex)
            self.state = NetState.Canceled

    def reset(self) -> None:
        """ModNet.vb:1147"""
        self.file_size = -2
        with self.lock_chain:
            for t in self.threads:
                if t.temp and os.path.exists(t.temp):
                    try:
                        os.remove(t.temp)
                    except OSError:
                        pass
            self.threads = []
        self.manager.download_done -= self.download_done
        self.download_done = 0
        self.is_unknown_size = False
        self._uuid_seq = 0

    # ---------------------------------------------------------- 合并
    def merge(self, th: NetThread) -> None:
        """ModNet.vb:1163"""
        if not self.lock_merge.acquire(blocking=False):
            return
        try:
            with self.lock_state:
                if self.state < NetState.Merging:
                    self.state = NetState.Merging
                else:
                    return
            attempt = 0
            while True:
                try:
                    os.makedirs(os.path.dirname(self.out) or ".", exist_ok=True)
                    part = self.out + ".part"
                    with self.lock_chain:
                        chain = list(self.threads)
                    if os.environ.get("PCLDL_DEBUG"):
                        sys.stderr.write("[merge] " + " | ".join(
                            f"{t.download_start}+{t.download_done}"
                            f"({os.path.getsize(t.temp) if t.temp and os.path.exists(t.temp) else -1})"
                            for t in chain) + f"  size={self.file_size}\n")
                        sys.stderr.flush()
                    if len(chain) == 1 and chain[0].temp and os.path.exists(chain[0].temp):
                        # :1184 单段直接拷贝
                        shutil.copyfile(chain[0].temp, part)
                    else:
                        with open(part, "wb") as out:             # :1193
                            for t in chain:
                                if t.download_done == 0 or not t.temp:
                                    continue
                                with open(t.temp, "rb") as fh:
                                    left = t.download_done      # :1197 按已下载量读
                                    while left > 0:
                                        blk = fh.read(min(1 << 20, left))
                                        if not blk:
                                            raise IOError(
                                                f"分片 @{t.download_start} 数据不足")
                                        out.write(blk)
                                        left -= len(blk)
                    self._verify(part)
                    os.replace(part, self.out)
                    with self.lock_chain:                         # :1221 清理
                        for t in chain:
                            if t.temp and os.path.exists(t.temp):
                                try:
                                    os.remove(t.temp)
                                except OSError:
                                    pass
                    self._clean_state()
                    self.elapsed = time.monotonic() - self.started
                    with self.lock_state:
                        self.state = NetState.Finished
                    if self.show:
                        self._log(f"  {Term.GRN}完成{Term.RST} -> {self.out}")
                    return
                except BaseException as e:                        # noqa: BLE001
                    attempt += 1
                    with self.lock_state:
                        if self.state > NetState.Merging:
                            return
                    if attempt >= MERGE_RETRY:                    # :1233
                        try:
                            if os.path.exists(self.out + ".part"):
                                os.remove(self.out + ".part")
                        except OSError:
                            pass
                        self.source_fail(th, e, True)
                        return
                    if self.show:
                        self._log(f"  {Term.YEL}合并出错，第 {attempt} 次重试：{e}{Term.RST}")
                    time.sleep(0.5 * attempt)
        finally:
            self.lock_merge.release()

    def _verify(self, part: str) -> None:
        size = os.path.getsize(part)
        if self.file_size > 0 and size != self.file_size:
            raise IOError(f"文件大小不一致：期望 {self.file_size}，实际 {size}")
        for algo, want in self.hashes.items():
            h = hashlib.new(algo)
            with open(part, "rb") as fh:
                for blk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(blk)
            if h.hexdigest().lower() != want:
                os.remove(part)
                raise IOError(f"{algo.upper()} 校验失败：{h.hexdigest()} != {want}")

    # ---------------------------------------------------------- 杂项
    @property
    def connect_average(self) -> int:
        """ModNet.vb:765"""
        n = self.connect_count
        return -1 if n == 0 else self.connect_time // n

    def _ensure_tempdir(self) -> None:
        os.makedirs(self.tempdir, exist_ok=True)

    def _temp_path(self, th: NetThread) -> str:
        """按起点命名，跨进程稳定（扩展：上游用随机名，无法续传）"""
        return os.path.join(self.tempdir, f"{self.url_key}_{th.download_start}.tmp")

    def _launch(self, th: NetThread) -> None:
        th.th = threading.Thread(target=self._thread_body, args=(th,), daemon=True,
                                 name=f"dl/{self.uuid}/{th.uuid}")
        self.manager.thread_count += 1
        th.th.start()

    def _check_disk(self, size: int) -> None:
        """ModNet.vb:951  缓存盘 1.1×，目标盘 size+5MB"""
        need = 0
        try:
            free = shutil.disk_usage(self.tempdir if os.path.isdir(self.tempdir)
                                     else (os.path.dirname(self.out) or ".")).free
            need = int(size * 1.1) + size + 5 * 1024 * 1024
            if free < need:
                raise Exception(f"空间不足：需要 {human(need)}，可用 {human(free)}")
        except OSError:
            pass

    # ---------------------------------------------------------- 状态持久化
    def save_state(self) -> None:
        if self.no_resume:
            return
        try:
            os.makedirs(self.tmpdir, exist_ok=True)
            with self.lock_chain:
                segs = [{"start": t.download_start, "done": t.download_done,
                         "temp": os.path.basename(t.temp) if t.temp else None}
                        for t in self.threads]
            snap = {"version": 3, "urls": [s.url for s in self.sources],
                    "size": self.file_size, "etag": self.etag,
                    "out": self.out, "segments": segs}
            if os.environ.get("PCLDL_DEBUG"):
                sys.stderr.write(f"[state] size={self.file_size} "
                                 f"segs={[(s['start'], s['done']) for s in segs]}\n")
            tmp = self.state_path + ".new"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def load_state(self) -> bool:
        if self.no_resume or not os.path.exists(self.state_path):
            return False
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception:
            return False
        if st.get("version") != 3 or st.get("size", 0) <= 0:
            return False
        self.file_size = st["size"]
        self.is_unknown_size = False
        self._ensure_tempdir()
        for d in st.get("segments", []):
            src = self.sources[0]
            th = NetThread(0, self, d["start"], src)
            th.temp = os.path.join(self.tempdir, d["temp"]) if d["temp"] else None
            if th.temp and os.path.exists(th.temp):
                th.download_done = min(d["done"], os.path.getsize(th.temp))
            else:
                th.download_done = 0
            th.state = NetState.WaitingToDownload
            self.threads.append(th)
            self._uuid_seq += 1
            th.uuid = self._uuid_seq
            self.download_done += th.download_done
        if not self.threads:
            return False
        self.manager.download_done += self.download_done
        return True

    def _clean_state(self) -> None:
        for p in (self.state_path,):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(self.tempdir)
        except OSError:
            pass
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    # ---------------------------------------------------------- 显示
    def _log(self, msg: str) -> None:
        with self._plock:
            try:
                sys.stdout.write("\r" + Term.CLR + msg + "\n")
            except UnicodeEncodeError:
                sys.stdout.write("\r" + msg.encode("ascii", "replace").decode("ascii") + "\n")
            sys.stdout.flush()

    def _report(self, force: bool = False) -> None:
        if not self.show:
            return
        now = time.monotonic()
        interval = 0.2 if sys.stdout.isatty() else 5.0
        if not force and now - self._last_print < interval:
            return
        if self.file_size <= 0 and not force:      # 还没拿到大小，先不画
            return
        self._last_print = now
        done = self.download_done
        size = max(self.file_size, 1)
        p = min(done / size, 1.0)
        shown = p if self.linear else 1 - (1 - p) ** 0.9
        w = 24
        bar = "█" * int(w * shown) + "░" * (w - int(w * shown))
        eta = (size - done) / self.manager.speed if self.manager.speed > 1 else 0
        eta_s = f"{int(eta)//60:02d}:{int(eta)%60:02d}" if eta else "--:--"
        live = sum(1 for t in self.threads if t.th and t.th.is_alive())
        line = (f"\r  {Term.CYA}{bar}{Term.RST} {shown*100:5.1f}%  "
                f"{human(done)}/{human(size)}  "
                f"{Term.GRN}{human(self.manager.speed)}/s{Term.RST}  "
                f"线程{live:<2d}  {eta_s}  ")
        with self._plock:
            try:
                sys.stdout.write(line + Term.CLR)
            except UnicodeEncodeError:
                sys.stdout.write(line.encode("ascii", "replace").decode("ascii"))
            sys.stdout.flush()

    # ---------------------------------------------------------- 主流程
    def run(self) -> str:
        self.started = time.monotonic()
        self.manager.register(self)
        self.manager.start()

        resumed = self.load_state()
        self.state = NetState.WaitingToDownload
        if resumed:
            if self.show:
                self._log(f"  {Term.YEL}断点续传：{human(self.download_done)} / "
                          f"{human(self.file_size)}，{len(self.threads)} 段{Term.RST}")
            self.state = NetState.Connecting
            for t in list(self.threads):
                if t.undone() > 0 and not t.is_ended:
                    self._launch(t)
        else:
            # 第一个线程由主线程拉起来（上游在 StartManager 的 waiting 分支做）
            self.try_begin_thread()

        last_save = time.monotonic()
        stall = 0
        try:
            while True:
                with self.lock_state:
                    st = self.state
                    err = self.ex[0] if self.ex else None
                self._report()

                now = time.monotonic()
                if self.file_size > 0 and now - last_save > 1.0:
                    last_save = now
                    self.save_state()

                if st == NetState.Canceled:
                    if err:
                        raise err
                    raise RuntimeError("下载被中断")
                if st == NetState.Finished:
                    break

                # 需要重新开始（换源重试 / 降级单线程）
                if st == NetState.WaitingToDownload:
                    self.try_begin_thread()

                # 线程全死了但还没下完 → 再拉一个（对应上游 ThreadStarter 的 waiting 分支）
                alive = [t for t in self.threads if t.th and t.th.is_alive()]
                if not alive and st != NetState.Merging:
                    need = self.file_size <= 0 or self.download_done < self.file_size
                    if need:
                        if self.try_begin_thread() is None:
                            stall += 1
                            if stall > 40:      # 2s 内起不来任何线程
                                self.fail(RuntimeError("无可用下载源"))
                        else:
                            stall = 0
                    elif self.file_size > 0 and self.download_done >= self.file_size:
                        if self.state < NetState.Merging and self.threads:
                            self.merge(self.threads[-1])
                time.sleep(0.05)
        except KeyboardInterrupt:
            with self.lock_state:
                self.state = NetState.Canceled
            self.save_state()
            self.manager.stop()
            if self.show:
                self._log(f"\n  {Term.YEL}已中断。重跑同一条命令可续传。{Term.RST}")
            raise SystemExit(130)
        except BaseException:
            self.save_state()
            self.manager.stop()
            raise
        self.manager.stop()
        self._report(force=True)
        if self.show:
            print()
        return self.out


class _SourceBreak(Exception):
    """ModNet.vb:1036 的 GoTo SourceBreak"""


# ================================================================== CLI
def derive_name(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    name = os.path.basename(path) or "download.bin"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:180]


def main(argv: list[str] | None = None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(
        prog="pcldl",
        description="PCL2 下载引擎（ModNet.vb）的 Python 移植",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  pcldl https://example.com/big.iso
  pcldl URL1 URL2 -o D:/dl/big.iso --limit 8M -x 16
  pcldl URL --sha256 <hex>
""")
    ap.add_argument("urls", nargs="+", help="下载地址，多个即视为同一文件的镜像")
    ap.add_argument("-o", "--out", help="输出文件路径（已存在的目录则按 URL 取名放进去）")
    ap.add_argument("-d", "--dir", help="输出目录，文件名按 URL 自动取")
    ap.add_argument("-n", "--name", help="指定文件名")
    ap.add_argument("--limit", type=parse_size, default="0", help="限速（0=不限）")
    ap.add_argument("-x", "--threads", type=int, default=16,
                    help="最大线程数，对应 PCL 的 ToolDownloadThread+1")
    ap.add_argument("--sha256")
    ap.add_argument("--sha1")
    ap.add_argument("--md5")
    ap.add_argument("-H", "--header", action="append", default=[], metavar="K: V")
    ap.add_argument("--referer")
    ap.add_argument("--cookie")
    ap.add_argument("--ua")
    ap.add_argument("-k", "--insecure", action="store_true")
    ap.add_argument("--linear", action="store_true", help="进度条线性显示")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("-V", "--version", action="version", version=f"pcldl {VERSION}")
    a = ap.parse_args(argv)

    headers = {}
    for h in a.header:
        if ":" not in h:
            ap.error(f"请求头格式应为 'Key: Value'，收到 {h!r}")
        k, v = h.split(":", 1)
        headers[k.strip()] = v.strip()
    if a.referer:
        headers["Referer"] = a.referer
    if a.cookie:
        headers["Cookie"] = a.cookie
    if a.ua:
        headers["User-Agent"] = a.ua

    out = None
    if a.dir:
        os.makedirs(a.dir, exist_ok=True)
        out = os.path.join(a.dir, a.name or derive_name(a.urls[0]))
    elif a.out:
        if os.path.isdir(a.out) or a.out.endswith(("/", "\\")):
            os.makedirs(a.out, exist_ok=True)
            out = os.path.join(a.out, a.name or derive_name(a.urls[0]))
        elif a.name:
            os.makedirs(a.out, exist_ok=True)
            out = os.path.join(a.out, a.name)
        else:
            out = a.out
    else:
        out = a.name or derive_name(a.urls[0])

    mgr = NetManager(limit_high=a.limit, thread_limit=max(1, a.threads))
    nf = NetFile(a.urls, out, mgr, headers=headers, insecure=a.insecure,
                 quiet=a.quiet, linear=a.linear, no_resume=a.no_resume,
                 hashes={"sha256": a.sha256, "sha1": a.sha1, "md5": a.md5})
    try:
        nf.run()
    except SystemExit:
        raise
    except Exception as e:                                  # noqa: BLE001
        print(f"\n  {Term.RED}失败：{e}{Term.RST}")
        return 1
    if not a.quiet:
        el = max(nf.elapsed, 1e-6)
        print(f"  {Term.DIM}用时 {el:.1f}s  平均 {human(nf.file_size / el)}/s"
              f"  峰值 {human(mgr.peak)}/s  线程上限 {mgr.thread_limit}"
              f"  重试 {nf.retry_total} 次{Term.RST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
