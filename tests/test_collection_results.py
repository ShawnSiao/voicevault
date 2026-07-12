from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from voicevault.collection_results import (
    MAX_CHECKPOINTS,
    MAX_CHECKPOINTS_BYTES,
    MAX_JSONL_LINE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_POSTS,
    MAX_POSTS_BYTES,
    MAX_SCREENSHOT_BYTES,
    MAX_TASK_BYTES,
    CollectionManifestInvalid,
    CollectionResultLoader,
    CollectionResultValidator,
    CollectionTargetSnapshot,
    CoverageUnproven,
    ResultSegmentTarget,
)
from voicevault.coverage import UtcInterval, serialize_utc
from voicevault.post_archive import post_content_sha256


UTC = timezone.utc
JOB_ID = "11111111-1111-4111-8111-111111111111"
SUBMISSION_ID = "22222222-2222-4222-8222-222222222222"
PERSON_ID = "33333333-3333-4333-8333-333333333333"
ACCOUNT_ID = "44444444-4444-4444-8444-444444444444"
SEGMENT_ID = "55555555-5555-4555-8555-555555555555"
EXTERNAL_USER_ID = "123456"
COLLECTOR_ID = "collector-a"
REQUEST_START = datetime(2026, 7, 1, tzinfo=UTC)
REQUEST_END = datetime(2026, 7, 3, tzinfo=UTC)
EXECUTION_START = datetime(2026, 7, 4, 0, 0, tzinfo=UTC)
EXECUTION_FINISH = datetime(2026, 7, 4, 0, 10, tzinfo=UTC)
HEARTBEAT_AT = datetime(2026, 7, 4, 0, 9, tzinfo=UTC)
NOW = datetime(2026, 7, 4, 0, 10, tzinfo=UTC)
SCREENSHOT_BYTES = b"\x89PNG\r\n\x1a\nvoicevault-proof"


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(records: list[dict[str, object]]) -> bytes:
    return b"".join(_json_bytes(record) for record in records)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ResultFixture:
    def __init__(self, root: Path, name: str) -> None:
        self.data_dir = root / name
        self.job_id = JOB_ID
        self.out_dir = self.data_dir / "jobs" / self.job_id / "out"
        self.posts_path = self.out_dir / "posts.jsonl"
        self.checkpoints_path = self.out_dir / "checkpoints.jsonl"
        self.manifest_path = self.out_dir / "manifest.json"
        self.start_checkpoint_id = "checkpoint-start"
        self.end_checkpoint_id = "checkpoint-end"
        self.screenshot_bytes = {
            "screenshots/start.png": SCREENSHOT_BYTES,
            "screenshots/end.png": SCREENSHOT_BYTES + b"-end",
        }
        self.posts = [
            self._post(
                "3004",
                REQUEST_END,
                checkpoint_ids=[self.start_checkpoint_id],
                captured_at=EXECUTION_START + timedelta(minutes=1),
            ),
            self._post(
                "3003",
                REQUEST_END - timedelta(hours=12),
                checkpoint_ids=[self.start_checkpoint_id],
                captured_at=EXECUTION_START + timedelta(minutes=1),
            ),
            self._post(
                "3002",
                REQUEST_START + timedelta(hours=12),
                checkpoint_ids=[self.start_checkpoint_id, self.end_checkpoint_id],
                captured_at=EXECUTION_START + timedelta(minutes=2),
            ),
            self._post(
                "3001",
                REQUEST_START,
                checkpoint_ids=[self.end_checkpoint_id],
                captured_at=EXECUTION_START + timedelta(minutes=2),
            ),
            self._post(
                "2999",
                REQUEST_START - timedelta(hours=1),
                checkpoint_ids=[self.end_checkpoint_id],
                captured_at=EXECUTION_START + timedelta(minutes=2),
            ),
        ]
        self.checkpoints = [
            {
                "checkpoint_id": self.start_checkpoint_id,
                "segment_id": SEGMENT_ID,
                "sequence": 0,
                "observed_at": serialize_utc(EXECUTION_START + timedelta(minutes=1)),
                "action_type": "initial_view",
                "triggered_remote_load": False,
                "remote_action_ordinal": None,
                "visible_post_ids": ["3004", "3003", "3002"],
                "earliest_non_pinned_at": serialize_utc(
                    REQUEST_START + timedelta(hours=12)
                ),
                "latest_non_pinned_at": serialize_utc(REQUEST_END),
                "anchor_post_id": None,
                "start_kind": "timeline_top",
                "completion_reason": None,
                "boundary_post_id": None,
                "reached_end": False,
                "evidence_keys": ["start-proof"],
            },
            {
                "checkpoint_id": self.end_checkpoint_id,
                "segment_id": SEGMENT_ID,
                "sequence": 1,
                "observed_at": serialize_utc(EXECUTION_START + timedelta(minutes=2)),
                "action_type": "scroll",
                "triggered_remote_load": True,
                "remote_action_ordinal": 1,
                "visible_post_ids": ["3002", "3001", "2999"],
                "earliest_non_pinned_at": serialize_utc(
                    REQUEST_START - timedelta(hours=1)
                ),
                "latest_non_pinned_at": serialize_utc(
                    REQUEST_START + timedelta(hours=12)
                ),
                "anchor_post_id": "3002",
                "start_kind": None,
                "completion_reason": "lower_bound_crossed",
                "boundary_post_id": "2999",
                "reached_end": False,
                "evidence_keys": ["end-proof"],
            },
        ]
        screenshots = [
            self._screenshot(
                evidence_key="start-proof",
                name="screenshots/start.png",
                purpose="segment_start",
            ),
            self._screenshot(
                evidence_key="end-proof",
                name="screenshots/end.png",
                purpose="segment_end",
            ),
        ]
        self.manifest: dict[str, object] = {
            "schema_version": 1,
            "submission_id": SUBMISSION_ID,
            "mode": "normal",
            "job": {
                "job_id": self.job_id,
                "handoff_version": 1,
                "collector_id": COLLECTOR_ID,
            },
            "target": {
                "person_id": PERSON_ID,
                "account_id": ACCOUNT_ID,
                "platform": "xueqiu",
                "external_user_id": EXTERNAL_USER_ID,
                "requested_interval": {
                    "start_at": serialize_utc(REQUEST_START),
                    "end_at": serialize_utc(REQUEST_END),
                },
            },
            "execution": {
                "started_at": serialize_utc(EXECUTION_START),
                "finished_at": serialize_utc(EXECUTION_FINISH),
                "last_heartbeat_at": serialize_utc(HEARTBEAT_AT),
                "remote_action_count": 1,
            },
            "outcome": {
                "kind": "complete",
                "completion_reason": "lower_bound_crossed",
                "stop_reason": None,
                "last_checkpoint_id": self.end_checkpoint_id,
            },
            "segments": [
                {
                    "segment_id": SEGMENT_ID,
                    "ordinal": 0,
                    "interval": {
                        "start_at": serialize_utc(REQUEST_START),
                        "end_at": serialize_utc(REQUEST_END),
                    },
                    "result": "complete",
                    "completion_reason": "lower_bound_crossed",
                    "first_checkpoint_id": self.start_checkpoint_id,
                    "last_checkpoint_id": self.end_checkpoint_id,
                    "checkpoint_count": 2,
                }
            ],
            "artifacts": {
                "posts": {
                    "name": "posts.jsonl",
                    "sha256": "0" * 64,
                    "bytes": 0,
                    "records": 0,
                },
                "checkpoints": {
                    "name": "checkpoints.jsonl",
                    "sha256": "0" * 64,
                    "bytes": 0,
                    "records": 0,
                },
                "screenshots": screenshots,
            },
        }
        self.snapshot = CollectionTargetSnapshot(
            job_id=self.job_id,
            handoff_version=1,
            collector_id=COLLECTOR_ID,
            person_id=PERSON_ID,
            account_id=ACCOUNT_ID,
            platform="xueqiu",
            external_user_id=EXTERNAL_USER_ID,
            mode="normal",
            requested_interval=UtcInterval(REQUEST_START, REQUEST_END),
            segments=(
                ResultSegmentTarget(
                    segment_id=SEGMENT_ID,
                    ordinal=0,
                    interval=UtcInterval(REQUEST_START, REQUEST_END),
                ),
            ),
            last_heartbeat_at=HEARTBEAT_AT,
            stored_remote_action_count=1,
            known_external_post_ids=frozenset({"2998", "2997"}),
            resume_checkpoint_id=None,
            now=NOW,
        )

    def _post(
        self,
        external_post_id: str,
        published_at: datetime,
        *,
        checkpoint_ids: list[str],
        captured_at: datetime,
        pinned: bool = False,
        status: str = "available",
        visible_text: str | None = None,
        body_capture: str | None = None,
    ) -> dict[str, object]:
        if visible_text is None and status == "available":
            visible_text = f"voice {external_post_id}"
        content_hash = (
            post_content_sha256(visible_text)
            if status == "available" and visible_text is not None
            else None
        )
        if body_capture is None:
            body_capture = "full" if status == "available" else "not_applicable"
        return {
            "record_id": f"record-{external_post_id}",
            "segment_id": SEGMENT_ID,
            "checkpoint_ids": checkpoint_ids,
            "external_post_id": external_post_id,
            "author_external_user_id": EXTERNAL_USER_ID,
            "author_display_name": "Alice",
            "published_at": serialize_utc(published_at),
            "captured_at": serialize_utc(captured_at),
            "source_url": f"https://xueqiu.com/{EXTERNAL_USER_ID}/{external_post_id}",
            "visible_text": visible_text,
            "body_capture": body_capture,
            "is_pinned": pinned,
            "observation_status": status,
            "content_sha256": content_hash,
            "evidence_keys": [],
        }

    def _screenshot(
        self, *, evidence_key: str, name: str, purpose: str
    ) -> dict[str, object]:
        data = self.screenshot_bytes[name]
        return {
            "evidence_key": evidence_key,
            "name": name,
            "sha256": _sha256(data),
            "bytes": len(data),
            "media_type": "image/png",
            "purpose": purpose,
            "segment_id": SEGMENT_ID,
        }

    @property
    def artifacts(self) -> dict[str, object]:
        return self.manifest["artifacts"]  # type: ignore[return-value]

    def rewrite_manifest(self) -> str:
        raw = _json_bytes(self.manifest)
        self.manifest_path.write_bytes(raw)
        return _sha256(raw)

    def write(
        self,
        *,
        skip_screenshots: frozenset[str] = frozenset(),
    ) -> str:
        screenshots_dir = self.out_dir / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        posts_raw = _jsonl_bytes(self.posts)
        checkpoints_raw = _jsonl_bytes(self.checkpoints)
        self.posts_path.write_bytes(posts_raw)
        self.checkpoints_path.write_bytes(checkpoints_raw)
        posts_meta = self.artifacts["posts"]  # type: ignore[index]
        posts_meta.update(  # type: ignore[union-attr]
            sha256=_sha256(posts_raw), bytes=len(posts_raw), records=len(self.posts)
        )
        checkpoints_meta = self.artifacts["checkpoints"]  # type: ignore[index]
        checkpoints_meta.update(  # type: ignore[union-attr]
            sha256=_sha256(checkpoints_raw),
            bytes=len(checkpoints_raw),
            records=len(self.checkpoints),
        )
        for screenshot in self.artifacts["screenshots"]:  # type: ignore[index,union-attr]
            name = screenshot["name"]
            data = self.screenshot_bytes[name]
            screenshot.update(sha256=_sha256(data), bytes=len(data))
            if name not in skip_screenshots:
                path = self.out_dir.joinpath(*name.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        return self.rewrite_manifest()

    def make_partial(self, stop_reason: str = "network_error") -> None:
        outcome = self.manifest["outcome"]
        outcome.update(  # type: ignore[union-attr]
            kind="partial", completion_reason=None, stop_reason=stop_reason
        )
        segment = self.manifest["segments"][0]  # type: ignore[index]
        segment.update(result="partial", completion_reason=None)
        self.checkpoints[-1].update(
            completion_reason=None,
            boundary_post_id=None,
            reached_end=False,
            evidence_keys=[],
        )
        self.artifacts["screenshots"] = [  # type: ignore[index]
            self.artifacts["screenshots"][0]  # type: ignore[index]
        ]


class CollectionResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.fixture_number = 0

    def fixture(self) -> ResultFixture:
        self.fixture_number += 1
        return ResultFixture(self.root, f"case-{self.fixture_number}")

    def load(self, fixture: ResultFixture, manifest_sha256: str):
        return CollectionResultLoader(fixture.data_dir).load(
            fixture.job_id, expected_manifest_sha256=manifest_sha256
        )

    def validate(self, fixture: ResultFixture):
        digest = fixture.write()
        staged = self.load(fixture, digest)
        return CollectionResultValidator().validate(staged, snapshot=fixture.snapshot)

    def assert_invalid(self, fixture: ResultFixture, error=CollectionManifestInvalid) -> None:
        digest = fixture.write()
        with self.assertRaises(error):
            staged = self.load(fixture, digest)
            CollectionResultValidator().validate(staged, snapshot=fixture.snapshot)

    @staticmethod
    def make_anchor_ineligible(fixture: ResultFixture, status: str) -> None:
        anchor = next(post for post in fixture.posts if post["external_post_id"] == "3002")
        if status == "pinned":
            anchor["is_pinned"] = True
        else:
            anchor.update(
                observation_status=status, visible_text=None, content_sha256=None
            )
            fixture.snapshot = replace(
                fixture.snapshot,
                known_external_post_ids=fixture.snapshot.known_external_post_ids | {"3002"},
            )
        fixture.checkpoints[0]["earliest_non_pinned_at"] = serialize_utc(
            REQUEST_END - timedelta(hours=12)
        )
        fixture.checkpoints[1]["latest_non_pinned_at"] = serialize_utc(REQUEST_START)

    def test_valid_lower_bound_result_is_immutable_and_half_open(self) -> None:
        fixture = self.fixture()
        before = {}
        digest = fixture.write()
        for path in fixture.out_dir.rglob("*"):
            if path.is_file():
                before[path.relative_to(fixture.out_dir).as_posix()] = path.read_bytes()

        staged = self.load(fixture, digest)
        result = CollectionResultValidator().validate(staged, snapshot=fixture.snapshot)

        self.assertEqual(result.submission_id, SUBMISSION_ID)
        self.assertEqual(result.manifest_sha256, digest)
        self.assertEqual(result.outcome_kind, "complete")
        self.assertEqual(result.completion_reason, "lower_bound_crossed")
        self.assertIsNone(result.stop_reason)
        self.assertEqual(result.remote_action_count, 1)
        self.assertEqual(
            [record.external_post_id for record in result.archive_records],
            ["3003", "3002", "3001"],
        )
        self.assertEqual(result.coverage_intervals, (UtcInterval(REQUEST_START, REQUEST_END),))
        self.assertEqual(len(result.checkpoints), 2)
        self.assertEqual(
            {artifact.evidence_key for artifact in result.artifacts},
            {
                "job-manifest",
                "job-posts",
                "job-checkpoints",
                "start-proof",
                "end-proof",
            },
        )
        self.assertNotIn(str(fixture.data_dir), repr(result.accepted_manifest))
        with self.assertRaises(FrozenInstanceError):
            result.outcome_kind = "partial"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            result.accepted_manifest["mode"] = "recheck"  # type: ignore[index]
        with self.assertRaises(TypeError):
            dict.__setitem__(result.accepted_manifest, "mode", "recheck")
        fixture.manifest["mode"] = "recheck"
        self.assertEqual(result.accepted_manifest["mode"], "normal")

        after = {}
        for path in fixture.out_dir.rglob("*"):
            if path.is_file():
                after[path.relative_to(fixture.out_dir).as_posix()] = path.read_bytes()
        self.assertEqual(after, before)

    def test_valid_end_of_history_result(self) -> None:
        fixture = self.fixture()
        fixture.manifest["outcome"].update(  # type: ignore[union-attr]
            completion_reason="end_of_history"
        )
        fixture.manifest["segments"][0].update(  # type: ignore[index]
            completion_reason="end_of_history"
        )
        fixture.checkpoints[-1].update(
            completion_reason="end_of_history",
            boundary_post_id=None,
            reached_end=True,
        )

        result = self.validate(fixture)

        self.assertEqual(result.completion_reason, "end_of_history")
        self.assertTrue(result.checkpoints[-1].reached_end)
        self.assertEqual(result.coverage_intervals, (UtcInterval(REQUEST_START, REQUEST_END),))

    def test_valid_partial_result_keeps_records_without_coverage_claim(self) -> None:
        fixture = self.fixture()
        fixture.make_partial()

        result = self.validate(fixture)

        self.assertEqual(result.outcome_kind, "partial")
        self.assertIsNone(result.completion_reason)
        self.assertEqual(result.stop_reason, "network_error")
        self.assertEqual(result.coverage_intervals, ())
        self.assertEqual(
            [record.external_post_id for record in result.archive_records],
            ["3003", "3002", "3001"],
        )

    def test_complete_recheck_validates_but_never_claims_coverage(self) -> None:
        fixture = self.fixture()
        fixture.manifest["mode"] = "recheck"
        fixture.snapshot = replace(fixture.snapshot, mode="recheck")

        result = self.validate(fixture)

        self.assertEqual(result.outcome_kind, "complete")
        self.assertEqual(result.coverage_intervals, ())

    def test_mode_is_required_controlled_and_matches_snapshot(self) -> None:
        cases = (
            ("missing", None, "delete"),
            ("unknown", "incremental", "set"),
            ("mismatch", "recheck", "set"),
        )
        for name, value, action in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                if action == "delete":
                    del fixture.manifest["mode"]
                else:
                    fixture.manifest["mode"] = value
                self.assert_invalid(fixture)

    def test_job_target_and_segments_must_match_authoritative_snapshot(self) -> None:
        other_job = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        other_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        cases = (
            ("job", lambda f: f.manifest["job"].update(job_id=other_job)),
            ("handoff", lambda f: f.manifest["job"].update(handoff_version=2)),
            ("collector", lambda f: f.manifest["job"].update(collector_id="other")),
            ("person", lambda f: f.manifest["target"].update(person_id=other_id)),
            ("account", lambda f: f.manifest["target"].update(account_id=other_id)),
            ("platform", lambda f: f.manifest["target"].update(platform="weibo")),
            ("uid", lambda f: f.manifest["target"].update(external_user_id="654321")),
            (
                "request interval",
                lambda f: f.manifest["target"]["requested_interval"].update(
                    start_at=serialize_utc(REQUEST_START + timedelta(hours=1))
                ),
            ),
            (
                "segment id",
                lambda f: f.manifest["segments"][0].update(segment_id=other_id),
            ),
            (
                "segment ordinal",
                lambda f: f.manifest["segments"][0].update(ordinal=1),
            ),
            (
                "segment interval",
                lambda f: f.manifest["segments"][0]["interval"].update(
                    end_at=serialize_utc(REQUEST_END - timedelta(hours=1))
                ),
            ),
            (
                "duplicate segment",
                lambda f: f.manifest["segments"].append(
                    dict(f.manifest["segments"][0])
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture)

    def test_available_posts_reject_collapsed_previews_and_require_capture_state(self) -> None:
        collapsed = self.fixture()
        collapsed.posts[0].update(
            visible_text="收盘总结，长篇正文预览…",
            body_capture="full",
            content_sha256=post_content_sha256("收盘总结，长篇正文预览…"),
        )
        self.assert_invalid(collapsed)

        declared_collapsed = self.fixture()
        declared_collapsed.posts[0]["body_capture"] = "collapsed"
        self.assert_invalid(declared_collapsed)

        missing = self.fixture()
        missing.posts[0].pop("body_capture")
        self.assert_invalid(missing)

    def test_uuid_fields_are_canonical_and_identity_keys_are_nonempty(self) -> None:
        cases = (
            (
                "submission uppercase",
                lambda f: f.manifest.update(
                    submission_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()
                ),
            ),
            ("submission braces", lambda f: f.manifest.update(submission_id="{" + SUBMISSION_ID + "}")),
            ("blank collector", lambda f: f.manifest["job"].update(collector_id=" ")),
            ("blank record", lambda f: f.posts[0].update(record_id="")),
            ("blank checkpoint", lambda f: f.checkpoints[0].update(checkpoint_id="")),
            (
                "blank evidence",
                lambda f: f.manifest["artifacts"]["screenshots"][0].update(
                    evidence_key=""
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture)

    def test_closed_json_contract_rejects_unknown_or_missing_keys(self) -> None:
        cases = (
            ("unknown top", lambda f: f.manifest.update(extra=True)),
            ("missing top", lambda f: f.manifest.pop("execution")),
            ("unknown job", lambda f: f.manifest["job"].update(extra=True)),
            ("unknown interval", lambda f: f.manifest["target"]["requested_interval"].update(extra=True)),
            ("unknown artifacts", lambda f: f.manifest["artifacts"].update(extra={})),
            ("unknown post", lambda f: f.posts[0].update(extra=True)),
            ("missing post", lambda f: f.posts[0].pop("source_url")),
            ("unknown checkpoint", lambda f: f.checkpoints[0].update(extra=True)),
            ("missing checkpoint", lambda f: f.checkpoints[0].pop("action_type")),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture)

    def test_checkpoint_sequence_manifest_ids_and_anchor_are_continuous(self) -> None:
        cases = (
            ("sequence gap", lambda f: f.checkpoints[1].update(sequence=2)),
            (
                "first id",
                lambda f: f.manifest["segments"][0].update(
                    first_checkpoint_id="wrong"
                ),
            ),
            (
                "last id",
                lambda f: f.manifest["segments"][0].update(last_checkpoint_id="wrong"),
            ),
            (
                "count",
                lambda f: f.manifest["segments"][0].update(checkpoint_count=3),
            ),
            ("anchor missing before", lambda f: f.checkpoints[1].update(anchor_post_id="3001")),
            ("anchor missing after", lambda f: f.checkpoints[1].update(anchor_post_id="3003")),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture, CoverageUnproven)

    def test_adjacent_anchor_requires_available_non_pinned_post(self) -> None:
        for status in ("pinned", "deleted", "unavailable"):
            with self.subTest(status=status):
                fixture = self.fixture()
                self.make_anchor_ineligible(fixture, status)
                self.assert_invalid(fixture, CoverageUnproven)

    def test_partial_result_still_rejects_ineligible_anchor(self) -> None:
        fixture = self.fixture()
        fixture.make_partial()
        self.make_anchor_ineligible(fixture, "pinned")

        self.assert_invalid(fixture, CoverageUnproven)

    def test_checkpoint_observed_times_are_monotonic_for_complete_and_partial(self) -> None:
        for outcome_kind in ("complete", "partial"):
            with self.subTest(outcome_kind=outcome_kind):
                fixture = self.fixture()
                if outcome_kind == "partial":
                    fixture.make_partial()
                fixture.checkpoints[1]["observed_at"] = serialize_utc(
                    EXECUTION_START + timedelta(seconds=30)
                )
                self.assert_invalid(fixture, CoverageUnproven)

    def test_equal_checkpoint_observed_times_are_allowed(self) -> None:
        fixture = self.fixture()
        fixture.checkpoints[1]["observed_at"] = fixture.checkpoints[0]["observed_at"]

        self.assertEqual(self.validate(fixture).outcome_kind, "complete")

    def test_checkpoint_ranges_are_recomputed_from_non_pinned_visible_posts(self) -> None:
        cases = (
            (
                "earliest",
                lambda f: f.checkpoints[1].update(
                    earliest_non_pinned_at=serialize_utc(REQUEST_START)
                ),
            ),
            (
                "latest",
                lambda f: f.checkpoints[0].update(
                    latest_non_pinned_at=serialize_utc(REQUEST_END - timedelta(hours=1))
                ),
            ),
            ("unexpected null", lambda f: f.checkpoints[0].update(earliest_non_pinned_at=None)),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture)

    def test_remote_actions_require_exact_global_ordinals_and_stored_floor(self) -> None:
        cases = (
            ("manifest count", lambda f: f.manifest["execution"].update(remote_action_count=0)),
            ("ordinal", lambda f: f.checkpoints[1].update(remote_action_ordinal=2)),
            ("false with ordinal", lambda f: f.checkpoints[0].update(remote_action_ordinal=1)),
            (
                "true without ordinal",
                lambda f: f.checkpoints[1].update(remote_action_ordinal=None),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture)

        fixture = self.fixture()
        fixture.snapshot = replace(fixture.snapshot, stored_remote_action_count=2)
        self.assert_invalid(fixture)

    def test_complete_requires_referenced_start_and_end_screenshots_per_segment(self) -> None:
        for purpose, checkpoint_index in (("segment_start", 0), ("segment_end", 1)):
            with self.subTest(purpose=purpose):
                fixture = self.fixture()
                screenshots = fixture.artifacts["screenshots"]
                fixture.artifacts["screenshots"] = [
                    item for item in screenshots if item["purpose"] != purpose
                ]
                fixture.checkpoints[checkpoint_index]["evidence_keys"] = []
                self.assert_invalid(fixture, CoverageUnproven)

    def test_pinned_post_cannot_prove_lower_bound(self) -> None:
        fixture = self.fixture()
        boundary = next(post for post in fixture.posts if post["external_post_id"] == "2999")
        boundary["is_pinned"] = True
        fixture.checkpoints[-1]["earliest_non_pinned_at"] = serialize_utc(REQUEST_START)

        self.assert_invalid(fixture, CoverageUnproven)

    def test_deleted_observation_cannot_prove_lower_bound(self) -> None:
        fixture = self.fixture()
        boundary = next(post for post in fixture.posts if post["external_post_id"] == "2999")
        boundary.update(
            observation_status="deleted", visible_text=None, content_sha256=None
        )
        fixture.snapshot = replace(
            fixture.snapshot,
            known_external_post_ids=fixture.snapshot.known_external_post_ids | {"2999"},
        )
        fixture.checkpoints[-1]["earliest_non_pinned_at"] = serialize_utc(REQUEST_START)

        self.assert_invalid(fixture, CoverageUnproven)

    def test_unavailable_observation_cannot_prove_lower_bound(self) -> None:
        fixture = self.fixture()
        boundary = next(post for post in fixture.posts if post["external_post_id"] == "2999")
        boundary.update(
            observation_status="unavailable", visible_text=None, content_sha256=None
        )
        fixture.snapshot = replace(
            fixture.snapshot,
            known_external_post_ids=fixture.snapshot.known_external_post_ids | {"2999"},
        )
        fixture.checkpoints[-1]["earliest_non_pinned_at"] = serialize_utc(REQUEST_START)

        self.assert_invalid(fixture, CoverageUnproven)

    def test_lower_bound_requires_first_earlier_non_pinned_post_and_matching_reasons(self) -> None:
        cases = (
            ("wrong boundary", lambda f: f.checkpoints[-1].update(boundary_post_id="3001")),
            (
                "checkpoint reason",
                lambda f: f.checkpoints[-1].update(completion_reason="end_of_history"),
            ),
            (
                "segment reason",
                lambda f: f.manifest["segments"][0].update(
                    completion_reason="end_of_history"
                ),
            ),
            (
                "outcome reason",
                lambda f: f.manifest["outcome"].update(
                    completion_reason="end_of_history"
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture, CoverageUnproven)

    def test_end_of_history_requires_reached_end_and_end_screenshot(self) -> None:
        fixture = self.fixture()
        fixture.manifest["outcome"].update(completion_reason="end_of_history")
        fixture.manifest["segments"][0].update(completion_reason="end_of_history")
        fixture.checkpoints[-1].update(
            completion_reason="end_of_history", boundary_post_id=None, reached_end=False
        )

        self.assert_invalid(fixture, CoverageUnproven)

    def test_start_kind_timeline_stable_entry_and_resume_contract(self) -> None:
        stable = self.fixture()
        stable.checkpoints[0]["start_kind"] = "stable_entry"
        self.assertEqual(self.validate(stable).outcome_kind, "complete")

        unstable = self.fixture()
        unstable.posts = [post for post in unstable.posts if post["external_post_id"] != "3004"]
        unstable.checkpoints[0]["visible_post_ids"] = ["3003", "3002"]
        unstable.checkpoints[0]["latest_non_pinned_at"] = serialize_utc(
            REQUEST_END - timedelta(hours=12)
        )
        unstable.checkpoints[0]["start_kind"] = "stable_entry"
        self.assert_invalid(unstable, CoverageUnproven)

        resumed = self.fixture()
        resumed.checkpoints[0]["start_kind"] = "resume_checkpoint"
        resumed.snapshot = replace(
            resumed.snapshot, resume_checkpoint_id=resumed.start_checkpoint_id
        )
        self.assertEqual(self.validate(resumed).outcome_kind, "complete")

        wrong_resume = self.fixture()
        wrong_resume.checkpoints[0]["start_kind"] = "resume_checkpoint"
        wrong_resume.snapshot = replace(wrong_resume.snapshot, resume_checkpoint_id="other")
        self.assert_invalid(wrong_resume, CoverageUnproven)

        unknown = self.fixture()
        unknown.checkpoints[0]["start_kind"] = "browser_guess"
        self.assert_invalid(unknown, CoverageUnproven)

    def test_available_posts_require_normalized_body_hash_author_and_canonical_url(self) -> None:
        cases = (
            ("empty body", lambda f: f.posts[1].update(visible_text="", content_sha256="0" * 64)),
            ("body hash", lambda f: f.posts[1].update(content_sha256="0" * 64)),
            ("author", lambda f: f.posts[1].update(author_external_user_id="654321")),
            ("post id", lambda f: f.posts[1].update(external_post_id="post-3003")),
            (
                "query URL",
                lambda f: f.posts[1].update(
                    source_url=f"https://xueqiu.com/{EXTERNAL_USER_ID}/3003?x=1"
                ),
            ),
            (
                "credential URL",
                lambda f: f.posts[1].update(
                    source_url=f"https://user@xueqiu.com/{EXTERNAL_USER_ID}/3003"
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture)

    def test_known_deleted_post_is_accepted_and_unknown_deleted_target_is_rejected(self) -> None:
        known = self.fixture()
        post = next(item for item in known.posts if item["external_post_id"] == "3003")
        post.update(
            observation_status="deleted", visible_text=None, content_sha256=None
        )
        known.snapshot = replace(
            known.snapshot,
            known_external_post_ids=known.snapshot.known_external_post_ids | {"3003"},
        )
        result = self.validate(known)
        record = next(item for item in result.archive_records if item.external_post_id == "3003")
        self.assertEqual(record.observation_status, "deleted")
        self.assertIsNone(record.content_text)

        unknown = self.fixture()
        post = next(item for item in unknown.posts if item["external_post_id"] == "3003")
        post.update(
            observation_status="unavailable", visible_text=None, content_sha256=None
        )
        self.assert_invalid(unknown)

    def test_status_only_records_require_null_body_and_hash(self) -> None:
        cases = (
            (
                "body",
                lambda f: f.posts[1].update(
                    observation_status="deleted", content_sha256=None
                ),
            ),
            (
                "hash",
                lambda f: f.posts[1].update(
                    observation_status="deleted", visible_text=None
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                fixture.snapshot = replace(
                    fixture.snapshot,
                    known_external_post_ids=fixture.snapshot.known_external_post_ids | {"3003"},
                )
                mutate(fixture)
                self.assert_invalid(fixture)

    def test_record_checkpoint_and_evidence_references_are_closed_and_same_segment(self) -> None:
        other_segment = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        cases = (
            (
                "unknown checkpoint",
                lambda f: f.posts[0]["checkpoint_ids"].append("unknown"),
            ),
            (
                "unknown visible post",
                lambda f: f.checkpoints[0]["visible_post_ids"].append("9999"),
            ),
            (
                "unknown record evidence",
                lambda f: f.posts[0]["evidence_keys"].append("unknown"),
            ),
            (
                "unknown checkpoint evidence",
                lambda f: f.checkpoints[0]["evidence_keys"].append("unknown"),
            ),
            (
                "evidence segment",
                lambda f: f.manifest["artifacts"]["screenshots"][0].update(
                    segment_id=other_segment
                ),
            ),
            (
                "duplicate record id",
                lambda f: f.posts[1].update(record_id=f.posts[0]["record_id"]),
            ),
            (
                "duplicate checkpoint id",
                lambda f: f.checkpoints[1].update(
                    checkpoint_id=f.checkpoints[0]["checkpoint_id"]
                ),
            ),
            (
                "duplicate evidence key",
                lambda f: f.manifest["artifacts"]["screenshots"][1].update(
                    evidence_key="start-proof"
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture)

    def test_unreferenced_screenshot_is_rejected(self) -> None:
        fixture = self.fixture()
        name = "screenshots/orphan.png"
        fixture.screenshot_bytes[name] = SCREENSHOT_BYTES + b"-orphan"
        fixture.artifacts["screenshots"].append(
            fixture._screenshot(
                evidence_key="orphan", name=name, purpose="checkpoint"
            )
        )

        self.assert_invalid(fixture)

    def test_times_are_aware_ordered_authoritative_and_not_too_far_future(self) -> None:
        cases = (
            ("naive execution", lambda f: f.manifest["execution"].update(started_at="2026-07-04T00:00:00")),
            (
                "future finish",
                lambda f: f.manifest["execution"].update(
                    finished_at=serialize_utc(NOW + timedelta(minutes=6))
                ),
            ),
            (
                "captured before",
                lambda f: f.posts[1].update(
                    captured_at=serialize_utc(EXECUTION_START - timedelta(seconds=1))
                ),
            ),
            (
                "checkpoint after",
                lambda f: f.checkpoints[1].update(
                    observed_at=serialize_utc(EXECUTION_FINISH + timedelta(seconds=1))
                ),
            ),
            (
                "published after captured",
                lambda f: f.posts[1].update(
                    published_at=serialize_utc(EXECUTION_FINISH + timedelta(days=1))
                ),
            ),
            (
                "heartbeat mismatch",
                lambda f: f.manifest["execution"].update(
                    last_heartbeat_at=serialize_utc(HEARTBEAT_AT - timedelta(seconds=1))
                ),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                mutate(fixture)
                self.assert_invalid(fixture)

        fixture = self.fixture()
        fixture.snapshot = replace(fixture.snapshot, last_heartbeat_at=None)
        self.assert_invalid(fixture)

    def test_null_heartbeat_is_valid_only_when_both_sides_are_null(self) -> None:
        fixture = self.fixture()
        fixture.manifest["execution"]["last_heartbeat_at"] = None
        fixture.snapshot = replace(fixture.snapshot, last_heartbeat_at=None)

        self.assertEqual(self.validate(fixture).outcome_kind, "complete")

    def test_accepted_manifest_canonicalizes_timezone_offsets(self) -> None:
        fixture = self.fixture()
        fixture.manifest["target"]["requested_interval"]["start_at"] = (
            "2026-07-01T08:00:00+08:00"
        )
        fixture.manifest["execution"]["started_at"] = "2026-07-04T08:00:00+08:00"

        result = self.validate(fixture)

        self.assertEqual(
            result.accepted_manifest["target"]["requested_interval"]["start_at"],
            "2026-07-01T00:00:00Z",
        )
        self.assertEqual(
            result.accepted_manifest["execution"]["started_at"],
            "2026-07-04T00:00:00Z",
        )

    def test_partial_contract_rejects_completion_claims_and_unknown_stop_reason(self) -> None:
        cases = (
            (
                "completion reason",
                lambda f: f.manifest["outcome"].update(
                    completion_reason="lower_bound_crossed"
                ),
            ),
            (
                "unknown stop",
                lambda f: f.manifest["outcome"].update(stop_reason="captcha_bypassed"),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                fixture.make_partial()
                mutate(fixture)
                self.assert_invalid(fixture)

    def test_complete_contract_rejects_partial_segment_or_stop_reason(self) -> None:
        fixture = self.fixture()
        fixture.manifest["segments"][0]["result"] = "partial"
        self.assert_invalid(fixture, CoverageUnproven)

        fixture = self.fixture()
        fixture.manifest["outcome"]["stop_reason"] = "network_error"
        self.assert_invalid(fixture, CoverageUnproven)

    def test_loader_requires_canonical_job_uuid_and_expected_digest(self) -> None:
        loader = CollectionResultLoader(self.root)
        invalid_job_ids = (
            "not-a-uuid",
            JOB_ID.upper(),
            "{" + JOB_ID + "}",
            "../" + JOB_ID,
            JOB_ID.replace("-", ""),
        )
        for job_id in invalid_job_ids:
            with self.subTest(job_id=job_id):
                with self.assertRaises(CollectionManifestInvalid):
                    loader.load(job_id, expected_manifest_sha256="a" * 64)

        fixture = self.fixture()
        digest = fixture.write()
        for expected in ("A" * 64, "short", "b" * 64):
            with self.subTest(expected=expected):
                with self.assertRaises(CollectionManifestInvalid):
                    self.load(fixture, expected)
        self.assertEqual(len(digest), 64)

    def test_duplicate_json_keys_non_objects_blank_lines_and_non_utf8_are_rejected(self) -> None:
        duplicate_manifest = self.fixture()
        duplicate_manifest.out_dir.mkdir(parents=True)
        raw_manifest = b'{"schema_version":1,"schema_version":1}\n'
        duplicate_manifest.manifest_path.write_bytes(raw_manifest)
        with self.assertRaises(CollectionManifestInvalid):
            self.load(duplicate_manifest, _sha256(raw_manifest))

        deep_manifest = self.fixture()
        (deep_manifest.out_dir / "screenshots").mkdir(parents=True)
        deep_raw = (b'{"nested":' * 1500) + b"null" + (b"}" * 1500)
        deep_manifest.manifest_path.write_bytes(deep_raw)
        with self.assertRaises(CollectionManifestInvalid):
            self.load(deep_manifest, _sha256(deep_raw))

        raw_cases = (
            ("duplicate key", b'{"record_id":"a","record_id":"b"}\n'),
            ("array", b"[]\n"),
            ("non finite", b'{"value":NaN}\n'),
            ("non utf8", b"\xff\n"),
            ("blank line", b"{}\n\n"),
        )
        for name, raw in raw_cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                fixture.write()
                fixture.posts_path.write_bytes(raw)
                fixture.artifacts["posts"].update(
                    sha256=_sha256(raw), bytes=len(raw), records=1
                )
                digest = fixture.rewrite_manifest()
                with self.assertRaises(CollectionManifestInvalid):
                    self.load(fixture, digest)

    def test_declared_hash_bytes_and_record_counts_are_recomputed(self) -> None:
        cases = (
            (
                "posts hash",
                lambda f: f.artifacts["posts"].update(sha256="a" * 64),
            ),
            (
                "posts bytes",
                lambda f: f.artifacts["posts"].update(
                    bytes=f.artifacts["posts"]["bytes"] + 1
                ),
            ),
            (
                "posts records",
                lambda f: f.artifacts["posts"].update(
                    records=f.artifacts["posts"]["records"] + 1
                ),
            ),
            (
                "checkpoint hash",
                lambda f: f.artifacts["checkpoints"].update(sha256="b" * 64),
            ),
            (
                "checkpoint bytes",
                lambda f: f.artifacts["checkpoints"].update(
                    bytes=f.artifacts["checkpoints"]["bytes"] + 1
                ),
            ),
            (
                "checkpoint records",
                lambda f: f.artifacts["checkpoints"].update(
                    records=f.artifacts["checkpoints"]["records"] + 1
                ),
            ),
            (
                "screenshot hash",
                lambda f: f.artifacts["screenshots"][0].update(sha256="c" * 64),
            ),
            (
                "screenshot bytes",
                lambda f: f.artifacts["screenshots"][0].update(
                    bytes=f.artifacts["screenshots"][0]["bytes"] + 1
                ),
            ),
            (
                "uppercase digest",
                lambda f: f.artifacts["screenshots"][0].update(sha256="A" * 64),
            ),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                fixture = self.fixture()
                fixture.write()
                mutate(fixture)
                digest = fixture.rewrite_manifest()
                with self.assertRaises(CollectionManifestInvalid):
                    self.load(fixture, digest)

    def test_artifact_names_are_canonical_posix_and_fixed_to_out_directory(self) -> None:
        invalid_names = (
            "screenshots\\proof.png",
            "screenshots//proof.png",
            "screenshots/./proof.png",
            "screenshots/../proof.png",
            "screenshots/proof.png/",
            "/screenshots/proof.png",
            "//server/share/proof.png",
            "C:/screenshots/proof.png",
            "screenshots/\x00/proof.png",
            "other/proof.png",
        )
        for name in invalid_names:
            with self.subTest(name=repr(name)):
                fixture = self.fixture()
                fixture.write()
                fixture.artifacts["screenshots"][0]["name"] = name
                digest = fixture.rewrite_manifest()
                with self.assertRaises(CollectionManifestInvalid):
                    self.load(fixture, digest)

        alias = self.fixture()
        alias_name = "screenshots/start.png."
        alias.artifacts["screenshots"][1]["name"] = alias_name
        alias.screenshot_bytes[alias_name] = SCREENSHOT_BYTES
        digest = alias.write()
        with self.assertRaises(CollectionManifestInvalid):
            self.load(alias, digest)

        for artifact, wrong_name in (("posts", "other.jsonl"), ("checkpoints", "other.jsonl")):
            with self.subTest(artifact=artifact):
                fixture = self.fixture()
                fixture.write()
                fixture.artifacts[artifact]["name"] = wrong_name
                digest = fixture.rewrite_manifest()
                with self.assertRaises(CollectionManifestInvalid):
                    self.load(fixture, digest)

    def test_symlink_or_reparse_artifact_is_rejected_when_platform_supports_it(self) -> None:
        fixture = self.fixture()
        link_name = "screenshots/link.png"
        fixture.artifacts["screenshots"][0]["name"] = link_name
        fixture.screenshot_bytes[link_name] = SCREENSHOT_BYTES
        digest = fixture.write(skip_screenshots=frozenset({link_name}))
        target = fixture.data_dir / "outside.png"
        target.write_bytes(SCREENSHOT_BYTES)
        link = fixture.out_dir / "screenshots" / "link.png"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink unavailable: {error}")

        with self.assertRaises(CollectionManifestInvalid):
            self.load(fixture, digest)

    def test_undeclared_files_are_rejected(self) -> None:
        fixture = self.fixture()
        digest = fixture.write()
        (fixture.out_dir / "extra.txt").write_text("unexpected", encoding="utf-8")

        with self.assertRaises(CollectionManifestInvalid):
            self.load(fixture, digest)

    def test_exact_resource_limit_constants_are_stable(self) -> None:
        self.assertEqual(MAX_MANIFEST_BYTES, 256 * 1024)
        self.assertEqual(MAX_POSTS_BYTES, 32 * 1024 * 1024)
        self.assertEqual(MAX_CHECKPOINTS_BYTES, 32 * 1024 * 1024)
        self.assertEqual(MAX_JSONL_LINE_BYTES, 1024 * 1024)
        self.assertEqual(MAX_SCREENSHOT_BYTES, 20 * 1024 * 1024)
        self.assertEqual(MAX_TASK_BYTES, 256 * 1024 * 1024)
        self.assertEqual(MAX_POSTS, 10_000)
        self.assertEqual(MAX_CHECKPOINTS, 5_000)

    def test_manifest_jsonl_line_screenshot_and_total_size_limits_are_enforced(self) -> None:
        fixture = self.fixture()
        fixture.out_dir.mkdir(parents=True)
        raw = b'{"padding":"' + (b"x" * MAX_MANIFEST_BYTES) + b'"}'
        fixture.manifest_path.write_bytes(raw)
        with self.assertRaises(CollectionManifestInvalid):
            self.load(fixture, _sha256(raw))

        for artifact, path, limit in (
            ("posts", "posts_path", MAX_POSTS_BYTES),
            ("checkpoints", "checkpoints_path", MAX_CHECKPOINTS_BYTES),
        ):
            with self.subTest(artifact=artifact):
                fixture = self.fixture()
                digest = fixture.write()
                with getattr(fixture, path).open("wb") as stream:
                    stream.truncate(limit + 1)
                with self.assertRaises(CollectionManifestInvalid):
                    self.load(fixture, digest)

        fixture = self.fixture()
        fixture.write()
        long_line = b'{"padding":"' + (b"x" * MAX_JSONL_LINE_BYTES) + b'"}\n'
        fixture.posts_path.write_bytes(long_line)
        fixture.artifacts["posts"].update(
            sha256=_sha256(long_line), bytes=len(long_line), records=1
        )
        digest = fixture.rewrite_manifest()
        with self.assertRaises(CollectionManifestInvalid):
            self.load(fixture, digest)

        fixture = self.fixture()
        digest = fixture.write()
        screenshot_path = fixture.out_dir / "screenshots" / "start.png"
        with screenshot_path.open("wb") as stream:
            stream.truncate(MAX_SCREENSHOT_BYTES + 1)
        with self.assertRaises(CollectionManifestInvalid):
            self.load(fixture, digest)

        fixture = self.fixture()
        digest = fixture.write()
        actual_total = sum(
            path.stat().st_size
            for path in fixture.out_dir.rglob("*")
            if path.is_file()
        )
        with patch("voicevault.collection_results.MAX_TASK_BYTES", actual_total - 1):
            with self.assertRaises(CollectionManifestInvalid):
                self.load(fixture, digest)

    def test_post_and_checkpoint_count_limits_are_enforced(self) -> None:
        fixture = self.fixture()
        fixture.posts = [{} for _ in range(MAX_POSTS + 1)]
        digest = fixture.write()
        with self.assertRaises(CollectionManifestInvalid):
            self.load(fixture, digest)

        fixture = self.fixture()
        fixture.checkpoints = [{} for _ in range(MAX_CHECKPOINTS + 1)]
        digest = fixture.write()
        with self.assertRaises(CollectionManifestInvalid):
            self.load(fixture, digest)


if __name__ == "__main__":
    unittest.main()
