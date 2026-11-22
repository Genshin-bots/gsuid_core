"""米游社 Token 交换、设备注册与原神签到契约回归测试。"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path
from dataclasses import dataclass

import pytest
from sqlmodel import SQLModel
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import gsuid_core.utils.database.base_models as base_models
from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event
from gsuid_core.utils.sign.sign import sign_in, sign_error
from gsuid_core.utils.api.mys.api import EXCHANGE_TOKEN
from gsuid_core.utils.api.mys_api import mys_api
from gsuid_core.utils.database.models import GsUID, GsUser
from gsuid_core.utils.cookie_manager.add_ck import build_cn_account_cookie
from gsuid_core.utils.cookie_manager.add_fp import deal_fp
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


async def _init_temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncEngine:
    db_file = tmp_path / "mys_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    monkeypatch.setattr(base_models, "engine", engine)
    monkeypatch.setattr(
        base_models,
        "async_maker",
        async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession),
    )
    return engine


def test_cn_stoken_exchange_and_v2_cookie_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[_CapturedRequest] = []

    async def fake_request(
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
        captured.append(_CapturedRequest(url, method, _str_headers(header), params, data, base_url))
        assert url == EXCHANGE_TOKEN.get()
        assert method == "POST"
        assert data is not None and "dst_token_type" in data
        dst = data["dst_token_type"]
        tok = "v2_test_cookie_token=.CAE=" if dst == 4 else "v2_test_ltoken=.CAE="
        return {
            "retcode": 0,
            "message": "OK",
            "data": {
                "token": {"token_type": dst, "token": tok},
                "user_info": {"aid": "20000001", "mid": "test_mid_mhy"},
            },
        }

    monkeypatch.setattr(mys_api, "_mys_request", fake_request)

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
    captured: list[_CapturedRequest] = []
    fail_mode = False

    async def fake_request(
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
        captured.append(_CapturedRequest(url, method, _str_headers(header), params, data, base_url))
        if fail_mode:
            return -100
        return {"retcode": 0, "message": "OK", "data": {}}

    monkeypatch.setattr(mys_api, "_mys_request", fake_request)

    async def scenario() -> None:
        nonlocal fail_mode
        engine = await _init_temp_db(tmp_path, monkeypatch)
        try:
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
            brand, model, sys_ver, build_id = mys_api._parse_device_info("TestBrand/TestModel/TestDev")
            assert (brand, model, sys_ver, build_id) == ("TestBrand", "TestModel", "14", "SKQ1.221119.001")
            fail_mode = False
            await mys_api.device_login_and_save(
                "11111111-2222-3333-4444-555555555555",
                "test_fp_00001",
                "TestBrand/TestModel/TestDev:12/TEST.001",
                "stuid=20000001;stoken=v2_owner_stoken=.CAE=;mid=owner_mid",
            )
            assert captured[-2].data is not None and captured[-2].data["os_version"] == "31"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_sign_uses_hk4e_paths_headers_and_auto_refreshes_expired_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[_CapturedRequest] = []
    sign_attempts = 0
    info_calls = 0

    async def fake_request(
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
        nonlocal sign_attempts, info_calls
        hdr = _str_headers(header)
        captured.append(_CapturedRequest(url, method, hdr, params, data, base_url))

        if url == "/event/luna/hk4e/info":
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
        if url == "/event/luna/hk4e/sign":
            sign_attempts += 1
            if "cookie_token_v2=v2_fresh_ck=.CAE=" not in hdr["Cookie"]:
                return -100
            assert hdr["X-Requested-With"] == "com.mihoyo.hyperion"
            assert hdr["Origin"] == "https://act.mihoyo.com"
            assert hdr["Referer"] == "https://act.mihoyo.com/"
            assert hdr["x-rpc-signgame"] == "hk4e"
            assert hdr["x-rpc-platform"] == "2"
            assert "account_mid_v2=test_mid_mhy" in hdr["Cookie"]
            assert "ltoken_v2=v2_fresh_lt=.CAE=" in hdr["Cookie"]
            return {"retcode": 0, "message": "OK", "data": {"code": "", "risk_code": 0, "gt": "", "challenge": ""}}
        if url == EXCHANGE_TOKEN.get():
            assert data is not None and "dst_token_type" in data
            dst = data["dst_token_type"]
            tok = "v2_fresh_ck=.CAE=" if dst == 4 else "v2_fresh_lt=.CAE="
            return {
                "retcode": 0,
                "message": "OK",
                "data": {
                    "token": {"token_type": dst, "token": tok},
                    "user_info": {"aid": "20000001", "mid": "test_mid_mhy"},
                },
            }
        if url == "/event/luna/hk4e/home":
            awards = [{"icon": "", "name": f"测试奖励{i}", "cnt": 20} for i in range(1, 31)]
            return {"retcode": 0, "message": "OK", "data": {"month": 11, "awards": awards}}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(mys_api, "_mys_request", fake_request)

    async def scenario() -> None:
        engine = await _init_temp_db(tmp_path, monkeypatch)
        try:
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
        finally:
            await engine.dispose()

    asyncio.run(scenario())
