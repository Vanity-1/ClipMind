"use client";

import { useState, useCallback, useSyncExternalStore } from "react";

type Theme = "dark" | "light";

// 客户端挂载检测：用 useSyncExternalStore 实现 client-only 渲染，
// 避免 effect 内 setState（react-hooks/set-state-in-effect 规则）。
// - server snapshot（构建时预渲染）返回 false
// - client snapshot（浏览器首帧）返回 true
// React 自动处理 hydration 一致性，无需手动 mounted state。
const _emptySubscribe = () => () => {};
function useIsClient() {
  return useSyncExternalStore(_emptySubscribe, () => true, () => false);
}

/**
 * 亮暗主题切换按钮。
 * - dark：默认（无 data-theme 属性，使用 :root 暗色变量）
 * - light：设置 <html data-theme="light">，覆盖为亮色变量
 * 偏好持久化到 localStorage，layout.tsx 中的内联脚本在首屏前已应用，避免闪烁。
 */
export default function ThemeToggle() {
  // 惰性初始化：首屏前 layout.tsx 内联脚本已设置 data-theme，
  // 在初始化阶段读取，避免 effect 内同步 setState 触发级联渲染。
  // SSR 阶段 document 不可用，统一返回 "dark" 占位，hydration 后由 mounted 修正。
  const [theme, setTheme] = useState<Theme>(() => {
    if (typeof document === "undefined") return "dark";
    return document.documentElement.getAttribute("data-theme") === "light"
      ? "light"
      : "dark";
  });
  const mounted = useIsClient();

  const toggle = useCallback(() => {
    setTheme((prev) => {
      const next: Theme = prev === "dark" ? "light" : "dark";
      try {
        if (next === "light") {
          document.documentElement.setAttribute("data-theme", "light");
          localStorage.setItem("theme", "light");
        } else {
          document.documentElement.removeAttribute("data-theme");
          localStorage.setItem("theme", "dark");
        }
      } catch {
        // 隐私模式或权限受限时忽略
      }
      return next;
    });
  }, []);

  // 首次渲染前用占位，避免 hydration mismatch
  if (!mounted) {
    return <button className="btn-icon" aria-label="切换主题" style={{ visibility: "hidden" }} />;
  }

  const isLight = theme === "light";

  return (
    <button
      onClick={toggle}
      className="btn-icon"
      title={isLight ? "切换到暗色模式" : "切换到亮色模式"}
      aria-label={isLight ? "切换到暗色模式" : "切换到亮色模式"}
    >
      {isLight ? (
        // 月亮图标（当前亮色，点击切到暗色）
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      ) : (
        // 太阳图标（当前暗色，点击切到亮色）
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
        </svg>
      )}
    </button>
  );
}
