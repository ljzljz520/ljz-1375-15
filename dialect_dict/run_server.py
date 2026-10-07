#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动：python3 run_server.py [--db dialect.db] [--port 8000]"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core.db import DB
from core.service import Service
from core.indexer import Indexer
from core.seed import seed
from core.api import serve

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "dialect.db"))
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    db = DB(args.db)
    svc = Service(db)
    seed(db, svc)
    if db.get_pointer("published") == 0:          # 首次启动：发布 v1
        svc.publish("approver1", "初始发布")
    idx = Indexer(db, svc)
    idx.start_background()                        # 异步索引：随机取任务，乱序完成
    srv = serve(svc, idx, args.port)
    print(f"公开检索页:  http://127.0.0.1:{args.port}/")
    print(f"审校后台:    http://127.0.0.1:{args.port}/editor  (editor1/ed-token, approver1/ap-token)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        idx.stop()

if __name__ == "__main__":
    main()
