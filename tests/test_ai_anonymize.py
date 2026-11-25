# -*- coding: utf-8 -*-
"""AI 分析前匿名化的单元测试：昵称统一替换、IP/UID 去除、正文保留。"""

from src.ai.ai_analyzer import _anonymize_for_ai


SAMPLE = '\n'.join([
    '> 作者：**张三** · 小组：**测试小组** · 发布：2024-01-01 · 回复：2 · 点赞：3',
    '',
    '这是张三本人发的帖子，@李四 你怎么看？',
    '',
    '---',
    '',
    '### #1 李四',
    '',
    '`2024-01-02 10:00` · `上海`',
    '',
    '李四回复张三：同意。',
    '',
    '### #2 张三',
    '',
    '`2024-01-03 11:00` · `北京`',
    '',
    '作者ID：12345678',
    '张三再次回复 @王五。',
])


def test_nicknames_are_mapped_consistently():
    out = _anonymize_for_ai(SAMPLE)
    # 首个出现的昵称张三 → 用户1，李四 → 用户2，王五 → 用户3
    assert '张三' not in out
    assert '李四' not in out
    assert '王五' not in out
    assert out.count('用户1') >= 3
    assert out.count('用户2') >= 2
    assert '用户3' in out


def test_ip_location_and_uid_removed():
    out = _anonymize_for_ai(SAMPLE)
    assert '上海' not in out
    assert '北京' not in out
    assert 'IP属地' not in out
    assert '作者ID' not in out
    assert '12345678' not in out
    assert 'UID' not in out


def test_group_time_floor_and_content_kept():
    out = _anonymize_for_ai(SAMPLE)
    assert '测试小组' in out
    assert '2024-01-01' in out
    assert '2024-01-02 10:00' in out
    assert '### #1 用户2' in out
    assert '### #2 用户1' in out
    assert '同意。' in out


def test_mention_replaced():
    out = _anonymize_for_ai(SAMPLE)
    assert '@用户2' in out
    assert '@用户3' in out


def test_empty_input():
    assert _anonymize_for_ai('') == ''
    assert _anonymize_for_ai(None) == ''
