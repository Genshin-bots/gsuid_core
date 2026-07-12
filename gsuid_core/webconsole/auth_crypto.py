"""
网页控制台认证报文加密（ECDH + HKDF + AES-256-GCM）

背景：本框架仅以 HTTP 对外提供网页控制台，登录 / 注册 / 改密请求体中的密码会以
明文形式出现在传输层，存在被同网段嗅探、ISP 窥探、抓包回放的风险。为在不引入
HTTPS 证书运维成本的前提下消除「密码明文上链路」这一问题，提供本模块实现的应用
层混合加密通道。

协议概览（详见 docs/WEBCONSOLE_AUTH_ENCRYPTION.md）：

1. 前端 ``GET /api/auth/pubkey`` 获取服务端 X25519 公钥与 key_id；
2. 前端本地生成临时 X25519 keypair，与服务端公钥做 ECDH 得到共享密钥；
3. 经 HKDF-SHA256 派生 32 字节对称密钥，用 AES-256-GCM 加密原始 JSON 字段
   （必须携带 ``ts`` 时间戳用于防重放），连同 ``client_pub`` / ``iv`` / ``ct``
   提交；
4. 后端用 key_id 取出对应私钥，复现 ECDH + HKDF + AES-GCM 解密，校验 ``ts``
   新鲜度后得到明文字段，交给既有认证逻辑处理。

安全性与边界：
- 彻底防住被动嗅探（密码永不出现在明文流量中）；
- 每次握手使用前端临时 keypair，具备前向保密特性；
- ``ts`` 窗口（默认 120s）封堵捕获重放；
- 不防主动 MITM 篡改前端 bundle（与 HTTPS 自签名首次访问同等的 TOFU 局限），
  彻底解决需上 HTTPS。
"""

from __future__ import annotations

import json
import time
import base64
import hashlib
import secrets
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PublicKey,
    X25519PrivateKey,
)

from gsuid_core.i18n import t
from gsuid_core.logger import logger

if TYPE_CHECKING:
    from collections.abc import Mapping

# 协议标识，同时作为 HKDF info，前端须使用完全一致的字符串
PROTOCOL_ID: str = "gsuid-webconsole-auth/v1"
# 允许的时间戳偏移（秒），用于防重放
TS_TOLERANCE_SECONDS: int = 120
# 派生密钥长度（AES-256）
_DERIVED_KEY_LEN: int = 32
# 密钥轮换周期（小时）：每隔此时长轮换一次服务端 X25519 密钥对（旧密钥保留一代容忍在途请求）
KEY_ROTATION_INTERVAL_HOURS: int = 12

# JSON 值的递归类型别名：避免裸 Any，同时精确表达「JSON 标量 / 列表 / 对象」。
# 直接复用 pydantic 的 ``JsonValue``——它是以 ``TypeAliasType`` 定义的递归别名，
# pydantic / FastAPI 能原生构建其递归 schema；自定义的 ``X | list["X"]`` 裸联合别名
# 含无法解析的自引用 ForwardRef，被当作 FastAPI 请求体类型时会触发
# ``TypeAdapter ... is not fully defined``（class-not-fully-defined）错误。
# 顶层认证报文字段集合（如 {"email": ..., "password": ..., "ts": ...}）
JsonObject = dict[str, JsonValue]


class AuthCryptoError(Exception):
    """加密报文解析失败（解密失败 / 格式错误 / 时间戳过期等）的统一异常。

    调用方捕获后应回一个与「账户密码错误」**相区分**的「请求无效，请刷新页面后重试」式提示：
    解密失败只发生在明文 / 篡改 / 重放 / 畸形报文上（非正常用户行为），明确提示反而帮正常用户
    排障（多为前端 bundle 过旧或本地时钟漂移）。本异常的**具体原因**（密钥不匹配 / ts 过期 /
    字段缺失等）不要回传给前端，避免给主动攻击者提供探测信号。
    """


def _parse_json_object(raw: str) -> object:
    """解析 JSON 字符串为 object。

    包装 ``json.loads`` 以将其固有的 ``Any`` 返回值收敛在单一位置：调用方拿到的
    是 ``object``，随后用 ``isinstance`` 收窄为具体类型，避免 Any 在调用链扩散。

    注：``json.loads`` 的类型存根声明返回 ``Any``，无法在类型层面消除（需关闭
    ``reportAny`` 规则），此处仅在日志层产生一个 warning，不影响正确性。
    """
    return json.loads(raw)


def _b64decode(raw: str) -> bytes:
    """容错地 base64 解码（容忍 URL-safe 变体与缺失 padding）。"""
    # 同时容忍标准 base64 与 urlsafe，以及缺失的 padding
    pad = "=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw + pad)
    except Exception as e:  # noqa: BLE001 - 两种 base64 变体解码失败的兜底
        # 回退到标准 base64（部分前端库默认标准变体）
        try:
            return base64.b64decode(raw + pad)
        except Exception:
            raise AuthCryptoError(t("base64 解码失败: {e}", e=e)) from e


class AuthKeyStore:
    """服务端 X25519 密钥对存储。

    进程启动时生成一对密钥并分配 key_id。为支持「密钥轮换期间旧请求仍可解密」，
    保留上一代密钥一小段时间（``_previous``）；默认单进程一把即足以达成「防嗅探」
    目标，轮换是可选加固。

    线程安全：FastAPI 在单事件循环内串行处理请求，本类的写入仅在模块导入时发生一
    次，运行期只读，故无需加锁。
    """

    def __init__(self) -> None:
        self._current_key_id: str = secrets.token_hex(8)
        self._current_priv: X25519PrivateKey = X25519PrivateKey.generate()
        self._previous: tuple[str, X25519PrivateKey] | None = None

        # 预序列化公钥 bytes（32B 原始），避免每次请求都做一次序列化
        self._current_pub_bytes: bytes = self._current_priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        fp = self.fingerprint(self._current_pub_bytes)
        logger.info(
            t("🔒️ [网页控制台] 认证加密密钥已生成 key_id={p0} pubkey_fingerprint={fp}", p0=self._current_key_id, fp=fp)
        )

    @staticmethod
    def fingerprint(pub_bytes: bytes) -> str:
        """公钥指纹（SHA-256 前 16 字符），供管理员人工核对防 MITM。"""
        return hashlib.sha256(pub_bytes).hexdigest()[:16]

    @property
    def current_key_id(self) -> str:
        return self._current_key_id

    @property
    def current_pubkey_b64(self) -> str:
        """当前公钥的 base64（urlsafe 无 padding）表示，直接返回给前端。"""
        return base64.urlsafe_b64encode(self._current_pub_bytes).rstrip(b"=").decode("ascii")

    def public_info(self) -> dict[str, str]:
        """返回 /api/auth/pubkey 的响应载荷。"""
        return {
            "key_id": self._current_key_id,
            "alg": "x25519-aes256gcm",
            "pubkey": self.current_pubkey_b64,
            "fingerprint": self.fingerprint(self._current_pub_bytes),
        }

    def _priv_for(self, key_id: str) -> X25519PrivateKey | None:
        """根据 key_id 取出对应私钥（当前或上一代）。"""
        if key_id == self._current_key_id:
            return self._current_priv
        if self._previous and key_id == self._previous[0]:
            return self._previous[1]
        return None

    def rotate(self) -> None:
        """主动轮换：当前密钥降级为上一代，生成新当前密钥。

        由 :func:`register_key_rotation_job` 接入 APScheduler 周期调用（默认每
        ``KEY_ROTATION_INTERVAL_HOURS`` 小时）；保留上一代密钥以容忍轮换瞬间在途的旧请求。
        """
        self._previous = (self._current_key_id, self._current_priv)
        self._current_key_id = secrets.token_hex(8)
        self._current_priv = X25519PrivateKey.generate()
        self._current_pub_bytes = self._current_priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        fp = self.fingerprint(self._current_pub_bytes)
        logger.info(
            t("🔒️ [网页控制台] 认证加密密钥已轮换 key_id={p0} pubkey_fingerprint={fp}", p0=self._current_key_id, fp=fp)
        )

    def derive_key(self, key_id: str, client_pub_b64: str) -> bytes:
        """用服务端私钥 + 客户端公钥做 ECDH + HKDF，派生 32B 对称密钥。"""
        priv = self._priv_for(key_id)
        if priv is None:
            raise AuthCryptoError(t("未知或已失效的 key_id: {key_id}", key_id=key_id))

        client_pub_raw = _b64decode(client_pub_b64)
        try:
            client_pub = X25519PublicKey.from_public_bytes(client_pub_raw)
        except Exception as e:  # noqa: BLE001 - 公钥字节非法的统一兜底
            raise AuthCryptoError(t("客户端公钥格式非法")) from e

        shared = priv.exchange(client_pub)
        return HKDF(
            algorithm=hashes.SHA256(),
            length=_DERIVED_KEY_LEN,
            salt=None,
            info=PROTOCOL_ID.encode("ascii"),
        ).derive(shared)

    def decrypt_payload(self, body: Mapping[str, JsonValue]) -> JsonObject:
        """解密一个加密报文，返回明文字段 dict。

        约定 body 形如::

            {"enc": true, "key_id": "...", "client_pub": "<base64>", "iv": "<base64 12B>", "ct": "<base64 密文+tag>"}

        校验项：``enc`` 标志、``key_id`` 存在性、AES-GCM 认证（``InvalidTag``）、
        JSON 可解析、``ts`` 时间戳新鲜度。任一失败均抛 ``AuthCryptoError``。

        注意：本方法不读取任何业务字段，仅做密码学层面的还原，业务校验（密码长度、
        注册码、限流等）仍由调用方在解密后的明文上执行。
        """
        enc_flag = body["enc"] if "enc" in body else None
        if enc_flag is not True:
            raise AuthCryptoError(t("非加密报文（缺少 enc 标志）"))

        key_id_raw = body["key_id"] if "key_id" in body else None
        client_pub_raw = body["client_pub"] if "client_pub" in body else None
        iv_b64_raw = body["iv"] if "iv" in body else None
        ct_b64_raw = body["ct"] if "ct" in body else None

        # 字段存在性与类型校验：四个字段都必须是非空字符串
        if not (
            isinstance(key_id_raw, str)
            and isinstance(client_pub_raw, str)
            and isinstance(iv_b64_raw, str)
            and isinstance(ct_b64_raw, str)
            and key_id_raw
            and client_pub_raw
            and iv_b64_raw
            and ct_b64_raw
        ):
            raise AuthCryptoError(t("加密报文字段不完整或类型非法"))

        key_id: str = key_id_raw
        client_pub: str = client_pub_raw
        iv_b64: str = iv_b64_raw
        ct_b64: str = ct_b64_raw

        try:
            key = self.derive_key(key_id, client_pub)
            iv = _b64decode(iv_b64)
            ct = _b64decode(ct_b64)
            plaintext = AESGCM(key).decrypt(iv, ct, associated_data=None)
        except AuthCryptoError:
            raise
        except InvalidTag:
            raise AuthCryptoError(t("AES-GCM 认证失败（密钥不匹配或数据被篡改）"))
        except Exception as e:  # noqa: BLE001 - 解密阶段各类异常的统一兜底
            raise AuthCryptoError(t("解密失败: {e}", e=e)) from e

        try:
            loaded = _parse_json_object(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise AuthCryptoError(t("解密后 JSON 解析失败: {e}", e=e)) from e

        if not isinstance(loaded, dict):
            raise AuthCryptoError(t("解密后载荷不是 JSON 对象"))
        payload: JsonObject = cast(JsonObject, loaded)

        # 防重放：载荷必须带 ts，且落在容忍窗口内
        ts = payload["ts"] if "ts" in payload else None
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise AuthCryptoError(t("缺少有效的 ts 时间戳"))
        if abs(time.time() - ts) > TS_TOLERANCE_SECONDS:
            raise AuthCryptoError(t("时间戳超出容忍窗口（疑似重放）"))

        return payload


# 模块级单例：整个进程共享一把服务端密钥
auth_keystore = AuthKeyStore()


def maybe_decrypt_auth_body(data: Mapping[str, JsonValue]) -> JsonObject:
    """认证接口入口处的统一解密。

    加密为**强制开启、无开关**：所有认证报文必须是加密形态（``enc=true`` + 握手字段），
    明文报文一律拒绝（抛 ``AuthCryptoError``）。解密成功后返回明文字段 dict，交由调用方
    执行限流 / 业务校验——确保限流作用在解密后的真实请求上（否则攻击者可用「触发解密失败」
    的方式绕过限流）。
    """
    enc_flag = data["enc"] if "enc" in data else None
    if enc_flag is True:
        return auth_keystore.decrypt_payload(data)
    raise AuthCryptoError(t("认证报文必须加密提交（enc=true）"))


def register_key_rotation_job() -> None:
    """把认证密钥轮换接入 APScheduler：每 ``KEY_ROTATION_INTERVAL_HOURS`` 小时轮换一次
    服务端 X25519 密钥对，提升前向保密强度（某代密钥即便日后被攻破，也只波及该窗口内的握手）。

    幂等（固定 job id + ``replace_existing``）；``add_job`` 可在 ``scheduler.start()`` 之前调用
    （APScheduler 会在启动时正式排程）。注册失败不影响认证主流程（不轮换仍可正常握手）。
    """
    try:
        from gsuid_core.aps import scheduler

        scheduler.add_job(
            func=auth_keystore.rotate,
            trigger="interval",
            hours=KEY_ROTATION_INTERVAL_HOURS,
            id="webconsole_auth_key_rotation",
            replace_existing=True,
        )
        logger.info(
            t(
                "🔒️ [网页控制台] 认证密钥轮换定时任务已注册（每 {KEY_ROTATION_INTERVAL_HOURS}h）",
                KEY_ROTATION_INTERVAL_HOURS=KEY_ROTATION_INTERVAL_HOURS,
            )
        )
    except Exception as e:
        logger.warning(t("🔒️ [网页控制台] 认证密钥轮换任务注册失败（不影响认证）: {e}", e=e))


# 模块导入即登记轮换任务（与框架既有"模块级 scheduler.add_job"一致；replace_existing 保证
# 重复 import / 热重载不会叠加重复 job）。add_job 早于 scheduler.start() 时由 APScheduler 暂存排程。
register_key_rotation_job()
