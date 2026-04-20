"""Session + SessionManager 單元測試。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication
    import sys
    app = QApplication.instance() or QApplication(sys.argv)
    return app


def test_session_default_id_is_uuid(qt_app):
    from teleprompter.core.session import Session
    s = Session()
    assert len(s.session_id) >= 32
    assert s.title == "未命名"


def test_session_to_json_round_trip(qt_app):
    from teleprompter.core.session import Session
    from teleprompter.core.rich_text_format import FormatSpan

    s = Session(
        title="test",
        transcript_path="/tmp/a.txt",
        slides_path="/tmp/b.pdf",
        current_global_char=120,
        current_sentence_index=5,
        skipped_ranges=[(0, 10), (50, 60)],
        format_spans=[FormatSpan(start=2, end=8, bold=True)],
        modified_text="Hello edited world",
        dirty=True,
    )
    data = s.to_json()
    assert "transcript" not in data
    assert "engine" not in data
    assert data["modified_text"] == "Hello edited world"
    assert data["dirty"] is True
    s2 = Session.from_json(data)
    assert s2.session_id == s.session_id
    assert s2.title == s.title
    assert s2.transcript_path == s.transcript_path
    assert s2.slides_path == s.slides_path
    assert s2.current_global_char == 120
    assert s2.current_sentence_index == 5
    assert s2.skipped_ranges == [(0, 10), (50, 60)]
    assert len(s2.format_spans) == 1
    assert s2.format_spans[0].bold
    assert s2.modified_text == "Hello edited world"
    assert s2.dirty is True


def test_manager_add_and_activate(qt_app):
    from teleprompter.core.session import Session, SessionManager

    m = SessionManager()
    s1 = Session(title="A")
    m.add(s1)
    assert m.active is s1
    assert m.active_id == s1.session_id


def test_manager_add_without_activate(qt_app):
    from teleprompter.core.session import Session, SessionManager

    m = SessionManager()
    s1 = Session(title="A")
    s2 = Session(title="B")
    m.add(s1)  # auto-activate
    m.add(s2, activate=False)
    assert m.active is s1


def test_manager_remove_active_switches_to_neighbor(qt_app):
    from teleprompter.core.session import Session, SessionManager

    m = SessionManager()
    s1 = Session(title="A")
    s2 = Session(title="B")
    s3 = Session(title="C")
    m.add(s1)
    m.add(s2)
    m.add(s3)
    m.set_active(s2.session_id)
    m.remove(s2.session_id)
    # 刪除 active（中間）→ 切到同 index 或 index-1
    assert m.active is not None
    assert m.active.session_id in (s1.session_id, s3.session_id)


def test_manager_remove_last_sets_empty_active(qt_app):
    from teleprompter.core.session import Session, SessionManager

    m = SessionManager()
    s1 = Session(title="A")
    m.add(s1)
    m.remove(s1.session_id)
    assert m.active is None
    assert m.active_id == ""


def test_manager_move_reorders(qt_app):
    from teleprompter.core.session import Session, SessionManager

    m = SessionManager()
    s1 = Session(title="A")
    s2 = Session(title="B")
    s3 = Session(title="C")
    m.add(s1); m.add(s2); m.add(s3)
    # C → 最前
    m.move(s3.session_id, 0)
    titles = [s.title for s in m.sessions]
    assert titles == ["C", "A", "B"]


def test_manager_persistence_round_trip(tmp_path, qt_app):
    from teleprompter.core.session import Session, SessionManager

    m = SessionManager()
    m.add(Session(title="first", transcript_path="/tmp/a.txt"))
    m.add(Session(title="second", slides_path="/tmp/b.pdf"))
    m.set_active(m.sessions[0].session_id)

    path = tmp_path / "sessions.json"
    m.save_to_disk(path)
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert len(raw["sessions"]) == 2

    m2 = SessionManager()
    m2.load_from_disk(path)
    assert len(m2) == 2
    assert m2.sessions[0].title == "first"
    assert m2.sessions[1].title == "second"
    assert m2.active_id == m.sessions[0].session_id


def test_load_from_disk_missing_file_no_op(tmp_path, qt_app):
    from teleprompter.core.session import SessionManager

    m = SessionManager()
    m.load_from_disk(tmp_path / "nope.json")
    assert len(m) == 0


def test_signals_fire_on_add_remove(qt_app):
    from teleprompter.core.session import Session, SessionManager

    m = SessionManager()
    changes: list[str] = []
    actives: list[str] = []
    m.sessions_changed.connect(lambda: changes.append("x"))
    m.active_session_changed.connect(lambda sid: actives.append(sid))

    s1 = Session()
    m.add(s1)
    assert changes  # at least once
    assert actives and actives[-1] == s1.session_id

    m.remove(s1.session_id)
    assert actives[-1] == ""
