"""
高性能数据库管理模块
使用 SQLite3 和 contextlib 进行资源管理
"""

import sqlite3
import contextlib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple, Any
from pathlib import Path
from utils.logger import get_logger  # 导入日志模块

# 初始化日志记录器
logger = get_logger("database")

# 导入配置（延迟导入，避免循环依赖）
_config = None

def _get_config():
    """延迟导入配置模块"""
    global _config
    if _config is None:
        import config
        _config = config
    return _config


# 数据库文件路径
DB_PATH = Path("app.db")


def get_column_names(table_name: str) -> List[str]:
    """
    获取表的列名列表（使用 PRAGMA 预检）
    
    Args:
        table_name: 表名
    
    Returns:
        列名列表
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        return [row[1] for row in cursor.fetchall()]


def get_current_time() -> str:
    """
    获取当前时间（ISO 格式，带时区信息）
    如果 PL_TIMEZONE 配置为空，使用系统默认时区
    
    Returns:
        ISO 格式时间字符串（带时区信息）
    """
    config = _get_config()
    
    if config.PL_TIMEZONE:
        # 使用指定的时区
        try:
            # Python 3.9+ 使用 zoneinfo
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(config.PL_TIMEZONE)).isoformat()
        except ImportError:
            # Python < 3.9，尝试使用 pytz（可选依赖）
            try:
                import pytz  # type: ignore  # 可选依赖，可能未安装
                tz = pytz.timezone(config.PL_TIMEZONE)
                return datetime.now(tz).isoformat()
            except ImportError:
                logger.warning("时区库不可用，使用系统默认时区")
                return datetime.now().astimezone().isoformat()
    else:
        # 使用系统默认时区（带时区信息）
        return datetime.now().astimezone().isoformat()


@contextlib.contextmanager
def get_db_connection():
    """
    数据库连接上下文管理器
    自动处理事务提交和回滚，确保资源正确释放
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    # 启用 WAL 模式以提高并发性能
    conn.execute("PRAGMA journal_mode=WAL")
    # 优化 SQLite 性能设置
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=10000")
    conn.execute("PRAGMA temp_store=MEMORY")
    # 启用外键约束
    conn.execute("PRAGMA foreign_keys=ON")
    
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """
    初始化数据库，创建所有表结构
    如果表已存在则跳过（使用 IF NOT EXISTS）
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 创建 tasks 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                due_time TEXT,
                category TEXT,
                priority INTEGER CHECK(priority >= 1 AND priority <= 5) DEFAULT 3,
                status TEXT CHECK(status IN ('pending', 'done', 'ignored', 'cancelled')) DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        
        # 创建 preferences 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 0.5 CHECK(confidence >= 0.0 AND confidence <= 1.0),
                source TEXT CHECK(source IN ('用户直说', 'AI推断')) DEFAULT 'AI推断',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        
        # 创建 memory_logs 表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                sentiment TEXT CHECK(sentiment IN ('positive', 'neutral', 'negative')),
                context_tag TEXT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        
        # 创建索引以提高查询性能
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status 
            ON tasks(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_due_time 
            ON tasks(due_time)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_category 
            ON tasks(category)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_preferences_key 
            ON preferences(key)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_preferences_confidence 
            ON preferences(confidence)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_logs_timestamp 
            ON memory_logs(timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_logs_context_tag 
            ON memory_logs(context_tag)
        """)
        
        conn.commit()
        logger.info("数据库初始化完成")
        
        # 执行数据库迁移（添加新字段）
        migrate_database()


def migrate_database():
    """
    数据库迁移函数（使用预检模式，避免异常控制流）
    安全地添加新字段，不丢失现有数据
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 1. 预检：获取现有字段
        tasks_columns = get_column_names("tasks")
        prefs_columns = get_column_names("preferences")
        memory_columns = get_column_names("memory_logs")
        
        logger.info("开始数据库迁移检查...")
        
        # 2. 迁移 tasks 表
        # 2.1 硬化：扩展 tasks.status 支持 cancelled（SQLite 无法直接 ALTER CHECK，需重建表）
        _migrate_tasks_status_check(cursor)

        if "last_notified_at" not in tasks_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN last_notified_at TEXT")
            logger.info("✅ 已添加 tasks.last_notified_at 字段")
        else:
            logger.debug("tasks.last_notified_at 字段已存在，跳过")
        
        if "notification_count" not in tasks_columns:
            cursor.execute("ALTER TABLE tasks ADD COLUMN notification_count INTEGER DEFAULT 0")
            logger.info("✅ 已添加 tasks.notification_count 字段")
        else:
            logger.debug("tasks.notification_count 字段已存在，跳过")
        
        # 3. 迁移 preferences 表
        if "created_at" not in prefs_columns:
            cursor.execute("ALTER TABLE preferences ADD COLUMN created_at TEXT")
            # 为现有记录补全 created_at（等于 updated_at）
            cursor.execute("""
                UPDATE preferences 
                SET created_at = updated_at 
                WHERE created_at IS NULL AND updated_at IS NOT NULL
            """)
            updated_count = cursor.rowcount
            logger.info(f"✅ 已添加 preferences.created_at 字段，并补全了 {updated_count} 条旧数据")
        else:
            logger.debug("preferences.created_at 字段已存在，跳过")
        
        # 4. 迁移 memory_logs 表
        if "is_processed" not in memory_columns:
            cursor.execute("ALTER TABLE memory_logs ADD COLUMN is_processed INTEGER DEFAULT 0")
            logger.info("✅ 已添加 memory_logs.is_processed 字段")
        else:
            logger.debug("memory_logs.is_processed 字段已存在，跳过")
        
        # 5. 创建新索引
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_last_notified_at 
            ON tasks(last_notified_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_notification_count 
            ON tasks(notification_count)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_logs_is_processed 
            ON memory_logs(is_processed)
        """)
        
        conn.commit()
        logger.info("数据库迁移完成")
        
        # 6. 验证迁移结果
        verify_migration()


def _migrate_tasks_status_check(cursor: sqlite3.Cursor) -> None:
    """
    扩展 tasks.status 的 CHECK 约束以支持 'cancelled'

    SQLite 不支持直接修改 CHECK 约束，因此采用“重建表”方式：
    - tasks_new 使用新的 CHECK(status IN (..., 'cancelled'))
    - 复制数据
    - 替换旧表
    - 重建索引（本文件其他逻辑也会 CREATE INDEX IF NOT EXISTS 兜底）
    """
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'")
    row = cursor.fetchone()
    if not row or not row[0]:
        return

    create_sql = row[0]
    if "status IN ('pending', 'done', 'ignored', 'cancelled')" in create_sql:
        logger.debug("tasks.status CHECK 已支持 cancelled，跳过重建")
        return

    if "status IN ('pending', 'done', 'ignored')" not in create_sql:
        # 未知的约束形态：保守跳过，避免破坏用户自定义 schema
        logger.warning("tasks.status CHECK 形态未知，跳过 cancelled 迁移；请手动检查 schema")
        return

    logger.info("开始迁移 tasks.status CHECK 以支持 cancelled（重建表）...")
    cursor.execute("PRAGMA foreign_keys=OFF")
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                due_time TEXT,
                category TEXT,
                priority INTEGER CHECK(priority >= 1 AND priority <= 5) DEFAULT 3,
                status TEXT CHECK(status IN ('pending', 'done', 'ignored', 'cancelled')) DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_notified_at TEXT,
                notification_count INTEGER DEFAULT 0
            )
        """)

        # 复制数据（保持原有字段）
        cursor.execute("""
            INSERT INTO tasks_new (id, content, due_time, category, priority, status, created_at, last_notified_at, notification_count)
            SELECT id, content, due_time, category, priority, status, created_at, last_notified_at, notification_count
            FROM tasks
        """)

        cursor.execute("DROP TABLE tasks")
        cursor.execute("ALTER TABLE tasks_new RENAME TO tasks")

        logger.info("✅ tasks.status CHECK 已迁移支持 cancelled")
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")


def verify_migration():
    """
    验证迁移是否成功，并打印结果到终端和日志
    """
    tasks_columns = get_column_names("tasks")
    prefs_columns = get_column_names("preferences")
    memory_columns = get_column_names("memory_logs")
    
    required_fields = {
        "tasks": ["last_notified_at", "notification_count"],
        "preferences": ["created_at"],
        "memory_logs": ["is_processed"]
    }
    
    all_ok = True
    missing_fields = []
    
    for table, fields in required_fields.items():
        columns = get_column_names(table)
        for field in fields:
            if field not in columns:
                logger.error(f"❌ {table}.{field} 字段迁移失败")
                missing_fields.append(f"{table}.{field}")
                all_ok = False
            else:
                logger.debug(f"✅ {table}.{field} 字段验证通过")
    
    # 打印到终端（用户可见）
    print("=" * 50)
    print("📊 数据库迁移验证结果")
    print("=" * 50)
    print(f"tasks 表字段 ({len(tasks_columns)} 个):")
    print(f"  {', '.join(tasks_columns)}")
    print(f"\npreferences 表字段 ({len(prefs_columns)} 个):")
    print(f"  {', '.join(prefs_columns)}")
    print(f"\nmemory_logs 表字段 ({len(memory_columns)} 个):")
    print(f"  {', '.join(memory_columns)}")
    print("=" * 50)
    
    if all_ok:
        logger.info("✅ 数据库迁移验证通过")
        print("✅ 所有字段迁移成功！")
    else:
        logger.error(f"❌ 数据库迁移验证失败，缺失字段: {', '.join(missing_fields)}")
        print(f"❌ 迁移验证失败，缺失字段: {', '.join(missing_fields)}")
    
    return all_ok


def record_interaction(content: str, sentiment: str = 'neutral', tag: Optional[str] = None) -> int:
    """
    一次性记录用户交互到 memory_logs 表
    
    Args:
        content: 交互内容
        sentiment: 情感倾向 ('positive', 'neutral', 'negative')
        tag: 上下文标签 (如: 'work', 'life', 'idle')
    
    Returns:
        插入记录的 ID
    """
    if sentiment not in ('positive', 'neutral', 'negative'):
        raise ValueError(f"sentiment 必须是 'positive', 'neutral' 或 'negative'，当前值: {sentiment}")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO memory_logs (content, sentiment, context_tag, timestamp)
            VALUES (?, ?, ?, ?)
        """, (content, sentiment, tag, get_current_time()))
        conn.commit()
        return cursor.lastrowid


def update_interaction(record_id: int, sentiment: str, tag: Optional[str] = None):
    """
    更新已存在的交互记录（用于修复重复记录问题）
    
    Args:
        record_id: 要更新的记录 ID
        sentiment: 情感倾向 ('positive', 'neutral', 'negative')
        tag: 上下文标签 (如: 'work', 'life', 'idle')
    """
    if sentiment not in ('positive', 'neutral', 'negative'):
        raise ValueError(f"sentiment 必须是 'positive', 'neutral' 或 'negative'，当前值: {sentiment}")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE memory_logs 
            SET sentiment = ?, context_tag = ?
            WHERE id = ?
        """, (sentiment, tag, record_id))
        conn.commit()


def update_habit(key: str, value: str, boost: float = 0.1, source: str = 'AI推断'):
    """
    更新或创建用户习惯偏好
    每次发现用户符合某个习惯时，给置信度加分
    
    Args:
        key: 习惯键名
        value: 习惯值
        boost: 置信度提升幅度（默认 0.1）
        source: 来源 ('用户直说' 或 'AI推断')
    """
    if not (0.0 <= boost <= 1.0):
        raise ValueError(f"boost 必须在 0.0 到 1.0 之间，当前值: {boost}")
    
    if source not in ('用户直说', 'AI推断'):
        raise ValueError(f"source 必须是 '用户直说' 或 'AI推断'，当前值: {source}")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 检查记录是否存在
        cursor.execute("SELECT confidence FROM preferences WHERE key = ?", (key,))
        result = cursor.fetchone()
        
        if result:
            # 更新现有记录：增加置信度（但不超过 1.0）
            current_confidence = result[0]
            new_confidence = min(1.0, current_confidence + boost)
            cursor.execute("""
                UPDATE preferences 
                SET value = ?, confidence = ?, source = ?, updated_at = ?
                WHERE key = ?
            """, (value, new_confidence, source, get_current_time(), key))
        else:
            # 创建新记录：初始置信度从配置读取 + boost（但不超过 1.0）
            config = _get_config()
            initial_confidence_base = config.PL_HABIT_INITIAL_CONFIDENCE
            initial_confidence = min(1.0, initial_confidence_base + boost)
            now = get_current_time()
            cursor.execute("""
                INSERT INTO preferences (key, value, confidence, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (key, value, initial_confidence, source, now, now))
        
        conn.commit()


def get_high_confidence_prefs(threshold: float = 0.7) -> List[Dict[str, Any]]:
    """
    获取高置信度的用户偏好习惯，用于决策
    
    Args:
        threshold: 置信度阈值（默认 0.7，即 70%）
    
    Returns:
        高置信度偏好列表，每个元素包含 key, value, confidence, source, updated_at
    """
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold 必须在 0.0 到 1.0 之间，当前值: {threshold}")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT key, value, confidence, source, updated_at, created_at
            FROM preferences
            WHERE confidence >= ?
            ORDER BY confidence DESC, updated_at DESC
        """, (threshold,))
        
        results = cursor.fetchall()
        return [
            {
                'key': row[0],
                'value': row[1],
                'confidence': row[2],
                'source': row[3],
                'updated_at': row[4],
                'created_at': row[5] if len(row) > 5 else None
            }
            for row in results
        ]


# 额外的实用函数

def get_all_tasks(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    获取所有任务，可选择性过滤状态
    
    Args:
        status: 可选的状态过滤 ('pending', 'done', 'ignored')
    
    Returns:
        任务列表
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        if status:
            cursor.execute("""
                SELECT id, content, due_time, category, priority, status, created_at, 
                       last_notified_at, notification_count
                FROM tasks
                WHERE status = ?
                ORDER BY priority DESC, due_time ASC, created_at ASC
            """, (status,))
        else:
            cursor.execute("""
                SELECT id, content, due_time, category, priority, status, created_at,
                       last_notified_at, notification_count
                FROM tasks
                ORDER BY priority DESC, due_time ASC, created_at ASC
            """)
        
        results = cursor.fetchall()
        return [
            {
                'id': row[0],
                'content': row[1],
                'due_time': row[2],
                'category': row[3],
                'priority': row[4],
                'status': row[5],
                'created_at': row[6],
                'last_notified_at': row[7],
                'notification_count': row[8] if len(row) > 8 else 0
            }
            for row in results
        ]


def add_task(content: str, due_time: Optional[str] = None, 
             category: Optional[str] = None, priority: int = 3) -> int:
    """
    添加新任务
    
    Returns:
        新创建的任务 ID
    """
    if not (1 <= priority <= 5):
        raise ValueError(f"priority 必须在 1 到 5 之间，当前值: {priority}")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO tasks (content, due_time, category, priority, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (content, due_time, category, priority, get_current_time()))
        conn.commit()
        return cursor.lastrowid


def update_task_status(task_id: int, status: str):
    """
    更新任务状态
    """
    if status not in ('pending', 'done', 'ignored', 'cancelled'):
        raise ValueError(f"status 必须是 'pending', 'done', 'ignored' 或 'cancelled'，当前值: {status}")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks 
            SET status = ?
            WHERE id = ?
        """, (status, task_id))
        conn.commit()


def cancel_task(task_id: int):
    """
    软取消任务（用于“取消上一个”等场景）
    等价于 update_task_status(task_id, 'cancelled')
    """
    update_task_status(task_id, 'cancelled')


def update_task_content(task_id: int, content: str):
    """
    更新任务内容（标题/描述）
    """
    if not content or not isinstance(content, str):
        raise ValueError("content 不能为空")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks
            SET content = ?
            WHERE id = ?
        """, (content, task_id))
        conn.commit()
        logger.debug(f"已更新任务 {task_id} 的内容为: {content}")


def get_recent_tasks(status: str = 'pending', limit: int = 3) -> List[Dict[str, Any]]:
    """
    获取最近创建的任务列表（默认：最近 3 条 pending）
    用于 Interpreter 的“上一个任务/刚才那个”上下文候选。
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, content, due_time, category, priority, status, created_at,
                   last_notified_at, notification_count
            FROM tasks
            WHERE status = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (status, limit))
        results = cursor.fetchall()
        return [
            {
                'id': row[0],
                'content': row[1],
                'due_time': row[2],
                'category': row[3],
                'priority': row[4],
                'status': row[5],
                'created_at': row[6],
                'last_notified_at': row[7],
                'notification_count': row[8] if len(row) > 8 else 0
            }
            for row in results
        ]


def update_task_notification_time(task_id: int):
    """
    更新任务的最后通知时间和通知次数
    用于 daemon.py 记录通知历史
    
    Args:
        task_id: 任务 ID
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks 
            SET last_notified_at = ?, 
                notification_count = notification_count + 1
            WHERE id = ?
        """, (get_current_time(), task_id))
        conn.commit()
        logger.debug(f"已更新任务 {task_id} 的通知时间和次数")


def update_task_due_time(task_id: int, new_due_time: Optional[str]):
    """
    更新任务的到期时间
    
    Args:
        task_id: 任务 ID
        new_due_time: 新的到期时间（ISO 8601 格式，可为 None）
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks 
            SET due_time = ?
            WHERE id = ?
        """, (new_due_time, task_id))
        conn.commit()
        logger.debug(f"已更新任务 {task_id} 的到期时间为: {new_due_time}")


def get_task_by_id(task_id: int) -> Optional[Dict[str, Any]]:
    """
    根据 ID 获取单个任务
    
    Args:
        task_id: 任务 ID
    
    Returns:
        任务字典（包含所有字段），如果不存在返回 None
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, content, due_time, category, priority, status, created_at,
                   last_notified_at, notification_count
            FROM tasks
            WHERE id = ?
        """, (task_id,))
        
        row = cursor.fetchone()
        if not row:
            return None
        
        return {
            'id': row[0],
            'content': row[1],
            'due_time': row[2],
            'category': row[3],
            'priority': row[4],
            'status': row[5],
            'created_at': row[6],
            'last_notified_at': row[7],
            'notification_count': row[8] if len(row) > 8 else 0
        }


def get_upcoming_tasks(hours: int = 24, status: str = 'pending') -> List[Dict[str, Any]]:
    """
    获取未来 N 小时内到期的任务
    
    Args:
        hours: 未来多少小时内（默认 24 小时）
        status: 任务状态过滤（默认 'pending'）
    
    Returns:
        即将到期的任务列表
    """
    # 在 Python 层进行时间比较，更稳健
    now = datetime.now().astimezone()  # 使用带时区的当前时间
    future_time = now + timedelta(hours=hours)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # 先查询所有符合条件的任务（只过滤状态和 due_time 非空）
        cursor.execute("""
            SELECT id, content, due_time, category, priority, status, created_at,
                   last_notified_at, notification_count
            FROM tasks
            WHERE status = ?
              AND due_time IS NOT NULL
            ORDER BY due_time ASC, priority DESC
        """, (status,))
        
        results = cursor.fetchall()
        
        # 在 Python 层进行时间比较和过滤
        filtered_tasks = []
        for row in results:
            due_time_str = row[2]
            if not due_time_str:
                continue
            
            try:
                # 将数据库时间字符串转换为 datetime 对象（处理时区）
                due_time_str_normalized = due_time_str.replace('Z', '+00:00')
                due_time_dt = datetime.fromisoformat(due_time_str_normalized)
                
                # 确保 due_time_dt 有时区信息
                if due_time_dt.tzinfo is None:
                    # 如果没有时区信息，假设为本地时区
                    due_time_dt = due_time_dt.replace(tzinfo=now.tzinfo)
                
                # 在 Python 层进行时间比较
                if now < due_time_dt <= future_time:
                    filtered_tasks.append({
                        'id': row[0],
                        'content': row[1],
                        'due_time': row[2],
                        'category': row[3],
                        'priority': row[4],
                        'status': row[5],
                        'created_at': row[6],
                        'last_notified_at': row[7],
                        'notification_count': row[8] if len(row) > 8 else 0
                    })
            except (ValueError, AttributeError) as e:
                # 如果时间格式解析失败，记录日志但继续处理其他任务
                logger.warning(f"无法解析任务 {row[0]} 的 due_time '{due_time_str}': {e}")
                continue
        
        return filtered_tasks


def get_overdue_tasks(status: str = 'pending') -> List[Dict[str, Any]]:
    """
    获取已过期的任务（截止时间已过但状态仍为 pending）
    
    Args:
        status: 任务状态过滤（默认 'pending'）
    
    Returns:
        过期任务列表
    """
    # 在 Python 层进行时间比较，更稳健
    now = datetime.now().astimezone()  # 使用带时区的当前时间
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # 先查询所有符合条件的任务（只过滤状态和 due_time 非空）
        cursor.execute("""
            SELECT id, content, due_time, category, priority, status, created_at,
                   last_notified_at, notification_count
            FROM tasks
            WHERE status = ?
              AND due_time IS NOT NULL
            ORDER BY due_time ASC, priority DESC
        """, (status,))
        
        results = cursor.fetchall()
        
        # 在 Python 层进行时间比较和过滤
        overdue_tasks = []
        for row in results:
            due_time_str = row[2]
            if not due_time_str:
                continue
            
            try:
                # 将数据库时间字符串转换为 datetime 对象（处理时区）
                due_time_str_normalized = due_time_str.replace('Z', '+00:00')
                due_time_dt = datetime.fromisoformat(due_time_str_normalized)
                
                # 确保 due_time_dt 有时区信息
                if due_time_dt.tzinfo is None:
                    # 如果没有时区信息，假设为本地时区
                    due_time_dt = due_time_dt.replace(tzinfo=now.tzinfo)
                
                # 在 Python 层进行时间比较
                if due_time_dt < now:
                    overdue_tasks.append({
                        'id': row[0],
                        'content': row[1],
                        'due_time': row[2],
                        'category': row[3],
                        'priority': row[4],
                        'status': row[5],
                        'created_at': row[6],
                        'last_notified_at': row[7],
                        'notification_count': row[8] if len(row) > 8 else 0
                    })
            except (ValueError, AttributeError) as e:
                # 如果时间格式解析失败，记录日志但继续处理其他任务
                logger.warning(f"无法解析任务 {row[0]} 的 due_time '{due_time_str}': {e}")
                continue
        
        return overdue_tasks


def get_tasks_by_date_range(start_date: str, end_date: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    按日期范围查询任务
    
    Args:
        start_date: 开始日期（ISO 格式，如 '2026-01-18T00:00:00'）
        end_date: 结束日期（ISO 格式，如 '2026-01-19T23:59:59'）
        status: 可选的状态过滤
    
    Returns:
        指定日期范围内的任务列表
    """
    # 在 Python 层进行时间比较，更稳健
    try:
        # 将输入的时间字符串转换为 datetime 对象
        start_date_normalized = start_date.replace('Z', '+00:00')
        end_date_normalized = end_date.replace('Z', '+00:00')
        start_dt = datetime.fromisoformat(start_date_normalized)
        end_dt = datetime.fromisoformat(end_date_normalized)
        
        # 确保有时区信息
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    except (ValueError, AttributeError) as e:
        logger.error(f"无法解析日期范围参数: start_date={start_date}, end_date={end_date}, 错误: {e}")
        return []
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 先查询所有符合条件的任务（只过滤状态和 due_time 非空）
        if status:
            cursor.execute("""
                SELECT id, content, due_time, category, priority, status, created_at,
                       last_notified_at, notification_count
                FROM tasks
                WHERE status = ?
                  AND due_time IS NOT NULL
                ORDER BY due_time ASC, priority DESC
            """, (status,))
        else:
            cursor.execute("""
                SELECT id, content, due_time, category, priority, status, created_at,
                       last_notified_at, notification_count
                FROM tasks
                WHERE due_time IS NOT NULL
                ORDER BY due_time ASC, priority DESC
            """)
        
        results = cursor.fetchall()
        
        # 在 Python 层进行时间比较和过滤
        filtered_tasks = []
        for row in results:
            due_time_str = row[2]
            if not due_time_str:
                continue
            
            try:
                # 将数据库时间字符串转换为 datetime 对象（处理时区）
                due_time_str_normalized = due_time_str.replace('Z', '+00:00')
                due_time_dt = datetime.fromisoformat(due_time_str_normalized)
                
                # 确保 due_time_dt 有时区信息
                if due_time_dt.tzinfo is None:
                    due_time_dt = due_time_dt.replace(tzinfo=start_dt.tzinfo)
                
                # 在 Python 层进行时间比较
                if start_dt <= due_time_dt <= end_dt:
                    filtered_tasks.append({
                        'id': row[0],
                        'content': row[1],
                        'due_time': row[2],
                        'category': row[3],
                        'priority': row[4],
                        'status': row[5],
                        'created_at': row[6],
                        'last_notified_at': row[7],
                        'notification_count': row[8] if len(row) > 8 else 0
                    })
            except (ValueError, AttributeError) as e:
                # 如果时间格式解析失败，记录日志但继续处理其他任务
                logger.warning(f"无法解析任务 {row[0]} 的 due_time '{due_time_str}': {e}")
                continue
        
        return filtered_tasks


def get_all_preferences() -> List[Dict[str, Any]]:
    """
    获取所有偏好设置
    
    Returns:
        所有偏好列表
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT key, value, confidence, source, updated_at, created_at
            FROM preferences
            ORDER BY confidence DESC, updated_at DESC
        """)
        
        results = cursor.fetchall()
        return [
            {
                'key': row[0],
                'value': row[1],
                'confidence': row[2],
                'source': row[3],
                'updated_at': row[4],
                'created_at': row[5] if len(row) > 5 else None
            }
            for row in results
        ]


def get_recent_memory_logs(limit: int = 50, tag: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    获取最近的交互记录
    
    Args:
        limit: 返回记录数量限制
        tag: 可选的上下文标签过滤
    
    Returns:
        最近的交互记录列表
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        if tag:
            cursor.execute("""
                SELECT id, content, sentiment, context_tag, timestamp
                FROM memory_logs
                WHERE context_tag = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (tag, limit))
        else:
            cursor.execute("""
                SELECT id, content, sentiment, context_tag, timestamp
                FROM memory_logs
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
        
        results = cursor.fetchall()
        return [
            {
                'id': row[0],
                'content': row[1],
                'sentiment': row[2],
                'context_tag': row[3],
                'timestamp': row[4]
            }
            for row in results
        ]


def get_unprocessed_memory_logs(days: int = 7, limit: int = 100) -> List[Dict[str, Any]]:
    """
    获取未处理的记忆日志（用于反思脚本）
    
    Args:
        days: 查询最近多少天的记录（默认 7 天）
        limit: 最多返回多少条（默认 100 条）
    
    Returns:
        未处理的记忆日志列表
    """
    # 在 Python 层进行时间比较，更稳健
    now = datetime.now().astimezone()
    cutoff_date = now - timedelta(days=days)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # 先查询所有未处理的记录
        cursor.execute("""
            SELECT id, content, sentiment, context_tag, timestamp
            FROM memory_logs
            WHERE is_processed = 0
            ORDER BY timestamp ASC
        """)
        
        results = cursor.fetchall()
        
        # 在 Python 层进行时间比较和过滤
        filtered_logs = []
        for row in results:
            timestamp_str = row[4]
            if not timestamp_str:
                continue
            
            try:
                # 将数据库时间字符串转换为 datetime 对象（处理时区）
                timestamp_str_normalized = timestamp_str.replace('Z', '+00:00')
                timestamp_dt = datetime.fromisoformat(timestamp_str_normalized)
                
                # 确保 timestamp_dt 有时区信息
                if timestamp_dt.tzinfo is None:
                    timestamp_dt = timestamp_dt.replace(tzinfo=now.tzinfo)
                
                # 在 Python 层进行时间比较
                if timestamp_dt >= cutoff_date:
                    filtered_logs.append({
                        'id': row[0],
                        'content': row[1],
                        'sentiment': row[2],
                        'context_tag': row[3],
                        'timestamp': row[4]
                    })
                    
                    # 达到限制数量后停止
                    if len(filtered_logs) >= limit:
                        break
            except (ValueError, AttributeError) as e:
                # 如果时间格式解析失败，记录日志但继续处理其他记录
                logger.warning(f"无法解析记忆日志 {row[0]} 的 timestamp '{timestamp_str}': {e}")
                continue
        
        return filtered_logs


def mark_memory_log_processed(log_id: int):
    """
    标记记忆日志为已处理（用于反思脚本）
    
    Args:
        log_id: 记忆日志 ID
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE memory_logs 
            SET is_processed = 1 
            WHERE id = ?
        """, (log_id,))
        conn.commit()
        logger.debug(f"已标记记忆日志 {log_id} 为已处理")


def get_status() -> Dict[str, Any]:
    """
    获取数据库状态信息（自检功能）
    
    Returns:
        包含各种统计信息的字典
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 统计任务数量（按状态分组）
        cursor.execute("""
            SELECT status, COUNT(*) 
            FROM tasks 
            GROUP BY status
        """)
        task_status_counts = dict(cursor.fetchall())
        total_tasks = sum(task_status_counts.values())
        
        # 统计高置信度偏好数量（从配置读取阈值）
        config = _get_config()
        threshold = config.PL_HABIT_HIGH_CONFIDENCE_THRESHOLD
        cursor.execute("""
            SELECT COUNT(*) 
            FROM preferences 
            WHERE confidence >= ?
        """, (threshold,))
        high_conf_prefs_count = cursor.fetchone()[0]
        
        # 统计所有偏好数量
        cursor.execute("SELECT COUNT(*) FROM preferences")
        total_prefs_count = cursor.fetchone()[0]
        
        # 统计记忆日志数量
        cursor.execute("SELECT COUNT(*) FROM memory_logs")
        total_memory_logs = cursor.fetchone()[0]
        
        # 统计最近7天的记忆日志
        cursor.execute("""
            SELECT COUNT(*) 
            FROM memory_logs 
            WHERE timestamp >= datetime('now', '-7 days')
        """)
        recent_memory_logs = cursor.fetchone()[0]
        
        # 获取平均置信度
        cursor.execute("""
            SELECT AVG(confidence) 
            FROM preferences
        """)
        avg_confidence = cursor.fetchone()[0]
        if avg_confidence is None:
            avg_confidence = 0.0
        
        # 检查数据库文件大小
        db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        db_size_mb = db_size / (1024 * 1024)
        
        return {
            'total_tasks': total_tasks,
            'task_status': task_status_counts,
            'high_conf_prefs': high_conf_prefs_count,
            'total_prefs': total_prefs_count,
            'total_memory_logs': total_memory_logs,
            'recent_memory_logs': recent_memory_logs,
            'avg_confidence': avg_confidence,
            'db_size_mb': db_size_mb,
            'is_healthy': True  # 可以添加更多健康检查逻辑
        }


def print_status():
    """
    打印友好的状态信息（用于命令行）
    """
    try:
        status = get_status()
        
        print("=" * 50)
        print("📊 Project Link 数据库状态")
        print("=" * 50)
        
        # 任务统计
        print(f"\n📋 任务管理")
        print(f"  总任务数: {status['total_tasks']}")
        if status['task_status']:
            for stat, count in status['task_status'].items():
                print(f"    - {stat}: {count} 条")
        else:
            print("    (暂无任务)")
        
        # 偏好统计
        print(f"\n🧠 用户偏好")
        print(f"  总偏好数: {status['total_prefs']}")
        print(f"  高置信度偏好 (≥0.7): {status['high_conf_prefs']} 个")
        if status['total_prefs'] > 0:
            print(f"  平均置信度: {status['avg_confidence']:.2%}")
        
        # 记忆统计
        print(f"\n💭 记忆日志")
        print(f"  总记录数: {status['total_memory_logs']}")
        print(f"  最近7天: {status['recent_memory_logs']} 条")
        
        # 数据库信息
        print(f"\n💾 数据库信息")
        print(f"  文件大小: {status['db_size_mb']:.2f} MB")
        print(f"  状态: {'✅ 正常' if status['is_healthy'] else '⚠️  异常'}")
        
        # 成长提示
        print(f"\n🌱 成长状态")
        if status['high_conf_prefs'] == 0:
            print("  AI 还在学习中，尚未确认任何高置信度习惯...")
        elif status['high_conf_prefs'] < 3:
            print(f"  AI 已确认 {status['high_conf_prefs']} 个习惯，正在成长中...")
        elif status['high_conf_prefs'] < 10:
            print(f"  AI 已确认 {status['high_conf_prefs']} 个习惯，越来越了解你了！")
        else:
            print(f"  AI 已确认 {status['high_conf_prefs']} 个习惯，非常了解你的工作模式！")
        
        print("=" * 50)
        
    except Exception as e:
        logger.error(f"状态检查失败: {e}", exc_info=True)
        print(f"❌ 状态检查失败: {e}")
        print("   提示: 请先运行 init_db() 初始化数据库")


def clean_test_data():
    """
    清理测试数据（删除所有数据）
    注意：这会删除所有数据，请谨慎使用
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 删除所有数据
        cursor.execute("DELETE FROM tasks")
        cursor.execute("DELETE FROM preferences")
        cursor.execute("DELETE FROM memory_logs")
        
        # 重置自增ID（如果 sqlite_sequence 表存在）
        try:
            cursor.execute("DELETE FROM sqlite_sequence WHERE name IN ('tasks', 'preferences', 'memory_logs')")
        except sqlite3.OperationalError:
            # sqlite_sequence 表可能不存在，忽略错误
            pass
        
        conn.commit()
        logger.info("测试数据已清理完成")
        print("✅ 所有数据已清理完成")


def clean_all_data():
    """
    清理所有数据（危险操作）
    """
    confirm = input("⚠️  警告：这将删除所有数据！确认请输入 'yes': ")
    if confirm.lower() == 'yes':
        clean_test_data()
        logger.warning("所有数据已清理（用户确认）")
        print("✅ 所有数据已清理")
    else:
        logger.info("清理操作已取消")
        print("❌ 操作已取消")


if __name__ == "__main__":
    import sys
    
    # 检查命令行参数
    if len(sys.argv) > 1:
        if sys.argv[1] == "--status":
            # 自检模式：显示数据库状态
            print_status()
        elif sys.argv[1] == "--clean":
            # 清理模式：删除所有测试数据
            clean_test_data()
        elif sys.argv[1] == "--clean-all":
            # 清理所有数据（危险操作）
            clean_all_data()
        elif sys.argv[1] == "--test":
            # 测试模式：运行测试代码（会创建测试数据）
            logger.info("开始运行数据库测试")
            print("初始化数据库...")
            init_db()
            
            # 测试记录交互
            record_interaction("用户完成了工作项目", "positive", "work")
            record_interaction("用户感到疲惫", "negative", "life")
            logger.debug("测试记录交互完成")
            
            # 测试更新习惯
            update_habit("morning_routine", "喝咖啡", boost=0.2)
            update_habit("morning_routine", "喝咖啡", boost=0.1)  # 再次确认，置信度提升
            update_habit("work_hours", "9-18", boost=0.15, source="用户直说")
            logger.debug("测试更新习惯完成")
            
            # 测试获取高置信度偏好
            high_conf_prefs = get_high_confidence_prefs(threshold=0.6)
            print("\n高置信度偏好:")
            for pref in high_conf_prefs:
                print(f"  {pref['key']}: {pref['value']} (置信度: {pref['confidence']:.2f})")
            
            logger.info("数据库模块测试完成")
            print("\n数据库模块测试完成！")
            print("\n提示: 使用 'python database.py --status' 查看数据库状态")
            print("提示: 使用 'python database.py --clean' 清理测试数据")
        else:
            print("用法:")
            print("  python database.py --status      # 查看数据库状态")
            print("  python database.py --test        # 运行测试（会创建测试数据）")
            print("  python database.py --clean       # 清理测试数据")
            print("  python database.py --clean-all   # 清理所有数据（危险）")
    else:
        # 默认模式：只初始化数据库，不创建测试数据
        logger.info("数据库模块启动（默认模式）")
        print("初始化数据库...")
        init_db()
        logger.info("数据库初始化完成")
        print("✅ 数据库初始化完成！")
        print("\n提示:")
        print("  - 使用 'python database.py --status' 查看数据库状态")
        print("  - 使用 'python database.py --test' 运行测试（会创建测试数据）")

