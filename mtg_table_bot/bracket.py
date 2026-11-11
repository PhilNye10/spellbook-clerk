from __future__ import annotations

from dataclasses import asdict, dataclass
import random
from typing import Sequence


@dataclass
class Match:
    match_id: int
    round_number: int
    match_number: int
    player1: str | None
    player2: str | None
    winner: str | None = None
    next_match_id: int | None = None
    next_slot: int | None = None
    bye: bool = False


@dataclass
class Bracket:
    players: tuple[str, ...]
    matches: list[Match]
    seed: int
    champion: str | None = None

    def to_dict(self) -> dict:
        return {
            "players": list(self.players),
            "matches": [asdict(m) for m in self.matches],
            "seed": self.seed,
            "champion": self.champion,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Bracket":
        return cls(
            players=tuple(data.get("players", [])),
            matches=[Match(**m) for m in data.get("matches", [])],
            seed=int(data["seed"]),
            champion=data.get("champion"),
        )

    def get_match(self, match_id: int) -> Match | None:
        return next((m for m in self.matches if m.match_id == match_id), None)

    def active_matches(self) -> list[Match]:
        return [
            m for m in self.matches
            if m.winner is None and m.player1 is not None and m.player2 is not None
        ]

    def record_winner(self, match_id: int, winner: str) -> Match:
        match = self.get_match(match_id)
        if match is None:
            raise ValueError("That match no longer exists.")
        if match.winner is not None:
            raise ValueError("That match has already been completed.")
        if winner not in {match.player1, match.player2}:
            raise ValueError("Winner must be one of the two players in the match.")
        match.winner = winner

        if match.next_match_id is None:
            self.champion = winner
            return match

        next_match = self.get_match(match.next_match_id)
        if next_match is None:
            raise ValueError("Bracket data is corrupted: next match is missing.")
        if match.next_slot == 1:
            next_match.player1 = winner
        else:
            next_match.player2 = winner
        return match

    def finalize_byes(self) -> None:
        changed = True
        while changed:
            changed = False
            for match in self.matches:
                if match.winner is None and match.bye:
                    if match.player1 and not match.player2:
                        match.winner = match.player1
                        changed = True
                        if match.next_match_id is not None:
                            nxt = self.get_match(match.next_match_id)
                            if match.next_slot == 1:
                                nxt.player1 = match.winner
                            else:
                                nxt.player2 = match.winner
                    elif match.player2 and not match.player1:
                        match.winner = match.player2
                        changed = True
                        if match.next_match_id is not None:
                            nxt = self.get_match(match.next_match_id)
                            if match.next_slot == 1:
                                nxt.player1 = match.winner
                            else:
                                nxt.player2 = match.winner


def build_single_elimination(players: Sequence[str], seed: int | None = None) -> Bracket:
    unique = list(dict.fromkeys(players))
    seed = seed if seed is not None else random.SystemRandom().randrange(1, 2**63)
    if not unique:
        return Bracket((), [], seed)
    if len(unique) == 1:
        return Bracket(tuple(unique), [], seed, champion=unique[0])

    rng = random.Random(seed)
    rng.shuffle(unique)

    size = 1
    while size < len(unique):
        size *= 2
    bye_count = size - len(unique)
    bye_players = set(rng.sample(unique, bye_count)) if bye_count else set()
    active_players = [p for p in unique if p not in bye_players]
    rng.shuffle(active_players)

    pairings: list[tuple[str | None, str | None, bool]] = []
    for player in sorted(bye_players, key=lambda p: p):
        pairings.append((player, None, True))
    for i in range(0, len(active_players), 2):
        pairings.append((active_players[i], active_players[i + 1], False))
    rng.shuffle(pairings)

    # The number of first-round matches is always size / 2, including explicit BYE slots.
    while len(pairings) < size // 2:
        pairings.append((None, None, False))

    matches: list[Match] = []
    match_id = 1
    round_counts: list[int] = []
    count = size // 2
    while count >= 1:
        round_counts.append(count)
        count //= 2

    round_match_ids: list[list[int]] = []
    for round_number, match_count in enumerate(round_counts, start=1):
        ids: list[int] = []
        for match_number in range(1, match_count + 1):
            if round_number == 1:
                p1, p2, bye = pairings[match_number - 1]
                matches.append(Match(match_id, round_number, match_number, p1, p2, bye=bye))
            else:
                matches.append(Match(match_id, round_number, match_number, None, None))
            ids.append(match_id)
            match_id += 1
        round_match_ids.append(ids)

    for round_index in range(len(round_match_ids) - 1):
        current = round_match_ids[round_index]
        nxt = round_match_ids[round_index + 1]
        for index, current_id in enumerate(current):
            m = next(x for x in matches if x.match_id == current_id)
            m.next_match_id = nxt[index // 2]
            m.next_slot = 1 if index % 2 == 0 else 2

    bracket = Bracket(tuple(unique), matches, seed)
    bracket.finalize_byes()
    return bracket
