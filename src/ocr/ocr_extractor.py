# -*- coding: utf-8 -*-
"""OCR 文字识别模块 - 从评论图片中提取文字

优先使用 pytesseract（需安装 Tesseract OCR）。
支持自动检测 Tesseract 安装路径（Windows / macOS / Linux）。
"""
import io
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import random

from PySide6.QtCore import QObject, Signal

from ..core.config import (
    MAX_RETRIES, RETRY_BACKOFF_BASE,
    OCR_LANGUAGE, OCR_TESSERACT_PATH, OCR_IMAGE_TIMEOUT, OCR_MIN_IMAGE_SIZE,
    OCR_ENGINE, OCR_WORKERS, OCR_RAPID_CLS, OCR_RAPID_TEXT_SCORE,
    OCR_RAPID_DET_SIDE, OCR_RAPID_THREADS, OCR_DEVICE,
)
from ..core.limiter import AdaptiveRateLimiter
from ..core.identity import (
    create_session, image_headers, pick_language, pick_profile,
)
from ..core.file_log import log_error

# ==================== OCR 引擎检测 ====================


# add_dll_directory 返回的句柄一旦被垃圾回收，注册的目录就会失效，
# 所以必须一直持有引用（否则偶发「找不到 cublasLt64_12.dll」）。
_DLL_DIR_HANDLES = []


def _prepare_gpu_dlls():
    """把 pip 安装的 NVIDIA CUDA DLL 目录加到 Windows 的 DLL 搜索路径。

    `pip install nvidia-cudnn-cu12` 之类会把 cudnn64_9.dll / cublasLt64_12.dll
    放在 site-packages/nvidia/<lib>/bin，默认不在 PATH 里；不先用
    add_dll_directory 注册，onnxruntime 的 CUDA EP 会静默回退到 CPU。
    """
    if os.name != 'nt':
        return False
    added = []
    try:
        import site
        roots = []
        try:
            import nvidia  # noqa: F401  pip 安装的命名空间包
            roots.append(os.path.dirname(os.path.dirname(nvidia.__file__)))
        except Exception:
            pass
        for path in list(site.getsitepackages()) + [site.getusersitepackages()]:
            if path and path not in roots:
                roots.append(path)
        for root in roots:
            nv = os.path.join(root, 'nvidia')
            if not os.path.isdir(nv):
                continue
            for name in sorted(os.listdir(nv)):
                if name.startswith('__'):
                    continue
                bin_dir = os.path.join(nv, name, 'bin')
                if os.path.isdir(bin_dir) and bin_dir not in added:
                    try:
                        _DLL_DIR_HANDLES.append(os.add_dll_directory(bin_dir))
                        added.append(bin_dir)
                    except OSError:
                        pass
            if added:
                break
        if added:
            # 只 add_dll_directory 不够稳：onnxruntime 用 LoadLibrary 加全路径，
            # 其依赖（cublasLt64_12.dll 等）走标准搜索顺序，PATH 里也要有。
            current = os.environ.get('PATH', '')
            extra = os.pathsep.join(d for d in added if d not in current)
            if extra:
                os.environ['PATH'] = extra + os.pathsep + current
    except Exception:
        log_error('注册 CUDA DLL 目录失败（将回退 CPU）：\n' + traceback.format_exc())
    return bool(added)


_prepare_gpu_dlls()

# 尝试导入 pytesseract + PIL
try:
    import pytesseract
    from PIL import Image
    HAS_PYTESSERACT = True
except ImportError:
    HAS_PYTESSERACT = False
    pytesseract = None
    Image = None


def _rapidocr_spec():
    """RapidOCR 是否可导入（只查包，不加载 onnxruntime，避免拖慢启动）"""
    try:
        import importlib.util
        return importlib.util.find_spec('rapidocr_onnxruntime') is not None
    except Exception:
        return False


def _rapidocr_version():
    try:
        from importlib.metadata import version
        return 'rapidocr-onnxruntime ' + version('rapidocr-onnxruntime')
    except Exception:
        return 'rapidocr-onnxruntime (版本未知)'


# 识别结果后处理：截图里的「2026.11.02 13:36:32」经常被连成「2026.11.0213:36:32」
_DATE_TIME_RE = re.compile(r'(\d{4}[./-]\d{1,2}[./-]\d{1,2})[ \t]*(\d{1,2}:\d{2}(?::\d{2})?)')
# 含字母/数字/下划线/汉字才算有信息量；纯标点行多半是识别噪声
_MEANINGFUL_RE = re.compile(r'[\w\u4e00-\u9fff]')


def tidy_text(text):
    """整理 OCR 文本：日期与时间补空格、丢弃纯符号噪声行、压缩连续空行。

    只做「不会改变语义」的清洗，避免把有用内容删掉。
    """
    if not text:
        return text or ''
    text = text.replace('\u200b', '').replace('\xa0', ' ')
    lines = []
    for raw in text.split('\n'):
        line = raw.rstrip()
        line = _DATE_TIME_RE.sub(r'\1 \2', line)
        if line and not _MEANINGFUL_RE.search(line):
            continue
        lines.append(line)
    text = '\n'.join(lines)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


# Tesseract 在 Windows 的常见安装路径
TESSERACT_WIN_PATHS = [
    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
    r'D:\Program Files\Tesseract-OCR\tesseract.exe',
    r'E:\Program Files\Tesseract-OCR\tesseract.exe',
    # Scoop 安装路径
    os.path.expanduser(r'~\scoop\apps\tesseract\current\tesseract.exe'),
]


def find_tesseract_binary():
    """在系统中查找 tesseract 可执行文件

    优先级：config.OCR_TESSERACT_PATH（用户手动指定）> PATH > 常见安装路径。
    返回路径字符串；找不到返回 None。
    """
    # 0. 配置里手动指定的路径优先
    configured = (OCR_TESSERACT_PATH or '').strip()
    if configured:
        if os.path.isfile(configured):
            return configured
        log_error(f'配置的 OCR_TESSERACT_PATH 不存在，已忽略: {configured}')

    # 1. 检查 PATH
    found = shutil.which('tesseract') or shutil.which('tesseract.exe')
    if found:
        return found

    # 2. Windows 常见安装路径
    if os.name == 'nt':
        for p in TESSERACT_WIN_PATHS:
            if os.path.isfile(p):
                return p

    # 3. macOS Homebrew 路径
    if os.path.isdir('/opt/homebrew/bin'):
        candidate = '/opt/homebrew/bin/tesseract'
        if os.path.isfile(candidate):
            return candidate
    if os.path.isdir('/usr/local/bin'):
        candidate = '/usr/local/bin/tesseract'
        if os.path.isfile(candidate):
            return candidate

    return None


def get_tesseract_version(binary_path, timeout=10):
    """获取 Tesseract 版本号，验证可用性

    返回版本字符串；不可用返回 None。
    注意：偶尔会遇到系统繁忙/杀毒扫描导致 --version 迟迟不返回，
    因此超时给得比较宽松；该函数应在后台线程调用，不要放在 UI 线程。
    """
    if not binary_path or not os.path.isfile(binary_path):
        return None
    try:
        result = subprocess.run(
            [binary_path, '--version'],
            capture_output=True, text=True, timeout=timeout,
        )
        # 输出形如：
        # tesseract v5.3.0.20221222
        # leptonica-1.78.0 ...
        first_line = (result.stdout or result.stderr).strip().split('\n')[0]
        if result.returncode != 0:
            log_error(f'OCR 版本检测失败: {binary_path} --version 返回码 {result.returncode}: {first_line[:200]}')
            return None
        # 必须是真正的 Tesseract 输出；坏二进制/其他程序即使能跑也不能当 OCR 引擎
        if not first_line.lower().startswith('tesseract'):
            log_error(f'OCR 版本检测失败: {binary_path} --version 输出不是 Tesseract: {first_line[:200]}')
            return None
        return first_line
    except Exception as e:
        log_error(f'OCR 版本检测异常: {binary_path} --version 执行出错: {e!r}')
        return None


class OCRExtractor:
    """OCR 引擎单例（线程安全）

    使用前调用 available 属性检查是否可用。
    """

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._available = False
        self._engine = None           # 'rapidocr' / 'pytesseract'
        self._binary_path = None
        self._language = OCR_LANGUAGE  # 默认取 config.OCR_LANGUAGE
        self._version = None
        self._rapid = None            # RapidOCR 实例（首次识别时才加载模型）
        self._rapid_lock = threading.Lock()
        self._device = 'CPU'          # 'GPU'(DirectML) / 'CPU'
        self._init_engine()

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    @classmethod
    def peek_instance(cls):
        """已初始化则返回单例；未初始化返回 None（不触发探测、不会阻塞）"""
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（用于配置变更后重新初始化）"""
        with cls._lock:
            cls._instance = None

    def _init_engine(self):
        """探测引擎；任何异常都降级为「不可用」并记日志，绝不把异常抛给调用方"""
        try:
            self._detect_engine()
        except Exception:
            log_error('OCR 引擎初始化异常:\n' + traceback.format_exc())
            self._available = False

    def _detect_engine(self):
        want = (OCR_ENGINE or 'auto').strip().lower()

        # 1. RapidOCR（中文截图识别明显更准，且支持多线程并发）
        if want in ('auto', 'rapidocr'):
            if Image is not None and _rapidocr_spec():
                self._engine = 'rapidocr'
                self._version = _rapidocr_version()
                backend = self._gpu_backend()
                self._device = {'cuda': 'CUDA', 'dml': 'DML'}.get(backend, 'CPU')
                self._available = True
                return
            if want == 'rapidocr':
                log_error('OCR 引擎不可用：config.OCR_ENGINE 指定 rapidocr，'
                          '但未安装 rapidocr-onnxruntime（pip install rapidocr-onnxruntime）')
                self._available = False
                return

        # 2. Tesseract 回退
        if not HAS_PYTESSERACT:
            log_error('OCR 引擎不可用：未安装 pytesseract 或 Pillow')
            self._available = False
            return

        # 查找 tesseract 二进制
        binary = find_tesseract_binary()
        if not binary:
            log_error('OCR 引擎不可用：未找到 tesseract 二进制（PATH 与常见安装路径均未命中）')
            self._available = False
            return

        # 验证版本
        version = get_tesseract_version(binary)
        if not version:
            self._available = False
            return

        # 配置 pytesseract 使用该二进制
        pytesseract.pytesseract.tesseract_cmd = binary
        self._binary_path = binary
        self._version = version
        self._engine = 'pytesseract'
        self._available = True

    @property
    def available(self):
        return self._available

    @property
    def engine_name(self):
        return self._engine

    @property
    def version(self):
        return self._version

    @property
    def binary_path(self):
        return self._binary_path

    @property
    def language(self):
        return self._language

    @language.setter
    def language(self, value):
        self._language = value

    @property
    def supports_language_selection(self):
        """是否支持选择语言包。

        RapidOCR 内置中英文模型，没有（也不需要）语言包；只有 Tesseract
        才有 `--list-langs` 语言包概念。界面据此决定是否展示语言选择入口。
        """
        return self._engine == 'pytesseract'

    def available_languages(self):
        """返回 Tesseract 已安装的语言包列表

        RapidOCR 使用内置中英文模型，没有语言包，直接返回 []，
        绝不对 _binary_path=None 执行 subprocess（旧实现会误报并吞异常）。
        """
        if not self._available:
            return []
        if self._engine != 'pytesseract' or not self._binary_path:
            return []
        try:
            result = subprocess.run(
                [self._binary_path, '--list-langs'],
                capture_output=True, text=True, timeout=5,
            )
            lines = (result.stdout or '').split('\n')
            langs = []
            for line in lines[1:]:  # 跳过首行标题
                line = line.strip()
                if line:
                    langs.append(line)
            return langs
        except Exception:
            return []

    def warmup(self):
        """后台预热引擎，消除首图等待

        RapidOCR 首次调用要加载 3 个 onnx 模型（约 2~4 秒）。在加载完帖子、
        用户还没点按钮的空档里预热，点下去就是满速。
        """
        if not self._available:
            return False
        try:
            if self._engine == 'rapidocr':
                self._rapid_engine()
            elif self._engine == 'pytesseract' and self._binary_path:
                subprocess.run([self._binary_path, '--version'],
                               capture_output=True, timeout=10)
            return True
        except Exception:
            log_error('OCR 引擎预热失败:\n' + traceback.format_exc())
            return False

    def _rapid_engine(self):
        """懒加载 RapidOCR 实例（首次识别时才读模型，避免拖慢启动）

        参数按「多图并发」调优：检测端限制最长边、每会话线程数按并发数分摊。
        否则 8 个 worker × ONNX 默认全核线程会互相抢 CPU，比单会话还慢。
        装了 onnxruntime-directml 时优先走 GPU（同一张图约快 5~8 倍）。
        """
        with self._rapid_lock:
            if self._rapid is None:
                from rapidocr_onnxruntime import RapidOCR
                threads = int(OCR_RAPID_THREADS or 0)
                if threads <= 0:
                    parallel = OCR_WORKERS or min(8, os.cpu_count() or 4)
                    threads = max(1, min(4, (os.cpu_count() or 4) // max(1, parallel)))
                kwargs = dict(
                    use_cls=OCR_RAPID_CLS,
                    print_verbose=False,
                    intra_op_num_threads=threads,
                    det_limit_side_len=OCR_RAPID_DET_SIDE,
                    det_limit_type='max',
                )
                backend = self._gpu_backend()
                if backend == 'cuda':
                    gpu_kwargs = dict(kwargs)
                    gpu_kwargs.pop('intra_op_num_threads', None)
                    gpu_kwargs.update(det_use_cuda=True, rec_use_cuda=True, cls_use_cuda=True)
                    try:
                        self._rapid = RapidOCR(**gpu_kwargs)
                        self._device = 'CUDA'
                        return self._rapid
                    except Exception:
                        log_error('CUDA 初始化失败，回退 CPU：\n' + traceback.format_exc())
                        self._device = 'CPU'
                elif backend == 'dml':
                    gpu_kwargs = dict(kwargs)
                    gpu_kwargs.pop('intra_op_num_threads', None)
                    gpu_kwargs.update(det_use_dml=True, rec_use_dml=True, cls_use_dml=True)
                    try:
                        self._rapid = RapidOCR(**gpu_kwargs)
                        self._device = 'DML'
                        return self._rapid
                    except Exception:
                        log_error('DirectML 初始化失败，回退 CPU：\n' + traceback.format_exc())
                        self._device = 'CPU'
                self._rapid = RapidOCR(**kwargs)
            return self._rapid

    @staticmethod
    def _gpu_backend():
        """返回 'cuda' / 'dml' / None（取决于 config.OCR_DEVICE 与本机 onnxruntime 能力）"""
        mode = str(OCR_DEVICE or 'auto').lower()
        if mode in ('cpu', 'off'):
            return None
        try:
            import onnxruntime as ort
            providers = set(ort.get_available_providers())
        except Exception:
            log_error('检测 GPU provider 失败：\n' + traceback.format_exc())
            return None
        if 'CUDAExecutionProvider' in providers and mode in ('auto', 'cuda'):
            return 'cuda'
        if 'DmlExecutionProvider' in providers and mode in ('auto', 'dml'):
            return 'dml'
        if mode in ('cuda', 'dml'):
            log_error(f'OCR_DEVICE={mode} 但 onnxruntime 没有对应 provider，将回退 CPU'
                      '（CUDA 用 onnxruntime-gpu，DirectML 用 onnxruntime-directml）')
        return None

    def _rapid_extract(self, img):
        """RapidOCR 识别：返回按行拼接的文字"""
        import numpy as np

        arr = np.asarray(img.convert('RGB'))
        result, _ = self._rapid_engine()(arr)
        if not result:
            return ''
        lines = []
        for item in result:
            text = (item[1] or '').strip()
            try:
                score = float(item[2])
            except (TypeError, ValueError, IndexError):
                score = 1.0
            if text and score >= float(OCR_RAPID_TEXT_SCORE):
                lines.append(text)
        return tidy_text('\n'.join(lines))

    def _fallback_to_tesseract(self):
        """RapidOCR 出错时回退 Tesseract（可用才切换）"""
        if not HAS_PYTESSERACT:
            self._available = False
            return
        binary = find_tesseract_binary()
        if not binary or not get_tesseract_version(binary):
            self._available = False
            return
        pytesseract.pytesseract.tesseract_cmd = binary
        self._binary_path = binary
        self._engine = 'pytesseract'
        self._version = get_tesseract_version(binary)

    def extract_text(self, image_bytes, lang=None):
        """从图片字节提取文字

        Args:
            image_bytes: 图片二进制数据
            lang: 可选，指定语言（仅 Tesseract 用；默认用 self._language）

        Returns:
            识别到的文字字符串；失败返回 None；无文字返回空字符串；
            图片过小（OCR_MIN_IMAGE_SIZE）返回 ''（视为无文字，避免误识别）。
        """
        if not self._available:
            return None
        try:
            img = Image.open(io.BytesIO(image_bytes))
            # 过小的图片（表情/图标）直接跳过，避免误识别出一堆噪声
            if OCR_MIN_IMAGE_SIZE and min(img.size) < OCR_MIN_IMAGE_SIZE:
                return ''
            if self._engine == 'rapidocr':
                try:
                    return self._rapid_extract(img)
                except Exception:
                    log_error('RapidOCR 识别异常，本次回退 Tesseract:\n' + traceback.format_exc())
                    self._fallback_to_tesseract()
                    if self._engine != 'pytesseract':
                        return None
            # 转为 RGB 避免 RGBA / P 模式问题
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            try:
                text = pytesseract.image_to_string(
                    img, lang=lang or self._language,
                    timeout=OCR_IMAGE_TIMEOUT,
                )
            except RuntimeError:
                # pytesseract 在超时时会杀掉子进程并抛 RuntimeError：
                # 记录一张失败即可，不阻塞后续图片（旧实现会无限等待卡死的图）
                log_error(f'OCR 单张识别超时（>{OCR_IMAGE_TIMEOUT}s，引擎={self._engine}）')
                return None
            # 清理多余空白
            return tidy_text('\n'.join(
                line.rstrip() for line in text.split('\n')
            ))
        except Exception:
            # 识别失败（坏图/解码失败/引擎异常）记录后返回 None，由调用方计入失败
            log_error('OCR 识别异常:\n' + traceback.format_exc())
            return None

    def status_message(self):
        """返回可读的状态描述"""
        if self._available:
            if self._engine == 'rapidocr':
                device = {'CUDA': 'GPU (CUDA)', 'DML': 'GPU (DirectML)'}.get(
                    self._device, 'CPU')
                workers = OCR_WORKERS or ('1（GPU 串行）' if self._device != 'CPU' else '自动')
                return (f'OCR 引擎: RapidOCR (PP-OCRv4 / onnxruntime)\n'
                        f'版本: {self._version}\n'
                        f'语言: 内置中英文模型（无需语言包）\n'
                        f'设备: {device}\n'
                        f'并发: {workers}')
            return (f'OCR 引擎: pytesseract\n'
                    f'版本: {self._version}\n'
                    f'路径: {self._binary_path}\n'
                    f'语言: {self._language}')
        if (OCR_ENGINE or '').strip().lower() == 'rapidocr':
            return ('OCR 不可用：config.OCR_ENGINE 指定了 rapidocr，但未安装\n'
                    '请运行: pip install rapidocr-onnxruntime')
        if not HAS_PYTESSERACT:
            return ('OCR 不可用：未安装 pytesseract 或 Pillow\n'
                    '请运行: pip install pytesseract Pillow\n'
                    '或安装 RapidOCR（推荐）: pip install rapidocr-onnxruntime')
        return ('OCR 不可用：未找到 Tesseract OCR 二进制\n'
                'Windows: 从 https://github.com/UB-Mannheim/tesseract/wiki 下载安装\n'
                'macOS: brew install tesseract tesseract-lang\n'
                'Linux: sudo apt install tesseract-ocr tesseract-ocr-chi-sim\n'
                '或安装 RapidOCR（推荐）: pip install rapidocr-onnxruntime')


# ==================== OCR 后台 Worker ====================

class OCRWorker(QObject):
    """后台 OCR 识别工作线程

    - 输入：图片 URL 列表
    - 输出：{url: ocr_text} 字典
    - 复用图片下载的反爬策略（TLS 指纹伪装 + 自适应延迟）
    - 结果通过 finished 信号返回
    """

    log = Signal(str)
    progress = Signal(int, int)            # current, total
    finished = Signal(dict)                # {url: text}
    error = Signal(str)
    image_ocr_done = Signal(str, str)      # url, text（单张完成时立即推送）
    image_ocr_failed = Signal(str)         # url（下载或识别失败，不写库）

    def __init__(self, image_urls, language=None, use_proxy=False, proxy=None, local_paths=None):
        super().__init__()
        self.image_urls = image_urls
        self._stop = False
        self._extractor = OCRExtractor.get_instance()
        # 语言存在 worker 自己身上，不改共享单例——否则两个 OCR worker 并发时
        # 会互相覆盖对方的识别语言
        self._language = language or self._extractor.language
        # 已下载图片的本地路径 {url: path}，可直接读文件避免重复下载
        self._local_paths = local_paths or {}
        self._proxy = proxy if use_proxy else None
        # 每个工作线程独立 session（requests/curl_cffi 的 session 不保证线程安全）
        self._thread_local = threading.local()
        # 自适应限速（与图片下载一致的小基础延迟）
        self._rate_limiter = AdaptiveRateLimiter(
            log_callback=self._log, base_delay=0.3)
        # 并发数：下载与识别共用同一批线程
        self.workers = self._resolve_workers(len(image_urls))
        # run() 中创建的执行器与在途 future：供 stop() 跨线程尽力取消
        self._ocr_pool = None
        self._pending_futures = ()

    @staticmethod
    def _resolve_workers(total):
        """并发数：config.OCR_WORKERS 优先，0 = 自动

        自动规则：
        - GPU（CUDA / DirectML）：固定 1。这两个执行器都不支持一个进程里多会话
          同时推理（实测两线程直接崩进程，无 traceback）；单张 0.1~0.3 秒，
          串行已经够快，反而是下载更容易成为瓶颈。
        - CPU：8 个（与核数取小）。
        """
        if OCR_WORKERS:
            n = OCR_WORKERS
        elif OCRExtractor._gpu_backend():
            n = 1
        else:
            n = min(8, os.cpu_count() or 4)
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 4
        return max(1, min(n, total or 1))

    def _session(self):
        """当前线程专属的 HTTP session（TLS 指纹伪装，延迟创建）

        线程首次调用时随机挑一个完整设备档位（TLS/UA/Client Hints 同族），
        该线程后续所有图片请求复用同一档位。
        """
        session = getattr(self._thread_local, 'session', None)
        if session is None:
            profile = pick_profile()
            self._thread_local.profile = profile
            self._thread_local.lang = pick_language()
            session = create_session(profile, proxy=self._proxy)
            self._thread_local.session = session
        return session

    def stop(self):
        """请求停止：置标志并尽力取消还没开始的识别任务

        正在跑的网络请求/推理无法强行中断（也不该依赖外部 terminate）：
        run() 会在 0.1s 级内跳出主循环，并在 finally 里取消排队中的 future。
        """
        self._stop = True
        for fut in list(self._pending_futures or ()):
            try:
                fut.cancel()
            except Exception:
                pass

    def _log(self, msg):
        """后台线程写日志：已停止则不再 emit，对象已销毁时吞掉 RuntimeError

        run() 返回后下载/识别线程可能还在收尾；此时向已删除的 QObject
        emit 信号会抛 RuntimeError，不能让它把后台线程打崩。
        """
        if self._stop:
            return
        try:
            self.log.emit(msg)
        except RuntimeError:
            pass

    def _sleep(self, seconds):
        """可被 stop() 打断的等待（避免「停止」等满整个退避时间）"""
        remaining = float(seconds)
        while remaining > 0:
            if self._stop:
                return False
            chunk = 0.1 if remaining > 0.1 else remaining
            time.sleep(chunk)
            remaining -= chunk
        return not self._stop

    def _build_headers(self, url=''):
        """构建图片下载请求头（设备档位与 session 保持一致）"""
        return image_headers(
            self._thread_local.profile,
            referer='https://m.douban.com/',
            lang=self._thread_local.lang,
            url=url,
        )

    def _download_image(self, url, idx, total):
        """获取图片字节（本地缓存优先，否则带重试下载）

        成功返回 (bytes, from_local)；失败/取消返回 (None, False)。
        """
        # 已下载过的图片直接读本地文件（省流量且更快）
        local = self._local_paths.get(url)
        if local:
            try:
                with open(local, 'rb') as f:
                    return f.read(), True
            except OSError:
                pass  # 本地文件读失败则回退网络下载

        for attempt in range(MAX_RETRIES):
            if self._stop:
                return None, False
            try:
                if attempt > 0:
                    d_min, _ = self._rate_limiter.get_delay()
                    backoff = max(RETRY_BACKOFF_BASE ** attempt, d_min) + random.uniform(0, 1)
                    self._log(f'  [{idx+1}/{total}] 重试下载 (等待 {backoff:.1f}s)')
                    if not self._sleep(backoff):
                        return None, False
                else:
                    # 请求前微延迟
                    d_min, _ = self._rate_limiter.get_delay()
                    if not self._sleep(random.uniform(0.1, max(0.3, d_min * 0.2))):
                        return None, False

                resp = self._session().get(
                    url, headers=self._build_headers(url), timeout=OCR_IMAGE_TIMEOUT,
                )
                # 兼容 curl_cffi / requests
                status = getattr(resp, 'status_code', 0)
                if status == 200:
                    self._rate_limiter.report_success()
                    return (resp.content if hasattr(resp, 'content') else resp.read()), False
                if status in (403, 429, 418, 503):
                    self._log(f'  [{idx+1}/{total}] 下载被限 (HTTP {status})，退避重试...')
                    self._rate_limiter.report_failure()
                    continue
                self._log(f'  [{idx+1}/{total}] 下载失败 (HTTP {status})')
                return None, False
            except Exception as e:
                # requests / curl_cffi 网络异常
                if attempt < MAX_RETRIES - 1:
                    self._rate_limiter.report_failure()
                    if not self._sleep(random.uniform(1, 3)):
                        return None, False
                else:
                    self._log(f'  [{idx+1}/{total}] 下载出错: {e}')
        return None, False

    def run(self):
        import queue
        from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

        if self._stop:
            # 启动即被取消：直接收尾，保证 UI 的「停止」不会卡在探测/排队上
            self.finished.emit({})
            return
        if not self._extractor.available:
            self.error.emit(self._extractor.status_message())
            return

        results = {}
        total = len(self.image_urls)
        engine = 'RapidOCR' if self._extractor.engine_name == 'rapidocr' else 'Tesseract'
        self.log.emit(f'开始 OCR 识别 {total} 张图片（{engine}，{self.workers} 并发）...')
        self.progress.emit(0, total)

        done = 0
        recognized = 0
        failed = 0
        max_pending = max(4, self.workers * 2)

        # 下载与识别拆成两段流水线：下载线程（含礼貌延迟）和识别池互不阻塞，
        # 识别池始终有活干，墙钟从「下载+识别 串行」变成「取快的那个」。
        in_queue = queue.Queue(maxsize=max(8, self.workers * 6))
        pending = {}                 # future -> url

        def producer():
            """下载线程池：并发取图放进队列；只做网络，不碰 Qt 信号

            GPU 单张识别只要 0.1~0.3 秒，一个线程挨个下载反而成了瓶颈，
            所以这里用少量并发先把图片备好（队列有界，不会把内存撑爆）。
            """
            from concurrent.futures import ThreadPoolExecutor as _DownloadPool

            dl_workers = max(2, min(4, os.cpu_count() or 2))
            gate = threading.Semaphore(max(4, dl_workers * 2))

            def fetch(idx, url):
                blob = None
                try:
                    blob, _ = self._download_image(url, idx, total)
                except Exception:
                    blob = None
                finally:
                    gate.release()
                while not self._stop:
                    try:
                        in_queue.put((idx, url, blob), timeout=0.2)
                        break
                    except queue.Full:
                        continue

            with _DownloadPool(max_workers=dl_workers) as pool:
                for idx, url in enumerate(self.image_urls):
                    if self._stop:
                        break
                    gate.acquire()
                    try:
                        pool.submit(fetch, idx, url)
                    except RuntimeError:
                        gate.release()
                        break

        extractor = self._extractor
        language = self._language

        def recognize(url, blob):
            # 只捕获局部变量：取消后这里的线程可能比 worker 活得久。
            # 已停止时即使任务被调度到也直接返回，不再白跑一次推理。
            if self._stop:
                return url, None
            return url, extractor.extract_text(blob, lang=language)

        def record(url, text):
            nonlocal done, recognized, failed
            done += 1
            if text is None:
                failed += 1
                self.log.emit(f'  [{done}/{total}] 识别失败')
                self.image_ocr_failed.emit(url)
            else:
                results[url] = text
                self.image_ocr_done.emit(url, text)
                if text:
                    recognized += 1
                    # 隐私：日志只记字数/引擎，绝不落盘识别出的原文
                    self.log.emit(f'  [{done}/{total}] ✓ 文本 {len(text)} 字（{engine}）')
                else:
                    self.log.emit(f'  [{done}/{total}] 无文字')
            self.progress.emit(done, total)

        dl_thread = threading.Thread(target=producer, daemon=True)
        dl_thread.start()
        ocr_pool = ThreadPoolExecutor(max_workers=self.workers)
        self._ocr_pool = ocr_pool
        try:
            while True:
                # 1) 把已下载好的图片送进识别池（限制在途数量，控制内存峰值）
                while (not self._stop and len(pending) < max_pending
                       and not in_queue.empty()):
                    idx, url, blob = in_queue.get()
                    if blob is None:
                        # 下载失败：不写库，计入失败（可重试）
                        done += 1
                        failed += 1
                        self.image_ocr_failed.emit(url)
                        self.progress.emit(done, total)
                        continue
                    pending[ocr_pool.submit(recognize, url, blob)] = url
                # 快照在途 future，stop() 可据此跨线程取消排队任务（tuple 赋值原子）
                self._pending_futures = tuple(pending)

                if self._stop:
                    break
                if not pending:
                    if not dl_thread.is_alive() and in_queue.empty():
                        break
                    time.sleep(0.05)
                    continue

                # 2) 谁先识别完就立刻出结果：边识别边刷新日志/进度
                ready, _ = wait(list(pending), timeout=0.1,
                                return_when=FIRST_COMPLETED)
                for fut in ready:
                    url = pending.pop(fut)
                    try:
                        url, text = fut.result()
                    except Exception as e:
                        done += 1
                        failed += 1
                        self.log.emit(f'  识别任务异常: {e}')
                        self.image_ocr_failed.emit(url)
                        self.progress.emit(done, total)
                        continue
                    record(url, text)
        finally:
            self._pending_futures = ()
            self._ocr_pool = None
            if self._stop:
                # 先逐张取消排队中的任务，再 shutdown(cancel_futures=True) 兜底；
                # 正在跑的推理无法中断，让它们在后台跑完即可（不写结果、不碰 Qt），
                # run() 因此能快速返回，不依赖外部 terminate。
                for fut in list(pending):
                    fut.cancel()
                ocr_pool.shutdown(wait=False, cancel_futures=True)
            else:
                ocr_pool.shutdown(wait=True)

        # 无论正常完成还是中途取消都发出 finished，携带已完成的部分结果
        # （不发信号会导致 worker 线程永不退出）
        tail = f', {failed} 张失败/无结果（未写入数据库）' if failed else ''
        if self._stop:
            self.log.emit(f'OCR 已取消: 已处理 {len(results)} 张, {recognized} 张含文字{tail}')
        else:
            self.log.emit(f'OCR 完成: {len(results)} 张图片已处理, {recognized} 张含文字{tail}')
        self.finished.emit(results)
