# ClipMind

多平台视频知识库 — 把你在 B 站、抖音收藏的视频变成可检索、可对话的个人知识库。

自动拉取收藏内容 → 语音转写（ASR）→ 向量检索 → RAG 对话问答。

## 功能特性

- **多平台支持**：B 站扫码登录读取收藏夹、抖音扫码登录同步喜欢/收藏
- **自动语音转写**：支持 DashScope ASR 和本地 faster-whisper 两种模式
- **语义检索**：基于向量检索 + BM25 关键词检索的混合检索，RRF 融合排序
- **RAG 对话问答**：基于检索结果生成回答，支持来源追溯
- **Markdown 导出**：将视频内容导出为 Markdown 笔记
- **本地存储**：SQLite + ChromaDB，数据完全在本地
- **Tauri 桌面端**（实验性）：可打包为桌面应用

## 技术架构

```
视频平台 API → 内容下载 → ASR 语音转写 → 文本分块 → 向量嵌入
                                                        ↓
用户提问 → 混合检索（向量+BM25） → RRF 融合 → LLM 生成回答
                                                        ↓
                                               来源追溯 + Markdown 导出
```

## 快速开始

### 前置要求

- Python 3.10+
- Node.js 18+
- ffmpeg（ASR 音频处理依赖）

#### 安装 ffmpeg

- **Windows**：下载安装包后将 `bin` 目录加入 PATH
- **macOS**：`brew install ffmpeg`
- **Linux**：`apt/yum/pacman` 安装 `ffmpeg`

### 1. 克隆仓库

```bash
git clone https://github.com/YOUR_USERNAME/ClipMind.git
cd ClipMind
```

### 2. 后端配置

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
playwright install chromium

# 配置环境变量
cp .env.example .env
# 编辑 .env，按需填写 API Key 等配置
```

### 3. 前端配置

```bash
cd frontend
npm install
```

### 4. 启动服务

后端：

```bash
python run.py
# 或：python -m uvicorn app.main:app --reload
```

前端：

```bash
cd frontend
npm run dev
```

- 前端页面：http://localhost:3000
- 后端 API 文档：http://localhost:8000/docs

### Docker 部署

后端：

```bash
cp .env.example .env
# 编辑 .env
docker build -t clipmind-backend .
docker run -p 8000:8000 --env-file .env -v $(pwd)/data:/app/data clipmind-backend
```

前端：

```bash
cd frontend
docker build -t clipmind-frontend .
docker run -p 3000:3000 clipmind-frontend
```

## 配置说明

ClipMind 支持三种配置方式，优先级从高到低：

1. **应用内设置面板**（settings.json）— 运行时修改，即时生效
2. **环境变量**（.env 文件）— 启动时加载
3. **默认值** — 代码内置

### LLM 配置

| Provider | 说明 | 需要配置 |
|----------|------|----------|
| `api` | OpenAI 兼容接口（OpenAI / DashScope / 第三方） | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_MODEL` |
| `ollama` | 本地 Ollama 模型 | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` |

### Embedding 配置

| Provider | 说明 | 需要配置 |
|----------|------|----------|
| `openai` | OpenAI Embedding API | `EMBEDDING_API_KEY`, `EMBEDDING_MODEL` |
| `dashscope` | 阿里云 DashScope | `DASHSCOPE_API_KEY` |
| `ollama` | 本地 Ollama | `OLLAMA_BASE_URL` |
| `nvidia` | NVIDIA Embedding API | `EMBEDDING_API_KEY` |
| `local` | 本地模型（bge/m3e） | 通过模型市场下载 |

### ASR 配置

| Provider | 说明 | 需要配置 |
|----------|------|----------|
| `dashscope` | 阿里云 DashScope ASR | `ASR_API_KEY`, `ASR_MODEL` |
| `local` | 本地 faster-whisper | `ASR_MODEL_LOCAL`（tiny/base/small/medium/large-v3） |

> 本地 ASR 模式不需要 API Key，首次使用时会自动从 HuggingFace 下载模型。

## 目录结构

```
ClipMind/
├── app/                    # 后端逻辑（FastAPI）
│   ├── routers/            # API 路由
│   │   ├── auth.py         # 登录认证
│   │   ├── chat.py         # 对话问答
│   │   ├── knowledge.py    # 知识库管理
│   │   ├── douyin.py       # 抖音相关 API
│   │   ├── favorites.py    # B站收藏夹
│   │   ├── settings.py     # 应用设置
│   │   └── ...
│   ├── services/           # 业务逻辑
│   │   ├── asr.py          # 语音转写
│   │   ├── rag.py          # RAG 检索增强
│   │   ├── browser_pool.py # 浏览器池
│   │   ├── ingest_pipeline.py # 入库流水线
│   │   └── ...
│   ├── config.py           # 配置管理
│   ├── main.py             # 应用入口
│   └── models.py           # 数据模型
├── frontend/               # 前端界面（Next.js + Tailwind）
│   ├── app/                # Next.js App Router
│   ├── components/         # React 组件
│   └── lib/                # API 客户端
├── src-tauri/              # Tauri 桌面端（实验性）
├── packaging/              # PyInstaller 打包配置
├── skills/                 # AI 助手 Skills
├── data/                   # 运行时数据（gitignored）
├── requirements.txt
├── run.py                  # 启动脚本
└── .env.example            # 环境变量模板
```

## 技术栈

| 层 | 技术 |
|----|------|
| 后端框架 | FastAPI + Uvicorn |
| LLM 框架 | LangChain |
| 向量数据库 | ChromaDB |
| 关系数据库 | SQLite (aiosqlite) |
| ASR | DashScope / faster-whisper |
| 前端框架 | Next.js 16 + React 19 |
| 样式 | Tailwind CSS |
| 桌面端 | Tauri 2（实验性） |
| 浏览器自动化 | Playwright |

## Tauri 桌面端（实验性）

ClipMind 支持通过 Tauri 打包为桌面应用，当前处于实验阶段。

```bash
# 前提：已安装 Rust 和 Tauri CLI
cd src-tauri
cargo tauri build
```

> 注意：桌面端打包需要先构建前端（`npm run build`）和后端（PyInstaller）。

## 开发指南

### 后端开发

```bash
# 热重载模式
python run.py --reload

# 运行测试
pytest
```

### 前端开发

```bash
cd frontend
npm run dev    # 开发模式
npm run build  # 生产构建
npm run lint   # 代码检查
```

## License

[MIT](LICENSE)