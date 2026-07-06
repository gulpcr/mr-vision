# Vendored from batmanlab/Mammo-CLIP (CC BY-NC-SA 4.0).
#
# The upstream __init__ eagerly imports trainer/validator, which pull in hydra,
# albumentations and the full data stack. For inference we only need
# `breastclip.model.build_model`, so this init is intentionally minimal to keep the
# import light. Import the model submodule directly: `from breastclip.model import build_model`.
