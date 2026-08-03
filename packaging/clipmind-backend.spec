# -*- mode: python ; coding: utf-8 -*-
"""
ClipMind 后端 PyInstaller 打包脚本

用法：
    pyinstaller packaging/clipmind-backend.spec --noconfirm

输出：dist/clipmind-backend/（onedir 模式，启动更快）

注意：
- Playwright 浏览器二进制不打包，需在 CI 中单独安装或首次运行时下载
- ffmpeg 需由用户自行安装或随应用分发
"""

import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

block_cipher = None

# 项目根目录（clipmind/），spec 文件位于 packaging/ 子目录
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))

# === 收集动态导入的子模块 ===
hiddenimports = []

# 应用自身代码（app 包及其子模块）。
# 虽然 Analysis 会从 app/main.py 静态追踪，但函数内部的延迟导入
# （如 asr.py 中的 faster_whisper、douyin_auth.py 中的 playwright）扫不到。
# 显式 collect_submodules("app") 确保 routers / services / models 全部入包。
hiddenimports += collect_submodules("app")

# FastAPI / Starlette / Pydantic
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("pydantic")
hiddenimports += collect_submodules("pydantic_settings")
hiddenimports += collect_submodules("pydantic_core")

# Uvicorn（ASGI 服务器）
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

# SQLAlchemy + 驱动
hiddenimports += [
    "sqlalchemy",
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.aiosqlite",
    "aiosqlite",
]

# LangChain 全套
hiddenimports += collect_submodules("langchain")
hiddenimports += collect_submodules("langchain_core")
hiddenimports += collect_submodules("langchain_community")
hiddenimports += collect_submodules("langchain_openai")
hiddenimports += collect_submodules("langchain_chroma")

# ChromaDB
hiddenimports += collect_submodules("chromadb")

# === chromadb 依赖（独立包，必须单独 collect）===
# chromadb 0.5.x 内部用 importlib.metadata.entry_points() 动态加载 components，
# 且其核心依赖（chroma_hnswlib/duckdb）是独立包，collect_submodules("chromadb")
# 不会跨包收集。漏收会导致入库时 ModuleNotFoundError 或 native 库缺失。
# chroma_hnswlib：HNSW 向量索引核心（C 扩展），MMR 检索和向量写入必用
#   注意：PyPI 包名是 chroma-hnswlib，但 import 名是 hnswlib
# duckdb：SQL 元数据后端（C 扩展），collection.get/count/delete 必用
# pypika：SQL 构造，chromadb.db.mixins 顶层导入
# posthog/opentelemetry：chromadb 启动期 import 的遥测模块
# bcrypt：认证（cffi 扩展）
hiddenimports += collect_submodules("hnswlib")
hiddenimports += collect_submodules("duckdb")
hiddenimports += collect_submodules("pypika")
hiddenimports += collect_submodules("posthog")
hiddenimports += collect_submodules("opentelemetry")
hiddenimports += collect_submodules("bcrypt")
hiddenimports += collect_submodules("tqdm")

# chromadb 内部 entry_points 加载的关键子模块（显式列出，防止 collect_submodules 漏收）
# 这些子模块在 chromadb 0.5.x 中通过 System() 动态加载，PyInstaller 静态分析看不到。
#
# 重要：下方列表已针对 chromadb==0.5.15 逐一 import 验证，并经运行时 sys.modules
# 监控（Chroma() 创建 + count() + similarity_search）确认覆盖完整。
# 旧版本（v0.3.9 及之前）列过 local_hnswlib / telemetry.posthog / auth.providers.simple
# 等模块名，在 0.5.15 中均不存在，导致打包后仍报 ModuleNotFoundError，本次已修正。
# 参考：https://github.com/chroma-core/chroma/issues/4092
hiddenimports += [
    "chromadb.config",
    "chromadb.api.models.Collection",
    "chromadb.api.segment",
    "chromadb.utils.embedding_functions",
    # --- issue #4092 报告的模块（0.5.15 仍存在）---
    "chromadb.execution.executor.local",
    "chromadb.db.impl",
    "chromadb.db.impl.sqlite",
    "chromadb.migrations",
    "chromadb.migrations.embeddings_queue",
    "chromadb.segment.impl.manager",
    "chromadb.segment.impl.manager.local",
    "chromadb.segment.impl.metadata",
    "chromadb.segment.impl.metadata.sqlite",
    "chromadb.segment.impl.vector",
    "chromadb.telemetry.product.posthog",
    "chromadb.rate_limit.simple_rate_limit",
    # --- 0.5.15 运行时监控发现的额外漏收模块 ---
    "chromadb.db.mixins",
    "chromadb.db.mixins.embeddings_queue",
    "chromadb.db.mixins.sysdb",
    "chromadb.execution.executor",
    "chromadb.execution.executor.abstract",
    "chromadb.execution.expression",
    "chromadb.execution.expression.operator",
    "chromadb.execution.expression.plan",
    "chromadb.migrations.metadb",
    "chromadb.migrations.sysdb",
    "chromadb.segment.impl.vector.batch",
    "chromadb.segment.impl.vector.brute_force_index",
    "chromadb.segment.impl.vector.hnsw_params",
    "chromadb.segment.impl.vector.local_hnsw",
    "chromadb.segment.impl.vector.local_persistent_hnsw",
    # 注意：chromadb.quota.simple_quota_enforcer 在 0.4.x 存在，0.5.15 已移除，故不列入。
]

# analytics（posthog 间接依赖），仅在已安装时加入，避免 CI 环境下 spec 抛错。
import importlib.util
if importlib.util.find_spec("analytics") is not None:
    hiddenimports += ["analytics"]

# OpenAI / DashScope
# 显式列出 asr.py 顶层导入的子模块，避免 collect_submodules 漏收
hiddenimports += collect_submodules("openai")
hiddenimports += collect_submodules("dashscope")
hiddenimports += [
    "dashscope.audio.asr",
    "dashscope.common.utils",
]

# Playwright（仅 Python 包，浏览器二进制单独管理）
hiddenimports += collect_submodules("playwright")

# faster-whisper + ctranslate2（本地 ASR 推理）
# asr.py 中 `from faster_whisper import WhisperModel` 是延迟导入，
# PyInstaller 静态分析扫不到，必须显式 collect_submodules。
# ctranslate2 是 faster-whisper 的 C++ 后端，带 .dll/.so/.pyd 动态库，
# 不 collect_dynamic_libs 会导致运行时 ImportError 或找不到符号。
hiddenimports += collect_submodules("faster_whisper")
hiddenimports += collect_submodules("ctranslate2")
hiddenimports += collect_submodules("tokenizers")  # faster-whisper 分词器依赖
hiddenimports += collect_submodules("onnxruntime")  # 部分模型走 onnx 后端

# 其他
hiddenimports += [
    "httpx",
    "anyio",
    "anyio._backends",
    "anyio._backends._asyncio",
    "sniffio",
    "h11",
    "cryptography",
    "loguru",
    "qrcode",
    "PIL",
    "aiofiles",
    "dotenv",
    "tzdata",  # zoneinfo 时区数据，缺失会触发 PyInstaller WARNING
]

# === 收集数据文件 ===
datas = []
datas += collect_data_files("chromadb")
# chromadb 依赖包的数据文件（hnswlib 配置、duckdb 配置等）
datas += collect_data_files("hnswlib")
datas += collect_data_files("duckdb")
datas += collect_data_files("opentelemetry")
datas += collect_data_files("langchain")
datas += collect_data_files("langchain_core")
datas += collect_data_files("dashscope")
# Playwright 驱动文件（node 可执行文件 + 协议定义），运行时必需
datas += collect_data_files("playwright")
# tzdata 带的时区数据文件
datas += collect_data_files("tzdata")
# faster-whisper / ctranslate2 自带的数据文件（如 ctranslate2 的线程池配置）
datas += collect_data_files("faster_whisper")
datas += collect_data_files("ctranslate2")

# === 收集 native 动态库 ===
# ChromaDB / onnxruntime 依赖 .dll/.so，仅收集 Python 子模块会漏掉二进制
binaries = []
binaries += collect_dynamic_libs("chromadb")
# chromadb 依赖的 native 库（C 扩展 .pyd/.dll）
# hnswlib 是 HNSW 向量索引核心，duckdb 是 SQL 元数据后端
# 缺失会导致 MMR 检索和 collection.get/count 直接崩溃
binaries += collect_dynamic_libs("hnswlib")
binaries += collect_dynamic_libs("duckdb")
binaries += collect_dynamic_libs("bcrypt")
binaries += collect_dynamic_libs("onnxruntime")
# ctranslate2 的 C++ 后端库（libctranslate2.so / ctranslate2.dll），
# 缺失会直接 ImportError: libctranslate2.so 无法加载
binaries += collect_dynamic_libs("ctranslate2")

# 前端静态导出产物（frontend/out/）。
# 打包后落到 dist/clipmind-backend/frontend/out/，与 exe 同目录，
# 供 main.py 用 sys.executable 定位并托管为同源 SPA。
# 必须在 PyInstaller 执行前先 npm run build 生成 out/ 目录。
_FRONTEND_OUT = os.path.join(PROJECT_ROOT, "frontend", "out")
if os.path.isdir(_FRONTEND_OUT):
    datas += [(_FRONTEND_OUT, "frontend/out")]
else:
    # 前端未构建时打印明确错误，避免静默打出无前端的安装包
    raise SystemExit(
        "[clipmind-backend.spec] frontend/out not found at %s. "
        "Run `npm run build` in clipmind/frontend before PyInstaller."
        % _FRONTEND_OUT
    )


a = Analysis(
    [os.path.join(PROJECT_ROOT, "app", "main.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "pytest_asyncio",
        "pytest_cov",
        "tests",
        "test",
        "unittest",
        "matplotlib",
        "notebook",
        "IPython",
        "jupyter",
        # chromadb 可选依赖（分布式模式），桌面应用不用
        # 不排除会导致 chromadb 条件导入时报 ModuleNotFoundError
        "kubernetes",
        "pulsar",
        "mypy",
        "ruff",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="clipmind-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # 保留控制台输出，Tauri 会转发日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="clipmind-backend",
)
