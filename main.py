from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import *
import asyncio
import datetime
import os
import json
import time

# 定义全局变量来存储课表数据和通知设置
schedule_data = {}
notification_targets = {}  # 用于存储需要通知的用户
notification_enabled = False  # 是否启用通知功能

# 通知配置文件路径
NOTIFICATION_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "notification_config.json")

# 课程时间表，用于计算提醒时间
class_time_table = {
    "1-2": {"start": "08:00", "end": "09:40"},
    "3-4": {"start": "10:00", "end": "11:40"},
    "5-6": {"start": "14:00", "end": "15:40"},
    "7-8": {"start": "16:00", "end": "17:40"},
    "9-10": {"start": "18:30", "end": "20:10"},
    "11": {"start": "20:20", "end": "21:05"}
}

@register("schedule", "xiaojuwa", "课表查询与提醒插件", "1.0.0", "https://github.com/xiaojuwa/astrbot_plugin_schedule")
class SchedulePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_file = os.path.join(os.path.dirname(__file__), "kebiao.json")
        self.load_schedule_data()
        self.load_notification_config()
        asyncio.get_event_loop().create_task(self.notification_task())
    
    def load_schedule_data(self):
        """加载课表数据"""
        global schedule_data
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    schedule_data = json.load(f)
                    logger.info("课表数据加载成功")
            else:
                logger.error(f"课表数据文件不存在: {self.data_file}")
        except Exception as e:
            logger.error(f"加载课表数据失败: {str(e)}")
    
    def load_notification_config(self):
        """加载通知配置"""
        global notification_enabled, notification_targets
        try:
            if os.path.exists(NOTIFICATION_CONFIG_FILE):
                with open(NOTIFICATION_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    notification_enabled = config.get("enabled", False)
                    notification_targets = config.get("targets", {})
                    logger.info("通知配置加载成功")
            else:
                logger.info(f"通知配置文件不存在，将使用默认配置")
                self.save_notification_config()
        except Exception as e:
            logger.error(f"加载通知配置失败: {str(e)}")
    
    def save_notification_config(self):
        """保存通知配置"""
        global notification_enabled, notification_targets
        try:
            config = {
                "enabled": notification_enabled,
                "targets": notification_targets
            }
            with open(NOTIFICATION_CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=4)
                logger.info("通知配置保存成功")
        except Exception as e:
            logger.error(f"保存通知配置失败: {str(e)}")
    
    @filter.command("课表")
    async def show_schedule(self, event: AstrMessageEvent, day: str = None):
        '''查询课表，可选参数：今天、明天、周一到周日'''
        if not schedule_data or not schedule_data.get("courses"):
            yield event.plain_result("课表数据未加载，请稍后再试")
            return
        
        # 确定查询的日期
        today = datetime.datetime.now()
        query_date = today
        
        if day:
            if day == "今天":
                query_date = today
            elif day == "明天":
                query_date = today + datetime.timedelta(days=1)
            elif day in ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]:
                weekday_map = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}
                days_ahead = weekday_map[day] - today.weekday()
                if days_ahead < 0:
                    days_ahead += 7
                query_date = today + datetime.timedelta(days=days_ahead)
            else:
                yield event.plain_result("日期格式不正确，请使用：今天、明天、周一到周日")
                return
        
        weekday = query_date.weekday()
        weekday_str = schedule_data["weekdays"][weekday]
        date_str = query_date.strftime("%Y年%m月%d日")
        
        # 获取当前周次（这里假设第一周的开始日期是2025年2月24日，可以根据实际情况调整）
        term_start = datetime.datetime(2025, 2, 24)
        days_passed = (query_date - term_start).days
        current_week = days_passed // 7 + 1
        
        # 获取当天的课程
        day_courses = []
        for time_slot in schedule_data["time_slots"]:
            slot = time_slot["slot"]
            course_key = f"{weekday_str}_{slot}"
            
            if course_key in schedule_data["courses"]:
                for course in schedule_data["courses"][course_key]:
                    # 检查当前周次是否在课程的周次范围内
                    weeks_range = course["weeks"]
                    if self.is_course_in_week(weeks_range, current_week):
                        day_courses.append({
                            "name": course["name"],
                            "time": f"{time_slot['period']}{slot}节 ({time_slot['time']})",
                            "classroom": course["classroom"],
                            "teacher": course["teacher"],
                            "raw_time_slot": slot
                        })
        
        # 按照时间顺序排序课程
        day_courses.sort(key=lambda x: x["raw_time_slot"])
        
        # 渲染课表信息为HTML图片
        if day_courses:
            html_template = self.get_schedule_template(date_str, weekday_str, current_week, day_courses)
            url = await self.html_render(html_template, {})
            yield event.image_result(url)
        else:
            # 如果没有课程，也渲染一个简单的图片
            html_template = self.get_empty_schedule_template(date_str, weekday_str, current_week)
            url = await self.html_render(html_template, {})
            yield event.image_result(url)
    
    def get_schedule_template(self, date_str, weekday_str, current_week, courses):
        """生成课表HTML模板"""
        courses_html = ""
        for course in courses:
            courses_html += f'''
            <div class="course-item">
                <div class="course-time">{course['time']}</div>
                <div class="course-content">
                    <div class="course-name">{course['name']}</div>
                    <div class="course-info">
                        <span class="course-location">📍 {course['classroom']}</span>
                        <span class="course-teacher">👨‍🏫 {course['teacher']}</span>
                    </div>
                </div>
            </div>
            <div class="course-divider"></div>
            '''
        
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
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                }}
                body {{
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    padding: 0;
                }}
                .schedule-container {{
                    width: 100%;
                    max-width: 100%;
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                    border-radius: 15px;
                    padding: 15px;
                    box-shadow: 0 10px 20px rgba(0, 0, 0, 0.1);
                    box-sizing: border-box;
                    margin: 0;
                }}
                .schedule-header {{
                    text-align: center;
                    margin-bottom: 15px;
                    padding-bottom: 12px;
                    border-bottom: 3px solid #7986cb;
                }}
                .date {{
                    font-size: 20px;
                    color: #5c6bc0;
                    margin-bottom: 8px;
                }}
                .weekday {{
                    font-size: 32px;
                    font-weight: bold;
                    color: #3f51b5;
                }}
                .schedule-content {{
                    background-color: white;
                    border-radius: 12px;
                    padding: 15px;
                    box-shadow: 0 5px 15px rgba(0, 0, 0, 0.05);
                }}
                .course-item {{
                    display: flex;
                    padding: 15px 10px;
                    margin-bottom: 5px;
                }}
                .course-time {{
                    flex: 0 0 120px;
                    color: #7986cb;
                    font-weight: bold;
                    font-size: 18px;
                    padding-right: 15px;
                    border-right: 4px solid #c5cae9;
                    display: flex;
                    align-items: center;
                }}
                .course-content {{
                    flex: 1;
                    padding-left: 15px;
                }}
                .course-name {{
                    font-size: 22px;
                    font-weight: bold;
                    color: #3f51b5;
                    margin-bottom: 8px;
                }}
                .course-info {{
                    display: flex;
                    justify-content: space-between;
                    color: #7986cb;
                    font-size: 16px;
                }}
                .course-divider {{
                    height: 1px;
                    background-color: #e8eaf6;
                    margin: 5px 0;
                }}
                .schedule-footer {{
                    text-align: center;
                    margin-top: 18px;
                    color: #7986cb;
                    font-size: 16px;
                    font-style: italic;
                }}
            </style>
        </head>
        <body>
            <div class="schedule-container">
                <div class="schedule-header">
                    <div class="date">{date_str}</div>
                    <div class="weekday">{weekday_str} (第{current_week}周)</div>
                </div>
                <div class="schedule-content">
                    {courses_html}
                </div>
                <div class="schedule-footer">
                    课表查询 - 每天都要元气满满哦~
                </div>
            </div>
        </body>
        </html>
        '''
    
    def get_empty_schedule_template(self, date_str, weekday_str, current_week):
        """生成空课表HTML模板"""
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
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                }}
                body {{
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    padding: 0;
                }}
                .schedule-container {{
                    width: 100%;
                    max-width: 100%;
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                    border-radius: 15px;
                    padding: 15px;
                    box-shadow: 0 10px 20px rgba(0, 0, 0, 0.1);
                    box-sizing: border-box;
                    margin: 0;
                }}
                .schedule-header {{
                    text-align: center;
                    margin-bottom: 15px;
                    padding-bottom: 12px;
                    border-bottom: 3px solid #7986cb;
                }}
                .date {{
                    font-size: 20px;
                    color: #5c6bc0;
                    margin-bottom: 8px;
                }}
                .weekday {{
                    font-size: 32px;
                    font-weight: bold;
                    color: #3f51b5;
                }}
                .schedule-content {{
                    background-color: white;
                    border-radius: 12px;
                    padding: 25px 15px;
                    box-shadow: 0 5px 15px rgba(0, 0, 0, 0.05);
                    text-align: center;
                }}
                .empty-schedule {{
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    padding: 25px 0;
                }}
                .calendar {{
                    width: 140px;
                    height: 160px;
                    background-color: white;
                    border-radius: 12px;
                    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.1);
                    overflow: hidden;
                    margin-bottom: 25px;
                }}
                .calendar-header {{
                    background-color: #e53935;
                    color: white;
                    text-align: center;
                    padding: 8px 0;
                    font-weight: bold;
                    font-size: 18px;
                }}
                .calendar-date {{
                    font-size: 72px;
                    font-weight: bold;
                    color: #333;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 120px;
                }}
                .empty-text {{
                    font-size: 28px;
                    font-weight: bold;
                    color: #3f51b5;
                    margin-bottom: 12px;
                }}
                .empty-subtext {{
                    font-size: 18px;
                    color: #7986cb;
                }}
                .schedule-footer {{
                    text-align: center;
                    margin-top: 18px;
                    color: #7986cb;
                    font-size: 16px;
                    font-style: italic;
                }}
            </style>
        </head>
        <body>
            <div class="schedule-container">
                <div class="schedule-header">
                    <div class="date">{date_str}</div>
                    <div class="weekday">{weekday_str} (第{current_week}周)</div>
                </div>
                <div class="schedule-content">
                    <div class="empty-schedule">
                        <div class="calendar">
                            <div class="calendar-header">日历</div>
                            <div class="calendar-date">{date_str.split('日')[0][-2:]}</div>
                        </div>
                        <div class="empty-text">今天没有课程安排</div>
                        <div class="empty-subtext">可以好好休息啦~</div>
                    </div>
                </div>
                <div class="schedule-footer">
                    课表查询 - 每天都要元气满满哦~
                </div>
            </div>
        </body>
        </html>
        '''
    
    def is_course_in_week(self, weeks_range, current_week):
        """判断当前周次是否在课程的周次范围内"""
        try:
            if "-" in weeks_range:
                start_week, end_week = map(int, weeks_range.split("-"))
                return start_week <= current_week <= end_week
            else:
                return int(weeks_range) == current_week
        except:
            return False
    

    def get_notification_template(self, course, time_slot):
        """生成课程提醒HTML模板"""
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
                    background: linear-gradient(135deg, #fff9c4 0%, #ffee58 100%);
                    border-radius: 15px;
                    padding: 15px;
                    box-shadow: 0 10px 20px rgba(0, 0, 0, 0.1);
                    box-sizing: border-box;
                    margin: 0;
                }}
                .notification-header {{
                    display: flex;
                    align-items: center;
                    margin-bottom: 15px;
                    padding-bottom: 12px;
                    border-bottom: 3px solid #fbc02d;
                }}
                .notification-icon {{
                    font-size: 38px;
                    margin-right: 12px;
                }}
                .notification-title {{
                    font-size: 28px;
                    font-weight: bold;
                    color: #f57f17;
                }}
                .notification-content {{
                    background-color: white;
                    border-radius: 12px;
                    padding: 15px;
                    box-shadow: 0 5px 15px rgba(0, 0, 0, 0.05);
                }}
                .notification-message {{
                    font-size: 22px;
                    font-weight: bold;
                    color: #f57f17;
                    margin-bottom: 15px;
                    text-align: center;
                }}
                .course-info {{
                    background-color: #fff8e1;
                    border-radius: 10px;
                    padding: 15px;
                    margin-bottom: 15px;
                }}
                .course-name {{
                    font-size: 24px;
                    font-weight: bold;
                    color: #e65100;
                    margin-bottom: 10px;
                    text-align: center;
                }}
                .course-time, .course-location, .course-teacher {{
                    font-size: 18px;
                    color: #ff6f00;
                    margin-bottom: 8px;
                }}
                .notification-footer {{
                    text-align: center;
                    font-size: 18px;
                    font-style: italic;
                    color: #f57f17;
                    margin-top: 10px;
                }}
            </style>
        </head>
        <body>
            <div class="notification-container">
                <div class="notification-header">
                    <div class="notification-icon">⏰</div>
                    <div class="notification-title">课程提醒</div>
                </div>
                <div class="notification-content">
                    <div class="notification-message">你有一节课程将在1小时后开始</div>
                    <div class="course-info">
                        <div class="course-name">{course['name']}</div>
                        <div class="course-time">{time_slot['period']}{time_slot['slot']}节 ({time_slot['time']})</div>
                        <div class="course-location">📍 {course['classroom']}</div>
                        <div class="course-teacher">👨‍🏫 {course['teacher']}</div>
                    </div>
                    <div class="notification-footer">请做好上课准备哦~</div>
                </div>
            </div>
        </body>
        </html>
        '''

    @filter.command("开启课程提醒")
    async def enable_notification(self, event: AstrMessageEvent):
        '''开启课程提醒功能，将在每节课前1小时提醒'''
        global notification_enabled, notification_targets
        notification_enabled = True
        notification_targets[event.unified_msg_origin] = True
        self.save_notification_config()
        yield event.plain_result("课程提醒已开启，将在每节课前1小时提醒你")
    
    @filter.command("关闭课程提醒")
    async def disable_notification(self, event: AstrMessageEvent):
        '''关闭课程提醒功能'''
        global notification_targets
        if event.unified_msg_origin in notification_targets:
            del notification_targets[event.unified_msg_origin]
            self.save_notification_config()
            yield event.plain_result("课程提醒已关闭")
        else:
            yield event.plain_result("你尚未开启课程提醒")
    
    @filter.command("通知状态")
    async def notification_status(self, event: AstrMessageEvent):
        '''查询当前通知功能的状态'''
        global notification_enabled, notification_targets
        
        if notification_enabled:
            target_count = len(notification_targets)
            if event.unified_msg_origin in notification_targets:
                yield event.plain_result(f"✅ 通知功能已开启\n当前共有 {target_count} 个用户接收通知\n你已开启课程提醒功能")
            else:
                yield event.plain_result(f"✅ 通知功能已开启\n当前共有 {target_count} 个用户接收通知\n你尚未开启课程提醒功能，可以使用 /开启课程提醒 命令开启")
        else:
            yield event.plain_result("❌ 通知功能当前已关闭\n可以使用 /开启课程提醒 命令开启")

    
    async def notification_task(self):
        """定时任务，检查是否需要发送课程提醒"""
        global notification_enabled, notification_targets, schedule_data
        
        while True:
            try:
                # 如果没有启用通知或没有通知目标，就跳过
                if not notification_enabled or not notification_targets:
                    logger.info("通知功能未启用或没有通知目标，跳过检查")
                    await asyncio.sleep(60)
                    continue
                
                now = datetime.datetime.now()
                logger.info(f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
                weekday = now.weekday()
                weekday_str = schedule_data["weekdays"][weekday]
                
                # 获取当前周次
                term_start = datetime.datetime(2025, 2, 26)
                days_passed = (now - term_start).days
                current_week = days_passed // 7 + 1
                logger.info(f"当前周次: 第{current_week}周, 星期{weekday_str}")
                
                # 记录今天的所有课程和提醒时间
                today_courses = []
                
                # 检查每个时间段是否有课程需要提醒
                for time_slot in schedule_data["time_slots"]:
                    slot = time_slot["slot"]
                    course_key = f"{weekday_str}_{slot}"
                    
                    if course_key in schedule_data["courses"]:
                        for course in schedule_data["courses"][course_key]:
                            # 检查当前周次是否在课程的周次范围内
                            weeks_range = course["weeks"]
                            if not self.is_course_in_week(weeks_range, current_week):
                                continue
                            
                            # 计算课程开始时间
                            start_time_str = class_time_table[slot]["start"]
                            start_hour, start_minute = map(int, start_time_str.split(":"))
                            
                            # 课程开始时间
                            course_start_time = now.replace(
                                hour=start_hour, 
                                minute=start_minute, 
                                second=0, 
                                microsecond=0
                            )
                            
                            # 如果课程时间已经过了，跳过
                            if course_start_time < now:
                                continue
                                
                            # 提前1小时提醒
                            remind_time = course_start_time - datetime.timedelta(hours=1)
                            
                            # 记录课程信息
                            today_courses.append({
                                "name": course["name"],
                                "start_time": course_start_time.strftime("%H:%M"),
                                "remind_time": remind_time.strftime("%H:%M"),
                                "classroom": course["classroom"],
                                "time_slot": time_slot,
                                "course": course
                            })
                            
                            # 计算当前时间与提醒时间的差值(绝对值，单位为秒)
                            time_diff_seconds = abs((now - remind_time).total_seconds())
                            
                            # 如果当前时间在提醒时间的前后2分钟内，发送提醒
                            # 使用2分钟的窗口，避免因为检查间隔导致错过提醒
                            if time_diff_seconds < 120:  # 2分钟内
                                logger.info(f"触发课程提醒: {course['name']}, 课程开始时间: {start_time_str}, 提醒时间: {remind_time.strftime('%H:%M:%S')}")
                                
                                # 检查是否已经发送过提醒，避免重复发送
                                reminder_key = f"{now.strftime('%Y-%m-%d')}_{course['name']}_{slot}"
                                reminder_cache_file = os.path.join(os.path.dirname(__file__), "reminder_cache.json")
                                sent_reminders = {}
                                
                                # 加载已发送的提醒记录
                                if os.path.exists(reminder_cache_file):
                                    try:
                                        with open(reminder_cache_file, 'r', encoding='utf-8') as f:
                                            sent_reminders = json.load(f)
                                    except Exception as e:
                                        logger.error(f"加载提醒缓存失败: {str(e)}")
                                
                                # 如果今天没有发送过这个课程的提醒
                                if reminder_key not in sent_reminders:
                                    for target in notification_targets:
                                        try:
                                            # 构建提醒消息
                                            html_template = self.get_notification_template(course, time_slot)
                                            url = await self.html_render(html_template, {})
                                            
                                            chain = MessageChain()
                                            chain.chain.extend([
                                                Image(file=url)
                                            ])
                                            await self.context.send_message(target, chain)
                                            logger.info(f"已成功发送课程提醒: {course['name']} 到 {target}")
                                        except Exception as e:
                                            logger.error(f"发送课程提醒失败: {str(e)}")
                                    
                                    # 记录已发送的提醒
                                    sent_reminders[reminder_key] = now.strftime("%Y-%m-%d %H:%M:%S")
                                    try:
                                        with open(reminder_cache_file, 'w', encoding='utf-8') as f:
                                            json.dump(sent_reminders, f, ensure_ascii=False, indent=4)
                                    except Exception as e:
                                        logger.error(f"保存提醒缓存失败: {str(e)}")
                                else:
                                    logger.info(f"今天已经发送过 {course['name']} 的提醒，跳过")
                
                # 输出今天的课程和下次提醒信息
                if today_courses:
                    today_courses.sort(key=lambda x: x["start_time"])
                    logger.info(f"今天共有 {len(today_courses)} 节课程:")
                    for idx, course_info in enumerate(today_courses):
                        logger.info(f"  {idx+1}. {course_info['name']} - 上课时间: {course_info['start_time']}, 提醒时间: {course_info['remind_time']}, 教室: {course_info['classroom']}")
                else:
                    logger.info("今天没有需要提醒的课程")
                
                # 每分钟检查一次
                logger.info("等待下一次检查...")
                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"课程提醒任务出错: {str(e)}")
                import traceback
                logger.error(f"堆栈信息: {traceback.format_exc()}")
                await asyncio.sleep(60)  # 出错后等待1分钟再试