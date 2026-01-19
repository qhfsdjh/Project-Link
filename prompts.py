"""
Project Link - 提示词管理模块
集中管理所有 AI 提示词，便于维护和修改
"""

from datetime import datetime, timedelta
from typing import Dict, Optional

# 尝试导入本地个性化配置（如果存在）
try:
    import prompts_local  # type: ignore
    HAS_LOCAL_CONFIG = True
except ImportError:
    # prompts_local.py 不存在，使用默认值
    prompts_local = None  # type: ignore
    HAS_LOCAL_CONFIG = False


def _get_local_config(key: str, default: any = None) -> any:
    """
    安全地获取本地配置值
    
    Args:
        key: 配置键名
        default: 默认值
    
    Returns:
        配置值或默认值
    """
    if not HAS_LOCAL_CONFIG:
        return default
    return getattr(prompts_local, key, default)


def get_system_prompt() -> str:
    """
    获取系统提示词，自动注入当前时间信息
    
    Returns:
        完整的系统提示词字符串
    """
    # 获取当前时间信息
    now = datetime.now()
    current_time = now.strftime('%Y-%m-%d %H:%M:%S')
    current_date = now.strftime('%Y-%m-%d')
    
    # 周几转换（中文格式）
    weekday_map = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
    weekday_cn = f"周{weekday_map[now.isoweekday()]}"
    
    # 计算明天的日期（用于示例）
    tomorrow = (now + timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 获取本地个性化配置（如果存在）
    user_name = _get_local_config('USER_NAME', None)
    user_context = _get_local_config('USER_CONTEXT', None)
    personalized_instructions = _get_local_config('PERSONALIZED_INSTRUCTIONS', '')
    learned_habits = _get_local_config('LEARNED_HABITS', [])
    
    # 构建个性化信息部分
    personal_info_section = ""
    if user_name or user_context:
        personal_info_section = "\n【主人画像】\n"
        if user_name:
            personal_info_section += f"姓名：{user_name}\n"
        if user_context:
            personal_info_section += f"背景：{user_context}\n"
    
    # 构建个性化指令部分
    personalized_section = ""
    if personalized_instructions and personalized_instructions.strip():
        personalized_section = f"\n【个性化指令】\n{personalized_instructions}\n"
    
    # 构建学习到的习惯部分
    habits_section = ""
    if learned_habits and isinstance(learned_habits, list) and len(learned_habits) > 0:
        habits_section = "\n【已学习到的习惯】\n"
        for habit in learned_habits:
            if isinstance(habit, dict):
                key = habit.get('key', '')
                value = habit.get('value', '')
                confidence = habit.get('confidence', 0)
                if key and value:
                    habits_section += f"- {key}: {value} (置信度: {confidence:.0%})\n"
        habits_section += "\n"
    
    return f"""
你是一个私密 AI 助理的意图解析器。
当前时间是：{current_time}，今天是 {current_date}（{weekday_cn}）。
{personal_info_section}{personalized_section}{habits_section}你的任务是将用户的输入转化为数据库操作指令。
输出必须是严格的 JSON 格式，不得包含任何解释文字。

重要：时间处理规则
- 当用户说"明天"、"后天"、"下周一"等相对时间时，请基于当前日期 {current_date} 计算具体日期
- 必须输出完整的 ISO 8601 格式：YYYY-MM-DDTHH:MM:SS（例如：{tomorrow}T15:00:00）
- 绝对不允许使用占位符（如 XX）或无效格式
- 如果无法确定具体日期或时间，请使用 null
- 时间部分如果用户没有明确说明，使用 00:00:00 作为默认时间

JSON 格式要求：
{{
    "action": "add_task" | "add_preference" | "record_memory" | "query_tasks" | "update_task" | "cancel_task" | "chat",
    "data": {{
        // 如果是 add_task: 
        // {{ "content": "任务内容", "due_time": "ISO格式时间字符串或null", "priority": 1-5的整数, "category": "分类(可选)" }}
        // 如果是 add_preference: 
        // {{ "key": "习惯键名", "value": "习惯值", "source": "AI推断" }}
        // 如果是 record_memory: 
        // {{ "content": "原始信息", "sentiment": "positive/neutral/negative", "tag": "分类标签" }}
        // 如果是 query_tasks:
        // {{ "time_range": "today/tomorrow/upcoming/overdue/all", "status": "pending/done/all", "limit": 数量(可选) }}
        // 如果是 update_task:
        // {{ "task_id": 任务ID(必须是整数，优先从候选任务中选择), "content": "新内容(可选)", "due_time": "新的ISO时间或null(可选)", "priority": 1-5(可选), "category": "分类(可选)" }}
        // 如果是 cancel_task:
        // {{ "task_id": 任务ID(必须是整数，优先从候选任务中选择) }}
        // 如果是 chat:
        // {{ "reply": "给用户的简短回复（非任务/非修改/非查询）" }}
    }}
}}

判断规则（重要：区分查询意图和添加意图）：
- query_tasks: 用户询问任务、查询待办事项（如"我明天有什么任务？"、"今天要做什么？"、"有什么过期任务？"）
- add_task: 用户明确提到要做什么任务、待办事项、计划（如"明天下午3点要开会"）
- update_task: 用户明确要修改已有任务（如"改一下时间"、"把刚才那个改成..."、"刚才打错了"）
- cancel_task: 用户明确要取消已有任务（如"取消上一个"、"把刚才那个取消掉"）
- chat: 用户在闲聊/情绪表达/泛聊，不要求存任务/改任务/查任务（如"你好"、"我有点累"且没有可结构化记忆需求时）
- add_preference: 用户表达习惯、偏好、工作方式（如"我通常9点上班"）
- record_memory: 其他所有情况，包括闲聊、状态更新、想法记录

查询意图的关键词：询问、查询、有什么、哪些、看看、显示、列出
添加意图的关键词：要、需要、计划、安排、提醒

示例（基于当前时间 {current_date}）：
用户输入: "明天下午3点要开会"
输出: {{"action": "add_task", "data": {{"content": "开会", "due_time": "{tomorrow}T15:00:00", "priority": 3}}}}

用户输入: "我明天有什么任务？"
输出: {{"action": "query_tasks", "data": {{"time_range": "tomorrow", "status": "pending"}}}}

用户输入: "今天要做什么？"
输出: {{"action": "query_tasks", "data": {{"time_range": "today", "status": "pending"}}}}

用户输入: "有什么过期任务吗？"
输出: {{"action": "query_tasks", "data": {{"time_range": "overdue", "status": "pending"}}}}

用户输入: "刚才打错了，把上一个任务标题改成写周报"
输出: {{"action": "update_task", "data": {{"task_id": 9, "content": "写周报"}}}}

用户输入: "取消上一个任务"
输出: {{"action": "cancel_task", "data": {{"task_id": 9}}}}

用户输入: "我习惯早上喝咖啡"
输出: {{"action": "add_preference", "data": {{"key": "morning_routine", "value": "喝咖啡", "source": "AI推断"}}}}

用户输入: "今天心情不错"
输出: {{"action": "record_memory", "data": {{"content": "今天心情不错", "sentiment": "positive", "tag": "life"}}}}
"""


def get_user_prompt(user_input: str, recent_tasks: Optional[list] = None) -> str:
    """
    获取用户提示词模板
    
    Args:
        user_input: 用户输入的自然语言
    
    Returns:
        格式化的用户提示词
    """
    context_section = ""
    if recent_tasks and isinstance(recent_tasks, list) and len(recent_tasks) > 0:
        lines = []
        for t in recent_tasks:
            try:
                tid = t.get("id")
                content = (t.get("content") or "").replace("\n", " ")
                due_time = t.get("due_time")
                created_at = t.get("created_at")
                lines.append(f"- ID {tid}: {content} | due_time={due_time} | created_at={created_at}")
            except Exception:
                continue
        if lines:
            context_section = (
                "\n【最近待办候选（按创建时间倒序，优先用于“上一个/刚才那个”）】\n"
                + "\n".join(lines)
                + "\n\n规则：如果 action 是 update_task 或 cancel_task，你必须从这些候选中选择 task_id（除非用户明确提供了任务ID）。\n"
            )

    return f"{context_section}用户输入: \"{user_input}\"\n请输出 JSON:"

