"""加载 Agent 和模型配置。"""

from app.config.loader import CONFIG_DIR, load_yaml
from app.runtime.config import AgentDefinitions

cfg = AgentDefinitions.model_validate(load_yaml(CONFIG_DIR / "agents.yaml"))
