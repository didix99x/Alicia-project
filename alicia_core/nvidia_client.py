"""
Cliente da API da NVIDIA (build.nvidia.com / NIM hospedado).

Formato: API compatível com OpenAI.
  - Base:    https://integrate.api.nvidia.com/v1
  - Chat:    POST {base}/chat/completions   (messages com papéis system/user/assistant)
  - Modelos: GET  {base}/models
  - Auth:    cabeçalho "Authorization: Bearer <NVIDIA_API_KEY>"

Garantias deste módulo:
  * A chave nunca entra em logs, exceções ou mensagens ao usuário.
  * Repetição LIMITADA (max_retries) e só para erros temporários
    (timeout, falha de conexão, HTTP 429/500/502/503/504).
  * Erros de autenticação / requisição inválida / modelo inexistente
    NÃO são repetidos.
  * Resposta vazia ou malformada vira BrainError (nunca texto vazio).
  * Não mistura formatos de outros provedores.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional

from .deps import requests
from .env import is_placeholder
from .logging_setup import logger

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Ordem de preferência para uma assistente de conversa em português (persona, instruções longas).
# O iniciador usa o primeiro que existir na SUA conta e responder de verdade a um teste curto.
# Catálogo conferido em out/2026: os Llama 3.x/4 e vários outros foram ENCERRADOS (HTTP 410) e a lista
# /v1/models ainda mostra IDs antigos (HTTP 404 "Not found for account"). Por isso nunca confiamos só
# na lista: cada candidato é testado com uma mensagem real. IDs que não existirem dão 404 e são pulados.
DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
PREFERRED_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b",        # o mais forte, endpoint gratuito (1M de contexto)
    "nvidia/nemotron-3.5-lightning-30b-a3b",    # rápido
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
    "moonshotai/kimi-k3",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-nano-30b-a3b",
    "openai/gpt-oss-120b",
    "nvidia/nvidia-nemotron-nano-9b-v2",
]
# Famílias que costumam ser boas de conversa; usadas para ordenar o que a conta listar além dos preferidos.
GOOD_FAMILIES = ("nemotron", "glm", "kimi", "gpt-oss", "deepseek", "qwen", "minimax", "gemma", "laguna", "mistral-medium", "mistral-small")
# Modelos que NÃO servem para conversa (visão, tradução, código, embeddings, segurança...)
NON_CHAT_HINTS = ("vision", "-vl", "omni", "translate", "riva", "code", "coder", "codestral", "embed", "rerank",
                  "guard", "reward", "parse", "clip", "chatqa", "safety", "voice", "usdcode", "retriev")


def chat_candidates(available, configured=""):
    """
    Ordem de tentativa: o modelo configurado; os preferidos (MESMO que a lista /models da conta
    não os mostre); por fim modelos de famílias boas de chat que a conta listar.
    """
    out = []
    def add(m):
        if m and m not in out:
            out.append(m)
    add(configured)
    for m in PREFERRED_MODELS:
        add(m)
    extras = [m for m in available
              if any(f in m.lower() for f in GOOD_FAMILIES)
              and not any(h in m.lower() for h in NON_CHAT_HINTS)]
    extras.sort(key=lambda m: m.lower(), reverse=True)   # IDs mais novos tendem a ter número maior
    for m in extras[:12]:
        add(m)
    return out


def rank_models(available):
    """Modelos preferidos que existem na conta, do melhor para o pior."""
    have = set(available)
    return [m for m in PREFERRED_MODELS if m in have]


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 10.0  # nunca espera mais que isso entre tentativas


class BrainError(Exception):
    """Falha do cérebro (API). `kind` permite decidir o que mostrar/salvar."""

    USER_MESSAGES = {
        "config": "Minha chave de API não está configurada. Abra o arquivo .env e preencha NVIDIA_API_KEY.",
        "auth": "A NVIDIA recusou minha chave de API (401/403). Gere uma chave nova em build.nvidia.com e atualize o .env.",
        "rate_limit": "Estou com muitas requisições seguidas e a NVIDIA pediu calma. Tenta de novo em instantes.",
        "server": "Os servidores da NVIDIA estão instáveis agora. Tenta de novo em alguns minutos.",
        "timeout": "A NVIDIA demorou demais para responder. Tenta de novo.",
        "network": "Não consegui me conectar à NVIDIA. Confira sua internet.",
        "not_found": "O modelo configurado não existe mais para a sua conta. Feche e rode Iniciar_Alicia.bat: ele escolhe outro sozinho.",
        "gone": "O modelo configurado foi descontinuado pela NVIDIA. Feche e rode Iniciar_Alicia.bat: ele escolhe outro sozinho.",
        "bad_request": "A NVIDIA rejeitou o pedido que eu enviei. Veja os detalhes em logs/alicia_system.log.",
        "bad_response": "A NVIDIA devolveu uma resposta vazia ou inválida. Tenta de novo.",
        "cancelled": "Resposta cancelada.",
    }

    def __init__(self, kind: str, detail: str = "", status: Optional[int] = None):
        self.kind = kind
        self.detail = detail
        self.status = status
        super().__init__(f"[{kind}] {detail}" if detail else f"[{kind}]")

    @property
    def user_message(self) -> str:
        return self.USER_MESSAGES.get(self.kind, "Meu cérebro falhou por um motivo desconhecido.")


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class NvidiaClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 60.0,
        max_retries: int = 2,
        backoff: float = 1.5,
    ):
        self._api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.backoff = float(backoff)
        self.last_error: Optional[BrainError] = None
        self._cancel = threading.Event()
        self._tier = 0  # degrau do formato de pedido que a API aceitou (ver _payload)

    # ------------------------------------------------------------------
    @property
    def has_key(self) -> bool:
        return not is_placeholder(self._api_key)

    @property
    def endpoint_chat(self) -> str:
        return f"{self.base_url}/chat/completions"

    @property
    def endpoint_models(self) -> str:
        return f"{self.base_url}/models"

    def cancel(self) -> None:
        """Interrompe tentativas/esperas pendentes (a requisição em voo termina por timeout)."""
        self._cancel.set()

    def reset_cancel(self) -> None:
        self._cancel.clear()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _require_config(self) -> None:
        if not self.has_key:
            raise BrainError("config", "NVIDIA_API_KEY ausente")
        if not self.model:
            raise BrainError("config", "NVIDIA_MODEL ausente")

    # ------------------------------------------------------------------
    @staticmethod
    def _error_detail(resp) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict):
                err = data.get("error") or data.get("detail") or data.get("message") or data
                if isinstance(err, dict):
                    err = err.get("message") or err
                return str(err)[:300]
        except Exception:
            pass
        return (resp.text or "")[:300]

    def _classify_status(self, resp) -> BrainError:
        status = resp.status_code
        detail = self._error_detail(resp)
        if status in (401, 403):
            return BrainError("auth", f"HTTP {status}: {detail}", status)
        if status == 404:
            return BrainError("not_found", f"HTTP 404: {detail}", status)
        if status == 410:
            return BrainError("gone", f"HTTP 410: {detail}", status)
        if status == 429:
            return BrainError("rate_limit", f"HTTP 429: {detail}", status)
        if status >= 500:
            return BrainError("server", f"HTTP {status}: {detail}", status)
        return BrainError("bad_request", f"HTTP {status}: {detail}", status)

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._cancel.is_set():
                raise BrainError("cancelled")
            time.sleep(0.1)

    # ------------------------------------------------------------------
    def _payload(self, messages, temperature, top_p, max_tokens, tier):
        """
        tier 0: completo (inclui 'chat_template_kwargs' para DESLIGAR o raciocínio dos modelos que pensam;
                sem isso eles gastam os tokens pensando e a resposta chega vazia)
        tier 1: sem temperature/top_p/stream
        tier 2: mínimo (model, messages, max_tokens)
        """
        base = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        if tier == 0:
            base.update({"temperature": temperature, "top_p": top_p, "stream": False})
        if tier <= 1:
            base["chat_template_kwargs"] = {"enable_thinking": False}
        return base

    def _post(self, payload):
        return requests.post(self.endpoint_chat, headers=self._headers(), json=payload, timeout=self.timeout)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.8,
        top_p: float = 0.9,
        max_tokens: int = 400,
    ) -> str:
        """Envia a conversa e devolve o texto da resposta (nunca vazio)."""
        self._require_config()
        last: Optional[BrainError] = None
        for attempt in range(1, self.max_retries + 2):
            if self._cancel.is_set():
                raise BrainError("cancelled")
            retry_after = 0.0
            try:
                resp = self._post(self._payload(messages, temperature, top_p, max_tokens, self._tier))
                if resp.status_code == 200:
                    return self._parse_chat(resp)
                # A API recusou parâmetros? Desce de degrau (no máximo 2 pedidos extras) e lembra.
                tier = self._tier
                while resp.status_code in (400, 422) and tier < 2:
                    tier += 1
                    logger.warning(f"API NVIDIA recusou parâmetros opcionais; tentando o formato mais simples ({tier}).")
                    resp = self._post(self._payload(messages, temperature, top_p, max_tokens, tier))
                    if resp.status_code == 200:
                        self._tier = tier
                        return self._parse_chat(resp)
                last = self._classify_status(resp)
                if resp.status_code in RETRYABLE_STATUS:
                    try:
                        retry_after = min(float(resp.headers.get("Retry-After", 0)), MAX_RETRY_AFTER)
                    except (TypeError, ValueError):
                        retry_after = 0.0
            except requests.exceptions.Timeout:
                last = BrainError("timeout", f"sem resposta em {self.timeout:.0f}s")
            except requests.exceptions.ConnectionError as e:
                last = BrainError("network", type(e).__name__)
            except requests.exceptions.RequestException as e:
                last = BrainError("network", type(e).__name__)
            except BrainError:
                raise

            self.last_error = last
            retryable = last.kind in ("timeout", "network") or (last.status in RETRYABLE_STATUS)
            if not retryable or attempt > self.max_retries:
                break
            wait = max(retry_after, self.backoff * attempt)
            logger.warning(f"API NVIDIA: {last.kind} (tentativa {attempt}/{self.max_retries + 1}); nova tentativa em {wait:.1f}s")
            self._sleep_interruptible(wait)

        assert last is not None
        logger.error(f"API NVIDIA falhou: {last}")
        raise last

    def _parse_chat(self, resp) -> str:
        try:
            data = resp.json()
            text = data["choices"][0]["message"].get("content")
        except Exception:
            raise BrainError("bad_response", "JSON fora do formato esperado")
        if not isinstance(text, str):
            raise BrainError("bad_response", "campo content ausente")
        text = _THINK_RE.sub("", text).strip()
        if not text:
            raise BrainError("bad_response", "resposta vazia")
        return text

    # ------------------------------------------------------------------
    def list_models(self, timeout: float = 15.0) -> List[str]:
        """Lista os IDs de modelo. Levanta BrainError em caso de falha."""
        if not self.has_key:
            raise BrainError("config", "NVIDIA_API_KEY ausente")
        try:
            resp = requests.get(self.endpoint_models, headers=self._headers(), timeout=timeout)
        except requests.exceptions.Timeout:
            raise BrainError("timeout", "listando modelos")
        except requests.exceptions.RequestException as e:
            raise BrainError("network", type(e).__name__)
        if resp.status_code != 200:
            raise self._classify_status(resp)
        try:
            return [m["id"] for m in resp.json().get("data", []) if isinstance(m, dict) and m.get("id")]
        except Exception:
            raise BrainError("bad_response", "lista de modelos inválida")


def discover_model(key, base_url, configured="", time_budget=150.0, per_try_timeout=40.0):
    """
    Procura um modelo que RESPONDA de verdade. Devolve um dict:
      {"ok": True, "chosen": id|None, "changed": bool, "tried": [[id, kind, detalhe]], "listed": bool, "models": [...]}
      {"ok": False, "kind": "auth"|"network"|..., "detail": ...}   (falha que impede qualquer teste)
    """
    import time as _t
    configured = (configured or "").strip() or DEFAULT_MODEL
    probe = NvidiaClient(key, configured, base_url)
    try:
        models = probe.list_models()
    except BrainError as e:
        return {"ok": False, "kind": e.kind, "detail": e.detail}
    tried, t0 = [], _t.time()
    for m in chat_candidates(models, configured):
        if _t.time() - t0 > time_budget:
            break
        c = NvidiaClient(key, m, base_url, timeout=per_try_timeout, max_retries=1, backoff=1.0)
        try:
            c.chat([{"role": "user", "content": "Responda apenas: ok"}], max_tokens=64)
            return {"ok": True, "chosen": m, "changed": m != configured, "configured": configured,
                    "tried": tried, "listed": m in models}
        except BrainError as e:
            if e.kind in ("auth", "network"):
                return {"ok": False, "kind": e.kind, "detail": e.detail}
            tried.append([m, e.kind, e.detail])
    return {"ok": True, "chosen": None, "configured": configured, "tried": tried,
            "models": [m for m in models if any(f in m.lower() for f in GOOD_FAMILIES)][:40] or models[:40]}


def normalize_messages(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Garante um histórico aceito por qualquer modelo de chat:
      - 1 mensagem 'system' no início (se houver);
      - a primeira mensagem não-system é 'user';
      - papéis alternam (mensagens consecutivas do mesmo papel são unidas);
      - sem conteúdo vazio.
    """
    system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    convo: List[Dict[str, str]] = []
    for m in messages:
        role, content = m.get("role"), (m.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        if not convo and role == "assistant":
            continue
        if convo and convo[-1]["role"] == role:
            convo[-1]["content"] += "\n" + content
        else:
            convo.append({"role": role, "content": content})
    out: List[Dict[str, str]] = []
    if system_parts:
        out.append({"role": "system", "content": "\n\n".join(system_parts)})
    return out + convo
