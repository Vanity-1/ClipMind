"""
系统管理路由 - 卸载全部内容、恢复出厂设置、临时文件清理、备份恢复
"""
import io
import os
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from loguru import logger

from app.config import _data_dir
from app.services.wipe import wipe_all_data

router = APIRouter(prefix="/system", tags=["系统"])


class WipeRequest(BaseModel):
    """清理请求"""
    confirm: bool = False


# 临时文件目录相对路径（均位于数据目录下）
# - uploads: 用户上传待入库的文件
# - tmp: 通用临时目录
# - asr_tmp: ASR 转写过程中的音频临时文件
_TEMP_DIRS = ("uploads", "tmp", "asr_tmp")

# 临时文件过期阈值：超过 1 天（秒）即视为可清理
_TEMP_FILE_MAX_AGE_SECONDS = 24 * 60 * 60


@router.post("/wipe")
async def system_wipe(req: WipeRequest):
    """卸载全部内容 - 清除所有用户数据（保留 ASR 模型）

    清理范围：
    - ChromaDB 向量库（bilibili_videos + douyin_videos）
    - DB 表（user_sessions/video_cache/favorite_folders/favorite_videos/task_records/pending_cleanup）
    - settings.json 配置文件
    - logs/ 日志文件
    - cookie_key.key 加密密钥

    保留：
    - models/ ASR 模型目录
    """
    logger.warning(f"[System] 收到卸载全部内容请求, confirm={req.confirm}")

    if not req.confirm:
        return {
            "success": False,
            "message": "需要确认参数 confirm=true 才能执行清理",
        }

    result = await wipe_all_data(confirm=True)

    if result.get("success"):
        logger.warning("[System] 卸载全部内容完成")
    else:
        logger.error(f"[System] 卸载全部内容部分失败: {result.get('errors')}")

    return result


@router.post("/cleanup/cache")
async def cleanup_cache():
    """清理临时文件和过期日志。

    清理范围（仅限临时文件，不碰数据库和向量库）：
    1. data/uploads/ 下的临时文件（超过 1 天）
    2. data/tmp/ 下的临时文件（超过 1 天）
    3. data/asr_tmp/ 下的临时文件（超过 1 天）

    返回：
        success: 是否成功
        cleaned_files: 已清理的文件数量
        cleaned_size: 已清理的总字节数
        details: 每个目录的清理明细
    """
    data_dir = Path(_data_dir())
    now = time.time()
    total_files = 0
    total_size = 0
    details = {}

    for sub in _TEMP_DIRS:
        sub_dir = data_dir / sub
        cleaned_files = 0
        cleaned_size = 0

        if not sub_dir.exists():
            details[sub] = {"cleaned_files": 0, "cleaned_size": 0}
            continue

        # 仅遍历该目录顶层文件，避免递归误删子目录内容
        for entry in sub_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except OSError:
                # 文件可能在遍历过程中被其他进程删除，跳过
                continue
            # 判断文件是否超过过期阈值
            if (now - stat.st_mtime) < _TEMP_FILE_MAX_AGE_SECONDS:
                continue
            # 清理前累加大小，便于返回给用户
            cleaned_size += stat.st_size
            try:
                entry.unlink()
                cleaned_files += 1
            except OSError as e:
                logger.warning(f"[System] 清理临时文件失败: {entry} ({e})")

        details[sub] = {
            "cleaned_files": cleaned_files,
            "cleaned_size": cleaned_size,
        }
        total_files += cleaned_files
        total_size += cleaned_size

    logger.info(
        f"[System] 临时文件清理完成: 清理 {total_files} 个文件, "
        f"释放 {total_size} 字节"
    )

    return {
        "success": True,
        "cleaned_files": total_files,
        "cleaned_size": total_size,
        "details": details,
    }


@router.post("/backup")
async def backup():
    """打包 data/ 目录为 zip 返回。

    - 不破坏现有数据，只读式打包。
    - 使用 tempfile 创建临时 zip 文件，避免污染 data/ 目录。
    - 跳过恢复前自动备份产生的目录（data_backup_时间戳/），防止递归打包历史备份。
    """
    data_dir = _data_dir()
    data_path = Path(data_dir)

    # 数据目录不存在或为空时直接报错，避免下发空 zip 造成误恢复
    if not data_path.exists() or not any(data_path.iterdir()):
        raise HTTPException(status_code=404, detail="数据目录为空或不存在，无法备份")

    # 临时 zip 文件（关闭后仍保留在磁盘上，由 FileResponse 读取）
    fd, tmp_zip_path = tempfile.mkstemp(prefix="clipmind_backup_", suffix=".zip")
    os.close(fd)

    try:
        with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(data_dir):
                # 跳过恢复前自动备份产生的目录，避免把历史备份一并打包
                dirs[:] = [
                    d for d in dirs
                    if not d.startswith("data_backup_")
                ]
                for file_name in files:
                    abs_path = os.path.join(root, file_name)
                    # arcname 相对于 data_dir，使解压后结构对齐
                    arcname = os.path.relpath(abs_path, data_dir)
                    zf.write(abs_path, arcname)

        logger.info(f"[System] 备份完成: {tmp_zip_path}")
        return FileResponse(
            tmp_zip_path,
            media_type="application/zip",
            filename="clipmind_backup.zip",
        )
    except Exception as e:
        # 出错时立即清理临时文件
        try:
            os.remove(tmp_zip_path)
        except OSError:
            pass
        logger.error(f"[System] 备份失败: {e}")
        raise HTTPException(status_code=500, detail=f"备份失败: {e}")


@router.post("/restore")
async def restore(file: UploadFile = File(...)):
    """接收 zip 解压恢复。

    安全策略：
    - 校验上传文件为合法 zip。
    - 恢复前自动备份当前 data/ 到 data_backup_时间戳/（防误操作）。
    - 解压时防 zip slip（拒绝绝对路径与 .. 路径）。
    """
    data_dir = _data_dir()
    data_path = Path(data_dir)

    # 读取上传内容
    upload_bytes = await file.read()
    if not upload_bytes:
        raise HTTPException(status_code=400, detail="上传文件为空")

    # 校验是 zip 文件（通过 magic number + ZipFile 构造）
    if not upload_bytes.startswith(b"PK\x03\x04") and not upload_bytes.startswith(b"PK\x05\x06"):
        raise HTTPException(status_code=400, detail="文件不是有效的 zip 包")

    try:
        zf = zipfile.ZipFile(io.BytesIO(upload_bytes))
    except zipfile.BadZipFile as e:
        raise HTTPException(status_code=400, detail=f"文件不是有效的 zip 包: {e}")

    # 校验条目，防 zip slip
    unsafe_entries = []
    for name in zf.namelist():
        # 规范化路径用于判断
        norm = os.path.normpath(name)
        if os.path.isabs(norm) or norm.startswith(".."):
            unsafe_entries.append(name)
    if unsafe_entries:
        zf.close()
        raise HTTPException(
            status_code=400,
            detail=f"zip 包含不安全路径: {unsafe_entries[:3]}",
        )

    # 恢复前自动备份当前 data/（如果存在且非空）
    backup_dir_name = None
    if data_path.exists() and any(data_path.iterdir()):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir_name = f"data_backup_{timestamp}"
        backup_path = data_path.parent / backup_dir_name
        try:
            # 若同时间戳备份已存在，附加序号避免覆盖
            idx = 1
            while backup_path.exists():
                backup_path = data_path.parent / f"{backup_dir_name}_{idx}"
                idx += 1
            shutil.copytree(data_path, backup_path)
            logger.info(f"[System] 恢复前自动备份: {backup_path}")
        except Exception as e:
            zf.close()
            logger.error(f"[System] 恢复前备份失败: {e}")
            raise HTTPException(status_code=500, detail=f"恢复前自动备份失败: {e}")

    # 清空当前 data/ 内容（保留目录本身），再解压
    try:
        if data_path.exists():
            for entry in data_path.iterdir():
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        else:
            data_path.mkdir(parents=True, exist_ok=True)

        zf.extractall(data_dir)
        zf.close()
        logger.info(f"[System] 恢复完成, 旧数据备份于: {backup_dir_name}")
    except Exception as e:
        zf.close()
        logger.error(f"[System] 恢复失败: {e}")
        raise HTTPException(status_code=500, detail=f"恢复失败: {e}")

    return {
        "success": True,
        "message": "恢复成功",
        "backup_dir": backup_dir_name,
    }
