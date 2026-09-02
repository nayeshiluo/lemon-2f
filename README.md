# 🍋 二楼有请 (Lemon 2F)

> **基于 Emby 媒体生态的众包影视入库、智能查重、FFprobe 质检与软妹币管理系统**
>
> *Crowdsourced Media Ingestion, Anti-Fraud Deduplication, 2F Coin Points Economy & Auto-Mount System for Emby.*

<p align="center">
  <img src="assets/logo.jpg" alt="二楼有请" width="360" style="border-radius: 16px; box-shadow: 0 8px 32px rgba(255, 42, 133, 0.3);">
</p>

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688.svg?logo=fastapi&logoColor=white)](backend/)
[![SQLAlchemy](https://img.shields.io/badge/ORM-SQLAlchemy%202.0%20Async-D71F00.svg)](https://www.sqlalchemy.org/)
[![Vue3](https://img.shields.io/badge/Frontend-Vue3%20%2B%20TailwindCSS-4FC08D.svg?logo=vuedotjs&logoColor=white)](frontend/)
[![Emby](https://img.shields.io/badge/Integration-Emby%20Server-52B043.svg)](https://emby.media)

---

## 🌟 核心特性 (Features)

1. **🔐 Emby 原生无缝穿透鉴权**：
   * 用户可直接使用 Emby 原生账号密码登录 Web 面板或 Telegram Bot，自动识别管理员与普通用户并同步建档。
2. **🪙 防通胀的「软妹币 (🪙)」全量原子总账**：
   * 采用 SQLAlchemy 2.0 异步关系型数据库与事务级原子结算，杜绝并发双花刷分；
   * 产出：每日签到（连签加成）、新片提交、剧集补全、4K 原画洗版；
   * 消耗：求片悬赏押金、二楼商城兑换 Emby VIP 观看时长、专属高速专线特权、赛博专属徽章。
3. **🔍 TMDB 权威刮削与 Emby 穿透智能查重**：
   * 提交时自动穿透 TMDB API 锁定权威 `tmdb_id`，并与 Emby 进行 `ProviderIds.Tmdb` 级精准对账；
   * 智能区分 **全库缺失 (全新奖励)**、**部分缺集 (缺集补片奖励)**、**全库已存在 (拦截防刷)**。
4. **🛡️ 工业级全自动入库流水线与防骗风控**：
   * **qBittorrent 异步下载引擎**：自动添加、分类打标（`lemon_2f`）、15分钟零速度死种超时自动熔断；
   * **FFprobe 深度指纹质检**：拦截小于30秒短视频及虚假假文件骗分，真实提取视频轨编码、4K分辨率、码率与时长；
   * **规范化归档入库**：按 TMDB 官方标准 `Show (Year)/Season XX/SxxExx - Title.mkv` 规范硬链接/复制到影视库目标目录，入库成功后自动触发 Emby 媒体库刷新。
5. **🤖 原生 Telegram 运维 Bot**：
   * 支持 `/start`, `/find <片名>`, `/upload <磁力>`, `/sign`, `/points`, `/shop` 等原生交互与可折叠富文本卡片。
6. **💻 赛博霓虹大屏 Web 面板**：
   * 基于 Vue 3 + TailwindCSS 赛博暗黑粉紫风格，全量对接真实后端 REST API。

---

## 🏛️ 系统架构 (Architecture)

```text
┌────────────────────────────────────────────────────────────┐
│                    Web 面板 / Telegram Bot                 │
└─────────────────────────────┬──────────────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │  FastAPI 核心网关  │
                    └────┬─────────┬────┘
                         │         │
       ┌─────────────────▼──┐   ┌──▼──────────────────┐
       │ TMDB 权威刮削与对账 │   │ Emby 穿透查重与鉴权 │
       └────────────────────┘   └─────────────────────┘
                         │
          ┌──────────────▼──────────────┐
          │   qB 离线 / FFprobe 质检     │
          └──────────────┬──────────────┘
                         │
          ┌──────────────▼──────────────┐
          │  自动挂载入库 + 软妹币原子总账│
          └─────────────────────────────┘
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
# 编辑 .env 填入你的 Emby URL、API Key 与 TMDB API Key
nano .env
```

### 3. Docker Compose 一键启动
```bash
docker compose up -d
```

启动完成后：
* 🌐 **Web 仪表盘**：`http://localhost:8888`
* 🔌 **后端 API 文档 (Swagger)**：`http://localhost:8000/docs`

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 开源发布。  
Designed with ❤️ for Emby & Media Server Enthusiasts.
