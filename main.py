import os
import time
import json
import asyncio
import logging
from urllib.parse import urlparse
from subprocess import run, CalledProcessError
from contextlib import suppress

import pyrogram
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, UsernameNotOccupied

import cv2
from moviepy import VideoFileClip

# --- 配置 ---
# 设置基本的日志记录
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 将所有用户可见的字符串放在一个地方，方便修改和国际化
MESSAGES = {
    "start": "👋 你好 **{mention}**!\n\n我是一个可以为你保存文件的机器人。\n你可以发送文件或受保护内容的链接给我。",
    "usage": """
**使用方法:**

- **直接发送文件**: 发送任何媒体文件（视频、图片、文档）。
- **发送链接**: 支持 `https://t.me/` 的帖子链接，以及 `http/https` 或 `magnet:` 的直接下载链接。

机器人会先向你确认，再开始下载。
""",
    "auth_failed": "❌ **鉴权失败**\n你没有权限使用此机器人。",
    "bot_not_in_chat": "❌ **设置错误**\n机器人尚未加入指定的保存频道/群组。请先将其加入并发送一条消息。",
    "waiting_for_tasks": "⏳ **任务繁忙**\n请等待其他任务执行完毕。",
    "invalid_link": "🔗 **链接无效**\n原因: `{error}`",
    "unsupported_chat": "🤷‍♂️ **类型不支持**\n暂不支持此类型的聊天链接。",
    "username_not_found": "🔍 **用户不存在**\n找不到此公开频道的用户名。",
    "no_media_in_link": "🤷‍♂️ **内容不支持**\n链接指向的消息不包含可下载的媒体。",
    "downloading": "📥 **正在下载...**",
    "uploading": "📤 **正在上传...**",
    "download_failed": "❌ **下载失败**\n错误: `{error}`",
    "upload_failed": "❌ **上传失败**\n错误: `{error}`",
    "unsupported_content": "🤷‍♂️ **内容不支持**\n此消息不包含可保存的媒体。",
    "file_not_found": "❌ **文件未找到**\n下载后，在本地未找到该文件。",
    "saved_success": "✅ **保存成功**\n文件名: `{filename}`\n大小: `{filesize}`",
    "progress_status": "{action}\n\n**进度**: {percent:.1f}% - {speed}/s\n**大小**: `{done} / {total}`",
    "confirm_download": "📋 **文件确认**\n\n**文件名**: `{filename}`\n**类型**: `{filetype}`\n**大小**: `{filesize}`\n\n你想要下载这个文件吗？",
    "task_cancelled": "🔴 **任务已取消**",
    "task_starting": "🚀 **任务即将开始...**",
    "unknown_size": "未知",
    "file_type_map": {
        "video": "视频", "photo": "图片", "document": "文档", "other": "其他",
        "text": "链接", "animation": "动画", "audio": "音频", "voice": "语音"
    }
}


class Config:
    """封装配置加载和访问的类"""

    def __init__(self, config_file='config.json'):
        if not os.path.exists(config_file):
            raise FileNotFoundError(f"{config_file} 未找到。请根据 config.example.json 创建。")
        with open(config_file, 'r') as f:
            self._data = json.load(f)

    def get(self, key, default=None):
        return os.environ.get(key) or self._data.get(key, default)

    def __post_init__(self):
        self.API_ID = int(self.get("ID"))
        self.API_HASH = self.get("HASH")
        self.BOT_TOKEN = self.get("TOKEN")
        self.ALLOWED_USERS = self.get("ALLOWED_USERS", "").split(",")
        self.SAVE_TO_CHAT_ID = int(self.get("SAVE_TO_CHAT_ID"))
        self.SAVE_TO_TOPIC_ID_DOCUMENT = int(self.get("SAVE_TO_TOPIC_ID_DOCUMENT", 0) or 0)
        self.SAVE_TO_TOPIC_ID_VIDEO = int(self.get("SAVE_TO_TOPIC_ID_VIDEO", 0) or 0)
        self.SAVE_TO_TOPIC_ID_PHOTO = int(self.get("SAVE_TO_TOPIC_ID_PHOTO", 0) or 0)

        if not all([self.API_ID, self.API_HASH, self.BOT_TOKEN, self.SAVE_TO_CHAT_ID]):
            raise ValueError("ID, HASH, TOKEN, 和 SAVE_TO_CHAT_ID 是必填项。")


class FileProcessor:
    """处理文件下载、上传和元数据提取的类"""

    def __init__(self, bot: Client, config: Config):
        self.bot = bot
        self.config = config
        self.download_dir = './downloads'
        os.makedirs(self.download_dir, exist_ok=True)
        # 用于存储与取消任务相关的临时文件路径
        self.cancellable_files = {}

    @staticmethod
    def sizeof_fmt(num, suffix='B'):
        if not isinstance(num, (int, float)):
            return MESSAGES["unknown_size"]
        for unit in ['', 'K', 'M', 'G', 'T', 'P']:
            if abs(num) < 1024.0:
                return f"{num:3.1f} {unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f} P{suffix}"

    async def _progress_callback(self, current, total, status_msg: Message, action: str):
        """直接更新状态消息，无需文件和线程"""
        try:
            now = time.time()
            if not hasattr(status_msg, 'last_update_time') or (now - status_msg.last_update_time) > 2:
                speed = (current - getattr(status_msg, 'last_update_bytes', 0)) / (
                        now - getattr(status_msg, 'last_update_time', now - 1))

                progress_text = MESSAGES['progress_status'].format(
                    action=action,
                    percent=(current * 100 / total),
                    speed=self.sizeof_fmt(speed),
                    done=self.sizeof_fmt(current),
                    total=self.sizeof_fmt(total)
                )

                # 更新按钮以允许取消
                await status_msg.edit_text(
                    progress_text,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔴 取消任务", callback_data=f"cancel_task:{status_msg.id}")
                    ]])
                )

                status_msg.last_update_time = now
                status_msg.last_update_bytes = current
        except MessageNotModified:
            pass  # 忽略未修改消息的错误
        except Exception as e:
            logger.warning(f"更新进度时出错: {e}")

    async def upload_file(self, file_path: str, status_msg: Message):
        """根据文件类型上传文件到指定聊天"""
        file_name = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        suffix = os.path.splitext(file_name)[1].lower()
        thumb_path = None

        try:
            progress_args = (status_msg, MESSAGES['uploading'])

            if suffix in ['.mp4', '.mkv', '.mov', '.flv', '.avi', '.wmv', '.webm', '.m4v']:
                duration, width, height, thumb_path = self._get_video_meta(file_path)
                await self.bot.send_video(
                    self.config.SAVE_TO_CHAT_ID, video=file_path,
                    duration=duration, width=width, height=height, thumb=thumb_path,
                    progress=self._progress_callback, progress_args=progress_args,
                    reply_to_message_id=self.config.SAVE_TO_TOPIC_ID_VIDEO or None
                )
            elif suffix in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
                await self.bot.send_photo(
                    self.config.SAVE_TO_CHAT_ID, photo=file_path,
                    reply_to_message_id=self.config.SAVE_TO_TOPIC_ID_PHOTO or None
                )
            else:
                await self.bot.send_document(
                    self.config.SAVE_TO_CHAT_ID, document=file_path,
                    progress=self._progress_callback, progress_args=progress_args,
                    reply_to_message_id=self.config.SAVE_TO_TOPIC_ID_DOCUMENT or None
                )

            await status_msg.edit_text(MESSAGES['saved_success'].format(
                filename=file_name, filesize=self.sizeof_fmt(file_size)
            ), reply_markup=None)
        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.error(f"上传失败: {e}", exc_info=True)
                await status_msg.edit_text(MESSAGES['upload_failed'].format(error=e), reply_markup=None)
            raise  # 重新抛出异常，以便任务处理程序可以捕获它
        finally:
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)

    @staticmethod
    def _get_video_meta(file_path: str):
        thumb_path = f"{os.path.splitext(file_path)[0]}.jpg"
        try:
            with VideoFileClip(file_path) as clip:
                duration = int(clip.duration)
                width, height = clip.size

            cap = cv2.VideoCapture(file_path)
            ret, frame = cap.read()
            if ret:
                cv2.imwrite(thumb_path, frame)
            else:
                thumb_path = None
            cap.release()
            return duration, width, height, thumb_path
        except Exception as e:
            logger.warning(f"无法提取视频元数据或生成缩略图: {e}")
            with suppress(FileNotFoundError):
                os.remove(thumb_path)
            return 0, 0, 0, None

    async def download_from_message(self, source_msg: Message, status_msg: Message) -> str | None:
        """从 Telegram 消息下载媒体"""
        try:
            progress_args = (status_msg, MESSAGES['downloading'])
            # 这里的 file_name 参数很重要，可以控制下载路径
            file_path = await self.bot.download_media(
                source_msg,
                progress=self._progress_callback,
                progress_args=progress_args
            )
            return file_path
        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.error(f"从消息下载失败: {e}", exc_info=True)
                await status_msg.edit_text(MESSAGES['download_failed'].format(error=e), reply_markup=None)
            raise  # 重新抛出异常

    def download_from_url(self, url: str) -> str | None:
        """使用 aria2c 从 URL 下载"""
        try:
            if url.startswith("magnet:?"):
                cmd = ["aria2c", url, "--dir", self.download_dir, "--summary-interval=1"]
                run(cmd, check=True)
                files = sorted([os.path.join(self.download_dir, f) for f in os.listdir(self.download_dir)],
                               key=os.path.getmtime, reverse=True)
                return files[0] if files else None
            else:
                parsed_path = urlparse(url).path
                filename = os.path.basename(parsed_path) or str(int(time.time()))
                output_path = os.path.join(self.download_dir, filename)
                cmd = ["aria2c", url, "--dir", self.download_dir, "-o", filename, "--summary-interval=1"]
                run(cmd, check=True)
                return output_path
        except CalledProcessError as e:
            raise IOError(f"Aria2c 错误: {e.stderr or e.stdout}")
        except Exception as e:
            raise IOError(f"未知下载错误: {e}")


class BotHandlers:
    """处理所有 Pyrogram 事件回调的类"""

    def __init__(self, bot: Client, config: Config, processor: FileProcessor):
        self.bot = bot
        self.config = config
        self.processor = processor
        self.active_tasks = {}  # 存储活动任务 {message_id: asyncio.Task}

    async def _is_authorized(self, message: Message) -> bool:
        """检查用户权限和机器人设置"""
        if str(message.from_user.id) not in self.config.ALLOWED_USERS:
            await message.reply_text(MESSAGES['auth_failed'])
            return False
        try:
            await self.bot.get_chat(self.config.SAVE_TO_CHAT_ID)
        except Exception:
            await message.reply_text(MESSAGES['bot_not_in_chat'])
            return False
        return True

    @staticmethod
    def get_message_type(msg: Message) -> str:
        try:
            msg.video.file_id
            return "Video"
        except:
            pass
        try:
            msg.photo.file_id
            return "Photo"
        except:
            pass
        try:
            msg.document.file_id
            return "Document"
        except:
            pass

        return "Other"

    @staticmethod
    def get_file_details(msg: Message) -> tuple[str, str, int | None]:
        """从消息中提取文件名、类型和大小"""
        file_type_key = BotHandlers.get_message_type(msg)
        file_type_str = MESSAGES["file_type_map"].get(file_type_key, "未知")

        media = msg.video or msg.photo or msg.document or msg.audio or msg.voice or msg.animation
        filename = getattr(media, 'file_name', "N/A")
        filesize = getattr(media, 'file_size', None)

        return filename, file_type_str, filesize

    async def on_start(self, _, message: Message):
        if not await self._is_authorized(message): return
        await message.reply_text(
            MESSAGES['start'].format(mention=message.from_user.mention) + '\n' + MESSAGES['usage'],
            quote=True
        )

    async def on_new_message(self, _, message: Message):
        """统一处理所有新消息（媒体和文本链接）"""
        if not await self._is_authorized(message): return

        filename, file_type, filesize = "N/A", "链接", None

        if message.media:
            filename, file_type, filesize = self.get_file_details(message)
        elif message.text:
            text = message.text.strip()
            if text.startswith("https://t.me/"):
                filename = "来自 Telegram 链接的文件"
                file_type = "链接"
                # Filesize is unknown until we fetch the message
            elif text.startswith(("http://", "https://", "magnet:?")):
                filename = os.path.basename(urlparse(text).path) or "来自链接的文件"
                file_type = "链接"
            else:
                return  # 忽略普通文本消息

        # 发送确认消息
        confirm_text = MESSAGES['confirm_download'].format(
            filename=filename,
            filetype=file_type,
            filesize=self.processor.sizeof_fmt(filesize)
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 下载", callback_data="confirm_download"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel_op")
        ]])
        await message.reply_text(confirm_text, reply_markup=keyboard, quote=True)

    async def on_callback_query(self, _, query: CallbackQuery):
        """处理内联按钮点击"""
        user_id = query.from_user.id
        if str(user_id) not in self.config.ALLOWED_USERS:
            await query.answer("你没有权限执行此操作。", show_alert=True)
            return

        data = query.data
        status_msg = query.message

        if data == "confirm_download":
            await query.answer("请求已确认，任务即将开始...")
            await status_msg.edit_text(MESSAGES['task_starting'], reply_markup=None)

            # 创建并存储任务
            task = asyncio.create_task(self._run_task(status_msg))
            self.active_tasks[status_msg.id] = task

        elif data == "cancel_op":
            await query.answer("操作已取消。")
            await status_msg.delete()

        elif data.startswith("cancel_task:"):
            task_id = int(data.split(":", 1)[1])
            if task_id in self.active_tasks:
                self.active_tasks[task_id].cancel()
                # 任务的 finally 块会处理字典清理
                await query.answer("正在取消任务...", show_alert=False)
            else:
                await query.answer("任务已完成或不存在。", show_alert=True)

    async def _run_task(self, status_msg: Message):
        """执行下载和上传的完整任务流程"""
        source_message = status_msg.reply_to_message
        task_id = status_msg.id
        file_path = None  # 将保存下载文件的路径

        try:
            message_to_download_from = None
            direct_download_url = None

            # --- 步骤 1: 确定下载源 ---
            if source_message.media:
                message_to_download_from = source_message
            elif source_message.text:
                url = source_message.text.strip()
                if url.startswith("https://t.me/"):
                    try:
                        datas = url.split("/")
                        if len(datas) < 5: raise ValueError("链接格式不完整。")

                        if url.startswith("https://t.me/c/"):
                            raise ValueError("暂不支持私人聊天 (c/) 链接。")
                        if url.startswith("https://t.me/b/"):
                            raise ValueError("暂不支持机器人 (b/) 链接。")

                        username = datas[3]
                        msg_id = int(datas[4].split("?")[0].split("-")[0])

                        fetched_msg = await self.bot.get_messages(username, msg_id)
                        if not fetched_msg or not fetched_msg.media:
                            await status_msg.edit_text(MESSAGES["no_media_in_link"], reply_markup=None)
                            return
                        message_to_download_from = fetched_msg

                    except UsernameNotOccupied:
                        await status_msg.edit_text(MESSAGES['username_not_found'], reply_markup=None)
                        return
                    except (ValueError, IndexError) as e:
                        await status_msg.edit_text(MESSAGES['invalid_link'].format(error=str(e)), reply_markup=None)
                        return
                    except Exception as e:
                        logger.error(f"获取 Telegram 消息时出错: {e}", exc_info=True)
                        await status_msg.edit_text(MESSAGES['download_failed'].format(error=str(e)), reply_markup=None)
                        return

                elif url.startswith(("http://", "https://", "magnet:?")):
                    direct_download_url = url
                else:
                    await status_msg.edit_text(MESSAGES['unsupported_content'], reply_markup=None)
                    return

            # --- 步骤 2: 执行下载 ---
            if message_to_download_from:
                file_path = await self.processor.download_from_message(message_to_download_from, status_msg)
            elif direct_download_url:
                loop = asyncio.get_event_loop()
                file_path = await loop.run_in_executor(None, self.processor.download_from_url, direct_download_url)
            else:
                await status_msg.edit_text(MESSAGES['unsupported_content'], reply_markup=None)
                return

            # --- 步骤 3: 上传文件 ---
            if not file_path or not os.path.exists(file_path):
                raise FileNotFoundError(MESSAGES['file_not_found'])

            self.processor.cancellable_files[task_id] = file_path
            await self.processor.upload_file(file_path, status_msg)

        except asyncio.CancelledError:
            await status_msg.edit_text(MESSAGES['task_cancelled'], reply_markup=None)
            logger.info(f"任务 {task_id} 已被用户取消。")
        except Exception as e:
            logger.error(f"任务 {task_id} 执行出错: {e}", exc_info=True)
            if not isinstance(e, asyncio.CancelledError):
                await status_msg.edit_text(MESSAGES['download_failed'].format(error=str(e)), reply_markup=None)
        finally:
            # --- 步骤 4: 清理 ---
            if task_id in self.processor.cancellable_files:
                path_to_clean = self.processor.cancellable_files.pop(task_id)
                with suppress(FileNotFoundError, IsADirectoryError):
                    os.remove(path_to_clean)
                    logger.info(f"已清理临时文件: {path_to_clean}")

            if task_id in self.active_tasks:
                del self.active_tasks[task_id]


def main():
    """主函数，用于设置和运行机器人"""
    try:
        config = Config()
        config.__post_init__()
    except (FileNotFoundError, ValueError) as e:
        logger.critical(f"配置错误: {e}")
        return

    bot = Client(
        'sessions/bot', api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN
    )

    processor = FileProcessor(bot, config)
    handlers = BotHandlers(bot, config, processor)

    # 注册处理器
    bot.add_handler(pyrogram.handlers.MessageHandler(handlers.on_start, filters.command(["start"]) & filters.private))
    bot.add_handler(pyrogram.handlers.MessageHandler(handlers.on_new_message, (
            filters.media | filters.text) & filters.private & ~filters.command(["start"])))
    bot.add_handler(pyrogram.handlers.CallbackQueryHandler(handlers.on_callback_query))

    logger.info("机器人正在启动...")
    bot.run()
    logger.info("机器人已停止。")


if __name__ == "__main__":
    main()
