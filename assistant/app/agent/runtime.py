"""Deep Agents 采购智能体，凭据仅保存在本次调用的上下文中。"""

from deepagents import create_deep_agent

from app.agent.context import TurnContext
from app.agent.model import create_model
from app.agent.prompts import SYSTEM
from app.agent.tools.items import query_items
from app.config import app_config


def build_agent(checkpointer, model=None):
    return create_deep_agent(
        model=model
        if model is not None
        else create_model(app_config.cfg.lm_config.active),
        tools=[query_items],
        system_prompt=SYSTEM,
        context_schema=TurnContext,
        checkpointer=checkpointer,
    )
