# Experimental vehicle detector — 24 October 2026

The selected app-preview candidate is the official **Faster R-CNN ResNet50 FPN v2 COCO detector**, with a confidence threshold of 0.4 and generic-vehicle NMS IoU 0.5. It counts visible boxes in one image. It has **no validated Batam accuracy** and is not enabled for operational road counts, queues, speeds, or routing.

| Check | Count MAE per image | Exact count | Within one vehicle |
|---|---:|---:|---:|
| Visaitech held-video development validation, 16 frames / 55 boxes | 1.50 | 25.0% | 62.5% |
| Reused TRANSSET diagnostic, 32 frames / 163 boxes | 1.094 | 34.4% | 81.3% |

The reused TRANSSET result improves count error by 69.7% relative to the previous pixel regressor's 3.614 MAE, and beats the training-median constant baseline's 2.469 MAE. Generic-vehicle AP50 is 0.888, mAP50–95 is 0.475, precision is 0.824 and recall is 0.920 at IoU 0.5 and the frozen score threshold. These are correlated non-Batam images, previously consumed in earlier experiments; they are not a fresh benchmark. The stronger architecture trial was added after seeing the small detector's poor diagnostic, and that adaptive protocol is recorded. Neither 99–100% accuracy nor a comparison with Google has been established.

Actual supervised learning was performed: 96 SSDLite detection-head optimizer updates and 80 Faster R-CNN predictor updates on original author boxes, with the backbone frozen. Four complete Visaitech source videos supplied 32 training frames and 175 vehicle boxes; two other videos supplied 16 validation frames. Exact and perceptual duplicate screening found no cross-split matches. Fine-tuned candidates did not beat the untouched stronger pretrained detector on the grouped validation objective; the selected application weights therefore remain pretrained. Model and threshold were frozen before the reused cross-source diagnostic. Fine-tuning losses and negative results remain in the two trial folders.

The Visaitech data are South Asian dashcam frames with no verified country/city. Source two-wheelers combine bicycles, scooters and motorcycles; source three-wheelers include rickshaws, and four-wheelers combine cars/vans/buses/trucks. Training maps these to a generic vehicle label and preserves pedestrian separately. The selected COCO detector has no explicit rickshaw class. Only generic vehicle counts/box matching are evaluated; subtype accuracy is not established. TRANSSET consists of US aerial highway images with unpublished session identities. No Batam boxes, tracking identities, road clock times, queue labels, or speed labels were fabricated.

On this CPU with two PyTorch threads, median model-forward latency was 1.686 seconds and p95 was 1.929 seconds; decoding, cold loading, process startup and application queues add latency. It is a single-image preview, not a measured live CCTV throughput system.

## Local inference

Install the isolated optional versions in `scripts/requirements-detector-training.txt`. No network access or model download occurs during inference. The selected full checkpoint is `model.pt` (175,221,657 bytes), SHA256 `dd69338a24b8d7381807e247652bdc356325bcbaf1cd3e092e00e0a1a58706bf`.

```python
from pathlib import Path
from services.vehicle_counter import VehicleCounter

counter = VehicleCounter(allow_experimental=True)
result = counter.count_image(Path("authorized_image.jpg").read_bytes())
```

The service is disabled by default, imports heavy dependencies only when counting, verifies the local checkpoint hash, and loads with `weights_only=True`. It accepts bounded static JPEG/PNG/WebP images, applies EXIF orientation, excludes pedestrians, merges overlapping vehicle detections, and supports a normalized polygon ROI using box-center inclusion. The response marks the result experimental, Batam-unvalidated and production-ineligible. At most 100 boxes are returned, with a possible-truncation flag; confidence scores are not calibrated accuracy percentages.

`deployment_qa.json` checks current-service parity with the saved offline boxes on three images and the full-image ROI. `qa.json` contains the selected trial's frozen-selection and source/hash checks. `manifest.json` hashes deployment files and references the separately retained trial manifests. `ssdlite_trial/` preserves the original small-detector trial; `fasterrcnn_trial/` retains stronger-model training checkpoints and results. Trial manifests record code hashes at their original run time; the root deployment manifest records the current service. Reproduce each trial into a new output directory because training scripts refuse to overwrite frozen selections.

## Attribution and terms

Visaitech (2026), *Visaitech Mixed-Traffic Vehicle Detection Dataset v0.1*, revision `de35185262bda34c104a626f866bab9d9aaf67ed`, [CC BY 4.0](https://huggingface.co/datasets/visaitech/vehicle-mixed-traffic-detection/blob/de35185262bda34c104a626f866bab9d9aaf67ed/README.md). Boxes were remapped and training images horizontally flipped. Gebre, T.; Blackston, Q.; Hashemi-Beni, L., *TRANSSET v5*, [doi:10.6084/m9.figshare.26082217.v5](https://doi.org/10.6084/m9.figshare.26082217.v5), CC BY 4.0. Original boxes were merged to generic vehicle for evaluation; selected sample images received red predicted-box overlays. Original data files are unchanged.

TorchVision software is BSD-3-Clause and PyTorch uses a BSD-style license; retained license notices are in `evidence/`. The [official Faster R-CNN weights documentation](https://docs.pytorch.org/vision/0.25/models/generated/torchvision.models.detection.fasterrcnn_resnet50_fpn_v2.html) identifies the COCO training source. TorchVision explicitly warns that pretrained models may inherit dataset terms. [Official COCO terms](https://github.com/cocodataset/cocodataset.github.io/blob/master/dataset/termsofuse.htm) license annotations under CC BY 4.0 and state that original image copyrights are held elsewhere. `licenses.json` distinguishes software, weights and fine-tune data; production weight/data-rights clearance has not been certified.
