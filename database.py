"""
高性能数据库管理模块
使用 SQLite3 和 contextlib 进行资源管理
"""

import sqlite3
import contextlib
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Any
from pathlib import Path


# 数据库文件路径
DB_PATH = Path("app.db")


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
                status TEXT CHECK(status IN ('pending', 'done', 'ignored')) DEFAULT 'pending',
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
        print("数据库初始化完成")


def record_interaction(content: str, sentiment: str = 'neutral', tag: Optional[str] = None):
    """
    一次性记录用户交互到 memory_logs 表
    
    Args:
        content: 交互内容
        sentiment: 情感倾向 ('positive', 'neutral', 'negative')
        tag: 上下文标签 (如: 'work', 'life', 'idle')
    """
    if sentiment not in ('positive', 'neutral', 'negative'):
        raise ValueError(f"sentiment 必须是 'positive', 'neutral' 或 'negative'，当前值: {sentiment}")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO memory_logs (content, sentiment, context_tag, timestamp)
            VALUES (?, ?, ?, ?)
        """, (content, sentiment, tag, datetime.now().isoformat()))
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
            """, (value, new_confidence, source, datetime.now().isoformat(), key))
        else:
            # 创建新记录：初始置信度为 0.5 + boost（但不超过 1.0）
            initial_confidence = min(1.0, 0.5 + boost)
            cursor.execute("""
                INSERT INTO preferences (key, value, confidence, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (key, value, initial_confidence, source, datetime.now().isoformat()))
        
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
            SELECT key, value, confidence, source, updated_at
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
                'updated_at': row[4]
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
                SELECT id, content, due_time, category, priority, status, created_at
                FROM tasks
                WHERE status = ?
                ORDER BY priority DESC, due_time ASC, created_at ASC
            """, (status,))
        else:
            cursor.execute("""
                SELECT id, content, due_time, category, priority, status, created_at
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
                'created_at': row[6]
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
        """, (content, due_time, category, priority, datetime.now().isoformat()))
        conn.commit()
        return cursor.lastrowid


def update_task_status(task_id: int, status: str):
    """
    更新任务状态
    """
    if status not in ('pending', 'done', 'ignored'):
        raise ValueError(f"status 必须是 'pending', 'done' 或 'ignored'，当前值: {status}")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE tasks 
            SET status = ?
            WHERE id = ?
        """, (status, task_id))
        conn.commit()


def get_all_preferences() -> List[Dict[str, Any]]:
    """
    获取所有偏好设置
    
    Returns:
        所有偏好列表
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT key, value, confidence, source, updated_at
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
                'updated_at': row[4]
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


if __name__ == "__main__":
    # 测试代码
    init_db()
    
    # 测试记录交互
    record_interaction("用户完成了工作项目", "positive", "work")
    record_interaction("用户感到疲惫", "negative", "life")
    
    # 测试更新习惯
    update_habit("morning_routine", "喝咖啡", boost=0.2)
    update_habit("morning_routine", "喝咖啡", boost=0.1)  # 再次确认，置信度提升
    update_habit("work_hours", "9-18", boost=0.15, source="用户直说")
    
    # 测试获取高置信度偏好
    high_conf_prefs = get_high_confidence_prefs(threshold=0.6)
    print("\n高置信度偏好:")
    for pref in high_conf_prefs:
        print(f"  {pref['key']}: {pref['value']} (置信度: {pref['confidence']:.2f})")
    
    print("\n数据库模块测试完成！")

