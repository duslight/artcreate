# artcreate 1.0 · 服务器部署（Ubuntu 22.04 / 阿里云 ECS 示例）

单进程架构：`python -m artcreate serve` 启动 FastAPI（评审视图 + 工作台 API）
+ 内嵌 worker 线程（串行生成队列）+ 空闲蒸馏。**一个 systemd 服务即全部**，
无需额外的队列/worker/数据库进程。

**访问方式：公网 IP 直连（无域名、无备案、无 nginx）**——
浏览器直接开 `http://<公网IP>:8870/workbench.html`。

## 1. 前置条件

- Ubuntu 20.04+，2C4G 起步（生成是调云端 API，本机只做后处理，负载很低）
- 安全组/防火墙：放行 22、**8870**（Web 入口）

## 2. 拉代码 + 环境

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo mkdir -p /opt/artcreate && sudo chown $USER /opt/artcreate
cd /opt/artcreate
git clone https://github.com/duslight/artcreate.git .
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. 密钥与配置（.env 迁移清单）

```bash
cp .env.example .env && chmod 600 .env && vim .env
```

必填项：

| 变量 | 用途 |
|---|---|
| `ZHIPU_API_KEY` | LLM 编译层 + VLM 意图回查（glm-4-flash / glm-4v-flash） |
| `ARK_API_KEY` | 主力生成 provider（火山方舟 Seedream） |
| `LIBLIB_ACCESS_KEY` / `LIBLIB_SECRET_KEY` | 备选 provider（不用可留空） |
| `WORKBENCH_TOKEN` | **工作台访问令牌**（另行配置，见第 4 步；公网部署必填） |

从本地开发机迁移：把本地 `.env` 内容原样拷到服务器 `.env`（不要经 git）。

## 4. 访问令牌（公网必配）

`WORKBENCH_TOKEN` 未配置时全部 API 本地放行（互信模型）；绑定 0.0.0.0 后
**必须配置**，否则任何知道 IP 的人都能跑图烧你的 API 配额。

- systemd：`Environment=WORKBENCH_TOKEN=xxx`（本仓库 deploy/artcreate.service 已留位）
- 客户端：首次打开工作台，在页面顶部「令牌」栏填入（存 localStorage，每次请求走 `X-Token` 头）

## 5. systemd 常驻

```bash
sudo cp deploy/artcreate.service /etc/systemd/system/
# 先改里面的路径与 WORKBENCH_TOKEN
sudo vim /etc/systemd/system/artcreate.service
sudo systemctl daemon-reload
sudo systemctl enable --now artcreate
systemctl status artcreate          # 应为 active (running)
journalctl -u artcreate -f          # 看日志（uvicorn 启动 + worker 心跳）
```

## 6. 直接访问（无 nginx）

服务绑定 0.0.0.0（systemd unit 已配置 `--host 0.0.0.0`）：

```bash
# 浏览器直接打开（任何设备）：
http://<公网IP>:8870/            # 评审视图
http://<公网IP>:8870/workbench.html   # 工作台
```

> 曾经的 nginx 反代 + HTTPS 方案已移除（域名路线废弃）。若未来需要 HTTPS，
> 再考虑加反代或 caddy；当前令牌鉴权（X-Token）已覆盖基本访问控制。

## 7. 验收

```bash
curl http://127.0.0.1:8870/api/events?limit=1 -H "X-Token: xxx"   # 应返回 JSON
# 浏览器：http://<公网IP>:8870/workbench.html
#   1) 令牌栏填 WORKBENCH_TOKEN
#   2) 提交一个小批量任务 → 轮询到 done → 跳评审视图
#   3) 提案页应显示历史蒸馏提案
```

## 8. 数据与备份

| 数据 | 位置 | 说明 |
|---|---|---|
| 资产库 + 事件 + 提案 | `assets.db`（SQLite） | 全部业务数据；worker 清理缩略也在这套库 |
| 定稿图 | `exports/<subject>/<run>/` | accepted 全保 |
| 候选原图 | 同上，rejected 超 N 天被 worker 删 PNG 留缩略 | 见 jobs.cleanup_candidates |
| 经验晋升产物 | `derived/*.yaml` | approve 时自动 git commit，天然有版本史 |
| `.env` | 项目根 | 密钥，永不入库 |

备份建议（cron 每日）：

```bash
0 3 * * * tar czf /backup/artcreate-$(date +\%F).tar.gz \
  -C /opt/artcreate assets.db exports derived project.yaml
```

## 9. 升级 / 回滚

```bash
cd /opt/artcreate
git pull origin main
sudo systemctl restart artcreate
# 回滚：git checkout v1.0.0 && sudo systemctl restart artcreate
```

## 10. 常见问题

- **worker 不跑任务**：`journalctl -u artcreate | grep worker`；jobs 表 `status=error` 的 `error` 字段有堆栈
- **生成 429**：方舟限流，串行队列会自动续跑；急件走 CLI `python -m artcreate run`
- **端口被占**：`serve --port 其他端口`，同步改 nginx 上游
- **磁盘涨得快**：候选原图保留期在 `jobs.cleanup_candidates`（默认 14 天）后自动清理，先查 `du -sh exports/`
