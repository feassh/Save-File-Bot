import os
import time
import json
import asyncio
import logging
from abc import abstractmethod, ABC
from dataclasses import dataclass
from urllib.parse import urlparse
from subprocess import run, CalledProcessError
from contextlib import suppress

import pyrogram
import httpx
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
- **发送链接**: 支持 `https://t.me/` 的帖子链接、抖音分享链接，以及 `http/https` (包括 .m3u8) 或 `magnet:` 的直接下载链接。

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
    "ffmpeg_processing": "⏳ **正在合并 M3U8 视频流...**\n这可能需要一些时间，且期间无进度更新。",
    "ffmpeg_failed": "FFmpeg 错误: 请检查链接是否有效以及 FFmpeg 是否已正确安装。",
    "file_type_map": {
        "video": "视频", "photo": "图片", "document": "文档", "other": "其他",
        "text": "链接", "animation": "动画", "audio": "音频", "voice": "语音",
        "m3u8_video": "M3U8 视频"
    }
}


class Config:
    """封装配置加载和访问的类"""

    def __init__(self, config_file='config.json'):
        if not os.path.exists(config_file):
            raise FileNotFoundError(f"{config_file} 未找到。请根据 config.example.json 创建。")
        with open(config_file, 'r') as f:
            self._data = json.load(f)

        self.API_ID = int(self.get("ID"))
        self.API_HASH = self.get("HASH")
        self.BOT_TOKEN = self.get("TOKEN")
        self.ALLOWED_USERS = [user.strip() for user in self.get("ALLOWED_USERS", "").split(",") if user.strip()]
        self.SAVE_TO_CHAT_ID = int(self.get("SAVE_TO_CHAT_ID"))
        self.SAVE_TO_TOPIC_ID_DOCUMENT = int(self.get("SAVE_TO_TOPIC_ID_DOCUMENT", 0))
        self.SAVE_TO_TOPIC_ID_VIDEO = int(self.get("SAVE_TO_TOPIC_ID_VIDEO", 0))
        self.SAVE_TO_TOPIC_ID_PHOTO = int(self.get("SAVE_TO_TOPIC_ID_PHOTO", 0))

        if not all([self.API_ID, self.API_HASH, self.BOT_TOKEN, self.SAVE_TO_CHAT_ID]):
            raise ValueError("ID, HASH, TOKEN, 和 SAVE_TO_CHAT_ID 是必填项。")

    def get(self, key, default=None):
        return os.environ.get(key) or self._data.get(key, default)


class FileProcessor:
    """处理文件下载、上传和元数据提取的类"""

    def __init__(self, bot: Client, config: Config):
        self.bot = bot
        self.config = config
        self.download_dir = './downloads'
        os.makedirs(self.download_dir, exist_ok=True)
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
        try:
            now = time.time()
            if not hasattr(status_msg, 'last_update_time') or (now - status_msg.last_update_time) > 2:
                if total > 0:
                    speed = (current - getattr(status_msg, 'last_update_bytes', 0)) / (
                            now - getattr(status_msg, 'last_update_time', now - 1))
                    percent = current * 100 / total
                else:
                    speed = 0
                    percent = 0  # or some other indicator for unknown total

                progress_text = MESSAGES['progress_status'].format(
                    action=action,
                    percent=percent,
                    speed=self.sizeof_fmt(speed),
                    done=self.sizeof_fmt(current),
                    total=self.sizeof_fmt(total)
                )
                await status_msg.edit_text(
                    progress_text,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔴 取消任务", callback_data=f"cancel_task:{status_msg.id}")
                    ]])
                )
                status_msg.last_update_time = now
                status_msg.last_update_bytes = current
        except MessageNotModified:
            pass
        except Exception as e:
            logger.warning(f"更新进度时出错: {e}")

    async def upload_file(self, file_path: str, status_msg: Message):
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
                await status_msg.edit_text(MESSAGES['upload_failed'].format(error=str(e)), reply_markup=None)
            raise
        finally:
            if thumb_path and os.path.exists(thumb_path):
                os.remove(thumb_path)

    @staticmethod
    def _get_video_meta(file_path: str):
        thumb_path = f"{os.path.splitext(file_path)[0]}.jpg"
        try:
            # Use moviepy to get metadata, as it's already a dependency
            with VideoFileClip(file_path) as clip:
                duration = int(clip.duration)
                width, height = clip.size
                # Generate thumbnail from the first frame
                clip.save_frame(thumb_path, t=0)
            return duration, width, height, thumb_path
        except Exception as e:
            logger.warning(f"无法使用 MoviePy 提取元数据: {e}。尝试使用 OpenCV。")
            try:
                cap = cv2.VideoCapture(file_path)
                if cap.isOpened():
                    ret, frame = cap.read()
                    duration = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) / cap.get(cv2.CAP_PROP_FPS))
                    height, width, _ = frame.shape
                    if ret:
                        cv2.imwrite(thumb_path, frame)
                    else:
                        thumb_path = None
                    cap.release()
                else:
                    thumb_path = None
                return duration, width, height, thumb_path
            except Exception as e_cv:
                logger.warning(f"无法提取视频元数据或生成缩略图: {e_cv}")
                with suppress(FileNotFoundError):
                    if thumb_path and os.path.exists(thumb_path):
                        os.remove(thumb_path)
                return 0, 0, 0, None

    def download_from_url_sync(self, url: str) -> str | None:
        """使用 aria2c 从 URL 下载 (这是一个阻塞方法)"""
        try:
            if url.startswith("magnet:?"):
                cmd = ["aria2c", url, "--dir", self.download_dir, "--summary-interval=0", "--bt-stop-timeout=300"]
                run(cmd, check=True, capture_output=True, text=True)
                # 这部分逻辑比较脆弱，aria2下载后最好能明确知道文件名
                files = sorted([os.path.join(self.download_dir, f) for f in os.listdir(self.download_dir)],
                               key=os.path.getmtime, reverse=True)
                return files[0] if files else None
            else:
                parsed_path = urlparse(url).path
                filename = os.path.basename(parsed_path) or str(int(time.time()))
                output_path = os.path.join(self.download_dir, filename)
                cmd = ["aria2c", url, "--dir", self.download_dir, "-o", filename, "--summary-interval=0"]
                run(cmd, check=True, capture_output=True, text=True)
                return output_path
        except CalledProcessError as e:
            error_output = e.stderr or e.stdout
            logger.error(f"Aria2c 执行失败: {error_output}")
            raise IOError(f"Aria2c 错误: 检查日志获取更多信息")
        except Exception as e:
            logger.error(f"未知下载错误: {e}")
            raise IOError(f"未知下载错误: {e}")


@dataclass
class MessageProcessorResult:
    """处理消息的结果"""
    file_name: str | None = "N/A"
    file_size: int | None = None
    file_type: str | None = "链接"
    link: str | None = None


class BaseMessageProcessor(ABC):
    """处理消息的基类，现在包含下载逻辑"""

    def __init__(self, msg: Message, bot: Client):
        self._msg = msg
        self._bot = bot

    @abstractmethod
    async def get_file_detail(self) -> MessageProcessorResult:
        """异步获取文件元数据"""
        pass

    @abstractmethod
    async def download(self, file_processor: FileProcessor, status_msg: Message) -> str | None:
        """执行下载并返回文件路径"""
        pass


class NoneMessageProcessor(BaseMessageProcessor):
    async def get_file_detail(self) -> MessageProcessorResult:
        return MessageProcessorResult()

    async def download(self, file_processor: FileProcessor, status_msg: Message) -> str | None:
        await status_msg.edit_text(MESSAGES['unsupported_content'], reply_markup=None)
        return None


class TGMediaMessageProcessor(BaseMessageProcessor):
    def _get_message_type(self) -> str:
        if self._msg.video: return "video"
        if self._msg.photo: return "photo"
        if self._msg.document: return "document"
        return "other"

    async def get_file_detail(self) -> MessageProcessorResult:
        file_type_key = self._get_message_type()
        file_type_str = MESSAGES["file_type_map"].get(file_type_key, "未知")
        media = getattr(self._msg, file_type_key, None)
        if not media:
            return MessageProcessorResult(file_type="不支持的媒体")
        return MessageProcessorResult(
            file_name=getattr(media, 'file_name', "N/A"),
            file_size=getattr(media, 'file_size', None),
            file_type=file_type_str
        )

    async def download(self, file_processor: FileProcessor, status_msg: Message) -> str | None:
        try:
            progress_args = (status_msg, MESSAGES['downloading'])
            return await self._bot.download_media(
                self._msg,
                progress=file_processor._progress_callback,
                progress_args=progress_args
            )
        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.error(f"从消息下载失败: {e}", exc_info=True)
                await status_msg.edit_text(MESSAGES['download_failed'].format(error=e), reply_markup=None)
            raise


class TGLinkMessageProcessor(BaseMessageProcessor):
    async def get_file_detail(self) -> MessageProcessorResult:
        return MessageProcessorResult(
            file_name="来自 Telegram 链接的文件",
            file_type="链接"
        )

    async def download(self, file_processor: FileProcessor, status_msg: Message) -> str | None:
        url = self._msg.text.strip()
        try:
            datas = url.split("/")
            if len(datas) < 5 or not datas[4].isdigit(): raise ValueError("链接格式不完整。")
            if "/c/" in url: raise ValueError("暂不支持私人聊天 (c/) 链接。")
            if "/b/" in url: raise ValueError("暂不支持机器人 (b/) 链接。")

            username = datas[3]
            msg_id = int(datas[4].split("?")[0])

            fetched_msg = await self._bot.get_messages(username, msg_id)
            if not fetched_msg or not fetched_msg.media:
                await status_msg.edit_text(MESSAGES["no_media_in_link"], reply_markup=None)
                return None

            # 使用 TGMediaMessageProcessor 的下载逻辑
            return await TGMediaMessageProcessor(fetched_msg, self._bot).download(file_processor, status_msg)

        except UsernameNotOccupied:
            await status_msg.edit_text(MESSAGES['username_not_found'], reply_markup=None)
        except (ValueError, IndexError) as e:
            await status_msg.edit_text(MESSAGES['invalid_link'].format(error=str(e)), reply_markup=None)
        except Exception as e:
            logger.error(f"获取 Telegram 消息时出错: {e}", exc_info=True)
            await status_msg.edit_text(MESSAGES['download_failed'].format(error=str(e)), reply_markup=None)
        return None


class AriaMessageProcessor(BaseMessageProcessor):
    async def get_file_detail(self) -> MessageProcessorResult:
        text = self._msg.text.strip()
        return MessageProcessorResult(
            file_name=os.path.basename(urlparse(text).path) or "来自链接的文件",
            file_type="链接"
        )

    async def download(self, file_processor: FileProcessor, status_msg: Message) -> str | None:
        url = self._msg.text.strip()
        await status_msg.edit_text("⏳ **下载任务已提交给 Aria2c...**\n这可能需要一些时间，且期间无进度更新。")
        loop = asyncio.get_event_loop()
        try:
            # 在 executor 中运行阻塞的下载方法
            file_path = await loop.run_in_executor(None, file_processor.download_from_url_sync, url)
            return file_path
        except IOError as e:
            await status_msg.edit_text(MESSAGES['download_failed'].format(error=str(e)), reply_markup=None)
            return None


class M3U8MessageProcessor(BaseMessageProcessor):
    """处理 M3U8 视频流的处理器"""

    def _download_with_ffmpeg_sync(self, url: str, download_dir: str) -> str:
        """
        使用 FFmpeg 下载并合并 M3U8 流 (这是一个阻塞方法)。
        此版本使用重新编码，以确保最大的兼容性。
        需要系统上安装了 FFmpeg。
        """
        try:
            filename = os.path.basename(urlparse(url).path).split('.m3u8')[0] or str(int(time.time()))
            output_filename = f"{filename}.mp4"
            output_path = os.path.join(download_dir, output_filename)

            # -c:v libx264: 指定视频编码器为 libx264 (H.264)，这会重新编码视频以修复宽高比问题。
            # -preset veryfast: 编码速度预设。越快的文件越大，cpu占用越低。'veryfast' 是速度和质量的一个很好平衡点。
            # -crf 23: 控制视频质量。数字越小，质量越高，文件越大。23 是一个公认的良好默认值。
            # -c:a copy: 保持音频流为直接复制，以节省时间。
            # -bsf:a aac_adtstoasc: 依然需要，用于修复 AAC 音频在 MP4 容器中的兼容性。
            cmd = [
                "ffmpeg",
                "-i", url,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "copy",
                "-bsf:a", "aac_adtstoasc",
                output_path
            ]

            # 使用 subprocess.run 执行命令
            run(cmd, check=True, capture_output=True, text=True, encoding='utf-8')

            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise IOError("FFmpeg 执行完毕，但未生成有效的输出文件。")

            return output_path
        except CalledProcessError as e:
            error_output = e.stderr or e.stdout
            logger.error(f"FFmpeg 执行失败: {error_output}")
            raise IOError(MESSAGES['ffmpeg_failed'])
        except FileNotFoundError:
            logger.error("FFmpeg 命令未找到。请确保 FFmpeg 已安装并位于系统的 PATH 中。")
            raise IOError("FFmpeg 未安装。")
        except Exception as e:
            logger.error(f"未知的 FFmpeg 下载错误: {e}")
            raise IOError(f"未知下载错误: {e}")

    async def get_file_detail(self) -> MessageProcessorResult:
        text = self._msg.text.strip()
        filename = os.path.basename(urlparse(text).path).replace('.m3u8', '.mp4') or "M3U8 视频.mp4"
        return MessageProcessorResult(
            file_name=filename,
            file_type=MESSAGES["file_type_map"]["m3u8_video"],
            file_size=None  # M3U8 大小未知
        )

    async def download(self, file_processor: FileProcessor, status_msg: Message) -> str | None:
        url = self._msg.text.strip()
        await status_msg.edit_text(MESSAGES['ffmpeg_processing'], reply_markup=None)
        loop = asyncio.get_event_loop()
        try:
            file_path = await loop.run_in_executor(
                None,
                self._download_with_ffmpeg_sync,
                url,
                file_processor.download_dir
            )
            return file_path
        except IOError as e:
            await status_msg.edit_text(MESSAGES['download_failed'].format(error=str(e)), reply_markup=None)
            return None


class DouyinMessageProcessor(BaseMessageProcessor):
    def __init__(self, msg: Message, bot: Client):
        super().__init__(msg, bot)
        self._details = None  # 缓存获取到的详情

    async def get_file_detail(self) -> MessageProcessorResult:
        if self._details:
            return self._details

        text = self._msg.text.strip()
        # 使用异步 httpx
        async with httpx.AsyncClient() as client:
            try:
                res = await client.get("https://api.douyin.wtf/api?url=" + text, timeout=20)
                res.raise_for_status()
                data = res.json()
                if data and data.get('video_data'):
                    video_data = data['video_data']
                    self._details = MessageProcessorResult(
                        file_name=video_data.get('title', '抖音视频') + '.mp4',
                        file_size=video_data.get('size'),
                        file_type="抖音视频",
                        link=video_data.get('nwm_video_url')  # 无水印链接
                    )
                    return self._details
                else:
                    raise ValueError("API 返回数据格式无效")
            except (httpx.RequestError, ValueError, json.JSONDecodeError) as e:
                logger.error(f"请求抖音 API 失败: {e}")
                self._details = MessageProcessorResult(file_name="抖音链接解析失败", file_type="抖音")
                return self._details

    async def download(self, file_processor: FileProcessor, status_msg: Message) -> str | None:
        details = await self.get_file_detail()
        if not details or not details.link:
            await status_msg.edit_text(MESSAGES['download_failed'].format(error="无法解析抖音下载链接。"),
                                       reply_markup=None)
            return None

        # 复用 AriaMessageProcessor 的下载逻辑
        # 创建一个临时的 Message 对象来传递 URL
        temp_msg = Message(text=details.link, id=self._msg.id, chat=self._msg.chat)
        return await AriaMessageProcessor(temp_msg, self._bot).download(file_processor, status_msg)


class MessageProcessorFactory:
    @staticmethod
    def create_processor(msg: Message, bot: Client) -> BaseMessageProcessor:
        if msg.media:
            return TGMediaMessageProcessor(msg, bot)
        elif msg.text:
            text = msg.text.strip()
            # 简单的路由
            if "douyin.com" in text or "iesdouyin.com" in text:
                return DouyinMessageProcessor(msg, bot)
            elif ".m3u8" in text.lower():  # --- 新增 M3U8 路由 ---
                return M3U8MessageProcessor(msg, bot)
            elif text.startswith("https://t.me/"):
                return TGLinkMessageProcessor(msg, bot)
            elif text.startswith(("http://", "https://", "magnet:?")):
                return AriaMessageProcessor(msg, bot)
        # 对于不匹配的文本或不支持的消息类型，返回 NoneProcessor
        return NoneMessageProcessor(msg, bot)


class BotHandlers:
    """处理所有 Pyrogram 事件回调的类"""

    def __init__(self, bot: Client, config: Config, processor: FileProcessor):
        self.bot = bot
        self.config = config
        self.file_processor = processor
        self.active_tasks = {}

    async def _is_authorized(self, message: Message) -> bool:
        if self.config.ALLOWED_USERS and str(message.from_user.id) not in self.config.ALLOWED_USERS:
            await message.reply_text(MESSAGES['auth_failed'])
            return False
        try:
            await self.bot.get_chat(self.config.SAVE_TO_CHAT_ID)
        except Exception:
            await message.reply_text(MESSAGES['bot_not_in_chat'])
            return False
        return True

    async def on_start(self, _, message: Message):
        if not await self._is_authorized(message): return
        await message.reply_text(
            MESSAGES['start'].format(mention=message.from_user.mention) + '\n' + MESSAGES['usage'],
            quote=True
        )

    async def on_new_message(self, _, message: Message):
        if not await self._is_authorized(message): return

        message_processor = MessageProcessorFactory.create_processor(message, self.bot)
        if isinstance(message_processor, NoneMessageProcessor):
            # 只有在私聊中才回复用法提示，避免在群组中对普通消息响应
            if message.chat.type == pyrogram.enums.ChatType.PRIVATE:
                await message.reply_text(MESSAGES['usage'], quote=True)
            return

        file_detail = await message_processor.get_file_detail()

        confirm_text = MESSAGES['confirm_download'].format(
            filename=file_detail.file_name,
            filetype=file_detail.file_type,
            filesize=self.file_processor.sizeof_fmt(file_detail.file_size)
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 下载", callback_data="confirm_download"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel_op")
        ]])
        await message.reply_text(confirm_text, reply_markup=keyboard, quote=True)

    async def on_callback_query(self, _, query: CallbackQuery):
        user_id = query.from_user.id
        if self.config.ALLOWED_USERS and str(user_id) not in self.config.ALLOWED_USERS:
            await query.answer("你没有权限执行此操作。", show_alert=True)
            return

        data = query.data
        status_msg = query.message

        if data == "confirm_download":
            if status_msg.id in self.active_tasks:
                await query.answer("此任务已在进行中，请勿重复点击。", show_alert=True)
                return
            await query.answer("请求已确认，任务即将开始...")
            await status_msg.edit_text(MESSAGES['task_starting'], reply_markup=None)
            task = asyncio.create_task(self._run_task(status_msg))
            self.active_tasks[status_msg.id] = task

        elif data == "cancel_op":
            await query.answer("操作已取消。")
            await status_msg.delete()

        elif data.startswith("cancel_task:"):
            task_id = int(data.split(":", 1)[1])
            if task_id in self.active_tasks:
                self.active_tasks[task_id].cancel()
                await query.answer("正在取消任务...", show_alert=False)
            else:
                await query.answer("任务已完成或不存在。", show_alert=True)

    async def _run_task(self, status_msg: Message):
        source_message = status_msg.reply_to_message
        if not source_message:
            await status_msg.edit_text("❌ **错误**\n无法找到原始消息，任务无法执行。")
            return
        task_id = status_msg.id
        file_path = None

        # 创建对应的处理器来处理下载逻辑
        processor = MessageProcessorFactory.create_processor(source_message, self.bot)

        try:
            # 步骤 1: 下载
            # 处理器的 download 方法负责所有特定于源的逻辑
            file_path = await processor.download(self.file_processor, status_msg)

            if not file_path or not os.path.exists(file_path):
                if not status_msg.text.startswith(MESSAGES['download_failed'].split('\n')[0]):
                    await status_msg.edit_text(MESSAGES['file_not_found'], reply_markup=None)
                return

            self.file_processor.cancellable_files[task_id] = file_path

            # 步骤 2: 上传
            await self.file_processor.upload_file(file_path, status_msg)

        except asyncio.CancelledError:
            await status_msg.edit_text(MESSAGES['task_cancelled'], reply_markup=None)
            logger.info(f"任务 {task_id} 已被用户取消。")
        except Exception as e:
            if not isinstance(e, asyncio.CancelledError):
                logger.error(f"任务 {task_id} 执行出错: {e}", exc_info=True)
                # 避免重复发送失败消息
                if hasattr(status_msg, 'text') and status_msg.text:
                    current_text = status_msg.text
                    if MESSAGES['download_failed'].split('\n')[0] not in current_text:
                        await status_msg.edit_text(MESSAGES['download_failed'].format(error=str(e)), reply_markup=None)
        finally:
            # 步骤 3: 清理
            if task_id in self.file_processor.cancellable_files:
                path_to_clean = self.file_processor.cancellable_files.pop(task_id)
                with suppress(FileNotFoundError, IsADirectoryError):
                    if os.path.isdir(path_to_clean):
                        import shutil
                        shutil.rmtree(path_to_clean)
                    else:
                        os.remove(path_to_clean)
                    logger.info(f"已清理临时文件/目录: {path_to_clean}")

            if task_id in self.active_tasks:
                del self.active_tasks[task_id]


def main():
    """
    主函数，用于设置和运行机器人。
    请注意: 新增的 M3U8 下载功能需要您的系统上安装了 FFmpeg。
    """
    try:
        config = Config()
    except (FileNotFoundError, ValueError) as e:
        logger.critical(f"配置错误: {e}")
        return

    bot = Client('sessions/bot', api_id=config.API_ID, api_hash=config.API_HASH, bot_token=config.BOT_TOKEN)

    file_processor = FileProcessor(bot, config)
    handlers = BotHandlers(bot, config, file_processor)

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
