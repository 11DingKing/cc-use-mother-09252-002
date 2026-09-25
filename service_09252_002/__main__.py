"""服务入口：python3 -m service_09252_002

环境变量：
- SKILL_MUTUAL_HOST（默认 127.0.0.1）
- SKILL_MUTUAL_PORT（默认 8080）
- SKILL_MUTUAL_DB（默认 ~/.local/share/service_09252_002/mutual.db）
- SKILL_MUTUAL_ADMIN_TOKEN（引导管理员令牌）
"""
from __future__ import annotations

import os

from .api import create_server
from .app import DEFAULT_DATA_DIR, build_service


def main() -> None:
    db_path = os.environ.get("SKILL_MUTUAL_DB", str(DEFAULT_DATA_DIR / "mutual.db"))
    host = os.environ.get("SKILL_MUTUAL_HOST", "127.0.0.1")
    port = int(os.environ.get("SKILL_MUTUAL_PORT", "8080"))
    container = build_service(db_path)
    server = create_server(container.service, host, port)
    actual_port = server.server_address[1]
    try:
        print(f"职业技能标准互认服务已启动: http://{host}:{actual_port} （数据库: {db_path}）")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        container.close()


if __name__ == "__main__":
    main()
