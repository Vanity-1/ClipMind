# ClipMind Frontend

ClipMind 前端界面，基于 Next.js + Tailwind CSS 构建。

## 开发

```bash
npm install
npm run dev
```

打开 http://localhost:3000 查看页面。

## 构建

```bash
npm run build    # 生产构建
npm run start    # 生产模式启动
```

## 主要组件

| 组件 | 功能 |
|------|------|
| `ChatPanel` | RAG 对话问答界面 |
| `DouyinPanel` | 抖音收藏管理与同步 |
| `SettingsPanel` | 应用设置（LLM/Embedding/ASR 配置） |
| `RagManagementPanel` | 知识库入库管理 |
| `ModelMarketPanel` | 本地模型市场 |
| `LoginModal` | B站/抖音扫码登录 |
| `SourcesPanel` | 检索来源展示 |

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `NEXT_PUBLIC_API_URL` | 后端 API 地址 | `http://localhost:8000` |

## Docker

```bash
docker build -t clipmind-frontend .
docker run -p 3000:3000 clipmind-frontend
```