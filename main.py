import os
import time
import json
import asyncio
import logging
from urllib.parse import urlparse
from subprocess import run, CalledProcessError

import pyrogram
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import UsernameNotOccupied

import cv2
from moviepy import VideoFileClip

# --- 配置 ---
# 设置基本的日志记录
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 将所有用户可见的字符串放在一个地方，方便修改和国际化
MESSAGES = {
    "start": "👋 你好 **{mention}**!\n\n我是一个可以为你保存文件的机器人。\n你可以发送文件或受保护内容的链接给我。",
    "usage_prompt": "使用方法请看下面的说明：",
    "usage": """
**对于公开频道的帖子**
`直接发送帖子链接即可。`

**对于私有频道的帖子**
`请先将我（机器人）或运行此机器人的用户账号加入该频道，然后发送帖子链接。`

**如何获取链接？**
`转发消息到 @get_link_bot 即可获得原始消息链接。`

**⚠️ 注意:**
首次将机器人拉入群组后，请先在群组发送任意一条消息，否则机器人会不识别 ChatID。
""",
    "waiting_for_tasks": "请等待其他任务执行完毕。",
    "auth_failed": "鉴权失败，你无权使用此机器人。",
    "bot_not_in_chat": "机器人尚未加入指定的保存频道/群组，或没有发言。请先将其加入并发送一条消息。",
    "invalid_link": "链接格式错误，无法解析。",
    "unsupported_chat": "暂不支持此类型的聊天链接。",
    "username_not_found": "找不到这个用户名。",
    "downloading": "📥 **正在下载...**",
    "uploading": "📤 **正在上传...**",
    "download_failed": "❌ 下载失败: {error}",
    "upload_failed": "❌ 上传失败: {error}",
    "unsupported_content": "🤷‍♂️ 暂不支持保存该类型的内容。",
    "file_not_found": "下载失败，未在本地找到文件。",
    "saved_success": "✅ 已保存: `{filename}` ({filesize})",
    "progress_status": "{percent:.1f}% - {speed}/s\n`{done}/{total}`",
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
        self.SAVE_TO_TOPIC_ID_DOCUMENT = int(self.get("SAVE_TO_TOPIC_ID_DOCUMENT"))
        self.SAVE_TO_TOPIC_ID_VIDEO = int(self.get("SAVE_TO_TOPIC_ID_VIDEO"))
        self.SAVE_TO_TOPIC_ID_PHOTO = int(self.get("SAVE_TO_TOPIC_ID_PHOTO"))

        if not all([self.API_ID, self.API_HASH, self.BOT_TOKEN, self.SAVE_TO_CHAT_ID]):
            raise ValueError("ID, HASH, TOKEN, 和 SAVE_TO_CHAT_ID 是必填项。")


class FileProcessor:
    """处理文件下载、上传和元数据提取的类"""

    def __init__(self, bot: Client, config: Config):
        self.bot = bot
        self.config = config
        self.download_dir = './downloads'
        os.makedirs(self.download_dir, exist_ok=True)

    @staticmethod
    def sizeof_fmt(num, suffix='B'):
        for unit in ['', 'K', 'M', 'G', 'T', 'P']:
            if abs(num) < 1024.0:
                return f"{num:3.1f} {unit}{suffix}"
            num /= 1024.0
        return f"{num:.1f} P{suffix}"

    async def _progress_callback(self, current, total, status_msg: Message, action: str):
        """直接更新状态消息，无需文件和线程"""
        try:
            now = time.time()
            # 限制更新频率
            if hasattr(status_msg, 'last_update_time') and (now - status_msg.last_update_time) < 2:
                return

            speed = (current - getattr(status_msg, 'last_update_bytes', 0)) / (
                    now - getattr(status_msg, 'last_update_time', now - 1))

            progress_text = MESSAGES['progress_status'].format(
                percent=(current * 100 / total),
                speed=self.sizeof_fmt(speed),
                done=self.sizeof_fmt(current),
                total=self.sizeof_fmt(total)
            )

            await status_msg.edit_text(f"{action}\n{progress_text}")

            # 在消息对象上存储状态以供下次调用
            status_msg.last_update_time = now
            status_msg.last_update_bytes = current
        except Exception as e:
            logger.warning(f"更新进度时出错: {e}")

    async def upload_file(self, user_message: Message, file_path: str, status_msg: Message):
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
                    self.config.SAVE_TO_CHAT_ID,
                    video=file_path,
                    duration=duration, width=width, height=height,
                    thumb=thumb_path,
                    progress=self._progress_callback, progress_args=progress_args,
                    reply_to_message_id=self.config.SAVE_TO_TOPIC_ID_VIDEO
                )
            elif suffix in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
                await self.bot.send_photo(
                    self.config.SAVE_TO_CHAT_ID,
                    photo=file_path,
                    reply_to_message_id=self.config.SAVE_TO_TOPIC_ID_PHOTO
                )
            else:
                await self.bot.send_document(
                    self.config.SAVE_TO_CHAT_ID,
                    document=file_path,
                    progress=self._progress_callback, progress_args=progress_args,
                    reply_to_message_id=self.config.SAVE_TO_TOPIC_ID_DOCUMENT
                )

            await status_msg.edit_text(MESSAGES['saved_success'].format(
                filename=file_name, filesize=self.sizeof_fmt(file_size)
            ))
        except Exception as e:
            logger.error(f"上传失败: {e}", exc_info=True)
            await status_msg.edit_text(MESSAGES['upload_failed'].format(error=e))
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)

    def _get_video_meta(self, file_path: str):
        """提取视频元数据并生成缩略图"""
        thumb_path = f"{file_path}.jpg"
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
            if os.path.exists(thumb_path):
                os.remove(thumb_path)
            return 0, 0, 0, None

    async def download_from_message(self, source_msg: Message, status_msg: Message) -> str | None:
        """从 Telegram 消息下载媒体"""
        file_path = None
        try:
            progress_args = (status_msg, MESSAGES['downloading'])
            file_path = await self.bot.download_media(
                source_msg,
                progress=self._progress_callback,
                progress_args=progress_args
            )
            return file_path
        except Exception as e:
            logger.error(f"从消息下载失败: {e}", exc_info=True)
            await status_msg.edit_text(MESSAGES['download_failed'].format(error=e))
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
            return None

    def download_from_url(self, url: str) -> str | None:
        """使用 aria2c 从 URL 下载"""
        try:
            if url.startswith("magnet:?"):
                # 对于磁力链接，我们无法预知文件名，让aria2下载到目录即可
                cmd = ["aria2c", url, "--dir", self.download_dir, "--summary-interval=1"]
                run(cmd, check=True)
                # 查找最新的文件
                files = sorted(
                    [os.path.join(self.download_dir, f) for f in os.listdir(self.download_dir)],
                    key=os.path.getmtime,
                    reverse=True
                )
                if files: return files[0]
                return None
            else:
                # 对于HTTP链接，我们可以指定文件名
                parsed_path = urlparse(url).path
                filename = os.path.basename(parsed_path) or str(int(time.time()))
                output_path = os.path.join(self.download_dir, filename)
                cmd = ["aria2c", url, "--dir", self.download_dir, "-o", filename, "--summary-interval=1"]
                run(cmd, check=True)
                return output_path
        except CalledProcessError as e:
            logger.error(f"Aria2c 执行失败: {e}")
            raise IOError(f"Aria2c 错误: {e.stderr or e.stdout}")
        except Exception as e:
            logger.error(f"URL 下载失败: {e}", exc_info=True)
            raise IOError(f"未知下载错误: {e}")


class BotHandlers:
    """处理所有 Pyrogram 事件回调的类"""

    def __init__(self, bot: Client, config: Config, processor: FileProcessor):
        self.bot = bot
        self.config = config
        self.processor = processor
        self.active_tasks = 0  # 简单的并发控制

    async def _is_authorized(self, message: Message) -> bool:
        """检查用户权限和机器人设置"""
        if str(message.from_user.id) not in self.config.ALLOWED_USERS:
            await message.reply_text(MESSAGES['auth_failed'])
            return False

        try:
            # 检查机器人是否在目标频道
            await self.bot.get_chat(self.config.SAVE_TO_CHAT_ID)
        except Exception:
            await message.reply_text(MESSAGES['bot_not_in_chat'])
            return False

        if self.active_tasks >= 4:
            await message.reply_text(MESSAGES['waiting_for_tasks'])
            return False

        return True

    async def on_start(self, _, message: Message):
        if not await self._is_authorized(message):
            return
        await message.reply_text(
            MESSAGES['start'].format(mention=message.from_user.mention),
            quote=True
        )
        await message.reply_text(MESSAGES['usage'], quote=False)

    async def on_media(self, _, message: Message):
        if not await self._is_authorized(message):
            return

        self.active_tasks += 1
        status_msg = await message.reply_text(MESSAGES['downloading'], quote=True)
        try:
            file_path = await self.processor.download_from_message(message, status_msg)
            if file_path:
                await self.processor.upload_file(message, file_path, status_msg)
        finally:
            self.active_tasks -= 1

    async def on_text(self, _, message: Message):
        if not await self._is_authorized(message):
            return

        text = message.text.strip()
        if text.startswith("https://t.me/"):
            await self._handle_tg_link(message)
        elif text.startswith(("http://", "https://", "magnet:?")):
            await self._handle_direct_link(message)

    async def _handle_tg_link(self, message: Message):
        text = message.text.strip()
        try:
            parts = text.split("/")
            if len(parts) < 5:
                raise ValueError("链接格式不正确")

            username = parts[3]
            msg_ids_str = parts[-1].split("?")[0]

            if text.startswith("https://t.me/c/"):
                await message.reply_text(MESSAGES['unsupported_chat'])
                return

            # TODO: 批量下载逻辑可以进一步实现
            msg_id = int(msg_ids_str.split("-")[0])

        except (ValueError, IndexError) as e:
            await message.reply_text(f"{MESSAGES['invalid_link']}: {e}")
            return

        self.active_tasks += 1
        status_msg = await message.reply_text("正在处理链接...", quote=True)
        try:
            source_msg = await self.bot.get_messages(username, msg_id)
            if not source_msg.media:
                await status_msg.edit_text(MESSAGES['unsupported_content'])
                return

            await status_msg.edit_text(MESSAGES['downloading'])
            file_path = await self.processor.download_from_message(source_msg, status_msg)
            if file_path:
                await self.processor.upload_file(message, file_path, status_msg)

        except UsernameNotOccupied:
            await status_msg.edit_text(MESSAGES['username_not_found'])
        except Exception as e:
            logger.error(f"处理Telegram链接时出错: {e}", exc_info=True)
            await status_msg.edit_text(str(e))
        finally:
            self.active_tasks -= 1

    async def _handle_direct_link(self, message: Message):
        self.active_tasks += 1
        status_msg = await message.reply_text(MESSAGES['downloading'], quote=True)
        try:
            # 在单独的线程中运行阻塞的下载任务
            loop = asyncio.get_event_loop()
            file_path = await loop.run_in_executor(None, self.processor.download_from_url, message.text.strip())

            if file_path and os.path.exists(file_path):
                await self.processor.upload_file(message, file_path, status_msg)
            else:
                await status_msg.edit_text(MESSAGES['file_not_found'])
        except Exception as e:
            logger.error(f"处理直接链接时出错: {e}", exc_info=True)
            await status_msg.edit_text(MESSAGES['download_failed'].format(error=e))
        finally:
            self.active_tasks -= 1


def main():
    """主函数，用于设置和运行机器人"""
    try:
        config = Config()
        config.__post_init__()  # Manually call post_init after instantiation
    except (FileNotFoundError, ValueError) as e:
        logger.critical(f"配置错误: {e}")
        return

    bot = Client(
        'sessions/bot',
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN
    )

    processor = FileProcessor(bot, config)
    handlers = BotHandlers(bot, config, processor)

    # 注册处理器
    bot.add_handler(pyrogram.handlers.MessageHandler(handlers.on_start, filters.command(["start"]) & filters.private))
    bot.add_handler(pyrogram.handlers.MessageHandler(handlers.on_media, (
            filters.photo | filters.video | filters.document) & filters.private))
    bot.add_handler(pyrogram.handlers.MessageHandler(handlers.on_text, filters.text & filters.private))

    logger.info("机器人正在启动...")
    bot.run()
    logger.info("机器人已停止。")


if __name__ == "__main__":
    main()
