from typing import Dict, Tuple, Union, Optional

from aiohttp import TCPConnector, ClientSession, ContentTypeError

from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .tools import get_ds_token
from .base_request import BaseMysApi

ssl_verify = core_plugins_config.get_config("MhySSLVerify").data


class PassMysApi(BaseMysApi):
    async def _pass(self, gt: str, ch: str, header: Dict) -> Tuple[Optional[str], Optional[str]]:
        # 警告：使用该服务（例如某RR等）需要注意风险问题
        # 本项目不以任何形式提供相关接口
        # 代码来源：GITHUB项目MIT开源
        _pass_api = core_plugins_config.get_config("_pass_API").data
        if _pass_api:
            async with ClientSession(connector=TCPConnector(verify_ssl=ssl_verify)) as client:
                async with client.request(
                    url=f"{_pass_api}&gt={gt}&challenge={ch}",
                    method="GET",
                ) as data:
                    try:
                        data = await data.json()
                    except ContentTypeError:
                        data = await data.text()
                        return None, None
                    logger.debug(data)
                    if isinstance(data, int):
                        return None, None
                    else:
                        if "code" in data and data["code"] != 0:
                            if "info" in data:
                                msg = data["info"]
                            else:
                                msg = f"错误码{data['code']}, 请检查API是否配置正确"
                            logger.info(f"[upass] {msg}")
                            return None, None
                        validate = data["data"]["validate"]
                        ch = data["data"]["challenge"]
        else:
            validate = None

        return validate, ch

    async def _upass(self, header: Dict, is_bbs: bool = False) -> str:
        logger.info("[upass] 进入处理...")
        if is_bbs:
            raw_data = await self.get_bbs_upass_link(header)
        else:
            raw_data = await self.get_upass_link(header)
        if isinstance(raw_data, int):
            return ""
        gt = raw_data["data"]["gt"]
        ch = raw_data["data"]["challenge"]

        vl, ch = await self._pass(gt, ch, header)

        if vl:
            await self.get_header_and_vl(header, ch, vl, is_bbs)
            if ch:
                logger.info(f"[upass] 获取ch -> {ch}")
                return ch
            else:
                return ""
        else:
            return ""

    async def get_upass_link(self, header: Dict) -> Union[int, Dict]:
        header["DS"] = get_ds_token("is_high=false")
        return await self._mys_request(
            url=self.MAPI["VERIFICATION_URL"],
            method="GET",
            header=header,
        )

    async def get_bbs_upass_link(self, header: Dict) -> Union[int, Dict]:
        header["DS"] = get_ds_token("is_high=true")
        return await self._mys_request(
            url=self.MAPI["BBS_VERIFICATION_URL"],
            method="GET",
            header=header,
        )

    async def get_header_and_vl(self, header: Dict, ch, vl, is_bbs: bool = False):
        header["DS"] = get_ds_token(
            "",
            {
                "geetest_challenge": ch,
                "geetest_validate": vl,
                "geetest_seccode": f"{vl}|jordan",
            },
        )
        _ = await self._mys_request(
            url=(self.MAPI["VERIFY_URL"] if not is_bbs else self.MAPI["BBS_VERIFY_URL"]),
            method="POST",
            header=header,
            data={
                "geetest_challenge": ch,
                "geetest_validate": vl,
                "geetest_seccode": f"{vl}|jordan",
            },
        )
