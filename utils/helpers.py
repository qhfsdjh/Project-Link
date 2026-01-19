"""
Project Link - 辅助工具函数模块
提供时间解析、字符串转义等通用工具函数
"""

from datetime import datetime
from typing import Optional


def parse_time(time_str: str) -> datetime:
    """
    解析 ISO 8601 格式时间字符串（带时区）
    
    Args:
        time_str: ISO 8601 格式时间字符串（如 "2026-01-20T10:00:00+08:00" 或 "2026-01-20T10:00:00Z"）
    
    Returns:
        datetime 对象（带时区信息）
    
    Raises:
        ValueError: 如果时间格式不正确
    """
    if not time_str:
        raise ValueError("时间字符串不能为空")
    
    # 处理 Z 后缀（UTC），转换为 +00:00
    normalized = time_str.replace('Z', '+00:00')
    
    try:
        # 解析 ISO 格式时间
        dt = datetime.fromisoformat(normalized)
    except ValueError as e:
        raise ValueError(f"无法解析时间字符串 '{time_str}': {e}")
    
    # 如果没有时区信息，使用系统默认时区
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    
    return dt


def escape_apple_script(text: str) -> str:
    """
    转义 AppleScript 字符串中的特殊字符
    
    AppleScript 使用单引号包裹字符串，需要转义：
    - 反斜杠 \\ → \\\\
    - 单引号 ' → \\'
    - 换行符 \\n → \\\\n
    
    Args:
        text: 需要转义的文本
    
    Returns:
        转义后的文本
    """
    if not text:
        return ""
    
    # 先转义反斜杠，再转义单引号
    escaped = text.replace('\\', '\\\\').replace("'", "\\'")
    
    # 转义换行符
    escaped = escaped.replace('\n', '\\n')
    
    return escaped

