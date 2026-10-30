"""
Cliente HTTP para a API local do Ollama (/api/generate, /api/tags).
"""

import json
from typing import Any, Dict, List, Optional

from .deps import requests
from .logging_setup import logger


class OllamaClient:
    def __init__(self, host: str):
        self.host = host.rstrip("/")
        self.endpoint_generate = f"{self.host}/api/generate"
        self.endpoint_tags = f"{self.host}/api/tags"
        self.last_error = ""

    def check_health(self) -> bool:
        try:
            return requests.get(self.endpoint_tags, timeout=5).status_code == 200
        except Exception as e:
            self.last_error = str(e)
            return False

    def list_models(self) -> List[str]:
        try:
            data = requests.get(self.endpoint_tags, timeout=5).json()
            return [str(m.get("name", "")).lower() for m in data.get("models", []) if m.get("name")]
        except Exception as e:
            logger.debug(f"Não foi possível listar modelos do Ollama: {e}")
            return []

    def generate(self, model: str, prompt: str, images: Optional[List[str]] = None,
                 options: Optional[Dict] = None, timeout: int = 75) -> Optional[str]:
        # Remove imagens vazias/corrompidas antes de enviar ao Ollama.
        clean_images = [x.strip() for x in (images or []) if isinstance(x, str) and len(x.strip()) > 32]
        
        # Garante um contexto maior por padrão (8192) para evitar estouros com imagens e prompts longos
        req_options = {"num_ctx": 8192}
        if options:
            req_options.update(options)

        payload = {
            "model": str(model).strip(), 
            "prompt": str(prompt), 
            "stream": False,
            "options": req_options
        }
        if clean_images:
            payload["images"] = clean_images

        if not payload["model"]:
            self.last_error = "Nenhum modelo configurado."
            logger.error(self.last_error)
            return None

        for attempt in range(1, 3):
            try:
                r = requests.post(self.endpoint_generate, json=payload, timeout=timeout)
                if r.status_code >= 400:
                    # O corpo da resposta contém a causa real do 400; antes ela era descartada.
                    try:
                        detail = r.json().get("error", r.text)
                    except Exception:
                        detail = r.text
                    self.last_error = f"HTTP {r.status_code}: {detail}"
                    logger.error(f"Erro Ollama em {self.endpoint_generate} | modelo={payload['model']} | imagens={len(clean_images)} | {self.last_error}")
                    return None
                data = r.json()
                return str(data.get("response", "")).strip()
            except requests.exceptions.Timeout:
                self.last_error = f"Timeout após {timeout}s"
                logger.warning(f"Timeout Ollama (tentativa {attempt}/2)")
            except requests.exceptions.RequestException as e:
                self.last_error = str(e)
                logger.error(f"Erro HTTP Ollama: {e}")
                return None
        return None
