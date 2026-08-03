"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { authApi, douyinApi, QRCodeResponse, UserInfo, DouyinQRCodeResponse } from "@/lib/api";

/* ============================================================
   Unified Login Modal
   ============================================================
   Tabbed interface: B站扫码 | 抖音扫码
   ============================================================ */

interface Props {
  defaultTab?: PlatformTab;
  isOpen: boolean;
  onClose: () => void;
  onBiliSuccess?: (sessionId: string, user: UserInfo) => void;
  onDouyinSuccess?: (sessionId: string, user: { uid: string; nickname: string; avatar: string }) => void;
}

type PlatformTab = "bilibili" | "douyin";
type QrStatus = "loading" | "ready" | "scanned" | "need_verify" | "success" | "error";

/* ---------- Bilibili QR Panel ---------- */
function BilibiliPanel({ onSuccess }: {
  onSuccess: (sessionId: string, user: UserInfo) => void;
}) {
  const [qr, setQr] = useState<QRCodeResponse | null>(null);
  const [status, setStatus] = useState<QrStatus>("loading");
  const [errorMsg, setErrorMsg] = useState("");
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const successTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // inFlight 互斥：避免上一次请求未返回时下一次轮询被并发触发
  const inFlightRef = useRef(false);
  const mounted = useRef(true);
  const successCalled = useRef(false);

  const stopPoll = useCallback(() => {
    if (pollTimerRef.current) { clearTimeout(pollTimerRef.current); pollTimerRef.current = null; }
    if (successTimerRef.current) { clearTimeout(successTimerRef.current); successTimerRef.current = null; }
    inFlightRef.current = false;
  }, []);

  const loadQR = useCallback(async () => {
    setStatus("loading");
    setErrorMsg("");
    setQr(null);
    stopPoll();
    try {
      const data = await authApi.getQRCode();
      if (!mounted.current) return;
      setQr(data);
      setStatus("ready");
    } catch (e) {
      if (mounted.current) {
        setStatus("error");
        setErrorMsg(e instanceof Error ? e.message : "获取二维码失败");
      }
    }
  }, [stopPoll]);

  useEffect(() => {
    mounted.current = true;
    successCalled.current = false;
    // 初始化加载二维码：挂载时必须 setState，属于合法的初始化副作用
    void loadQR();
    return () => {
      mounted.current = false;
      stopPoll();
    };
  }, [loadQR, stopPoll]);

  useEffect(() => {
    if (status !== "ready" && status !== "scanned" && status !== "need_verify") return;
    if (!qr) return;

    // 改为递归 setTimeout：上一次请求返回后才调度下一次，避免并发堆积
    const doPoll = async () => {
      if (!mounted.current || inFlightRef.current) return;
      inFlightRef.current = true;
      try {
        const res = await authApi.pollQRCode(qr.qrcode_key);
        if (!mounted.current) return;
        if (res.status === "scanned") setStatus("scanned");
        else if (res.status === "confirmed") {
          stopPoll();
          setStatus("success");
          if (successCalled.current) return;
          successCalled.current = true;
          // 500ms 延迟通过 ref 跟踪，组件卸载时清理
          if (!res.session_id || !res.user_info) {
            setStatus("error");
            return;
          }
          successTimerRef.current = setTimeout(() => {
            if (!mounted.current) return;
            onSuccess(res.session_id!, res.user_info!);
          }, 500);
          return; // 不再调度下一次
        } else if (res.status === "expired") {
          stopPoll();
          setStatus("error");
          setErrorMsg("二维码已过期");
          return;
        }
      } catch {
        // 单次失败不中断轮询
      } finally {
        inFlightRef.current = false;
      }
      // 调度下一次（仅在未停止时）
      if (mounted.current && !successCalled.current) {
        pollTimerRef.current = setTimeout(doPoll, 2000);
      }
    };

    pollTimerRef.current = setTimeout(doPoll, 2000);
    return () => {
      if (pollTimerRef.current) { clearTimeout(pollTimerRef.current); pollTimerRef.current = null; }
    };
  }, [status, qr, onSuccess, stopPoll]);

  return (
    <div className="flex flex-col items-center gap-4">
      <p className="text-sm text-[var(--muted)]">使用哔哩哔哩 APP 扫描二维码登录</p>

      {status === "loading" && (
        <div className="w-56 h-56 flex items-center justify-center border border-dashed border-[var(--border)] rounded-2xl">
          <div className="w-8 h-8 border-2 border-[var(--accent)] border-t-transparent rounded-full animate-spin" />
        </div>
      )}

      {(status === "ready" || status === "scanned") && qr && (
        <div className="relative">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={qr.qrcode_image_base64}
            alt="B站二维码"
            className="w-56 h-56 rounded-2xl border border-[var(--border)]"
          />
          {status === "scanned" && (
            <div className="absolute inset-0 bg-white/90 rounded-2xl flex flex-col items-center justify-center">
              <div className="status-pill">已扫码</div>
              <span className="text-sm mt-3 text-[var(--muted)]">请在手机上确认</span>
            </div>
          )}
        </div>
      )}

      {status === "success" && (
        <div className="w-64 h-64 flex flex-col items-center justify-center">
          <div className="status-pill ok">登录成功</div>
          <p className="text-sm text-[var(--muted)] mt-3">正在进入工作台</p>
        </div>
      )}

      {status === "error" && (
        <div className="flex flex-col items-center gap-3">
          <p className="text-sm text-red-500">{errorMsg || "二维码已过期"}</p>
          <button onClick={loadQR} className="btn btn-primary btn-sm">重新获取</button>
        </div>
      )}
    </div>
  );
}

/* ---------- Douyin QR Panel ---------- */
function DouyinPanel({ onSuccess }: {
  onSuccess: (sessionId: string, user: { uid: string; nickname: string; avatar: string }) => void;
}) {
  const [qr, setQr] = useState<DouyinQRCodeResponse | null>(null);
  const [status, setStatus] = useState<QrStatus>("loading");
  const [errorMsg, setErrorMsg] = useState("");
  const [errorDetail, setErrorDetail] = useState("");
  const [showDetail, setShowDetail] = useState(false);
  const [loadElapsed, setLoadElapsed] = useState(0);
  const [scannedElapsed, setScannedElapsed] = useState(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const loadTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const scannedTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mounted = useRef(true);
  const successCalled = useRef(false);

  const stopPoll = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const stopLoadTimer = useCallback(() => {
    if (loadTimerRef.current) { clearInterval(loadTimerRef.current); loadTimerRef.current = null; }
    setLoadElapsed(0);
  }, []);

  const stopScannedTimer = useCallback(() => {
    if (scannedTimerRef.current) { clearInterval(scannedTimerRef.current); scannedTimerRef.current = null; }
    setScannedElapsed(0);
  }, []);

  const loadQR = useCallback(async () => {
    setStatus("loading");
    setErrorMsg("");
    setErrorDetail("");
    setShowDetail(false);
    setQr(null);
    setLoadElapsed(0);
    stopPoll();
    stopLoadTimer();
    stopScannedTimer();
    // 启动加载计时（每 2 秒更新一次 elapsed）
    loadTimerRef.current = setInterval(() => {
      setLoadElapsed((prev) => prev + 2);
    }, 2000);
    try {
      const data = await douyinApi.getQRCode();
      if (!mounted.current) return;
      setQr(data);
      setStatus("ready");
      stopLoadTimer();
    } catch (e) {
      if (!mounted.current) return;
      setStatus("error");
      const errMsg = e instanceof Error ? e.message : "加载超时，请点击刷新重试，或使用 Cookie 登录";
      // 分离主错误信息和诊断信息（[诊断: ...] 格式）
      const diagMatch = errMsg.match(/\[诊断:\s*([^\]]+)\]/);
      if (diagMatch) {
        setErrorMsg(errMsg.replace(/\s*\[诊断:[^\]]*\]\s*/, "").trim());
        setErrorDetail(diagMatch[1]);
      } else {
        setErrorMsg(errMsg);
        setErrorDetail("");
      }
      stopLoadTimer();
    }
  }, [stopPoll, stopLoadTimer]);

  useEffect(() => {
    mounted.current = true;
    successCalled.current = false;
    // 初始化加载二维码：挂载时必须 setState，属于合法的初始化副作用
    void loadQR(); // eslint-disable-line react-hooks/set-state-in-effect
    return () => { mounted.current = false; stopPoll(); stopLoadTimer(); stopScannedTimer(); };
  }, [loadQR, stopPoll, stopLoadTimer, stopScannedTimer]);

  useEffect(() => {
    if (status !== "ready" && status !== "scanned" && status !== "need_verify") return;
    if (!qr) return;

    let active = true;  // per-effect cancellation flag
    let intervalId: ReturnType<typeof setInterval> | null = null;
    let delayId: ReturnType<typeof setTimeout> | null = null;

    const doPoll = async () => {
      if (!active) return;
      try {
        const res = await douyinApi.pollQRCode(qr.session_key);
        if (!active) return;
        if (res.status === "scanned") {
          if (status !== "scanned") {
            setStatus("scanned");
            // 启动扫码后计时
            setScannedElapsed(0);
            if (scannedTimerRef.current) clearInterval(scannedTimerRef.current);
            scannedTimerRef.current = setInterval(() => {
              setScannedElapsed((prev) => prev + 2);
            }, 2000);
          }
        } else if (res.status === "need_verify") {
          if (status !== "need_verify") {
            setStatus("need_verify");
          }
        } else if (res.status === "confirmed") {
          if (intervalId) clearInterval(intervalId);
          if (scannedTimerRef.current) { clearInterval(scannedTimerRef.current); scannedTimerRef.current = null; }
          setStatus("success");
          if (successCalled.current) return;
          successCalled.current = true;
          // Call onSuccess directly (no setTimeout to avoid unmount race)
          onSuccess(res.session_id || "", {
            uid: res.user_info?.uid || "",
            nickname: res.user_info?.nickname || "抖音用户",
            avatar: res.user_info?.avatar || "",
          });
        } else if (res.status === "expired" || res.status === "error") {
          if (intervalId) clearInterval(intervalId);
          if (scannedTimerRef.current) { clearInterval(scannedTimerRef.current); scannedTimerRef.current = null; }
          setStatus("error");
          setErrorMsg(res.message || "二维码已过期，请刷新重试");
        }
      } catch { /* keep polling */ }
    };

    // Wait 1.5s before first poll
    delayId = setTimeout(() => {
      if (!active) return;
      doPoll();  // immediate first poll
      intervalId = setInterval(doPoll, 2000);
      pollRef.current = intervalId;
    }, 1500);

    return () => {
      active = false;
      if (delayId) clearTimeout(delayId);
      if (intervalId) clearInterval(intervalId);
      pollRef.current = null;
      if (scannedTimerRef.current) { clearInterval(scannedTimerRef.current); scannedTimerRef.current = null; }
    };
  }, [status, qr, onSuccess, stopPoll, loadQR]);

  return (
    <div className="flex flex-col items-center gap-4">
      <p className="text-sm text-[var(--muted)]">使用抖音 App 扫描二维码登录</p>

      {status === "loading" && (
        <div className="flex flex-col items-center gap-3">
          <div className="w-64 h-64 flex items-center justify-center border border-dashed border-[var(--border)] rounded-2xl">
            <div className="w-10 h-10 border-2 border-[#fe2c55] border-t-transparent rounded-full animate-spin" />
          </div>
          <p className="text-sm text-[var(--muted)]">
            {loadElapsed < 5 ? "正在加载二维码..." :
             loadElapsed < 15 ? "即将完成，请稍候..." :
             "加载较慢，请检查网络环境"}
          </p>
          {loadElapsed >= 15 && (
            <button onClick={loadQR} className="btn btn-ghost btn-sm">
              加载太慢？点击刷新
            </button>
          )}
        </div>
      )}

      {(status === "ready" || status === "scanned" || status === "need_verify") && qr && (
        <div className="relative">
          <div className="bg-white rounded-xl">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={`data:image/png;base64,${qr.qrcode_image_base64}`}
              alt="抖音二维码"
              className="w-64 h-64 object-contain rounded-lg"
            />
          </div>
          {status === "scanned" && (
            <div className="absolute inset-0 bg-white/90 rounded-xl flex flex-col items-center justify-center">
              <div className="status-pill ok">已扫码</div>
              <span className="text-sm mt-3 text-[var(--muted)]">请在手机上确认登录</span>
              <p className="text-xs mt-3 text-orange-500 text-center max-w-[220px] leading-relaxed">
                若需身份验证（短信/刷脸/密码），请在手机上完成
              </p>
              {scannedElapsed > 15 && (
                <p className="text-xs mt-3 text-[var(--muted)]">
                  确认后将自动登录，长时间无响应可
                  <button onClick={loadQR} className="underline hover:text-[var(--ink)] ml-1">刷新二维码</button>
                </p>
              )}
            </div>
          )}
          {status === "need_verify" && (
            <div className="absolute inset-0 bg-white/90 rounded-xl flex flex-col items-center justify-center">
              <div className="w-10 h-10 border-2 border-[#fe2c55] border-t-transparent rounded-full animate-spin" />
              <span className="text-sm mt-3 text-[var(--ink)] font-medium">需要二次验证</span>
              <p className="text-xs mt-2 text-[var(--muted)] text-center max-w-[220px] leading-relaxed">
                已弹出浏览器窗口，请在窗口中完成安全验证
              </p>
              <p className="text-xs mt-2 text-[var(--muted)]">
                验证完成后将自动登录
              </p>
            </div>
          )}
        </div>
      )}

      {status === "success" && (
        <div className="w-64 h-64 flex flex-col items-center justify-center">
          <div className="status-pill ok">登录成功</div>
          <p className="text-sm text-[var(--muted)] mt-3">正在进入工作台</p>
        </div>
      )}

      {(status === "ready" || status === "scanned" || status === "need_verify") && (
        <p className="text-xs text-[var(--muted)]">
          <button onClick={loadQR} className="underline hover:text-[var(--ink)]">刷新二维码</button>
        </p>
      )}
      {status === "ready" && (
        <p className="text-xs text-[var(--muted)] text-center max-w-[280px] leading-relaxed">
          扫码后若需身份验证，请在手机上完成
        </p>
      )}
      {status === "error" && (
        <div className="flex flex-col items-center gap-3">
          <p className="text-sm text-red-500 text-center max-w-xs">{errorMsg || "登录失败"}</p>
          <button onClick={loadQR} className="btn btn-primary btn-sm">重新获取</button>
          <p className="text-xs text-[var(--muted)] mt-1">提示：也可用 Cookie 登录</p>
          {errorDetail && (
            <div className="w-full mt-2">
              <button
                onClick={() => setShowDetail(!showDetail)}
                className="text-xs text-[var(--muted)] underline hover:text-[var(--ink)]"
              >
                {showDetail ? "收起诊断信息" : "查看诊断信息"}
              </button>
              {showDetail && (
                <div className="mt-2 p-3 bg-[var(--bg-subtle)] rounded-lg text-xs text-[var(--muted)] font-mono whitespace-pre-wrap break-all max-h-40 overflow-y-auto">
                  {errorDetail.split(" | ").map((line, i) => (
                    <div key={i}>{line}</div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- Main Modal ---------- */
export default function LoginModal({ isOpen, onClose, onBiliSuccess, onDouyinSuccess, defaultTab }: Props) {
  const [tab, setTab] = useState<PlatformTab>(defaultTab || "bilibili");

  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const tabs: { key: PlatformTab; label: string; icon: string; color: string }[] = [
    { key: "bilibili", label: "B站扫码", icon: "📺", color: "var(--accent)" },
    { key: "douyin", label: "抖音扫码", icon: "🎵", color: "#fe2c55" },
  ];

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="modal-card max-w-lg" onClick={(e) => e.stopPropagation()}>

        {/* Tab bar */}
        <div className="login-tabs">
          {tabs.map((t) => (
            <button
              key={t.key}
              className={`login-tab ${tab === t.key ? "active" : ""}`}
              onClick={() => setTab(t.key)}
            >
              <span className="login-tab-icon">{t.icon}</span>
              <span>{t.label}</span>
            </button>
          ))}
        </div>

        {/* Panel content */}
        <div className="login-panel">
          {tab === "bilibili" && onBiliSuccess && (
            <BilibiliPanel onSuccess={onBiliSuccess} />
          )}
          {tab === "bilibili" && !onBiliSuccess && (
            <p className="text-sm text-[var(--muted)]">请从首页登录</p>
          )}
          {tab === "douyin" && onDouyinSuccess && (
            <DouyinPanel onSuccess={onDouyinSuccess} />
          )}
          {tab === "douyin" && !onDouyinSuccess && (
            <p className="text-sm text-[var(--muted)]">请从抖音面板登录</p>
          )}
        </div>

        {/* Footer hint */}
        <p className="login-footer-note">
          {tab === "bilibili" ? "二维码有效期为 2 分钟" : "二维码有效期为 2 分钟 · 也可用 Cookie 登录"}
        </p>
      </div>
    </div>
  );
}
