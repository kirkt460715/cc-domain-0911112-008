"""OAI-PMH 风格采集：协议解析、分页采集与来源落库。"""
from .manager import HarvestManager, local_identifier_for

__all__ = ["HarvestManager", "local_identifier_for"]
