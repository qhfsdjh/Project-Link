# Phase 3 核心守护进程实现清单

## 📋 总体目标
实现 macOS 菜单栏守护进程，提供任务提醒、交互式对话框和动态菜单更新功能。

---

## ✅ 第一部分：配置和工具类补完

### 1.1 config.py 扩展

#### 需要添加的函数：
- [ ] **`get_bool(key: str, default: bool) -> bool`**
  - 功能：安全地从环境变量读取布尔值
  - 逻辑：`value.lower() in ('true', '1', 'yes', 'on')`
  - 位置：在 `get_str()` 函数之后

#### 需要添加的配置项：
```python
# ==================== 守护进程配置 ====================
PL_DAEMON_ENABLED = get_bool("PL_DAEMON_ENABLED", True)
PL_DAEMON_CHECK_INTERVAL = get_int("PL_DAEMON_CHECK_INTERVAL", 60)  # 秒
PL_DAEMON_MENU_UPDATE_INTERVAL = get_int("PL_DAEMON_MENU_UPDATE_INTERVAL", 60)  # 秒
PL_DAEMON_POSTPONE_MINUTES = get_int("PL_DAEMON_POSTPONE_MINUTES", 30)  # 分钟
PL_DAEMON_DIALOG_TIMEOUT = get_int("PL_DAEMON_DIALOG_TIMEOUT", 30)  # 秒
PL_DAEMON_NOTIFICATION_ENABLED = get_bool("PL_DAEMON_NOTIFICATION_ENABLED", True)
PL_DAEMON_VOICE_ENABLED = get_bool("PL_DAEMON_VOICE_ENABLED", False)

# 通知频率配置（优先级阶梯）
PL_DAEMON_HIGH_PRIORITY_INTERVAL = get_int("PL_DAEMON_HIGH_PRIORITY_INTERVAL", 15)  # 分钟
PL_DAEMON_HIGH_PRIORITY_MAX_COUNT = get_int("PL_DAEMON_HIGH_PRIORITY_MAX_COUNT", 5)
PL_DAEMON_MEDIUM_PRIORITY_INTERVAL = get_int("PL_DAEMON_MEDIUM_PRIORITY_INTERVAL", 30)  # 分钟
PL_DAEMON_MEDIUM_PRIORITY_MAX_COUNT = get_int("PL_DAEMON_MEDIUM_PRIORITY_MAX_COUNT", 3)
```

---

### 1.2 utils/logger.py 扩展

#### 需要修改的函数：
- [ ] **`setup_logger(name: str, log_file: Optional[str] = None)`**
  - 添加 `log_file` 参数（可选）
  - 如果 `log_file` 为 `None`，使用默认 `"app.log"`
  - 如果 `log_file` 不为 `None`，使用自定义文件名（如 `"daemon.log"`）
  - 文件路径：`logs/{log_file or "app.log"}`

- [ ] **`get_logger(name: str, log_file: Optional[str] = None)`**
  - 添加 `log_file` 参数并传递给 `setup_logger()`

**实现要点：**
```python
log_file_name = log_file or "app.log"
log_file_path = log_dir / log_file_name
file_handler = logging.FileHandler(log_file_path, encoding='utf-8', mode='a')
```

---

### 1.3 utils/helpers.py 创建（新文件）

#### 需要实现的函数：

- [ ] **`parse_time(time_str: str) -> datetime`**
  - 功能：解析 ISO 8601 格式时间字符串（带时区）
  - 处理：
    - 支持 `Z` 后缀（UTC）转换为 `+00:00`
    - 如果时间字符串没有时区信息，使用系统默认时区
    - 返回 `datetime` 对象（带时区信息）
  - 错误处理：抛出 `ValueError` 如果格式不正确

- [ ] **`escape_apple_script(text: str) -> str`**
  - 功能：转义 AppleScript 字符串中的特殊字符
  - 需要转义的字符：
    - `\` → `\\`
    - `"` → `\"`
    - `'` → `\'`（如果使用单引号）
    - 换行符 `\n` → `\\n`
  - 注意：AppleScript 使用单引号时，只需转义单引号本身

**实现要点：**
```python
from datetime import datetime
from typing import Optional

def parse_time(time_str: str) -> datetime:
    """解析 ISO 8601 时间字符串（带时区）"""
    # 处理 Z 后缀
    normalized = time_str.replace('Z', '+00:00')
    dt = datetime.fromisoformat(normalized)
    
    # 如果没有时区信息，使用系统默认时区
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    
    return dt

def escape_apple_script(text: str) -> str:
    """转义 AppleScript 字符串中的特殊字符"""
    # AppleScript 使用单引号，所以需要转义单引号和反斜杠
    return text.replace('\\', '\\\\').replace("'", "\\'")
```

---

## ✅ 第二部分：数据库扩展

### 2.1 database.py 新增函数

- [ ] **`update_task_due_time(task_id: int, new_due_time: Optional[str])`**
  - 功能：更新任务的到期时间
  - 参数：
    - `task_id`: 任务 ID
    - `new_due_time`: 新的到期时间（ISO 8601 格式，可为 `None`）
  - 实现：使用 `UPDATE tasks SET due_time = ? WHERE id = ?`
  - 日志：记录更新操作

- [ ] **`get_task_by_id(task_id: int) -> Optional[Dict[str, Any]]`**
  - 功能：根据 ID 获取单个任务
  - 返回：任务字典（包含所有字段），如果不存在返回 `None`
  - 字段：`id, content, due_time, category, priority, status, created_at, last_notified_at, notification_count`

**实现要点：**
```python
def get_task_by_id(task_id: int) -> Optional[Dict[str, Any]]:
    """根据 ID 获取任务"""
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
```

---

## ✅ 第三部分：核心守护进程 (main_daemon.py)

### 3.1 架构设计

#### 类结构：
```python
class ProjectLinkApp(rumps.App):
    def __init__(self):
        # 继承 rumps.App，设置图标和标题
        # 初始化线程锁
        # 初始化任务菜单项列表
        # 初始化固定菜单项
        # 启动 BackgroundScheduler
        # 添加定时任务
        # 立即更新一次菜单
```

#### 依赖导入：
```python
import rumps
import threading
import subprocess
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import database
import config
from utils.logger import get_logger
from utils.helpers import parse_time, escape_apple_script
```

---

### 3.2 初始化方法 (`__init__`)

- [ ] 调用 `super().__init__("🔗 Project Link")`
- [ ] 初始化 `self.lock = threading.Lock()`
- [ ] 初始化 `self.task_menu_items = []`（存储任务菜单项引用）
- [ ] 创建固定菜单项：`self.quick_chat_item = rumps.MenuItem("快速对话", callback=self.start_quick_chat)`
- [ ] 设置初始菜单：`self.menu = [self.quick_chat_item]`
- [ ] 创建并启动 `BackgroundScheduler`：
  ```python
  self.scheduler = BackgroundScheduler()
  self.scheduler.start()
  ```
- [ ] 添加定时任务：
  - `update_menu`：每 `PL_DAEMON_MENU_UPDATE_INTERVAL` 秒执行一次
  - `check_and_notify`：每 `PL_DAEMON_CHECK_INTERVAL` 秒执行一次
- [ ] 启动时立即调用 `self.update_menu()`
- [ ] 初始化日志：`logger = get_logger("daemon", log_file="daemon.log")`
- [ ] **优雅退出处理**：重写 `rumps.quit_application` 方法（或使用 `@rumps.notifications` 装饰器）
  - 在退出时显式调用 `self.scheduler.shutdown()`
  - 防止后台线程残留

---

### 3.3 动态菜单更新 (`update_menu`)

- [ ] **使用非阻塞锁**：`if not self.lock.acquire(blocking=False): return`
  - 如果数据库查询或菜单刷新异常缓慢，下一次定时任务不会堆积等待锁
  - 直接跳过本次更新，保证 UI 响应流畅
  - 使用 `try-finally` 确保锁被释放
- [ ] 删除旧任务菜单项：
  ```python
  for item in self.task_menu_items:
      if item in self.menu:
          del self.menu[item]
  self.task_menu_items.clear()
  ```
- [ ] 添加分隔符：`self.menu.add(None)`
- [ ] 获取最新任务：`tasks = database.get_all_tasks(status='pending')[:5]`
- [ ] 遍历任务，创建菜单项：
  ```python
  for task in tasks:
      menu_item = rumps.MenuItem(
          f"{task['content'][:30]}...",
          callback=lambda _, tid=task['id']: self.show_task_dialog(tid)
      )
      self.menu.add(menu_item)
      self.task_menu_items.append(menu_item)
  ```
- [ ] 如果没有任务，显示 "暂无任务"
- [ ] 错误处理：使用 `try-except` 包裹，记录日志

**关键点：**
- ✅ 使用 `lambda _, tid=task['id']:` 正确绑定 ID（避免闭包问题）
- ✅ 保存菜单项引用到 `self.task_menu_items` 列表
- ✅ 使用 `None` 作为分隔符
- ✅ **非阻塞锁**：使用 `lock.acquire(blocking=False)` 避免 UI 阻塞

---

### 3.4 任务检查逻辑 (`check_and_notify`)

- [ ] 使用 `with self.lock:` 保护临界区
- [ ] 获取即将到期的任务：`tasks = database.get_upcoming_tasks(hours=1, status='pending')`
- [ ] 遍历任务，对每个任务：
  - 调用 `should_notify_task(task)` 判断是否需要通知
  - 如果需要通知，调用 `show_task_dialog(task['id'])`
  - 如果对话框返回结果，调用 `handle_dialog_response(response, task['id'])`
- [ ] 错误处理：
  - 外层 `try-except`：捕获数据库错误和系统错误
  - 内层 `try-except`：单个任务出错不影响其他任务
  - 记录详细日志（`logger.error(..., exc_info=True)`）

---

### 3.5 通知判断逻辑 (`should_notify_task`)

- [ ] 获取任务属性：`priority`, `notification_count`, `last_notified_at`
- [ ] 获取当前时间：`now = datetime.now().astimezone()`
- [ ] 高优先级（`priority >= 4`）：
  - 如果 `notification_count >= PL_DAEMON_HIGH_PRIORITY_MAX_COUNT`，返回 `False`
  - 如果 `last_notified_at` 存在，计算时间间隔
  - 如果间隔小于 `PL_DAEMON_HIGH_PRIORITY_INTERVAL` 分钟，返回 `False`
  - 否则返回 `True`
- [ ] 中优先级（`priority == 3`）：
  - 如果 `notification_count >= PL_DAEMON_MEDIUM_PRIORITY_MAX_COUNT`，返回 `False`
  - 如果 `last_notified_at` 存在，计算时间间隔
  - 如果间隔小于 `PL_DAEMON_MEDIUM_PRIORITY_INTERVAL` 分钟，返回 `False`
  - 否则返回 `True`
- [ ] 低优先级（`priority <= 2`）：
  - 如果 `notification_count == 0`，返回 `True`
  - 否则返回 `False`
- [ ] 使用 `parse_time()` 解析 `last_notified_at`
- [ ] 时间间隔计算：`(now - last_notified).total_seconds() < interval_seconds`

---

### 3.6 对话框显示 (`show_task_dialog`)

- [ ] 获取任务：`task = database.get_task_by_id(task_id)`
- [ ] 如果任务不存在，记录警告并返回 `None`
- [ ] 转义任务内容：`content = escape_apple_script(task['content'])`
- [ ] 构建 AppleScript（**置顶优化**）：
  ```applescript
  tell application "System Events" to activate
  try
      set theAnswer to display dialog "任务提醒: {content}" buttons {"完成", "推迟 30 分钟", "稍后"} default button "稍后"
      return button returned of theAnswer
  on error
      return "稍后"
  end try
  ```
  - **关键**：首行加入 `tell application "System Events" to activate` 确保弹窗强制出现在屏幕最前方，防止被其他窗口遮挡
- [ ] 执行 `osascript`：
  ```python
  result = subprocess.run(
      ["osascript", "-e", script],
      capture_output=True,
      text=True,
      timeout=config.PL_DAEMON_DIALOG_TIMEOUT
  )
  ```
- [ ] 验证返回值：`valid_responses = ["完成", "推迟 30 分钟", "稍后"]`
- [ ] 错误处理：
  - `subprocess.TimeoutExpired`：记录警告，返回 `None`（视为"稍后"）
  - 其他异常：记录错误，返回 `None`
- [ ] 返回：`"完成"`, `"推迟 30 分钟"`, `"稍后"`, 或 `None`

---

### 3.7 对话框响应处理 (`handle_dialog_response`)

- [ ] 如果 `response == "完成"`：
  - 调用 `confirm_completion(task_id)` 进行二次确认
  - 如果确认，调用 `database.update_task_status(task_id, 'done')`
  - 调用 `database.update_task_notification_time(task_id)`
  - 调用 `self.update_menu()` 更新菜单
- [ ] 如果 `response == "推迟 30 分钟"`：
  - 调用 `postpone_task(task_id, minutes=config.PL_DAEMON_POSTPONE_MINUTES)`
- [ ] 如果 `response == "稍后"`：
  - 只调用 `database.update_task_notification_time(task_id)`（触发冷却计时）
- [ ] 错误处理：使用 `try-except` 包裹，记录日志

---

### 3.8 二次确认对话框 (`confirm_completion`)

- [ ] 获取任务：`task = database.get_task_by_id(task_id)`
- [ ] 如果任务不存在，返回 `False`
- [ ] 转义任务内容：`content = escape_apple_script(task['content'])`
- [ ] 构建 AppleScript（**置顶优化**）：
  ```applescript
  tell application "System Events" to activate
  try
      set theAnswer to display dialog "确认完成任务？\n\n{content}" buttons {"确认", "取消"} default button "确认"
      return button returned of theAnswer
  on error
      return "取消"
  end try
  ```
  - **关键**：首行加入 `tell application "System Events" to activate` 确保弹窗强制出现在屏幕最前方，防止被其他窗口遮挡
- [ ] 执行 `osascript`（同 `show_task_dialog`）
- [ ] 判断返回值：`confirmed = response == "确认"`
- [ ] 错误处理：超时视为取消，返回 `False`
- [ ] 返回：`True`（确认）或 `False`（取消）

---

### 3.9 推迟任务 (`postpone_task`)

- [ ] 获取任务：`task = database.get_task_by_id(task_id)`
- [ ] 如果任务不存在，返回 `False`
- [ ] 如果 `task['due_time']` 为 `None`，记录警告并返回 `False`
- [ ] 解析原到期时间：`original_due = parse_time(task['due_time'])`
- [ ] **计算新到期时间（基于原 due_time 累加）**：
  ```python
  new_due = original_due + timedelta(minutes=minutes)
  ```
  - **关键**：必须基于任务原有的 `due_time` 进行累加，而不是基于 `datetime.now()`
  - 这样能保证多次推迟的逻辑严谨性（每次推迟都是在上一次的基础上累加）
- [ ] 转换为 ISO 格式：`new_due_str = new_due.isoformat()`
- [ ] 更新数据库：`database.update_task_due_time(task_id, new_due_str)`
- [ ] 更新通知时间：`database.update_task_notification_time(task_id)`
- [ ] 错误处理：
  - 时间解析失败：记录警告，返回 `False`
  - 其他异常：记录错误，返回 `False`
- [ ] 返回：`True`（成功）或 `False`（失败）

---

### 3.10 快速对话入口 (`start_quick_chat`)

- [ ] 使用装饰器：`@rumps.clicked("快速对话")`
- [ ] 获取项目路径：`project_path = os.getcwd()`
- [ ] 转义路径（用于 AppleScript）：
  ```python
  escaped_path = project_path.replace("'", "\\'")
  ```
- [ ] 构建 AppleScript：
  ```applescript
  tell application "Terminal"
      do script "cd '{escaped_path}' && python3 interpreter.py"
      activate
  end tell
  ```
- [ ] 执行 `osascript`：
  ```python
  subprocess.run(["osascript", "-e", script], check=False)
  ```
- [ ] 错误处理：记录错误，显示 `rumps.alert()`
- [ ] 记录日志：`logger.info("已打开快速对话窗口")`

---

### 3.11 主函数 (`main`)

- [ ] 初始化数据库：`database.init_db()`
- [ ] 创建应用实例：`app = ProjectLinkApp()`
- [ ] 运行应用：`app.run()`
- [ ] 错误处理：捕获异常，记录日志

**完整结构：**
```python
def main():
    """主函数"""
    try:
        # 初始化数据库
        database.init_db()
        
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
```

---

## ✅ 第四部分：依赖和配置

### 4.1 依赖安装

- [ ] 确认已安装 `rumps`：`pip install rumps`
- [ ] 确认已安装 `APScheduler`：`pip install apscheduler`
- [ ] 确认已安装 `zoneinfo`（Python 3.9+）或 `pytz`（Python < 3.9）

### 4.2 环境变量配置

- [ ] 更新 `.env.example`，添加守护进程相关配置项
- [ ] 确保 `.env` 文件存在（如果用户需要自定义配置）

---

## ✅ 第五部分：测试和验证

### 5.1 功能测试清单

- [ ] 守护进程可以正常启动（菜单栏显示图标）
- [ ] 菜单可以正常显示任务列表（最多 5 个）
- [ ] 点击任务菜单项可以弹出对话框
- [ ] 对话框的三个按钮（完成、推迟、稍后）都能正常工作
- [ ] 点击"完成"后弹出二次确认对话框
- [ ] 点击"推迟 30 分钟"后任务到期时间正确更新
- [ ] 点击"稍后"后任务通知时间更新（触发冷却）
- [ ] 优先级阶梯规则正确执行（高优先级 15 分钟/5 次，中优先级 30 分钟/3 次，低优先级 1 次）
- [ ] 快速对话入口可以正常打开终端并运行 `interpreter.py`
- [ ] 菜单定时更新（每 60 秒）
- [ ] 任务检查定时执行（每 60 秒）
- [ ] 日志文件 `logs/daemon.log` 正常生成和记录

### 5.2 错误处理测试

- [ ] 数据库连接失败时，守护进程不崩溃
- [ ] 单个任务处理失败时，其他任务仍能正常处理
- [ ] 对话框超时时，正确处理（视为"稍后"）
- [ ] 时间解析失败时，记录警告但不崩溃
- [ ] 菜单更新失败时，记录错误但不崩溃

### 5.3 技术细节验证测试

- [ ] **AppleScript 置顶优化**：对话框弹窗时，即使有其他窗口打开，也能强制显示在最前方
- [ ] **菜单刷新非阻塞锁**：在菜单更新过程中，UI 仍然响应，不会因为锁等待而卡顿
- [ ] **优雅退出处理**：点击 "Quit" 后，调度器正确关闭，无后台线程残留（检查进程列表）
- [ ] **推迟逻辑的基准点**：
  - 创建一个任务，到期时间为 "2026-01-20T10:00:00+08:00"
  - 第一次推迟 30 分钟，新时间应为 "2026-01-20T10:30:00+08:00"
  - 第二次推迟 30 分钟，新时间应为 "2026-01-20T11:00:00+08:00"（基于第一次推迟后的时间，而不是当前时间）

---

## 📝 实现顺序建议

1. **第一阶段：工具类和配置**
   - 实现 `config.py` 扩展（`get_bool` + 配置项）
   - 实现 `utils/logger.py` 扩展（支持 `log_file`）
   - 创建 `utils/helpers.py`（`parse_time` + `escape_apple_script`）

2. **第二阶段：数据库扩展**
   - 实现 `database.py` 新增函数（`update_task_due_time` + `get_task_by_id`）

3. **第三阶段：核心守护进程**
   - 创建 `main_daemon.py` 基础结构（`__init__` + `main`）
   - 实现菜单更新（`update_menu`）
   - 实现任务检查（`check_and_notify` + `should_notify_task`）
   - 实现对话框交互（`show_task_dialog` + `handle_dialog_response`）
   - 实现辅助功能（`confirm_completion` + `postpone_task` + `start_quick_chat`）

4. **第四阶段：测试和优化**
   - 功能测试
   - 错误处理测试
   - 性能优化（如果需要）

---

## ⚠️ 关键注意事项

1. **lambda 闭包问题**：必须使用 `lambda _, tid=task['id']:` 而不是 `lambda sender: self.show_task_dialog(task['id'])`

2. **菜单项引用管理**：必须保存菜单项引用到 `self.task_menu_items` 列表，才能正确删除

3. **线程安全**：所有共享资源访问（菜单更新、任务检查）必须使用 `self.lock` 保护

4. **时间处理**：所有时间比较必须在 Python 层完成（使用 `parse_time()` 转换后比较）

5. **错误隔离**：单个任务处理失败不能影响其他任务，也不能导致守护进程崩溃

6. **日志记录**：所有关键操作和错误都要记录日志，使用 `exc_info=True` 记录异常堆栈

7. **AppleScript 置顶优化**：
   - 在 `show_task_dialog` 和 `confirm_completion` 的 AppleScript 脚本首行加入 `tell application "System Events" to activate`
   - 确保弹窗强制出现在屏幕最前方，防止被其他窗口遮挡

8. **菜单刷新非阻塞锁**：
   - 在 `update_menu` 中使用 `if not self.lock.acquire(blocking=False): return`
   - 如果数据库查询或菜单刷新异常缓慢，下一次定时任务不会堆积等待锁
   - 直接跳过本次更新，保证 UI 响应流畅
   - 使用 `try-finally` 确保锁被释放

9. **优雅退出处理**：
   - 在 `ProjectLinkApp` 中添加退出处理逻辑（重写 `rumps.quit_application` 或使用信号处理）
   - 确保在用户选择 "Quit" 或程序被关闭时，显式调用 `self.scheduler.shutdown()`
   - 防止后台线程残留

10. **推迟逻辑的基准点**：
    - 在 `postpone_task` 中，计算新时间必须基于任务原有的 `due_time` 进行累加
    - 使用 `original_due + timedelta(minutes=minutes)`，而不是基于 `datetime.now()`
    - 这样能保证多次推迟的逻辑严谨性（每次推迟都是在上一次的基础上累加）

---

## 🎯 完成标准

- ✅ 所有函数都已实现
- ✅ 所有配置项都已添加
- ✅ 所有错误处理都已实现
- ✅ 所有日志记录都已添加
- ✅ 功能测试全部通过
- ✅ 错误处理测试全部通过
- ✅ 代码已提交到 Git 仓库

---

**最后更新：** 2026-01-XX
**状态：** 待实现

