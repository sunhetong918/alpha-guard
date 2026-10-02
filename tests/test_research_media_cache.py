from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from research.store import ResearchStore

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def source(**changes):
    return {"external_id": "7686727666610883903", "platform": "douyin",
            "source_url": "https://www.douyin.com/video/7686727666610883903",
            "title": "同一视频的原始标题", "author": "原始作者", "published_at": "2026-09-18T04:29:00+00:00",
            "summary": "来源简介", "content_kind": "metadata", "completeness": "metadata", **changes}


def transcript(**changes):
    text = "这段来自视频的完整转写仍然需要人工核对，保存的发布时间和取得时间都不得因为再次扫描而刷新。"
    return source(content_kind="transcript", completeness="full", content_text=text,
        segments=[{"start_seconds": 0.0, "end_seconds": 10.0, "text": text, "speaker": ""}],
        quality_issues=["local_media_transcription", "machine_transcription_unverified"], **changes)


def test_reuse_returns_original_record_without_renewing_any_clocks(tmp_path):
    with ResearchStore(tmp_path / "state.db") as store:
        old = store.upsert_evidence(transcript(), now=NOW)
        cached = store.latest_reusable_transcript(source())
        assert cached is not None and cached == old
        saved_again = store.upsert_evidence(cached, now=NOW + timedelta(days=5))
        assert saved_again == old
        assert len(store.list_evidence()) == 1


@pytest.mark.parametrize("changes", [
    {"title": "标题已经修改"},
    {"published_at": "2026-09-19T04:29:00+00:00"},
    {"published_at": None},
    {"external_id": "other-video"},
    {"source_url": "https://www.douyin.com/video/7686727666610883904"},
])
def test_changed_current_metadata_does_not_reuse_old_transcript(tmp_path, changes):
    with ResearchStore(tmp_path / "state.db") as store:
        store.upsert_evidence(transcript(), now=NOW)
        assert store.latest_reusable_transcript(source(**changes)) is None


def test_latest_metadata_is_a_revision_barrier_even_when_old_title_returns(tmp_path):
    with ResearchStore(tmp_path / "state.db") as store:
        store.upsert_evidence(transcript(), now=NOW)
        store.upsert_evidence(source(title="后续观察到修改", summary="新的说明"), now=NOW + timedelta(minutes=1))
        assert store.latest_reusable_transcript(source()) is None
        assert store.latest_reusable_transcript(source(title="后续观察到修改", summary="新的说明")) is None


@pytest.mark.parametrize("key,value", [
    ("content_kind", "article"), ("completeness", "partial"),
    ("quality_issues", ["machine_transcription_unverified"]),
    ("segments", []),
    ("segments", [{"start_seconds": 0.0, "end_seconds": None, "text": "结束时间未知", "speaker": ""}]),
])
def test_newest_ineligible_evidence_does_not_fall_back_to_older_full_version(tmp_path, key, value):
    with ResearchStore(tmp_path / "state.db") as store:
        store.upsert_evidence(transcript(), now=NOW)
        newer = transcript()
        newer[key] = value
        # Ensure a genuinely distinct immutable content version is persisted.
        newer["content_text"] += " 这是较新的版本。"
        store.upsert_evidence(newer, now=NOW + timedelta(minutes=1))
        assert store.latest_reusable_transcript(source()) is None


def test_latest_full_local_version_wins_and_lookup_uses_existing_source_index(tmp_path):
    with ResearchStore(tmp_path / "state.db") as store:
        first = store.upsert_evidence(transcript(), now=NOW)
        second_item = transcript()
        second_item["content_text"] += " 新的语句。"
        second_item["segments"] = [{"start_seconds": 0.0, "end_seconds": 12.0,
                                    "text": second_item["content_text"], "speaker": ""}]
        second = store.upsert_evidence(second_item, now=NOW + timedelta(minutes=1))
        assert first.id != second.id
        assert store.latest_reusable_transcript(source()) == second
        plan = store._connection.execute(
            "EXPLAIN QUERY PLAN SELECT payload_json FROM research_evidence WHERE source_identity=? "
            "ORDER BY discovered_at DESC, rowid DESC LIMIT 1", (second.source_identity,),
        ).fetchall()
        assert any("USING INDEX research_evidence_source" in str(row[3]) for row in plan)
        cluster_plan = store._connection.execute(
            "EXPLAIN QUERY PLAN SELECT discovered_at FROM research_evidence WHERE source_cluster=?",
            (second.source_cluster,),
        ).fetchall()
        assert any("USING INDEX research_evidence_cluster" in str(row[3]) for row in cluster_plan)


def test_unknown_publication_stays_unknown_on_reuse(tmp_path):
    with ResearchStore(tmp_path / "state.db") as store:
        item = transcript()
        item["published_at"] = None
        first = store.upsert_evidence(item, now=NOW)
        cached = store.latest_reusable_transcript(source(published_at=None))
        assert cached is not None and cached.event_at is None and cached == first


def test_media_settings_defaults_are_disabled_and_persist_only_explicit_settings(tmp_path):
    with ResearchStore(tmp_path / "state.db") as store:
        settings = store.load_settings()
        assert settings["media_enabled"] is False
        assert settings["max_media_items"] == 2 and settings["max_media_seconds"] == 600
        store.save_settings({"media_enabled": True, "max_media_items": 3, "max_media_seconds": 180})
        saved = store.load_settings()
        assert saved["media_enabled"] is True and saved["max_media_items"] == 3 and saved["max_media_seconds"] == 180


@pytest.mark.parametrize("settings", [
    {"media_enabled": 1}, {"max_media_items": True}, {"max_media_items": 0}, {"max_media_items": 6},
    {"max_media_seconds": 59}, {"max_media_seconds": 601}, {"max_media_seconds": 180.0},
])
def test_media_settings_reject_wrong_types_or_unbounded_values(tmp_path, settings):
    with ResearchStore(tmp_path / "state.db") as store:
        with pytest.raises(ValueError):
            store.save_settings(settings)


def test_metadata_same_summary_new_title_is_an_immutable_revision_barrier(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        original = store.upsert_evidence(transcript(), now=NOW)
        first = store.upsert_evidence(source(), now=NOW + timedelta(minutes=1))
        revised = store.upsert_evidence(source(title='改过的标题'), now=NOW + timedelta(minutes=2))
        assert first.id != revised.id
        assert first.content_hash == revised.content_hash
        assert first.source_cluster == revised.source_cluster
        assert store.latest_reusable_transcript(source()) is None
        assert store.latest_reusable_transcript(source(title='改过的标题')) is None
        assert store.get_evidence(original.id).title == original.title


def test_source_head_follows_full_metadata_full_and_back_without_clock_refresh(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        full = store.upsert_evidence(transcript(), now=NOW)
        metadata = store.upsert_evidence(source(), now=NOW + timedelta(minutes=1))
        assert store.latest_reusable_transcript(source()) is None
        restored = store.upsert_evidence(transcript(), now=NOW + timedelta(days=1))
        assert restored.id == full.id
        assert restored.retrieved_at == full.retrieved_at == NOW
        assert restored.discovered_at == full.discovered_at == NOW
        assert restored.published_at == full.published_at
        assert store.latest_reusable_transcript(source()).id == full.id
        barrier_again = store.upsert_evidence(source(), now=NOW + timedelta(days=2))
        assert barrier_again.id == metadata.id
        assert barrier_again.retrieved_at == metadata.retrieved_at
        assert store.latest_reusable_transcript(source()) is None
        assert len(store.list_evidence()) == 2
    with ResearchStore(tmp_path / 'state.db') as reopened:
        assert reopened.latest_reusable_transcript(source()) is None
        reopened.upsert_evidence(transcript(), now=NOW + timedelta(days=3))
        cached = reopened.latest_reusable_transcript(source())
        assert cached.id == full.id and cached.retrieved_at == NOW


@pytest.mark.parametrize('change', [
    {'title': '修订标题'}, {'author': '修订作者'}, {'published_at': '2026-09-19T04:29:00Z'},
])
def test_header_versions_keep_pure_body_hashes_and_history(tmp_path, change):
    with ResearchStore(tmp_path / 'state.db') as store:
        original = store.upsert_evidence(transcript(), now=NOW)
        revised = store.upsert_evidence(transcript(**change), now=NOW + timedelta(minutes=1))
        assert revised.id != original.id
        assert revised.content_hash == original.content_hash
        assert revised.source_cluster == original.source_cluster
        assert store.latest_reusable_transcript(source()) is None
        assert store.latest_reusable_transcript(source(**change)).id == revised.id
        historical = store.get_evidence(original.id)
        assert historical.title == original.title and historical.author == original.author
        assert historical.published_at == original.published_at and historical.retrieved_at == NOW
        repost = store.upsert_evidence(transcript(source_url='https://example.org/authorized-repost',
            external_id='a-different-source', **change), now=NOW + timedelta(minutes=2))
        assert repost.source_identity != revised.source_identity
        assert repost.content_hash == revised.content_hash and repost.source_cluster == revised.source_cluster


def test_title_change_with_identical_transcription_records_only_once_per_header(tmp_path, monkeypatch):
    from research.video_research import VideoResearch

    calls = []
    def capture(page, path, **kwargs):
        calls.append('capture')
        path.write_bytes(b'complete source audio')
        return {'capture_complete': True, 'expected_duration_seconds': 10, 'start_seconds': 0}
    def transcribe(capture, item, **kwargs):
        calls.append('transcribe')
        return {'status': 'success', 'item': transcript(title=item['title'])}
    monkeypatch.setattr('research.video_research.capture_audio', capture)
    monkeypatch.setattr('research.video_research.transcribe_captured_media', transcribe)
    path = tmp_path / 'state.db'
    for title in ['原始标题', '修订标题', '修订标题']:
        worker = VideoResearch(path, command=['asr', '{input}', '{output_dir}'])
        value = source(title=title)
        worker(None, value)
    assert calls == ['capture', 'transcribe', 'capture', 'transcribe']
    assert worker.attempts == 0 and worker.cache_hits == 1
    with ResearchStore(path) as store:
        versions = store.list_evidence()
        assert len(versions) == 2 and {e.title for e in versions} == {'原始标题', '修订标题'}
        saved = store.latest_reusable_transcript(source(title='修订标题'))
        assert saved is not None
        old_clocks = (saved.discovered_at, saved.retrieved_at, saved.published_at)
        repeated = store.upsert_evidence(value, now=NOW + timedelta(days=100))
        assert (repeated.discovered_at, repeated.retrieved_at, repeated.published_at) == old_clocks


def test_legacy_database_without_heads_keeps_old_id_and_clocks_on_reuse(tmp_path):
    from research.models import digest, evidence_from_item

    path = tmp_path / 'legacy.db'
    old = evidence_from_item(transcript(), now=NOW)
    old = old.model_copy(update={'id': digest([old.source_identity, old.content_hash])})
    with ResearchStore(path) as store:
        with store._connection:
            store._connection.execute('INSERT INTO research_evidence VALUES (?, ?, ?, ?, ?, ?)',
                (old.id, old.source_identity, old.content_hash, old.source_cluster,
                 old.model_dump_json(), old.discovered_at.isoformat()))
            store._connection.execute('INSERT INTO research_reports VALUES (?, ?, ?, ?)',
                ('legacy-report', old.id, '{"evidence_id":"' + old.id + '"}', NOW.isoformat()))
            store._connection.execute('DROP TABLE research_source_heads')
    with ResearchStore(path) as store:
        import sqlite3

        assert store.backup_path is not None and store.backup_path.is_file()
        with sqlite3.connect(store.backup_path) as backup:
            assert backup.execute('SELECT id FROM research_evidence').fetchone()[0] == old.id
            assert backup.execute('SELECT evidence_id FROM research_reports').fetchone()[0] == old.id
            assert backup.execute("SELECT 1 FROM sqlite_master WHERE name='research_source_heads'").fetchone() is None
        assert store._connection.execute('SELECT evidence_id FROM research_reports').fetchone()[0] == old.id
        cached = store.latest_reusable_transcript(source())
        assert cached.id == old.id and cached.retrieved_at == NOW
        saved = store.upsert_evidence(cached.model_dump(mode='json'), now=NOW + timedelta(days=20))
        assert saved.id == old.id and saved.retrieved_at == NOW
        assert len(store.list_evidence()) == 1
        changed = store.upsert_evidence(transcript(title='新标题'), now=NOW + timedelta(days=21))
        assert changed.id != old.id
        assert store.latest_reusable_transcript(source(title='新标题')).id == changed.id
        store.upsert_evidence(transcript(), now=NOW + timedelta(days=22))
        assert store.latest_reusable_transcript(source()).id == old.id
        assert store.get_evidence(old.id).retrieved_at == NOW
    with ResearchStore(path) as reopened:
        assert reopened.backup_path is None
        assert reopened.get_evidence(old.id).retrieved_at == NOW
        assert reopened._connection.execute('SELECT evidence_id FROM research_reports').fetchone()[0] == old.id
    assert len(list(tmp_path.glob('legacy.db.pre-research-*.bak'))) == 1


def test_returning_header_points_to_old_immutable_version_not_newest_clock(tmp_path):
    with ResearchStore(tmp_path / 'state.db') as store:
        first = store.upsert_evidence(transcript(title='A'), now=NOW)
        second = store.upsert_evidence(transcript(title='B'), now=NOW + timedelta(minutes=1))
        store.upsert_evidence(transcript(title='A'), now=NOW + timedelta(days=1))
        assert store.latest_reusable_transcript(source(title='A')).id == first.id
        assert store.latest_reusable_transcript(source(title='B')) is None
        assert store.get_evidence(first.id).retrieved_at == NOW
        assert store.get_evidence(second.id).retrieved_at == NOW + timedelta(minutes=1)


def test_reanalyzing_old_record_does_not_override_new_metadata_barrier(tmp_path):
    from research.analysis import analyze_evidence

    with ResearchStore(tmp_path / 'state.db') as store:
        old = store.upsert_evidence(transcript(), now=NOW)
        metadata = store.upsert_evidence(source(), now=NOW + timedelta(minutes=1))
        store.upsert_evidence(old, now=NOW + timedelta(days=1))
        assert store.latest_reusable_transcript(source()) is None
        analyze_evidence(old, [], horizons=['long'], store=store, ai_enabled=False)
        assert store.latest_reusable_transcript(source()) is None
        head = store._connection.execute('SELECT evidence_id FROM research_source_heads WHERE source_identity=?',
            (old.source_identity,)).fetchone()
        assert head[0] == metadata.id


def test_first_import_of_an_evidence_record_establishes_head(tmp_path):
    from research.models import evidence_from_item

    with ResearchStore(tmp_path / 'state.db') as store:
        record = evidence_from_item(transcript(), now=NOW)
        store.upsert_evidence(record)
        assert store.latest_reusable_transcript(source()).id == record.id
        head = store._connection.execute('SELECT evidence_id FROM research_source_heads WHERE source_identity=?',
            (record.source_identity,)).fetchone()
        assert head[0] == record.id
