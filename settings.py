"""Validated settings shared by the dashboard and the plugin runtime."""

from urllib.parse import urlsplit

DEFAULTS = {
    "enabled": True,
    "search_enabled": True,
    "user_enabled": True,
    "view_enabled": True,
    "send_enabled": True,
    "attach_guidance": True,
    "default_count": 3,
    "proxy_mode": "system",
    "proxy_url": "",
    "timeout_seconds": 30,
    "max_image_mb": 4,
    "exclude_ai": True,
    "cache_ttl_seconds": 120,
    "cache_max_entries": 100,
    "max_concurrency": 3,
    "send_cooldown_seconds": 8,
}
LIMITS = {
    "default_count": (1, 5),
    "timeout_seconds": (5, 90),
    "max_image_mb": (1, 8),
    "cache_ttl_seconds": (0, 3600),
    "cache_max_entries": (0, 500),
    "max_concurrency": (1, 6),
    "send_cooldown_seconds": (0, 120),
}


def validate_settings(values: dict) -> dict:
    """Validate a complete settings snapshot without coercing malformed input.

    Args:
        values: Settings supplied by AstrBot or the authenticated dashboard.

    Returns:
        A complete dictionary containing only supported settings.

    Raises:
        ValueError: If a setting has an invalid type, range, or proxy URL.
    """
    result = {**DEFAULTS, **{k: v for k, v in values.items() if k in DEFAULTS}}
    for key, default in DEFAULTS.items():
        value = result[key]
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise TypeError(f"{key} 必须是开关值")
        elif key in LIMITS:
            low, high = LIMITS[key]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} 必须是 {low}–{high} 的整数")
        elif not isinstance(value, str):
            raise ValueError(f"{key} 必须是文本")
    if result["proxy_mode"] not in {"system", "direct", "custom"}:
        raise ValueError("请选择系统代理、直连或自定义代理")
    value = result["proxy_url"].strip()
    if value:
        try:
            parts = urlsplit(value)
            valid = (
                parts.scheme in {"http", "https"}
                and parts.hostname
                and parts.username is None
                and parts.password is None
                and parts.path in {"", "/"}
                and not parts.query
                and not parts.fragment
                and parts.port != 0
                and "\\" not in value
                and not any(ord(char) < 33 for char in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(
                "代理应为 http(s)://主机:端口，不支持账号密码、路径或查询参数"
            )
    if result["proxy_mode"] == "custom" and not value:
        raise ValueError("自定义代理模式需要填写代理地址")
    result["proxy_url"] = value
    return result
