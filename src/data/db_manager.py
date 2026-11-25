# -*- coding: utf-8 -*-
"""SQLite 数据库管理 — 爬取结果持久化存储"""

import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime

from ..core.file_log import log_info


def _default_db_file():
    """数据库位置：环境变量 > 打包后的用户数据目录 > 源码根目录。"""
    env_path = os.environ.get('DBP_DB_FILE')
    if env_path:
        return os.path.abspath(os.path.expanduser(env_path))
    if getattr(sys, 'frozen', False):
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'douban-post-crawler', 'douban_data.db')
    # src/data/db_manager.py → 上溯三级才是项目根目录
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'douban_data.db')


DB_FILE = _default_db_file()
_db_initialized = False
_db_init_lock = threading.Lock()


def _ensure_db():
    """惰性初始化数据库（首次调用时建表）

    只在真正成功后置位 _db_initialized；之前是先把标志置 True 再 init_db()，
    一旦建表抛异常（磁盘满/文件被占用/权限问题），后续调用会认为已初始化，
    数据库永远建不起来且不报错。
    """
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if _db_initialized:
            return
        init_db()
        _db_initialized = True


def _connect_raw():
    """创建连接；不触发 _ensure_db，避免初始化时重入非重入锁。"""
    os.makedirs(os.path.dirname(DB_FILE) or '.', exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL 下 synchronous=NORMAL：崩溃最多丢失最近若干已提交事务，但不会损坏库，
    # 同时避免每次提交都 fsync 造成的写入卡顿（init_db 也做同样设置）。
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _get_conn():
    _ensure_db()
    return _connect_raw()


def init_db():
    """建表（幂等）并启用 WAL 模式

    注意：这里必须用裸连接，不能走 _get_conn()；_ensure_db() 持有
    _db_init_lock 调本函数，再进 _ensure_db 会二次获取同一把非重入锁，
    导致进程内第一次数据库调用永久挂起。
    """
    os.makedirs(os.path.dirname(DB_FILE) or '.', exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        # 迁移期间关闭外键，兼容历史库里可能存在的孤儿行；迁移结束后再开启。
        conn.execute("PRAGMA foreign_keys=OFF")
        # 启用 WAL 模式，提高并发写入性能，避免 "database is locked" 错误
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        c = conn.cursor()
        _recover_images_migration(c)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS topics (
                id           INTEGER PRIMARY KEY,
                title        TEXT,
                author       TEXT,
                author_id    TEXT,
                group_name   TEXT,
                content      TEXT,
                abstract     TEXT,
                comments_count INTEGER,
                like_count   INTEGER,
                create_time  TEXT,
                scraped_at   TEXT,
                raw_json     TEXT
            );

            CREATE TABLE IF NOT EXISTS comments (
                rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id     INTEGER NOT NULL,
                floor        INTEGER,
                author       TEXT,
                author_id    TEXT,
                content      TEXT,
                text         TEXT,
                create_time  TEXT,
                ip_location  TEXT,
                likes        INTEGER DEFAULT 0,
                reply_to_floor INTEGER,
                reply_to_author TEXT,
                raw_json     TEXT,
                FOREIGN KEY (topic_id) REFERENCES topics(id)
            );

            CREATE TABLE IF NOT EXISTS images (
                rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id     INTEGER NOT NULL,
                url          TEXT NOT NULL,
                filename     TEXT,
                source       TEXT,          -- 'topic' | 'comment'
                comment_floor INTEGER,
                downloaded   INTEGER DEFAULT 0,
                local_path   TEXT,
                ocr_text     TEXT,          -- OCR 识别出的文字（NULL 表示未识别）
                ocr_at       TEXT,          -- OCR 识别时间
                stale        INTEGER NOT NULL DEFAULT 0,  -- 本次抓取未再出现（不删行，只标记）
                UNIQUE (topic_id, url),
                FOREIGN KEY (topic_id) REFERENCES topics(id)
            );
        """)
        conn.commit()

        # ---- 增量迁移：为旧库的 images 表补充新字段 ----
        _migrate_add_column(c, 'images', 'ocr_text', 'TEXT')
        _migrate_add_column(c, 'images', 'ocr_at', 'TEXT')
        # stale：本次抓取未再出现的旧图不删除，只标记失效（见 save_images）
        _migrate_add_column(c, 'images', 'stale', 'INTEGER NOT NULL DEFAULT 0')

        # ---- 增量迁移：topics 表补充 url（旧库没有这个字段，回填历史数据）----
        _migrate_topic_url(c)

        # ---- 增量迁移：images.url 由「全局 UNIQUE」改为「(topic_id, url) UNIQUE」----
        # 旧约束会让两个帖子里的同一张图只能存一行，配合 INSERT OR IGNORE
        # 直接变成跨帖去重，第二篇帖子的图片记录会挂到第一篇的 topic_id 上。
        _migrate_images_unique(c)

        # ---- 索引维护 ----
        # 必须放在表迁移之后：_migrate_images_unique 会重建 images 表，
        # 迁移前建在旧表上的索引会随旧表一起被删除。
        # idx_comments_author 几乎用不到（关键词搜索走 title/author LIKE 全表扫描），
        # 反而增加每条评论写入的维护成本，旧库直接清掉。
        c.executescript("""
            DROP INDEX IF EXISTS idx_comments_author;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_unique ON comments(topic_id, floor);
            CREATE INDEX IF NOT EXISTS idx_images_topic ON images(topic_id);
            CREATE INDEX IF NOT EXISTS idx_topics_scraped_at ON topics(scraped_at DESC);
            CREATE INDEX IF NOT EXISTS idx_images_downloaded ON images(downloaded);
        """)
        conn.commit()
        # 外键开关不能在事务内生效，提交后再打开。
        conn.execute("PRAGMA foreign_keys=ON")
    finally:
        conn.close()


def _migrate_add_column(cursor, table, column, col_type):
    """安全地为已有表添加字段（不存在时才添加）"""
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def topic_url(topic_id):
    """帖子在豆瓣的规范地址（老数据没有存 url 时用它兜底）"""

    return f'https://www.douban.com/group/topic/{_as_int(topic_id)}/'


def _migrate_topic_url(cursor):
    """给 topics 加 url 字段，并用 raw_json 里的原始地址回填历史记录"""

    cursor.execute("PRAGMA table_info(topics)")
    existing = {row[1] for row in cursor.fetchall()}
    added = 'url' not in existing
    if added:
        cursor.execute("ALTER TABLE topics ADD COLUMN url TEXT")
    cursor.execute("SELECT id, raw_json, url FROM topics WHERE url IS NULL OR url = ''")
    rows = cursor.fetchall()
    for tid, raw_json, url in rows:
        if url:
            continue
        candidate = ''
        try:
            candidate = (json.loads(raw_json or '{}') or {}).get('url') or ''
        except Exception:
            candidate = ''
        cursor.execute("UPDATE topics SET url=? WHERE id=?", (candidate or topic_url(tid), tid))


def _has_global_url_unique(cursor):
    """判断 images.url 上是否存在「单列全局 UNIQUE」索引"""
    cursor.execute("PRAGMA index_list(images)")
    for row in cursor.fetchall():
        # row: (seq, name, unique, origin, partial)
        name, is_unique = row[1], row[2]
        if not is_unique:
            continue
        cursor.execute(f"PRAGMA index_info({name})")
        cols = [r[2] for r in cursor.fetchall()]
        if cols == ['url']:
            return True
    return False


def _recover_images_migration(cursor):
    """修复上次迁移中断留下的 images_old_migration。"""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    if 'images_old_migration' not in tables:
        return
    if 'images' not in tables:
        cursor.execute("ALTER TABLE images_old_migration RENAME TO images")
        return

    # 两张表都在：把旧表里新表没有的行补回去，再删旧表。
    try:
        cursor.execute("PRAGMA table_info(images)")
        new_cols = [row[1] for row in cursor.fetchall()]
        cursor.execute("PRAGMA table_info(images_old_migration)")
        old_cols = {row[1] for row in cursor.fetchall()}
        cols = [col for col in new_cols if col in old_cols]
        if not cols:
            return
        col_sql = ', '.join(f'"{col}"' for col in cols)
        cursor.execute(
            f'INSERT OR IGNORE INTO images ({col_sql}) '
            f'SELECT {col_sql} FROM images_old_migration'
        )
        cursor.execute("DROP TABLE images_old_migration")
    except sqlite3.Error:
        # 迁移失败时保留旧表，下次启动继续尝试，不能直接丢弃数据。
        pass


def _migrate_images_unique(cursor):
    """把 images.url 的单列 UNIQUE 重建为 (topic_id, url) 复合 UNIQUE"""
    if not _has_global_url_unique(cursor):
        return
    # 旧索引名会被带到重命名后的旧表上，先删掉，否则新表建不了同名索引。
    cursor.execute("DROP INDEX IF EXISTS idx_images_topic")
    cursor.execute("ALTER TABLE images RENAME TO images_old_migration")
    cursor.execute("""
        CREATE TABLE images (
            rowid        INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id     INTEGER NOT NULL,
            url          TEXT NOT NULL,
            filename     TEXT,
            source       TEXT,
            comment_floor INTEGER,
            downloaded   INTEGER DEFAULT 0,
            local_path   TEXT,
            ocr_text     TEXT,
            ocr_at       TEXT,
            stale        INTEGER NOT NULL DEFAULT 0,
            UNIQUE (topic_id, url),
            FOREIGN KEY (topic_id) REFERENCES topics(id)
        )
    """)
    cursor.execute("""
        INSERT OR IGNORE INTO images
            (rowid, topic_id, url, filename, source, comment_floor,
             downloaded, local_path, ocr_text, ocr_at, stale)
        SELECT rowid, topic_id, url, filename, source, comment_floor,
               downloaded, local_path, ocr_text, ocr_at, stale
        FROM images_old_migration
    """)
    cursor.execute("DROP TABLE images_old_migration")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_images_topic ON images(topic_id)")


# ==================== 写入 ====================

def _as_int(value, default=0):
    """把 API 里的数字/“1.2万”等写法安全转成 int，失败用 default。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(',', '').replace('，', '')
    if not text:
        return default
    match = re.fullmatch(r'(-?\d+(?:\.\d+)?)\s*([万千wWkK]?)', text)
    if not match:
        return default
    number = float(match.group(1))
    unit = match.group(2)
    if unit in ('万', 'w', 'W'):
        number *= 10000
    elif unit in ('千', 'k', 'K'):
        number *= 1000
    return int(number)


def _save_topic_with_conn(cursor, topic_data):
    """帖子 upsert 的实际 SQL（不提交、不关连接，供原子保存复用），返回 topic_id"""
    tid = _as_int(topic_data.get('id', 0))
    author = topic_data.get('author', {})
    author_name = author.get('name', '') if isinstance(author, dict) else str(author)
    author_uid = author.get('uid', '') if isinstance(author, dict) else ''
    group_info = topic_data.get('group', {})
    group_name = group_info.get('name', '') if isinstance(group_info, dict) else ''

    scraped_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    raw_json = json.dumps(topic_data, ensure_ascii=False)

    cursor.execute("""
        INSERT INTO topics
            (id, title, author, author_id, group_name, content, abstract,
             comments_count, like_count, create_time, scraped_at, raw_json, url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, author=excluded.author,
            author_id=excluded.author_id, group_name=excluded.group_name,
            content=excluded.content, abstract=excluded.abstract,
            comments_count=excluded.comments_count, like_count=excluded.like_count,
            create_time=excluded.create_time, scraped_at=excluded.scraped_at,
            raw_json=excluded.raw_json, url=excluded.url
    """, (
        tid,
        topic_data.get('title', ''),
        author_name,
        author_uid,
        group_name,
        topic_data.get('content', ''),
        topic_data.get('abstract', ''),
        _as_int(topic_data.get('comments_count', 0)),
        _as_int(topic_data.get('like_count', 0)),
        topic_data.get('create_time', ''),
        scraped_at,
        raw_json,
        topic_data.get('url') or topic_url(tid),
    ))
    return tid


def save_topic(topic_data):
    """保存帖子，返回 topic_id"""
    conn = _get_conn()
    try:
        topic_id = _save_topic_with_conn(conn.cursor(), topic_data)
        conn.commit()
        return topic_id
    finally:
        conn.close()


def _save_comments_with_conn(cursor, topic_id, comments, replace=True, allow_shrink=False):
    """评论批量写入的实际 SQL（不提交、不关连接，供原子保存复用）

    replace=True 时带「缩水保护」：新评论数不足库中一半直接拒绝覆盖并抛
    RuntimeError（外层事务整体回滚），避免完整性误判把历史评论删光；降到
    80% 以下但未到一半时照常覆盖，只写日志提醒人工核对。
    """
    comments = list(comments or [])
    # 空列表绝不能触发 DELETE：调用方可能把状态误判成「抓取完整」而传入空数据，
    # replace=True 会先把该帖历史评论整批删掉，且无法恢复。
    if not comments:
        return
    if replace:
        cursor.execute("SELECT COUNT(*) FROM comments WHERE topic_id=?", (topic_id,))
        old_count = cursor.fetchone()[0] or 0
        if old_count > 0 and not allow_shrink:
            new_count = len(comments)
            if new_count < old_count * 0.5:
                raise RuntimeError(
                    f'新评论数 {new_count} 远少于库中 {old_count}，已拒绝覆盖')
            if new_count < old_count * 0.8:
                log_info(f'topic_id={topic_id} 评论覆盖数量明显减少：'
                         f'{old_count} 条 -> {new_count} 条（降幅超过 20%）'
                         f'，请核对是评论被删还是抓取不完整')
        cursor.execute("DELETE FROM comments WHERE topic_id=?", (topic_id,))
    else:
        # 没有楼层号的评论无法走唯一索引冲突，先清掉旧的 NULL 楼层行防止重复。
        cursor.execute("DELETE FROM comments WHERE topic_id=? AND floor IS NULL", (topic_id,))
    rows = []
    for cm in comments:
        author = cm.get('author', {})
        a_name = author.get('name', '') if isinstance(author, dict) else str(author)
        a_uid = author.get('uid', '') if isinstance(author, dict) else ''
        # 豆瓣 API 里「回复某条评论」的字段名是 ref_comment，且不带楼层；
        # 统一优先用 reply_to（scraper 已归一化并补好楼层），缺失时退回 ref_comment。
        reply_to = cm.get('reply_to') or cm.get('ref_comment')
        rt_floor = None
        rt_author = None
        if reply_to and isinstance(reply_to, dict):
            rt_floor = _as_int(reply_to.get('floor'), None)
            ra = reply_to.get('author', {})
            rt_author = ra.get('name', '') if isinstance(ra, dict) else ''
        rows.append((
            topic_id,
            _as_int(cm.get('floor'), None),
            a_name,
            a_uid,
            cm.get('content', ''),
            cm.get('text', ''),
            cm.get('create_time', cm.get('time', '')),
            cm.get('ip_location', ''),
            _as_int(cm.get('likes', 0)),
            rt_floor,
            rt_author,
            json.dumps(cm, ensure_ascii=False),
        ))
    cursor.executemany("""
        INSERT INTO comments
            (topic_id, floor, author, author_id, content, text,
             create_time, ip_location, likes, reply_to_floor, reply_to_author, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(topic_id, floor) DO UPDATE SET
            author=excluded.author, author_id=excluded.author_id,
            content=excluded.content, text=excluded.text,
            create_time=excluded.create_time, ip_location=excluded.ip_location,
            likes=excluded.likes, reply_to_floor=excluded.reply_to_floor,
            reply_to_author=excluded.reply_to_author, raw_json=excluded.raw_json
    """, rows)


def save_comments(topic_id, comments, replace=True, allow_shrink=False):
    """批量保存评论。

    replace=True：抓取完整（或明确要覆盖）时先清空该帖旧评论，避免已删除评论残留。
    replace=False：只抓到部分分页时合并写入，绝不删除已有完整评论。
    allow_shrink=True：显式允许 replace 覆盖后评论数大幅减少（跳过缩水保护）。
    """
    conn = _get_conn()
    try:
        _save_comments_with_conn(conn.cursor(), topic_id, comments,
                                 replace=replace, allow_shrink=allow_shrink)
        conn.commit()
    finally:
        conn.close()


def _save_images_with_conn(cursor, topic_id, image_urls, source='topic', floors=None):
    """图片记录写入的实际 SQL（不提交、不关连接，供原子保存复用）

    本次未再出现的同来源旧行不再删除（可能已被界面/OCR 引用），改标 stale=1；
    常规读取默认过滤 stale，图片在后续抓取中重新出现时 upsert 会把 stale 清回 0。
    """
    if image_urls is None:
        return
    urls = list(dict.fromkeys(str(u) for u in image_urls if u))
    # 只处理同一来源的旧记录：爬虫会分两次调用（topic / comment），
    # 若按整帖清理，第二次调用会把第一次写入的另一来源图片全部标成 stale。
    cursor.execute("SELECT url FROM images WHERE topic_id=? AND source=?", (topic_id, source))
    url_set = set(urls)
    stale_urls = [row[0] for row in cursor.fetchall() if row[0] not in url_set]
    if stale_urls:
        cursor.executemany(
            "UPDATE images SET stale=1 WHERE topic_id=? AND url=? AND stale=0",
            [(topic_id, url) for url in stale_urls],
        )
    if urls:
        cursor.executemany("""
            INSERT INTO images (topic_id, url, source, comment_floor)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(topic_id, url) DO UPDATE SET
                source=excluded.source,
                comment_floor=COALESCE(excluded.comment_floor, images.comment_floor),
                stale=0
        """, [(topic_id, url, source, floors.get(url) if floors else None) for url in urls])


def save_images(topic_id, image_urls, source='topic', floors=None):
    """保存图片 URL 记录，并把该帖已不存在的同来源旧记录标为 stale。

    Args:
        topic_id: 帖子 ID
        image_urls: 图片 URL 列表
        source: 图片来源，'topic' 或 'comment'
        floors: 可选 {url: 评论楼层}，source='comment' 时记录图片所属楼层
    """
    conn = _get_conn()
    try:
        _save_images_with_conn(conn.cursor(), topic_id, image_urls,
                               source=source, floors=floors)
        conn.commit()
    finally:
        conn.close()


def save_scrape_result(topic_data, comments, replace=True,
                       topic_image_urls=None, comment_image_urls=None,
                       comment_image_floors=None):
    """一次抓取结果原子落库：帖子 + 评论 + 两组图片共用一个连接、一个事务。

    任一步失败（包括 replace 缩水保护触发的 RuntimeError）都会整体回滚并
    向上抛出，调用方据此提示「数据保存失败，已回滚」，不会出现「帖子已更新、
    评论却被清空」的中间态。

    Args:
        topic_data: 帖子原始数据
        comments: 本次要写入的评论列表
        replace: True＝完整抓取，覆盖旧评论；False＝部分抓取，合并
        topic_image_urls: 正文图片 URL；None 表示本次不更新该类图片记录
        comment_image_urls: 评论图片 URL；None 表示本次不更新
        comment_image_floors: {url: 评论楼层}

    Returns:
        写入的 topic_id
    """
    conn = _get_conn()
    try:
        cursor = conn.cursor()
        topic_id = _save_topic_with_conn(cursor, topic_data)
        _save_comments_with_conn(cursor, topic_id, comments, replace=replace)
        if topic_image_urls is not None:
            _save_images_with_conn(cursor, topic_id, topic_image_urls, source='topic')
        if comment_image_urls is not None:
            _save_images_with_conn(cursor, topic_id, comment_image_urls,
                                   source='comment', floors=comment_image_floors)
        conn.commit()
        return topic_id
    except Exception:
        # 任何一步失败都回滚整个事务，绝不留半套数据
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_image_downloaded(url, local_path, topic_id=None):
    """标记图片已下载

    url 现在在 (topic_id, url) 上唯一，同一张图可以被多个帖子各自记录，
    因此带上 topic_id 只更新本篇帖子的记录，避免误改其他帖子的行。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        if topic_id:
            c.execute(
                "UPDATE images SET downloaded=1, local_path=? WHERE url=? AND topic_id=?",
                (local_path, url, topic_id))
        else:
            c.execute("UPDATE images SET downloaded=1, local_path=? WHERE url=?", (local_path, url))
        conn.commit()
    finally:
        conn.close()


def mark_images_downloaded(rows, topic_id=None):
    """批量标记图片已下载（一次连接、一次提交）

    Args:
        rows: 可迭代的 (url, local_path)
        topic_id: 可选，限定只更新该帖子的记录
    """
    prepared = [(str(path), str(url), topic_id) for url, path in rows if url and path]
    if not prepared:
        return
    conn = _get_conn()
    try:
        c = conn.cursor()
        if topic_id:
            c.executemany(
                "UPDATE images SET downloaded=1, local_path=? WHERE url=? AND topic_id=?",
                prepared)
        else:
            c.executemany(
                "UPDATE images SET downloaded=1, local_path=? WHERE url=?",
                [(path, url) for path, url, _tid in prepared])
        conn.commit()
    finally:
        conn.close()


def save_ocr_text(url, ocr_text, topic_id=None):
    """保存图片的 OCR 识别结果

    Args:
        url: 图片 URL
        ocr_text: 识别出的文字（空字符串表示无文字，None 表示识别失败）
        topic_id: 可选，限定只更新该帖子的记录
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        ocr_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # None 表示识别失败，落库为 NULL：与「图里没有文字」（空串）区分开，
        # 重新识别时才能只挑失败过的图片重试。
        value = ocr_text
        if topic_id:
            c.execute(
                "UPDATE images SET ocr_text=?, ocr_at=? WHERE url=? AND topic_id=?",
                (value, ocr_at, url, topic_id))
        else:
            c.execute("UPDATE images SET ocr_text=?, ocr_at=? WHERE url=?", (value, ocr_at, url))
        conn.commit()
    finally:
        conn.close()


def save_ocr_results_batch(results, topic_id=None):
    """批量保存 OCR 结果

    Args:
        results: dict {url: ocr_text}
        topic_id: 可选，限定只更新该帖子的记录
    """
    if not results:
        return
    conn = _get_conn()
    try:
        c = conn.cursor()
        ocr_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # text 为 None 表示该图识别失败：写入 NULL 而不是空串，
        # 这样 get_ocr_map 不会返回它，下次 OCR 会重新尝试。
        if topic_id:
            rows = [(text, ocr_at, url, topic_id)
                    for url, text in results.items()]
            c.executemany(
                "UPDATE images SET ocr_text=?, ocr_at=? WHERE url=? AND topic_id=?", rows)
        else:
            rows = [(text, ocr_at, url)
                    for url, text in results.items()]
            c.executemany("UPDATE images SET ocr_text=?, ocr_at=? WHERE url=?", rows)
        conn.commit()
    finally:
        conn.close()


def get_ocr_text(url):
    """获取单张图片的 OCR 文本"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT ocr_text FROM images WHERE url=?", (url,))
        row = c.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_ocr_map(topic_id):
    """获取帖子所有图片的 OCR 结果 {url: ocr_text}"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT url, ocr_text FROM images WHERE topic_id=? AND ocr_text IS NOT NULL",
            (topic_id,),
        )
        result = {row[0]: row[1] for row in c.fetchall()}
        return result
    finally:
        conn.close()


# ==================== 查询 ====================

def get_topic(topic_id):
    """获取单条帖子"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM topics WHERE id=?", (topic_id,))
        row = c.fetchone()
        if not row:
            return None
        data = dict(row)
        if not data.get('url'):
            data['url'] = topic_url(topic_id)
        # 还原成抓取时的结构，界面 / 导出才能取到作者名与小组名
        if not isinstance(data.get('author'), dict):
            data['author'] = {'name': data.get('author') or '', 'uid': data.get('author_id') or ''}
        if not isinstance(data.get('group'), dict):
            data['group'] = {'name': data.get('group_name') or ''}
        return data
    finally:
        conn.close()


def _like_pattern(keyword):
    """把用户输入变成 LIKE 字面量，避免 % _ 被当成通配符。"""
    escaped = keyword.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{escaped}%'


def get_topics(limit=50, offset=0, keyword=''):
    """获取帖子列表（支持标题/作者关键词搜索）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        # 只取列表真正要用的列：content / abstract / raw_json 体积最大，
        # 100 行一起取回来会让数据库浏览器翻页明显变卡，需要时用 get_topic() 单取。
        columns = ('id, title, author, author_id, group_name, comments_count, '
                   'like_count, create_time, scraped_at')
        if keyword:
            pattern = _like_pattern(keyword)
            c.execute(
                f"SELECT {columns} FROM topics WHERE title LIKE ? ESCAPE '\\' "
                f"OR author LIKE ? ESCAPE '\\' "
                f"ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
                (pattern, pattern, limit, offset),
            )
        else:
            c.execute(
                f"SELECT {columns} FROM topics ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        conn.close()


def get_topics_count(keyword=''):
    """获取帖子总数（支持标题/作者关键词过滤）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        if keyword:
            pattern = _like_pattern(keyword)
            c.execute(
                "SELECT COUNT(*) FROM topics WHERE title LIKE ? ESCAPE '\\' "
                "OR author LIKE ? ESCAPE '\\'",
                (pattern, pattern),
            )
        else:
            c.execute("SELECT COUNT(*) FROM topics")
        return c.fetchone()[0]
    finally:
        conn.close()


def get_comments_all(topic_id):
    """获取帖子所有评论（无限制）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM comments WHERE topic_id=? ORDER BY floor ASC", (topic_id,))
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        conn.close()


def get_max_floor(topic_id):
    """该帖已入库评论的最大楼层（无评论时返回 0）

    增量更新用它决定从哪个绝对偏移开始拉取：API 的 start 参数与楼层一一对应
    （start=N 返回的第一条是第 N+1 条评论）。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT MAX(floor) FROM comments WHERE topic_id=?", (topic_id,))
        row = c.fetchone()
        return int(row[0]) if row and row[0] else 0
    finally:
        conn.close()


def get_comment_by_floor(topic_id, floor):
    """按楼层取单条评论（返回 dict，不存在时 None）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT * FROM comments WHERE topic_id=? AND floor=? LIMIT 1",
            (topic_id, floor),
        )
        row = c.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def normalize_ref_comment(ref, id_to_floor=None):
    """把 API 的 ref_comment 归一化成 reply_to 结构（补上楼层）

    豆瓣 comments 接口用 ref_comment 表示「这条评论回复了谁」：它是被回复评论的
    完整对象（含 author/text/id），但没有 floor 字段，楼层只能用 id 在本帖评论里反查。
    """

    if not isinstance(ref, dict):
        return None
    rt_author = ref.get('author')
    if isinstance(rt_author, dict):
        author = {'name': rt_author.get('name', ''), 'uid': rt_author.get('uid', '')}
    else:
        author = {'name': str(rt_author or '')}
    rid = ref.get('id')
    floor = None
    if id_to_floor and rid is not None:
        floor = id_to_floor.get(str(rid))
    return {
        'id': rid,
        'floor': floor,
        'author': author,
        'text': ref.get('text', '') or '',
    }


def get_comment_dicts(topic_id):
    """读取该帖全部评论并还原成爬虫使用的评论 dict（按楼层升序）

    优先反序列化 raw_json（保留 author/photos/reply_to 等完整结构，可直接给
    界面渲染）；raw_json 缺失或损坏时用表字段拼一个最小可用 dict。
    增量更新靠它把「库里已有的旧评论」和「本次新抓的评论」拼成完整列表。
    """
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM comments WHERE topic_id=? ORDER BY floor ASC", (topic_id,))
        rows = [dict(r) for r in c.fetchall()]
    finally:
        conn.close()

    out = []
    for row in rows:
        cm = None
        raw = row.get('raw_json')
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    cm = loaded
            except (ValueError, TypeError):
                cm = None
        if cm is None:
            cm = {
                'id': row.get('id'),
                'author': {'name': row.get('author') or '', 'uid': row.get('author_id') or ''},
                'content': row.get('content') or '',
                'text': row.get('text') or '',
                'create_time': row.get('create_time') or '',
                'ip_location': row.get('ip_location') or '',
                'likes': row.get('likes') or 0,
            }
        if cm.get('floor') in (None, ''):
            cm['floor'] = row.get('floor')
        out.append(cm)

    # 历史数据（本次修复前抓的）只存了 raw_json 里的 ref_comment，没有 reply_to；
    # 读取时补一次归一化，楼层用被回复评论的 id 在本帖评论里反查。
    id_to_floor = {
        str(cm.get('id')): cm.get('floor')
        for cm in out if cm.get('id') is not None
    }
    for cm in out:
        if cm.get('reply_to'):
            continue
        ref = cm.get('ref_comment')
        if isinstance(ref, dict):
            cm['reply_to'] = normalize_ref_comment(ref, id_to_floor)
    return out


def get_comment_count(topic_id):
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM comments WHERE topic_id=?", (topic_id,))
        count = c.fetchone()[0]
        return count
    finally:
        conn.close()


def get_images(topic_id, include_stale=False):
    """获取帖子图片记录；默认过滤 stale=1（本次抓取已不再出现的旧图）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        if include_stale:
            c.execute("SELECT * FROM images WHERE topic_id=? ORDER BY rowid", (topic_id,))
        else:
            c.execute("SELECT * FROM images WHERE topic_id=? AND stale=0 ORDER BY rowid",
                      (topic_id,))
        rows = [dict(r) for r in c.fetchall()]
        return rows
    finally:
        conn.close()


def get_downloaded_image_map(topic_id):
    """获取帖子已下载图片的 {url: local_path}（本地文件仍存在、且未标 stale 的）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT url, local_path FROM images "
            "WHERE topic_id=? AND downloaded=1 AND local_path IS NOT NULL AND stale=0",
            (topic_id,),
        )
        return {
            row[0]: row[1] for row in c.fetchall()
            if row[1] and os.path.isfile(row[1])
        }
    finally:
        conn.close()


def get_stats():
    """获取统计信息"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        stats = {}
        c.execute("SELECT COUNT(*) FROM topics")
        stats['total_topics'] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM comments")
        stats['total_comments'] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM images")
        stats['total_images'] = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM images WHERE downloaded=1")
        stats['downloaded_images'] = c.fetchone()[0] or 0
        return stats
    finally:
        conn.close()


def delete_topic(topic_id):
    """删除帖子及其关联数据，返回应清理的本地图片路径列表"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT i.local_path FROM images i "
            "WHERE i.topic_id=? AND i.downloaded=1 AND i.local_path IS NOT NULL "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM images other "
            "  WHERE other.local_path=i.local_path AND other.topic_id<>i.topic_id"
            ")",
            (topic_id,))
        local_paths = [r[0] for r in c.fetchall()]
        c.execute("DELETE FROM images WHERE topic_id=?", (topic_id,))
        c.execute("DELETE FROM comments WHERE topic_id=?", (topic_id,))
        c.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        conn.commit()
        return local_paths
    finally:
        conn.close()


# ==================== 维护 ====================

def _data_root():
    """可写数据根目录：源码运行即项目根，打包后放 %LOCALAPPDATA%\\douban-post-crawler。

    与 core.paths.DATA_DIR 口径一致；这里保持自包含，避免 data 层反向依赖 UI 侧路径模块。
    """
    env_path = os.environ.get('DBP_DB_FILE')
    if env_path:
        # 指定了自定义数据库位置（测试 / CLI）时，备份放它旁边，避免污染项目根
        return os.path.dirname(_default_db_file())
    if getattr(sys, 'frozen', False):
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'douban-post-crawler')
    # src/data/db_manager.py → 上溯三级是项目根目录
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def backup_db(keep=5):
    """用 VACUUM INTO 生成一致性快照，只保留最近 keep 份，返回备份文件路径。

    VACUUM INTO 产出的是整理过的完整快照；WAL 模式下直接拷贝 .db 文件会漏掉
    尚在 -wal 文件里的已提交事务，因此备份必须走 VACUUM INTO。
    """
    if not isinstance(keep, int) or keep < 1:
        keep = 1
    _ensure_db()
    backup_dir = os.path.join(_data_root(), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    # 微秒级时间戳：同一秒内连续备份也不会因目标文件已存在而失败
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    dest = os.path.join(backup_dir, f'douban_data_{stamp}.db')
    conn = _connect_raw()
    try:
        conn.execute("VACUUM INTO ?", (dest,))
    finally:
        conn.close()
    # 文件名时间戳定长，按名称倒序即按时间倒序；超出的旧备份删掉
    try:
        names = [
            name for name in os.listdir(backup_dir)
            if re.fullmatch(r'douban_data_\d{8}_\d{6}_\d{6}\.db', name)
        ]
        for name in sorted(names, reverse=True)[keep:]:
            try:
                os.remove(os.path.join(backup_dir, name))
            except OSError:
                pass
    except OSError:
        # 清理失败不影响本次备份本身
        pass
    return dest


def quick_check():
    """执行 PRAGMA quick_check，返回 'ok' 或首个损坏描述（供启动/退出自检）"""
    conn = _get_conn()
    try:
        c = conn.cursor()
        c.execute("PRAGMA quick_check")
        row = c.fetchone()
        return row[0] if row else 'ok'
    finally:
        conn.close()
