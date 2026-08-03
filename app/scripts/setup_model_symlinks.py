"""
ClipMind 模型文件集中管理脚本

将模型从 data/models/ 迁移到集中目录（%LOCALAPPDATA%/ClipMind/models/），
创建符号链接避免数据目录清理时重复下载。

用法：
  python -m app.scripts.setup_model_symlinks          # 执行迁移
  python -m app.scripts.setup_model_symlinks --check  # 仅检查状态
"""
import os
import sys
import shutil
import argparse
from pathlib import Path


def _get_central_models_dir() -> str:
    """获取集中模型目录路径（与项目同盘，确保 junction 可用）。"""
    data_dir = os.environ.get("CLIPMIND_DATA_DIR", "data")
    data_dir = os.path.abspath(data_dir)
    drive = os.path.splitdrive(data_dir)[0]  # 例如 "H:"
    return os.path.join(drive, os.sep, "ClipMindModels")


def _is_symlink_or_junction(path: str) -> bool:
    """检查路径是否是符号链接或 junction。"""
    try:
        if os.path.islink(path):
            return True
    except Exception:
        pass
    try:
        import ctypes
        FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        if attrs != -1 and (attrs & FILE_ATTRIBUTE_REPARSE_POINT):
            return True
    except Exception:
        pass
    return False


def _try_create_junction(link_path: str, target: str) -> bool:
    """创建目录 junction（同盘，不需要管理员权限）。返回是否成功。"""
    import subprocess
    try:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            print(f"[OK] junction 创建成功: {link_path} -> {target}")
            return True
        else:
            print(f"[!] junction 创建失败: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"[!] junction 创建异常: {e}")
        return False


def check_status() -> dict:
    """检查当前模型存储状态。"""
    local_models = os.path.join(os.environ.get("CLIPMIND_DATA_DIR", "data"), "models")
    central_dir = _get_central_models_dir()

    status = {
        "local_models": local_models,
        "central_dir": central_dir,
        "local_exists": os.path.exists(local_models),
        "central_exists": os.path.exists(central_dir),
        "is_symlinked": False,
        "local_models_list": [],
        "central_models_list": [],
    }

    if os.path.exists(local_models):
        status["is_symlinked"] = _is_symlink_or_junction(local_models)
        if os.path.isdir(local_models):
            status["local_models_list"] = [
                d for d in os.listdir(local_models)
                if os.path.isdir(os.path.join(local_models, d))
            ]
    if os.path.isdir(central_dir):
        status["central_models_list"] = [
            d for d in os.listdir(central_dir)
            if os.path.isdir(os.path.join(central_dir, d))
        ]

    return status


def print_status(status: dict):
    """打印状态信息。"""
    print("=" * 60)
    print("  ClipMind 模型存储状态")
    print("=" * 60)
    print(f"  本地目录:  {status['local_models']}")
    print(f"  集中目录:  {status['central_dir']}")
    print(f"  本地存在:  {'是' if status['local_exists'] else '否'}")
    print(f"  集中存在:  {'是' if status['central_exists'] else '否'}")
    print(f"  已符号链接:{'是' if status['is_symlinked'] else '否'}")
    if status["local_models_list"]:
        print(f"  本地模型:  {', '.join(status['local_models_list'])}")
    if status["central_models_list"]:
        print(f"  集中模型:  {', '.join(status['central_models_list'])}")
    print("=" * 60)


def do_migrate() -> bool:
    """执行迁移。返回是否成功创建符号链接。"""
    local_models = os.path.join(os.environ.get("CLIPMIND_DATA_DIR", "data"), "models")
    central_dir = _get_central_models_dir()

    # 确保集中目录存在
    os.makedirs(central_dir, exist_ok=True)

    # 如果本地目录不存在，直接创建 junction
    if not os.path.exists(local_models):
        print("[*] 本地模型目录不存在，直接创建 junction")
        return _try_create_junction(local_models, central_dir)

    # 已 junction，跳过
    if _is_symlink_or_junction(local_models):
        print(f"[*] 模型目录已是 junction，跳过")
        return True

    # 迁移模型目录
    migrated = []
    failed = []
    for name in os.listdir(local_models):
        src = os.path.join(local_models, name)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(central_dir, name)
        if os.path.exists(dst):
            print(f"[=] 集中目录已存在 {name}，跳过迁移")
            # 删除本地副本（已迁移过）
            shutil.rmtree(src, ignore_errors=True)
            migrated.append(name)
            continue
        try:
            print(f"[>] 迁移 {name} ...", end=" ", flush=True)
            shutil.move(src, dst)
            print("OK")
            migrated.append(name)
        except Exception as e:
            print(f"失败: {e}")
            failed.append(name)

    if failed:
        print(f"[!] 部分迁移失败: {', '.join(failed)}，跳过符号链接创建")
        return False

    # 检查本地目录是否清空（只剩 .cache 等隐藏目录）
    remaining = [n for n in os.listdir(local_models) if not n.startswith(".")]
    if remaining:
        print(f"[!] 本地目录仍有未迁移内容: {remaining}，跳过符号链接创建")
        return False

    if not migrated:
        # 本地没有模型，但目录存在（空目录）
        os.rmdir(local_models)
        return _try_create_junction(local_models, central_dir)

    # 用 junction 替换本地目录
    backup = local_models + ".bak"
    try:
        os.rename(local_models, backup)
        if _try_create_junction(local_models, central_dir):
            shutil.rmtree(backup, ignore_errors=True)
            return True
        else:
            # 恢复
            os.rename(backup, local_models)
            return False
    except Exception as e:
        print(f"[!] 目录替换失败: {e}")
        if os.path.exists(backup) and not os.path.exists(local_models):
            os.rename(backup, local_models)
        return False


def main():
    parser = argparse.ArgumentParser(description="ClipMind 模型集中管理工具")
    parser.add_argument("--check", action="store_true", help="仅检查状态，不执行迁移")
    args = parser.parse_args()

    if args.check:
        status = check_status()
        print_status(status)
        return

    status = check_status()
    print_status(status)

    if status["is_symlinked"]:
        print("[*] 模型已集中管理，无需操作")
        return

    print("\n[*] 开始迁移模型到集中目录...")
    success = do_migrate()
    if success:
        print("\n[OK] 模型迁移完成，已启用 junction 集中管理")
        print(f"     模型文件位置: {_get_central_models_dir()}")
        print("     数据目录清理时不再需要重新下载模型")
    else:
        print("\n[!] 模型迁移未完成（junction 创建失败）")
        print("   模型文件仍保留在本地 data/models/ 目录")
        print("   请确保集中目录与项目在同一盘符")


if __name__ == "__main__":
    main()