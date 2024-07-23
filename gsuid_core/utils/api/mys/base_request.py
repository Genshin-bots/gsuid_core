from __future__ import annotations

import copy
import json
import time
import uuid
import random
from string import digits
from abc import abstractmethod
from typing import Any, Dict, Tuple, Union, Literal, Optional, overload

import httpx

from gsuid_core.bot import call_bot
from gsuid_core.logger import logger
from gsuid_core.utils.database.api import DBSqla
from gsuid_core.utils.database.models import GsUser
from gsuid_core.utils.database.utils import SR_SERVER, ZZZ_SERVER
from gsuid_core.utils.database.utils import SERVER as RECOGNIZE_SERVER
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .api import _API
from .tools import (
    random_hex,
    mys_version,
    get_ds_token,
    generate_os_ds,
    generate_passport_ds,
)

_DEAD_CODE = [10035, 5003, 10041, 1034]


Gproxy = core_plugins_config.get_config('Gproxy').data
Nproxy = core_plugins_config.get_config('Nproxy').data
ssl_verify = core_plugins_config.get_config('MhySSLVerify').data


class BaseMysApi:
    Gproxy: Optional[str] = Gproxy if Gproxy else None
    Nproxy: Optional[str] = Nproxy if Nproxy else None
    mysVersion = mys_version
    _HEADER = {
        'x-rpc-app_version': mysVersion,
        'X-Requested-With': "com.mihoyo.hyperion",
        'User-Agent': (
            'Mozilla/5.0 (Linux; Android 13; PHK110 Build/SKQ1.221119.001; wv)'
            'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/'
            f'126.0.6478.133 Mobile Safari/537.36 miHoYoBBS/{mysVersion}'
        ),
        'x-rpc-client_type': '5',
        'Referer': 'https://webstatic.mihoyo.com/',
        'Origin': 'https://webstatic.mihoyo.com/',
    }
    _HEADER_OS = {
        'x-rpc-app_version': '1.5.0',
        'x-rpc-client_type': '4',
        'x-rpc-language': 'zh-cn',
    }
    MAPI = _API
    is_sr = False
    RECOGNIZE_SERVER = RECOGNIZE_SERVER
    chs = {}
    dbsqla: DBSqla = DBSqla()

    @abstractmethod
    async def _upass(self, header: Dict) -> str: ...

    @abstractmethod
    async def _pass(
        self, gt: str, ch: str, header: Dict
    ) -> Tuple[Optional[str], Optional[str]]: ...

    @abstractmethod
    async def get_ck(
        self,
        uid: str,
        mode: Literal['OWNER', 'RANDOM'] = 'RANDOM',
        game_name: Optional[str] = None,
    ) -> Optional[str]:
        if game_name == 'gs':
            game_name = None

        if mode == 'RANDOM':
            if game_name == 'zzz':
                condition = (
                    {
                        'zzz_region': self.get_server_id(uid, 'zzz'),
                    }
                    if len(uid) >= 10
                    else None
                )
            elif game_name == 'sr':
                condition = {
                    'sr_region': self.get_server_id(uid, 'sr'),
                }
            elif game_name == 'gs':
                condition = {
                    'region': self.get_server_id(uid, 'gs'),
                }
            else:
                condition = None

            return await GsUser.get_random_cookie(
                uid,
                game_name=game_name,
                condition=condition,
            )
        else:
            return await GsUser.get_user_cookie_by_uid(
                uid, game_name=game_name
            )

    @abstractmethod
    async def get_stoken(
        self, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]: ...

    @abstractmethod
    async def get_user_fp(
        self, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]: ...

    @abstractmethod
    async def get_user_device_id(
        self, uid: str, game_name: Optional[str] = None
    ) -> Optional[str]: ...

    def check_os(self, uid: str, game_name: str = 'gs') -> bool:
        if game_name == 'gs' or game_name == 'sr':
            is_os = False if int(str(uid)[0]) < 6 else True
        elif game_name == 'zzz':
            is_os = False if len(str(uid)) < 10 else True
        return is_os

    def get_server_id(self, uid: str, game_name: str = 'gs') -> str:
        server_id = 'prod_gf_cn'
        if game_name == 'gs':
            server_id = self.RECOGNIZE_SERVER.get(str(uid)[0], 'cn_gf01')
        elif game_name == 'sr':
            server_id = SR_SERVER.get(str(uid)[0], 'prod_gf_cn')
        elif game_name == 'zzz':
            if len(uid) < 10:
                server_id = 'prod_gf_cn'
            else:
                server_id = ZZZ_SERVER.get(uid[:2], 'prod_gf_jp')
        return server_id

    def get_device_id(self) -> str:
        device_id = str(uuid.uuid4()).lower()
        return device_id

    def generate_random_fp(self, length: int = 13) -> str:
        char = digits + "abcdef"
        return ''.join(random.choices(char, k=length))

    def generate_seed(self, length: int):
        characters = '0123456789abcdef'
        result = ''.join(random.choices(characters, k=length))
        return result

    def generate_ID(self, length: int = 64):
        characters = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        result = ''.join(random.choices(characters, k=length))
        return result

    def generate_model_name(self):
        return self.generate_ID(6)

    def get_seed(self):
        return self.get_device_id(), str(int(time.time() * 1000))

    async def generate_fake_fp(
        self, device_id: str, seed_id: str, seed_time: str
    ):
        return await self.generate_fp(
            device_id,
            'PHK110',
            'PHK110',
            'OP5913L1',
            'taro',
            '1f1971b188c472f0',
            'OnePlus/PHK110/OP5913L1:13/'
            'SKQ1.221119.001/T.1328291_b9_41:user/release-keys',
            seed_id,
            seed_time,
        )

    async def generate_fp(
        self,
        device_id: str,
        model_name: str,
        device: str,
        device_type: str,
        board: str,
        oaid: str,
        device_info: str,
        seed_id: str,
        seed_time: str,
    ) -> str:
        device_brand = device_info.split('/')[0]
        ext_fields = f'''{{"cpuType":"arm64-v8a","romCapacity":"512","productName":"{device}","romRemain":"422","manufacturer":"{device_brand}","appMemory":"512","hostname":"dg02-pool03-kvm87","screenSize":"1264x2640","osVersion":"13","aaid":"{self.generate_ID()}","vendor":"中国联通","accelerometer":"0.44027936x7.256833x6.422336","buildTags":"release-keys","model":"{model_name}","brand":"XiaoMi","oaid":"{oaid}","hardware":"qcom","deviceType":"{device_type}","devId":"REL","serialNumber":"unknown","buildTime":"1687848011000","buildUser":"root","ramCapacity":"469679","magnetometer":"20.081251x-27.487501x2.1937501","display":"{model_name}_13.1.0.181(CN01)","ramRemain":"215344","deviceInfo":"{device_info}","gyroscope":"0.030226856x0.014647375x0.010652636","vaid":"{self.generate_ID()}","buildType":"user","sdkVersion":"33","board":"{board}"}}'''  # noqa

        body = {
            'device_id': self.generate_seed(16),
            'seed_id': seed_id,  # uuid4
            'platform': '2',
            'seed_time': seed_time,
            'ext_fields': ext_fields,
            'app_name': 'bbs_cn',
            'bbs_device_id': device_id,
            'device_fp': self.generate_random_fp(),
        }

        HEADER = copy.deepcopy(self._HEADER)
        res = await self._mys_request(
            url=self.MAPI['GET_FP_URL'],
            method='POST',
            header=HEADER,
            data=body,
        )
        if not isinstance(res, Dict):
            logger.error(f"获取fp连接失败{res}")
            return random_hex(13).lower()
        elif res["data"]["code"] != 200:
            logger.error(f"获取fp参数不正确{res['data']['msg']}")
            return random_hex(13).lower()
        else:
            return res["data"]["device_fp"]

    async def device_login_and_save(
        self, device_id: str, device_fp: str, device_info: str, cookie: str
    ):
        info = device_info.split('/')
        brand, model_name = info[0], info[1]
        body = {
            "app_version": self.mysVersion,
            "device_id": device_id,
            "device_name": f"{brand}{model_name}",
            "os_version": "33",
            "platform": "Android",
            "registration_id": self.generate_seed(19),
        }

        HEADER = copy.deepcopy(self._HEADER)
        HEADER['x-rpc-device_id'] = device_id
        HEADER['x-rpc-device_fp'] = device_fp
        HEADER['x-rpc-device_name'] = f"{brand} {model_name}"
        HEADER['x-rpc-device_model'] = model_name
        HEADER['x-rpc-csm_source'] = 'myself'
        HEADER['Referer'] = 'https://app.mihoyo.com'
        HEADER['Host'] = 'bbs-api.miyoushe.com'
        HEADER['DS'] = generate_passport_ds('', body)
        HEADER['Cookie'] = cookie

        await self._mys_request(
            url=self.MAPI['DEVICE_LOGIN'],
            method='POST',
            header=HEADER,
            data=body,
        )

        await self._mys_request(
            url=self.MAPI['SAVE_DEVICE'],
            method='POST',
            header=HEADER,
            data=body,
        )

    async def simple_mys_req(
        self,
        URL: str,
        uid: Union[str, bool],
        params: Dict = {},  # noqa: B006
        header: Dict = {},  # noqa: B006
        cookie: Optional[str] = None,
        game_name: Optional[str] = None,
    ) -> Union[Dict, int]:
        if isinstance(uid, bool):
            is_os = uid
            server_id = (
                ('cn_qd01' if is_os else 'cn_gf01')
                if not self.is_sr
                else ('prod_gf_cn' if is_os else 'prod_gf_cn')
            )
        else:
            server_id = self.RECOGNIZE_SERVER.get(uid[0])
            is_os = False if int(uid[0]) < 6 else True
        ex_params = '&'.join([f'{k}={v}' for k, v in params.items()])
        if is_os:
            _URL = self.MAPI[f'{URL}_OS']
            HEADER = copy.deepcopy(self._HEADER_OS)
            HEADER['DS'] = generate_os_ds()
        else:
            _URL = self.MAPI[URL]
            HEADER = copy.deepcopy(self._HEADER)
            HEADER['DS'] = get_ds_token(
                ex_params if ex_params else f'role_id={uid}&server={server_id}'
            )
        HEADER.update(header)
        if cookie is not None:
            HEADER['Cookie'] = cookie
        elif 'Cookie' not in HEADER and isinstance(uid, str):
            ck = await self.get_ck(uid)
            if ck is None:
                return -51
            HEADER['Cookie'] = ck
        data = await self._mys_request(
            url=_URL,
            method='GET',
            header=HEADER,
            params=params if params else {'role_id': uid, 'server': server_id},
            use_proxy=True if is_os else False,
            game_name=game_name,
        )
        return data

    async def _mys_req_get(
        self,
        url: str,
        is_os: bool,
        params: Dict,
        header: Optional[Dict] = None,
    ) -> Union[Dict, int]:
        if is_os:
            _URL = self.MAPI[f'{url}_OS']
            HEADER = copy.deepcopy(self._HEADER_OS)
            use_proxy = True
        else:
            _URL = self.MAPI[url]
            HEADER = copy.deepcopy(self._HEADER)
            use_proxy = False
        if header:
            HEADER.update(header)

        if 'Cookie' not in HEADER and 'uid' in params:
            ck = await self.get_ck(params['uid'])
            if ck is None:
                return -51
            HEADER['Cookie'] = ck
        data = await self._mys_request(
            url=_URL,
            method='GET',
            header=HEADER,
            params=params,
            use_proxy=use_proxy,
        )
        return data

    @overload
    async def ck_in_new_device(
        self, uid: str, app_cookie: str
    ) -> Tuple[str, str, str, str]: ...

    @overload
    async def ck_in_new_device(
        self, uid: str, app_cookie: Optional[str] = None
    ) -> Optional[Tuple[str, str, str, str]]: ...

    async def ck_in_new_device(
        self, uid: str, app_cookie: Optional[str] = None
    ):
        data = await GsUser.base_select_data(stoken=app_cookie)
        device_id = self.get_device_id()
        seed_id, seed_time = self.get_seed()
        if data and data.device_info:
            fp = data.fp
            device_info = data.device_info
        else:
            fp = await self.generate_fake_fp(device_id, seed_id, seed_time)
            device_info = 'OnePlus/PHK110/OP2020L1'
        if app_cookie is None:
            app_cookie = await self.get_stoken(uid)
            if app_cookie is None:
                return logger.warning('设备登录流程错误...')
        await self.device_login_and_save(
            device_id, fp, device_info, app_cookie
        )
        if await GsUser.user_exists(uid, 'sr' if self.is_sr else None):
            await GsUser.update_data_by_uid_without_bot_id(
                uid, 'sr' if self.is_sr else None, fp=fp, device_id=device_id
            )
        return fp, device_id, seed_id, seed_time

    async def _mys_request(
        self,
        url: str,
        method: Literal['GET', 'POST'] = 'GET',
        header: Dict[str, Any] = _HEADER,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        use_proxy: Optional[bool] = False,
        base_url: str = '',
        game_name: Optional[str] = None,
    ) -> Union[Dict, int]:
        if use_proxy and self.Gproxy:
            proxy = self.Gproxy
        elif self.Nproxy and not use_proxy:
            proxy = self.Nproxy
        else:
            proxy = None

        async with httpx.AsyncClient(
            verify=ssl_verify,
            proxies=proxy,
            base_url=base_url,
        ) as client:
            raw_data = {}
            uid = None
            if params and 'role_id' in params:
                uid = params['role_id']
            elif data and 'role_id' in data:
                uid = data['role_id']

            if uid is not None:
                device_id = await self.get_user_device_id(
                    uid,
                    game_name,
                )
                header['x-rpc-device_fp'] = await self.get_user_fp(
                    uid,
                    game_name,
                )
                if device_id is not None:
                    header['x-rpc-device_id'] = device_id

                dfp: Optional[str] = await GsUser.get_user_attr_by_uid(
                    uid, 'device_info', 'sr' if self.is_sr else game_name
                )
                if dfp is not None:
                    df = dfp.split('/')
                    header['User-Agent'] = (
                        f"Mozilla/5.0 (Linux; Android 13; {df[1]} {df[3]}"
                        "; wv)AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Version/4.0 Chrome/104.0.5112.97"
                        f"Mobile Safari/537.36 miHoYoBBS/2{mys_version}"
                    )

            logger.debug(header)
            for _ in range(2):
                try:
                    resp = await client.request(
                        method,
                        url=url,
                        headers=header,
                        params=params,
                        json=data,
                        timeout=300,
                    )
                except httpx.ConnectError:
                    await call_bot().send('[mys_request] 请求连接错误...')
                    continue
                except:  # noqa
                    await call_bot().send(
                        '[mys_request] 请求错误, 请联系Bot主人检查控制台!'
                    )
                    continue

                try:
                    raw_data = resp.json()
                except (
                    httpx.ConnectError,
                    httpx.RequestError,
                    json.decoder.JSONDecodeError,
                ):
                    _raw_data = resp.text
                    raw_data = {'retcode': -999, 'data': _raw_data}

                logger.debug(raw_data)

                # 判断retcode
                if 'retcode' in raw_data:
                    retcode = raw_data['retcode']
                elif 'code' in raw_data:
                    retcode = raw_data['code']
                else:
                    retcode = 0

                # 做特殊处理
                if retcode in _DEAD_CODE:
                    if uid:
                        header['x-rpc-challenge_game'] = (
                            '6' if self.is_sr else '2'
                        )
                        header['x-rpc-page'] = (
                            'v1.4.1-rpg_#/rpg'
                            if self.is_sr
                            else 'v4.1.5-ys_#ys'
                        )
                        header['x-rpc-tool-verison'] = (
                            'v1.4.1-rpg' if self.is_sr else 'v4.1.5-ys'
                        )

                    if core_plugins_config.get_config('MysPass').data:
                        pass_header = copy.deepcopy(header)
                        ch = await self._upass(pass_header)
                        if ch == '':
                            return 114514
                        else:
                            header['x-rpc-challenge'] = ch

                    if 'DS' in header:
                        if isinstance(params, Dict):
                            q = '&'.join(
                                [
                                    f'{k}={v}'
                                    for k, v in sorted(
                                        params.items(),
                                        key=lambda x: x[0],
                                    )
                                ]
                            )
                        else:
                            q = ''
                        header['DS'] = get_ds_token(q, data)

                    logger.debug(header)
                elif retcode != 0:
                    return retcode
                else:
                    return raw_data
            else:
                return -999
