# -*- coding: utf-8 -*-
"""SQLite 持久层。

设计要点：
- 所有可变实体表都是「追加式版本表」：PK = (id, version)，更新 = 插入 version+1 的新行，
  旧版本行永不修改/删除。这是旧深链接、引用存档、版本闭包的基础。
- closure 表：每次发布把当时全部实体的 (kind, id, version) 指针快照进去，
  即「版本化关系闭包」——闭包同时钉住关系两端实体的版本。
- media_rights 单独一张「永远当前」的表：内容可版本化，权利状态必须即时生效（音频撤权）。
"""
import sqlite3, threading, time, uuid
from contextlib import contextmanager

def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY, token TEXT NOT NULL, role TEXT NOT NULL);  -- role: editor|approver

CREATE TABLE IF NOT EXISTS entries(
  id TEXT, version INT, headword TEXT, context_note TEXT,
  status TEXT,            -- active | merged
  merged_into TEXT, split_from TEXT,
  updated_by TEXT, updated_at REAL, PRIMARY KEY(id, version));

CREATE TABLE IF NOT EXISTS forms(          -- 词形（含异体字），script: simp|trad|variant|latin...
  id TEXT, version INT, entry_id TEXT, text TEXT, script TEXT,
  is_standard INT, note TEXT, deleted INT DEFAULT 0, PRIMARY KEY(id, version));

CREATE TABLE IF NOT EXISTS prons(          -- 标音：永远保存原始方案与原始记录
  id TEXT, version INT, entry_id TEXT, scheme TEXT, value TEXT,
  region TEXT, source_id TEXT, deleted INT DEFAULT 0, PRIMARY KEY(id, version));

CREATE TABLE IF NOT EXISTS conversions(    -- 标音转换结果缓存；status: ok|unconvertible
  id TEXT PRIMARY KEY, pron_id TEXT, target_scheme TEXT, value TEXT,
  status TEXT, detail TEXT, rule_version INT, created_at REAL);

CREATE TABLE IF NOT EXISTS senses(         -- 义项；deleted=1 为软删除（引用仍可解析存档）
  id TEXT, version INT, entry_id TEXT, num INT, pos TEXT, definition TEXT,
  region TEXT, deleted INT DEFAULT 0, PRIMARY KEY(id, version));

CREATE TABLE IF NOT EXISTS examples(
  id TEXT, version INT, sense_id TEXT, text TEXT, translation TEXT,
  region TEXT, source_id TEXT, media_id TEXT, deleted INT DEFAULT 0, PRIMARY KEY(id, version));

CREATE TABLE IF NOT EXISTS relations(      -- 关系边：homophone|variant_char|regional_variant|merged_into|split_from|see
  id TEXT, version INT, src TEXT, dst TEXT, type TEXT, note TEXT,
  deleted INT DEFAULT 0, PRIMARY KEY(id, version));

CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, title TEXT, author TEXT, year INT, license TEXT);

CREATE TABLE IF NOT EXISTS media(
  id TEXT, version INT, path TEXT, transcript TEXT,
  deleted INT DEFAULT 0, PRIMARY KEY(id, version));

CREATE TABLE IF NOT EXISTS media_rights(   -- 权利状态即时生效，不随版本快照
  media_id TEXT PRIMARY KEY, revoked INT DEFAULT 0, updated_at REAL);

CREATE TABLE IF NOT EXISTS citations(      -- 引用文章对义项的引用：钉住 sense_version
  id TEXT PRIMARY KEY, article_uri TEXT, sense_id TEXT, sense_version INT, created_at REAL);

CREATE TABLE IF NOT EXISTS versions(       -- 发布版本
  id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, published_by TEXT, published_at REAL);

CREATE TABLE IF NOT EXISTS closure(        -- 版本化关系闭包
  version_id INT, kind TEXT, entity_id TEXT, entity_version INT,
  PRIMARY KEY(version_id, kind, entity_id));

CREATE TABLE IF NOT EXISTS pointers(key TEXT PRIMARY KEY, value INT);  -- published / indexed

CREATE TABLE IF NOT EXISTS index_tasks(    -- 异步索引任务；允许乱序完成
  id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INT, entry_id TEXT,
  status TEXT DEFAULT 'pending', done_at REAL);

CREATE TABLE IF NOT EXISTS idx_norm(       -- 规范化检索键索引（含原文与偏移映射，供可解释高亮）
  entry_id TEXT, version_id INT, field TEXT, sub_id TEXT,
  text TEXT, norm_text TEXT, offsets TEXT, rules TEXT,
  PRIMARY KEY(entry_id, version_id, field, sub_id));

CREATE TABLE IF NOT EXISTS idx_tokens(     -- 原文分字段倒排（不做任何折叠）
  token TEXT, entry_id TEXT, version_id INT, field TEXT, sub_id TEXT,
  start INT, end INT);
CREATE INDEX IF NOT EXISTS idx_tokens_t ON idx_tokens(token);

CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, detail TEXT, at REAL);
"""

class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.tx() as c:
            c.executescript(SCHEMA)
        for k in ("published", "indexed"):
            self.execute("INSERT OR IGNORE INTO pointers(key,value) VALUES(?,0)", (k,))

    @contextmanager
    def tx(self):
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def execute(self, sql, args=()):
        with self.tx() as c:
            return c.execute(sql, args)

    def query(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def get_pointer(self, key) -> int:
        r = self.one("SELECT value FROM pointers WHERE key=?", (key,))
        return r["value"] if r else 0

    def set_pointer(self, key, value: int):
        self.execute("INSERT INTO pointers(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def audit(self, actor, action, detail):
        self.execute("INSERT INTO audit(actor,action,detail,at) VALUES(?,?,?,?)",
                     (actor, action, detail, time.time()))
