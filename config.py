from __future__ import annotations
from dataclasses import dataclass
import os
import yaml

@dataclass(frozen=True)
class ModeConfig:
    preferred_size: int
    minimum_size: int
    maximum_size: int

@dataclass(frozen=True)
class AppConfig:
    draft: ModeConfig
    commander: ModeConfig
    attending_option: str = "✅"
    include_maybe: bool = False
    admin_only_commands: bool = True


def load_config(path: str = "config.yaml") -> AppConfig:
    raw = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    def mode(name: str, defaults: tuple[int, int, int]) -> ModeConfig:
        x = raw.get(name, {})
        return ModeConfig(
            int(x.get("preferred_size", defaults[0])),
            int(x.get("minimum_size", defaults[1])),
            int(x.get("maximum_size", defaults[2])),
        )
    rsvp = raw.get("rsvp", {})
    perms = raw.get("permissions", {})
    return AppConfig(
        draft=mode("draft", (4, 3, 5)),
        commander=mode("commander", (4, 3, 5)),
        attending_option=str(rsvp.get("attending_option", "✅")),
        include_maybe=bool(rsvp.get("include_maybe", False)),
        admin_only_commands=bool(perms.get("admin_only_commands", True)),
    )
