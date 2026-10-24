# Batam vehicle-counting image pilot — 24 October 2026

Training on the Batam image pilot **did not improve the vehicle counter**. The experiment used 96 blind assistant-authored vehicle boxes across 16 included photographs, and validation retained the unchanged pretrained detector. On four held-out Batam photographs, its mean count error was **3.0 vehicles per image**, with **0 of 4 exact frame counts**. The app default remains unchanged and experimental. The requested 99–100% accuracy and Google-superiority target has not been reached.

## What entered the experiment

Fifty source-candidate photographs were reviewed from public Wikimedia Commons and Flickr sources with individual CC BY or CC BY-SA licenses. Thirty-four were excluded before inference because they were outside the traffic domain or had unresolved vehicle counts. Excluded photographs were not treated as empty traffic scenes.

| Partition | Included photographs | Vehicle boxes | Use |
| --- | ---: | ---: | --- |
| Training | 9 | 56 | Fit the detector's ROI box predictor |
| Validation | 3 | 22 | Select the checkpoint and score threshold |
| Test | 4 | 18 | One final comparison after selection froze |

Three Batam photographs were newly downloaded in this pass: two new native Flickr IDs and one previously known metadata-only record. All three were by Sharon Hahn Darlin on 28 December 2024 and explicitly licensed CC BY 2.0 on their individual public source pages. Two photographs, with seven vehicle boxes, entered training. The third was excluded because a distant possible motorcycle cluster could not be counted reliably.

The remaining 47 source-candidate photographs were already local: 44 baseline captures and three validation additions. Six larger Commons representations were downloaded, but were versions of existing captures. Existing 960 px previews were not upsampled. Map features, map nodes, foreign photographs, resized inspection crops, model predictions and augmented copies did not increase the Batam source-image or annotation counts.

The source cohort is `research/batam-traffic/datasets/round10_batam_vehicle_images/`. `split_v2.json` retains exact source URLs, attribution, per-file license URL, published capture dates, image dimensions, image-byte SHA-256 and decoded RGB SHA-256. Published titles/captions and supplied geotags provide location evidence; camera clock accuracy and capture authenticity have not been independently field-verified.

## Separation and annotation safeguards

Photographer-level assignment was frozen before annotations or inference. All photographs by the same photographer stay in one partition, including distinct capture sessions. The original split remains available. A documented V2 extension added three existing validation images and three newly downloaded training images before any model outputs were read; the original test candidate membership and annotation bytes stayed unchanged. This was an adaptive coverage extension prompted by limited validation coverage, not by model scores.

The label definition is the visible extent of identifiable ground vehicles, including parked vehicles. The minimum box has a long side of 12 px and short side of 4 px in the actual acquired image, before model resizing. Riders are excluded when separable; boxes are clipped to the image. Boats, aircraft, pedestrians, printed vehicle depictions and unidentifiable fragments are excluded. A photograph with unresolved above-threshold vehicle counts was excluded before inference. Original reviews, ambiguity notes, a second training-image review and corrections are retained in `annotations/`.

The annotation origin is `ai_visual_blind`, with `human_certified=false`. Annotators inspected source pixels without detector predictions. These labels are useful for a pilot, but are not an independently human-certified benchmark. The separate test file is opened by the training pipeline only after the chosen model and threshold are committed to `selection_frozen_before_batam_test.json`.

## Training and evaluation

The initial checkpoint is official TorchVision Faster R-CNN ResNet-50 FPN V2, with SHA-256 `dd69338a24b8d7381807e247652bdc356325bcbaf1cd3e092e00e0a1a58706bf`. The experiment freezes the backbone, proposal network, box head and normalization statistics, and trains only the ROI box predictor. The fine-tuned predictor combines eligible vehicle types into one vehicle class.

The bounded protocol uses CPU inference with two threads, seed `20261024`, AdamW, learning rate `0.0003`, weight decay `0.0001`, batch size two, and 10 epochs. Training-only horizontal flips are seeded augmentation, not extra source images. Predeclared checkpoint candidates are epochs 2, 5 and 10 plus the original pretrained detector. Predeclared score thresholds are 0.2, 0.3, 0.4, 0.5 and 0.6. Validation selection minimizes count MAE averaged across photographer groups, then maximizes detection F1 at IoU 0.5, then prefers the higher threshold.

The experiment completed 50 optimizer updates over 10 epochs. Training loss decreased from approximately 0.468 to 0.228, but none of the fine-tuned epoch-2, epoch-5 or epoch-10 checkpoints won validation. Validation selected the **unchanged pretrained COCO detector at score threshold 0.4**. Lower training loss therefore did not translate into a useful counting improvement.

| Measure | Selected detector | Fixed prior detector reference |
| --- | ---: | ---: |
| Batam test photographs / labeled instances | 4 / 18 | 4 / 18 |
| Mean absolute count error | 3.0 vehicles/image | 3.0 vehicles/image |
| Count RMSE | 3.536 vehicles/image | 3.536 vehicles/image |
| Exact frame-count agreement | 0/4 (0%) | 0/4 (0%) |
| Within one vehicle | 1/4 (25%) | 1/4 (25%) |
| Total predicted vehicle instances | 30 | 30 |
| Detection precision at IoU 0.5 | 60% | 60% |
| Detection recall at IoU 0.5 | 100% | 100% |
| Detection F1 at IoU 0.5 | 75% | 75% |

The detector matched all 18 annotated instances but produced 12 unmatched detections under the pilot matching rule. It overcounted every test photograph. The test mean absolute error improvement is **0%**. The selected candidate and fixed reference have identical weights and thresholds, so identical predictions are expected.

Validation count MAE was 0.667 vehicles/image on three photographs, with two exact counts. The poorer result on held-out photographers illustrates why validation scores alone cannot support app accuracy claims. Median selected CPU forward time was approximately 1.70 seconds with two threads; this excludes decoding, first loading, queuing and network time.

The frozen results are in `backend/data/research/batam_detector_training/report.json`. The prior app detector stays the default; no release pointer or model promotion is written for this result. An optional local research adapter applies the frozen native-pixel 12/4 size policy and preserves `production_eligible=false`, `batam_validated=false` and `human_certified=false`. A research preview is not a validated app release. Integrated app integrity checks are recorded separately from accuracy evidence. Integrated QA passed for engineering integrity while the accuracy gate failed. The focused checks passed **100 tests**: 36 backend app tests, 23 training-protocol tests and 41 frontend preview regressions. A separate dataset audit passed **876 checks** over source receipts, annotations, photographer separation, exact bytes and decoded pixels, with no flagged cross-partition near duplicates under its dHash screening rule. That heuristic does not prove capture independence.

`app_preview_qa.json` records real ASGI routes and the real isolated detector worker: authorized status/image requests returned 200, missing operator authorization returned 403, and invalid image bytes returned 422; all checked responses were private/no-store. The newly acquired **training** photograph `flickr:54235662106` returned four detections. This was a plumbing/reproduction check on a training example, not a new accuracy measurement or an untouched test. A separate app review reported no remaining material engineering issues. `scripts/verify_batam_preview.py` reproduces that local QA with a test-only identity and makes no traffic-provider calls.

The default model directory was checked and remains the previous detector. No release pointer or production activation was written. `annotation_example.png` is a review illustration derived from a training source with its frozen annotation boxes; it is not an additional photograph or label.

The reference uses the prior official COCO checkpoint at fixed score 0.4, with the same native-pixel 12/4 size filter used in this pilot. It is not the literal unfiltered image count returned by the previous app preview. A confidence score is not an accuracy percentage. Counting agreement, localization quality and end-to-end response latency are different measures.

## Local reproduction and package

The focused archive is prepared by `scripts/package_batam_image_pilot.py` only after passed dataset QA, training QA and integrated QA. It verifies all frozen source images, annotation receipts, selected checkpoint and training manifests. The archive contains 50 canonical candidate source photographs; annotation provenance and exclusion reviews; the preannotation policy and both split versions; public acquisition evidence; training/serving helpers and focused tests; experiment reports; and one selected checkpoint. Duplicate trial weights, credentials, environment files, SQLite archives, private provider observations and the full modified application entrypoint are excluded.

The selected model's SHA-256, archive CRC, every archive member SHA-256 and archive SHA-256 are checked. The archive has stable ordering, timestamps and permissions. Prior training archives and reports remain snapshots of their original experiments. The focused archive is an update for the existing workspace, not a standalone app.

Use an isolated vision runtime with `scripts/requirements-detector-training.txt`; the ordinary API runtime does not need Torch. No downloader or paid provider call is part of training. To replay the frozen experiment from `CrossFlow_AI`, provide the original official checkpoint separately and use a new output directory:

```sh
python scripts/train_batam_vehicle_detector.py \
  --split ../research/batam-traffic/datasets/round10_batam_vehicle_images/split_v2.json \
  --annotations ../research/batam-traffic/datasets/round10_batam_vehicle_images/annotation_manifest.json \
  --pretrained /absolute/path/to/fasterrcnn_resnet50_fpn_v2_coco-dd69338a.pth \
  --output backend/data/research/batam_detector_reproduction
```

The reference checkpoint is available from the [official PyTorch model host](https://download.pytorch.org/models/fasterrcnn_resnet50_fpn_v2_coco-dd69338a.pth); compare the full SHA-256 above before use. The package includes one selected model rather than duplicate pretrained/trial weights. The trainer rejects an occupied output directory and never replaces the current app checkpoint.

A replay is a reproducibility check, not fresh accuracy evidence. Once the test result has been opened, do not use it to tune thresholds, choose another checkpoint, relabel difficult frames, or claim a new independent test result. Further development needs independently reviewed Batam camera labels and a new untouched photographer/camera group.

## Practical limits

Only four included test photographs, with 18 vehicle instances, remain after the fixed ambiguity exclusions. Thirty-four of 50 source candidates were excluded, selecting easier scenes than unrestricted camera use. The public photographs have uneven dates, road coverage, lighting, vehicle sizes and viewpoints. Multiple photographs from one session can share the same physical vehicles; boxes count image instances, not unique vehicle identities. Possible overlap with the original COCO pretraining inventory is unknown.

The model is experimental, with `production_eligible=false` and `batam_validated=false`. The pilot does not measure unique vehicle flow, queue length, road speed, travel time or live congestion severity. No Google benchmarking was performed. Every photograph retains its own attribution and license; CC BY-SA material carries ShareAlike obligations. TorchVision software licensing and the rights of original COCO images/derived weights are distinct; production rights are not certified.
