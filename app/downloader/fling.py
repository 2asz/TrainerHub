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
import unicodedata
import urllib.parse

from bs4 import BeautifulSoup
from packaging.version import Version

from .base import TrainerDownloader


class FlingTrainerDownloader(TrainerDownloader):
    SOURCE = "风灵月影"
    BASE_URL = "https://flingtrainer.com"
    # 下载域白名单（子类自带，新增源 = 新子类自带域名即可）
    ALLOWED_HOSTS = ("flingtrainer.com",)
    # 站内标题为英文：中文名游戏需要英文名重试（调用方据此注入英文名提供者）
    NEEDS_ENGLISH_NAME = True

    # 英文功能词（停用词）：仅靠这些词命中视为无关（"Some Random Game Of"
    # 不能因为标题里有 "of" 就保留——2026-09-13 审查 P2-1 实测反例）。
    # 只收功能词，不收 edition/remake 这类区分作品版本的内容词
    _STOPWORDS = {"of", "the", "from", "a", "an", "and", "in", "to", "for",
                  "with", "on", "at", "by", "or", "v", "vs"}

    # ---------- 搜索 ----------
    def search(self, query: str) -> list:
        """WordPress 站内搜索，返回 [{title, page_url}]（仅修改器页）。"""
        import urllib.parse
        q = urllib.parse.quote(query.strip())
        html = self.fetch_page(f"{self.BASE_URL}/?s={q}")
        soup = BeautifulSoup(html, "lxml")
        results = []
        for a in soup.select("a[href*='/trainer/']"):
            # urljoin：站点改用相对链接（"/trainer/x"）时也能命中，
            # 否则静默 0 结果与"真无结果"不可区分（best-effort 加固）
            href = urllib.parse.urljoin(self.BASE_URL + "/", a.get("href", ""))
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
        return self._filter_relevant(uniq, query)[:20]

    @staticmethod
    def _ascii_norm(s) -> str:
        """NFKD 去变音符 + casefold：Steam 官方英文名常带 ö/é/ü
        （God of War Ragnarök / Brütal Legend / Pokémon），而站点标题是
        ASCII（Ragnarok）——匹配前两侧都要归一，否则带变音符的名字会被
        判"非 ASCII"走整串分支而全灭（2026-09-13 审查 P2）。"""
        s = unicodedata.normalize("NFKD", s or "").casefold()
        return "".join(ch for ch in s if not unicodedata.combining(ch))

    @classmethod
    def _filter_relevant(cls, results, query):
        """相关性过滤：站点在"无匹配"时会返回一批推荐文章，必须滤掉
        （中文查询恒返回同一批英文结果，看起来"有结果但全无关"）。

        规则（2026-09-13 审查 P2-1 收紧）：
        - 查询含**非 ASCII**（中文/日文假名/韩文，先经 _ascii_norm 归一）→
          整串匹配：站点标题为英文，通常恒空，交由上层触发英文名重试（P2-2）
        - 英文查询 → 去停用词后取实词（≥2 字符）；要求命中数 ≥ min(2, 实词数)
          （单词查询命中该词即可；多词查询至少 2 个实词命中，避免仅凭
          "of/from" 之类的词或单词巧合成交）
        - 纯符号/单字符 token（如 "S.T.A.L.K.E.R. 2" 分词后全为单字符）→
          退化为去分隔符整串包含，不放行站点噪音（2026-09-13 审查 P3）"""
        q = cls._ascii_norm(query).strip()
        if not q:
            return results
        if any(ord(ch) > 127 for ch in q):
            return [r for r in results
                    if q in cls._ascii_norm(r.get("title"))]
        words = [w for w in re.split(r"[^a-z0-9]+", q)
                 if len(w) >= 2 and w not in cls._STOPWORDS]
        if not words:
            joined = re.sub(r"[^a-z0-9]", "", q)
            if not joined:
                return []
            return [r for r in results
                    if joined in re.sub(r"[^a-z0-9]", "",
                                        cls._ascii_norm(r.get("title")))]
        need = min(2, len(words))
        out = []
        for r in results:
            t = cls._ascii_norm(r.get("title"))
            hits = sum(1 for w in words if w in t)
            if hits >= need:
                out.append(r)
        return out

    # ---------- 下载解析 ----------

    def resolve_downloads(self, page_url: str) -> list:
        """解析下载页内所有 /downloads/<token>,, 链接，按版本号降序。
        同版本同名称的多条目视为镜像线路（保留第一条）。

        注意：官网的「Auto-Updating Version」自更新存根走的是
        download.php?title_id=... 链接，不含 /downloads/ 子串，这里
        **解析不到**（有意为之——它是需联网自更新的存根 exe，sha256 每次
        自更新都会变，不适合入库）。检测与提示见 latest_autoupdate_link()。
        """
        html = self.fetch_page(page_url)
        soup = BeautifulSoup(html, "lxml")
        entries = []
        for a in soup.select("a[href*='/downloads/']"):
            # 同上：urljoin 兼容相对链接改版
            href = urllib.parse.urljoin(self.BASE_URL + "/", a.get("href", ""))
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

    def latest_autoupdate_link(self, page_url: str) -> str | None:
        """检测下载页是否提供「Auto-Updating Version」自更新版，返回其
        下载链接（download.php?title_id=...）；没有返回 None。

        自更新版是通用存根 exe（所有游戏都精确 132KB），安装后联网自更新：
        - 选项数通常比独立版更多、更新更及时；
        - 但 sha256 每次自更新都会变、离线不可用——因此**只把链接交给用户**，
          不自动入库（入库会破坏「sha256 记录 + 版本比对」的更新检查机制）。
        """
        try:
            html = self.fetch_page(page_url)
        except Exception:
            return None
        soup = BeautifulSoup(html, "lxml")
        import urllib.parse
        # 策略一：链接文本/标题带 LatestVersion
        for a in soup.find_all("a", href=re.compile(r"download\.php\?")):
            blob = (a.get_text(" ", strip=True) + " " + (a.get("title") or ""))
            if "latestversion" in blob.lower():
                return urllib.parse.urljoin(page_url, a.get("href", ""))
        # 策略二：页面存在 Auto-Updating 区块标题时，取页面上第一个
        # download.php 链接（该站点只有自更新版走这个入口）
        if soup.find(string=re.compile(r"Auto-Updating", re.I)):
            for a in soup.find_all("a", href=re.compile(r"download\.php\?")):
                return urllib.parse.urljoin(page_url, a.get("href", ""))
        return None

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
