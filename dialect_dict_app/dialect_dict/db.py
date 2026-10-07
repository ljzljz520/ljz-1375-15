"""SQLite 持久层：完整词库只存在于服务端数据库，前端不持有静态词表。"""
import sqlite3
import uuid
from datetime import datetime, timezone


class NotFound(Exception):
    """资源不存在。"""


class Conflict(Exception):
    """状态冲突（如重复关系、非法拆分）。"""


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('editor','approver')),
  token TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS regions(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT DEFAULT '',
  year INTEGER, note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY,
  headword TEXT NOT NULL,
  note TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK(status IN ('draft','review','approved','published','merged')),
  merged_into TEXT REFERENCES entries(id),
  current_version INTEGER NOT NULL DEFAULT 0,
  created_by TEXT, created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS entry_versions(
  entry_id TEXT NOT NULL, version INTEGER NOT NULL,
  headword TEXT NOT NULL, note TEXT DEFAULT '',
  op TEXT NOT NULL, actor TEXT, created_at TEXT,
  PRIMARY KEY(entry_id, version)
);

CREATE TABLE IF NOT EXISTS pronunciations(
  id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  scheme TEXT NOT NULL,            -- jyutping / yale / ipa / pinyin
  text TEXT,                       -- 无法无损转换时为 NULL
  origin TEXT NOT NULL DEFAULT 'recorded' CHECK(origin IN ('recorded','converted')),
  converted_from TEXT,             -- 来源标音记录 id
  status TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok','unconverted')),
  region_id TEXT,
  created_by TEXT, created_at TEXT
);

CREATE TABLE IF NOT EXISTS senses(
  id TEXT PRIMARY KEY,
  entry_id TEXT NOT NULL,
  idx INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted')),
  region_id TEXT,
  created_by TEXT, created_at TEXT,
  deleted_by TEXT, deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS sense_versions(
  sense_id TEXT NOT NULL, version INTEGER NOT NULL,
  definition TEXT NOT NULL, usage_note TEXT DEFAULT '', region_id TEXT,
  op TEXT NOT NULL, actor TEXT, created_at TEXT,
  PRIMARY KEY(sense_id, version)
);

CREATE TABLE IF NOT EXISTS examples(
  id TEXT PRIMARY KEY,
  sense_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted'))
);

CREATE TABLE IF NOT EXISTS example_versions(
  example_id TEXT NOT NULL, version INTEGER NOT NULL,
  text TEXT NOT NULL, translation TEXT DEFAULT '',
  source_id TEXT, region_id TEXT,
  op TEXT NOT NULL, actor TEXT, created_at TEXT,
  PRIMARY KEY(example_id, version)
);

-- 关系边：同音异义 / 异体字 / 地区变体 / 参见
CREATE TABLE IF NOT EXISTS relations(
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL
    CHECK(type IN ('homophone','variant_char','regional_variant','see_also')),
  from_entry TEXT NOT NULL, to_entry TEXT NOT NULL,
  note TEXT DEFAULT '',
  created_by TEXT, created_at TEXT,
  UNIQUE(type, from_entry, to_entry)
);

-- 合并/拆分的移动溯源（拆分时可精确还原）
CREATE TABLE IF NOT EXISTS move_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  op_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('sense','pron','relation_from','relation_to')),
  obj_id TEXT NOT NULL,
  from_entry TEXT, to_entry TEXT, at TEXT
);

CREATE TABLE IF NOT EXISTS audio(
  id TEXT PRIMARY KEY,
  example_id TEXT NOT NULL,
  url TEXT NOT NULL, transcript TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'authorized' CHECK(status IN ('authorized','revoked')),
  revoked_by TEXT, revoked_at TEXT, revoked_reason TEXT DEFAULT ''
);

-- 引用文章：按 (义项, 义项版本) 固化引用
CREATE TABLE IF NOT EXISTS articles(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT DEFAULT '',
  created_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS article_citations(
  article_id TEXT NOT NULL, sense_id TEXT NOT NULL, sense_version INTEGER NOT NULL,
  PRIMARY KEY(article_id, sense_id)
);

-- 版本化关系闭包（发布物，不可变）
CREATE TABLE IF NOT EXISTS publications(
  entry_id TEXT NOT NULL, version INTEGER NOT NULL,
  closure_json TEXT NOT NULL,
  published_by TEXT, published_at TEXT,
  PRIMARY KEY(entry_id, version)
);

-- 异步索引任务：版本守卫保证乱序完成也收敛到最新版
CREATE TABLE IF NOT EXISTS index_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id TEXT NOT NULL, version INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','applied','superseded','failed')),
  created_at TEXT, completed_at TEXT, detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS index_state(
  entry_id TEXT PRIMARY KEY, applied_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS index_docs(
  entry_id TEXT NOT NULL,
  kind TEXT NOT NULL,             -- headword/note/pron/sense_def/sense_note/example/translation
  oid TEXT NOT NULL DEFAULT '',   -- 对象 id（义项/例句/标音）
  sub TEXT NOT NULL DEFAULT '',   -- 子类型（如标音方案）
  raw TEXT NOT NULL,              -- 原文（分字段）
  norm TEXT NOT NULL,             -- 规范化检索键
  offsets TEXT NOT NULL,          -- 规范化字符 -> 原文偏移 的映射(JSON)
  flags TEXT NOT NULL,            -- 该字段应用过的折叠(JSON)
  version INTEGER NOT NULL,
  PRIMARY KEY(entry_id, kind, oid, sub)
);

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT, action TEXT NOT NULL, target TEXT DEFAULT '',
  detail TEXT DEFAULT '', at TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


def connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # :memory: 不支持 WAL
    return conn


def init_db(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def d(row):
    return dict(row) if row is not None else None
