#!/usr/bin/env python3
"""启动方言词典系统：  python3 run.py [port]

- 数据落盘 ./data/dict.db（完整词库只在服务端，前端无静态词表）
- 后台异步索引线程消费 index_tasks
- 公开页 http://127.0.0.1:8000/  后台 http://127.0.0.1:8000/admin
  演示账号：编辑 tok-editor / 审校 tok-approver
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dialect_dict import db, seed
from dialect_dict.indexer import Indexer
from dialect_dict.api import create_server

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "dict.db")
STATIC = os.path.join(BASE, "static")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    os.makedirs(DATA, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    if seed.seed(conn):
        print("已写入演示数据")
    conn.close()

    indexer = Indexer(lambda: db.connect(DB_PATH))
    indexer.complete_all()          # 启动时先消化遗留任务
    indexer.start_background()

    server = create_server(DB_PATH, STATIC, port=port)
    print("公开页:  http://127.0.0.1:%d/" % port)
    print("后台:    http://127.0.0.1:%d/admin" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        indexer.stop()
        server.server_close()


if __name__ == "__main__":
    main()
