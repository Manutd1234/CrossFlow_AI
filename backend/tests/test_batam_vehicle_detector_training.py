"""Artificial metadata/box fixtures check holdout safety, never model accuracy."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/train_batam_vehicle_detector.py"
SPEC = importlib.util.spec_from_file_location("batam_vehicle_detector_training", SCRIPT)
training = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(training)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value))


@pytest.fixture
def bundle(tmp_path):
    policy = tmp_path / "policy.json"
    write(policy, {"policy": "Artificial fixture; split by independent author before annotations"})
    split = {"schema": "crossflow.batam_vehicle_split.v1", "split_policy": {"path": "policy.json", "sha256": training.digest(policy)}, "images": []}
    manifest = {"schema": "crossflow.batam_vehicle_annotations.v1", "annotation_origin": "ai_visual_blind", "human_certified": False, "files": {}}
    for index, partition in enumerate(training.SPLITS):
        data = f"artificial image bytes {index}".encode()
        (tmp_path / f"{partition}.jpg").write_bytes(data)
        split["images"].append({"image_id": f"fixture-{partition}", "path": f"{partition}.jpg", "image_sha256": sha(data),
                                "decoded_sha256": sha(b"artificial rgb " + data), "width": 100, "height": 100,
                                "source_id": f"fixture-source-{index}", "source_group": f"fixture-capture-{index}",
                                "photographer": f"fixture-author-{index}", "split": partition, "geography": "Batam",
                                "license": "CC0-1.0 synthetic fixture only", "training_eligible": True})
        path = tmp_path / f"{partition}.json"
        write(path, {"images": [{"image_id": f"fixture-{partition}", "boxes_xyxy": [[.1, .1, .4, .4]]}]})
        manifest["files"][partition] = {"path": path.name, "sha256": training.digest(path)}
    split_path, manifest_path = tmp_path / "split.json", tmp_path / "annotation_manifest.json"
    write(split_path, split)
    manifest["split_sha256"] = training.digest(split_path)
    write(manifest_path, manifest)
    return {"root": tmp_path, "split": split, "manifest": manifest, "split_path": split_path, "manifest_path": manifest_path}


def rebind(bundle):
    write(bundle["split_path"], bundle["split"])
    bundle["manifest"]["split_sha256"] = training.digest(bundle["split_path"])
    write(bundle["manifest_path"], bundle["manifest"])


def protocol(bundle):
    return training.load_protocol(bundle["split_path"], bundle["manifest_path"])


def frozen(bundle, context):
    directory = bundle["root"] / "output"
    directory.mkdir(exist_ok=True)
    model = directory / "model.pt"
    model.write_bytes(b"artificial checkpoint integrity fixture")
    selection = {"schema": "crossflow.batam_vehicle_selection.v1", "split_sha256": context["split_sha256"],
                 "annotation_manifest_sha256": context["annotation_manifest_sha256"], "test_labels_read_before_freeze": False,
                 "production_eligible": False, "model_sha256": training.digest(model), "architecture": training.ARCHITECTURE,
                 "num_classes": 3, "vehicle_classes": {"1": "vehicle"}, "score_threshold": .4, "vehicle_nms_iou": .5,
                 "min_vehicle_long_side_px": 12, "min_vehicle_short_side_px": 4,
                 "selected_model": "batam_head_epoch_2", "validation": {"partition": "validation", "candidate": "batam_head_epoch_2", "score_threshold": .4}}
    path = directory / training.CORRECT_FREEZE
    write(path, selection)
    return path


def test_protocol_metadata_does_not_open_any_partition_labels(bundle, monkeypatch):
    original_read_bytes, original_read_text = Path.read_bytes, Path.read_text
    protected = {bundle["root"] / f"{part}.json" for part in training.SPLITS}

    def bytes_guard(path, *args, **kwargs):
        assert path not in protected, "Metadata preflight must not read annotation bytes"
        return original_read_bytes(path, *args, **kwargs)

    def text_guard(path, *args, **kwargs):
        assert path not in protected, "Metadata preflight must not parse annotations"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", bytes_guard)
    monkeypatch.setattr(Path, "read_text", text_guard)
    context = protocol(bundle)
    assert len(context["images"]) == 3


@pytest.mark.parametrize("field", ["source_group", "source_id", "photographer"])
def test_source_or_author_overlap_refused_before_any_training(bundle, field):
    bundle["split"]["images"][1][field] = bundle["split"]["images"][0][field]
    rebind(bundle)
    with pytest.raises(ValueError, match="overlaps partitions"):
        protocol(bundle)


@pytest.mark.parametrize("field", ["image_sha256", "decoded_sha256"])
def test_exact_bytes_and_reencoded_duplicate_pixels_refused(bundle, field):
    bundle["split"]["images"][2][field] = bundle["split"]["images"][0][field]
    rebind(bundle)
    with pytest.raises(ValueError, match="Duplicate image"):
        protocol(bundle)


def test_split_and_preannotation_policy_hashes_are_bound(bundle):
    bundle["split_path"].write_text(bundle["split_path"].read_text() + " ")
    with pytest.raises(ValueError, match="unchanged frozen split"):
        protocol(bundle)
    rebind(bundle)
    (bundle["root"] / "policy.json").write_text("changed grouping after annotations")
    with pytest.raises(ValueError, match="split policy changed"):
        protocol(bundle)


def test_non_batam_unknown_rights_or_path_escape_rejected(bundle):
    for update in ({"geography": "Singapore"}, {"training_eligible": False}, {"license": ""}, {"path": "../outside.jpg"}):
        original = deepcopy(bundle["split"]["images"][0])
        bundle["split"]["images"][0].update(update)
        rebind(bundle)
        with pytest.raises(ValueError):
            protocol(bundle)
        bundle["split"]["images"][0] = original


def test_development_labels_can_load_while_test_remains_unread(bundle, monkeypatch):
    context = protocol(bundle)
    original_read_bytes, original_read_text = Path.read_bytes, Path.read_text
    test_path = bundle["root"] / "test.json"

    def bytes_guard(path, *args, **kwargs):
        assert path != test_path, "Training must not hash/read sealed test labels"
        return original_read_bytes(path, *args, **kwargs)

    def text_guard(path, *args, **kwargs):
        assert path != test_path, "Training must not parse sealed test labels"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", bytes_guard)
    monkeypatch.setattr(Path, "read_text", text_guard)
    rows = training.load_annotations(context, "train")
    assert rows[0]["vehicle_boxes"] == [[10., 10., 40., 40.]]
    with pytest.raises(ValueError, match="sealed"):
        training.load_annotations(context, "test")


def test_test_requires_intact_selection_and_only_then_checks_its_hash(bundle):
    context = protocol(bundle)
    (bundle["root"] / "test.json").write_text("changed unopened labels")
    assert training.load_annotations(context, "train")
    selection = frozen(bundle, context)
    with pytest.raises(ValueError, match="Annotation bytes changed"):
        training.load_annotations(context, "test", freeze_path=selection)
    (selection.parent / "model.pt").write_bytes(b"changed checkpoint")
    with pytest.raises(ValueError, match="checkpoint differs"):
        training.load_annotations(context, "test", freeze_path=selection)


def test_annotation_tampering_and_model_pseudo_labels_are_refused(bundle):
    context = protocol(bundle)
    (bundle["root"] / "train.json").write_text("changed")
    with pytest.raises(ValueError, match="annotation bytes changed"):
        training.load_annotations(context, "train")
    bundle["manifest"]["annotation_origin"] = "model_predictions"
    rebind(bundle)
    with pytest.raises(ValueError, match="blind assistant visual"):
        protocol(bundle)


@pytest.mark.parametrize("boxes", [[[.1, .1, .09, .4]], [[.1, .1, float("nan"), .4]], [[.1, .1, .15, .15]], [[0, 0, 2, 1]]])
def test_invalid_or_below_policy_boxes_refused(bundle, boxes):
    path = bundle["root"] / "train.json"
    write(path, {"images": [{"image_id": "fixture-train", "boxes_xyxy": boxes}]})
    bundle["manifest"]["files"]["train"]["sha256"] = training.digest(path)
    rebind(bundle)
    with pytest.raises(ValueError, match="annotation|extent"):
        training.load_annotations(protocol(bundle), "train")


def test_empty_annotated_images_are_included_and_missing_images_are_refused(bundle):
    path = bundle["root"] / "validation.json"
    write(path, {"images": [{"image_id": "fixture-validation", "boxes_xyxy": []}]})
    bundle["manifest"]["files"]["validation"]["sha256"] = training.digest(path)
    rebind(bundle)
    rows = training.load_annotations(protocol(bundle), "validation")
    assert rows[0]["vehicle_boxes"] == []
    write(path, {"images": []})
    bundle["manifest"]["files"]["validation"]["sha256"] = training.digest(path)
    rebind(bundle)
    with pytest.raises(ValueError, match="cover every image"):
        training.load_annotations(protocol(bundle), "validation")


def test_ambiguous_exclusions_require_frozen_decision_reason_and_remain_disclosed(bundle):
    extra = deepcopy(bundle["split"]["images"][0])
    extra.update(image_id="fixture-ambiguous", path="ambiguous.jpg", image_sha256=sha(b"extra"), decoded_sha256=sha(b"extra rgb"))
    bundle["split"]["images"].append(extra)
    path = bundle["root"] / "train.json"
    annotations = {"images": [{"image_id": "fixture-train", "boxes_xyxy": [[.1, .1, .4, .4]]},
                               {"image_id": "fixture-ambiguous", "review_status": "excluded_ambiguous", "decision": "exclude",
                                "exclusion_reason": "Artificial unresolved visible vehicle example"}]}
    write(path, annotations)
    bundle["manifest"]["files"]["train"]["sha256"] = training.digest(path)
    rebind(bundle)
    context = protocol(bundle)
    rows = training.load_annotations(context, "train")
    assert [row["image_id"] for row in rows] == ["fixture-train"]
    assert context["exclusions"]["fixture-ambiguous"]["reason"] == annotations["images"][1]["exclusion_reason"]
    del annotations["images"][1]["exclusion_reason"]
    write(path, annotations)
    bundle["manifest"]["files"]["train"]["sha256"] = training.digest(path)
    rebind(bundle)
    with pytest.raises(ValueError, match="explicit decision, recognized category and reason"):
        training.load_annotations(protocol(bundle), "train")


@pytest.mark.parametrize("review_status", ["excluded_off_domain", "reviewed"])
def test_off_domain_exclusion_cannot_become_an_easy_zero_count_training_frame(bundle, review_status):
    extra = deepcopy(bundle["split"]["images"][0])
    extra.update(image_id="fixture-indoor", path="indoor.jpg", image_sha256=sha(b"indoor"), decoded_sha256=sha(b"indoor rgb"))
    bundle["split"]["images"].append(extra)
    path = bundle["root"] / "train.json"
    write(path, {"images": [{"image_id": "fixture-train", "boxes_xyxy": [[.1, .1, .4, .4]]},
                             {"image_id": "fixture-indoor", "review_status": review_status, "decision": "exclude",
                              "exclusion_reason": "Artificial indoor shopping mall fixture, outside roadway domain", "boxes_xyxy": []}]})
    bundle["manifest"]["files"]["train"]["sha256"] = training.digest(path)
    rebind(bundle)
    context = protocol(bundle)
    assert [row["image_id"] for row in training.load_annotations(context, "train")] == ["fixture-train"]
    assert context["exclusions"]["fixture-indoor"]["review_status"] == review_status


def test_box_matching_is_one_to_one_and_does_not_call_confidence_accuracy():
    rows = [{"source_group": "first", "vehicle_boxes": [[0, 0, 20, 20]]},
            {"source_group": "second", "vehicle_boxes": [[10, 10, 30, 30]]}]
    predictions = [[{"score": .9, "box_xyxy": [0, 0, 20, 20]}, {"score": .8, "box_xyxy": [0, 0, 20, 20]}], []]
    result = training.metrics(predictions, rows)
    assert result["count"]["mae_vehicles_per_frame"] == 1
    assert result["count"]["exact_count_pct"] == 0
    assert result["count"]["within_one_vehicle_pct"] == 100
    assert result["detection_iou_50"] == {"precision": .5, "recall": .5, "f1": .5, "true_positive": 1, "false_positive": 1, "false_negative": 1}
    assert "accuracy" not in result


def test_model_selection_uses_group_macro_validation_only():
    def candidate(name, macro, f1, threshold=.4, partition="validation"):
        return {"candidate": name, "partition": partition, "score_threshold": threshold,
                "metrics": {"count": {"macro_source_group_mae": macro}, "detection_iou_50": {"f1": f1}}}
    first, second = candidate("first", 1., .5), candidate("second", 2., .9)
    assert training.select_validation([second, first])["candidate"] == "first"
    assert training.select_validation([first, candidate("tied_mae_better_f1", 1., .6)])["candidate"] == "tied_mae_better_f1"
    assert training.select_validation([first, candidate("higher_threshold", 1., .5, .5)])["candidate"] == "higher_threshold"
    with pytest.raises(ValueError, match="Only validation"):
        training.select_validation([first, candidate("test-winner", 0., 1., partition="test")])


def test_prediction_size_policy_matches_annotation_inclusion_in_native_pixels():
    boxes = [[0, 0, 12, 4], [0, 0, 4, 12], [0, 0, 11.99, 4], [0, 0, 12, 3.99],
             [0, 0, 8, 8], [0, 0, 50, 20], [0, 0, float("nan"), 20]]
    detections = [{"box_xyxy": box, "score": .9, "id": index} for index, box in enumerate(boxes)]
    retained = training.apply_size_policy(detections)
    assert [box["id"] for box in retained] == [0, 1, 5]
    assert all(box["score"] == .9 for box in retained)


def test_existing_results_cannot_be_overwritten_before_loading_torch(bundle):
    output = bundle["root"] / "prior-experiment"
    output.mkdir()
    (output / "report.json").write_text("preserve this frozen experiment")
    with pytest.raises(FileExistsError, match="never overwritten"):
        training.run(bundle["split_path"], bundle["manifest_path"], output, bundle["root"] / "missing.pt")
    assert (output / "report.json").read_text() == "preserve this frozen experiment"
