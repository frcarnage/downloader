import os
import json
import sqlite3
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Set, Tuple

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"


def today_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = str(db_path)
        self._lock = asyncio.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def init_db(self):
        async with self._lock:
            await asyncio.to_thread(self._sync_init_and_migrate)

    def _sync_init_and_migrate(self):
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")

            cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                joined TEXT DEFAULT '',
                downloads INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                preferred_quality TEXT DEFAULT 'ask',
                referred_by INTEGER DEFAULT 0,
                referral_count INTEGER DEFAULT 0,
                is_vip INTEGER DEFAULT 0,
                lang TEXT DEFAULT 'en'
            );
            """)

            # Add columns if upgrading from earlier table
            existing_cols = [r["name"] for r in cur.execute("PRAGMA table_info(users);").fetchall()]
            if "referred_by" not in existing_cols:
                cur.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT 0;")
            if "referral_count" not in existing_cols:
                cur.execute("ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0;")
            if "is_vip" not in existing_cols:
                cur.execute("ALTER TABLE users ADD COLUMN is_vip INTEGER DEFAULT 0;")
            if "lang" not in existing_cols:
                cur.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'en';")
            if "vip_until" not in existing_cols:
                cur.execute("ALTER TABLE users ADD COLUMN vip_until TEXT DEFAULT '';")
            if "bonus_quota" not in existing_cols:
                cur.execute("ALTER TABLE users ADD COLUMN bonus_quota INTEGER DEFAULT 0;")

            cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                users INTEGER DEFAULT 0,
                downloads INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS quality_stats (
                quality TEXT PRIMARY KEY,
                count INTEGER DEFAULT 0
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS user_daily_quota (
                user_id INTEGER,
                date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY(user_id, date)
            );
            """)

            # Default settings
            cur.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('maintenance', '0');")
            cur.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('ad_enabled', '0');")
            cur.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('ad_text', '📢 Join our channel for updates');")
            cur.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('ad_url', 'https://t.me/botupdatesor');")
            cur.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('autodelete_enabled', '1');")
            cur.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('autodelete_minutes', '30');")
            conn.commit()

    # ---------------- User Registration & Referrals ----------------
    async def register_user(self, user, referrer_id: int = 0) -> Tuple[bool, Optional[int]]:
        """
        Returns (is_new, unlocked_vip_referrer_id).
        If inviter hits 3 referrals, returns their user_id so bot can notify them!
        """
        uid = user.id
        uname = user.username or ""
        fname = user.first_name or ""
        now = datetime.utcnow().isoformat()
        detected_lang = (user.language_code or "en")[:2]
        if detected_lang not in ("en", "hi", "ru", "es", "uz", "ar"):
            detected_lang = "en"

        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT user_id, is_vip, referral_count FROM users WHERE user_id = ?", (uid,))
                row = cur.fetchone()
                if row is None:
                    # New user
                    ref_by = referrer_id if (referrer_id and referrer_id != uid) else 0
                    cur.execute("""
                    INSERT INTO users (user_id, username, first_name, joined, downloads, referred_by, lang)
                    VALUES (?, ?, ?, ?, 0, ?, ?)
                    """, (uid, uname, fname, now, ref_by, detected_lang))

                    day = today_key()
                    cur.execute("""
                    INSERT INTO daily_stats (date, users) VALUES (?, 1)
                    ON CONFLICT(date) DO UPDATE SET users = users + 1
                    """, (day,))

                    unlocked_vip_for = None
                    milestone_type = None
                    if ref_by > 0:
                        cur.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?", (ref_by,))
                        cur.execute("SELECT referral_count, is_vip FROM users WHERE user_id = ?", (ref_by,))
                        ref_row = cur.fetchone()
                        if ref_row:
                            rc = ref_row["referral_count"]
                            # Milestone 1: +3 bonus daily quota
                            if rc == 1:
                                cur.execute("UPDATE users SET bonus_quota = bonus_quota + 3 WHERE user_id = ?", (ref_by,))
                                milestone_type = "milestone_1"
                            # Milestone 3: 7 Days VIP
                            elif rc == 3:
                                from datetime import timedelta
                                exp = (datetime.utcnow() + timedelta(days=7)).isoformat()
                                cur.execute("UPDATE users SET is_vip = 1, vip_until = ? WHERE user_id = ?", (exp, ref_by))
                                unlocked_vip_for = ref_by
                                milestone_type = "milestone_3"
                            # Milestone 10: Lifetime VIP
                            elif rc >= 10 and not ref_row["is_vip"]:
                                cur.execute("UPDATE users SET is_vip = 1, vip_until = 'lifetime' WHERE user_id = ?", (ref_by,))
                                unlocked_vip_for = ref_by
                                milestone_type = "milestone_10"

                    conn.commit()
                    return True, unlocked_vip_for, milestone_type
                else:
                    cur.execute("UPDATE users SET username = ?, first_name = ? WHERE user_id = ?", (uname, fname, uid))
                    conn.commit()
                    return False, None, None

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                row = cur.fetchone()
                return dict(row) if row else None

        return await asyncio.to_thread(_sync)

    async def get_all_user_ids(self) -> List[int]:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM users WHERE is_banned = 0")
                return [r["user_id"] for r in cur.fetchall()]

        return await asyncio.to_thread(_sync)

    async def get_total_users_count(self) -> int:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) as cnt FROM users")
                return cur.fetchone()["cnt"]

        return await asyncio.to_thread(_sync)

    async def increment_user_downloads(self, user_id: int, quality: str = "720"):
        day = today_key()
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO users (user_id, downloads) VALUES (?, 1)
                ON CONFLICT(user_id) DO UPDATE SET downloads = downloads + 1
                """, (user_id,))
                cur.execute("""
                INSERT INTO daily_stats (date, downloads) VALUES (?, 1)
                ON CONFLICT(date) DO UPDATE SET downloads = downloads + 1
                """, (day,))
                cur.execute("""
                INSERT INTO quality_stats (quality, count) VALUES (?, 1)
                ON CONFLICT(quality) DO UPDATE SET count = count + 1
                """, (quality,))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    async def record_error(self):
        day = today_key()
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO daily_stats (date, errors) VALUES (?, 1)
                ON CONFLICT(date) DO UPDATE SET errors = errors + 1
                """, (day,))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    # ---------------- Daily Quota System (3 Free/Day + Bonus, Unlimited for VIP) ----------------
    async def check_daily_quota(self, user_id: int) -> Tuple[bool, int, int, bool]:
        """
        Returns (can_download, current_usage, max_limit, is_vip).
        Free users: limit 3 + bonus_quota. VIP users: unlimited.
        """
        is_vip = await self.is_vip(user_id)
        if is_vip:
            return True, 0, 999999, True

        day = today_key()
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT bonus_quota FROM users WHERE user_id = ?", (user_id,))
                user_row = cur.fetchone()
                bonus = user_row["bonus_quota"] if (user_row and "bonus_quota" in user_row.keys()) else 0
                limit = 3 + (bonus or 0)

                cur.execute("SELECT count FROM user_daily_quota WHERE user_id = ? AND date = ?", (user_id, day))
                row = cur.fetchone()
                usage = row["count"] if row else 0
                return usage < limit, usage, limit, False

        can_dl, usage, limit, vip = await asyncio.to_thread(_sync)
        return can_dl, usage, limit, vip

    async def increment_daily_quota(self, user_id: int):
        day = today_key()
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO user_daily_quota (user_id, date, count) VALUES (?, ?, 1)
                ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1
                """, (user_id, day))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    # ---------------- VIP & Referrals ----------------
    async def is_vip(self, user_id: int) -> bool:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT is_vip, is_admin, vip_until FROM users WHERE user_id = ?", (user_id,))
                row = cur.fetchone()
                if not row:
                    return False
                if row["is_admin"]:
                    return True
                if row["is_vip"]:
                    vip_until = row["vip_until"] if "vip_until" in row.keys() else ""
                    if not vip_until or vip_until == "lifetime":
                        return True
                    try:
                        exp = datetime.fromisoformat(vip_until)
                        if exp > datetime.utcnow():
                            return True
                        else:
                            cur.execute("UPDATE users SET is_vip = 0, vip_until = '' WHERE user_id = ?", (user_id,))
                            conn.commit()
                            return False
                    except Exception:
                        return True
                return False

        return await asyncio.to_thread(_sync)

    async def set_vip(self, user_id: int, is_vip: bool, days: int = 0, is_lifetime: bool = False):
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                val = 1 if is_vip else 0
                until_val = ""
                if is_vip:
                    if is_lifetime:
                        until_val = "lifetime"
                    elif days > 0:
                        from datetime import timedelta
                        until_val = (datetime.utcnow() + timedelta(days=days)).isoformat()
                    else:
                        until_val = "lifetime"

                cur.execute("""
                INSERT INTO users (user_id, is_vip, vip_until) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET is_vip = ?, vip_until = ?
                """, (user_id, val, until_val, val, until_val))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    async def get_referral_info(self, user_id: int) -> Tuple[int, bool, str, int]:
        """Returns (referral_count, is_vip, vip_until, bonus_quota)."""
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT referral_count, is_vip, vip_until, bonus_quota FROM users WHERE user_id = ?", (user_id,))
                row = cur.fetchone()
                if row:
                    return (
                        row["referral_count"] or 0,
                        bool(row["is_vip"]),
                        row["vip_until"] or "",
                        row["bonus_quota"] or 0
                    )
                return 0, False, "", 0

        return await asyncio.to_thread(_sync)

    # ---------------- Language / i18n ----------------
    async def get_lang(self, user_id: int) -> str:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT lang FROM users WHERE user_id = ?", (user_id,))
                row = cur.fetchone()
                return row["lang"] if (row and row["lang"]) else "en"

        return await asyncio.to_thread(_sync)

    async def set_lang(self, user_id: int, lang: str):
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO users (user_id, lang) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET lang = ?
                """, (user_id, lang, lang))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    # ---------------- Quality Preferences ----------------
    async def get_preferred_quality(self, user_id: int) -> str:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT preferred_quality FROM users WHERE user_id = ?", (user_id,))
                row = cur.fetchone()
                return row["preferred_quality"] if row and row["preferred_quality"] else "ask"

        return await asyncio.to_thread(_sync)

    async def set_preferred_quality(self, user_id: int, quality: str):
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO users (user_id, preferred_quality) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET preferred_quality = ?
                """, (user_id, quality, quality))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    # ---------------- Ban / Unban ----------------
    async def is_banned(self, user_id: int) -> bool:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
                row = cur.fetchone()
                return bool(row and row["is_banned"])

        return await asyncio.to_thread(_sync)

    async def ban_user(self, user_id: int) -> bool:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO users (user_id, is_banned) VALUES (?, 1)
                ON CONFLICT(user_id) DO UPDATE SET is_banned = 1
                """, (user_id,))
                conn.commit()
                return True

        async with self._lock:
            return await asyncio.to_thread(_sync)

    async def unban_user(self, user_id: int) -> bool:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
                conn.commit()
                return True

        async with self._lock:
            return await asyncio.to_thread(_sync)

    # ---------------- Admin Management ----------------
    async def get_admins(self, hardcoded_admins: Set[int]) -> Set[int]:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM users WHERE is_admin = 1")
                db_admins = {r["user_id"] for r in cur.fetchall()}
                return db_admins.union(hardcoded_admins)

        return await asyncio.to_thread(_sync)

    async def add_admin(self, user_id: int):
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                INSERT INTO users (user_id, is_admin) VALUES (?, 1)
                ON CONFLICT(user_id) DO UPDATE SET is_admin = 1
                """, (user_id,))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    # ---------------- Maintenance & Settings ----------------
    async def get_maintenance(self) -> bool:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT value FROM bot_settings WHERE key = 'maintenance'")
                row = cur.fetchone()
                return row and row["value"] == "1"

        return await asyncio.to_thread(_sync)

    async def set_maintenance(self, enabled: bool):
        val = "1" if enabled else "0"
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('maintenance', ?)", (val,))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    # ---------------- Auto-Delete Settings ----------------
    async def get_autodelete_settings(self) -> Tuple[bool, int]:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM bot_settings WHERE key IN ('autodelete_enabled', 'autodelete_minutes')")
                rows = {r["key"]: r["value"] for r in cur.fetchall()}
                enabled = rows.get("autodelete_enabled", "1") == "1"
                try:
                    mins = int(rows.get("autodelete_minutes", "30"))
                except ValueError:
                    mins = 30
                return enabled, mins

        return await asyncio.to_thread(_sync)

    async def set_autodelete_settings(self, enabled: bool, minutes: int = 30):
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('autodelete_enabled', ?)", ("1" if enabled else "0",))
                cur.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('autodelete_minutes', ?)", (str(minutes),))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    # ---------------- Sponsored Ad Settings ----------------
    async def get_ad_settings(self) -> Dict[str, Any]:
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM bot_settings WHERE key IN ('ad_enabled', 'ad_text', 'ad_url')")
                rows = {r["key"]: r["value"] for r in cur.fetchall()}
                return {
                    "enabled": rows.get("ad_enabled") == "1",
                    "text": rows.get("ad_text", ""),
                    "url": rows.get("ad_url", ""),
                }

        return await asyncio.to_thread(_sync)

    async def set_ad_settings(self, enabled: bool, text: str = "", url: str = ""):
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('ad_enabled', ?)", ("1" if enabled else "0",))
                if text:
                    cur.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('ad_text', ?)", (text,))
                if url:
                    cur.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('ad_url', ?)", (url,))
                conn.commit()

        async with self._lock:
            await asyncio.to_thread(_sync)

    # ---------------- Stats & Aggregations ----------------
    async def get_stats_summary(self) -> Dict[str, Any]:
        day = today_key()
        def _sync():
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) as total_users FROM users")
                total_users = cur.fetchone()["total_users"]

                cur.execute("SELECT COUNT(*) as banned FROM users WHERE is_banned = 1")
                banned_count = cur.fetchone()["banned"]

                cur.execute("SELECT COUNT(*) as vips FROM users WHERE is_vip = 1")
                vip_count = cur.fetchone()["vips"]

                cur.execute("SELECT SUM(downloads) as total_dl FROM users")
                row_dl = cur.fetchone()
                total_dl = row_dl["total_dl"] or 0

                cur.execute("SELECT SUM(errors) as total_err FROM daily_stats")
                row_err = cur.fetchone()
                total_err = row_err["total_err"] or 0

                cur.execute("SELECT * FROM daily_stats WHERE date = ?", (day,))
                today_row = cur.fetchone()
                today_stats = dict(today_row) if today_row else {"users": 0, "downloads": 0, "errors": 0}

                cur.execute("SELECT quality, count FROM quality_stats")
                by_quality = {r["quality"]: r["count"] for r in cur.fetchall()}

                return {
                    "total_users": total_users,
                    "banned_count": banned_count,
                    "vip_count": vip_count,
                    "total_downloads": total_dl,
                    "total_errors": total_err,
                    "today": today_stats,
                    "by_quality": by_quality,
                }

        return await asyncio.to_thread(_sync)


db = Database()
