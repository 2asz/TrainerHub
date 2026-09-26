"""小幸修改器官网适配器（xiaoxingjie.com，小幸软件工作室）。

实测结构（2026-09）：
- 首页列表：https://www.xiaoxingjie.com/ ，条目链接为 /archives/<id>.html，
  标题形如「赛博朋克2077 多功能修改器 V2.6.2」（站点仅十余个条目，无分页）
- 详情页：下载地址区块含 MediaFire 分享链接（href 直接可见，免登录）：
    https://www.mediafire.com/file/<key>/<文件名>.zip/file
  （百度网盘渠道无法程序化下载，忽略；部分条目的腾讯微云/OneDrive
  标注是站长填错、与百度/MediaFire 重复，以链接域名为准）
- MediaFire 分享页 HTML 内含 download*.mediafire.com 直链，标准解析即可

best-effort：个人站与 MediaFire 页面改版都可能失效；MediaFire 国内直连
可能不稳，失败时 UI 降级为复制链接手动下载。
"""
import re
import urllib.parse

from bs4 import BeautifulSoup

from .base import TrainerDownloader


class XiaoXingDownloader(TrainerDownloader):
    SOURCE = "小幸"
    BASE_URL = "https://www.xiaoxingjie.com"
    # 下载域白名单（子类自带）：详情页 + MediaFire 分享页/直链域
    ALLOWED_HOSTS = ("xiaoxingjie.com", "mediafire.com")

    # ---------- 搜索 ----------
    # 英文名 → 官网标题用词：官网标题只有中文（"God of War" 这类 Steam
    # 英文名直接子串匹配不到）；仅覆盖官网现有条目，未收录的英文名
    # 返回空（属已知边界），
    # 因此别名表不全也不影响可用性（2026-09-13 用户实测反馈）
    _ALIASES = {
        "god of war": "战神",
        "black myth": "黑神话", "black myth wukong": "黑神话",
        "wukong": "黑神话",
        "cyberpunk 2077": "赛博朋克", "cyberpunk": "赛博朋克",
        "palworld": "幻兽帕鲁",
        "baldur's gate 3": "博德之门", "baldurs gate 3": "博德之门",
        "baldurs gate": "博德之门",
        "starfield": "星空",
        "afterimage": "心渊梦境",
        "wo long": "卧龙",
        "hogwarts legacy": "霍格沃茨", "hogwarts": "霍格沃茨",
        "xuan-yuan sword": "轩辕剑", "xuan yuan sword": "轩辕剑",
        "fate seeker": "天命奇御",
    }

    def search(self, query: str) -> list:
        """WordPress 站内搜索（/?s=）：服务器端匹配，能搜到首页未列出的
        条目（实测「战神4」不在首页列表但站内搜索命中——2026-09-13 用户
        实测反馈后从"本地匹配首页"改为站内搜索）。
        站内搜索只认中文：英文名先经别名表转中文再搜一次；别名表仅覆盖
        官网现有条目，未收录的英文名返回空（站点无英文名数据，属已知边界）。
        返回 [{title, page_url}]。"""
        q = (query or "").strip()
        if not q:
            return self._all_entries()[:40]
        results = self._site_search(q)
        if not results:
            key = q.casefold()
            alias = self._ALIASES.get(key) or self._ALIASES.get(key.replace(" ", ""))
            if alias:
                results = self._site_search(alias)
        return results[:20]

    @staticmethod
    def _parse_entry_links(soup) -> list:
        """主列表条目（article > h2.entry-title > a）：侧边栏"近期文章"
        等小部件链接被选择器天然排除。"""
        out, seen = [], set()
        for a in soup.select("article h2.entry-title a, h2.entry-title a"):
            href = urllib.parse.urljoin(
                XiaoXingDownloader.BASE_URL + "/", a.get("href", ""))
            if not re.search(r"/archives/\d+\.html$", href) or href in seen:
                continue
            title = a.get_text(" ", strip=True)
            if not title or "修改器" not in title:
                continue
            seen.add(href)
            out.append({"title": title, "page_url": href})
        return out

    def _site_search(self, q: str) -> list:
        """站内搜索结果页（首页即第 1 页）。"""
        url = (f"{self.BASE_URL}/?s={urllib.parse.quote(q)}"
               if q else self.BASE_URL + "/")
        html = self.fetch_page(url)
        return self._parse_entry_links(BeautifulSoup(html, "lxml"))

    def _all_entries(self) -> list:
        """全量条目：站内搜索「修改器」按页枚举（首页只列最近 16 条，
        实测站点 30+ 个条目——2026-09-13 用户指正「archives/2.html 才像
        全部」后改为翻页取全量；上限 6 页防御）。"""
        out, seen = [], set()
        for page in range(1, 7):
            url = (f"{self.BASE_URL}/?s={urllib.parse.quote('修改器')}"
                   + (f"&paged={page}" if page > 1 else ""))
            try:
                html = self.fetch_page(url)
            except Exception:
                break
            new = [e for e in self._parse_entry_links(BeautifulSoup(html, "lxml"))
                   if e["page_url"] not in seen]
            if not new:
                break
            for e in new:
                seen.add(e["page_url"])
                out.append(e)
        return out

    # ---------- 下载解析 ----------
    def resolve_downloads(self, page_url: str) -> list:
        """详情页 → MediaFire 分享链接 → 直链。
        返回 [{url, version, name}]（url 为可直接下载的直链）。
        全部解析失败返回 []（UI 提示复制详情页链接手动下载）。"""
        html = self.fetch_page(page_url)
        soup = BeautifulSoup(html, "lxml")
        out = []
        seen_share = set()
        for a in soup.find_all("a",
                               href=re.compile(r"mediafire\.com/file/", re.I)):
            share = (a.get("href") or "").strip()
            if not share or share in seen_share:
                continue
            seen_share.add(share)
            # 分享 URL 倒数第二段即文件名：
            # .../file/<key>/<文件名>.zip/file
            fname = urllib.parse.unquote(share.rstrip("/").split("/")[-2])
            version = (self._extract_version(a.get_text(" ", strip=True))
                       or self._extract_version(fname))
            direct = self._mediafire_direct(share)
            if not direct:
                continue
            out.append({"url": direct, "version": version, "name": fname})
        # 同文件多渠道重复时按 (version, name) 去重
        seen_vn, uniq = set(), []
        for e in out:
            key = (e["version"], e["name"])
            if key in seen_vn:
                continue
            seen_vn.add(key)
            uniq.append(e)
        uniq.sort(key=lambda e: self._version_key(e["version"]), reverse=True)
        return uniq

    # 手动渠道只保留网盘域：详情页还有微博/主题页脚/图片 CDN 等链接，
    # 不按域名过滤会混进一堆与下载无关的条目（2026-09-13 实测）
    _MANUAL_HOSTS = ("pan.baidu.com", "mediafire.com", "pan.quark.cn",
                     "alipan.com", "aliyundrive.com", "pan.xunlei.com",
                     "share.weiyun.com", "123pan.com", "lanzou",
                     "cloud.189.cn", "caiyun.139.com")

    def manual_links(self, page_url: str) -> list:
        """详情页里的**手动下载渠道**（自动直链失效时的兜底）：
        提取详情页的网盘链接（百度网盘/MediaFire 等，仅可复制，不能自动
        下载）。返回 [{name, url}]。仅当 resolve_downloads 为空时由调用方
        使用。"""
        try:
            html = self.fetch_page(page_url)
        except Exception:
            return []
        soup = BeautifulSoup(html, "lxml")
        out, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            if not href.startswith("http") or self.BASE_URL in href:
                continue
            host = (urllib.parse.urlparse(href).netloc or "").lower()
            if not any(host == h or host.endswith("." + h)
                       for h in self._MANUAL_HOSTS):
                continue
            if href in seen:
                continue
            seen.add(href)
            label = a.get_text(" ", strip=True) or href
            out.append({"name": f"{label}（手动下载）", "url": href})
        return out[:8]

    def _mediafire_direct(self, share_url: str) -> str | None:
        """MediaFire 分享页 → download*.mediafire.com 直链。

        MediaFire 前置 Cloudflare 会把 requests 拦成 403（实测三种 Referer
        组合均 403），改用系统 curl.exe 取页面（实测 200；Windows 10 1803+
        自带）。仍取不到返回 None（best-effort，UI 降级为复制链接）。"""
        html = self._fetch_via_curl(share_url)
        if not html:
            return None
        soup = BeautifulSoup(html, "lxml")
        a = soup.find(
            "a", href=re.compile(r"^https://download[^\"']+\.mediafire\.com/",
                                 re.I))
        if a is None:
            # MediaFire 自家的 Error 页 = 文件已被删除（版权清理常见）：
            # 留痕便于区分"被删"与"页面改版"
            if "MediaFire.com - Error" in html:
                from .. import audit
                audit.info(f"MediaFire 文件可能已被删除: {share_url}")
            return None
        return (a.get("href") or "").strip() or None

    def _fetch_via_curl(self, url: str) -> str:
        """用系统 curl.exe 取页面（绕 Cloudflare 对 requests 的 403）。
        URL 先过本源白名单（与 fetch_page 同口径，无 shell 拼接）。

        必须**带全套浏览器头 + 来源页 Referer**：实测仅 UA 是 403
        （Cloudflare 按 Accept/sec-ch-ua 等头的缺失判 bot），补齐后 200
        （2026-09-26 用户实测"浏览器能下、程序内不能"的根因）。"""
        import os
        import subprocess
        from pathlib import Path

        if not self.url_allowed(url):
            return ""
        curl = (Path(os.environ.get("SystemRoot") or r"C:\Windows")
                / "System32" / "curl.exe")
        if not curl.is_file():
            return ""
        from .base import BROWSER_HEADERS
        argv = [str(curl), "-s", "-w", "\n%{http_code}", "--noproxy", "*",
                "--max-time", "25"]
        for k, v in BROWSER_HEADERS.items():
            argv += ["-H", f"{k}: {v}"]
        # Referer 用小幸详情页域名：模拟"从官网点过来"的跳转来源
        argv += ["-H", f"Referer: {self.BASE_URL}/", url]
        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=45,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if proc.returncode != 0:
            return ""
        out = (proc.stdout or b"").decode("utf-8", "replace")
        body, _, code = out.rpartition("\n")   # 尾行是 curl -w 的状态码
        code = code.strip()
        if code in ("403", "429"):
            # Cloudflare 限流/拦截——与"文件被删"区分留痕，避免误判
            from .. import audit
            audit.info(f"MediaFire 页面被 Cloudflare 拦截（HTTP {code}）")
            return ""
        return body

    # ---------- 版本提取（与 fling 同思路，适配小幸标题格式）----------
    @staticmethod
    def _extract_version(text: str) -> str:
        """标题/文件名里的版本：V2.6.2 → "2.6.2"；V1.7.0（2024/03/01）
        之类取首个点分版本。"""
        m = re.search(r"[Vv]\s*(\d+(?:\.\d+){0,3})", text or "")
        return m.group(1) if m else ""

    @staticmethod
    def _version_key(version: str):
        from packaging.version import Version
        try:
            return Version(version or "0")
        except Exception:
            return Version("0")
