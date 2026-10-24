# xhttp

一个用 Python 写的 **VLESS over XHTTP（stream-one）+ 本地 HTTP / SOCKS5 代理** 客户端。
给 Windows 本机使用，最终可以用 PyInstaller 打包成单个 `xhttp.exe`。

它实现的是 Xray 的 XHTTP `stream-one` 形态：

```text
本地程序 --HTTP/SOCKS5--> xhttp --HTTP/2 POST (双向流)--> Xray 服务端
                   请求体 = 上行（VLESS 头 + 数据）
                   响应体 = 下行（VLESS 响应头 + 数据）
```

服务端只需支持 Xray 的 XHTTP `stream-one`（`network: xhttp` /
`mode: stream-one`），走 HTTP/2 或 h2c。

> 分层说明：`h11` 只用于**本地 HTTP 代理**的 HTTP/1.1 请求解析（浏览器/Windows
> 系统代理只会说 HTTP/1.1）；到服务端的 XHTTP 传输**始终是 HTTP/2**（TLS ALPN
> `h2`，或 `--no-tls` 的 h2c prior knowledge），不会回落到 HTTP/1.1。SOCKS5
> 入站不经过 h11。

> 只实现了 HTTP/2 传输（TLS + ALPN `h2`，或明文 h2c prior knowledge）。
> 不支持 HTTP/3（QUIC）、REALITY、Mux.Cool 和 UDP；这些不在本客户端的范围内。

## 功能

- **本地 HTTP 代理**（默认 `127.0.0.1:1080`）：
  - `CONNECT host:port`：HTTPS、任意 TCP 隧道；
  - `GET http://host/path` 绝对 URI 形式：普通 HTTP 转发。
- **本地 SOCKS5 代理**（**默认关闭**，用 `--socks-listen 127.0.0.1:1081` 开启）：
  - CONNECT，无认证；域名不本地解析，直接交给 VLESS 服务端（SOCKS5H / 远程 DNS）；
  - BIND / UDP ASSOCIATE 返回 command not supported（本客户端不实现 UDP）；
  - 协议处理使用开源库 `asyncio-socks5`，本工程只提供 VLESS 上游适配。
- **上游 HTTP 代理**：支持让本客户端自己通过公司的 HTTP 代理访问 XHTTP 服务端
  （`--upstream-proxy http://host:port`，HTTP CONNECT）。
- **TLS**：系统 CA 校验、`--ca` 自定义 CA、`--pin-sha256` 证书 pin、`--insecure`；
  也支持 `--no-tls` 的明文 h2c。
- **一条 HTTP/2 连接多路复用**：每个本地连接是一条 h2 stream，全双工流式转发，
  不缓冲整个请求/响应。
- **自动重连**：h2 连接断开后，下一次请求自动重新建连；已建立的隧道会收到错误并关闭。
- 配置来源：`config.json` / 命令行 / `vless://` 分享链接。

依赖：`h2`（HTTP/2 帧/HPACK）、`h11`（HTTP/1.1 请求解析/序列化）和
`asyncio-socks5`（SOCKS5 协议/中继），见 `requirements.txt`。VLESS 头和
XHTTP `stream-one` 的流形状没有维护中的 Python 库，这部分保留一层很薄的
适配代码；HTTP/2 帧、HPACK、HTTP/1.1 解析和 SOCKS5 握手/中继都交给库。

## Windows 快速开始

要求 **Python 3.11+**（64 位）。安装时勾选 “Add python.exe to PATH”。

1. 复制配置模板并修改：

   ```bat
   copy config.example.json config.json
   notepad config.json
   ```

   至少填 `server`、`port`、`uuid`、`path`，以及 `sni`（TLS 证书名）。

2. 安装虚拟环境依赖：

   ```bat
   setup.bat
   ```

3. 启动代理：

   ```bat
   run.bat
   ```

   看到 `HTTP proxy listening on ('127.0.0.1', 1080)` 即成功。SOCKS5 默认
   不监听；需要时加 `--socks-listen 127.0.0.1:1081`（或在 config.json 里填
   `"socks_listen": "127.0.0.1:1081"`），启动后会多一行
   `SOCKS5 proxy listening on ...`。

4. 在 Windows「设置 → 网络和 Internet → 代理 → 手动设置代理」里填
   `127.0.0.1:1080`，打开「使用代理服务器」。也可以只给某个程序设置：

   ```bat
   set HTTPS_PROXY=http://127.0.0.1:1080
   set HTTP_PROXY=http://127.0.0.1:1080
   ```

   如果启用了 SOCKS5，支持 SOCKS5 的程序填 `127.0.0.1:1081`；需要远程 DNS
   （SOCKS5H）时不要用本地解析的 `socks5://`，用 `socks5h://` 或程序里的
   “remote DNS” 选项。

5. 验证：

   ```bat
   curl -x http://127.0.0.1:1080 https://example.com/
   curl -x http://127.0.0.1:1080 http://example.com/

   :: 仅在启用了 SOCKS5 时
   curl --socks5-hostname 127.0.0.1:1081 https://example.com/
   ```

没有 `config.json` 时也可以全部用命令行：

```bat
:: UUID / 证书指纹 / 服务器地址替换成自己的
run.bat --server 1.2.3.4:8443 --uuid 00000000-0000-0000-0000-000000000000 ^
        --path /xhttp --sni example.com ^
        --pin-sha256 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff ^
        --listen 127.0.0.1:1080
```

或者直接使用 VLESS 分享链接：

```bat
run.bat --url "vless://00000000-0000-0000-0000-000000000000@1.2.3.4:8443?type=xhttp&mode=stream-one&security=tls&path=%2Fxhttp&sni=example.com"
```

## 通过 pip + Git 安装（Windows）

本仓库就是可安装的 Python 包（根目录有 `pyproject.toml`）：

```bat
py -3 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install "git+https://github.com/fantasy-peak/xhttp.git@main"
```

私有仓库需要先让本机 git 能访问 GitHub（HTTPS 凭据或 SSH）：

```bat
:: HTTPS（Git for Windows 会弹登录/凭据管理器）
pip install "git+https://github.com/fantasy-peak/xhttp.git@main"

:: 或 SSH（需要 ssh-agent / 已配置 key）
pip install "git+ssh://git@github.com/fantasy-peak/xhttp.git@main"
```

安装后入口在虚拟环境里：

```bat
.venv\Scripts\xhttp.exe --version
.venv\Scripts\xhttp.exe --config C:\path\to\config.json
```

注意：

- 代码必须先 **commit 并 push** 到 GitHub，`pip` 从 Git 安装时只读取提交内容。
- 建议把 `@main` 换成 release tag 或 commit hash，升级可复现：
  `.../xhttp.git@<tag>`。
- 升级：`pip install -U --force-reinstall "git+https://github.com/fantasy-peak/xhttp.git@main"`。
- Windows 上需要安装 **Git for Windows**（`pip` 会调用 `git clone`）。
- 配置文件查找顺序是「当前工作目录 → Python/程序所在目录 → `--config`」。
  用 pip 安装时建议显式 `--config`，或在启动目录放 `config.json`。
- **pip 装出来的是虚拟环境里的启动器**（`Scripts\xhttp.exe`），它依赖
  该 venv 和 Python。换一台没有 Python 的机器不能直接拷贝它；要单文件分发，
  仍然用下面的 PyInstaller `build.bat` 或 CI artifact。

## 打包成 Windows exe

在 Windows 上（**PyInstaller 不能从 Linux 交叉编译 Windows exe**）：

```bat
build.bat
```

产物：

```text
dist\xhttp.exe
```

把 `config.json` 放在 exe 同目录（或当前工作目录），双击 exe 或建快捷方式即可。
exe 是 `--onefile --console`：会显示一个控制台窗口，日志和错误都在里面。

首次启动单文件 exe 需要解压到临时目录，通常 1 秒左右；如果介意，可以把
`xhttp.spec` 里的 `onefile` 形态改成目录形态（去掉 `EXE` 里的
`a.binaries, a.datas`，改用 `COLLECT`），或直接分发 `.venv` 里的
`python -m xhttp`。

没有 Windows 机器时，仓库里已经带了一个 GitHub Actions 工作流
`.github/workflows/build-xhttp.yml`：在 GitHub 的 Actions 页面手动运行
「Build Windows exe」，完成后下载 `xhttp-windows` artifact 即可。
它会在 `windows-latest` 上建 venv、跑单元测试、用同一个 spec 打包 exe。

## 配置项

未指定 `--config` 时会依次看当前目录、程序/exe 所在目录的 `config.json`；
只有包含 `"server"` 键的文件才会被当作本程序的配置，避免误读别的工具的同名文件。

`config.json`（键名与命令行一一对应，命令行优先）：

| 键 | 默认 | 说明 |
|---|---|---|
| `server` | 必填 | XHTTP 服务端地址（IP 或域名） |
| `port` | 443 / 80 | 服务端端口；不填时 TLS=443、明文=80 |
| `uuid` | 必填 | VLESS 用户 UUID |
| `path` | `/` | XHTTP 路径，例如 `/xhttp` |
| `sni` | `server` | TLS SNI；用 IP 连服务端时也应填证书名 |
| `host` | 空 | HTTP `Host` / `:authority` 覆盖（Xray 的 `xhttpSettings.host`） |
| `user_agent` | 浏览器 UA | 发往服务端的 User-Agent |
| `tls` | `true` | `false` = 明文 h2c prior knowledge |
| `insecure` | `false` | 跳过证书校验（仅测试） |
| `ca` | 空 | CA 文件路径（PEM） |
| `pin_sha256` | 空 | 叶子证书 SHA-256，可带冒号 |
| `listen_host` | `127.0.0.1` | 本地 HTTP 代理监听地址 |
| `listen_port` | `1080` | 本地 HTTP 代理端口 |
| `socks_listen` | 空（关闭） | 本地 SOCKS5 监听地址，例如 `127.0.0.1:1081`；空字符串关闭 |
| `upstream_proxy` | 空 | `http://host:port`，通过上游 HTTP 代理连服务端 |
| `connect_timeout` | `15` | TCP/TLS 建连超时（秒） |
| `response_timeout` | `15` | 等请求头 / VLESS 响应头超时（秒） |
| `idle_timeout` | `0` | 下行静默多少秒后断开；0=关闭 |
| `verbose` | `false` | DEBUG 日志（h2 帧级日志会很吵） |

常用命令行参数：

```text
--config FILE          指定 JSON 配置
--url VLESS_URL        vless:// 分享链接
--server HOST[:PORT]   服务端
--port PORT
--uuid UUID
--path PATH
--sni SNI
--host HOST            Host / :authority 覆盖
--user-agent UA
--listen [HOST:]PORT   本地 HTTP 监听（也可只写 1080）
--socks-listen [HOST:]PORT  开启本地 SOCKS5 监听（默认关闭）
--no-socks             显式关闭本地 SOCKS5 监听（覆盖配置）
--no-tls               明文 h2c
--insecure             跳过证书校验
--ca FILE              自定义 CA
--pin-sha256 HEX       证书 pin
--upstream-proxy URL   上游 HTTP 代理
--connect-timeout SEC
--response-timeout SEC
--idle-timeout SEC
-v, --verbose
--check                只校验配置并打印摘要
--version
```

证书 pin 的算法是叶子证书 DER 的 SHA-256：

```sh
openssl x509 -in server_cert.pem -noout -fingerprint -sha256
# SHA256 Fingerprint=9C:B3:07:88:...:2A:1E:32
```

`--pin-sha256` 可以带冒号，也可以直接写 64 位十六进制。只给 pin、不给 `--ca`
时，客户端用 pin 替代 CA/主机名校验，适合自签证书；同时给 `--ca` 时会先做
PKI 校验（含主机名）再比对 pin。

## 代理行为

**HTTP 入站（浏览器 → 本程序）**

- 请求行和头由 `h11` 解析；绝对 URI 转发时再用 `h11` 序列化成 origin-form。
- `CONNECT host:port`：先建立 VLESS stream，成功后才回
  `HTTP/1.1 200 Connection Established`，然后双向透明转发。
- 绝对 URI（`GET http://host/path`）：重写为 origin-form，删除 hop-by-hop 头
  （`Proxy-Connection`、`Connection`、`Keep-Alive`、`Upgrade`、`TE` 等），
  强制 `Connection: close`，完成一个请求后关闭本地连接。
- 暂不代理 `ws://`（明文 WebSocket 的 `Upgrade` 会被当作 hop-by-hop 删除）；
  `wss://` 走 CONNECT，正常。
- HTTP/1.1 请求必须带 `Host`（`h11` 按 RFC 校验）；HTTP/1.0 可以不带，转发时会补。
- 本地代理**没有认证**，默认只监听 `127.0.0.1`，不要改成 `0.0.0.0` 暴露到局域网。

**SOCKS5 入站（SOCKS5 客户端 → 本程序，默认关闭）**

- 启动时加 `--socks-listen 127.0.0.1:1081` 才会监听。
- 无认证，只实现 CONNECT；`BIND` 和 `UDP ASSOCIATE` 返回
  `REP_COMMAND_NOT_SUPPORTED (0x07)`。
- 域名以 SOCKS5 的 `ATYP=DOMAIN` 原样进入 VLESS 头，**不在本机解析**。
  因此 `curl --socks5-hostname`、浏览器的 “remote DNS / SOCKS5H” 都可用；
  `curl --socks5`（本地解析）也能用，但解析发生在本机。
- 协议与中继由 `asyncio-socks5` 提供；本工程只实现 `ConnectSession.connect_remote`
  和 `Socket` 适配层。握手阶段使用 `readexactly` 包装，客户端分段发送 greeting
  也不会失败。
- 没有认证，仍然只监听 `127.0.0.1`。

**出站（本程序 → 上游 HTTP 代理 → XHTTP 服务端）**

```bash
python -m xhttp --upstream-proxy http://10.0.0.1:3128 ...
```

先对 `Host: server:port` 发 `CONNECT`，收到 200 后再做 TLS（如果启用了 TLS）。
只支持 `http://` 上游代理，不支持上游代理的 `Proxy-Authorization`
（需要的话可以在 `h2client.py` 的 `_open_via_proxy` 里加）。

## 服务端配合

服务端 `streamSettings` 使用：

```json
"network": "xhttp",
"security": "tls",
"xhttpSettings": {"path": "/xhttp", "mode": "stream-one"}
```

TLS 的 ALPN 需要包含 `h2`（不要只配 `h3`，否则本客户端会报
`server selected ALPN 'h3'; XHTTP stream-one needs 'h2'`）。

客户端：

```bash
python -m xhttp --server 1.2.3.4:8443 --uuid <UUID> \
  --path /xhttp --sni example.com --ca /path/to/ca.pem --listen 127.0.0.1:1080
```

## 开发与测试

```bash
./setup.sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m xhttp --check --server ... --uuid ...
./build.sh          # Linux 二进制，仅用于验证 spec
```

已用本地 XHTTP 服务端（HTTP/2 与 h2c）验证：

- 绝对 URI 与 `CONNECT` 两种 HTTP 代理形态；
- SOCKS5 CONNECT（IPv4 / 域名、分片握手、BIND/UDP 拒绝）；
- 10 MB 本地 HTTP 下载、3 MB `Content-Length` / chunked 上传；
- 16 路并发；
- 二级代理链（`--upstream-proxy` 指向上一个本程序实例）；
- PyInstaller 单文件产物运行；
- 单元测试 46 项。

已知限制：

- 只走 HTTP/2；HTTP/3 需要另外接 QUIC 栈（例如 `aioquic`）。
- 一条共享 h2 连接上，如果某个本地客户端读得极慢，队列满后会对其他流产生
  队头阻塞（有界内存换来的背压）。普通浏览器/下载场景不受影响。
- 每个明文 HTTP 请求强制 `Connection: close`，不做代理侧 keep-alive 复用。
- SOCKS5 只实现 CONNECT；BIND / UDP ASSOCIATE 返回不支持，所以没有 UDP。

## 故障排查

| 现象 | 排查 |
|---|---|
| `XHTTP endpoint returned HTTP 404` | `path` 不对、UUID 不对，或服务端该路径没开 h2/xhttp；XHTTP stream-one 只接受 HTTP/2/3，不接受 HTTP/1.1 |
| `server selected ALPN ... needs 'h2'` | 服务端 TLS 只开了 `h3`，改为同时开 `h2`，或客户端换端口 |
| `certificate pin mismatch` | 重新取叶子证书指纹；确认没有连到反代/其他证书 |
| `CERTIFICATE_VERIFY_FAILED` | 自签证书用 `--pin-sha256` 或 `--ca`；测试可 `--insecure` |
| `CONNECT ... 502` | VLESS 会话建立失败（UUID/path/服务端不可达），不是目标站问题 |
| 浏览器完全不走上代理 | Windows 代理设置里的“请勿对以下列项使用代理服务器”是否包含目标；或程序自己读环境变量 |
| HTTP 端口 1080 被占用 | `--listen 127.0.0.1:1082` 换端口，并同步改系统代理 |
| SOCKS5 端口 1081 被占用 | `--socks-listen 127.0.0.1:1082` 换端口，或 `--no-socks` 关闭 |
| 大文件卡住 | 先看日志有无 `HTTP/2 connection lost`；`-v` 会打开 h2 帧日志（非常吵）。本地链路可先用 `curl --proxytunnel` 测 |

## 安全说明

- 本地代理无鉴权；默认 bind `127.0.0.1` 是安全边界，不要随意改。
- `--insecure` 只用于测试；生产用 `--ca` 或 `--pin-sha256`。
- XHTTP 本身不加密内容，TLS 由外层提供；明文 h2c 只适合本机/内网调试。
- 本程序不检查/修改被代理的 TLS，属于透明字节转发。

## 目录

```text
.
├── run.py            # PyInstaller / 直接启动入口
├── xhttp/
│   ├── __main__.py           # 命令行、配置合并、事件循环
│   ├── config.py             # Config / JSON / vless:// 解析
│   ├── vless.py              # VLESS 请求头/响应头
│   ├── h2client.py           # HTTP/2 + VLESS stream-one 客户端
│   ├── relay.py              # HTTP CONNECT 的双向字节中继
│   ├── proxy.py              # 本地 HTTP 代理（CONNECT / 绝对 URI）
│   └── socks5.py             # 本地 SOCKS5（asyncio-socks5 + VLESS 适配）
├── tests/                    # unittest
├── .github/workflows/        # Windows exe 构建
├── config.example.json
├── requirements.txt
├── requirements-build.txt
├── xhttp.spec                # PyInstaller
├── version_info.txt          # exe 版本信息
├── setup.bat / run.bat / build.bat
└── setup.sh / build.sh       # Linux 开发/验证用
```
