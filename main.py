import os
import re
import json
import uuid
import asyncio
import aiohttp
from pathlib import Path
from typing import Optional, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, Image, Video


@register("douyin_parser", "YourName", "抖音分享链接自动解析插件", "1.4.1")
class DouyinParser(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 统一使用 pathlib.Path 处理路径
        self.data_dir = StarTools.get_data_dir() / "plugin_data" / "douyin_parser"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        '''监听所有消息，提取并解析抖音链接'''
        message_str = event.message_str

        urls = re.findall(r'https://v\.douyin\.com/[a-zA-Z0-9]+/?', message_str)
        if not urls:
            return

        for url in urls:
            # 1. 获取视频详细数据
            item = await self.fetch_video_info(url)
            if not item:
                yield event.plain_result("❌ 无法获取抖音视频详情，可能是链接失效或风控。")
                continue

            # 2. 构造并秒发文本详情卡片
            info_text = self.build_detail_md(item)
            yield event.plain_result(info_text)

            # 3. 读取配置阈值
            try:
                threshold = int(self.config.get("max_video_duration", 300))
            except:
                threshold = 300

            # 4. 根据媒体类型和时长进行分发处理
            images = item.get('images')
            duration_sec = item.get('video', {}).get('duration', 0) / 1000

            if images:
                # 处理图集
                async for res in self.handle_images_send(event, item):
                    yield res
            elif duration_sec > threshold:
                # 处理超长视频（仅发直链提示）
                async for res in self.handle_long_video_send(event, item, duration_sec, threshold):
                    yield res
            else:
                # 处理普通短视频
                async for res in self.handle_video_send(event, item):
                    yield res

    async def fetch_video_info(self, url: str) -> Optional[Dict[str, Any]]:
        '''获取并解析网页中的 JSON 数据'''
        headers = {
            'user-agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36',
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, allow_redirects=True) as resp:
                    if resp.status != 200:
                        return None
                    res = await resp.text()

                data_match = re.findall(r'window\._ROUTER_DATA\s*=\s*(.*?)</script>', res)
                if data_match:
                    json_data = json.loads(data_match[0])
                    return json_data['loaderData']['video_(id)/page']['videoInfoRes']['item_list'][0]
        except Exception as e:
            logger.error(f"抖音 JSON 获取异常: {e}")
        return None

    def build_detail_md(self, item: dict) -> str:
        '''构建信息文本'''
        title = item.get('desc', '无标题').strip()
        nickname = item.get('author', {}).get('nickname', '未知博主')
        stats = item.get('statistics', {})

        return (
            f"🎬 标题: {title}\n"
            f"👤 博主: {nickname}\n"
            f"❤️ 点赞: {stats.get('digg_count', 0)} | 💬 评论: {stats.get('comment_count', 0)}\n"
            f"🔄 分享: {stats.get('share_count', 0)} | ⭐ 收藏: {stats.get('collect_count', 0)}"
        )

    async def handle_video_send(self, event: AstrMessageEvent, item: dict):
        '''处理并发送短视频'''
        video_uri = item.get('video', {}).get('play_addr', {}).get('uri', '')
        custom_url = f"https://www.douyin.com/aweme/v1/play/?video_id={video_uri}"
        fallback_urls = item.get('video', {}).get('play_addr', {}).get('url_list', [])

        download_urls = [custom_url] + fallback_urls

        yield event.plain_result("🚀 正在下载视频文件...")
        path = await self.download_file_robust(download_urls, ".mp4")

        if path:
            try:
                yield event.chain_result([Video.fromFileSystem(str(path))])
            finally:
                if path.exists():
                    path.unlink()
                    logger.info(f"已清理视频临时文件: {path}")
        else:
            yield event.plain_result("❌ 视频下载失败（可能被风控拦截）。")

    async def handle_long_video_send(self, event: AstrMessageEvent, item: dict, duration: float, threshold: int):
        '''处理超长视频，仅发送直链提示'''
        video_uri = item.get('video', {}).get('play_addr', {}).get('uri', '')
        custom_url = f"https://www.douyin.com/aweme/v1/play/?video_id={video_uri}"

        warning_text = (
            f"⚠️ 视频时长({duration:.1f}s)超过设定阈值({threshold}s)，为避免刷屏，请点击直链观看：\n"
            f"🔗 直链: {custom_url}"
        )
        yield event.plain_result(warning_text)

    async def handle_images_send(self, event: AstrMessageEvent, item: dict):
        '''处理图集，发送首图'''
        images = item.get('images', [])
        if not images: return

        cover_urls = images[0].get('url_list', [])
        path = await self.download_file_robust(cover_urls, ".jpg")

        if path:
            try:
                yield event.chain_result([
                    Plain("📷 类型: 图集 (已为您提取首图)\n"),
                    Image.fromFileSystem(str(path))
                ])
            finally:
                if path.exists():
                    path.unlink()
                    logger.info(f"已清理图集临时文件: {path}")

    async def download_file_robust(self, urls: list, suffix: str) -> Optional[Path]:
        '''多策略轮询下载媒体文件，返回本地 Path 对象'''
        header_strategies = [
            {
                'User-Agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U) AppleWebKit/537.36 Chrome/116.0.0.0 Mobile Safari/537.36',
                'Referer': 'https://www.douyin.com/'},
            {
                'User-Agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U) AppleWebKit/537.36 Chrome/116.0.0.0 Mobile Safari/537.36'},
            {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'}
        ]

        filename = f"{uuid.uuid4().hex}{suffix}"
        file_path = self.data_dir / filename

        async with aiohttp.ClientSession() as session:
            for url in urls:
                if not url: continue
                for headers in header_strategies:
                    try:
                        async with session.get(url, headers=headers, allow_redirects=True, timeout=15) as resp:
                            if resp.status == 200:
                                with open(file_path, 'wb') as f:
                                    async for chunk in resp.content.iter_chunked(8192):
                                        f.write(chunk)
                                return file_path
                    except Exception as e:
                        continue
        return None