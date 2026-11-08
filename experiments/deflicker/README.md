# Deflicker experiments

These scripts preserve two investigated approaches that were useful
diagnostically but did **not** meet the visual acceptance threshold:

- `correct_discrete_exposure_states.py` estimates one fixed offset between two
  alternating shutter states and applies it only to the darker state.
- `refine_exposure_residuals.py` combines the production correction with a
  nine-region common temporal residual.

They never modify originals, but they are not imported by the production
post-processing workflow. See `docs/EXPOSURE_AND_DEFLICKER_FINDINGS.md` for the
measurements and conclusions.
