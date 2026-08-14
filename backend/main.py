"""
KeyHeatmap FastAPI 后端
GaiaDB (MySQL 8.0) — 按键统计 & 排行榜服务
"""

import threading
import time
from datetime import date, datetime
from typing import List, Optional

import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------- Config ----------
import os
DB_CONFIG = {
    "host": os.getenv("DB_HOST", ""),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "keyheatmap"),
    "charset": "utf8mb4",
    "autocommit": True,
}

app = FastAPI(title="KeyHeatmap API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 过滤名单：排行榜中隐藏的测试/临时用户（key_stats.user_id）
HIDDEN_USER_IDS = {"test_sync_debug"}


@app.on_event("startup")
def _cleanup_hidden_users():
    """启动时清理测试/临时用户的 key_stats 数据"""
    if not HIDDEN_USER_IDS:
        return
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS app_stats (
                    user_id VARCHAR(128) NOT NULL,
                    stat_date DATE NOT NULL,
                    app_name VARCHAR(128) NOT NULL,
                    count INT NOT NULL DEFAULT 0,
                    PRIMARY KEY (user_id, stat_date, app_name),
                    KEY idx_app_date (stat_date, app_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
            )
            placeholders = ", ".join(["%s"] * len(HIDDEN_USER_IDS))
            cur.execute(
                f"DELETE FROM key_stats WHERE user_id IN ({placeholders})",
                list(HIDDEN_USER_IDS),
            )
            cur.execute(
                f"DELETE FROM app_stats WHERE user_id IN ({placeholders})",
                list(HIDDEN_USER_IDS),
            )
            deleted = cur.rowcount
        conn.commit()
        conn.close()
        if deleted:
            print(f"[startup] cleaned {deleted} hidden user key_stats rows")
    except Exception as e:
        print(f"[startup] cleanup hidden users failed: {e}")


def get_conn():
    return pymysql.connect(**DB_CONFIG, connect_timeout=5)


# ---------- 排行榜响应缓存（TTL 30 秒，避免高频请求直查 DB） ----------
_CACHE_LOCK = threading.Lock()
_LEADERBOARD_CACHE = {"key": None, "ts": 0.0, "data": None}
_KEYHEAT_CACHE = {"key": None, "ts": 0.0, "data": None}
_APP_LEADERBOARD_CACHE = {"key": None, "ts": 0.0, "data": None}
_CACHE_TTL = 30.0


def _cache_key(target_date, include_mouse, limit):
    return (str(target_date), include_mouse, limit)


def _get_cached(cache, key):
    with _CACHE_LOCK:
        if cache["key"] == key and time.time() - cache["ts"] < _CACHE_TTL:
            return cache["data"]
    return None


def _set_cached(cache, key, data):
    with _CACHE_LOCK:
        cache["key"] = key
        cache["ts"] = time.time()
        cache["data"] = data


# ---------- Models ----------
class KeyStatItem(BaseModel):
    user_id: str
    stat_date: date
    key_name: str
    count: int


class AppStatItem(BaseModel):
    user_id: str
    stat_date: date
    app_name: str
    count: int


class UploadPayload(BaseModel):
    items: List[KeyStatItem]
    apps: Optional[List[AppStatItem]] = None


class UserInfo(BaseModel):
    device_id: str
    nickname: Optional[str] = None


# ---------- Sensitive Words ----------
SENSITIVE_WORDS = [
    "fuck", "shit", "bitch", "asshole", "nigger", "faggot", "cunt", "dick", "pussy", "whore",
    "妈的", "操你", "傻逼", "煞笔", "草泥马", "尼玛", "你妈", "去死", "贱人", "婊子",
    "嫖", "卖淫", "赌博", "赌场", "博彩", "彩票", "代开发票", "办证", "贷款", "刷单", "兼职日结",
    "习", "毛泽东", "法轮", "六四", "天安门", "台独", "藏独", "疆独", "邪教",
    "毒品", "冰毒", "海洛因", "枪支", "弹药", "爆炸", "恐怖",
]

def _normalize_nickname(nickname):
    """规范化昵称：去首尾空白，压缩连续空白"""
    if nickname is None:
        return ""
    return " ".join(str(nickname).strip().split())

def _check_sensitive(nickname):
    """检查昵称是否包含敏感词，返回 (ok, hit_word)"""
    low = nickname.lower()
    for w in SENSITIVE_WORDS:
        if w.lower() in low:
            return False, w
    return True, None


class LeaderboardEntry(BaseModel):
    rank: int
    user_id: str
    nickname: str
    key_name: str
    total_count: int


class LeaderboardResponse(BaseModel):
    leaderboard: List[LeaderboardEntry]
    dates: List[str]


# ---------- Health ----------
@app.get("/api/health")
def health():
    try:
        conn = get_conn()
        conn.close()
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ---------- Upload ----------
@app.post("/api/upload")
def upload(payload: UploadPayload):
    if not payload.items and not payload.apps:
        raise HTTPException(400, "empty items")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 合并仅大小写不同的按键名（Shift/SHIFT），避免撞唯一索引
            merged = {}
            for it in payload.items:
                key = (it.user_id, it.stat_date, it.key_name.casefold())
                if key not in merged:
                    merged[key] = [it.user_id, it.stat_date, it.key_name, 0]
                merged[key][3] += it.count
            dates_by_user = {}
            for uid, stat_date, key_name, count in merged.values():
                dates_by_user.setdefault((uid, stat_date), []).append((uid, stat_date, key_name, count))
            for (user_id, stat_date), items in dates_by_user.items():
                cur.execute(
                    "DELETE FROM key_stats WHERE user_id = %s AND stat_date = %s",
                    (user_id, stat_date),
                )
                sql = """
                    INSERT INTO key_stats (user_id, stat_date, key_name, count)
                    VALUES (%s, %s, %s, %s)
                """
                params = items
                cur.executemany(sql, params)
            # 应用按键统计：同样按 user+date 覆盖，应用名大小写不敏感合并
            if payload.apps:
                app_merged = {}
                for it in payload.apps:
                    key = (it.user_id, it.stat_date, it.app_name.casefold())
                    if key not in app_merged:
                        app_merged[key] = [it.user_id, it.stat_date, it.app_name, 0]
                    app_merged[key][3] += it.count
                app_by_user = {}
                for uid, stat_date, app_name, count in app_merged.values():
                    app_by_user.setdefault((uid, stat_date), []).append((uid, stat_date, app_name, count))
                for (user_id, stat_date), items in app_by_user.items():
                    cur.execute(
                        "DELETE FROM app_stats WHERE user_id = %s AND stat_date = %s",
                        (user_id, stat_date),
                    )
                    cur.executemany(
                        """INSERT INTO app_stats (user_id, stat_date, app_name, count)
                           VALUES (%s, %s, %s, %s)""",
                        items,
                    )
        conn.commit()
        return {"inserted": len(payload.items), "apps_inserted": len(payload.apps) if payload.apps else 0}
    finally:
        conn.close()


# ---------- Leaderboard ----------
# 鼠标三键在 key_stats 中的实际字段名（KeyHeatmap 客户端上传的是 LMB/RMB/MMB）
MOUSE_KEY_NAMES = ["LMB", "RMB", "MMB"]


@app.get("/api/leaderboard", response_model=LeaderboardResponse)
def leaderboard(
    target_date: Optional[date] = None,
    key_name: Optional[str] = None,
    include_mouse: bool = True,
    limit: int = 30,
):
    # 30 秒响应缓存：命中时直接返回，不查 DB
    ck = _cache_key(f"{target_date}|{key_name}", include_mouse, limit)
    cached = _get_cached(_LEADERBOARD_CACHE, ck)
    if cached is not None:
        return cached

    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            where = []
            params = []

            if target_date:
                where.append("ks.stat_date = %s")
                params.append(target_date)
            if key_name:
                where.append("ks.key_name = %s")
                params.append(key_name)
            if not include_mouse:
                where.append("ks.key_name NOT IN (%s, %s, %s)")
                params.extend(MOUSE_KEY_NAMES)
            # 过滤测试/临时用户
            if HIDDEN_USER_IDS:
                placeholders = ", ".join(["%s"] * len(HIDDEN_USER_IDS))
                where.append(f"ks.user_id NOT IN ({placeholders})")
                params.extend(HIDDEN_USER_IDS)

            where_clause = ("WHERE " + " AND ".join(where)) if where else ""

            sql = f"""
                SELECT
                    ks.user_id,
                    COALESCE(u.nickname, ks.user_id) AS nickname,
                    '' AS key_name,
                    SUM(ks.count) AS total_count
                FROM key_stats ks
                LEFT JOIN users u ON ks.user_id = u.device_id
                {where_clause}
                GROUP BY ks.user_id
                ORDER BY total_count DESC
                LIMIT %s
            """
            params.append(limit)

            cur.execute(sql, params)
            rows = cur.fetchall()

        leaderboard = [
            LeaderboardEntry(
                rank=i + 1,
                user_id=r["user_id"],
                nickname=r["nickname"],
                key_name=r["key_name"],
                total_count=r["total_count"],
            )
            for i, r in enumerate(rows)
        ]
        # 前端已不使用 dates，省略额外查询以提速
        result = {"leaderboard": leaderboard, "dates": []}
        _set_cached(_LEADERBOARD_CACHE, ck, result)
        return result
    finally:
        conn.close()


# ---------- Key Heat Ranking ----------
class KeyHeatEntry(BaseModel):
    rank: int
    key_name: str
    total_count: int


@app.get("/api/keyheat")
def keyheat(
    target_date: Optional[date] = None,
    include_mouse: bool = True,
    limit: int = 50,
):
    """按键热度分析：按 key_name 聚合所有用户的按键总次数排行"""
    # 30 秒响应缓存
    ck = _cache_key(target_date, include_mouse, limit)
    cached = _get_cached(_KEYHEAT_CACHE, ck)
    if cached is not None:
        return cached

    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            where = []
            params = []

            if target_date:
                where.append("stat_date = %s")
                params.append(target_date)
            if not include_mouse:
                where.append("key_name NOT IN (%s, %s, %s)")
                params.extend(MOUSE_KEY_NAMES)
            # 过滤测试/临时用户
            if HIDDEN_USER_IDS:
                placeholders = ", ".join(["%s"] * len(HIDDEN_USER_IDS))
                where.append(f"user_id NOT IN ({placeholders})")
                params.extend(HIDDEN_USER_IDS)

            where_clause = ("WHERE " + " AND ".join(where)) if where else ""

            sql = f"""
                SELECT
                    key_name,
                    SUM(count) AS total_count
                FROM key_stats
                {where_clause}
                GROUP BY key_name
                ORDER BY total_count DESC
                LIMIT %s
            """
            params.append(limit)

            cur.execute(sql, params)
            rows = cur.fetchall()

        ranking = [
            KeyHeatEntry(
                rank=i + 1,
                key_name=r["key_name"],
                total_count=r["total_count"],
            )
            for i, r in enumerate(rows)
        ]

        result = {"ranking": ranking}
        _set_cached(_KEYHEAT_CACHE, ck, result)
        return result
    finally:
        conn.close()


@app.get("/api/app-leaderboard")
def app_leaderboard(
    target_date: Optional[date] = None,
    limit: int = 30,
):
    """今日应用按键榜：按 app_name 聚合全用户按键次数"""
    ck = (str(target_date), limit)
    cached = _get_cached(_APP_LEADERBOARD_CACHE, ck)
    if cached is not None:
        return cached

    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            where = []
            params = []
            if target_date:
                where.append("stat_date = %s")
                params.append(target_date)
            if HIDDEN_USER_IDS:
                placeholders = ", ".join(["%s"] * len(HIDDEN_USER_IDS))
                where.append(f"user_id NOT IN ({placeholders})")
                params.extend(HIDDEN_USER_IDS)
            where_clause = ("WHERE " + " AND ".join(where)) if where else ""
            sql = f"""
                SELECT app_name, SUM(count) AS total_count
                FROM app_stats
                {where_clause}
                GROUP BY app_name
                ORDER BY total_count DESC
                LIMIT %s
            """
            params.append(limit)
            cur.execute(sql, params)
            rows = cur.fetchall()
        result = {
            "apps": [
                {"rank": i + 1, "app_name": r["app_name"], "total_count": r["total_count"]}
                for i, r in enumerate(rows)
            ]
        }
        _set_cached(_APP_LEADERBOARD_CACHE, ck, result)
        return result
    finally:
        conn.close()


@app.get("/api/dates")
def get_dates():
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT stat_date FROM key_stats ORDER BY stat_date DESC"
            )
            rows = cur.fetchall()
        return {"dates": [str(r["stat_date"]) for r in rows]}
    finally:
        conn.close()


# ---------- User ----------
@app.post("/api/user")
def upsert_user(user: UserInfo):
    nickname = _normalize_nickname(user.nickname)
    if not nickname:
        raise HTTPException(400, "昵称不能为空")
    if len(nickname) > 10:
        raise HTTPException(400, "昵称不能超过 10 个字符")

    ok, hit = _check_sensitive(nickname)
    if not ok:
        raise HTTPException(400, f"昵称包含敏感词: {hit}")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 昵称唯一性校验（排除自己）
            cur.execute(
                "SELECT device_id FROM users WHERE nickname = %s AND device_id != %s",
                (nickname, user.device_id),
            )
            if cur.fetchone() is not None:
                raise HTTPException(409, "该昵称已被使用，请换一个")

            cur.execute(
                """INSERT INTO users (device_id, nickname)
                   VALUES (%s, %s)
                   ON DUPLICATE KEY UPDATE nickname = VALUES(nickname)""",
                (user.device_id, nickname),
            )
        conn.commit()
        return {"device_id": user.device_id, "nickname": nickname}
    except HTTPException:
        raise
    finally:
        conn.close()


@app.get("/api/user")
def get_user(device_id: str):
    conn = get_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT device_id, nickname FROM users WHERE device_id = %s",
                (device_id,),
            )
            row = cur.fetchone()
        if row is None:
            return {"registered": False, "nickname": None}
        return {"registered": True, "nickname": row["nickname"]}
    finally:
        conn.close()


@app.delete("/api/user")
def delete_user(device_id: str):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM users WHERE device_id = %s",
                (device_id,),
            )
            if cur.fetchone() is None:
                raise HTTPException(404, "user not found")
            cur.execute(
                "DELETE FROM key_stats WHERE user_id = %s",
                (device_id,),
            )
            cur.execute(
                "DELETE FROM users WHERE device_id = %s",
                (device_id,),
            )
        conn.commit()
        return {"deleted": True}
    finally:
        conn.close()


# ---------- Entry ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
