"""测试配置独立于本机密钥；外部模型与 ERP 调用由各测试模拟。"""

import os

os.environ["ASSISTANT_PG_PASSWORD"] = "test-only"
os.environ["DEEPSEEK_API_KEY"] = "test-only"
