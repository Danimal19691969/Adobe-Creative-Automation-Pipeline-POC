"""File-path helpers and locale fallback logic.

This module no longer holds the source of truth for market→locale mappings or
aspect-ratio dimensions — those live in ``inputs/brand/guidelines.yaml`` under
``market_locales`` and ``aspect_ratios``. The helpers below are pure functions
that operate on values passed in by the caller.
"""

from __future__ import annotations


def pick_locale(
    market: str,
    available_locales: list[str],
    market_locales: dict[str, list[str]],
    fallback_language: str = "en",
) -> str:
    """Return the first locale in the market's fallback chain that is available.

    Args:
        market: market code, e.g. "MX".
        available_locales: locale codes that have copy defined in the brief.
        market_locales: market → ordered fallback chain, from brand guidelines.
        fallback_language: default if market is unknown and chain has no match.
    """
    chain = market_locales.get(market, [fallback_language])
    for locale in chain:
        if locale in available_locales:
            return locale
    if fallback_language in available_locales:
        return fallback_language
    raise ValueError(
        f"No suitable locale for market {market!r}; "
        f"chain={chain}, available={available_locales}, fallback={fallback_language}"
    )


def output_path(output_dir: str, product_id: str, ratio: str, market: str, locale: str) -> str:
    """Canonical output filename: outputs/{pid}/{ratio}/{market}_{locale}.png"""
    return f"{output_dir}/{product_id}/{ratio}/{market}_{locale}.png"
