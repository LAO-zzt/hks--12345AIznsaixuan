"""配置文件加载器"""
import os
import yaml
from typing import Dict, Any, Optional


class ConfigLoader:
    """配置文件加载器"""
    
    _instance = None
    _config = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if self._config is None:
            self.load_config()
    
    def load_config(self, config_path: Optional[str] = None):
        """加载配置文件"""
        if config_path is None:
            # 默认配置文件路径
            config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.yaml')
        
        if not os.path.exists(config_path):
            # 如果配置文件不存在，使用默认配置
            self._config = self._get_default_config()
            return
        
        with open(config_path, 'r', encoding='utf-8') as f:
            self._config = yaml.safe_load(f)
    
    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            'llm': {
                'mode': 'ollama',
                'ollama': {
                    'api_url': 'http://localhost:11434/api/chat',
                    'model': 'qwen2.5:7b'
                },
                'openai': {
                    'api_url': 'https://api.openai.com/v1/chat/completions',
                    'api_key': '',
                    'model': 'gpt-3.5-turbo'
                },
                'temperature': 0.3,
                'max_tokens': 500,
                'timeout': 30
            },
            'gaode': {
                'api_key': '',
                'cache_file': 'database/cache/shunde_poi_cache.json',
                'enabled': True
            },
            'database': {
                'db_path': 'database/cleaner.db',
                'batch_size': 200
            },
            'cleaning': {
                'min_quality_score': 0.4,
                'max_consecutive_batch_failures': 3,
                'dedup_similarity_threshold': 0.85
            },
            'source': {
                'default_excel': 'database/testdata/政数局资料-顺德区12345热线工单（2025年1月至3月）.xlsx',
                'upload_dir': 'database/uploads'
            },
            'web': {
                'port': 5000,
                'debug': False,
                'max_upload_size_mb': 500
            },
            'logging': {
                'level': 'INFO',
                'log_file': 'database/logs/cleaner.log'
            }
        }
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """获取配置值
        
        Args:
            key_path: 配置键路径，如 'llm.mode' 或 'database.db_path'
            default: 默认值
        
        Returns:
            配置值
        """
        keys = key_path.split('.')
        value = self._config
        
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        
        return value
    
    def get_llm_config(self) -> Dict[str, Any]:
        """获取LLM配置"""
        return self._config.get('llm', {})
    
    def get_gaode_config(self) -> Dict[str, Any]:
        """获取高德地图配置"""
        return self._config.get('gaode', {})
    
    def get_database_config(self) -> Dict[str, Any]:
        """获取数据库配置"""
        return self._config.get('database', {})
    
    def get_cleaning_config(self) -> Dict[str, Any]:
        """获取清洗参数配置"""
        return self._config.get('cleaning', {})
    
    def get_source_config(self) -> Dict[str, Any]:
        """获取数据源配置"""
        return self._config.get('source', {})
    
    def get_web_config(self) -> Dict[str, Any]:
        """获取Web服务配置"""
        return self._config.get('web', {})
    
    def get_logging_config(self) -> Dict[str, Any]:
        """获取日志配置"""
        return self._config.get('logging', {})


# 全局配置实例
config = ConfigLoader()
