"""
通知系统 — 关键事件发生时主动通知用户。

支持渠道：
  - Telegram Bot（推荐）
  - 邮件（SMTP）
  - 系统通知（Windows Toast）
  - 扩展接口（自定义渠道）

触发通知的事件：
  ┌─────────────────────┬────────────────────────────────┐
  │ 事件               │ 通知级别                        │
  ├─────────────────────┼────────────────────────────────┤
  │ 发布成功            │ INFO（可选，避免刷屏）          │
  │ 发布失败            │ WARNING                         │
  │ 连续 3 次失败       │ CRITICAL                        │
  │ 风控触发             │ WARNING                         │
  │ 掉线                │ CRITICAL                        │
  │ 版本变化             │ INFO                            │
  │ 模板重建完成         │ INFO                            │
  │ 今日发布达上限      │ WARNING                         │
  └─────────────────────┴────────────────────────────────┘

使用方式：
    notifier = Notifier()
    notifier.add_channel(TelegramChannel(bot_token="...", chat_id="..."))
    notifier.send("发布成功", "朋友圈已发送: 今天天气真好", level="info")

    # 集成到 EventBus
    bus.on(EventType.STEP_FAILED, notifier.on_step_failed)

Author: 版本无关微信自动化系统
"""

import logging
import json
import threading
from typing import Optional, List, Protocol
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 通知渠道接口
# ═══════════════════════════════════════════════════════════════

class NotifyChannel(Protocol):
    """通知渠道接口 — 实现此接口即可添加新渠道"""

    def send(self, title: str, message: str, level: str = "info") -> bool:
        """发送通知"""
        ...


# ═══════════════════════════════════════════════════════════════
# Telegram Bot 渠道
# ═══════════════════════════════════════════════════════════════

class TelegramChannel:
    """
    Telegram Bot 通知渠道。

    创建 Bot:
      1. 在 Telegram 搜索 @BotFather
      2. 发送 /newbot
      3. 获取 bot token

    获取 chat_id:
      1. 搜索你的 bot 并发送任意消息
      2. 访问 https://api.telegram.org/bot<TOKEN>/getUpdates
      3. 从响应中复制 chat_id
    """

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id

    def send(self, title: str, message: str, level: str = "info") -> bool:
        emoji_map = {
            'info': 'ℹ️',
            'warning': '⚠️',
            'critical': '🚨',
            'success': '✅',
        }
        emoji = emoji_map.get(level, '📢')

        text = f"{emoji} *{title}*\n{message}"

        try:
            import urllib.request
            import urllib.parse

            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            data = json.dumps({
                'chat_id': self._chat_id,
                'text': text,
                'parse_mode': 'Markdown',
            }).encode()

            req = urllib.request.Request(
                url, data=data,
                headers={'Content-Type': 'application/json'}
            )
            urllib.request.urlopen(req, timeout=10)
            return True
        except Exception as e:
            logger.warning(f"Telegram 通知发送失败: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# 邮件渠道
# ═══════════════════════════════════════════════════════════════

class EmailChannel:
    """SMTP 邮件通知渠道"""

    def __init__(self, smtp_host: str, smtp_port: int,
                 username: str, password: str,
                 to_email: str, from_email: str = None):
        self._host = smtp_host
        self._port = smtp_port
        self._user = username
        self._pass = password
        self._to = to_email
        self._from = from_email or username

    def send(self, title: str, message: str, level: str = "info") -> bool:
        try:
            import smtplib
            from email.mime.text import MIMEText

            body = f"[{level.upper()}] {title}\n\n{message}\n\n{datetime.now()}"
            msg = MIMEText(body, 'plain', 'utf-8')
            msg['Subject'] = f"[朋友圈自动化] {title}"
            msg['From'] = self._from
            msg['To'] = self._to

            with smtplib.SMTP_SSL(self._host, self._port, timeout=10) as smtp:
                smtp.login(self._user, self._pass)
                smtp.send_message(msg)
            return True
        except Exception as e:
            logger.warning(f"邮件发送失败: {e}")
            return False


# ═══════════════════════════════════════════════════════════════
# Windows Toast 通知
# ═══════════════════════════════════════════════════════════════

class WindowsToastChannel:
    """Windows 系统通知渠道"""

    def send(self, title: str, message: str, level: str = "info") -> bool:
        try:
            from winotify import Notification
            notif = Notification(
                app_id="WeChat Moments Automation",
                title=title,
                msg=message,
            )
            notif.show()
            return True
        except ImportError:
            pass
        except Exception as e:
            logger.debug(f"Windows 通知失败: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# 日志文件渠道 — 始终启用的基础渠道
# ═══════════════════════════════════════════════════════════════

class LogFileChannel:
    """日志文件通知（始终启用）"""

    def __init__(self, log_dir: str = "logs/notifications"):
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def send(self, title: str, message: str, level: str = "info") -> bool:
        try:
            today = datetime.now().strftime("%Y%m%d")
            filepath = self._dir / f"{today}.log"

            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(
                    f"[{datetime.now().isoformat()}] [{level.upper()}] "
                    f"{title}: {message}\n"
                )
            return True
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════
# 通知管理器
# ═══════════════════════════════════════════════════════════════

@dataclass
class NotifyConfig:
    """通知配置"""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    email_smtp_host: str = ""
    email_smtp_port: int = 465
    email_user: str = ""
    email_pass: str = ""
    email_to: str = ""
    notify_on_success: bool = False    # 是否每次成功都通知
    notify_on_failure: bool = True
    notify_on_risk: bool = True
    notify_on_login_lost: bool = True
    notify_on_version_change: bool = True


class Notifier:
    """
    通知管理器。

    使用方式：
        notifier = Notifier(config)
        notifier.add_channel(TelegramChannel(token, chat_id))
        notifier.critical("掉线了", "微信被强制登出")

        # 集成到 EventBus
        bus.on(EventType.STEP_FAILED, notifier.handle_event)
        bus.on(EventType.RISK_WARNING, notifier.handle_event)
        bus.on(EventType.LOGIN_LOST, notifier.handle_event)
    """

    def __init__(self, config: NotifyConfig = None):
        self._config = config or NotifyConfig()
        self._channels: List[NotifyChannel] = []

        # 默认渠道：日志文件
        self._channels.append(LogFileChannel())

        # 根据配置启用渠道
        self._init_channels()

    def _init_channels(self):
        cfg = self._config

        if cfg.telegram_bot_token and cfg.telegram_chat_id:
            self._channels.append(
                TelegramChannel(cfg.telegram_bot_token, cfg.telegram_chat_id)
            )
            logger.info("Telegram 通知已启用")

        if cfg.email_smtp_host and cfg.email_user:
            self._channels.append(EmailChannel(
                cfg.email_smtp_host, cfg.email_smtp_port,
                cfg.email_user, cfg.email_pass, cfg.email_to,
            ))
            logger.info("邮件通知已启用")

    def add_channel(self, channel: NotifyChannel):
        """添加自定义通知渠道"""
        self._channels.append(channel)

    # ── 便捷方法 ──

    def info(self, title: str, message: str):
        if self._config.notify_on_success:
            self._broadcast(title, message, 'info')

    def warning(self, title: str, message: str):
        if self._config.notify_on_failure:
            self._broadcast(title, message, 'warning')

    def critical(self, title: str, message: str):
        self._broadcast(title, message, 'critical')

    def success(self, title: str, message: str):
        if self._config.notify_on_success:
            self._broadcast(title, message, 'success')

    # ── EventBus 集成 ──

    def handle_event(self, event):
        """处理 EventBus 事件"""
        from ..core.events import EventType

        if event.type == EventType.STEP_FAILED:
            step = event.payload.get('step', 'unknown')
            error = event.payload.get('error', '')
            self.critical(
                f"步骤失败: {step}",
                f"步骤 {step} 执行失败\n{error}"
            )

        elif event.type == EventType.RISK_WARNING:
            self.warning("风控警告", event.payload.get('signal', ''))

        elif event.type == EventType.RISK_CRITICAL:
            self.critical("风控严重", event.payload.get('signal', ''))

        elif event.type == EventType.LOGIN_LOST:
            self.critical("微信掉线", event.payload.get('detectedPage', ''))

    # ── 内部 ──

    def _broadcast(self, title: str, message: str, level: str):
        """广播到所有渠道（非阻塞）"""
        def _send():
            for channel in self._channels:
                try:
                    channel.send(title, message, level)
                except Exception as e:
                    logger.debug(f"通知渠道异常: {e}")

        # 在后台线程发送，不阻塞主流程
        t = threading.Thread(target=_send, daemon=True)
        t.start()


# ═══════════════════════════════════════════════════════════════
# 便捷函数：从 YAML 配置创建 Notifier
# ═══════════════════════════════════════════════════════════════

def create_notifier_from_config(config: dict) -> Notifier:
    """从 settings.yaml 的通知配置创建 Notifier"""
    notif_cfg = config.get('notifications', {})
    cfg = NotifyConfig(
        telegram_bot_token=notif_cfg.get('telegram_bot_token', ''),
        telegram_chat_id=notif_cfg.get('telegram_chat_id', ''),
        email_smtp_host=notif_cfg.get('email_smtp_host', ''),
        email_smtp_port=notif_cfg.get('email_smtp_port', 465),
        email_user=notif_cfg.get('email_user', ''),
        email_pass=notif_cfg.get('email_pass', ''),
        email_to=notif_cfg.get('email_to', ''),
        notify_on_success=notif_cfg.get('notify_on_success', False),
        notify_on_failure=notif_cfg.get('notify_on_failure', True),
        notify_on_risk=notif_cfg.get('notify_on_risk', True),
        notify_on_login_lost=notif_cfg.get('notify_on_login_lost', True),
    )
    return Notifier(cfg)
