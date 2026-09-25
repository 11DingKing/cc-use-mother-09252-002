"""服务启动入口：``python3 -m service_09252_002``。

数据库路径由环境变量 RECOGNITION_DB 指定（默认 ./recognition.db），
运行数据不写入源码目录之外的约定由部署方保证。
"""
from __future__ import annotations

import os

from .api import create_app


def main() -> None:
    app = create_app()
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
