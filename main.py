import os
import json
import uuid
import hashlib
from datetime import datetime

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain, Image, Record, Video
from astrbot.api.message_components import File as CompFile
from quart import jsonify, request

import aiohttp

# 尝试导入 OneBot 特有组件（其他平台可能不存在）
try:
    from astrbot.api.message_components import At, Reply
except ImportError:
    At = Reply = None
try:
    from astrbot.api.message_components import Poke, Node, Nodes, Face
except ImportError:
    Poke = Node = Nodes = Face = None

PLUGIN_NAME = "message_saver"
RULES_FILE = os.path.join("data", "plugin_data", PLUGIN_NAME, "rules.json")


class MessageSaver(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.save_dir = os.path.join("data", "plugin_data", PLUGIN_NAME)

        # 注册 Page 后端 API
        context.register_web_api(
            f"/{PLUGIN_NAME}/rules",
            self._api_get_rules,
            ["GET"],
            "获取所有保存规则",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/rules",
            self._api_save_rules,
            ["POST"],
            "保存规则列表",
        )

    # ==================== 后端 API ====================

    async def _api_get_rules(self):
        rules = _load_rules()
        return jsonify({"status": "ok", "data": rules})

    async def _api_save_rules(self):
        try:
            payload = await request.get_json()
            rules = payload.get("rules", []) if payload else []
            _save_rules_to_file(rules)
            return jsonify({"status": "ok", "message": f"已保存 {len(rules)} 条规则"})
        except Exception as e:
            logger.error(f"[MessageSaver] 保存规则失败: {e}")
            return jsonify({"status": "error", "message": str(e)}), 400

    # ==================== 消息处理 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，根据规则过滤并保存到本地"""
        try:
            rules = _load_rules()
            if not rules:
                return

            message_obj = event.message_obj
            message_id = message_obj.message_id or ""
            sender_id = _get_sender_id(message_obj.sender)
            session_ids = _get_session_ids(event)
            message_chain = event.get_messages()
            msg_types = _detect_message_types(message_chain)

            matched_rule = _match_any_rule(rules, sender_id, session_ids, msg_types)
            if not matched_rule:
                return

            await self._save_message(event, message_chain, msg_types, message_id, sender_id)

            if matched_rule.get("stop_propagation"):
                event.stop_event()
                logger.info(f"[MessageSaver] 已阻止事件传播, session={event.unified_msg_origin}")

        except Exception as e:
            logger.error(f"[MessageSaver] 处理消息异常: {e}")

    # ==================== 消息保存 ====================

    async def _save_message(self, event, message_chain, msg_types, message_id, sender_id):
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        ts_str = now.strftime("%H%M%S_%f")
        id_slug = hashlib.md5((message_id or str(uuid.uuid4())).encode()).hexdigest()[:8]

        date_dir = os.path.join(self.save_dir, date_str)
        os.makedirs(date_dir, exist_ok=True)
        prefix = f"{ts_str}_{id_slug}"

        metadata = {
            "message_id": message_id,
            "sender_id": sender_id,
            "sender_name": event.get_sender_name(),
            "group_id": event.get_group_id() or "",
            "session_id": event.unified_msg_origin,
            "timestamp": now.isoformat(),
            "message_types": sorted(msg_types),
            "message_str": event.message_str,
        }
        with open(os.path.join(date_dir, f"{prefix}_metadata.json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        # 文本内容
        for comp in message_chain:
            if isinstance(comp, Plain) and comp.text and comp.text.strip():
                with open(os.path.join(date_dir, f"{prefix}_content.txt"), "w", encoding="utf-8") as f:
                    f.write(comp.text)
                break

        # 媒体文件
        idx = 0
        for comp in message_chain:
            if isinstance(comp, (Image, Record, Video, CompFile)):
                media_url = _get_media_url(comp)
                if not media_url:
                    logger.warning(f"[MessageSaver] 无法获取媒体URL, comp 属性: {[a for a in dir(comp) if not a.startswith('_')]}")
                    continue
                ext = _guess_extension(media_url, comp)
                filepath = os.path.join(date_dir, f"{prefix}_media_{idx}{ext}")
                await _download(media_url, filepath)
                idx += 1

        logger.info(f"[MessageSaver] 已保存消息: {date_dir}/{prefix}_*")


# ==================== 工具函数 ====================

def _load_rules() -> list:
    try:
        if os.path.exists(RULES_FILE):
            with open(RULES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"[MessageSaver] 加载规则失败: {e}")
    return []


def _save_rules_to_file(rules: list):
    os.makedirs(os.path.dirname(RULES_FILE), exist_ok=True)
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


def _get_sender_id(sender) -> str:
    for attr in ("uid", "user_id", "id", "qq", "sender_id"):
        val = getattr(sender, attr, None)
        if val:
            return str(val)
    return str(sender)


def _get_session_ids(event: AstrMessageEvent) -> list:
    """返回可用来匹配 session_id 的多个候选值"""
    ids = [event.unified_msg_origin]
    gid = event.get_group_id()
    if gid:
        ids.append(gid)
    return ids


def _detect_message_types(message_chain) -> set:
    types = set()
    for comp in message_chain:
        if isinstance(comp, Plain) and comp.text and comp.text.strip():
            types.add("Plain")
        elif isinstance(comp, Image):
            types.add("Image")
        elif isinstance(comp, Record):
            types.add("Record")
        elif isinstance(comp, Video):
            types.add("Video")
        elif isinstance(comp, CompFile):
            types.add("File")
        elif At is not None and isinstance(comp, At):
            types.add("At")
        elif Reply is not None and isinstance(comp, Reply):
            types.add("Reply")
        elif Poke is not None and isinstance(comp, Poke):
            types.add("Poke")
        elif Node is not None and isinstance(comp, (Node, Nodes)):
            types.add("Node")
        elif Face is not None and isinstance(comp, Face):
            types.add("Face")
    return types


def _match_any_rule(rules: list, sender_id: str, session_ids: list, msg_types: set) -> dict | None:
    """返回匹配到的第一条规则，未匹配返回 None"""
    for rule in rules:
        # 发送人 ID（留空=全部匹配）
        rule_senders = rule.get("sender_ids") or []
        if rule_senders and sender_id not in rule_senders:
            continue

        # 会话 ID（留空=全部匹配）
        rule_sessions = rule.get("session_ids") or []
        if rule_sessions:
            if not any(s in rule_sessions for s in session_ids):
                continue

        # 消息类型（留空=全部匹配）
        rule_types = set(rule.get("message_types") or [])
        if rule_types and not (rule_types & msg_types):
            continue

        return rule
    return None


def _get_media_url(comp) -> str | None:
    """从消息组件中提取可下载的媒体 URL，支持多种属性名"""
    for attr in ("url", "file", "path", "src", "data"):
        val = getattr(comp, attr, None)
        if val and isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _guess_extension(url: str, comp) -> str:
    url_lower = url.lower()
    known = [
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
        ".mp3", ".wav", ".ogg", ".aac", ".flac", ".amr", ".silk",
        ".mp4", ".avi", ".mov", ".mkv", ".webm",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar",
    ]
    for ext in known:
        if ext in url_lower:
            return ext
    if isinstance(comp, Image):
        return ".jpg"
    elif isinstance(comp, Record):
        return ".mp3"
    elif isinstance(comp, Video):
        return ".mp4"
    return ".bin"


async def _download(url: str, filepath: str):
    try:
        if url.startswith("base64://"):
            # OneBot base64 编码数据
            import base64
            data = base64.b64decode(url[len("base64://"):])
            with open(filepath, "wb") as f:
                f.write(data)
            logger.info(f"[MessageSaver] base64 解码保存: {filepath}")
            return

        if url.startswith("file://"):
            # 本地文件路径，直接复制
            import shutil
            src = url[len("file://"):]
            if src.startswith("/"):
                src = src[1:] if os.name == "nt" and not src.startswith("\\\\") else src
            src = os.path.normpath(src)
            if os.path.exists(src):
                shutil.copy2(src, filepath)
                logger.info(f"[MessageSaver] 本地文件复制: {src} -> {filepath}")
            else:
                logger.warning(f"[MessageSaver] 本地文件不存在: {src}")
            return

        # HTTP/HTTPS 下载
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    with open(filepath, "wb") as f:
                        f.write(await resp.read())
                else:
                    logger.warning(f"[MessageSaver] 下载失败 HTTP {resp.status}: {url}")
    except Exception as e:
        logger.error(f"[MessageSaver] 下载异常: {url} — {e}")
