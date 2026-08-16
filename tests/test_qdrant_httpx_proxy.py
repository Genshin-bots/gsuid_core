"""本地/内网 Qdrant URL 必须关闭 httpx 系统代理，避免 Windows Clash 502。"""

from gsuid_core.ai_core.rag.qdrant_provider import remote_qdrant_httpx_kwargs


def test_loopback_qdrant_disables_env_proxy() -> None:
    assert remote_qdrant_httpx_kwargs("http://127.0.0.1:6333") == {"trust_env": False}
    assert remote_qdrant_httpx_kwargs("http://localhost:6333") == {"trust_env": False}
    assert remote_qdrant_httpx_kwargs("http://[::1]:6333") == {"trust_env": False}
    assert remote_qdrant_httpx_kwargs("127.0.0.1:6333") == {"trust_env": False}


def test_private_lan_qdrant_disables_env_proxy() -> None:
    assert remote_qdrant_httpx_kwargs("http://192.168.1.10:6333") == {"trust_env": False}
    assert remote_qdrant_httpx_kwargs("http://10.0.0.8:6333") == {"trust_env": False}


def test_public_qdrant_keeps_env_proxy() -> None:
    assert remote_qdrant_httpx_kwargs("https://xxxx.cloud.qdrant.io:6333") == {}
    assert remote_qdrant_httpx_kwargs("https://qdrant.example.com") == {}
