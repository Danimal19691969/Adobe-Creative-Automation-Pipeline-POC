"""File-path helpers and locale fallback rules."""

from __future__ import annotations

# Spec §6.3 — market → ordered locale fallback chain.
MARKET_TO_LOCALE: dict[str, list[str]] = {
    "MX": ["es", "en"],
    "BR": ["pt", "en"],
    "CO": ["es", "en"],
    "US": ["en"],
}

ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1x1":  (1080, 1080),
    "9x16": (1080, 1920),
    "16x9": (1920, 1080),
}


def pick_locale(market: str, available_locales: list[str]) -> str:
    """Return the first locale in the market's fallback chain that is available.

    Falls back to 'en' for unknown markets. Raises if 'en' itself is missing
    (BriefParserAgent already guards this, but defensive here).
    """
    chain = MARKET_TO_LOCALE.get(market, ["en"])
    for locale in chain:
        if locale in available_locales:
            return locale
    if "en" in available_locales:
        return "en"
    raise ValueError(
        f"No suitable locale for market {market!r}; "
        f"chain={chain}, available={available_locales}"
    )


def output_path(output_dir: str, product_id: str, ratio: str, market: str, locale: str) -> str:
    """Canonical output filename: outputs/{pid}/{ratio}/{market}_{locale}.png"""
    return f"{output_dir}/{product_id}/{ratio}/{market}_{locale}.png"
