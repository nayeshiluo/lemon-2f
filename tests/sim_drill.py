"""
全链路生产模拟演练 (End-to-End Simulation Drill)

模拟真实用户与管理员的完整生命周期操作，暴露隐藏缺陷：
1. 用户登录 -> 查缺补漏大厅 -> 多源投稿 (磁力/挂载/网盘/直传)
2. 流水线全状态机推进 (pending -> inspecting -> delivering -> waiting_emby -> accepted)
3. 积分结算 -> 排行榜 -> 商城消费
4. 错片自删 3 倍惩罚 -> 管理员删片 -> 积分自由调控
5. 边界攻击：目录穿越、并发双删、越权删除、重复投稿
"""
import os
import io
import json
import tempfile
import asyncio
import httpx
from unittest.mock import patch, AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool
from sqlalchemy import select, func

from backend.config import settings
settings.APP_ENV = "testing"

from backend.database import Base, get_db
from backend.main import app
from backend.models.user import User
from backend.models.task import MediaTask, TaskItem
from backend.models.submission import Submission, SubmissionItem
from backend.models.shop import ShopItem
from backend.security import create_access_token
from backend.services.pipeline_service import SubmissionPipelineService
from backend.services.submission_service import SubmissionService
from backend.delivery.adapter import LocalDeliveryAdapter
from backend.qc.inspector import ffprobe_qc
from backend.clients.emby import emby_client
from backend.clients.tmdb import tmdb_client

FINDINGS = []
PASSES = []

def ok(msg):
    PASSES.append(msg)
    print(f"  ✅ {msg}")

def bug(msg):
    FINDINGS.append(msg)
    print(f"  🐞 缺陷: {msg}")

MOCK_META = {
    "duration_seconds": 2400.0, "width": 1920, "height": 1080,
    "video_codec": "h264", "audio_codec": "aac", "bitrate_kbps": 5000,
    "is_4k": False, "file_size": 2048, "raw_json": "{}"
}

MOCK_TMDB_TV = {
    "title": "模拟连续剧", "original_title": "Sim Series", "year": 2026,
    "poster_url": "https://img/p.jpg", "overview": "模拟简介",
    "number_of_episodes": 6,
    "seasons": [{"season_number": 1, "episode_count": 6}]
}
MOCK_TMDB_MOVIE = {
    "title": "模拟电影", "original_title": "Sim Movie", "year": 2026,
    "poster_url": "https://img/m.jpg", "overview": "模拟电影简介",
    "number_of_episodes": 1, "seasons": []
}


async def main():
    tmpdir = tempfile.mkdtemp(prefix="sim_drill_")
    movies_dir = os.path.join(tmpdir, "movies")
    tv_dir = os.path.join(tmpdir, "tv")
    dl_dir = os.path.join(tmpdir, "downloads")
    for d in (movies_dir, tv_dir, dl_dir):
        os.makedirs(d, exist_ok=True)

    settings.QB_CONTAINER_DOWNLOAD_PATH = dl_dir
    settings.MEDIA_MOVIES_CONTAINER_PATH = movies_dir
    settings.MEDIA_TV_CONTAINER_PATH = tv_dir

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False,
                                connect_args={"check_same_thread": False, "uri": True},
                                poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SF = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with SF() as s:
        alice = User(username="alice", role="user", balance=300)
        bob = User(username="bob", role="user", balance=300)
        admin = User(username="boss", role="owner", balance=9999)
        s.add_all([alice, bob, admin])
        await s.flush()
        s.add(ShopItem(title="Emby VIP 30天", description="会员", category="emby_vip",
                       cost_points=100, stock=10, fulfillment_type="manual", is_active=True))
        await s.commit()
        alice_id, bob_id, admin_id = alice.id, bob.id, admin.id

    async def override_get_db():
        async with SF() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    tr = httpx.ASGITransport(app=app)
    H_A = {"Authorization": f"Bearer {create_access_token(subject=alice_id, role='user')}"}
    H_B = {"Authorization": f"Bearer {create_access_token(subject=bob_id, role='user')}"}
    H_ADMIN = {"Authorization": f"Bearer {create_access_token(subject=admin_id, role='owner')}"}

    async def tmdb_details(tmdb_id, mtype):
        return MOCK_TMDB_MOVIE if mtype == "movie" else MOCK_TMDB_TV

    async def verify_presence(tmdb_id, media_type, season=None, episode=None, expected_dest_path=None):
        # 真实语义：投稿前查重时 Emby 尚无此片 -> False；
        # 交付落盘后带 expected_dest_path 做最终对账确认 -> True。
        return expected_dest_path is not None

    patchers = [
        patch.object(tmdb_client, "get_details", new=AsyncMock(side_effect=tmdb_details)),
        patch.object(emby_client, "find_by_tmdb_id", new=AsyncMock(return_value=None)),
        patch.object(emby_client, "get_series_episodes", new=AsyncMock(return_value=[])),
        patch.object(emby_client, "refresh_library", new=AsyncMock(return_value=True)),
        patch.object(emby_client, "verify_item_presence", new=AsyncMock(side_effect=verify_presence)),
        patch.object(ffprobe_qc, "inspect", new=AsyncMock(return_value=(True, "OK", MOCK_META))),
    ]
    for p in patchers:
        p.start()

    try:
        async with httpx.AsyncClient(transport=tr, base_url="http://sim") as c:
            print("\n=== 场景 1: 公共看板与鉴权 ===")
            r = await c.get("/api/tasks/public-stats")
            ok(f"公开统计可用: users={r.json()['total_users']}") if r.status_code == 200 else bug(f"public-stats {r.status_code}")

            r = await c.get("/api/submissions/my")
            bug("未登录竟能访问 /submissions/my") if r.status_code == 200 else ok(f"未登录访问被拒 ({r.status_code})")

            print("\n=== 场景 2: Alice 磁力投稿剧集 S01E01 ===")
            payload = {
                "tmdb_id": 7001, "media_type": "tv", "title": "模拟连续剧", "year": 2026,
                "season": 1, "episode": 1, "source_type": "magnet",
                "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40,
            }
            r = await c.post("/api/submissions/", headers=H_A, json=payload)
            if r.status_code != 200:
                bug(f"磁力投稿失败 {r.status_code}: {r.text[:300]}")
                sub1 = None
            else:
                sub1 = r.json()
                ok(f"磁力投稿受理 #{sub1['id']} status={sub1['status']} 预估={sub1['estimated_reward_points']}")

            print("\n=== 场景 3: 重复投稿同一单集防重 ===")
            payload2 = dict(payload)
            payload2["magnet_uri"] = "magnet:?xt=urn:btih:" + "b" * 40
            r = await c.post("/api/submissions/", headers=H_B, json=payload2)
            ok(f"同单集重复抢单被拦截 ({r.status_code})") if r.status_code == 409 else bug(f"同单集重复投稿未被拦截! {r.status_code} {r.text[:200]}")

            print("\n=== 场景 4: 目录穿越攻击 (local_mount) ===")
            atk = dict(payload)
            atk.pop("magnet_uri")
            atk.update({"source_type": "local_mount", "resource_url": "/etc/passwd", "episode": 2})
            r = await c.post("/api/submissions/", headers=H_A, json=atk)
            ok(f"目录穿越被安全拦截 ({r.status_code})") if r.status_code >= 400 else bug(f"严重: /etc/passwd 竟被受理! {r.status_code}")

            print("\n=== 场景 5: 本地挂载乱名文件 -> 强制 TMDB 重命名 ===")
            messy = os.path.join(dl_dir, "【压制组】@#$随机乱码_HDR_raw.mkv")
            with open(messy, "wb") as f:
                f.write(b"x" * (6 * 1024 * 1024))
            lm = dict(payload)
            lm.pop("magnet_uri")
            lm.update({"source_type": "local_mount", "resource_url": messy, "episode": 3})
            r = await c.post("/api/submissions/", headers=H_A, json=lm)
            if r.status_code != 200:
                bug(f"本地挂载投稿失败 {r.status_code}: {r.text[:300]}")
                sub_lm = None
            else:
                sub_lm = r.json()
                ok(f"本地挂载受理 #{sub_lm['id']} status={sub_lm['status']}")

            print("\n=== 场景 6: 网盘分享投稿 (夸克) ===")
            pan = dict(payload)
            pan.pop("magnet_uri")
            pan.update({"source_type": "pan_share", "resource_url": "https://pan.quark.cn/s/simxyz",
                        "share_code": "8888", "episode": 4})
            r = await c.post("/api/submissions/", headers=H_A, json=pan)
            if r.status_code == 200:
                sub_pan = r.json()
                ok(f"网盘投稿受理 #{sub_pan['id']} pan_type={sub_pan.get('pan_type')} status={sub_pan['status']}")
            else:
                bug(f"网盘投稿失败 {r.status_code}: {r.text[:200]}")
                sub_pan = None

            print("\n=== 场景 7: 视频文件直传 ===")
            files = {"file": ("垃圾名字_raw.mp4", io.BytesIO(b"y" * (6 * 1024 * 1024)), "video/mp4")}
            form = {"tmdb_id": "7001", "media_type": "tv", "season": "1", "episode": "5",
                    "title": "模拟连续剧", "year": "2026"}
            r = await c.post("/api/submissions/upload-file", headers=H_A, data=form, files=files)
            if r.status_code == 200:
                sub_up = r.json()
                ok(f"视频直传受理 #{sub_up['id']} status={sub_up['status']}")
            else:
                bug(f"视频直传失败 {r.status_code}: {r.text[:300]}")
                sub_up = None

            print("\n=== 场景 8: 查缺补漏大厅 ===")
            r = await c.get("/api/tasks/missing-board", headers=H_A)
            if r.status_code == 200:
                d = r.json()
                if d["total"] > 0:
                    it = d["items"][0]
                    ok(f"查缺大厅: {it['title']} 缺{it['missing_episodes_count']}集 [{it['missing_ranges_formatted']}] {it['completion_percent']}%")
                else:
                    bug("查缺大厅为空 (应显示模拟连续剧的缺集)")
            else:
                bug(f"查缺大厅异常 {r.status_code}: {r.text[:200]}")

    finally:
        pass

    # ===== 流水线推进 (直接走 service 层，模拟 Worker) =====
    print("\n=== 场景 9: 流水线推进 local_mount 投稿到 accepted 发币 ===")
    async with SF() as s:
        pipe = SubmissionPipelineService(s)
        pipe.delivery_adapter = LocalDeliveryAdapter(movies_root=movies_dir, tv_root=tv_dir, delivery_mode="copy")
        target = (await s.execute(
            select(Submission).where(Submission.source_type == "local_mount")
        )).scalars().first()
        if not target:
            bug("找不到 local_mount 投稿，跳过流水线推进")
        else:
            for _ in range(6):
                cur = (await s.execute(select(Submission).where(Submission.id == target.id))).scalar_one()
                st = cur.status
                if st in ("accepted", "partial", "failed", "rejected", "deleted"):
                    break
                if st in ("pending", "reserved"):
                    await pipe._handle_pending(cur)
                elif st == "downloading":
                    await pipe._handle_downloading(cur)
                elif st == "inspecting":
                    await pipe._handle_inspecting(cur)
                elif st == "delivering":
                    await pipe._handle_delivering(cur)
                elif st == "waiting_emby":
                    await pipe._handle_waiting_emby(cur)
                await s.commit()
            final = (await s.execute(select(Submission).where(Submission.id == target.id))).scalar_one()
            if final.status == "accepted":
                ok(f"流水线全通: #{final.id} accepted 实发={final.reward_points}🪙")
            else:
                bug(f"流水线未走通: #{final.id} status={final.status} err={final.error_message}")

            item = (await s.execute(select(SubmissionItem).where(SubmissionItem.submission_id == final.id))).scalars().first()
            if item and item.dest_file:
                expect = os.path.join("模拟连续剧 (2026) [tmdbid=7001]", "Season 01", "模拟连续剧 - S01E03.mkv")
                if str(item.dest_file).endswith(expect) and os.path.exists(item.dest_file):
                    ok(f"乱名强制 TMDB 规范落盘: .../{expect}")
                else:
                    bug(f"落盘命名不符规范: {item.dest_file}")
            else:
                bug("未生成 SubmissionItem / dest_file")

            u = await s.get(User, alice_id)
            ok(f"Alice 结算后余额: {u.balance}🪙")

    # ===== 排行榜 / 商城 / 删除惩罚 / 管理操作 =====
    async with httpx.AsyncClient(transport=tr, base_url="http://sim") as c:
        print("\n=== 场景 10: 排行榜 ===")
        for cat in ("uploads", "earned", "balance"):
            r = await c.get(f"/api/points/leaderboard?category={cat}&timespan=all", headers=H_A)
            if r.status_code == 200:
                items = r.json()["items"]
                top = items[0]["username"] + " " + items[0]["score_label"] if items else "空"
                ok(f"榜单[{cat}] 条目={len(items)} 榜首={top}")
            else:
                bug(f"榜单[{cat}] 异常 {r.status_code}")

        print("\n=== 场景 11: 商城兑换 ===")
        r = await c.get("/api/shop/items", headers=H_A)
        if r.status_code == 200 and r.json():
            iid = r.json()[0]["id"] if isinstance(r.json(), list) else r.json().get("items", [{}])[0].get("id")
            r2 = await c.post("/api/shop/exchange", headers=H_A, json={"item_id": iid})
            ok(f"商城兑换: {r2.status_code} {str(r2.json())[:120]}")
        else:
            bug(f"商城列表异常 {r.status_code}: {r.text[:150]}")

        print("\n=== 场景 12: 越权删除他人投稿 ===")
        async with SF() as s:
            acc = (await s.execute(select(Submission).where(Submission.status == "accepted"))).scalars().first()
            acc_id = acc.id if acc else None
            acc_reward = acc.reward_points if acc else 0
        if acc_id:
            r = await c.post(f"/api/submissions/{acc_id}/delete", headers=H_B)
            ok(f"越权删除被拒 ({r.status_code})") if r.status_code >= 400 else bug(f"严重: Bob 删掉了 Alice 的投稿! {r.status_code}")

            print("\n=== 场景 13: 并发双删 (独立会话真实并发) ===")
            async with SF() as s:
                before = (await s.get(User, alice_id)).balance
            # 双客户端、双独立会话并发提交，模拟真实生产多请求
            H_A2 = {"Authorization": f"Bearer {create_access_token(subject=alice_id, role='user')}"}
            async with httpx.AsyncClient(transport=tr, base_url="http://sim") as c2:
                r1, r2 = await asyncio.gather(
                    c.post(f"/api/submissions/{acc_id}/delete", headers=H_A2),
                    c2.post(f"/api/submissions/{acc_id}/delete", headers=H_A2),
                    return_exceptions=True,
                )
            codes = [getattr(x, "status_code", str(x)) for x in (r1, r2)]
            async with SF() as s:
                after = (await s.get(User, alice_id)).balance
            delta = before - after
            expect_once = acc_reward * 3
            if delta == expect_once:
                ok(f"并发双删仅扣一次: {delta}🪙 (=3×{acc_reward}) codes={codes}")
            elif delta == 0:
                bug(f"并发双删未扣分 codes={codes}")
            else:
                bug(f"并发双删扣分异常: 扣了{delta}🪙 期望{expect_once} codes={codes}")

        print("\n=== 场景 14: 管理员积分自由调控 ===")
        r = await c.post(f"/api/admin/adjust-points?target_user_id={bob_id}&amount=-500&reason=模拟违规处罚", headers=H_ADMIN)
        if r.status_code == 200:
            ok(f"管理员扣分穿透负债: {r.json()['new_balance']}🪙")
        else:
            bug(f"管理员调分失败 {r.status_code}: {r.text[:200]}")

        print("\n=== 场景 15: 管理员删片三模式 + 动态控分 ===")
        r = await c.get("/api/admin/users", headers=H_ADMIN)
        ok(f"管理员用户列表: {r.json()['total']} 人") if r.status_code == 200 else bug(f"用户列表异常 {r.status_code}")

        r = await c.get("/api/admin/points-config", headers=H_ADMIN)
        ok(f"控分规则读取: 电影={r.json().get('MOVIE_UPLOAD_REWARD')} 剧集={r.json().get('EPISODE_UPLOAD_REWARD')}") if r.status_code == 200 else bug(f"控分读取异常 {r.status_code}")

        r = await c.post("/api/admin/points-config", headers=H_ADMIN,
                         json={"EPISODE_UPLOAD_REWARD": 45, "SUBMISSION_DELETE_PENALTY_MULTIPLIER": 5})
        if r.status_code == 200 and r.json()["rules"]["EPISODE_UPLOAD_REWARD"] == 45:
            ok("动态控分热更新生效 (剧集=45, 惩罚倍数=5)")
        else:
            bug(f"动态控分失败 {r.status_code}: {r.text[:200]}")

        # 验证新规则立刻作用于新投稿预估
        np = {"tmdb_id": 7002, "media_type": "tv", "title": "模拟连续剧", "year": 2026,
              "season": 1, "episode": 1, "source_type": "magnet",
              "magnet_uri": "magnet:?xt=urn:btih:" + "c" * 40}
        r = await c.post("/api/submissions/", headers=H_B, json=np)
        if r.status_code == 200:
            est = r.json()["estimated_reward_points"]
            ok(f"新规则热生效于新投稿预估: {est}🪙") if est == 45 else bug(f"新规则未生效: 预估={est} 期望45")
            new_sub_id = r.json()["id"]
        else:
            bug(f"新规则后投稿失败 {r.status_code}: {r.text[:200]}")
            new_sub_id = None

        if new_sub_id:
            r = await c.post(f"/api/admin/submissions/{new_sub_id}/delete", headers=H_ADMIN,
                             json={"action": "custom", "custom_amount": 77, "reason": "模拟自定义扣分"})
            if r.status_code == 200:
                ok(f"管理员自定义扣分删片: 扣={r.json()['points_deducted']}🪙")
            else:
                bug(f"管理员删片失败 {r.status_code}: {r.text[:200]}")

        print("\n=== 场景 16: 全站脱敏检查 ===")
        r = await c.get("/api/submissions/all", headers=H_B)
        if r.status_code == 200:
            body = r.text
            leaks = [k for k in ("magnet_uri", "torrent_hash", "dest_file", "resource_url", "share_code") if f'"{k}"' in body]
            ok("全站流无敏感字段泄漏") if not leaks else bug(f"全站公共流泄漏敏感字段: {leaks}")
        else:
            bug(f"全站流异常 {r.status_code}")

    app.dependency_overrides.clear()
    for p in patchers:
        p.stop()
    await engine.dispose()

    print("\n" + "=" * 60)
    print(f"通过项: {len(PASSES)}    发现缺陷: {len(FINDINGS)}")
    if FINDINGS:
        print("\n需修复清单:")
        for i, f in enumerate(FINDINGS, 1):
            print(f"  {i}. {f}")
    print("=" * 60)


asyncio.run(main())
