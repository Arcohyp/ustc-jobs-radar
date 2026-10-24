#!/usr/bin/env python3
"""钉钉通知模块（独立实现，不依赖 dingtalk-server-monitor）。

凭证读取优先级：
    1. 环境变量 DINGTALK_WEBHOOK / DINGTALK_SECRET
    2. 项目根目录 secrets.yaml（不入库，格式见 example）：

        dingtalk:
          web_hook: "https://oapi.dingtalk.com/robot/send?access_token=xxx"
          signature: "SECxxx"   # 加签密钥，未开启加签则省略

对外接口与 DSM 的 core.notifier.send_notification 保持一致：
    send_notification(title, content, status="success") -> bool
"""

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
SECRETS_PATH = BASE / "secrets.yaml"


def _load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        return {}
    return yaml.safe_load(SECRETS_PATH.read_text(encoding="utf-8")) or {}


def _get_credential(env_name: str, key: str) -> str:
    return os.environ.get(env_name) or _load_secrets().get("dingtalk", {}).get(key, "")


def _sign(secret: str) -> tuple[str, str]:
    """钉钉加签：HMAC-SHA256(timestamp\nsecret) 后 base64 + URL 编码。"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"),
                         string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    return timestamp, urllib.parse.quote_plus(base64.b64encode(hmac_code))


def send_notification(title: str, content: str, status: str = "success") -> bool:
    """发送 Markdown 消息到钉钉群机器人。status: success / warning / error。"""
    webhook = _get_credential("DINGTALK_WEBHOOK", "web_hook")
    if not webhook:
        print("DINGTALK_WEBHOOK 未配置（环境变量或 secrets.yaml），跳过推送")
        return False

    url = webhook
    secret = _get_credential("DINGTALK_SECRET", "signature")
    if secret:
        timestamp, sign = _sign(secret)
        url = f"{webhook}&timestamp={timestamp}&sign={sign}"

    color = {"success": "#52c41a", "warning": "#faad14", "error": "#f5222d"}.get(status, "#1890ff")
    icon = {"success": "✅", "warning": "⚠️", "error": "❌"}.get(status, "ℹ️")
    markdown_text = f"## {icon} {title}\n\n**状态：** {status}\n\n{content}"

    body = {"msgtype": "markdown",
            "markdown": {"title": title, "text": markdown_text},
            "at": {"isAtAll": False}}
    try:
        resp = requests.post(url, json=body, timeout=15)
        result = resp.json()
        if result.get("errcode") == 0:
            return True
        print(f"钉钉 API 返回错误：{result}")
        return False
    except Exception as e:
        print(f"钉钉推送失败：{e}")
        return False
