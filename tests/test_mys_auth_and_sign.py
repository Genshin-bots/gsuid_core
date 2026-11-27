"""米游社 Token 交换、设备注册与原神签到契约回归测试。"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable

import pytest

from gsuid_core.bot import Bot, _Bot
from tests._support import isolated_sqlite_db
from gsuid_core.models import Event
from gsuid_core.utils.sign.sign import sign_in, sign_error
from gsuid_core.utils.api.mys.api import (
    BBS_LIKE,
    BBS_SIGN,
    BBS_TASKS,
    NEW_BBS_URL,
    BBS_TASKS_WEB,
    EXCHANGE_TOKEN,
    LOGIN_BY_AUTH_TICKET,
    CREATE_AUTH_TICKET_BY_GAME_BIZ,
)
from gsuid_core.utils.api.mys_api import mys_api
from gsuid_core.utils.database.models import GsUID, GsBind, GsUser
from gsuid_core.utils.cookie_manager.add_ck import _deal_ck, build_cn_account_cookie
from gsuid_core.utils.cookie_manager.add_fp import deal_fp
from gsuid_core.utils.cookie_manager.qrlogin import qrcode_login
from gsuid_core.buildin_plugins.core_command.user_login import send_add_device_msg


@dataclass
class _CapturedRequest:
    url: str
    method: str
    header: dict[str, str]
    params: dict[str, object] | None
    data: dict[str, object] | None
    base_url: str | None


class _RecordingBot(Bot):
    def __init__(self, ev: Event) -> None:
        super().__init__(_Bot("test_bot"), ev)
        self.sent: list[object] = []

    async def send(self, message: object, at_sender: bool = False) -> None:
        self.sent.append(message)


def _str_headers(header: dict[str, object] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if header is None:
        return out
    for k, v in header.items():
        if isinstance(v, str):
            out[k] = v
    return out


_RequestResponder = Callable[[_CapturedRequest], dict[str, object] | int]


def _mock_mys_request(
    monkeypatch: pytest.MonkeyPatch,
    responder: _RequestResponder,
) -> list[_CapturedRequest]:
    captured: list[_CapturedRequest] = []

    async def _fake_request(
        url: str,
        method: str = "GET",
        header: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        use_proxy: bool | None = False,
        base_url: str | None = None,
        game_name: str | None = None,
        time_out: int | None = 300,
    ) -> dict[str, object] | int:
        req = _CapturedRequest(url, method, _str_headers(header), params, data, base_url)
        captured.append(req)
        return responder(req)

    monkeypatch.setattr(mys_api, "_mys_request", _fake_request)
    return captured


def _exchange_token_response(
    data: dict[str, object] | None,
    cookie_token: str,
    ltoken: str,
) -> dict[str, object]:
    assert data is not None and "dst_token_type" in data
    dst = data["dst_token_type"]
    tok = cookie_token if dst == 4 else ltoken
    return {
        "retcode": 0,
        "message": "OK",
        "data": {
            "token": {"token_type": dst, "token": tok},
            "user_info": {"aid": "20000001", "mid": "test_mid_mhy"},
        },
    }


def test_cn_stoken_exchange_and_v2_cookie_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert BBS_TASKS.get().startswith(NEW_BBS_URL)
    assert BBS_SIGN.get().startswith(NEW_BBS_URL)
    assert BBS_LIKE.get().startswith(NEW_BBS_URL)

    def _respond(req: _CapturedRequest) -> dict[str, object]:
        assert req.url == EXCHANGE_TOKEN.get()
        assert req.method == "POST"
        return _exchange_token_response(req.data, "v2_test_cookie_token=.CAE=", "v2_test_ltoken=.CAE=")

    captured = _mock_mys_request(monkeypatch, _respond)

    async def scenario() -> None:
        sk = "stuid=20000001;stoken=v2_test_stoken=.CAE=;mid=test_mid_mhy"
        res = await mys_api.get_cookie_token_by_stoken("v2_test_stoken=.CAE=", "20000001", sk)
        assert isinstance(res, dict)
        assert res["cookie_token"] == "v2_test_cookie_token=.CAE="
        assert "cookies" in res
        cookies = res["cookies"]
        assert cookies["cookie_token_v2"] == "v2_test_cookie_token=.CAE="
        assert cookies["account_id_v2"] == "20000001"
        assert cookies["account_mid_v2"] == "test_mid_mhy"
        assert cookies["ltoken_v2"] == "v2_test_ltoken=.CAE="
        assert cookies["ltuid_v2"] == "20000001"
        assert cookies["ltmid_v2"] == "test_mid_mhy"

        assert len(captured) == 2
        assert captured[0].header["x-rpc-client_type"] == "2"
        assert captured[0].header["x-rpc-app_id"] == "bll8iq97cem8"
        assert captured[0].header["User-Agent"] == "okhttp/4.9.3"
        assert "DS" in captured[0].header

        web_cookie = build_cn_account_cookie("20000001", res["cookie_token"], cookies, "test_mid_mhy")
        for expected in (
            "account_id=20000001",
            "cookie_token=v2_test_cookie_token=.CAE=",
            "account_id_v2=20000001",
            "cookie_token_v2=v2_test_cookie_token=.CAE=",
            "account_mid_v2=test_mid_mhy",
            "ltoken_v2=v2_test_ltoken=.CAE=",
            "ltuid_v2=20000001",
            "ltmid_v2=test_mid_mhy",
        ):
            assert expected in web_cookie
        assert "stoken=" not in web_cookie

    asyncio.run(scenario())


def test_device_login_uses_stoken_and_updates_exact_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fail_mode = False
    captured = _mock_mys_request(
        monkeypatch,
        lambda _req: -100 if fail_mode else {"retcode": 0, "message": "OK", "data": {}},
    )

    async def scenario() -> None:
        nonlocal fail_mode
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="mys_test.db"):
            # 两个仅绑星铁（uid=None）的独立账号，校验更新不误伤他人
            await GsUser.insert_data(
                user_id="100000001",
                bot_id="test_bot",
                uid=None,
                sr_uid="100000002",
                mys_id="20000001",
                cookie="account_id=20000001;cookie_token=old_ck",
                stoken="stuid=20000001;stoken=v2_owner_stoken=.CAE=;mid=owner_mid",
            )
            await GsUser.insert_data(
                user_id="100000099",
                bot_id="test_bot",
                uid=None,
                sr_uid="100000098",
                mys_id="20000099",
                cookie="account_id=20000099;cookie_token=other_ck",
                stoken="stuid=20000099;stoken=v2_other_stoken=.CAE=;mid=other_mid",
                fp="keep_other_fp",
                device_id="keep-other-device",
            )

            payload = json.dumps(
                {
                    "fp": "test_fp_00001",
                    "device_id": "11111111-2222-3333-4444-555555555555",
                    "device_info": "TestBrand/TestModel/TestDev:16/TEST.001",
                }
            )
            ev = Event(bot_id="test_bot", user_id="100000001", text=payload)
            bot = _RecordingBot(ev)
            await send_add_device_msg(bot, ev)

            assert bot.sent == ["设备绑定成功!"]
            assert len(captured) == 2
            assert captured[0].url.endswith("/apihub/api/deviceLogin")
            assert captured[1].url.endswith("/apihub/api/saveDevice")
            assert "stoken=v2_owner_stoken=.CAE=" in captured[0].header["Cookie"]
            assert captured[0].header["x-rpc-client_type"] == "2"
            assert captured[0].header["User-Agent"] == "okhttp/4.9.3"
            assert captured[0].data is not None and captured[0].data["os_version"] == "36"

            owner = await GsUser.select_data_by_uid("100000002", "sr")
            other = await GsUser.select_data_by_uid("100000098", "sr")
            assert owner is not None and owner.fp == "test_fp_00001"
            assert other is not None and other.fp == "keep_other_fp"

            # stoken 轮换或仅刷新 CK 时按 mys_id 保留先前绑定的真实设备 fp/device_id
            kept_fp, kept_did, _, _ = await mys_api.ck_in_new_device(
                "0",
                "stuid=20000001;stoken=v2_rotated_stoken=.CAE=;mid=owner_mid",
                mys_id="20000001",
            )
            assert kept_fp == "test_fp_00001"
            assert kept_did == "11111111-2222-3333-4444-555555555555"

            kept_fp2, kept_did2, _, _ = await mys_api.ck_in_new_device("0", "", mys_id="20000001")
            assert (kept_fp2, kept_did2) == ("test_fp_00001", "11111111-2222-3333-4444-555555555555")

            # 空 fp 不应遮蔽有效的 device_fp；空 fp/device_id 模板仍应拒绝
            resolved = await deal_fp(
                {
                    "fp": "",
                    "device_fp": "test_fp_00002",
                    "device_id": "   ",
                    "deviceId": "22222222-2222-3333-4444-555555555555",
                }
            )
            assert resolved[:2] == ("test_fp_00002", "22222222-2222-3333-4444-555555555555")
            with pytest.raises(ValueError):
                await deal_fp({"fp": "", "device_id": ""})

            # 注册接口返回失败时不再误报成功
            fail_mode = True
            bot_fail = _RecordingBot(ev)
            await send_add_device_msg(bot_fail, ev)
            assert len(bot_fail.sent) == 1
            assert isinstance(bot_fail.sent[0], str)
            assert "失败" in bot_fail.sent[0] and "-100" in bot_fail.sent[0]

            # 3 段短 device_info 不应触发 IndexError；Android 12 映射到 API 31
            fail_mode = False
            for dev_info, expected_sys, expected_api in (
                ("TestBrand/TestModel/TestDev", "14", "34"),
                ("TestBrand/TestModel/TestDev:12/TEST.001", "12", "31"),
            ):
                await mys_api.device_login_and_save(
                    "11111111-2222-3333-4444-555555555555",
                    "test_fp_00001",
                    dev_info,
                    "stuid=20000001;stoken=v2_owner_stoken=.CAE=;mid=owner_mid",
                )
                login_req = captured[-2]
                assert login_req.header["x-rpc-device_name"] == "TestBrand TestModel"
                assert login_req.header["x-rpc-device_model"] == "TestModel"
                assert login_req.header["x-rpc-sys_version"] == expected_sys
                assert login_req.data is not None and login_req.data["os_version"] == expected_api

    asyncio.run(scenario())


def test_sign_uses_hk4e_paths_headers_and_auto_refreshes_expired_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sign_attempts = 0
    info_calls = 0

    def _respond(req: _CapturedRequest) -> dict[str, object] | int:
        nonlocal sign_attempts, info_calls
        if req.url == "/event/luna/hk4e/info":
            info_calls += 1
            return {
                "retcode": 0,
                "message": "OK",
                "data": {
                    "total_sign_day": 10 if info_calls == 1 else 11,
                    "today": "2026-11-22",
                    "is_sign": info_calls > 1,
                    "is_sub": False,
                    "region": "cn_gf01",
                    "sign_cnt_missed": 0,
                    "short_sign_day": 0,
                },
            }
        if req.url == "/event/luna/hk4e/sign":
            sign_attempts += 1
            if "cookie_token_v2=v2_fresh_ck=.CAE=" not in req.header["Cookie"]:
                return -100
            assert req.header["X-Requested-With"] == "com.mihoyo.hyperion"
            assert req.header["Origin"] == "https://act.mihoyo.com"
            assert req.header["Referer"] == "https://act.mihoyo.com/"
            assert req.header["x-rpc-signgame"] == "hk4e"
            assert req.header["x-rpc-platform"] == "2"
            assert "account_mid_v2=test_mid_mhy" in req.header["Cookie"]
            assert "ltoken_v2=v2_fresh_lt=.CAE=" in req.header["Cookie"]
            return {"retcode": 0, "message": "OK", "data": {"code": "", "risk_code": 0, "gt": "", "challenge": ""}}
        if req.url == EXCHANGE_TOKEN.get():
            return _exchange_token_response(req.data, "v2_fresh_ck=.CAE=", "v2_fresh_lt=.CAE=")
        if req.url == "/event/luna/hk4e/home":
            awards = [{"icon": "", "name": f"测试奖励{i}", "cnt": 20} for i in range(1, 31)]
            return {"retcode": 0, "message": "OK", "data": {"month": 11, "awards": awards}}
        raise AssertionError(f"unexpected url: {req.url}")

    _mock_mys_request(monkeypatch, _respond)

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="mys_sign.db"):
            await GsUser.insert_data(
                user_id="100000001",
                bot_id="test_bot",
                uid="100000001",
                mys_id="20000001",
                cookie="account_id=20000001;cookie_token=legacy_expired_v1",
                stoken="stuid=20000001;stoken=v2_test_stoken=.CAE=;mid=test_mid_mhy",
                fp="test_fp_00001",
                device_id="11111111-2222-3333-4444-555555555555",
                device_info="TestBrand/TestModel/TestDev:16/TEST.001",
            )

            result = await sign_in("100000001", "gs")
            assert "✅签到成功" in result
            assert sign_attempts == 2

            updated = await GsUser.select_data_by_uid("100000001")
            assert updated is not None and updated.cookie is not None
            assert "cookie_token_v2=v2_fresh_ck=.CAE=" in updated.cookie
            assert "ltoken_v2=v2_fresh_lt=.CAE=" in updated.cookie
            assert updated.status is None

            await GsUID.full_insert_data(main_uid="100000001", game_name="gs", uid_1="100000003")
            await sign_error("100000003", -100, "gs")
            marked = await GsUser.select_data_by_uid("100000001")
            assert marked is not None and marked.status == "error"

    asyncio.run(scenario())


def test_hyp_stoken_upgrade_to_bbs_scope_and_qr_dedup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert BBS_TASKS_WEB.get() == f"{NEW_BBS_URL}/apihub/wapi/getUserMissionsState"

    exchange_calls = 0

    def _respond(req: _CapturedRequest) -> dict[str, object]:
        nonlocal exchange_calls
        if req.url == CREATE_AUTH_TICKET_BY_GAME_BIZ.get():
            assert req.method == "POST"
            assert req.header["x-rpc-app_id"] == "ddxf5dufpuyo"
            assert req.header["x-rpc-client_type"] == "3"
            assert req.data == {
                "game_biz": "bbs_cn",
                "stoken": "v2_hyp_scoped_stoken=.CAE=",
                "uid": 20000001,
                "mid": "test_mid_mhy",
            }
            return {"retcode": 0, "message": "OK", "data": {"ticket": "test_auth_ticket_32"}}
        if req.url == LOGIN_BY_AUTH_TICKET.get():
            assert req.method == "POST"
            assert req.header["x-rpc-app_id"] == "bll8iq97cem8"
            assert req.header["x-rpc-client_type"] == "2"
            assert "DS" in req.header
            assert req.data == {"ticket": "test_auth_ticket_32"}
            return {
                "retcode": 0,
                "message": "OK",
                "data": {
                    "token": {"token_type": 1, "token": "v2_bbs_scoped_stoken=.CAE="},
                    "user_info": {"aid": "20000001", "mid": "test_mid_mhy"},
                },
            }
        if req.url == EXCHANGE_TOKEN.get():
            exchange_calls += 1
            assert req.data is not None
            src = req.data["src_token"]
            assert isinstance(src, dict) and src["token"] == "v2_bbs_scoped_stoken=.CAE="
            return _exchange_token_response(req.data, "v2_fresh_ck=.CAE=", "v2_fresh_lt=.CAE=")
        if req.url.endswith("/game_record/card/wapi/getGameRecordCard"):
            return {
                "retcode": 0,
                "message": "OK",
                "data": {"list": [{"game_id": 2, "game_role_id": "100000001"}]},
            }
        if req.url.endswith(("/apihub/api/deviceLogin", "/apihub/api/saveDevice")):
            assert "stoken=v2_bbs_scoped_stoken=.CAE=" in req.header["Cookie"]
            return {"retcode": 0, "message": "OK", "data": {}}
        if req.url.endswith("/device-fp/api/getFp"):
            return {"retcode": 0, "message": "OK", "data": {"code": 200, "device_fp": "test_fp_00001"}}
        raise AssertionError(f"unexpected url: {req.url}")

    _mock_mys_request(monkeypatch, _respond)

    async def _fake_create_hyp() -> dict[str, str]:
        return {"ticket": "qr_tk", "url": "https://example.invalid/qr", "device_id": "dev_hyp_1"}

    async def _fake_qr_bytes(url: str, path: Path, bot_id: str) -> bytes:
        _ = (url, path, bot_id)
        return b"png"

    async def _fake_refresh(code_data: dict[str, str]) -> tuple[bool, dict[str, object]]:
        _ = code_data
        return True, {
            "status": "Confirmed",
            "tokens": [{"token_type": 1, "token": "v2_hyp_scoped_stoken=.CAE="}],
            "user_info": {"aid": "20000001", "mid": "test_mid_mhy"},
        }

    monkeypatch.setattr(mys_api, "create_hyp_qrcode_url", _fake_create_hyp)
    monkeypatch.setattr("gsuid_core.utils.cookie_manager.qrlogin.get_qrcode_base64", _fake_qr_bytes)
    monkeypatch.setattr("gsuid_core.utils.cookie_manager.qrlogin.refresh", _fake_refresh)

    async def scenario() -> None:
        async with isolated_sqlite_db(tmp_path, monkeypatch, db_name="mys_qr_upgrade.db"):
            await GsBind.insert_data(user_id="100000001", bot_id="test_bot", uid="100000001")
            ev = Event(bot_id="test_bot", user_id="100000001")
            bot = _RecordingBot(ev)

            ck_str = await qrcode_login(bot, ev, "100000001")
            assert "stoken_v2=v2_bbs_scoped_stoken=.CAE=" in ck_str
            assert "cookie_token_v2=v2_fresh_ck=.CAE=" in ck_str
            assert exchange_calls == 2

            # send_qrcode_login 传 refresh_stoken=False，不再重复换票
            im = await _deal_ck("test_bot", ck_str, "100000001", refresh_stoken=False)
            assert "添加Stoken成功" in im and "添加Cookies成功" in im
            assert exchange_calls == 2

            saved = await GsUser.select_data_by_uid("100000001")
            assert saved is not None and saved.stoken is not None
            assert "stoken=v2_bbs_scoped_stoken=.CAE=" in saved.stoken

    asyncio.run(scenario())
