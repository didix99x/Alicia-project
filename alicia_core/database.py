"""
Banco de dados local (SQLite) da Alicia.

Três camadas, bem separadas:
  1. HISTÓRICO     (conversation_history): o que foi efetivamente dito.
  2. MEMÓRIA       (memories, user_profile): fatos úteis e confirmados,
                   com origem e data. Nunca inferências.
  3. VISÃO         (vision_logs + linhas kind='vision'): observações da tela,
                   que NÃO são falas do usuário nem entram no contexto de chat.

Migração: o esquema antigo é preservado. Colunas novas são adicionadas com
ALTER TABLE (nada é apagado ou reescrito além de preencher as colunas novas
das linhas antigas). Um banco corrompido gera DatabaseUnavailable — o arquivo
NUNCA é apagado automaticamente.
"""

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional

from .logging_setup import logger

SCHEMA_VERSION = 2
USER_ROLE_ALIASES = {"user", "usuario", "usuário"}


class DatabaseUnavailable(Exception):
    """Banco ausente, corrompido ou inacessível (mensagem já em português)."""


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


STOPWORDS = {
    "a", "o", "e", "de", "do", "da", "dos", "das", "um", "uma", "que", "para", "com", "em", "no", "na",
    "os", "as", "por", "eu", "voce", "me", "se", "isso", "esse", "essa", "meu", "minha", "mais", "mas",
    "como", "foi", "ser", "ter", "tem", "uns", "umas", "ao", "aos", "ja", "so", "te", "lhe", "nos",
    "alicia", "falamos", "lembra", "lembrar", "sobre", "qual", "quais", "quando", "onde", "ontem", "antes",
}


def keywords(text: str) -> List[str]:
    norm = _strip_accents((text or "").lower())
    words = re.findall(r"[a-z0-9]{3,}", norm)
    seen, out = set(), []
    for w in words:
        if w not in STOPWORDS and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", _strip_accents((text or "").lower())).strip()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def normalize_role(raw_role: str) -> str:
    """Papel padronizado ('user'|'assistant') a partir do papel gravado (que antes era o NOME do usuário)."""
    r = (raw_role or "").strip().lower()
    if r in ("assistant", "alicia") or r.startswith("alicia ("):
        return "assistant"
    return "user"


class MemoryDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        try:
            self._init_db()
        except sqlite3.DatabaseError as e:
            raise DatabaseUnavailable(
                f"Não consegui abrir o banco de memória '{db_path}': {e}. "
                "O arquivo NÃO foi apagado. Faça uma cópia dele e, se quiser, renomeie-o para "
                "criar uma memória nova."
            ) from e

    # ------------------------------------------------------------------
    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 10000")
            with self._lock:
                yield conn
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _column_names(self, conn, table: str) -> set:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def _init_db(self):
        with self._conn() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                raise
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    expression TEXT DEFAULT 'feliz',
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    metadata TEXT
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vision_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_hash TEXT NOT NULL,
                    comment TEXT,
                    expression TEXT,
                    perceived_activity TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            cols = self._column_names(conn, "conversation_history")
            for col, ddl in (
                ("session_id", "TEXT"),
                ("turn_id", "TEXT"),
                ("source", "TEXT"),
                ("kind", "TEXT"),
            ):
                if col not in cols:
                    conn.execute(f"ALTER TABLE conversation_history ADD COLUMN {col} {ddl}")
            if version < 2:
                # Preenche 'kind' das linhas antigas sem alterar role/content.
                conn.execute("""
                    UPDATE conversation_history SET kind = CASE
                        WHEN lower(role) LIKE 'alicia (%' THEN 'vision' ELSE 'chat' END
                    WHERE kind IS NULL""")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_history_turn
                            ON conversation_history(turn_id, role) WHERE turn_id IS NOT NULL""")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_history_kind ON conversation_history(kind, id)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profile (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    category TEXT DEFAULT 'geral',
                    origin TEXT,
                    source_turn_id TEXT,
                    norm_hash TEXT NOT NULL UNIQUE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def check_integrity(self) -> bool:
        try:
            with self._conn() as conn:
                return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            return False

    # ------------------------------------------------------------------
    # HISTÓRICO
    # ------------------------------------------------------------------
    def add_history(
        self,
        role: str,
        content: str,
        expression: str = "feliz",
        metadata: Optional[Dict] = None,
        *,
        turn_id: Optional[str] = None,
        session_id: Optional[str] = None,
        source: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> bool:
        """Grava uma mensagem. Devolve False se for duplicada (mesmo turn_id+papel)."""
        content = (content or "").strip()
        if not content:
            return False
        if kind is None:
            kind = "vision" if (role or "").lower().startswith("alicia (") else "chat"
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO conversation_history "
                    "(role, content, expression, metadata, session_id, turn_id, source, kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (role, content, expression, json.dumps(metadata or {}, ensure_ascii=False),
                     session_id, turn_id, source, kind),
                )
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Mensagem duplicada ignorada (turno {turn_id}).")
            return False

    def add_exchange(
        self,
        user_text: str,
        assistant_text: str,
        expression: str,
        turn_id: str,
        session_id: Optional[str] = None,
        source: str = "keyboard",
        metadata: Optional[Dict] = None,
    ) -> bool:
        """
        Grava a mensagem do usuário e a resposta da Alicia na MESMA transação.
        Se o turno já existir (turn_id repetido) nada é gravado e devolve False.
        Turnos que falham (erro de API) nunca chegam aqui: nada é salvo para eles.
        """
        user_text, assistant_text = (user_text or "").strip(), (assistant_text or "").strip()
        if not user_text or not assistant_text:
            return False
        meta = json.dumps(metadata or {}, ensure_ascii=False)
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO conversation_history "
                    "(role, content, expression, metadata, session_id, turn_id, source, kind) "
                    "VALUES ('user', ?, 'feliz', ?, ?, ?, ?, 'chat')",
                    (user_text, "{}", session_id, turn_id, source))
                conn.execute(
                    "INSERT INTO conversation_history "
                    "(role, content, expression, metadata, session_id, turn_id, source, kind) "
                    "VALUES ('assistant', ?, ?, ?, ?, ?, 'brain', 'chat')",
                    (assistant_text, expression, meta, session_id, turn_id))
            return True
        except sqlite3.IntegrityError:
            logger.warning(f"Turno duplicado ignorado ({turn_id}).")
            return False

    def get_recent_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Compatível com a versão antiga: linhas brutas (inclui visão)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT role, content, expression, timestamp FROM conversation_history "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_chat_messages(self, limit: int = 20, before_id: Optional[int] = None, after_id: int = 0) -> List[Dict[str, Any]]:
        """Últimas mensagens de CONVERSA (sem visão), papéis padronizados, em ordem cronológica."""
        sql = ("SELECT id, role, content, expression, timestamp FROM conversation_history "
               "WHERE COALESCE(kind,'chat')='chat'")
        params: List[Any] = []
        if after_id:
            sql += " AND id > ?"
            params.append(after_id)
        if before_id is not None:
            sql += " AND id < ?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["role"] = normalize_role(d["role"])
            out.append(d)
        return out

    def count_chat_messages(self) -> int:
        with self._conn() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM conversation_history WHERE COALESCE(kind,'chat')='chat'").fetchone()[0]

    def search_history(self, query: str, limit: int = 4, skip_latest: int = 0) -> List[Dict[str, Any]]:
        """
        Procura em conversas ANTIGAS (ignora as `skip_latest` mensagens mais recentes, que já vão
        no contexto). Pontua por quantidade de palavras-chave em comum; empate -> mais recente.
        """
        kws = keywords(query)
        if not kws:
            return []
        with self._conn() as conn:
            max_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM conversation_history").fetchone()[0]
            cutoff = conn.execute(
                "SELECT id FROM conversation_history WHERE COALESCE(kind,'chat')='chat' "
                "ORDER BY id DESC LIMIT 1 OFFSET ?", (max(skip_latest - 1, 0),)).fetchone()
            cutoff_id = cutoff["id"] if (cutoff and skip_latest > 0) else max_id + 1
            rows = conn.execute(
                "SELECT id, role, content, timestamp FROM conversation_history "
                "WHERE COALESCE(kind,'chat')='chat' AND id < ? ORDER BY id DESC LIMIT 2000",
                (cutoff_id,)).fetchall()
        scored = []
        for r in rows:
            words = set(keywords(r["content"]))
            score = sum(1 for k in kws if k in words)
            if score:
                scored.append((score, r["id"], dict(r)))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        out = []
        for _, _, d in scored[:limit]:
            d["role"] = normalize_role(d["role"])
            out.append(d)
        return sorted(out, key=lambda d: d["id"])

    # ------------------------------------------------------------------
    # PERFIL (nome etc.)
    # ------------------------------------------------------------------
    def get_profile(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM user_profile WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_profile(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO user_profile(key, value, updated_at) VALUES(?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (key, value))

    def get_user_name(self) -> Optional[str]:
        return self.get_profile("user_name")

    def set_user_name(self, name: str) -> None:
        self.set_profile("user_name", name.strip())

    # ------------------------------------------------------------------
    # MEMÓRIA DE LONGO PRAZO
    # ------------------------------------------------------------------
    def add_memory(self, text: str, category: str = "geral", origin: str = "dito pelo usuário",
                   source_turn_id: Optional[str] = None) -> Optional[int]:
        """Guarda um fato confirmado. Devolve o id, ou None se já existia."""
        text = re.sub(r"\s+", " ", (text or "")).strip()
        if len(text) < 3:
            return None
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO memories(text, category, origin, source_turn_id, norm_hash) VALUES(?,?,?,?,?)",
                    (text, category, origin, source_turn_id, _hash(text)))
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def list_memories(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, text, category, origin, created_at FROM memories ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def search_memories(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        kws = keywords(query)
        if not kws:
            return []
        scored = []
        for m in self.list_memories(limit=500):
            words = set(keywords(m["text"]))
            score = sum(1 for k in kws if k in words)
            if score:
                scored.append((score, m["id"], m))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [m for _, _, m in scored[:limit]]

    def delete_memories_matching(self, query: str) -> int:
        """Apaga memórias que contenham TODAS as palavras-chave do pedido. Devolve quantas."""
        kws = keywords(query)
        if not kws:
            return 0
        ids = [m["id"] for m in self.list_memories(limit=1000)
               if all(k in set(keywords(m["text"])) for k in kws)]
        if not ids:
            return 0
        with self._conn() as conn:
            conn.executemany("DELETE FROM memories WHERE id=?", [(i,) for i in ids])
        return len(ids)

    # ------------------------------------------------------------------
    # VISÃO
    # ------------------------------------------------------------------
    def log_vision_event(self, image_hash: str, comment: str, expression: str, activity: str = ""):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO vision_logs (image_hash, comment, expression, perceived_activity) VALUES (?, ?, ?, ?)",
                (image_hash, comment, expression, activity))

    # ------------------------------------------------------------------
    # CONTEXTO, BACKUP E EXPORTAÇÃO
    # ------------------------------------------------------------------
    def last_chat_id(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COALESCE(MAX(id),0) FROM conversation_history").fetchone()[0]

    def start_new_context(self) -> None:
        """Marca um novo assunto: o histórico antigo continua guardado e pesquisável,
        mas deixa de ir automaticamente no contexto das próximas respostas."""
        self.set_profile("context_start_id", str(self.last_chat_id()))

    def context_start_id(self) -> int:
        try:
            return int(self.get_profile("context_start_id") or 0)
        except ValueError:
            return 0

    def backup(self, dest_dir: str = "backups", keep: int = 5, min_interval_hours: float = 0) -> Optional[str]:
        """Cópia consistente do banco (API de backup do SQLite). Mantém só as `keep` mais recentes."""
        import datetime, pathlib
        folder = pathlib.Path(dest_dir)
        folder.mkdir(exist_ok=True)
        existing = sorted(folder.glob("alicia_memory_*.db"))
        if min_interval_hours and existing:
            age = time.time() - existing[-1].stat().st_mtime
            if age < min_interval_hours * 3600:
                return None
        name = folder / f"alicia_memory_{datetime.datetime.now():%Y%m%d_%H%M%S}.db"
        src = sqlite3.connect(self.db_path)
        try:
            dst = sqlite3.connect(str(name))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        for old in sorted(folder.glob("alicia_memory_*.db"))[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
        return str(name)

    def export_text(self, dest_dir: str = "exportacoes") -> str:
        """Exporta a conversa (sem observações de tela) e as memórias para um .txt legível."""
        import datetime, pathlib
        folder = pathlib.Path(dest_dir)
        folder.mkdir(exist_ok=True)
        path = folder / f"conversa_{datetime.datetime.now():%Y%m%d_%H%M%S}.txt"
        name = self.get_user_name() or "Usuário"
        lines = ["# Memórias guardadas"]
        lines += [f"- {m['text']} ({m['created_at']} UTC)" for m in self.list_memories(1000)] or ["(nenhuma)"]
        lines += ["", "# Conversa"]
        for m in self.get_chat_messages(limit=1000000):
            who = name if m["role"] == "user" else "Alicia"
            lines.append(f"[{m['timestamp']} UTC] {who}: {m['content']}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)
