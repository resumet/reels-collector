from __future__ import annotations

from ..config import settings
from .base import NotSupported, ProviderUnavailable, SearchProvider


def get_provider(name: str) -> SearchProvider:
    if name == "hikerapi":
        from .hikerapi import HikerApiProvider
        return HikerApiProvider()
    if name == "apify":
        from .apify import ApifyProvider
        return ApifyProvider()
    if name in ("mock", "mock_backup"):
        from .mock import MockProvider
        return MockProvider(name)
    raise ProviderUnavailable(f"알 수 없는 제공사: {name}")


def configured_providers() -> list[str]:
    names = [settings.primary_provider]
    if settings.fallback_provider and settings.fallback_provider != settings.primary_provider:
        names.append(settings.fallback_provider)
    return names


def refresh_capable(preferred: str | None):
    """URL 재발급이 가능한 제공사를 선호 순서대로 돌려준다 (Apify 는 미지원)."""
    order = [p for p in [preferred, *configured_providers(), "hikerapi"] if p]
    seen = set()
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        try:
            yield get_provider(name)
        except (ProviderUnavailable, NotSupported):
            continue
