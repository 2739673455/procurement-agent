"""读取 agents.yaml，解析为 Runtime 的模型和角色配置。"""

from app.config.loader import CONFIG_DIR, load_yaml
from app.runtime.config import AgentDefinitions

cfg = AgentDefinitions.model_validate(load_yaml(CONFIG_DIR / "agents.yaml"))
