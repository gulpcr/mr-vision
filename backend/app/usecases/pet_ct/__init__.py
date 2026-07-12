"""Whole-body FDG-PET/CT oncology use-case plugin.

Modular, domain-driven architecture (see each module's docstring):

    pipeline.py        Orchestrator/facade — BasePipeline preprocess/infer/postprocess.
    thresholding.py    Stage 1 — reference stats, detection threshold, TotalSegmentator
                       organ masks, connected-component clustering + measurement/naming.
    registration.py    Stage 2 — DICOM/SUV geometry, CT co-registration, and
                       world-coordinate per-lesion display-slice matching.
    visualization.py   Stage 3 — MIP/fused renderers + numbered coronal roadmap and
                       composite regional crops for the vision-language model.
    prompts.py         Stage 4 — structured MedGemma data-matrix payload builder.

The use-case loader imports ``app.usecases.pet_ct.pipeline:Pipeline`` directly, so
nothing is re-exported here (kept import-light to avoid package-init import cycles).
"""
