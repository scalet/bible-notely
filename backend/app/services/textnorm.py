"""Normalização e comparação de texto em português, sem modelo de ML.

Serve para achar versículo e hino na transcrição. O custo é um conjunto de
tokens por trecho, o bastante para um PC comum.
"""

from __future__ import annotations

import re
import unicodedata

_STOP = {
    "que", "para", "com", "nao", "uma", "por", "mais", "como", "mas", "foi",
    "ele", "das", "tem", "seu", "sua", "ser", "quando", "muito", "nos", "ja",
    "tambem", "pelo", "pela", "ate", "isso", "ela", "entre", "era", "depois",
    "sem", "mesmo", "aos", "ter", "seus", "quem", "nas", "esse", "eles",
    "estao", "voce", "tinha", "foram", "essa", "num", "nem", "suas", "meu",
    "minha", "numa", "pelos", "elas", "havia", "seja", "qual", "sera", "lhe",
    "deles", "essas", "esses", "pelas", "esta", "estes", "estas", "isto",
    "aquele", "aquela", "vos", "dos", "das", "uma", "uns", "umas", "the",
}


def fold(text: str) -> str:
    stripped = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in stripped if unicodedata.category(ch) != "Mn").lower()


def content_tokens(text: str) -> list[str]:
    """Palavras que distinguem um trecho. Artigos e preposições saem."""
    found = []
    for token in re.findall(r"[a-z0-9]+", fold(text)):
        if len(token) < 3 or token in _STOP:
            continue
        found.append(token)
    return found


def overlap_score(left: list[str], right: list[str]) -> float:
    """Quanto do lado menor aparece no outro. 1.0 é o mesmo vocabulário."""
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def signature(text: str) -> str:
    """Impressão estável de um trecho cantado, para a biblioteca local."""
    return " ".join(sorted(set(content_tokens(text)))[:48])
