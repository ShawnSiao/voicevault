from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import templates


@dataclass(frozen=True)
class KnowledgeBase:
    root: Path

    @classmethod
    def from_path(cls, root: str | Path) -> "KnowledgeBase":
        return cls(Path(root).expanduser().resolve())

    @property
    def content_dir(self) -> Path:
        return self.root / "content"

    @property
    def roles_dir(self) -> Path:
        return self.content_dir / "roles"

    @property
    def events_dir(self) -> Path:
        return self.content_dir / "events"

    @property
    def topics_dir(self) -> Path:
        return self.content_dir / "topics"

    @property
    def reports_dir(self) -> Path:
        return self.content_dir / "reports"

    @property
    def sources_dir(self) -> Path:
        return self.content_dir / "sources"

    @property
    def evaluations_dir(self) -> Path:
        return self.content_dir / "evaluations"

    @property
    def inbox_dir(self) -> Path:
        return self.root / "inbox"

    @property
    def inbox_captures_dir(self) -> Path:
        return self.inbox_dir / "captures"

    @property
    def inbox_archive_dir(self) -> Path:
        return self.inbox_dir / "archive"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def state_dir(self) -> Path:
        return self.root / ".voicevault"

    @property
    def index_path(self) -> Path:
        return self.state_dir / "index.sqlite"


def _write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8", newline="\n")


def init_kb(root: str | Path) -> KnowledgeBase:
    kb = KnowledgeBase.from_path(root)
    for directory in [
        kb.roles_dir,
        kb.events_dir,
        kb.topics_dir,
        kb.reports_dir,
        kb.sources_dir,
        kb.evaluations_dir,
        kb.inbox_dir,
        kb.inbox_captures_dir,
        kb.inbox_archive_dir,
        kb.exports_dir,
        kb.state_dir,
    ]:
        directory.mkdir(parents=True, exist_ok=True)

    sample_role = kb.roles_dir / "sample-investor"
    sample_theses = sample_role / "theses"
    sample_theses.mkdir(parents=True, exist_ok=True)
    _write_if_missing(sample_role / "profile.md", templates.PROFILE_MD)
    _write_if_missing(sample_role / "statements.csv", templates.STATEMENTS_CSV)
    _write_if_missing(sample_theses / "ai-infrastructure.md", templates.EXAMPLE_THESIS_MD)
    _write_if_missing(kb.events_dir / "example-event.md", templates.EXAMPLE_EVENT_MD)
    _write_if_missing(kb.inbox_captures_dir / "README.md", templates.CAPTURES_README_MD)
    return kb
