import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viabot_survey import config as config_module  # noqa: E402
from viabot_survey.app import create_app  # noqa: E402
from viabot_survey.runner import SurveyRunner  # noqa: E402
from viabot_survey.storage import Storage  # noqa: E402
from viabot_survey.updater import Updater  # noqa: E402


@pytest.fixture
def config(tmp_path):
    """Example defaults, pointed at a temp dir with all hardware workers off."""
    cfg = config_module.load(path=tmp_path / "missing.yaml", environ={})
    cfg._data["storage"]["data_dir"] = str(tmp_path / "data")
    for section in ("ping", "dns", "camera"):
        cfg._data[section]["enabled"] = False
    return cfg


@pytest.fixture
def storage(config):
    return Storage(config.db_path)


@pytest.fixture
def runner(config, storage):
    survey = SurveyRunner(config, storage)
    yield survey
    survey.shutdown()


@pytest.fixture
def client(config, storage, runner, tmp_path):
    app = create_app(config, runner, storage, Updater(tmp_path, enabled=False))
    app.config["TESTING"] = True
    test_client = app.test_client()
    # Every request must look like it arrived at the AP address, or the captive
    # portal will redirect it.
    test_client.environ_base["HTTP_HOST"] = config["ap"]["address"]
    return test_client
