# 豆瓣小组帖子采集器

**语言 / Language：** 中文 | [English](#english-documentation)

基于 PySide6 的桌面 GUI 应用，通过豆瓣移动端 Rexxar API 爬取小组帖子内容、评论和图片，内置多层反反爬机制。

> ⚠️ **免责声明：本项目仅供学习与技术研究使用（非商业用途）。**
> 请遵守豆瓣用户协议与 robots 协议，控制请求频率；请勿用于商业用途、大规模采集或侵犯他人合法权益。使用本项目产生的任何后果由使用者自行承担。

## 功能

- **帖子爬取** — 输入帖子 URL 或 ID，获取标题、作者、正文、小组、发布时间、点赞/回复数等完整信息
- **批量爬取** — 支持多行 / 逗号分隔批量输入，自动排队逐个爬取
- **增量更新** — 已入库的帖子一键只拉新评论（按楼层接续，实测新增 N 条），历史数据不重抓、不清空
- **评论抓取** — 并发多页获取全部评论，含楼层、作者、IP 属地、回复链等
- **楼中楼回复** — 自动把豆瓣 API 的 `ref_comment` 归一化成回复关系：评论卡片显示「↳ 回复 #楼层 @作者」并在正文上方引用被回复内容，TXT / CSV / Markdown 导出同样带回复信息（历史数据读取时现场回填楼层，无需重抓）
- **图片提取与下载** — 自动识别帖子和评论中的图片（支持 `<img>`、`data-src`、裸图床链接），一键批量下载
- **OCR 图片文字识别** — 对评论中的图片运行 OCR 提取文字，结果在评论中可视化展示并持久化到数据库（推荐安装 RapidOCR，未装时回退 Tesseract）
- **富文本渲染** — 帖子内容和评论使用 QWebEngineView 原生渲染，支持图片、表格等 HTML 内容
- **搜索高亮** — 在帖子内容和评论中关键词搜索，支持上一个 / 下一个跳转
- **数据持久化** — SQLite 数据库自动保存爬取结果
- **数据库浏览** — 内置浏览器查看、搜索、加载、导出、删除历史记录
- **导出** — 支持 JSON / TXT / CSV / Markdown 格式导出（TXT 自动包含 OCR 识别文字）
- **AI 分析** — 把帖子与评论整理成 Markdown 交给 DeepSeek 分析（需配置 API Key），发送前可匿名化昵称 / IP 属地
- **反反爬** — 多层防护策略（见下方）
- **隐私保护** — 代理账号密码与 API Key 使用 Windows DPAPI 加密存储（不可用时不会写明文）；AI 首次发送前弹窗确认
- **数据安全** — 抓取结果单事务原子保存，失败回滚并如实报错；评论数异常缩水时拒绝覆盖历史；启动自动备份 + 数据库完整性自检
- **稳定交互** — 停止 / 关闭改为异步收尾，不再强制终止线程；重复启动提示已在运行；窗口位置、当前视图、导出目录自动记忆
- **工程质量** — 内置 pytest 单元测试与 GitHub Actions CI（Windows / Python 3.12），提供 pyproject.toml 与日志级别开关

---

## 豆瓣 API 说明

本工具使用豆瓣移动端 **Rexxar API v2**，这是豆瓣官方 m.douban.com 前端调用的接口，格式为 JSON，无需登录即可访问公开数据。

### 接口列表

#### 1. 获取帖子详情

```
GET https://m.douban.com/rexxar/api/v2/group/topic/{topic_id}
```

**返回字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 帖子 ID |
| `title` | string | 帖子标题 |
| `content` | string | 正文 HTML |
| `abstract` | string | 摘要（投票帖等特殊类型使用） |
| `author` | object | 作者信息 `{name, uid, avatar, …}` |
| `group` | object | 小组信息 `{name, id, …}` |
| `comments_count` | int | 总评论数 |
| `like_count` | int | 点赞数 |
| `create_time` | string | 发布时间（ISO 8601 格式） |
| `photos` | array | 帖子配图 |

#### 2. 获取评论列表

```
GET https://m.douban.com/rexxar/api/v2/group/topic/{topic_id}/comments
```

**请求参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `start` | int | 偏移量（从 0 开始） |
| `count` | int | 每页条数（传 100；实测单页上限 100，传更大值也只回 100 条） |
| `order` | string | 排序方式（固定 `asc`，按楼层升序） |

**返回字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `total` | int | 评论总数 |
| `comments` | array | 评论列表 |

每条评论包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `floor` | int | 楼层号 |
| `author` | object | 作者 `{name, uid, avatar}` |
| `content` | string | 评论正文 HTML |
| `text` | string | 纯文本（部分场景） |
| `create_time` | string | 发布时间 |
| `ip_location` | string | IP 属地（如"北京"） |
| `likes` | int | 点赞数 |
| `reply_to` | object | 被回复的评论 `{floor, author, text}` |
| `photos` | array | 评论配图（嵌套在 `image.normal.url` 中） |

### 预热页面（模拟真实浏览路径）

| 页面 | URL | 说明 |
|------|-----|------|
| 豆瓣移动端首页 | `https://m.douban.com/` | 必访：服务端在这里下发 bid cookie |
| 小组首页 | `https://m.douban.com/group/` | 第二跳（从预热路径随机挑选） |
| 帖子页面 | `https://m.douban.com/group/topic/{topic_id}/` | 接口 Referer 指向它；m 站已不提供该 HTML 页（实测各种 UA 恒 404），因此不实际请求 |

> 只保留实测可用的预热页：`/explore` 恒 404，`/movie/`、`/gallery/`、`/subject_collection/hot_movies` 在当前网络下经常十几秒不响应，已从预热路径中移除。

---

## 爬取逻辑流程

```
用户输入（URL/ID）
      │
      ▼
┌─────────────────────────────┐
│ 1. 解析输入                  │
│    正则提取帖子 ID           │
│    支持 URL / 纯数字 / 批量多行 │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ 2. 选择设备档位并建 Session  │
│    ├─ Chrome 131 / Android  │
│    │   impersonate=         │
│    │   chrome131_android    │
│    └─ Safari 18 / iOS 18    │
│        impersonate=         │
│        safari180_ios        │
│    TLS / UA / Client Hints  │
│    来自同一档位，不再拼凑    │
│    回退：TLSAdapter         │
│        (自定义 Cipher Suite │
│         + ALPN + TLS 1.2+)  │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ 3. 预热并从响应拿 bid        │
│    GET 首页                 │
│    └─ 服务端下发 bid cookie │
│       (11 字符 base64url)   │
│    不伪造 bid / dbcl2 / ck  │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ 4. 浏览路径预热 (Warmup)     │
│    ① GET 首页               │
│       └─ 导航头 + 档位 UA   │
│       └─ delay 0.4~1.0s     │
│    ② GET 小组首页（子频道） │
│       └─ Referer: 首页      │
│       └─ delay 0.4~1.0s     │
│    ③ 接口 Referer 指向帖子页 │
│       (m 站已不提供该 HTML  │
│        页，实测恒 404，不请求)│
│                              │
│    目的：构建完整 Referer 链， │
│    让服务端看到真实用户路径    │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ 5. 获取帖子详情              │
│    GET /rexxar/api/v2/      │
│        group/topic/{id}     │
│                              │
│    失败则重试（最多4次）：    │
│    ├─ 指数退避 + 尊重       │
│    │   Retry-After 响应头    │
│    ├─ 连续被拦才更换完整身份 │
│    │   (新 session + 重新预热│
│    │    + 至少间隔 45s)      │
│    └─ 连续 5 次被拦直接熔断  │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ 6. 获取评论                  │
│                              │
│    第1页：顺序请求           │
│    GET .../comments          │
│    ?start=0&count=100&order=asc │
│    └─ 拿到 total 总数        │
│                              │
│    剩余页：并发请求           │
│    ThreadPoolExecutor        │
│    并发数: 被限→1, 正常→2    │
│    ┌─ 每个线程独立 Session   │
│    ├─ 随机错开 0.1~0.6s     │
│    ├─ 继承主 Session Cookie │
│    └─ 不按页换 UA / 伪造 bid │
│                              │
│    首页返回空列表但帖子有评论 │
│    → 判定被限流/风控并熔断   │
│    结果按 offset 排序合并    │
│    自动补全缺失的 floor 字段 │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ 7. 提取图片 URL              │
│    来源：                    │
│    ├─ 帖子正文 content       │
│    │   ├─ <img src="...">   │
│    │   ├─ data-src (懒加载) │
│    │   └─ 裸图床链接         │
│    └─ 评论 content + photos │
│        ├─ 正文内 <img>      │
│        └─ photos[].image    │
│           .normal.url       │
│                              │
│    去重 (seen set)           │
│    过滤: .svg/.ico/.css/.js │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ 8. 持久化到 SQLite          │
│    ├─ save_topic()          │
│    ├─ save_comments()       │
│    └─ save_images()         │
│       (INSERT OR IGNORE 去重)│
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ 9. 渲染展示                  │
│    ├─ QWebEngineView 渲染   │
│    │   HTML5 + 内嵌 CSS      │
│    ├─ 安全清洗 HTML          │
│    │   (去 script/style/     │
│    │    iframe/事件属性)     │
│    └─ 搜索高亮 (JS注入)     │
│       TreeWalker 遍历文本节点 │
└─────────────────────────────┘
```

### 自适应反反爬策略

```
请求前: 微抖动 0.05~0.15s
    │
    ▼
发送请求（头组合来自当前设备档位，不自造矛盾指纹）
    │
    ├── 成功 ──► note_success()
    │              ├─ 连续被拦计数清零
    │              └─ 连续成功 >60s → 重置延迟等级
    │
    └── 被限 ──► note_block()
                   ├─ 累计连续被拦次数 +1
                   ├─ 延迟等级 +1 (最高8级)
                   ├─ 冷却时间 = max(Retry-After, 20s × 2^(n-1))，上限 120s
                   ├─ 连续 2 次被拦 → 更换完整身份
                   │   ├─ 换设备档位（Chrome/Safari 随机）
                   │   ├─ 新建 session（新 TLS 会话）
                   │   ├─ 重新预热并拿新 bid
                   │   └─ 两次换身份至少间隔 45s
                   ├─ 连续 5 次被拦 → 熔断中止本次任务
                   │   （保留已抓到的部分数据，不覆盖历史完整数据）
                   └─ 单次请求最多重试 4 次，仍失败则报错
```

### 反爬检测项

请求返回后逐项检查，任一命中即视为被反爬：先累计次数并冷却重试；连续命中才升级为更换完整身份，连续 5 次直接熔断。

| 检测项 | 判断条件 |
|--------|----------|
| HTTP 状态码 | `403` / `429` / `418` / `503` |
| 登录重定向 | 响应 URL 包含 `login` |
| 内容类型异常 | `Content-Type: text/html`（API 应返回 JSON） |
| 验证码页面 | 响应体包含 `captcha` |
| 反爬关键词 | 响应体前 512 字符含 `rate_limit` / `too_many` / `verify` / `challenge` / `block` / `访问过于频繁` / `请稍后再试` |
| 响应体过短 | `len(text) < 20` 字节 |
| 安全 Cookie | `Set-Cookie` 含 `captcha` / `sec=` / `_sec` / `challenge` |

---

## 反反爬配置

所有配置集中在 `src/core/config.py`，可调参数：

### 设备档位（核心）

一个档位同时决定 TLS 指纹（curl_cffi impersonate）、User-Agent 和 Client Hints，
保证三者同族同版本，不再出现「Safari UA + Chrome TLS」这类矛盾组合：

| 档位 | impersonate | User-Agent | Client Hints |
|------|-------------|------------|--------------|
| Chrome 131 / Android 10 | `chrome131_android` | Android 10 Mobile Chrome 131 | 发 Sec-CH-UA / Mobile / Platform |
| Safari 18 / iOS 18 | `safari180_ios` | iPhone OS 18_0 Safari 18.0 | 不发（Safari 本来就没有） |

补充说明：

- **bid 只从服务端拿**：首页响应会 `Set-Cookie: bid=<11 字符 base64url>`，程序保存该 cookie；**不再**自己伪造 16 位 hex bid，也不写空 `dbcl2` / `ck`。
- **不发未被申请的 client hints**：豆瓣没有通过 `Accept-CH` 请求 Device-Memory / Downlink / ECT / RTT / Viewport-Width，主动发送反而是脚本特征，已全部移除。
- **Accept-Encoding 交给 curl_cffi**：按档位自动使用 `gzip, deflate, br, zstd`（与真实浏览器一致），不再手写 `gzip, deflate`。
- **Sec-Fetch-Site 按真实域名关系计算**：图片在 `img*.doubanio.com`，跨注册域时发 `cross-site`，不再写死 `same-origin`。
- **UA 在同一 session 内固定**：评论页并发线程各自复用一套档位，不再每页 / 每次重试换 UA。

### 身份轮换与熔断

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `IDENTITY_ROTATE_MIN_INTERVAL` | 45s | 两次更换完整身份的最小间隔 |
| `IDENTITY_ROTATE_AFTER_BLOCKS` | 2 | 连续被拦多少次后升级为换身份 |
| `MAX_CONSECUTIVE_BLOCKS` | 5 | 连续被拦多少次直接熔断中止 |
| `BLOCK_COOLDOWN_BASE` | 20s | 被拦冷却基数（指数增长） |
| `BLOCK_COOLDOWN_MAX` | 120s | 单次冷却上限 |
| `RETRY_AFTER_MAX` | 120s | 尊重服务端 `Retry-After` 的最大等待 |

> 「更换身份」不只是换 UA：会新建 session（新 TLS 会话）+ 换设备档位 + 重新预热拿新 bid。只换 UA 而沿用同一 TLS 会话没有意义，服务端标记的是整个连接身份。

### 请求超时与重试

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `DEFAULT_TIMEOUT` | 25s | API / 图片单次请求超时（正常请求 1s 内完成，25s 只用于兜住网络抖动） |
| `WARMUP_TIMEOUT` | 8s | 预热导航页超时（页面仅几十 KB，快速失败后补一次首页拿 bid） |
| `SHORT_LINK_TIMEOUT` | 10s | 分享短链跳转解析超时 |
| `MAX_RETRIES` | 4 | 最大重试次数 |
| `RETRY_BACKOFF_BASE` | 2s | 重试退避基数（指数增长） |

### 并发控制

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `COMMENTS_PER_PAGE` / `COMMENTS_PAGE_STEP` | 100 | 每页评论条数；实测接口单页上限 100，300 条评论 3 页即可拉完 |
| `MAX_CONCURRENT_PAGES` | 2 | 正常并发页数（降低并发以贴合真实用户行为） |
| `MAX_CONCURRENT_ADAPTIVE_MIN` | 1 | 被限时最低并发数（降级后串行） |
| `IMAGE_DOWNLOAD_CONCURRENCY` | 6 | 图片下载并发数（静态 CDN；实测 2→6 并发把 16 张图从 20~54s 降到 3~4.5s） |

### 自适应延迟

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `ADAPTIVE_DELAY_MIN` | 1.0s | 正常延迟下界 |
| `ADAPTIVE_DELAY_MAX` | 15.0s | 最高延迟上界（8级被限时） |
| `ADAPTIVE_BACKOFF_FACTOR` | 1.5× | 每级乘法因子 |
| `ADAPTIVE_COOLDOWN` | 60.0s | 冷却时间：连续成功此时间后恢复默认延迟 |

---

## 隐私与数据安全

| 敏感项 | 存储方式 | 说明 |
|--------|----------|------|
| DeepSeek API Key | Windows DPAPI 加密（`dpapi:` 前缀密文，QSettings 保存） | 加密失败时不保存、不回退明文；解密失败会提示重新输入 |
| 代理地址（可能含账号密码） | Windows DPAPI 加密 | 旧版本留下的明文代理地址首次读取时自动迁移为密文；报错与日志中的 URL 凭证统一脱敏为 `scheme://***@host` |
| AI 分析内容 | 发送前确认 + 可选匿名化 | 首次发送前弹窗确认；默认开启「匿名化昵称与 IP 属地」：昵称按出现顺序替换为「用户1 / 用户2…」，删除 IP 属地与 UID 字段；群组名、时间、楼层、正文语义保留 |
| OCR 识别文本 | 只存数据库，不写日志 | 日志只记录识别字数与引擎，不落具体文字 |

非 Windows 平台或 DPAPI 不可用时，程序会明确提示「当前系统不支持安全存储，不会保存明文 Key」，代理地址仅在本次会话内以明文保有并提示，可由用户自行决定是否使用。

---

## 数据可靠性与备份

- **原子保存**：一次抓取的帖子 / 评论 / 图片在同一事务内写入，任一步失败整体回滚，并弹出「数据保存失败，已回滚」而不是假装成功
- **覆盖保护**：新评论数比库中少 20% 以上会在日志中警告，少 50% 以上直接拒绝覆盖（防止被限流时用残缺数据删光历史）
- **完整性判定**：按实际请求到的 offset 集合判断是否完整；发生熔断 / 被限流时强制标记为不完整，绝不覆盖历史完整数据
- **自动备份**：每次启动用 `VACUUM INTO` 备份到数据目录下 `backups/`，保留最近 5 份；启动时执行 `PRAGMA quick_check` 并记录异常
- **图片状态**：`images.stale` 标记不再出现的旧图；下载状态写库失败会重试并告警，坏图（魔数 / 大小 / Pillow 校验失败）会被重新下载
- **数据库位置**：`DBP_DB_FILE` 环境变量 > 打包运行时的 `%LOCALAPPDATA%\douban-post-crawler\douban_data.db` > 源码根目录 `douban_data.db`

---

## 工程化（开发）

```bash
# 安装开发依赖（pytest / ruff）
pip install -e ".[dev]"

# 运行单元测试
python -m pytest -q
```

- 测试用例在 `tests/`（设备档位与请求头、配置约束、限速器、日志、SQLite 读写与迁移、scraper 辅助函数、敏感配置加密、AI 匿名化），CI 配置见 `.github/workflows/ci.yml`（Windows + Python 3.12：compileall + pytest）
- 日志：默认写 `logs/dbp.log`（2MB × 3 滚动）；`DBP_LOG_LEVEL=DEBUG|INFO|WARNING` 调整级别，`DBP_LOG_CONSOLE=1` 同时输出到控制台
- 代码风格：ruff（配置在 `pyproject.toml`，line-length 100）

---

## 界面

浅色主题，三栏布局：左侧导航（帖子内容 / 评论 / 图片 / 数据库），中间内容区，右侧帖子概览与快捷操作。

- **帖子内容**：正文富文本渲染（标题、列表、引用、表格、代码块、内嵌图片）
- **评论**：按楼层升序排列，回复关系显示为「↳ 回复 #N @作者」并附被引用评论的片段
- **图片**：瀑布流画廊，完整展示图片，悬停提示「点击查看」，点击放大
- **右栏**：帖子概览（回复 / 点赞 / 作者 / 小组 / 发布）、快捷操作（增量更新、下载图片、OCR、AI 分析）、导出（JSON / TXT / CSV / MD）、全文搜索（上一个 / 下一个 / 清除）

---

## 环境要求

- Python 3.12+（源码使用 PEP 701 f-string 语法，低版本无法启动）
- 依赖包见下方安装说明

## 安装

```bash
# 克隆仓库
git clone <repo-url>
cd dbp

# 创建虚拟环境（推荐）
python -m venv venv
venv\Scripts\activate     # Windows
# source venv/bin/activate  # macOS / Linux

# 安装依赖
pip install -r requirements.txt
```

`requirements.txt`（已锁定主版本上界，避免未来大版本破坏兼容）：

```
PySide6>=6.5.0,<7
PySide6-WebEngine>=6.5.0,<7
requests>=2.28.0,<3
curl_cffi>=0.8.0,<1        # 可选：TLS 指纹伪装（实测 0.15.0）
pytesseract>=0.3.10,<0.4   # 可选：OCR 兜底（配合 Tesseract 引擎）
Pillow>=10.0.0,<13         # 可选：OCR 图片处理
# 可选（推荐）：pip install "rapidocr-onnxruntime>=1.3.0,<2"
```

> `curl_cffi` 为可选依赖。安装后爬虫按随机设备档位启用对应 TLS 指纹伪装（Chrome 131 / Android 或 Safari 18 / iOS，含 JA3 + HTTP/2 Settings），UA 与 Client Hints 同族，大幅降低被识别为爬虫的概率。旧版 curl_cffi 不认识新档位时会自动退回 `chrome120`；完全不安装则使用内置增强 TLSAdapter（手动设置 Cipher Suite / ALPN / TLS 1.2+），也能应对大部分反爬场景，但指纹一致性弱于 impersonate。

### OCR 依赖安装（可选）

如需启用「OCR 识别图片文字」功能，推荐安装 **RapidOCR**（内置中英文模型，中文准确率高于 Tesseract，且支持多线程并发）：

```bash
pip install rapidocr-onnxruntime
```

装好后无需其他配置，程序会优先使用 RapidOCR；如未安装则自动回退 Tesseract（需自行安装引擎本体）：

1. **Tesseract OCR 引擎**（系统级二进制，仅作兜底）
   - **Windows**：从 [UB-Mannheim/tesseract](https://github.com/UB-Mannheim/tesseract/wiki) 下载安装包，安装时勾选 **Chinese (Simplified)** 语言包。默认安装到 `C:\Program Files\Tesseract-OCR\`，程序会自动检测。
   - **macOS**：`brew install tesseract tesseract-lang`
   - **Linux**：`sudo apt install tesseract-ocr tesseract-ocr-chi-sim`

2. **Python 依赖**
   ```bash
   pip install pytesseract Pillow
   ```

3. 重启程序，通过 **设置与更多 → OCR 引擎状态** 确认可用性。

#### GPU 加速（可选，Windows 推荐）

识别可以跑在显卡上：单张从 CPU 的约 0.3~0.5 秒降到约 0.1~0.2 秒（整条流水线含下载约 0.17 秒/张）。有两种后端，按显卡任选其一。

**NVIDIA 显卡（CUDA，最快）**

```bash
pip uninstall onnxruntime onnxruntime-directml -y
pip install onnxruntime-gpu==1.22.0
pip install nvidia-cudnn-cu12 nvidia-cublas-cu12 nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 nvidia-curand-cu12
```

**其他显卡 / 核显（DirectML，通用）**

```bash
pip uninstall onnxruntime onnxruntime-gpu -y
pip install onnxruntime-directml
```

程序启动时会自动探测并优先用 CUDA、其次 DirectML，状态框显示「设备: GPU (CUDA)」或「设备: GPU (DirectML)」即生效；初始化失败会自动回退 CPU 并写日志。两种 GPU 执行器都不支持一个进程里多会话同时推理（两线程会直接崩），所以 GPU 模式下识别并发固定为 1，靠单张速度和「下载 4 线程 + 识别」流水线取胜。想强制走 CPU 可设 `OCR_DEVICE = 'cpu'`。

> 注意：`onnxruntime-gpu` 需要 CUDA 12 运行库（上面 5 个 `nvidia-*` 包自带，程序会自动把它们加入 DLL 搜索路径），不需要单独装 CUDA Toolkit。

OCR 相关配置（`src/core/config.py`）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `OCR_ENGINE` | `'auto'` | `auto` = 装了 RapidOCR 就用、否则回退 Tesseract；也可强制 `'rapidocr'` / `'tesseract'` |
| `OCR_WORKERS` | `0` | 下载 + 识别并发数，`0` = 自动（CPU 最多 8 且不超过核数；GPU 固定 1） |
| `OCR_DEVICE` | `'auto'` | `auto` = 有 CUDA 用 CUDA、其次 DirectML，都没有则 CPU；也可写 `'cuda'` / `'dml'` / `'cpu'` 强制指定 |
| `OCR_RAPID_CLS` | `False` | 是否做方向分类（截图基本正向，关掉更快） |
| `OCR_RAPID_DET_SIDE` | `960` | 检测端最长边，越小越快（小字多时调大） |
| `OCR_RAPID_THREADS` | `0` | 每个会话的 onnxruntime 线程数，`0` = 按并发数自动分摊（GPU 模式忽略） |
| `OCR_RAPID_TEXT_SCORE` | `0.5` | 置信度下限，低于该值的行丢弃 |

**速度**：下载与识别拆成两条流水线（下载线程池 + 识别线程池），识别池不会因为等网络而空转；打开帖子后模型已在后台预热，点按钮即满速。实测 CPU 约 **0.3~0.45 秒/张**、GPU（DirectML）约 **0.24 秒/张**、GPU（CUDA）约 **0.17 秒/张**（600 张左右的帖子 1.7~4 分钟），未安装 RapidOCR 时 Tesseract 串行约 0.5~1 秒/张。识别文本还会做轻量后处理（日期与时间之间自动补空格、丢弃纯符号噪声行、压缩空行）。

> 未安装 OCR 依赖时，爬虫与图片下载等其他功能不受影响，仅 OCR 按钮不可用。

## 使用

```bash
python douban_gui.py
```

1. 在左侧输入框粘贴豆瓣小组帖子 URL 或帖子 ID
   - 单帖：`https://www.douban.com/group/topic/123456789/` 或直接 `123456789`
   - 批量：每行一个 ID / URL，或用逗号分隔
2. 点击 **开始爬取**（或 `Ctrl+Enter`），等待抓取完成
3. 在右侧 **帖子内容** 和 **评论** 标签页查看结果
4. 已入库的帖子想追新时，点 **增量更新（只拉新评论）**：

   - 以数据库里该帖的最大楼层为起点，只请求这之后的分页，界面展示的是「旧评论 + 新评论」的完整列表
   - 会先重取本地最后一条评论做校验：如果内容对不上（帖子被删楼、楼层整体位移），自动回退为完整抓取
   - 增量抓取永不覆盖历史数据；本地没有该帖记录时自动按完整抓取处理
   - 增量不会补齐历史空洞（帖子有删楼时楼层本身就不连续）
5. 可选操作：
   - **下载图片** — 选择保存目录，自动下载所有图片到 `douban_images_{帖子ID}/`
   - **OCR 识别图片文字** — 对所有评论图片运行 OCR 提取文字，结果显示在评论下方黄色卡片中，并保存到数据库
   - **导出 JSON** — 保存完整数据为 JSON 文件
   - **导出 TXT** — 保存为可读文本文件（自动包含 OCR 识别文字）
   - **按日期导出** — 评论页右上角「按日期」筛选某一天后，再点任意导出按钮，会询问是「只导出这一天的评论」还是「导出全部」；只导出时文件名自动带 `_2026-11-18` 后缀，TXT / Markdown 头部与状态栏都会标注导出范围
   - **浏览数据库** — 打开历史记录浏览器，支持加载、搜索、导出、删除

关于停止与关闭：

- 点击「停止」或按 `Esc` 只是发送停止请求，界面立即恢复可操作，后台线程会在下一个可中断点自行退出；停止后界面显示「本次抓取已停止」，不会一直卡在「正在抓取」。
- 关闭窗口时会等待进行中的抓取 / OCR / 导出收尾完成（异步等待、不阻塞界面事件），不再强制终止线程，避免破坏 SQLite 写入。
- 程序为单实例：重复启动只会提示「程序已经在运行」，不会打开多个窗口。
- 画廊优先显示本地已下载图片，远程图带懒加载与失败占位；导出在后台线程执行，不冻结界面。
- 窗口位置 / 大小、当前视图、最近导出目录、图片保存目录会自动保存，下次启动恢复。

快捷键：

| 快捷键 | 功能 |
|--------|------|
| `Ctrl + Enter` | 开始爬取 |
| `Esc` | 停止爬取 |
| `Ctrl + Q` | 退出程序 |

---

## 项目结构

```
dbp/
├── douban_gui.py            # 应用入口：版本检查、单实例、全局异常钩子、数据库自检/备份、主窗口
├── douban_gui.pyw           # 无控制台入口（内容与 .py 一致）
├── pyproject.toml           # 项目元数据、依赖、pytest / ruff 配置
├── requirements.txt         # 运行依赖清单
├── src/
│   ├── __init__.py
│   ├── core/                # 基础设施：配置、身份、路径、安全存储、日志、限速、TLS
│   │   ├── config.py        # 所有可调配置：设备档位、轮换/熔断、延迟、并发、API常量、OCR
│   │   ├── identity.py      # 设备档位 + session + 请求头构造（TLS/UA/CH 同族）
│   │   ├── paths.py         # 源码 / 打包运行时的资源与数据目录
│   │   ├── secret_store.py  # Windows DPAPI 加密存储 + URL 凭证脱敏
│   │   ├── file_log.py      # 滚动文件日志（级别 / 控制台可用环境变量控制）
│   │   ├── limiter.py       # 自适应延迟 / 并发控制器
│   │   └── tls.py           # curl_cffi 优先，回退自定义 TLSAdapter
│   ├── data/                # 存储层
│   │   └── db_manager.py    # SQLite：建表/迁移、原子保存、查询、备份与自检
│   ├── crawl/               # 抓取层
│   │   ├── scraper.py       # 爬虫核心：QThread Worker + 请求/重试/熔断/并发
│   │   └── image_downloader.py  # 图片 URL 提取（正则）+ 下载线程 + 校验/重试
│   ├── ocr/                 # OCR
│   │   └── ocr_extractor.py # RapidOCR 优先 / Tesseract 兜底 + 并发流水线
│   ├── ai/                  # AI 分析
│   │   └── ai_analyzer.py   # DeepSeek 分析对话框（隐私确认 / 匿名化 / 重试）
│   └── ui/                  # 界面层（按职责拆分，主窗口由 mixin 组合）
│       ├── main_window.py   # DoubanScraperWindow 主体：__init__ / _init_ui / 导航 / 菜单 / 快捷键 / 关闭
│       ├── scraping.py      # ScrapeMixin：粘贴解析、开始/停止爬取、批量模式与摘要弹窗
│       ├── renderer.py      # RenderMixin：_display_result、信息面板、正文/评论/画廊渲染、OCR 注入
│       ├── ocr_control.py   # OcrControlMixin：OCR 启动/停止/进度/引擎探测与按钮状态
│       ├── images.py        # ImageMixin：图片下载任务
│       ├── exporters.py     # ExportMixin：JSON/TXT/CSV/Markdown 导出与文本清洗
│       ├── search.py        # SearchMixin：正文+评论跨视图搜索高亮与跳转
│       ├── dialogs.py       # DialogMixin：数据库浏览、OCR 状态/语言、代理、关于、批量摘要
│       ├── html_render.py   # 纯函数：HTML 清洗/构建、评论卡片、OCR 注入 JS
│       ├── widgets.py       # 小控件：InfoRow、图标按钮、跨线程信号 relay
│       └── ui_theme.py      # 调色板 + QSS / HTML 样式模板
├── tests/                   # pytest 单元测试（身份头、配置、限速、日志、数据库、加密、匿名化）
├── .github/workflows/ci.yml # CI：Windows + Python 3.12，compileall + pytest
├── assets/                  # 应用图标：logo.png(1024) / logo_256 / logo_128 / logo_64 / logo.ico
├── logs/                    # 运行日志（自动创建，已 gitignore）
├── backups/                 # 数据库自动备份（位于数据目录下，已 gitignore）
├── douban_data.db           # SQLite 数据库文件（源码运行时的默认位置，首次运行自动生成）
└── README.md
```

---

## 数据库结构

数据文件位置按以下优先级决定：`DBP_DB_FILE` 环境变量 > 打包运行时的 `%LOCALAPPDATA%\douban-post-crawler\douban_data.db` > 源码根目录 `douban_data.db`（SQLite3，WAL 模式），三张主表：

### topics

| 列 | 类型 | 说明 |
|----|------|------|
| `id` | INTEGER PK | 帖子 ID |
| `title` | TEXT | 标题 |
| `author` | TEXT | 作者昵称 |
| `author_id` | TEXT | 作者 UID |
| `group_name` | TEXT | 小组名 |
| `content` | TEXT | 正文 HTML |
| `abstract` | TEXT | 摘要 |
| `comments_count` | INTEGER | 回复总数 |
| `like_count` | INTEGER | 点赞数 |
| `create_time` | TEXT | 发布时间 |
| `scraped_at` | TEXT | 爬取时间 |
| `raw_json` | TEXT | 原始 API 响应 JSON |

### comments

| 列 | 类型 | 说明 |
|----|------|------|
| `rowid` | INTEGER PK | 自增主键 |
| `topic_id` | INTEGER FK | 关联帖子 |
| `floor` | INTEGER | 楼层号 |
| `author` | TEXT | 作者昵称 |
| `author_id` | TEXT | 作者 UID |
| `content` / `text` | TEXT | 评论正文（HTML / 纯文本） |
| `create_time` | TEXT | 发布时间 |
| `ip_location` | TEXT | IP 属地 |
| `likes` | INTEGER | 点赞数 |
| `reply_to_floor` | INTEGER | 回复的楼层号 |
| `reply_to_author` | TEXT | 回复的作者名 |
| `raw_json` | TEXT | 原始 API 响应 JSON |

> 唯一索引 `(topic_id, floor)` 保证同帖同楼层不重复插入。

### images

| 列 | 类型 | 说明 |
|----|------|------|
| `rowid` | INTEGER PK | 自增主键 |
| `topic_id` | INTEGER FK | 关联帖子 |
| `url` | TEXT UNIQUE | 图片 URL（去重） |
| `filename` | TEXT | 下载后的文件名 |
| `source` | TEXT | 来源：`topic` / `comment` |
| `comment_floor` | INTEGER | 所属评论楼层 |
| `downloaded` | INTEGER | 是否已下载（0/1） |
| `stale` | INTEGER | 是否已失效（1 = 最近一次抓取未再出现，不再作为该帖有效图片） |
| `local_path` | TEXT | 本地保存路径 |
| `ocr_text` | TEXT | OCR 识别出的文字（NULL 表示未识别） |
| `ocr_at` | TEXT | OCR 识别时间 |

> 旧数据库在程序启动时会自动迁移，补充 `ocr_text` / `ocr_at` / `stale` 字段与索引，无需手动操作。
>
> 索引：`idx_comments_unique(topic_id, floor)`、`idx_images_topic(topic_id)`、`idx_topics_scraped_at(scraped_at)`、`idx_images_downloaded(topic_id, downloaded)`。
>
> 抓取结果由 `save_scrape_result()` 在单连接单事务内写入；评论替换前会做「缩水保护」（新数量明显少于库中时拒绝覆盖），图片记录会按来源标记 `stale` 并过滤失效项。

---

## 许可

MIT License

> 再次提示：本项目仅供学习与技术研究使用，请勿用于商业用途或大规模采集；使用前请确认你的行为符合豆瓣用户协议与当地法律法规。

---

<a id="english-documentation"></a>

# Douban Post Collector

**Language / 语言：** [中文](#豆瓣小组帖子采集器) | English

A PySide6-based desktop GUI application that crawls group topic content, comments and images through Douban's mobile Rexxar API, with multiple layers of anti-anti-crawling mechanisms built in.

> ⚠️ **Disclaimer: this project is for learning and technical research only (non-commercial use).**
> Please comply with Douban's User Agreement and robots policy and keep your request rate under control; do not use it for commercial purposes, large-scale collection, or infringement of others' lawful rights. Any consequences arising from the use of this project are borne by the user.

## Features

- **Topic crawling** — Enter a topic URL or ID to get the complete information: title, author, body, group, publish time, like/reply counts, etc.
- **Batch crawling** — Supports multi-line / comma-separated batch input, automatically queued and crawled one by one
- **Incremental update** — One click to fetch only new comments for topics already in the database (continuing by floor; N new comments observed in practice); historical data is neither re-crawled nor cleared
- **Comment fetching** — Concurrently fetch all comments across pages, including floor, author, IP location, reply chains, etc.
- **Nested replies** — Automatically normalizes the Douban API `ref_comment` into reply relationships: comment cards show "↳ Reply to #floor @author" and quote the replied-to content above the body; TXT / CSV / Markdown exports carry the reply information as well (floors are back-filled on the fly when reading historical data, no re-crawl needed)
- **Image extraction and download** — Automatically detects images in topics and comments (supports `<img>`, `data-src`, bare image-host links), with one-click batch download
- **OCR image text recognition** — Runs OCR over images in comments to extract text; the result is displayed visually in the comment and persisted to the database (RapidOCR recommended; falls back to Tesseract when not installed)
- **Rich-text rendering** — Topic content and comments are rendered natively by QWebEngineView, supporting HTML content such as images and tables
- **Search highlighting** — Search keywords in topic content and comments, with previous / next navigation
- **Data persistence** — SQLite database automatically saves crawl results
- **Database browsing** — Built-in browser to view, search, load, export and delete historical records
- **Export** — Export in JSON / TXT / CSV / Markdown formats (TXT automatically includes the OCR text)
- **AI analysis** — Compiles the topic and comments into Markdown and hands them to DeepSeek for analysis (API Key required); nicknames / IP locations can be anonymized before sending
- **Anti-anti-crawling** — Multi-layer protection strategy (see below)
- **Privacy protection** — Proxy credentials and API Key are stored encrypted with Windows DPAPI (no plaintext is written when it is unavailable); a confirmation dialog is shown before the first AI send
- **Data safety** — Crawl results are saved atomically in a single transaction and rolled back with an honest error on failure; refuses to overwrite history when the comment count shrinks abnormally; automatic backup on startup + database integrity self-check
- **Stable interaction** — Stop / close now finish asynchronously instead of force-killing threads; duplicate launches show an "already running" notice; window position, current view and export directory are remembered
- **Engineering quality** — Built-in pytest unit tests and GitHub Actions CI (Windows / Python 3.12), with pyproject.toml and a log-level switch provided

---

## Douban API

This tool uses Douban's mobile **Rexxar API v2**, the interface called by Douban's official m.douban.com front end. It returns JSON and allows access to public data without logging in.

### Endpoint List

#### 1. Get Topic Details

```
GET https://m.douban.com/rexxar/api/v2/group/topic/{topic_id}
```

**Response fields:**

| Field | Type | Description |
|------|------|------|
| `id` | int | Topic ID |
| `title` | string | Topic title |
| `content` | string | Body HTML |
| `abstract` | string | Summary (used by special types such as poll topics) |
| `author` | object | Author info `{name, uid, avatar, …}` |
| `group` | object | Group info `{name, id, …}` |
| `comments_count` | int | Total comment count |
| `like_count` | int | Like count |
| `create_time` | string | Publish time (ISO 8601 format) |
| `photos` | array | Topic images |

#### 2. Get Comment List

```
GET https://m.douban.com/rexxar/api/v2/group/topic/{topic_id}/comments
```

**Request parameters:**

| Parameter | Type | Description |
|------|------|------|
| `start` | int | Offset (starting from 0) |
| `count` | int | Items per page (pass 100; the measured single-page cap is 100, larger values still return only 100 items) |
| `order` | string | Sort order (fixed `asc`, ascending by floor) |

**Response fields:**

| Field | Type | Description |
|------|------|------|
| `total` | int | Total number of comments |
| `comments` | array | List of comments |

Each comment contains:

| Field | Type | Description |
|------|------|------|
| `floor` | int | Floor number |
| `author` | object | Author `{name, uid, avatar}` |
| `content` | string | Comment body HTML |
| `text` | string | Plain text (in some cases) |
| `create_time` | string | Publish time |
| `ip_location` | string | IP location (e.g. Beijing) |
| `likes` | int | Like count |
| `reply_to` | object | The comment being replied to `{floor, author, text}` |
| `photos` | array | Comment images (nested in `image.normal.url`) |

### Warm-up Pages (Simulating a Real Browsing Path)

| Page | URL | Description |
|------|-----|------|
| Douban mobile homepage | `https://m.douban.com/` | Must visit: the server issues the bid cookie here |
| Group homepage | `https://m.douban.com/group/` | Second hop (randomly picked from the warm-up path) |
| Topic page | `https://m.douban.com/group/topic/{topic_id}/` | The API Referer points to it; the m site no longer serves this HTML page (always 404 under every UA tested), so it is not actually requested |

> Only warm-up pages verified to work are kept: `/explore` always returns 404, and `/movie/`, `/gallery/` and `/subject_collection/hot_movies` often take over ten seconds without responding on the current network, so they were removed from the warm-up path.

---

## Crawling Workflow

```
User input (URL / ID)
                                  |
                                  v
+------------------------------------------------------------------+
| 1. Parse input                                                   |
|    Regex-extract the topic ID                                    |
|    Supports URL / bare number / multi-line batch                 |
+---------------------------------+--------------------------------+
                                  |
                                  v
+------------------------------------------------------------------+
| 2. Select a device profile and create a Session                  |
|    +- Chrome 131 / Android                                       |
|    |    impersonate=                                             |
|    |    chrome131_android                                        |
|    +- Safari 18 / iOS 18                                         |
|       impersonate=                                               |
|       safari180_ios                                              |
|    TLS / UA / Client Hints                                       |
|    come from the same profile, never mixed                       |
|    Fallback: TLSAdapter                                          |
|    (custom Cipher Suite + ALPN + TLS 1.2+)                       |
+---------------------------------+--------------------------------+
                                  |
                                  v
+------------------------------------------------------------------+
| 3. Warm up and obtain bid from the response                      |
|    GET homepage                                                  |
|    +- the server issues the bid cookie                           |
|       (11-character base64url)                                   |
|    Never forge bid / dbcl2 / ck                                  |
+---------------------------------+--------------------------------+
                                  |
                                  v
+------------------------------------------------------------------+
| 4. Browsing-path warm-up (Warmup)                                |
|    (1) GET homepage                                              |
|        +- navigation headers + profile UA                        |
|        +- delay 0.4~1.0s                                         |
|    (2) GET group homepage (sub-channel)                          |
|        +- Referer: homepage                                      |
|        +- delay 0.4~1.0s                                         |
|    (3) API Referer points to the topic page                      |
|        (the m site no longer serves that                         |
|         HTML page, always 404 in tests,                          |
|         so it is not requested)                                  |
|    Purpose: build a complete Referer chain                       |
|    so the server sees a real user path                           |
+---------------------------------+--------------------------------+
                                  |
                                  v
+------------------------------------------------------------------+
| 5. Get topic details                                             |
|    GET /rexxar/api/v2/group/topic/{id}                           |
|    On failure retry (max 4):                                     |
|    +- exponential backoff + honor the                            |
|    |  Retry-After response header                                |
|    +- rotate the full identity only after                        |
|    |  consecutive blocks (new session +                          |
|    |  re-warm-up + at least 45s apart)                           |
|    +- circuit-break after 5 consecutive                          |
|       blocks                                                     |
+---------------------------------+--------------------------------+
                                  |
                                  v
+------------------------------------------------------------------+
| 6. Get comments                                                  |
|    Page 1: sequential request                                    |
|    GET .../comments                                              |
|    ?start=0&count=100&order=asc                                  |
|    +- obtain the total count                                     |
|    Remaining pages: concurrent requests                          |
|    ThreadPoolExecutor                                            |
|    concurrency: throttled -> 1, normal -> 2                      |
|    +- each thread has its own Session                            |
|    +- random stagger 0.1~0.6s                                    |
|    +- inherit the main Session cookies                           |
|    +- no per-page UA change / forged bid                         |
|    Empty list on page 1 while the topic has                      |
|    comments -> treat as rate-limited / risk                      |
|    control and circuit-break                                     |
|    Merge results sorted by offset                                |
|    Auto-fill missing floor fields                                |
+---------------------------------+--------------------------------+
                                  |
                                  v
+------------------------------------------------------------------+
| 7. Extract image URLs                                            |
|    Sources:                                                      |
|    +- topic body content                                         |
|    |  +- <img src="...">                                         |
|    |  +- data-src (lazy loading)                                 |
|    |  +- bare image-host links                                   |
|    +- comment content + photos                                   |
|       +- <img> in the body                                       |
|       +- photos[].image.normal.url                               |
|    Dedupe (seen set)                                             |
|    Filter: .svg/.ico/.css/.js                                    |
+---------------------------------+--------------------------------+
                                  |
                                  v
+------------------------------------------------------------------+
| 8. Persist to SQLite                                             |
|    +- save_topic()                                               |
|    +- save_comments()                                            |
|    +- save_images()                                              |
|       (INSERT OR IGNORE dedupe)                                  |
+---------------------------------+--------------------------------+
                                  |
                                  v
+------------------------------------------------------------------+
| 9. Render and display                                            |
|    +- QWebEngineView renders                                     |
|    |  HTML5 + inline CSS                                         |
|    +- sanitize HTML                                              |
|    |  (strip script/style/iframe/event                           |
|    |   attributes)                                               |
|    +- search highlighting (JS injection)                         |
|       TreeWalker over text nodes                                 |
+---------------------------------+--------------------------------+
```

### Adaptive Anti-Anti-Crawling Strategy

```
Before request: micro-jitter 0.05~0.15s
    |
    v
Send request (the header set comes from the current device profile;
              no self-contradicting fingerprint)
    |
    +-- success --> note_success()
    |               +-- reset the consecutive-block counter
    |               +-- consecutive success > 60s -> reset the delay level
    |
    +-- blocked --> note_block()
                    +-- consecutive blocks +1
                    +-- delay level +1 (max level 8)
                    +-- cooldown = max(Retry-After, 20s * 2^(n-1)), capped at 120s
                    +-- 2 consecutive blocks -> rotate the full identity
                    |   +-- switch device profile (random Chrome/Safari)
                    |   +-- create a new session (new TLS session)
                    |   +-- re-warm up and obtain a new bid
                    |   +-- at least 45s between two identity rotations
                    +-- 5 consecutive blocks -> circuit-break, abort this task
                    |   +-- keep the partial data already scraped;
                    |       never overwrite complete historical data
                    +-- at most 4 retries per request; error if still failing
```

### Anti-Crawl Detection Checks

After each response, the checks below are run one by one; any hit is treated as being blocked by the anti-crawl system: first accumulate the count and retry after a cooldown; only consecutive hits escalate to a full identity rotation, and 5 consecutive hits trip the circuit breaker directly.

| Check | Condition |
|--------|----------|
| HTTP status code | `403` / `429` / `418` / `503` |
| Login redirect | Response URL contains `login` |
| Abnormal content type | `Content-Type: text/html` (the API should return JSON) |
| CAPTCHA page | Response body contains `captcha` |
| Anti-crawl keywords | First 512 characters of the response body contain `rate_limit` / `too_many` / `verify` / `challenge` / `block` / `访问过于频繁` (too frequent) / `请稍后再试` (please try again later) |
| Response body too short | `len(text) < 20` bytes |
| Security cookie | `Set-Cookie` contains `captcha` / `sec=` / `_sec` / `challenge` |

---

## Anti-Anti-Crawling Configuration

All configuration is centralized in `src/core/config.py`; tunable parameters:

### Device Profiles (Core)

A single profile determines the TLS fingerprint (curl_cffi impersonate), the User-Agent and the Client Hints at the same time, guaranteeing that all three belong to the same family and version, so contradictory combinations such as "Safari UA + Chrome TLS" no longer occur:

| Profile | impersonate | User-Agent | Client Hints |
|------|-------------|------------|--------------|
| Chrome 131 / Android 10 | `chrome131_android` | Android 10 Mobile Chrome 131 | Sends Sec-CH-UA / Mobile / Platform |
| Safari 18 / iOS 18 | `safari180_ios` | iPhone OS 18_0 Safari 18.0 | Not sent (Safari never had them) |

Additional notes:

- **bid is obtained only from the server**: the homepage response carries `Set-Cookie: bid=<11-character base64url>`, and the program stores that cookie; it **no longer** forges a 16-hex-digit bid itself, nor writes an empty `dbcl2` / `ck`.
- **No unrequested client hints are sent**: Douban does not request Device-Memory / Downlink / ECT / RTT / Viewport-Width via `Accept-CH`; sending them voluntarily is itself a script signature, so they were all removed.
- **Accept-Encoding is left to curl_cffi**: it uses `gzip, deflate, br, zstd` automatically per profile (consistent with real browsers) instead of hand-written `gzip, deflate`.
- **Sec-Fetch-Site is computed from the real domain relationship**: images live on `img*.doubanio.com`, so `cross-site` is sent across registrable domains instead of a hard-coded `same-origin`.
- **The UA is fixed within one session**: the concurrent comment-page threads each reuse one profile's set instead of changing the UA per page / per retry.

### Identity Rotation and Circuit Breaking

| Parameter | Default | Description |
|------|--------|------|
| `IDENTITY_ROTATE_MIN_INTERVAL` | 45s | Minimum interval between two full identity rotations |
| `IDENTITY_ROTATE_AFTER_BLOCKS` | 2 | Number of consecutive blocks after which a rotation is triggered |
| `MAX_CONSECUTIVE_BLOCKS` | 5 | Number of consecutive blocks that trips the circuit breaker and aborts |
| `BLOCK_COOLDOWN_BASE` | 20s | Base cooldown after a block (exponential growth) |
| `BLOCK_COOLDOWN_MAX` | 120s | Upper bound of a single cooldown |
| `RETRY_AFTER_MAX` | 120s | Maximum wait that honors the server's `Retry-After` |

> "Rotating identity" is not just changing the UA: it creates a new session (new TLS session) + switches the device profile + re-warms up to obtain a new bid. Changing only the UA while keeping the same TLS session is pointless — what the server tags is the identity of the whole connection.

### Request Timeouts and Retries

| Parameter | Default | Description |
|------|--------|------|
| `DEFAULT_TIMEOUT` | 25s | Timeout of a single API / image request (normal requests finish within 1s; 25s only covers network jitter) |
| `WARMUP_TIMEOUT` | 8s | Timeout of a warm-up navigation page (the page is only tens of KB; fail fast and then fetch the homepage once more for the bid) |
| `SHORT_LINK_TIMEOUT` | 10s | Timeout for resolving a shared short-link redirect |
| `MAX_RETRIES` | 4 | Maximum number of retries |
| `RETRY_BACKOFF_BASE` | 2s | Retry backoff base (exponential growth) |

### Concurrency Control

| Parameter | Default | Description |
|------|--------|------|
| `COMMENTS_PER_PAGE` / `COMMENTS_PAGE_STEP` | 100 | Comments per page; the measured single-page cap of the API is 100, so 300 comments are fully fetched in 3 pages |
| `MAX_CONCURRENT_PAGES` | 2 | Normal number of concurrent pages (concurrency is lowered to match real user behavior) |
| `MAX_CONCURRENT_ADAPTIVE_MIN` | 1 | Lowest concurrency when throttled (serial after degradation) |
| `IMAGE_DOWNLOAD_CONCURRENCY` | 6 | Image download concurrency (static CDN; in practice going from 2 to 6 concurrent downloads cut 16 images from 20~54s to 3~4.5s) |

### Adaptive Delay

| Parameter | Default | Description |
|------|--------|------|
| `ADAPTIVE_DELAY_MIN` | 1.0s | Lower bound of the normal delay |
| `ADAPTIVE_DELAY_MAX` | 15.0s | Upper bound of the highest delay (when throttled at level 8) |
| `ADAPTIVE_BACKOFF_FACTOR` | 1.5× | Multiplicative factor per level |
| `ADAPTIVE_COOLDOWN` | 60.0s | Cooldown: after this long of consecutive success, the default delay is restored |

---

## Privacy and Data Security

| Sensitive item | Storage | Description |
|--------|----------|------|
| DeepSeek API Key | Windows DPAPI encryption (`dpapi:` prefixed ciphertext, stored in QSettings) | Not saved and never falls back to plaintext if encryption fails; prompts for re-entry if decryption fails |
| Proxy address (may contain credentials) | Windows DPAPI encryption | Plaintext proxy addresses left by older versions are migrated to ciphertext automatically on first read; URL credentials in errors and logs are uniformly masked as `scheme://***@host` |
| AI analysis content | Confirmation before sending + optional anonymization | A confirmation dialog before the first send; "anonymize nicknames and IP locations" is on by default: nicknames are replaced in order of appearance by "User1 / User2 …", and the IP location and UID fields are removed; group name, time, floor and body semantics are preserved |
| OCR recognized text | Database only, never written to logs | Logs record only the character count and the engine, never the actual text |

On non-Windows platforms or when DPAPI is unavailable, the program explicitly states "the current system does not support secure storage and the plaintext key will not be saved"; the proxy address is kept in plaintext for this session only, with a notice, and the user decides whether to use it.

---

## Data Reliability and Backup

- **Atomic save**: the topic / comments / images of one crawl are written within the same transaction; if any step fails the whole thing is rolled back and "data save failed, rolled back" is shown instead of pretending success
- **Overwrite protection**: a new comment count more than 20% lower than the database logs a warning; more than 50% lower rejects the overwrite outright (preventing truncated data from wiping history while rate-limited)
- **Completeness determination**: completeness is judged from the set of offsets actually requested; when a circuit break / rate limit occurs the result is forcibly marked incomplete and complete historical data is never overwritten
- **Automatic backup**: on every startup, `VACUUM INTO` backs up into `backups/` under the data directory, keeping the latest 5; `PRAGMA quick_check` runs at startup and anomalies are logged
- **Image state**: `images.stale` marks old images that no longer appear; a failed write of the download state is retried and warned about, and bad images (magic number / size / Pillow validation failure) are re-downloaded
- **Database location**: `DBP_DB_FILE` env var > `%LOCALAPPDATA%\douban-post-crawler\douban_data.db` when running packaged > `douban_data.db` in the source root

---

## Engineering (Development)

```bash
# Install development dependencies (pytest / ruff)
pip install -e ".[dev]"

# Run unit tests
python -m pytest -q
```

- Test cases live in `tests/` (device profiles and request headers, configuration constraints, rate limiter, logging, SQLite read/write and migration, scraper helper functions, sensitive-config encryption, AI anonymization); the CI configuration is in `.github/workflows/ci.yml` (Windows + Python 3.12: compileall + pytest)
- Logging: writes to `logs/dbp.log` by default (2MB × 3 rotation); `DBP_LOG_LEVEL=DEBUG|INFO|WARNING` adjusts the level, and `DBP_LOG_CONSOLE=1` also outputs to the console
- Code style: ruff (configured in `pyproject.toml`, line-length 100)

---

## Interface

Light theme, three-column layout: left navigation (Topic Content / Comments / Images / Database), the center content area, and the topic overview and quick actions on the right.

- **Topic content**: rich-text rendering of the body (headings, lists, quotes, tables, code blocks, embedded images)
- **Comments**: sorted by floor in ascending order; reply relationships are shown as "↳ Reply to #N @author" with a snippet of the quoted comment
- **Images**: masonry gallery, images fully displayed, "click to view" on hover, click to zoom in
- **Right column**: topic overview (replies / likes / author / group / published), quick actions (incremental update, download images, OCR, AI analysis), export (JSON / TXT / CSV / MD), full-text search (previous / next / clear)

---

## Requirements

- Python 3.12+ (the source uses PEP 701 f-string syntax and will not start on older versions)
- See the installation instructions below for dependencies

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd dbp

# Create a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate     # Windows
# source venv/bin/activate  # macOS / Linux

# Install dependencies
pip install -r requirements.txt
```

`requirements.txt` (major-version upper bounds are pinned to avoid future major releases breaking compatibility):

```
PySide6>=6.5.0,<7
PySide6-WebEngine>=6.5.0,<7
requests>=2.28.0,<3
curl_cffi>=0.8.0,<1        # Optional: TLS fingerprint impersonation (0.15.0 observed)
pytesseract>=0.3.10,<0.4   # Optional: OCR fallback (with the Tesseract engine)
Pillow>=10.0.0,<13         # Optional: OCR image processing
# Optional (recommended): pip install "rapidocr-onnxruntime>=1.3.0,<2"
```

> `curl_cffi` is an optional dependency. Once installed, the crawler enables the corresponding TLS fingerprint impersonation for a random device profile (Chrome 131 / Android or Safari 18 / iOS, including JA3 + HTTP/2 Settings), with the UA and Client Hints in the same family, greatly reducing the chance of being identified as a crawler. Older curl_cffi versions that do not recognize a new profile automatically fall back to `chrome120`; with it not installed at all, the built-in enhanced TLSAdapter is used (manually setting Cipher Suite / ALPN / TLS 1.2+), which still handles most anti-crawl scenarios, though fingerprint consistency is weaker than impersonate.

### OCR Dependencies (Optional)

To enable "OCR image text recognition", installing **RapidOCR** is recommended (built-in Chinese and English models, higher Chinese accuracy than Tesseract, and multithreaded concurrency supported):

```bash
pip install rapidocr-onnxruntime
```

No further configuration is needed once installed — the program prefers RapidOCR; if it is not installed it falls back to Tesseract automatically (the engine itself must be installed separately):

1. **Tesseract OCR engine** (system-level binary, fallback only)
   - **Windows**: download the installer from [UB-Mannheim/tesseract](https://github.com/UB-Mannheim/tesseract/wiki) and check the **Chinese (Simplified)** language pack during installation. It installs to `C:\Program Files\Tesseract-OCR\` by default and the program detects it automatically.
   - **macOS**: `brew install tesseract tesseract-lang`
   - **Linux**: `sudo apt install tesseract-ocr tesseract-ocr-chi-sim`

2. **Python dependencies**
   ```bash
   pip install pytesseract Pillow
   ```

3. Restart the program and confirm availability under **Settings & More → OCR Engine Status**.

#### GPU Acceleration (Optional, Recommended on Windows)

Recognition can run on the GPU: a single image drops from about 0.3~0.5 s on CPU to about 0.1~0.2 s (the whole pipeline including download is about 0.17 s/image). There are two backends; pick either one according to your graphics card.

**NVIDIA GPUs (CUDA, fastest)**

```bash
pip uninstall onnxruntime onnxruntime-directml -y
pip install onnxruntime-gpu==1.22.0
pip install nvidia-cudnn-cu12 nvidia-cublas-cu12 nvidia-cuda-runtime-cu12 nvidia-cufft-cu12 nvidia-curand-cu12
```

**Other GPUs / integrated GPUs (DirectML, universal)**

```bash
pip uninstall onnxruntime onnxruntime-gpu -y
pip install onnxruntime-directml
```

On startup the program probes automatically and prefers CUDA, then DirectML; the status box shows "Device: GPU (CUDA)" or "Device: GPU (DirectML)" when it takes effect. If initialization fails it falls back to CPU automatically and writes a log entry. Neither GPU executor supports multiple sessions inferring in one process at the same time (two threads crash outright), so in GPU mode the recognition concurrency is fixed at 1, winning through per-image speed and the "download 4 threads + recognition" pipeline. To force CPU, set `OCR_DEVICE = 'cpu'`.

> Note: `onnxruntime-gpu` requires the CUDA 12 runtime libraries (the 5 `nvidia-*` packages above bundle them, and the program adds them to the DLL search path automatically); a separate CUDA Toolkit installation is not needed.

OCR-related configuration (`src/core/config.py`):

| Configuration | Default | Description |
| --- | --- | --- |
| `OCR_ENGINE` | `'auto'` | `auto` = use RapidOCR if installed, otherwise fall back to Tesseract; can also be forced to `'rapidocr'` / `'tesseract'` |
| `OCR_WORKERS` | `0` | Download + recognition concurrency; `0` = automatic (CPU: at most 8 and no more than the core count; GPU: fixed 1) |
| `OCR_DEVICE` | `'auto'` | `auto` = CUDA if available, then DirectML, otherwise CPU; can also be forced with `'cuda'` / `'dml'` / `'cpu'` |
| `OCR_RAPID_CLS` | `False` | Whether to run orientation classification (screenshots are basically upright, so turning it off is faster) |
| `OCR_RAPID_DET_SIDE` | `960` | Longest side of the detection input; smaller is faster (increase it when there is a lot of small text) |
| `OCR_RAPID_THREADS` | `0` | onnxruntime threads per session; `0` = split automatically by concurrency (ignored in GPU mode) |
| `OCR_RAPID_TEXT_SCORE` | `0.5` | Confidence threshold; lines below it are dropped |

**Speed**: download and recognition are split into two pipelines (a download thread pool + a recognition thread pool), so the recognition pool does not spin idle waiting for the network; after a topic is opened the model is already warmed up in the background, so pressing the button runs at full speed. Measured: CPU about **0.3~0.45 s/image**, GPU (DirectML) about **0.24 s/image**, GPU (CUDA) about **0.17 s/image** (a topic with about 600 images takes 1.7~4 minutes); without RapidOCR, Tesseract is serial at about 0.5~1 s/image. The recognized text also gets lightweight post-processing (a space is inserted between date and time, pure-symbol noise lines are dropped, blank lines are compressed).

> Without the OCR dependencies installed, other features such as crawling and image download are unaffected; only the OCR button is unavailable.

## Usage

```bash
python douban_gui.py
```

1. Paste a Douban group topic URL or topic ID into the input box on the left
   - Single topic: `https://www.douban.com/group/topic/123456789/` or just `123456789`
   - Batch: one ID / URL per line, or separated by commas
2. Click **Start Crawling** (or `Ctrl+Enter`) and wait for the crawl to finish
3. View the results in the **Topic Content** and **Comments** tabs on the right
4. To catch up on a topic already in the database, click **Incremental Update (new comments only)**:

   - Starting from the maximum floor of that topic in the database, only pages after it are requested; the view shows the complete list of "old comments + new comments"
   - The last local comment is re-fetched first for validation: if the content does not match (deleted floors in the topic, global floor shift), it automatically falls back to a full crawl
   - Incremental crawls never overwrite historical data; when there is no local record for the topic it is handled as a full crawl automatically
   - Incremental updates do not fill in historical gaps (floors are inherently non-contiguous when a topic has deleted floors)
5. Optional actions:
   - **Download Images** — choose a save directory; all images are downloaded into `douban_images_{topic_ID}/`
   - **OCR Image Text** — run OCR over all comment images to extract text; the result appears in a yellow card below the comment and is saved to the database
   - **Export JSON** — save the complete data as a JSON file
   - **Export TXT** — save as a readable text file (OCR text included automatically)
   - **Export by Date** — after filtering a single day with "By date" in the top-right corner of the comments page, clicking any export button asks whether to "export only that day's comments" or "export everything"; when exporting only that day, the filename automatically gets a `_2026-11-18` suffix, and both the TXT / Markdown header and the status bar state the export scope
   - **Browse Database** — open the history browser, supporting load, search, export and delete

About stopping and closing:

- Clicking "Stop" or pressing `Esc` only sends a stop request; the interface becomes usable again immediately and the background thread exits by itself at the next interruptible point; after stopping, the interface shows "this crawl has been stopped" instead of being stuck on "crawling".
- Closing the window waits for in-progress crawling / OCR / export to finish (asynchronous waiting that does not block interface events) instead of force-killing threads, avoiding damage to SQLite writes.
- The program is single-instance: launching it again only shows "the program is already running" and never opens multiple windows.
- The gallery prefers locally downloaded images, with lazy loading and failure placeholders for remote ones; exports run in a background thread and do not freeze the interface.
- Window position / size, current view, most recent export directory and image save directory are saved automatically and restored on the next launch.

Keyboard shortcuts:

| Shortcut | Function |
|--------|------|
| `Ctrl + Enter` | Start crawling |
| `Esc` | Stop crawling |
| `Ctrl + Q` | Quit the program |

---

## Project Structure

```
dbp/
├── douban_gui.py                   # Application entry: version check, single instance, global exception hook, DB self-check/backup, main window
├── douban_gui.pyw                  # Console-less entry (same content as the .py)
├── pyproject.toml                  # Project metadata, dependencies, pytest / ruff configuration
├── requirements.txt                # Runtime dependency list
├── src/
│   ├── __init__.py
│   ├── core/                       # Infrastructure: config, identity, paths, secure storage, logging, rate limiting, TLS
│   │   ├── config.py               # All tunable configuration: device profiles, rotation/circuit breaking, delay, concurrency, API constants, OCR
│   │   ├── identity.py             # Device profile + session + request-header construction (TLS/UA/CH in the same family)
│   │   ├── paths.py                # Resource and data directories for source / packaged runs
│   │   ├── secret_store.py         # Windows DPAPI encrypted storage + URL credential masking
│   │   ├── file_log.py             # Rotating file log (level / console controllable via env vars)
│   │   ├── limiter.py              # Adaptive delay / concurrency controller
│   │   └── tls.py                  # curl_cffi preferred, with a custom TLSAdapter fallback
│   ├── data/                       # Storage layer
│   │   └── db_manager.py           # SQLite: table creation/migration, atomic save, queries, backup and self-check
│   ├── crawl/                      # Crawling layer
│   │   ├── scraper.py              # Crawler core: QThread Worker + request/retry/circuit breaking/concurrency
│   │   └── image_downloader.py     # Image URL extraction (regex) + download thread + validation/retry
│   ├── ocr/                        # OCR
│   │   └── ocr_extractor.py        # RapidOCR preferred / Tesseract fallback + concurrent pipeline
│   ├── ai/                         # AI analysis
│   │   └── ai_analyzer.py          # DeepSeek analysis dialog (privacy confirmation / anonymization / retry)
│   └── ui/                         # Interface layer (split by responsibility; the main window is composed of mixins)
│       ├── main_window.py          # DoubanScraperWindow body: __init__ / _init_ui / navigation / menus / shortcuts / close
│       ├── scraping.py             # ScrapeMixin: paste parsing, start/stop crawling, batch mode and summary dialog
│       ├── renderer.py             # RenderMixin: _display_result, info panel, body/comment/gallery rendering, OCR injection
│       ├── ocr_control.py          # OcrControlMixin: OCR start/stop/progress/engine probing and button state
│       ├── images.py               # ImageMixin: image download tasks
│       ├── exporters.py            # ExportMixin: JSON/TXT/CSV/Markdown export and text cleanup
│       ├── search.py               # SearchMixin: cross-view search highlighting and navigation in body + comments
│       ├── dialogs.py              # DialogMixin: database browser, OCR status/language, proxy, about, batch summary
│       ├── html_render.py          # Pure functions: HTML sanitizing/building, comment cards, OCR injection JS
│       ├── widgets.py              # Small widgets: InfoRow, icon buttons, cross-thread signal relay
│       └── ui_theme.py             # Color palette + QSS / HTML style templates
├── tests/                          # pytest unit tests (identity headers, config, rate limiting, logging, database, encryption, anonymization)
├── .github/workflows/ci.yml        # CI: Windows + Python 3.12, compileall + pytest
├── assets/                         # Application icons: logo.png(1024) / logo_256 / logo_128 / logo_64 / logo.ico
├── logs/                           # Runtime logs (created automatically, gitignored)
├── backups/                        # Automatic database backups (under the data directory, gitignored)
├── douban_data.db                  # SQLite database file (default location for source runs, created on first run)
└── README.md
```

---

## Database Schema

The data file location is decided by the following priority: `DBP_DB_FILE` env var > `%LOCALAPPDATA%\douban-post-crawler\douban_data.db` when running packaged > `douban_data.db` in the source root (SQLite3, WAL mode), with three main tables:

### topics

| Column | Type | Description |
|----|------|------|
| `id` | INTEGER PK | Topic ID |
| `title` | TEXT | Title |
| `author` | TEXT | Author nickname |
| `author_id` | TEXT | Author UID |
| `group_name` | TEXT | Group name |
| `content` | TEXT | Body HTML |
| `abstract` | TEXT | Summary |
| `comments_count` | INTEGER | Total number of replies |
| `like_count` | INTEGER | Like count |
| `create_time` | TEXT | Publish time |
| `scraped_at` | TEXT | Crawl time |
| `raw_json` | TEXT | Raw API response JSON |

### comments

| Column | Type | Description |
|----|------|------|
| `rowid` | INTEGER PK | Auto-increment primary key |
| `topic_id` | INTEGER FK | Related topic |
| `floor` | INTEGER | Floor number |
| `author` | TEXT | Author nickname |
| `author_id` | TEXT | Author UID |
| `content` / `text` | TEXT | Comment body (HTML / plain text) |
| `create_time` | TEXT | Publish time |
| `ip_location` | TEXT | IP location |
| `likes` | INTEGER | Like count |
| `reply_to_floor` | INTEGER | Floor number of the replied-to comment |
| `reply_to_author` | TEXT | Author name of the replied-to comment |
| `raw_json` | TEXT | Raw API response JSON |

> The unique index `(topic_id, floor)` guarantees that the same floor of the same topic is not inserted twice.

### images

| Column | Type | Description |
|----|------|------|
| `rowid` | INTEGER PK | Auto-increment primary key |
| `topic_id` | INTEGER FK | Related topic |
| `url` | TEXT UNIQUE | Image URL (deduplicated) |
| `filename` | TEXT | Filename after download |
| `source` | TEXT | Source: `topic` / `comment` |
| `comment_floor` | INTEGER | Floor of the owning comment |
| `downloaded` | INTEGER | Whether downloaded (0/1) |
| `stale` | INTEGER | Whether invalid (1 = did not appear in the most recent crawl, no longer a valid image of that topic) |
| `local_path` | TEXT | Local save path |
| `ocr_text` | TEXT | Text recognized by OCR (NULL means not recognized) |
| `ocr_at` | TEXT | Time of OCR recognition |

> Old databases are migrated automatically when the program starts, adding the `ocr_text` / `ocr_at` / `stale` columns and indexes; no manual action is needed.
>
> Indexes: `idx_comments_unique(topic_id, floor)`, `idx_images_topic(topic_id)`, `idx_topics_scraped_at(scraped_at)`, `idx_images_downloaded(topic_id, downloaded)`.
>
> Crawl results are written by `save_scrape_result()` within a single connection and a single transaction; before comments are replaced a "shrink protection" check runs (refuses to overwrite when the new count is clearly lower than the database), and image records are marked `stale` by source with invalid entries filtered out.

---

## License

MIT License

> Reminder again: this project is for learning and technical research only; do not use it for commercial purposes or large-scale collection; before use, make sure your actions comply with Douban's User Agreement and local laws and regulations.
