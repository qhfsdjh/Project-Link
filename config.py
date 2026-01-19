"""
Project Link - 配置管理模块
统一管理所有外部化配置，提供安全的类型转换
"""

import os
from typing import Union


def get_float(key: str, default: float) -> float:
    """
    安全地从环境变量读取浮点数
    
    Args:
        key: 环境变量键名
        default: 默认值（如果环境变量不存在或格式错误）
    
    Returns:
        浮点数值
    """
    try:
        value = os.getenv(key)
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def get_int(key: str, default: int) -> int:
    """
    安全地从环境变量读取整数
    
    Args:
        key: 环境变量键名
        default: 默认值（如果环境变量不存在或格式错误）
    
    Returns:
        整数值
    """
    try:
        value = os.getenv(key)
        if value is None:
            return default
        return int(value)
    except (ValueError, TypeError):
        return default


def get_str(key: str, default: str) -> str:
    """
    安全地从环境变量读取字符串
    
    Args:
        key: 环境变量键名
        default: 默认值（如果环境变量不存在）
    
    Returns:
        字符串值
    """
    return os.getenv(key, default)


# ==================== Ollama AI 配置 ====================
OLLAMA_MODEL = get_str("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = get_int("OLLAMA_TIMEOUT", 30)  # 秒

# AI 生成参数
PL_AI_TEMPERATURE = get_float("PL_AI_TEMPERATURE", 0.3)
PL_AI_MAX_RETRIES = get_int("PL_AI_MAX_RETRIES", 3)
PL_AI_RETRY_DELAY = get_int("PL_AI_RETRY_DELAY", 1)  # 秒

# ==================== 习惯学习配置 ====================
PL_HABIT_BOOST = get_float("PL_HABIT_BOOST", 0.1)
PL_HABIT_INITIAL_CONFIDENCE = get_float("PL_HABIT_INITIAL_CONFIDENCE", 0.5)
PL_HABIT_HIGH_CONFIDENCE_THRESHOLD = get_float("PL_HABIT_HIGH_CONFIDENCE_THRESHOLD", 0.7)

# ==================== 时区配置 ====================
PL_TIMEZONE = get_str("PL_TIMEZONE", "")  # 空字符串表示使用系统默认时区

