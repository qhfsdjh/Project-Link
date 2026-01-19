"""
Project Link - 意图解析翻译官
将用户的自然语言输入转换为数据库操作指令
"""

import json
import os
import sys
from typing import Optional, Dict, Any, Literal, Tuple
from datetime import datetime, timedelta

import database  # 导入数据库模块
import prompts  # 导入提示词模块
import config  # 导入配置模块
from utils.logger import get_logger  # 导入日志模块

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
    调用 Ollama 让 Qwen 解析用户的真实意图
    
    Args:
        user_input: 用户输入的自然语言
    
    Returns:
        解析后的 JSON 对象，如果失败则返回 None
    """
    # 从 prompts 模块获取提示词（自动注入当前时间信息）
    system_prompt = prompts.get_system_prompt()
    # 启发式两段式：把最近 N 条 pending 任务作为候选上下文塞进 prompt
    recent_tasks = []
    try:
        recent_tasks = database.get_recent_tasks(status='pending', limit=3)
    except Exception as e:
        logger.warning(f"获取最近任务候选失败: {e}")
    user_prompt = prompts.get_user_prompt(user_input, recent_tasks=recent_tasks)
    
    try:
        # 使用 ollama.chat() API
        response = ollama.chat(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            format="json",
            options={
                "temperature": config.PL_AI_TEMPERATURE,  # 从配置读取
            }
        )
        
        # 提取响应内容
        # ChatResponse 对象支持字典式访问（response['message']['content']）
        # 也支持属性访问（response.message.content）
        try:
            # 优先使用字典式访问（更通用）
            if isinstance(response, dict):
                response_text = response.get('message', {}).get('content', '')
            else:
                # 使用属性访问
                response_text = response.message.content if hasattr(response, 'message') else ''
        except (AttributeError, TypeError, KeyError) as e:
            logger.warning(f"无法提取响应内容: {e}")
            print(f"⚠️  警告: 无法提取响应内容: {e}")
            response_text = ''
        
        if not response_text:
            logger.error("AI 返回空响应")
            print("❌ AI 返回空响应")
            return None
        
        # 解析 JSON
        try:
            logger.debug(f"AI 解析成功，返回 JSON: {json.loads(response_text)}")
            return json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析失败: {e}，AI 返回的原始内容: {response_text[:200]}...")
            print(f"❌ JSON 解析失败: {e}")
            print(f"   AI 返回的原始内容: {response_text[:200]}...")
            return None
            
    except Exception as e:
        logger.error(f"调用 Ollama API 失败: {e}", exc_info=True)
        print(f"❌ 调用 Ollama API 失败: {e}")
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
    
    # 2. 让 AI 解析意图
    logger.info(f"开始解析用户输入: {user_text[:50]}...")
    print("🧠 正在思考...")
    result = get_ai_interpretation(user_text)
    
    if not result:
        logger.warning("AI 解析失败，返回 None")
        print("❌ 我没听懂，请换种说法。")
        return False
    
    # 3. 验证 JSON 结构
    action, data = validate_action_data(result)
    if not action or not data:
        logger.error("解析结果格式不正确")
        print("❌ 解析结果格式不正确。")
        return False
    
    logger.info(f"AI 解析成功，action: {action}")
    
    # 4. 根据意图执行数据库操作
    try:
        if action == "add_task":
            # 验证和规范化数据
            content = data.get("content")
            if not content:
                print("❌ 任务内容不能为空")
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
            print(f"✅ 已存入任务 (ID: {task_id}): {content}")
            if due_time:
                print(f"   截止时间: {due_time}")
            return True
            
        elif action == "add_preference":
            # 验证数据
            key = data.get("key")
            value = data.get("value")
            source = data.get("source", "AI推断")
            
            if not key or not value:
                print("❌ 偏好键名和值不能为空")
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
            print(f"💡 我学到了一个新习惯: {key} -> {value}")
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
                    print(f"🧠 已存入深度记忆 (情感: {sentiment}, 标签: {tag or '无'})")
                except Exception as e:
                    logger.warning(f"更新记忆记录失败: {e}")
                    print(f"⚠️  警告: 更新记忆记录失败: {e}")
                    # 如果更新失败，至少原始记录已保存
            else:
                # 如果原始记录失败，这里尝试新增（降级处理）
                try:
                    database.record_interaction(user_text, sentiment, tag)
                    logger.info(f"记忆已记录: sentiment={sentiment}, tag={tag}")
                    print(f"🧠 已存入深度记忆 (情感: {sentiment}, 标签: {tag or '无'})")
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
                    print("❌ 无法确定要修改哪个任务：请明确任务 ID，或说“修改上一个任务…”。")
                    return False

            try:
                task_id = int(task_id)
            except (ValueError, TypeError):
                print("❌ task_id 必须是整数")
                return False

            old = database.get_task_by_id(task_id)
            if not old:
                print(f"❌ 找不到任务 ID {task_id}")
                return False

            # 允许部分字段更新
            new_content = data.get("content") if "content" in data else None
            new_due_time = parse_due_time(data.get("due_time")) if "due_time" in data else None
            new_priority = validate_priority(data.get("priority")) if "priority" in data else None
            new_category = data.get("category") if "category" in data else None

            did_update = False

            if "content" in data:
                if not isinstance(new_content, str) or not new_content.strip():
                    print("❌ 新任务内容不能为空")
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
                print(f"⚠️  任务 ID {task_id} 没有实际变更（可能是字段未提供或与原值相同）")
                return True

            updated = database.get_task_by_id(task_id)
            if not updated:
                print(f"✅ 已更新任务 ID {task_id}（但读取更新后数据失败）")
                return True

            # 清晰反馈（前后对比）
            if updated.get("content") != old.get("content"):
                print(f"✅ 已将任务 ID {task_id} 的标题从「{old.get('content')}」修改为「{updated.get('content')}」")
            if updated.get("due_time") != old.get("due_time"):
                print(f"✅ 已将任务 ID {task_id} 的截止时间从 {old.get('due_time')} 修改为 {updated.get('due_time')}")
            if updated.get("priority") != old.get("priority"):
                print(f"✅ 已将任务 ID {task_id} 的优先级从 {old.get('priority')} 修改为 {updated.get('priority')}")
            if updated.get("category") != old.get("category"):
                print(f"✅ 已将任务 ID {task_id} 的分类从 {old.get('category')} 修改为 {updated.get('category')}")

            return True

        elif action == "cancel_task":
            # 软取消任务：status='cancelled'（用于“取消上一个/刚才那个”）
            task_id = data.get("task_id")
            if task_id is None:
                candidates = []
                try:
                    candidates = database.get_recent_tasks(status='pending', limit=3)
                except Exception:
                    candidates = []
                if candidates:
                    task_id = candidates[0]["id"]
                else:
                    print("❌ 没有可取消的 pending 任务")
                    return False

            try:
                task_id = int(task_id)
            except (ValueError, TypeError):
                print("❌ task_id 必须是整数")
                return False

            old = database.get_task_by_id(task_id)
            if not old:
                print(f"❌ 找不到任务 ID {task_id}")
                return False

            database.cancel_task(task_id)
            print(f"✅ 已取消任务 ID {task_id}: {old.get('content')}")
            return True

        elif action == "chat":
            reply = data.get("reply") if isinstance(data, dict) else None
            print(reply or "👌")
            return True

        elif action == "query_tasks":
            # 验证数据
            time_range = data.get("time_range", "all")
            status_filter = data.get("status", "pending")
            limit = data.get("limit", 50)  # 默认最多返回 50 条
            
            # 根据 time_range 查询任务
            tasks = []
            now = datetime.now()
            
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
            
            # 格式化输出（简单格式化）
            if not tasks:
                if time_range == "today":
                    print("📋 你今天没有任务")
                elif time_range == "tomorrow":
                    print("📋 你明天没有任务")
                elif time_range == "upcoming":
                    print("📋 未来 24 小时内没有即将到期的任务")
                elif time_range == "overdue":
                    print("📋 没有过期未完成的任务")
                else:
                    print("📋 没有找到任务")
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
                print(f"📋 你{range_name}有 {len(tasks)} 个任务：")
                
                for i, task in enumerate(tasks, 1):
                    due_time_str = ""
                    if task['due_time']:
                        try:
                            # 解析 ISO 格式时间并格式化显示
                            due_dt = datetime.fromisoformat(task['due_time'])
                            due_time_str = f" ({due_dt.strftime('%m月%d日 %H:%M')})"
                        except:
                            due_time_str = f" ({task['due_time']})"
                    
                    priority_str = "⭐" * task['priority']
                    print(f"  {i}. {task['content']}{due_time_str} {priority_str}")
            
            return True
        
        else:
            logger.error(f"未知的 action: {action}")
            print(f"❌ 未知的 action: {action}")
            return False
            
    except Exception as e:
        logger.error(f"数据库操作失败: {e}", exc_info=True)
        print(f"❌ 数据库操作失败: {e}")
        return False


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
    
    # 确保数据库已初始化
    try:
        database.init_db()
        logger.info("数据库初始化成功")
        print("✅ 数据库已就绪\n")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}", exc_info=True)
        print(f"❌ 数据库初始化失败: {e}")
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
            print(f"\n❌ 发生未预期的错误: {e}")
            print("   程序将继续运行...\n")


if __name__ == "__main__":
    main()

