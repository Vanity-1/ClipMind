"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  modelMarketApi,
  type CatalogModel,
  type ModelCategory,
  type ModelMarketEvent,
} from "@/lib/api";

interface ModelMarketPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

/** 单个模型的运行时状态（由 SSE / catalog 合并而来） */
interface ModelState {
  downloaded: boolean;
  active: boolean;
  downloading: boolean;
  onnxMissing: boolean;
  progress: number;
  downloaded_mb: number;
  total_mb: number;
  error: string | null;
}

const CATEGORY_LABEL: Record<ModelCategory, string> = {
  llm: "LLM 对话模型",
  embedding: "向量嵌入模型",
  asr: "语音转写 (ASR)",
};

const CATEGORY_ICON: Record<ModelCategory, string> = {
  llm: "💬",
  embedding: "🔢",
  asr: "🎤",
};

function formatSize(mb: number): string {
  if (mb >= 1024) return `${(mb / 1024).toFixed(1)} GB`;
  return `${Math.round(mb)} MB`;
}

function defaultModelState(): ModelState {
  return {
    downloaded: false,
    active: false,
    downloading: false,
    onnxMissing: false,
    progress: 0,
    downloaded_mb: 0,
    total_mb: 0,
    error: null,
  };
}

export default function ModelMarketPanel({ isOpen, onClose }: ModelMarketPanelProps) {
  const [loading, setLoading] = useState(true);
  const [models, setModels] = useState<CatalogModel[]>([]);
  const [ollamaInstalled, setOllamaInstalled] = useState(true);
  const [ollamaError, setOllamaError] = useState("");
  // model_id -> ModelState
  const [states, setStates] = useState<Record<string, ModelState>>({});
  const [toast, setToast] = useState<{ type: "success" | "error" | "info"; msg: string } | null>(null);
  // 弹窗确认（切换/删除模型）
  const [confirmDialog, setConfirmDialog] = useState<{
    title: string;
    message: string;
    confirmText: string;
    onConfirm: () => void;
  } | null>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);

  // 加载 catalog
  const loadCatalog = useCallback(async () => {
    setLoading(true);
    try {
      const data = await modelMarketApi.getCatalog();
      setModels(data.models);
      setOllamaInstalled(data.ollama_installed);
      setOllamaError(data.ollama_error || "");
      // 用 catalog 返回的状态初始化 states
      const newStates: Record<string, ModelState> = {};
      for (const m of data.models) {
        newStates[m.id] = {
          ...defaultModelState(),
          downloaded: !!m.downloaded,
          active: !!m.active,
          downloading: !!m.downloading,
          onnxMissing: !!m.onnx_missing,
        };
      }
      setStates(newStates);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "加载模型清单失败";
      showToast("error", msg);
    } finally {
      setLoading(false);
    }
  }, []);

  // 显示 toast
  const showToast = (type: "success" | "error" | "info", msg: string) => {
    setToast({ type, msg });
    setTimeout(() => setToast(null), 3500);
  };

  // SSE 订阅
  useEffect(() => {
    if (!isOpen) return;
    // 打开时先加载一次 catalog
    loadCatalog();
    // 订阅 SSE
    const unsub = modelMarketApi.subscribeEvents((event: ModelMarketEvent) => {
      handleSSEEvent(event);
    });
    unsubscribeRef.current = unsub;
    return () => {
      if (unsubscribeRef.current) {
        unsubscribeRef.current();
        unsubscribeRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen]);

  // 处理 SSE 事件
  const handleSSEEvent = useCallback((event: ModelMarketEvent) => {
    if (event.type === "snapshot" && event.tasks) {
      // 初始快照：恢复所有任务状态
      setStates((prev) => {
        const next = { ...prev };
        for (const t of event.tasks!) {
          if (next[t.model_id]) {
            next[t.model_id] = {
              ...next[t.model_id],
              downloading: t.status === "pending" || t.status === "downloading",
              progress: t.progress,
              downloaded_mb: t.downloaded_mb,
              total_mb: t.total_mb,
              error: t.status === "failed" ? t.error : null,
            };
          }
        }
        return next;
      });
      return;
    }

    const modelId = event.model_id;
    if (!modelId) return;

    setStates((prev) => {
      const cur = prev[modelId] || defaultModelState();
      const next = { ...prev };

      if (event.type === "started") {
        next[modelId] = { ...cur, downloading: true, progress: 0, error: null };
      } else if (event.type === "progress") {
        next[modelId] = {
          ...cur,
          downloading: true,
          progress: event.progress || 0,
          downloaded_mb: event.downloaded_mb || 0,
          total_mb: event.total_mb || 0,
        };
      } else if (event.type === "completed") {
        next[modelId] = {
          ...cur,
          downloading: false,
          downloaded: true,
          progress: 1.0,
          error: null,
        };
        showToast("success", `模型下载完成：${modelId}`);
        // 下载完成后刷新 catalog 以同步状态
        setTimeout(() => loadCatalog(), 300);
      } else if (event.type === "failed") {
        next[modelId] = {
          ...cur,
          downloading: false,
          error: event.error || "下载失败",
        };
        showToast("error", `${modelId} 下载失败：${event.error || "未知错误"}`);
      } else if (event.type === "cancelled") {
        next[modelId] = {
          ...cur,
          downloading: false,
          progress: 0,
        };
        showToast("info", `已取消下载：${modelId}`);
      }
      return next;
    });
  }, [loadCatalog]);

  // 触发下载
  const handleDownload = async (modelId: string) => {
    try {
      const result = await modelMarketApi.download(modelId);
      if (!result.ok) {
        showToast("error", result.error || "下载失败");
        return;
      }
      showToast("info", "下载已开始...");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "下载请求失败";
      showToast("error", msg);
    }
  };

  // 取消下载
  const handleCancel = async (modelId: string) => {
    try {
      await modelMarketApi.cancel(modelId);
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "取消失败");
    }
  };

  // 应用模型（弹窗确认后执行）
  const handleApplyClick = (model: CatalogModel) => {
    const cur = states[model.id];
    setConfirmDialog({
      title: `启用 ${model.display_name}`,
      message: `是否将此模型设为当前${CATEGORY_LABEL[model.category]}？设置将立即生效。`,
      confirmText: "启用",
      onConfirm: () => {
        setConfirmDialog(null);
        doApply(model.id);
      },
    });
    // 若是向量模型切换，提前告知维度风险（后端会校验）
    if (model.category === "embedding" && cur?.active === false) {
      // 后端会返回 dim_mismatch，前端靠结果处理
    }
  };

  const doApply = async (modelId: string) => {
    try {
      const result = await modelMarketApi.apply(modelId);
      if (!result.ok) {
        if (result.code === "dim_mismatch") {
          // 维度不匹配，弹窗提示用户清空已入库内容
          setConfirmDialog({
            title: "维度不匹配，无法切换",
            message: result.error || "向量维度不一致，请先在知识库页清空已入库内容后再切换。",
            confirmText: "我知道了",
            onConfirm: () => setConfirmDialog(null),
          });
        } else {
          showToast("error", result.error || "启用失败");
        }
        return;
      }
      showToast("success", "已切换为当前模型");
      // 刷新状态
      await loadCatalog();
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "启用请求失败");
    }
  };

  // 删除模型（弹窗确认）
  const handleDeleteClick = (model: CatalogModel) => {
    setConfirmDialog({
      title: `删除 ${model.display_name}`,
      message: `将从本地删除此模型文件，释放磁盘空间。此操作不可撤销。`,
      confirmText: "删除",
      onConfirm: () => {
        setConfirmDialog(null);
        doDelete(model.id);
      },
    });
  };

  const doDelete = async (modelId: string) => {
    try {
      const result = await modelMarketApi.delete(modelId);
      if (!result.ok) {
        showToast("error", result.error || "删除失败");
        return;
      }
      showToast("success", "模型已删除");
      await loadCatalog();
    } catch (err) {
      showToast("error", err instanceof Error ? err.message : "删除请求失败");
    }
  };

  if (!isOpen) return null;

  // 按类别分组
  const groups: Record<ModelCategory, CatalogModel[]> = {
    llm: models.filter((m) => m.category === "llm"),
    embedding: models.filter((m) => m.category === "embedding"),
    asr: models.filter((m) => m.category === "asr"),
  };

  return (
    <>
      <div className="settings-overlay">
        <div className="settings-backdrop" onClick={onClose} />
        <aside className="settings-panel model-market-panel" role="dialog" aria-label="模型市场">
          {/* Header */}
          <div className="settings-header">
            <div>
              <div className="settings-header-title">📦 模型市场</div>
              <div className="settings-header-subtitle">本地模型一键下载 · 自动配置</div>
            </div>
            <button className="btn-icon" onClick={onClose} title="关闭" aria-label="关闭">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Body */}
          <div className="settings-body">
            {/* Ollama 未安装提示 */}
            {!ollamaInstalled && (
              <div className="model-market-warning">
                <div className="model-market-warning-title">⚠️ 未检测到 Ollama</div>
                <div className="model-market-warning-desc">
                  LLM 对话模型需要 Ollama 运行时。请先安装 Ollama：
                  <a href="https://ollama.com/download" target="_blank" rel="noopener noreferrer" className="model-market-link">
                    前往下载 →
                  </a>
                  {ollamaError && (
                    <div className="model-market-warning-detail">{ollamaError}</div>
                  )}
                </div>
              </div>
            )}

            {loading ? (
              <div style={{ textAlign: "center", padding: "40px 0", color: "var(--muted)" }}>
                加载中...
              </div>
            ) : (
              <>
                {(["llm", "embedding", "asr"] as ModelCategory[]).map((cat) => (
                  <div key={cat} className="settings-section">
                    <div className="settings-section-title">
                      {CATEGORY_ICON[cat]} {CATEGORY_LABEL[cat]}
                    </div>
                    <div className="model-market-grid">
                      {groups[cat].map((model) => {
                        const st = states[model.id] || defaultModelState();
                        return (
                          <ModelCard
                            key={model.id}
                            model={model}
                            state={st}
                            onDownload={() => handleDownload(model.id)}
                            onCancel={() => handleCancel(model.id)}
                            onApply={() => handleApplyClick(model)}
                            onDelete={() => handleDeleteClick(model)}
                          />
                        );
                      })}
                    </div>
                  </div>
                ))}
              </>
            )}
          </div>

          {/* Toast */}
          {toast && (
            <div className={`model-market-toast model-market-toast-${toast.type}`}>
              {toast.msg}
            </div>
          )}

          {/* 确认弹窗 */}
          {confirmDialog && (
            <div className="confirm-overlay">
              <div className="confirm-dialog">
                <div className="confirm-title">{confirmDialog.title}</div>
                <div className="confirm-message">{confirmDialog.message}</div>
                <div className="confirm-actions">
                  {confirmDialog.confirmText !== "我知道了" && (
                    <button className="btn-secondary" onClick={() => setConfirmDialog(null)}>
                      取消
                    </button>
                  )}
                  <button
                    className="btn btn-primary"
                    onClick={confirmDialog.onConfirm}
                    style={
                      confirmDialog.confirmText === "删除"
                        ? { background: "var(--danger)" }
                        : undefined
                    }
                  >
                    {confirmDialog.confirmText}
                  </button>
                </div>
              </div>
            </div>
          )}
        </aside>
      </div>
    </>
  );
}

// ============================================================================
// 单个模型卡片
// ============================================================================

interface ModelCardProps {
  model: CatalogModel;
  state: ModelState;
  onDownload: () => void;
  onCancel: () => void;
  onApply: () => void;
  onDelete: () => void;
}

function ModelCard({ model, state, onDownload, onCancel, onApply, onDelete }: ModelCardProps) {
  const showProgress = state.downloading;
  const progressPct = Math.round(state.progress * 100);

  return (
    <div className={`model-card ${state.active ? "model-card-active" : ""}`}>
      <div className="model-card-header">
        <div className="model-card-name">
          {model.display_name}
          {model.recommended && <span className="model-card-badge">推荐</span>}
          {state.active && state.downloaded && (
            <span className="model-card-badge model-card-badge-active">已启用</span>
          )}
          {state.active && !state.downloaded && (
            <span className="model-card-badge" style={{ background: "var(--warning-bg, #fef3c7)", color: "var(--warning-text, #92400e)" }}>
              需重新下载
            </span>
          )}
        </div>
        <div className="model-card-size">{formatSize(model.size_mb)}</div>
      </div>

      <div className="model-card-desc">{model.description}</div>

      {/* 进度条（下载中显示） */}
      {showProgress && (
        <div className="model-card-progress">
          <div className="model-card-progress-bar">
            <div
              className="model-card-progress-fill"
              style={{ width: `${progressPct}%` }}
            />
          </div>
          <div className="model-card-progress-text">
            {progressPct}%
            {state.total_mb > 0 && (
              <span style={{ marginLeft: 8, color: "var(--muted)" }}>
                {formatSize(state.downloaded_mb)} / {formatSize(state.total_mb)}
              </span>
            )}
          </div>
        </div>
      )}

      {/* 错误提示 */}
      {state.error && !state.downloading && (
        <div className="model-card-error">{state.error}</div>
      )}

      {/* 模型标记为已启用但文件缺失 */}
      {state.active && !state.downloaded && !state.downloading && (
        <div className="model-card-error" style={{ background: "var(--warning-bg, #fef3c7)", color: "var(--warning-text, #92400e)" }}>
          模型文件缺失，请重新下载后才能使用
        </div>
      )}

      {/* 已下载但缺少 ONNX 权重（旧版本下载的模型，打包环境无 torch 时必须走 ONNX 推理） */}
      {state.downloaded && state.onnxMissing && !state.downloading && (
        <div className="model-card-error" style={{ background: "var(--warning-bg, #fef3c7)", color: "var(--warning-text, #92400e)" }}>
          该模型缺少 ONNX 权重，请点击「重新下载」补齐后使用本地向量功能
        </div>
      )}

      {/* 操作按钮区 */}
      <div className="model-card-actions">
        {state.downloading ? (
          <button className="btn-secondary model-card-btn" onClick={onCancel}>
            取消下载
          </button>
        ) : state.downloaded ? (
          <>
            {state.active && (
              <span className="model-card-active-label">✓ 当前使用中</span>
            )}
            {!state.active && (
              <button className="btn btn-primary model-card-btn" onClick={onApply}>
                启用
              </button>
            )}
            {state.active && (
              <button
                className="btn-secondary model-card-btn"
                onClick={onDownload}
                title="重新下载模型文件（覆盖现有）"
              >
                重新下载
              </button>
            )}
            <button
              className="btn-secondary model-card-btn model-card-btn-danger"
              onClick={onDelete}
            >
              删除
            </button>
          </>
        ) : (
          <button className="btn btn-primary model-card-btn" onClick={onDownload}>
            下载
          </button>
        )}
      </div>
    </div>
  );
}
