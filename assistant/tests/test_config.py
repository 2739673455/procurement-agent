import pytest
from omegaconf import OmegaConf
from pydantic import ValidationError

from app.agent.model import ChatCredential
from app.config import app_config
from app.errors.agent import AgentError


@pytest.mark.parametrize("field", ["model", "base_url", "api_key"])
def test_loading_config_rejects_empty_model_settings(tmp_path, field):
    config = OmegaConf.load(app_config.CONFIG_FILE)
    key = config.lm_config.active
    config.lm_config.models[key][field] = ""
    path = tmp_path / "app_config.yaml"
    OmegaConf.save(config, path)

    with pytest.raises(ValidationError) as error:
        app_config.load_config(path)
    assert error.value.errors()[0]["loc"] == ("lm_config", "models", key, field)


def test_deleted_model_reference_reports_configuration_error(monkeypatch):
    monkeypatch.setattr(app_config.cfg.lm_config, "models", {})
    credential = ChatCredential(id="test", name="test", config_key="removed-model")
    with pytest.raises(AgentError, match="模型配置"):
        _ = credential.settings
