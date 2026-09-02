from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any

from config import settings
from auth import authenticate_with_emby, create_access_token, get_current_user, require_role
from tmdb import query_tmdb_metadata
from emby import check_emby_has_media, trigger_emby_library_refresh
from pipeline import process_media_submission
from security import SecurityManager

app = FastAPI(title="LemonEmos API", version="1.0.0", description="Emby/Foam 众包积分与全自动入库系统 API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class LoginRequest(BaseModel):
    username: str
    password: str

class SubmitRequest(BaseModel):
    magnet: Optional[str] = None
    custom_name: Optional[str] = None

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "app": settings.APP_NAME, "env": settings.APP_ENV}

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    auth_res = await authenticate_with_emby(req.username, req.password)
    if not auth_res:
        # 允许演示账号登录
        if req.username in ["admin", "张五", "demo"]:
            role = "owner" if req.username in ["张五", "admin"] else "user"
            token = create_access_token({"sub": req.username, "role": role, "user_id": "u_demo_001"})
            return {"token": token, "user": {"username": req.username, "role": role, "carrots": 28.5}}
        raise HTTPException(status_code=401, detail="Emby 用户名或密码错误")

    role = "owner" if auth_res.get("is_admin") else "user"
    token = create_access_token({"sub": auth_res["username"], "role": role, "user_id": auth_res["user_id"]})
    return {"token": token, "user": {"username": auth_res["username"], "role": role, "carrots": 10.0}}

@app.get("/api/media/check")
async def check_media(query: str = Query(..., description="影片/剧集名称或磁力链接")):
    if not SecurityManager.check_rate_limit("client_global", limit_count=30):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍候再试")

    tmdb_info = await query_tmdb_metadata(query)
    if not tmdb_info or not tmdb_info.get("success"):
        return {"success": False, "message": "TMDB 未检索到匹配的影视元数据"}

    emby_res = await check_emby_has_media(
        tmdb_id=tmdb_info["tmdb_id"],
        media_type=tmdb_info["media_type"],
        season=tmdb_info["season"],
        episode=tmdb_info["episode"]
    )

    return {
        "success": True,
        "tmdb": tmdb_info,
        "emby": emby_res,
        "can_upload": not emby_res.get("has_exact_episode", False),
        "reward_points": settings.POINTS_NEW_MOVIE if tmdb_info["media_type"] == "movie" else settings.POINTS_EPISODE
    }

@app.post("/api/media/submit")
async def submit_media(req: SubmitRequest, user: Dict[str, Any] = Depends(get_current_user)):
    res = await process_media_submission(
        magnet=req.magnet or "",
        custom_name=req.custom_name or "",
        username=user["username"],
        user_role=user["role"]
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message"))
    return res

@app.post("/api/admin/emby/scan")
async def trigger_scan(user: Dict[str, Any] = Depends(require_role(["owner", "admin"]))):
    ok = await trigger_emby_library_refresh()
    return {"success": ok, "message": "Emby 媒体库深度扫描已触发！"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
