"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  settingsApi,
  systemApi,
  type AppSettings,
  type SettingsStatus,
} from "@/lib/api";

interface SettingsPanelProps {
  isOpen: boolean;
  onClose: () => void;
  /** 跳转到模型市场的回调（由父组件 page.tsx 控制 ModelMarketPanel 显隐） */
  onOpenModelMarket?: () => void;
}

type ToastType = "success" | "error" | null;

/** 单类测试 UI 状态：testing / ok / error / 未测试 */
type TestUIState = {
  testing?: boolean;
  ok?: boolean;
  error?: string;
  latency_ms?: number;
  cached?: boolean;
};

type CategoryKey = "llm" | "embedding" | "asr";

type TestResults = Partial<Record<CategoryKey, TestUIState>>;

/** 字段到 category 的映射，用于修改字段时重置对应测试状态 */
const FIELD_CATEGORY: Record<string, CategoryKey> = {
  // LLM
  openai_api_key: "llm",
  openai_base_url: "llm",
  llm_model: "llm",
  llm_provider: "llm",
  ollama_base_url: "llm",
  ollama_model: "llm",
  // Embedding
  embedding_api_key: "embedding",
  embedding_base_url: "embedding",
  embedding_model: "embedding",
  embedding_provider: "embedding",
  // ASR
  asr_api_key: "asr",
  asr_model_local: "asr",
  asr_provider: "asr",
  hf_mirror_url: "asr",
};

export default function SettingsPanel({ isOpen, onClose, onOpenModelMarket }: SettingsPanelProps) {
  const [settings, setSettings] = useState<AppSettings>({});
  const [status, setStatus] = useState<SettingsStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ type: ToastType; msg: string } | null>(null);
  // 跟踪用户是否修改过敏感字段（脱敏值不应回写）
  const [touchedKeys, setTouchedKeys] = useState<Set<string>>(new Set());
  // 三类配置实时测试结果
  const [testResults, setTestResults] = useState<TestResults>({});
  // 卸载全部内容（危险操作）
  const [showWipeConfirm, setShowWipeConfirm] = useState(false);
  const [wiping, setWiping] = useState(false);
  // 防抖定时器
  const testTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 加载设置
  const loadSettings = useCallback(async () => {
    setLoading(true);
    try {
      const [data, st] = await Promise.all([
        settingsApi.get(),
        settingsApi.getStatus(),
      ]);
      setSettings(data);
      setStatus(st);
      // 初始化测试结果：基于 status 推断（已配置但未测试时显示"未测试"灰色）
      setTestResults({});
    } catch (err) {
      const msg = err instanceof Error ? err.message : "加载设置失败";
      showToast("error", msg);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) {
      loadSettings();
      setTouchedKeys(new Set());
    }
  }, [isOpen, loadSettings]);

  useEffect(() => {
    return () => {
      if (testTimerRef.current) {
        clearTimeout(testTimerRef.current);
      }
    };
  }, []);

  // Toast 自动消失
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(timer);
  }, [toast]);

  function showToast(type: ToastType, msg: string) {
    setToast({ type, msg });
  }

  // 更新单个字段
  // 修改字段时重置对应 category 的测试状态为"未测试"（移除测试结果）
  // 这样用户能看到旧结果已失效，需要重新测试
  function updateField(key: keyof AppSettings, value: string | number | boolean) {
    setSettings((prev) => ({ ...prev, [key]: value }));
    setTouchedKeys((prev) => new Set(prev).add(key));
    // 重置对应 category 的测试状态
    const category = FIELD_CATEGORY[key as string];
    if (category) {
      setTestResults((prev) => {
        if (!prev[category]) return prev; // 本来就没有结果，无需重置
        const next = { ...prev };
        delete next[category];
        return next;
      });
    }
  }

  // 调用后端测试接口（按键触发，支持单类或全量测试）
  async function runTest(category: CategoryKey | "all") {
    // 构造 payload：敏感字段（api_key）仅在用户修改过（touchedKeys）时才传值，
    // 未修改传空字符串，后端 _resolve 会回退到 settings.json 已保存的真实值。
    // 这避免脱敏值（如 sk-xx****yy，GET /settings 返回）被当作有效 key 传给测试端点，
    // 导致 401 Token is invalid。与后端 _resolve 脱敏兜底形成双重保险。
    const payload: Partial<{
      openai_api_key: string;
      openai_base_url: string;
      llm_model: string;
      embedding_api_key: string;
      embedding_base_url: string;
      embedding_model: string;
      asr_model_local: string;
    }> = {
      // 敏感字段：仅 touchedKeys 包含时传值，否则传空让后端回退到已保存真实值
      openai_api_key: touchedKeys.has("openai_api_key") ? (settings.openai_api_key || "") : "",
      openai_base_url: settings.openai_base_url || "",
      llm_model: settings.llm_model || "",
      embedding_api_key: touchedKeys.has("embedding_api_key") ? (settings.embedding_api_key || "") : "",
      embedding_base_url: settings.embedding_base_url || "",
      embedding_model: settings.embedding_model || "",
      asr_model_local: settings.asr_model_local || "",
    };

    // 过滤掉无需测试的类别（shouldSkipTest 动态判断）
    const allTargets: CategoryKey[] = category === "all"
      ? ["llm", "embedding", "asr"]
      : [category];
    const targets = allTargets.filter((c) => !shouldSkipTest(c));
    if (targets.length === 0) return; // 全部为本地模式，无需测试

    // 标记目标类别为 testing
    setTestResults((prev) => {
      const next = { ...prev };
      for (const t of targets) {
        next[t] = { testing: true };
      }
      return next;
    });

    try {
      // 使用独立测试端点，避免一次测三类造成不必要开销
      const results: Partial<Record<CategoryKey, TestUIState>> = {};
      const promises: Promise<void>[] = [];

      if (targets.includes("llm")) {
        promises.push(
          settingsApi.testLLM({
            openai_api_key: payload.openai_api_key,
            openai_base_url: payload.openai_base_url,
            llm_model: payload.llm_model,
          }).then((r) => {
            results.llm = { ok: r.ok, error: r.error, latency_ms: r.latency_ms };
          }).catch((e) => {
            results.llm = { ok: false, error: e instanceof Error ? e.message : "测试失败" };
          })
        );
      }
      if (targets.includes("embedding")) {
        promises.push(
          settingsApi.testEmbedding({
            embedding_api_key: payload.embedding_api_key,
            embedding_base_url: payload.embedding_base_url,
            embedding_model: payload.embedding_model,
            openai_api_key: payload.openai_api_key,
            openai_base_url: payload.openai_base_url,
          }).then((r) => {
            results.embedding = { ok: r.ok, error: r.error, latency_ms: r.latency_ms };
          }).catch((e) => {
            results.embedding = { ok: false, error: e instanceof Error ? e.message : "测试失败" };
          })
        );
      }
      if (targets.includes("asr")) {
        promises.push(
          settingsApi.testASR({
            asr_model_local: payload.asr_model_local,
          }).then((r) => {
            results.asr = { ok: r.ok, error: r.error, cached: r.cached };
          }).catch((e) => {
            results.asr = { ok: false, error: e instanceof Error ? e.message : "测试失败" };
          })
        );
      }

      await Promise.all(promises);

      setTestResults((prev) => {
        const next = { ...prev };
        for (const t of targets) {
          if (results[t]) {
            next[t] = results[t]!;
          }
        }
        return next;
      });
      // 同步刷新 status（last_test_results 已在后端持久化）
      try {
        const st = await settingsApi.getStatus();
        setStatus(st);
      } catch {
        // 忽略 status 刷新失败
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "测试失败";
      setTestResults((prev) => {
        const next = { ...prev };
        for (const t of targets) {
          next[t] = { ok: false, error: msg };
        }
        return next;
      });
    }
  }

  // 防抖调度测试
  function scheduleTest(category: CategoryKey | "all") {
    if (testTimerRef.current) {
      clearTimeout(testTimerRef.current);
    }
    testTimerRef.current = setTimeout(() => {
      runTest(category);
      testTimerRef.current = null;
    }, 500);
  }

  // 保存设置
  async function handleSave() {
    setSaving(true);
    try {
      // 仅提交用户修改过的字段
      const updates: Partial<AppSettings> = {};
      for (const key of touchedKeys) {
        const value = settings[key as keyof AppSettings];
        if (value !== undefined) {
          updates[key as keyof AppSettings] = value as never;
        }
      }

      if (Object.keys(updates).length === 0) {
        showToast("success", "无修改内容");
        setSaving(false);
        return;
      }

      const resp = await settingsApi.update(updates);
      if (resp.updated) {
        showToast("success", `设置已保存并生效（${resp.fields?.length || 0} 项）`);
        setTouchedKeys(new Set());
        // 重新加载设置（脱敏后的值）
        const data = await settingsApi.get();
        setSettings(data);
        // 保存后触发完整测试，由测试结果驱动状态点
        scheduleTest("all");
      } else {
        showToast("error", resp.message || "保存失败");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "保存失败";
      showToast("error", msg);
    } finally {
      setSaving(false);
    }
  }

  /**
   * 重启应用。
   *
   * 优先尝试调用 Tauri 的 relaunch（需 @tauri-apps/api/process + tauri-plugin-process）；
   * 当前项目未引入该依赖，运行时动态导入会失败，此时回退到重新加载页面。
   * ClipMind 为同源 SPA 架构（前端由后端 8000 端口托管），reload 会让前端重新
   * 初始化并拉取已清空的数据，功能上等同于应用重置。
   *
   * 使用 Function 构造器执行动态 import，避免打包器在构建期解析未安装的包。
   */
  async function relaunchApp(): Promise<void> {
    try {
      const safeImport = new Function(
        "spec",
        "return import(spec)",
      ) as (spec: string) => Promise<{ relaunch?: () => Promise<void> }>;
      const mod = await safeImport("@tauri-apps/api/process");
      if (typeof mod.relaunch === "function") {
        await mod.relaunch();
        return;
      }
    } catch {
      // 包未安装或权限不足，走回退逻辑
    }
    window.location.reload();
  }

  // 卸载全部内容：调用 /system/wipe，成功后提示并重启
  async function handleWipe() {
    setWiping(true);
    try {
      const result = await systemApi.wipe(true);
      if (result.success) {
        showToast("success", "数据已清除，应用将重启...");
        setShowWipeConfirm(false);
        // 留 2 秒让用户看到提示，再重启
        setTimeout(() => {
          relaunchApp().catch((e) => {
            console.error("重启失败:", e);
            showToast("error", "数据已清除，请手动重启应用");
          });
        }, 2000);
      } else {
        showToast("error", result.message || "清理失败");
        setShowWipeConfirm(false);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "清理失败";
      showToast("error", msg);
      setShowWipeConfirm(false);
    } finally {
      setWiping(false);
    }
  }

  // 判断指定类别是否应跳过 API 测试
  // - LLM: Ollama 本地模式无 API 可测
  // - Embedding: 本地模型模式无 API 可测
  // - ASR: DashScope 云端模式无后端测试函数（仅 local 模式可测 faster-whisper）
  // 动态计算：provider 变化时立即生效，无需额外管理 testResults 状态
  function shouldSkipTest(category: CategoryKey): boolean {
    switch (category) {
      case "llm": return settings.llm_provider === "ollama";
      case "embedding": return settings.embedding_provider === "local";
      case "asr": return settings.asr_provider !== "local";
      default: return false;
    }
  }

  // 渲染单类测试状态点（inline style，CSS 类由其他任务处理）
  function renderDot(category: CategoryKey, state?: TestUIState) {
    let bg = "var(--muted-dim)"; // 未测试
    if (shouldSkipTest(category)) bg = "var(--muted)";
    else if (state?.testing) bg = "var(--warning)";
    else if (state?.ok) bg = "var(--success)";
    else if (state?.error) bg = "var(--danger)";
    return (
      <span
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: bg,
          marginRight: 6,
          verticalAlign: "middle",
          animation: state?.testing ? "spin 1s linear infinite" : undefined,
        }}
      />
    );
  }

  // 渲染区块标题右侧的测试状态文字
  function renderStatusText(category: CategoryKey, state?: TestUIState) {
    if (shouldSkipTest(category)) {
      const label = category === "asr" ? "DashScope 云端模式" : "本地模式";
      return <span style={{ color: "var(--muted)", marginLeft: 8, fontSize: 12 }}>— {label}，无需测试</span>;
    }
    if (state?.testing) return <span style={{ color: "var(--warning)", marginLeft: 8, fontSize: 12 }}>⋯ 测试中</span>;
    if (state?.ok) {
      const latency = state.latency_ms != null ? ` (${state.latency_ms}ms)` : "";
      const cachedTag = state.cached === true ? " [本地缓存]" : state.cached === false ? " [网络下载]" : "";
      return <span style={{ color: "var(--success)", marginLeft: 8, fontSize: 12 }}>✓ 测试通过{latency}{cachedTag}</span>;
    }
    if (state?.error) {
      return <span style={{ color: "var(--danger)", marginLeft: 8, fontSize: 12 }}>✗ 失败: {state.error}</span>;
    }
    return <span style={{ color: "var(--muted-dim)", marginLeft: 8, fontSize: 12 }}>— 未测试</span>;
  }

  // 渲染测试按钮
  function renderTestButton(category: CategoryKey) {
    const state = testResults[category];
    const testing = state?.testing;
    return (
      <button
        type="button"
        className="btn-secondary"
        style={{ marginLeft: "auto", padding: "4px 12px", fontSize: 12 }}
        onClick={() => runTest(category)}
        disabled={testing}
        title={`测试${category === "llm" ? "LLM 对话" : category === "embedding" ? "向量嵌入" : "ASR 本地模型"}配置`}
      >
        {testing ? "测试中..." : "测试"}
      </button>
    );
  }

  if (!isOpen) return null;

  return (
    <>
      <div className="settings-overlay">
        <div className="settings-backdrop" onClick={onClose} />
        <aside className="settings-panel" role="dialog" aria-label="应用设置">
          {/* Header */}
          <div className="settings-header">
            <div>
              <div className="settings-header-title">应用设置</div>
              <div className="settings-header-subtitle">Configuration · Hot Reload</div>
            </div>
            <button className="btn-icon" onClick={onClose} title="关闭" aria-label="关闭设置">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Body */}
          <div className="settings-body">
            {loading ? (
              <div style={{ textAlign: "center", padding: "40px 0", color: "var(--muted)" }}>
                加载中...
              </div>
            ) : (
              <>
                {/* 配置状态 */}
                <div className="settings-section">
                  <div className="settings-section-title">配置状态</div>
                  <div className="settings-status-row">
                    {renderDot("llm", testResults.llm)}
                    <span>LLM 对话 {shouldSkipTest("llm") ? "本地模式" : testResults.llm?.ok ? "已配置" : testResults.llm?.error ? "测试失败" : "未测试"}</span>
                  </div>
                  <div className="settings-status-row">
                    {renderDot("embedding", testResults.embedding)}
                    <span>向量嵌入 {shouldSkipTest("embedding") ? "本地模式" : testResults.embedding?.ok ? "已配置" : testResults.embedding?.error ? "测试失败" : "未测试"}</span>
                  </div>
                  <div className="settings-status-row">
                    {renderDot("asr", testResults.asr)}
                    <span>语音转写 {shouldSkipTest("asr") ? "DashScope 模式" : testResults.asr?.ok ? "已配置" : testResults.asr?.error ? "测试失败" : "未测试"}</span>
                  </div>
                  {!status?.configured && (
                    <div className="settings-hint" style={{ color: "var(--warning)", marginTop: 8 }}>
                      请在下方填写 API Key 后保存，设置将立即生效。
                    </div>
                  )}
                  {status?.tested === false && (
                    <div className="settings-hint" style={{ color: "var(--muted)", marginTop: 8 }}>
                      修改配置后点击对应区块的&ldquo;测试&rdquo;按钮验证连通性。
                    </div>
                  )}
                </div>

                {/* LLM 配置 */}
                <div className="settings-section">
                  <div className="settings-section-title" style={{ display: "flex", alignItems: "center" }}>
                    LLM 对话模型
                    {onOpenModelMarket && (
                      <button
                        type="button"
                        className="settings-market-link"
                        onClick={onOpenModelMarket}
                        title="前往模型市场下载本地模型"
                      >
                        📦 模型市场
                      </button>
                    )}
                    {renderStatusText("llm", testResults.llm)}
                    {!shouldSkipTest("llm") && renderTestButton("llm")}
                  </div>

                  {/* Provider 模式选择 */}
                  <div className="settings-field">
                    <label className="settings-label">运行模式</label>
                    <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
                      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        <input
                          type="radio"
                          name="llm_provider"
                          checked={settings.llm_provider !== "ollama"}
                          onChange={() => updateField("llm_provider", "api")}
                        />
                        <span style={{ fontSize: 13 }}>API 模式（OpenAI / DashScope / 兼容接口）</span>
                      </label>
                      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        <input
                          type="radio"
                          name="llm_provider"
                          checked={settings.llm_provider === "ollama"}
                          onChange={() => updateField("llm_provider", "ollama")}
                        />
                        <span style={{ fontSize: 13 }}>Ollama 本地模式</span>
                      </label>
                    </div>
                  </div>

                  {/* API 模式字段 */}
                  {settings.llm_provider !== "ollama" ? (
                    <>
                      <div className="settings-field">
                        <label className="settings-label">API Key</label>
                        <input
                          type="password"
                          className="settings-input"
                          placeholder="sk-..."
                          value={settings.openai_api_key || ""}
                          onChange={(e) => updateField("openai_api_key", e.target.value)}
                        />
                        <div className="settings-hint">支持 OpenAI / DashScope / 兼容接口</div>
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">Base URL</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="https://api.openai.com/v1"
                          value={settings.openai_base_url || ""}
                          onChange={(e) => updateField("openai_base_url", e.target.value)}
                        />
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">对话模型</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="gpt-4-turbo"
                          value={settings.llm_model || ""}
                          onChange={(e) => updateField("llm_model", e.target.value)}
                        />
                      </div>
                    </>
                  ) : (
                    <>
                      {/* Ollama 本地模式字段 */}
                      <div className="settings-field">
                        <label className="settings-label">Ollama Base URL</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="http://localhost:11434"
                          value={settings.ollama_base_url || ""}
                          onChange={(e) => updateField("ollama_base_url", e.target.value)}
                        />
                        <div className="settings-hint">Ollama 服务地址，默认 http://localhost:11434</div>
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">Ollama 模型</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="qwen2.5:7b-instruct"
                          value={settings.ollama_model || ""}
                          onChange={(e) => updateField("ollama_model", e.target.value)}
                        />
                        <div className="settings-hint">
                          从模型市场下载并启用后自动填充，也可手动输入已有模型名
                        </div>
                      </div>
                      <div className="settings-hint" style={{ color: "var(--success)", marginTop: 4 }}>
                        ✓ 当前使用本地 Ollama 模型，无需 API Key
                      </div>
                    </>
                  )}

                  <div className="settings-field">
                    <label className="settings-label">LLM 路由优化</label>
                    <select
                      className="settings-input"
                      value={settings.chat_use_llm_router ? "true" : "false"}
                      onChange={(e) => updateField("chat_use_llm_router", e.target.value === "true")}
                    >
                      <option value="false">关闭（规则路由，更快）</option>
                      <option value="true">开启（LLM 路由，更精准）</option>
                    </select>
                    <div className="settings-hint">开启后使用 LLM 判断问题路由，消耗额外 token</div>
                  </div>
                </div>

                {/* 向量嵌入配置 */}
                <div className="settings-section">
                  <div className="settings-section-title" style={{ display: "flex", alignItems: "center" }}>
                    向量嵌入模型
                    {onOpenModelMarket && (
                      <button
                        type="button"
                        className="settings-market-link"
                        onClick={onOpenModelMarket}
                        title="前往模型市场下载本地模型"
                      >
                        📦 模型市场
                      </button>
                    )}
                    {renderStatusText("embedding", testResults.embedding)}
                    {!shouldSkipTest("embedding") && renderTestButton("embedding")}
                  </div>

                  {/* Provider 模式选择 */}
                  <div className="settings-field">
                    <label className="settings-label">运行模式</label>
                    <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
                      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        <input
                          type="radio"
                          name="embedding_provider"
                          checked={settings.embedding_provider !== "local"}
                          onChange={() => updateField("embedding_provider", "openai")}
                        />
                        <span style={{ fontSize: 13 }}>API 模式（OpenAI / NVIDIA / DashScope）</span>
                      </label>
                      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        <input
                          type="radio"
                          name="embedding_provider"
                          checked={settings.embedding_provider === "local"}
                          onChange={() => updateField("embedding_provider", "local")}
                        />
                        <span style={{ fontSize: 13 }}>本地模型模式（BGE / M3E）</span>
                      </label>
                    </div>
                  </div>

                  {/* API 模式字段 */}
                  {settings.embedding_provider !== "local" ? (
                    <>
                      <div className="settings-field">
                        <label className="settings-label">Embedding API Key</label>
                        <input
                          type="password"
                          className="settings-input"
                          placeholder="留空则复用上方 LLM Key"
                          value={settings.embedding_api_key || ""}
                          onChange={(e) => updateField("embedding_api_key", e.target.value)}
                        />
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">Embedding Base URL</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="https://api.openai.com/v1（NVIDIA 模型留空自动用官方 URL）"
                          value={settings.embedding_base_url || ""}
                          onChange={(e) => updateField("embedding_base_url", e.target.value)}
                        />
                        <div className="settings-hint">NVIDIA 模型（nvidia/开头）留空自动使用 https://integrate.api.nvidia.com/v1</div>
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">嵌入模型</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="text-embedding-3-small"
                          value={settings.embedding_model || ""}
                          onChange={(e) => updateField("embedding_model", e.target.value)}
                        />
                        <div className="settings-hint">NVIDIA NIM 模型示例：nvidia/nv-embedqa-e5-v5</div>
                      </div>
                    </>
                  ) : (
                    <>
                      {/* 本地向量模型模式 */}
                      <div className="settings-field">
                        <label className="settings-label">当前本地模型</label>
                        {settings.embedding_model ? (
                          <div className="settings-hint" style={{ color: "var(--success)" }}>
                            ✓ 已启用本地向量模型：{settings.embedding_model.split(/[\\/]/).pop()}
                          </div>
                        ) : (
                          <div className="settings-hint" style={{ color: "var(--warning)" }}>
                            ⚠️ 尚未启用本地向量模型，请前往模型市场下载并启用
                          </div>
                        )}
                        {onOpenModelMarket && (
                          <button
                            type="button"
                            className="settings-market-link"
                            onClick={onOpenModelMarket}
                            style={{ marginTop: 8 }}
                          >
                            📦 前往模型市场选择模型
                          </button>
                        )}
                      </div>
                      <div className="settings-hint" style={{ color: "var(--muted)" }}>
                        本地向量模型由模型市场管理，下载后点击"启用"自动配置，无需手动填写
                      </div>
                    </>
                  )}
                </div>

                {/* ASR 配置 */}
                <div className="settings-section">
                  <div className="settings-section-title" style={{ display: "flex", alignItems: "center" }}>
                    语音转写 (ASR)
                    {onOpenModelMarket && (
                      <button
                        type="button"
                        className="settings-market-link"
                        onClick={onOpenModelMarket}
                        title="前往模型市场下载本地模型"
                      >
                        📦 模型市场
                      </button>
                    )}
                    {renderStatusText("asr", testResults.asr)}
                    {!shouldSkipTest("asr") && renderTestButton("asr")}
                  </div>

                  {/* Provider 模式选择 */}
                  <div className="settings-field">
                    <label className="settings-label">运行模式</label>
                    <div style={{ display: "flex", gap: 24, flexWrap: "wrap" }}>
                      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        <input
                          type="radio"
                          name="asr_provider"
                          checked={settings.asr_provider !== "local"}
                          onChange={() => updateField("asr_provider", "dashscope")}
                        />
                        <span style={{ fontSize: 13 }}>DashScope 云端转写</span>
                      </label>
                      <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                        <input
                          type="radio"
                          name="asr_provider"
                          checked={settings.asr_provider === "local"}
                          onChange={() => updateField("asr_provider", "local")}
                        />
                        <span style={{ fontSize: 13 }}>本地 faster-whisper</span>
                      </label>
                    </div>
                  </div>

                  {settings.asr_provider !== "local" ? (
                    <>
                      {/* DashScope 云端模式字段 */}
                      <div className="settings-field">
                        <label className="settings-label">DashScope API Key</label>
                        <input
                          type="password"
                          className="settings-input"
                          placeholder="sk-...（留空则使用本地 faster-whisper 转写）"
                          value={settings.asr_api_key || ""}
                          onChange={(e) => updateField("asr_api_key", e.target.value)}
                        />
                        <div className="settings-hint">用于云端 ASR（DashScope paraformer）。留空时自动使用下方本地 faster-whisper 模型转写，无需 API Key</div>
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">DashScope Base URL</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="https://dashscope.aliyuncs.com/api/v1"
                          value={settings.dashscope_base_url || ""}
                          onChange={(e) => updateField("dashscope_base_url", e.target.value)}
                        />
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">ASR 模型</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="paraformer-v2"
                          value={settings.asr_model || ""}
                          onChange={(e) => updateField("asr_model", e.target.value)}
                        />
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">DashScope Recognition 模型</label>
                        <input
                          type="text"
                          className="settings-input"
                          placeholder="paraformer-realtime-v2"
                          value={settings.dashscope_recognition_model || ""}
                          onChange={(e) => updateField("dashscope_recognition_model", e.target.value)}
                        />
                        <div className="settings-hint">DashScope 本地文件直传使用的 Recognition 模型</div>
                      </div>

                      <div className="settings-field">
                        <label className="settings-label">ASR 超时（秒）</label>
                        <input
                          type="number"
                          className="settings-input"
                          placeholder="600"
                          value={settings.asr_timeout ?? ""}
                          onChange={(e) => updateField("asr_timeout", parseInt(e.target.value) || 600)}
                        />
                      </div>
                    </>
                  ) : null}

                  {/* 本地 ASR 模型（两种模式都可能需要，local 模式为主，dashscope 模式作兜底） */}
                  <div className="settings-field">
                    <label className="settings-label">本地 ASR 模型（faster-whisper）</label>
                    <select
                      className="settings-input"
                      value={settings.asr_model_local || "medium"}
                      onChange={(e) => updateField("asr_model_local", e.target.value)}
                    >
                      <option value="tiny">tiny</option>
                      <option value="base">base</option>
                      <option value="small">small</option>
                      <option value="medium">medium</option>
                      <option value="large-v3">large-v3</option>
                    </select>
                    <div className="settings-hint">
                      {settings.asr_provider === "local"
                        ? "✓ 当前使用本地 faster-whisper 转写，无需 API Key。可从模型市场下载更多模型"
                        : "DashScope API Key 为空时，自动回退到本地 faster-whisper 模型转写"}
                    </div>
                  </div>

                  <div className="settings-field">
                    <label className="settings-label">HuggingFace 镜像 URL</label>
                    <input
                      type="text"
                      className="settings-input"
                      placeholder="https://hf-mirror.com"
                      value={settings.hf_mirror_url ?? ""}
                      onChange={(e) => updateField("hf_mirror_url", e.target.value)}
                    />
                    <div className="settings-hint">faster-whisper 模型下载走此镜像。默认 https://hf-mirror.com（国内镜像）。留空则使用官方 huggingface.co。修改后保存即生效，下次下载模型时应用</div>
                  </div>
                </div>

                {/* 检索配置 */}
                <div className="settings-section">
                  <div className="settings-section-title">检索参数</div>
                  <div className="settings-hint" style={{ marginBottom: 12, color: "var(--muted)" }}>
                    以下参数控制 AI 回答问题时从知识库中查找多少内容。数值越大结果越全面，但速度越慢、消耗越多。不确定的话保持默认即可。
                  </div>

                  <div className="settings-field">
                    <label className="settings-label">候选数量 (candidate_k)</label>
                    <input
                      type="number"
                      className="settings-input"
                      placeholder="24"
                      value={settings.retrieval_candidate_k ?? ""}
                      onChange={(e) => updateField("retrieval_candidate_k", parseInt(e.target.value) || 24)}
                    />
                    <div className="settings-hint">第一步粗筛：从知识库中初步捞出多少条相关内容。建议 20-50，越大越不会漏但越慢</div>
                  </div>

                  <div className="settings-field">
                    <label className="settings-label">最终返回 (top_k)</label>
                    <input
                      type="number"
                      className="settings-input"
                      placeholder="8"
                      value={settings.retrieval_top_k ?? ""}
                      onChange={(e) => updateField("retrieval_top_k", parseInt(e.target.value) || 8)}
                    />
                    <div className="settings-hint">最终喂给 AI 的内容条数。建议 5-10，太多会超出模型上下文，太少信息不够</div>
                  </div>

                  <div className="settings-field">
                    <label className="settings-label">MMR 获取数 (fetch_k)</label>
                    <input
                      type="number"
                      className="settings-input"
                      placeholder="32"
                      value={settings.retrieval_mmr_fetch_k ?? ""}
                      onChange={(e) => updateField("retrieval_mmr_fetch_k", parseInt(e.target.value) || 32)}
                    />
                    <div className="settings-hint">去重前临时拉取的条数，需大于等于候选数量。一般不用改</div>
                  </div>

                  <div className="settings-field">
                    <label className="settings-label">MMR 多样性 (lambda)</label>
                    <input
                      type="number"
                      step="0.05"
                      min="0"
                      max="1"
                      className="settings-input"
                      placeholder="0.55"
                      value={settings.retrieval_mmr_lambda ?? ""}
                      onChange={(e) => updateField("retrieval_mmr_lambda", parseFloat(e.target.value) || 0.55)}
                    />
                    <div className="settings-hint">0=尽量给不同角度的内容（防重复），1=只给最相关的。建议 0.5 左右平衡</div>
                  </div>
                </div>

                {/* 危险操作 */}
                <div className="settings-section" style={{ borderTop: "1px solid var(--danger)", marginTop: 24 }}>
                  <div className="settings-section-title" style={{ color: "var(--danger)" }}>危险操作</div>
                  <div className="settings-hint" style={{ marginBottom: 12, color: "var(--muted)" }}>
                    卸载全部内容将清除所有账号、Cookie、视频、配置和日志，且不可恢复。仅保留 ASR 模型。
                  </div>
                  <button
                    type="button"
                    className="btn btn-sm"
                    style={{ background: "var(--danger)", color: "#fff", border: "none" }}
                    onClick={() => setShowWipeConfirm(true)}
                    disabled={wiping}
                  >
                    {wiping ? "正在清理..." : "卸载全部内容"}
                  </button>
                </div>
              </>
            )}
          </div>

          {/* Footer */}
          <div className="settings-footer">
            <button className="btn btn-ghost btn-sm" onClick={onClose}>取消</button>
            <button
              className="btn btn-primary btn-sm"
              onClick={handleSave}
              disabled={saving || loading}
            >
              {saving ? "保存中..." : "保存并生效"}
            </button>
          </div>
        </aside>
      </div>

      {/* 卸载全部内容 — 二次确认弹窗 */}
      {showWipeConfirm && (
        <div
          role="dialog"
          aria-label="确认卸载全部内容"
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 60,
          }}
          onClick={wiping ? undefined : () => setShowWipeConfirm(false)}
        >
          <div
            style={{
              background: "var(--bg, #fff)",
              borderRadius: 12,
              padding: 24,
              maxWidth: 440,
              width: "90%",
              boxShadow: "0 12px 40px rgba(0,0,0,0.2)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ fontSize: 16, fontWeight: 600, color: "var(--danger)", marginBottom: 12 }}>
              确认卸载全部内容
            </div>
            <div style={{ fontSize: 13, color: "var(--text)", marginBottom: 12, lineHeight: 1.6 }}>
              此操作将清除所有账号、Cookie、视频、配置和日志，且不可恢复。仅保留 ASR 模型。
            </div>
            <div style={{ fontSize: 13, color: "var(--danger)", fontWeight: 600, marginBottom: 16, lineHeight: 1.6 }}>
              ⚠️ 确认后应用将自动重启，所有数据将被清除！
            </div>
            <div style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => setShowWipeConfirm(false)}
                disabled={wiping}
              >
                取消
              </button>
              <button
                type="button"
                className="btn btn-sm"
                style={{ background: "var(--danger)", color: "#fff", border: "none" }}
                onClick={handleWipe}
                disabled={wiping}
              >
                {wiping ? "正在清理..." : "确认卸载"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Toast */}
      {toast && (
        <div className={`settings-toast ${toast.type || ""}`}>
          {toast.msg}
        </div>
      )}
    </>
  );
}
