# -*- coding: utf-8 -*-
"""AI 分析（DeepSeek API）— 将帖子内容以 Markdown 格式发送给 DeepSeek 总结分析"""

import json
import re
import threading
import time

import requests

from PySide6.QtCore import Qt, QObject, QEvent, Signal, QSettings, QTimer
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTextEdit, QTextBrowser, QComboBox, QMessageBox, QFileDialog,
    QFrame, QCheckBox, QScrollArea, QWidget, QSizePolicy,
)

from ..core.file_log import log_error, log_info
from ..core.secret_store import (available as secret_available, protect_b64,
                                 unprotect_b64)

# ==================== 配置 ====================

DEEPSEEK_API_URL = 'https://api.deepseek.com/chat/completions'
# 第一个元素是提交给 API 的 model 名，第二个是界面显示名
DEEPSEEK_MODELS = [
    ('deepseek-flash', 'deepseek-flash（速度快）'),
    ('deepseek-v4-pro', 'deepseek-v4-pro（能力更强，深度推理）'),
]
DEFAULT_MODEL = 'deepseek-flash'
# 旧模型名 → 新模型名：deepseek-v4-flash 等已下线，仍可用但请求会被转到
# deepseek-flash 后端；这里主动迁移设置里残留的旧值，避免界面显示一个已下线的名字
LEGACY_MODEL_ALIASES = {
    'deepseek-v4-flash': 'deepseek-flash',
    'deepseek-v4-flash-vision-exp': 'deepseek-flash',
}

# 发送内容最大长度（字符）。
# deepseek-flash / deepseek-v4-pro 上下文 1M token，中文约 1 字 ≈ 0.6 token，
# 10 万字符远低于上限；这里保留更保守的上限，超出部分截断（保留头尾，中间以标记替代）
MAX_CONTENT_CHARS = 100000

# 系统提示词：角色设定 + 工作原则（约束行为，不含具体任务）
SYSTEM_PROMPT = (
    '# 角色\n'
    '你是资深的社区内容分析师，擅长阅读论坛帖子'
    '（含口语化表达、网络用语的评论区），提炼关键信息并输出严谨的结构化中文报告。\n'
    '\n'
    '# 工作原则\n'
    '1. **忠于原文**：所有结论必须来自帖子内容本身，不编造、不脑补、不引入外部知识。\n'
    '2. **标注依据**：重要结论附上楼层来源，格式如 `#12`（评论第 12 楼）、`#3`（第 3 楼）；'
    '正文信息标注 `楼主`。\n'
    '3. **区分事实与观点**：转述时用「楼主称 / 网友认为」等措辞，与客观信息分开呈现。\n'
    '4. **过滤噪声**：广告、灌水、纯表情、无意义刷楼直接忽略，不纳入分析。\n'
    '5. **克制输出**：严格按模板组织报告；模板中某部分无相关内容时写「无」或整节省略，不硬凑。'
)

# 用户任务提示词：任务说明 + 输出模板
TASK_PROMPT = (
    '请阅读下方 <帖子内容> 标签内的豆瓣小组帖子'
    '（Markdown 格式，含标题、正文、全部评论及图片 OCR 文字），按以下模板输出分析报告：\n'
    '\n'
    '---\n'
    '## 帖子概览\n'
    '- **主题**：一句话概括帖子在讨论什么\n'
    '- **类型**：经验分享 / 求助咨询 / 观点讨论 / 吐槽爆料 / 二手交易 / 拼组活动 / 其他（选一）\n'
    '- **热度**：结合评论量与互动氛围，一句话简述\n'
    '\n'
    '## 正文要点\n'
    '楼主陈述的关键信息逐条列出（人物、时间、地点、经过、诉求等），条目后标注 `楼主`。\n'
    '\n'
    '## 评论区分析\n'
    '- **主流观点**：多数评论的立场或最有价值的信息点，附楼层依据\n'
    '- **分歧与争议**：如有对立观点，分别概括双方立场及代表楼层；无分歧则省略本条\n'
    '- **情绪倾向**：一个词概括整体氛围（如支持 / 质疑 / 吃瓜 / 愤怒 / 玩梗）+ 一句解释\n'
    '\n'
    '## 关键信息提取\n'
    '用表格汇总值得记录的具体信息，列为 `类别 | 内容 | 来源`；\n'
    '类别涵盖：时间、地点、价格/费用、数量、联系方式、人名/机构、链接等；无则整节省略。\n'
    '\n'
    '## 风险提示\n'
    '仅当内容涉及交易、转账、集资、隐私爆料等需要读者警惕的情况时输出，说明风险点；否则省略。\n'
    '\n'
    '## 一句话总结\n'
    '不超过 40 字，概括全帖核心。\n'
    '---'
)

# ==================== 后台请求 ====================


def _dpapi_protect(text):
    """用 Windows DPAPI 加密文本，返回 base64；失败或非 Windows 返回空串。

    具体实现已统一到 core.secret_store（代理地址等敏感配置也复用它，避免重复写
    ctypes）。这里保留函数名与旧语义（不带前缀的 base64），已有配置文件才能继续解密。
    """
    return protect_b64(text)


def _dpapi_unprotect(token):
    """解密 DPAPI base64 文本；失败或非 Windows 返回空串（旧语义，见 _dpapi_protect）。"""
    return unprotect_b64(token)


def _load_saved_api_key(settings):
    """读取 API Key：优先 DPAPI 密文，并迁移旧版明文。

    返回空串表示没有可用的 Key。密文解密失败会写日志（界面据此提示用户重新输入），
    不会静默清空已有配置。
    """
    token = str(settings.value('ai/deepseek_key_protected', '') or '')
    if token:
        key = _dpapi_unprotect(token)
        if key:
            return key
        log_error('已保存的 API Key 解密失败（可能更换了 Windows 用户或系统重装），'
                  '请在设置中重新填写 API Key')
    old = settings.value('ai/deepseek_key', '')
    if old:
        old = str(old)
        protected = _dpapi_protect(old)
        if protected:
            settings.setValue('ai/deepseek_key_protected', protected)
            settings.remove('ai/deepseek_key')
            log_info('API Key 已从旧版明文迁移为 DPAPI 密文保存')
        else:
            # 加密不可用时保留旧明文配置（不主动删除），并提示用户当前平台无法安全保存
            log_info('当前系统不支持 DPAPI 安全存储，API Key 旧明文配置保留，但不会再写入新明文')
        return old
    return ''


def _save_api_key(settings, api_key):
    """用 DPAPI 保存 API Key，成功后删除旧版明文；返回是否安全保存成功。

    注意：加密失败时**不清空旧值、也不写明文**（旧实现在这里 remove 明文，
    加密失败时会把用户已经保存的 Key 静默清掉）。调用方拿到 False 时应提示用户。
    """
    api_key = (api_key or '').strip()
    if not api_key:
        # 输入框为空：视为用户主动清除已保存的 Key
        settings.remove('ai/deepseek_key_protected')
        settings.remove('ai/deepseek_key')
        return True
    protected = _dpapi_protect(api_key)
    if not protected:
        return False
    settings.setValue('ai/deepseek_key_protected', protected)
    settings.remove('ai/deepseek_key')
    return True


def _build_messages(md_content, extra_prompt):
    """构造 DeepSeek chat messages（XML 标签分块；超长内容截断，保留头尾）"""
    content = md_content
    # 用显式标记记录是否截断：不能再靠 '已截断' in content 判断，
    # 否则帖子正文里本来就有「已截断」三个字时会误加提示。
    truncated = len(content) > MAX_CONTENT_CHARS
    if truncated:
        keep_head = int(MAX_CONTENT_CHARS * 0.7)
        keep_tail = int(MAX_CONTENT_CHARS * 0.3)
        content = (content[:keep_head]
                   + '\n\n[...中间内容过长已截断...]\n\n'
                   + content[-keep_tail:])

    # 用 XML 标签把「任务 / 补充要求 / 帖子内容」明确分块，边界清晰不易混淆
    blocks = [f'<任务>\n{TASK_PROMPT}\n</任务>']
    if extra_prompt:
        blocks.append(f'<补充要求>\n{extra_prompt}\n</补充要求>')
    if truncated:
        blocks.append('<注意事项>\n'
                      '帖子内容过长，中间部分已被截断，请仅基于现有内容分析，'
                      '并在报告开头注明「部分评论未纳入分析」。\n'
                      '</注意事项>')
    blocks.append(f'<帖子内容>\n{content}\n</帖子内容>')

    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': '\n\n'.join(blocks)},
    ]


# ==================== 隐私确认 / 匿名化 ====================

# 首次发送整帖前的隐私确认标记（确认后不再重复提示）
PRIVACY_ACK_KEY = 'ai/privacy_ack_v1'


def _privacy_settings():
    """隐私确认状态使用的 QSettings。

    单独封装成函数便于测试时替换（测试用独立组织名，避免污染真实配置）。
    """
    return QSettings('DBScraper', 'DBScraper')


def ensure_privacy_consent(parent=None):
    """首次发送整帖内容前确认隐私风险；返回 True 表示可以继续发送。

    帖子正文/评论包含昵称、IP 属地等个人信息，OCR 文本还可能包含图片里的文字，
    这些内容都会随请求发送到 DeepSeek API。用户确认后写入 ack，不再重复打扰。
    """
    settings = _privacy_settings()
    if settings.value(PRIVACY_ACK_KEY, False, type=bool):
        return True
    box = QMessageBox(parent)
    box.setWindowTitle('发送内容隐私确认')
    box.setIcon(QMessageBox.Warning)
    box.setText(
        '接下来会把整个帖子的内容发送到 DeepSeek API 进行总结：\n\n'
        '· 帖子正文与全部评论（含评论者昵称、IP 属地）\n'
        '· 图片 OCR 识别出来的文字\n\n'
        '请确认其中不含你不希望上传的个人敏感信息。\n'
        '如果介意，可以在「设置」中勾选「发送前匿名化」，'
        '把昵称替换为「用户N」并去掉评论的 IP 属地。'
    )
    box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
    ok_btn = box.button(QMessageBox.Ok)
    if ok_btn is not None:
        ok_btn.setText('同意并继续')
    cancel_btn = box.button(QMessageBox.Cancel)
    if cancel_btn is not None:
        cancel_btn.setText('取消')
    box.setDefaultButton(QMessageBox.Cancel)
    if box.exec() != QMessageBox.Ok:
        return False
    settings.setValue(PRIVACY_ACK_KEY, True)
    return True


# 匿名化只针对 _build_md() 生成的固定 Markdown 结构，尽量少破坏正文：
# - 作者行：> 作者：**昵称** · …
# - 评论标题行：### #楼层 昵称
# - 回复引用：> ↳ 回复 #楼层 @昵称（以及正文里出现的 @昵称）
# - 评论元信息行：`时间` · `IP属地`（去掉 IP 属地，只留时间）
_RE_ANON_AUTHOR = re.compile(r'^(> 作者：\*\*)(.+?)(\*\*.*)$')
_RE_ANON_COMMENT_HEAD = re.compile(r'^(### #\d+ )(.+)$')
_RE_ANON_MENTION = re.compile(r'@([^\s#@，。！？、：；]+)')
_RE_ANON_META_LINE = re.compile(r'^(`[^`]*`)(?: · `[^`]*`)+$')
_RE_ANON_UID = re.compile(r'(?i)(?:\buid\b|用户\s*id|作者\s*id)\s*[:：]\s*\S+')


def _anonymize_for_ai(md_text):
    """发送给 AI 前做轻量匿名化：昵称统一替换为「用户N」，去掉 IP 属地/UID。

    - 先按出现顺序收集作者/评论者/被 @ 的昵称，同一昵称全篇映射到同一编号；
    - 再替换作者行、评论标题、@ 提及，以及正文中直接出现的 2 字以上昵称；
    - 评论元信息行的 IP 属地、以及 UID 字段一并去掉；
    - 群组名、时间、楼层号、正文语义保持不变，避免破坏分析质量。
    """
    if not md_text:
        return ''
    lines = str(md_text).split('\n')
    mapping = {}

    def _alias(name):
        name = (name or '').strip()
        if not name:
            return name
        if name not in mapping:
            mapping[name] = f'用户{len(mapping) + 1}'
        return mapping[name]

    # 第一遍：按出现顺序收集昵称，确定整份内容里统一的「用户N」编号
    for line in lines:
        m = _RE_ANON_AUTHOR.match(line)
        if m:
            _alias(m.group(2))
        else:
            m = _RE_ANON_COMMENT_HEAD.match(line)
            if m:
                _alias(m.group(2))
        for mm in _RE_ANON_MENTION.finditer(line):
            _alias(mm.group(1))

    # 正文里直接提到的昵称也替换（单字昵称不做全局替换，避免误伤普通用字）
    name_re = None
    replaceable = sorted((n for n in mapping if len(n) >= 2), key=len, reverse=True)
    if replaceable:
        name_re = re.compile('|'.join(re.escape(n) for n in replaceable))

    out = []
    for line in lines:
        m = _RE_ANON_AUTHOR.match(line)
        if m:
            line = m.group(1) + _alias(m.group(2)) + m.group(3)
        else:
            m = _RE_ANON_COMMENT_HEAD.match(line)
            if m:
                line = m.group(1) + _alias(m.group(2))
        line = _RE_ANON_MENTION.sub(lambda mm: '@' + _alias(mm.group(1)), line)
        if name_re is not None:
            line = name_re.sub(lambda mm: _alias(mm.group(0)), line)
        m = _RE_ANON_META_LINE.match(line)
        if m:
            # 评论元信息行第二段是 IP 属地，属于个人信息，直接去掉
            line = m.group(1)
        if _RE_ANON_UID.search(line):
            line = _RE_ANON_UID.sub('', line).strip()
            if not line:
                continue
        out.append(line)
    return '\n'.join(out)


# ==================== 分析对话框（聊天式） ====================


class _StreamRelay(QObject):
    """后台线程 → UI 线程的流式片段/完成/失败/重试信号"""

    delta = Signal(str)
    done = Signal(str)
    fail = Signal(str)
    retry = Signal(int, float)


# 单次请求的连接/读取超时（秒）。读超时从原来的 300 秒缩短到 45 秒：
# 取消后请求迟迟不断开的根因就是过长的读超时，配合可中断重试与主动 close 已足够。
CHAT_CONNECT_TIMEOUT = 10
CHAT_READ_TIMEOUT = 45
# 重试次数（不含首次尝试）：429/5xx/网络错误时按指数退避重试
MAX_API_RETRIES = 2
# 尊重 Retry-After，但做上限截断，避免服务端返回超大值让界面长时间无响应
RETRY_AFTER_CAP = 30.0
# 可安全重试的 HTTP 状态码
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _retry_after_seconds(resp, default):
    """解析 Retry-After 响应头（秒数）；无法解析时用 default 退避时间。"""
    try:
        raw = resp.headers.get('Retry-After')
    except Exception:  # noqa: BLE001
        raw = None
    if not raw:
        return default
    try:
        return max(0.0, min(RETRY_AFTER_CAP, float(str(raw).strip())))
    except (TypeError, ValueError):
        return default


def _sleep_cancellable(seconds, cancel_event):
    """可被取消事件打断的 sleep；返回 False 表示等待期间被取消。"""
    deadline = time.time() + max(0.0, seconds)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        remain = deadline - time.time()
        if remain <= 0:
            return True
        time.sleep(min(0.2, remain))


def _request_chat(api_key, messages, model, proxy, thinking=False,
                  reasoning_effort='high', on_delta=None, on_done=None,
                  on_fail=None, cancel_event=None, session_holder=None,
                  session_lock=None, on_retry=None):
    """后台线程调用 DeepSeek chat（SSE 流式），每收到一段增量就回调 on_delta。

    - cancel_event 置位后不再回调、不再重试；session_holder/session_lock 用于
      对话框关闭时线程安全地主动断开连接；
    - 429/5xx/网络错误按指数退避重试 MAX_API_RETRIES 次（尊重 Retry-After，
      取消时不重试），每次重试前通过 on_retry(第N次, 等待秒数) 通知界面；
    - 读超时缩短到 CHAT_READ_TIMEOUT，取消后最多等这段时间就能退出。
    """

    def _cancelled():
        return cancel_event is not None and cancel_event.is_set()

    def _fail(msg):
        if _cancelled():
            return  # 取消/关闭对话框后静默，不再触发失败提示
        if on_fail:
            on_fail(msg)

    def _register_session(session):
        if session_holder is None:
            return
        if session_lock is not None:
            with session_lock:
                session_holder.append(session)
        else:
            session_holder.append(session)

    def _notify_retry(attempt, delay):
        if on_retry and not _cancelled():
            try:
                on_retry(attempt, delay)
            except Exception:  # noqa: BLE001
                pass

    def _work():
        session = None
        try:
            if _cancelled():
                return
            headers = {
                'Authorization': f'Bearer {api_key.strip()}',
                'Content-Type': 'application/json',
            }
            payload = {
                'model': model,
                'messages': messages,
                'stream': True,
            }
            if thinking:
                payload['thinking'] = {'type': 'enabled'}
                payload['reasoning_effort'] = reasoning_effort
            proxies = {'http': proxy, 'https': proxy} if proxy else None

            session = requests.Session()
            _register_session(session)
            if _cancelled():
                return

            attempt = 0
            while True:
                if _cancelled():
                    return
                try:
                    r = session.post(
                        DEEPSEEK_API_URL, headers=headers, json=payload,
                        proxies=proxies,
                        timeout=(CHAT_CONNECT_TIMEOUT, CHAT_READ_TIMEOUT),
                        stream=True,
                    )
                except (requests.Timeout, requests.ConnectionError) as e:
                    if _cancelled():
                        return
                    if attempt < MAX_API_RETRIES:
                        attempt += 1
                        delay = float(2 ** (attempt - 1))
                        _notify_retry(attempt, delay)
                        if not _sleep_cancellable(delay, cancel_event):
                            return
                        continue
                    if isinstance(e, requests.Timeout):
                        retried = f'，已重试 {attempt} 次' if attempt else ''
                        _fail(f'请求超时（DeepSeek 响应时间过长）{retried}，请稍后重试。')
                    else:
                        _fail(f'网络连接失败：{e}\n'
                              '如需代理，请在主窗口「设置 → 代理设置」中配置后重试。')
                    return

                if r.status_code == 200:
                    break

                body = ''
                try:
                    body = r.text[:500]
                except Exception:  # noqa: BLE001
                    pass
                status = r.status_code
                retry_after = _retry_after_seconds(r, 0.0)
                try:
                    r.close()
                except Exception:  # noqa: BLE001
                    pass

                if status in _RETRYABLE_STATUS and attempt < MAX_API_RETRIES:
                    attempt += 1
                    wait = retry_after if retry_after > 0 else float(2 ** (attempt - 1))
                    _notify_retry(attempt, wait)
                    if not _sleep_cancellable(wait, cancel_event):
                        return
                    continue

                retried = f'已重试 {attempt} 次，' if attempt else ''
                if status == 401:
                    _fail('API Key 无效（HTTP 401），请检查后重试。')
                elif status == 402:
                    _fail('账户余额不足（HTTP 402），请前往 DeepSeek 开放平台充值。')
                elif status == 429:
                    _fail(f'请求过于频繁（HTTP 429），{retried}请稍等片刻再试。')
                else:
                    tail = f'\n（已重试 {attempt} 次）' if attempt else ''
                    _fail(f'API 请求失败：HTTP {status}\n{body}{tail}')
                return

            parts = []
            try:
                for raw in r.iter_lines(decode_unicode=True):
                    if _cancelled():
                        return
                    if not raw:
                        continue
                    line = raw.strip()
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    choices = obj.get('choices') or []
                    if not choices:
                        continue
                    delta = choices[0].get('delta') or {}
                    piece = delta.get('content') or ''
                    if piece:
                        parts.append(piece)
                        if on_delta and not _cancelled():
                            on_delta(piece)
            finally:
                try:
                    r.close()
                except Exception:  # noqa: BLE001
                    pass

            if _cancelled():
                return
            text = ''.join(parts).strip()
            if not text:
                _fail('API 返回了空的分析结果，请稍后重试。')
                return
            if on_done and not _cancelled():
                on_done(text)
        except requests.Timeout:
            _fail('请求超时（DeepSeek 响应时间过长），请稍后重试。')
        except requests.ConnectionError as e:
            _fail(f'网络连接失败：{e}\n'
                  '如需代理，请在主窗口「设置 → 代理设置」中配置后重试。')
        except Exception as e:  # noqa: BLE001
            if _cancelled():
                return  # 取消后主动 close 会话会抛异常，属于正常退出路径，静默处理
            _fail(f'AI 请求出错：{e}')
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass

    threading.Thread(target=_work, daemon=True).start()


def _short_time():
    from datetime import datetime
    return datetime.now().strftime('%H:%M')


class _ChatInput(QTextEdit):
    """聊天输入框：Enter 发送、Shift+Enter 换行。"""

    submitted = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not (event.modifiers() & Qt.ShiftModifier):
            event.accept()
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class _ChatMessage(QWidget):
    """一条聊天消息：头像 + 气泡。用户靠右、AI 靠左。"""

    def __init__(self, role, text='', meta='', parent=None):
        super().__init__(parent)
        self.role = role
        self.text_value = text

        outer = QHBoxLayout(self)
        outer.setContentsMargins(2, 3, 2, 3)
        outer.setSpacing(8)

        avatar = QLabel('我' if role == 'user' else 'AI')
        avatar.setFixedSize(30, 30)
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setObjectName('avatarMe' if role == 'user' else 'avatarAI')

        bubble = QFrame()
        bubble.setObjectName('bubbleMe' if role == 'user' else 'bubbleAI')
        bubble.setMaximumWidth(620)
        box = QVBoxLayout(bubble)
        box.setContentsMargins(13, 8, 13, 9)
        box.setSpacing(3)

        self.meta_label = QLabel(meta or ('我 · ' + _short_time() if role == 'user'
                                          else 'DeepSeek · ' + _short_time()))
        self.meta_label.setObjectName('bubbleMeta')

        if role == 'user':
            body = QLabel(text)
            body.setWordWrap(True)
            body.setTextFormat(Qt.PlainText)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            body.setObjectName('bubbleTextMe')
        else:
            body = QTextBrowser()
            body.setObjectName('bubbleTextAI')
            body.setReadOnly(True)
            body.setFrameShape(QFrame.NoFrame)
            body.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body.setOpenExternalLinks(True)
            body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            body.document().setDocumentMargin(0)
            body.setMarkdown(text or '…')
        self.body = body

        box.addWidget(self.meta_label)
        box.addWidget(body)

        if role == 'user':
            outer.addStretch(1)
            outer.addWidget(bubble, 0)
            outer.addWidget(avatar, 0, Qt.AlignTop)
        else:
            outer.addWidget(avatar, 0, Qt.AlignTop)
            outer.addWidget(bubble, 0)
            outer.addStretch(1)

        self._bubble = bubble

    def set_markdown(self, text):
        self.text_value = text
        if isinstance(self.body, QTextBrowser):
            self.body.setMarkdown(text)
            self._fit()
        else:
            self.body.setText(text)

    def set_meta(self, text):
        self.meta_label.setText(text)

    def _fit(self):
        if not isinstance(self.body, QTextBrowser):
            return
        # 气泡宽度随窗口自适应：太窄读起来挤，太宽又不像聊天
        avail = self.width() - 30 - 8 - 10
        width = max(320, min(620, int(avail * 0.92)))
        if self._bubble.width() != width:
            self._bubble.setFixedWidth(width)
        # 以正文控件 viewport 的真实宽度排版：QTextBrowser 自身还带边框内缩，
        # 若按控件宽度排，文档会比可视区宽出十几像素，最后一行就会被裁掉半行。
        vp = self.body.viewport()
        inner = vp.width() if vp.width() > 100 else max(200, width - 28)
        doc = self.body.document()
        doc.setTextWidth(inner)
        chrome = max(0, self.body.height() - vp.height())
        need = int(doc.size().height()) + chrome + 6
        if self.body.height() != need:
            self.body.setFixedHeight(need)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self._fit()


class AIAnalysisDialog(QDialog):
    """AI 分析（聊天式）：先出结构化总结，之后可继续追问。"""

    def __init__(self, md_text, proxy=None, parent=None, title=None):
        super().__init__(parent)
        self.md_text = md_text or ''
        self.proxy = proxy
        self.topic_title = (title or '').strip()

        self._running = False
        self._cancel_event = threading.Event()
        self._http_sessions = []
        # _http_sessions 会在后台线程被追加、在 UI 线程被 swap/close，必须加锁
        self._http_lock = threading.Lock()
        self._api_messages = []
        self._started = False
        self._stream_text = ''
        self._stream_row = None
        self._stream_relay = None
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(70)
        self._flush_timer.timeout.connect(self._flush_stream)

        self.setWindowTitle('AI 分析总结（对话）')
        self.setMinimumSize(720, 620)
        self.resize(820, 720)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self._settings = QSettings('DBScraper', 'DBScraper')
        saved_key = _load_saved_api_key(self._settings)
        # DPAPI 能力与「密文解密失败」状态：决定设置区 Key 的提示文案
        self._key_storage_available = secret_available()
        token = str(self._settings.value('ai/deepseek_key_protected', '') or '')
        self._key_load_failed = bool(token) and not bool(saved_key)
        # 安全保存失败只提示一次，避免每次请求都弹框
        self._key_save_warned = False

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(8)

        # ---- 标题行 ----
        title_row = QHBoxLayout()
        head = QLabel('AI 分析总结')
        head.setObjectName('dlgTitle')
        sub = QLabel(f'上下文 {len(self.md_text):,} 字')
        sub.setObjectName('dlgSub')
        self.btn_settings = QPushButton('⚙ 设置')
        self.btn_settings.setObjectName('btnMini')
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.clicked.connect(self._toggle_settings)
        title_row.addWidget(head)
        title_row.addSpacing(8)
        title_row.addWidget(sub)
        title_row.addStretch()
        title_row.addWidget(self.btn_settings)
        root.addLayout(title_row)

        # ---- 设置面板（有 Key 时默认收起） ----
        self.cfg_frame = self._build_settings(saved_key)
        self.cfg_frame.setVisible(not bool(saved_key))
        root.addWidget(self.cfg_frame)

        # ---- 聊天区 ----
        self.chat_area = QScrollArea()
        self.chat_area.setObjectName('chatArea')
        self.chat_area.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName('chatInner')
        self.chat_layout = QVBoxLayout(inner)
        self.chat_layout.setContentsMargins(4, 4, 4, 4)
        self.chat_layout.setSpacing(2)
        self.chat_layout.addStretch(1)
        self.chat_area.setWidget(inner)
        root.addWidget(self.chat_area, stretch=1)

        welcome = ('已载入帖子内容'
                   + (f'《{self.topic_title}》' if self.topic_title else '')
                   + f'（{len(self.md_text):,} 字，含正文、评论与 OCR 文字）。\n\n'
                   '点下方「生成总结」，我会先通读全文给出结构化报告；之后可以在输入框里继续追问细节。')
        if len(self.md_text) > MAX_CONTENT_CHARS:
            welcome += (f'\n\n> 提示：内容超过单次上限 {MAX_CONTENT_CHARS:,} 字，'
                        '将保留开头 70% 与结尾 30% 发送，中间部分不纳入分析。')
        self._add_message('assistant', welcome, meta='DeepSeek · 就绪')

        # ---- 操作行 ----
        action = QHBoxLayout()
        self.btn_summary = QPushButton('生成总结')
        self.btn_summary.setObjectName('btnRun')
        self.btn_summary.setFixedHeight(34)
        self.btn_summary.setMinimumWidth(110)
        self.btn_summary.setCursor(Qt.PointingHandCursor)
        self.btn_summary.clicked.connect(self._start_summary)
        self.status_label = QLabel('')
        self.status_label.setObjectName('dlgSub')
        self.btn_copy = QPushButton('复制对话')
        self.btn_copy.setObjectName('btnMini')
        self.btn_copy.setCursor(Qt.PointingHandCursor)
        self.btn_copy.clicked.connect(self._copy_transcript)
        self.btn_copy.setEnabled(False)
        self.btn_save = QPushButton('保存 Markdown')
        self.btn_save.setObjectName('btnMini')
        self.btn_save.setCursor(Qt.PointingHandCursor)
        self.btn_save.clicked.connect(self._save_transcript)
        self.btn_save.setEnabled(False)
        btn_close = QPushButton('关闭')
        btn_close.setObjectName('btnMini')
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.clicked.connect(self.reject)
        action.addWidget(self.btn_summary)
        action.addSpacing(10)
        action.addWidget(self.status_label)
        action.addStretch()
        action.addWidget(self.btn_copy)
        action.addWidget(self.btn_save)
        action.addWidget(btn_close)
        root.addLayout(action)

        # ---- 输入行 ----
        input_row = QHBoxLayout()
        self.input_edit = _ChatInput()
        self.input_edit.setObjectName('chatInput')
        self.input_edit.setPlaceholderText(
            '生成总结后可以在这里继续追问（Enter 发送，Shift+Enter 换行）')
        self.input_edit.setFixedHeight(52)
        self.input_edit.setEnabled(False)
        self.input_edit.submitted.connect(self._send_followup)
        self.btn_send = QPushButton('发送')
        self.btn_send.setObjectName('btnSend')
        self.btn_send.setFixedSize(72, 36)
        self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.clicked.connect(self._send_followup)
        self.btn_send.setEnabled(False)
        input_row.addWidget(self.input_edit, stretch=1)
        input_row.addWidget(self.btn_send, 0, Qt.AlignBottom)
        root.addLayout(input_row)

        self._apply_styles()

        if not saved_key:
            self.key_input.setFocus()

    # ==================== 构建 ====================

    def _build_settings(self, saved_key):
        frame = QFrame()
        frame.setObjectName('cfgFrame')
        box = QVBoxLayout(frame)
        box.setContentsMargins(12, 10, 12, 10)
        box.setSpacing(8)

        key_row = QHBoxLayout()
        key_label = QLabel('API Key：')
        key_label.setObjectName('fieldLabel')
        self.key_input = QLineEdit(saved_key)
        self.key_input.setEchoMode(QLineEdit.Password)
        # 提示文案必须与真实能力一致：不可用/解密失败时不能再说「已加密保存」
        if not self._key_storage_available:
            key_ph = 'sk-...（当前系统不支持安全存储，不会保存明文 Key）'
            key_tip = ('当前系统（非 Windows 或 DPAPI 不可用）不支持安全存储：'
                       '程序不会把 API Key 明文写入配置，本次运行可用，关闭后需重新输入。')
        elif self._key_load_failed:
            key_ph = 'sk-...（已保存的 Key 无法解密，请重新输入）'
            key_tip = ('已保存的 API Key 解密失败（可能更换了 Windows 用户或重装了系统），'
                       '请重新输入；旧密文不会被覆盖或删除。')
        else:
            key_ph = 'sk-...（使用 Windows DPAPI 加密保存在本机）'
            key_tip = 'API Key 通过 Windows DPAPI 加密后保存，不会以明文写入配置。'
        self.key_input.setPlaceholderText(key_ph)
        self.key_input.setToolTip(key_tip)
        self.btn_show_key = QPushButton('显示')
        self.btn_show_key.setFixedWidth(48)
        self.btn_show_key.setObjectName('btnMini')
        self.btn_show_key.setCursor(Qt.PointingHandCursor)
        self.btn_show_key.clicked.connect(self._toggle_key_visible)
        key_row.addWidget(key_label)
        key_row.addWidget(self.key_input, stretch=1)
        key_row.addWidget(self.btn_show_key)
        box.addLayout(key_row)

        model_row = QHBoxLayout()
        model_label = QLabel('模型：')
        model_label.setObjectName('fieldLabel')
        self.model_combo = QComboBox()
        for mid, mname in DEEPSEEK_MODELS:
            self.model_combo.addItem(mname, mid)
        saved_model = self._settings.value('ai/model', DEFAULT_MODEL) or DEFAULT_MODEL
        saved_model = LEGACY_MODEL_ALIASES.get(saved_model, saved_model)
        idx = self.model_combo.findData(saved_model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.model_combo.setCursor(Qt.PointingHandCursor)
        link = QLabel('<a href="https://platform.deepseek.com/api_keys">获取 API Key</a>')
        link.setOpenExternalLinks(True)
        link.setObjectName('dlgSub')
        model_row.addWidget(model_label)
        model_row.addWidget(self.model_combo, stretch=1)
        model_row.addWidget(link)
        box.addLayout(model_row)

        thinking_row = QHBoxLayout()
        thinking_label = QLabel('深度思考：')
        thinking_label.setObjectName('fieldLabel')
        self.chk_thinking = QCheckBox('启用')
        self.chk_thinking.setCursor(Qt.PointingHandCursor)
        effort_label = QLabel('推理强度：')
        effort_label.setObjectName('fieldLabel')
        self.effort_combo = QComboBox()
        for eff, eff_name in [('low', 'low（快）'), ('medium', 'medium'), ('high', 'high（推荐）')]:
            self.effort_combo.addItem(eff_name, eff)
        saved_effort = self._settings.value('ai/reasoning_effort', 'high')
        eidx = self.effort_combo.findData(saved_effort)
        if eidx >= 0:
            self.effort_combo.setCurrentIndex(eidx)
        self.effort_combo.setCursor(Qt.PointingHandCursor)
        saved_thinking = self._settings.value('ai/thinking', None)
        if saved_thinking is None:
            self.chk_thinking.setChecked(saved_model == 'deepseek-v4-pro')
        else:
            self.chk_thinking.setChecked(saved_thinking in (True, 'true'))
        self.effort_combo.setEnabled(self.chk_thinking.isChecked())
        self.chk_thinking.toggled.connect(self.effort_combo.setEnabled)
        hint = QLabel('结果更深入，但耗时更长')
        hint.setObjectName('dlgSub')
        thinking_row.addWidget(thinking_label)
        thinking_row.addWidget(self.chk_thinking)
        thinking_row.addSpacing(12)
        thinking_row.addWidget(effort_label)
        thinking_row.addWidget(self.effort_combo)
        thinking_row.addStretch()
        thinking_row.addWidget(hint)
        box.addLayout(thinking_row)

        prompt_row = QHBoxLayout()
        prompt_label = QLabel('补充要求：')
        prompt_label.setObjectName('fieldLabel')
        self.prompt_input = QLineEdit(self._settings.value('ai/extra_prompt', ''))
        self.prompt_input.setPlaceholderText('可选，如：重点分析价格信息 / 列出所有提到的时间点')
        prompt_row.addWidget(prompt_label)
        prompt_row.addWidget(self.prompt_input, stretch=1)
        box.addLayout(prompt_row)

        privacy_row = QHBoxLayout()
        privacy_label = QLabel('匿名化：')
        privacy_label.setObjectName('fieldLabel')
        self.chk_anonymize = QCheckBox('发送前匿名化（昵称→用户N，去掉 IP 属地）')
        self.chk_anonymize.setCursor(Qt.PointingHandCursor)
        self.chk_anonymize.setChecked(
            self._settings.value('ai/anonymize', False) in (True, 1, 'true', '1'))
        self.chk_anonymize.setToolTip(
            '勾选后，首次发送的帖子内容会把评论者昵称替换为「用户1/用户2…」，'
            '并去掉评论的 IP 属地；作者与评论使用同一份昵称映射，保持上下文一致。')
        privacy_row.addWidget(privacy_label)
        privacy_row.addWidget(self.chk_anonymize)
        privacy_row.addStretch()
        box.addLayout(privacy_row)
        return frame

    # ==================== 样式 ====================

    def _apply_styles(self):
        self.setStyleSheet("""
            QDialog { background-color: #F4F5F7; }
            QLabel#dlgTitle { font-size: 16px; font-weight: 800; color: #1D2129; border: none; }
            QLabel#dlgSub { color: #86909C; font-size: 12px; border: none; }
            QLabel#fieldLabel { color: #4E5969; font-size: 12px; border: none; }
            QFrame#cfgFrame {
                background-color: #FFFFFF;
                border: 1px solid #E5E8EC;
                border-radius: 10px;
            }
            QLineEdit, QComboBox {
                background-color: #FFFFFF;
                border: 1px solid #E5E8EC;
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
                color: #1D2129;
            }
            QLineEdit:focus, QComboBox:focus { border-color: #2BAE66; }
            QPushButton#btnRun {
                background-color: #2BAE66;
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#btnRun:hover { background-color: #36C279; }
            QPushButton#btnRun:disabled { background-color: #A8DCC2; }
            QPushButton#btnMini {
                background-color: #FFFFFF;
                color: #1D2129;
                border: 1px solid #E5E8EC;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 12px;
            }
            QPushButton#btnMini:hover { border-color: #2BAE66; color: #1E8E55; }
            QPushButton#btnMini:disabled { color: #C9CDD4; }
            QPushButton#btnSend {
                background-color: #2BAE66;
                color: #FFFFFF;
                border: none;
                border-radius: 10px;
                font-size: 13px;
                font-weight: 700;
            }
            QPushButton#btnSend:hover { background-color: #36C279; }
            QPushButton#btnSend:disabled { background-color: #D8E4DD; color: #FFFFFF; }
            QScrollArea#chatArea { border: none; background: transparent; }
            QWidget#chatInner { background: transparent; }
            QFrame#bubbleAI {
                background-color: #FFFFFF;
                border: 1px solid #E5E8EC;
                border-radius: 14px;
            }
            QFrame#bubbleMe {
                background-color: #2BAE66;
                border: 1px solid #2BAE66;
                border-radius: 14px;
            }
            QLabel#bubbleMeta { color: #86909C; font-size: 10.5px; border: none; background: transparent; }
            QFrame#bubbleMe QLabel#bubbleMeta { color: #D6F2E4; }
            QLabel#bubbleTextMe { color: #FFFFFF; font-size: 13px; border: none; background: transparent; }
            QTextBrowser#bubbleTextAI {
                background: transparent;
                border: none;
                color: #1D2129;
                font-size: 13px;
            }
            QLabel#avatarAI {
                background-color: #E8F6EE;
                color: #1E8E55;
                border-radius: 15px;
                font-weight: 800;
                font-size: 12px;
            }
            QLabel#avatarMe {
                background-color: #2BAE66;
                color: #FFFFFF;
                border-radius: 15px;
                font-weight: 800;
                font-size: 12px;
            }
            QTextEdit#chatInput {
                background-color: #FFFFFF;
                border: 1px solid #E5E8EC;
                border-radius: 12px;
                padding: 8px 12px;
                font-size: 13px;
                color: #1D2129;
            }
            QTextEdit#chatInput:focus { border-color: #2BAE66; }
        """)
        # 样式会改变字号/行高，已有气泡需要按新字体重算高度
        QTimer.singleShot(0, self._refit_messages)

    def _refit_messages(self, verify=True):
        try:
            rows = self.findChildren(_ChatMessage)
        except RuntimeError:
            return  # 对话框已销毁
        for row in rows:
            row._fit()
        if verify:
            # 布局/字体可能在同一轮事件循环里才落定，再校正一次（只做一轮）
            QTimer.singleShot(80, lambda: self._refit_messages(False))

    # ==================== 聊天区 ====================

    def _add_message(self, role, text, meta=''):
        row = _ChatMessage(role, text, meta, self)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, row)
        self._scroll_bottom()
        return row

    def _scroll_bottom(self):
        def go():
            bar = self.chat_area.verticalScrollBar()
            bar.setValue(bar.maximum())
        QTimer.singleShot(0, go)

    def _flush_stream(self):
        row = self._stream_row
        if row is None:
            return
        row.set_markdown(self._stream_text or '…')
        n = len(self._stream_text)
        if n:
            self.status_label.setText(f'正在生成… 已输出 {n} 字（关闭窗口可取消）')
        self._scroll_bottom()

    # ==================== 发送 ====================

    def _toggle_key_visible(self):
        if self.key_input.echoMode() == QLineEdit.Password:
            self.key_input.setEchoMode(QLineEdit.Normal)
            self.btn_show_key.setText('隐藏')
        else:
            self.key_input.setEchoMode(QLineEdit.Password)
            self.btn_show_key.setText('显示')

    def _toggle_settings(self):
        self.cfg_frame.setVisible(not self.cfg_frame.isVisible())

    def _require_key(self):
        key = self.key_input.text().strip()
        if key:
            return key
        self.cfg_frame.setVisible(True)
        self.key_input.setFocus()
        QMessageBox.warning(self, '提示', '请先在「设置」里填写 DeepSeek API Key。')
        return ''

    def _save_prefs(self, api_key):
        s = self._settings
        saved = _save_api_key(s, api_key)
        s.setValue('ai/model', self.model_combo.currentData())
        s.setValue('ai/thinking', self.chk_thinking.isChecked())
        s.setValue('ai/reasoning_effort', self.effort_combo.currentData())
        s.setValue('ai/extra_prompt', self.prompt_input.text().strip())
        s.setValue('ai/anonymize', self.chk_anonymize.isChecked())
        if not saved and not self._key_save_warned:
            # 加密不可用：不写明文，本次运行仍可用，但要明确告知用户不会被保存
            self._key_save_warned = True
            QMessageBox.warning(
                self, '无法安全保存 API Key',
                '当前系统不支持 Windows DPAPI 安全存储，程序不会把 API Key 明文写入配置。\n'
                '本次运行期间可以正常使用，关闭程序后需要重新输入。')

    def _check_privacy_consent(self):
        """发送整帖前确认隐私风险；取消返回 False。"""

        return ensure_privacy_consent(self)

    def _start_summary(self):
        if self._running:
            return
        if self._started:
            return
        api_key = self._require_key()
        if not api_key:
            return
        if not self._check_privacy_consent():
            return
        self._save_prefs(api_key)
        self._started = True
        self.btn_summary.setEnabled(False)
        n = len(self.md_text)
        display = '帮我总结这个帖子的内容，重点看大家在讨论什么、有哪些关键信息。'
        self._add_message('user', display, meta='我 · ' + _short_time())
        content = self.md_text
        if self.chk_anonymize.isChecked():
            # 首轮就把匿名化后的内容写进历史，后续追问的上下文也不含昵称/IP 属地
            content = _anonymize_for_ai(content)
        first = _build_messages(content, self.prompt_input.text().strip())[1]
        self._api_messages.append({'role': 'user', 'content': first['content']})
        self._start_stream(api_key, first_request=True, total_chars=n)

    def _send_followup(self):
        if self._running:
            return
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        api_key = self._require_key()
        if not api_key:
            return
        if not self._check_privacy_consent():
            return
        self._save_prefs(api_key)
        self.input_edit.clear()
        self._add_message('user', text, meta='我 · ' + _short_time())
        self._api_messages.append({'role': 'user', 'content': text})
        self._start_stream(api_key, first_request=False)

    def _start_stream(self, api_key, first_request=False, total_chars=0):
        self._running = True
        self._stream_text = ''
        self.btn_send.setEnabled(False)
        self.btn_summary.setEnabled(False)
        self.input_edit.setEnabled(False)
        self.status_label.setText('正在通读帖子…' if first_request else '正在思考…')
        row = self._add_message('assistant', '…', meta='DeepSeek · 生成中')
        self._stream_row = row

        relay = _StreamRelay(self)
        relay.delta.connect(self._on_delta)
        relay.done.connect(self._on_stream_done)
        relay.fail.connect(self._on_stream_fail)
        relay.retry.connect(self._on_stream_retry)
        self._stream_relay = relay

        self._cancel_event = threading.Event()
        with self._http_lock:
            self._http_sessions = []
            sessions = self._http_sessions
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}] + self._api_messages
        _request_chat(
            api_key=api_key,
            messages=messages,
            model=self.model_combo.currentData(),
            proxy=self.proxy,
            thinking=self.chk_thinking.isChecked(),
            reasoning_effort=self.effort_combo.currentData(),
            on_delta=lambda piece: self._emit_safe(relay.delta, piece),
            on_done=lambda text: self._emit_safe(relay.done, text),
            on_fail=lambda msg: self._emit_safe(relay.fail, msg),
            on_retry=lambda attempt, delay: self._emit_retry_safe(relay, attempt, delay),
            cancel_event=self._cancel_event,
            session_holder=sessions,
            session_lock=self._http_lock,
        )

    # ==================== 流式回调 ====================

    @staticmethod
    def _emit_safe(sig, arg):
        try:
            sig.emit(arg)
        except RuntimeError:
            pass  # 对话框已销毁

    @staticmethod
    def _emit_retry_safe(relay, attempt, delay):
        try:
            relay.retry.emit(int(attempt), float(delay))
        except RuntimeError:
            pass  # 对话框已销毁

    def _on_stream_retry(self, attempt, delay):
        if not self._running or self._cancel_event.is_set():
            return
        self.status_label.setText(f'请求失败，{delay:.0f} 秒后自动重试（第 {attempt} 次）…')

    def _on_delta(self, piece):
        if not self._running:
            return
        self._stream_text += piece
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _on_stream_done(self, text):
        self._running = False
        self._flush_timer.stop()
        full = text or self._stream_text
        if self._stream_row is not None:
            self._stream_row.set_markdown(full)
            self._stream_row.set_meta('DeepSeek · ' + _short_time())
        self._api_messages.append({'role': 'assistant', 'content': full})
        self.status_label.setText(f'完成 · 本轮 {len(full)} 字，可以继续追问。')
        self.btn_copy.setEnabled(True)
        self.btn_save.setEnabled(True)
        self.input_edit.setEnabled(True)
        self.input_edit.setPlaceholderText('继续追问，例如：第 12 楼说的价格是多少？（Enter 发送，Shift+Enter 换行）')
        self.btn_send.setEnabled(True)
        self.btn_summary.setVisible(False)
        self.input_edit.setFocus()
        self._scroll_bottom()

    def _on_stream_fail(self, msg):
        self._running = False
        self._flush_timer.stop()
        first_turn = not any(m.get('role') == 'assistant' for m in self._api_messages)
        if self._api_messages and self._api_messages[-1].get('role') == 'user':
            # 失败时收回最后一条提问，重试不会重复带入上下文
            self._api_messages.pop()
        if first_turn:
            # 首轮失败：清掉这一问一答，回到「还没开始」的状态，按钮可重试
            self._drop_last_rows(2)
            self._started = False
            self._stream_row = None
            self.btn_summary.setEnabled(True)
            self.status_label.setText('生成失败，可重试。')
        else:
            if self._stream_row is not None:
                partial = self._stream_text.strip()
                body = (partial + '\n\n---\n' if partial else '') + f'**请求失败**：{msg}'
                self._stream_row.set_markdown(body)
                self._stream_row.set_meta('DeepSeek · 失败')
            self.status_label.setText('生成失败，可重试。')
        self.input_edit.setEnabled(True)
        self.btn_send.setEnabled(True)
        self.btn_copy.setEnabled(bool(self._transcript_md()))
        self.btn_save.setEnabled(bool(self._transcript_md()))
        # 用户主动取消/关闭对话框后不再弹失败框，避免关窗后还冒出阻塞提示
        if self.isVisible() and not self._cancel_event.is_set():
            QMessageBox.warning(self, 'AI 分析失败', msg)

    def _drop_last_rows(self, count):
        """移除聊天区最后 count 条真实消息（不含底部 stretch）。"""
        removed = 0
        for i in range(self.chat_layout.count() - 1, -1, -1):
            if removed >= count:
                break
            item = self.chat_layout.itemAt(i)
            w = item.widget() if item is not None else None
            if isinstance(w, _ChatMessage):
                self.chat_layout.takeAt(i)
                w.setParent(None)
                w.deleteLater()
                removed += 1

    # ==================== 复制 / 保存 ====================

    def _transcript_md(self):
        lines = []
        title = self.topic_title or '帖子'
        lines.append(f'# AI 分析对话 · {title}')
        lines.append('')
        for row in self._iter_messages():
            if row.role == 'user':
                lines.append('## 我')
            else:
                lines.append('## DeepSeek')
            lines.append('')
            lines.append(row.text_value or '')
            lines.append('')
        return '\n'.join(lines).strip() + '\n'

    def _iter_messages(self):
        for i in range(self.chat_layout.count()):
            w = self.chat_layout.itemAt(i).widget()
            if isinstance(w, _ChatMessage) and not w.text_value.startswith('已载入帖子内容'):
                yield w

    def _copy_transcript(self):
        md = self._transcript_md()
        if not md:
            return
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(md)
        self.status_label.setText('对话已复制到剪贴板。')

    def _save_transcript(self):
        md = self._transcript_md()
        if not md:
            return
        default_name = 'ai_chat.md'
        path, _ = QFileDialog.getSaveFileName(
            self, '保存对话', default_name, 'Markdown 文件 (*.md)')
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(md)
            self.status_label.setText(f'已保存: {path}')
        except PermissionError:
            QMessageBox.warning(self, '保存失败', f'没有权限写入该路径:\n{path}')
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, '保存失败', f'保存文件时出错:\n{e}')

    # ==================== 生命周期 ====================

    def _cancel_running(self):
        self._cancel_event.set()
        with self._http_lock:
            sessions, self._http_sessions = self._http_sessions, []
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        self._running = False

    def showEvent(self, event):
        super().showEvent(event)
        # 首次显示后控件宽度才定型，重算一遍气泡高度，避免文字被裁
        QTimer.singleShot(0, self._refit_messages)

    def reject(self):
        self._cancel_running()
        super().reject()

    def closeEvent(self, event):
        self._cancel_running()
        super().closeEvent(event)
