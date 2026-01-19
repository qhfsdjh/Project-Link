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
import re
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
        
        # 禁用 rumps 默认 Quit（我们提供自定义“退出”，用于优雅关闭 scheduler）
        # 说明：rumps 会默认注入 Quit 菜单项；如果不禁用，就会出现两个 Quit。
        self.quit_button = None

        # 创建固定菜单项（后续 update_menu 会重建整个菜单）
        # 注意：rumps 支持菜单项快捷键（如 Cmd+Q），但全局快捷键需要系统权限
        # 全局快捷键可以通过 macOS "系统设置 > 键盘 > 快捷键 > 应用快捷键" 配置
        self.quick_chat_item = rumps.MenuItem("快速对话", callback=self.start_quick_chat)
        self.menu = [self.quick_chat_item]
        
        # 尝试注册全局快捷键（需要辅助功能权限）
        # 注意：这需要用户在"系统设置 > 隐私与安全性 > 辅助功能"中授权
        try:
            self._setup_global_hotkey()
        except Exception as e:
            logger.debug(f"全局快捷键设置失败（可能需要权限）: {e}")

        # 使用 rumps.Timer 确保所有 UI 操作在主线程执行（Cocoa/rumps 的硬要求）
        self.menu_timer = rumps.Timer(self.update_menu, config.PL_DAEMON_MENU_UPDATE_INTERVAL)
        self.check_timer = rumps.Timer(self.check_and_notify, config.PL_DAEMON_CHECK_INTERVAL)
        self.menu_timer.start()
        self.check_timer.start()
        logger.info(f"主线程定时器已启动：菜单更新间隔 {config.PL_DAEMON_MENU_UPDATE_INTERVAL} 秒，任务检查间隔 {config.PL_DAEMON_CHECK_INTERVAL} 秒")
        
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
    
    def quit_application(self, _=None):
        """优雅退出应用（自定义“退出”菜单项会调用这里）"""
        logger.info("用户选择退出，开始关闭定时器...")

        try:
            if getattr(self, "menu_timer", None):
                self.menu_timer.stop()
            if getattr(self, "check_timer", None):
                self.check_timer.stop()
            logger.info("主线程定时器已关闭")
        except Exception as e:
            logger.warning(f"关闭定时器失败: {e}")
        
        # 调用父类的退出方法
        rumps.quit_application(None)
    
    def update_menu(self, _=None):
        """更新菜单列表（使用非阻塞锁）"""
        # 使用非阻塞锁，避免 UI 阻塞
        if not self.lock.acquire(blocking=False):
            logger.debug("菜单更新跳过（上一次更新仍在进行中）")
            return
        
        try:
            # 直接重建整个菜单（rumps.Menu 的删除语义在不同版本上不一致）
            # 这样最稳健，也天然避免“旧菜单项引用不可删除/不可哈希”的问题。
            self.menu.clear()
            self.menu.add(self.quick_chat_item)
            self.menu.add(None)  # 分隔符
            
            # 获取最新任务
            try:
                tasks = database.get_all_tasks(status='pending')[:5]
                
                if tasks:
                    for task in tasks:
                        suffix = ""
                        if task.get("due_time"):
                            try:
                                dt = parse_time(task["due_time"])
                                suffix = f" ({dt.strftime('%H:%M')})"
                            except Exception:
                                suffix = ""
                        content = (task.get("content") or "")
                        content_short = content[:30] + ("..." if len(content) > 30 else "")
                        # 使用 lambda 闭包正确绑定 task_id
                        menu_item = rumps.MenuItem(
                            f"{content_short}{suffix}",
                            callback=lambda _, tid=task['id']: self.show_task_dialog(tid)
                        )
                        self.menu.add(menu_item)
                else:
                    no_task_item = rumps.MenuItem("暂无任务")
                    self.menu.add(no_task_item)
            except Exception as e:
                logger.error(f"获取任务列表失败: {e}", exc_info=True)

            # 底部固定区
            self.menu.add(None)
            self.menu.add(rumps.MenuItem("快速输入", callback=lambda _: self.show_quick_input()))
            self.menu.add(rumps.MenuItem("退出", callback=self.quit_application))
                
        finally:
            # 确保锁被释放
            self.lock.release()
    
    def check_and_notify(self, _=None):
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
    
    def _extract_link_or_path(self, content: str) -> Optional[str]:
        """
        从任务内容中提取链接或文件路径
        
        Args:
            content: 任务内容
        
        Returns:
            链接或文件路径，如果没有则返回 None
        """
        if not content:
            return None
        
        # 检测 HTTP/HTTPS 链接
        url_pattern = r'https?://[^\s]+'
        match = re.search(url_pattern, content)
        if match:
            return match.group(0)
        
        # 检测文件路径（macOS 路径格式：/Users/... 或 ~/...）
        path_pattern = r'(?:/Users/|~/|/Volumes/)[^\s]+'
        match = re.search(path_pattern, content)
        if match:
            return match.group(0)
        
        return None
    
    def show_task_dialog(self, task_id: int) -> Optional[str]:
        """
        显示任务对话框（置顶优化，支持链接/文件路径检测）
        
        Args:
            task_id: 任务 ID
        
        Returns:
            "完成", "推迟", "暂时忽略", "立即前往", 或 None（超时/取消）
        """
        try:
            task = database.get_task_by_id(task_id)
            if not task:
                logger.warning(f"任务 {task_id} 不存在")
                return None
            
            # 弹窗信息增强：显示任务内容 + 原始到期时间（MM-DD HH:mm）
            due_display = "(无截止)"
            if task.get("due_time"):
                try:
                    dt = parse_time(task["due_time"])
                    due_display = dt.strftime("%m-%d %H:%M")
                except Exception:
                    due_display = str(task.get("due_time"))

            message = f"任务提醒:\\n{task.get('content')}\\n截止：{due_display}"
            message_escaped = escape_apple_script(message)
            
            # 检测任务内容是否包含链接/文件路径
            link_or_path = self._extract_link_or_path(task.get('content', ''))
            
            # 根据是否有链接决定按钮列表
            if link_or_path:
                buttons = ["完成", "推迟", "立即前往", "暂时忽略"]
            else:
                buttons = ["完成", "推迟", "暂时忽略"]
            
            buttons_str = "{" + ", ".join([f'"{b}"' for b in buttons]) + "}"
            
            # 构建 AppleScript（置顶优化）
            script = f'''
            tell application "System Events" to activate
            try
                set theAnswer to display dialog "{message_escaped}" buttons {buttons_str} default button "暂时忽略"
                return button returned of theAnswer
            on error
                return "暂时忽略"
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
            valid_responses = ["完成", "推迟", "暂时忽略", "立即前往"]
            if response in valid_responses:
                logger.info(f"用户选择: {response} (任务 {task_id})")
                return response
            else:
                logger.warning(f"未知的对话框返回值: {response} (任务 {task_id})")
                return None
                
        except subprocess.TimeoutExpired:
            logger.warning(f"对话框超时，视为'暂时忽略' (任务 {task_id})")
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
                # 提升效率：取消二次确认，直接完成
                database.update_task_status(task_id, 'done')
                logger.info(f"任务 {task_id} 已标记为完成")
                self.update_menu()
                try:
                    rumps.notification("Project Link", "", "任务已完成 ✅")
                except Exception as e:
                    logger.debug(f"发送系统通知失败: {e}")
            
            elif response == "推迟":
                # 推迟任务：弹出输入框询问推迟分钟数（默认 30，防呆）
                minutes = self.ask_postpone_minutes(default_minutes=30)
                if minutes is None:
                    return
                if minutes <= 0:
                    # <=0：视为“暂时忽略”
                    database.update_task_notification_time(task_id)
                    logger.info(f"任务 {task_id} 暂时忽略（由推迟输入 <=0 触发）")
                    return
                minutes = min(minutes, 4320)
                if self.postpone_task(task_id, minutes=minutes):
                    logger.info(f"任务 {task_id} 已推迟 {minutes} 分钟")
                    self.update_menu()
                else:
                    logger.warning(f"推迟任务 {task_id} 失败")
            
            elif response == "暂时忽略":
                # “暂时忽略”的语义：仅更新 last_notified_at（触发冷却），不改 due_time / 不改 status
                database.update_task_notification_time(task_id)
                logger.info(f"任务 {task_id} 暂时忽略（仅更新 last_notified_at）")
            
            elif response == "立即前往":
                # 一键跳转：打开链接或文件
                task = database.get_task_by_id(task_id)
                if task:
                    link_or_path = self._extract_link_or_path(task.get('content', ''))
                    if link_or_path:
                        try:
                            # 使用 macOS 的 open 命令打开链接或文件
                            subprocess.run(["open", link_or_path], check=False, timeout=5)
                            logger.info(f"已打开链接/文件: {link_or_path} (任务 {task_id})")
                            # 打开后更新通知时间（视为"已处理"）
                            database.update_task_notification_time(task_id)
                        except Exception as e:
                            logger.error(f"打开链接/文件失败: {e} (任务 {task_id})")
                            rumps.alert("错误", f"无法打开: {link_or_path}")
                    else:
                        logger.warning(f"任务 {task_id} 内容中未找到链接或文件路径")
            
            else:
                logger.warning(f"未知的对话框响应: {response} (任务 {task_id})")
                
        except Exception as e:
            logger.error(f"处理对话框响应失败 (任务 {task_id}): {e}", exc_info=True)
    
    def ask_postpone_minutes(self, default_minutes: int = 30) -> Optional[int]:
        """
        弹出输入框询问“推迟多少分钟？”
        防呆策略：
        - 空/非数字：默认 30
        - <=0：视为“暂时忽略”
        - 上限在调用方截断为 4320（3天）
        """
        try:
            window = rumps.Window(
                message="推迟多少分钟？",
                title="Project Link",
                default_text=str(default_minutes),
                ok="确定",
                cancel="取消",
            )
            res = window.run()
            if not res.clicked:
                return None
            raw = (res.text or "").strip()
            if not raw:
                return default_minutes
            try:
                return int(raw)
            except ValueError:
                return default_minutes
        except Exception as e:
            logger.error(f"推迟输入框失败: {e}", exc_info=True)
            return default_minutes
    
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
    
    def _setup_global_hotkey(self):
        """
        设置全局快捷键（唤起极简输入框）
        注意：macOS 需要辅助功能权限，如果失败则降级为菜单项快捷键
        """
        # 方案1：使用 pynput（需要安装：pip install pynput）
        # 方案2：使用 macOS 系统快捷键配置（推荐，无需额外权限）
        # 这里先预留接口，实际实现可以通过系统设置配置
        pass
    
    def show_quick_input(self):
        """
        显示极简输入框（通过菜单项或全局快捷键调用）
        使用 rumps.Window 在主线程弹出输入框
        """
        try:
            window = rumps.Window(
                message="快速输入任务：",
                default_text="",
                title="Project Link 快速输入",
                ok="确定",
                cancel="取消",
                dimensions=(400, 50)
            )
            response = window.run()
            
            if response.clicked == 1:  # 点击了"确定"
                user_input = response.text.strip()
                if user_input:
                    # 调用 interpreter 处理输入（需要在后台线程执行，避免阻塞 UI）
                    import threading
                    def process_in_background():
                        try:
                            import interpreter
                            interpreter.process_user_input(user_input)
                        except Exception as e:
                            logger.error(f"处理快速输入失败: {e}", exc_info=True)
                    
                    thread = threading.Thread(target=process_in_background, daemon=True)
                    thread.start()
                    logger.info(f"快速输入已提交: {user_input[:50]}...")
        except Exception as e:
            logger.error(f"显示快速输入框失败: {e}", exc_info=True)
    
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

