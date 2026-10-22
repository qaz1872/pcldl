#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pcldl.py —— PCL2 下载引擎（Modules/Base/ModNet.vb）的 1:1 复刻，纯标准库。

https://github.com/qaz1872/pcldl

复刻的语义：
  NetSource  镜像源 + 失败计数 + 存活判定
  NetThread  一段字节区间：一个独立 HTTP 请求、一个独立 .tmp、可单独回滚
  NetFile    一个文件：探测大小、动态长分片、合并、校验、状态持久化
  NetManager 全局调度：120ms 心跳、边际收益判据、令牌桶限速

和 PCL 保持一致的行为：
  * 先跑 1 段，喂不满再切；切"剩余最多"那段的尾部 40%
  * 切完总速度涨不到 10% 就指数退避，不无脑堆线程
  * 每段一个 .tmp，合并串行读，段内多余字节丢弃
  * 服务端无视 Range（回 200）→ 判定不支持续传，降级单流
  * 超时随失败次数放宽、失败到阈值换源
  * 开工前检查 1.1 倍磁盘空间
  * 进度条非线性映射 1-(1-p)^0.9 + 200ms 节流

用法：
  python pcldl.py https://example.com/big.iso
  python pcldl.py https://a/x.iso https://b/x.iso -o D:/dl/x.iso --limit 8M -x 16
  python pcldl.py URL --sha256 <hex>          # 下完校验
  Ctrl+C 中断后重跑同一条命令即断点续传
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ------------------------------------------------------------------ 常量
VERSION = "1.0"
CHUNK = 64 * 1024            # 单次读取缓冲（PCL 用 16KB，Python 里 64KB 更划算）
PIECE_MIN = 256 * 1024       # 新开一段的最小剩余量，低于它就不切了
TICK = 0.12                  # 调度心跳 120ms
SPLIT_RATIO = 0.40           # 每次从目标段尾部切走的比例
GAIN_KEEP = 1.10             # 切完速度涨幅 <10% 视为带宽已满
MAX_SRC_FAIL = 8             # 单源失败到这个数就判死
MAX_CHUNK_RETRY = 10         # 单段连续断流重连上限
UA = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) pcldl/{VERSION}"


# ------------------------------------------------------------------ 小工具
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def parse_size(text: str) -> int:
    t = text.strip().upper().replace(" ", "")
    m = re.fullmatch(r"([\d.]+)([KMG]?)", t)
    if not m:
        raise argparse.ArgumentTypeError(f"无法解析大小: {text}")
    mult = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[m.group(2)]
    return int(float(m.group(1)) * mult)


def setup_console() -> None:
    """Windows 控制台 UTF-8 + ANSI。"""
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
    BLD = "\x1b[1m" if ON else ""
    RST = "\x1b[0m" if ON else ""
    CLR = "\x1b[K" if ON else ""


# ------------------------------------------------------------------ 异常
class NoRangeSupport(Exception):
    """服务端无视 Range，返回 200 全量。"""


class SourceDead(Exception):
    pass


class Aborted(Exception):
    pass


# ------------------------------------------------------------------ 令牌桶
class TokenBucket:
    """容量取 rate/10（约 100ms 配额），和 PCL 每 100ms 补一次配额一致，
    不做 1 秒的突发，否则小文件会被整包放行、看起来没限速。"""

    def __init__(self, rate: int = 0, chunk: int = CHUNK):
        self.rate = max(0, rate)
        self.cap = max(chunk, self.rate // 10) if self.rate else 0
        self.tokens = float(self.cap)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def take(self, n: int) -> None:
        if self.rate <= 0:
            return
        while n > 0:                      # 单次请求大于桶容量就拆开等
            k = min(n, self.cap)
            while True:
                with self.lock:
                    now = time.monotonic()
                    self.tokens = min(float(self.cap), self.tokens + (now - self.last) * self.rate)
                    self.last = now
                    if self.tokens >= k:
                        self.tokens -= k
                        break
                    wait = (k - self.tokens) / self.rate
                time.sleep(min(wait, 0.05))
            n -= k


# ------------------------------------------------------------------ 镜像源
class Source:
    def __init__(self, url: str):
        self.url = url
        self.tag = urllib.parse.urlsplit(url).netloc or url
        self.fail = 0
        self.dead = False
        self.bytes = 0
        self.conn_ms: list[float] = []

    def timeout(self) -> float:
        avg = (sum(self.conn_ms) / len(self.conn_ms) / 1000.0) if self.conn_ms else 0.0
        return min(max(avg, 6.0) * (1 + self.fail), 30.0)

    @property
    def avg_conn_ms(self) -> float:
        return sum(self.conn_ms) / len(self.conn_ms) if self.conn_ms else 0.0

    def __repr__(self) -> str:
        return f"<Source {self.tag} fail={self.fail}{' DEAD' if self.dead else ''}>"


# ------------------------------------------------------------------ 一段
class Segment:
    __slots__ = ("start", "end", "pos", "tmp", "th", "busy")

    def __init__(self, start: int, end: int, tmp: str, pos: int | None = None):
        self.start = start
        self.end = end                  # 闭区间
        self.pos = start if pos is None else pos
        self.tmp = tmp
        self.th: threading.Thread | None = None
        self.busy = 0

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def done(self) -> int:
        return max(0, min(self.pos, self.end + 1) - self.start)

    @property
    def remain(self) -> int:
        return max(0, self.length - self.done)


# ================================================================== NetFile
class NetFile:
    def __init__(self, urls: list[str], out: str, *,
                 limit: int = 0, max_threads: int = 16,
                 min_piece: int = PIECE_MIN, chunk: int = CHUNK,
                 headers: dict[str, str] | None = None,
                 insecure: bool = False, quiet: bool = False,
                 linear_progress: bool = False, no_resume: bool = False,
                 hashes: dict[str, str] | None = None):
        self.urls = urls
        self.sources = [Source(u) for u in urls]
        self.out = os.path.abspath(out)
        self.limit = limit
        self.max_threads = max(1, max_threads)
        self.min_piece = max(64 * 1024, min_piece)
        self.chunk = max(8 * 1024, chunk)
        self.headers = headers or {}
        self.quiet = quiet
        self.linear = linear_progress
        self.no_resume = no_resume
        self.hashes = {k: v.lower() for k, v in (hashes or {}).items() if v}

        self.size = -1
        self.supports_range = True
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.plain = False

        self.segments: list[Segment] = []
        self.lock = threading.Lock()
        self.bucket = TokenBucket(limit, chunk)
        self.stop = False
        self.error: BaseException | None = None
        self.retry_total = 0

        # 调速
        self.speed = 0.0
        self.peak = 0.0
        self.split_speed = 0.0
        self.next_split_at = 0.0
        self.cool = 1.0
        self._last_done = 0
        self._last_t = time.monotonic()

        root = os.path.dirname(self.out)
        self.tmpdir = os.path.join(root, ".pcl_tmp")
        self.state_path = os.path.join(self.tmpdir, os.path.basename(self.out) + ".json")

        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._ssl = ctx

        self._print_lock = threading.Lock()
        self._last_print = 0.0
        self.started = 0.0
        self.elapsed = 0.0
        self.show = not quiet

    # ---------------------------------------------------------- HTTP
    def _req(self, url: str, first: int | None = None, last: int | None = None):
        h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        h.update(self.headers)
        if first is not None:
            h["Range"] = f"bytes={first}-{'' if last is None else last}"
        return urllib.request.Request(url, headers=h, method="GET")

    def _open(self, src: Source, first: int | None = None, last: int | None = None):
        t0 = time.monotonic()
        resp = urllib.request.urlopen(self._req(src.url, first, last),
                                       timeout=src.timeout(), context=self._ssl)
        src.conn_ms.append((time.monotonic() - t0) * 1000)
        del src.conn_ms[:-8]
        return resp

    # ---------------------------------------------------------- 探测
    def probe(self) -> None:
        """取文件大小 / 是否支持 Range / ETag。镜像逐个试。"""
        errs = []
        for src in self.sources:
            try:
                with self._open(src, 0, 0) as r:
                    cr = r.headers.get("Content-Range")
                    if r.status == 206 and cr and "/" in cr:
                        total = cr.rsplit("/", 1)[-1].strip()
                        if total.isdigit():
                            self.size = int(total)
                            self.supports_range = True
                        else:
                            self.size = -1
                            self.supports_range = False
                        self.etag = r.headers.get("ETag")
                        self.last_modified = r.headers.get("Last-Modified")
                    elif r.status == 200:
                        self.size = int(r.headers.get("Content-Length") or -1)
                        self.supports_range = False
                    else:
                        raise urllib.error.HTTPError(src.url, r.status, "unexpected status", r.headers, None)
                if self.size > 0 or not self.supports_range:
                    return
            except Exception as e:                      # noqa: BLE001
                errs.append(f"{src.tag}: {e}")
                src.fail += 1
                if src.fail >= MAX_SRC_FAIL:
                    src.dead = True
        raise RuntimeError("探测失败，所有源都不可用：\n  " + "\n  ".join(errs))

    # ---------------------------------------------------------- 状态
    def _snapshot(self) -> dict:
        with self.lock:
            segs = [{"start": s.start, "end": s.end, "pos": s.pos,
                     "tmp": os.path.basename(s.tmp)} for s in self.segments]
        return {"version": 1, "urls": self.urls, "size": self.size,
                "etag": self.etag, "last_modified": self.last_modified,
                "out": self.out, "plain": self.plain,
                "hashes": self.hashes, "segments": segs}

    def _save_state(self) -> None:
        try:
            os.makedirs(self.tmpdir, exist_ok=True)
            tmp = self.state_path + ".new"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._snapshot(), fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _load_state(self) -> bool:
        if self.no_resume or not os.path.exists(self.state_path):
            return False
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception:
            return False
        if st.get("size") != self.size or st.get("plain"):
            return False
        if st.get("etag") and self.etag and st["etag"] != self.etag:
            return False
        if st.get("last_modified") and self.last_modified and st["last_modified"] != self.last_modified:
            return False

        segs = []
        for d in st.get("segments", []):
            tmp = os.path.join(self.tmpdir, d["tmp"])
            start, end = int(d["start"]), int(d["end"])
            if not os.path.exists(tmp):
                pos = start
                open(tmp, "wb").close()
            else:
                pos = start + min(os.path.getsize(tmp), end - start + 1)
            pos = max(start, min(pos, end + 1))
            segs.append(Segment(start, end, tmp, pos))
        if not segs:
            return False
        self.segments = sorted(segs, key=lambda s: s.start)
        return True

    def _clean(self) -> None:
        for s in self.segments:
            try:
                os.remove(s.tmp)
            except OSError:
                pass
        try:
            os.remove(self.state_path)
        except OSError:
            pass
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    # ---------------------------------------------------------- 源选择
    def _pick_source(self) -> Source:
        with self.lock:
            alive = [s for s in self.sources if not s.dead]
            if not alive:
                raise SourceDead("所有镜像源均已失效")
            alive.sort(key=lambda s: (s.fail, s.bytes))
            return alive[0]

    def _blame(self, src: Source, why: str) -> None:
        with self.lock:
            src.fail += 1
            dead = src.fail >= MAX_SRC_FAIL
            if dead:
                src.dead = True
        if dead and self.show and not self.quiet:
            self._log(f"{Term.RED}源 {src.tag} 已失效（{src.fail} 次失败）{Term.RST}")

    # ---------------------------------------------------------- 工作线程
    def _start(self, seg: Segment) -> None:
        seg.th = threading.Thread(target=self._work, args=(seg,), daemon=True)
        seg.th.start()

    def _work(self, seg: Segment) -> None:
        fh = None
        try:
            mode = "r+b" if os.path.exists(seg.tmp) else "w+b"
            fh = open(seg.tmp, mode)
            stall = 0
            while not self.stop:
                if seg.pos > seg.end:
                    return
                src = self._pick_source()
                pos = seg.pos
                try:
                    resp = self._open(src, pos, seg.end)
                    if resp.status != 206:
                        resp.close()
                        raise NoRangeSupport
                except urllib.error.HTTPError as e:
                    if e.code == 416:
                        seg.pos = seg.end + 1
                        return
                    self._blame(src, f"HTTP {e.code}")
                    self.retry_total += 1
                    time.sleep(min(0.3 * src.fail, 3.0))
                    continue
                except NoRangeSupport:
                    raise
                except Exception as e:                  # noqa: BLE001
                    self._blame(src, str(e))
                    self.retry_total += 1
                    time.sleep(min(0.3 * src.fail, 3.0))
                    continue

                got_any = 0
                try:
                    fh.seek(pos - seg.start)
                    while not self.stop:
                        end = seg.end
                        if seg.pos > end:
                            break
                        n = min(self.chunk, end - seg.pos + 1)
                        self.bucket.take(n)
                        buf = resp.read(n)
                        if not buf:
                            break
                        fh.write(buf)
                        seg.pos += len(buf)
                        got_any += len(buf)
                        src.bytes += len(buf)
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass

                if seg.pos > seg.end:
                    return
                if got_any == 0:
                    stall += 1
                    self._blame(src, "断流")
                    if stall > MAX_CHUNK_RETRY:
                        raise IOError(f"分片 @{seg.start} 反复断流")
                    time.sleep(min(0.5 * stall, 4.0))
                else:
                    stall = 0
        except NoRangeSupport:
            with self.lock:
                self.error = NoRangeSupport("该下载源不支持断点续传")
        except BaseException as e:                      # noqa: BLE001
            with self.lock:
                self.error = self.error or e
        finally:
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass

    # ---------------------------------------------------------- 调度
    def _manager(self) -> None:
        last_save = 0.0
        while not self.stop:
            time.sleep(TICK)
            now = time.monotonic()
            with self.lock:
                done = sum(s.done for s in self.segments)
            dt = now - self._last_t
            if dt <= 0:
                continue
            inst = max(0.0, (done - self._last_done) / dt)
            self.speed = 0.7 * self.speed + 0.3 * inst
            self.peak = max(self.peak, self.speed)
            self._last_done, self._last_t = done, now

            if now - last_save > 1.0:
                last_save = now
                self._save_state()

            with self.lock:
                remain = self.size - done
            if remain <= self.min_piece * 2 or now < self.next_split_at:
                continue
            if len(self.segments) >= self.max_threads:
                continue
            if self.split_speed > 0 and self.speed < self.split_speed * GAIN_KEEP:
                self.cool = min(max(self.cool * 2, 1.0), 8.0)
                self.next_split_at = now + self.cool
                continue
            if self._split_once():
                self.split_speed = self.speed
                self.cool = 1.0
                self.next_split_at = now + 1.2

    def _split_once(self) -> bool:
        if not self.supports_range or self.size <= 0:
            return False
        with self.lock:
            if len(self.segments) >= self.max_threads:
                return False
            tgt = max(self.segments, key=lambda s: s.remain)
            if tgt.remain < self.min_piece * 2:
                return False
            cut = tgt.end - int(tgt.remain * SPLIT_RATIO)
            if cut <= tgt.start:
                return False
            new = Segment(cut + 1, tgt.end,
                          os.path.join(self.tmpdir, f"{random.randrange(10**9):09d}.tmp"))
            tgt.end = cut
            self.segments.append(new)
        self._start(new)
        return True

    # ---------------------------------------------------------- 显示
    def _log(self, msg: str) -> None:
        with self._print_lock:
            sys.stdout.write("\r" + Term.CLR + msg + "\n")
            sys.stdout.flush()

    def _report(self, done: int, force: bool = False) -> None:
        if not self.show:
            return
        now = time.monotonic()
        interval = 0.2 if sys.stdout.isatty() else 5.0
        if not force and now - self._last_print < interval:
            return
        self._last_print = now
        p = done / self.size if self.size > 0 else 0.0
        shown = p if self.linear else 1 - (1 - p) ** 0.9
        bar_w = 24
        fill = int(bar_w * shown)
        bar = "█" * fill + "░" * (bar_w - fill)
        eta = (self.size - done) / self.speed if self.speed > 1 else 0
        eta_s = f"{int(eta)//60:02d}:{int(eta)%60:02d}" if eta else "--:--"
        line = (f"\r  {Term.CYA}{bar}{Term.RST} {shown*100:5.1f}%  "
                f"{human(done)}/{human(self.size)}  "
                f"{Term.GRN}{human(self.speed)}/s{Term.RST}  "
                f"分片{len(self.segments):<2d}  {eta_s}  ")
        with self._print_lock:
            sys.stdout.write(line + Term.CLR)
            sys.stdout.flush()

    # ---------------------------------------------------------- 合并校验
    def _merge(self) -> str:
        part = self.out + ".part"
        with open(part, "wb") as out:
            for seg in sorted(self.segments, key=lambda s: s.start):
                with open(seg.tmp, "rb") as fh:
                    left = seg.length
                    while left > 0:
                        buf = fh.read(min(1 << 20, left))
                        if not buf:
                            raise IOError(f"分片 @{seg.start} 数据不足，可能已被截断")
                        out.write(buf)
                        left -= len(buf)
        return part

    def _verify(self, part: str) -> None:
        got = os.path.getsize(part)
        if self.size > 0 and got != self.size:
            raise IOError(f"大小校验失败：期望 {self.size}，实际 {got}")
        for algo, want in self.hashes.items():
            h = hashlib.new(algo)
            with open(part, "rb") as fh:
                for blk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(blk)
            if h.hexdigest().lower() != want:
                os.remove(part)
                raise IOError(f"{algo.upper()} 校验失败：{h.hexdigest()} != {want}")

    # ---------------------------------------------------------- 主流程
    def run(self) -> str:
        if os.path.exists(self.out) and self.size < 0:
            pass
        self.started = time.monotonic()
        self.probe()
        if self.show:
            self._log(f"  {Term.DIM}大小 {human(self.size) if self.size > 0 else '未知'}"
                      f"  {'支持 Range' if self.supports_range else '不支持 Range'}"
                      f"  源 {len([s for s in self.sources if not s.dead])} 个{Term.RST}")

        if self.size <= 0 or not self.supports_range:
            return self._run_plain()

        resumed = self._load_state()
        if resumed and self.show:
            have = sum(s.done for s in self.segments)
            self._log(f"  {Term.YEL}断点续传：已完成 {human(have)} / {human(self.size)}{Term.RST}")

        os.makedirs(self.tmpdir, exist_ok=True)
        free = 0
        try:
            import shutil
            free = shutil.disk_usage(os.path.dirname(self.out) or ".").free
        except Exception:
            pass
        need = int(self.size * 1.1)
        if free and free < need and not resumed:
            raise OSError(f"磁盘空间不足：需要 {human(need)}，可用 {human(free)}")

        if not self.segments:
            self.segments.append(Segment(0, self.size - 1,
                                         os.path.join(self.tmpdir, f"{random.randrange(10**9):09d}.tmp")))
        for seg in self.segments:
            if seg.remain > 0:
                self._start(seg)
        threading.Thread(target=self._manager, daemon=True).start()

        try:
            while True:
                with self.lock:
                    done = sum(s.done for s in self.segments)
                    err = self.error
                self._report(done)
                if err:
                    if isinstance(err, NoRangeSupport):
                        self._shutdown()
                        return self._run_plain()
                    raise err
                if done >= self.size:
                    break
                if not any(s.th and s.th.is_alive() for s in self.segments):
                    with self.lock:
                        if sum(s.done for s in self.segments) < self.size:
                            raise IOError("所有分片线程已退出，但文件未下完")
                time.sleep(0.05)
        except KeyboardInterrupt:
            self._shutdown()
            self._save_state()
            if self.show:
                self._log(f"\n  {Term.YEL}已中断。重跑同一条命令可续传。{Term.RST}")
            raise Aborted
        except BaseException:
            self._shutdown()
            self._save_state()
            raise

        self._shutdown()
        self._report(self.size, force=True)
        if self.show:
            print()

        part = self._merge()
        self._verify(part)
        os.replace(part, self.out)
        self._clean()
        self.elapsed = time.monotonic() - self.started
        if self.show:
            self._log(f"  {Term.GRN}完成{Term.RST} -> {self.out}")
        return self.out

    def _shutdown(self) -> None:
        self.stop = True
        for s in self.segments:
            if s.th:
                s.th.join(timeout=3)

    # ---------------------------------------------------------- 降级单流
    def _run_plain(self) -> str:
        self.plain = True
        if self.show:
            self._log(f"  {Term.YEL}[降级]{Term.RST} 该源不支持 Range，改用单流下载")
        # 清掉分段产物
        for s in self.segments:
            try:
                os.remove(s.tmp)
            except OSError:
                pass
        os.makedirs(self.tmpdir, exist_ok=True)
        tmp = os.path.join(self.tmpdir, "plain.tmp")
        done = 0
        t0 = time.monotonic()
        while True:
            src = self._pick_source()
            try:
                req = self._req(src.url)
                with urllib.request.urlopen(req, timeout=30, context=self._ssl) as r, open(tmp, "wb") as fh:
                    total = int(r.headers.get("Content-Length") or 0)
                    if total:
                        self.size = total
                    while True:
                        self.bucket.take(self.chunk)
                        buf = r.read(self.chunk)
                        if not buf:
                            break
                        fh.write(buf)
                        done += len(buf)
                        el = max(time.monotonic() - t0, 1e-6)
                        self.speed = 0.7 * self.speed + 0.3 * (done / el)
                        if self.show and (time.monotonic() - self._last_print > 0.2):
                            self._last_print = time.monotonic()
                            with self._print_lock:
                                sys.stdout.write(
                                    f"\r  {done*100/max(self.size,1):5.1f}%  "
                                    f"{human(done)}/{human(self.size)}  "
                                    f"{Term.GRN}{human(self.speed)}/s{Term.RST}  单流 " + Term.CLR)
                                sys.stdout.flush()
                break
            except Exception:                            # noqa: BLE001
                self._blame(src, "单流失败")
                done = 0
                if all(s.dead for s in self.sources):
                    raise
                time.sleep(1.0)
        if self.show:
            print()
        self._verify(tmp)
        os.replace(tmp, self.out)
        self._clean()
        self.elapsed = time.monotonic() - self.started
        if self.show:
            self._log(f"  {Term.GRN}完成{Term.RST} -> {self.out}")
        return self.out


# ================================================================== CLI
def derive_name(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    name = os.path.basename(path) or "download.bin"
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:180]


def main(argv: list[str] | None = None) -> int:
    setup_console()
    ap = argparse.ArgumentParser(
        prog="pcldl",
        description="PCL2 下载引擎复刻版（动态分片 / 多源容错 / 断点续传）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  pcldl https://example.com/big.iso
  pcldl URL1 URL2 -o D:/dl/big.iso --limit 8M -x 16
  pcldl URL --sha256 <hex>
""")
    ap.add_argument("urls", nargs="+", help="下载地址，多个即视为同一文件的镜像")
    ap.add_argument("-o", "--out", help="输出文件路径或目录")
    ap.add_argument("-n", "--name", help="输出文件名（-o 为目录时生效）")
    ap.add_argument("--limit", type=parse_size, default="0", help="限速，如 8M（0=不限）")
    ap.add_argument("-x", "--connections", type=int, default=16, help="最大分片数，默认 16")
    ap.add_argument("--min-piece", type=parse_size, default=str(PIECE_MIN), help="最小分片，默认 256K")
    ap.add_argument("--chunk", type=parse_size, default=str(CHUNK), help="读取缓冲，默认 64K")
    ap.add_argument("--sha256"), ap.add_argument("--sha1"), ap.add_argument("--md5")
    ap.add_argument("-H", "--header", action="append", default=[], metavar="K: V",
                    help="自定义请求头，可重复")
    ap.add_argument("--referer"), ap.add_argument("--cookie"), ap.add_argument("--ua")
    ap.add_argument("-k", "--insecure", action="store_true", help="跳过 TLS 证书校验")
    ap.add_argument("--linear", action="store_true", help="进度条线性显示（默认模拟 PCL 曲线）")
    ap.add_argument("--no-resume", action="store_true", help="忽略已有断点，重新下")
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

    url = a.urls[0]
    out = a.out
    if not out:
        out = a.name or derive_name(url)
    elif os.path.isdir(out) or out.endswith(("/", "\\")):
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, a.name or derive_name(url))
    elif a.name:
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, a.name)

    hashes = {"sha256": a.sha256, "sha1": a.sha1, "md5": a.md5}

    nf = NetFile(a.urls, out,
                 limit=a.limit, max_threads=a.connections,
                 min_piece=a.min_piece, chunk=a.chunk,
                 headers=headers, insecure=a.insecure, quiet=a.quiet,
                 linear_progress=a.linear, no_resume=a.no_resume, hashes=hashes)
    try:
        nf.run()
    except Aborted:
        return 130
    except KeyboardInterrupt:
        return 130
    except Exception as e:                              # noqa: BLE001
        print(f"\n  {Term.RED}失败：{e}{Term.RST}")
        return 1
    if not a.quiet:
        el = max(nf.elapsed, 1e-6)
        print(f"  {Term.DIM}用时 {el:.1f}s  平均 {human(nf.size / el)}/s"
              f"  峰值 {human(nf.peak)}/s  分片 {max(len(nf.segments), 1)}"
              f"  重试 {nf.retry_total} 次{Term.RST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
