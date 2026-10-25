"""Bounded, anonymous access to Pixiv's official website JSON and image CDN."""

from __future__ import annotations

import asyncio
import copy
import io
import ipaddress
import json
import math
import re
import socket
import ssl
import sys
import time
import warnings
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpcore
import httpx
from PIL import Image, UnidentifiedImageError

OFFICIAL_HOST = "www.pixiv.net"
IMAGE_HOSTS = {"i.pximg.net", "i-f.pximg.net", "s.pximg.net"}
MAX_PIXELS = 64_000_000
JSON_LIMIT = 2 * 1024 * 1024
CACHE_BYTE_LIMIT = 16 * 1024 * 1024
_ID = re.compile(r"[1-9][0-9]{0,11}\Z")
_CLEAN = re.compile(r"[\s\x00-\x1f\x7f]+")
_METRICS = {"likes": "likeCount", "bookmarks": "bookmarkCount", "views": "viewCount"}


class PixivError(Exception):
    """An actionable error safe to display to a user."""


def _clean(value: Any, maximum: int = 120) -> str:
    return _CLEAN.sub(" ", str(value or "")).strip()[:maximum]


def _number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
        if isinstance(value, bool) or not math.isfinite(parsed):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _counter(value: Any) -> int | None:
    """Keep unknown counters distinct from a genuine zero."""
    return value if type(value) is int and value >= 0 else None


def _ranking_options(sort_by, min_likes, min_bookmarks, min_views) -> dict[str, int]:
    if not isinstance(sort_by, str) or sort_by not in {"latest", *_METRICS}:
        raise PixivError("排序应为 latest、likes、bookmarks 或 views")
    minimums = {
        "likeCount": min_likes,
        "bookmarkCount": min_bookmarks,
        "viewCount": min_views,
    }
    if any(
        type(value) is not int or not 0 <= value <= 1_000_000_000
        for value in minimums.values()
    ):
        raise PixivError("点赞、收藏和浏览门槛应为 0 至 1000000000 的整数")
    return minimums


def validate_pixiv_url(value: str, *, image: bool = False) -> str:
    """Accept only fixed official HTTPS peers, with no user information."""
    try:
        parsed = urlsplit(str(value))
        allowed = IMAGE_HOSTS if image else {OFFICIAL_HOST}
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or "\\" in str(value)
            or any(ord(char) < 32 for char in str(value))
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise PixivError("只允许 Pixiv 官方 HTTPS 地址") from None
    return str(value)


def _reference_id(value: Any, *, user: bool = False) -> str:
    text = str(value).strip()
    candidate = text
    if not _ID.fullmatch(text):
        try:
            validate_pixiv_url(text)
            parsed = urlsplit(text)
            pattern = (
                r"/(?:[a-z]{2}/)?users/([0-9]+)(?:/(?:artworks|illustrations|manga))?/?"
                if user
                else r"/(?:[a-z]{2}/)?artworks/([0-9]+)/?"
            )
            matched = re.fullmatch(pattern, parsed.path)
            candidate = matched.group(1) if matched else ""
        except PixivError:
            candidate = ""
    if not _ID.fullmatch(candidate):
        label = (
            "作者 UID 或作者主页链接；昵称请先确认官方主页"
            if user
            else "PID 或作品链接"
        )
        raise PixivError(f"请输入有效的 Pixiv {label}")
    return candidate


def artwork_id(value: Any) -> str:
    return _reference_id(value)


def user_id(value: Any) -> str:
    return _reference_id(value, user=True)


def validate_proxy(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
            or "\\" in value
            or any(ord(char) < 33 for char in value)
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise PixivError(
            "代理应为 http(s)://主机:端口，不包含凭据、路径或参数"
        ) from None
    return value.rstrip("/")


def _system_proxy() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        key_name = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name) as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return ""
            raw = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
        if "=" in raw:
            entries = dict(
                item.strip().split("=", 1) for item in raw.split(";") if "=" in item
            )
            raw = entries.get("https", entries.get("http", ""))
        if raw and "://" not in raw:
            raw = "http://" + raw
        return validate_proxy(raw) if raw else ""
    except OSError:
        return ""


async def _resolve_public(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    )
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise PixivError("Pixiv 域名解析到非公网地址，已拒绝连接；可检查系统代理")
    return addresses


class _PublicBackend(httpcore.AsyncNetworkBackend):
    """Pin DNS answers while leaving TLS verification on the original hostname."""

    def __init__(
        self, resolver: Callable[[str], Awaitable[list[str]]] = _resolve_public
    ):
        self.backend = httpcore.AnyIOBackend()
        self.resolver = resolver

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        if host not in IMAGE_HOSTS | {OFFICIAL_HOST} or port != 443:
            raise PixivError("Pixiv 连接目标无效")
        addresses = await self.resolver(host)
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise PixivError("Pixiv 域名解析到非公网地址，已拒绝连接")
        # The caller's total deadline also bounds DNS and all candidate attempts.
        for index, address in enumerate(addresses):
            try:
                return await self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if index == len(addresses) - 1:
                    raise
        raise PixivError("Pixiv 无可用公网地址")

    async def connect_unix_socket(self, *args, **kwargs):
        raise PixivError("Pixiv 不允许本地套接字连接")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class _DirectTransport(httpx.AsyncBaseTransport):
    def __init__(self, concurrency: int):
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=concurrency,
            max_keepalive_connections=concurrency,
            network_backend=_PublicBackend(),
        )

    async def handle_async_request(self, request):
        response = await self.pool.handle_async_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.pool.aclose()


class PixivClient:
    """Search and inspect safe-age artworks without login credentials.

    Args:
        config: Flat plugin settings. A new client is needed after settings change.
        transport: Optional HTTPX transport for isolated integration tests. Injected
            transports intentionally bypass actual DNS and network connections.
    """

    def __init__(
        self,
        config: dict | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = dict(config or {})
        self.timeout = _number(self.config.get("timeout_seconds"), 30, 5, 120)
        self.max_image_bytes = int(
            _number(self.config.get("max_image_mb"), 4, 1, 8) * 1024 * 1024
        )
        self.cache_ttl = _number(self.config.get("cache_ttl_seconds"), 120, 0, 3600)
        self.cache_max = int(_number(self.config.get("cache_max_entries"), 100, 0, 500))
        self.concurrency = int(_number(self.config.get("max_concurrency"), 3, 1, 8))
        self.exclude_ai = self.config.get("exclude_ai", True) is not False
        self.proxy_mode = str(self.config.get("proxy_mode", "system"))
        if self.proxy_mode not in {"system", "direct", "custom"}:
            raise PixivError("代理模式应为 system、direct 或 custom")
        proxy = ""
        if self.proxy_mode == "custom":
            proxy = validate_proxy(str(self.config.get("proxy_url", "")).strip())
        elif self.proxy_mode == "system":
            proxy = _system_proxy()
        self._proxy_active = bool(proxy)
        if transport is None:
            transport = (
                httpx.AsyncHTTPTransport(
                    proxy=proxy,
                    verify=True,
                    limits=httpx.Limits(
                        max_connections=self.concurrency,
                        max_keepalive_connections=self.concurrency,
                    ),
                    trust_env=False,
                )
                if proxy
                else _DirectTransport(self.concurrency)
            )
        self.http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(min(20, self.timeout)),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.pixiv.net/",
                "Accept-Encoding": "identity",
            },
        )
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._cache: OrderedDict[str, tuple[float, Any, int, str]] = OrderedDict()
        self._cache_bytes = 0
        self._closed = False
        self._tasks: set[asyncio.Task] = set()
        self._stats = {"requests": 0, "cache_hits": 0, "errors": 0}

    async def _operation(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        if self._closed:
            raise PixivError("Pixiv 客户端已关闭")
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        try:
            async with asyncio.timeout(self.timeout):
                return await operation()
        except (TimeoutError, httpx.TimeoutException, httpcore.TimeoutException):
            self._stats["errors"] += 1
            raise PixivError("Pixiv 请求超时，请检查系统代理或网络后重试") from None
        except PixivError:
            self._stats["errors"] += 1
            raise
        except (
            httpx.HTTPError,
            httpcore.NetworkError,
            httpcore.ProtocolError,
            httpcore.ProxyError,
            OSError,
            ssl.SSLError,
        ):
            self._stats["errors"] += 1
            raise PixivError("Pixiv 网络连接失败，请检查代理和网络状态") from None
        finally:
            if task:
                self._tasks.discard(task)

    async def _bytes(
        self, url: str, *, image: bool = False, maximum: int = JSON_LIMIT
    ) -> tuple[bytes, str]:
        validate_pixiv_url(url, image=image)
        async with self._semaphore:
            return await self._request_bytes(url, image=image, maximum=maximum)

    async def _request_bytes(
        self, url: str, *, image: bool, maximum: int
    ) -> tuple[bytes, str]:
        """Read one request while the shared network concurrency slot is held."""
        self._stats["requests"] += 1
        async with self.http.stream(
            "GET", url, headers={"Accept": "image/*" if image else "application/json"}
        ) as response:
            if response.status_code != 200:
                hint = (
                    "，站点拒绝访问，请检查代理或浏览器验证"
                    if response.status_code in (401, 403)
                    else ""
                )
                raise PixivError(f"Pixiv HTTP {response.status_code}{hint}")
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > maximum:
                raise PixivError(
                    "Pixiv 图片超过大小限制" if image else "Pixiv 响应超过大小限制"
                )
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > maximum:
                    raise PixivError(
                        "Pixiv 图片超过大小限制" if image else "Pixiv 响应超过大小限制"
                    )
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("content-type", "").split(
                ";", 1
            )[0].lower().strip()

    async def _json(
        self, route: str, *, use_cache: bool = True, include_fetched_at: bool = False
    ) -> Any:
        now = time.monotonic()
        cached = self._cache.get(route) if use_cache else None
        if cached and cached[0] > now:
            self._cache.move_to_end(route)
            self._stats["cache_hits"] += 1
            body = copy.deepcopy(cached[1])
            return (body, cached[3]) if include_fetched_at else body
        if cached:
            self._cache_bytes -= self._cache.pop(route)[2]
        data, _ = await self._bytes("https://www.pixiv.net" + route)
        try:
            response = json.loads(data)
        except (ValueError, UnicodeError):
            raise PixivError(
                "Pixiv 未返回作品数据，可能需要网站验证；请在浏览器检查"
            ) from None
        if (
            not isinstance(response, dict)
            or response.get("error")
            or response.get("body") is None
        ):
            raise PixivError("Pixiv 未找到可访问的数据，作品可能已删除或不公开")
        body = response["body"]
        fetched_at = datetime.now(UTC).isoformat()
        if use_cache and self.cache_ttl and self.cache_max:
            # Concurrent operations can finish the same uncached route. Count
            # the replacement once, rather than leaking the byte budget.
            previous = self._cache.pop(route, None)
            if previous:
                self._cache_bytes -= previous[2]
            self._cache[route] = (now + self.cache_ttl, body, len(data), fetched_at)
            self._cache_bytes += len(data)
            while (
                len(self._cache) > self.cache_max
                or self._cache_bytes > CACHE_BYTE_LIMIT
            ):
                self._cache_bytes -= self._cache.popitem(last=False)[1][2]
        body = copy.deepcopy(body)
        return (body, fetched_at) if include_fetched_at else body

    def _meta(self, body: Any, *, discovery: bool = False) -> dict:
        if not isinstance(body, dict):
            raise PixivError("Pixiv 作品数据格式异常")
        # Equality alone would also accept False as the numeric rating zero.
        rating = body.get("xRestrict")
        if type(rating) not in (str, int) or rating not in (0, "0"):
            raise PixivError("该作品不是可确认的全年龄作品，已跳过")
        ai = body.get("aiType")
        valid_ai = type(ai) in (str, int) and ai in (1, "1", 2, "2")
        if discovery and (not valid_ai or (self.exclude_ai and str(ai) != "1")):
            raise PixivError("该作品为 AI 生成或 AI 标记无法确认，已跳过")
        pid = artwork_id(body.get("id", ""))
        tags = body.get("tags", [])
        if isinstance(tags, dict):
            tags = tags.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        author_id = str(body.get("userId", ""))
        thumbnail = body.get("url", "")
        if not thumbnail and isinstance(body.get("urls"), dict):
            thumbnail = body["urls"].get("small", "")
        try:
            thumbnail = validate_pixiv_url(thumbnail, image=True) if thumbnail else ""
        except PixivError:
            thumbnail = ""
        return {
            "id": pid,
            "title": _clean(body.get("title")),
            "author": _clean(body.get("userName")),
            "authorId": author_id if _ID.fullmatch(author_id) else "",
            "url": "https://www.pixiv.net/artworks/" + pid,
            "pageCount": int(_number(body.get("pageCount"), 1, 1, 1000)),
            "type": "ugoira-preview"
            if str(body.get("illustType")) == "2"
            else "illustration",
            "publishedAt": _clean(body.get("createDate"), 50),
            "tags": [
                _clean(tag.get("tag") if isinstance(tag, dict) else tag, 40)
                for tag in tags
                if isinstance(tag, (str, dict))
            ][:12],
            "aiType": int(ai) if valid_ai else None,
            "aiGenerated": str(ai) == "2" if valid_ai else None,
            "thumbnailUrl": thumbnail,
            "width": int(_number(body.get("width"), 0, 0, MAX_PIXELS)),
            "height": int(_number(body.get("height"), 0, 0, MAX_PIXELS)),
            "likeCount": _counter(body.get("likeCount")),
            "bookmarkCount": _counter(body.get("bookmarkCount")),
            "viewCount": _counter(body.get("viewCount")),
        }

    @staticmethod
    def _sort_items(items: list[dict], sort_by: str) -> list[dict]:
        def key(meta):
            if sort_by in _METRICS:
                value = meta[_METRICS[sort_by]]
                return value is not None, value or 0, int(meta["id"])
            try:
                published = datetime.fromisoformat(meta["publishedAt"]).timestamp()
            except (ValueError, OverflowError, OSError):
                published = 0
            return published, int(meta["id"])

        return sorted(items, key=key, reverse=True)

    async def _enrich_candidates(
        self, candidates: list[dict], *, author_id: str | None = None
    ) -> tuple[list[dict], int]:
        """Fetch authoritative detail counters, rechecking discovery eligibility."""

        async def enrich(candidate):
            try:
                meta, _ = await self._detail(candidate["id"], discovery=True)
                if author_id is not None and meta["authorId"] != author_id:
                    return None
                return meta
            except (
                PixivError,
                httpx.HTTPError,
                httpcore.NetworkError,
                httpcore.ProtocolError,
                httpcore.ProxyError,
                httpcore.TimeoutException,
                OSError,
                ssl.SSLError,
            ):
                # A single removed or unavailable work must not make other
                # candidates disappear, and list counters are never a fallback.
                return None

        results = await asyncio.gather(*(enrich(candidate) for candidate in candidates))
        items = [item for item in results if item is not None]
        return items, len(candidates) - len(items)

    @staticmethod
    def _filter_items(items: list[dict], minimums: dict[str, int]) -> list[dict]:
        return [
            item
            for item in items
            if all(
                minimum == 0 or (item[field] is not None and item[field] >= minimum)
                for field, minimum in minimums.items()
            )
        ]

    async def search(
        self,
        keyword: str,
        count: int = 3,
        page: int = 1,
        sort_by: str = "latest",
        min_likes: int = 0,
        min_bookmarks: int = 0,
        min_views: int = 0,
        scan_pages: int = 1,
    ) -> dict:
        async def run():
            minimums = _ranking_options(sort_by, min_likes, min_bookmarks, min_views)
            if type(scan_pages) is not int or not 1 <= scan_pages <= 3:
                raise PixivError("搜索扫描页数应为 1 至 3 的整数")
            word = _clean(keyword, 80)
            if not word:
                raise PixivError("Pixiv 搜索关键词不能为空")
            selected_page = int(_number(page, 1, 1, 20))
            limit = int(_number(count, 3, 1, 5))
            ranked = sort_by != "latest" or any(minimums.values())
            query = {
                "word": word,
                "order": "date_d",
                "mode": "safe",
                "s_mode": "s_tag",
                "type": "all",
                "lang": "zh",
            }
            if self.exclude_ai:
                query["ai_type"] = "1"
            pages = []
            seen = set()
            preview_candidates = []
            preview_ids = set()
            candidate_sources = []
            has_more = False
            for current_page in range(
                selected_page, min(21, selected_page + scan_pages)
            ):
                query["p"] = str(current_page)
                body = await self._json(
                    "/ajax/search/artworks/"
                    + quote(word, safe="")
                    + "?"
                    + urlencode(query)
                )
                section = body.get("illustManga") if isinstance(body, dict) else None
                data = section.get("data") if isinstance(section, dict) else None
                if not isinstance(data, list):
                    raise PixivError("Pixiv 搜索结果格式异常")
                if ranked and current_page == 1:
                    popular = body.get("popular")
                    if isinstance(popular, dict):
                        for group in ("recent", "permanent"):
                            previews = popular.get(group)
                            if not isinstance(previews, list):
                                continue
                            selected = 0
                            for item in previews[:30]:
                                try:
                                    meta = self._meta(item, discovery=True)
                                except PixivError:
                                    continue
                                if meta["id"] in preview_ids:
                                    continue
                                preview_ids.add(meta["id"])
                                preview_candidates.append(meta)
                                selected += 1
                                if selected == 5:
                                    break
                            if selected:
                                candidate_sources.append("popular_preview_" + group)
                page_items = []
                for item in data[:60]:
                    try:
                        meta = self._meta(item, discovery=True)
                        if meta["id"] not in seen:
                            page_items.append(meta)
                            seen.add(meta["id"])
                    except PixivError:
                        continue
                pages.append(page_items)
                total = _number(section.get("total"), 0, 0, 1_000_000_000)
                has_more = current_page < 20 and (
                    len(data) >= 60 or total > current_page * 60
                )
                if not has_more:
                    break
            # Interleave pages so a three-page scan broadens the sample instead
            # of spending all 30 detail requests on the first page.
            if ranked:
                ordinary_candidates = [
                    entries[index]
                    for index in range(max(map(len, pages), default=0))
                    for entries in pages
                    if index < len(entries) and entries[index]["id"] not in preview_ids
                ]
                candidates = (preview_candidates + ordinary_candidates)[:30]
            else:
                candidates = [item for entries in pages for item in entries][:limit]
            if any(pages):
                candidate_sources.append("search_pages")
            items, failures = await self._enrich_candidates(candidates)
            items = self._filter_items(items, minimums)
            if ranked:
                items = self._sort_items(items, sort_by)
            source_query = {"mode": "safe"}
            if self.exclude_ai:
                source_query["ai_type"] = "1"
            return {
                "items": items[:limit],
                "keyword": word,
                "page": selected_page,
                "hasMore": has_more,
                "nextPage": selected_page + len(pages) if has_more else None,
                "sortBy": sort_by,
                "rankingScope": "sampled_candidates" if ranked else "latest_results",
                "scannedPages": len(pages),
                "candidatePoolSize": len(seen | preview_ids),
                "candidateSources": candidate_sources,
                "popularPreviewCandidates": len(preview_candidates),
                "scannedCandidates": len(candidates),
                "detailFailures": failures,
                "partial": bool(failures),
                "statsNote": "点赞、收藏、浏览量来自作品详情；"
                + (
                    "仅在官方公开人气预览与搜索页候选范围内排序，"
                    if preview_candidates
                    else "仅在搜索页候选范围内排序，"
                )
                + "不代表 Pixiv 全站人气排名。",
                "searchUrl": "https://www.pixiv.net/tags/"
                + quote(word, safe="")
                + "/artworks?"
                + urlencode(source_query),
                "source": "Pixiv",
            }

        return await self._operation(run)

    async def user_works(
        self,
        reference: str,
        count: int = 3,
        sort_by: str = "latest",
        min_likes: int = 0,
        min_bookmarks: int = 0,
        min_views: int = 0,
    ) -> dict:
        async def run():
            minimums = _ranking_options(sort_by, min_likes, min_bookmarks, min_views)
            limit = int(_number(count, 3, 1, 5))
            ranked = sort_by != "latest" or any(minimums.values())
            uid = user_id(reference)
            user = await self._json(f"/ajax/user/{uid}?full=0&lang=zh")
            profile = await self._json(f"/ajax/user/{uid}/profile/all?lang=zh")
            if not isinstance(user, dict) or str(user.get("userId")) != uid:
                raise PixivError("Pixiv 作者 UID 不匹配")
            if not isinstance(profile, dict):
                raise PixivError("Pixiv 作者作品列表格式异常")
            # Pixiv serializes empty PHP maps as [], including manga-only or
            # illustration-only profiles. Nonempty arrays remain invalid.
            illusts = profile.get("illusts")
            if illusts == []:
                illusts = {}
            if not isinstance(illusts, dict):
                raise PixivError("Pixiv 作者作品列表格式异常")
            manga = profile.get("manga", {})
            if manga == []:
                manga = {}
            if not isinstance(manga, dict):
                raise PixivError("Pixiv 作者作品列表格式异常")
            ids = sorted(
                {str(pid) for pid in [*illusts, *manga] if _ID.fullmatch(str(pid))},
                key=int,
                reverse=True,
            )
            candidates = ids[:30]
            items = []
            if candidates:
                query = [
                    ("work_category", "illustManga"),
                    ("is_first_page", "1"),
                    ("lang", "zh"),
                ]
                query.extend(("ids[]", pid) for pid in candidates)
                details = await self._json(
                    f"/ajax/user/{uid}/profile/illusts?" + urlencode(query)
                )
                works = details.get("works") if isinstance(details, dict) else None
                if works == []:
                    works = {}
                if not isinstance(works, dict):
                    raise PixivError("Pixiv 作者作品详情格式异常")
                for item in works.values():
                    try:
                        meta = self._meta(item, discovery=True)
                        if meta["authorId"] == uid and meta["id"] in candidates:
                            items.append(meta)
                    except PixivError:
                        continue

                items = self._sort_items(items, "latest")
            pool_size = len(items)
            detail_candidates = items if ranked else items[:limit]
            items, failures = await self._enrich_candidates(
                detail_candidates, author_id=uid
            )
            items = self._filter_items(items, minimums)
            items = self._sort_items(items, sort_by)
            return {
                "items": items[:limit],
                "user": {
                    "id": uid,
                    "name": _clean(user.get("name")),
                    "url": f"https://www.pixiv.net/users/{uid}",
                },
                "scanned": len(candidates),
                "moreCandidates": len(ids) > 30,
                "sortBy": sort_by,
                "rankingScope": "author_recent_30",
                "candidatePoolSize": pool_size,
                "scannedCandidates": len(detail_candidates),
                "detailFailures": failures,
                "partial": bool(failures),
                "statsNote": "点赞、收藏、浏览量来自作品详情；仅在作者最近 30 个候选中筛选排序。",
                "source": "Pixiv",
            }

        return await self._operation(run)

    async def _detail(
        self, reference: str, *, discovery: bool = False
    ) -> tuple[dict, dict]:
        pid = artwork_id(reference)
        body, fetched_at = await self._json(
            f"/ajax/illust/{pid}?lang=zh", include_fetched_at=True
        )
        meta = self._meta(body, discovery=discovery)
        if meta["id"] != pid:
            raise PixivError("Pixiv 作品 ID 不匹配")
        meta["statsFetchedAt"] = fetched_at
        meta["statsSource"] = "artwork_detail"
        return meta, body

    async def detail(self, reference: str) -> dict:
        async def run():
            meta, _ = await self._detail(reference)
            return meta

        return await self._operation(run)

    async def image(self, reference: str, page: int = 0) -> dict:
        async def run():
            meta, body = await self._detail(reference)
            if type(page) is not int or not 0 <= page < meta["pageCount"]:
                raise PixivError("Pixiv 页码超出范围（从 0 开始）")
            urls = body.get("urls")
            image_url = urls.get("regular") if isinstance(urls, dict) else None
            if page:
                pages = await self._json(f"/ajax/illust/{meta['id']}/pages?lang=zh")
                if (
                    not isinstance(pages, list)
                    or page >= len(pages)
                    or not isinstance(pages[page], dict)
                ):
                    raise PixivError("Pixiv 分页图片数据格式异常")
                urls = pages[page].get("urls")
                image_url = urls.get("regular") if isinstance(urls, dict) else None
            if not image_url:
                raise PixivError("该作品没有可发送的图片")
            data, mime = await self._bytes(
                image_url, image=True, maximum=self.max_image_bytes
            )
            verified_mime = self._verify_image(data, mime)
            return {"meta": meta, "data": data, "mime": verified_mime, "page": page}

        return await self._operation(run)

    @staticmethod
    def _verify_image(data: bytes, mime: str) -> str:
        allowed = {
            "JPEG": "image/jpeg",
            "PNG": "image/png",
            "WEBP": "image/webp",
            "GIF": "image/gif",
        }
        if mime not in set(allowed.values()):
            raise PixivError("Pixiv 返回的内容不是支持的图片 MIME 类型")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as picture:
                    verified = allowed.get(picture.format)
                    if verified != mime:
                        raise PixivError("Pixiv 图片 MIME 类型与内容不匹配")
                    if picture.width * picture.height > MAX_PIXELS:
                        raise PixivError("Pixiv 图片像素超过 6400 万限制")
                    picture.verify()
        except PixivError:
            raise
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ):
            raise PixivError("Pixiv 返回的内容不是有效图片或图片像素过大") from None
        return mime

    def clear_cache(self) -> int:
        count = len(self._cache)
        self._cache.clear()
        self._cache_bytes = 0
        return count

    def status(self) -> dict:
        return {
            **self._stats,
            "cache_entries": len(self._cache),
            "cache_bytes": self._cache_bytes,
            "proxy_mode": self.proxy_mode,
            "proxy_active": self._proxy_active,
            "exclude_ai": self.exclude_ai,
            "active_operations": len(self._tasks),
            "closed": self._closed,
        }

    async def diagnose(self) -> dict:
        started = time.monotonic()
        try:

            async def run():
                query = {
                    "word": "風景",
                    "order": "date_d",
                    "mode": "safe",
                    "p": "1",
                    "s_mode": "s_tag",
                    "type": "all",
                    "lang": "zh",
                }
                if self.exclude_ai:
                    query["ai_type"] = "1"
                body = await self._json(
                    "/ajax/search/artworks/"
                    + quote("風景", safe="")
                    + "?"
                    + urlencode(query),
                    use_cache=False,
                )
                section = body.get("illustManga") if isinstance(body, dict) else None
                if not isinstance(section, dict) or not isinstance(
                    section.get("data"), list
                ):
                    raise PixivError("Pixiv 搜索结果格式异常")
                return len(section["data"])

            item_count = await self._operation(run)
            return {
                "ok": True,
                "message": "Pixiv 搜索接口连接正常",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "item_count": item_count,
                **self.status(),
            }
        except PixivError as exc:
            return {
                "ok": False,
                "message": str(exc),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                **self.status(),
            }

    async def close(self):
        self._closed = True
        current = asyncio.current_task()
        tasks = [
            task for task in self._tasks if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.http.aclose()
        self.clear_cache()
