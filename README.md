# pcldl

> [PCL2](https://github.com/Meloong-Git/PCL) 内置下载引擎 `Modules/Base/ModNet.vb` 的 Python 移植。
> 单文件、零依赖、逐段对照源码移植，关键逻辑标注了 VB 行号。

PCL2 的下载引擎有个和 aria2 完全相反的设计：**分片数量不预先决定，而是在下载过程中按"当前连接喂不喂得满"动态生长**；段的边界不存在任何变量里，而是由链表中下一个节点的起点反推——插入一个新段，前一段就自动收缩了。

```bash
python pcldl.py https://example.com/big.iso
```

```
  ████████████████████████ 100.0%  36.9MB/36.9MB  12.2MB/s  线程0  --:--
  完成 -> ls-lR.gz
  用时 3.5s  平均 10.6MB/s  峰值 16.8MB/s  线程上限 16  重试 0 次
```

---

## 这是怎么做到的

上游 `ModNet.vb` 1933 行，核心是四个类：

```
NetManagerClass   全局调度：2 个 ThreadStarter（20ms）+ 1 个 StatRefresher（100ms）
   ├─ NetFile     一个文件：大小探测、链表、合并、校验
   │    ├─ NetSource  镜像源：FailCount / IsFailed / SingleThread
   │    └─ NetThread  一段字节区间：一个请求、一个 .tmp
   └─ 全局速度 / 速度下限 / 限速额度
```

移植时逐条核对了行为，下面每一条都能在源码里找到对应行：

| 行为 | 源码位置 | 本仓库实现 |
|---|---|---|
| 首线程**不发** Range，靠 `Content-Length` 拿大小 | `:917` | `_thread_body` 里 `if not th.is_first_thread` |
| 段边界由下一节点起点推导 | `:483-497` | `NetThread.download_end()` |
| 切分点 = 最大段末端 − 未完成量 × 0.4 | `:862` | `try_begin_thread()` |
| 未完成量 < 256KB 不切 | `:772` / `:861` | `FILE_PIECE_LIMIT` |
| 速度下限初值 256KB/s | `:319` | `SPEED_LOW_INIT` |
| 速度下限只涨不跌：近 10 次均值 × 0.85 | `:1699` | `refresh_stat()` |
| 调度 20ms、限速配额 100ms | `:1727` / `:1783` | `_starter` / `_refresher` |
| 收到数据就把 FailCount 清零 | `:1014` | `src.fail_count = 0` |
| 续传判定：`Start > 0 且 ContentLength ≥ FileSize` | `:964` | `RangeNotSupported` |
| 分段大小对不上也判不支持 | `:970` | 同上 |
| 超时 `min(max(ConnectAvg,15000)×(1+Fail),30000)` | `:909` | `timeout` |
| 慢速断开：间隔 >5s 且 间隔 > 本次字节数 | `:1025` | 同左 |
| 只对 `pcl2-server/meloong/bmclapi/github/optifine/momot.rs` 禁多线程 | `:853` | `NO_SPLIT_HOSTS` |
| 403/429/404/502/Range 问题 → 禁用源 | `:1079-1084` | `source_fail()` |
| 全源失败 → 标记全部 IsFailed → 单线程重来一次 | `:1105` | `retry` 分支 |
| 合并按 `DownloadDone` 逐段读，单段直接拷贝 | `:1197` / `:1184` | `merge()` |
| 合并失败重试 3 次，间隔 500ms×n | `:1233` | `MERGE_RETRY` |
| >50MB 才查磁盘，缓存盘 1.1× + 目标盘 size+5MB | `:951` | `_check_disk()` |

### 一个容易看错的细节

`TryBeginThread` 里那行

```vb
StartPosition = FilePieceMax.DownloadEnd - FilePieceMax.DownloadUndone * 0.4
```

看着像"从末尾往前 40%"，实际效果是**从该段当前进度的前方插入一个新节点**。因为 `DownloadEnd` 是 `下一节点.Start - 1`，插入后原段自动缩短，正在跑的那个线程下一次循环发现 `DownloadUndone == 0` 就自己退出了——没有任何显式的"修改区间"操作，靠链表顺序保证一致性。

### 什么时候才会加分片

不是"一上来开 16 个"，而是：

```
全局速度 < 速度下限（初始 256KB/s） 且 准备中的线程数 ≤ 下载中的线程数
```

而速度下限会随着实际速度抬升到"近期均速 × 0.85"，所以链路跑满之后就不会再加了——它的目标是**别让现有连接饿着**，不是榨干带宽。实测：公网 8MB/s 的源，一路加到 5~10 个线程；本地回环 30MB/s 的源，2~3 个线程就停。

---

## 和上游不一样的地方

移植不是照抄。上游有几处不适合命令行场景，做了明确改动：

| 项 | 上游 | 本仓库 | 原因 |
|---|---|---|---|
| 跨进程续传 | 无（每次 `Start` 会清空 `Download` 缓存目录，`:1809`） | 有，状态每 1s 落盘 | 命令行被杀是常态 |
| 临时文件名 | `{FileUuid}_{ThreadUuid}_{随机}.tmp` | `{urlHash}_{起始偏移}.tmp` | 续传要能找回 |
| Range 起点 | 恒为 `DownloadStart`（因为 Done 恒为 0） | `Start + Done` | 续传必需 |
| 限速额度 | 无上限累积 | 钳到 1 秒额度 | 避免空闲后突发 |
| 未知大小文件 | `DownloadDone > 0` 就合并（`:1066`） | 等线程自然结束 | 避免截断 chunked 响应 |
| 界面 | WPF | 终端进度条（同样的 `1-(1-p)^0.9` 曲线） | — |

---

## 安装

Python ≥ 3.9，无第三方依赖：

```bash
git clone https://github.com/qaz1872/pcldl.git
cd pcldl
python pcldl.py --help
```

或装成命令：

```bash
pip install .
pcldl https://example.com/big.iso
```

---

## 用法

```bash
# 最简
pcldl https://example.com/big.iso

# 指定输出 + 限速 8MB/s + 最多 16 线程
pcldl https://example.com/big.iso -o D:/dl/big.iso --limit 8M -x 16

# 多镜像（同一文件的不同源，自动负载 + 容错）
pcldl https://a.com/x.iso https://b.com/x.iso -o x.iso

# 下完校验
pcldl URL --sha256 3f7a...e21

# 需要 Cookie / Referer
pcldl URL --referer https://example.com/ --cookie "sid=abc"

# 中断了？重跑同一条命令即续传
pcldl URL -o D:/dl/big.iso
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `-o, --out` | URL 文件名 | 输出**文件**路径（给已存在的目录则放进去） |
| `-d, --dir` | 当前目录 | 输出**目录**，文件名按 URL 自动取 |
| `--limit` | 0（不限） | 限速，支持 `4M` / `512K` |
| `-x, --threads` | 16 | 线程上限，对应 PCL 的 `ToolDownloadThread + 1` |
| `--sha256/--sha1/--md5` | — | 完成后校验 |
| `-H, --header` | — | 自定义请求头，可重复 |
| `--referer/--cookie/--ua` | — | 常用头快捷方式 |
| `-k, --insecure` | — | 跳过 TLS 校验 |
| `--linear` | — | 进度条线性显示 |
| `--no-resume` | — | 忽略断点 |
| `-q, --quiet` | — | 静默 |

---

## 测试

本地起 HTTP 服务，六个用例覆盖引擎的主要分支，产物逐字节比对 SHA-256：

```
[PASS] 不支持 Range 退化为单线程  线程=1
[PASS] 多源容错（首源 500）
[PASS] 令牌桶限速 4MB/s          实测 4.29MB/s
[PASS] 正常下载                  24MB  线程=5
[PASS] 慢源自动加分片            120KB/s 单连接 → 线程=10
[PASS] 中断后续传                32MB  首跑14.5MB + 续传共34.7MB  hash=True
```

```bash
python tests/test_pcldl.py
```

真实网络（清华源 36.9MB）：3.5s，均速 10.6MB/s，`gzip` 完整解压校验通过（416,176,744 字节）。

---

## 已知限制

- **峰值磁盘约 2×**：分段期间 1×，合并时 `.part` + 所有 `.tmp` 同时存在。上游的 1.1 倍检查是提前拦截，不是精确峰值保证。
- **合并是串行的**：大文件最后一步受单盘顺序写限制。
- **状态每 1s 落盘**：强杀最多丢 1s 进度。
- **无 HTTP/2**：一个分片一条 TCP 连接，与上游一致。
- **镜像需内容一致**：多源模式只校验总大小与哈希，不做事后一致性比对。
- **代理**：走环境变量 `HTTP_PROXY` / `HTTPS_PROXY`。

## 许可

MIT，见 [LICENSE](LICENSE)。

上游 [Meloong-Git/PCL](https://github.com/Meloong-Git/PCL) 为 VB.NET / WPF 项目，本仓库是独立实现的 Python 移植，**不含其源码**，仅参考其算法与常量。
