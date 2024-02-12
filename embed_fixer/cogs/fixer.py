import io
import re
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from ..fixes import FIX_PATTERNS, FIXES
from ..models import GuildSettings
from ..translator import Translator
from ..ui.delete_webhook_msg import DeleteWebhookMsgView
from ..utils import extract_urls

if TYPE_CHECKING:
    from embed_fixer.bot import EmbedFixer


class FixerCog(commands.Cog):
    def __init__(self, bot: "EmbedFixer") -> None:
        self.bot = bot

    async def _find_fixes(
        self,
        message: discord.Message,
        disabled_fixes: list[str],
        extract_media: bool,
    ) -> tuple[bool, list[discord.File], list[str]]:
        fix_found = False
        medias: list[discord.File] = []
        sauces: list[str] = []

        urls = extract_urls(message.content)

        for url in urls:
            for pattern in FIX_PATTERNS.values():
                if re.match(pattern, url) is not None:
                    break
            else:
                continue

            for domain, fix in FIXES.items():
                if domain in disabled_fixes:
                    continue

                if extract_media:
                    fix_found = await self._extract_media(medias, url, domain)
                    if fix_found:
                        message.content = message.content.replace(url, "")
                        sauces.append(url)
                        break

                if domain in url:
                    fix_found = True
                    fixed_url = url.replace(domain, fix)
                    message.content = message.content.replace(url, fixed_url)
                    break

        return fix_found, medias, sauces

    async def _extract_media(self, medias: list[discord.File], url: str, domain: str) -> bool:
        image_urls: list[str] = []
        if domain in url and domain == "pixiv.net":
            image_urls = await self._fetch_pixiv_image_urls(url)
        elif domain in url and domain in {"twitter.com", "x.com"}:
            image_urls = await self._fetch_twitter_media_urls(url)

        fix_found = bool(image_urls)
        medias.extend([await self._download_media(image_url) for image_url in image_urls])

        return fix_found

    async def _send_fixes(
        self, message: discord.Message, medias: list[discord.File], sauces: list[str]
    ) -> None:
        files = [await a.to_file() for a in message.attachments]
        files.extend(medias)

        view = DeleteWebhookMsgView(message.author, message.guild, self.bot.translator)
        view.message = message
        await view.start(sauces=sauces)

        if isinstance(message.channel, discord.TextChannel):
            webhooks = await message.channel.webhooks()
            webhook_name = "Embed Fixer"
            webhook = discord.utils.get(webhooks, name=webhook_name)
            if webhook is None:
                webhook = await message.channel.create_webhook(
                    name=webhook_name, avatar=await self.bot.user.display_avatar.read()
                )

            fixed_message = await webhook.send(
                message.content,
                username=f"{message.author.display_name} (Embed Fixer)",
                avatar_url=message.author.display_avatar.url,
                tts=message.tts,
                files=files,
                view=view,
                wait=True,
            )
        else:
            fixed_message = await message.channel.send(
                message.content, tts=message.tts, files=files, view=view
            )

        if message.reference is not None and isinstance(
            resolved_ref := message.reference.resolved, discord.Message
        ):
            await fixed_message.reply(
                self.bot.translator.get(
                    await Translator.get_guild_lang(message.guild),
                    "replying_to",
                    user=resolved_ref.author.mention,
                    url=resolved_ref.jump_url,
                ),
                mention_author=False,
            )

    async def _fetch_pixiv_image_urls(self, url: str) -> list[str]:
        artwork_id = url.split("/")[-1]
        api_url = f"https://phixiv.net/api/info?id={artwork_id}"
        async with self.bot.session.get(api_url) as response:
            data = await response.json()
            return data["image_proxy_urls"]

    async def _fetch_twitter_media_urls(self, url: str) -> list[str]:
        if "twitter.com" in url:
            api_url = url.replace("twitter.com", "api.fxtwitter.com")
        else:
            api_url = url.replace("x.com", "api.fxtwitter.com")

        async with self.bot.session.get(api_url) as response:
            data = await response.json()
            tweet = data["tweet"]
            medias = tweet.get("media")
            if medias is None:
                return []
            return [media["url"] for media in medias["all"] if media["type"] in {"photo", "video"}]

    async def _download_media(self, url: str) -> discord.File:
        async with self.bot.session.get(url) as response:
            data = await response.read()
            return discord.File(io.BytesIO(data), filename=url.split("/")[-1])

    async def _reply_to_webhook(
        self, message: discord.Message, resolved_ref: discord.Message
    ) -> None:
        guild = message.guild
        if guild is None:
            return

        if not guild.chunked:
            await guild.chunk()

        author = guild.get_member_named(message.author.display_name.removesuffix(" (Embed Fixer)"))
        if author is not None:
            await message.reply(
                self.bot.translator.get(
                    await Translator.get_guild_lang(guild),
                    "replying_to",
                    user=resolved_ref.author.mention,
                    url=resolved_ref.jump_url,
                ),
                mention_author=False,
            )

    @commands.Cog.listener("on_message")
    async def embed_fixer(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        guild_settings = await GuildSettings.get(id=message.guild.id)
        if message.channel.id in guild_settings.disable_fix_channels:
            return

        fix_found, medias, sauces = await self._find_fixes(
            message,
            guild_settings.disabled_fixes,
            message.channel.id in guild_settings.extract_media_channels,
        )

        if fix_found:
            await self._send_fixes(message, medias, sauces)
            await message.delete()
        elif (
            message.reference is not None
            and isinstance(resolved_ref := message.reference.resolved, discord.Message)
            and resolved_ref.webhook_id is not None
            and not message.author.bot
            and message.channel.id not in guild_settings.disable_fix_channels
        ):
            await self._reply_to_webhook(message, resolved_ref)


async def setup(bot: "EmbedFixer") -> None:
    await bot.add_cog(FixerCog(bot))
