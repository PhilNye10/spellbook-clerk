from __future__ import annotations
from dataclasses import dataclass
import random
from typing import Sequence

@dataclass(frozen=True)
class Table:
    number: int
    players: tuple[str, ...]

@dataclass(frozen=True)
class Assignment:
    mode: str
    players: tuple[str, ...]
    tables: tuple[Table, ...]
    seed: int


def _score(groups: list[int], preferred: int, minimum: int, maximum: int) -> tuple:
    # Lower is better. Prefer fewer tables, groups near preferred size, and no 2-person tables.
    penalty = 0
    for n in groups:
        penalty += abs(n - preferred) * 10
        if n < minimum:
            penalty += 1000 + (minimum - n) * 100
        if n > maximum:
            penalty += 1000 + (n - maximum) * 100
        if n == 2:
            penalty += 500
        if n == 1:
            penalty += 5000
    return (penalty, len(groups), tuple(sorted(groups)))


def choose_sizes(count: int, preferred: int, minimum: int, maximum: int) -> list[int]:
    if count <= 0:
        return []
    best = None
    for tables in range(1, count + 1):
        def rec(remaining: int, parts: list[int]):
            nonlocal best
            if remaining == 0:
                candidate = sorted(parts, reverse=True)
                s = _score(candidate, preferred, minimum, maximum)
                if best is None or s < best[0]:
                    best = (s, candidate)
                return
            if len(parts) >= tables:
                return
            for size in range(minimum, maximum + 1):
                if size <= remaining:
                    rec(remaining - size, parts + [size])
        rec(count, [])
        # A valid solution at this table count is enough once it is found.
        if best and best[0][0] < 1000:
            break
    if not best:
        # Graceful fallback for tiny groups: one table.
        return [count]
    return best[1]


def make_assignment(players: Sequence[str], mode: str, preferred: int, minimum: int, maximum: int, seed: int | None = None) -> Assignment:
    unique = list(dict.fromkeys(players))
    seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**63)
    rng = random.Random(seed)
    rng.shuffle(unique)
    sizes = choose_sizes(len(unique), preferred, minimum, maximum)
    tables: list[Table] = []
    pos = 0
    for i, size in enumerate(sizes, 1):
        tables.append(Table(i, tuple(unique[pos:pos + size])))
        pos += size
    return Assignment(mode, tuple(unique), tuple(tables), seed)
