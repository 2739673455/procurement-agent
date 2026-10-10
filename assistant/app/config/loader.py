"""提供配置目录与 YAML 加载，统一解析 .env 和环境变量插值。"""

from pathlib import Path

from dotenv import load_dotenv
from omegaconf import OmegaConf

ROOT_DIR = Path(__file__).resolve().parents[2]  # Assistant 服务根目录。
CONFIG_DIR = ROOT_DIR / "conf"  # YAML 配置和 .env 所在目录。


def load_yaml(path: Path) -> object:
    """读取配置文件旁的 .env，进程环境变量优先，并展开 YAML 插值。"""
    load_dotenv(path.parent / ".env", override=False)
    return OmegaConf.to_container(OmegaConf.load(path), resolve=True)
