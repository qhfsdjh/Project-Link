"""
Project Link - 意图解析翻译官
将用户的自然语言输入转换为数据库操作指令
"""

import json
import os
import sys
from typing import Optional, Dict, Any, Literal, Tuple, List
from datetime import datetime, timedelta

import database  # 导入数据库模块
import prompts  # 导入提示词模块
import config  # 导入配置模块
from utils.logger import get_logger  # 导入日志模块
from utils.helpers import parse_time, parse_offset  # 导入时间解析工具

# 初始化日志记录器
logger = get_logger("interpreter")

try:
    import ollama
except ImportError:
    logger.error("未安装 ollama 库，请运行: pip install ollama")
    print("❌ 错误: 未安装 ollama 库")
    print("   请运行: pip install ollama")
    sys.exit(1)

# ==================== 配置 ====================
MODEL_NAME = config.OLLAMA_MODEL
TIMEOUT = config.OLLAMA_TIMEOUT
MAX_RETRIES = config.PL_AI_MAX_RETRIES
RETRY_DELAY = config.PL_AI_RETRY_DELAY

TrackType = Literal["daily", "meeting"]


# ==================== 对话历史缓存（最近3轮）====================
# 用于上下文感知和指代消解（"刚才那个"、"它"、"改一下"等）
_conversation_history = []

def add_to_history(user_input: str, ai_response: Optional[str] = None):
    """
    添加对话到历史记录（最多保留最近5轮）
    
    Args:
        user_input: 用户输入
        ai_response: AI 回复（可选，用于完整对话记录）
    """
    global _conversation_history
    entry = {"user": user_input, "ai": ai_response}
    _conversation_history.append(entry)
    # 只保留最近3轮（强制提速：避免 token 堆积）
    if len(_conversation_history) > 3:
        _conversation_history = _conversation_history[-3:]

def get_recent_history(limit: int = 3) -> list:
    """
    获取最近N轮对话历史
    
    Args:
        limit: 返回的轮数（默认5）
    
    Returns:
        对话历史列表（从旧到新）
    """
    return _conversation_history[-limit:]

def clear_history():
    """清空对话历史"""
    global _conversation_history
    _conversation_history = []


def _detect_track(user_input: str) -> TrackType:
    """
    根据长度与关键词粗分两类轨道：
    - daily  : 日常碎碎念 / 简短提醒
    - meeting: 会议纪要 / 长文总结
    """
    text = (user_input or "").strip()
    length = len(text)

    # 明确会议/总结类关键词
    meeting_keywords = [
        "会议纪要",
        "会议记录",
        "总结一下",
        "帮我总结",
        "帮我梳理",
        "复盘一下",
        "行动项",
        "待办清单",
        "meeting",
        "记录一下刚才的会议",
    ]

    # 明确日常提醒类关键词
    daily_keywords = [
        "提醒我",
        "记下",
        "帮我记",
        "待会",
        "一会儿",
        "稍后提醒",
        "明天",
        "后天",
        "下周",
        "喝水",
        "休息一下",
    ]

    # 长文本优先视为会议轨道
    if length > 200:
        for kw in meeting_keywords:
            if kw in text:
                return "meeting"
        # 超长但没明显会议词，也按 meeting 处理，后续再细分
        return "meeting"

    # 短文本且包含日常提醒关键词，优先走 daily 快通道
    for kw in daily_keywords:
        if kw in text:
            return "daily"

    # 包含会议/总结词但不算特别长，也归入 meeting 轨道
    for kw in meeting_keywords:
        if kw in text:
            return "meeting"

    # 默认视为 daily，保证日常体验优先丝滑
    return "daily"

# ==================== 工具函数 ====================

def parse_due_time(due_time_str: Optional[str]) -> Optional[str]:
    """
    解析并验证时间字符串
    将 "None"、"null"、空字符串转换为 None
    验证 ISO 格式的有效性
    
    Args:
        due_time_str: 时间字符串（可能是 "None"、ISO 格式或 None）
    
    Returns:
        有效的 ISO 格式字符串或 None
    """
    if not due_time_str:
        return None
    
    # 处理字符串形式的 None
    if isinstance(due_time_str, str):
        if due_time_str.lower() in ("none", "null", ""):
            return None
        
        # 验证 ISO 格式（简单验证）
        try:
            # 尝试解析 ISO 格式
            datetime.fromisoformat(due_time_str.replace('Z', '+00:00'))
            return due_time_str
        except (ValueError, AttributeError):
            # 如果格式无效，返回 None（或可以抛出错误）
            logger.warning(f"时间格式无效 '{due_time_str}'，已忽略")
            print(f"⚠️  警告: 时间格式无效 '{due_time_str}'，已忽略")
            return None
    
    return None


def _resolve_time_to_due_time_iso(
    time_obj: Any,
    *,
    now: datetime,
) -> Optional[str]:
    """
    将 LLM 输出的 time 对象解析成最终 due_time（ISO 字符串，带本地时区）。

    约定：
    - time.type = "none"  -> None
    - time.type = "absolute" -> 使用 time.iso
    - time.type = "relative" -> now + parse_offset(time.offset)，可选 time.at_time="HH:MM"
    """
    if time_obj is None:
        return None

    # 兼容旧字段：如果 LLM 仍输出 due_time（旧 prompt/旧模型缓存），继续走旧逻辑
    if isinstance(time_obj, str):
        return parse_due_time(time_obj)

    if not isinstance(time_obj, dict):
        raise ValueError(f"time 必须是对象/字符串/None，收到: {type(time_obj)}")

    ttype = (time_obj.get("type") or "").strip().lower()
    if ttype in ("", "none", "null"):
        return None

    if ttype == "absolute":
        iso = time_obj.get("iso")
        iso_norm = parse_due_time(iso)
        if iso is not None and iso_norm is None:
            raise ValueError(f"time.iso 不是合法 ISO: {iso!r}")
        return iso_norm

    if ttype == "relative":
        offset = time_obj.get("offset")
        if not isinstance(offset, str) or not offset.strip():
            raise ValueError("time.offset 不能为空（relative 必填）")

        delta = parse_offset(offset)
        target = (now + delta).astimezone()

        at_time = time_obj.get("at_time")
        if isinstance(at_time, str) and at_time.strip():
            parts = at_time.strip().split(":")
            if len(parts) != 2:
                raise ValueError(f"time.at_time 格式应为 HH:MM，收到: {at_time!r}")
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError(f"time.at_time 不合法: {at_time!r}")
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)

        return target.isoformat()

    raise ValueError(f"未知 time.type: {ttype!r}")


def _ollama_chat_stream_text(**kwargs) -> str:
    """
    使用 Ollama stream 模式获取完整文本，同时在日志中记录原始流（截断）。
    返回聚合后的 message content。
    """
    chunks = []
    raw_preview = []
    for part in ollama.chat(stream=True, **kwargs):
        # 兼容 dict/对象两种形态
        text = ""
        if isinstance(part, dict):
            text = (part.get("message") or {}).get("content") or ""
        else:
            try:
                text = part.message.content  # type: ignore[attr-defined]
            except Exception:
                text = ""

        if text:
            chunks.append(text)
            if len("".join(raw_preview)) < 500:
                raw_preview.append(text)

    preview = "".join(raw_preview)
    if preview:
        logger.info(f"[llm_stream_preview] {preview[:500]}")

    return "".join(chunks)


def _is_backfill_past_intent(user_text: str) -> bool:
    """
    判断用户是否明确在“补记/回忆过去”的意图（允许 due_time 早于当前时间）
    """
    if not user_text:
        return False
    keywords = ("补记", "补录", "回忆", "回顾", "之前", "昨天", "上周", "上个月", "前天", "刚刚完成", "刚刚做了")
    return any(k in user_text for k in keywords)


def _is_time_correction_intent(user_text: str) -> bool:
    """
    判断用户是否在纠正时间（应该优先更新最近一个 pending 任务）
    """
    if not user_text:
        return False
    keywords = ("时间不对", "不对", "错了", "现在是", "现在已经", "怎么是", "穿越", "翻车")
    return any(k in user_text for k in keywords)


def _validate_due_time_not_past(due_time: Optional[str], now: datetime, user_text: str) -> bool:
    """
    硬性校验：除非用户明确补记/过去，否则 due_time 必须晚于 now
    """
    if not due_time:
        return True
    if _is_backfill_past_intent(user_text):
        return True
    try:
        dt = parse_time(due_time)
        # 允许 30 秒的容错（模型可能只精确到分钟）
        return dt >= (now - timedelta(seconds=30))
    except Exception:
        # 无法解析时，交给上层当作无效处理
        return False


def _extract_first_json(text: str) -> Optional[Dict[str, Any]]:
    """
    从 LLM 输出中提取第一段 JSON 对象并解析。
    兼容“JSON 后面夹带解释文字”的情况。
    """
    if not text:
        return None
    # 先快路径：整段就是 JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    # 慢路径：括号配对截取首个 JSON 对象
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == "\"":
                in_str = False
            continue
        else:
            if ch == "\"":
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    snippet = text[start:i+1]
                    try:
                        obj = json.loads(snippet)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        return None
    return None


def _pick_recent_pending(limit: int = 5) -> List[Dict[str, Any]]:
    """获取最近 pending 候选（created_at 倒序）。"""
    try:
        return database.get_recent_tasks(status='pending', limit=limit)
    except Exception:
        return []


def _normalize_task_id(task_id: Optional[Any], candidates: List[Dict[str, Any]]) -> Optional[int]:
    """
    纠偏 task_id：如果不在候选列表里，返回候选第一条（最近 pending）。
    """
    if not candidates:
        return None
    ids = {t.get("id") for t in candidates}
    try:
        tid = int(task_id) if task_id is not None else None
    except (ValueError, TypeError):
        tid = None
    if tid in ids:
        return tid
    return candidates[0].get("id")


def _is_completion_intent(user_text: str) -> bool:
    """
    判断用户是否在表达“已完成/已做完/已喝了”等完成意图。
    """
    if not user_text:
        return False
    completion_keywords = ("我已经", "我已", "搞定了", "做完了", "完成了", "喝了", "喝完了", "已经处理", "处理好了")
    cancel_keywords = ("取消", "不要了", "算了", "撤销")
    return any(k in user_text for k in completion_keywords) and not any(k in user_text for k in cancel_keywords)


def validate_priority(priority: Any) -> int:
    """
    验证并规范化优先级
    
    Args:
        priority: 优先级值（可能是字符串、整数或 None）
    
    Returns:
        有效的优先级 (1-5)，默认 3
    """
    if priority is None:
        return 3
    
    try:
        priority = int(priority)
        if 1 <= priority <= 5:
            return priority
        else:
            logger.warning(f"优先级 {priority} 超出范围 (1-5)，已使用默认值 3")
            print(f"⚠️  警告: 优先级 {priority} 超出范围 (1-5)，已使用默认值 3")
            return 3
    except (ValueError, TypeError):
        logger.warning(f"优先级格式无效 '{priority}'，已使用默认值 3")
        print(f"⚠️  警告: 优先级格式无效 '{priority}'，已使用默认值 3")
        return 3


def validate_sentiment(sentiment: Any) -> Literal["positive", "neutral", "negative"]:
    """
    验证情感倾向
    
    Args:
        sentiment: 情感值
    
    Returns:
        有效的情感值
    """
    valid_sentiments = ("positive", "neutral", "negative")
    if sentiment in valid_sentiments:
        return sentiment
    return "neutral"  # 默认值


def validate_action_data(result: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    验证 AI 返回的 JSON 结构
    
    Args:
        result: AI 返回的 JSON 对象
    
    Returns:
        (action, data) 元组，如果验证失败则返回 (None, None)
    """
    if not result:
        return None, None
    
    # 检查必需的键
    if "action" not in result or "data" not in result:
        logger.error(f"JSON 结构不完整: 缺少 'action' 或 'data' 键，收到的数据: {result}")
        print(f"❌ JSON 结构不完整: 缺少 'action' 或 'data' 键")
        print(f"   收到的数据: {result}")
        return None, None
    
    action = result["action"]
    data = result["data"]
    
    # 验证 action 类型
    if not isinstance(action, str):
        logger.error(f"'action' 必须是字符串，收到: {type(action)}")
        print(f"❌ 'action' 必须是字符串，收到: {type(action)}")
        return None, None
    
    # 验证 data 类型
    if not isinstance(data, dict):
        logger.error(f"'data' 必须是字典，收到: {type(data)}")
        print(f"❌ 'data' 必须是字典，收到: {type(data)}")
        return None, None
    
    # 验证 action 值
    valid_actions = ("add_task", "add_preference", "record_memory", "query_tasks", "update_task", "cancel_task", "chat")
    if action not in valid_actions:
        logger.error(f"未知的 action: '{action}'，有效值: {valid_actions}")
        print(f"❌ 未知的 action: '{action}'，有效值: {valid_actions}")
        return None, None
    
    return action, data


# ==================== AI 调用函数 ====================

def get_ai_interpretation(user_input: str) -> Optional[Dict[str, Any]]:
    """
    调用 Ollama 让 Qwen 解析用户的真实意图（支持上下文感知）
    
    Args:
        user_input: 用户输入的自然语言
    
    Returns:
        解析后的 JSON 对象，如果失败则返回 None
    """
    # 统一使用本地时区（避免 UTC/本地混用）
    now = datetime.now().astimezone()
    current_time_iso = now.isoformat()

    # 从 prompts 模块获取提示词（自动注入当前时间信息）
    system_prompt = prompts.get_system_prompt()
    # 调试增强：记录注入给 LLM 的 current_time
    logger.info(f"[time_anchor] current_time_iso={current_time_iso}")
    
    # 启发式两段式：把最近 N 条 pending 任务作为候选上下文塞进 prompt
    recent_tasks = []
    try:
        recent_tasks = database.get_recent_tasks(status='pending', limit=3)
    except Exception as e:
        logger.warning(f"获取最近任务候选失败: {e}")
    
    # 获取最近3轮对话历史（用于上下文感知和指代消解）
    conversation_history = get_recent_history(limit=3)
    
    base_user_prompt = prompts.get_user_prompt(
        user_input,
        recent_tasks=recent_tasks,
        conversation_history=conversation_history
    )

    # 强制时间校验 + 自动纠错重试（最多 2 次）
    last_error_hint = ""
    for attempt in range(2):
        user_prompt = base_user_prompt
        if last_error_hint:
            user_prompt = (
                base_user_prompt
                + "\n\n【系统校验失败】\n"
                + last_error_hint
                + f"\n请基于 current_time_iso={current_time_iso} 重新计算并仅输出 JSON。\n"
            )

        try:
            response_text = _ollama_chat_stream_text(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                format="json",
                options={
                    "temperature": config.PL_AI_TEMPERATURE,
                },
            )

            # 调试增强：记录聚合后的 LLM 原始返回（截断）
            logger.info(f"[llm_raw] {response_text[:500]}")

            if not response_text:
                last_error_hint = "你返回了空响应。"
                continue

            parsed = _extract_first_json(response_text)
            if not parsed:
                last_error_hint = "JSON 解析失败：你必须输出严格 JSON，且不要在 JSON 后追加解释文字。"
                continue

            action = parsed.get("action")
            data = parsed.get("data", {}) if isinstance(parsed.get("data"), dict) else {}

            # 时间统一校验：LLM 只输出 time（relative/absolute/none），由 Python 计算 due_time；
            # 兼容旧字段 due_time（防止旧模型/旧缓存输出）。
            if action in ("add_task", "update_task") and isinstance(data, dict):
                due_time_candidate = None

                if "time" in data:
                    try:
                        due_time_candidate = _resolve_time_to_due_time_iso(data.get("time"), now=now)
                    except Exception as e:
                        last_error_hint = f"time 解析失败：{e}。请修正 time 对象并仅输出 JSON。"
                        continue
                elif "due_time" in data:
                    due_time_candidate = parse_due_time(data.get("due_time"))

                if due_time_candidate and not _validate_due_time_not_past(due_time_candidate, now, user_input):
                    last_error_hint = (
                        f"你输出的时间计算结果 due_time={due_time_candidate} 早于当前时间 current_time_iso={current_time_iso}。"
                        "除非用户明确说是补记/过去，否则请输出一个正向 offset（例如 +1m/+2h/+1d）或正确的绝对时间。"
                    )
                    continue

                # 将最终 due_time 写回（执行层直接用这个字段），并移除 time（避免后续分支混乱）
                if "time" in data:
                    data.pop("time", None)
                if "due_time" in data:
                    # 统一规范化（允许 None）
                    data["due_time"] = due_time_candidate
                else:
                    data["due_time"] = due_time_candidate
                parsed["data"] = data

            logger.debug(f"AI 解析成功，返回 JSON: {parsed}")
            return parsed

        except Exception as e:
            logger.error(f"调用 Ollama API 失败: {e}", exc_info=True)
            last_error_hint = f"调用失败：{e}"
            continue

    return None


# ==================== 主逻辑 ====================

def process_user_input(user_text: str) -> bool:
    """
    处理用户输入的主函数
    
    Args:
        user_text: 用户输入
    
    Returns:
        是否处理成功
    """
    # 轨道检测：区分日常快通道 / 会议长文本
    track: TrackType = _detect_track(user_text)
    logger.info(f"[track] current_track={track}")

    # 1. 先记录原始对话（保存每一句记忆）
    # 注意：这里只记录原始输入，不记录 sentiment 和 tag
    # 如果后续解析成功且是 record_memory，会更新该记录而不是新增
    raw_record_id = None
    try:
        raw_record_id = database.record_interaction(
            content=user_text,
            sentiment="neutral",  # 原始记录使用中性
            tag=None  # 原始记录不分类
        )
        logger.debug(f"已记录原始输入，record_id: {raw_record_id}")
    except Exception as e:
        logger.warning(f"记录原始输入失败: {e}")
        print(f"⚠️  警告: 记录原始输入失败: {e}")
        # 继续执行，不因为记录失败而中断
    
    # 2. 让 AI 解析意图（支持上下文感知）
    logger.info(f"开始解析用户输入: {user_text[:50]}... (track={track})")

    # 完成意图兜底：像“我已经喝了/做完了”这种，优先标记最近 pending 为 done（不走 cancel）
    if _is_completion_intent(user_text):
        candidates = _pick_recent_pending(limit=5)
        if candidates:
            tid = candidates[0].get("id")
            task = database.get_task_by_id(tid) if tid is not None else None
            if task:
                try:
                    database.update_task_status(int(tid), "done")
                    msg = f"好哒，已经帮你把「{task.get('content')}」标记为完成了"
                    print(msg)
                    add_to_history(user_text, msg)
                    return True
                except Exception as e:
                    logger.warning(f"完成任务失败: {e}")

    # 纠错意图识别升级：当用户说“时间不对/错了/现在是…”时，优先更新最近一个 pending 任务
    user_text_for_ai = user_text
    if _is_time_correction_intent(user_text):
        try:
            candidates = database.get_recent_tasks(status='pending', limit=1)
        except Exception:
            candidates = []
        if candidates:
            last_task = candidates[0]
            user_text_for_ai = (
                f"用户反馈上一个任务的时间不对。请对任务ID {last_task['id']} 执行 update_task，"
                f"并基于当前时间重新计算正确的 due_time。用户原话：{user_text}"
            )

    result = get_ai_interpretation(user_text_for_ai)
    
    if not result:
        logger.warning("AI 解析失败，返回 None")
        print("嗯，我没太理解，能换个说法吗？")
        return False
    
    # 3. 验证 JSON 结构
    action, data = validate_action_data(result)
    if not action or not data:
        logger.error("解析结果格式不正确")
        print("抱歉，解析出错了，能再说一遍吗？")
        return False
    
    logger.info(f"AI 解析成功，action: {action}")
    
    # 记录到对话历史（用于下一轮的上下文感知）
    add_to_history(user_text)
    
    # 4. 根据意图执行数据库操作
    try:
        if action == "add_task":
            # 验证和规范化数据
            content = data.get("content")
            if not content:
                print("任务内容不能为空哦")
                return False
            
            due_time = parse_due_time(data.get("due_time"))
            priority = validate_priority(data.get("priority"))
            category = data.get("category")  # 可选
            
            # 执行数据库操作
            task_id = database.add_task(
                content=content,
                due_time=due_time,
                category=category,
                priority=priority
            )
            logger.info(f"任务已添加: task_id={task_id}, content={content}, due_time={due_time}, priority={priority}")
            
            # 自然口语风格回复
            if due_time:
                try:
                    dt = parse_time(due_time)
                    time_str = dt.strftime('%m月%d日 %H:%M')
                    print(f"好哒，已经记下了：{content}，截止时间是 {time_str}")
                except:
                    print(f"好哒，已经记下了：{content}")
            else:
                print(f"好哒，已经记下了：{content}")
            
            # 记录到对话历史
            add_to_history(user_text, f"已添加任务：{content}")
            return True
            
        elif action == "add_preference":
            # 验证数据
            key = data.get("key")
            value = data.get("value")
            source = data.get("source", "AI推断")
            
            if not key or not value:
                print("偏好键名和值不能为空哦")
                return False
            
            # 验证 source
            if source not in ("用户直说", "AI推断"):
                source = "AI推断"
            
            # 执行数据库操作（从配置读取 boost 值）
            database.update_habit(
                key=key,
                value=value,
                source=source,
                boost=config.PL_HABIT_BOOST  # 从配置读取
            )
            logger.info(f"习惯已更新: key={key}, value={value}, source={source}, boost={config.PL_HABIT_BOOST}")
            print(f"好的，我记住了：{key} -> {value}")
            add_to_history(user_text, f"已学习习惯：{key} -> {value}")
            return True
            
        elif action == "record_memory":
            # 验证数据
            sentiment = validate_sentiment(data.get("sentiment"))
            tag = data.get("tag")
            
            # 执行数据库操作
            # 更新原始记录，而不是新增（修复重复记录问题）
            if raw_record_id:
                try:
                    database.update_interaction(raw_record_id, sentiment, tag)
                    logger.info(f"记忆已更新: record_id={raw_record_id}, sentiment={sentiment}, tag={tag}")
                    print("好的，我记住了")
                    add_to_history(user_text, "已记录记忆")
                except Exception as e:
                    logger.warning(f"更新记忆记录失败: {e}")
                    print(f"⚠️  警告: 更新记忆记录失败: {e}")
                    # 如果更新失败，至少原始记录已保存
            else:
                # 如果原始记录失败，这里尝试新增（降级处理）
                try:
                    database.record_interaction(user_text, sentiment, tag)
                    logger.info(f"记忆已记录: sentiment={sentiment}, tag={tag}")
                    print("好的，我记住了")
                    add_to_history(user_text, "已记录记忆")
                except Exception as e:
                    logger.warning(f"记录记忆失败: {e}")
                    print(f"⚠️  警告: 记录记忆失败: {e}")
            return True
        
        elif action == "update_task":
            # 更新已有任务（修改标题/时间/优先级/分类）
            task_id = data.get("task_id")
            if task_id is None:
                # 如果只有一个候选 pending，则自动选中；否则要求明确
                candidates = []
                try:
                    candidates = database.get_recent_tasks(status='pending', limit=3)
                except Exception:
                    candidates = []
                if len(candidates) == 1:
                    task_id = candidates[0]["id"]
                else:
                    print("无法确定要修改哪个任务，能明确一下任务 ID 吗？或者说“修改上一个任务…”")
                    return False

            try:
                task_id = int(task_id)
            except (ValueError, TypeError):
                print("task_id 必须是整数哦")
                return False

            old = database.get_task_by_id(task_id)
            if not old:
                print(f"找不到任务 ID {task_id}，可能已经被删除了")
                return False

            # 允许部分字段更新
            new_content = data.get("content") if "content" in data else None
            new_due_time = parse_due_time(data.get("due_time")) if "due_time" in data else None
            new_priority = validate_priority(data.get("priority")) if "priority" in data else None
            new_category = data.get("category") if "category" in data else None

            did_update = False

            # content 支持“未提供/为 null”：代表不改标题；只有显式提供字符串时才更新
            if "content" in data and new_content is not None:
                if not isinstance(new_content, str) or not new_content.strip():
                    print("新任务内容不能为空哦")
                    return False
                if new_content.strip() != old.get("content"):
                    database.update_task_content(task_id, new_content.strip())
                    did_update = True

            if "due_time" in data:
                # 允许清空截止时间（null/None）
                database.update_task_due_time(task_id, new_due_time)
                if new_due_time != old.get("due_time"):
                    did_update = True

            if new_priority is not None and "priority" in data and new_priority != old.get("priority"):
                with database.get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("UPDATE tasks SET priority = ? WHERE id = ?", (new_priority, task_id))
                    conn.commit()
                did_update = True

            if "category" in data and new_category != old.get("category"):
                with database.get_db_connection() as conn:
                    cur = conn.cursor()
                    cur.execute("UPDATE tasks SET category = ? WHERE id = ?", (new_category, task_id))
                    conn.commit()
                did_update = True

            if not did_update:
                print("好的，任务没有实际变更（可能字段未提供或与原值相同）")
                add_to_history(user_text, "任务无变更")
                return True

            updated = database.get_task_by_id(task_id)
            if not updated:
                print("好的，已经更新了（但读取更新后数据失败）")
                add_to_history(user_text, "已更新任务")
                return True

            # 自然口语风格反馈（前后对比，闭环确认）
            changes = []
            if updated.get("content") != old.get("content"):
                changes.append(f"标题从「{old.get('content')}」改为「{updated.get('content')}」")
            if updated.get("due_time") != old.get("due_time"):
                old_time = old.get("due_time") or "无"
                new_time = updated.get("due_time") or "无"
                if new_time != "无":
                    try:
                        dt = parse_time(new_time)
                        new_time = dt.strftime('%m月%d日 %H:%M')
                    except:
                        pass
                changes.append(f"时间从 {old_time} 改为 {new_time}")
            if updated.get("priority") != old.get("priority"):
                changes.append(f"优先级从 {old.get('priority')} 改为 {updated.get('priority')}")
            if updated.get("category") != old.get("category"):
                old_cat = old.get("category") or "无"
                new_cat = updated.get("category") or "无"
                changes.append(f"分类从 {old_cat} 改为 {new_cat}")
            
            if changes:
                # 若是“时间纠错”场景，先道歉再确认
                prefix = "抱歉，刚才我把时间算错了。"
                if not _is_time_correction_intent(user_text):
                    prefix = "没问题。"

                reply = f"{prefix}我已经帮你把刚才那项更新为「{updated.get('content')}」了"
                if updated.get("due_time"):
                    try:
                        dt = parse_time(updated.get("due_time"))
                        time_str = dt.strftime('%m月%d日 %H:%M')
                        reply += f"，时间设在 {time_str}"
                    except:
                        pass
                reply += "，这样对吗？"
                print(reply)
                add_to_history(user_text, reply)
            else:
                print("好的，已经更新了")
                add_to_history(user_text, "已更新任务")

            return True

        elif action == "cancel_task":
            # 软取消任务：status='cancelled'（用于“取消上一个/刚才那个”）
            candidates = _pick_recent_pending(limit=5)
            task_id = _normalize_task_id(data.get("task_id"), candidates)
            if task_id is None:
                print("没有可取消的待办任务")
                return False

            old = database.get_task_by_id(task_id)
            if not old:
                print(f"找不到任务 ID {task_id}，可能已经被删除了")
                return False

            database.cancel_task(task_id)
            print(f"好的，已经取消「{old.get('content')}」了")
            add_to_history(user_text, f"已取消任务：{old.get('content')}")
            return True

        elif action == "chat":
            # 纯聊天/情绪表达（不存任务/改任务/查任务）
            reply = data.get("reply", "")
            if reply:
                print(reply)
                add_to_history(user_text, reply)
            else:
                print("好的，我明白了")
                add_to_history(user_text, "好的，我明白了")
            return True

        elif action == "query_tasks":
            # 验证数据
            time_range = data.get("time_range", "all")
            status_filter = data.get("status", "pending")
            limit = data.get("limit", 50)  # 默认最多返回 50 条
            
            # 根据 time_range 查询任务
            tasks = []
            now = datetime.now().astimezone()
            
            if time_range == "today":
                # 查询今天的任务（00:00:00 - 23:59:59）
                start_date = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                end_date = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
                tasks = database.get_tasks_by_date_range(start_date, end_date, status_filter if status_filter != "all" else None)
                
            elif time_range == "tomorrow":
                # 查询明天的任务
                tomorrow = now + timedelta(days=1)
                start_date = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                end_date = tomorrow.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
                tasks = database.get_tasks_by_date_range(start_date, end_date, status_filter if status_filter != "all" else None)
                
            elif time_range == "upcoming":
                # 查询未来 24 小时内的任务
                tasks = database.get_upcoming_tasks(hours=24, status=status_filter if status_filter != "all" else "pending")
                
            elif time_range == "overdue":
                # 查询过期未完成的任务
                tasks = database.get_overdue_tasks(status=status_filter if status_filter != "all" else "pending")
                
            else:  # "all" 或其他
                # 查询所有任务
                tasks = database.get_all_tasks(status=status_filter if status_filter != "all" else None)
            
            # 限制返回数量
            tasks = tasks[:limit]
            
            logger.info(f"查询任务成功: time_range={time_range}, status={status_filter}, 找到 {len(tasks)} 个任务")
            
            # 自然口语风格输出
            if not tasks:
                if time_range == "today":
                    print("今天暂时没有任务")
                elif time_range == "tomorrow":
                    print("明天暂时没有任务")
                elif time_range == "upcoming":
                    print("未来 24 小时内没有即将到期的任务")
                elif time_range == "overdue":
                    print("没有过期未完成的任务")
                else:
                    print("暂时没有找到任务")
                add_to_history(user_text, "查询任务：无结果")
            else:
                # 格式化任务列表
                time_range_names = {
                    "today": "今天",
                    "tomorrow": "明天",
                    "upcoming": "未来 24 小时",
                    "overdue": "过期",
                    "all": "所有"
                }
                range_name = time_range_names.get(time_range, "指定时间")
                print(f"{range_name}有 {len(tasks)} 个任务：")
                
                for i, task in enumerate(tasks, 1):
                    due_time_str = ""
                    if task['due_time']:
                        try:
                            # 解析 ISO 格式时间并格式化显示
                            due_dt = datetime.fromisoformat(task['due_time'].replace('Z', '+00:00'))
                            due_time_str = f" ({due_dt.strftime('%m月%d日 %H:%M')})"
                        except:
                            due_time_str = f" ({task['due_time']})"
                    
                    priority_str = "⭐" * task['priority']
                    print(f"  {i}. {task['content']}{due_time_str} {priority_str}")
                
                add_to_history(user_text, f"查询到 {len(tasks)} 个任务")
            
            return True
        
        else:
            logger.error(f"未知的 action: {action}")
            print("抱歉，这个操作我暂时处理不了")
            return False
            
    except Exception as e:
        logger.error(f"数据库操作失败: {e}", exc_info=True)
        print(f"处理出错了: {e}")
        return False


def get_clipboard_text() -> Optional[str]:
    """
    读取系统剪切板内容（macOS）
    
    Returns:
        剪切板文本内容，如果失败则返回 None
    """
    try:
        import subprocess
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=1
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def main():
    """
    主函数：交互式循环
    """
    logger.info("Project Link 翻译官启动")
    print("=" * 50)
    print("🤖 Project Link 翻译官已上线")
    print("=" * 50)
    print("提示: 输入 'exit' 或 'quit' 退出")
    print()
    
    # 启动时读取剪切板作为参考上下文（可选）
    clipboard_text = get_clipboard_text()
    if clipboard_text:
        # 将剪切板内容作为初始上下文提示（不自动处理，仅作为参考）
        print(f"💡 检测到剪切板内容（仅供参考）: {clipboard_text[:50]}{'...' if len(clipboard_text) > 50 else ''}")
        print()
    
    # 确保数据库已初始化
    try:
        database.init_db()
        logger.info("数据库初始化成功")
        print("数据库已就绪\n")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}", exc_info=True)
        print(f"数据库初始化失败: {e}")
        print("   请检查 database.py 和 app.db")
        return
    
    # 交互循环
    while True:
        try:
            user_text = input("👤 你说: ").strip()
            
            if not user_text:
                continue
            
            if user_text.lower() in ['exit', 'quit', '退出']:
                logger.info("用户退出程序")
                print("\n👋 再见！")
                break
            
            # 处理用户输入
            process_user_input(user_text)
            print()  # 空行分隔
            
        except KeyboardInterrupt:
            logger.info("用户中断程序 (KeyboardInterrupt)")
            print("\n\n👋 再见！")
            break
        except EOFError:
            logger.info("用户退出程序 (EOFError)")
            print("\n\n👋 再见！")
            break
        except Exception as e:
            logger.error(f"发生未预期的错误: {e}", exc_info=True)
            print(f"\n抱歉，出错了: {e}")
            print("   程序将继续运行...\n")


if __name__ == "__main__":
    main()

