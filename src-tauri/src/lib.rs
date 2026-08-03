use std::io::{BufRead, BufReader};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

/// 后端进程状态：存储子进程句柄，用于退出时清理
struct BackendState(Mutex<Option<Child>>);

const BACKEND_URL: &str = "http://127.0.0.1:8000";
const HEALTH_TIMEOUT_SECS: u64 = 30;

/// 获取用户数据目录（Windows: %APPDATA%/ClipMind/data）
fn get_data_dir() -> String {
    #[cfg(target_os = "windows")]
    {
        let appdata = std::env::var("APPDATA").unwrap_or_else(|_| ".".to_string());
        format!("{}\\ClipMind\\data", appdata)
    }
    #[cfg(target_os = "macos")]
    {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        format!("{}/Library/Application Support/ClipMind/data", home)
    }
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        format!("{}/.clipmind/data", home)
    }
}

/// 轮询后端 /health 端点，等待服务就绪
///
/// 关键：reqwest 必须设置 .no_proxy()，否则系统代理会拦截 localhost 请求，
/// 导致健康检查永远失败（用户开启代理软件时尤为明显）。
fn wait_for_backend() -> bool {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(2))
        .no_proxy()
        .build()
        .unwrap();

    let start = std::time::Instant::now();
    let timeout = std::time::Duration::from_secs(HEALTH_TIMEOUT_SECS);

    while start.elapsed() < timeout {
        if let Ok(resp) = client.get(format!("{}/health", BACKEND_URL)).send() {
            if resp.status().is_success() {
                return true;
            }
        }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    false
}

/// 解析后端可执行文件路径（资源目录下的 clipmind-backend 子目录）
fn resolve_backend_exe(resource_dir: &std::path::Path) -> std::path::PathBuf {
    let backend_dir = resource_dir.join("resources").join("clipmind-backend");
    #[cfg(target_os = "windows")]
    {
        backend_dir.join("clipmind-backend.exe")
    }
    #[cfg(not(target_os = "windows"))]
    {
        backend_dir.join("clipmind-backend")
    }
}

/// 创建主窗口
///
/// 窗口先以隐藏状态创建（加载 Tauri 内置的占位 index.html，即 frontend/out/index.html），
/// 等后端就绪后通过 webview.eval 导航到后端 URL，实现前后端同源。
///
/// 同源架构的好处：
/// 1. 前端用相对路径请求 API，彻底绕开 localhost/127.0.0.1 代理拦截问题
/// 2. 同源请求不受系统代理影响（浏览器安全策略优先于代理配置）
/// 3. 只要本机后端可访问，任何网络环境（含代理/VPN）都能正常使用
///
/// 健康检查失败时窗口仍加载本地占位页（不导航到不可达的后端 URL），
/// 用户看到的是前端的"无法连接后端"提示，而不是浏览器的 ERR_CONNECTION_REFUSED。
fn create_main_window(app: &tauri::App) -> tauri::Result<()> {
    let window_builder = WebviewWindowBuilder::new(
        app,
        "main",
        WebviewUrl::App("index.html".into()),
    )
    .title("ClipMind — 收藏夹知识库")
    .inner_size(1200.0, 800.0)
    .min_inner_size(900.0, 600.0)
    .resizable(true)
    .fullscreen(false)
    .decorations(true)
        .visible(false);

    // Windows: 让本地地址绕过系统代理，确保后端健康检查和窗口导航不受代理影响。
    //
    // 虽然 Tauri Rust 端的 reqwest 已用 .no_proxy()，但 WebView2 本身仍走系统代理。
    // 这里保留 bypass list 作为双重保障：即使代理软件异常配置，本地地址也不被转发。
    // bypass list 显式包含 127.0.0.1 和 localhost，两者均可被前端 fetch 命中。
    #[cfg(target_os = "windows")]
    let window_builder = {
        window_builder.additional_browser_args(
            "--proxy-bypass-list=<local>;127.0.0.1;localhost"
        )
    };

    window_builder.build()?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(BackendState(Mutex::new(None)))
        .setup(|app| {
            // 创建主窗口（隐藏状态，等后端就绪后再显示）
            create_main_window(app)?;

            let resource_dir = app.path().resource_dir()?;

            // 后端 exe 路径
            let backend_exe = resolve_backend_exe(&resource_dir);
            let backend_dir = backend_exe.parent().unwrap_or(&resource_dir);

            // 资源子目录
            let chromium_dir = resource_dir.join("resources").join("chromium");
            let ffmpeg_dir = resource_dir.join("resources").join("ffmpeg");
            // 打包内置的 faster-whisper 模型目录（仅"带 ASR 模型版"包含）
            // 轻量版该目录不存在，asr.py 会自动跳过并回退到网络下载
            let bundled_models_dir = resource_dir.join("resources").join("data").join("models");

            // 用户数据目录
            let data_dir = get_data_dir();
            std::fs::create_dir_all(&data_dir).ok();

            // 构建 PATH：把 ffmpeg 和 backend 目录加入搜索路径
            let original_path = std::env::var("PATH").unwrap_or_default();
            let sep = if cfg!(windows) { ";" } else { ":" };
            let new_path = format!(
                "{}{}{}{}{}",
                ffmpeg_dir.display(),
                sep,
                backend_dir.display(),
                sep,
                original_path
            );

            // 启动后端进程
            let mut cmd = Command::new(&backend_exe);
            cmd.env("CLIPMIND_DATA_DIR", &data_dir)
                .env("CLIPMIND_BUNDLED_MODELS_DIR", &bundled_models_dir)
                .env("PLAYWRIGHT_BROWSERS_PATH", &chromium_dir)
                .env("PATH", &new_path)
                .current_dir(backend_dir)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());

            // Windows: CREATE_NO_WINDOW 避免控制台窗口弹出
            #[cfg(target_os = "windows")]
            {
                use std::os::windows::process::CommandExt;
                const CREATE_NO_WINDOW: u32 = 0x08000000;
                cmd.creation_flags(CREATE_NO_WINDOW);
            }

            let mut child = cmd.spawn().expect("failed to spawn clipmind-backend");

            // 后台线程转发后端 stdout
            if let Some(stdout) = child.stdout.take() {
                std::thread::spawn(move || {
                    for line in BufReader::new(stdout).lines() {
                        if let Ok(line) = line {
                            println!("[backend] {}", line);
                        }
                    }
                });
            }

            // 后台线程转发后端 stderr
            if let Some(stderr) = child.stderr.take() {
                std::thread::spawn(move || {
                    for line in BufReader::new(stderr).lines() {
                        if let Ok(line) = line {
                            eprintln!("[backend] {}", line);
                        }
                    }
                });
            }

            // 存储子进程句柄
            let state = app.state::<BackendState>();
            *state.0.lock().unwrap() = Some(child);

            // 在独立线程中等待后端就绪，然后导航到后端 URL 并显示窗口
            //
            // 同源架构：窗口导航到 http://127.0.0.1:8000（后端托管的 SPA），
            // 前端用相对路径请求 API，彻底绕开代理拦截问题。
            // 使用 127.0.0.1：Rust reqwest 已用 .no_proxy() 绕过代理，
            // WebView2 有 --proxy-bypass-list 双重保障（显式包含 127.0.0.1 和 localhost）。
            //
            // 健康检查失败时：保留本地占位页（Tauri 内置 frontend/out/index.html），
            // 不导航到不可达的后端 URL。前端会显示"无法连接后端"提示，
            // 用户可看到应用框架而非浏览器的 ERR_CONNECTION_REFUSED。
            let app_handle = app.handle().clone();
            tauri::async_runtime::spawn_blocking(move || {
                let ready = wait_for_backend();
                if let Some(window) = app_handle.get_webview_window("main") {
                    if ready {
                        println!("[clipmind] backend ready, navigating to backend URL");
                        // 导航到后端托管的 SPA（同源），前端将用相对路径请求 API。
                        // 用 window.eval 执行 JS 跳转：Tauri 2 的 eval 可在任意线程调用
                        // （内部通过 channel 转发到主线程执行）。
                        let js = format!(
                            "window.location.replace('{}');",
                            BACKEND_URL
                        );
                        let _ = window.eval(&js);
                    } else {
                        // 健康检查超时：保留本地占位页，让前端展示连接错误状态
                        eprintln!("[clipmind] backend health check timeout, keeping local placeholder page");
                    }
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            // 主窗口关闭时终止后端
            if let tauri::WindowEvent::Destroyed = event {
                let app = window.app_handle();
                // 先在独立作用域中取出 child，避免 MutexGuard 生命周期问题
                let child = app.state::<BackendState>().0.lock().unwrap().take();
                if let Some(mut child) = child {
                    let _ = child.kill();
                    let _ = child.wait();
                    println!("[clipmind] backend process killed");
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running ClipMind application");
}
