# -*- coding: utf-8 -*-
"""本地视觉近邻识别：ONNX 模型（DINOv2 / CLIP 视觉塔）+ 角色样本库 kNN。完全免费、离线。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

MODELS = {
    'dinov2': {
        'path': 'data/models/dinov2-small.onnx',
        'size': 224,
        'mean': (0.485, 0.456, 0.406),
        'std': (0.229, 0.224, 0.225),
    },
    'clip': {
        'path': 'data/models/clip-vit-base-patch32-vision.onnx',
        'size': 224,
        'mean': (0.48145466, 0.4578275, 0.40821073),
        'std': (0.26862954, 0.26130258, 0.27577711),
    },
}


class LocalVisionIndex:
    """把角色样本图编码成向量，用余弦相似度做 kNN 分类。"""

    def __init__(self, backend: str = 'dinov2'):
        self.backend = backend if backend in MODELS else 'dinov2'
        cfg = MODELS[self.backend]
        self.model_path = Path(cfg['path'])
        self.size = int(cfg['size'])
        self.mean = np.array(cfg['mean'], dtype=np.float32)
        self.std = np.array(cfg['std'], dtype=np.float32)
        self._session = None
        self._input_name = ''
        self._labels: list[str] = []
        self._vectors: np.ndarray | None = None
        self._index_mtime: float = 0.0

    # ---------- 模型 ----------
    def available(self) -> bool:
        return self.model_path.exists() and self.model_path.stat().st_size > 1_000_000

    def session(self):
        if self._session is None:
            import onnxruntime as ort
            providers = [p for p in ('CUDAExecutionProvider', 'CPUExecutionProvider')
                         if p in ort.get_available_providers()]
            self._session = ort.InferenceSession(str(self.model_path), providers=providers or None)
            self._input_name = self._session.get_inputs()[0].name
        return self._session

    # ---------- 预处理/编码 ----------
    def _prep(self, path: str | Path) -> np.ndarray:
        from PIL import Image
        im = Image.open(path).convert('RGB')
        w, h = im.size
        s = min(w, h)
        im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2)).resize((self.size, self.size), Image.BICUBIC)
        arr = np.asarray(im, dtype=np.float32) / 255.0
        arr = (arr - self.mean) / self.std
        return np.transpose(arr, (2, 0, 1))[None, ...].astype(np.float32)

    def embed(self, path: str | Path) -> np.ndarray | None:
        try:
            sess = self.session()
            out = sess.run(None, {self._input_name: self._prep(path)})
            vec = np.asarray(out[0], dtype=np.float32).reshape(-1)
            n = float(np.linalg.norm(vec))
            return vec / n if n > 0 else vec
        except Exception:
            return None

    # ---------- 索引 ----------
    def build_index(self, sample_dir: str | Path = 'data/character_samples', cache: str | Path | None = None) -> int:
        base = Path(sample_dir)
        cache_path = Path(cache) if cache else Path(f'data/models/index_{self.backend}.npz')
        labels: list[str] = []
        vecs: list[np.ndarray] = []
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            for f in sorted(d.rglob('*')):
                if f.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
                    continue
                v = self.embed(f)
                if v is not None:
                    labels.append(d.name)
                    vecs.append(v)
        if not vecs:
            return 0
        self._labels = labels
        self._vectors = np.vstack(vecs)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache_path, labels=np.array(labels), vectors=self._vectors)
        self._index_mtime = cache_path.stat().st_mtime
        return len(labels)

    def load_index(self, cache: str | Path | None = None) -> int:
        cache_path = Path(cache) if cache else Path(f'data/models/index_{self.backend}.npz')
        if not cache_path.exists():
            return 0
        data = np.load(cache_path, allow_pickle=True)
        self._labels = [str(x) for x in data['labels']]
        self._vectors = np.asarray(data['vectors'], dtype=np.float32)
        self._index_mtime = cache_path.stat().st_mtime
        return len(self._labels)

    def ensure_index(self, sample_dir: str | Path = 'data/character_samples') -> int:
        if self.load_index():
            return len(self._labels or [])
        return self.build_index(sample_dir)

    # ---------- 查询 ----------
    def query(self, path: str | Path, topk: int = 5) -> list[tuple[str, float]]:
        if self._vectors is None or not self._labels:
            return []
        v = self.embed(path)
        if v is None:
            return []
        sims = self._vectors @ v
        idx = np.argsort(-sims)[: max(1, int(topk))]
        return [(self._labels[i], float(sims[i])) for i in idx]

    def vote(self, path: str | Path, topk: int = 5) -> tuple[str, float, list[tuple[str, float]]]:
        hits = self.query(path, topk=topk)
        if not hits:
            return '', 0.0, []
        agg: dict[str, float] = {}
        for name, sim in hits:
            agg[name] = max(agg.get(name, 0.0), sim)
        best = max(agg.items(), key=lambda kv: kv[1])
        return best[0], float(best[1]), hits
