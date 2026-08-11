"""
AstrBot 积分签到插件 v1.3.2
作者：小星萤
功能：
- 每日签到（带连续奖励 + 漏签宽限）
- 积分查询、转账、流水
- 积分排行榜 / 签到排行榜
- 补签（消耗积分）
- 可配置奖池抽奖
- 管理员可调整任意用户的积分 / 签到数据
- 管理员清零指令
所有数据持久化到 SQLite，所有积分变动写流水可追溯。
"""

import html
import json
import math
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api import AstrBotConfig


# 数据库路径：AstrBot data 目录下，避免插件更新时数据丢失
DATA_DIR = Path("data") / "astrbot_plugin_signin"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "signin.db"


# ============================================================
# SQL 建表语句
# ============================================================
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    points INTEGER NOT NULL DEFAULT 0,
    total_points INTEGER NOT NULL DEFAULT 0,
    total_sign INTEGER NOT NULL DEFAULT 0,
    continuous_sign INTEGER NOT NULL DEFAULT 0,
    max_continuous_sign INTEGER NOT NULL DEFAULT 0,
    last_sign_date TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sign_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    sign_date TEXT NOT NULL,
    points_gained INTEGER NOT NULL,
    continuous_days INTEGER NOT NULL,
    is_makeup INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, sign_date)
);
CREATE INDEX IF NOT EXISTS idx_sign_log_user ON sign_log(user_id);

CREATE TABLE IF NOT EXISTS points_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    change_type TEXT NOT NULL,
    change_amount INTEGER NOT NULL,
    balance_after INTEGER NOT NULL,
    related_user_id TEXT DEFAULT '',
    description TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_points_log_user ON points_log(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS lottery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    prize_name TEXT NOT NULL,
    reward INTEGER NOT NULL,
    cost INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_lottery_log_user ON lottery_log(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS shop_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cost INTEGER NOT NULL,
    description TEXT DEFAULT '',
    stock INTEGER NOT NULL DEFAULT -1,
    delivery TEXT DEFAULT '请联系管理员',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS shop_purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL UNIQUE,
    user_id TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    cost INTEGER NOT NULL,
    delivery TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_shop_purchases_user ON shop_purchases(user_id, created_at DESC);
"""


# 默认奖池（用户在 WebUI 的 lottery_pool_json 配置里可改）
DEFAULT_LOTTERY_POOL = [
    {"name": "🎉 谢谢参与", "weight": 50, "reward": 0},
    {"name": "💰 小额积分", "weight": 30, "reward": 50},
    {"name": "💎 大额积分", "weight": 15, "reward": 200},
    {"name": "🏆 暴富积分", "weight": 4, "reward": 1000},
    {"name": "✨ 欧皇积分", "weight": 1, "reward": 5000},
]


class SigninPlugin(Star):
    """积分签到插件主类 by 小星萤"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        admin_str = str(self.config.get("admin_umo_list") or "").strip()
        self.admin_ids = {
            item for item in re.split(r"[,，;；\s]+", admin_str) if item
        }
        # 解析奖池配置
        self.lottery_pool = self._load_lottery_pool()
        logger.info(f"[signin] 插件加载完成，管理员: {self.admin_ids or '（空）'}，奖池 {len(self.lottery_pool)} 项")

    async def initialize(self):
        await self._init_db()

    async def _init_db(self):
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.executescript(SCHEMA_SQL)
            await db.commit()
        await self._init_shop_items()
        logger.info(f"[signin] 数据库初始化完成: {DB_PATH}")

    def _read_shop_config(self) -> list:
        """读取默认商品配置。

        优先级：
        1. shop_default_items_editor（template_list 可视化编辑结果）
        2. shop_default_items（兼容旧版普通 list）
        3. 旧字段 shop_default_items_json（JSON 字符串或 list）
        4. 都没有 -> 返回空 list，由调用方跳过导入
        """
        editor_items = self.config.get("shop_default_items_editor", None)
        if isinstance(editor_items, list) and editor_items:
            return editor_items
        new_items = self.config.get("shop_default_items", None)
        if isinstance(new_items, list) and new_items:
            return new_items
        if isinstance(new_items, str) and new_items.strip():
            try:
                parsed = json.loads(new_items.strip())
                if isinstance(parsed, list) and parsed:
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        old_items = self.config.get("shop_default_items_json", None)
        if isinstance(old_items, list) and old_items:
            return old_items
        if isinstance(old_items, str) and old_items.strip():
            try:
                parsed = json.loads(old_items.strip())
                if isinstance(parsed, list) and parsed:
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    async def _init_shop_items(self):
        """从配置补充尚未入库的默认商品。

        商品表可能已经包含通过「商品上架」指令创建的商品，因此不能仅在
        整张表为空时导入。这里按商品名称检查并补充缺失项，同时保留已有
        商品的库存、价格和上下架状态，避免插件重载时覆盖运营数据。
        """
        if not self.config.get("shop_enabled", True):
            return
        try:
            async with aiosqlite.connect(str(DB_PATH)) as db:
                items = self._read_shop_config()
                if not items:
                    return
                if not isinstance(items, list):
                    logger.warning(
                        f"[signin] 默认商品配置类型不支持: {type(items).__name__}"
                    )
                    return
                inserted = 0
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if "name" not in it or "cost" not in it:
                        continue
                    name = str(it["name"]).strip()
                    if not name:
                        continue
                    try:
                        cost = int(it["cost"])
                        stock = int(it.get("stock", -1))
                    except (TypeError, ValueError):
                        continue
                    if cost < 0 or stock < -1:
                        continue
                    cur = await db.execute(
                        "SELECT 1 FROM shop_items WHERE name = ? LIMIT 1",
                        (name,),
                    )
                    if await cur.fetchone():
                        continue
                    await db.execute(
                        """INSERT INTO shop_items
                           (name, cost, description, stock, delivery, enabled)
                           VALUES (?, ?, ?, ?, ?, 1)""",
                        (
                            name,
                            cost,
                            str(it.get("description", "")),
                            stock,
                            str(it.get("delivery", "请联系管理员")),
                        ),
                    )
                    inserted += 1
                if inserted:
                    await db.commit()
                    logger.info(f"[signin] 自动补充了 {inserted} 个默认商品")
        except Exception as e:
            logger.warning(f"[signin] 默认商品配置加载失败: {e}")

    # ============================================================
    # 工具方法
    # ============================================================
    @staticmethod
    def _extract_user_id(s: str) -> str:
        if not s:
            return ""
        s = str(s).strip()
        m = re.search(r"\d+", s)
        return m.group(0) if m else s

    async def _resolve_target_user_id(self, event: AstrMessageEvent, target: str) -> str:
        """把 @target 解析为真实的 user_id，避免把昵称当成假 user 写入数据库。

        - 纯数字（含 @ 前缀）: 直接返回
        - 昵称: 在当前平台下查库，返回对应 user_id
        - 找不到: 返回空字符串
        """
        if not target:
            return ""
        raw = str(target).strip().lstrip("@").strip()
        if not raw:
            return ""
        if raw.isdigit():
            return raw
        platform, _ = self._user_key(event)
        try:
            async with aiosqlite.connect(str(DB_PATH)) as db:
                cur = await db.execute(
                    "SELECT user_id FROM users "
                    "WHERE platform = ? AND nickname = ? "
                    "ORDER BY points DESC LIMIT 1",
                    (platform, raw),
                )
                row = await cur.fetchone()
                if row:
                    return str(row[0])
        except Exception as e:
            logger.warning(f"[signin] 按昵称查找用户失败: {e}")
        return ""

    def _config_int(self, key: str, default: int, minimum: int | None = None,
                    maximum: int | None = None) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError, OverflowError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _config_float(self, key: str, default: float, minimum: float | None = None,
                      maximum: float | None = None) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError, OverflowError):
            value = default
        if not math.isfinite(value):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _user_key(self, event: AstrMessageEvent):
        return str(event.get_platform_name() or ""), str(event.get_sender_id() or "").strip()

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sid = str(event.get_sender_id() or "").strip()
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        return bool(self.admin_ids) and (sid in self.admin_ids or umo in self.admin_ids)

    def _read_pool_config(self) -> list:
        """读取奖池配置。

        优先级：
        1. 新字段 lottery_pool（template_list 返回 list of dict）
        2. 旧字段 lottery_pool_json（JSON 字符串或 list）
        3. 都没有 -> 返回空 list，由调用方回退默认奖池
        """
        # 1. 可视化编辑字段：template_list 返回 list of dict
        editor_pool = self.config.get("lottery_pool_editor", None)
        if isinstance(editor_pool, list) and editor_pool:
            return editor_pool
        # 2. 兼容字段：普通 list 或 JSON 字符串
        new_pool = self.config.get("lottery_pool", None)
        if isinstance(new_pool, list) and new_pool:
            return new_pool
        if isinstance(new_pool, str) and new_pool.strip():
            try:
                parsed = json.loads(new_pool.strip())
                if isinstance(parsed, list) and parsed:
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        # 2. 旧字段
        old_pool = self.config.get("lottery_pool_json", None)
        if isinstance(old_pool, list) and old_pool:
            return old_pool
        if isinstance(old_pool, str) and old_pool.strip():
            try:
                parsed = json.loads(old_pool.strip())
                if isinstance(parsed, list) and parsed:
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return []

    def _load_lottery_pool(self) -> list:
        """从配置加载奖池，配置错误时回退默认奖池。

        兼容 AstrBot 在不同版本／不同入口下把 lottery_pool_json 分别存为
        JSON 字符串或 Python list 的情况；任一情况解析失败都回退默认奖池，
        保证每日抽奖不会因为配置类型差异而偶发不可用。
        """
        try:
            pool = self._read_pool_config()
            if not pool:
                return DEFAULT_LOTTERY_POOL
            if not isinstance(pool, list):
                logger.warning(
                    f"[signin] 奖池配置类型不支持: {type(pool).__name__}，使用默认奖池"
                )
                return DEFAULT_LOTTERY_POOL
            valid = []
            for item in pool:
                if not isinstance(item, dict):
                    continue
                if "name" not in item or "weight" not in item:
                    continue
                try:
                    w = float(item["weight"])
                    r = int(item.get("reward", 0))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(w) or w <= 0:
                    continue
                valid.append({
                    "name": str(item["name"]),
                    "weight": w,
                    "reward": r,
                })
            if valid:
                return valid
        except Exception as e:
            logger.warning(f"[signin] 奖池配置加载失败，使用默认奖池: {e}")
        return DEFAULT_LOTTERY_POOL

    def _spin_lottery(self) -> dict:
        """根据权重抽取一个奖品"""
        pool = self.lottery_pool
        total = sum(p["weight"] for p in pool)
        r = random.uniform(0, total)
        cum = 0
        for p in pool:
            cum += p["weight"]
            if r <= cum:
                return p
        return pool[-1]

    async def _get_or_create_user(self, db, platform, user_id, nickname):
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            if nickname and nickname != row[2]:
                await db.execute(
                    "UPDATE users SET nickname=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
                    (nickname, user_id),
                )
            return {
                "user_id": row[0], "platform": row[1], "nickname": row[2],
                "points": row[3], "total_points": row[4], "total_sign": row[5],
                "continuous_sign": row[6], "max_continuous_sign": row[7],
                "last_sign_date": row[8] or "",
            }
        await db.execute(
            "INSERT INTO users (user_id, platform, nickname, points) VALUES (?, ?, ?, 0)",
            (user_id, platform, nickname or user_id),
        )
        return {
            "user_id": user_id, "platform": platform, "nickname": nickname or user_id,
            "points": 0, "total_points": 0, "total_sign": 0,
            "continuous_sign": 0, "max_continuous_sign": 0, "last_sign_date": "",
        }

    def _calc_sign_points(self, continuous_days: int):
        base = self._config_int("base_points", 10, 0)
        per_day = self._config_int("continuous_bonus_per_day", 2, 0)
        max_bonus = self._config_int("continuous_bonus_max", 30, 0)
        cap = self._config_int("daily_sign_max_points", 50, 0)
        bonus = min(max(0, continuous_days) * per_day, max_bonus)
        total = min(base + bonus, cap)
        return total, max(0, total - base)

    @staticmethod
    def _generate_order_id() -> str:
        """生成订单 ID（时间戳 + 4 位随机）"""
        import secrets
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        rand = secrets.token_hex(2).upper()
        return f"ORD{ts}{rand}"

    def _format_leaderboard_html(self, title: str, rows: list, header_map: dict, value_format=None) -> str:
        """把排行榜数据渲染成 HTML（给 html_render 用）"""
        width = self._config_int("image_width", 800, 320, 2400)
        medal_map = ["🥇", "🥈", "🥉"]
        body_rows = []
        for i, row in enumerate(rows, 1):
            medal = medal_map[i - 1] if i <= 3 else f"{i}."
            cells = [f'<td style="padding:8px 14px;font-size:18px;">{medal}</td>']
            for key, label in header_map.items():
                val = row.get(key, "")
                if value_format and key in value_format:
                    val = value_format[key](val)
                val = html.escape(str(val))
                cells.append(
                    f'<td style="padding:8px 14px;font-size:18px;color:#333;text-align:center;">{val}</td>'
                )
            body_rows.append("<tr>" + "".join(cells) + "</tr>")
        # 表头
        header_cells = ['<th style="padding:10px 14px;font-size:18px;background:#f5f5f5;">#</th>']
        for label in header_map.values():
            header_cells.append(
                f'<th style="padding:10px 14px;font-size:18px;background:#f5f5f5;">{html.escape(str(label))}</th>'
            )
        html_content = f"""<div style="width:{width}px;font-family:'Microsoft YaHei',sans-serif;padding:24px;background:#fff;">
<h1 style="text-align:center;color:#222;margin:0 0 16px 0;">{html.escape(str(title))}</h1>
<table style="width:100%;border-collapse:collapse;">
<thead><tr>{"".join(header_cells)}</tr></thead>
<tbody>
{"".join(body_rows)}
</tbody>
</table>
</div>"""
        return html_content

    def _format_calendar_html(self, nickname: str, days: int, signed_map: dict, makeup_map: dict,
                              continuous: int, max_continuous: int, total_sign: int) -> str:
        """签到日历 HTML（格子视图）"""
        from datetime import date
        width = self._config_int("image_width", 800, 320, 2400)
        today = date.today()
        # 构造最近 N 天的日期列表（升序）
        day_list = [today - timedelta(days=days - 1 - i) for i in range(days)]
        # 5 个一行
        cells = []
        for d in day_list:
            ds = d.strftime("%Y-%m-%d")
            is_today = (d == today)
            if ds in makeup_map:
                glyph = "🩹"
                bg = "#fff3e0"
                title = f"补签 {ds}"
            elif ds in signed_map:
                glyph = "✅"
                bg = "#e8f5e9"
                title = f"已签 {ds}"
            else:
                glyph = "⬜"
                bg = "#f5f5f5"
                title = f"未签 {ds}"
            border = "border:2px solid #ff9800;" if is_today else ""
            cells.append(
                f'<td style="padding:6px;background:{bg};{border}text-align:center;font-size:22px;border-radius:6px;" title="{title}">{glyph}</td>'
            )
        # 每 5 个一行
        rows_html = []
        for i in range(0, len(cells), 5):
            rows_html.append("<tr>" + "".join(cells[i:i + 5]) + "</tr>")
        # 统计
        signed_count = sum(1 for d in day_list if d.strftime("%Y-%m-%d") in signed_map)
        makeup_count = sum(1 for d in day_list if d.strftime("%Y-%m-%d") in makeup_map)
        rate = round(signed_count / days * 100, 1) if days > 0 else 0
        # 起始/结束日期
        d_start = day_list[0].strftime("%m/%d")
        d_end = day_list[-1].strftime("%m/%d")
        html_content = f"""<div style="width:{width}px;font-family:'Microsoft YaHei',sans-serif;padding:24px;background:#fff;">
<h1 style="text-align:center;color:#222;margin:0 0 8px 0;">📅 {html.escape(str(nickname))} 的签到日历</h1>
<p style="text-align:center;color:#999;font-size:14px;margin:0 0 16px 0;">{d_start} ~ {d_end}（最近 {days} 天）</p>
<table style="width:100%;border-collapse:separate;border-spacing:6px;">
<tbody>
{"".join(rows_html)}
</tbody>
</table>
<div style="display:flex;justify-content:space-around;margin-top:20px;padding:12px;background:#f9f9f9;border-radius:8px;">
<div style="text-align:center;"><div style="font-size:24px;color:#4caf50;font-weight:bold;">{signed_count}/{days}</div><div style="font-size:12px;color:#666;">已签到</div></div>
<div style="text-align:center;"><div style="font-size:24px;color:#ff9800;font-weight:bold;">{makeup_count}</div><div style="font-size:12px;color:#666;">补签</div></div>
<div style="text-align:center;"><div style="font-size:24px;color:#2196f3;font-weight:bold;">{continuous}</div><div style="font-size:12px;color:#666;">当前连续</div></div>
<div style="text-align:center;"><div style="font-size:24px;color:#9c27b0;font-weight:bold;">{max_continuous}</div><div style="font-size:12px;color:#666;">最高连续</div></div>
<div style="text-align:center;"><div style="font-size:24px;color:#f44336;font-weight:bold;">{rate}%</div><div style="font-size:12px;color:#666;">签到率</div></div>
</div>
<p style="text-align:center;color:#999;font-size:12px;margin-top:12px;">✅ 已签 &nbsp; 🩹 补签 &nbsp; ⬜ 未签 &nbsp; 🟧 今日</p>
</div>"""
        return html_content

    async def _add_points(self, db, user_id, amount, change_type,
                          related_user_id="", description="", log_amount=None):
        """通用积分变动：更新余额并写流水；log_amount 可记录已由外部 SQL 完成的变动。"""
        user_id = str(user_id)
        amount = int(amount)
        if amount != 0:
            cur = await db.execute(
                "UPDATE users SET points = points + ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (amount, user_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"用户 {user_id} 不存在，无法调整积分")
        cur = await db.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"用户 {user_id} 不存在，无法写入积分流水")
        balance_after = row[0]
        recorded_amount = amount if log_amount is None else int(log_amount)
        await db.execute(
            """INSERT INTO points_log
               (user_id, change_type, change_amount, balance_after, related_user_id, description)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user_id, change_type, recorded_amount, balance_after,
                str(related_user_id or ""), str(description or ""),
            ),
        )


    # ============================================================
    # 指令：签到
    # ============================================================
    @filter.command("签到", alias={"qiandao", "daily", "qd"})
    async def sign_in(self, event: AstrMessageEvent):
        """每日签到，带连续奖励"""
        platform, user_id = self._user_key(event)
        nickname = event.get_sender_name()
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        async with aiosqlite.connect(str(DB_PATH)) as db:
            # 串行化写事务，避免同一用户并发签到获得多次积分。
            await db.execute("BEGIN IMMEDIATE")
            user = await self._get_or_create_user(db, platform, user_id, nickname)
            if user["last_sign_date"] == today:
                await db.rollback()
                yield event.plain_result(
                    f"⚠️ {nickname} 你今天已经签过到了～\n"
                    f"💰 当前积分：{user['points']}\n"
                    f"🔥 连续签到：{user['continuous_sign']} 天\n明天再来吧！"
                )
                return

            grace_h = self._config_int("grace_hours", 8, 0, 23)
            continuous = user["continuous_sign"]
            if user["last_sign_date"]:
                try:
                    last = datetime.strptime(user["last_sign_date"], "%Y-%m-%d")
                    diff_days = (now.date() - last.date()).days
                except (TypeError, ValueError):
                    logger.warning(
                        f"[signin] 用户 {user_id} 的上次签到日期无效，已重置连续签到"
                    )
                    diff_days = -1
                if diff_days == 1:
                    continuous += 1
                elif diff_days == 2 and now.hour < grace_h:
                    continuous += 1
                else:
                    continuous = 1
            else:
                continuous = 1

            gained, bonus = self._calc_sign_points(continuous)
            new_total_sign = user["total_sign"] + 1
            new_max = max(user["max_continuous_sign"], continuous)
            is_first = user["total_sign"] == 0

            # 唯一索引是最终防线；只有成功写入签到记录才发放积分。
            try:
                await db.execute(
                    """INSERT INTO sign_log
                       (user_id, sign_date, points_gained, continuous_days, is_makeup)
                       VALUES (?, ?, ?, ?, 0)""",
                    (user_id, today, gained, continuous),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                yield event.plain_result("⚠️ 今天已经签过到了，请勿重复签到")
                return

            await db.execute(
                """UPDATE users SET
                   points = points + ?,
                   total_points = total_points + ?,
                   total_sign = ?,
                   continuous_sign = ?,
                   max_continuous_sign = ?,
                   last_sign_date = ?,
                   updated_at = CURRENT_TIMESTAMP
                   WHERE user_id = ?""",
                (gained, gained, new_total_sign, continuous, new_max, today, user_id),
            )
            await self._add_points(
                db, user_id, 0, "sign",
                description=f"签到 +{gained} (连续 {continuous} 天)",
                log_amount=gained,
            )
            await db.commit()
            cur = await db.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            cur_points = row[0] if row else 0

        welcome = ""
        if is_first:
            tmpl = str(
                self.config.get("welcome_message")
                or "欢迎 {nickname} 开启积分之旅！🎉"
            )
            try:
                welcome = tmpl.format(nickname=nickname) + "\n"
            except (KeyError, ValueError, IndexError):
                logger.warning("[signin] 首次签到欢迎语格式无效，已按普通文本发送")
                welcome = tmpl.replace("{nickname}", str(nickname)) + "\n"

        bonus_text = f" (含连续奖励 +{bonus})" if bonus > 0 else ""
        result = (
            f"{welcome}✅ 签到成功！\n"
            f"━━━━━━━━━━━━━━\n"
            f"🎁 本次获得：+{gained} 积分{bonus_text}\n"
            f"🔥 连续签到：{continuous} 天 (最高 {new_max} 天)\n"
            f"📅 累计签到：{new_total_sign} 次\n"
            f"💰 当前积分：{cur_points}\n"
            f"━━━━━━━━━━━━━━\n明天继续来哦～"
        )
        yield event.plain_result(result)

    # ============================================================
    # 指令：查询积分
    # ============================================================
    @filter.command("积分", alias={"jifen", "points", "我的积分", "余额"})
    async def query_points(self, event: AstrMessageEvent, target: str = ""):
        """查询积分：/积分 或 /积分 @某人"""
        _, sender_id = self._user_key(event)
        if target:
            query_id = await self._resolve_target_user_id(event, target)
            if not query_id:
                yield event.plain_result(
                    "❌ 找不到该用户，请确认对方已签到过；\n"
                    "   或使用 @QQ号 / 长按头像@ 重新尝试"
                )
                return
        else:
            query_id = sender_id

        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (query_id,))
            row = await cur.fetchone()
            if not row:
                yield event.plain_result(f"❌ 用户 {query_id} 还没有签到记录哦")
                return
            nickname = row[2] or query_id
            points = row[3]
            total_sign = row[5]
            continuous = row[6]
            max_continuous = row[7]

        yield event.plain_result(
            f"💰 {nickname} 的积分信息\n"
            f"━━━━━━━━━━━━━━\n"
            f"💎 当前积分：{points}\n"
            f"📅 累计签到：{total_sign} 次\n"
            f"🔥 当前连续：{continuous} 天\n"
            f"🏆 最高连续：{max_continuous} 天"
        )

    # ============================================================
    # 指令：积分记录
    # ============================================================
    @filter.command("积分记录", alias={"流水", "jifenlog"})
    async def query_records(self, event: AstrMessageEvent, page: int = 1):
        """查询个人积分变动记录"""
        if page < 1:
            page = 1
        per_page = self._config_int("records_per_page", 8, 1, 100)
        offset = (page - 1) * per_page
        _, user_id = self._user_key(event)
        nickname = event.get_sender_name()

        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM points_log WHERE user_id = ?", (user_id,)
            )
            total = (await cur.fetchone())[0]
            cur = await db.execute(
                """SELECT change_type, change_amount, balance_after, description, created_at
                   FROM points_log WHERE user_id = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (user_id, per_page, offset),
            )
            rows = await cur.fetchall()

        if not rows:
            yield event.plain_result("📭 你还没有任何积分记录")
            return

        type_map = {
            "sign": "🎁 签到", "transfer_in": "📥 收到转账", "transfer_out": "📤 转出",
            "admin_add": "🛠 管理员增加", "admin_reduce": "🛠 管理员扣除",
            "makeup_cost": "💸 补签消耗", "makeup_gain": "🩹 补签奖励",
            "lottery": "🎰 抽奖", "admin_reset": "🔄 管理员重置",
        }
        lines = [f"📒 {nickname} 的积分记录 (第 {page} 页)\n━━━━━━━━━━━━━━"]
        for r in rows:
            ct, amt, bal, desc, ts = r
            sign = "+" if amt >= 0 else ""
            label = type_map.get(ct, ct)
            ts_short = (ts or "")[:16]
            lines.append(f"{ts_short}  {label}\n   {sign}{amt}  | 余额 {bal}\n   {desc}")
        total_pages = max(1, (total + per_page - 1) // per_page)
        lines.append(f"━━━━━━━━━━━━━━\n第 {page}/{total_pages} 页，共 {total} 条")
        yield event.plain_result("\n".join(lines))

    # ============================================================
    # 指令：积分排行榜
    # ============================================================
    @filter.command("排行榜", alias={"排行", "rank", "top", "积分排行"})
    async def leaderboard_points(self, event: AstrMessageEvent):
        """积分总榜（可转图片）"""
        top_n = self._config_int("leaderboard_top_n", 10, 1, 100)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute(
                """SELECT nickname, points, total_sign, continuous_sign
                   FROM users ORDER BY points DESC LIMIT ?""",
                (top_n,),
            )
            rows = await cur.fetchall()
        if not rows:
            yield event.plain_result("📭 暂无积分数据")
            return
        data = [
            {"nickname": r[0], "points": r[1], "total_sign": r[2], "continuous_sign": r[3]}
            for r in rows
        ]
        # 转图片
        if self.config.get("leaderboard_image_enabled", True):
            try:
                html_content = self._format_leaderboard_html(
                    "🏆 积分排行榜 TOP " + str(top_n),
                    data,
                    {"nickname": "昵称", "points": "积分", "total_sign": "累计签到", "continuous_sign": "连续"},
                )
                url = await self.html_render(html_content, {})
                yield event.image_result(url)
                return
            except Exception as e:
                logger.warning(f"[signin] 排行榜转图片失败，降级为文字: {e}")
        # 文字版
        lines = [f"🏆 积分排行榜 TOP {top_n}\n━━━━━━━━━━━━━━"]
        for i, r in enumerate(rows, 1):
            nick, pts, ts, cs = r
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            lines.append(f"{medal} {nick}\n   💎 {pts}  |  签到 {ts}  |  连续 {cs} 天")
        yield event.plain_result("\n".join(lines))

    # ============================================================
    # 指令：签到榜
    # ============================================================
    @filter.command("签到榜", alias={"签到排行", "signrank"})
    async def leaderboard_sign(self, event: AstrMessageEvent):
        """签到天数榜（可转图片）"""
        top_n = self._config_int("leaderboard_top_n", 10, 1, 100)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute(
                """SELECT nickname, total_sign, continuous_sign, max_continuous_sign, points
                   FROM users WHERE total_sign > 0
                   ORDER BY total_sign DESC LIMIT ?""",
                (top_n,),
            )
            rows = await cur.fetchall()
        if not rows:
            yield event.plain_result("📭 暂无签到数据")
            return
        data = [
            {"nickname": r[0], "total_sign": r[1], "continuous_sign": r[2],
             "max_continuous_sign": r[3], "points": r[4]}
            for r in rows
        ]
        if self.config.get("leaderboard_image_enabled", True):
            try:
                html_content = self._format_leaderboard_html(
                    "📅 签到排行榜 TOP " + str(top_n),
                    data,
                    {"nickname": "昵称", "total_sign": "累计", "continuous_sign": "连续", "max_continuous_sign": "最长", "points": "积分"},
                )
                url = await self.html_render(html_content, {})
                yield event.image_result(url)
                return
            except Exception as e:
                logger.warning(f"[signin] 签到榜转图片失败，降级为文字: {e}")
        lines = [f"📅 签到排行榜 TOP {top_n}\n━━━━━━━━━━━━━━"]
        for i, r in enumerate(rows, 1):
            nick, ts, cs, mcs, pts = r
            medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
            lines.append(
                f"{medal} {nick}\n   📅 累计 {ts}  |  🔥 连续 {cs}  |  🏆 最长 {mcs}  |  💎 {pts}"
            )
        yield event.plain_result("\n".join(lines))

    # ============================================================
    # 指令：转账
    # ============================================================
    @filter.command("转账", alias={"transfer", "给", "赠送"})
    async def transfer_points(self, event: AstrMessageEvent, target: str = "", amount: int = 0):
        """转账：/转账 @某人 数量"""
        target_id = await self._resolve_target_user_id(event, target) if target else ""
        if not target_id or amount <= 0:
            if not target_id and target:
                yield event.plain_result(
                    "❌ 找不到该用户，请确认对方已签到过；\n"
                    "   或使用 @QQ号 / 长按头像@ 重新尝试"
                )
            else:
                yield event.plain_result("⚠️ 用法: /转账 @某人 数量\n例如: /转账 @张三 100")
            return

        transfer_min = self._config_int("transfer_min", 1, 1)
        transfer_max = self._config_int("transfer_max", 10000, transfer_min)
        if amount < transfer_min:
            yield event.plain_result(f"⚠️ 最少转账 {transfer_min} 积分")
            return
        if amount > transfer_max:
            yield event.plain_result(f"⚠️ 单次最多转账 {transfer_max} 积分")
            return

        platform, sender_id = self._user_key(event)
        if sender_id == target_id:
            yield event.plain_result("⚠️ 不能给自己转账")
            return

        fee_rate = self._config_float("transfer_fee_rate", 0.0, 0.0, 1.0)
        fee = int(amount * fee_rate)
        total_cost = amount + fee
        sender_name = event.get_sender_name()

        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("BEGIN IMMEDIATE")
            await self._get_or_create_user(db, platform, sender_id, sender_name)
            cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (target_id,))
            trow = await cur.fetchone()
            target_nick = (trow[0] if trow and trow[0] else target_id)
            if not trow:
                await self._get_or_create_user(db, platform, target_id, target_nick)

            cur = await db.execute("SELECT points FROM users WHERE user_id = ?", (sender_id,))
            srow = await cur.fetchone()
            cur_balance = srow[0] if srow else 0
            if cur_balance < total_cost:
                await db.rollback()
                yield event.plain_result(
                    f"⚠️ 积分不足！需要 {total_cost} (含手续费 {fee})，当前 {cur_balance}"
                )
                return

            await self._add_points(
                db, sender_id, -total_cost, "transfer_out",
                related_user_id=target_id,
                description=f"转给 {target_nick} -{amount} 手续费 -{fee}",
            )
            await self._add_points(
                db, target_id, amount, "transfer_in",
                related_user_id=sender_id,
                description=f"收到 {sender_name} +{amount}",
            )
            await db.commit()

        msg_lines = [
            "✅ 转账成功！",
            "━━━━━━━━━━━━━━",
            f"💸 转出：{amount} 积分",
        ]
        if fee > 0:
            msg_lines.append(f"💰 手续费：{fee} 积分")
        msg_lines.append(f"➡️ 收款人：{target_nick}")
        msg_lines.append("💎 你的余额已更新")
        yield event.plain_result("\n".join(msg_lines))

    # ============================================================
    # 指令：补签
    # ============================================================
    @filter.command("补签", alias={"补卡", "makeup"})
    async def makeup_sign(self, event: AstrMessageEvent):
        """补签（消耗积分，补昨天）"""
        cost = self._config_int("makeup_cost", 50, 0)
        if cost <= 0:
            yield event.plain_result("⚠️ 补签功能未开启")
            return

        today_str = datetime.now().strftime("%Y-%m-%d")
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        platform, user_id = self._user_key(event)
        nickname = event.get_sender_name()

        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "SELECT 1 FROM sign_log WHERE user_id = ? AND sign_date = ?",
                (user_id, yesterday_str),
            )
            if await cur.fetchone():
                await db.rollback()
                yield event.plain_result("⚠️ 昨天已经签到，不需要补签")
                return

            user = await self._get_or_create_user(db, platform, user_id, nickname)
            if user["points"] < cost:
                await db.rollback()
                yield event.plain_result(f"⚠️ 积分不足，补签需要 {cost} 积分")
                return

            # 补的是昨天：若此前签到到前天，补签昨天应接在原连续天数后；
            # 若今天已签到，则补签记录插入今天之前，也同样延续当前连续天数。
            day_before_yesterday = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
            if user["last_sign_date"] == today_str:
                new_continuous = max(2, user["continuous_sign"] + 1)
                new_last_sign_date = today_str
            elif user["last_sign_date"] == day_before_yesterday:
                new_continuous = user["continuous_sign"] + 1
                new_last_sign_date = yesterday_str
            else:
                new_continuous = 1
                new_last_sign_date = yesterday_str
            new_total_sign = user["total_sign"] + 1
            new_max = max(user["max_continuous_sign"], new_continuous)
            gained, _ = self._calc_sign_points(new_continuous)
            net = gained - cost

            try:
                await db.execute(
                    """INSERT INTO sign_log
                       (user_id, sign_date, points_gained, continuous_days, is_makeup)
                       VALUES (?, ?, ?, ?, 1)""",
                    (user_id, yesterday_str, gained, new_continuous),
                )
            except aiosqlite.IntegrityError:
                await db.rollback()
                yield event.plain_result("⚠️ 昨天已经签到，不需要补签")
                return

            old_balance = user["points"]
            new_balance = old_balance - cost + gained
            await db.execute(
                """UPDATE users SET
                   points = ?,
                   total_points = total_points + ?,
                   total_sign = ?,
                   continuous_sign = ?,
                   max_continuous_sign = ?,
                   last_sign_date = ?,
                   updated_at = CURRENT_TIMESTAMP
                   WHERE user_id = ?""",
                (
                    new_balance, gained, new_total_sign, new_continuous, new_max,
                    new_last_sign_date, user_id,
                ),
            )
            # 两条流水分别记录扣费后的余额和奖励后的最终余额。
            await db.execute(
                """INSERT INTO points_log
                   (user_id, change_type, change_amount, balance_after, description)
                   VALUES (?, 'makeup_cost', ?, ?, ?)""",
                (user_id, -cost, old_balance - cost, f"补签 {yesterday_str} 消耗 {cost} 积分"),
            )
            await db.execute(
                """INSERT INTO points_log
                   (user_id, change_type, change_amount, balance_after, description)
                   VALUES (?, 'makeup_gain', ?, ?, ?)""",
                (user_id, gained, new_balance, f"补签 {yesterday_str} 奖励 +{gained}"),
            )
            await db.commit()
            cur_points = new_balance

        yield event.plain_result(
            f"✅ 补签成功！\n"
            f"💸 消耗：{cost} 积分\n"
            f"🎁 获得：+{gained} 积分 (净 {'+' if net >= 0 else ''}{net})\n"
            f"💎 当前余额：{cur_points}\n"
            f"🔥 连续签到：{new_continuous} 天"
        )


    # ============================================================
    # 指令：抽奖
    # ============================================================
    @filter.command("抽奖", alias={"lottery", "摇奖"})
    async def lottery(self, event: AstrMessageEvent, count: int = 1):
        """抽奖：/抽奖 [次数]，消耗积分抽取奖池里的奖品"""
        if not self.config.get("lottery_enabled", True):
            yield event.plain_result("⚠️ 抽奖功能未开启")
            return
        if count < 1:
            count = 1
        if count > 100:
            count = 100  # 单次上限保护

        cost = self._config_int("lottery_default_cost", 10, 0)
        total_cost = cost * count
        max_count = self._config_int("lottery_daily_max_count", 50, 0, 100000)

        platform, user_id = self._user_key(event)
        nickname = event.get_sender_name()

        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("BEGIN IMMEDIATE")
            user = await self._get_or_create_user(db, platform, user_id, nickname)
            if user["points"] < total_cost:
                await db.rollback()
                yield event.plain_result(
                    f"⚠️ 积分不足！{count} 次需 {total_cost} 积分，当前 {user['points']}"
                )
                return

            # 每日次数限制
            if max_count > 0:
                today_start = datetime.now().strftime("%Y-%m-%d") + " 00:00:00"
                cur = await db.execute(
                    "SELECT COUNT(*) FROM lottery_log WHERE user_id = ? AND created_at >= ?",
                    (user_id, today_start),
                )
                used = (await cur.fetchone())[0]
                if used + count > max_count:
                    await db.rollback()
                    yield event.plain_result(
                        f"⚠️ 今日已抽 {used}/{max_count} 次，剩余可抽 {max_count - used} 次"
                    )
                    return

            # 扣积分（写流水）
            await self._add_points(
                db, user_id, -total_cost, "lottery",
                description=f"抽奖 {count} 次 -{total_cost}",
            )

            # 抽 N 次
            results = [self._spin_lottery() for _ in range(count)]
            total_reward = sum(p["reward"] for p in results)

            # 写抽奖记录
            for p in results:
                await db.execute(
                    """INSERT INTO lottery_log
                       (user_id, prize_name, reward, cost)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, p["name"], p["reward"], cost),
                )

            # 发奖励
            if total_reward > 0:
                await self._add_points(
                    db, user_id, total_reward, "lottery",
                    description=f"抽奖奖励 +{total_reward}",
                )

            await db.commit()
            cur = await db.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            cur_points = row[0] if row else 0

        # 渲染结果
        lines = [
            f"🎰 抽奖结果（{count} 次）",
            "━━━━━━━━━━━━━━",
            f"💸 消耗：{total_cost} 积分",
        ]
        for i, p in enumerate(results, 1):
            sign = "+" if p["reward"] >= 0 else ""
            lines.append(f"  {i}. {p['name']} ({sign}{p['reward']})")
        lines.append("━━━━━━━━━━━━━━")
        if total_reward > 0:
            lines.append(f"🎁 总奖励：+{total_reward} 积分")
        else:
            lines.append("💔 很遗憾，没有获得积分")
        net = total_reward - total_cost
        sign = "+" if net >= 0 else ""
        lines.append(f"📊 净收益：{sign}{net}")
        lines.append(f"💎 当前余额：{cur_points}")
        yield event.plain_result("\n".join(lines))

    @filter.command("抽奖记录", alias={"lottery_log", "摇奖记录"})
    async def lottery_records(self, event: AstrMessageEvent, page: int = 1):
        """查询个人抽奖记录"""
        if page < 1:
            page = 1
        per_page = self._config_int("records_per_page", 8, 1, 100)
        offset = (page - 1) * per_page
        _, user_id = self._user_key(event)
        nickname = event.get_sender_name()

        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM lottery_log WHERE user_id = ?", (user_id,)
            )
            total = (await cur.fetchone())[0]
            cur = await db.execute(
                """SELECT prize_name, reward, cost, created_at
                   FROM lottery_log WHERE user_id = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (user_id, per_page, offset),
            )
            rows = await cur.fetchall()

        if not rows:
            yield event.plain_result("📭 暂无抽奖记录")
            return

        lines = [f"🎰 {nickname} 的抽奖记录 (第 {page} 页)\n━━━━━━━━━━━━━━"]
        for r in rows:
            name, reward, cost, ts = r
            sign = "+" if reward >= 0 else ""
            ts_short = (ts or "")[:16]
            lines.append(f"{ts_short}  {name}\n   消耗 {cost}  |  奖励 {sign}{reward}")
        total_pages = max(1, (total + per_page - 1) // per_page)
        lines.append(f"━━━━━━━━━━━━━━\n第 {page}/{total_pages} 页，共 {total} 条")
        yield event.plain_result("\n".join(lines))

    @filter.command("奖池", alias={"lotterypool", "抽奖池", "奖池预览"})
    async def show_pool(self, event: AstrMessageEvent):
        """查看当前奖池配置"""
        pool = self.lottery_pool
        total_weight = sum(p["weight"] for p in pool)
        lines = ["🎰 当前奖池\n━━━━━━━━━━━━━━"]
        for p in pool:
            prob = p["weight"] / total_weight * 100 if total_weight > 0 else 0
            sign = "+" if p["reward"] >= 0 else ""
            lines.append(
                f"  {p['name']}\n   权重 {p['weight']}  |  概率 {prob:.2f}%  |  奖励 {sign}{p['reward']}"
            )
        cost = self._config_int("lottery_default_cost", 10, 0)
        max_count = self._config_int("lottery_daily_max_count", 50, 0, 100000)
        lines.append(f"━━━━━━━━━━━━━━\n💸 每次消耗：{cost} 积分")
        if max_count > 0:
            lines.append(f"📊 每日上限：{max_count} 次")
        yield event.plain_result("\n".join(lines))

    # ============================================================
    # 指令：管理员指令
    # ============================================================
    @filter.command("addpoints", alias={"加分"})
    async def admin_add(self, event: AstrMessageEvent, target: str = "", amount: int = 0, *, reason: str = ""):
        """管理员加分：/addpoints @某人 数量 [原因]"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有管理员权限")
            return
        target_id = await self._resolve_target_user_id(event, target) if target else ""
        if not target_id or amount <= 0:
            if not target_id and target:
                yield event.plain_result(
                    "❌ 找不到该用户，请确认对方已签到过"
                )
            else:
                yield event.plain_result("⚠️ 用法: /addpoints @某人 正整数数量 [原因]")
            return
        platform, sender_id = self._user_key(event)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            # 已存在用户时不要把真实昵称覆盖为用户 ID；仅为新用户创建档案。
            cur = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (target_id,))
            if not await cur.fetchone():
                await self._get_or_create_user(db, platform, target_id, target_id)
            await self._add_points(
                db, target_id, amount, "admin_add",
                related_user_id=sender_id,
                description=reason or f"管理员 {event.get_sender_name()} 调整",
            )
            await db.commit()
        sign = "+" if amount > 0 else ""
        yield event.plain_result(f"✅ 已为 {target_id} {sign}{amount} 积分 (原因: {reason or '无'})")

    @filter.command("reducepoints", alias={"减分", "扣除积分"})
    async def admin_reduce(self, event: AstrMessageEvent, target: str = "", amount: int = 0, *, reason: str = ""):
        """管理员减分：/reducepoints @某人 数量 [原因]"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有管理员权限")
            return
        target_id = await self._resolve_target_user_id(event, target) if target else ""
        if not target_id or amount <= 0:
            if not target_id and target:
                yield event.plain_result(
                    "❌ 找不到该用户，请确认对方已签到过"
                )
            else:
                yield event.plain_result("⚠️ 用法: /reducepoints @某人 数量 [原因]")
            return
        platform, sender_id = self._user_key(event)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute("SELECT points FROM users WHERE user_id = ?", (target_id,))
            row = await cur.fetchone()
            if not row:
                yield event.plain_result("⚠️ 用户不存在")
                return
            if row[0] < amount:
                yield event.plain_result(f"⚠️ 用户积分不足，当前 {row[0]}")
                return
            await self._add_points(
                db, target_id, -amount, "admin_reduce",
                related_user_id=sender_id,
                description=reason or f"管理员 {event.get_sender_name()} 扣除",
            )
            await db.commit()
        yield event.plain_result(f"✅ 已扣除 {target_id} {amount} 积分 (原因: {reason or '无'})")

    @filter.command("setsign", alias={"setsignin", "设置签到", "改签到"})
    async def admin_set_sign(self, event: AstrMessageEvent, target: str = "", field: str = "", value: str = ""):
        """管理员设置签到数据
        用法:
          /setsign @人 累计 30     - 设置累计签到
          /setsign @人 连续 7      - 设置当前连续天数
          /setsign @人 最长 10     - 设置历史最长连续
          /setsign @人 上次 2024-01-01  - 设置上次签到日期
          /setsign @人 上次 空     - 清空上次签到
          /setsign @人             - 查看用户签到数据
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有管理员权限")
            return
        target_id = await self._resolve_target_user_id(event, target) if target else ""
        if not target_id:
            if not target_id and target:
                yield event.plain_result(
                    "❌ 找不到该用户，请确认对方已签到过"
                )
            else:
                yield event.plain_result(
                    "⚠️ 用法: /setsign @人 字段 值\n"
                    "字段: 累计 / 连续 / 最长 / 上次"
                )
            return
        platform, sender_id = self._user_key(event)

        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (target_id,))
            row = await cur.fetchone()
            if not row:
                await self._get_or_create_user(db, platform, target_id, target_id)
                cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (target_id,))
                row = await cur.fetchone()

            # 无字段参数：查看用户签到数据
            if not field:
                nickname = row[2] or target_id
                yield event.plain_result(
                    f"📋 {nickname} (ID: {target_id}) 的签到数据\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"💎 当前积分：{row[3]}\n"
                    f"📅 累计签到：{row[5]} 次\n"
                    f"🔥 当前连续：{row[6]} 天\n"
                    f"🏆 最高连续：{row[7]} 天\n"
                    f"📆 上次签到：{row[8] or '未签到'}"
                )
                return

            field = str(field).strip()
            val = str(value).strip()
            admin_name = event.get_sender_name()

            try:
                if field in ("累计", "total", "total_sign", "总"):
                    new_val = int(val)
                    if new_val < 0:
                        raise ValueError
                    await db.execute(
                        "UPDATE users SET total_sign = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (new_val, target_id),
                    )
                    msg = f"✅ 已将 {target_id} 累计签到设为 {new_val}"
                elif field in ("连续", "continuous", "continuous_sign"):
                    new_val = int(val)
                    if new_val < 0:
                        raise ValueError
                    await db.execute(
                        "UPDATE users SET continuous_sign = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (new_val, target_id),
                    )
                    msg = f"✅ 已将 {target_id} 当前连续设为 {new_val}"
                elif field in ("最长", "max", "max_continuous_sign", "历史最长"):
                    new_val = int(val)
                    if new_val < 0:
                        raise ValueError
                    await db.execute(
                        "UPDATE users SET max_continuous_sign = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                        (new_val, target_id),
                    )
                    msg = f"✅ 已将 {target_id} 最高连续设为 {new_val}"
                elif field in ("上次", "last", "last_sign_date", "日期"):
                    if val.lower() in ("none", "空", "清空", "无", "null", ""):
                        await db.execute(
                            "UPDATE users SET last_sign_date = '', updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                            (target_id,),
                        )
                        msg = f"✅ 已清空 {target_id} 的上次签到日期"
                    else:
                        try:
                            datetime.strptime(val, "%Y-%m-%d")
                        except ValueError:
                            yield event.plain_result("⚠️ 日期格式错误，应为 YYYY-MM-DD（如 2024-01-01）")
                            return
                        await db.execute(
                            "UPDATE users SET last_sign_date = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                            (val, target_id),
                        )
                        msg = f"✅ 已将 {target_id} 上次签到设为 {val}"
                else:
                    yield event.plain_result(
                        f"⚠️ 未知字段: {field}\n支持: 累计 / 连续 / 最长 / 上次"
                    )
                    return

                # 写一条操作流水
                await self._add_points(
                    db, target_id, 0, "admin_set_sign",
                    related_user_id=sender_id,
                    description=f"管理员 {admin_name} 设置 {field}={val}",
                )
                await db.commit()
                yield event.plain_result(msg)
            except ValueError:
                yield event.plain_result("⚠️ 数值格式错误，请输入合法的整数或日期")

    @filter.command("清零", alias={"reset", "resetuser", "重置"})
    async def admin_reset(self, event: AstrMessageEvent, target: str = "", scope: str = "all"):
        """清零指令（仅管理员）
        用法:
          /清零 @人 all        - 清零所有（积分+签到+流水）
          /清零 @人 points     - 仅清零积分
          /清零 @人 signin     - 仅清零签到（累计/连续/最长/上次）
          /清零 @人 continuous - 仅重置连续天数（保留累计和最高连续）
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有管理员权限")
            return
        target_id = await self._resolve_target_user_id(event, target) if target else ""
        if not target_id:
            if not target_id and target:
                yield event.plain_result(
                    "❌ 找不到该用户，请确认对方已签到过"
                )
            else:
                yield event.plain_result(
                    "⚠️ 用法: /清零 @人 [范围]\n"
                    "范围: all / points / signin / continuous"
                )
            return
        scope = (scope or "all").strip().lower()
        platform, sender_id = self._user_key(event)
        admin_name = event.get_sender_name()

        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (target_id,))
            row = await cur.fetchone()
            if not row:
                yield event.plain_result("⚠️ 用户不存在（没有签到记录）")
                return

            old_points = row[3]
            old_total = row[5]
            old_continuous = row[6]
            old_max = row[7]
            old_last = row[8] or ""

            if scope in ("all", "全部", "all"):
                await db.execute(
                    """UPDATE users SET
                       points = 0,
                       total_sign = 0,
                       continuous_sign = 0,
                       max_continuous_sign = 0,
                       last_sign_date = '',
                       updated_at = CURRENT_TIMESTAMP
                       WHERE user_id = ?""",
                    (target_id,),
                )
                await db.execute("DELETE FROM sign_log WHERE user_id = ?", (target_id,))
                await db.execute("DELETE FROM lottery_log WHERE user_id = ?", (target_id,))
                await self._add_points(
                    db, target_id, 0, "admin_reset",
                    related_user_id=sender_id,
                    description=f"管理员 {admin_name} 全清零（积分{old_points}→0, 签到{old_total}→0）",
                    log_amount=-old_points,
                )
                msg = (
                    f"✅ 已全清零 {target_id}\n"
                    f"   积分：{old_points} → 0\n"
                    f"   累计签到：{old_total} → 0\n"
                    f"   连续签到：{old_continuous} → 0\n"
                    f"   最高连续：{old_max} → 0\n"
                    f"   上次签到：{old_last or '空'} → 空\n"
                    f"   流水：保留（可在积分记录里查）"
                )
            elif scope in ("points", "积分"):
                if old_points == 0:
                    yield event.plain_result(f"ℹ️ {target_id} 当前积分已经是 0，无需清零")
                    return
                await db.execute(
                    "UPDATE users SET points = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (target_id,),
                )
                await self._add_points(
                    db, target_id, 0, "admin_reset",
                    related_user_id=sender_id,
                    description=f"管理员 {admin_name} 清零积分（{old_points}→0）",
                    log_amount=-old_points,
                )
                msg = f"✅ 已清零 {target_id} 的积分：{old_points} → 0"
            elif scope in ("signin", "签到", "签到数据"):
                await db.execute(
                    """UPDATE users SET
                       total_sign = 0,
                       continuous_sign = 0,
                       max_continuous_sign = 0,
                       last_sign_date = '',
                       updated_at = CURRENT_TIMESTAMP
                       WHERE user_id = ?""",
                    (target_id,),
                )
                await db.execute("DELETE FROM sign_log WHERE user_id = ?", (target_id,))
                await self._add_points(
                    db, target_id, 0, "admin_reset",
                    related_user_id=sender_id,
                    description=f"管理员 {admin_name} 清零签到数据",
                )
                msg = (
                    f"✅ 已清零 {target_id} 的签到数据\n"
                    f"   累计签到：{old_total} → 0\n"
                    f"   连续签到：{old_continuous} → 0\n"
                    f"   最高连续：{old_max} → 0"
                )
            elif scope in ("continuous", "连续", "连续天数"):
                if old_continuous == 0:
                    yield event.plain_result(f"ℹ️ {target_id} 当前连续天数已经是 0")
                    return
                await db.execute(
                    "UPDATE users SET continuous_sign = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                    (target_id,),
                )
                await self._add_points(
                    db, target_id, 0, "admin_reset",
                    related_user_id=sender_id,
                    description=f"管理员 {admin_name} 重置连续天数（{old_continuous}→0）",
                )
                msg = (
                    f"✅ 已重置 {target_id} 的连续天数：{old_continuous} → 0\n"
                    f"   （累计 {old_total} 和最高 {old_max} 保持不变）"
                )
            else:
                yield event.plain_result(
                    f"⚠️ 未知范围: {scope}\n支持: all / points / signin / continuous"
                )
                return

            await db.commit()
        yield event.plain_result(msg)

    # ============================================================
    # 指令：帮助
    # ============================================================
    @filter.command("签到帮助", alias={"积分帮助", "signhelp", "帮助"})
    async def help_cmd(self, event: AstrMessageEvent):
        """帮助（by 小星萤）"""
        yield event.plain_result(
            "📖 积分签到插件 v1.3.2\n"
            "━━━━━━━━━━━━━━\n"
            "🎁 /签到 - 每日签到\n"
            "💰 /积分 [@人] - 查积分\n"
            "📒 /积分记录 [页码] - 积分流水\n"
            "🏆 /排行榜 - 积分榜（图片）\n"
            "📅 /签到榜 - 签到榜（图片）\n"
            "📆 /签到日历 [@人] - 签到日历（图片）\n"
            "💸 /转账 @人 数量 - 转账\n"
            "🩹 /补签 - 补签（消耗积分）\n"
            "🎰 /抽奖 [次数] - 抽奖\n"
            "📋 /抽奖记录 - 抽奖流水\n"
            "🎁 /奖池 - 查看奖池\n"
            "🛒 /商店 - 查看积分商城\n"
            "💰 /购买 <ID> - 兑换商品\n"
            "🎫 /我的兑换 - 我的兑换历史\n"
            "━━━━━━━━━━━━━━\n"
            "🛠 管理员指令（需在配置中加白名单）：\n"
            "  /addpoints @人 数量 原因\n"
            "  /reducepoints @人 数量 原因\n"
            "  /setsign @人 字段 值\n"
            "    字段: 累计 / 连续 / 最长 / 上次\n"
            "  /清零 @人 [范围]\n"
            "    范围: all / points / signin / continuous\n"
            "  /商品上架 名字 | 价格 | 库存 | 描述 | 兑换\n"
            "  /商品下架 <ID> - 下架商品\n"
            "  /商品改价 <ID> | 新价 | 新库存 | 新说明\n"
            "  /补发 <订单ID> - 重发兑换说明"
        )

    async def terminate(self):
        """插件卸载/停用时调用"""
        logger.info("[signin] 插件已卸载")

    # ============================================================
    # 指令：签到日历（图片）
    # ============================================================
    @filter.command("签到日历", alias={"calendar", "我的签到", "日历"})
    async def signin_calendar(self, event: AstrMessageEvent, target: str = ""):
        """签到日历：最近 N 天格子视图（图片）"""
        days = self._config_int("signin_calendar_days", 30, 1, 366)
        platform, sender_id = self._user_key(event)
        if target:
            query_id = await self._resolve_target_user_id(event, target)
            if not query_id:
                yield event.plain_result(
                    "❌ 找不到该用户，请确认对方已签到过；\n"
                    "   或使用 @QQ号 / 长按头像@ 重新尝试"
                )
                return
        else:
            query_id = sender_id
        nickname = event.get_sender_name() if not target else ""

        async with aiosqlite.connect(str(DB_PATH)) as db:
            # 查用户
            cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (query_id,))
            user_row = await cur.fetchone()
            if not user_row:
                yield event.plain_result(f"❌ 用户 {query_id} 还没有签到记录哦")
                return
            db_nickname = user_row[2] or query_id
            continuous = user_row[6]
            max_continuous = user_row[7]
            total_sign = user_row[5]

            # 查最近 N 天的签到
            cur = await db.execute(
                """SELECT sign_date, is_makeup FROM sign_log
                   WHERE user_id = ? AND sign_date >= date('now', ?)""",
                (query_id, f"-{days} days"),
            )
            rows = await cur.fetchall()

        nickname = db_nickname
        signed_map = {}
        makeup_map = {}
        for ds, is_makeup in rows:
            if is_makeup:
                makeup_map[ds] = True
            else:
                signed_map[ds] = True

        # 优先转图片
        if self.config.get("leaderboard_image_enabled", True):
            try:
                html_content = self._format_calendar_html(
                    nickname, days, signed_map, makeup_map,
                    continuous, max_continuous, total_sign,
                )
                url = await self.html_render(html_content, {})
                yield event.image_result(url)
                return
            except Exception as e:
                logger.warning(f"[signin] 签到日历转图片失败，降级为文字: {e}")
        # 文字版降级
        from datetime import date
        today = date.today()
        day_list = [today - timedelta(days=days - 1 - i) for i in range(days)]
        signed_count = sum(1 for d in day_list if d.strftime("%Y-%m-%d") in signed_map)
        makeup_count = sum(1 for d in day_list if d.strftime("%Y-%m-%d") in makeup_map)
        rate = round(signed_count / days * 100, 1) if days > 0 else 0
        lines = [f"📅 {nickname} 的签到日历 (最近 {days} 天)\n━━━━━━━━━━━━━━"]
        # 5 个一行
        for i in range(0, len(day_list), 5):
            row_cells = []
            for d in day_list[i:i + 5]:
                ds = d.strftime("%Y-%m-%d")
                if ds in makeup_map:
                    row_cells.append(f"{d.strftime('%m/%d')}🩹")
                elif ds in signed_map:
                    row_cells.append(f"{d.strftime('%m/%d')}✅")
                else:
                    row_cells.append(f"{d.strftime('%m/%d')}⬜")
            lines.append("  ".join(row_cells))
        lines.append(f"━━━━━━━━━━━━━━\n✅ 已签 {signed_count}/{days}（{rate}%）  🩹 补签 {makeup_count}\n🔥 当前连续 {continuous} 天  🏆 最高 {max_continuous} 天")
        yield event.plain_result("\n".join(lines))

    # ============================================================
    # 指令：积分商城（用户）
    # ============================================================
    @filter.command("商店", alias={"shop", "商城", "积分商城"})
    async def shop_list(self, event: AstrMessageEvent):
        """查看商城商品列表"""
        if not self.config.get("shop_enabled", True):
            yield event.plain_result("⚠️ 积分商城未开启")
            return
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute(
                """SELECT id, name, cost, description, stock
                   FROM shop_items WHERE enabled = 1
                   ORDER BY cost ASC, id ASC"""
            )
            rows = await cur.fetchall()
        if not rows:
            yield event.plain_result("🛒 商城暂无商品\n管理员可在 WebUI 配置「默认商品列表」添加默认商品")
            return
        lines = ["🛒 积分商城\n━━━━━━━━━━━━━━"]
        for item_id, name, cost, desc, stock in rows:
            if stock == -1:
                stock_text = "∞"
            elif stock <= 0 and stock != -1:
                stock_text = "已售罄"
            else:
                stock_text = f"剩 {stock}"
            desc_line = f"\n   📝 {desc}" if desc else ""
            lines.append(
                f"🆔 [{item_id}] {name}\n   💰 {cost} 积分  |  📦 {stock_text}{desc_line}"
            )
        lines.append("━━━━━━━━━━━━━━\n💡 用法: /购买 <商品ID>")
        yield event.plain_result("\n".join(lines))

    @filter.command("购买", alias={"buy", "兑换"})
    async def shop_buy(self, event: AstrMessageEvent, item_id_str: str = ""):
        """购买商城商品：/购买 1"""
        if not self.config.get("shop_enabled", True):
            yield event.plain_result("⚠️ 积分商城未开启")
            return
        if not item_id_str:
            yield event.plain_result("⚠️ 用法: /购买 <商品ID>\n例如: /购买 1")
            return
        try:
            item_id = int(self._extract_user_id(item_id_str))
        except (TypeError, ValueError):
            yield event.plain_result("⚠️ 商品ID必须是数字")
            return

        platform, sender_id = self._user_key(event)
        nickname = event.get_sender_name()

        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.execute("BEGIN IMMEDIATE")
            # 商品
            cur = await db.execute(
                "SELECT id, name, cost, description, stock, delivery, enabled "
                "FROM shop_items WHERE id = ? AND enabled = 1",
                (item_id,),
            )
            item = await cur.fetchone()
            if not item:
                await db.rollback()
                yield event.plain_result(f"❌ 商品 {item_id} 不存在或已下架")
                return
            item_id_db, name, cost, desc, stock, delivery, enabled = item
            # 库存检查
            if stock <= 0 and stock != -1:
                await db.rollback()
                yield event.plain_result(f"❌ 商品「{name}」已售罄")
                return
            # 用户
            await self._get_or_create_user(db, platform, sender_id, nickname)
            cur = await db.execute("SELECT points FROM users WHERE user_id = ?", (sender_id,))
            urow = await cur.fetchone()
            cur_points = urow[0] if urow else 0
            if cur_points < cost:
                await db.rollback()
                yield event.plain_result(
                    f"❌ 积分不足！需要 {cost}，你只有 {cur_points}"
                )
                return
            # 扣积分（写流水）
            await self._add_points(
                db, sender_id, -cost, "shop",
                related_user_id=str(item_id_db),
                description=f"购买商品「{name}」-{cost}",
            )
            # 扣库存
            if stock > 0:
                await db.execute(
                    "UPDATE shop_items SET stock = stock - 1 WHERE id = ?",
                    (item_id_db,),
                )
            # 生成订单
            order_id = self._generate_order_id()
            await db.execute(
                """INSERT INTO shop_purchases
                   (order_id, user_id, item_id, item_name, cost, delivery)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_id, sender_id, item_id_db, name, cost, delivery),
            )
            await db.commit()
            cur = await db.execute("SELECT points FROM users WHERE user_id = ?", (sender_id,))
            urow = await cur.fetchone()
            new_points = urow[0] if urow else 0

        msg = (
            f"✅ 购买成功！\n"
            f"━━━━━━━━━━━━━━\n"
            f"🛒 商品：{name}\n"
            f"💰 花费：{cost} 积分\n"
            f"💎 当前余额：{new_points}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🎫 订单号：`{order_id}`\n"
            f"📦 兑换说明：{delivery}"
        )
        yield event.plain_result(msg)

    @filter.command("我的兑换", alias={"myorders", "my_purchases", "兑换记录"})
    async def shop_my_orders(self, event: AstrMessageEvent, page: int = 1):
        """我的兑换历史"""
        if page < 1:
            page = 1
        per_page = self._config_int("records_per_page", 8, 1, 100)
        offset = (page - 1) * per_page
        _, user_id = self._user_key(event)
        nickname = event.get_sender_name()

        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM shop_purchases WHERE user_id = ?", (user_id,)
            )
            total = (await cur.fetchone())[0]
            cur = await db.execute(
                """SELECT order_id, item_name, cost, created_at
                   FROM shop_purchases WHERE user_id = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (user_id, per_page, offset),
            )
            rows = await cur.fetchall()

        if not rows:
            yield event.plain_result("📭 你还没有兑换记录")
            return
        lines = [f"🎫 {nickname} 的兑换记录 (第 {page} 页)\n━━━━━━━━━━━━━━"]
        for order_id, item_name, cost, ts in rows:
            ts_short = (ts or "")[:16]
            lines.append(f"{ts_short}  {item_name}  -{cost} 积分\n   订单: {order_id}")
        total_pages = max(1, (total + per_page - 1) // per_page)
        lines.append(f"━━━━━━━━━━━━━━\n第 {page}/{total_pages} 页，共 {total} 单")
        yield event.plain_result("\n".join(lines))

    # ============================================================
    # 指令：积分商城（管理员）
    # ============================================================
    @filter.command("商品上架", alias={"additem", "shop_add"})
    async def admin_shop_add(self, event: AstrMessageEvent, *, args: str = ""):
        """添加商品
        用法: /商品上架 名字 | 价格 | 库存 | 描述 | 兑换说明
        例: /商品上架 群专属头衔 | 500 | -1 | 7天 | 联系群主
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有管理员权限")
            return
        if not args:
            yield event.plain_result(
                "⚠️ 用法: /商品上架 名字 | 价格 | 库存 | 描述 | 兑换说明\n"
                "例: /商品上架 群专属头衔 | 500 | -1 | 7天 | 联系群主\n"
                "（价格必填，库存 -1 表示无限）"
            )
            return
        parts = [p.strip() for p in args.split("|")]
        if len(parts) < 2:
            yield event.plain_result("⚠️ 至少需要 名字 | 价格 两段")
            return
        name = parts[0]
        try:
            cost = int(parts[1])
        except ValueError:
            yield event.plain_result("⚠️ 价格必须是整数")
            return
        if cost < 0:
            yield event.plain_result("⚠️ 价格不能为负")
            return
        try:
            stock = int(parts[2]) if len(parts) >= 3 and parts[2] else -1
            if stock < -1:
                raise ValueError
        except ValueError:
            yield event.plain_result("⚠️ 库存必须为 -1（无限）或非负整数")
            return
        desc = parts[3] if len(parts) >= 4 else ""
        delivery = parts[4] if len(parts) >= 5 else "请联系管理员"

        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute(
                """INSERT INTO shop_items
                   (name, cost, description, stock, delivery, enabled)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (name, cost, desc, stock, delivery),
            )
            new_id = cur.lastrowid
            await db.commit()
        yield event.plain_result(
            f"✅ 商品已上架\n"
            f"━━━━━━━━━━━━━━\n"
            f"🆔 ID: {new_id}\n"
            f"📦 名称: {name}\n"
            f"💰 价格: {cost} 积分\n"
            f"📊 库存: {'无限' if stock == -1 else stock}\n"
            f"📝 描述: {desc or '(无)'}\n"
            f"🚚 兑换: {delivery}"
        )

    @filter.command("商品下架", alias={"removeitem", "shop_remove", "下架商品"})
    async def admin_shop_remove(self, event: AstrMessageEvent, item_id_str: str = ""):
        """下架商品（软删：enabled=0）"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有管理员权限")
            return
        if not item_id_str:
            yield event.plain_result("⚠️ 用法: /商品下架 <商品ID>")
            return
        try:
            item_id = int(self._extract_user_id(item_id_str))
        except (TypeError, ValueError):
            yield event.plain_result("⚠️ 商品ID必须是数字")
            return
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute("SELECT name, enabled FROM shop_items WHERE id = ?", (item_id,))
            row = await cur.fetchone()
            if not row:
                yield event.plain_result(f"❌ 商品 {item_id} 不存在")
                return
            name, enabled = row
            if enabled == 0:
                yield event.plain_result(f"ℹ️ 商品「{name}」已经是下架状态")
                return
            await db.execute("UPDATE shop_items SET enabled = 0 WHERE id = ?", (item_id,))
            await db.commit()
        yield event.plain_result(f"✅ 已下架商品「{name}」(ID: {item_id})")

    @filter.command("商品改价", alias={"edititem", "shop_edit", "改价"})
    async def admin_shop_edit(self, event: AstrMessageEvent, *, args: str = ""):
        """修改商品价格/库存/说明
        用法: /商品改价 <ID> | 新价格 | 新库存 | 新说明
        价格/库存/说明 可选，传 空 表示不修改
        例: /商品改价 1 | 300 | 20 | 限时优惠
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有管理员权限")
            return
        if not args:
            yield event.plain_result(
                "⚠️ 用法: /商品改价 <ID> | 新价格 | 新库存 | 新说明\n"
                "例: /商品改价 1 | 300 | 20 | 限时优惠"
            )
            return
        parts = [p.strip() for p in args.split("|")]
        if len(parts) < 2:
            yield event.plain_result("⚠️ 至少需要 ID | 新价格 两段")
            return
        try:
            item_id = int(self._extract_user_id(parts[0]))
        except (TypeError, ValueError):
            yield event.plain_result("⚠️ 商品ID必须是数字")
            return
        updates = []
        params = []
        # 价格
        if len(parts) >= 2 and parts[1]:
            try:
                new_cost = int(parts[1])
                if new_cost < 0:
                    yield event.plain_result("⚠️ 价格不能为负")
                    return
                updates.append("cost = ?")
                params.append(new_cost)
            except ValueError:
                yield event.plain_result("⚠️ 价格必须是整数")
                return
        # 库存
        if len(parts) >= 3 and parts[2]:
            try:
                new_stock = int(parts[2])
                if new_stock < -1:
                    raise ValueError
                updates.append("stock = ?")
                params.append(new_stock)
            except ValueError:
                yield event.plain_result("⚠️ 库存必须为 -1（无限）或非负整数")
                return
        # 说明
        if len(parts) >= 4 and parts[3]:
            updates.append("delivery = ?")
            params.append(parts[3])
        if not updates:
            yield event.plain_result("⚠️ 没有要修改的字段")
            return
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute("SELECT name FROM shop_items WHERE id = ?", (item_id,))
            row = await cur.fetchone()
            if not row:
                yield event.plain_result(f"❌ 商品 {item_id} 不存在")
                return
            name = row[0]
            params.append(item_id)
            await db.execute(f"UPDATE shop_items SET {', '.join(updates)} WHERE id = ?", params)
            await db.commit()
        yield event.plain_result(f"✅ 已修改商品「{name}」(ID: {item_id})")

    @filter.command("补发", alias={"resend", "redeem"})
    async def admin_resend(self, event: AstrMessageEvent, order_id: str = ""):
        """管理员重发兑换说明：/补发 <订单ID>"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有管理员权限")
            return
        if not order_id:
            yield event.plain_result("⚠️ 用法: /补发 <订单ID>\n例如: /补发 ORD20250101120000ABCD")
            return
        async with aiosqlite.connect(str(DB_PATH)) as db:
            cur = await db.execute(
                """SELECT order_id, user_id, item_name, cost, delivery, created_at
                   FROM shop_purchases WHERE order_id = ?""",
                (order_id,),
            )
            row = await cur.fetchone()
            if not row:
                yield event.plain_result(f"❌ 订单 {order_id} 不存在")
                return
            o_id, uid, item_name, cost, delivery, ts = row
            cur = await db.execute("SELECT nickname FROM users WHERE user_id = ?", (uid,))
            u = await cur.fetchone()
            nick = u[0] if u and u[0] else uid
        yield event.plain_result(
            f"📨 兑换信息（订单 {o_id}）\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 用户：{nick} ({uid})\n"
            f"🛒 商品：{item_name}\n"
            f"💰 花费：{cost} 积分\n"
            f"⏰ 时间：{ts}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🚚 兑换说明：{delivery}"
        )

