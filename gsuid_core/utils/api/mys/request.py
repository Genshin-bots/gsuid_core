'''
米游社 API 请求模块。
'''
from __future__ import annotations

import copy
import time
import uuid
import random
from abc import abstractmethod
from string import digits, ascii_letters
from typing import (
    Any,
    Dict,
    List,
    Tuple,
    Union,
    Literal,
    Optional,
    cast,
    overload,
)

from aiohttp import TCPConnector, ClientSession, ContentTypeError

from gsuid_core.logger import logger
from gsuid_core.utils.database.api import DBSqla
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

from .api import _API
from .tools import (
    random_hex,
    mys_version,
    random_text,
    get_ds_token,
    generate_os_ds,
    gen_payment_sign,
    get_web_ds_token,
    generate_passport_ds,
)
from .models import (
    BsIndex,
    GcgInfo,
    MysGame,
    MysSign,
    RegTime,
    GachaLog,
    MysGoods,
    MysOrder,
    SignInfo,
    SignList,
    AbyssData,
    IndexData,
    AuthKeyInfo,
    GcgDeckInfo,
    MonthlyAward,
    QrCodeStatus,
    CalculateInfo,
    DailyNoteData,
    GameTokenInfo,
    MysOrderCheck,
    RolesCalendar,
    CharDetailData,
    CookieTokenInfo,
    LoginTicketInfo,
)

proxy_url = core_plugins_config.get_config('proxy').data
ssl_verify = core_plugins_config.get_config('MhySSLVerify').data
RECOGNIZE_SERVER = {
    '1': 'cn_gf01',
    '2': 'cn_gf01',
    '5': 'cn_qd01',
    '6': 'os_usa',
    '7': 'os_euro',
    '8': 'os_asia',
    '9': 'os_cht',
}


class BaseMysApi:
    proxy_url: Optional[str] = proxy_url if proxy_url else None
    mysVersion = mys_version
    _HEADER = {
        'x-rpc-app_version': mysVersion,
        'User-Agent': (
            'Mozilla/5.0 (Linux; Android 13; PHK110 Build/SKQ1.221119.001; wv)'
            'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/'
            f'118.0.0.0 Mobile Safari/537.36 miHoYoBBS/{mysVersion}'
        ),
        'x-rpc-client_type': '5',
        'Referer': 'https://webstatic.mihoyo.com/',
        'Origin': 'https://webstatic.mihoyo.com/',
        # 'X-Requested-With': 'com.mihoyo.hyperion',
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
    async def _upass(self, header: Dict) -> str:
        ...

    @abstractmethod
    async def _pass(
        self, gt: str, ch: str, header: Dict
    ) -> Tuple[Optional[str], Optional[str]]:
        ...

    @abstractmethod
    async def get_ck(
        self, uid: str, mode: Literal['OWNER', 'RANDOM'] = 'RANDOM'
    ) -> Optional[str]:
        ...

    @abstractmethod
    async def get_stoken(self, uid: str) -> Optional[str]:
        ...

    @abstractmethod
    async def get_user_fp(self, uid: str) -> Optional[str]:
        ...

    @abstractmethod
    async def get_user_device_id(self, uid: str) -> Optional[str]:
        ...

    def get_device_id(self) -> str:
        device_id = str(uuid.uuid4()).lower()
        return device_id

    def generate_fp(self, length: int = 13) -> str:
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

    async def generate_fp_by_uid(
        self, uid: str, seed_id: str, seed_time: str, model_name: str
    ) -> str:
        device_id = await self.get_user_device_id(uid)
        ext_fields = f'''{{\"cpuType\":\"arm64-v8a\",\"romCapacity\":\"512\",\"productName\":\"{model_name}\",\"romRemain\":\"422\",\"manufacturer\":\"XiaoMi\",\"appMemory\":\"512\",\"hostname\":\"dg02-pool03-kvm87\",\"screenSize\":\"1240x2662\",\"osVersion\":\"13\",\"aaid\":\"{self.generate_ID()}\",\"vendor\":\"中国联通\",\"accelerometer\":\"1.4883357x7.1712894x6.2847486\",\"buildTags\":\"release-keys\",\"model\":\"{model_name}\",\"brand\":\"XiaoMi\",\"oaid\":\"6C47BC55BBC443F49B603129AFCD621E6f70bfc8a663a364e224fe59c248b471\",\"hardware\":\"qcom\",\"deviceType\":\"OP5913L1\",\"devId\":\"REL\",\"serialNumber\":\"unknown\",\"buildTime\":\"1687848011000\",\"buildUser\":\"root\",\"ramCapacity\":\"469679\",\"magnetometer\":\"20.081251x-27.487501x2.1937501\",\"display\":\"{model_name}_13.1.0.181(CN01)\",\"ramRemain\":\"215344\",\"deviceInfo\":\"XiaoMi\\\/{model_name}\\\/OP5913L1:13\\\/SKQ1.221119.001\\\/T.118e6c7-5aa23-73911:user\\\/release-keys\",\"gyroscope\":\"0.030226856x0.014647375x0.010652636\",\"vaid\":\"{self.generate_ID()}\",\"buildType\":\"user\",\"sdkVersion\":\"33\",\"board\":\"taro\"}}'''  # noqa
        body = {
            'device_id': self.generate_seed(16),
            'seed_id': seed_id,  # uuid4
            'platform': '2',
            'seed_time': seed_time,
            'ext_fields': ext_fields,
            'app_name': 'bbs_cn',
            'bbs_device_id': device_id,
            'device_fp': self.generate_fp(),
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
        self, device_id: str, device_fp: str, model_name: str, cookie: str
    ):
        body = {
            "app_version": self.mysVersion,
            "device_id": device_id,
            "device_name": f"XiaoMi{model_name}",
            "os_version": "33",
            "platform": "Android",
            "registration_id": self.generate_seed(19),
        }

        HEADER = copy.deepcopy(self._HEADER)
        HEADER['x-rpc-device_id'] = device_id
        HEADER['x-rpc-device_fp'] = device_fp
        HEADER['x-rpc-device_name'] = f"XiaoMi{model_name}"
        HEADER['x-rpc-device_model'] = model_name
        HEADER['DS'] = generate_passport_ds('', body)
        HEADER['cookie'] = cookie

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
        params: Dict = {},
        header: Dict = {},
        cookie: Optional[str] = None,
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
    ) -> Tuple[str, str, str, str]:
        ...

    @overload
    async def ck_in_new_device(
        self, uid: str, app_cookie: Optional[str] = None
    ) -> Optional[Tuple[str, str, str, str]]:
        ...

    async def ck_in_new_device(
        self, uid: str, app_cookie: Optional[str] = None
    ):
        device_id = self.get_device_id()
        seed_id, seed_time = self.get_seed()
        model_name = self.generate_model_name()
        fp = await self.generate_fp_by_uid(uid, seed_id, seed_time, model_name)
        if app_cookie is None:
            app_cookie = await self.get_stoken(uid)
            if app_cookie is None:
                return logger.warning('设备登录流程错误...')
        await self.device_login_and_save(device_id, fp, model_name, app_cookie)
        return fp, device_id, seed_id, seed_time

    async def _mys_request(
        self,
        url: str,
        method: Literal['GET', 'POST'] = 'GET',
        header: Dict[str, Any] = _HEADER,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        use_proxy: Optional[bool] = False,
    ) -> Union[Dict, int]:
        async with ClientSession(
            connector=TCPConnector(verify_ssl=ssl_verify)
        ) as client:
            raw_data = {}
            uid = None
            if params and 'role_id' in params:
                uid = params['role_id']
                device_id = await self.get_user_device_id(uid)
                header['x-rpc-device_fp'] = await self.get_user_fp(uid)
                if device_id is not None:
                    header['x-rpc-device_id'] = device_id

            logger.debug(header)
            for _ in range(2):
                async with client.request(
                    method,
                    url=url,
                    headers=header,
                    params=params,
                    json=data,
                    proxy=self.proxy_url if use_proxy else None,
                    timeout=300,
                ) as resp:
                    try:
                        raw_data = await resp.json()
                    except ContentTypeError:
                        _raw_data = await resp.text()
                        raw_data = {'retcode': -999, 'data': _raw_data}

                    logger.debug(raw_data)

                    # 判断retcode
                    if 'retcode' in raw_data:
                        retcode: int = raw_data['retcode']
                    elif 'code' in raw_data:
                        retcode: int = raw_data['code']
                    else:
                        retcode = 0

                    # 针对1034做特殊处理
                    if retcode == 1034 or retcode == 5003:
                        if uid:
                            nd = await self.ck_in_new_device(uid)
                            ck = header['Cookie']
                            if 'DEVICEFP_SEED_ID' not in ck and nd:
                                header['Cookie'] = (
                                    f'DEVICEFP_SEED_ID={nd[2]};'
                                    f'DEVICEFP_SEED_TIME={nd[3]};'
                                    f'{ck};DEVICE_FP={nd[0]}'
                                )

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


class MysApi(BaseMysApi):
    async def _pass(
        self, gt: str, ch: str, header: Dict
    ) -> Tuple[Optional[str], Optional[str]]:
        # 警告：使用该服务（例如某RR等）需要注意风险问题
        # 本项目不以任何形式提供相关接口
        # 代码来源：GITHUB项目MIT开源
        _pass_api = core_plugins_config.get_config('_pass_API').data
        if _pass_api:
            async with ClientSession(
                connector=TCPConnector(verify_ssl=ssl_verify)
            ) as client:
                async with client.request(
                    url=f'{_pass_api}&gt={gt}&challenge={ch}',
                    method='GET',
                ) as data:
                    try:
                        data = await data.json()
                    except ContentTypeError:
                        data = await data.text()
                        return None, None
                    if isinstance(data, int):
                        return None, None
                    else:
                        validate = data['data']['validate']
                        ch = data['data']['challenge']
        else:
            validate = None

        return validate, ch

    async def _upass(self, header: Dict, is_bbs: bool = False) -> str:
        logger.info('[upass] 进入处理...')
        if is_bbs:
            raw_data = await self.get_bbs_upass_link(header)
        else:
            raw_data = await self.get_upass_link(header)
        if isinstance(raw_data, int):
            return ''
        gt = raw_data['data']['gt']
        ch = raw_data['data']['challenge']

        vl, ch = await self._pass(gt, ch, header)

        if vl:
            await self.get_header_and_vl(header, ch, vl, is_bbs)
            if ch:
                logger.info(f'[upass] 获取ch -> {ch}')
                return ch
            else:
                return ''
        else:
            return ''

    async def get_upass_link(self, header: Dict) -> Union[int, Dict]:
        header['DS'] = get_ds_token('is_high=false')
        return await self._mys_request(
            url=self.MAPI['VERIFICATION_URL'],
            method='GET',
            header=header,
        )

    async def get_bbs_upass_link(self, header: Dict) -> Union[int, Dict]:
        header['DS'] = get_ds_token('is_high=true')
        return await self._mys_request(
            url=self.MAPI['BBS_VERIFICATION_URL'],
            method='GET',
            header=header,
        )

    async def get_header_and_vl(
        self, header: Dict, ch, vl, is_bbs: bool = False
    ):
        header['DS'] = get_ds_token(
            '',
            {
                'geetest_challenge': ch,
                'geetest_validate': vl,
                'geetest_seccode': f'{vl}|jordan',
            },
        )
        _ = await self._mys_request(
            url=self.MAPI['VERIFY_URL']
            if not is_bbs
            else self.MAPI['BBS_VERIFY_URL'],
            method='POST',
            header=header,
            data={
                'geetest_challenge': ch,
                'geetest_validate': vl,
                'geetest_seccode': f'{vl}|jordan',
            },
        )

    def check_os(self, uid: str) -> bool:
        return False if int(str(uid)[0]) < 6 else True

    async def get_info(
        self, uid, ck: Optional[str] = None
    ) -> Union[IndexData, int]:
        data = await self.simple_mys_req('PLAYER_INFO_URL', uid, cookie=ck)
        if isinstance(data, Dict):
            data = cast(IndexData, data['data'])
        return data

    async def get_daily_data(self, uid: str) -> Union[DailyNoteData, int]:
        data = await self.simple_mys_req('DAILY_NOTE_URL', uid)
        if isinstance(data, Dict):
            data = cast(DailyNoteData, data['data'])
        return data

    async def get_gcg_info(self, uid: str) -> Union[GcgInfo, int]:
        data = await self.simple_mys_req('GCG_INFO', uid)
        if isinstance(data, Dict):
            data = cast(GcgInfo, data['data'])
        return data

    async def get_gcg_deck(self, uid: str) -> Union[GcgDeckInfo, int]:
        data = await self.simple_mys_req('GCG_DECK_URL', uid)
        if isinstance(data, Dict):
            data = cast(GcgDeckInfo, data['data'])
        return data

    async def get_cookie_token(
        self, token: str, uid: str
    ) -> Union[CookieTokenInfo, int]:
        data = await self._mys_request(
            self.MAPI['GET_COOKIE_TOKEN_BY_GAME_TOKEN'],
            'GET',
            params={
                'game_token': token,
                'account_id': uid,
            },
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data['data'])
        return data

    async def get_sign_list(self, uid) -> Union[SignList, int]:
        is_os = self.check_os(uid)
        if is_os:
            params = {
                'act_id': 'e202102251931481',
                'lang': 'zh-cn',
            }
        else:
            params = {'act_id': 'e202009291139501'}
        data = await self._mys_req_get('SIGN_LIST_URL', is_os, params)
        if isinstance(data, Dict):
            data = cast(SignList, data['data'])
        return data

    async def get_sign_info(self, uid) -> Union[SignInfo, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        is_os = self.check_os(uid)
        if is_os:
            params = {
                'act_id': 'e202102251931481',
                'lang': 'zh-cn',
                'region': server_id,
                'uid': uid,
            }
            header = {
                'DS': generate_os_ds(),
            }
        else:
            params = {
                'act_id': 'e202009291139501',
                'region': server_id,
                'uid': uid,
            }
            header = {}
        data = await self._mys_req_get('SIGN_INFO_URL', is_os, params, header)
        if isinstance(data, Dict):
            data = cast(SignInfo, data['data'])
        return data

    async def mys_sign(
        self, uid, header={}, server_id='cn_gf01'
    ) -> Union[MysSign, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        if int(str(uid)[0]) < 6:
            HEADER = copy.deepcopy(self._HEADER)
            HEADER['Cookie'] = ck
            HEADER['x-rpc-device_id'] = random_hex(32)
            HEADER['x-rpc-app_version'] = mys_version
            HEADER['x-rpc-client_type'] = '5'
            HEADER['X_Requested_With'] = 'com.mihoyo.hyperion'
            HEADER['DS'] = get_web_ds_token(True)
            HEADER['Referer'] = (
                'https://webstatic.mihoyo.com/bbs/event/signin-ys/index.html'
                '?bbs_auth_required=true&act_id=e202009291139501'
                '&utm_source=bbs&utm_medium=mys&utm_campaign=icon'
            )
            HEADER.update(header)
            data = await self._mys_request(
                url=self.MAPI['SIGN_URL'],
                method='POST',
                header=HEADER,
                data={
                    'act_id': 'e202009291139501',
                    'uid': uid,
                    'region': server_id,
                },
            )
        else:
            HEADER = copy.deepcopy(self._HEADER_OS)
            HEADER['Cookie'] = ck
            HEADER['DS'] = generate_os_ds()
            HEADER.update(header)
            data = await self._mys_request(
                url=self.MAPI['SIGN_URL_OS'],
                method='POST',
                header=HEADER,
                data={
                    'act_id': 'e202102251931481',
                    'lang': 'zh-cn',
                    'uid': uid,
                    'region': server_id,
                },
                use_proxy=True,
            )
        if isinstance(data, Dict):
            data = cast(MysSign, data['data'])
        return data

    async def get_award(self, uid) -> Union[MonthlyAward, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        if int(str(uid)[0]) < 6:
            HEADER = copy.deepcopy(self._HEADER)
            HEADER['Cookie'] = ck
            HEADER['DS'] = get_web_ds_token(True)
            HEADER['x-rpc-device_id'] = random_hex(32)
            data = await self._mys_request(
                url=self.MAPI['MONTHLY_AWARD_URL'],
                method='GET',
                header=HEADER,
                params={
                    'act_id': 'e202009291139501',
                    'bind_region': server_id,
                    'bind_uid': uid,
                    'month': '0',
                    'bbs_presentation_style': 'fullscreen',
                    'bbs_auth_required': 'true',
                    'utm_source': 'bbs',
                    'utm_medium': 'mys',
                    'utm_campaign': 'icon',
                },
            )
        else:
            HEADER = copy.deepcopy(self._HEADER_OS)
            HEADER['Cookie'] = ck
            HEADER['x-rpc-device_id'] = random_hex(32)
            HEADER['DS'] = generate_os_ds()
            data = await self._mys_request(
                url=self.MAPI['MONTHLY_AWARD_URL_OS'],
                method='GET',
                header=HEADER,
                params={
                    'act_id': 'e202009291139501',
                    'region': server_id,
                    'uid': uid,
                    'month': '0',
                },
                use_proxy=True,
            )
        if isinstance(data, Dict):
            data = cast(MonthlyAward, data['data'])
        return data

    async def get_draw_calendar(self, uid: str) -> Union[int, RolesCalendar]:
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        hk4e_token = await self.get_hk4e_token(uid)
        header = {}
        header['Cookie'] = f'{ck};{hk4e_token}'
        params = {
            'lang': 'zh-cn',
            'badge_uid': uid,
            'badge_region': server_id,
            'game_biz': 'hk4e_cn',
            'activity_id': 20220301153521,
            'year': 2023,
        }
        data = await self._mys_request(
            self.MAPI['CALENDAR_URL'], 'GET', header, params
        )
        if isinstance(data, Dict):
            return cast(RolesCalendar, data['data'])
        return data

    async def get_bs_index(self, uid: str) -> Union[int, BsIndex]:
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        hk4e_token = await self.get_hk4e_token(uid)
        header = {}
        header['Cookie'] = f'{ck};{hk4e_token}'
        data = await self._mys_request(
            self.MAPI['BS_INDEX_URL'],
            'GET',
            header,
            {
                'lang': 'zh-cn',
                'badge_uid': uid,
                'badge_region': server_id,
                'game_biz': 'hk4e_cn',
                'activity_id': 20220301153521,
            },
        )
        if isinstance(data, Dict):
            return cast(BsIndex, data['data'])
        return data

    async def post_draw(self, uid: str, role_id: int) -> Union[int, Dict]:
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        hk4e_token = await self.get_hk4e_token(uid)
        header = {}
        header['Cookie'] = f'{ck};{hk4e_token}'
        data = await self._mys_request(
            self.MAPI['RECEIVE_URL'],
            'POST',
            header,
            {
                'lang': 'zh-cn',
                'badge_uid': uid,
                'badge_region': server_id,
                'game_biz': 'hk4e_cn',
                'activity_id': 20220301153521,
            },
            {'role_id': role_id},
        )
        if isinstance(data, Dict):
            return data
        elif data == -512009:
            return {'data': None, 'message': '这张画片已经被收录啦~', 'retcode': -512009}
        else:
            return -999

    async def get_spiral_abyss_info(
        self, uid: str, schedule_type='1', ck: Optional[str] = None
    ) -> Union[AbyssData, int]:
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        data = await self.simple_mys_req(
            'PLAYER_ABYSS_INFO_URL',
            uid,
            {
                'role_id': uid,
                'schedule_type': schedule_type,
                'server': server_id,
            },
            cookie=ck,
        )
        if isinstance(data, Dict):
            data = cast(AbyssData, data['data'])
        return data

    async def get_character(
        self, uid: str, character_ids: List[int], ck: Union[str, None] = None
    ) -> Union[CharDetailData, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])

        if ck is None:
            ck = await self.get_ck(uid)
            if ck is None:
                return -51

        if int(str(uid)[0]) < 6:
            HEADER = copy.deepcopy(self._HEADER)
            HEADER['Cookie'] = ck
            HEADER['DS'] = get_ds_token(
                '',
                {
                    'character_ids': character_ids,
                    'role_id': uid,
                    'server': server_id,
                },
            )
            data = await self._mys_request(
                self.MAPI['PLAYER_DETAIL_INFO_URL'],
                'POST',
                HEADER,
                data={
                    'character_ids': character_ids,
                    'role_id': uid,
                    'server': server_id,
                },
            )
        else:
            HEADER = copy.deepcopy(self._HEADER_OS)
            HEADER['Cookie'] = ck
            HEADER['DS'] = generate_os_ds()
            data = await self._mys_request(
                self.MAPI['PLAYER_DETAIL_INFO_URL_OS'],
                'POST',
                HEADER,
                data={
                    'character_ids': character_ids,
                    'role_id': uid,
                    'server': server_id,
                },
                use_proxy=True,
            )
        if isinstance(data, Dict):
            data = cast(CharDetailData, data['data'])
        return data

    async def get_calculate_info(
        self, uid, char_id: int
    ) -> Union[CalculateInfo, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        data = await self.simple_mys_req(
            'CALCULATE_INFO_URL',
            uid,
            {'avatar_id': char_id, 'uid': uid, 'region': server_id},
        )
        if isinstance(data, Dict):
            data = cast(CalculateInfo, data['data'])
        return data

    async def get_mihoyo_bbs_info(
        self,
        mys_id: str,
        cookie: Optional[str] = None,
        is_os: bool = False,
    ) -> Union[List[MysGame], int]:
        if not cookie:
            cookie = await self.get_ck(mys_id, 'OWNER')
        data = await self.simple_mys_req(
            'MIHOYO_BBS_PLAYER_INFO_URL',
            is_os,
            {'uid': mys_id},
            {'Cookie': cookie},
        )
        if isinstance(data, Dict):
            data = cast(List[MysGame], data['data']['list'])
        return data

    async def create_qrcode_url(self) -> Union[Dict, int]:
        device_id: str = ''.join(random.choices(ascii_letters + digits, k=64))
        app_id: str = '8'
        data = await self._mys_request(
            self.MAPI['CREATE_QRCODE'],
            'POST',
            header={},
            data={'app_id': app_id, 'device': device_id},
        )
        if isinstance(data, Dict):
            url: str = data['data']['url']
            ticket = url.split('ticket=')[1]
            return {
                'app_id': app_id,
                'ticket': ticket,
                'device': device_id,
                'url': url,
            }
        return data

    async def check_qrcode(
        self, app_id: str, ticket: str, device: str
    ) -> Union[QrCodeStatus, int]:
        data = await self._mys_request(
            self.MAPI['CHECK_QRCODE'],
            'POST',
            data={
                'app_id': app_id,
                'ticket': ticket,
                'device': device,
            },
        )
        if isinstance(data, Dict):
            data = cast(QrCodeStatus, data['data'])
        return data

    async def get_gacha_log_by_authkey(
        self,
        uid: str,
        gacha_type: str = '301',
        page: int = 1,
        end_id: str = '0',
    ) -> Union[int, GachaLog]:
        server_id = 'cn_qd01' if uid[0] == '5' else 'cn_gf01'
        authkey_rawdata = await self.get_authkey_by_cookie(uid)
        if isinstance(authkey_rawdata, int):
            return authkey_rawdata
        authkey = authkey_rawdata['authkey']
        data = await self._mys_request(
            url=self.MAPI['GET_GACHA_LOG_URL'],
            method='GET',
            header=self._HEADER,
            params={
                'authkey_ver': '1',
                'sign_type': '2',
                'auth_appid': 'webview_gacha',
                'init_type': '200',
                'gacha_id': 'fecafa7b6560db5f3182222395d88aaa6aaac1bc',
                'timestamp': str(int(time.time())),
                'lang': 'zh-cn',
                'device_type': 'mobile',
                'plat_type': 'ios',
                'region': server_id,
                'authkey': authkey,
                'game_biz': 'hk4e_cn',
                'gacha_type': gacha_type,
                'page': page,
                'size': '20',
                'end_id': end_id,
            },
        )
        if isinstance(data, Dict):
            data = cast(GachaLog, data['data'])
        return data

    async def get_cookie_token_by_game_token(
        self, token: str, uid: str
    ) -> Union[CookieTokenInfo, int]:
        data = await self._mys_request(
            self.MAPI['GET_COOKIE_TOKEN_BY_GAME_TOKEN'],
            'GET',
            params={
                'game_token': token,
                'account_id': uid,
            },
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data['data'])
        return data

    async def get_cookie_token_by_stoken(
        self, stoken: str, mys_id: str, full_sk: Optional[str] = None
    ) -> Union[CookieTokenInfo, int]:
        HEADER = copy.deepcopy(self._HEADER)
        if full_sk:
            HEADER['Cookie'] = full_sk
        else:
            HEADER['Cookie'] = f'stuid={mys_id};stoken={stoken}'
        data = await self._mys_request(
            url=self.MAPI['GET_COOKIE_TOKEN_URL'],
            method='GET',
            header=HEADER,
            params={
                'stoken': stoken,
                'uid': mys_id,
            },
        )
        if isinstance(data, Dict):
            data = cast(CookieTokenInfo, data['data'])
        return data

    async def get_stoken_by_login_ticket(
        self, lt: str, mys_id: str
    ) -> Union[LoginTicketInfo, int]:
        data = await self._mys_request(
            url=self.MAPI['GET_STOKEN_URL'],
            method='GET',
            header=self._HEADER,
            params={
                'login_ticket': lt,
                'token_types': '3',
                'uid': mys_id,
            },
        )
        if isinstance(data, Dict):
            data = cast(LoginTicketInfo, data['data'])
        return data

    async def get_stoken_by_game_token(
        self, account_id: int, game_token: str
    ) -> Union[GameTokenInfo, int]:
        _data = {
            'account_id': account_id,
            'game_token': game_token,
        }
        data = await self._mys_request(
            self.MAPI['GET_STOKEN'],
            'POST',
            {
                'x-rpc-app_version': '2.41.0',
                'DS': generate_passport_ds(b=_data),
                'x-rpc-aigis': '',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'x-rpc-game_biz': 'bbs_cn',
                'x-rpc-sys_version': '11',
                'x-rpc-device_id': uuid.uuid4().hex,
                'x-rpc-device_fp': ''.join(
                    random.choices(ascii_letters + digits, k=13)
                ),
                'x-rpc-device_name': 'GenshinUid_login_device_lulu',
                'x-rpc-device_model': 'GenshinUid_login_device_lulu',
                'x-rpc-app_id': 'bll8iq97cem8',
                'x-rpc-client_type': '2',
                'User-Agent': 'okhttp/4.8.0',
            },
            data=_data,
        )
        if isinstance(data, Dict):
            data = cast(GameTokenInfo, data['data'])
        return data

    async def get_authkey_by_cookie(self, uid: str) -> Union[AuthKeyInfo, int]:
        server_id = self.RECOGNIZE_SERVER.get(str(uid)[0])
        HEADER = copy.deepcopy(self._HEADER)
        stoken = await self.get_stoken(uid)
        if stoken is None:
            return -51
        HEADER['Cookie'] = stoken
        HEADER['DS'] = get_web_ds_token(True)
        HEADER['User-Agent'] = 'okhttp/4.8.0'
        HEADER['x-rpc-app_version'] = mys_version
        HEADER['x-rpc-sys_version'] = '12'
        HEADER['x-rpc-client_type'] = '5'
        HEADER['x-rpc-channel'] = 'mihoyo'
        HEADER['x-rpc-device_id'] = random_hex(32)
        HEADER['x-rpc-device_name'] = random_text(random.randint(1, 10))
        HEADER['x-rpc-device_model'] = 'Mi 10'
        HEADER['Referer'] = 'https://app.mihoyo.com'
        HEADER['Host'] = 'api-takumi.mihoyo.com'
        data = await self._mys_request(
            url=self.MAPI['GET_AUTHKEY_URL'],
            method='POST',
            header=HEADER,
            data={
                'auth_appid': 'webview_gacha',
                'game_biz': 'hk4e_cn',
                'game_uid': uid,
                'region': server_id,
            },
        )
        if isinstance(data, Dict):
            data = cast(AuthKeyInfo, data['data'])
        return data

    async def get_hk4e_token(self, uid: str):
        # 获取e_hk4e_token
        server_id = self.RECOGNIZE_SERVER.get(uid[0])
        header = {
            'Cookie': await self.get_ck(uid, 'OWNER'),
            'Content-Type': 'application/json;charset=UTF-8',
            'Referer': 'https://webstatic.mihoyo.com/',
            'Origin': 'https://webstatic.mihoyo.com',
        }
        use_proxy = False
        data = {
            'game_biz': 'hk4e_cn',
            'lang': 'zh-cn',
            'uid': f'{uid}',
            'region': f'{server_id}',
        }
        if int(str(uid)[0]) < 6:
            url = self.MAPI['HK4E_LOGIN_URL']
        else:
            url = self.MAPI['HK4E_LOGIN_URL_OS']
            data['game_biz'] = 'hk4e_global'
            use_proxy = True

        async with ClientSession() as client:
            async with client.request(
                method='POST',
                url=url,
                headers=header,
                json=data,
                proxy=self.proxy_url if use_proxy else None,
                timeout=300,
            ) as resp:
                raw_data = await resp.json()
                if 'retcode' in raw_data and raw_data['retcode'] == 0:
                    _k = resp.cookies['e_hk4e_token'].key
                    _v = resp.cookies['e_hk4e_token'].value
                    ck = f'{_k}={_v}'
                    return ck
                else:
                    return None

    async def get_regtime_data(self, uid: str) -> Union[RegTime, int]:
        hk4e_token = await self.get_hk4e_token(uid)
        ck_token = await self.get_ck(uid, 'OWNER')
        params = {
            'game_biz': 'hk4e_cn',
            'lang': 'zh-cn',
            'badge_uid': uid,
            'badge_region': self.RECOGNIZE_SERVER.get(uid[0]),
        }
        data = await self.simple_mys_req(
            'REG_TIME',
            uid,
            params,
            {'Cookie': f'{hk4e_token};{ck_token}' if int(uid[0]) <= 5 else {}},
        )
        if isinstance(data, Dict):
            return cast(RegTime, data['data'])
        else:
            return data

    '''充值相关'''

    async def get_fetchgoods(self) -> Union[int, List[MysGoods]]:
        data = {
            'released_flag': True,
            'game': 'hk4e_cn',
            'region': 'cn_gf01',
            'uid': '1',
            'account': '1',
        }
        resp = await self._mys_request(
            url=self.MAPI['fetchGoodsurl'],
            method='POST',
            data=data,
        )
        if isinstance(resp, int):
            return resp
        return cast(List[MysGoods], resp['data']['goods_list'])

    async def topup(
        self,
        uid: str,
        goods: MysGoods,
        method: Literal['weixin', 'alipay'] = 'alipay',
    ) -> Union[int, MysOrder]:
        device_id = str(uuid.uuid4())
        HEADER = copy.deepcopy(self._HEADER)
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        HEADER['Cookie'] = ck
        account = HEADER['Cookie'].split('account_id=')[1].split(';')[0]
        order = {
            'account': str(account),
            'region': 'cn_gf01',
            'uid': uid,
            'delivery_url': '',
            'device': device_id,
            'channel_id': 1,
            'client_ip': '',
            'client_type': 4,
            'game': 'hk4e_cn',
            'amount': goods['price'],
            # 'amount': 600,
            'goods_num': 1,
            'goods_id': goods['goods_id'],
            'goods_title': f'{goods["goods_name"]}×{str(goods["goods_unit"])}'
            if int(goods['goods_unit']) > 0
            else goods['goods_name'],
            'price_tier': goods['tier_id'],
            # 'price_tier': 'Tier_1',
            'currency': 'CNY',
            'pay_plat': method,
        }
        data = {
            'order': order,
            'special_info': 'topup_center',
            'sign': gen_payment_sign(order),
        }
        HEADER['x-rpc-device_id'] = device_id
        HEADER['x-rpc-client_type'] = '4'
        resp = await self._mys_request(
            url=self.MAPI['CreateOrderurl'],
            method='POST',
            header=HEADER,
            data=data,
        )
        if isinstance(resp, int):
            return resp
        return cast(MysOrder, resp['data'])

    async def check_order(
        self, order: MysOrder, uid: str
    ) -> Union[int, MysOrderCheck]:
        HEADER = copy.deepcopy(self._HEADER)
        ck = await self.get_ck(uid, 'OWNER')
        if ck is None:
            return -51
        HEADER['Cookie'] = ck
        data = {
            'order_no': order['order_no'],
            'game': 'hk4e_cn',
            'region': 'cn_gf01',
            'uid': uid,
        }
        resp = await self._mys_request(
            url=self.MAPI['CheckOrderurl'],
            method='GET',
            header=HEADER,
            params=data,
        )
        if isinstance(resp, int):
            return resp
        return cast(MysOrderCheck, resp['data'])
