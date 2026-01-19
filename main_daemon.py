"""
Project Link - 核心守护进程
macOS 菜单栏应用，提供任务提醒、交互式对话框和动态菜单更新
"""

import rumps
import threading
import subprocess
import os
import signal
import sys
import sqlite3
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import database
import config
from utils.logger import get_logger
from utils.helpers import parse_time, escape_apple_script

# 初始化日志记录器（使用独立的 daemon.log）
logger = get_logger("daemon", log_file="daemon.log")


class ProjectLinkApp(rumps.App):
    """Project Link 菜单栏应用"""
    
    def __init__(self):
        """初始化应用"""
        super().__init__("🔗 Project Link")
        
        # 线程锁（用于保护共享资源）
        self.lock = threading.Lock()
        
        # 任务菜单项列表（用于管理动态菜单项）
        self.task_menu_items = []
        
        # 创建固定菜单项
        self.quick_chat_item = rumps.MenuItem("快速对话", callback=self.start_quick_chat)
        self.menu = [self.quick_chat_item]
        
        # 创建并启动后台调度器
        self.scheduler = BackgroundScheduler()
        self.scheduler.start()
        logger.info("后台调度器已启动")
        
        # 添加定时任务
        self.scheduler.add_job(
            self.update_menu,
            'interval',
            seconds=config.PL_DAEMON_MENU_UPDATE_INTERVAL,
            id='update_menu'
        )
        self.scheduler.add_job(
            self.check_and_notify,
            'interval',
            seconds=config.PL_DAEMON_CHECK_INTERVAL,
            id='check_tasks'
        )
        logger.info(f"定时任务已添加：菜单更新间隔 {config.PL_DAEMON_MENU_UPDATE_INTERVAL} 秒，任务检查间隔 {config.PL_DAEMON_CHECK_INTERVAL} 秒")
        
        # 启动时立即更新一次菜单
        self.update_menu()
        
        # 注册信号处理（优雅退出）
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        
        logger.info("Project Link 守护进程已启动")
    
    def _signal_handler(self, signum, frame):
        """信号处理函数（用于优雅退出）"""
        logger.info(f"收到信号 {signum}，开始优雅退出...")
        self.quit_application(None)
    
    @rumps.clicked("Quit")
    def quit_application(self, _):
        """优雅退出应用"""
        logger.info("用户选择退出，开始关闭调度器...")
        
        # 关闭后台调度器
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
            logger.info("后台调度器已关闭")
        
        # 调用父类的退出方法
        rumps.quit_application(None)
    
    def update_menu(self):
        """更新菜单列表（使用非阻塞锁）"""
        # 使用非阻塞锁，避免 UI 阻塞
        if not self.lock.acquire(blocking=False):
            logger.debug("菜单更新跳过（上一次更新仍在进行中）")
            return
        
        try:
            # 删除旧任务菜单项
            for item in self.task_menu_items:
                if item in self.menu:
                    del self.menu[item]
            self.task_menu_items.clear()
            
            # 添加分隔符
            self.menu.add(None)
            
            # 获取最新任务
            try:
                tasks = database.get_all_tasks(status='pending')[:5]
                
                if tasks:
                    for task in tasks:
                        # 使用 lambda 闭包正确绑定 task_id
                        menu_item = rumps.MenuItem(
                            f"{task['content'][:30]}...",
                            callback=lambda _, tid=task['id']: self.show_task_dialog(tid)
                        )
                        self.menu.add(menu_item)
                        self.task_menu_items.append(menu_item)
                else:
                    no_task_item = rumps.MenuItem("暂无任务")
                    self.menu.add(no_task_item)
                    self.task_menu_items.append(no_task_item)
            except Exception as e:
                logger.error(f"获取任务列表失败: {e}", exc_info=True)
                
        finally:
            # 确保锁被释放
            self.lock.release()
    
    def check_and_notify(self):
        """检查并通知任务"""
        try:
            with self.lock:
                # 获取即将到期的任务
                tasks = database.get_upcoming_tasks(hours=1, status='pending')
                logger.debug(f"检查到 {len(tasks)} 个即将到期的任务")
                
                for task in tasks:
                    try:  # 内层 try-except：单个任务出错不影响其他任务
                        if self.should_notify_task(task):
                            logger.info(f"需要通知任务 {task['id']}: {task['content'][:30]}...")
                            response = self.show_task_dialog(task['id'])
                            if response:
                                self.handle_dialog_response(response, task['id'])
                    except ValueError as e:
                        # 时间解析失败等可恢复错误
                        logger.warning(f"任务 {task.get('id')} 处理失败（可恢复）: {e}")
                        continue
                    except Exception as e:
                        # 其他不可恢复错误
                        logger.error(
                            f"任务 {task.get('id')} 处理失败（不可恢复）: {e}",
                            exc_info=True
                        )
                        continue
                        
        except sqlite3.Error as e:
            logger.error(f"数据库错误: {e}", exc_info=True)
            # 不中断守护进程，等待下次检查
        except Exception as e:
            logger.error(f"任务检查失败: {e}", exc_info=True)
            # 不中断守护进程，等待下次检查
    
    def should_notify_task(self, task: Dict[str, Any]) -> bool:
        """
        判断是否应该通知任务（基于优先级阶梯规则）
        
        Args:
            task: 任务字典
        
        Returns:
            True 如果需要通知，False 如果不需要
        """
        priority = task.get('priority', 3)
        notification_count = task.get('notification_count', 0)
        last_notified_at = task.get('last_notified_at')
        
        now = datetime.now().astimezone()
        
        # 高优先级（priority >= 4）
        if priority >= 4:
            if notification_count >= config.PL_DAEMON_HIGH_PRIORITY_MAX_COUNT:
                return False
            if last_notified_at:
                try:
                    last_notified = parse_time(last_notified_at)
                    interval_seconds = config.PL_DAEMON_HIGH_PRIORITY_INTERVAL * 60
                    if (now - last_notified).total_seconds() < interval_seconds:
                        return False
                except ValueError:
                    # 时间解析失败，允许通知
                    pass
            return True
        
        # 中优先级（priority == 3）
        elif priority == 3:
            if notification_count >= config.PL_DAEMON_MEDIUM_PRIORITY_MAX_COUNT:
                return False
            if last_notified_at:
                try:
                    last_notified = parse_time(last_notified_at)
                    interval_seconds = config.PL_DAEMON_MEDIUM_PRIORITY_INTERVAL * 60
                    if (now - last_notified).total_seconds() < interval_seconds:
                        return False
                except ValueError:
                    # 时间解析失败，允许通知
                    pass
            return True
        
        # 低优先级（priority <= 2）
        else:
            return notification_count == 0
    
    def show_task_dialog(self, task_id: int) -> Optional[str]:
        """
        显示任务对话框（置顶优化）
        
        Args:
            task_id: 任务 ID
        
        Returns:
            "完成", "推迟 30 分钟", "稍后", 或 None（超时/取消）
        """
        try:
            task = database.get_task_by_id(task_id)
            if not task:
                logger.warning(f"任务 {task_id} 不存在")
                return None
            
            content = escape_apple_script(task['content'])
            
            # 构建 AppleScript（置顶优化）
            script = f'''
            tell application "System Events" to activate
            try
                set theAnswer to display dialog "任务提醒: {content}" buttons {{"完成", "推迟 30 分钟", "稍后"}} default button "稍后"
                return button returned of theAnswer
            on error
                return "稍后"
            end try
            '''
            
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=config.PL_DAEMON_DIALOG_TIMEOUT
            )
            
            response = result.stdout.strip()
            
            # 验证返回值
            valid_responses = ["完成", "推迟 30 分钟", "稍后"]
            if response in valid_responses:
                logger.info(f"用户选择: {response} (任务 {task_id})")
                return response
            else:
                logger.warning(f"未知的对话框返回值: {response} (任务 {task_id})")
                return None
                
        except subprocess.TimeoutExpired:
            logger.warning(f"对话框超时，视为'稍后处理' (任务 {task_id})")
            return None
        except Exception as e:
            logger.error(f"对话框执行失败 (任务 {task_id}): {e}", exc_info=True)
            return None
    
    def handle_dialog_response(self, response: str, task_id: int):
        """
        处理对话框返回结果
        
        Args:
            response: 对话框返回的响应
            task_id: 任务 ID
        """
        try:
            if response == "完成":
                # 二次确认
                if self.confirm_completion(task_id):
                    database.update_task_status(task_id, 'done')
                    database.update_task_notification_time(task_id)
                    logger.info(f"任务 {task_id} 已标记为完成")
                    # 更新菜单（因为任务状态改变）
                    self.update_menu()
                else:
                    logger.info(f"用户取消完成任务 {task_id}")
            
            elif response == "推迟 30 分钟":
                # 推迟任务
                if self.postpone_task(task_id, minutes=config.PL_DAEMON_POSTPONE_MINUTES):
                    logger.info(f"任务 {task_id} 已推迟 {config.PL_DAEMON_POSTPONE_MINUTES} 分钟")
                else:
                    logger.warning(f"推迟任务 {task_id} 失败")
            
            elif response == "稍后":
                # 只更新通知时间（触发冷却计时）
                database.update_task_notification_time(task_id)
                logger.info(f"任务 {task_id} 标记为稍后提醒")
            
            else:
                logger.warning(f"未知的对话框响应: {response} (任务 {task_id})")
                
        except Exception as e:
            logger.error(f"处理对话框响应失败 (任务 {task_id}): {e}", exc_info=True)
    
    def confirm_completion(self, task_id: int) -> bool:
        """
        显示确认对话框（置顶优化）
        
        Args:
            task_id: 任务 ID
        
        Returns:
            True 如果用户确认，False 如果取消
        """
        try:
            task = database.get_task_by_id(task_id)
            if not task:
                return False
            
            content = escape_apple_script(task['content'])
            
            # 构建 AppleScript（置顶优化）
            script = f'''
            tell application "System Events" to activate
            try
                set theAnswer to display dialog "确认完成任务？\\n\\n{content}" buttons {{"确认", "取消"}} default button "确认"
                return button returned of theAnswer
            on error
                return "取消"
            end try
            '''
            
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=config.PL_DAEMON_DIALOG_TIMEOUT
            )
            
            response = result.stdout.strip()
            confirmed = response == "确认"
            
            if confirmed:
                logger.info(f"用户确认完成任务 {task_id}")
            else:
                logger.info(f"用户取消完成任务 {task_id}")
            
            return confirmed
            
        except subprocess.TimeoutExpired:
            logger.warning(f"确认对话框超时，视为取消 (任务 {task_id})")
            return False
        except Exception as e:
            logger.error(f"确认对话框失败 (任务 {task_id}): {e}", exc_info=True)
            return False
    
    def postpone_task(self, task_id: int, minutes: int) -> bool:
        """
        推迟任务（基于原 due_time 累加）
        
        Args:
            task_id: 任务 ID
            minutes: 推迟的分钟数
        
        Returns:
            True 如果成功，False 如果失败
        """
        try:
            task = database.get_task_by_id(task_id)
            if not task:
                logger.warning(f"任务 {task_id} 不存在")
                return False
            
            if task['due_time'] is None:
                logger.warning(f"任务 {task_id} 没有到期时间，无法推迟")
                return False
            
            # 解析原到期时间
            original_due = parse_time(task['due_time'])
            
            # 计算新到期时间（基于原 due_time 累加，而不是基于 now()）
            new_due = original_due + timedelta(minutes=minutes)
            
            # 转换为 ISO 格式
            new_due_str = new_due.isoformat()
            
            # 更新数据库
            database.update_task_due_time(task_id, new_due_str)
            
            # 更新通知时间
            database.update_task_notification_time(task_id)
            
            logger.info(f"任务 {task_id} 已推迟 {minutes} 分钟：{task['due_time']} -> {new_due_str}")
            return True
            
        except ValueError as e:
            logger.warning(f"时间解析失败，无法推迟任务 {task_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"推迟任务失败 (任务 {task_id}): {e}", exc_info=True)
            return False
    
    @rumps.clicked("快速对话")
    def start_quick_chat(self, _):
        """打开新的终端窗口运行 interpreter.py"""
        try:
            project_path = os.getcwd()
            
            # 转义路径（用于 AppleScript）
            escaped_path = project_path.replace("'", "\\'")
            
            # 构建 AppleScript
            script = f'''
            tell application "Terminal"
                do script "cd '{escaped_path}' && python3 interpreter.py"
                activate
            end tell
            '''
            
            subprocess.run(["osascript", "-e", script], check=False)
            logger.info("已打开快速对话窗口")
        except Exception as e:
            logger.error(f"打开终端失败: {e}", exc_info=True)
            rumps.alert("错误", f"无法打开终端: {e}")


def main():
    """主函数"""
    try:
        # 初始化数据库
        database.init_db()
        logger.info("数据库初始化完成")
        
        # 创建并运行应用
        app = ProjectLinkApp()
        app.run()
    except KeyboardInterrupt:
        logger.info("守护进程被用户中断")
    except Exception as e:
        logger.error(f"守护进程启动失败: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()

