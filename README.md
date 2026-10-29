# Mihomo Deploy Kit

[![验证部署套件](https://github.com/lwystian/mihomo-deploy-kit/actions/workflows/test.yml/badge.svg)](https://github.com/lwystian/mihomo-deploy-kit/actions/workflows/test.yml)

轻量的 Mihomo + MetaCubeXD Docker Compose 部署工具。一个官方一体化容器，提供中文交互安装、带账号密码的 HTTP/SOCKS5 代理、订阅导入和基本维护功能。

项目只负责部署。**不包含 Caddy/Nginx，不询问域名，不申请证书，不修改现有反向代理或防火墙。** 本项目不是 MetaCubeX 官方项目。

## 在 `/opt` 安装

适用 Linux amd64/arm64，建议 Ubuntu 24.04。需要 Docker Engine、Docker Compose 插件、Python 3.8+ 和 PyYAML。安装器检查依赖，不自动安装或升级 Docker。

缺少 Git 或 Python 依赖时，先执行：

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-yaml
```

以 root 身份克隆项目，进入项目目录后执行 `./deploy.sh`：

```bash
git clone https://github.com/lwystian/mihomo-deploy-kit.git /opt/metacubexd
cd /opt/metacubexd
./deploy.sh
```

请先阅读脚本再运行。无需下载 ZIP，无需在聊天中提供服务器密码、代理密码或订阅链接。

默认直接部署在脚本所在的项目目录，不要求目录必须叫 `metacubexd`。例如克隆到 `/opt/mihomo-deploy-kit` 后，进入该目录执行 `./deploy.sh` 即可。非 root 用户使用 `sudo ./deploy.sh`。

需要将运行配置及数据安装到另一个独立空目录时，可执行 `./deploy.sh --root /opt/其他独立目录`。

## 交互内容

普通安装询问：允许外部设备使用代理与否、代理端口、用户名、密码、是否导入订阅。密码隐藏输入并确认，留空生成随机强密码。订阅链接也隐藏输入；导入时询问名称（默认“主订阅”）及是否激活。

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
sudo bash /opt/metacubexd/manage.sh help
sudo bash /opt/metacubexd/manage.sh status
sudo bash /opt/metacubexd/manage.sh doctor
sudo bash /opt/metacubexd/manage.sh logs -f
sudo bash /opt/metacubexd/manage.sh restart
sudo bash /opt/metacubexd/manage.sh import-subscription
sudo bash /opt/metacubexd/manage.sh refresh-subscription
sudo bash /opt/metacubexd/manage.sh switch-profile
sudo bash /opt/metacubexd/manage.sh change-proxy-auth
sudo bash /opt/metacubexd/manage.sh show-secret proxy
sudo bash /opt/metacubexd/manage.sh show-secret api
sudo bash /opt/metacubexd/manage.sh backup
sudo bash /opt/metacubexd/manage.sh restore
sudo bash /opt/metacubexd/manage.sh update
sudo bash /opt/metacubexd/manage.sh uninstall
```

`start`、`stop`、`restart` 只操作本项目容器。`doctor` 验证实际数据挂载、端口、内核、管理认证和代理认证；不把容器 `Up` 当成出站可用的证明。出站网站、公网安全组和自有 HTTPS 入口需另外验证。

`update` 默认重新检查固定版本镜像，不自动跳到 `latest`。可显式使用 `update --image ghcr.io/metacubex/metacubexd-server:v版本号` 更新到经审查的新版本。更新前完整备份，失败时恢复旧配置和数据。内核版本跟随上游镜像，不额外下载或替换二进制。

更新本仓库的脚本可执行 `git -C /opt/metacubexd pull --ff-only`；这只更新代码，不自动重启容器。若存在本地修改应先审查差异，勿强制覆盖。

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

- `/opt/metacubexd/data` 绑定到容器 `/data`：订阅内容、覆盖层、运行配置、缓存及 GEO 文件。
- `/opt/metacubexd/settings.json`：代理凭据、管理密钥、订阅 URL 与配置 ID。
- `/opt/metacubexd/.env`：镜像、端口、管理密钥。不要提交 Git。
- `/opt/metacubexd-backups`：冷备份 `.tar.gz` 与 `.sha256`；更换安装目录时使用其同级 `目录名-backups`。

代理密码按 Mihomo 要求以可用凭据保存，并非密码哈希。目录权限 `700`，敏感文件和备份权限 `600`；容器使用 `umask 077`。备份含完整敏感信息，不加密，不可上传公共仓库。

备份会短暂停止本项目容器，完成后恢复原运行状态。恢复先验证清单和每个文件的 SHA256，拒绝路径穿越、重复路径、链接及特殊文件；确认后再备份当前状态。恢复前文件及失败候选均隔离保留，不直接删除数据。

`uninstall` 备份后仅移除本项目容器，保留配置、数据、备份及网络；不提供一键清空数据功能。不会删除 Docker volumes，不调用全局清理，不操作其他 Compose 项目。

本套件管理的部署会直接复用。检测到其他来源的 `metacubexd-server`、同名 Compose 项目或已有 `.env`/数据目录时，停止新装并提示人工审查，不自动迁移或覆盖。迁移前应单独备份、比较配置并确认。

`logs` 会隐藏管理密钥、代理密码和 URL；Docker 原始日志、管理员自行运行的命令和面板显示内容不受该过滤器保护。不要分享未经检查的原始日志。

## 版本与验证

当前默认官方镜像为 `ghcr.io/metacubex/metacubexd-server:v1.273.1`，其 Dockerfile 默认内置 Mihomo `v1.19.27`。面板版本与内核版本是两回事；本套件不会把它描述成最新内核。

本地测试和 GitHub Actions 分为纯逻辑测试、Compose 检查及实际容器集成测试。执行结果与尚未验证的项目见 [TEST_REPORT.md](TEST_REPORT.md)。

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
- [Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/)
