"""组合根：装配端口、持久化与应用服务。

运行数据默认写入用户数据目录（而非源码目录），可用环境变量覆盖：
- SKILL_MUTUAL_DB：SQLite 数据库路径
- SKILL_MUTUAL_ADMIN_TOKEN：引导管理员令牌（开发默认值 dev-admin-token）
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .ports import Clock, IdGenerator, SystemClock, UuidIds
from .services import MutualRecognitionService
from .storage import Database

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "service_09252_002"
DEFAULT_ADMIN_TOKEN = "dev-admin-token"


@dataclass
class ServiceContainer:
    service: MutualRecognitionService
    db: Database

    def close(self) -> None:
        self.db.close()


def build_service(db_path: str, *, clock: Clock | None = None,
                  id_generator: IdGenerator | None = None,
                  admin_token: str | None = None) -> ServiceContainer:
    """构建完整服务；测试可注入固定时钟与序列 ID 以稳定复现。"""
    db = Database(str(db_path))
    service = MutualRecognitionService(db, clock or SystemClock(),
                                       id_generator or UuidIds())
    token = admin_token or os.environ.get("SKILL_MUTUAL_ADMIN_TOKEN",
                                          DEFAULT_ADMIN_TOKEN)
    service.ensure_bootstrap_admin(token)
    return ServiceContainer(service=service, db=db)
