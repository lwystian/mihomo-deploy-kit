# Mihomo Deploy Kit

[![验证部署套件](https://github.com/lwystian/mihomo-deploy-kit/actions/workflows/test.yml/badge.svg)](https://github.com/lwystian/mihomo-deploy-kit/actions/workflows/test.yml)

轻量的 Mihomo + MetaCubeXD Docker Compose 部署工具。一个官方一体化容器，提供中文交互安装、带账号密码的 HTTP/SOCKS5 代理、订阅导入和基本维护功能。

项目只负责部署。**不包含 Caddy/Nginx，不询问域名，不申请证书，不修改现有反向代理或防火墙。** 本项目不是 MetaCubeX 官方项目。

## 快速开始

面向满足依赖的 Linux amd64/arm64 系统，不限定发行版，也不限定安装路径。需要 Git、Bash、Docker Engine、Docker Compose 插件、Python 3.8+ 和 PyYAML（Python 导入名为 `yaml`）。请用当前发行版的包管理器准备依赖；安装器只检查，不自动安装或升级 Docker。

在你选定的父目录下克隆项目：

```bash
git clone https://github.com/lwystian/mihomo-deploy-kit.git
cd mihomo-deploy-kit
./deploy.sh
```

部署和容器管理使用 root 或 `sudo ./deploy.sh`。可以先检查依赖：

```bash
docker version
docker compose version
python3 -c 'import yaml'
```

请先阅读脚本再运行。无需下载 ZIP，无需在聊天中提供服务器密码、代理密码或订阅链接。

默认直接部署在脚本所在的项目目录，不要求目录使用固定名称或位于 `/opt`。选择独立的项目目录即可；不要直接把系统目录或用户主目录本身当成部署目录。

需要将运行配置及数据安装到另一个独立空目录时，可执行 `./deploy.sh --root /你选择的路径/项目目录`。之后进入该安装目录使用同一个 `./deploy.sh` 管理。

## 交互内容

所有有默认值的安装问题都可直接回车。密码和订阅链接隐藏输入，不在普通日志显示。

| 问题 | 回车后的行为 |
| --- | --- |
| 允许外部设备使用代理？ | 是 |
| 代理端口 | `9092` |
| 代理用户名 | `proxy` |
| 代理密码 | 留空生成随机密码；自设密码只输入一次 |
| 修改高级设置？ | 否，使用当前目录及默认管理端口 |
| 现在导入订阅？ | 否，稍后可单独导入 |
| 订阅 URL | 留空跳过导入 |
| 配置名称 | `主订阅` |
| 导入后立即激活？ | 是 |

自设密码**不检查复杂度，不强制 16 位，单字符、短密码、中文和特殊符号都可用**。SOCKS5 用户名和密码各自最长为 255 个 UTF-8 字节，密码须非空且不能含换行或 NUL；用户名还不能含冒号，因为 Mihomo 配置用它分隔用户名和密码。这些是协议及输入格式边界，不是密码强度要求。密码只输入一次；格式不符合协议边界时再重新输入。

修改已有账号密码时，用户名回车沿用原值，密码回车保留原密码；全部未改动则不重启。恢复路径或切换配置留空可取消。显示密钥、恢复和卸载命令直接执行，不再输入 SHOW 或再次确认；自动备份、校验和失败回退继续执行。

高级设置可修改安装目录、面板端口、API 端口。管理密钥自动随机生成；重复运行安装器时复用已有配置，不重新生成密钥。

| 默认宿主机地址 | 容器实际端口 | 用途 |
| --- | --- | --- |
| `127.0.0.1:9090` | `9090/tcp` | MetaCubeXD 页面和 `/api/control` 配置管理 |
| `127.0.0.1:9091` | `9091/tcp` | Mihomo API、WebSocket |
| 公网模式 `0.0.0.0:9092`，本机模式 `127.0.0.1:9092` | `9092/tcp` | HTTP、HTTPS CONNECT、SOCKS5 代理 |

不发布宿主机 80/443，不发布 UDP，不使用 host 网络，不启用 TUN，也不需要额外特权。公网模式需要用户自行放行选定 TCP 端口；Docker 发布端口的访问控制不能只依赖一般的 UFW 入站规则。

初始化期间先仅绑定本机。验证 API 认证、HTTP 错误凭据拒绝及 SOCKS5 正确/错误凭据后，才应用公网绑定。任何启动验证失败会停止本项目容器并保留数据。

**初始配置拒绝所有出站请求。** 未导入订阅时，面板及认证可以正常验证，但代理尚不能访问目标网站。导入并激活有效订阅后按其规则运行；如果只需要服务器自身作为出口，可在面板新建本地配置，例如：

```yaml
proxies: []
proxy-groups: []
rules:
  - MATCH,DIRECT
```

安装器创建的“本地访问设置”全局覆盖层会保留代理认证。不要停用或删除它。导入的订阅决定流量规则和出口，并非所有请求都会经过订阅节点。

## 客户端连接

客户端分别填写服务器公网 IP、代理端口、用户名和密码即可。支持 URI 的客户端使用：

```text
http://用户名:密码@服务器IP:9092
socks5://用户名:密码@服务器IP:9092
```

URI 中的用户名、密码须按客户端要求进行百分号编码；分别填写字段更方便。不要把带凭据的完整 URI 留在 shell 历史、进程参数或共享日志中。

**普通 HTTP/SOCKS5 账号密码认证不加密客户端到代理服务器的连接。** HTTPS CONNECT 可保护目标网站的 TLS 内容，但不会自动加密代理认证过程。此精简版不提供代理入站 TLS、加密隧道或 SOCKS5 UDP 转发。

服务器本机也使用同一组账号密码，通过 `127.0.0.1:9092` 连接。本项目不设置免认证地址。其他 Docker 容器可由用户自行接入本项目网络，通过 Compose 服务名 `server:9092` 使用代理；推荐配置唯一网络别名，避免多个项目都叫 `server` 时歧义。容器之间可以访问该网络内的监听端口，务必只接入可信容器。

## 测试代理

先导入并激活配置，再在面板选择代理节点或合适规则。以下命令中的 `proxy` 换成实际用户名，curl 会提示输入代理密码：

```bash
# 服务器直接访问，作为出口对照
curl --noproxy '*' -sS --max-time 20 -w '\n' https://api.ipify.org

# 服务器本机通过 HTTP 代理访问
curl --noproxy '' -sS -x http://127.0.0.1:9092 -U proxy --max-time 20 -w '\n' https://api.ipify.org

# 通过 SOCKS5 访问，并由代理端解析目标域名
curl --noproxy '' -sS -x socks5h://127.0.0.1:9092 -U proxy --max-time 20 -w '\n' https://api.ipify.org
```

返回 IP 表示该次请求已完成。面板“连接”中的命中规则和连接链可进一步确认经过的节点。走 `DIRECT` 时出口 IP 可能与服务器相同；IP 相同本身不代表代理失效。

测试公网入口时，在另一台设备将代理地址中的 `127.0.0.1` 换成服务器公网 IP，端口按实际配置填写。本机成功并不证明公网入口可达。HTTP `407` 表示代理认证未通过；拒绝连接时检查服务和端口，连接超时时结合日志判断防火墙、节点或目标服务问题。内置 `doctor` 侧重配置与认证检查，不等同于实际出站成功。[curl 参数说明](https://curl.se/docs/manpage.html)。

## 访问面板

两种方式均由用户选择和配置：

- SSH 隧道同时转发面板和 API，然后浏览器访问 `http://127.0.0.1:9090`，面板后端填写 `http://127.0.0.1:9091` 及 API Secret。
- 在自己的反向代理中转发面板、配置管理接口，以及 Mihomo API 和 WebSocket。可以采用同域不同路径，不需要向本安装器提供域名。

例如从本地电脑建立 SSH 隧道（替换用户和 IP；如改过端口也需调整）：

```bash
ssh -N -L 9090:127.0.0.1:9090 -L 9091:127.0.0.1:9091 用户@服务器IP
```

面板端口不内置独立网页登录认证。**上游会把 `CONTROL_TOKEN` 注入浏览器，因此它不能保护面板本身。** 公布面板前，应在自有反向代理中保护整个面板站点，包括 `/config.js` 和 `/api/control`。API 使用 `CLASH_SECRET`，WebSocket 需要正确 Upgrade 转发；不要记录查询参数中的 token。

容器内的 `127.0.0.1` 指向该容器自身。如果用户的反向代理也运行在容器中，需自行配置合适的 Docker 网络及服务别名，不能把该容器的 `127.0.0.1:9090` 当成本项目地址。安装器不会自动把核心加入现有 `edge-proxy` 网络。

初始 CORS 只允许本地面板的两个明确 Origin。使用跨域 API 时，请在“本地访问设置”覆盖层中显式配置自己的面板 Origin，不要设置通配符。CORS 不是身份认证。

## 管理命令

```bash
./deploy.sh                          # 首次部署，已有部署则识别复用
./deploy.sh help
./deploy.sh status
./deploy.sh doctor
./deploy.sh logs -f
./deploy.sh start
./deploy.sh stop
./deploy.sh restart
./deploy.sh update
./deploy.sh backup
./deploy.sh restore
./deploy.sh import-subscription
./deploy.sh refresh-subscription
./deploy.sh switch-profile
./deploy.sh change-proxy-auth
./deploy.sh show-secret proxy
./deploy.sh show-secret api
./deploy.sh uninstall
```

`start`、`stop`、`restart` 只操作本项目容器。`doctor` 验证实际数据挂载、端口、内核、管理认证和代理认证；不把容器 `Up` 当成出站可用的证明。出站网站、公网安全组和自有 HTTPS 入口需另外验证。

**之后更新只需执行一条命令，无需先手动 `git pull`：**

```bash
./deploy.sh update
```

它先检查当前 Git 分支及本地修改，拉取配置的远程跟踪分支，备份旧代码并仅允许快进更新，然后自动执行新版脚本，完整备份运行配置及数据、检查镜像并验证服务。密码、端口、订阅及持久化数据继续沿用。尚未完成首次部署时，该命令只更新脚本，之后运行 `./deploy.sh` 安装。

本地代码有改动、分支分叉、文件冲突或网络失败时会停止，不执行强制覆盖、自动 stash 或清理。请审查本地修改后重试。代码来自当前配置的 Git upstream，因此应只使用你信任的仓库和分支。旧代码保留在 Git 提交历史，不额外生成代码压缩包。运行配置和数据只保留项目 `backups/` 内最近一份完整校验备份。

`update` 保留已配置的明确版本镜像，不自动跳到 `latest`。可显式使用 `update --image ghcr.io/metacubex/metacubexd-server:v版本号` 同时更新代码并指定镜像版本。镜像启动验证失败时恢复旧运行配置和数据；代码保持此次拉取的版本。内核版本跟随上游镜像，不额外下载或替换二进制。备份及更新会短暂中断本项目服务。

需要跳过代码更新时，可执行 `./deploy.sh update --image-only`。使用 `--root` 复制到其他目录的安装不含 `.git`，只能在该目录使用此镜像更新模式；想使用完整的一键更新，应直接在 Git 克隆的目录部署。

**已经安装旧版脚本的用户，需要先在项目目录执行一次 `git pull --ff-only`，取得本次新增的自动更新入口。** 此后直接使用 `./deploy.sh update` 即可。

## 订阅实现及限制

只接收 Mihomo/Clash YAML 配置订阅，不提供第三方订阅转换服务。原生节点列表、`ss://`/`vmess://` 列表或 Base64 节点集合不能直接当成配置导入。

已核实官方 `v1.273.1` 接口：

| 接口 | 方法与格式 | 返回/用途 |
| --- | --- | --- |
| `/api/control/profiles/import` | POST `{url, name}` | 原生远程订阅元数据，包含 ID 和 URL |
| `/api/control/profiles` | POST `{name, type, content}` | 创建本地或覆盖层配置，返回配置元数据 |
| `/api/control/profiles/{id}/validate` | POST | `{valid, message}`，校验源文件 |
| `/api/control/profiles/{id}/editor` | GET | `composedYaml`、配置及合成信息 |
| `/api/control/profiles/{id}/activate` | POST | 校验合成配置、启动内核，返回内核状态 |
| `/api/control/kernel/status` | GET | 内核状态，包含敏感字段，工具只显示必要状态 |
| `/api/control/config/runtime` | GET | 实际运行 YAML，不能公开展示 |

上述控制接口使用 `Authorization: Bearer CONTROL_TOKEN`。Mihomo 的 `/version` 使用另一个 `CLASH_SECRET`。上游健康接口只检查 Agent，本套件额外检查真实内核及代理认证。

上游原生远程导入在超时错误中可能包含完整 URL，因此套件在宿主机隐藏下载，再使用官方本地配置接口创建、校验、可选激活。订阅链接仅长期保存在权限 `600` 的 `settings.json` 中，供 `refresh-subscription` 使用，不放进进程参数。面板里显示的是本地配置；请用本套件命令更新对应订阅，而非面板的远程订阅自动刷新。

每次更新创建新候选配置，保留此前配置。失败不删除已有配置。激活前检查全局覆盖后的认证设置，激活后检查实际内核、认证及 active 标记；失败恢复此前配置，恢复仍失败则停止本项目。

不要添加未审查的脚本覆盖层。套件拒绝在存在已启用脚本配置时自动激活订阅。管理员可通过面板修改配置，因此套件不构成防止管理员关闭认证的安全边界；修改后运行 `doctor`。

## 数据、备份与恢复

- 项目目录的 `data/` 绑定到容器 `/data`：订阅内容、覆盖层、运行配置、缓存及 GEO 文件。
- 项目目录的 `settings.json`：代理凭据、管理密钥、订阅 URL 与配置 ID。
- 项目目录的 `.env`：镜像、端口、管理密钥。不要提交 Git。
- 项目目录内的 `backups/`：只保留最近一份冷备份 `.tar.gz` 及配套 `.sha256`，不依赖固定安装路径，也不把备份递归打进备份。

代理密码按 Mihomo 要求以可用凭据保存，并非密码哈希。目录权限 `700`，敏感文件和备份权限 `600`；容器使用 `umask 077`。备份含完整敏感信息，不加密，不可上传公共仓库。

新备份写完并校验通过后，才清理旧备份；失败保留上一份完整备份。首次新版备份成功时，也会清理同级旧目录 `项目目录名-backups/` 中可识别的本项目备份，空目录自动移除。未识别、损坏、其他项目的文件和符号链接不删除。

备份会短暂停止本项目容器，完成后恢复原运行状态。恢复先验证清单和每个文件的 SHA256，拒绝路径穿越、重复路径、链接及特殊文件；验证通过后自动备份当前状态。恢复来源先完整读入临时目录，再备份当前状态及轮换旧备份，因此即使恢复来源就是唯一的那份备份也可使用。恢复前文件及失败候选仍隔离保留，不属于自动轮换的归档备份。

`uninstall` 备份后仅移除本项目容器，保留配置、数据、备份及网络；不提供一键清空数据功能。不会删除 Docker volumes，不调用全局清理，不操作其他 Compose 项目。

本套件管理的部署会直接复用。检测到其他来源的 `metacubexd-server`、同名 Compose 项目或已有 `.env`/数据目录时，停止新装并提示人工审查，不自动迁移或覆盖。迁移前应单独备份、比较配置并确认。

`logs` 会隐藏管理密钥、代理密码和 URL；Docker 原始日志、管理员自行运行的命令和面板显示内容不受该过滤器保护。不要分享未经检查的原始日志。

## 版本与验证

当前默认官方镜像为 `ghcr.io/metacubex/metacubexd-server:v1.273.1`，其 Dockerfile 默认内置 Mihomo `v1.19.27`。面板版本与内核版本是两回事；本套件不会把它描述成最新内核。

本地测试和 GitHub Actions 分为纯逻辑测试、Compose 检查及实际容器集成测试。Linux 自动化测试目前运行于 Ubuntu runner，这代表已验证的环境，不是安装器对发行版的限制。其他发行版及 arm64 的实际部署需在相应环境验证。执行结果与尚未验证的项目见 [TEST_REPORT.md](TEST_REPORT.md)。

```bash
python3 tests/test-kit.py
docker compose --env-file .env.example config --quiet
python3 tests/integration.py --run
```

最后一个命令会拉取镜像并创建**独立临时测试项目**，只在有可用 Docker 的测试环境执行，不要把它当成生产服务器只读诊断。测试结束仅清理该临时项目，不删除生产数据。

## 官方资料

- [MetaCubeXD Server、环境变量及安全边界](https://github.com/MetaCubeX/metacubexd/tree/v1.273.1)
- [官方 Dockerfile 与内置内核版本](https://github.com/MetaCubeX/metacubexd/blob/v1.273.1/apps/server/Dockerfile)
- [控制接口源码](https://github.com/MetaCubeX/metacubexd/blob/v1.273.1/packages/agent/src/http.ts)
- [订阅及覆盖层源码](https://github.com/MetaCubeX/metacubexd/blob/v1.273.1/packages/agent/src/profiles.ts)
- [Mihomo 代理端口](https://wiki.metacubex.one/config/inbound/port/)
- [Mihomo 认证与外部访问](https://wiki.metacubex.one/config/general/)
- [SOCKS5 用户名密码协议长度边界](https://www.rfc-editor.org/rfc/rfc1929)
- [Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/)
