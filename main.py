import os
import re
import json
import uuid
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


@register("douyin_parser", "YourName", "抖音分享链接自动解析插件", "1.3.0")
class DouyinParser(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.plugin_data_path = os.path.join(get_astrbot_data_path(), "plugin_data", "douyin_parser")
        os.makedirs(self.plugin_data_path, exist_ok=True)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        '''监听所有消息，提取并解析抖音链接'''
        message_str = event.message_str

        urls = re.findall(r'https://v\.douyin\.com/[a-zA-Z0-9]+/?', message_str)
        if not urls:
            return

        for url in urls:
            file_to_delete = None
            try:
                # 接收拆分后的两个消息链：文字信息链、独立媒体链
                info_chain, media_chain, file_to_delete = await self.parse_douyin(url)

                # 1. 先发送文本信息（如果是图文，也在这里发送）
                if info_chain:
                    yield event.chain_result(info_chain)

                # 2. 再单独发送视频/图片本体，适配 QQ 视频独占的规则
                if media_chain:
                    yield event.chain_result(media_chain)

            except Exception as e:
                logger.error(f"抖音链接解析失败: {e}")
                yield event.plain_result(f"⚠️ 抖音解析出错了: {str(e)}")

            finally:
                # 等两个 yield 都执行（即消息都发送完毕）后，清理临时文件
                if file_to_delete and os.path.exists(file_to_delete):
                    try:
                        os.remove(file_to_delete)
                        logger.info(f"已清理临时文件: {file_to_delete}")
                    except Exception as e:
                        logger.error(f"清理临时文件失败: {file_to_delete}, 错误: {e}")

    async def _download_media_robust(self, session: aiohttp.ClientSession, urls: list, file_path: str) -> bool:
        '''多策略轮询下载媒体文件，专治 403 Forbidden'''
        header_strategies = [
            {
                'User-Agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36',
                'Referer': 'https://www.douyin.com/'
            },
            {
                'User-Agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36'
            },
            {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
        ]

        for url in urls:
            if not url:
                continue

            for headers in header_strategies:
                try:
                    async with session.get(url, headers=headers, allow_redirects=True, timeout=15) as resp:
                        if resp.status == 200:
                            with open(file_path, 'wb') as f:
                                async for chunk in resp.content.iter_chunked(8192):
                                    f.write(chunk)
                            return True
                        else:
                            continue
                except Exception as e:
                    continue
        return False

    async def parse_douyin(self, url: str) -> tuple[list, list, str]:
        '''
        提取抖音信息并分发下载任务。
        返回: (信息消息链, 独立媒体消息链, 下载到本地的临时文件路径)
        '''
        init_headers = {
            'user-agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36',
        }

        async with aiohttp.ClientSession() as session:
            # 1. 获取网页 JSON 数据
            async with session.get(url, headers=init_headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise Exception(f"页面请求失败，状态码: {resp.status}")
                res = await resp.text()

            data_match = re.findall(r'window\._ROUTER_DATA\s*=\s*(.*?)</script>', res)
            if not data_match:
                raise Exception("页面未包含 JSON 数据，可能是频繁请求被拦截，或链接已失效。")

            json_data = json.loads(data_match[0])
            item = json_data['loaderData']['video_(id)/page']['videoInfoRes']['item_list'][0]

            # 2. 提取基本信息
            title = item.get('desc', '无标题')
            nickname = item['author'].get('nickname', '未知博主')
            stats = item.get('statistics', {})
            digg_count = stats.get('digg_count', 0)
            comment_count = stats.get('comment_count', 0)
            share_count = stats.get('share_count', 0)
            collect_count = stats.get('collect_count', 0)

            info_text = (
                f"🎬 标题: {title}\n"
                f"👤 博主: {nickname}\n"
                f"❤️ 点赞: {digg_count} | 💬 评论: {comment_count} | 🔄 分享: {share_count} | ⭐ 收藏: {collect_count}\n"
            )
            # 信息链，专门负责发文字
            info_chain = [Comp.Plain(info_text)]

            # 3. 收集备用下载链接列表
            images = item.get('images')
            download_urls = []
            file_ext = ""
            is_video = False

            max_duration = self.config.get("max_video_duration", 300)

            if images:
                # [图集]
                download_urls = images[0].get('url_list', [])
                file_ext = ".jpg"
                info_chain.append(Comp.Plain("📷 类型: 图集 (已为您提取首图)\n"))
            else:
                # [视频]
                duration_sec = item.get('video', {}).get('duration', 0) / 1000
                video_uri = item['video']['play_addr'].get('uri', '')

                custom_url = f"https://www.douyin.com/aweme/v1/play/?video_id={video_uri}"
                fallback_urls = item['video']['play_addr'].get('url_list', [])
                cover_urls = item['video']['cover'].get('url_list', [])

                if duration_sec <= max_duration:
                    download_urls = [custom_url] + fallback_urls
                    file_ext = ".mp4"
                    is_video = True
                    # info_chain.append(Comp.Plain(f"⏱️ 时长: {duration_sec:.1f}秒"))
                else:
                    download_urls = cover_urls
                    file_ext = ".jpg"
                    warning_text = (
                        f"⏱️ 时长: {duration_sec:.1f}秒\n"
                        f"⚠️ 视频超过设定阈值 ({max_duration}s)，为您发送封面与解析直链：\n"
                        f"🔗 直链: {custom_url}"
                    )
                    info_chain.append(Comp.Plain(warning_text))

            # 4. 执行多策略下载
            file_name = f"{uuid.uuid4().hex}{file_ext}"
            local_file_path = os.path.join(self.plugin_data_path, file_name)

            success = await self._download_media_robust(session, download_urls, local_file_path)
            if not success:
                raise Exception("下载被抖音防火墙拦截 (403/404)，所有备用节点尝试均失败。")

            # 5. 组装独立的媒体链
            media_chain = []
            if is_video:
                media_chain.append(Comp.Video.fromFileSystem(local_file_path))
            else:
                # 即使是图集或封面，也作为单独一条消息发出，保证极佳的排版观感
                media_chain.append(Comp.Image.fromFileSystem(local_file_path))

            # 将两个链分开返回
            return info_chain, media_chain, local_file_path