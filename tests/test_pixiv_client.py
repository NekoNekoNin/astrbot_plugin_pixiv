"""Behavior and network-boundary tests for the native Pixiv client."""

import asyncio
import copy
import io
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pixiv_client import (
    PixivClient,
    PixivError,
    _PublicBackend,
    artwork_id,
    user_id,
    validate_pixiv_url,
    validate_proxy,
)

WORK = {
    "id": "12345",
    "title": "测试风景",
    "userName": "测试作者",
    "userId": "777",
    "xRestrict": 0,
    "aiType": 1,
    "illustType": 0,
    "pageCount": 2,
    "createDate": "2026-10-20T12:00:00Z",
    "tags": ["風景"],
    "likeCount": 10,
    "bookmarkCount": 20,
    "viewCount": 100,
    "urls": {"regular": "https://i.pximg.net/img-master/12345_p0.png"},
}


def png():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "#4499aa").save(output, format="PNG")
    return output.getvalue()


def response(body):
    return httpx.Response(200, json={"error": False, "body": body})


def client(handler, config=None):
    return PixivClient(
        {"proxy_mode": "direct", **(config or {})},
        transport=httpx.MockTransport(handler),
    )


def run(awaitable):
    return asyncio.run(awaitable)


def test_only_official_sources_and_reference_shapes():
    assert artwork_id("https://www.pixiv.net/en/artworks/12345?foo=1") == "12345"
    assert user_id("https://www.pixiv.net/users/777/illustrations") == "777"
    for invalid in [
        "0",
        "-1",
        "1/../../secret",
        "https://evil.test/artworks/1",
        "https://www.pixiv.net.evil.test/artworks/1",
        "file:///a",
        "https://www.pixiv.net/users/777",
    ]:
        with pytest.raises(PixivError):
            artwork_id(invalid)
    for invalid in [
        "torino",
        "0",
        "https://evil.test/users/777",
        "https://www.pixiv.net/artworks/777",
    ]:
        with pytest.raises(PixivError):
            user_id(invalid)
    for invalid in [
        "http://i.pximg.net/a",
        "https://i.pximg.net:444/a",
        "https://u:p@i.pximg.net/a",
        "https://127.0.0.1/a",
        "https://evil.pximg.net/a",
        "https://i.pximg.net/a#x",
        "https://i.pximg.net\\@localhost/a",
    ]:
        with pytest.raises(PixivError):
            validate_pixiv_url(invalid, image=True)
    assert validate_proxy("http://127.0.0.1:7897/") == "http://127.0.0.1:7897"
    assert validate_proxy("https://proxy.example:443") == "https://proxy.example:443"
    for invalid in [
        "http://user:secret@127.0.0.1:7897",
        "socks5://localhost:9999",
        "http://localhost/path",
        "http://localhost/?secret=1",
        "http://localhost:0",
        "",
    ]:
        with pytest.raises(PixivError) as error:
            validate_proxy(invalid)
        assert "secret" not in str(error.value)


def test_safe_search_filters_ai_unknown_ratings_before_count_and_deduplicates():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.startswith("/ajax/illust/"):
            return response({**WORK, "id": request.url.path.rsplit("/", 1)[-1]})
        data = [
            {**WORK, "id": str(100 + i), "aiType": marker}
            for i, marker in enumerate([2, "2", None, 0, True, False, {}, 1, "1"])
        ]
        data.extend(
            [
                {**WORK, "id": "200", "xRestrict": False},
                {**WORK, "id": "201", "xRestrict": None},
                {**WORK, "id": "202", "xRestrict": 1},
                {**WORK, "id": "203", "xRestrict": "2"},
            ]
        )
        data.append(data[-5])
        return response({"illustManga": {"data": data}})

    async def scenario():
        c = client(handler)
        result = await c.search("風景\n测试", count=99, page=99)
        assert [item["id"] for item in result["items"]] == ["107", "108"]
        assert result["keyword"] == "風景 测试"
        assert calls[0].url.params["mode"] == "safe"
        assert calls[0].url.params["ai_type"] == "1"
        assert calls[0].url.params["p"] == "20"
        assert calls[0].headers["referer"] == "https://www.pixiv.net/"
        assert "cookie" not in calls[0].headers
        await c.close()

    run(scenario())


def test_explicit_ai_setting_still_requires_safe_age():
    async def scenario():
        def handler(request):
            if request.url.path.startswith("/ajax/illust/"):
                return response({**WORK, "aiType": 2})
            assert "ai_type" not in request.url.params
            return response(
                {
                    "illustManga": {
                        "data": [
                            {**WORK, "aiType": 2},
                            {**WORK, "id": "2", "xRestrict": 1},
                            {**WORK, "id": "3", "aiType": None},
                            {**WORK, "id": "4", "aiType": False},
                        ]
                    }
                }
            )

        c = client(handler, {"exclude_ai": False})
        result = await c.search("風景")
        assert len(result["items"]) == 1
        assert result["items"][0]["aiGenerated"] is True
        await c.close()

    run(scenario())


def test_filtered_page_preserves_more_pages():
    async def scenario():
        c = client(
            lambda _: response(
                {
                    "illustManga": {
                        "data": [
                            {**WORK, "id": str(100 + n), "aiType": 2} for n in range(60)
                        ]
                    }
                }
            )
        )
        result = await c.search("風景")
        assert result["items"] == []
        assert result["hasMore"] is True
        await c.close()

    run(scenario())


def test_author_scan_limits_candidates_validates_ownership_and_sorts_dates():
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path.endswith("/profile/all"):
            return response(
                {"illusts": {str(n): None for n in range(12340, 12380)}, "manga": {}}
            )
        if path.endswith("/profile/illusts"):
            assert len(request.url.params.get_list("ids[]")) == 30
            return response(
                {
                    "works": {
                        "1": {
                            **WORK,
                            "id": "12370",
                            "createDate": "2026-10-02T12:00:00Z",
                        },
                        "2": {
                            **WORK,
                            "id": "12360",
                            "createDate": "2026-10-21T12:00:00Z",
                        },
                        "3": {**WORK, "id": "12379", "userId": "999"},
                        "4": {**WORK, "id": "12378", "aiType": 2},
                        "5": {**WORK, "id": "12377", "xRestrict": 1},
                        "6": {**WORK, "id": "99999"},
                    }
                }
            )
        if path.startswith("/ajax/illust/"):
            pid = path.rsplit("/", 1)[-1]
            return response(
                {
                    **WORK,
                    "id": pid,
                    "createDate": "2026-10-21T12:00:00Z"
                    if pid == "12360"
                    else "2026-10-02T12:00:00Z",
                }
            )
        return response({"userId": "777", "name": "测试作者"})

    async def scenario():
        c = client(handler)
        result = await c.user_works("https://www.pixiv.net/users/777/artworks")
        assert [item["id"] for item in result["items"]] == ["12360", "12370"]
        assert result["user"]["id"] == "777"
        assert result["scanned"] == 30
        assert result["moreCandidates"] is True
        assert len(calls) == 5
        await c.close()

    run(scenario())


def test_author_mismatch_fails():
    async def scenario():
        c = client(
            lambda request: response(
                {"illusts": {}}
                if request.url.path.endswith("/profile/all")
                else {"userId": "999"}
            )
        )
        with pytest.raises(PixivError, match="UID 不匹配"):
            await c.user_works("777")
        await c.close()

    run(scenario())


@pytest.mark.parametrize(
    "illusts,manga,expect_ids",
    [
        ({"12345": None}, [], ["12345"]),
        ([], {"12345": None}, ["12345"]),
        ([], [], []),
    ],
)
def test_real_profile_all_accepts_empty_array_maps(illusts, manga, expect_ids):
    async def scenario():
        def handler(request):
            if request.url.path.endswith("/profile/all"):
                return response({"illusts": illusts, "manga": manga})
            if request.url.path.endswith("/profile/illusts"):
                return response({"works": {"12345": WORK}})
            if request.url.path.startswith("/ajax/illust/"):
                return response(WORK)
            return response({"userId": "777", "name": "测试作者"})

        c = client(handler)
        result = await c.user_works("777")
        assert [item["id"] for item in result["items"]] == expect_ids
        await c.close()

    run(scenario())


def test_author_empty_detail_array_returns_no_available_works():
    async def scenario():
        def handler(request):
            if request.url.path.endswith("/profile/all"):
                return response({"illusts": {"12345": None}, "manga": []})
            if request.url.path.endswith("/profile/illusts"):
                return response({"works": []})
            return response({"userId": "777", "name": "测试作者"})

        c = client(handler)
        result = await c.user_works("777")
        assert result["items"] == []
        assert result["scanned"] == 1
        await c.close()

    run(scenario())


@pytest.mark.parametrize("field", ["illusts", "manga"])
def test_author_nonempty_array_maps_still_rejected(field):
    async def scenario():
        def handler(request):
            if request.url.path.endswith("/profile/all"):
                return response({"illusts": {}, "manga": {}, field: ["12345"]})
            return response({"userId": "777", "name": "测试作者"})

        c = client(handler)
        with pytest.raises(PixivError, match="列表格式"):
            await c.user_works("777")
        await c.close()

    run(scenario())


def test_image_page_download_mime_and_bytes():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "i.pximg.net":
            return httpx.Response(
                200, content=png(), headers={"Content-Type": "image/png"}
            )
        if request.url.path.endswith("/pages"):
            return response(
                [
                    {"urls": WORK["urls"]},
                    {"urls": {"regular": "https://i.pximg.net/second.png"}},
                ]
            )
        return response(WORK)

    async def scenario():
        c = client(handler)
        result = await c.image("12345", page=1)
        assert result["data"] == png()
        assert result["mime"] == "image/png"
        assert result["meta"]["id"] == "12345"
        assert calls[-1] == "https://i.pximg.net/second.png"
        for page in [2, -1, 0.0, True, "0"]:
            with pytest.raises(PixivError, match="页码"):
                await c.image("12345", page=page)
        await c.close()

    run(scenario())


@pytest.mark.parametrize(
    "override",
    [
        {"xRestrict": 1},
        {"xRestrict": None},
        {"xRestrict": False},
        {"id": "999"},
        {"urls": {"regular": "https://localhost/secret.png"}},
    ],
)
def test_unsafe_detail_never_downloads_image(override):
    async def scenario():
        def handler(request):
            assert request.url.host == "www.pixiv.net"
            return response({**WORK, **override})

        c = client(handler)
        with pytest.raises(PixivError):
            await c.image("12345")
        await c.close()

    run(scenario())


@pytest.mark.parametrize(
    "mime,content",
    [
        ("text/html", b"<html>captcha</html>"),
        ("image/png", b"broken image"),
        ("image/jpeg", png()),
    ],
)
def test_non_images_or_mime_mismatch_rejected(mime, content):
    async def scenario():
        c = client(
            lambda request: (
                response(WORK)
                if request.url.host == "www.pixiv.net"
                else httpx.Response(
                    200, content=content, headers={"Content-Type": mime}
                )
            )
        )
        with pytest.raises(PixivError, match="图片"):
            await c.image("12345")
        await c.close()

    run(scenario())


def test_image_dimensions_and_declared_size_are_bounded():
    async def scenario():
        c = client(
            lambda request: (
                response(WORK)
                if request.url.host == "www.pixiv.net"
                else httpx.Response(
                    200,
                    content=png(),
                    headers={
                        "Content-Type": "image/png",
                        "Content-Length": str(9 * 1024 * 1024),
                    },
                )
            )
        )
        with pytest.raises(PixivError, match="大小限制"):
            await c.image("12345")
        await c.close()

    run(scenario())
    with Image.new("1", (8001, 8000)) as big:
        output = io.BytesIO()
        big.save(output, format="PNG")
    with pytest.raises(PixivError, match="像素"):
        PixivClient._verify_image(output.getvalue(), "image/png")


def test_html_challenge_and_redirect_are_safe_clear_failures():
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://localhost/secret"})

        c = client(handler)
        with pytest.raises(PixivError, match="HTTP 302"):
            await c.search("風景")
        assert len(calls) == 1
        await c.close()
        c = client(lambda _: httpx.Response(200, text="<html>captcha</html>"))
        with pytest.raises(PixivError, match="验证"):
            await c.search("風景")
        await c.close()

    run(scenario())


def test_cache_reuses_metadata_but_does_not_leak_mutations_or_store_images():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "i.pximg.net":
            return httpx.Response(
                200, content=png(), headers={"Content-Type": "image/png"}
            )
        return response(copy.deepcopy(WORK))

    async def scenario():
        c = client(handler)
        first = await c.detail("12345")
        first["tags"].append("mutated")
        second = await c.detail("12345")
        assert second["tags"] == ["風景"]
        await c.image("12345")
        await c.image("12345")
        assert len(calls) == 3
        assert c.status()["cache_hits"] == 3
        assert c.status()["cache_entries"] == 1
        assert c.clear_cache() == 1
        assert c.status()["cache_bytes"] == 0
        await c.close()

    run(scenario())


def test_total_deadline_cancels_request_and_close_cancels_inflight():
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def hanging(_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        c = client(hanging)
        c.timeout = 0.03
        with pytest.raises(PixivError, match="超时"):
            await c.search("風景")
        assert cancelled.is_set()
        assert c.status()["active_operations"] == 0
        started.clear()
        c.timeout = 10
        task = asyncio.create_task(c.search("風景"))
        await started.wait()
        await c.close()
        assert task.cancelled()
        with pytest.raises(PixivError, match="关闭"):
            await c.search("風景")

    run(scenario())


def test_public_dns_backend_rejects_private_and_pins_public_address():
    async def scenario():
        async def private(_):
            return ["127.0.0.1"]

        backend = _PublicBackend(private)
        with pytest.raises(PixivError, match="非公网"):
            await backend.connect_tcp("www.pixiv.net", 443)

        async def public(_):
            return ["93.184.216.34"]

        backend = _PublicBackend(public)
        addresses = []

        class Fake:
            async def connect_tcp(self, host, port, **kwargs):
                addresses.append((host, port))
                return object()

        backend.backend = Fake()
        await backend.connect_tcp("www.pixiv.net", 443)
        assert addresses == [("93.184.216.34", 443)]
        with pytest.raises(PixivError):
            await backend.connect_tcp("localhost", 443)

    run(scenario())


def test_cache_lru_and_disabled_cache():
    async def scenario():
        calls = []

        def handler(request):
            calls.append(request)
            pid = request.url.path.rsplit("/", 1)[-1]
            return response({**WORK, "id": pid})

        c = client(handler, {"cache_max_entries": 1})
        await c.detail("12345")
        await c.detail("12346")
        await c.detail("12345")
        assert len(calls) == 3
        assert c.status()["cache_entries"] == 1
        await c.close()
        c = client(handler, {"cache_ttl_seconds": 0})
        await c.detail("12345")
        await c.detail("12345")
        assert c.status()["cache_entries"] == 0
        await c.close()

    run(scenario())


def test_diagnosis_bypasses_cached_search_and_never_reports_raw_proxy_error():
    async def scenario():
        fail = False

        def handler(request):
            if fail:
                raise httpx.ConnectError("http://user:secret@localhost:1234 refused")
            if request.url.path.startswith("/ajax/illust/"):
                return response(WORK)
            return response({"illustManga": {"data": [WORK]}})

        c = client(handler)
        await c.search("風景")
        fail = True
        result = await c.diagnose()
        assert result["ok"] is False
        assert "secret" not in str(result)
        assert c.status()["requests"] == 3
        await c.close()

    run(scenario())


def test_multistage_operation_shares_one_total_deadline():
    async def scenario():
        async def handler(request):
            await asyncio.sleep(0.3)
            return (
                response(WORK)
                if request.url.host == "www.pixiv.net"
                else httpx.Response(
                    200, content=png(), headers={"Content-Type": "image/png"}
                )
            )

        c = client(handler)
        c.timeout = 0.5
        with pytest.raises(PixivError, match="超时"):
            await c.image("12345")
        assert c.status()["requests"] == 2
        await c.close()

    run(scenario())


@pytest.mark.parametrize("bad", [None, True, False, -1, 1.5, "10", {}, []])
def test_detail_counters_preserve_unknown_instead_of_inventing_zero(bad):
    async def scenario():
        c = client(
            lambda _: response(
                {**WORK, "likeCount": 0, "bookmarkCount": bad, "viewCount": bad}
            )
        )
        result = await c.detail("12345")
        assert result["likeCount"] == 0
        assert result["bookmarkCount"] is None
        assert result["viewCount"] is None
        assert result["statsSource"] == "artwork_detail"
        assert result["statsFetchedAt"].endswith("+00:00")
        await c.close()

    run(scenario())


def test_missing_detail_counters_and_cached_statistics_timestamp():
    async def scenario():
        def handler(_):
            work = {
                key: value
                for key, value in WORK.items()
                if key not in {"likeCount", "bookmarkCount", "viewCount"}
            }
            return response(work)

        c = client(handler)
        first = await c.detail("12345")
        await asyncio.sleep(0.001)
        second = await c.detail("12345")
        assert all(
            first[key] is None for key in ["likeCount", "bookmarkCount", "viewCount"]
        )
        assert second["statsFetchedAt"] == first["statsFetchedAt"]
        c.clear_cache()
        third = await c.detail("12345")
        assert third["statsFetchedAt"] != first["statsFetchedAt"]
        await c.close()

    run(scenario())


def test_default_latest_enriches_only_selected_count_and_ignores_list_counters():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.startswith("/ajax/search/"):
            return response(
                {
                    "illustManga": {
                        "data": [
                            {**WORK, "id": str(n), "likeCount": 9999}
                            for n in range(100, 160)
                        ]
                    }
                }
            )
        return response(
            {**WORK, "id": request.url.path.rsplit("/", 1)[-1], "likeCount": 0}
        )

    async def scenario():
        c = client(handler)
        result = await c.search("風景 1000users入り", count=2)
        assert [item["id"] for item in result["items"]] == ["100", "101"]
        assert all(item["likeCount"] == 0 for item in result["items"])
        assert result["scannedCandidates"] == 2
        assert result["rankingScope"] == "latest_results"
        assert len(calls) == 3
        await c.close()

    run(scenario())


@pytest.mark.parametrize(
    "sort_by,expected",
    [
        ("likes", ["102", "104"]),
        ("bookmarks", ["104", "103"]),
        ("views", ["103", "104"]),
    ],
)
def test_popularity_sorts_authoritative_details_before_limiting(sort_by, expected):
    works = {
        "101": {
            **WORK,
            "id": "101",
            "likeCount": None,
            "bookmarkCount": None,
            "viewCount": None,
        },
        "102": {
            **WORK,
            "id": "102",
            "likeCount": 100,
            "bookmarkCount": 1,
            "viewCount": 1,
        },
        "103": {
            **WORK,
            "id": "103",
            "likeCount": 3,
            "bookmarkCount": 100,
            "viewCount": 1000,
        },
        "104": {
            **WORK,
            "id": "104",
            "likeCount": 3,
            "bookmarkCount": 100,
            "viewCount": 100,
        },
    }

    async def scenario():
        c = client(
            lambda request: (
                response(
                    {
                        "illustManga": {
                            "data": [
                                {**work, "likeCount": 999999} for work in works.values()
                            ]
                        }
                    }
                )
                if request.url.path.startswith("/ajax/search/")
                else response(works[request.url.path.rsplit("/", 1)[-1]])
            )
        )
        result = await c.search("風景", count=2, sort_by=sort_by)
        assert [item["id"] for item in result["items"]] == expected
        assert result["rankingScope"] == "sampled_candidates"
        assert result["scannedCandidates"] == 4
        result = await c.search("風景", count=5, sort_by=sort_by)
        assert result["items"][-1]["id"] == "101"
        await c.close()

    run(scenario())


def test_thresholds_exclude_unknown_and_use_inclusive_real_values():
    async def scenario():
        works = [
            {
                **WORK,
                "id": "101",
                "likeCount": 100,
                "bookmarkCount": 50,
                "viewCount": 1000,
            },
            {
                **WORK,
                "id": "102",
                "likeCount": 99,
                "bookmarkCount": 50,
                "viewCount": 1000,
            },
            {
                **WORK,
                "id": "103",
                "likeCount": None,
                "bookmarkCount": 50,
                "viewCount": 1000,
            },
            {
                **WORK,
                "id": "104",
                "likeCount": 100,
                "bookmarkCount": 50,
                "viewCount": None,
            },
        ]
        c = client(
            lambda request: (
                response({"illustManga": {"data": works}})
                if request.url.path.startswith("/ajax/search/")
                else response(
                    next(
                        work
                        for work in works
                        if work["id"] == request.url.path.rsplit("/", 1)[-1]
                    )
                )
            )
        )
        result = await c.search("風景", min_likes=100, min_bookmarks=50, min_views=1000)
        assert [item["id"] for item in result["items"]] == ["101"]
        assert result["scannedCandidates"] == 4
        await c.close()

    run(scenario())


@pytest.mark.parametrize(
    "options",
    [
        {"sort_by": "popular_d"},
        {"sort_by": []},
        {"min_likes": True},
        {"min_likes": "1"},
        {"min_bookmarks": -1},
        {"min_views": 1_000_000_001},
        {"scan_pages": 0},
        {"scan_pages": 4},
        {"scan_pages": 1.0},
    ],
)
def test_invalid_popularity_options_fail_before_network(options):
    async def scenario():
        c = client(lambda _: pytest.fail("invalid options must not access network"))
        with pytest.raises(PixivError):
            await c.search("風景", **options)
        if "scan_pages" not in options:
            with pytest.raises(PixivError):
                await c.user_works("777", **options)
        await c.close()

    run(scenario())


def test_three_page_sample_is_deduplicated_capped_and_network_concurrency_bounded():
    async def scenario():
        active = peak = 0
        detail_ids = []

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.001)
                if request.url.path.startswith("/ajax/search/"):
                    page = int(request.url.params["p"])
                    works = [{**WORK, "id": str(page * 100 + n)} for n in range(1, 61)]
                    if page == 2:
                        works[0]["id"] = "101"
                    return response({"illustManga": {"data": works, "total": 1000}})
                pid = request.url.path.rsplit("/", 1)[-1]
                detail_ids.append(pid)
                return response({**WORK, "id": pid, "bookmarkCount": int(pid)})
            finally:
                active -= 1

        c = client(handler, {"max_concurrency": 2})
        result = await c.search("風景", sort_by="bookmarks", scan_pages=3, count=5)
        assert result["scannedPages"] == 3
        assert result["nextPage"] == 4
        assert result["candidatePoolSize"] == 179
        assert (
            result["scannedCandidates"] == len(detail_ids) == len(set(detail_ids)) == 30
        )
        assert {pid[0] for pid in detail_ids} == {"1", "2", "3"}
        assert peak == 2
        assert all(item["id"].startswith("3") for item in result["items"])
        await c.close()

    run(scenario())


def test_partial_detail_failures_never_reuse_list_stats_or_unsafe_details():
    async def scenario():
        works = [
            {**WORK, "id": str(pid), "bookmarkCount": 999999} for pid in range(101, 106)
        ]

        def handler(request):
            if request.url.path.startswith("/ajax/search/"):
                return response({"illustManga": {"data": works}})
            pid = request.url.path.rsplit("/", 1)[-1]
            if pid == "101":
                return httpx.Response(404)
            if pid == "102":
                raise httpx.ConnectError("private proxy detail")
            if pid == "103":
                return response({**WORK, "id": pid, "aiType": 2})
            if pid == "104":
                return response({**WORK, "id": pid, "xRestrict": 1})
            return response({**WORK, "id": pid, "bookmarkCount": 3})

        c = client(handler)
        result = await c.search("風景", sort_by="bookmarks", min_bookmarks=100)
        assert result["items"] == []
        assert result["detailFailures"] == 4
        assert result["partial"] is True
        assert "private proxy" not in str(result)
        await c.close()

    run(scenario())


def test_author_popularity_filters_and_limits_recent_candidates():
    async def scenario():
        def handler(request):
            path = request.url.path
            if path.endswith("/profile/all"):
                return response(
                    {"illusts": {str(n): None for n in range(100, 140)}, "manga": {}}
                )
            if path.endswith("/profile/illusts"):
                return response(
                    {"works": {str(n): {**WORK, "id": str(n)} for n in range(110, 140)}}
                )
            if path.startswith("/ajax/illust/"):
                pid = path.rsplit("/", 1)[-1]
                return response(
                    {
                        **WORK,
                        "id": pid,
                        "likeCount": 1000 - int(pid),
                        "userId": "999" if pid == "110" else "777",
                    }
                )
            return response({"userId": "777", "name": "测试作者"})

        c = client(handler)
        result = await c.user_works("777", count=2, sort_by="likes", min_likes=880)
        assert [item["id"] for item in result["items"]] == ["111", "112"]
        assert result["scannedCandidates"] == 30
        assert result["detailFailures"] == 1
        assert result["rankingScope"] == "author_recent_30"
        await c.close()

    run(scenario())


def test_popularity_detail_batch_uses_one_total_deadline_and_cancels_children():
    async def scenario():
        active = 0

        async def handler(request):
            nonlocal active
            if request.url.path.startswith("/ajax/search/"):
                return response(
                    {
                        "illustManga": {
                            "data": [{**WORK, "id": str(n)} for n in range(100, 130)]
                        }
                    }
                )
            active += 1
            try:
                await asyncio.sleep(1)
                return response(WORK)
            finally:
                active -= 1

        c = client(handler, {"max_concurrency": 2})
        c.timeout = 0.03
        with pytest.raises(PixivError, match="超时"):
            await c.search("風景", sort_by="likes", scan_pages=3)
        assert active == 0
        assert c.status()["active_operations"] == 0
        assert c.status()["requests"] == 3
        await c.close()

    run(scenario())


def test_parallel_operations_share_network_limit_and_count_cached_bytes_once():
    async def scenario():
        active = peak = 0

        async def handler(_):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.002)
                return response(WORK)
            finally:
                active -= 1

        c = client(handler, {"max_concurrency": 2})
        await asyncio.gather(*(c.detail("12345") for _ in range(4)))
        assert peak == 2
        assert c.status()["cache_entries"] == 1
        assert c.status()["cache_bytes"] == sum(entry[2] for entry in c._cache.values())
        await c.close()

    run(scenario())


def test_default_latest_ignores_public_popular_preview():
    async def scenario():
        requested = []

        def handler(request):
            if request.url.path.startswith("/ajax/search/"):
                return response(
                    {
                        "illustManga": {"data": [WORK]},
                        "popular": {
                            "permanent": [{**WORK, "id": "999", "bookmarkCount": 10000}]
                        },
                    }
                )
            pid = request.url.path.rsplit("/", 1)[-1]
            requested.append(pid)
            return response(WORK)

        c = client(handler)
        result = await c.search("風景")
        assert requested == ["12345"]
        assert result["popularPreviewCandidates"] == 0
        assert result["candidateSources"] == ["search_pages"]
        await c.close()

    run(scenario())


def test_ranked_public_preview_finds_high_bookmarks_and_revalidates_safety():
    async def scenario():
        requested = []
        high = {**WORK, "id": "999", "bookmarkCount": 8744}

        def handler(request):
            if request.url.path.startswith("/ajax/search/"):
                assert request.url.params["order"] == "date_d"
                return response(
                    {
                        "illustManga": {"data": [WORK, high]},
                        "popular": {
                            "recent": [
                                {**WORK, "id": "500", "aiType": 2},
                                {**WORK, "id": "501", "xRestrict": 1},
                                {**WORK, "id": "502", "aiType": 0},
                                {**WORK, "id": "503", "aiType": None},
                            ],
                            "permanent": [high, high, {**WORK, "id": "504"}],
                        },
                    }
                )
            pid = request.url.path.rsplit("/", 1)[-1]
            requested.append(pid)
            if pid == "999":
                return response(high)
            if pid == "504":
                return response({**WORK, "id": pid, "xRestrict": 1})
            return response(WORK)

        c = client(handler)
        result = await c.search("風景", sort_by="bookmarks", min_bookmarks=1000)
        assert [item["id"] for item in result["items"]] == ["999"]
        assert sorted(requested) == ["12345", "504", "999"]
        assert result["popularPreviewCandidates"] == 2
        assert result["candidatePoolSize"] == 3
        assert result["detailFailures"] == 1
        assert result["candidateSources"] == [
            "popular_preview_permanent",
            "search_pages",
        ]
        assert "公开人气预览" in result["statsNote"]
        await c.close()

    run(scenario())


def test_popular_preview_seed_limit_reserves_twenty_regular_candidates():
    async def scenario():
        requested = []
        regular = [{**WORK, "id": str(n)} for n in range(100, 160)]

        def handler(request):
            if request.url.path.startswith("/ajax/search/"):
                return response(
                    {
                        "illustManga": {"data": regular},
                        "popular": {
                            "recent": [{**WORK, "id": str(n)} for n in range(200, 210)],
                            "permanent": [
                                {**WORK, "id": str(n)} for n in range(300, 310)
                            ],
                        },
                    }
                )
            pid = request.url.path.rsplit("/", 1)[-1]
            requested.append(pid)
            return response({**WORK, "id": pid})

        c = client(handler)
        result = await c.search("風景", sort_by="likes")
        assert len(requested) == len(set(requested)) == 30
        assert sum(100 <= int(pid) < 200 for pid in requested) == 20
        assert sum(200 <= int(pid) < 400 for pid in requested) == 10
        assert result["popularPreviewCandidates"] == 10
        assert result["candidatePoolSize"] == 70
        await c.close()

    run(scenario())


def test_later_search_pages_do_not_repeat_popular_preview_seeds():
    async def scenario():
        def handler(request):
            if request.url.path.startswith("/ajax/search/"):
                return response(
                    {
                        "illustManga": {"data": [WORK]},
                        "popular": {"permanent": [{**WORK, "id": "999"}]},
                    }
                )
            assert request.url.path.rsplit("/", 1)[-1] != "999"
            return response(WORK)

        c = client(handler)
        result = await c.search("風景", page=2, sort_by="bookmarks")
        assert result["popularPreviewCandidates"] == 0
        assert result["candidateSources"] == ["search_pages"]
        await c.close()

    run(scenario())
