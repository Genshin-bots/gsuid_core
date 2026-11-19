"""静态词表嵌入（model2vec / safetensors）直读引擎。

直接用 ``tokenizers`` + ``numpy.memmap`` 读取 ``model.safetensors`` 并做池化与 L2 归一化，
完全绕过 ``fastembed`` 与 ``onnxruntime``，避免静态模型启动时拉入 ONNX 运行时。
"""

from __future__ import annotations

import json
import mmap as std_mmap
import itertools
from typing import TYPE_CHECKING, Iterable, Sequence
from pathlib import Path
from dataclasses import dataclass

if TYPE_CHECKING:
    import numpy as np
    from tokenizers import Tokenizer
    from numpy.typing import NDArray

STATIC_MAX_LENGTH: int = 512
_POOLING_CELLS: int = 4_000_000

_SAFETENSORS_DTYPES: dict[str, str] = {
    "F64": "<f8",
    "F32": "<f4",
    "F16": "<f2",
    "I64": "<i8",
    "I32": "<i4",
    "I16": "<i2",
    "I8": "i1",
    "U8": "u1",
    "BOOL": "?",
}


@dataclass(frozen=True)
class KnownStaticModel:
    model: str
    hf_repo: str
    dim: int
    model_file: str = "model.safetensors"


KNOWN_STATIC_MODELS: tuple[KnownStaticModel, ...] = (
    KnownStaticModel("minishlab/potion-base-8M", "minishlab/potion-base-8M", 256),
    KnownStaticModel("minishlab/potion-retrieval-32M", "minishlab/potion-retrieval-32M", 512),
    KnownStaticModel("minishlab/potion-multilingual-128M", "minishlab/potion-multilingual-128M", 256),
)


def find_known_static_model(model_name: str) -> KnownStaticModel | None:
    target = model_name.strip().lower()
    for item in KNOWN_STATIC_MODELS:
        if item.model.lower() == target or item.hf_repo.lower() == target:
            return item
    return None


def resolve_snapshot_dir(cache_root: Path, model_file: str) -> Path:
    """定位含 model.safetensors 与 tokenizer.json 的实际模型目录（兼容 HF cache 与平铺目录）。"""
    weight_name = Path(model_file).name
    if (cache_root / weight_name).is_file() and (cache_root / "tokenizer.json").is_file():
        return cache_root

    ref = cache_root / "refs" / "main"
    if ref.is_file():
        rev = ref.read_text(encoding="utf-8").strip()
        if rev:
            named = cache_root / "snapshots" / rev
            if (named / weight_name).is_file() and (named / "tokenizer.json").is_file():
                return named

    snapshots = cache_root / "snapshots"
    if snapshots.is_dir():
        for snap in sorted(snapshots.iterdir()):
            if snap.is_dir() and (snap / weight_name).is_file() and (snap / "tokenizer.json").is_file():
                return snap

    raise FileNotFoundError(f"未找到静态模型快照（缺少 {weight_name} 或 tokenizer.json）: {cache_root}")


def _read_tensor(
    path: Path,
    dtype: str,
    offset: int,
    shape: tuple[int, ...],
    use_mmap: bool,
) -> NDArray[np.generic]:
    import numpy as np

    if use_mmap:
        return np.memmap(path, dtype=dtype, mode="r", offset=offset, shape=shape)
    count = 1
    for dim in shape:
        count *= dim
    return np.fromfile(path, dtype=dtype, count=count, offset=offset).reshape(shape)


def load_safetensors(path: Path, *, use_mmap: bool = True) -> dict[str, NDArray[np.generic]]:
    """按 safetensors 二进制格式直接读取张量；``use_mmap=True`` 只映射不整块拷贝进 RAM。"""
    import numpy as np

    with open(path, "rb") as fh:
        header_len_bytes = fh.read(8)
        if len(header_len_bytes) != 8:
            raise ValueError(f"safetensors 文件头不完整: {path}")
        header_size = int.from_bytes(header_len_bytes, "little")
        raw_header = json.loads(fh.read(header_size))

    if not isinstance(raw_header, dict):
        raise ValueError(f"safetensors 元数据非法: {path}")

    data_start = 8 + header_size
    tensors: dict[str, NDArray[np.generic]] = {}
    for name, info in raw_header.items():
        if name == "__metadata__" or not isinstance(info, dict):
            continue
        dtype_raw = info["dtype"] if "dtype" in info else ""
        shape_raw = info["shape"] if "shape" in info else []
        offsets_raw = info["data_offsets"] if "data_offsets" in info else []
        if not isinstance(dtype_raw, str) or not isinstance(shape_raw, list) or not isinstance(offsets_raw, list):
            raise ValueError(f"safetensors 张量元数据缺失: {name} in {path}")
        if len(offsets_raw) != 2:
            raise ValueError(f"safetensors data_offsets 长度非法: {name} in {path}")
        start, end = int(offsets_raw[0]), int(offsets_raw[1])
        shape = tuple(int(dim) for dim in shape_raw)

        if dtype_raw != "BF16" and dtype_raw not in _SAFETENSORS_DTYPES:
            raise ValueError(f"不支持的 safetensors dtype {dtype_raw}: {name} in {path}")

        if end == start:
            empty_dtype = np.float32 if dtype_raw == "BF16" else np.dtype(_SAFETENSORS_DTYPES[dtype_raw])
            tensors[str(name)] = np.empty(shape, dtype=empty_dtype)
            continue

        if dtype_raw == "BF16":
            raw = _read_tensor(path, "<u2", data_start + start, shape, use_mmap)
            tensors[str(name)] = (raw.astype(np.uint32) << 16).view(np.float32)
            continue

        tensors[str(name)] = _read_tensor(path, _SAFETENSORS_DTYPES[dtype_raw], data_start + start, shape, use_mmap)
    return tensors


def load_static_tokenizer(model_dir: Path) -> tuple[Tokenizer, int | None]:
    """加载 tokenizer.json（截断至 512，不 padding），并解析 unk_token_id。"""
    from tokenizers import Tokenizer

    tokenizer_path = model_dir / "tokenizer.json"
    if not tokenizer_path.is_file():
        raise FileNotFoundError(f"未找到 tokenizer.json: {model_dir}")

    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=STATIC_MAX_LENGTH)
    tokenizer.no_padding()

    raw_doc = json.loads(tokenizer_path.read_text(encoding="utf-8"))
    unk_token_id: int | None = None
    if isinstance(raw_doc, dict) and "model" in raw_doc and isinstance(raw_doc["model"], dict):
        tok_model = raw_doc["model"]
        model_type = tok_model["type"] if "type" in tok_model else None
        if model_type == "Unigram":
            raw_unk_id = tok_model["unk_id"] if "unk_id" in tok_model else None
            if isinstance(raw_unk_id, int):
                unk_token_id = raw_unk_id
        else:
            raw_unk = tok_model["unk_token"] if "unk_token" in tok_model else None
            raw_vocab = tok_model["vocab"] if "vocab" in tok_model else None
            if isinstance(raw_unk, str) and isinstance(raw_vocab, dict) and raw_unk in raw_vocab:
                val = raw_vocab[raw_unk]
                if isinstance(val, int):
                    unk_token_id = val
    return tokenizer, unk_token_id


def _l2_normalize(matrix: NDArray[np.float32]) -> NDArray[np.float32]:
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    safe_norms = np.where(norms == 0.0, 1.0, norms).astype(np.float32)
    return (matrix / safe_norms).astype(np.float32)


class StaticMmapEmbedding:
    """直读 safetensors 的静态嵌入模型，数值输出与 fastembed StaticTextEmbedding 一致。"""

    def __init__(self, model_dir: Path, model_file: str = "model.safetensors", *, use_mmap: bool = True) -> None:
        self._model_dir = model_dir
        self._use_mmap = use_mmap
        tensors = load_safetensors(model_dir / Path(model_file).name, use_mmap=use_mmap)
        if "embeddings" not in tensors:
            raise ValueError(f"静态模型缺少 embeddings 张量: {model_dir}")
        self.embeddings: NDArray[np.generic] | None = tensors["embeddings"]
        self.weights: NDArray[np.generic] | None = tensors["weights"] if "weights" in tensors else None
        self.token_mapping: NDArray[np.generic] | None = tensors["mapping"] if "mapping" in tensors else None
        self.tokenizer, self.unk_token_id = load_static_tokenizer(model_dir)

    def _token_ids(self, documents: Sequence[str]) -> tuple[NDArray[np.int64], NDArray[np.int64]]:
        import numpy as np

        encodings = self.tokenizer.encode_batch_fast(list(documents), add_special_tokens=False)
        id_lists = [enc.ids for enc in encodings]
        lengths = np.fromiter((len(ids) for ids in id_lists), dtype=np.int64, count=len(documents))
        ids = np.fromiter(
            itertools.chain.from_iterable(id_lists),
            dtype=np.int64,
            count=int(lengths.sum()),
        )
        return ids, lengths

    def _sum_rows(self, ids: NDArray[np.int64], lengths: NDArray[np.int64]) -> NDArray[np.float32]:
        import numpy as np

        embeddings = self.embeddings
        assert embeddings is not None
        if not ids.size:
            return np.zeros((len(lengths), int(embeddings.shape[1])), dtype=np.float32)

        rows = ids if self.token_mapping is None else self.token_mapping[ids].astype(np.int64)
        if len(lengths) == 1:
            vectors = embeddings[rows].astype(np.float32, copy=False)
            if self.weights is not None:
                vectors = vectors * self.weights[ids, None].astype(np.float32, copy=False)
            return vectors.sum(axis=0, keepdims=True, dtype=np.float32)

        distinct_rows, row_index = np.unique(rows, return_inverse=True)
        if len(lengths) > 1 and len(lengths) * len(distinct_rows) > _POOLING_CELLS:
            half = len(lengths) // 2
            split = int(lengths[:half].sum())
            return np.concatenate(
                [
                    self._sum_rows(ids[:split], lengths[:half]),
                    self._sum_rows(ids[split:], lengths[half:]),
                ]
            )

        doc_index = np.repeat(np.arange(len(lengths)), lengths)
        raw_weights = None if self.weights is None else self.weights[ids].astype(np.float64, copy=False)
        counts = np.bincount(
            doc_index * len(distinct_rows) + row_index,
            weights=raw_weights,
            minlength=len(lengths) * len(distinct_rows),
        ).astype(np.float32)
        selected = embeddings[distinct_rows].astype(np.float32, copy=False)
        return (counts.reshape(len(lengths), len(distinct_rows)) @ selected).astype(np.float32)

    def raw_embed(self, documents: Sequence[str]) -> NDArray[np.float32]:
        import numpy as np

        if not documents:
            embeddings = self.embeddings
            assert embeddings is not None
            return np.zeros((0, int(embeddings.shape[1])), dtype=np.float32)
        ids, lengths = self._token_ids(documents)
        if self.unk_token_id is not None:
            known = ids != self.unk_token_id
            if not bool(known.all()):
                doc_index = np.repeat(np.arange(len(documents)), lengths)
                lengths = np.bincount(doc_index[known], minlength=len(documents)).astype(np.int64)
                ids = ids[known]
        return _l2_normalize(self._sum_rows(ids, lengths))

    def embed(self, documents: Sequence[str], batch_size: int = 256) -> Iterable[NDArray[np.float32]]:
        step = max(1, batch_size)
        for start in range(0, len(documents), step):
            batch = documents[start : start + step]
            matrix = self.raw_embed(batch)
            for row in matrix:
                yield row

    def close(self) -> None:
        """显式关闭 mmap 句柄，防止 Windows 清理缓存目录时文件被占用。"""
        for arr in (self.embeddings, self.weights, self.token_mapping):
            if arr is None:
                continue
            base = arr.base
            if isinstance(base, std_mmap.mmap):
                base.close()
        self.embeddings = None
        self.weights = None
        self.token_mapping = None
