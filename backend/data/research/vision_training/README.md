# Static image vehicle-count transfer diagnostic

Trained `ridge_1000` from pixel features of 48 licensed Visaitech frames,
then scored 32 source-disjoint TRANSSET aerial images.
The target is the count of original vehicle annotation boxes, excluding pedestrians.
This is **not a bounding-box detector or a Batam camera model**.

Holdout MAE: **3.614 vehicles/frame**.
Constant training-mean MAE: 2.469;
constant training-median MAE: 2.469.
Model beats both baselines: **False**.
Grouped validation preferred strategy: `constant_training_median`.

Five fixed lightweight candidates are compared by leave-whole-source-video-group
out validation within Visaitech. Exact/perceptual similarity can merge related
video groups and excludes any test image resembling training. No random frame
split, pretrained model, additional photos, augmentation or network access is used.
Features are fixed RGB/gradient summaries, not annotation geometry or source IDs.
TRANSSET capture-session provenance is absent, so its frames are not independent
trials. Pixel counts can fail under viewpoint/source shift. Full limitations,
candidate metrics, source attribution and environment are in `report.json`.

Both source images and annotations are CC BY 4.0. Attribution: Visaitech,
Mixed-Traffic Vehicle Detection Dataset v0.1 (revision de35185262bda34c104a626f866bab9d9aaf67ed);
Gebre, T.; Blackston, Q.; Hashemi-Beni, L., TRANSSET v5,
doi:10.6084/m9.figshare.26082217.v5. Original source files remain unchanged.

`pixel_features.npz` contains feature arrays only, not new photos. The serialized
trusted local artifact remains research-only and requires the recorded environment.
Reproduce into a fresh directory with `python3 scripts/train_visual_counts.py --output-dir <new-path>`.
