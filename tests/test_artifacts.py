from weather_arb_live import config
from weather_arb_live.calibration import load_calibration


def test_seed_artifacts_exist_in_standalone_repo():
    assert config.RESIDUALS_CACHE_PATH.exists()
    assert config.SIGMA_CACHE_PATH.exists()
    assert config.CALIBRATION_PATH.exists()


def test_calibration_table_loads():
    calibration = load_calibration()

    assert calibration is not None
    assert calibration.lookup
