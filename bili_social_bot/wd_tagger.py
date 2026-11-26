# -*- coding: utf-8 -*-
"""WD14（动漫打标模型）本地角色识别：ONNX 推理 + 角色标签→中文名。免费、离线。"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

MODEL = Path('data/models/wd-swinv2-tagger-v3.onnx')
TAGS = Path('data/models/wd-selected_tags.csv')


class WDTagger:
    def __init__(self, model_path: Path | str = MODEL, tags_path: Path | str = TAGS):
        self.model_path = Path(model_path)
        self.tags_path = Path(tags_path)
        self._session = None
        self._input_name = ''
        self._input_size = 448
        self._names: list[str] = []
        self._categories: list[int] = []

    def available(self) -> bool:
        return self.model_path.exists() and self.tags_path.exists() and self.model_path.stat().st_size > 1_000_000

    def _load(self):
        if self._session is None:
            import onnxruntime as ort
            providers = [p for p in ('CUDAExecutionProvider', 'CPUExecutionProvider')
                         if p in ort.get_available_providers()]
            options = ort.SessionOptions()
            options.intra_op_num_threads = 3
            options.add_session_config_entry('session.intra_op.allow_spinning', '0')
            self._session = ort.InferenceSession(str(self.model_path), sess_options=options, providers=providers or None)
            inp = self._session.get_inputs()[0]
            self._input_name = inp.name
            try:
                self._input_size = int(inp.shape[1]) if isinstance(inp.shape[1], int) else 448
            except Exception:
                self._input_size = 448
        if not self._names:
            with open(self.tags_path, 'r', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    self._names.append(str(row.get('name') or ''))
                    try:
                        self._categories.append(int(row.get('category') or 0))
                    except Exception:
                        self._categories.append(0)

    def _prep(self, path: str | Path) -> np.ndarray:
        from PIL import Image
        im = Image.open(path).convert('RGB')
        w, h = im.size
        s = max(w, h)
        canvas = Image.new('RGB', (s, s), (255, 255, 255))
        canvas.paste(im, ((s - w) // 2, (s - h) // 2))
        canvas = canvas.resize((self._input_size, self._input_size), Image.BICUBIC)
        arr = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]  # RGB→BGR
        return arr[None, ...]

    def predict(self, path: str | Path, topk: int = 8, characters_only: bool = True) -> list[tuple[str, float]]:
        try:
            self._load()
            out = self._session.run(None, {self._input_name: self._prep(path)})[0][0]
        except Exception:
            return []
        idx = np.argsort(-out)
        hits: list[tuple[str, float]] = []
        for i in idx:
            if characters_only and self._categories[i] != 4:
                continue
            hits.append((self._names[i], float(out[i])))
            if len(hits) >= topk:
                break
        return hits
