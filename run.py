"""
ClipMind 启动脚本

在 uvicorn 创建事件循环之前设置 ProactorEventLoop，
解决 Windows 上 Playwright 启动浏览器报 NotImplementedError 的问题。

用法：
    python run.py           # 普通模式
    python run.py --reload  # 热重载模式
"""
import asyncio
import sys

# 关键：必须在 import uvicorn 之前设置事件循环策略
# uvicorn 创建 worker 事件循环时会读取此策略
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import uvicorn

if __name__ == "__main__":
    reload = "--reload" in sys.argv
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=reload,
        log_level="info",
    )
