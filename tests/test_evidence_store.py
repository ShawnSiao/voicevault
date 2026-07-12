from __future__ import annotations

import hashlib
import multiprocessing
import os
import re
import shutil
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from voicevault.collection_results import ValidatedArtifact
from voicevault.evidence_store import (
    EvidenceContentConflict,
    EvidenceSourceChanged,
    EvidenceStore,
    EvidenceStoreError,
    UnsupportedEvidenceMediaType,
)
import voicevault.evidence_store as evidence_store_module


MEDIA_EXTENSIONS = {
    "application/json": ".json",
    "application/x-ndjson": ".jsonl",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}
REPARSE_POINT = 0x400


class _StatOverlay:
    def __init__(self, original: os.stat_result, **overrides: int) -> None:
        self._original = original
        self._overrides = overrides

    def __getattr__(self, name: str) -> object:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._original, name)


def _multiprocess_preserve(
    data_dir: str,
    source_path: str,
    evidence_key: str,
    media_type: str,
    digest: str,
    byte_size: int,
    barrier: object,
    results: object,
) -> None:
    artifact = ValidatedArtifact(
        evidence_key=evidence_key,
        source_path=Path(source_path),
        sha256=digest,
        byte_size=byte_size,
        media_type=media_type,
        purpose="checkpoint",
        segment_id=None,
    )
    real_reject = evidence_store_module._reject_other_media_targets
    first_scan = True

    def synchronized_first_scan(path: Path, prepared: object) -> None:
        nonlocal first_scan
        if not first_scan:
            return
        first_scan = False
        real_reject(path, prepared)  # type: ignore[arg-type]
        barrier.wait(timeout=20)  # type: ignore[attr-defined]

    try:
        with patch(
            "voicevault.evidence_store._reject_other_media_targets",
            side_effect=synchronized_first_scan,
        ):
            stored = EvidenceStore(data_dir).preserve((artifact,))
    except Exception as error:
        results.put(  # type: ignore[attr-defined]
            ("error", media_type, type(error).__name__, str(error))
        )
    else:
        results.put(  # type: ignore[attr-defined]
            ("ok", media_type, stored[evidence_key].relative_path, "")
        )


class EvidenceStoreTests(unittest.TestCase):
    def root(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="voicevault-evidence-store-"))

    def artifact(
        self,
        root: Path,
        *,
        evidence_key: str = "proof",
        content: bytes = b"proof-bytes",
        media_type: str = "image/png",
        source_name: str = "proof-source.bin",
        declared_sha256: str | None = None,
        declared_size: int | None = None,
        source_path: Path | None = None,
    ) -> ValidatedArtifact:
        source = source_path or (root / "sources" / source_name)
        if source_path is None:
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)
        return ValidatedArtifact(
            evidence_key=evidence_key,
            source_path=source,
            sha256=declared_sha256 or hashlib.sha256(content).hexdigest(),
            byte_size=len(content) if declared_size is None else declared_size,
            media_type=media_type,
            purpose="checkpoint",
            segment_id=None,
        )

    def relative_path(self, artifact: ValidatedArtifact) -> str:
        return (
            f"evidence/sha256/{artifact.sha256[:2]}/{artifact.sha256}"
            f"{MEDIA_EXTENSIONS[artifact.media_type]}"
        )

    @staticmethod
    def same_path(left: object, right: Path) -> bool:
        try:
            left_path = os.path.normcase(os.path.abspath(os.fspath(left)))
        except (TypeError, ValueError):
            return False
        return left_path == os.path.normcase(os.path.abspath(os.fspath(right)))

    def assert_final_cas_layout(
        self,
        target: Path,
        *,
        expected_retained_temporaries: int | tuple[int, ...] | None = None,
        require_hard_link_temporaries: bool = False,
    ) -> None:
        entries = list(target.parent.iterdir())
        self.assertEqual([entry for entry in entries if entry.name == target.name], [target])
        digest = target.name[:64]
        extension = target.name[64:]
        media_type = next(
            media
            for media, candidate_extension in MEDIA_EXTENSIONS.items()
            if candidate_extension == extension
        )
        sidecar = target.parent / f"{digest}.meta"
        self.assertEqual(
            sidecar.read_bytes(),
            (
                f"voicevault-evidence-v1\n{media_type}\n{target.stat().st_size}\n"
            ).encode("ascii"),
        )
        target_temporary_pattern = re.compile(
            rf"\.{re.escape(target.name)}\.[0-9a-f]{{32}}\.tmp\Z"
        )
        sidecar_temporary_pattern = re.compile(
            rf"\.{re.escape(sidecar.name)}\.[0-9a-f]{{32}}\.tmp\Z"
        )
        target_temporaries = [
            entry
            for entry in entries
            if target_temporary_pattern.fullmatch(entry.name)
        ]
        sidecar_temporaries = [
            entry
            for entry in entries
            if sidecar_temporary_pattern.fullmatch(entry.name)
        ]
        temporaries = target_temporaries + sidecar_temporaries
        self.assertEqual(len(entries), 2 + len(temporaries))
        for temporary, final in (
            *((temporary, target) for temporary in target_temporaries),
            *((temporary, sidecar) for temporary in sidecar_temporaries),
        ):
            self.assertTrue(temporary.is_file())
            self.assertEqual(temporary.read_bytes(), final.read_bytes())
            if require_hard_link_temporaries:
                self.assertTrue(os.path.samefile(temporary, final))
        if expected_retained_temporaries is not None:
            if isinstance(expected_retained_temporaries, int):
                self.assertEqual(len(temporaries), expected_retained_temporaries)
            else:
                self.assertIn(len(temporaries), expected_retained_temporaries)

    def test_preserves_json_at_exact_content_addressed_path(self) -> None:
        root = self.root()
        data_dir = root / "data"
        source = root / "manifest.json"
        content = b'{"schema_version":1}\n'
        source.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        artifact = ValidatedArtifact(
            evidence_key="job-manifest",
            source_path=source,
            sha256=digest,
            byte_size=len(content),
            media_type="application/json",
            purpose="manifest",
            segment_id=None,
        )

        stored = EvidenceStore(data_dir).preserve((artifact,))

        expected_relative = f"evidence/sha256/{digest[:2]}/{digest}.json"
        self.assertEqual(stored["job-manifest"].relative_path, expected_relative)
        self.assertEqual((data_dir / expected_relative).read_bytes(), content)

    def test_four_media_types_use_exact_paths_and_leave_sources_unchanged(self) -> None:
        root = self.root()
        data_dir = root / "data"
        cases = (
            ("manifest", b'{"schema_version":1}\n', "application/json"),
            ("posts", b'{"record_id":"1"}\n', "application/x-ndjson"),
            ("screen-png", b"\x89PNG\r\n\x1a\nfixture", "image/png"),
            ("screen-jpeg", b"\xff\xd8\xfffixture\xff\xd9", "image/jpeg"),
        )
        artifacts = tuple(
            self.artifact(
                root,
                evidence_key=key,
                content=content,
                media_type=media_type,
                source_name=f"source-{index}",
            )
            for index, (key, content, media_type) in enumerate(cases)
        )
        source_state = {
            artifact.evidence_key: (
                artifact.source_path.read_bytes(),
                artifact.source_path.stat().st_mtime_ns,
            )
            for artifact in artifacts
        }

        stored = EvidenceStore(data_dir).preserve(artifacts)

        self.assertEqual(set(stored), {item[0] for item in cases})
        for artifact in artifacts:
            with self.subTest(media_type=artifact.media_type):
                expected_relative = self.relative_path(artifact)
                item = stored[artifact.evidence_key]
                self.assertEqual(item.evidence_key, artifact.evidence_key)
                self.assertEqual(item.sha256, artifact.sha256)
                self.assertEqual(item.media_type, artifact.media_type)
                self.assertEqual(item.byte_size, artifact.byte_size)
                self.assertEqual(item.relative_path, expected_relative)
                self.assertNotIn("\\", item.relative_path)
                self.assertNotIn(str(artifact.source_path), repr(item))
                self.assertEqual(
                    (data_dir / expected_relative).read_bytes(),
                    source_state[artifact.evidence_key][0],
                )
                self.assertTrue(artifact.source_path.is_file())
                self.assertEqual(
                    artifact.source_path.read_bytes(),
                    source_state[artifact.evidence_key][0],
                )
                self.assertEqual(
                    artifact.source_path.stat().st_mtime_ns,
                    source_state[artifact.evidence_key][1],
                )
        with self.assertRaises(TypeError):
            stored["extra"] = stored[artifacts[0].evidence_key]  # type: ignore[index]
        with self.assertRaises(FrozenInstanceError):
            stored[artifacts[0].evidence_key].relative_path = "changed"  # type: ignore[misc]

    def test_same_sha_is_shared_and_exact_replay_does_not_touch_target(self) -> None:
        root = self.root()
        data_dir = root / "data"
        content = b"shared-content"
        first = self.artifact(
            root,
            evidence_key="first",
            content=content,
            source_name="first.bin",
        )
        second = self.artifact(
            root,
            evidence_key="second",
            content=content,
            source_name="second.bin",
        )
        store = EvidenceStore(data_dir)

        stored = store.preserve((first, second))
        target = data_dir / self.relative_path(first)
        old_mtime = target.stat().st_mtime_ns
        replayed = store.preserve((first, second))

        self.assertEqual(
            stored["first"].relative_path, stored["second"].relative_path
        )
        self.assertEqual(
            replayed["first"].relative_path, stored["first"].relative_path
        )
        self.assertEqual(target.stat().st_mtime_ns, old_mtime)
        self.assert_final_cas_layout(
            target,
            expected_retained_temporaries=0 if os.name == "nt" else None,
        )

    def test_changed_source_content_or_size_is_rejected_before_final_publish(self) -> None:
        for name, replacement in (
            ("content", b"modified-byte"),
            ("size", b"changed-size-longer"),
        ):
            with self.subTest(name=name):
                root = self.root()
                data_dir = root / "data"
                artifact = self.artifact(root, content=b"original-byte")
                artifact.source_path.write_bytes(replacement)
                target = data_dir / self.relative_path(artifact)

                with self.assertRaises(EvidenceSourceChanged):
                    EvidenceStore(data_dir).preserve((artifact,))

                self.assertFalse(target.exists())
                self.assertEqual(artifact.source_path.read_bytes(), replacement)

    def test_source_lstat_and_open_identity_must_match(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root)
        target = data_dir / self.relative_path(artifact)
        real_lstat = evidence_store_module._TrustedDirectory.lstat

        def changed_identity(
            directory: object, name: str, *args: object, **kwargs: object
        ) -> object:
            info = real_lstat(directory, name, *args, **kwargs)
            if (
                getattr(directory, "path", None) == artifact.source_path.parent
                and name == artifact.source_path.name
            ):
                return _StatOverlay(info, st_ino=info.st_ino + 1)
            return info

        with patch.object(
            evidence_store_module._TrustedDirectory,
            "lstat",
            new=changed_identity,
        ):
            with self.assertRaises(EvidenceSourceChanged):
                EvidenceStore(data_dir).preserve((artifact,))

        self.assertFalse(target.exists())
        self.assertTrue(artifact.source_path.is_file())

    def test_source_symlink_and_reparse_point_are_rejected(self) -> None:
        root = self.root()
        real_source = root / "real-source.png"
        real_source.write_bytes(b"source-content")
        source_link = root / "source-link.png"
        try:
            os.symlink(real_source, source_link)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink unavailable: {error}")
        artifact = self.artifact(
            root,
            content=real_source.read_bytes(),
            source_path=source_link,
        )
        data_dir = root / "data"

        with self.assertRaises(EvidenceSourceChanged):
            EvidenceStore(data_dir).preserve((artifact,))

        self.assertFalse((data_dir / self.relative_path(artifact)).exists())
        self.assertTrue(source_link.is_symlink())
        self.assertTrue(real_source.is_file())

    def test_source_reparse_attribute_is_rejected(self) -> None:
        if os.name != "nt":
            self.skipTest("reparse-point attributes are specific to Windows")
        root = self.root()
        artifact = self.artifact(root)
        data_dir = root / "data"
        real_lstat = os.lstat

        def source_reparse(path: object, *args: object, **kwargs: object) -> object:
            info = real_lstat(path, *args, **kwargs)
            if self.same_path(path, artifact.source_path):
                attributes = getattr(info, "st_file_attributes", 0) | REPARSE_POINT
                return _StatOverlay(info, st_file_attributes=attributes)
            return info

        with patch("voicevault.evidence_store.os.lstat", side_effect=source_reparse):
            with self.assertRaises(EvidenceSourceChanged):
                EvidenceStore(data_dir).preserve((artifact,))

        self.assertFalse((data_dir / self.relative_path(artifact)).exists())

    def test_existing_valid_target_is_reused_without_overwrite_or_mtime_change(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"already-stored")
        target = data_dir / self.relative_path(artifact)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"already-stored")
        fixed_ns = target.stat().st_mtime_ns - 5_000_000_000
        os.utime(target, ns=(fixed_ns, fixed_ns))

        stored = EvidenceStore(data_dir).preserve((artifact,))

        self.assertEqual(stored[artifact.evidence_key].relative_path, self.relative_path(artifact))
        self.assertEqual(target.read_bytes(), b"already-stored")
        self.assertEqual(target.stat().st_mtime_ns, fixed_ns)

    def test_existing_wrong_target_is_rejected_and_never_overwritten(self) -> None:
        cases = (b"wrong-content", b"wrong-size-much-longer")
        for existing in cases:
            with self.subTest(existing=existing):
                root = self.root()
                data_dir = root / "data"
                artifact = self.artifact(root, content=b"expected-data")
                target = data_dir / self.relative_path(artifact)
                target.parent.mkdir(parents=True)
                target.write_bytes(existing)
                old_mtime = target.stat().st_mtime_ns

                with self.assertRaises(EvidenceContentConflict):
                    EvidenceStore(data_dir).preserve((artifact,))

                self.assertEqual(target.read_bytes(), existing)
                self.assertEqual(target.stat().st_mtime_ns, old_mtime)
                self.assertTrue(artifact.source_path.is_file())

    def test_existing_target_changed_after_source_check_is_rejected(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"original-bytes")
        target = data_dir / self.relative_path(artifact)
        store = EvidenceStore(data_dir)
        store.preserve((artifact,))
        replacement = b"replaced-bytes"
        real_verify_source = evidence_store_module._verify_source
        tampered = False

        def verify_then_tamper(
            prepared: object, destination: object = None
        ) -> None:
            nonlocal tampered
            real_verify_source(prepared, destination)  # type: ignore[arg-type]
            target.write_bytes(replacement)
            tampered = True

        with patch(
            "voicevault.evidence_store._verify_source",
            side_effect=verify_then_tamper,
        ):
            with self.assertRaises(EvidenceContentConflict):
                store.preserve((artifact,))

        self.assertTrue(tampered)
        self.assertEqual(target.read_bytes(), replacement)
        self.assertEqual(artifact.source_path.read_bytes(), b"original-bytes")

    def test_same_sha_with_different_media_or_size_is_a_conflict(self) -> None:
        for name, mutate in (
            ("media", lambda artifact: replace(artifact, media_type="image/jpeg")),
            ("size", lambda artifact: replace(artifact, byte_size=artifact.byte_size + 1)),
        ):
            with self.subTest(name=name):
                root = self.root()
                data_dir = root / "data"
                first = self.artifact(root, evidence_key="first")
                second = mutate(replace(first, evidence_key="second"))

                with self.assertRaises(EvidenceContentConflict):
                    EvidenceStore(data_dir).preserve((first, second))

                self.assertFalse((data_dir / self.relative_path(first)).exists())

    def test_existing_sha_under_another_media_extension_is_a_conflict(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(
            root,
            content=b"same-digest",
            media_type="application/json",
        )
        store = EvidenceStore(data_dir)
        store.preserve((artifact,))
        existing = data_dir / self.relative_path(artifact)
        png_artifact = replace(
            artifact,
            evidence_key="png-proof",
            media_type="image/png",
        )

        with self.assertRaises(EvidenceContentConflict):
            store.preserve((png_artifact,))

        self.assertTrue(existing.is_file())
        self.assertFalse((data_dir / self.relative_path(png_artifact)).exists())

    def test_legacy_sibling_without_metadata_is_not_poisoned_by_conflict(self) -> None:
        root = self.root()
        data_dir = root / "data"
        content = b"legacy-sibling-content"
        png_artifact = self.artifact(
            root,
            content=content,
            media_type="image/png",
        )
        shard = data_dir / "evidence" / "sha256" / png_artifact.sha256[:2]
        shard.mkdir(parents=True)
        legacy_jpeg = shard / f"{png_artifact.sha256}.jpg"
        legacy_jpeg.write_bytes(content)
        fixed_ns = legacy_jpeg.stat().st_mtime_ns - 5_000_000_000
        os.utime(legacy_jpeg, ns=(fixed_ns, fixed_ns))
        sidecar = shard / f"{png_artifact.sha256}.meta"

        with self.assertRaises(EvidenceContentConflict):
            EvidenceStore(data_dir).preserve((png_artifact,))

        self.assertEqual(legacy_jpeg.read_bytes(), content)
        self.assertEqual(legacy_jpeg.stat().st_mtime_ns, fixed_ns)
        self.assertFalse(sidecar.exists())
        self.assertFalse((data_dir / self.relative_path(png_artifact)).exists())

    def test_processes_cannot_publish_different_media_for_one_digest(self) -> None:
        root = self.root()
        data_dir = root / "data"
        content = b"cross-process-content"
        digest = hashlib.sha256(content).hexdigest()
        sources = []
        for name in ("png-source.bin", "jpeg-source.bin"):
            source = root / name
            source.write_bytes(content)
            sources.append(source)
        shard = data_dir / "evidence" / "sha256" / digest[:2]
        shard.mkdir(parents=True)
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_multiprocess_preserve,
                args=(
                    str(data_dir),
                    str(source),
                    f"proof-{index}",
                    media_type,
                    digest,
                    len(content),
                    barrier,
                    results,
                ),
            )
            for index, (source, media_type) in enumerate(
                zip(sources, ("image/png", "image/jpeg"), strict=True)
            )
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)

        self.assertEqual([process.exitcode for process in processes], [0, 0])
        outcomes = [results.get(timeout=10) for _ in processes]
        successes = [outcome for outcome in outcomes if outcome[0] == "ok"]
        conflicts = [outcome for outcome in outcomes if outcome[2] == "EvidenceContentConflict"]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual(len(conflicts), 1, outcomes)
        winning_media = successes[0][1]
        expected_extension = MEDIA_EXTENSIONS[winning_media]
        final_targets = [
            path
            for extension in (".png", ".jpg")
            if (path := shard / f"{digest}{extension}").is_file()
        ]
        self.assertEqual(
            final_targets,
            [shard / f"{digest}{expected_extension}"],
        )
        sidecar = shard / f"{digest}.meta"
        self.assertEqual(
            sidecar.read_bytes(),
            (
                f"voicevault-evidence-v1\n{winning_media}\n{len(content)}\n"
            ).encode("ascii"),
        )

    def test_processes_with_same_digest_metadata_both_succeed(self) -> None:
        root = self.root()
        data_dir = root / "data"
        content = b"same-process-metadata"
        digest = hashlib.sha256(content).hexdigest()
        sources = []
        for name in ("first-source.bin", "second-source.bin"):
            source = root / name
            source.write_bytes(content)
            sources.append(source)
        shard = data_dir / "evidence" / "sha256" / digest[:2]
        shard.mkdir(parents=True)
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_multiprocess_preserve,
                args=(
                    str(data_dir),
                    str(source),
                    f"proof-{index}",
                    "image/png",
                    digest,
                    len(content),
                    barrier,
                    results,
                ),
            )
            for index, source in enumerate(sources)
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=30)

        self.assertEqual([process.exitcode for process in processes], [0, 0])
        outcomes = [results.get(timeout=10) for _ in processes]
        self.assertEqual([outcome[0] for outcome in outcomes], ["ok", "ok"])
        relative_path = f"evidence/sha256/{digest[:2]}/{digest}.png"
        self.assertEqual({outcome[2] for outcome in outcomes}, {relative_path})
        target = data_dir / relative_path
        self.assertEqual(target.read_bytes(), content)
        self.assert_final_cas_layout(
            target,
            expected_retained_temporaries=(0, 1, 2) if os.name == "nt" else None,
        )

    def test_existing_metadata_without_data_can_be_completed(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"complete-missing-data")
        shard = data_dir / "evidence" / "sha256" / artifact.sha256[:2]
        shard.mkdir(parents=True)
        sidecar = shard / f"{artifact.sha256}.meta"
        sidecar_bytes = (
            f"voicevault-evidence-v1\n{artifact.media_type}\n{artifact.byte_size}\n"
        ).encode("ascii")
        sidecar.write_bytes(sidecar_bytes)
        fixed_ns = sidecar.stat().st_mtime_ns - 5_000_000_000
        os.utime(sidecar, ns=(fixed_ns, fixed_ns))

        stored = EvidenceStore(data_dir).preserve((artifact,))

        target = data_dir / stored[artifact.evidence_key].relative_path
        self.assertEqual(target.read_bytes(), b"complete-missing-data")
        self.assertEqual(sidecar.read_bytes(), sidecar_bytes)
        self.assertEqual(sidecar.stat().st_mtime_ns, fixed_ns)
        self.assert_final_cas_layout(
            target,
            expected_retained_temporaries=0 if os.name == "nt" else None,
        )

    def test_canonical_metadata_changed_after_claim_is_rejected(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"canonical-metadata")
        store = EvidenceStore(data_dir)
        stored = store.preserve((artifact,))
        target = data_dir / stored[artifact.evidence_key].relative_path
        sidecar = target.parent / f"{artifact.sha256}.meta"
        expected = sidecar.read_bytes()
        replacement = bytes([expected[0] ^ 1]) + expected[1:]
        real_claim = evidence_store_module._claim_digest_metadata
        tampered = False

        def claim_then_tamper(directory: object, prepared: object) -> None:
            nonlocal tampered
            real_claim(directory, prepared)  # type: ignore[arg-type]
            sidecar.write_bytes(replacement)
            tampered = True

        with patch(
            "voicevault.evidence_store._claim_digest_metadata",
            side_effect=claim_then_tamper,
        ):
            with self.assertRaises(EvidenceContentConflict):
                store.preserve((artifact,))

        self.assertTrue(tampered)
        self.assertEqual(target.read_bytes(), b"canonical-metadata")
        self.assertEqual(sidecar.read_bytes(), replacement)

    def test_source_changed_after_metadata_claim_is_rejected(self) -> None:
        for existing_target in (False, True):
            with self.subTest(existing_target=existing_target):
                root = self.root()
                data_dir = root / "data"
                original = b"source-before-claim"
                replacement = b"source-after--claim"
                artifact = self.artifact(root, content=original)
                store = EvidenceStore(data_dir)
                if existing_target:
                    store.preserve((artifact,))
                real_claim = evidence_store_module._claim_digest_metadata
                tampered = False

                def claim_then_tamper(directory: object, prepared: object) -> None:
                    nonlocal tampered
                    real_claim(directory, prepared)  # type: ignore[arg-type]
                    artifact.source_path.write_bytes(replacement)
                    tampered = True

                with patch(
                    "voicevault.evidence_store._claim_digest_metadata",
                    side_effect=claim_then_tamper,
                ):
                    with self.assertRaises(EvidenceSourceChanged):
                        store.preserve((artifact,))

                self.assertTrue(tampered)
                self.assertEqual(len(replacement), len(original))
                self.assertEqual(artifact.source_path.read_bytes(), replacement)

    def test_metadata_verification_rejects_signature_change_during_read(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"metadata-signature")
        stored = EvidenceStore(data_dir).preserve((artifact,))
        target = data_dir / stored[artifact.evidence_key].relative_path
        sidecar_name = f"{artifact.sha256}.meta"
        prepared = evidence_store_module._prepare_artifacts((artifact,))[0]
        real_open_read = evidence_store_module._TrustedDirectory.open_read
        real_fstat = os.fstat
        metadata_fd: int | None = None
        metadata_fstat_calls = 0

        def tracking_open_read(
            directory: object,
            name: str,
            error_type: object = None,
            message: str | None = None,
        ) -> object:
            nonlocal metadata_fd
            opened = real_open_read(
                directory,
                name,
                error_type,
                message,
            )  # type: ignore[arg-type]
            if name == sidecar_name:
                metadata_fd = opened.fileno()
            return opened

        def changed_fstat(descriptor: int) -> object:
            nonlocal metadata_fstat_calls
            info = real_fstat(descriptor)
            if descriptor == metadata_fd:
                metadata_fstat_calls += 1
                if metadata_fstat_calls == 2:
                    return _StatOverlay(
                        info,
                        st_mtime_ns=info.st_mtime_ns + 1,
                    )
            return info

        with evidence_store_module._TrustedDirectory(
            target.parent,
            EvidenceStoreError,
            "Directory unavailable.",
        ) as directory:
            with (
                patch.object(
                    evidence_store_module._TrustedDirectory,
                    "open_read",
                    autospec=True,
                    side_effect=tracking_open_read,
                ),
                patch(
                    "voicevault.evidence_store.os.fstat",
                    side_effect=changed_fstat,
                ),
            ):
                with self.assertRaises(EvidenceContentConflict):
                    evidence_store_module._verify_digest_metadata(
                        directory,
                        sidecar_name,
                        prepared,
                    )

        self.assertEqual(metadata_fstat_calls, 2)

    def test_conflicting_metadata_or_target_is_never_modified(self) -> None:
        for name in ("metadata", "target"):
            with self.subTest(name=name):
                root = self.root()
                data_dir = root / "data"
                content = b"immutable-conflict"
                artifact = self.artifact(root, content=content)
                shard = data_dir / "evidence" / "sha256" / artifact.sha256[:2]
                shard.mkdir(parents=True)
                sidecar = shard / f"{artifact.sha256}.meta"
                expected_sidecar = (
                    f"voicevault-evidence-v1\n{artifact.media_type}\n{artifact.byte_size}\n"
                ).encode("ascii")
                target = data_dir / self.relative_path(artifact)
                if name == "metadata":
                    sidecar.write_bytes(b"conflicting-metadata")
                else:
                    sidecar.write_bytes(expected_sidecar)
                    target.write_bytes(bytes([content[0] ^ 1]) + content[1:])
                sidecar_before = (sidecar.read_bytes(), sidecar.stat().st_mtime_ns)
                target_before = (
                    (target.read_bytes(), target.stat().st_mtime_ns)
                    if target.exists()
                    else None
                )

                with self.assertRaises(EvidenceContentConflict):
                    EvidenceStore(data_dir).preserve((artifact,))

                self.assertEqual(
                    (sidecar.read_bytes(), sidecar.stat().st_mtime_ns),
                    sidecar_before,
                )
                if target_before is None:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(
                        (target.read_bytes(), target.stat().st_mtime_ns),
                        target_before,
                    )

    def test_source_parent_swap_cannot_redirect_the_opened_source(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"same-content-inside-and-outside")
        source_parent = artifact.source_path.parent
        original_parent = source_parent.with_name("sources-original")
        outside = root / "outside-source"
        outside.mkdir()
        outside_source = outside / artifact.source_path.name
        outside_source.write_bytes(artifact.source_path.read_bytes())
        target = data_dir / self.relative_path(artifact)
        real_check = evidence_store_module._require_safe_source_parent
        attack = {"attempted": False, "swapped": False}

        def check_then_swap(path: Path) -> None:
            real_check(path)
            if attack["attempted"]:
                return
            attack["attempted"] = True
            try:
                os.rename(source_parent, original_parent)
                os.symlink(outside, source_parent, target_is_directory=True)
            except OSError:
                return
            attack["swapped"] = True

        error: EvidenceStoreError | None = None
        with patch(
            "voicevault.evidence_store._require_safe_source_parent",
            side_effect=check_then_swap,
        ):
            try:
                EvidenceStore(data_dir).preserve((artifact,))
            except EvidenceStoreError as caught:
                error = caught

        self.assertTrue(attack["attempted"])
        if attack["swapped"]:
            self.assertIsInstance(error, EvidenceSourceChanged)
            self.assertFalse(target.exists())
            self.assertTrue((original_parent / artifact.source_path.name).is_file())
        else:
            self.assertIsNone(error)
            self.assertEqual(target.read_bytes(), b"same-content-inside-and-outside")
        self.assertEqual(outside_source.read_bytes(), b"same-content-inside-and-outside")

    def test_target_parent_swap_cannot_redirect_temporary_or_publish(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"target-parent-race")
        shard = data_dir / "evidence" / "sha256" / artifact.sha256[:2]
        shard.mkdir(parents=True)
        original_shard = shard.with_name(shard.name + "-original")
        outside = root / "outside-target"
        outside.mkdir()
        real_reject = evidence_store_module._reject_other_media_targets
        attack = {"attempted": False, "swapped": False}

        def check_then_swap(path: Path, prepared: object) -> None:
            real_reject(path, prepared)  # type: ignore[arg-type]
            if attack["attempted"]:
                return
            attack["attempted"] = True
            try:
                os.rename(shard, original_shard)
                os.symlink(outside, shard, target_is_directory=True)
            except OSError:
                return
            attack["swapped"] = True

        error: EvidenceStoreError | None = None
        with patch(
            "voicevault.evidence_store._reject_other_media_targets",
            side_effect=check_then_swap,
        ):
            try:
                EvidenceStore(data_dir).preserve((artifact,))
            except EvidenceStoreError as caught:
                error = caught

        self.assertTrue(attack["attempted"])
        self.assertEqual(list(outside.iterdir()), [])
        if attack["swapped"]:
            self.assertIsNotNone(error)
            self.assertFalse((outside / f"{artifact.sha256}.png").exists())
        else:
            self.assertIsNone(error)
            self.assertEqual(
                (data_dir / self.relative_path(artifact)).read_bytes(),
                b"target-parent-race",
            )

    def test_directory_creation_parent_swap_cannot_write_outside(self) -> None:
        root = self.root()
        data_dir = root / "data"
        evidence_dir = data_dir / "evidence"
        evidence_dir.mkdir(parents=True)
        original_evidence = data_dir / "evidence-original"
        outside = root / "outside-create"
        outside.mkdir()
        artifact = self.artifact(root, content=b"directory-create-race")
        real_mkdir = os.mkdir
        real_dir_fd_support = evidence_store_module.os.supports_dir_fd
        attack = {"attempted": False, "swapped": False}

        def swap_before_mkdir(path: object, *args: object, **kwargs: object) -> None:
            candidate = Path(os.fspath(path))
            if candidate.name == "sha256" and not attack["attempted"]:
                attack["attempted"] = True
                try:
                    os.rename(evidence_dir, original_evidence)
                    os.symlink(outside, evidence_dir, target_is_directory=True)
                except OSError:
                    pass
                else:
                    attack["swapped"] = True
            real_mkdir(path, *args, **kwargs)

        error: EvidenceStoreError | None = None
        with patch(
            "voicevault.evidence_store.os.mkdir",
            side_effect=swap_before_mkdir,
        ) as mocked_mkdir:
            with patch.object(
                evidence_store_module.os,
                "supports_dir_fd",
                set(real_dir_fd_support) | {mocked_mkdir},
            ):
                try:
                    EvidenceStore(data_dir).preserve((artifact,))
                except EvidenceStoreError as caught:
                    error = caught

        self.assertTrue(attack["attempted"])
        self.assertEqual(list(outside.iterdir()), [])
        if attack["swapped"]:
            self.assertIsNotNone(error)
        else:
            self.assertIsNone(error)
            self.assertEqual(
                (data_dir / self.relative_path(artifact)).read_bytes(),
                b"directory-create-race",
            )

    def test_evidence_symlink_does_not_escape_data_directory(self) -> None:
        root = self.root()
        data_dir = root / "data"
        outside = root / "outside"
        data_dir.mkdir()
        outside.mkdir()
        evidence_link = data_dir / "evidence"
        try:
            os.symlink(outside, evidence_link, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"directory symlink unavailable: {error}")
        artifact = self.artifact(root)
        outside_target = outside / "sha256" / artifact.sha256[:2] / (
            artifact.sha256 + ".png"
        )

        with self.assertRaises(EvidenceStoreError):
            EvidenceStore(data_dir).preserve((artifact,))

        self.assertFalse(outside_target.exists())
        self.assertTrue(evidence_link.is_symlink())

    def test_evidence_parent_reparse_attribute_is_rejected(self) -> None:
        if os.name != "nt":
            self.skipTest("reparse-point attributes are specific to Windows")
        root = self.root()
        data_dir = root / "data"
        evidence_dir = data_dir / "evidence"
        evidence_dir.mkdir(parents=True)
        artifact = self.artifact(root)
        real_lstat = os.lstat

        def evidence_reparse(path: object, *args: object, **kwargs: object) -> object:
            info = real_lstat(path, *args, **kwargs)
            if self.same_path(path, evidence_dir):
                attributes = getattr(info, "st_file_attributes", 0) | REPARSE_POINT
                return _StatOverlay(info, st_file_attributes=attributes)
            return info

        with patch("voicevault.evidence_store.os.lstat", side_effect=evidence_reparse):
            with self.assertRaises(EvidenceStoreError):
                EvidenceStore(data_dir).preserve((artifact,))

        self.assertFalse((data_dir / self.relative_path(artifact)).exists())

    def test_invalid_keys_digest_size_and_media_are_rejected(self) -> None:
        root = self.root()
        valid = self.artifact(root)
        invalid_cases = (
            ("empty key", (replace(valid, evidence_key=""),), EvidenceStoreError),
            ("blank key", (replace(valid, evidence_key="   "),), EvidenceStoreError),
            (
                "duplicate key",
                (valid, replace(valid, source_path=root / "other-source")),
                EvidenceStoreError,
            ),
            (
                "uppercase digest",
                (replace(valid, sha256=valid.sha256.upper()),),
                EvidenceStoreError,
            ),
            ("short digest", (replace(valid, sha256="abc"),), EvidenceStoreError),
            ("negative size", (replace(valid, byte_size=-1),), EvidenceStoreError),
            ("boolean size", (replace(valid, byte_size=True),), EvidenceStoreError),
            (
                "unsupported media",
                (replace(valid, media_type="text/plain"),),
                UnsupportedEvidenceMediaType,
            ),
        )
        for name, artifacts, error_type in invalid_cases:
            with self.subTest(name=name):
                data_dir = root / f"data-{name.replace(' ', '-')}"
                with self.assertRaises(error_type):
                    EvidenceStore(data_dir).preserve(artifacts)

    def test_two_threads_publish_one_final_cas_file(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"thread-safe-content")
        barrier = threading.Barrier(2)

        def preserve() -> str:
            barrier.wait(timeout=10)
            result = EvidenceStore(data_dir).preserve((artifact,))
            return result[artifact.evidence_key].relative_path

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(preserve) for _ in range(2)]
            relative_paths = [future.result(timeout=20) for future in futures]

        self.assertEqual(relative_paths, [self.relative_path(artifact)] * 2)
        target = data_dir / self.relative_path(artifact)
        self.assertEqual(target.read_bytes(), b"thread-safe-content")
        self.assert_final_cas_layout(
            target,
            expected_retained_temporaries=0 if os.name == "nt" else None,
        )

    def test_generic_unix_fallback_hard_links_without_removing_source(self) -> None:
        root = self.root()
        source = root / "source.tmp"
        target = root / "target.bin"
        source.write_bytes(b"fallback-content")
        real_link = os.link
        directory_fd = 123
        calls: list[tuple[str, str, int, int, bool]] = []

        def hard_link_at(
            source_name: str,
            target_name: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
            follow_symlinks: bool,
        ) -> None:
            calls.append(
                (
                    source_name,
                    target_name,
                    src_dir_fd,
                    dst_dir_fd,
                    follow_symlinks,
                )
            )
            real_link(
                root / source_name,
                root / target_name,
                follow_symlinks=follow_symlinks,
            )

        with (
            patch("voicevault.evidence_store.sys.platform", "linux"),
            patch(
                "voicevault.evidence_store._linux_rename_noreplace_at",
                return_value=False,
            ) as native_publish,
            patch(
                "voicevault.evidence_store.os.link",
                side_effect=hard_link_at,
            ),
        ):
            evidence_store_module._rename_noreplace_at(
                directory_fd,
                source.name,
                target.name,
            )

        native_publish.assert_called_once_with(
            directory_fd,
            source.name,
            target.name,
        )
        self.assertEqual(
            calls,
            [
                (
                    source.name,
                    target.name,
                    directory_fd,
                    directory_fd,
                    False,
                )
            ],
        )
        self.assertTrue(source.is_file())
        self.assertEqual(target.read_bytes(), b"fallback-content")
        self.assertTrue(os.path.samefile(source, target))

    def test_concurrent_hard_link_fallback_retains_each_controlled_temporary(self) -> None:
        root = self.root()
        data_dir = root / "data"
        content = b"concurrent-fallback-content"
        artifacts = tuple(
            self.artifact(
                root,
                evidence_key=f"proof-{index}",
                content=content,
                source_name=f"fallback-source-{index}.bin",
            )
            for index in range(2)
        )
        metadata_barrier = threading.Barrier(2)

        class NoopLock:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *_args: object) -> None:
                return None

        def hard_link_publish(
            directory: object, source_name: str, target_name: str
        ) -> None:
            if target_name.endswith(".meta"):
                metadata_barrier.wait(timeout=10)
            os.link(
                directory.path / source_name,  # type: ignore[attr-defined]
                directory.path / target_name,  # type: ignore[attr-defined]
                follow_symlinks=False,
            )

        def preserve(artifact: ValidatedArtifact) -> str:
            stored = EvidenceStore(data_dir).preserve((artifact,))
            return stored[artifact.evidence_key].relative_path

        with (
            patch("voicevault.evidence_store._PRESERVE_LOCK", NoopLock()),
            patch.object(
                evidence_store_module._TrustedDirectory,
                "publish_noreplace",
                autospec=True,
                side_effect=hard_link_publish,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            relative_paths = list(executor.map(preserve, artifacts))

        self.assertEqual(relative_paths, [self.relative_path(artifacts[0])] * 2)
        target = data_dir / relative_paths[0]
        self.assert_final_cas_layout(
            target,
            expected_retained_temporaries=4,
        )

    def test_store_never_calls_delete_primitives_and_failure_preserves_files(self) -> None:
        root = self.root()
        data_dir = root / "data"
        retained = self.artifact(
            root,
            evidence_key="retained",
            content=b"retained-cas",
            source_name="retained.bin",
        )
        EvidenceStore(data_dir).preserve((retained,))
        retained_target = data_dir / self.relative_path(retained)
        failing = self.artifact(
            root,
            evidence_key="failing",
            content=b"expected-data",
            source_name="failing.bin",
        )
        failing.source_path.write_bytes(b"tampered-data")
        failing_target = data_dir / self.relative_path(failing)

        with (
            patch("os.unlink") as os_unlink,
            patch("os.remove") as os_remove,
            patch.object(Path, "unlink") as path_unlink,
            patch.object(shutil, "rmtree") as shutil_rmtree,
        ):
            with self.assertRaises(EvidenceSourceChanged):
                EvidenceStore(data_dir).preserve((failing,))

            os_unlink.assert_not_called()
            os_remove.assert_not_called()
            path_unlink.assert_not_called()
            shutil_rmtree.assert_not_called()

        self.assertEqual(retained_target.read_bytes(), b"retained-cas")
        self.assertTrue(retained.source_path.is_file())
        self.assertEqual(failing.source_path.read_bytes(), b"tampered-data")
        self.assertFalse(failing_target.exists())

    def test_posix_directory_open_closes_new_descriptor_when_fstat_fails(self) -> None:
        directory_info = _StatOverlay(
            os.stat_result(
                (stat.S_IFDIR | 0o700, 11, 22, 1, 0, 0, 0, 0, 0, 0)
            ),
            st_file_attributes=0,
        )
        path = Path(Path.cwd().anchor) / "voicevault-posix-fd-test"

        with (
            patch("voicevault.evidence_store.os.open", side_effect=(100, 101)) as opened,
            patch("voicevault.evidence_store.os.stat", return_value=directory_info) as stated,
            patch("voicevault.evidence_store.os.fstat", side_effect=OSError("fstat failed")),
            patch("voicevault.evidence_store.os.close") as closed,
            patch.object(
                evidence_store_module.os,
                "supports_dir_fd",
                {opened, stated},
            ),
        ):
            with self.assertRaises(EvidenceStoreError):
                evidence_store_module._open_posix_directory_chain(
                    path,
                    EvidenceStoreError,
                    "Directory unavailable.",
                    create=False,
                )

        self.assertEqual(
            [call.args[0] for call in closed.call_args_list],
            [101, 100],
        )

    def test_fdopen_failure_closes_owned_descriptor(self) -> None:
        for method_name in ("open_read", "open_exclusive"):
            with self.subTest(method=method_name):
                directory = evidence_store_module._TrustedDirectory(
                    Path.cwd(),
                    EvidenceStoreError,
                    "Directory unavailable.",
                )
                directory.posix_handles = [100]
                with (
                    patch("voicevault.evidence_store.os.open", return_value=101),
                    patch(
                        "voicevault.evidence_store.os.fdopen",
                        side_effect=OSError("fdopen failed"),
                    ),
                    patch("voicevault.evidence_store.os.close") as closed,
                ):
                    with self.assertRaises(EvidenceStoreError):
                        getattr(directory, method_name)("artifact.bin")

                closed.assert_called_once_with(101)
                directory.posix_handles = []

    def test_staged_evidence_tamper_before_publish_is_rejected(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"original-bytes")
        replacement = b"replaced-bytes"
        target = data_dir / self.relative_path(artifact)
        real_claim = evidence_store_module._claim_digest_metadata
        tampered = False

        def tamper_then_claim(
            directory: object, prepared: object
        ) -> None:
            nonlocal tampered
            trusted = directory  # keep the race hook explicit and local to this test
            candidate_names = [
                entry.name
                for entry in trusted.path.iterdir()  # type: ignore[attr-defined]
                if entry.name.startswith(f".{artifact.sha256}.png.")
                and entry.name.endswith(".tmp")
            ]
            self.assertEqual(len(candidate_names), 1)
            (trusted.path / candidate_names[0]).write_bytes(  # type: ignore[attr-defined]
                replacement
            )
            tampered = True
            real_claim(trusted, prepared)  # type: ignore[arg-type]

        with patch(
            "voicevault.evidence_store._claim_digest_metadata",
            side_effect=tamper_then_claim,
        ):
            with self.assertRaises(EvidenceContentConflict):
                EvidenceStore(data_dir).preserve((artifact,))

        self.assertTrue(tampered)
        self.assertFalse(target.exists())

    def test_staged_metadata_tamper_cannot_return_success(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"metadata-tamper")
        target = data_dir / self.relative_path(artifact)
        sidecar_name = f"{artifact.sha256}.meta"
        expected_metadata = (
            f"voicevault-evidence-v1\n{artifact.media_type}\n{artifact.byte_size}\n"
        ).encode("ascii")
        replacement = b"x" * len(expected_metadata)
        real_publish = evidence_store_module._TrustedDirectory.publish_noreplace
        tampered = False

        def tamper_then_publish(
            directory: object, source_name: str, target_name: str
        ) -> None:
            nonlocal tampered
            if target_name == sidecar_name:
                (directory.path / source_name).write_bytes(replacement)  # type: ignore[attr-defined]
                tampered = True
            real_publish(directory, source_name, target_name)  # type: ignore[arg-type]

        with patch.object(
            evidence_store_module._TrustedDirectory,
            "publish_noreplace",
            autospec=True,
            side_effect=tamper_then_publish,
        ):
            with self.assertRaises(EvidenceContentConflict):
                EvidenceStore(data_dir).preserve((artifact,))

        self.assertTrue(tampered)
        self.assertFalse(target.exists())
        self.assertEqual((target.parent / sidecar_name).read_bytes(), replacement)

    def test_publish_time_evidence_tamper_cannot_return_success(self) -> None:
        root = self.root()
        data_dir = root / "data"
        artifact = self.artifact(root, content=b"original-bytes")
        replacement = b"replaced-bytes"
        target = data_dir / self.relative_path(artifact)
        real_publish = evidence_store_module._TrustedDirectory.publish_noreplace
        tampered = False

        def tamper_then_publish(
            directory: object, source_name: str, target_name: str
        ) -> None:
            nonlocal tampered
            if target_name == target.name:
                (directory.path / source_name).write_bytes(replacement)  # type: ignore[attr-defined]
                tampered = True
            real_publish(directory, source_name, target_name)  # type: ignore[arg-type]

        with patch.object(
            evidence_store_module._TrustedDirectory,
            "publish_noreplace",
            autospec=True,
            side_effect=tamper_then_publish,
        ):
            with self.assertRaises(EvidenceContentConflict):
                EvidenceStore(data_dir).preserve((artifact,))

        self.assertTrue(tampered)
        self.assertEqual(target.read_bytes(), replacement)

    def test_error_messages_do_not_reveal_absolute_paths(self) -> None:
        root = self.root()
        data_dir = root / "private-data-dir"
        missing_source = root / "private-source" / "missing.png"
        artifact = self.artifact(
            root,
            content=b"",
            source_path=missing_source,
            declared_sha256=hashlib.sha256(b"").hexdigest(),
            declared_size=0,
        )

        with self.assertRaises(EvidenceSourceChanged) as raised:
            EvidenceStore(data_dir).preserve((artifact,))

        message = str(raised.exception)
        self.assertNotIn(str(root), message)
        self.assertNotIn(str(data_dir), message)
        self.assertNotIn(str(missing_source), message)


if __name__ == "__main__":
    unittest.main()
