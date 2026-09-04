# 🍋 二楼有请 (Lemon 2F)

> **基于 Emby 媒体生态的众包影视入库、查缺补漏、TMDB 强制规范化、FFprobe 质检与软妹币经济系统**
>
> *Crowdsourced Media Ingestion, Missing-Episode Board, Forced TMDB Normalization, QC Pipeline & 2F Coin Economy for Emby.*

<p align="center">
  <img src="assets/logo.jpg" alt="二楼有请" width="360" style="border-radius: 16px; box-shadow: 0 8px 32px rgba(255, 42, 133, 0.3);">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688.svg?logo=fastapi&logoColor=white)](backend/)
[![SQLAlchemy](https://img.shields.io/badge/ORM-SQLAlchemy%202.0%20Async-D71F00.svg)](https://www.sqlalchemy.org/)
[![Alembic](https://img.shields.io/badge/Migration-Alembic-7B1FA2.svg)](migrations/)
[![Vue3](https://img.shields.io/badge/Frontend-Vue3%20%2B%20TailwindCSS-4FC08D.svg?logo=vuedotjs&logoColor=white)](frontend/)
[![Emby](https://img.shields.io/badge/Integration-Emby%20Server-52B043.svg)](https://emby.media)
[![Tests](https://img.shields.io/badge/Tests-150%20passed-brightgreen.svg)](tests/)

---

## 🌟 核心特性 (Features)

### 1. 🔐 Emby 原生无缝穿透鉴权
用户直接使用 Emby 原生账号密码登录 Web 面板或 Telegram Bot，自动识别管理员与普通用户并同步建档；支持 Telegram 账号绑定码互认（`/api/auth/tg-bind/*`）。

### 2. 🔍 TMDB 权威识别与 Emby 穿透查重
* 提交时穿透 TMDB API 锁定权威 `tmdb_id`，与 Emby `ProviderIds.Tmdb` 精准对账；
* 智能区分 **全库缺失（全新奖励）**、**部分缺集（补片奖励）**、**已存在（拦截防刷）**；
* 管理员可一键 **全库剧集扫描同步建档**（`POST /api/admin/sync-emby-series`），把 Emby 现有剧集全部纳入 TMDB 台账。

### 3. 📋 查缺补漏大厅 (Missing Board)
* `GET /api/tasks/missing-board` 实时计算每部剧的缺集区间（如 `S01E03-E06、S01E09`）、完成度百分比与缺集总数；
* 用户在面板点开即可带参锁定目标单集投稿，彻底告别"不知道缺哪集"。

### 4. 🔌 四轨多源资源投稿接口
同一个投稿入口支持四种资源提供方式，全部走同一条质检与规范化流水线：

| 接口模式 | `source_type` | 说明 |
| :--- | :--- | :--- |
| 🧲 磁力链接 | `magnet` | 推入 qBittorrent 异步离线拉取 |
| 📁 qB / 本地挂载路径 | `local_mount` | 服务器磁盘已存在的文件直接引入，**跳过下载秒级质检**（含白名单目录穿越防护） |
| ☁️ 网盘分享链接 | `pan_share` | 自动识别 **光鸭云盘 / 移动云盘 139 / 夸克网盘**，持久化提取码 |
| 📤 视频文件直传 | `direct_upload` | `POST /api/submissions/upload-file` 分块流式接收本地视频 |

### 5. 🏷️ 强制 TMDB 规范化重命名（无条件洗名）
**无论原始文件名多乱**（压制组前缀、乱码、`111.mp4`、无集数标识的裸切片），只要锁定了 TMDB 目标与季集，质检放行后一律强制重命名并建标准目录：

```text
电影：{片名} ({年份}) [tmdbid={id}]/{片名} ({年份}).mkv
剧集：{剧名} ({年份}) [tmdbid={id}]/Season 01/{剧名} - S01E03.mkv
```

### 6. 🛡️ 工业级入库流水线与防骗风控
* **事件驱动状态机**：`pending → downloading → inspecting → delivering → waiting_emby → accepted`，Redis 唤醒 + 轮询兜底，Redis 不可用自动降级不停摆；
* **qBittorrent 引擎**：自动打标 `lemon_2f`、零速度死种超时熔断；
* **FFprobe 深度指纹质检**：拦截短视频/假文件骗分，提取编码、分辨率、码率、时长，同集多文件自动择优；
* **广告杂质粉碎**：锁定正片、过滤推广短视频与 txt/url/html 杂质。

### 7. 🪙 软妹币经济与贡献排行榜
* 全量原子总账（幂等键 + 行级锁 + SAVEPOINT 冲突兜底），杜绝并发双花刷分；
* 产出：每日签到（连签加成）、新片提交、剧集补全、4K 原画洗版；
* 消耗：商城兑换 Emby VIP 时长、高速专线特权、赛博徽章；
* **三维度贡献排行榜**（`GET /api/points/leaderboard`）：上传数量 / 累计赚币 / 当前余额 × 周榜·月榜·总榜。

### 8. ⚖️ 错片下架与惩罚闭环
* **用户自删**：扣除实发积分的 N 倍（默认 **3 倍**，允许穿透为负债），杜绝"赚币兑VIP→自删坏种→零成本白嫖"；
* **管理员删片三模式**：不扣分 / 倍数扣分 / 自定义扣分，附审计备注；
* 删除时同步执行 **qB 种子清理 + 物理文件下架 + Emby 刷新 + 缺集状态回滚**（可重新补片）；
* **并发幂等**：原子 CAS 状态闸门（`UPDATE ... WHERE status != 'deleted'`），双击或并发重试只扣一次。

### 9. 🎛️ 管理方全权控盘
* **动态积分规则热生效**（`GET/POST /api/admin/points-config`）：电影奖励、单集奖励、4K 加成、签到区间、连签加成、删除惩罚倍数，改完立刻作用于新投稿，**无需重启**；
* **积分无限制自由调控**（`POST /api/admin/adjust-points`）：任意加减、允许负债、全程审计留痕；
* 全量用户资产列表、全站投稿台账、磁盘容量看板。

### 10. 🤖 原生 Telegram Bot & 💻 赛博霓虹大屏
* Bot 支持 `/start`、`/find <片名>`、`/upload <磁力>`、`/sign`、`/points`、`/shop` 与可折叠富文本卡片；
* Web 面板基于 Vue 3 + TailwindCSS 赛博暗黑粉紫风，全量对接真实 REST API。

---

## 🏛️ 系统架构 (Architecture)

```text
┌──────────────────────────────────────────────────────────────┐
│              Web 赛博面板  /  Telegram 运维 Bot               │
└──────────────────────────────┬───────────────────────────────┘
                               │
                     ┌─────────▼─────────┐
                     │  FastAPI 核心网关  │
                     └────┬─────────┬────┘
                          │         │
        ┌─────────────────▼──┐   ┌──▼──────────────────┐
        │ TMDB 权威刮削与对账 │   │ Emby 穿透查重与鉴权 │
        └─────────────────┬──┘   └──┬──────────────────┘
                          │         │
              ┌───────────▼─────────▼───────────┐
              │   四轨多源接入 (磁力/挂载/网盘/直传)│
              └───────────────┬─────────────────┘
                              │
              ┌───────────────▼─────────────────┐
              │  qB 离线  →  FFprobe 深度质检    │
              └───────────────┬─────────────────┘
                              │
              ┌───────────────▼─────────────────┐
              │ 强制 TMDB 规范重命名 + 挂载入库   │
              └───────────────┬─────────────────┘
                              │
              ┌───────────────▼─────────────────┐
              │ 软妹币原子总账 + 排行榜 + 惩罚闭环│
              └─────────────────────────────────┘
```

---

## 🚀 极速部署指南 (Quick Start)

### 1. 克隆仓库
```bash
git clone https://github.com/nayeshiluo/lemon-2f.git
cd lemon-2f
```

### 2. 配置环境变量
```bash
cp .env.example .env
# 编辑 .env 填入 Emby URL / API Key、TMDB API Key、qBittorrent 端点与挂载路径
nano .env
```

### 3. Docker Compose 一键启动
```bash
docker compose up -d
```

### 4. 数据库增量迁移（升级已有部署必做）
```bash
alembic upgrade head
```

启动完成后：
* 🌐 **Web 仪表盘**：`http://localhost:8888`
* 🔌 **后端 API 文档 (Swagger)**：`http://localhost:8000/docs`

---

## 🧪 测试与质量保障 (Testing)

```bash
# 全量单元 / 集成 / 路由契约测试（150 项）
PYTHONPATH=. pytest tests

# 端到端生产模拟演练（16 场景 / 25 项断言：多源投稿、流水线全通、
# 并发双删、越权拦截、目录穿越、动态控分、全站脱敏）
PYTHONPATH=. APP_ENV=testing python3 tests/sim_drill.py
```

覆盖范围：状态机全路径、质检 fail-closed、幂等与并发竞态、TMDB 客户端错误分类、HTTP 路由契约、Telegram 绑定、多源投稿与强制重命名、运行时韧性（Redis/Emby 不可用降级）。

---

## 🗺️ 后续路线图 (Roadmap)

* **P0**：PostgreSQL + Redis 真生产部署验证、API 限流与并发预占配额、Emby Webhook 实时联动
* **P1**：AList 统一网盘真实转存引擎（打通夸克/移动/光鸭离线闭环）、PT 一键发种联动
* **P2**：Overseerr 同款分季集数矩阵色块盘、观众报错工单与抓虫赏金

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 开源发布。
Designed with ❤️ for Emby & Media Server Enthusiasts.
