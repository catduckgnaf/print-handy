"""The print repair skill (#1777): procedure, references and geometry tools.

``load()`` returns what a heavy-tier repair call needs: the instructions
(``SKILL.md``), the reference files by name, and the tool schemas. Tool calls
run through ``tools.Session``, which confines them to one job folder.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import tools

HERE = Path(__file__).resolve().parent
NAME = 'print_repair'


@dataclass(frozen=True)
class Skill:
    name: str
    instructions: str
    references: dict
    tools: list

    def system_prompt(self) -> str:
        """The instructions followed by every reference, for a model's system message."""
        parts = [self.instructions.strip()]
        for name, text in self.references.items():
            parts.append(f'## Reference: {name}\n\n{text.strip()}')
        return '\n\n'.join(parts) + '\n'


@lru_cache(maxsize=1)
def load() -> Skill:
    """The packaged skill, read once from the files beside this module."""
    references = {path.stem: path.read_text(encoding='utf-8')
                  for path in sorted((HERE / 'references').glob('*.md'))}
    return Skill(name=NAME, instructions=(HERE / 'SKILL.md').read_text(encoding='utf-8'),
                 references=references, tools=tools.openai_tools())


Session = tools.Session
