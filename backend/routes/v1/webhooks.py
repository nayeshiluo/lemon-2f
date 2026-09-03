"""
qBittorrent 下载完成 Webhook 回调。

目的：把状态机从"纯轮询"升级为"事件驱动 + 轮询兜底"。

原实现是单进程 `while True: sleep(15)` 扫全表，一条投稿从下载完成到被
发现平均要等半个轮询周期。qB 支持「Torrent 完成时运行外部程序」，
让它 curl 本端点即可做到秒级推进。

安全设计：
- 必须携带共享密钥；QB_WEBHOOK_TOKEN 未配置时端点一律拒绝（Fail-Closed），
  绝不允许无鉴权的公网端点触发内部流水线；
- 密钥比对使用 secrets.compare_digest 防时序侧信道；
- 端点只负责"投递唤醒信号"，绝不在请求线程里跑重活 ——
  避免 qB 的回调超时把流水线拖垮，也杜绝被刷接口打满 CPU。
"""
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status

from backend.config import settings
from backend.redis_client import redis_manager

logger = logging.getLogger("lemon_2f.webhook")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def _verify_token(provided: Optional[str]) -> None:
    """校验共享密钥，Fail-Closed"""
    expected = (settings.QB_WEBHOOK_TOKEN or "").strip()
    if not expected:
        logger.warning("qB webhook called but QB_WEBHOOK_TOKEN is not configured; rejecting")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook 未启用：服务端未配置 QB_WEBHOOK_TOKEN"
        )
    if not provided or not secrets.compare_digest(provided.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook 鉴权失败"
        )


@router.post("/qb/complete")
async def qb_download_complete(
    torrent_hash: Optional[str] = Query(default=None, description="qB 传入的 info_hash (%I)"),
    x_lemon_webhook_token: Optional[str] = Header(default=None, alias="X-Lemon-Webhook-Token"),
    token: Optional[str] = Query(default=None, description="备用：以查询参数传递密钥"),
):
    """
    qB 下载完成回调。在 qBittorrent 中配置：
      设置 → 下载 → Torrent 完成时运行外部程序：
      curl -s -X POST "http://lemon-2f:8000/api/webhooks/qb/complete?torrent_hash=%I" \
           -H "X-Lemon-Webhook-Token: <QB_WEBHOOK_TOKEN>"

    本端点不做任何重活，只投递一个唤醒信号让 Worker 立即推进状态机。
    即使信号投递失败（Redis 不可用），轮询兜底依然会在下一个周期发现该任务，
    因此这里返回 200 而不是 5xx —— 避免 qB 侧反复重试刷日志。
    """
    _verify_token(x_lemon_webhook_token or token)

    reason = f"qb_complete:{(torrent_hash or 'unknown')[:40]}"
    signaled = await redis_manager.signal_wake(reason)

    if signaled:
        logger.info(f"qB webhook accepted, pipeline wake signaled ({reason})")
    else:
        logger.warning(f"qB webhook accepted but wake signal failed ({reason}); polling will cover it")

    return {
        "accepted": True,
        "wake_signaled": signaled,
        "torrent_hash": torrent_hash,
        "note": "已受理；若唤醒信号投递失败，轮询兜底仍会在下一周期推进该任务"
    }
