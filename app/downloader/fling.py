"""风灵月影官网适配器（实测版）。

实测结构（2026-08）：
- 搜索页：https://flingtrainer.com/?s=<游戏名>，结果含 a[href*='/trainer/<游戏>-trainers/']
- 下载页：https://flingtrainer.com/trainer/<游戏>-trainers/
  - 下载链接：<a href="https://flingtrainer.com/downloads/<token>,,"
               title="<游戏名>.v<版本号>.Plus.<n>.Trainer-FLiNG" class="attachment-link…">
  - 该链接 302 → 同域名 /download-trainer.php?path=… → 直连 exe（Content-Disposition 带文件名）
- 全流程同域名（flingtrainer.com），通过下载域白名单校验。

best-effort：站点改版/加防护后可能失效，UI 会提示并降级为复制链接。
"""
import re

from bs4 import BeautifulSoup
from packaging.version import Version

from .base import TrainerDownloader


class FlingTrainerDownloader(TrainerDownloader):
    SOURCE = "风灵月影"
    BASE_URL = "https://flingtrainer.com"

    # ---------- 搜索 ----------
    def search(self, query: str) -> list:
        """WordPress 站内搜索，返回 [{title, page_url}]（仅修改器页）。"""
        import urllib.parse
        q = urllib.parse.quote(query.strip())
        html = self._dl.fetch_page(f"{self.BASE_URL}/?s={q}")
        soup = BeautifulSoup(html, "lxml")
        results = []
        for a in soup.select("a[href*='/trainer/']"):
            href = a.get("href", "")
            title = a.get_text(" ", strip=True)
            # 只保留修改器页面，排除分类页/锚点/搜索链接与空标题
            if not title or "/category/" in href or "?s=" in href or "#" in href:
                continue
            if href.startswith(self.BASE_URL + "/trainer/"):
                results.append({"title": title, "page_url": href})
        seen, uniq = set(), []
        for r in results:
            if r["page_url"] not in seen:
                seen.add(r["page_url"])
                uniq.append(r)
        return uniq[:20]

    # ---------- 下载解析 ----------
    def resolve_downloads(self, page_url: str) -> list:
        """解析下载页内所有 /downloads/<token>,, 链接，按版本号降序。
        同版本同名称的多条目视为镜像线路（保留第一条）。"""
        html = self._dl.fetch_page(page_url)
        soup = BeautifulSoup(html, "lxml")
        entries = []
        for a in soup.select("a[href*='/downloads/']"):
            href = a.get("href", "")
            if not href.startswith(self.BASE_URL + "/downloads/"):
                continue
            title = a.get("title", "") or a.get_text(" ", strip=True) or ""
            version = self._extract_version(title)
            fname = title.strip() or ""
            entries.append({"url": href, "version": version,
                            "name": fname or href.rsplit("/", 1)[-1].strip(",")})

        # 去重：先按 URL（页面重复链接），再按 (version, name)——
        # 风灵同版本常挂多个镜像 token，不去重会在下拉框里出现多条一模一样的版本
        seen_url, uniq = set(), []
        for e in entries:
            if e["url"] in seen_url:
                continue
            seen_url.add(e["url"])
            uniq.append(e)
        seen_vn, out = set(), []
        for e in uniq:
            key = (e["version"], e["name"])
            if key in seen_vn:
                continue
            seen_vn.add(key)
            out.append(e)
        out.sort(key=lambda e: self._version_key(e["version"]), reverse=True)
        return out[:8]

    @staticmethod
    def _extract_version(title: str) -> str:
        """提取版本：优先 8 位日期（v20250130）；其次版本区间（v1.0-v1.2 →
        "1.0-1.2"，保留区间避免多条目都显示 v1.0）；再退单点分/整数版本。"""
        t = title or ""
        m = re.search(r"v(\d{8})", t)
        if m:
            return m.group(1)
        m = re.search(r"v(\d+(?:\.\d+){0,3})-v?(\d+(?:\.\d+){0,3})", t)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
        m = re.search(r"v(\d+(?:\.\d+){0,3})", t)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _version_key(version: str):
        """排序键：版本区间（如 1.0-1.2）取上限参与比较（PEP440 不认区间）。"""
        v = (version or "").split("-")[-1] if version else "0"
        try:
            return Version(v if v else "0")
        except Exception:
            return Version("0")
