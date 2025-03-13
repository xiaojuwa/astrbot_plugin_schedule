import asyncio
from datetime import datetime, timedelta
import heapq
from astrbot.api import logger
import traceback
from astrbot.api.event import MessageChain
from typing import List, Tuple, Optional
from functools import wraps
import json
import os


def scheduler_error_handler(func):
    """调度器错误处理装饰器"""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"{func.__name__} 执行出错: {str(e)}")
            logger.error(traceback.format_exc())
            # 出错后等待一段时间再继续
            await asyncio.sleep(60)
            return None

    return wrapper


class CourseScheduler:
    def __init__(self, context, schedule_data, class_time_table):
        self.context = context
        self.schedule_data = schedule_data
        self.class_time_table = class_time_table
        self.task_queue: List[Tuple[datetime, str, dict]] = []  # (时间, 目标ID, 课程信息)
        self.wakeup_event = asyncio.Event()
        self.scheduled_task_ref: Optional[asyncio.Task] = None
        self.notification_config_file = os.path.join(os.path.dirname(__file__), "notification_config.json")
        self.reminder_cache_file = os.path.join(os.path.dirname(__file__), "reminder_cache.json")
        self.notification_enabled = False
        self.notification_targets = {}
        self.term_start = datetime(2025, 2, 26)  # 学期开始日期
        self.html_render = None  # 将在主类中设置

    def set_html_render(self, html_render_func):
        """设置HTML渲染函数"""
        self.html_render = html_render_func

    def load_notification_config(self):
        """加载通知配置"""
        try:
            if os.path.exists(self.notification_config_file):
                with open(self.notification_config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    self.notification_enabled = config.get("enabled", False)
                    self.notification_targets = config.get("targets", {})
                    logger.info("通知配置加载成功")
            else:
                logger.info(f"通知配置文件不存在，将使用默认配置")
                self.save_notification_config()
        except Exception as e:
            logger.error(f"加载通知配置失败: {str(e)}")

    def save_notification_config(self):
        """保存通知配置"""
        try:
            config = {
                "enabled": self.notification_enabled,
                "targets": self.notification_targets
            }
            with open(self.notification_config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
                logger.info("通知配置保存成功")
        except Exception as e:
            logger.error(f"保存通知配置失败: {str(e)}")

    def update_task_queue(self):
        """更新任务队列"""
        # 清空当前队列
        self.task_queue = []

        # 如果通知未启用或没有目标，则不添加任务
        if not self.notification_enabled or not self.notification_targets:
            logger.info("通知功能未启用或没有通知目标，任务队列为空")
            return

        # 获取当前时间和日期信息
        now = datetime.now()
        weekday = now.weekday()
        weekday_str = self.schedule_data["weekdays"][weekday]

        # 计算当前周次
        days_passed = (now - self.term_start).days
        current_week = days_passed // 7 + 1

        # 获取今天和明天的所有课程
        for day_offset in range(2):  # 0=今天, 1=明天
            target_date = now + timedelta(days=day_offset)
            target_weekday = target_date.weekday()
            target_weekday_str = self.schedule_data["weekdays"][target_weekday]
            
            # 计算目标日期的周次
            target_days_passed = (target_date - self.term_start).days
            target_week = target_days_passed // 7 + 1
            
            # 检查每个时间段是否有课程
            for time_slot in self.schedule_data["time_slots"]:
                slot = time_slot["slot"]
                course_key = f"{target_weekday_str}_{slot}"
                
                if course_key in self.schedule_data["courses"]:
                    for course in self.schedule_data["courses"][course_key]:
                        # 检查当前周次是否在课程的周次范围内
                        weeks_range = course["weeks"]
                        if not self._is_course_in_week(weeks_range, target_week):
                            continue
                        
                        # 计算课程开始时间
                        start_time_str = self.class_time_table[slot]["start"]
                        start_hour, start_minute = map(int, start_time_str.split(":"))
                        
                        # 课程开始时间
                        course_start_time = target_date.replace(
                            hour=start_hour, 
                            minute=start_minute, 
                            second=0, 
                            microsecond=0
                        )
                        
                        # 提前1小时提醒
                        remind_time = course_start_time - timedelta(hours=1)
                        
                        # 如果提醒时间已经过了，跳过
                        if remind_time <= now:
                            continue
                        
                        # 为每个通知目标添加任务
                        for target in self.notification_targets:
                            # 课程信息字典
                            course_info = {
                                "name": course["name"],
                                "classroom": course["classroom"],
                                "teacher": course["teacher"],
                                "time_slot": time_slot,
                                "start_time": start_time_str,
                                "course_date": target_date.strftime("%Y-%m-%d")
                            }
                            
                            # 添加到任务队列
                            heapq.heappush(self.task_queue, (remind_time, target, course_info))
                            logger.info(f"已添加课程提醒任务: {course['name']}, 提醒时间: {remind_time.strftime('%Y-%m-%d %H:%M')}, 目标: {target}")

    def _is_course_in_week(self, weeks_range, current_week):
        """判断当前周次是否在课程的周次范围内"""
        try:
            if "-" in weeks_range:
                start_week, end_week = map(int, weeks_range.split("-"))
                return start_week <= current_week <= end_week
            else:
                return int(weeks_range) == current_week
        except:
            return False

    @scheduler_error_handler
    async def _execute_task(self, target: str, course_info: dict) -> None:
        """执行课程提醒任务"""
        try:
            # 检查是否已经发送过提醒，避免重复发送
            reminder_key = f"{course_info['course_date']}_{course_info['name']}_{course_info['time_slot']['slot']}"
            sent_reminders = {}
            
            # 加载已发送的提醒记录
            if os.path.exists(self.reminder_cache_file):
                try:
                    with open(self.reminder_cache_file, 'r', encoding='utf-8') as f:
                        sent_reminders = json.load(f)
                except Exception as e:
                    logger.error(f"加载提醒缓存失败: {str(e)}")
            
            # 如果今天没有发送过这个课程的提醒
            if reminder_key not in sent_reminders:
                # 构建提醒消息
                html_template = self._get_notification_template(course_info)
                
                # 使用html_render方法渲染HTML为图片
                if self.html_render:
                    url = await self.html_render(html_template, {})
                    
                    logger.info(f"准备发送课程提醒: {course_info['name']} 到 {target}")
                    
                    # 创建消息链
                    from astrbot.api.message_components import Image
                    chain = MessageChain([Image(file=url)])
                    
                    try:
                        await self.context.send_message(target, chain)
                        logger.info(f"已成功发送课程提醒: {course_info['name']} 到 {target}")
                        
                        # 记录已发送的提醒
                        sent_reminders[reminder_key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        try:
                            with open(self.reminder_cache_file, 'w', encoding='utf-8') as f:
                                json.dump(sent_reminders, f, ensure_ascii=False, indent=4)
                        except Exception as e:
                            logger.error(f"保存提醒缓存失败: {str(e)}")
                    except Exception as e:
                        logger.error(f"发送课程提醒失败: {str(e)}")
                else:
                    logger.error("HTML渲染函数未设置，无法发送提醒")
            else:
                logger.info(f"今天已经发送过 {course_info['name']} 的提醒，跳过")
                
        except Exception as e:
            logger.error(f"执行课程提醒任务出错: {str(e)}")
            logger.error(traceback.format_exc())

    @scheduler_error_handler
    async def scheduled_task(self) -> None:
        """定时任务主循环"""
        while True:
            try:
                # 如果任务队列为空，等待唤醒
                if not self.task_queue:
                    logger.info("任务队列为空，等待唤醒")
                    self.wakeup_event.clear()
                    await self.wakeup_event.wait()
                    continue

                # 获取下一个任务
                next_time, target, course_info = self.task_queue[0]

                # 计算等待时间
                now = datetime.now()
                if next_time > now:
                    wait_seconds = (next_time - now).total_seconds()

                    # 设置唤醒事件的超时
                    try:
                        # 等待唤醒事件或超时
                        await asyncio.wait_for(
                            self.wakeup_event.wait(), timeout=wait_seconds
                        )

                        # 如果被唤醒，重新计算任务
                        self.wakeup_event.clear()
                        continue
                    except asyncio.TimeoutError:
                        # 超时，执行任务
                        pass

                # 弹出当前任务
                next_time, target, course_info = heapq.heappop(self.task_queue)

                # 执行任务
                await self._execute_task(target, course_info)

            except asyncio.CancelledError:
                # 任务被取消
                logger.info("定时任务被取消")
                break
            except Exception as e:
                logger.error(f"定时任务循环出错: {str(e)}")
                logger.error(traceback.format_exc())
                # 出错后等待一段时间再继续
                await asyncio.sleep(60)

    def start(self) -> None:
        """启动定时任务"""
        if not self.scheduled_task_ref:
            logger.info("创建定时任务...")
            self.scheduled_task_ref = asyncio.get_event_loop().create_task(
                self.scheduled_task()
            )
            logger.info("定时任务已创建并启动")
        else:
            logger.info("定时任务已经在运行中")

    async def stop(self) -> None:
        """停止定时任务"""
        if self.scheduled_task_ref:
            self.scheduled_task_ref.cancel()
            self.scheduled_task_ref = None
            logger.info("定时任务已停止")

    def _get_notification_template(self, course_info, time_slot=None):
        """生成课程提醒HTML模板"""
        # 如果time_slot为None但course_info中包含time_slot，则使用course_info中的time_slot
        if time_slot is None and 'time_slot' in course_info:
            time_slot = course_info['time_slot']
            
        return f'''
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8"/>
            <style>
                html, body {{
                    margin: 0;
                    padding: 0;
                    width: 100%;
                    height: 100%;
                    font-family: "Microsoft YaHei", sans-serif;
                    background: linear-gradient(135deg, #fff9c4 0%, #ffee58 100%);
                }}
                body {{
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    padding: 0;
                }}
                .notification-container {{
                    width: 100%;
                    max-width: 100%;
                    background: linear-gradient(135deg, #ffffff 0%, #f5f5f5 100%);
                    border-radius: 15px;
                    box-shadow: 0 10px 20px rgba(0,0,0,0.1);
                    overflow: hidden;
                    padding: 20px;
                    margin: 20px;
                    text-align: center;
                }}
                .notification-header {{
                    background-color: #ffeb3b;
                    color: #333;
                    padding: 15px;
                    border-radius: 10px 10px 0 0;
                    margin: -20px -20px 20px -20px;
                }}
                .notification-title {{
                    font-size: 24px;
                    font-weight: bold;
                    margin: 0;
                }}
                .notification-subtitle {{
                    font-size: 16px;
                    margin: 10px 0 0 0;
                    color: #555;
                }}
                .course-info {{
                    padding: 15px;
                    background-color: #f9f9f9;
                    border-radius: 10px;
                    margin-bottom: 15px;
                }}
                .course-name {{
                    font-size: 22px;
                    font-weight: bold;
                    color: #ff6f00;
                    margin-bottom: 10px;
                }}
                .info-row {{
                    display: flex;
                    justify-content: space-between;
                    margin: 10px 0;
                    padding: 8px 0;
                    border-bottom: 1px dashed #ddd;
                }}
                .info-label {{
                    font-weight: bold;
                    color: #555;
                }}
                .info-value {{
                    color: #333;
                }}
                .reminder-footer {{
                    margin-top: 20px;
                    font-size: 14px;
                    color: #777;
                    font-style: italic;
                }}
            </style>
        </head>
        <body>
            <div class="notification-container">
                <div class="notification-header">
                    <h1 class="notification-title">课程提醒</h1>
                    <p class="notification-subtitle">距离上课还有1小时</p>
                </div>
                <div class="course-info">
                    <div class="course-name">{course_info['name']}</div>
                    <div class="info-row">
                        <span class="info-label">上课时间:</span>
                        <span class="info-value">{course_info['course_date']} {course_info['start_time']}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">上课地点:</span>
                        <span class="info-value">{course_info['classroom']}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">任课教师:</span>
                        <span class="info-value">{course_info['teacher']}</span>
                    </div>
                    <div class="info-row">
                        <span class="info-label">课程节次:</span>
                        <span class="info-value">{time_slot['period']} {time_slot['slot']}节</span>
                    </div>
                </div>
                <div class="reminder-footer">
                    宝宝请提前做好上课准备哦！
                </div>
            </div>
        </body>
        </html>
        '''