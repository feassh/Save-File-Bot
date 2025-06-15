import pyrogram
from pyrogram import Client, filters
from pyrogram.errors import UsernameNotOccupied
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import time
import os
import threading
import json

with open('config.json', 'r') as f:
    DATA = json.load(f)


def getenv(var):
    return os.environ.get(var) or DATA.get(var, None)


api_id = int(getenv("ID"))
api_hash = getenv("HASH")
bot_token = getenv("TOKEN")
allowed_users = getenv("ALLOWED_USERS").split(",")
save_to_chat_id = int(getenv("SAVE_TO_CHAT_ID"))
save_to_topic_id_document = int(getenv("SAVE_TO_TOPIC_ID_DOCUMENT"))
save_to_topic_id_video = int(getenv("SAVE_TO_TOPIC_ID_VIDEO"))
save_to_topic_id_photo = int(getenv("SAVE_TO_TOPIC_ID_PHOTO"))

os.makedirs('./sessions', exist_ok=True)

bot = Client('./sessions/bot', api_id=api_id, api_hash=api_hash, bot_token=bot_token)


def is_allowed_user(message: pyrogram.types.messages_and_media.message.Message):
    if str(message.from_user.id) in allowed_users:
        return True

    bot.send_message(message.chat.id, "鉴权失败", reply_to_message_id=message.id)
    return False


# 下载状态函数
def downstatus(statusfile, message):
    while True:
        if os.path.exists(statusfile):
            break

    time.sleep(3)
    while os.path.exists(statusfile):
        with open(statusfile, "r") as downread:
            txt = downread.read()
        try:
            bot.edit_message_text(message.chat.id, message.id, f"__正在下载__ : **{txt}**")
            time.sleep(10)
        except:
            time.sleep(5)


# 上传状态
def upstatus(statusfile, message):
    while True:
        if os.path.exists(statusfile):
            break

    time.sleep(3)
    while os.path.exists(statusfile):
        with open(statusfile, "r") as upread:
            txt = upread.read()
        try:
            bot.edit_message_text(message.chat.id, message.id, f"__正在上传__ : **{txt}**")
            time.sleep(10)
        except:
            time.sleep(5)


# 进度写入函数
def progress(current, total, message, type):
    with open(f'{message.id}{type}status.txt', "w") as fileup:
        fileup.write(f"{current * 100 / total:.1f}%")


# 开始命令处理函数
@bot.on_message(filters.command(["start"]))
def send_start(client: pyrogram.client.Client, message: pyrogram.types.messages_and_media.message.Message):
    if not is_allowed_user(message):
        return

    bot.send_message(message.chat.id,
                     f"__👋 Hi **{message.from_user.mention}**, I am Save File Bot\n你可以发送文件或受限内容的链接让我保存__\n\n{USAGE}",
                     reply_markup=InlineKeyboardMarkup(
                         [[InlineKeyboardButton("🌐 源码仓库", url="https://github.com/feassh/Save-File-Bot")]]),
                     reply_to_message_id=message.id)


# 收到视频或图片执行
@bot.on_message(filters.photo | filters.video | filters.document)
def save_media(client, message):
    if not is_allowed_user(message):
        return
    handle_private(message)


# 收到“https://t.me/***”后执行
@bot.on_message(filters.text)
def save(client: pyrogram.client.Client, message: pyrogram.types.messages_and_media.message.Message):
    if not is_allowed_user(message):
        return

    # 收到消息
    if "https://t.me/" in message.text:
        url = message.text.strip()
        if url == '': return

        try:
            datas = url.split("/")
            msgid = int(datas[-1].replace("?single", "").split("-")[0])
        except Exception as e:
            bot.send_message(message.chat.id, f"**错误** : __{e}__", reply_to_message_id=message.id)
            return

        try:
            if len(datas) < 4: return
            # 私人的聊天
            if "https://t.me/c/" in url:
                bot.send_message(message.chat.id, f"**错误** : 暂不支持私人聊天", reply_to_message_id=message.id)
            # 机器人的聊天
            elif "https://t.me/b/" in url:
                bot.send_message(message.chat.id, f"**错误** : 暂不支持机器人聊天", reply_to_message_id=message.id)
            # 公开的聊天
            else:
                username = datas[3]

                try:
                    msg = bot.get_messages(username, msgid)
                except UsernameNotOccupied:
                    bot.send_message(message.chat.id, f"**不存在这个用户名**", reply_to_message_id=message.id)
                    return

                try:
                    handle_private(message, msg=msg)
                except Exception as e:
                    bot.send_message(message.chat.id, f"**错误** : __{e}__", reply_to_message_id=message.id)
        except Exception as e:
            bot.send_message(message.chat.id, f"**错误** : __{e}__", reply_to_message_id=message.id)
    else:
        return


# 处理私人的聊天
def handle_private(message: pyrogram.types.messages_and_media.message.Message, msg=None):
    msg_user = message
    if msg is not None:
        msg_source = msg
    else:
        msg_source = message

    msg_type = get_message_type(msg_source)

    if "Other" == msg_type:
        bot.send_message(msg_user.chat.id, f"**错误** : 暂不支持保存该类型的内容", entities=msg.entities,
                         reply_to_message_id=msg_user.id)
        return

    smsg = bot.send_message(msg_user.chat.id, '__下载中__', reply_to_message_id=msg_user.id)
    dosta = threading.Thread(target=lambda: downstatus(f'{msg_source.id}downstatus.txt', smsg), daemon=True)
    dosta.start()
    file = bot.download_media(msg_source, progress=progress, progress_args=[msg_source, "down"])
    os.remove(f'{msg_source.id}downstatus.txt')

    upsta = threading.Thread(target=lambda: upstatus(f'{msg_source.id}upstatus.txt', smsg), daemon=True)
    upsta.start()

    if "Video" == msg_type:
        try:
            thumb = bot.download_media(msg_source.video.thumbs[0].file_id)
        except:
            thumb = None

        # 发送到指定聊天
        bot.send_video(save_to_chat_id, file, duration=msg_source.video.duration, width=msg_source.video.width,
                       height=msg_source.video.height, has_spoiler=True, thumb=thumb, progress=progress,
                       progress_args=[msg_source, "up"],
                       reply_to_message_id=save_to_topic_id_video)
        if thumb is not None: os.remove(thumb)
    elif "Photo" == msg_type:
        # 发送到指定聊天
        bot.send_photo(save_to_chat_id, file, has_spoiler=True,
                       reply_to_message_id=save_to_topic_id_photo)
    elif "Document" == msg_type:
        try:
            thumb = bot.download_media(msg_source.document.thumbs[0].file_id)
        except:
            thumb = None

        # 发送到指定聊天
        bot.send_document(save_to_chat_id, file, thumb=thumb, caption=msg_source.caption,
                          caption_entities=msg_source.caption_entities,
                          progress=progress,
                          progress_args=[msg_source, "up"], reply_to_message_id=save_to_topic_id_document)
        if thumb is not None: os.remove(thumb)

    os.remove(file)
    if os.path.exists(f'{msg_source.id}upstatus.txt'): os.remove(f'{msg_source.id}upstatus.txt')
    # 删除用户发送的聊天
    bot.delete_messages(msg_user.chat.id, [smsg.id])


# 获取消息类型
def get_message_type(msg: pyrogram.types.messages_and_media.message.Message):
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


USAGE = """**对于公开聊天的文件**

__只需发送相应链接__

**对于非公开聊天的文件**

__首先发送聊天的邀请链接 (如果当前提供会话的帐户已经是聊天成员，则不需要发送邀请链接)
然后发送链接__

**对于机器人聊天**

__发送带有“/b/”的链接、机器人的用户名和消息 ID，你可能需要安装一些非官方客户端来获取如下所示的 ID__

```
https://t.me/b/botusername/4321
```

**如果你需要一次保存多个受限文件**

__发送公共/私人帖子链接，如上所述，使用格式“发件人 - 收件人”发送多条消息，如下所示__

```
https://t.me/xxxx/1001-1010

https://t.me/c/xxxx/101 - 120
```

__最好在中间加上空格__

__⚠️注意：__首次将机器人拉入群组后，请先在群组发送任意一条消息，否则机器人会不识别 ChatID
"""

# 启动机器人（进入无限轮询）
print("Bot is running...")
bot.run()

# from telethon import TelegramClient, events, types
# from telethon.errors import UsernameNotOccupiedError
# import os
# import json
# import asyncio
#
# with open('config.json', 'r') as f:
#     DATA = json.load(f)
#
#
# def getenv(var):
#     return os.environ.get(var) or DATA.get(var, None)
#
#
# api_id = int(getenv("ID"))
# api_hash = getenv("HASH")
# bot_token = getenv("TOKEN")
# allowed_users = getenv("ALLOWED_USERS").split(",")
# save_to_chat_id = int(getenv("SAVE_TO_CHAT_ID"))
# save_to_topic_id_document = int(getenv("SAVE_TO_TOPIC_ID_DOCUMENT"))
# save_to_topic_id_video = int(getenv("SAVE_TO_TOPIC_ID_VIDEO"))
# save_to_topic_id_photo = int(getenv("SAVE_TO_TOPIC_ID_PHOTO"))
#
# os.makedirs('./sessions', exist_ok=True)
#
# bot = TelegramClient('./sessions/bot', api_id, api_hash).start(bot_token=bot_token)
#
#
# # 状态文件管理
# def progress_write(current, total, message_id, type_):
#     with open(f'{message_id}{type_}status.txt', 'w') as f:
#         f.write(f"{current * 100 / total:.1f}%")
#
#
# async def watch_status(filepath, message, action="下载"):
#     while not os.path.exists(filepath):
#         await asyncio.sleep(1)
#     while os.path.exists(filepath):
#         with open(filepath) as f:
#             txt = f.read()
#         try:
#             await bot.edit_message(message.chat_id, message.id, f"__正在{action}__ : **{txt}**")
#         except:
#             pass
#         await asyncio.sleep(5)
#
#
# # 认证
# async def is_allowed_user(event):
#     if str(event.sender_id) in allowed_users:
#         return True
#     await event.reply("鉴权失败")
#     return False
#
#
# @bot.on(events.NewMessage(pattern='/start'))
# async def start(event):
#     if not await is_allowed_user(event):
#         return
#     btn = [
#         [types.KeyboardButtonUrl(text="🌐 源码仓库", url="https://github.com/feassh/Save-File-Bot")]
#     ]
#     await event.respond(
#         f"__👋 Hi {event.sender.first_name}, I am Save File Bot\n你可以发送文件或受限内容的链接让我保存__\n\n{USAGE}",
#         buttons=btn
#     )
#
#
# @bot.on(events.NewMessage(func=lambda e: e.file or e.photo))
# async def handle_file(event):
#     if not await is_allowed_user(event): return
#     await handle_private(event)
#
#
# @bot.on(events.NewMessage())
# async def handle_text(event):
#     if not await is_allowed_user(event): return
#
#     text = event.raw_text.strip()
#     if "https://t.me/" not in text:
#         # await event.reply("**错误** : 不支持的消息链接")
#         return
#
#     try:
#         datas = text.split("/")
#         msgid = int(datas[-1].replace("?single", "").split("-")[0])
#         username = datas[3]
#
#         if "https://t.me/c/" in text:
#             await event.reply("**错误** : 暂不支持私人聊天")
#             return
#         elif "https://t.me/b/" in text:
#             await event.reply("**错误** : 暂不支持机器人聊天")
#             return
#
#         try:
#             msg = await bot.get_messages(username, ids=msgid)
#             await handle_private(event, msg)
#         except UsernameNotOccupiedError:
#             await event.reply("**不存在这个用户名**")
#     except Exception as e:
#         await event.reply(f"**错误** : __{e}__")
#
#
# async def handle_private(event, msg=None):
#     msg_source = msg or event.message
#
#     msg_type = get_msg_type(msg_source)
#     if msg_type == "Other":
#         await event.reply("**错误** : 暂不支持保存该类型的内容")
#         return
#
#     smsg = await event.reply("__下载中__")
#     status_file = f"{msg_source.id}downstatus.txt"
#
#     async def progress_callback(current, total):
#         progress_write(current, total, msg_source.id, "down")
#
#     async def progress_up_callback(current, total):
#         progress_write(current, total, msg_source.id, "up")
#
#     asyncio.create_task(watch_status(status_file, smsg, action="下载"))
#     file = await bot.download_media(msg_source, file=None, progress_callback=progress_callback)
#     os.remove(status_file)
#
#     up_file = f"{msg_source.id}upstatus.txt"
#     asyncio.create_task(watch_status(up_file, smsg, action="上传"))
#
#     caption = ""  # msg_source.message or ""
#
#     if msg_type == "Video":
#         await bot.send_file(save_to_chat_id, file, caption=caption, video_note=False, reply_to=save_to_topic_id_video,
#                             spoiler=True, supports_streaming=True,
#                             progress_callback=progress_up_callback)
#     elif msg_type == "Photo":
#         await bot.send_file(save_to_chat_id, file, caption=caption, reply_to=save_to_topic_id_photo, spoiler=True,
#                             progress_callback=progress_up_callback)
#     elif msg_type == "Document":
#         await bot.send_file(save_to_chat_id, file, caption=caption, reply_to=save_to_topic_id_document,
#                             progress_callback=progress_up_callback)
#
#     if os.path.exists(up_file): os.remove(up_file)
#     os.remove(file)
#     await smsg.delete()
#
#
# def get_msg_type(msg):
#     if isinstance(msg.media, types.MessageMediaPhoto):
#         return "Photo"
#     elif isinstance(msg.media, types.MessageMediaDocument):
#         mime = msg.document.mime_type or ""
#         if mime.startswith("video/"):
#             return "Video"
#         elif mime.startswith("image/"):
#             return "Photo"  # 也当成图片处理
#         else:
#             return "Document"
#
#
# USAGE = '''**对于公开聊天的文件**
#
# __只需发送相应链接__
#
# **对于非公开聊天的文件**
#
# __首先发送聊天的邀请链接 (如果当前提供会话的帐户已经是聊天成员，则不需要发送邀请链接)
# 然后发送链接__
#
# **对于机器人聊天**
#
# __发送带有“/b/”的链接、机器人的用户名和消息 ID，你可能需要安装一些非官方客户端来获取如下所示的 ID__
#
# ```
# https://t.me/b/botusername/4321
# ```
#
# **如果你需要一次保存多个受限文件**
#
# __发送公共/私人帖子链接，如上所述，使用格式“发件人 - 收件人”发送多条消息，如下所示__
#
# ```
# https://t.me/xxxx/1001-1010
#
# https://t.me/c/xxxx/101 - 120
# ```
#
# __最好在中间加上空格__'''
#
# print("Bot is running...")
# bot.run_until_disconnected()
