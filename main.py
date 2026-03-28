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


@register("douyin_parser", "YourName", "抖音分享链接自动解析插件", "1.1.0")
class DouyinParser(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 修复点 1：使用 os.path.join 替代 / 进行字符串路径拼接
        self.plugin_data_path = os.path.join(get_astrbot_data_path(), "plugin_data", "douyin_parser")
        os.makedirs(self.plugin_data_path, exist_ok=True)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        '''监听所有消息，提取并解析抖音链接'''
        message_str = event.message_str

        # 正则匹配抖音的短链接形式
        urls = re.findall(r'https://v\.douyin\.com/[a-zA-Z0-9]+/?', message_str)
        if not urls:
            return

        for url in urls:
            file_to_delete = None
            try:
                # 解析抖音链接并获取要发送的消息链以及本地文件路径
                chain, file_to_delete = await self.parse_douyin(url)
                if chain:
                    yield event.chain_result(chain)

            except Exception as e:
                logger.error(f"抖音链接解析失败: {e}")
                yield event.plain_result(f"⚠️ 抖音解析出错了: {str(e)}")

            finally:
                # 这一步非常关键：yield 挂起交由框架发送消息，发送完成后代码从这里继续执行，触发清理逻辑
                if file_to_delete and os.path.exists(file_to_delete):
                    try:
                        os.remove(file_to_delete)
                        logger.info(f"已清理临时文件: {file_to_delete}")
                    except Exception as e:
                        logger.error(f"清理临时文件失败: {file_to_delete}, 错误: {e}")

    async def parse_douyin(self, url: str) -> tuple[list, str]:
        '''
        异步获取并解析抖音视频数据，下载媒体到本地。
        返回: (MessageChain列表, 下载到本地的临时文件路径)
        '''
        headers = {
            'user-agent': 'Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36',
            'referer': 'https://www.douyin.com/?is_from_mobile_home=1&recommend=1'
        }

        async with aiohttp.ClientSession() as session:
            # 1. 获取网页 JSON 数据
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise Exception(f"网络请求失败，状态码: {resp.status}")
                res = await resp.text()

            data_match = re.findall(r'window\._ROUTER_DATA\s*=\s*(.*?)</script>', res)
            if not data_match:
                raise Exception("未能从页面提取到有效数据，可能由于风控或链接失效。")

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
            chain = [Comp.Plain(info_text)]

            # 3. 判断媒体类型及逻辑分发
            images = item.get('images')
            download_url = ""
            file_ext = ""
            is_video = False

            # 获取动态配置的阈值，默认300秒
            max_duration = self.config.get("max_video_duration", 300)

            if images:
                # [情况A] 图集
                download_url = images[0]['url_list'][0]
                file_ext = ".jpg"
                chain.append(Comp.Plain("📷 类型: 图集 (已发送首图)\n"))
            else:
                # [情况B] 视频
                duration_sec = item.get('video', {}).get('duration', 0) / 1000
                video_uri = item['video']['play_addr'].get('uri', '')
                video_url = f"https://www.douyin.com/aweme/v1/play/?video_id={video_uri}"
                cover_url = item['video']['cover']['url_list'][0]

                if duration_sec <= max_duration:
                    # 视频不超长，下载视频
                    download_url = video_url
                    file_ext = ".mp4"
                    is_video = True
                    chain.append(Comp.Plain(f"⏱️ 时长: {duration_sec:.1f}秒\n"))
                else:
                    # 视频超长，下载封面
                    download_url = cover_url
                    file_ext = ".jpg"
                    warning_text = (
                        f"⏱️ 时长: {duration_sec:.1f}秒\n"
                        f"⚠️ 视频超过设定阈值 ({max_duration}s)，为您发送封面与解析直链：\n"
                        f"🔗 直链: {video_url}\n"
                    )
                    chain.append(Comp.Plain(warning_text))

            # 4. 下载选定的媒体文件到规范目录
            file_name = f"{uuid.uuid4().hex}{file_ext}"  # 使用 UUID 避免文件名冲突
            # 修复点 2：使用 os.path.join 替代 / 进行路径拼接
            local_file_path = os.path.join(self.plugin_data_path, file_name)

            async with session.get(download_url, headers=headers) as media_resp:
                if media_resp.status == 200:
                    with open(local_file_path, 'wb') as f:
                        f.write(await media_resp.read())
                else:
                    raise Exception(f"媒体文件下载失败，状态码: {media_resp.status}")

            # 5. 组装最终的 MessageChain
            if is_video:
                chain.append(Comp.Video.fromFileSystem(local_file_path))
            else:
                chain.append(Comp.Image.fromFileSystem(local_file_path))

            return chain, local_file_path