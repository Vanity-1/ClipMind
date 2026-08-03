"""
Bilibili RAG 知识库系统

B站 API 服务模块
"""
import asyncio
import httpx
import qrcode
import io
import base64
import aiofiles
from typing import Optional, Dict, Any, List
from loguru import logger
from app.services.wbi import wbi_signer
from app.services.tracing import trace_logger


class BilibiliService:
    """B站 API 服务封装"""
    
    BASE_URL = "https://api.bilibili.com"
    PASSPORT_URL = "https://passport.bilibili.com"
    
    # 通用请求头
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
    }
    
    def __init__(
        self,
        sessdata: str = None,
        bili_jct: str = None,
        dedeuserid: str = None,
        use_browser_pool: bool = True,
        qrcode_timeout: int = 10,
        qrcode_retries: int = 2,
    ):
        """
        初始化 B站服务
        
        Args:
            sessdata: B站登录后的 SESSDATA cookie
            bili_jct: B站登录后的 bili_jct cookie (csrf token)
            dedeuserid: B站用户 ID
            use_browser_pool: 是否使用浏览器池进行二维码生成
            qrcode_timeout: 二维码生成超时时间（秒）
            qrcode_retries: 二维码生成重试次数
        """
        self.sessdata = sessdata
        self.bili_jct = bili_jct
        self.dedeuserid = dedeuserid
        self.use_browser_pool = use_browser_pool
        self.qrcode_timeout = qrcode_timeout
        self.qrcode_retries = qrcode_retries
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers=self.HEADERS,
            trust_env=True,
            follow_redirects=True,
        )
    
    def _get_cookies(self) -> Dict[str, str]:
        """获取 Cookie"""
        cookies = {}
        if self.sessdata:
            cookies["SESSDATA"] = self.sessdata
        if self.bili_jct:
            cookies["bili_jct"] = self.bili_jct
        if self.dedeuserid:
            cookies["DedeUserID"] = self.dedeuserid
        return cookies
    
    async def close(self):
        """关闭客户端"""
        await self.client.aclose()
    
    # ==================== 登录相关 ====================
    
    async def generate_qrcode(self) -> Dict[str, Any]:
        """
        生成登录二维码

        优先使用 API 方式（快速、稳定、不依赖浏览器进程），
        失败时回退到浏览器池方式。包含超时控制和重试机制。

        Returns:
            {
                "qrcode_key": "二维码 key",
                "qrcode_url": "二维码内容 URL",
                "qrcode_image_base64": "二维码图片 base64"
            }
        """
        try:
            return await self._generate_qrcode_via_api()
        except Exception as e:
            logger.warning(f"API 方式生成二维码失败，回退到浏览器池方式: {e}")

        if not self.use_browser_pool:
            raise Exception("浏览器池已禁用，且 API 方式生成二维码失败，无可用方案")

        return await self._generate_qrcode_via_browser()

    async def _generate_qrcode_via_browser(self) -> Dict[str, Any]:
        """通过浏览器池从 B站登录页抓取二维码"""
        from app.services.browser_pool import browser_pool

        last_error = None
        for attempt in range(self.qrcode_retries + 1):
            try:
                page = await browser_pool.get_page("bilibili")
                try:
                    result = await asyncio.wait_for(
                        self._bilibili_browser_qrcode(page),
                        timeout=self.qrcode_timeout,
                    )
                    logger.info(f"浏览器池方式生成二维码成功 (尝试 {attempt + 1})")
                    return result
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"二维码生成超时 ({self.qrcode_timeout}s)")
                logger.warning(f"浏览器池二维码超时 (尝试 {attempt + 1}/{self.qrcode_retries + 1})")
            except Exception as e:
                last_error = e
                logger.warning(f"浏览器池二维码失败 (尝试 {attempt + 1}/{self.qrcode_retries + 1}): {e}")

            if attempt < self.qrcode_retries:
                await asyncio.sleep(1)

        raise Exception(f"浏览器池方式生成二维码失败（已重试 {self.qrcode_retries} 次）: {last_error}")

    async def _bilibili_browser_qrcode(self, page) -> Dict[str, Any]:
        """在浏览器页面中提取 B站二维码"""
        await page.goto(
            "https://passport.bilibili.com/login",
            wait_until="domcontentloaded",
            timeout=self.qrcode_timeout * 1000,
        )

        await page.wait_for_selector(
            ".login-scan-box__img, .qrcode-img, img[src*='qrcode']",
            timeout=self.qrcode_timeout * 1000,
        )

        qrcode_key = ""
        try:
            key_result = await page.evaluate("""() => {
                var scripts = document.querySelectorAll('script');
                for (var i = 0; i < scripts.length; i++) {
                    var text = scripts[i].textContent || '';
                    var m = text.match(/qrcode_key['"]\\s*[:=]\\s*['"]([a-zA-Z0-9_-]+)['"]/);
                    if (m) return m[1];
                }
                var html = document.documentElement.outerHTML;
                var m2 = html.match(/qrcode_key['"]\\s*[:=]\\s*['"]([a-zA-Z0-9_-]+)['"]/);
                return m2 ? m2[1] : '';
            }""")
            qrcode_key = key_result or ""
        except Exception:
            pass

        qr_element = await page.query_selector(
            ".login-scan-box__img, .qrcode-img, img[src*='qrcode']"
        )

        if qr_element:
            try:
                screenshot = await qr_element.screenshot(type="png")
            except Exception:
                screenshot = await page.screenshot(type="png")
        else:
            screenshot = await page.screenshot(type="png")

        img_base64 = "data:image/png;base64," + base64.b64encode(screenshot).decode()

        if not qrcode_key:
            qrcode_key = f"bilibili_browser_{int(asyncio.get_event_loop().time())}"

        return {
            "qrcode_key": qrcode_key,
            "qrcode_url": f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}",
            "qrcode_image_base64": img_base64,
        }

    async def _generate_qrcode_via_api(self) -> Dict[str, Any]:
        """通过 API 方式生成二维码（带超时和重试）"""
        last_error = None
        for attempt in range(self.qrcode_retries + 1):
            try:
                url = f"{self.PASSPORT_URL}/x/passport-login/web/qrcode/generate"
                response = await asyncio.wait_for(
                    self.client.get(url),
                    timeout=self.qrcode_timeout,
                )
                data = response.json()

                if data["code"] != 0:
                    raise Exception(f"生成二维码失败: {data['message']}")

                qrcode_key = data["data"]["qrcode_key"]
                qrcode_url = data["data"]["url"]

                qr = qrcode.QRCode(version=1, box_size=10, border=2)
                qr.add_data(qrcode_url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")

                buffer = io.BytesIO()
                img.save(buffer, format="PNG")
                img_base64 = base64.b64encode(buffer.getvalue()).decode()

                logger.info(f"API 方式生成二维码成功 (尝试 {attempt + 1})")
                return {
                    "qrcode_key": qrcode_key,
                    "qrcode_url": qrcode_url,
                    "qrcode_image_base64": f"data:image/png;base64,{img_base64}",
                }
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"API 二维码生成超时 ({self.qrcode_timeout}s)")
                logger.warning(f"API 二维码超时 (尝试 {attempt + 1}/{self.qrcode_retries + 1})")
            except Exception as e:
                last_error = e
                logger.warning(f"API 二维码失败 (尝试 {attempt + 1}/{self.qrcode_retries + 1}): {e}")

            if attempt < self.qrcode_retries:
                await asyncio.sleep(1)

        raise Exception(f"API 方式生成二维码失败（已重试 {self.qrcode_retries} 次）: {last_error}")
    
    async def poll_qrcode_status(self, qrcode_key: str) -> Dict[str, Any]:
        """
        轮询二维码登录状态
        
        Args:
            qrcode_key: 二维码 key
            
        Returns:
            {
                "status": "waiting" | "scanned" | "confirmed" | "expired",
                "message": "状态描述",
                "cookies": {...} (仅在 confirmed 时有值)
            }
        """
        url = f"{self.PASSPORT_URL}/x/passport-login/web/qrcode/poll"
        response = await self.client.get(url, params={"qrcode_key": qrcode_key})
        data = response.json()
        
        if data["code"] != 0:
            raise Exception(f"轮询二维码状态失败: {data['message']}")
        
        inner_code = data["data"]["code"]
        message = data["data"]["message"]
        
        status_map = {
            86101: ("waiting", "等待扫码"),
            86090: ("scanned", "已扫码，等待确认"),
            86038: ("expired", "二维码已过期"),
            0: ("confirmed", "登录成功")
        }
        
        status, msg = status_map.get(inner_code, ("unknown", message))
        
        result = {
            "status": status,
            "message": msg
        }
        
        # 登录成功时，从响应头中提取 cookies
        if status == "confirmed":
            cookies = {}
            for cookie in response.cookies.jar:
                cookies[cookie.name] = cookie.value
            
            # 也可能在 URL 中
            url_str = data["data"].get("url", "")
            if "SESSDATA=" in url_str:
                # 从 URL 解析 cookies
                import urllib.parse
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url_str).query)
                for key in ["SESSDATA", "bili_jct", "DedeUserID"]:
                    if key in parsed:
                        cookies[key] = parsed[key][0]
            
            result["cookies"] = cookies
            result["refresh_token"] = data["data"].get("refresh_token", "")
        
        return result
    
    async def get_user_info(self) -> Dict[str, Any]:
        """
        获取当前登录用户信息
        
        Returns:
            用户信息字典
        """
        url = f"{self.BASE_URL}/x/web-interface/nav"
        response = await self.client.get(url, cookies=self._get_cookies())
        data = response.json()
        
        if data["code"] != 0:
            raise Exception(f"获取用户信息失败: {data['message']}")
        
        return data["data"]
    
    # ==================== 收藏夹相关 ====================
    
    async def get_user_favorites(self, mid: int = None) -> List[Dict[str, Any]]:
        """
        获取用户的所有收藏夹
        
        Args:
            mid: 用户 ID，不传则使用当前登录用户
            
        Returns:
            收藏夹列表
        """
        if mid is None:
            mid = self.dedeuserid
            
        if not mid:
            raise Exception("未指定用户 ID")
        
        url = f"{self.BASE_URL}/x/v3/fav/folder/created/list-all"
        params = {"up_mid": mid}
        
        response = await self.client.get(url, params=params, cookies=self._get_cookies())
        data = response.json()
        
        if data["code"] != 0:
            raise Exception(f"获取收藏夹失败: {data['message']}")
        
        return data["data"]["list"] or []
    
    async def get_favorite_content(
        self,
        media_id: int,
        pn: int = 1,
        ps: int = 20,
        order: str = "mtime",
    ) -> Dict[str, Any]:
        """
        获取收藏夹内容

        Args:
            media_id: 收藏夹 ID
            pn: 页码
            ps: 每页数量 (最大20)
            order: 排序方式，``mtime`` 按修改（收藏）时间倒序（最新在前），
                ``view`` 按播放量。默认 ``mtime`` 以支持增量同步提前 break。

        Returns:
            {
                "info": 收藏夹信息,
                "medias": 视频列表,
                "has_more": 是否有更多
            }
        """
        url = f"{self.BASE_URL}/x/v3/fav/resource/list"
        params = {
            "media_id": media_id,
            "pn": pn,
            "ps": min(ps, 20),
            "order": order,
            "platform": "web"
        }

        response = await self.client.get(url, params=params, cookies=self._get_cookies())
        data = response.json()

        if data["code"] != 0:
            raise Exception(f"获取收藏夹内容失败: {data['message']}")

        return {
            "info": data["data"]["info"],
            "medias": data["data"]["medias"] or [],
            "has_more": data["data"]["has_more"]
        }

    async def get_all_favorite_videos(
        self,
        media_id: int,
        last_sync_at: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取收藏夹的所有视频

        Args:
            media_id: 收藏夹 ID
            last_sync_at: 上次同步的最大 fav_time（Unix 时间戳，秒）。
                - 为 None（首次同步）时走全量遍历。
                - 非 None 时按 ``order=mtime`` 倒序拉取，遍历每个视频的
                  ``fav_time`` 字段，遇到 ``fav_time <= last_sync_at`` 提前
                  break（后续都是更老的视频，无需再拉）。

        Returns:
            视频列表。增量模式下仅包含 ``fav_time > last_sync_at`` 的新增视频。
        """
        all_videos = []
        pn = 1
        incremental = last_sync_at is not None

        while True:
            # 增量同步强制 order=mtime（按收藏时间倒序，最新在前），全量同步保持原 mtime 行为
            result = await self.get_favorite_content(media_id, pn=pn, ps=20, order="mtime")
            medias = result["medias"]

            if incremental and medias:
                # 遇到 fav_time <= last_sync_at 提前 break：order=mtime 保证后续都是更老的
                hit_old = False
                for media in medias:
                    fav_time = media.get("fav_time")
                    if fav_time is None:
                        # 缺少 fav_time 字段时无法判断新旧，保守纳入避免漏同步
                        all_videos.append(media)
                        continue
                    try:
                        fav_time_int = int(fav_time)
                    except (ValueError, TypeError):
                        all_videos.append(media)
                        continue
                    if fav_time_int <= last_sync_at:
                        # 命中已同步边界，后续视频更老，提前终止
                        hit_old = True
                        break
                    all_videos.append(media)
                if hit_old:
                    break
            else:
                all_videos.extend(medias)

            if not result["has_more"]:
                break
            pn += 1

            # 避免请求过快
            import asyncio
            await asyncio.sleep(0.3)

        return all_videos

    async def move_favorite_resources(
        self,
        src_media_id: int,
        tar_media_id: int,
        resources: List[str],
    ) -> Dict[str, Any]:
        """
        批量移动收藏夹内容

        Args:
            src_media_id: 源收藏夹 ID
            tar_media_id: 目标收藏夹 ID
            resources: ["avid:type", ...]
        """
        if not self.bili_jct:
            raise Exception("缺少 bili_jct，无法进行收藏夹移动")

        if not resources:
            return {"moved": 0}

        url = f"{self.BASE_URL}/x/v3/fav/resource/move"
        data = {
            "src_media_id": src_media_id,
            "tar_media_id": tar_media_id,
            "resources": ",".join(resources),
            "csrf": self.bili_jct,
        }
        if self.dedeuserid:
            data["mid"] = self.dedeuserid

        response = await self.client.post(url, data=data, cookies=self._get_cookies())
        result = response.json()
        if result.get("code") != 0:
            raise Exception(f"移动收藏夹内容失败: {result.get('message')}")
        return result.get("data") or {}

    async def clean_favorite_resources(self, media_id: int) -> Dict[str, Any]:
        """
        清理收藏夹失效内容
        """
        if not self.bili_jct:
            raise Exception("缺少 bili_jct，无法清理失效内容")

        url = f"{self.BASE_URL}/x/v3/fav/resource/clean"
        data = {"media_id": media_id, "csrf": self.bili_jct}
        response = await self.client.post(url, data=data, cookies=self._get_cookies())
        result = response.json()
        if result.get("code") != 0:
            raise Exception(f"清理失效内容失败: {result.get('message')}")
        return result.get("data") or {}
    
    # ==================== 视频信息相关 ====================
    
    async def get_video_info(self, bvid: str) -> Dict[str, Any]:
        """
        获取视频详细信息
        
        Args:
            bvid: 视频 BV 号
            
        Returns:
            视频信息字典
        """
        url = f"{self.BASE_URL}/x/web-interface/view"
        params = {"bvid": bvid}
        
        response = await self.client.get(url, params=params, cookies=self._get_cookies())
        data = response.json()
        
        if data["code"] != 0:
            raise Exception(f"获取视频信息失败: {data['message']}")
        
        return data["data"]
    
    async def get_video_summary(self, bvid: str, cid: int, up_mid: int = None) -> Dict[str, Any]:
        """
        获取视频 AI 摘要
        
        Args:
            bvid: 视频 BV 号
            cid: 视频 cid
            up_mid: UP主 ID (可选)
            
        Returns:
            AI 摘要信息
        """
        url = f"{self.BASE_URL}/x/web-interface/view/conclusion/get"
        
        params = {
            "bvid": bvid,
            "cid": cid,
        }
        if up_mid:
            params["up_mid"] = up_mid
        
        # 需要 Wbi 签名
        signed_params = await wbi_signer.sign(params, cookies=self._get_cookies())
        
        response = await self.client.get(
            url, 
            params=signed_params, 
            cookies=self._get_cookies()
        )
        data = response.json()
        
        if data["code"] != 0:
            trace_logger.warning(f"获取视频摘要失败 [{bvid}]: {data.get('message', 'unknown error')}")
            return None
        
        return data["data"]
    
    async def get_player_info(self, bvid: str, cid: int, aid: int = None) -> Dict[str, Any]:
        """
        获取播放器信息（包含字幕信息）
        
        Args:
            bvid: 视频 BV 号
            cid: 视频 cid
            aid: 视频 aid (可选)
            
        Returns:
            播放器信息
        """
        params = {
            "bvid": bvid,
            "cid": cid,
        }
        if aid:
            params["aid"] = aid

        # 优先使用 WBI 版本，提高字幕获取成功率
        try:
            cookies = self._get_cookies()
            cookies_for_sign = cookies if cookies else None
            signed_params = await wbi_signer.sign(params, cookies=cookies_for_sign)
            wbi_url = f"{self.BASE_URL}/x/player/wbi/v2"
            response = await self.client.get(wbi_url, params=signed_params, cookies=cookies)
            data = response.json()
            if data.get("code") == 0:
                return data.get("data")
            trace_logger.warning(f"WBI 播放器信息失败 [{bvid}]: {data.get('message', 'unknown error')}")
        except Exception as e:
            trace_logger.warning(f"WBI 播放器信息异常 [{bvid}]: {e}")

        # 回退到普通接口
        url = f"{self.BASE_URL}/x/player/v2"
        response = await self.client.get(url, params=params, cookies=self._get_cookies())
        data = response.json()

        if data["code"] != 0:
            trace_logger.warning(f"获取播放器信息失败 [{bvid}]: {data.get('message', 'unknown error')}")
            return None

        return data["data"]

    async def get_audio_url(self, bvid: str, cid: int) -> Optional[str]:
        """
        获取音频流 URL（用于 ASR）
        
        Args:
            bvid: 视频 BV 号
            cid: 视频 cid
            
        Returns:
            音频 URL（可能为空）
        """
        params = {
            "bvid": bvid,
            "cid": cid,
            "fnval": 16,
            "fnver": 0,
            "fourk": 1,
        }

        cookies = self._get_cookies()
        cookies_for_sign = cookies if cookies else None

        # 优先使用 WBI 接口
        try:
            signed_params = await wbi_signer.sign(params, cookies=cookies_for_sign)
            url = f"{self.BASE_URL}/x/player/wbi/playurl"
            response = await self.client.get(url, params=signed_params, cookies=cookies)
            data = response.json()
        except Exception as e:
            trace_logger.warning(f"获取音频信息失败(WBI) [{bvid}]: {e}")
            data = None

        # 回退到普通接口
        if not data or data.get("code") != 0:
            try:
                url = f"{self.BASE_URL}/x/player/playurl"
                response = await self.client.get(url, params=params, cookies=cookies)
                data = response.json()
            except Exception as e:
                trace_logger.warning(f"获取音频信息失败 [{bvid}]: {e}")
                return None

        if data.get("code") != 0:
            trace_logger.warning(f"获取音频信息失败 [{bvid}]: {data.get('message', 'unknown error')}")
            return None

        payload = data.get("data") or {}
        dash = payload.get("dash") or {}
        audio_list = dash.get("audio") or []
        if audio_list:
            def _bw(item) -> int:
                value = item.get("bandwidth") or item.get("bandWidth") or 0
                try:
                    return int(value)
                except Exception:
                    return 0

            # 优先选择 <= 96kbps 的最高档，兼顾速度与识别效果；否则选最低带宽兜底
            max_bw = 64_000
            candidates = [a for a in audio_list if _bw(a) > 0]
            if candidates:
                preferred = [a for a in candidates if _bw(a) <= max_bw]
                if preferred:
                    best = max(preferred, key=_bw)
                else:
                    best = min(candidates, key=_bw)
            else:
                best = audio_list[0]
            return best.get("baseUrl") or best.get("base_url") or best.get("url")

        durl = payload.get("durl") or []
        if durl:
            return durl[0].get("url")

        return None

    async def download_subtitle(self, subtitle_url: str) -> str:
        """
        下载字幕文件
        
        Args:
            subtitle_url: 字幕 URL
            
        Returns:
            字幕文本
        """
        # 处理协议
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        
        response = await self.client.get(subtitle_url)
        data = response.json()
        
        # 拼接字幕文本
        texts = []
        for item in data.get("body", []):
            content = item.get("content", "")
            if content:
                texts.append(content)
        
        return "\n".join(texts)

    async def download_audio_to_file(self, audio_url: str, file_path: str) -> bool:
        """
        下载音频流到本地文件（带 Cookie 与 Referer）
        
        Args:
            audio_url: 音频 URL
            file_path: 本地保存路径
            
        Returns:
            是否下载成功
        """
        if not audio_url:
            return False

        headers = dict(self.HEADERS)
        cookies = self._get_cookies()

        try:
            async with self.client.stream(
                "GET", audio_url, headers=headers, cookies=cookies
            ) as resp:
                if resp.status_code not in (200, 206):
                    trace_logger.warning(
                        f"下载音频失败: status_code={resp.status_code} url={audio_url}"
                    )
                    return False
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        await f.write(chunk)
            return True
        except Exception as e:
            trace_logger.warning(f"下载音频异常: {e}")
            return False
