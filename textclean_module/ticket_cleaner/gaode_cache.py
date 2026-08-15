"""高德地图POI缓存模块。

缓存顺德区所有地点信息，用于主体识别验证。
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Set
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError


def _get_gaode_config():
    """获取高德地图配置"""
    try:
        from ticket_cleaner.config_loader import config
        return config.get_gaode_config()
    except Exception:
        return {}


# 顺德区行政区划代码
SHUNDE_DISTRICT_CODE = "440606"

# 顺德区各镇街
SHUNDE_TOWNS = [
    "大良街道", "容桂街道", "伦教街道", "勒流街道",
    "陈村镇", "北滘镇", "乐从镇", "龙江镇",
    "杏坛镇", "均安镇"
]


class GaodePOICache:
    """高德地图POI缓存管理器。"""

    def __init__(self):
        gaode_config = _get_gaode_config()
        self.api_key = gaode_config.get('api_key', '')
        self.enabled = gaode_config.get('enabled', True)
        
        # 缓存文件路径
        cache_file = gaode_config.get('cache_file', 'database/cache/shunde_poi_cache.json')
        if not os.path.isabs(cache_file):
            cache_file = os.path.join(os.path.dirname(__file__), '..', cache_file)
        self.cache_file = os.path.abspath(cache_file)
        
        self._cache: Dict[str, Dict] = {}
        self._load_cache()

    def _load_cache(self):
        """从文件加载缓存。"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._cache = {}

    def _save_cache(self):
        """保存缓存到文件。"""
        os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)

    def search_poi(self, keyword: str, city: str = "佛山") -> Optional[Dict]:
        """搜索POI。"""
        if not self.enabled or not self.api_key:
            return None

        params = {
            "key": self.api_key,
            "keywords": keyword,
            "city": city,
            "citylimit": "true",
            "output": "json",
        }

        url = f"https://restapi.amap.com/v3/place/text?{urlencode(params)}"

        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("status") == "1" and data.get("pois"):
                    pois = data["pois"]
                    # 返回第一个结果
                    poi = pois[0]
                    return {
                        "name": poi.get("name", ""),
                        "address": poi.get("address", ""),
                        "type": poi.get("type", ""),
                        "location": poi.get("location", ""),
                        "tel": poi.get("tel", ""),
                    }
        except (URLError, HTTPError, json.JSONDecodeError, KeyError):
            pass

        return None

    def get_or_search(self, keyword: str) -> Optional[Dict]:
        """获取或搜索POI。"""
        if keyword in self._cache:
            return self._cache[keyword]

        result = self.search_poi(keyword)
        if result:
            self._cache[keyword] = result
            self._save_cache()
            return result

        return None

    def verify_entity(self, entity_name: str) -> bool:
        """验证实体是否在高德地图中存在。"""
        result = self.get_or_search(entity_name)
        return result is not None

    def batch_verify(self, entity_names: List[str]) -> Dict[str, bool]:
        """批量验证实体。"""
        results = {}
        for name in entity_names:
            results[name] = self.verify_entity(name)
            time.sleep(0.1)  # 避免API限流
        return results

    def get_all_cached_names(self) -> Set[str]:
        """获取所有缓存的POI名称。"""
        return set(self._cache.keys())


# 全局缓存实例
_poi_cache: Optional[GaodePOICache] = None


def get_poi_cache() -> GaodePOICache:
    """获取全局POI缓存实例。"""
    global _poi_cache
    if _poi_cache is None:
        _poi_cache = GaodePOICache()
    return _poi_cache


def verify_entity_in_gaode(entity_name: str) -> bool:
    """验证实体是否在高德地图中。"""
    cache = get_poi_cache()
    return cache.verify_entity(entity_name)
