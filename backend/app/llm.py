"""Turn a model spec string from the database into a PydanticAI model.

Specs:
  ollama:<model>            -> your Ollama server (AHQ_OLLAMA_BASE_URL), e.g. ollama:gpt-oss:20b
  openai:<model>            -> OpenAI (OPENAI_API_KEY)
  anthropic:<model>, google-gla:<model>, groq:<model>, ...  -> any PydanticAI provider
  <prefix>:<model>          -> a factory registered with register_model_factory()
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

from .config import settings

_factories: dict[str, Callable[[str], Model]] = {}


def register_model_factory(prefix: str, factory: Callable[[str], Model]) -> None:
    """Add a custom provider prefix (also used by the tests to inject fake models)."""
    _factories[prefix] = factory


def build_model(spec: str) -> Model:
    prefix, _, name = spec.partition(":")
    if prefix in _factories:
        return _factories[prefix](name)
    if prefix == "ollama":
        return OllamaModel(name, provider=OllamaProvider(base_url=settings.ollama_base_url))
    return infer_model(spec)
