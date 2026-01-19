"""
Project Link - 日志系统模块
提供统一的日志记录功能，支持终端和文件双重输出
"""

import logging
from pathlib import Path
from typing import Optional


def setup_logger(name: str = "project_link") -> logging.Logger:
    """
    设置并返回日志记录器（使用标准模式防止重复初始化）
    
    Args:
        name: 日志记录器名称
    
    Returns:
        配置好的日志记录器
    """
    logger = logging.getLogger(name)
    
    # 如果已经配置过 handler，直接返回（防止重复初始化）
    if logger.handlers:
        return logger
    
    # 设置日志级别
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # 防止传播到根 logger
    
    # 创建日志目录
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # 文件处理器（DEBUG 级别，记录所有日志）
    log_file = log_dir / "app.log"
    file_handler = logging.FileHandler(log_file, encoding='utf-8', mode='a')
    file_handler.setLevel(logging.DEBUG)
    
    # 终端处理器（INFO 级别，只显示重要信息）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    
    # 日志格式（包含时间、级别、模块、函数名、行号、消息）
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:%(lineno)d - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger


def get_logger(name: str = "project_link") -> logging.Logger:
    """
    获取日志记录器（便捷函数）
    
    Args:
        name: 日志记录器名称
    
    Returns:
        日志记录器实例
    """
    return setup_logger(name)

