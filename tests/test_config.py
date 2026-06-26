"""Config loading test — make sure stages.yaml is valid."""

from __future__ import annotations

from audit.config import load_config


def test_default_config_loads() -> None:
    cfg = load_config()
    for name in ["recon", "hunt", "validate", "gapfill", "dedupe", "trace",
                 "feedback", "report"]:
        sc = cfg.get(name)
        assert sc.model, f"{name}: missing model"
        assert sc.concurrency >= 1, f"{name}: invalid concurrency"
        assert sc.tools, f"{name}: missing tools"


def test_hunt_validate_disagreement_configured() -> None:
    """Validate is the adversarial 'deliberate disagreement' stage. On the
    cursor backend all stages default to composer-2.5, so the disagreement
    is prompt-driven rather than model-driven. If you point Validate at a
    different model to strengthen it, that's fine too — either is valid.
    This test just guards that both stages are configured with a model."""
    cfg = load_config()
    assert cfg.get("hunt").model
    assert cfg.get("validate").model
