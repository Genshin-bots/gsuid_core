# 十二、嵌入 Provider 注册表

> 本章教你如何让插件**注册第三方 Embedding Provider**（与内置 `local`(fastembed) / `openai` 并列），
> 注册后自动出现在 WebConsole「嵌入模型提供方」下拉选项中，并可被 AI 配置向导识别。
>
> 本 SKILL 是 AI Core API 速查视角；与 [`gscore-plugin-development` SKILL §二十、嵌入 Provider 注册表](../gscore-plugin-development/references/20-embedding-provider-registry.md)（插件开发视角）内容一致，**两处是同一份协议的两种说法**。

## 12.1 注册 API

```python
# 插件子模块 __init__.py —— import 期只注册，不 import torch 等重依赖
from gsuid_core.ai_core.rag.embedding import EmbeddingProvider
from gsuid_core.ai_core.rag.embedding_registry import (
    EmbeddingProviderEntry,
    register_embedding_provider,
)

def _factory() -> EmbeddingProvider:
    from .provider import MyProvider   # ← 重依赖在这里才被 import（AI 后台初始化线程）
    return MyProvider.from_config()

register_embedding_provider(EmbeddingProviderEntry(
    name="my_provider",                  # 配置项 embedding_provider 的取值
    factory=_factory,                    # 懒构造工厂
    kind="local",                        # local: RAG 同步小批量 / remote: 大批量
    display_name="My Provider",          # 网页控制台展示名
    check_config=None,                   # 可选：AI 向导状态检查钩子
    config_source=None,                  # 可选：插件 StringConfig，供 summary API 返回
    plugin="MyPluginName",               # 来源插件名（报错归因）
))
```

## 12.2 `EmbeddingProviderEntry` 字段说明

| 字段 | 必填 | 含义 |
|------|------|------|
| `name` | ✅ | 字符串，配置项 `embedding_provider` 的取值（如 `"my_provider"`），全框架唯一 |
| `factory` | ✅ | 无参 callable，返回 `EmbeddingProvider` 实例。**重依赖在内部 import** |
| `kind` | ✅ | `"local"` / `"remote"`——影响 RAG 同步并发策略与 UI 提示 |
| `display_name` | ✅ | WebConsole 下拉项里显示的可读名称 |
| `check_config` | ❌ | 可选状态检查函数，AI 配置向导用它判断 provider 是否可用 |
| `config_source` | ❌ | 插件自身的 `StringConfig`，`summary` API 会回传给前端做配置面板渲染 |
| `plugin` | ✅ | 来源插件名（用于报错归因、日志、卸载时清理） |

## 12.3 实现 Provider 类

实现 `EmbeddingProvider` 抽象基类的 `dimension` 属性与 `embed_sync(texts)` 方法即可。
异步方法（`embed` / `embed_single`）由基类默认实现自动移入线程池，**不需要自己写**。

```python
# plugins/MyPlugin/MyPlugin/provider.py
from gsuid_core.ai_core.rag.embedding import EmbeddingProvider

class MyProvider(EmbeddingProvider):
    def __init__(self, model_name: str, device: str = "cpu"):
        # 重依赖在 import 期就一次性加载（factory 内部 import 后）
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(model_name, device=device)

    @property
    def dimension(self) -> int:
        return self._model.get_sentence_embedding_dimension()

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        # 同步实现：基类会把 async 入口移入线程池
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [v.tolist() for v in vectors]

    @classmethod
    def from_config(cls) -> "MyProvider":
        # 读取插件 StringConfig / 业务配置
        return cls(model_name="BAAI/bge-m3", device="cuda")
```

## 12.4 强制规范与注意事项

### 12.4.1 必须懒 import

插件模块**顶层禁止** `import torch` / `import sentence_transformers` 等重依赖。

```python
# ❌ 错误：插件 __init__.py 顶层 import 重依赖
from sentence_transformers import SentenceTransformer   # 启动拖慢数秒！

# ✅ 正确：工厂内部 import
def _factory() -> EmbeddingProvider:
    from sentence_transformers import SentenceTransformer   # AI 后台线程才执行
    return MyProvider(...)
```

**原因**：插件同步加载发生在 WS 启动前，顶层重导入会让 bot 启动卡顿数秒。

### 12.4.2 注册时序

- 插件 `load_gss` **同步加载期**调用 `register_embedding_provider`
- `get_embedding_provider()` 在 AI 后台初始化线程里才消费
- **注册必然早于消费**，因此 import 期注册是安全的，无需异步钩子

### 12.4.3 容错降级

配置指向的插件 Provider 未注册（插件被卸载）或工厂构造失败时，框架自动**降级回 `local`** 并记录 error：

- AI 核心整体仍可用
- WebConsole 状态指示器会标红并提示原因
- 知识库检索使用 fastembed 兜底
- 不会抛出未捕获异常导致整个 AI 进程崩溃

### 12.4.4 同维度换模型

切换到**相同维度**的新模型时，框架**不会触发自动迁移**：

- 旧向量与新向量位于不同空间
- 混存会导致检索质量**静默下降**（不报错，但召回率掉）
- 切换前请确认维度变化或**手动重建向量库**（参考 WebConsole「知识库 → 重建索引」）

## 12.5 完整示例

`gsuid_core/plugins/STEmbedding/` 是一个生产级参考实现，覆盖了：

- `sentence_transformers` 真实集成（懒 import、GPU/CPU 自适应）
- `check_config` 钩子（检测 CUDA 可用性、模型是否已下载）
- `config_source` 接入插件 StringConfig
- `plugin` 字段正确归因
- 卸载时清理注册表项（避免热重载后旧 Provider 残留）

```bash
# 目录结构
plugins/STEmbedding/
├── STEmbedding/
│   ├── __init__.py        # register_embedding_provider(...) 在这里
│   ├── provider.py        # SentenceTransformerProvider 实现
│   └── config.py          # StringConfig 定义（model_name / device / cache_dir）
├── __init__.py
└── __nest__.py
```

## 12.6 调试技巧

```python
# 在 AI 后台线程中查看当前已注册的 Provider
from gsuid_core.ai_core.rag.embedding_registry import list_embedding_providers
print(list_embedding_providers())  # → [EmbeddingProviderEntry(name='my_provider', ...), ...]
```

如果你的 Provider 没出现在下拉项里，按以下顺序排查：

1. 检查插件 `__init__.py` 顶层是否真的调用了 `register_embedding_provider`
2. 检查 `name` 是否与已有 Provider 冲突
3. 检查 `factory` 是否抛了异常（看启动日志）
4. 确认 `plugin` 字段填写正确（影响注册表清理与归因日志）
