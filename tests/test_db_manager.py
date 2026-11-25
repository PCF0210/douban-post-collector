# -*- coding: utf-8 -*-
"""db_manager 数据链路测试（临时 DB，不联网）

重点覆盖审查报告里的数据安全问题：
- replace 覆盖的缩水保护与空列表保护
- save_scrape_result 单事务原子性（失败整体回滚）
- images 的 stale 标记与查询过滤
- 索引/同步设置/备份/快速检查
"""
import os

import pytest

from src.data import db_manager


def make_topic(tid=1, title='原帖子'):
    return {
        'id': tid,
        'title': title,
        'author': {'name': '楼主', 'uid': 'u1'},
        'group': {'name': '测试小组'},
        'content': '正文内容',
        'abstract': '摘要',
        'comments_count': 10,
        'like_count': 5,
        'create_time': '2026-01-01 00:00:00',
        'url': f'https://www.douban.com/group/topic/{tid}/',
    }


def make_comment(floor, text=None):
    return {
        'floor': floor,
        'author': {'name': f'用户{floor}', 'uid': f'u{floor}'},
        'content': f'<p>{text or ("评论" + str(floor))}</p>',
        'text': text or f'评论{floor}',
        'create_time': '2026-01-01 00:00:00',
        'likes': floor,
        'ip_location': '北京',
    }


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """每个测试用独立临时数据库文件，并重置惰性初始化标志"""
    db_file = tmp_path / 'test.db'
    monkeypatch.setattr(db_manager, 'DB_FILE', str(db_file))
    monkeypatch.setattr(db_manager, '_db_initialized', False)
    db_manager.init_db()
    yield db_file


def count_comments(topic_id):
    conn = db_manager._connect_raw()
    try:
        return conn.execute("SELECT COUNT(*) FROM comments WHERE topic_id=?",
                            (topic_id,)).fetchone()[0]
    finally:
        conn.close()


def test_init_db_idempotent_and_quick_check():
    db_manager.init_db()
    db_manager.init_db()
    assert db_manager.quick_check() == 'ok'


def test_save_comments_empty_does_not_delete_existing():
    tid = db_manager.save_topic(make_topic())
    db_manager.save_comments(tid, [make_comment(i) for i in range(1, 4)])
    assert count_comments(tid) == 3
    # 空列表代表「没有可写数据」，绝不能触发 replace 的 DELETE
    db_manager.save_comments(tid, [], replace=True)
    assert count_comments(tid) == 3


def test_save_comments_shrink_protection():
    tid = db_manager.save_topic(make_topic())
    db_manager.save_comments(tid, [make_comment(i) for i in range(1, 11)])
    assert count_comments(tid) == 10
    # 新数据不足旧数据一半 → 拒绝覆盖
    with pytest.raises(RuntimeError, match='拒绝覆盖'):
        db_manager.save_comments(tid, [make_comment(i) for i in range(1, 4)],
                                 replace=True)
    assert count_comments(tid) == 10


def test_save_comments_allow_shrink_override():
    tid = db_manager.save_topic(make_topic())
    db_manager.save_comments(tid, [make_comment(i) for i in range(1, 11)])
    db_manager.save_comments(tid, [make_comment(i) for i in range(1, 4)],
                             replace=True, allow_shrink=True)
    assert count_comments(tid) == 3


def test_save_comments_merge_keeps_existing_floors():
    tid = db_manager.save_topic(make_topic())
    db_manager.save_comments(tid, [make_comment(i) for i in range(1, 4)], replace=True)
    # 部分抓取（replace=False）只 upsert 新楼层，不删除已有楼层
    db_manager.save_comments(tid, [make_comment(2, '更新后的2楼'), make_comment(9)],
                             replace=False)
    assert count_comments(tid) == 4
    conn = db_manager._connect_raw()
    try:
        row = conn.execute(
            "SELECT content FROM comments WHERE topic_id=? AND floor=2", (tid,)).fetchone()
        assert '更新后的2楼' in row[0]
    finally:
        conn.close()


def test_save_scrape_result_commits_all_parts():
    topic = make_topic(title='新标题')
    comments = [make_comment(i) for i in range(1, 7)]
    tid = db_manager.save_scrape_result(
        topic, comments, replace=True,
        topic_image_urls=['https://img3.doubanio.com/a.jpg'],
        comment_image_urls=['https://img3.doubanio.com/b.jpg'],
        comment_image_floors={'https://img3.doubanio.com/b.jpg': 3},
    )
    assert tid == 1
    assert count_comments(tid) == 6
    assert db_manager.get_topic(1)['title'] == '新标题'
    images = db_manager.get_images(tid)
    assert {i['url'] for i in images} == {
        'https://img3.doubanio.com/a.jpg', 'https://img3.doubanio.com/b.jpg'}


def test_save_scrape_result_rolls_back_everything_on_failure():
    tid = db_manager.save_topic(make_topic(title='旧标题'))
    db_manager.save_comments(tid, [make_comment(i) for i in range(1, 11)])

    # 缩水保护触发 RuntimeError：帖子标题的更新也不能生效（单事务回滚）
    with pytest.raises(RuntimeError, match='拒绝覆盖'):
        db_manager.save_scrape_result(
            make_topic(title='不该生效的新标题'),
            [make_comment(i) for i in range(1, 4)],
            replace=True,
            topic_image_urls=['https://img3.doubanio.com/a.jpg'],
        )
    assert count_comments(tid) == 10
    assert db_manager.get_topic(tid)['title'] == '旧标题'
    assert db_manager.get_images(tid) == []


def test_images_stale_marking_and_filter():
    tid = db_manager.save_topic(make_topic())
    db_manager.save_images(tid, ['https://img3.doubanio.com/a.jpg',
                                 'https://img3.doubanio.com/b.jpg'], source='topic')
    assert {i['url'] for i in db_manager.get_images(tid)} == {
        'https://img3.doubanio.com/a.jpg', 'https://img3.doubanio.com/b.jpg'}

    # 本次抓取 a 不再出现、新增 c：a 只标 stale，不删行
    db_manager.save_images(tid, ['https://img3.doubanio.com/b.jpg',
                                 'https://img3.doubanio.com/c.jpg'], source='topic')
    assert {i['url'] for i in db_manager.get_images(tid)} == {
        'https://img3.doubanio.com/b.jpg', 'https://img3.doubanio.com/c.jpg'}
    all_images = {i['url']: i for i in db_manager.get_images(tid, include_stale=True)}
    assert all_images['https://img3.doubanio.com/a.jpg']['stale'] == 1

    # 图片重新出现时 upsert 恢复 stale=0
    db_manager.save_images(tid, ['https://img3.doubanio.com/a.jpg'], source='topic')
    assert 'https://img3.doubanio.com/a.jpg' in {
        i['url'] for i in db_manager.get_images(tid)}


def test_downloaded_image_map_filters_missing_files(tmp_path):
    tid = db_manager.save_topic(make_topic())
    db_manager.save_images(tid, ['https://img3.doubanio.com/a.jpg'], source='topic')
    local_file = tmp_path / 'a.jpg'
    local_file.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 16)
    db_manager.mark_images_downloaded(
        [('https://img3.doubanio.com/a.jpg', str(local_file))], topic_id=tid)
    assert db_manager.get_downloaded_image_map(tid) == {
        'https://img3.doubanio.com/a.jpg': str(local_file)}

    # 本地文件被删后不再算「已下载」（否则 OCR 会读到不存在的路径）
    local_file.unlink()
    assert db_manager.get_downloaded_image_map(tid) == {}


def test_new_indexes_present_and_old_index_dropped():
    conn = db_manager._connect_raw()
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    finally:
        conn.close()
    assert 'idx_topics_scraped_at' in names
    assert 'idx_images_downloaded' in names
    assert 'idx_comments_unique' in names
    assert 'idx_comments_author' not in names


def test_connect_raw_uses_wal_and_normal_synchronous():
    conn = db_manager._connect_raw()
    try:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
        assert conn.execute('PRAGMA synchronous').fetchone()[0] == 1  # NORMAL
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    finally:
        conn.close()


def test_backup_db_creates_snapshot_and_keeps_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(db_manager, '_data_root', lambda: str(tmp_path))
    tid = db_manager.save_topic(make_topic())
    db_manager.save_comments(tid, [make_comment(1)])

    first = db_manager.backup_db(keep=2)
    assert os.path.isfile(first)
    second = db_manager.backup_db(keep=2)
    third = db_manager.backup_db(keep=2)
    backups = sorted(os.listdir(tmp_path / 'backups'))
    assert len(backups) == 2
    # 保留的是最近两份
    assert os.path.basename(third) in backups
    assert os.path.basename(first) not in backups
    assert second != first


def test_data_root_follows_custom_db_file(tmp_path, monkeypatch):
    """DBP_DB_FILE 指向自定义库时，备份目录跟随该库所在目录。"""
    monkeypatch.setenv('DBP_DB_FILE', str(tmp_path / 'custom.db'))
    assert db_manager._data_root() == str(tmp_path)
    monkeypatch.delenv('DBP_DB_FILE')
    # 未指定环境变量时回到源码项目根（打包路径依赖 sys.frozen，不在本测试覆盖）
    assert db_manager._data_root() == os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(db_manager.__file__))))
