#!/usr/bin/env python3
"""Download web novels from a chapter list or a chapter page.

Usage:
    python novel_downloader.py "https://example.com/book/123"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

CONTENT_HINTS = (
    "content",
    "chaptercontent",
    "chapter-content",
    "article",
    "booktxt",
    "readcontent",
    "txt",
    "text",
    "entry-content",
    "post-content",
    "yd_text2",
)

NEXT_HINTS = (
    "下一章",
    "下一页",
    "next chapter",
    "next",
    "后一章",
)

CATALOG_HINTS = (
    "目录",
    "章节目录",
    "全部章节",
    "chapter list",
    "chapters",
)

TITLE_BLACKLIST = (
    "首页",
    "主页",
    "主目录",
    "上一章",
    "下一章",
    "上一页",
    "下一页",
    "加入书签",
    "返回目录",
    "投推荐票",
)

NON_CHAPTER_HINTS = (
    "首页",
    "书首页",
    "书架",
    "书库",
    "分类",
    "排行",
    "排行榜",
    "最新章节",
    "最新更新",
    "完本",
    "全本",
    "专题",
    "推荐",
    "猜你喜欢",
    "相关阅读",
    "上一篇",
    "下一篇",
    "作者",
    "简介",
    "目录",
    "主目录",
    "返回",
    "主页",
    "登录",
    "注册",
    "ntr",
    "乱伦",
    "乡村",
)


@dataclass
class ChapterLink:
    title: str
    url: str


@dataclass
class CrawlNode:
    title: str
    url: str
    kind: str
    children: list[dict] | None = None


class NovelDownloader:
    def __init__(
        self,
        output_dir: Path,
        delay: float = 0.05,
        timeout: int = 20,
        book_workers: int = 2,
        category_workers: int = 1,
    ) -> None:
        self.output_dir = output_dir
        self.delay = delay
        self.timeout = timeout
        self.book_workers = max(1, book_workers)
        self.category_workers = max(1, category_workers)
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def is_alicesw(self, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return "alicesw.com" in hostname

    def is_alicesw_list_page(self, url: str) -> bool:
        return self.is_alicesw(url) and "/lists/" in urlparse(url).path

    def is_alicesw_home_page(self, url: str) -> bool:
        parsed = urlparse(url)
        return self.is_alicesw(url) and parsed.path.strip("/") in ("", "index.html")

    def is_alicesw_novel_page(self, url: str) -> bool:
        return self.is_alicesw(url) and "/novel/" in urlparse(url).path

    def is_alicesw_book_page(self, url: str) -> bool:
        return self.is_alicesw(url) and "/book/" in urlparse(url).path

    def is_xbookcn(self, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname.endswith("xbookcn.net")

    def is_xbookcn_label_page(self, url: str) -> bool:
        return self.is_xbookcn(url) and "/search/label/" in urlparse(url).path

    def is_xbookcn_post_page(self, url: str) -> bool:
        path = urlparse(url).path
        return self.is_xbookcn(url) and re.search(r"/\d{4}/\d{2}/.+\.html$", path) is not None

    def is_manwaka(self, url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname.endswith("manwaka.cc")

    def is_manwaka_category_page(self, url: str) -> bool:
        return self.is_manwaka(url) and urlparse(url).path.startswith("/cate")

    def is_manwaka_comic_page(self, url: str) -> bool:
        path = urlparse(url).path.rstrip("/")
        return self.is_manwaka(url) and re.fullmatch(r"/comic/\d+", path) is not None

    def is_manwaka_chapter_page(self, url: str) -> bool:
        path = urlparse(url).path.rstrip("/")
        return self.is_manwaka(url) and re.fullmatch(r"/comic/\d+/\d+(?:_\d+)?", path) is not None

    def fetch_html(self, url: str) -> str:
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 403:
            body = response.text or ""
            parsed = urlparse(url)
            site = parsed.netloc or "目标网站"
            if "banip403" in body or "/banIp/" in body:
                raise RuntimeError(
                    f"{site} 拒绝了脚本访问（403 banip）。这个站点需要浏览器会话、登录状态或人工访问验证，"
                    "当前下载器不能安全地绕过这类限制。可以换一个公开可访问的链接，或先在浏览器里确认该资源是否无需登录就能打开。"
                )
            raise RuntimeError(
                f"{site} 返回 403，表示没有权限访问或禁止脚本抓取。请确认链接可公开访问，或换一个不需要登录/验证的资源链接。"
            )
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding or "utf-8"
        return response.text

    def fetch_soup(self, url: str) -> BeautifulSoup:
        html = self.fetch_html(url)
        return BeautifulSoup(html, "html.parser")

    def fetch_rendered_alicesw_chapter(self, url: str) -> dict:
        if sync_playwright is None:
            raise RuntimeError("缺少 Playwright 依赖，请重新启动下载器让它自动安装。")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                return self.render_alicesw_chapter_with_page(page, url)
            finally:
                browser.close()

    def render_alicesw_chapter_with_page(self, page, url: str) -> dict:
        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
        page.wait_for_selector(".j_readContent", timeout=self.timeout * 1000)
        page.wait_for_function(
            """
            () => {
              const el = document.querySelector('.j_readContent');
              if (!el) return false;
              const text = (el.innerText || '').trim();
              return text.length > 100 && !text.includes('章节加载中');
            }
            """,
            timeout=self.timeout * 1000,
        )
        title = page.locator(".j_chapterName").first.inner_text().strip()
        content = page.locator(".j_readContent").first.inner_text().strip()
        return {"title": title, "content": self.normalize_content(content)}

    def detect_book_title(self, soup: BeautifulSoup, url: str) -> str:
        if self.is_manwaka(url):
            node = soup.select_one(".comic-title")
            if node:
                value = node.get_text(" ", strip=True)
                if value:
                    return self.clean_book_title(value)

        selectors = [
            "meta[property='og:novel:book_name']",
            "meta[property='og:title']",
            "meta[name='book_name']",
            "h1",
            "title",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if not node:
                continue
            if node.name == "meta":
                value = node.get("content", "").strip()
            else:
                value = node.get_text(" ", strip=True)
            cleaned = self.clean_book_title(value)
            if cleaned:
                return cleaned

        hostname = urlparse(url).hostname or "novel"
        return self.clean_book_title(hostname)

    def get_page_title(self, soup: BeautifulSoup, fallback_url: str) -> str:
        h1 = soup.select_one("h1")
        if h1:
            title = h1.get_text(" ", strip=True)
            if title:
                return title
        title_node = soup.select_one("title")
        if title_node:
            title = title_node.get_text(" ", strip=True)
            if title:
                return title
        return self.detect_book_title(soup, fallback_url)

    def clean_book_title(self, value: str) -> str:
        value = re.split(r"[_|\-]", value)[0].strip()
        value = re.sub(r"\s+", " ", value)
        value = re.sub(r'[\\/:*?"<>|]+', "_", value)
        return value[:80] or "未命名小说"

    def clean_chapter_title(self, value: str, index: int) -> str:
        value = re.sub(r"\s+", " ", value).strip()
        value = re.sub(r'[\\/:*?"<>|]+', "_", value)
        if not value:
            value = f"第{index:04d}章"
        return value[:120]

    def extract_alicesw_category_name(self, soup: BeautifulSoup, url: str) -> str:
        heading = soup.select_one("h1, h2")
        if heading:
            name = heading.get_text(" ", strip=True).replace("小说列表", "").strip()
            if name:
                return self.clean_book_title(name)

        title = self.get_page_title(soup, url)
        title = title.replace("小说列表", "").replace("爱丽丝书屋", "").strip()
        return self.clean_book_title(title or "分类")

    def extract_alicesw_category_links(self, soup: BeautifulSoup, base_url: str) -> list[ChapterLink]:
        links: list[ChapterLink] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            title = anchor.get_text(" ", strip=True)
            if "/lists/" not in href or not href.endswith(".html"):
                continue
            if not title or title in TITLE_BLACKLIST:
                continue
            if re.fullmatch(r"\d+", title):
                continue
            if any(hint in title for hint in ("上一页", "下一页", "尾页", "末页", "排行", "登录", "注册")):
                continue
            absolute_url = urljoin(base_url, href)
            if absolute_url in seen:
                continue
            links.append(ChapterLink(title=title, url=absolute_url))
            seen.add(absolute_url)
        return links

    def extract_alicesw_book_links(self, soup: BeautifulSoup, base_url: str) -> list[ChapterLink]:
        links: list[ChapterLink] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            title = anchor.get_text(" ", strip=True)
            if "/novel/" not in href or not href.endswith(".html"):
                continue
            absolute_url = urljoin(base_url, href)
            if absolute_url in seen:
                continue
            if not title or title in TITLE_BLACKLIST:
                continue
            if title in ("开始阅读", "查看所有章节"):
                continue
            if any(hint in title for hint in ("首页", "排行", "登录", "注册")):
                continue
            links.append(ChapterLink(title=title, url=absolute_url))
            seen.add(absolute_url)
        return links

    def extract_alicesw_pagination_urls(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        pages: list[str] = []
        seen: set[str] = set()
        base_category = self.alicesw_list_category_id(base_url)
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            if "/lists/" not in href:
                continue
            absolute_url = urljoin(base_url, href)
            if base_category and self.alicesw_list_category_id(absolute_url) != base_category:
                continue
            if absolute_url in seen:
                continue
            pages.append(absolute_url)
            seen.add(absolute_url)
        return pages

    def alicesw_list_category_id(self, url: str) -> str | None:
        match = re.search(r"/lists/(\d+)", urlparse(url).path)
        return match.group(1) if match else None

    def collect_alicesw_list_pages(self, url: str, max_pages: int = 100) -> list[str]:
        pending = [url]
        seen: set[str] = set()
        pages: list[str] = []

        while pending and len(pages) < max_pages:
            page_url = pending.pop(0)
            if page_url in seen:
                continue
            seen.add(page_url)
            pages.append(page_url)
            try:
                soup = self.fetch_soup(page_url)
            except Exception:
                continue
            for next_url in self.extract_alicesw_pagination_urls(soup, page_url):
                if next_url not in seen and next_url not in pending:
                    pending.append(next_url)
        return pages

    def extract_xbookcn_title_from_label(self, url: str, soup: BeautifulSoup | None = None) -> str:
        path = urlparse(url).path
        label = path.split("/search/label/", 1)[-1] if "/search/label/" in path else ""
        if label:
            return self.clean_book_title(unquote(label))
        if soup:
            title = self.get_page_title(soup, url)
            title = title.replace("长篇成人情色小说", "").strip(" -:")
            if title:
                return self.clean_book_title(title)
        return self.detect_book_title(soup, url) if soup else "未命名小说"

    def extract_xbookcn_catalog_links_from_soup(self, soup: BeautifulSoup, base_url: str) -> list[ChapterLink]:
        links: list[ChapterLink] = []
        seen: set[str] = set()
        for anchor in soup.select(".blog-posts h3.post-title a[href], .blog-posts .post-title a[href]"):
            href = anchor.get("href", "").strip()
            title = anchor.get_text(" ", strip=True)
            if not href or not title:
                continue
            if title in TITLE_BLACKLIST or any(hint in title for hint in ("主页", "主目录", "上一页", "下一页")):
                continue
            absolute_url = urljoin(base_url, href)
            if not self.is_xbookcn_post_page(absolute_url) or absolute_url in seen:
                continue
            links.append(ChapterLink(title=title, url=absolute_url))
            seen.add(absolute_url)
        return links

    def extract_xbookcn_next_catalog_page(self, soup: BeautifulSoup, base_url: str) -> str | None:
        anchor = soup.select_one("#blog-pager-older-link a[href]")
        if not anchor:
            return None
        href = anchor.get("href", "").strip()
        if not href:
            return None
        next_url = urljoin(base_url, href)
        return next_url if self.is_xbookcn_label_page(next_url) else None

    def collect_xbookcn_catalog_pages(self, url: str, max_pages: int = 100) -> list[str]:
        pages: list[str] = []
        seen: set[str] = set()
        current_url: str | None = url
        while current_url and current_url not in seen and len(pages) < max_pages:
            seen.add(current_url)
            pages.append(current_url)
            try:
                soup = self.fetch_soup(current_url)
            except Exception:
                break
            current_url = self.extract_xbookcn_next_catalog_page(soup, current_url)
        return pages

    def extract_xbookcn_catalog_links(self, url: str, max_pages: int = 100) -> list[ChapterLink]:
        links: list[ChapterLink] = []
        seen: set[str] = set()
        for page_url in self.collect_xbookcn_catalog_pages(url, max_pages=max_pages):
            try:
                soup = self.fetch_soup(page_url)
            except Exception:
                continue
            for chapter in self.extract_xbookcn_catalog_links_from_soup(soup, page_url):
                if chapter.url in seen:
                    continue
                links.append(chapter)
                seen.add(chapter.url)
        return links

    def extract_manwaka_category_name(self, soup: BeautifulSoup, url: str) -> str:
        active = soup.select_one(".ctag a.active, .tag-container a.active")
        if active:
            text = active.get_text(" ", strip=True)
            if text and text != "全部":
                return self.clean_book_title(text)
        path = urlparse(url).path.rstrip("/").split("/")[-1]
        return self.clean_book_title(unquote(path) if path else self.get_page_title(soup, url))

    def extract_manwaka_comic_links(self, soup: BeautifulSoup, base_url: str) -> list[ChapterLink]:
        links: list[ChapterLink] = []
        seen: set[str] = set()
        for anchor in soup.select("#dataList section .item a[href], .books-row section .item a[href], a[href^='/comic/']"):
            href = anchor.get("href", "").strip()
            if not re.fullmatch(r"/?comic/\d+/?", href.strip("/")) and re.fullmatch(r"/comic/\d+", href) is None:
                if re.fullmatch(r"https?://[^/]+/comic/\d+/?", href) is None:
                    continue
            absolute_url = urljoin(base_url, href)
            if not self.is_manwaka_comic_page(absolute_url) or absolute_url in seen:
                continue
            title_node = anchor.select_one(".title")
            title = title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True)
            if not title:
                title = absolute_url.rstrip("/").split("/")[-1]
            links.append(ChapterLink(title=self.clean_book_title(title), url=absolute_url))
            seen.add(absolute_url)
        return links

    def extract_manwaka_chapter_links(self, soup: BeautifulSoup, base_url: str) -> list[ChapterLink]:
        links: list[ChapterLink] = []
        seen: set[str] = set()
        for anchor in soup.select(".chapter-grid a.chapter-item[href], a.chapter-item[href], a[href*='/comic/']"):
            href = anchor.get("href", "").strip()
            absolute_url = urljoin(base_url, href)
            if not self.is_manwaka_chapter_page(absolute_url) or absolute_url in seen:
                continue
            title = anchor.get("data-title", "").strip()
            if not title:
                title_node = anchor.select_one(".chapter-name")
                title = title_node.get_text(" ", strip=True) if title_node else anchor.get_text(" ", strip=True)
            if not title:
                title = f"章节 {len(links) + 1}"
            links.append(ChapterLink(title=title, url=absolute_url))
            seen.add(absolute_url)
        return self._sort_chapters(links)

    def extract_manwaka_ids(self, url: str) -> tuple[str, str | None, int]:
        match = re.search(r"/comic/(\d+)(?:/(\d+)(?:_(\d+))?)?", urlparse(url).path)
        if not match:
            raise RuntimeError("不是可识别的漫画链接。")
        comic_id = match.group(1)
        chapter_id = match.group(2)
        page = int(match.group(3) or 1)
        return comic_id, chapter_id, page

    def extract_manwaka_image_source(self, soup: BeautifulSoup) -> str:
        html = str(soup)
        match = re.search(r'"url"\s*:\s*"(https?://[^"]+)"\s*,\s*"region"', html)
        if match:
            return match.group(1).encode("utf-8").decode("unicode_escape")
        return "https://img.mwzu.cc"

    def fetch_manwaka_images(self, chapter_url: str, page_size: int = 60) -> tuple[list[str], dict]:
        soup = self.fetch_soup(chapter_url)
        _comic_id, chapter_id, first_page = self.extract_manwaka_ids(chapter_url)
        if not chapter_id:
            raise RuntimeError("缺少漫画章节 ID。")
        source = self.extract_manwaka_image_source(soup)
        parsed = urlparse(chapter_url)
        api_base = f"{parsed.scheme}://{parsed.netloc}"
        images: list[str] = []
        pagination: dict = {"current_page": first_page, "total_pages": 1, "total": 0}

        page = first_page
        while True:
            response = self.session.get(
                f"{api_base}/api/comic/image/{chapter_id}",
                params={"page": page, "page_size": page_size, "image_source": source},
                headers={"Referer": chapter_url, "X-Requested-With": "XMLHttpRequest"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("code") != 200:
                raise RuntimeError(data.get("msg") or "漫画图片接口返回失败。")
            payload = data.get("data") or {}
            for item in payload.get("images") or []:
                image_url = item.get("url") if isinstance(item, dict) else None
                if image_url:
                    images.append(image_url)
            pagination = payload.get("pagination") or pagination
            total_pages = int(pagination.get("total_pages") or page)
            if page >= total_pages:
                break
            page += 1
        return images, pagination

    def preview_manwaka_chapter(self, url: str) -> dict:
        soup = self.fetch_soup(url)
        title = self.clean_chapter_title(self.get_page_title(soup, url), 1)
        try:
            images, pagination = self.fetch_manwaka_images(url)
        except Exception as exc:  # noqa: BLE001
            return {"title": title, "url": url, "content": f"漫画图片预览加载失败：{exc}", "media_type": "image", "images": []}
        content = f"漫画章节图片：{len(images)} 张"
        total = pagination.get("total")
        if total:
            content += f"\n接口总数：{total} 张"
        return {"title": title, "url": url, "content": content, "media_type": "image", "images": images[:12]}

    def infer_xbookcn_label_url_from_post(self, soup: BeautifulSoup, base_url: str) -> str | None:
        for anchor in soup.select("#Blog1 a[href], .post-labels a[href], a[rel='tag'][href]"):
            href = anchor.get("href", "").strip()
            text = anchor.get_text(" ", strip=True)
            if "/search/label/" not in href:
                continue
            if text in TITLE_BLACKLIST or text in ("首页", "主页"):
                continue
            return urljoin(base_url, href)
        return None

    def extract_alicesw_catalog_links(self, url: str) -> list[ChapterLink]:
        novel_id_match = re.search(r"/novel/(\d+)\.html", url)
        if not novel_id_match:
            return []
        novel_id = novel_id_match.group(1)
        response = self.session.get(
            "https://www.alicesw.com/home/chapter/lists",
            params={"id": novel_id},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        html_fragment = response.json()
        soup = BeautifulSoup(html_fragment, "html.parser")
        links: list[ChapterLink] = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            title = anchor.get_text(" ", strip=True)
            if not href or "/book/" not in href:
                continue
            links.append(ChapterLink(title=title, url=urljoin(url, href)))
        return self._sort_chapters(self._dedupe_links(links))

    def looks_like_chapter_url(self, href: str) -> bool:
        lowered = href.lower()
        return any(
            token in lowered
            for token in ("/chapter", "/read", ".html", ".htm", "/book/", "/shu/", "/txt/")
        )

    def looks_like_chapter_title(self, title: str) -> bool:
        text = re.sub(r"\s+", " ", title).strip()
        lowered = text.lower()
        if not text or len(text) < 2:
            return False
        if any(hint in lowered for hint in NON_CHAPTER_HINTS):
            return False
        if re.search(r"第\s*[0-9零一二三四五六七八九十百千两]+\s*[章节回卷集部篇]", text):
            return True
        if re.search(r"(楔子|序章|尾声|终章|番外|后记|附录)", text):
            return True
        if re.search(r"^\d+[、.\-_\s]", text):
            return True
        return False

    def _dedupe_links(self, links: list[ChapterLink]) -> list[ChapterLink]:
        unique: list[ChapterLink] = []
        seen_urls: set[str] = set()
        for item in links:
            if item.url in seen_urls:
                continue
            unique.append(item)
            seen_urls.add(item.url)
        return unique

    def extract_catalog_links(self, soup: BeautifulSoup, base_url: str) -> list[ChapterLink]:
        if self.is_xbookcn_label_page(base_url):
            return self.extract_xbookcn_catalog_links(base_url)

        containers: list[Tag] = []
        for selector in (
            "#list",
            ".listmain",
            ".chapterlist",
            ".chapters",
            ".catalog",
            ".booklist",
            "dl",
            "ul",
            "ol",
            "div",
        ):
            containers.extend(soup.select(selector))

        best_links: list[ChapterLink] = []
        best_score = -1

        for container in containers:
            anchors = container.select("a[href]")
            if len(anchors) < 3:
                continue
            local_links: list[ChapterLink] = []
            chapter_like_count = 0
            sequence_like_count = 0

            for anchor in anchors:
                title = anchor.get_text(" ", strip=True)
                href = anchor.get("href", "").strip()
                if not href or href.startswith("javascript:"):
                    continue
                absolute_url = urljoin(base_url, href)
                if len(title) < 2 or title in TITLE_BLACKLIST:
                    continue
                if any(hint in title.lower() for hint in ("login", "register")):
                    continue
                if any(hint in title.lower() for hint in NON_CHAPTER_HINTS):
                    continue

                is_chapter_title = self.looks_like_chapter_title(title)
                has_chapter_url = self.looks_like_chapter_url(href)
                if not is_chapter_title and not has_chapter_url:
                    continue

                local_links.append(ChapterLink(title=title, url=absolute_url))
                if is_chapter_title:
                    chapter_like_count += 1
                if re.search(r"\d", title):
                    sequence_like_count += 1

            local_links = self._dedupe_links(local_links)
            if len(local_links) < 3:
                continue

            attrs = " ".join(
                filter(
                    None,
                    [container.get("id", ""), " ".join(container.get("class", []))],
                )
            ).lower()
            score = len(local_links) * 10 + chapter_like_count * 20 + sequence_like_count * 5
            if any(hint in attrs for hint in ("list", "chapter", "catalog", "read")):
                score += 40

            if chapter_like_count == 0 and len(local_links) < 10:
                continue

            if score > best_score:
                best_score = score
                best_links = local_links

        sorted_links = self._sort_chapters(best_links)

        # If the result still does not look like a real chapter list, treat it as no catalog.
        chapter_title_hits = sum(1 for item in sorted_links if self.looks_like_chapter_title(item.title))
        if sorted_links and chapter_title_hits < max(3, len(sorted_links) // 3):
            return []

        return sorted_links

    def _sort_chapters(self, links: list[ChapterLink]) -> list[ChapterLink]:
        def chapter_key(item: ChapterLink) -> tuple[int, str]:
            text = item.title
            match = re.search(r"第\s*([0-9零一二三四五六七八九十百千两]+)", text)
            if match:
                raw = match.group(1)
                value = self._cn_number_to_int(raw)
                return (value, text)
            numeric = re.findall(r"\d+", text)
            if numeric:
                return (int(numeric[0]), text)
            return (10**9, text)

        return sorted(links, key=chapter_key)

    def _cn_number_to_int(self, text: str) -> int:
        if text.isdigit():
            return int(text)

        digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        units = {"十": 10, "百": 100, "千": 1000}
        total = 0
        current = 0
        for char in text:
            if char in digits:
                current = digits[char]
            elif char in units:
                if current == 0:
                    current = 1
                total += current * units[char]
                current = 0
        return total + current or 10**9

    def extract_content(self, soup: BeautifulSoup) -> str:
        candidates: list[tuple[int, str]] = []

        for node in soup.find_all(["article", "div", "section", "td"]):
            if not isinstance(node, Tag):
                continue
            attrs = " ".join(
                filter(
                    None,
                    [
                        node.get("id", ""),
                        " ".join(node.get("class", [])),
                    ],
                )
            ).lower()
            text = node.get_text("\n", strip=True)
            if len(text) < 200:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            long_lines = sum(1 for line in lines if len(line) >= 18)
            chapter_markers = sum(
                1 for line in lines if re.search(r"第\s*[0-9零一二三四五六七八九十百千两]+\s*[章节回卷集部篇]", line)
            )
            prose_markers = text.count("。") + text.count("，") + text.count("！") + text.count("？")
            short_noise = sum(1 for line in lines if len(line) <= 8)

            score = len(text)
            if any(hint in attrs for hint in CONTENT_HINTS):
                score += 10000
            if long_lines >= 8:
                score += 3000
            if prose_markers >= 20:
                score += 3000
            if chapter_markers:
                score += 1500
            score -= short_noise * 40
            candidates.append((score, text))

        if candidates:
            best_text = max(candidates, key=lambda item: item[0])[1]
            return self.normalize_content(best_text)

        body = soup.get_text("\n", strip=True)
        return self.normalize_content(body)

    def normalize_content(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines()]
        filtered = [
            line
            for line in lines
            if line
            and not any(bad in line for bad in TITLE_BLACKLIST)
            and "最新网址" not in line
            and "手机阅读" not in line
            and "上一章" != line
            and "下一章" != line
        ]
        compact = "\n".join(filtered)
        compact = re.sub(r"\n{3,}", "\n\n", compact)
        return compact.strip()

    def detect_page_type(self, soup: BeautifulSoup, base_url: str) -> str:
        if self.is_manwaka_category_page(base_url):
            return "comic_list"
        if self.is_manwaka_comic_page(base_url):
            return "comic"
        if self.is_manwaka_chapter_page(base_url):
            return "comic_chapter"
        if self.is_alicesw_home_page(base_url):
            return "site"
        if self.is_xbookcn_label_page(base_url):
            return "catalog"
        if self.is_alicesw_list_page(base_url):
            return "book_list"
        if self.is_alicesw_novel_page(base_url):
            return "catalog"
        if self.is_alicesw_book_page(base_url):
            return "chapter"
        if self.is_xbookcn_post_page(base_url):
            return "chapter"
        if self.is_alicesw(base_url) and len(self.extract_alicesw_category_links(soup, base_url)) >= 2:
            return "site"
        catalog_links = self.extract_catalog_links(soup, base_url)
        content = self.extract_content(soup)
        if len(catalog_links) >= 5:
            return "catalog"
        if len(content) >= 500:
            return "chapter"
        return "unknown"

    def build_tree(self, url: str, preview_limit: int = 5000, max_pages: int = 100) -> dict:
        soup = self.fetch_soup(url)
        page_type = self.detect_page_type(soup, url)
        title = self.detect_book_title(soup, url)

        if page_type == "site":
            categories = self.extract_alicesw_category_links(soup, url)
            category_nodes = []
            total_books = 0
            for category in categories[:preview_limit]:
                category_soup = self.fetch_soup(category.url)
                page_urls = self.collect_alicesw_list_pages(category.url, max_pages=max_pages)
                books = self.collect_books_from_list_pages(page_urls)
                total_books += len(books)
                category_nodes.append(
                    {
                        "kind": "category",
                        "title": category.title,
                        "url": category.url,
                        "page_count": len(page_urls),
                        "book_count": len(books),
                        "children": [
                            {
                                "kind": "book",
                                "title": book.title,
                                "url": book.url,
                                "children": [],
                            }
                            for book in books[:preview_limit]
                        ],
                    }
                )
                if not category_nodes[-1]["title"]:
                    category_nodes[-1]["title"] = self.extract_alicesw_category_name(category_soup, category.url)
            return {
                "page_type": "site",
                "book_title": title,
                "catalog_url": url,
                "tree": {
                    "kind": "site",
                    "title": title,
                    "url": url,
                    "children": category_nodes,
                },
                "category_count": len(categories),
                "book_count": total_books,
                "chapter_count": 0,
                "first_preview": {
                    "title": title,
                    "url": url,
                    "content": "这是总入口。左侧按 一级分类 → 二级书名 → 三级目录 → 四级正文 展示；点击书名会加载目录，点击章节会显示真正正文。",
                },
            }

        if page_type == "book_list":
            page_urls = self.collect_alicesw_list_pages(url, max_pages=max_pages)
            books = self.collect_books_from_list_pages(page_urls)
            category_name = self.extract_alicesw_category_name(soup, url)
            return {
                "page_type": "book_list",
                "book_title": category_name,
                "catalog_url": url,
                "tree": {
                    "kind": "category",
                    "title": category_name,
                    "url": url,
                    "page_count": len(page_urls),
                    "book_count": len(books),
                    "children": [
                        {"kind": "book", "title": book.title, "url": book.url, "children": []}
                        for book in books[:preview_limit]
                    ],
                },
                "category_count": 1,
                "book_count": len(books),
                "chapter_count": 0,
                "first_preview": {
                    "title": category_name,
                    "url": url,
                    "content": f"这是分类页，已识别 {len(page_urls)} 个分页、{len(books)} 本小说。点击书名可加载目录，开始下载会下载该分类下所有分页中的全部小说。",
                },
            }

        if page_type == "comic_list":
            comics = self.extract_manwaka_comic_links(soup, url)
            category_name = self.extract_manwaka_category_name(soup, url)
            return {
                "page_type": "comic_list",
                "book_title": category_name,
                "catalog_url": url,
                "tree": {
                    "kind": "category",
                    "title": category_name,
                    "url": url,
                    "page_count": 1,
                    "book_count": len(comics),
                    "children": [
                        {"kind": "comic", "title": comic.title, "url": comic.url, "children": []}
                        for comic in comics[:preview_limit]
                    ],
                },
                "category_count": 1,
                "book_count": len(comics),
                "chapter_count": 0,
                "first_preview": {
                    "title": category_name,
                    "url": url,
                    "content": f"这是漫画分类页，已识别当前页 {len(comics)} 部漫画。点击漫画名会加载章节，下载时会按 漫画/章节/图片 分开保存，并跳过已经下载过的图片。",
                },
            }

        if page_type == "comic":
            chapters = self.extract_manwaka_chapter_links(soup, url)
            title = self.detect_book_title(soup, url)
            first_preview = self.preview_chapter(chapters[0].url) if chapters else None
            return {
                "page_type": "comic",
                "book_title": title,
                "catalog_url": url,
                "tree": {
                    "kind": "comic",
                    "title": title,
                    "url": url,
                    "children": [
                        {"kind": "catalog", "title": "章节", "url": url, "children": [
                            {"kind": "comic_chapter", "title": chapter.title, "url": chapter.url}
                            for chapter in chapters[:preview_limit]
                        ]}
                    ],
                },
                "category_count": 0,
                "book_count": 1,
                "chapter_count": len(chapters),
                "first_preview": first_preview,
            }

        if page_type == "comic_chapter":
            first_preview = self.preview_chapter(url)
            return {
                "page_type": "comic_chapter",
                "book_title": title,
                "catalog_url": url,
                "tree": {
                    "kind": "comic",
                    "title": title,
                    "url": url,
                    "children": [
                        {"kind": "catalog", "title": "当前章节", "url": url, "children": [
                            {"kind": "comic_chapter", "title": first_preview["title"], "url": url}
                        ]}
                    ],
                },
                "category_count": 0,
                "book_count": 1,
                "chapter_count": 1,
                "first_preview": first_preview,
            }

        if page_type == "catalog":
            if self.is_alicesw_novel_page(url):
                chapters = self.extract_alicesw_catalog_links(url)
                title = self.detect_book_title(soup, url)
            elif self.is_xbookcn_label_page(url):
                chapters = self.extract_xbookcn_catalog_links(url)
                title = self.extract_xbookcn_title_from_label(url, soup)
            else:
                chapters = self.extract_catalog_links(soup, url)
            first_preview = self.preview_chapter(chapters[0].url) if chapters else None
            return {
                "page_type": "catalog",
                "book_title": title,
                "catalog_url": url,
                "tree": {
                    "kind": "book",
                    "title": title,
                    "url": url,
                    "children": [
                        {"kind": "catalog", "title": "目录", "url": url, "children": [
                            {"kind": "chapter", "title": chapter.title, "url": chapter.url}
                            for chapter in chapters[:preview_limit]
                        ]}
                    ],
                },
                "category_count": 0,
                "book_count": 1,
                "chapter_count": len(chapters),
                "first_preview": first_preview,
            }

        first_preview = self.preview_chapter(url)
        return {
            "page_type": "chapter",
            "book_title": title,
            "catalog_url": self.infer_catalog_url(soup, url),
            "tree": {
                "kind": "book",
                "title": title,
                "url": url,
                "children": [
                    {"kind": "catalog", "title": "当前正文", "url": url, "children": [
                        {"kind": "chapter", "title": first_preview["title"], "url": url}
                    ]}
                ],
            },
            "category_count": 0,
            "book_count": 1,
            "chapter_count": 1,
            "first_preview": first_preview,
        }

    def collect_books_from_list_pages(self, page_urls: list[str]) -> list[ChapterLink]:
        books: list[ChapterLink] = []
        seen: set[str] = set()
        for page_url in page_urls:
            try:
                page_soup = self.fetch_soup(page_url)
            except Exception:
                continue
            for book in self.extract_alicesw_book_links(page_soup, page_url):
                if book.url in seen:
                    continue
                books.append(book)
                seen.add(book.url)
        return books

    def infer_catalog_url(self, soup: BeautifulSoup, base_url: str) -> str | None:
        for anchor in soup.select("a[href]"):
            text = anchor.get_text(" ", strip=True).lower()
            href = anchor.get("href", "").strip()
            if not href:
                continue
            if any(hint in text for hint in CATALOG_HINTS):
                return urljoin(base_url, href)
        return None

    def infer_alicesw_novel_url_from_chapter(self, soup: BeautifulSoup, base_url: str) -> str | None:
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            text = anchor.get_text(" ", strip=True)
            if "/novel/" in href and href.endswith(".html"):
                return urljoin(base_url, href)
            if any(hint in text for hint in CATALOG_HINTS) and href:
                candidate = urljoin(base_url, href)
                if self.is_alicesw_novel_page(candidate):
                    return candidate

        match = re.search(r"/book/(\d+)", urlparse(base_url).path)
        if match:
            return urljoin(base_url, f"/novel/{match.group(1)}.html")
        return None

    def find_next_page(self, soup: BeautifulSoup, base_url: str, current_url: str) -> str | None:
        for anchor in soup.select("a[href]"):
            text = anchor.get_text(" ", strip=True).lower()
            href = anchor.get("href", "").strip()
            if not href:
                continue
            next_url = urljoin(base_url, href)
            if next_url == current_url:
                continue
            if any(hint in text for hint in NEXT_HINTS):
                return next_url
        return None

    def inspect(self, url: str, preview_limit: int = 5000) -> dict:
        result = self.build_tree(url, preview_limit=preview_limit)
        tree = result.get("tree") or {}
        if "chapters" not in result:
            result["chapters"] = self.flatten_preview_nodes(tree)
        return result

    def flatten_preview_nodes(self, node: dict) -> list[dict]:
        items: list[dict] = []
        for child in node.get("children") or []:
            items.append(
                {
                    "kind": child.get("kind"),
                    "title": child.get("title"),
                    "url": child.get("url"),
                }
            )
            items.extend(self.flatten_preview_nodes(child))
        return items

    def inspect_legacy(self, url: str, preview_limit: int = 20) -> dict:
        soup = self.fetch_soup(url)
        page_type = self.detect_page_type(soup, url)
        book_title = self.detect_book_title(soup, url)

        if self.is_alicesw_list_page(url):
            books = self.extract_alicesw_book_links(soup, url)
            category_name = self.extract_alicesw_category_name(soup, url)
            return {
                "page_type": "book_list",
                "book_title": category_name,
                "catalog_url": url,
                "chapters": [{"title": item.title, "url": item.url} for item in books[:preview_limit]],
                "chapter_count": len(books),
                "first_preview": {
                    "title": category_name,
                    "url": url,
                    "content": "这是分类书库页，左侧显示的是本页小说列表。点击开始下载后，会继续进入每本书的目录页与正文页。",
                },
            }

        if self.is_alicesw_novel_page(url):
            chapters = self.extract_alicesw_catalog_links(url)
            first_preview = None
            if chapters:
                first_preview = self.preview_chapter(chapters[0].url)
            return {
                "page_type": "catalog",
                "book_title": book_title,
                "catalog_url": url,
                "chapters": [{"title": chapter.title, "url": chapter.url} for chapter in chapters[:preview_limit]],
                "chapter_count": len(chapters),
                "first_preview": first_preview,
            }

        if self.is_alicesw_book_page(url):
            first_preview = self.preview_chapter(url)
            return {
                "page_type": "chapter",
                "book_title": book_title,
                "catalog_url": None,
                "chapters": [{"title": first_preview["title"], "url": url}],
                "chapter_count": 1,
                "first_preview": first_preview,
            }

        if page_type == "catalog":
            chapters = self.extract_catalog_links(soup, url)
            preview_chapters = chapters[:preview_limit]
            first_preview = None
            if preview_chapters:
                first_preview = self.preview_chapter(preview_chapters[0].url)
            return {
                "page_type": page_type,
                "book_title": book_title,
                "catalog_url": url,
                "chapters": [{"title": chapter.title, "url": chapter.url} for chapter in preview_chapters],
                "chapter_count": len(chapters),
                "first_preview": first_preview,
            }

        catalog_url = self.infer_catalog_url(soup, url)
        chapter_title = self.clean_chapter_title(self.get_page_title(soup, url), 1)
        chapter_content = self.extract_content(soup)
        return {
            "page_type": "chapter",
            "book_title": book_title,
            "catalog_url": catalog_url,
            "chapters": [{"title": chapter_title, "url": url}],
            "chapter_count": 1,
            "first_preview": {
                "title": chapter_title,
                "url": url,
                "content": chapter_content,
            },
        }

    def preview_chapter(self, url: str) -> dict:
        if self.is_manwaka_chapter_page(url):
            return self.preview_manwaka_chapter(url)
        if self.is_alicesw_book_page(url):
            rendered = self.fetch_rendered_alicesw_chapter(url)
            return {
                "title": rendered["title"],
                "url": url,
                "content": rendered["content"],
            }
        soup = self.fetch_soup(url)
        title = self.clean_chapter_title(self.get_page_title(soup, url), 1)
        content = self.extract_content(soup)
        return {
            "title": title,
            "url": url,
            "content": content,
        }

    def write_image_file(self, path: Path, url: str, referer: str) -> bool:
        if path.exists():
            try:
                if path.stat().st_size > 1024:
                    return False
            except OSError:
                pass
        response = self.session.get(url, headers={"Referer": referer}, timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "")
        if "image" not in content_type.lower() and len(response.content) < 1024:
            raise RuntimeError("返回内容不像图片。")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.content)
        return True

    def image_extension_from_url(self, url: str) -> str:
        ext = Path(urlparse(url).path).suffix.lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            return ext
        return ".jpg"

    def download_manwaka_chapter(self, comic_dir: Path, index: int, chapter: ChapterLink) -> int:
        chapter_title = self.clean_chapter_title(chapter.title, index)
        chapter_dir = comic_dir / "chapters" / f"{index:04d}_{chapter_title}"
        images, pagination = self.fetch_manwaka_images(chapter.url)
        expected = len(images)
        if expected == 0:
            raise RuntimeError("没有拿到章节图片。")

        existing = [
            path for path in chapter_dir.glob("*.*")
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") and path.stat().st_size > 1024
        ] if chapter_dir.exists() else []
        if len(existing) >= expected:
            print(f"[{index}] 漫画章节已存在，跳过: {chapter_title}")
            return len(existing)

        def image_worker(item: tuple[int, str]) -> bool:
            image_index, image_url = item
            ext = self.image_extension_from_url(image_url)
            image_path = chapter_dir / f"{image_index:04d}{ext}"
            return self.write_image_file(image_path, image_url, chapter.url)

        saved = 0
        workers = min(4, max(1, len(images)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(image_worker, (image_index, image_url)) for image_index, image_url in enumerate(images, start=1)]
            for future in as_completed(futures):
                try:
                    if future.result():
                        saved += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"图片下载失败: {exc}")

        manifest = {
            "chapter_title": chapter_title,
            "image_count": expected,
            "api_total": pagination.get("total"),
            "source_url": chapter.url,
            "generated_at": int(time.time()),
        }
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{index}] 漫画章节已保存: {chapter_title}，新增 {saved} 张")
        return expected

    def download_from_comic(self, url: str) -> Path:
        soup = self.fetch_soup(url)
        comic_title = self.detect_book_title(soup, url)
        chapters = self.extract_manwaka_chapter_links(soup, url)
        if not chapters:
            raise RuntimeError("未从漫画目录提取到章节链接。")
        comic_parent = self.output_dir if self.output_dir.parent.name == "漫画" else self.output_dir / "漫画"
        comic_dir = comic_parent / comic_title
        comic_dir.mkdir(parents=True, exist_ok=True)
        total_images = 0
        for index, chapter in enumerate(chapters, start=1):
            try:
                total_images += self.download_manwaka_chapter(comic_dir, index, chapter)
                if self.delay:
                    time.sleep(self.delay)
            except Exception as exc:  # noqa: BLE001
                print(f"[{index}/{len(chapters)}] 跳过失败漫画章节: {chapter.title} -> {exc}")
        manifest = {
            "comic_title": comic_title,
            "chapter_count": len(chapters),
            "image_count": total_images,
            "source_url": url,
            "generated_at": int(time.time()),
        }
        (comic_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return comic_dir

    def download_from_comic_chapter(self, url: str) -> Path:
        soup = self.fetch_soup(url)
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            candidate = urljoin(url, href)
            if self.is_manwaka_comic_page(candidate):
                print(f"检测到漫画目录，改为下载整部漫画: {candidate}")
                return self.download_from_comic(candidate)
        comic_id, _chapter_id, _page = self.extract_manwaka_ids(url)
        comic_dir = self.output_dir / "漫画" / f"comic_{comic_id}"
        self.download_manwaka_chapter(comic_dir, 1, ChapterLink(title=self.get_page_title(soup, url), url=url))
        return comic_dir

    def download_from_comic_list(self, url: str) -> Path:
        soup = self.fetch_soup(url)
        category_name = self.extract_manwaka_category_name(soup, url)
        category_dir = self.output_dir / "漫画" / category_name
        category_dir.mkdir(parents=True, exist_ok=True)
        comics = self.extract_manwaka_comic_links(soup, url)
        if not comics:
            raise RuntimeError("未从漫画分类页识别到漫画列表。")
        downloaded = self.download_books_parallel(category_dir, comics)
        manifest = {
            "category_name": category_name,
            "comic_count": downloaded,
            "source_url": url,
            "generated_at": int(time.time()),
        }
        (category_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return category_dir

    def write_chapter(self, book_dir: Path, index: int, title: str, content: str) -> Path:
        chapter_name = f"{index:04d}_{self.clean_chapter_title(title, index)}.txt"
        chapter_path = book_dir / "chapters" / chapter_name
        chapter_path.parent.mkdir(parents=True, exist_ok=True)
        chapter_path.write_text(f"{title}\n\n{content}\n", encoding="utf-8")
        return chapter_path

    def find_existing_chapter(self, book_dir: Path, index: int) -> Path | None:
        chapter_dir = book_dir / "chapters"
        if not chapter_dir.exists():
            return None
        for path in chapter_dir.glob(f"{index:04d}_*.txt"):
            try:
                if path.stat().st_size > 80:
                    return path
            except OSError:
                continue
        return None

    def is_book_complete_by_title(self, parent_dir: Path, title: str) -> bool:
        book_dir = parent_dir / self.clean_book_title(title)
        if not book_dir.exists():
            return False
        manifest_path = book_dir / "manifest.json"
        merged_candidates = list(book_dir.glob("*.txt"))
        chapter_files = list((book_dir / "chapters").glob("*.txt")) if (book_dir / "chapters").exists() else []
        if not manifest_path.exists() or not merged_candidates or not chapter_files:
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = int(manifest.get("chapter_count") or 0)
        except Exception:
            return False
        return expected > 0 and len(chapter_files) >= expected

    def is_comic_complete_by_title(self, parent_dir: Path, title: str) -> bool:
        comic_dir = parent_dir / self.clean_book_title(title)
        manifest_path = comic_dir / "manifest.json"
        chapter_dir = comic_dir / "chapters"
        if not manifest_path.exists() or not chapter_dir.exists():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected = int(manifest.get("chapter_count") or 0)
        except Exception:
            return False
        chapter_manifests = list(chapter_dir.glob("*/manifest.json"))
        return expected > 0 and len(chapter_manifests) >= expected

    def write_manifest(self, book_dir: Path, book_title: str, chapters: Iterable[ChapterLink]) -> None:
        manifest = {
            "book_title": book_title,
            "chapter_count": len(list(chapters)),
            "generated_at": int(time.time()),
        }
        (book_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def merge_book(self, book_dir: Path, book_title: str) -> Path:
        chapter_files = sorted((book_dir / "chapters").glob("*.txt"))
        merged_path = book_dir / f"{book_title}.txt"
        merged_content = []
        for chapter_file in chapter_files:
            merged_content.append(chapter_file.read_text(encoding="utf-8").strip())
        merged_path.write_text("\n\n".join(merged_content) + "\n", encoding="utf-8")
        return merged_path

    def download_from_catalog(self, url: str) -> Path:
        soup = self.fetch_soup(url)
        if self.is_alicesw_novel_page(url):
            book_title = self.detect_book_title(soup, url)
            links = self.extract_alicesw_catalog_links(url)
        elif self.is_xbookcn_label_page(url):
            book_title = self.extract_xbookcn_title_from_label(url, soup)
            links = self.extract_xbookcn_catalog_links(url)
        else:
            book_title = self.detect_book_title(soup, url)
            links = self.extract_catalog_links(soup, url)
        if not links:
            raise RuntimeError("未从目录页提取到章节链接，请换一个更明确的目录页链接再试。")

        book_dir = self.output_dir / book_title
        book_dir.mkdir(parents=True, exist_ok=True)

        print(f"书名: {book_title}")
        print(f"章节数: {len(links)}")
        print(f"保存目录: {book_dir}")

        if links and all(self.is_alicesw_book_page(chapter.url) for chapter in links):
            self.download_alicesw_catalog_chapters(book_dir, links)
            self.write_manifest(book_dir, book_title, links)
            return self.merge_book(book_dir, book_title)

        for index, chapter in enumerate(links, start=1):
            try:
                existing = self.find_existing_chapter(book_dir, index)
                if existing:
                    print(f"[{index}/{len(links)}] 已存在，跳过: {existing.name}")
                    continue
                if self.is_alicesw_book_page(chapter.url):
                    preview = self.preview_chapter(chapter.url)
                    content = preview["content"]
                    title = self.clean_chapter_title(preview["title"] or chapter.title, index)
                else:
                    chapter_soup = self.fetch_soup(chapter.url)
                    content = self.extract_content(chapter_soup)
                    title = self.clean_chapter_title(chapter.title, index)
                self.write_chapter(book_dir, index, title, content)
                print(f"[{index}/{len(links)}] 已保存: {title}")
                time.sleep(self.delay)
            except Exception as exc:  # noqa: BLE001
                print(f"[{index}/{len(links)}] 跳过失败章节: {chapter.title} -> {exc}")

        self.write_manifest(book_dir, book_title, links)
        merged_path = self.merge_book(book_dir, book_title)
        return merged_path

    def download_alicesw_catalog_chapters(self, book_dir: Path, links: list[ChapterLink]) -> None:
        if sync_playwright is None:
            raise RuntimeError("缺少 Playwright 依赖，请重新启动下载器让它自动安装。")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                for index, chapter in enumerate(links, start=1):
                    try:
                        existing = self.find_existing_chapter(book_dir, index)
                        if existing:
                            print(f"[{index}/{len(links)}] 已存在，跳过: {existing.name}")
                            continue
                        preview = self.render_alicesw_chapter_with_page(page, chapter.url)
                        title = self.clean_chapter_title(preview["title"] or chapter.title, index)
                        self.write_chapter(book_dir, index, title, preview["content"])
                        print(f"[{index}/{len(links)}] 已保存: {title}")
                        if self.delay:
                            time.sleep(self.delay)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[{index}/{len(links)}] 跳过失败章节: {chapter.title} -> {exc}")
            finally:
                browser.close()

    def download_from_chapter(self, url: str, max_chapters: int = 500) -> Path:
        if self.is_alicesw_book_page(url):
            soup = self.fetch_soup(url)
            catalog_url = self.infer_alicesw_novel_url_from_chapter(soup, url)
            if catalog_url:
                print(f"检测到书籍目录，改为下载整本书: {catalog_url}")
                return self.download_from_catalog(catalog_url)
            preview = self.preview_chapter(url)
            book_title = self.detect_book_title(soup, url)
            book_dir = self.output_dir / book_title
            book_dir.mkdir(parents=True, exist_ok=True)
            self.write_chapter(book_dir, 1, preview["title"], preview["content"])
            links = [ChapterLink(title=preview["title"], url=url)]
            self.write_manifest(book_dir, book_title, links)
            return self.merge_book(book_dir, book_title)

        soup = self.fetch_soup(url)
        if self.is_xbookcn_post_page(url):
            catalog_url = self.infer_xbookcn_label_url_from_post(soup, url)
            if catalog_url:
                print(f"检测到书籍目录，改为下载整本书: {catalog_url}")
                return self.download_from_catalog(catalog_url)

        catalog_url = self.infer_catalog_url(soup, url)
        if catalog_url:
            print(f"检测到目录入口，改为从目录下载: {catalog_url}")
            return self.download_from_catalog(catalog_url)

        book_title = self.detect_book_title(soup, url)
        book_dir = self.output_dir / book_title
        book_dir.mkdir(parents=True, exist_ok=True)

        current_url = url
        visited: set[str] = set()
        index = 1

        while current_url and current_url not in visited and index <= max_chapters:
            visited.add(current_url)
            current_soup = self.fetch_soup(current_url)

            title = self.clean_chapter_title(self.get_page_title(current_soup, current_url), index)

            existing = self.find_existing_chapter(book_dir, index)
            if existing:
                print(f"[{index}] 已存在，跳过: {existing.name}")
            else:
                content = self.extract_content(current_soup)
                self.write_chapter(book_dir, index, title, content)
                print(f"[{index}] 已保存: {title}")

            next_url = self.find_next_page(current_soup, current_url, current_url)
            if not next_url:
                break

            current_url = next_url
            index += 1
            time.sleep(self.delay)

        links = [ChapterLink(title=path.stem, url="") for path in sorted((book_dir / "chapters").glob("*.txt"))]
        self.write_manifest(book_dir, book_title, links)
        merged_path = self.merge_book(book_dir, book_title)
        return merged_path

    def download_from_book_list(self, url: str, max_pages: int = 100) -> Path:
        first_soup = self.fetch_soup(url)
        category_name = self.extract_alicesw_category_name(first_soup, url)
        category_dir = self.output_dir / category_name
        category_dir.mkdir(parents=True, exist_ok=True)

        page_urls = self.collect_alicesw_list_pages(url, max_pages=max_pages)
        if not page_urls:
            page_urls = [url]

        all_books: list[ChapterLink] = []
        seen_books: set[str] = set()
        for page_index, page_url in enumerate(page_urls, start=1):
            page_soup = self.fetch_soup(page_url)
            book_links = self.extract_alicesw_book_links(page_soup, page_url)
            print(f"[分类第{page_index}页] 识别到 {len(book_links)} 本小说")
            for book in book_links:
                if book.url in seen_books:
                    continue
                all_books.append(book)
                seen_books.add(book.url)

        downloaded_books = self.download_books_parallel(category_dir, all_books)
        manifest = {
            "category_name": category_name,
            "book_count": downloaded_books,
            "generated_at": int(time.time()),
        }
        (category_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return category_dir

    def download_books_parallel(self, output_dir: Path, books: list[ChapterLink]) -> int:
        if not books:
            return 0

        pending: list[ChapterLink] = []
        completed = 0
        for book in books:
            is_complete = (
                self.is_comic_complete_by_title(output_dir, book.title)
                if self.is_manwaka_comic_page(book.url)
                else self.is_book_complete_by_title(output_dir, book.title)
            )
            if is_complete:
                print(f"已完成，跳过小说: {book.title}")
                completed += 1
            else:
                pending.append(book)

        if not pending:
            return completed

        def worker(book: ChapterLink) -> bool:
            try:
                local_downloader = NovelDownloader(
                    output_dir=output_dir,
                    delay=self.delay,
                    timeout=self.timeout,
                    book_workers=1,
                    category_workers=1,
                )
                if self.is_manwaka_comic_page(book.url):
                    local_downloader.download_from_comic(book.url)
                else:
                    local_downloader.download_from_catalog(book.url)
                return True
            except Exception as exc:  # noqa: BLE001
                print(f"跳过失败小说: {book.title} -> {exc}")
                return False

        workers = min(self.book_workers, len(pending))
        print(f"安全并行下载小说数: {workers}")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker, book) for book in pending]
            for future in as_completed(futures):
                if future.result():
                    completed += 1
        return completed

    def download_from_site(self, url: str, max_pages_per_category: int = 100) -> Path:
        soup = self.fetch_soup(url)
        site_title = self.detect_book_title(soup, url)
        site_dir = self.output_dir / site_title
        site_dir.mkdir(parents=True, exist_ok=True)

        categories = self.extract_alicesw_category_links(soup, url)
        if not categories:
            raise RuntimeError("未从总入口识别到分类链接，请确认输入的是小说站首页或分类导航页。")

        downloaded_categories = 0

        def category_worker(category: ChapterLink) -> bool:
            try:
                local_downloader = NovelDownloader(
                    output_dir=site_dir,
                    delay=self.delay,
                    timeout=self.timeout,
                    book_workers=self.book_workers,
                    category_workers=1,
                )
                local_downloader.download_from_book_list(category.url, max_pages=max_pages_per_category)
                return True
            except Exception as exc:  # noqa: BLE001
                print(f"跳过失败分类: {category.title} -> {exc}")
                return False

        workers = min(self.category_workers, len(categories))
        print(f"安全并行下载分类数: {workers}")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(category_worker, category) for category in categories]
            for future in as_completed(futures):
                if future.result():
                    downloaded_categories += 1

        manifest = {
            "site_title": site_title,
            "category_count": downloaded_categories,
            "generated_at": int(time.time()),
        }
        (site_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return site_dir

    def download(self, url: str) -> Path:
        soup = self.fetch_soup(url)
        page_type = self.detect_page_type(soup, url)
        print(f"页面类型判断: {page_type}")

        if page_type == "site":
            return self.download_from_site(url)
        if page_type == "book_list":
            return self.download_from_book_list(url)
        if page_type == "comic_list":
            return self.download_from_comic_list(url)
        if page_type == "comic":
            return self.download_from_comic(url)
        if page_type == "comic_chapter":
            return self.download_from_comic_chapter(url)
        if page_type == "catalog":
            return self.download_from_catalog(url)
        return self.download_from_chapter(url)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="下载网页小说到本地文件夹。")
    parser.add_argument("url", help="小说目录页或章节页链接")
    parser.add_argument(
        "-o",
        "--output",
        default="downloads",
        help="输出目录，默认为 downloads",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.05,
        help="章节请求间隔秒数，默认 0.05",
    )
    parser.add_argument(
        "--book-workers",
        type=int,
        default=2,
        help="分类下载时并行下载的小说数量，默认 2",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    downloader = NovelDownloader(output_dir=Path(args.output), delay=args.delay, book_workers=args.book_workers)

    try:
        merged_path = downloader.download(args.url)
    except requests.HTTPError as exc:
        print(f"请求失败: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"下载失败: {exc}")
        return 1

    print(f"\n完成，已合并输出到: {merged_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
