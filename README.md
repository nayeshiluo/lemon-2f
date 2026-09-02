# 🍋 LemonEmos (柠萌云影)

> **基于 Emby / Foam 媒体生态的众包积分求片、智能查重与全自动入库管理系统**
>
> *Crowdsourced Media Ingestion, Anti-Fraud Deduplication, Points Economy & Auto-Mount System for Emby & Foam.*

[![GitHub license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?logo=docker&logoColor=white)](docker-compose.yml)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688.svg?logo=fastapi&logoColor=white)](backend/)
[![Vue3](https://img.shields.io/badge/Frontend-Vue3%20%2B%20TailwindCSS-4FC08D.svg?logo=vuedotjs&logoColor=white)](frontend/)
[![Emby](https://img.shields.io/badge/Integration-Emby%20%2F%20Foam-52B043.svg)](https://emby.media)

---

## 🌟 核心特性 (Features)

1. **🔐 Emby / Foam 双轨无缝原生鉴权**：
   * 用户可直接使用 Emby 原生账号密码登录 Web 面板或绑定 Telegram Bot，自动提取 Telegram ID、用户名并建档。
2. **🎭 三级权限矩阵 (Role-Based Access Control)**：
   * **👑 最高权限 (Owner)**：总账调控、分值规则引擎、系统全盘配置、节点监控；
   * **🛡️ 管理员 (Admin)**：异常任务审核、队列管理、Emby 库一键全量扫描；
   * **👤 众包用户 (User)**：全库查重、提交磁力/分片直传视频、积分明细、商城权益兑换。
3. **🔍 TMDB 穿透式智能查重比对 (防刷分核心)**：
   * 提交时自动穿透 TMDB API 锁定权威 `tmdb_id`，并与 Emby 进行 `ProviderIds.Tmdb` 级精准对账；
   * 智能区分 **全库缺失 (全新奖励)**、**部分缺失 (缺集补片奖励)**、**全库已存在 (拦截防刷)**。
4. **🛡️ 六重铜墙铁壁安全风控体系**：
   * **分布式并发锁**：防多线程抢单“双花”刷分；
   * **FFprobe 深度指纹质检**：防 5 秒假视频/空壳文件骗分，校验真实时长、码率与编码；
   * **后置原子结算**：未成功挂载入库绝不发分；
   * **磁盘水位熔断**：可用空间低于 15% 自动保护；
   * **死种超时熔断**：15分钟无速度自动清理释放；
   * **令牌桶频控与防刷惩罚**。
5. **🥕 防通胀的积分经济闭环 (Carrot Points Economy)**：
   * 积分产出：新片提交、剧集补全、4K 原画洗版、每日签到；
   * 积分消耗：兑换 Emby VIP 观看天数、专属专线高速节点特权、悬赏求片、积分抽奖。
6. **🤖 Telegram 原生 Inline 交互 Bot**：
   * 支持 `/find`, `/upload`, `/shop`, `/admin`, `/sign` 等原生可折叠富文本卡片指令。

---

## 🚀 极速部署指南 (Quick Start)

### 1. 克隆仓库
```bash
git clone https://github.com/nayeshiluo/lemon-emos.git
cd lemon-emos
```

### 2. 配置环境变量
```bash
cp .env.example .env
# 编辑 .env 填入你的 Emby URL, API Key 与 TMDB API Key
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
       │ TMDB 权威刮削与对账 │   │ Emby / Foam 穿透校验 │
       └────────────────────┘   └─────────────────────┘
                         │
          ┌──────────────▼──────────────┐
          │   qB 离线 / FFprobe 质检     │
          └──────────────┬──────────────┘
                         │
          ┌──────────────▼──────────────┐
          │  自动挂载入库 + 积分原子结算  │
          └─────────────────────────────┘
```

---

## 📄 开源许可证 (License)

本项目基于 [MIT License](LICENSE) 开源发布。
Designed with ❤️ for Emby & Media Server Enthusiasts.
