/**
 * ClipMind API 客户端
 *
 * 统一封装所有后端接口调用，包含：
 * - B站认证 / 收藏夹 / 知识库 / 对话
 * - 抖音认证 / 收藏 / 视频
 * - 应用设置（settings.json 热加载）
 *
 * 桌面应用模式下，后端绑定 127.0.0.1，前端通过同源或 localhost 访问。
 */

// ============================================================================
// 基础配置
// ============================================================================

/**
 * API 基础地址。
 *
 * 桌面应用打包模式（Tauri 加载后端 URL，前后端同源）：使用相对路径 ""，
 * 前端请求自动发往同源后端，彻底绕开 localhost/127.0.0.1 代理拦截问题。
 *
 * 开发模式（next dev，前端在 localhost:3000）：使用环境变量或回退地址，
 * 后端运行在 localhost:8000。
 *
 * 判断逻辑：window.location.hostname 为空（tauri:// 协议）或指向 127.0.0.1/localhost
 * 且端口为 8000 时，视为打包模式（同源）。
 */
function detectApiBaseUrl(): string {
  if (typeof window === "undefined") {
    // SSR 构建阶段，使用占位地址（不影响静态导出）
    return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  }
  const { hostname, port } = window.location;
  // 打包模式：窗口已导航到后端 URL（127.0.0.1:8000 或 localhost:8000）
  if ((hostname === "127.0.0.1" || hostname === "localhost") && port === "8000") {
    return "";
  }
  // 开发模式或其他情况
  return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
}

export const API_BASE_URL = detectApiBaseUrl();

// ============================================================================
// 错误类型
// ============================================================================

export class ApiError extends Error {
  status: number;
  detail?: unknown;

  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// ============================================================================
// 通用请求工具
// ============================================================================

interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** 是否以 Blob 形式返回响应（用于文件下载） */
  blob?: boolean;
  /** 请求超时时间（毫秒），默认 15 秒。设为 0 表示不超时 */
  timeout?: number;
}

/** 默认请求超时（毫秒） */
const DEFAULT_REQUEST_TIMEOUT = 15000;

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const url = `${API_BASE_URL}${path}`;
  const headers: Record<string, string> = {
    ...options.headers,
  };

  if (options.body !== undefined && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  // 超时处理：如果调用方未提供 signal，创建 AbortController
  // 防止后端锁竞争或网络问题导致 fetch 无限等待
  const timeoutMs = options.timeout ?? DEFAULT_REQUEST_TIMEOUT;
  let controller: AbortController | null = null;
  let timeoutId: ReturnType<typeof setTimeout> | null = null;
  let signal = options.signal;
  if (!signal && timeoutMs > 0) {
    controller = new AbortController();
    signal = controller.signal;
    timeoutId = setTimeout(() => controller!.abort(), timeoutMs);
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method: options.method || "GET",
      headers,
      body:
        options.body instanceof FormData
          ? options.body
          : options.body !== undefined
            ? JSON.stringify(options.body)
            : undefined,
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      // 区分用户主动取消和超时取消
      const msg = timeoutId ? `请求超时 (${timeoutMs}ms)` : "请求已取消";
      throw new ApiError(msg, 408);
    }
    // "Failed to fetch" 通常意味着网络层面无法连接：
    // 1. 后端未启动  2. 系统代理拦截了 localhost 请求  3. CSP 阻止
    const rawMsg = err instanceof Error ? err.message : "网络请求失败";
    const hint = rawMsg === "Failed to fetch"
      ? `无法连接后端 (${API_BASE_URL})，请检查：1) 应用是否正常启动 2) 代理软件是否拦截了 localhost 请求`
      : rawMsg;
    throw new ApiError(hint, 0);
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }

  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      try {
        detail = await response.text();
      } catch {
        detail = undefined;
      }
    }
    const message =
      (detail && typeof detail === "object" && "detail" in detail
        ? String((detail as Record<string, unknown>).detail)
        : null) ||
      `请求失败 (${response.status})`;
    throw new ApiError(message, response.status, detail);
  }

  if (options.blob) {
    return response.blob() as unknown as T;
  }

  // 部分端点返回空响应
  const contentLength = response.headers.get("content-length");
  if (contentLength === "0") {
    return {} as T;
  }

  try {
    return await response.json();
  } catch {
    return {} as T;
  }
}

/** 构建查询字符串 */
function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const str = search.toString();
  return str ? `?${str}` : "";
}

// ============================================================================
// SSE 流式响应解析（POST 场景，使用 fetch + ReadableStream）
// ============================================================================

/**
 * 从 fetch Response 中解析 SSE 事件流，逐事件回调。
 *
 * 协议格式：
 *   event: <name>\n
 *   data: <json>\n\n
 *
 * 不依赖 EventSource（仅支持 GET），适用于 POST SSE 端点。
 */
async function consumeSSEStream(
  response: Response,
  onEvent: (event: IngestStreamEvent) => void,
): Promise<void> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE 事件之间以 "\n\n" 分隔
      let sepIdx: number;
      while ((sepIdx = buffer.indexOf("\n\n")) >= 0) {
        const rawEvent = buffer.slice(0, sepIdx);
        buffer = buffer.slice(sepIdx + 2);
        const evt = parseSSEBlock(rawEvent);
        if (evt) onEvent(evt);
      }
    }
    // 处理尾部残留
    if (buffer.trim()) {
      const evt = parseSSEBlock(buffer);
      if (evt) onEvent(evt);
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // noop
    }
  }
}

function parseSSEBlock(block: string): IngestStreamEvent | null {
  let eventName = "progress";
  let dataStr = "";
  for (const line of block.split("\n")) {
    if (!line) continue;
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataStr += line.slice(5).trim();
    }
  }
  if (!dataStr) return null;
  try {
    const data = JSON.parse(dataStr);
    return {
      event: eventName as IngestStreamEvent["event"],
      step: data.step ?? "",
      status: data.status ?? "",
      message: data.message ?? "",
    };
  } catch {
    return null;
  }
}

// ============================================================================
// 类型定义 — B站认证
// ============================================================================

export interface UserInfo {
  mid: number;
  uname: string;
  face?: string;
}

export interface QRCodeResponse {
  qrcode_key: string;
  qrcode_url: string;
  qrcode_image_base64: string;
}

export interface LoginStatusResponse {
  status: "waiting" | "scanned" | "confirmed" | "expired";
  message: string;
  user_info?: UserInfo;
  session_id?: string;
}

// ============================================================================
// 类型定义 — 抖音认证
// ============================================================================

export interface DouyinQRCodeResponse {
  session_key: string;
  qrcode_image_base64: string;
  message?: string;
}

export interface DouyinQRCodePollResponse {
  status: string;
  message: string;
  session_id?: string;
  user_info?: {
    uid?: string;
    nickname?: string;
    avatar?: string;
  };
}

export interface DouyinAuthStatusResponse {
  logged_in: boolean;
  uid?: string;
  nickname?: string;
}

export interface DouyinCookieLoginResponse {
  success: boolean;
  message: string;
  uid: string;
  nickname: string;
}

export interface DouyinSyncFolderStat {
  synced: number;
  new: number;
  folder_id?: number;
}

export interface DouyinSyncResult {
  success: boolean;
  first_sync?: boolean;
  like?: DouyinSyncFolderStat;
  collect?: Record<string, unknown> | null;
  collect_flat?: DouyinSyncFolderStat;
  message?: string;
}

// ============================================================================
// 类型定义 — 收藏夹
// ============================================================================

export interface FavoriteFolder {
  media_id: number;
  title: string;
  media_count: number;
  is_selected?: boolean;
  is_default?: boolean;
  platform?: string;
}

export interface Video {
  bvid: string;
  title: string;
  cid?: number;
  duration?: number;
  owner_name?: string;
}

export interface AllVideosResponse {
  total: number;
  videos: Video[];
}

export interface OrganizePreviewItem {
  bvid: string;
  title: string;
  resource_id: number;
  resource_type: number;
  target_folder_id: number | null;
  target_folder_title: string;
  reason?: string;
}

export interface OrganizePreviewResponse {
  default_folder_id: number;
  default_folder_title: string;
  folders: FavoriteFolder[];
  items: OrganizePreviewItem[];
  stats: Record<string, number>;
}

export interface OrganizeExecuteResponse {
  moved: number;
  message?: string;
}

// ============================================================================
// 类型定义 — 知识库
// ============================================================================

export interface KnowledgeStats {
  total_videos?: number;
  total_folders?: number;
  total_chunks?: number;
  [key: string]: unknown;
}

export interface FolderStatus {
  media_id: number;
  indexed_count: number;
  failed_count?: number;
  media_count?: number;
  last_sync_at?: string | null;
}

export interface BuildStatus {
  task_id: string;
  status: "pending" | "running" | "completed" | "failed";
  progress: number;
  current_step: string;
  total_videos: number;
  processed_videos: number;
  total_folders?: number;
  processed_folders?: number;
  current_folder_id?: number;
  current_folder_title?: string;
  current_video_title?: string;
  message: string;
  succeeded?: number;
  failed?: number;
}

export interface BuildRequest {
  folder_ids: number[];
  exclude_bvids?: string[];
}

export interface VideoIngestItem {
  bvid: string;
  platform: "bilibili" | "douyin";
  tags?: string[];
}

export interface VideoListItem {
  bvid: string;
  platform: string;
  title: string;
  author: string;
  duration: number;
  is_processed: boolean;
  process_error: string | null;
  folder_id: number;
  folder_title: string;
  tags?: string[] | null;
  /** 最近一次失败的阶段（download/asr/embedding/vector/...） */
  last_error_stage?: string | null;
  /** 最近一次失败的详细错误信息 */
  last_error_detail?: string | null;
  /** 累计重试次数 */
  retry_count?: number | null;
}

// ============================================================================
// 类型定义 — 对话
// ============================================================================

export interface ChatSource {
  bvid: string;
  title: string;
  url: string;
}

export interface ChatResponse {
  answer: string;
  sources: ChatSource[];
}

// ============================================================================
// 类型定义 — 抖音视频
// ============================================================================

export interface DouyinVideoItem {
  video_id: string;
  title: string;
  author: string;
  duration: number;
  content_source?: string;
  is_processed: boolean;
  created_at?: string;
}

export interface DouyinVideoListResponse {
  total: number;
  videos: DouyinVideoItem[];
}

export interface DouyinParseResponse {
  video_id: string;
  title: string;
  description: string;
  author: string;
  cover_url: string;
  duration: number;
}

export interface DouyinIngestRequest {
  video_id: string;
  title?: string;
  description?: string;
  author?: string;
  duration?: number;
  cover_url?: string;
}

export interface DouyinIngestResponse {
  video_id: string;
  title: string;
  message: string;
}

export interface DouyinBatchIngestRequest {
  folder_id?: number;
  limit?: number;
  video_ids?: string[];
}

export interface DouyinBatchIngestResultItem {
  video_id: string;
  title: string;
  status: "ok" | "fail";
  error?: string;
}

export interface DouyinBatchIngestResponse {
  total_pending: number;
  processed: number;
  succeeded: number;
  failed: number;
  results: DouyinBatchIngestResultItem[];
  /** 后台任务 ID（用于轮询进度）；无待入库视频时为 null */
  task_id?: string | null;
  message?: string;
}

/** 抖音批量入库后台任务状态（复用 BuildStatus 结构） */
export type DouyinBatchIngestStatus = BuildStatus;

/** SSE 事件：单视频入库步骤进度 */
export interface IngestStreamEvent {
  /** 事件名：progress / done / error */
  event: "progress" | "done" | "error";
  /** 步骤名 */
  step: string;
  /** 状态：running / completed / failed / cancelled */
  status: string;
  /** 人类可读消息 */
  message: string;
}

export interface DouyinFolderInfo {
  folder_id: number;
  title: string;
  media_count: number;
  indexed_count: number;
  is_selected: boolean;
  status: "all_indexed" | "partial" | "none";
}

export interface DouyinFolderVideo {
  video_id: string;
  title: string;
  author: string;
  duration: number;
  is_selected: boolean;
  is_processed: boolean;
}

export interface DouyinFolderVideosResponse {
  total: number;
  videos: DouyinFolderVideo[];
}

// ============================================================================
// 类型定义 — 应用设置
// ============================================================================

export interface AppSettings {
  // LLM
  /** LLM 运行模式：api / ollama */
  llm_provider?: string;
  openai_api_key?: string;
  openai_base_url?: string;
  llm_model?: string;
  /** Ollama 本地模式 base_url */
  ollama_base_url?: string;
  /** Ollama 本地模型名 */
  ollama_model?: string;
  chat_use_llm_router?: boolean;
  // Embedding
  /** Embedding 运行模式：openai / dashscope / ollama / nvidia / local */
  embedding_provider?: string;
  embedding_model?: string;
  embedding_api_key?: string;
  embedding_base_url?: string;
  // ASR
  /** ASR 运行模式：dashscope / local */
  asr_provider?: string;
  dashscope_base_url?: string;
  asr_api_key?: string;
  asr_model?: string;
  asr_timeout?: number;
  asr_model_local?: string;
  dashscope_recognition_model?: string;
  asr_input_format?: string;
  hf_mirror_url?: string;
  // Retrieval
  retrieval_candidate_k?: number;
  retrieval_top_k?: number;
  retrieval_mmr_fetch_k?: number;
  retrieval_mmr_lambda?: number;
}

/** 单类配置测试结果 */
export interface TestResult {
  ok: boolean;
  error?: string;
  latency_ms?: number;
  /** ASR 专属：模型是否走本地缓存（true=本地已缓存，false=走网络下载） */
  cached?: boolean;
}

/** POST /settings/test 整体响应 */
export interface SettingsTestResponse {
  llm: TestResult;
  embedding: TestResult;
  asr: TestResult;
}

export interface SettingsStatus {
  llm_configured: boolean;
  embedding_configured: boolean;
  asr_configured: boolean;
  configured: boolean;
  tested?: boolean;
}

export interface SettingsUpdateResponse {
  message: string;
  updated: boolean;
  fields?: string[];
}

// ============================================================================
// 类型定义 — 系统管理
// ============================================================================

/** POST /system/wipe 响应：全量清理结果 */
export interface SystemWipeResult {
  success: boolean;
  message?: string;
  /** 已清理的资源类别（如 chroma / db / settings / logs / cookie_key） */
  wiped?: string[];
  /** 保留的资源类别（如 models） */
  preserved?: string[];
  /** 清理过程中出现的错误（不致命时 success 仍可能为 true） */
  errors?: string[];
}

// ============================================================================
// API 模块 — 应用设置
// ============================================================================

export const authApi = {
  /** 获取登录二维码 */
  getQRCode(): Promise<QRCodeResponse> {
    return request<QRCodeResponse>("/auth/qrcode");
  },

  /** 轮询二维码登录状态 */
  pollQRCode(qrcodeKey: string): Promise<LoginStatusResponse> {
    return request<LoginStatusResponse>(`/auth/qrcode/poll/${encodeURIComponent(qrcodeKey)}`);
  },

  /** 退出登录 */
  logout(sessionId: string): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/auth/session/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    );
  },
};

// ============================================================================
// API 模块 — 抖音认证
// ============================================================================

export const douyinApi = {
  /**
   * 获取抖音登录二维码
   *
   * 通过 modal_id=login 参数直接打开登录弹窗，页面自动加载 QR 码，
   * 不需要点击登录按钮，绕开了 CDP 阻塞问题。
   * 冷启动场景约 10-15s，预热命中场景 1-2s。
   */
  getQRCode(): Promise<DouyinQRCodeResponse> {
    return request<DouyinQRCodeResponse>("/douyin/auth/qrcode", { timeout: 60000 });
  },

  /** 轮询抖音二维码状态 */
  pollQRCode(sessionKey: string): Promise<DouyinQRCodePollResponse> {
    return request<DouyinQRCodePollResponse>(
      `/douyin/auth/qrcode/poll${buildQuery({ session_key: sessionKey })}`,
    );
  },

  /** 检查抖音登录状态 */
  getAuthStatus(): Promise<DouyinAuthStatusResponse> {
    return request<DouyinAuthStatusResponse>("/douyin/auth/status");
  },

  /** 退出抖音登录 */
  logout(sessionId?: string): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/douyin/auth/logout${buildQuery({ session_id: sessionId })}`,
      { method: "DELETE" },
    );
  },

  /** Cookie 登录 */
  loginWithCookie(payload: { cookie: string }): Promise<DouyinCookieLoginResponse> {
    return request<DouyinCookieLoginResponse>("/douyin/auth/login", {
      method: "POST",
      body: payload,
    });
  },

  /** 同步抖音收藏（涉及浏览器启动+页面采集，需要较长超时） */
  syncFav(limit: number): Promise<DouyinSyncResult> {
    return request<DouyinSyncResult>(
      `/douyin/auth/sync${buildQuery({ limit })}`,
      { method: "POST", timeout: 120000 },
    );
  },

  // --- 抖音视频管理 ---

  /** 解析抖音分享链接 */
  parse(payload: { url: string }): Promise<DouyinParseResponse> {
    return request<DouyinParseResponse>("/douyin/parse", {
      method: "POST",
      body: payload,
    });
  },

  /** 入库抖音视频 */
  ingest(payload: DouyinIngestRequest): Promise<DouyinIngestResponse> {
    return request<DouyinIngestResponse>("/douyin/ingest", {
      method: "POST",
      body: payload,
    });
  },

  /** 批量入库：将已同步但未处理的抖音视频写入知识库 */
  ingestBatch(payload: DouyinBatchIngestRequest): Promise<DouyinBatchIngestResponse> {
    return request<DouyinBatchIngestResponse>("/douyin/ingest-batch", {
      method: "POST",
      body: payload,
    });
  },

  /** 查询抖音批量入库后台任务进度（复用 build_tasks 机制） */
  getIngestBatchStatus(taskId: string): Promise<DouyinBatchIngestStatus> {
    return request<DouyinBatchIngestStatus>(
      `/douyin/ingest-batch/${encodeURIComponent(taskId)}/status`,
    );
  },

  /**
   * 单视频入库 SSE 流（POST + ReadableStream）。
   *
   * 使用 fetch streaming 接收 text/event-stream，逐事件回调。
   * 调用方可在 onEvent 中更新 UI，并在 done / error 时停止。
   */
  async ingestStream(
    payload: DouyinIngestRequest,
    onEvent: (event: IngestStreamEvent) => void,
    sessionId?: string,
    signal?: AbortSignal,
  ): Promise<void> {
    const url = `${API_BASE_URL}/douyin/ingest/stream${buildQuery({ session_id: sessionId })}`;
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });
    if (!resp.ok) {
      let detail: unknown;
      try {
        detail = await resp.json();
      } catch {
        try {
          detail = await resp.text();
        } catch {
          detail = undefined;
        }
      }
      const message =
        (detail && typeof detail === "object" && "detail" in detail
          ? String((detail as Record<string, unknown>).detail)
          : null) || `入库失败 (${resp.status})`;
      throw new ApiError(message, resp.status, detail);
    }
    await consumeSSEStream(resp, onEvent);
  },

  /** 获取抖音视频列表 */
  listVideos(): Promise<DouyinVideoListResponse> {
    return request<DouyinVideoListResponse>("/douyin/videos");
  },

  /** 删除抖音视频 */
  deleteVideo(videoId: string, platform: string, sessionId: string): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/knowledge/video/${encodeURIComponent(videoId)}${buildQuery({ platform, session_id: sessionId })}`,
      { method: "DELETE" },
    );
  },

  // --- 抖音文件夹管理 ---

  /** 获取抖音文件夹列表 */
  listFolders(sessionId?: string): Promise<DouyinFolderInfo[]> {
    return request<DouyinFolderInfo[]>(
      `/douyin/folders/list${buildQuery({ session_id: sessionId })}`,
    );
  },

  /** 获取文件夹内视频 */
  getFolderVideos(
    folderId: number,
    sessionId?: string,
  ): Promise<DouyinFolderVideosResponse> {
    return request<DouyinFolderVideosResponse>(
      `/douyin/folders/${folderId}/videos${buildQuery({ session_id: sessionId })}`,
    );
  },

  /** 切换文件夹选中状态 */
  toggleFolderSelect(folderId: number, selectAll: boolean): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/douyin/folders/${folderId}/select${buildQuery({ select_all: selectAll })}`,
      { method: "POST" },
    );
  },

  /** 切换单个视频选中状态 */
  toggleVideoSelect(videoId: string, folderId: number, selected: boolean): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/douyin/folders/video/${encodeURIComponent(videoId)}/select${buildQuery({ folder_id: folderId, select: selected })}`,
      { method: "POST" },
    );
  },
};

// ============================================================================
// API 模块 — 收藏夹
// ============================================================================

export const favoritesApi = {
  /** 获取收藏夹列表 */
  getList(sessionId: string): Promise<FavoriteFolder[]> {
    return request<FavoriteFolder[]>(
      `/favorites/list${buildQuery({ session_id: sessionId })}`,
    );
  },

  /** 获取收藏夹所有视频 */
  getAllVideos(mediaId: number, sessionId: string): Promise<AllVideosResponse> {
    return request<AllVideosResponse>(
      `/favorites/${mediaId}/all-videos${buildQuery({ session_id: sessionId })}`,
    );
  },

  /** 整理预览 */
  organizePreview(folderId: number, sessionId: string): Promise<OrganizePreviewResponse> {
    return request<OrganizePreviewResponse>(
      `/favorites/organize/preview${buildQuery({ session_id: sessionId })}`,
      { method: "POST", body: { folder_id: folderId } },
    );
  },

  /** 执行整理移动 */
  organizeExecute(
    payload: { default_folder_id: number; moves: Array<{ resource_id: number; resource_type: number; target_folder_id: number }> },
    sessionId: string,
  ): Promise<OrganizeExecuteResponse> {
    return request<OrganizeExecuteResponse>(
      `/favorites/organize/execute${buildQuery({ session_id: sessionId })}`,
      { method: "POST", body: payload },
    );
  },

  /** 清理失效内容 */
  cleanInvalid(folderId: number, sessionId: string): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/favorites/organize/clean-invalid${buildQuery({ session_id: sessionId })}`,
      { method: "POST", body: { folder_id: folderId } },
    );
  },
};

// ============================================================================
// API 模块 — 知识库
// ============================================================================

export const knowledgeApi = {
  /** 获取知识库统计 */
  getStats(): Promise<KnowledgeStats> {
    return request<KnowledgeStats>("/knowledge/stats");
  },

  /** 获取收藏夹入库状态 */
  getFolderStatus(sessionId: string): Promise<FolderStatus[]> {
    return request<FolderStatus[]>(
      `/knowledge/folders/status${buildQuery({ session_id: sessionId })}`,
    );
  },

  /** 构建知识库 */
  build(payload: BuildRequest, sessionId: string): Promise<{ task_id: string }> {
    return request<{ task_id: string }>(
      `/knowledge/build${buildQuery({ session_id: sessionId })}`,
      { method: "POST", body: payload },
    );
  },

  /** 获取构建状态 */
  getBuildStatus(taskId: string, sessionId?: string): Promise<BuildStatus> {
    return request<BuildStatus>(
      `/knowledge/build/status/${encodeURIComponent(taskId)}${buildQuery({ session_id: sessionId })}`,
    );
  },

  /** 跨平台视频列表（RAG入库管理） */
  listAllVideos(
    biliSessionId: string,
    douyinSessionId?: string,
    platform?: string,
    status?: string,
    tag?: string,
  ): Promise<VideoListItem[]> {
    return request<VideoListItem[]>(
      `/knowledge/videos/list${buildQuery({
        session_id: biliSessionId,
        douyin_session_id: douyinSessionId,
        platform,
        status,
        tag,
      })}`,
    );
  },

  /** 视频级批量入库（后台任务） */
  ingestVideos(
    payload: { videos: VideoIngestItem[] },
    biliSessionId: string,
    douyinSessionId?: string,
  ): Promise<{ task_id: string }> {
    return request<{ task_id: string }>(
      `/knowledge/ingest-videos${buildQuery({
        session_id: biliSessionId,
        douyin_session_id: douyinSessionId,
      })}`,
      { method: "POST", body: payload },
    );
  },

  /** 出库：仅删除 RAG 向量，保留 VideoCache 元数据 */
  removeVideoFromRag(
    bvid: string,
    platform: string,
    sessionId: string,
  ): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/knowledge/video/${encodeURIComponent(bvid)}/rag${buildQuery({ platform, session_id: sessionId })}`,
      { method: "DELETE" },
    );
  },

  /** 删除视频：同时删除向量 + VideoCache 缓存 + 收藏关系 */
  deleteVideo(
    bvid: string,
    platform: string,
    sessionId: string,
  ): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/knowledge/video/${encodeURIComponent(bvid)}${buildQuery({ platform, session_id: sessionId })}`,
      { method: "DELETE" },
    );
  },

  /** 导出视频 Markdown */
  exportMarkdown(
    bvid: string,
    mode: "original" | "ai",
    sessionId: string,
    operationId?: string,
    signal?: AbortSignal,
  ): Promise<Blob> {
    return request<Blob>(
      `/knowledge/video/${encodeURIComponent(bvid)}/export${buildQuery({ session_id: sessionId })}`,
      {
        method: "POST",
        body: { mode, operation_id: operationId },
        signal,
        blob: true,
      },
    );
  },

  /** 单视频入库 */
  ingestVideo(
    bvid: string,
    folderId: number,
    sessionId: string,
    operationId?: string,
    signal?: AbortSignal,
  ): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/knowledge/video/${encodeURIComponent(bvid)}/ingest${buildQuery({ session_id: sessionId })}`,
      {
        method: "POST",
        body: { folder_id: folderId, operation_id: operationId },
        signal,
      },
    );
  },

  /**
   * 单视频入库 SSE 流（POST + ReadableStream）。
   *
   * 通过 fetch streaming 接收 /knowledge/video/{bvid}/ingest/stream 推送的步骤事件，
   * 调用方在 onEvent 中更新 UI；done / error 事件后流自动结束。
   */
  async ingestVideoStream(
    bvid: string,
    folderId: number,
    sessionId: string,
    onEvent: (event: IngestStreamEvent) => void,
    operationId?: string,
    signal?: AbortSignal,
  ): Promise<void> {
    const url = `${API_BASE_URL}/knowledge/video/${encodeURIComponent(bvid)}/ingest/stream${buildQuery({ session_id: sessionId })}`;
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_id: folderId, operation_id: operationId }),
      signal,
    });
    if (!resp.ok) {
      let detail: unknown;
      try {
        detail = await resp.json();
      } catch {
        try {
          detail = await resp.text();
        } catch {
          detail = undefined;
        }
      }
      const message =
        (detail && typeof detail === "object" && "detail" in detail
          ? String((detail as Record<string, unknown>).detail)
          : null) || `单视频入库失败 (${resp.status})`;
      throw new ApiError(message, resp.status, detail);
    }
    await consumeSSEStream(resp, onEvent);
  },

  /** 取消操作 */
  cancelOperation(operationId: string, sessionId: string): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/knowledge/operations/${encodeURIComponent(operationId)}/cancel${buildQuery({ session_id: sessionId })}`,
      { method: "POST" },
    );
  },

  /** 取消入库任务（通过 task_tracker 的 task_id） */
  cancel(taskId: string): Promise<{ message: string }> {
    return request<{ message: string }>(
      `/api/tasks/${encodeURIComponent(taskId)}/cancel`,
      { method: "POST" },
    );
  },
};

// ============================================================================
// API 模块 — 对话
// ============================================================================

export const chatApi = {
  /** 普通问答 */
  ask(
    question: string,
    sessionId?: string,
    folderIds?: number[],
    platform?: string | null,
  ): Promise<ChatResponse> {
    return request<ChatResponse>("/chat/ask", {
      method: "POST",
      body: {
        question,
        session_id: sessionId,
        folder_ids: folderIds,
        platform: platform === "all" ? null : platform,
      },
    });
  },
};

// ============================================================================
// API 模块 — 应用设置
// ============================================================================

export const settingsApi = {
  /** 获取当前设置（敏感字段脱敏） */
  get(): Promise<AppSettings> {
    return request<AppSettings>("/settings");
  },

  /** 更新设置并热加载 */
  update(payload: Partial<AppSettings>): Promise<SettingsUpdateResponse> {
    return request<SettingsUpdateResponse>("/settings", {
      method: "PUT",
      body: payload,
    });
  },

  /** 检查关键配置是否就绪 */
  getStatus(): Promise<SettingsStatus> {
    return request<SettingsStatus>("/settings/status");
  },

  /** 实时测试三类模型连通性（字段空则后端回退到已保存值） */
  test(payload: Partial<{
    openai_api_key: string;
    openai_base_url: string;
    llm_model: string;
    embedding_api_key: string;
    embedding_base_url: string;
    embedding_model: string;
    asr_model_local: string;
  }>): Promise<SettingsTestResponse> {
    return request<SettingsTestResponse>("/settings/test", {
      method: "POST",
      body: payload,
    });
  },

  /** 单独测试 LLM 配置 */
  testLLM(payload: Partial<{
    openai_api_key: string;
    openai_base_url: string;
    llm_model: string;
  }>): Promise<TestResult> {
    return request<TestResult>("/settings/test/llm", {
      method: "POST",
      body: payload,
    });
  },

  /** 单独测试 Embedding 配置 */
  testEmbedding(payload: Partial<{
    embedding_api_key: string;
    embedding_base_url: string;
    embedding_model: string;
    openai_api_key: string;
    openai_base_url: string;
  }>): Promise<TestResult> {
    return request<TestResult>("/settings/test/embedding", {
      method: "POST",
      body: payload,
    });
  },

  /** 单独测试 ASR 本地模型加载 */
  testASR(payload: Partial<{ asr_model_local: string }>): Promise<TestResult> {
    return request<TestResult>("/settings/test/asr", {
      method: "POST",
      body: payload,
    });
  },
};

// ============================================================================
// API 模块 — 系统管理
// ============================================================================

export const systemApi = {
  /**
   * 卸载全部内容 — 清除所有用户数据（账号、Cookie、视频、配置、日志、密钥），
   * 仅保留 ASR 模型目录。需传入 confirm=true 才会执行，否则后端返回 400。
   */
  wipe(confirm: boolean): Promise<SystemWipeResult> {
    return request<SystemWipeResult>("/system/wipe", {
      method: "POST",
      body: { confirm },
    });
  },
};

// ============================================================================
// 类型定义 — 模型市场
// ============================================================================

export type ModelCategory = "llm" | "embedding" | "asr";
export type ModelEngine = "ollama" | "hf_whisper" | "hf_embedding";
export type DownloadStatus = "pending" | "downloading" | "completed" | "failed" | "cancelled";

export interface CatalogModel {
  id: string;
  category: ModelCategory;
  display_name: string;
  size_mb: number;
  engine: ModelEngine;
  model_id: string;
  recommended: boolean;
  description: string;
  // 状态字段（由 catalog 接口合并返回）
  downloaded?: boolean;
  active?: boolean;
  downloading?: boolean;
  // 已下载但缺少 ONNX 权重（旧版本下载的模型），需要重新下载以支持无 torch 环境
  onnx_missing?: boolean;
}

export interface ModelCatalog {
  models: CatalogModel[];
  ollama_installed: boolean;
  ollama_error: string;
}

export interface DownloadTaskInfo {
  task_id: string;
  model_id: string;
  category: ModelCategory;
  status: DownloadStatus;
  progress: number;
  downloaded_mb: number;
  total_mb: number;
  error: string | null;
  started_at: number;
  completed_at: number | null;
}

export interface ModelMarketStatus {
  tasks: DownloadTaskInfo[];
  models: Record<string, { downloaded: boolean; active: boolean; downloading: boolean }>;
}

/** SSE 推送的模型市场事件 */
export interface ModelMarketEvent {
  type: "started" | "progress" | "completed" | "failed" | "cancelled" | "snapshot";
  task_id?: string;
  model_id?: string;
  progress?: number;
  downloaded_mb?: number;
  total_mb?: number;
  error?: string;
  tasks?: DownloadTaskInfo[];
}

export interface ModelOpResult {
  ok: boolean;
  error?: string;
  code?: string; // 如 "dim_mismatch"
  task_id?: string;
  model_id?: string;
  engine?: ModelEngine;
  message?: string;
}

// ============================================================================
// API 模块 — 模型市场
// ============================================================================

export const modelMarketApi = {
  /** 获取推荐模型清单 + 当前下载状态 + Ollama 安装状态 */
  getCatalog(): Promise<ModelCatalog> {
    return request<ModelCatalog>("/api/model-market/catalog");
  },

  /** 查询所有任务的状态快照（不订阅 SSE 时的轮询兜底接口） */
  getStatus(): Promise<ModelMarketStatus> {
    return request<ModelMarketStatus>("/api/model-market/status");
  },

  /** 触发模型下载，立即返回 task_id，进度通过 SSE 推送 */
  download(modelId: string): Promise<ModelOpResult> {
    return request<ModelOpResult>("/api/model-market/download", {
      method: "POST",
      body: { model_id: modelId },
    });
  },

  /** 取消正在进行的下载任务 */
  cancel(modelId: string): Promise<ModelOpResult> {
    return request<ModelOpResult>("/api/model-market/cancel", {
      method: "POST",
      body: { model_id: modelId },
    });
  },

  /** 将已下载的模型应用为当前配置（写 settings.json + 热加载） */
  apply(modelId: string): Promise<ModelOpResult> {
    return request<ModelOpResult>("/api/model-market/apply", {
      method: "POST",
      body: { model_id: modelId },
    });
  },

  /** 删除已下载的本地模型文件 */
  delete(modelId: string): Promise<ModelOpResult> {
    return request<ModelOpResult>("/api/model-market/delete", {
      method: "POST",
      body: { model_id: modelId },
    });
  },

  /**
   * 订阅 SSE 事件流，返回 unsubscribe 函数。
   *
   * 与 ingestStream 不同，模型市场 SSE 是 GET 端点（无 body），
   * 因此可以用原生 EventSource，连接断开自动重连。
   */
  subscribeEvents(onEvent: (event: ModelMarketEvent) => void): () => void {
    const url = `${API_BASE_URL}/api/model-market/events`;
    const es = new EventSource(url);
    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        onEvent(data);
      } catch {
        // 忽略解析失败的事件（如 heartbeat 注释行）
      }
    };
    es.onerror = () => {
      // EventSource 内置自动重连，无需手动处理
    };
    return () => {
      es.close();
    };
  },
};

// ============================================================================
// 工具函数 — 外部浏览器打开链接
// ============================================================================

/**
 * 通过后端调用系统默认浏览器打开 URL。
 * Tauri webview 中 window.open / <a target="_blank"> 会在内部 webview 打开，
 * 导致用户看不到窗口但能听到视频声音，必须走后端 webbrowser.open。
 */
export async function openExternal(url: string): Promise<void> {
  try {
    await request("/api/open-external", {
      method: "POST",
      body: { url },
    });
  } catch (e) {
    console.error("打开外部浏览器失败", e);
  }
}
