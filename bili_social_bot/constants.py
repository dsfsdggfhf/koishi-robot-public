"""集中常量：预算 / 超时 / 上限 / 图片限制 / 状态限制。

把所有散落在 ai.py / media.py / chat_bot.py / bot.py 的魔法数字集中到这里，
避免"改一处漏多处"导致的配置修改无效问题。
"""

# ============ 证据/文本预算（字符数） ============
# 证据截断上限（视觉+字幕+音频+元数据打包后的最大字符数）
EVIDENCE_MAX_CHARS = 10000
# 多模态证据文本上限
EVIDENCE_TEXT_MAX_CHARS = 10000
# 字幕/音频单类证据上限
EVIDENCE_PER_SOURCE_MAX_CHARS = 12000
# B站AI总结 / 画面理解上限
EVIDENCE_AI_CONCLUSION_CHARS = 3000
EVIDENCE_PREMIUM_VISION_CHARS = 3000
EVIDENCE_VISION_CHARS = 6000
# 摘要输入上限
SUMMARY_TEXT_MAX_CHARS = 12000

# 知识文本预算（适度压缩：角色特征库前部已含高频角色，800 字足够覆盖常用识别）
KNOWLEDGE_CHARS_FULL = 3000      # 生成评论时的知识
KNOWLEDGE_CHARS_BRIEF = 800      # 合并任务/多模态时的精简知识
KNOWLEDGE_CHARS_REFERENCE = 4000  # 视觉角色识别参考
KNOWLEDGE_CHARS_POLISH = 1500    # 润色草稿上限
KNOWLEDGE_HASH_PREFIX = 8000     # 缓存 key 里知识文本哈希前缀长度

# 角色特征库截断
CHARACTER_REFERENCE_CHARS = 16000
CHARACTER_REFERENCE_FALLBACK = 4500
CHARACTER_REFERENCE_MIN = 3000

# ============ Token 上限 ============
TOKEN_FINAL_MAX = 2200           # 聊天回复最终阶段上限
TOKEN_FINAL_MIN = 300
TOKEN_THINK_QUESTION = 300       # 提问时思考阶段
TOKEN_THINK_NORMAL = 150         # 普通思考阶段
TOKEN_COMMENT_MAX = 900          # 评论生成上限
TOKEN_MULTIMODAL = 900           # 多模态评论上限
TOKEN_VISION = 2000              # 视觉描述上限
TOKEN_POLISH_MIN = 400           # 润色下限
TOKEN_KOISHI_LIKES = 10          # 喜欢判断

# 思考/记忆上下文预算
THINKING_MAX_CHARS = 600         # 思考要点截断
MEMORY_USER_CHARS = 1200         # 用户记忆注入
MEMORY_RECALL_CHARS = 1600       # 加权召回注入
KNOW_BUDGET_QUESTION = 2000      # 提问时知识注入
KNOW_BUDGET_NORMAL = 1000        # 普通知识注入
REPLY_DEFAULT_MAX_LEN = 500      # 回复默认上限（无配置时）

# ============ 图片限制 ============
IMAGE_MAX_PER_CALL = 21          # 多模态单次最多图
IMAGE_FALLBACK_MAX = 6           # rightapi 视觉兜底最多图
IMAGE_PREMIUM_MAX = 4            # 中转站 GPT 视觉增强最多图
IMAGE_SEGMENTED_MIN = 12         # 分段视觉触发的最小帧数
IMAGE_MAIN_MAX = 8               # 主视觉默认最多图（config 可覆盖）
IMAGE_MULTIMODAL_DEFAULT = 8     # 多模态评论默认最多图
IMAGE_COMPRESS_MAX_SIDE = 640    # 压缩边长
IMAGE_COMPRESS_QUALITY = 82      # JPEG 质量
IMAGE_COMPRESS_SKIP_BYTES = 120 * 1024  # 小于此体积不压缩

# ============ 超时（秒） ============
TIMEOUT_VISION = 180
TIMEOUT_MULTIMODAL = 90
TIMEOUT_MULTIMODAL_INNER = 60
TIMEOUT_POLISH = 45
TIMEOUT_THB_WIKI = 30
TIMEOUT_ANALYSIS_TOTAL = 900
TIMEOUT_AUDIO_DOWNLOAD = 600
TIMEOUT_FFMPEG = 180
TIMEOUT_IMAGE_ANALYZE = 60
TIMEOUT_DM_IMAGE = 60

# ============ 状态限制 ============
STATE_REPLIED_MAX = 30000        # interactive_replied 上限
STATE_REPLIED_TRIM_TO = 25000    # 裁剪到
STATE_UNFOLLOW_TTL = 24 * 3600   # 取关缓存 TTL（秒）
STATE_UNFOLLOW_STALE = 30 * 86400  # 取关缓存清理阈值
STATE_HISTORY_KEEP = 1000        # 历史记录保留条数
STATE_USER_MEMORY_TTL_DAYS = 180  # 用户记忆 TTL

# ============ 节流 ============
THROTTLE_SESSION_MIN = 8.0       # 会话接口节流下限
THROTTLE_SESSION_JITTER = 6.0    # 会话接口节流抖动
THROTTLE_IMAGE_SECONDS = 1.0     # 图片下载节流
THROTTLE_OFFICIAL_SECONDS = 2.0  # 认证查询节流

# ============ 其它 ============
GREETING_BATCH_DEFAULT = 5       # 问候批次默认
GREETING_FOLLOWINGS_DEFAULT = 300  # 问候关注上限默认
GREETING_FAIL_LIMIT = 3          # 问候失败重试上限
PEAK_GRACE_SECONDS = 1800        # 定时任务宽限窗口
DEGRADED_COMMENT_LIMIT_DEFAULT = 20  # 降级模式评论上限默认
RETRY_SLEEP_509_BASE = 60        # -509 退避基数
RETRY_SLEEP_509_MAX = 60 * 5     # -509 退避上限
RETRY_SLEEP_NETWORK = 3          # 网络错误重试等待
