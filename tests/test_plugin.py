"""Exercise plugin orchestration without network access or a QQ adapter."""

import asyncio
import base64
import importlib.util
import inspect
import json
import logging
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import ClassVar

import docstring_parser
import pytest
from quart import Quart


class FakeFilter:
    """Retain registration metadata while checking real tool docstring parsing."""

    def __init__(self):
        self.tools = {}

    def llm_tool(self, *, name):
        def decorate(handler):
            parsed = docstring_parser.parse(handler.__doc__ or "")
            self.tools[name] = (handler, parsed)
            return handler

        return decorate

    @staticmethod
    def on_llm_request(**_kwargs):
        return lambda handler: handler

    @staticmethod
    def command(*_args, **_kwargs):
        return lambda handler: handler


class FakeStar:
    def __init__(self, context):
        self.context = context
        self.logger = logging.getLogger("pixiv-plugin-test")


class FakeChain:
    def __init__(self, chain):
        self.chain = chain


class FakePlain:
    def __init__(self, text):
        self.text = text


class FakeImage:
    def __init__(self, data):
        self.data = data

    @classmethod
    def fromBytes(cls, data):
        return cls(data)


class FakeEvent:
    def __init__(self, origin="qq:GroupMessage:group-a", *, stopped=False):
        self.unified_msg_origin = origin
        self.stopped = stopped
        self.sent = []
        self.send_error = None
        self.message = ""

    def is_stopped(self):
        return self.stopped

    def get_message_str(self):
        return self.message

    def plain_result(self, text):
        return text

    async def send(self, chain):
        if self.send_error:
            raise self.send_error
        self.sent.append(chain)


class FakeConfig(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.saved = []
        self.save_error = None

    async def save_config_async(self, snapshot):
        self.update(snapshot)
        if self.save_error:
            raise self.save_error
        self.saved.append(dict(snapshot))
        return True


class FakeClient:
    instances: ClassVar[list] = []

    def __init__(self, settings):
        self.settings = dict(settings)
        self.closed = False
        self.calls = []
        self.entered = None
        self.release = None
        self.error = None
        self.cleared = False
        self.instances.append(self)

    async def close(self):
        self.closed = True

    def status(self):
        return {"cache_entries": 0, "closed": self.closed}

    def clear_cache(self):
        self.cleared = True

    async def diagnose(self):
        return {"ok": True, "anonymous": True}

    async def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        if self.error:
            raise self.error
        return {
            "items": [dict(META)],
            "page": kwargs.get("page", 1),
            "keyword": query,
        }

    async def user_works(self, query, **kwargs):
        self.calls.append(("user", query, kwargs))
        if self.error:
            raise self.error
        return {"items": [dict(META)]}

    async def detail(self, query):
        self.calls.append(("detail", query, {}))
        if self.error:
            raise self.error
        return dict(META)

    async def image(self, query, *, page=0):
        self.calls.append(("image", query, {"page": page}))
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        if self.error:
            raise self.error
        return {
            "meta": dict(META),
            "page": page,
            "mime": "image/png",
            "data": b"fixture-png",
        }


META = {
    "id": "12345",
    "title": "Fixture landscape",
    "author": "Fixture artist",
    "authorId": "777",
    "pageCount": 2,
    "url": "https://www.pixiv.net/artworks/12345",
    "likeCount": 2864,
    "bookmarkCount": 3822,
    "viewCount": 19024,
    "statsFetchedAt": "2026-10-24T05:00:00Z",
}

POPULARITY_DEFAULTS = {
    "sort_by": "latest",
    "min_likes": 0,
    "min_bookmarks": 0,
    "min_views": 0,
}

TOOL_PARAMETERS = {
    "pixiv_search": {
        "keyword": "string",
        "count": "number",
        "page": "number",
        "sort_by": "string",
        "min_likes": "number",
        "min_bookmarks": "number",
        "min_views": "number",
        "scan_pages": "number",
    },
    "pixiv_user_works": {
        "reference": "string",
        "count": "number",
        "sort_by": "string",
        "min_likes": "number",
        "min_bookmarks": "number",
        "min_views": "number",
    },
    "pixiv_stats": {"reference": "string"},
    "pixiv_view": {"reference": "string", "page": "number"},
    "pixiv_send": {"reference": "string", "page": "number"},
}


@pytest.fixture
def plugin_module(monkeypatch):
    """Load the actual plugin with isolated AstrBot integration boundaries."""
    registry = FakeFilter()
    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.event",
            "astrbot.api.message_components",
            "astrbot.api.provider",
            "astrbot.api.star",
            "astrbot.core",
            "astrbot.core.star",
            "astrbot.core.star.filter",
            "astrbot.core.star.filter.command",
        )
    }
    modules["astrbot.api.event"].AstrMessageEvent = FakeEvent
    modules["astrbot.api.event"].MessageChain = FakeChain
    modules["astrbot.api.event"].filter = registry
    modules["astrbot.api.message_components"].Image = FakeImage
    modules["astrbot.api.message_components"].Plain = FakePlain
    modules["astrbot.api.provider"].ProviderRequest = object
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = FakeStar
    modules["astrbot.core.star.filter.command"].GreedyStr = str
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    name = "_pixiv_plugin_test"
    directory = Path(__file__).resolve().parents[1]
    package = types.ModuleType(name)
    package.__path__ = [str(directory)]
    monkeypatch.setitem(sys.modules, name, package)
    spec = importlib.util.spec_from_file_location(f"{name}.main", directory / "main.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PixivClient", FakeClient)
    FakeClient.instances = []
    module.test_registry = registry
    yield module
    for child in tuple(sys.modules):
        if child.startswith(f"{name}.") and child != spec.name:
            monkeypatch.delitem(sys.modules, child, raising=False)


@pytest.fixture
def plugin(plugin_module):
    context = types.SimpleNamespace(routes=[])
    context.register_web_api = lambda *args: context.routes.append(args)
    return plugin_module.PixivPlugin(context, FakeConfig())


def test_five_tool_registrations_have_parseable_documented_parameters(plugin_module):
    assert set(plugin_module.test_registry.tools) == set(TOOL_PARAMETERS)
    for name, parameters in TOOL_PARAMETERS.items():
        handler, parsed = plugin_module.test_registry.tools[name]
        assert parsed.description
        assert {item.arg_name: item.type_name for item in parsed.params} == parameters
        assert set(inspect.signature(handler).parameters) == {
            "self",
            "event",
            *parameters,
        }


def test_management_routes_are_registered_without_any_send_endpoint(plugin):
    routes = {
        path: methods for path, _handler, methods, _description in plugin.context.routes
    }
    assert routes["/astrbot_plugin_pixiv/state"] == ["GET"]
    assert routes["/astrbot_plugin_pixiv/settings"] == ["POST"]
    assert routes["/astrbot_plugin_pixiv/image"] == ["POST"]
    assert not any("send" in route for route in routes)


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"enabled": "false"}, "开关值"),
        ({"default_count": True}, "整数"),
        ({"default_count": 6}, "整数"),
        ({"timeout_seconds": 4}, "整数"),
        ({"cache_max_entries": 501}, "整数"),
        ({"max_concurrency": 0}, "整数"),
        ({"proxy_mode": "other"}, "代理"),
        ({"proxy_mode": "custom", "proxy_url": ""}, "代理地址"),
        ({"proxy_url": "http://user:secret@localhost:7897"}, "账号密码"),
        ({"proxy_url": "http://@localhost:7897"}, "账号密码"),
        ({"proxy_url": "http://localhost:0"}, "代理"),
        ({"proxy_url": "http://local host:7897"}, "代理"),
        ({"proxy_url": "http://local\\host:7897"}, "代理"),
        ({"proxy_url": "http://localhost:7897/path"}, "路径"),
        ({"proxy_url": "http://localhost:7897/?token=secret"}, "查询参数"),
    ],
)
def test_settings_reject_malformed_or_credential_values(plugin_module, patch, message):
    with pytest.raises((ValueError, TypeError), match=message):
        plugin_module.validate_settings(patch)


def test_settings_discard_unknown_values_and_normalize_proxy(plugin_module):
    settings = plugin_module.validate_settings(
        {
            "unknown": "discard",
            "proxy_mode": "custom",
            "proxy_url": " http://localhost:7897 ",
        }
    )
    assert "unknown" not in settings
    assert settings["proxy_url"] == "http://localhost:7897"
    assert settings["exclude_ai"] is True
    assert settings["enabled"] is True


@pytest.mark.parametrize(
    ("tool", "flag", "args"),
    [
        ("pixiv_search", "search_enabled", {"keyword": "landscape"}),
        ("pixiv_user_works", "user_enabled", {"reference": "777"}),
        ("pixiv_stats", "view_enabled", {"reference": "12345"}),
        ("pixiv_view", "view_enabled", {"reference": "12345"}),
        ("pixiv_send", "send_enabled", {"reference": "12345"}),
    ],
)
def test_disabled_tools_refuse_before_client_or_delivery(plugin, tool, flag, args):
    async def exercise():
        plugin.settings[flag] = False
        event = FakeEvent()
        result = json.loads(await getattr(plugin, tool)(event, **args))
        assert result["ok"] is False
        assert not plugin.client.calls
        assert not event.sent

    asyncio.run(exercise())


def test_paused_plugin_and_stopped_event_refuse_all_tools(plugin):
    async def exercise():
        for paused, stopped in ((True, False), (False, True)):
            plugin.settings["enabled"] = not paused
            event = FakeEvent(stopped=stopped)
            assert json.loads(await plugin.pixiv_search(event, "tag"))["ok"] is False
            assert (
                json.loads(await plugin.pixiv_user_works(event, "777"))["ok"] is False
            )
            assert json.loads(await plugin.pixiv_view(event, "12345"))["ok"] is False
            assert json.loads(await plugin.pixiv_stats(event, "12345"))["ok"] is False
            assert json.loads(await plugin.pixiv_send(event, "12345"))["sent"] == 0
        assert not plugin.client.calls

    asyncio.run(exercise())


def test_search_artist_and_visual_tool_return_actual_results_without_sending(plugin):
    async def exercise():
        event = FakeEvent()
        search = json.loads(
            await plugin.pixiv_search(event, "landscape", count=2, page=3)
        )
        author = json.loads(await plugin.pixiv_user_works(event, "777", count=1))
        view = await plugin.pixiv_view(event, "12345", page=1)
        assert search["items"][0]["id"] == "12345"
        assert author["items"][0]["authorId"] == "777"
        assert plugin.client.calls[:2] == [
            (
                "search",
                "landscape",
                {"count": 2, "page": 3, **POPULARITY_DEFAULTS, "scan_pages": 1},
            ),
            ("user", "777", {"count": 1, **POPULARITY_DEFAULTS}),
        ]
        assert json.loads(view.content[0].text)["page"] == 1
        assert view.content[1].type == "image"
        assert base64.b64decode(view.content[1].data) == b"fixture-png"
        assert not event.sent

    asyncio.run(exercise())


def test_popularity_tools_forward_every_filter_and_preserve_counts(plugin):
    async def exercise():
        event = FakeEvent()
        filters = {
            "sort_by": "bookmarks",
            "min_likes": 100,
            "min_bookmarks": 200,
            "min_views": 300,
        }
        search = json.loads(
            await plugin.pixiv_search(
                event, "風景", count=5, page=4, scan_pages=3, **filters
            )
        )
        artist = json.loads(
            await plugin.pixiv_user_works(event, "777", count=2, **filters)
        )
        assert plugin.client.calls == [
            (
                "search",
                "風景",
                {"count": 5, "page": 4, **filters, "scan_pages": 3},
            ),
            ("user", "777", {"count": 2, **filters}),
        ]
        for data in (search, artist):
            assert data["items"] == [META]
            assert data["items"][0]["likeCount"] != data["items"][0]["bookmarkCount"]
        assert not event.sent

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("arguments", "expected_keyword"),
    [
        (
            {"min_bookmarks": 10001, "sort_by": "bookmarks", "scan_pages": 3},
            "10000users入り",
        ),
        ({}, "オリジナル"),
        (
            {
                "keyword": "初音ミク",
                "min_bookmarks": 10001,
                "sort_by": "bookmarks",
                "scan_pages": 3,
            },
            "初音ミク",
        ),
        (
            {
                "keyword": " \t\n",
                "min_bookmarks": 10001,
                "sort_by": "bookmarks",
                "scan_pages": 3,
            },
            "10000users入り",
        ),
    ],
    ids=["omitted-high-bookmarks", "omitted-default", "explicit-tag", "blank-tag"],
)
def test_llm_search_without_subject_uses_explicit_bounded_default_tag(
    plugin, arguments, expected_keyword
):
    """Exercise the actual tool callsite used when the model omits a subject."""

    async def exercise():
        event = FakeEvent()
        result = json.loads(await plugin.pixiv_search(event, **arguments))
        expected = {
            "count": 3,
            "page": 1,
            **POPULARITY_DEFAULTS,
            "scan_pages": 1,
            **{key: value for key, value in arguments.items() if key != "keyword"},
        }
        assert plugin.client.calls == [("search", expected_keyword, expected)]
        assert result["keyword"] == expected_keyword
        assert result["items"] == [META]
        assert not event.sent

    asyncio.run(exercise())


@pytest.mark.parametrize("endpoint", ["search", "user"])
@pytest.mark.parametrize("custom_filters", [False, True])
def test_management_search_and_artist_forward_popularity_parameters(
    plugin, endpoint, custom_filters
):
    async def exercise():
        app = Quart(__name__)
        filters = (
            {
                "sort_by": "views",
                "min_likes": 321,
                "min_bookmarks": 654,
                "min_views": 987,
            }
            if custom_filters
            else dict(POPULARITY_DEFAULTS)
        )
        body = (
            {"keyword": "風景", "count": 4, "page": 7}
            if endpoint == "search"
            else {"reference": "777", "count": 4}
        )
        if custom_filters:
            body.update(filters)
            if endpoint == "search":
                body["scan_pages"] = 3
        async with app.test_request_context(
            f"/astrbot_plugin_pixiv/{endpoint}", method="POST", json=body
        ):
            response = await plugin.web_api()
            data = await response.get_json()
        expected = {"count": 4, **filters}
        if endpoint == "search":
            expected.update(page=7, scan_pages=3 if custom_filters else 1)
        assert data["ok"] is True
        assert data["items"] == [META]
        assert plugin.client.calls == [
            (endpoint, "風景" if endpoint == "search" else "777", expected)
        ]
        assert not plugin._send_times
        assert not plugin._sending

    asyncio.run(exercise())


def test_stats_uses_text_detail_only_without_image_download_or_delivery(plugin):
    async def exercise():
        event = FakeEvent()
        data = json.loads(await plugin.pixiv_stats(event, META["url"]))
        assert data == {"ok": True, "item": META}
        assert plugin.client.calls == [("detail", META["url"], {})]
        assert not event.sent
        assert not plugin._send_times
        assert not plugin._sending
        assert plugin.recent[0]["action"] == "detail"

    asyncio.run(exercise())


def test_stats_reports_lookup_failure_without_fabricating_counts(plugin, plugin_module):
    async def exercise():
        plugin.client.error = plugin_module.PixivError("Fixture unavailable artwork")
        event = FakeEvent()
        data = json.loads(await plugin.pixiv_stats(event, "12345"))
        assert data == {"ok": False, "message": "Fixture unavailable artwork"}
        assert plugin.client.calls == [("detail", "12345", {})]
        assert not event.sent

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (META, "点赞 2,864 · 收藏 3,822 · 浏览 19,024"),
        (
            {"likeCount": 0, "bookmarkCount": None, "viewCount": 1000},
            "点赞 0 · 收藏 未取得 · 浏览 1,000",
        ),
        (
            {"likeCount": None, "bookmarkCount": 0, "viewCount": None},
            "点赞 未取得 · 收藏 0 · 浏览 未取得",
        ),
        ({}, "点赞 未取得 · 收藏 未取得 · 浏览 未取得"),
        (
            {"likeCount": True, "bookmarkCount": "100", "viewCount": False},
            "点赞 未取得 · 收藏 未取得 · 浏览 未取得",
        ),
    ],
)
def test_stats_display_keeps_likes_bookmarks_views_distinct_and_unknowns_not_zero(
    plugin_module, meta, expected
):
    assert plugin_module.format_stats(meta) == expected


@pytest.mark.parametrize("command", ["search_command", "user_command"])
def test_lookup_commands_include_real_labeled_counts(plugin, command):
    async def exercise():
        event = FakeEvent()
        argument = "landscape" if command == "search_command" else "777"
        results = [item async for item in getattr(plugin, command)(event, argument)]
        assert len(results) == 1
        assert "点赞 2,864 · 收藏 3,822 · 浏览 19,024" in results[0]
        assert META["url"] in results[0]
        assert not event.sent

    asyncio.run(exercise())


def test_send_targets_only_current_event_and_keeps_attribution(
    plugin, plugin_module, monkeypatch
):
    async def exercise():
        monkeypatch.setattr(
            plugin_module,
            "time",
            types.SimpleNamespace(time=lambda: 10, monotonic=lambda: 10),
        )
        event = FakeEvent("qq:FriendMessage:friend-a")
        other = FakeEvent("qq:GroupMessage:group-b")
        result = json.loads(await plugin.pixiv_send(event, "12345", page=1))
        assert result["ok"] is True
        assert result["sent"] == 1
        assert len(event.sent) == 1
        assert not other.sent
        caption, image = event.sent[0].chain
        assert META["author"] in caption.text
        assert META["url"] in caption.text
        assert "第 2/2 页" in caption.text
        assert "点赞 2,864 · 收藏 3,822 · 浏览 19,024" in caption.text
        assert image.data == b"fixture-png"
        assert list(plugin._send_times) == [event.unified_msg_origin]
        assert not plugin._sending

    asyncio.run(exercise())


def test_per_session_cooldown_refuses_duplicate_but_not_other_chat(
    plugin, plugin_module, monkeypatch
):
    async def exercise():
        instant = [10]
        monkeypatch.setattr(
            plugin_module,
            "time",
            types.SimpleNamespace(
                time=lambda: instant[0], monotonic=lambda: instant[0]
            ),
        )
        event = FakeEvent()
        other = FakeEvent("qq:GroupMessage:group-b")
        assert (await plugin._send(event, "12345", 0))["sent"] == 1
        assert (await plugin._send(event, "12345", 0))["sent"] == 0
        assert (await plugin._send(other, "12345", 0))["sent"] == 1
        instant[0] += plugin.settings["send_cooldown_seconds"]
        assert (await plugin._send(event, "12345", 0))["sent"] == 1
        assert len(event.sent) == 2

    asyncio.run(exercise())


def test_same_chat_parallel_send_refuses_before_second_download(plugin):
    async def exercise():
        plugin.client.entered, plugin.client.release = asyncio.Event(), asyncio.Event()
        first, second = FakeEvent(), FakeEvent()
        running = asyncio.create_task(plugin._send(first, "12345", 0))
        await plugin.client.entered.wait()
        refused = await plugin._send(second, "12345", 0)
        assert refused["sent"] == 0
        assert len(plugin.client.calls) == 1
        plugin.client.release.set()
        assert (await running)["sent"] == 1
        assert len(first.sent) == 1
        assert not second.sent

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["pause", "disable_send", "stop_event"])
def test_send_rechecks_permissions_after_download(plugin, change):
    async def exercise():
        plugin.client.entered, plugin.client.release = asyncio.Event(), asyncio.Event()
        event = FakeEvent()
        running = asyncio.create_task(plugin._send(event, "12345", 0))
        await plugin.client.entered.wait()
        if change == "pause":
            plugin.settings["enabled"] = False
        elif change == "disable_send":
            plugin.settings["send_enabled"] = False
        else:
            event.stopped = True
        plugin.client.release.set()
        assert (await running)["sent"] == 0
        assert not event.sent
        assert not plugin._sending
        assert not plugin._send_times

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["download", "delivery"])
def test_failed_send_is_not_reported_successful_or_charged_cooldown(
    plugin, plugin_module, failure
):
    async def exercise():
        event = FakeEvent()
        if failure == "download":
            plugin.client.error = plugin_module.PixivError("Fixture blocked work")
        else:
            event.send_error = RuntimeError("Fixture adapter failure")
        result = await plugin._send(event, "12345", 0)
        assert result["ok"] is False
        assert result["sent"] == 0
        assert not event.sent
        assert not plugin._sending
        assert not plugin._send_times
        plugin.client.error = event.send_error = None
        assert (await plugin._send(event, "12345", 0))["sent"] == 1

    asyncio.run(exercise())


def test_cancelled_download_releases_session_and_allows_later_send(plugin):
    async def exercise():
        plugin.client.entered, plugin.client.release = asyncio.Event(), asyncio.Event()
        event = FakeEvent()
        running = asyncio.create_task(plugin._send(event, "12345", 0))
        await plugin.client.entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not event.sent
        assert not plugin._sending
        assert not plugin._send_times
        plugin.client.release.set()
        assert (await plugin._send(event, "12345", 0))["sent"] == 1

    asyncio.run(exercise())


def test_settings_save_swaps_client_only_after_success(plugin):
    async def exercise():
        app = Quart(__name__)
        previous = plugin.client
        async with app.test_request_context(
            "/astrbot_plugin_pixiv/settings",
            method="POST",
            json={"default_count": 5, "enabled": False},
        ):
            response = await plugin.web_api()
            data = await response.get_json()
        assert data["ok"] is True
        assert plugin.settings["default_count"] == 5
        assert plugin.settings["enabled"] is False
        assert plugin.config.saved[-1] == plugin.settings
        assert previous.closed
        assert plugin.client is not previous
        assert not plugin.client.closed

    asyncio.run(exercise())


def test_cancelled_platform_send_releases_session_without_claiming_success(plugin):
    async def exercise():
        event = FakeEvent()
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_send(_chain):
            entered.set()
            await release.wait()

        event.send = held_send
        running = asyncio.create_task(plugin._send(event, "12345", 0))
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not plugin._sending
        assert not plugin._send_times
        assert not event.sent

    asyncio.run(exercise())


def test_invalid_settings_leave_config_and_client_untouched(plugin):
    async def exercise():
        app = Quart(__name__)
        previous = plugin.client
        async with app.test_request_context(
            "/astrbot_plugin_pixiv/settings",
            method="POST",
            json={"max_concurrency": True},
        ):
            response, status = await plugin.web_api()
            data = await response.get_json()
        assert status == 400
        assert data["ok"] is False
        assert plugin.client is previous
        assert not previous.closed
        assert not plugin.config.saved

    asyncio.run(exercise())


def test_failed_persistence_keeps_previous_runtime_and_closes_replacement(plugin):
    async def exercise():
        app = Quart(__name__)
        previous = plugin.client
        settings = dict(plugin.settings)
        config_snapshot = dict(plugin.config)
        plugin.config.save_error = OSError("Fixture disk failure")
        async with app.test_request_context(
            "/astrbot_plugin_pixiv/settings", method="POST", json={"default_count": 5}
        ):
            response, status = await plugin.web_api()
            data = await response.get_json()
        assert status == 500
        assert data["ok"] is False
        assert plugin.client is previous
        assert plugin.settings == settings
        assert dict(plugin.config) == config_snapshot
        assert not previous.closed
        assert FakeClient.instances[-1].closed

    asyncio.run(exercise())


def test_successful_lookup_history_is_bounded_and_failures_are_recorded(
    plugin, plugin_module
):
    async def exercise():
        for _index in range(45):
            await plugin._lookup("search", "landscape")
        assert len(plugin.recent) == 40
        assert plugin.recent[0]["ok"] is True
        plugin.client.error = plugin_module.PixivError("Fixture lookup failure")
        with pytest.raises(plugin_module.PixivError):
            await plugin._lookup("search", "landscape")
        assert len(plugin.recent) == 40
        assert plugin.recent[0]["ok"] is False
        assert plugin.recent[0]["error"] == "Fixture lookup failure"

    asyncio.run(exercise())


def test_sync_settings_save_fallback_preserves_existing_config_fields(plugin):
    async def exercise():
        app = Quart(__name__)
        plugin.config["custom_field"] = "preserve"
        plugin.config.save_config_async = None
        plugin.config.save_config = lambda: plugin.config.saved.append(
            dict(plugin.config)
        )
        async with app.test_request_context(
            "/astrbot_plugin_pixiv/settings", method="POST", json={"default_count": 4}
        ):
            data = await (await plugin.web_api()).get_json()
        assert data["ok"] is True
        assert plugin.config.saved[-1]["default_count"] == 4
        assert plugin.config.saved[-1]["custom_field"] == "preserve"

    asyncio.run(exercise())


def test_management_preview_cache_clear_and_diagnosis_never_send(plugin):
    async def exercise():
        app = Quart(__name__)
        async with app.test_request_context(
            "/astrbot_plugin_pixiv/image",
            method="POST",
            json={"reference": "12345", "page": 1},
        ):
            preview = await (await plugin.web_api()).get_json()
        assert preview["ok"] is True
        assert preview["data_url"].startswith("data:image/png;base64,")
        assert "data" not in preview
        for endpoint in ("cache/clear", "diagnose"):
            async with app.test_request_context(
                f"/astrbot_plugin_pixiv/{endpoint}", method="POST", json={}
            ):
                assert (await (await plugin.web_api()).get_json())["ok"] is True
        assert plugin.client.cleared
        assert not plugin._sending
        assert not plugin._send_times

    asyncio.run(exercise())


def test_real_astrbot_import_registers_tools_in_isolated_process(tmp_path):
    """Import the actual AstrBot registries under a temporary data directory."""
    plugin_root = Path(__file__).resolve().parents[1]
    astrbot_root = (
        Path(os.environ["ASTRBOT_SOURCE"])
        if os.environ.get("ASTRBOT_SOURCE")
        else plugin_root.parents[1] / "Astrbot"
    )
    if not (astrbot_root / "astrbot").is_dir():
        pytest.skip("The local AstrBot source checkout is unavailable")
    script = """
import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[2]).parent))
import runtime_bootstrap
runtime_bootstrap.initialize_runtime_bootstrap()
from astrbot.core.provider.register import llm_tools
from astrbot.core.utils.astrbot_path import get_astrbot_root

assert Path(get_astrbot_root()).resolve() == Path(sys.argv[3]).resolve()
module = importlib.import_module("astrbot_plugin_pixiv.main")
tools = [tool for tool in llm_tools.func_list if tool.name.startswith("pixiv_")]
expected = json.loads(sys.argv[4])
assert {tool.name for tool in tools} == set(expected)
assert all(tool.description for tool in tools)
for tool in tools:
    assert tool.parameters["type"] == "object"
    properties = tool.parameters["properties"]
    assert {name: spec["type"] for name, spec in properties.items()} == expected[tool.name]
    assert all(spec.get("description") for spec in properties.values())
    assert "event" not in properties
    json.dumps(tool.parameters)
routes = []
context = SimpleNamespace(register_web_api=lambda *args: routes.append(args))
plugin = module.PixivPlugin(context, {})
assert len(routes) == 8
asyncio.run(plugin.terminate())
print(json.dumps({"ok": True, "tools": sorted(tool.name for tool in tools), "routes": len(routes)}))
"""
    env = dict(os.environ, ASTRBOT_ROOT=str(tmp_path), PYTHONUTF8="1", TESTING="true")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(astrbot_root),
            str(plugin_root),
            str(tmp_path),
            json.dumps(TOOL_PARAMETERS),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    assert json.loads(result.stdout.splitlines()[-1])["ok"] is True


def test_guidance_is_targeted_nonduplicated_and_respects_toggle(plugin):
    async def exercise():
        event = FakeEvent()
        req = types.SimpleNamespace(prompt="hello", system_prompt="persona")
        await plugin.add_guidance(event, req)
        assert req.system_prompt == "persona"
        event.message = "从P站找一张风景图"
        await plugin.add_guidance(event, req)
        await plugin.add_guidance(event, req)
        assert req.system_prompt.count("[Pixiv 搜图]") == 1
        plugin.settings["attach_guidance"] = False
        req.system_prompt = "persona"
        await plugin.add_guidance(event, req)
        assert req.system_prompt == "persona"

    asyncio.run(exercise())


def test_popularity_guidance_explains_metrics_sampling_and_retrieval_limits(plugin):
    async def exercise():
        event = FakeEvent()
        event.message = "P站找收藏最多而且点赞至少1000的风景图，再看观看次数"
        request = types.SimpleNamespace(prompt=event.message, system_prompt="persona")
        await plugin.add_guidance(event, request)
        guidance = request.system_prompt
        for instruction in (
            "likeCount 是点赞",
            "bookmarkCount 是收藏（喜欢）",
            "viewCount 是浏览",
            "缺失值不是0",
            "sort_by=likes",
            "bookmarks",
            "min_likes/min_bookmarks/min_views",
            "scan_pages最多3",
            "rankingScope",
            "scannedCandidates",
            "scannedPages",
            "不能声称是全站人气榜",
            "最多按需补查两次",
            "pixiv_stats",
            "无需下载图片",
            "缓存可能有延迟",
        ):
            assert instruction in guidance
        assert not plugin.client.calls
        assert not event.sent

    asyncio.run(exercise())


def test_stats_command_reports_counts_and_query_time_without_image_or_delivery(plugin):
    async def exercise():
        event = FakeEvent()
        results = [item async for item in plugin.stats_command(event, "12345")]
        assert len(results) == 1
        assert "点赞 2,864 · 收藏 3,822 · 浏览 19,024" in results[0]
        assert META["statsFetchedAt"] in results[0]
        assert META["url"] in results[0]
        assert plugin.client.calls == [("detail", "12345", {})]
        assert not event.sent
        assert not plugin._send_times

    asyncio.run(exercise())


def test_command_pages_are_one_based_and_invalid_page_never_downloads(plugin):
    async def exercise():
        event = FakeEvent()
        assert [item async for item in plugin.send_command(event, "12345", page=0)]
        assert not plugin.client.calls
        assert [
            item async for item in plugin.send_command(event, "12345", page=2)
        ] == []
        assert plugin.client.calls == [("image", "12345", {"page": 1})]
        assert len(event.sent) == 1

    asyncio.run(exercise())


def test_plugin_shutdown_closes_client(plugin):
    asyncio.run(plugin.terminate())
    assert plugin.client.closed
