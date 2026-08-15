"""Embedding 模块。

策略：
- 默认使用 TF-IDF + TruncatedSVD（本地、无外部依赖）。
- 提供可插拔接口 EmbedderBase，方便替换为外部模型（如 OpenAI/百度/阿里 Embedding）。
- 批处理：一次一个Batch，避免一次性塞入12万条。
- 缓存：相同 content_hash 不重复计算。
"""

from __future__ import annotations

import hashlib
import pickle
from abc import ABC, abstractmethod
from typing import List, Optional

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

from ticket_cleaner.schema import CleanedTicket


# ---------- 简单中文分词（按字+二元） ----------

def tokenize_zh(text: str) -> List[str]:
    """简单中文分词：单字 + 二元gram。

    不依赖 jieba，保证零外部依赖。
    """
    if not text:
        return []
    # 保留中文、字母、数字
    chars = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fa5" or ch.isalnum():
            chars.append(ch)
        else:
            chars.append(" ")
    s = "".join(chars)
    tokens = []
    for word in s.split():
        if len(word) == 1:
            tokens.append(word)
        else:
            # 单字 + 二元gram
            for i in range(len(word)):
                tokens.append(word[i])
            for i in range(len(word) - 1):
                tokens.append(word[i:i + 2])
    return tokens


# ---------- Embedder 接口 ----------

class EmbedderBase(ABC):
    """Embedding 抽象接口。可替换为外部模型。"""

    @abstractmethod
    def fit(self, texts: List[str]) -> None:
        """基于本批文本拟合（如构建词汇表）。"""

    @abstractmethod
    def embed(self, texts: List[str]) -> np.ndarray:
        """生成向量。返回 (n, dim) 的 float32 数组。"""

    @abstractmethod
    def dim(self) -> int:
        """向量维度。"""


class TfidfEmbedder(EmbedderBase):
    """TF-IDF + SVD 降维。

    增量策略：第一次 fit 时构建全局词汇表，后续 batch 复用。
    若新 batch 出现未登录词，TF-IDF transform 会自动忽略。
    """

    def __init__(self, target_dim: int = 256) -> None:
        self.target_dim = target_dim
        self._vectorizer: Optional[TfidfVectorizer] = None
        self._svd: Optional[TruncatedSVD] = None
        self._fitted = False

    def fit(self, texts: List[str]) -> None:
        if not texts:
            return
        # 过滤空文本
        valid = [t for t in texts if t and t.strip()]
        if not valid:
            return
        self._vectorizer = TfidfVectorizer(
            tokenizer=tokenize_zh,
            token_pattern=None,
            min_df=1,
            max_df=0.95,
            sublinear_tf=True,
        )
        tfidf = self._vectorizer.fit_transform(valid)
        # SVD 降维
        n_components = min(self.target_dim, tfidf.shape[1] - 1, tfidf.shape[0] - 1)
        n_components = max(n_components, 1)
        self._svd = TruncatedSVD(n_components=n_components, random_state=42)
        self._svd.fit(tfidf)
        self._fitted = True

    def embed(self, texts: List[str]) -> np.ndarray:
        if not self._fitted or self._vectorizer is None or self._svd is None:
            # 未fit时返回零向量
            return np.zeros((len(texts), self.target_dim), dtype=np.float32)
        # 空文本用占位符
        safe_texts = [t if t and t.strip() else " " for t in texts]
        tfidf = self._vectorizer.transform(safe_texts)
        vec = self._svd.transform(tfidf).astype(np.float32)
        # L2 归一化
        norms = np.linalg.norm(vec, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vec / norms

    def dim(self) -> int:
        if self._svd is not None:
            return self._svd.components_.shape[0]
        return self.target_dim


def serialize_embedding(vec: np.ndarray) -> bytes:
    """序列化向量到bytes。"""
    return pickle.dumps(vec)


def deserialize_embedding(data: bytes) -> np.ndarray:
    """反序列化向量。"""
    return pickle.loads(data)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """两个向量的余弦相似度。"""
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
